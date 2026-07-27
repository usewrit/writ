"""
PersonaService — encryption, secret resolution, TOTP generation, warm-session
persistence, and server-side email-OTP reading for Personas.

Design notes
------------
- All secret material reuses the shared Fernet cipher (security.encryption.SecretEncryption).
  - Login credentials are stored Fernet(JSON) (NO gzip) to match the existing
    AutomationWorkflow.credentials_encrypted convention so the agent's
    {{secret:key}} substitution consumes them unchanged.
  - The warm auth session reuses the gzip+base64+Fernet framing from
    SessionStateService (so inject/extract_session_state works identically).
- 2FA codes are minted SERVER-SIDE at the /api/personas/{id}/otp callback
  (TOTP via pyotp, or by reading the connected mailbox). The raw TOTP seed and
  mail OAuth tokens NEVER leave the backend — the agent only ever receives the
  final 6-digit code or magic-link. This is what makes 2FA structurally a
  coordinator concern (a bare agent has nothing to mint the code for it).
"""
import base64
import gzip
import json
import logging
import re
import socket
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from security.encryption import SecretEncryption
from security.validation import InputValidator
from services.session_state_service import SessionStateService
from models.persona import Persona
from models.mail_connection import MailConnection

logger = logging.getLogger(__name__)

# OTP dispatch-token lifetime. Minted at dispatch time but only presented later,
# after a real login + navigation, so the window must cover a full run (a 2-minute
# window expired before legitimate use and broke email/SMS-OTP runs). 15 minutes
# is the original run-safe window; replay is prevented by the single-use Redis jti
# marker below (the token is burned on first consume), so the property here is
# "single-use within 15 min".
_OTP_TOKEN_TTL_SECONDS = 900
# Redis key prefix for spent OTP-token jtis (single-use enforcement).
_OTP_JTI_SEEN_PREFIX = "otp_jti_seen:"

# Default OTP message regexes (overridable per-persona via otp_extract_config)
_DEFAULT_CODE_RE = r"\b(\d{6,8})\b"
_DEFAULT_LINK_RE = r"https?://[^\s\"'<>]+"
_DEFAULT_OTP_MAX_AGE_SECONDS = 300
# Tolerance for agent/mail-server clock drift when filtering "received after submit".
_OTP_CLOCK_SKEW_SECONDS = 120


class PersonaError(Exception):
    """Raised when a persona cannot supply the requested credential/OTP."""


class PersonaService:

    # ----------------------------------------------------------------- crypto
    # Every persona blob is sealed/read with the global key.
    @staticmethod
    def encrypt(value: str) -> str:
        return SecretEncryption.encrypt_secret(value)

    @staticmethod
    def decrypt(value: str) -> str:
        return SecretEncryption.decrypt_secret(value)

    @staticmethod
    def encrypt_json(data: Dict[str, Any]) -> str:
        """Fernet(JSON) — matches AutomationWorkflow.credentials_encrypted framing."""
        return SecretEncryption.encrypt_secret(json.dumps(data))

    @staticmethod
    def decrypt_json(blob: str) -> Dict[str, Any]:
        return json.loads(SecretEncryption.decrypt_secret(blob))

    @staticmethod
    def _serialize_session(auth_session: dict) -> str:
        """gzip+base64+Fernet — identical framing to SessionStateService."""
        compressed = base64.b64encode(gzip.compress(json.dumps(auth_session).encode())).decode()
        return SecretEncryption.encrypt_secret(compressed)

    @staticmethod
    def _deserialize_session(blob: str) -> dict:
        compressed = SecretEncryption.decrypt_secret(blob)
        return json.loads(gzip.decompress(base64.b64decode(compressed)).decode())

    # ------------------------------------------------------------- login creds
    @staticmethod
    def resolve_login_credentials(persona: Persona) -> Dict[str, str]:
        """Decrypt the persona's login identity into a flat credentials dict.

        Keys land under 'username'/'password' (and any extra fields stored in
        credentials_encrypted), so existing {{secret:username}} / {{secret:password}}
        fill steps consume them with no agent change.
        """
        creds: Dict[str, str] = {}
        if persona.login_username:
            creds["username"] = persona.login_username
        if persona.credentials_encrypted:
            try:
                extra = PersonaService.decrypt_json(persona.credentials_encrypted)
                if isinstance(extra, dict):
                    creds.update({k: v for k, v in extra.items() if isinstance(v, str)})
            except Exception as e:  # pragma: no cover - corrupted/rotated key
                logger.error("Persona %s credential decrypt failed: %s", persona.id, e)
                raise PersonaError("Persona credentials could not be decrypted")
        return creds

    # ----------------------------------------------------------- BYO proxy
    # Default no-proxy bypass list applied when a persona's stored proxy config
    # omits one. Mirrors the shape the Python agent already validates/applies
    # (automation_engine.py: pops credentials['__proxy__'], reads .server, sets
    # the per-run PROXY_SERVER). Localhost/link-local stay direct so health
    # checks and the agent's own callbacks never tunnel through the proxy.
    _DEFAULT_PROXY_BYPASS = "0.0.0.0/1,128.0.0.0/1,localhost,[::1]"

    @staticmethod
    def resolve_proxy(persona: Persona) -> Optional[Dict[str, Any]]:
        """Decrypt a persona's BYO residential proxy config, or None.

        Returns the {server,username,password,bypass} dict that the agent applies
        as the run's browser egress ONLY when the persona has both:
          - a stored proxy_config_encrypted blob, AND
          - proxy_lawful_use_ack_at set (the owner acknowledged lawful use).
        An unacknowledged proxy is treated as absent — we never egress through a
        proxy the owner hasn't affirmed they may lawfully use.

        This is ONLY a resolver; whether the resolved proxy is actually injected
        for a given run is decided by the money-safe precedence in
        build_execute_workflow_msg (never override a revenue-generating creator-IP
        relay).
        """
        if not (persona.proxy_config_encrypted and persona.proxy_lawful_use_ack_at):
            return None
        try:
            cfg = PersonaService.decrypt_json(persona.proxy_config_encrypted)
        except Exception as e:  # pragma: no cover - corrupted/rotated key
            logger.error("Persona %s proxy decrypt failed: %s", persona.id, e)
            return None
        if not isinstance(cfg, dict) or not cfg.get("server"):
            return None
        if not cfg.get("bypass"):
            cfg["bypass"] = PersonaService._DEFAULT_PROXY_BYPASS
        return cfg

    @staticmethod
    def linked_secret_refs(persona: Persona) -> Dict[str, str]:
        """Return {credential_field: vault_secret_name} for any persona field
        linked to a vault secret (stored as "{{vault:name}}" or
        "{{vault:name.subfield}}"). The reported name is the BASE secret name
        (subfield stripped), so a paired credential reports the same name for
        both 'username' and 'password'.

        Exposes only the secret NAME, never the value — safe for API responses so
        the editor can show/pre-select which secret a field is linked to.
        """
        import re
        pat = re.compile(r"\{\{vault:([a-zA-Z_][a-zA-Z0-9_.-]*)\}\}")

        def base_name(ref: str) -> str:
            # "shopify_login.password" -> "shopify_login"; "stripe_key" -> "stripe_key"
            return ref.rsplit(".", 1)[0] if "." in ref else ref

        refs: Dict[str, str] = {}
        # The username lives on the plaintext column, not in credentials_encrypted.
        if persona.login_username:
            m = pat.search(persona.login_username)
            if m:
                refs["username"] = base_name(m.group(1))
        if persona.credentials_encrypted:
            try:
                creds = PersonaService.decrypt_json(persona.credentials_encrypted)
                if isinstance(creds, dict):
                    for field, value in creds.items():
                        if isinstance(value, str):
                            m = pat.search(value)
                            if m:
                                refs[field] = base_name(m.group(1))
            except Exception:
                pass
        return refs

    # -------------------------------------------------------------------- 2FA
    @staticmethod
    def generate_totp(persona: Persona) -> str:
        """Generate the current TOTP code from the persona's stored seed."""
        if not persona.totp_seed_encrypted:
            raise PersonaError("Persona has no TOTP seed configured")
        import pyotp  # local import: only needed on the OTP-mint path
        seed = SecretEncryption.decrypt_secret(persona.totp_seed_encrypted)
        totp = pyotp.TOTP(
            seed,
            digits=persona.totp_digits or 6,
            interval=persona.totp_period_seconds or 30,
            digest=_totp_digest(persona.totp_algorithm),
        )
        return totp.now()

    @staticmethod
    async def mint_otp(db: AsyncSession, persona: Persona, since_ts: Optional[float] = None) -> Dict[str, str]:
        """Return the live 2FA value for a persona.

        Returns {"type": "code", "value": "123456"} or
                {"type": "magic_link", "value": "https://..."}.
        Raises PersonaError if unavailable.
        """
        method = (persona.twofa_method or "none").lower()
        if method == "totp":
            return {"type": "code", "value": PersonaService.generate_totp(persona)}
        if method == "email_otp":
            return await PersonaService._read_email_otp(db, persona, since_ts=since_ts)
        if method == "sms":
            return await PersonaService._read_sms_otp(db, persona, since_ts=since_ts)
        raise PersonaError(f"Persona 2FA method '{method}' cannot mint an OTP")

    # ---------------------------------------------------------- email-OTP read
    @staticmethod
    async def _read_email_otp(db: AsyncSession, persona: Persona, since_ts: Optional[float] = None) -> Dict[str, str]:
        cfg = persona.otp_extract_config or {}
        # Scope sender to the target site's domain by default (kills most ambiguity).
        from_filter = cfg.get("from_filter") or persona.target_domain
        subject_regex = cfg.get("subject_regex")
        max_age = int(cfg.get("max_age_seconds") or _DEFAULT_OTP_MAX_AGE_SECONDS)

        if (persona.email_otp_mode or "oauth_mailbox") == "relay":
            # Self-host has no inbound relay ingress; this stays unsupported.
            msg = await PersonaService._fetch_relay_message(db, persona, max_age, since_ts=since_ts)
        else:
            # Connected mailbox (IMAP): read the newest matching code directly from
            # the user's own IMAP inbox. This is the ONLY email-OTP mode self-host
            # supports (no OAuth mailbox, no relay).
            if not persona.mail_connection_id:
                raise PersonaError("Email-OTP mailbox is not connected — attach an IMAP mailbox first")
            conn = await db.get(MailConnection, persona.mail_connection_id)
            if not conn:
                raise PersonaError("Connected mailbox not found")
            msg = await PersonaService._fetch_mailbox_message(
                db, conn, from_filter, subject_regex, max_age, since_ts=since_ts,
            )

        if not msg:
            raise PersonaError("No matching OTP email found in the time window")

        result = _extract_otp(
            subject=msg.get("subject", ""),
            text=(msg.get("text") or _strip_html(msg.get("html"))),
            html=msg.get("html"),
            cfg=cfg,
        )
        if not result:
            raise PersonaError("OTP email found but no code/link could be extracted")
        return result

    @staticmethod
    async def _fetch_mailbox_message(
        db: AsyncSession, conn: MailConnection,
        from_filter: Optional[str], subject_regex: Optional[str], max_age: int,
        since_ts: Optional[float] = None, expected_tenant_id=None,
    ) -> Optional[dict]:
        # This helper is retained for the IMAP test path; the OAuth-mailbox mint
        # path is disabled in _read_email_otp.
        if conn.provider == "imap":
            return await PersonaService._imap_latest(conn, from_filter, max_age, since_ts)
        access_token = await PersonaService._get_fresh_access_token(db, conn, expected_tenant_id=expected_tenant_id)
        if conn.provider == "google":
            return await PersonaService._gmail_latest(access_token, from_filter, max_age, since_ts)
        if conn.provider == "microsoft":
            return await PersonaService._graph_latest(access_token, from_filter, max_age, since_ts)
        raise PersonaError(f"Unsupported mail provider '{conn.provider}'")

    @staticmethod
    def _validate_imap_host(host: str, use_ssl: bool) -> None:
        """SSRF + transport guard for IMAP, run BEFORE any imaplib connect.

        - Resolves the host and rejects it if ANY resolved address is
          private/loopback/link-local/reserved (reusing the shared
          InputValidator._is_private_ip). Validating every resolved address
          (not just the first) blocks the multi-record SSRF trick.
        - Refuses plaintext IMAP (use_ssl=False): only a loopback target could
          legitimately skip TLS, and loopback is already rejected above, so a
          non-SSL connection is always refused.

        Raises PersonaError if the host is unsafe.
        """
        if not host or not str(host).strip():
            raise PersonaError("IMAP host is required")
        host = str(host).strip()
        # Refuse plaintext IMAP4 (no SSL/STARTTLS) to ANY host. The only safe
        # plaintext target would be loopback, which the SSRF check rejects anyway.
        if not use_ssl:
            raise PersonaError("Plaintext IMAP is not allowed; enable SSL/TLS")
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            raise PersonaError("IMAP host could not be resolved")
        except Exception as e:
            raise PersonaError(f"IMAP host resolution failed: {e}")

        resolved = False
        for _family, _type, _proto, _canon, sockaddr in infos:
            resolved = True
            ip_str = sockaddr[0]
            if InputValidator._is_private_ip(ip_str):
                raise PersonaError(
                    "IMAP host resolves to a private/internal address and is not allowed"
                )
        if not resolved:
            raise PersonaError("IMAP host could not be resolved")

    @staticmethod
    async def _imap_latest(conn: MailConnection, from_filter: Optional[str], max_age: int,
                           since_ts: Optional[float] = None) -> Optional[dict]:
        """Read the most recent matching message from an IMAP mailbox (BYO/app-password)."""
        import asyncio
        if not (conn.imap_host and conn.imap_username and conn.imap_password_encrypted):
            raise PersonaError("IMAP mailbox is not fully configured")
        password = SecretEncryption.decrypt_secret(conn.imap_password_encrypted)
        host = conn.imap_host
        port = conn.imap_port or (993 if conn.imap_use_ssl else 143)
        use_ssl = bool(conn.imap_use_ssl)
        username = conn.imap_username
        mailbox = conn.imap_mailbox or "INBOX"
        cutoff = (since_ts - _OTP_CLOCK_SKEW_SECONDS) if since_ts else None
        # SSRF + plaintext guard: validate BEFORE connecting.
        PersonaService._validate_imap_host(host, use_ssl)

        def _read() -> Optional[dict]:
            import imaplib
            import email as email_mod
            M = imaplib.IMAP4_SSL(host, port) if use_ssl else imaplib.IMAP4(host, port)
            try:
                M.login(username, password)
                M.select(mailbox, readonly=True)
                criteria = ["ALL"]
                if from_filter:
                    criteria = ["FROM", f'"{from_filter}"']
                typ, data = M.search(None, *criteria)
                if typ != "OK" or not data or not data[0]:
                    return None
                ids = data[0].split()
                # Inspect the few most recent messages, newest first
                for mid in reversed(ids[-5:]):
                    typ, msg_data = M.fetch(mid, "(RFC822)")
                    if typ != "OK" or not msg_data or not msg_data[0]:
                        continue
                    msg = email_mod.message_from_bytes(msg_data[0][1])
                    # Skip messages received before the login-submit time (stale codes).
                    if cutoff is not None:
                        try:
                            from email.utils import parsedate_to_datetime
                            d = parsedate_to_datetime(msg.get("Date"))
                            if d is not None and d.timestamp() < cutoff:
                                continue
                        except Exception:
                            pass
                    parts = _imap_message_parts(msg)
                    if parts and (parts.get("text") or parts.get("html")):
                        return parts
                return None
            finally:
                try:
                    M.logout()
                except Exception:
                    pass

        return await asyncio.to_thread(_read)

    @staticmethod
    async def test_imap_connection(host: str, port: Optional[int], use_ssl: bool,
                                   username: str, password: str, mailbox: Optional[str] = None) -> None:
        """Verify IMAP creds by logging in and opening the mailbox. Raises PersonaError on failure."""
        import asyncio
        eff_port = port or (993 if use_ssl else 143)
        box = mailbox or "INBOX"
        # SSRF + plaintext guard: validate BEFORE connecting (covers the
        # set_persona_imap validation path, which calls this).
        PersonaService._validate_imap_host(host, use_ssl)

        def _t():
            import imaplib
            M = imaplib.IMAP4_SSL(host, eff_port) if use_ssl else imaplib.IMAP4(host, eff_port)
            try:
                M.login(username, password)
                typ, _ = M.select(box, readonly=True)
                if typ != "OK":
                    raise PersonaError(f"Mailbox '{box}' could not be opened")
            finally:
                try:
                    M.logout()
                except Exception:
                    pass

        try:
            await asyncio.to_thread(_t)
        except PersonaError:
            raise
        except Exception as e:
            raise PersonaError(f"IMAP connection failed: {e}")

    @staticmethod
    async def _gmail_latest(access_token: str, from_filter: Optional[str], max_age: int,
                            since_ts: Optional[float] = None) -> Optional[dict]:
        # Narrow, time-bounded query to minimize data access.
        q_parts = ["newer_than:1d"]
        if from_filter:
            q_parts.append(f"from:{from_filter}")
        if since_ts:
            # Gmail accepts a unix timestamp for after: — only mail received post-submit.
            q_parts.append(f"after:{int(since_ts - _OTP_CLOCK_SKEW_SECONDS)}")
        q = " ".join(q_parts)
        headers = {"Authorization": f"Bearer {access_token}"}
        async with httpx.AsyncClient(timeout=15) as http:
            r = await http.get(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages",
                params={"q": q, "maxResults": 3}, headers=headers,
            )
            r.raise_for_status()
            msgs = (r.json() or {}).get("messages") or []
            if not msgs:
                return None
            mr = await http.get(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msgs[0]['id']}",
                params={"format": "full"}, headers=headers,
            )
            mr.raise_for_status()
            return _gmail_extract_parts(mr.json())

    @staticmethod
    async def _graph_latest(access_token: str, from_filter: Optional[str], max_age: int,
                            since_ts: Optional[float] = None) -> Optional[dict]:
        headers = {"Authorization": f"Bearer {access_token}"}
        # $orderby can't combine with $search, so fetch newest few and filter in code
        # (by received-after-submit + sender domain).
        params = {"$top": "5", "$orderby": "receivedDateTime desc",
                  "$select": "subject,body,from,receivedDateTime"}
        cutoff = (since_ts - _OTP_CLOCK_SKEW_SECONDS) if since_ts else None
        async with httpx.AsyncClient(timeout=15) as http:
            r = await http.get(
                "https://graph.microsoft.com/v1.0/me/messages",
                params=params, headers=headers,
            )
            r.raise_for_status()
            items = (r.json() or {}).get("value") or []
        for item in items:
            # Recency filter
            if cutoff is not None:
                rcvd = _parse_iso8601(item.get("receivedDateTime"))
                if rcvd is not None and rcvd < cutoff:
                    continue
            # Sender-domain filter
            if from_filter:
                addr = (((item.get("from") or {}).get("emailAddress") or {}).get("address") or "").lower()
                if from_filter.lower() not in addr:
                    continue
            body = item.get("body") or {}
            content = body.get("content") or ""
            subject = item.get("subject") or ""
            if (body.get("contentType") or "").lower() == "html":
                return {"subject": subject, "text": _strip_html(content), "html": content}
            return {"subject": subject, "text": content, "html": None}
        return None

    # ----------------------------------------------------------- sms-OTP read
    @staticmethod
    async def _read_sms_otp(db: AsyncSession, persona: Persona, since_ts: Optional[float] = None) -> Dict[str, str]:
        """Read an SMS-delivered OTP. SMS is relay-only: an external forwarder
        posts the inbound text to /api/relay/inbound keyed by the persona's
        relay_address (phone number / relay token); we read the newest one and
        extract the code with the same deterministic scorer used for email.
        """
        if not persona.relay_address:
            raise PersonaError("Persona SMS-OTP has no relay_address configured")
        cfg = persona.otp_extract_config or {}
        max_age = int(cfg.get("max_age_seconds") or _DEFAULT_OTP_MAX_AGE_SECONDS)
        msg = await PersonaService._fetch_relay_message(db, persona, max_age, since_ts=since_ts)
        if not msg:
            raise PersonaError("No matching OTP text message found in the time window")
        result = _extract_otp(
            subject=msg.get("subject", ""),
            text=(msg.get("text") or _strip_html(msg.get("html"))),
            html=msg.get("html"),
            cfg=cfg,
        )
        if not result:
            raise PersonaError("OTP text found but no code/link could be extracted")
        return result

    @staticmethod
    async def _fetch_relay_message(db: AsyncSession, persona: Persona, max_age: int,
                                   since_ts: Optional[float] = None) -> Optional[dict]:
        # No inbound-email/SMS relay store is configured, so relay-delivered OTPs
        # are unavailable. A persona with an email/SMS 2FA method must use a mailbox
        # provider (IMAP) or a TOTP seed instead.
        raise PersonaError("Mail relay not configured")

    @staticmethod
    async def _get_fresh_access_token(db: AsyncSession, conn: MailConnection, expected_tenant_id=None) -> str:
        """Return a valid access token, refreshing via the stored refresh token if needed."""
        now = datetime.now(timezone.utc)
        if conn.access_token_encrypted and conn.token_expires_at and conn.token_expires_at > now + timedelta(seconds=60):
            return SecretEncryption.decrypt_secret(conn.access_token_encrypted)
        if not conn.refresh_token_encrypted:
            raise PersonaError("Mailbox has no refresh token; reconnect required")
        refresh_token = SecretEncryption.decrypt_secret(conn.refresh_token_encrypted)
        # Lazy import to avoid a router<->service import cycle.
        _get_provider_credentials = PROVIDER_CONFIGS = None
        client_id, client_secret = await _get_provider_credentials(conn.provider, db)
        token_url = PROVIDER_CONFIGS[conn.provider]["token_url"]
        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.post(token_url, data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            })
            # A revoked/expired refresh token (Google returns 400 invalid_grant) is
            # a TERMINAL setup problem — the user must reconnect the mailbox. Surface
            # it as a clear PersonaError instead of a raw HTTP error that the OTP
            # poller would misread as "code not here yet" and retry for the full
            # deadline.
            if resp.status_code >= 400:
                detail = ""
                try:
                    detail = (resp.json() or {}).get("error") or ""
                except Exception:
                    detail = (resp.text or "")[:120]
                logger.warning(
                    "Mailbox token refresh failed for connection %s (provider=%s, status=%s, error=%s)",
                    conn.id, conn.provider, resp.status_code, detail,
                )
                raise PersonaError(
                    "Mailbox authorization expired or was revoked; reconnect required"
                )
            tok = resp.json()
        access_token = tok.get("access_token")
        if not access_token:
            raise PersonaError("Mailbox token refresh returned no access_token")
        conn.access_token_encrypted = SecretEncryption.encrypt_secret(access_token)
        expires_in = int(tok.get("expires_in") or 3600)
        conn.token_expires_at = now + timedelta(seconds=expires_in)
        if tok.get("refresh_token"):
            conn.refresh_token_encrypted = SecretEncryption.encrypt_secret(tok["refresh_token"])
        await db.commit()
        return access_token

    # --------------------------------------------------------- warm session
    @staticmethod
    async def save_session(db: AsyncSession, persona: Persona, auth_session: dict, ttl_seconds: Optional[int] = None) -> None:
        """Persist a returned auth_session onto the persona (identity-scoped, shared across workflows)."""
        persona.session_state_encrypted = PersonaService._serialize_session(auth_session)
        cookie_expiry = SessionStateService.compute_cookie_expiry(auth_session.get("cookies", []))
        now = datetime.now(timezone.utc)
        candidates = []
        if cookie_expiry:
            candidates.append(cookie_expiry)
        if ttl_seconds:
            candidates.append(now + timedelta(seconds=ttl_seconds))
        persona.earliest_cookie_expiry = cookie_expiry
        persona.expires_at = min(candidates) if candidates else None
        persona.validation_status = "valid"
        persona.last_login_at = now
        persona.last_used_at = now
        await db.commit()

    @staticmethod
    def load_session(persona: Persona) -> Optional[dict]:
        """Return the persona's warm auth_session if present and not expired."""
        if not persona.session_state_encrypted:
            return None
        if persona.expires_at and persona.expires_at <= datetime.now(timezone.utc):
            return None
        try:
            return PersonaService._deserialize_session(persona.session_state_encrypted)
        except Exception as e:  # pragma: no cover
            logger.error("Persona %s session decrypt failed: %s", persona.id, e)
            return None

    # ------------------------------------------------------------ utilities
    @staticmethod
    def domain_matches(persona: Persona, entry_url: Optional[str]) -> bool:
        """Soft host-suffix match between the persona's target_domain and a workflow URL."""
        if not persona.target_domain or not entry_url:
            return True  # nothing to compare -> don't block
        try:
            host = (urlparse(entry_url).hostname or "").lower()
        except Exception:
            return True
        pd = persona.target_domain.lower().lstrip(".")
        return bool(host) and (host == pd or host.endswith("." + pd))

    @staticmethod
    async def create_from_capture(db: AsyncSession, *, name: str,
                                  domain: Optional[str] = None, login_username: Optional[str] = None,
                                  creds: Optional[Dict[str, Any]] = None,
                                  auth_session: Optional[dict] = None,
                                  fingerprint: Optional[dict] = None) -> Persona:
        """Create a persona from a captured identity (login creds + optional warm session).

        Shared by from-workflow / from-task / from-ai-session and the create_persona
        automation action. Names are de-duplicated for the single owner.
        """
        base = (name or "Captured login").strip() or "Captured login"
        nm, i = base, 2
        while (await db.execute(
            select(Persona).where(Persona.name == nm)
        )).scalar_one_or_none():
            nm = f"{base} {i}"
            i += 1

        clean_creds = {k: v for k, v in (creds or {}).items() if isinstance(v, str) and v}
        p = Persona(
            name=nm, target_domain=domain,
            login_username=login_username,
            credentials_encrypted=(PersonaService.encrypt_json(clean_creds) if clean_creds else None),
            fingerprint=(fingerprint or (auth_session or {}).get("fingerprint")),
            twofa_method="none",
        )
        db.add(p)
        await db.flush()
        if auth_session:
            p.session_state_encrypted = PersonaService._serialize_session(auth_session)
            ce = SessionStateService.compute_cookie_expiry(auth_session.get("cookies", []))
            p.earliest_cookie_expiry = ce
            p.expires_at = ce
            p.validation_status = "valid"
            p.last_login_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(p)
        return p

    @staticmethod
    async def get_owned(db: AsyncSession, persona_id: int) -> Optional[Persona]:
        result = await db.execute(
            select(Persona).where(Persona.id == persona_id)
        )
        return result.scalar_one_or_none()

    # --------------------------------------------------- OTP-callback token
    # A short-lived encrypted token minted at dispatch and handed to the agent
    # in its task config. The agent presents it to POST /api/personas/{id}/otp;
    # the backend validates it (scope = persona_id + task_id + exp) before minting
    # the live code. The TOTP seed / mail tokens never leave here.
    @staticmethod
    def make_otp_token(persona_id: int, task_id=None, ttl_seconds: int = _OTP_TOKEN_TTL_SECONDS) -> str:
        # A random jti makes the token single-use: it is burned in Redis on the
        # first successful consume so a captured token cannot be replayed within
        # its (already short) validity window.
        payload = {
            "jti": uuid.uuid4().hex,
            "persona_id": int(persona_id),
            "task_id": str(task_id) if task_id is not None else None,
            "exp": (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).timestamp(),
        }
        return SecretEncryption.encrypt_secret(json.dumps(payload))

    @staticmethod
    def verify_otp_token(token: str) -> Dict[str, Any]:
        """Decrypt + validate an OTP dispatch token (signature/exp only).

        Single-use is NOT enforced here (this is a pure, side-effect-free check);
        callers that actually mint an OTP MUST call consume_otp_token() so the
        jti is burned exactly once.
        """
        try:
            data = json.loads(SecretEncryption.decrypt_secret(token))
        except Exception:
            raise PersonaError("Invalid OTP token")
        if float(data.get("exp", 0)) < datetime.now(timezone.utc).timestamp():
            raise PersonaError("OTP token expired")
        if not data.get("jti"):
            # Legacy tokens with no jti can't be made single-use; reject so a
            # missing marker can't silently become unlimited-replay.
            raise PersonaError("Invalid OTP token")
        return data

    @staticmethod
    async def consume_otp_token(token: str) -> Dict[str, Any]:
        """Validate AND atomically burn an OTP token's jti (single-use).

        Raises PersonaError if the token is invalid/expired OR has already been
        consumed. Use this on any path that actually mints a code.
        """
        data = PersonaService.verify_otp_token(token)
        jti = str(data.get("jti"))
        # TTL the marker to the token's remaining lifetime (>=1s) so spent jtis
        # expire on their own; never longer than the token could be valid.
        remaining = float(data.get("exp", 0)) - datetime.now(timezone.utc).timestamp()
        ttl = max(1, int(remaining) + _OTP_CLOCK_SKEW_SECONDS)
        try:
            from utils.redis_client import get_redis
            redis = get_redis()
            was_set = await redis.set(f"{_OTP_JTI_SEEN_PREFIX}{jti}", "1", ex=ttl, nx=True)
        except PersonaError:
            raise
        except Exception as e:
            # Fail CLOSED: if we can't prove the token is unused, don't mint.
            logger.error("OTP single-use check failed (redis): %s", e)
            raise PersonaError("OTP token could not be validated")
        if not was_set:
            raise PersonaError("OTP token already used")
        return data


def _totp_digest(algorithm: Optional[str]):
    import hashlib
    return {"SHA1": hashlib.sha1, "SHA256": hashlib.sha256, "SHA512": hashlib.sha512}.get(
        (algorithm or "SHA1").upper(), hashlib.sha1
    )


def _imap_message_parts(msg) -> dict:
    """Split an email.message.Message into {subject, text, html}."""
    subject = msg.get("Subject") or ""
    text_parts, html_parts = [], []

    def _decode(part):
        try:
            payload = part.get_payload(decode=True)
            if payload:
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, "ignore")
        except Exception:
            pass
        return None

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain":
                d = _decode(part)
                if d:
                    text_parts.append(d)
            elif ctype == "text/html":
                d = _decode(part)
                if d:
                    html_parts.append(d)
    else:
        d = _decode(msg)
        if d:
            (html_parts if msg.get_content_type() == "text/html" else text_parts).append(d)
    return {"subject": subject, "text": "\n".join(text_parts), "html": ("\n".join(html_parts) or None)}


def _gmail_extract_parts(message: dict) -> dict:
    """Split a Gmail API message into {subject, text, html}."""
    payload = message.get("payload") or {}
    headers = {h.get("name", "").lower(): h.get("value", "") for h in payload.get("headers", [])}
    subject = headers.get("subject", "")
    text_parts, html_parts = [], []

    def walk(p):
        if not p:
            return
        mime = p.get("mimeType", "")
        data = (p.get("body") or {}).get("data")
        if data:
            try:
                decoded = base64.urlsafe_b64decode(data + "===").decode("utf-8", "ignore")
            except Exception:
                decoded = ""
            if mime == "text/plain":
                text_parts.append(decoded)
            elif mime == "text/html":
                html_parts.append(decoded)
        for sub in p.get("parts") or []:
            walk(sub)

    walk(payload)
    text = "\n".join(text_parts) or (message.get("snippet") or "")
    return {"subject": subject, "text": text, "html": ("\n".join(html_parts) or None)}


# ===========================================================================
# Deterministic OTP extraction — NO AI.
# Scope the message hard (sender + recency + subject, done upstream), then score
# candidate tokens by shape + keyword proximity, excluding URLs/phones/years.
# ===========================================================================
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
_HREF_RE = re.compile(r'href=["\']?(https?://[^"\'>\s]+)', re.I)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+")
# Candidate code tokens: 4–9 digits (optionally grouped by one space/dash) OR a 5–8 char alnum.
_CODE_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])((?:\d[ \-]?){3,8}\d|[A-Za-z0-9]{5,8})(?![A-Za-z0-9])")

# Keywords near the code (multilingual, lowercased).
_OTP_KEYWORDS = (
    "code", "verification", "verify", "passcode", "one-time", "one time", "onetime", "otp",
    "security code", "login code", "log-in", "sign-in", "sign in", "signin", "authentication",
    "auth code", "confirm", "confirmation", "access code", "2fa", "two-factor", "two factor",
    "verification code", "pin", "verifica", "código", "codigo", "vérification", "code de",
    "bestätigung", "verifizierung", "verificación", "verificatie", "验证码", "認証", "確認コード",
    "رمز", "인증", "código de",
)
# Tokens near these are probably NOT the code.
_NEG_KEYWORDS = ("phone", "tel", "call", "mobile", "order", "invoice", "account number",
                 "zip", "postal", "fax", "ext", "card ending", "amount", "total")
_LINK_HINTS = ("verify", "verification", "confirm", "confirmation", "login", "log-in", "signin",
               "sign-in", "activate", "magic", "authenticate", "auth", "token", "validate", "access")


def _strip_html(html: Optional[str]) -> str:
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup
        return BeautifulSoup(html, "lxml").get_text("\n")
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)


def _extract_links(text: str, html: Optional[str]) -> list:
    links = []
    if html:
        links += _HREF_RE.findall(html)
    links += _URL_RE.findall(text or "")
    seen, out = set(), []
    for l in links:
        if l not in seen:
            seen.add(l)
            out.append(l)
    return out


def _pick_magic_link(links: list) -> Optional[str]:
    for l in links:
        if any(h in l.lower() for h in _LINK_HINTS):
            return l
    return None


def _parse_iso8601(value: Optional[str]) -> Optional[float]:
    """Parse an ISO8601 timestamp (e.g. Graph receivedDateTime) to epoch seconds."""
    if not value:
        return None
    try:
        from datetime import datetime as _dt
        return _dt.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _score_otp_candidate(raw: str, context: str, src: str, start: int, end: int, in_subject: bool):
    """Return (score, value) for a candidate token, or None to disqualify."""
    before = src[max(0, start - 2):start]
    after = src[end:end + 2]
    sep_count = len(re.findall(r"[ \-]", raw))

    # Phone/SSN/card shapes: 2+ internal separators, or a token that is part of a
    # longer separated run (continues with sep+digit, or is preceded by digit+sep).
    if sep_count >= 2:
        return None
    if re.match(r"[ \-]\d", after) or re.search(r"\d[ \-]$", before):
        return None
    # currency immediately before → an amount, not a code
    if any(sym in before for sym in ("$", "€", "£", "¥")):
        return None

    digits = re.sub(r"[^0-9A-Za-z]", "", raw)
    if not any(c.isdigit() for c in digits):
        return None
    L = len(digits)
    if L < 4 or L > 8:
        return None
    is_numeric = digits.isdigit()
    has_kw = any(k in context for k in _OTP_KEYWORDS)

    score = 0
    if is_numeric:
        score += {6: 25, 5: 18, 7: 18, 4: 12, 8: 12}.get(L, 0)
        # Bare year (1900–2099) with no code keyword nearby → not an OTP.
        if L == 4 and re.fullmatch(r"(19|20)\d{2}", digits) and not has_kw:
            return None
    elif 5 <= L <= 8:
        score += 15  # alphanumeric code (must contain a digit, checked above)
    else:
        return None

    if has_kw:
        score += 60
    if in_subject:
        score += 100
    # Only penalize phone/order/total/amount proximity when NO code keyword is near.
    if not has_kw and any(k in context for k in _NEG_KEYWORDS):
        score -= 80
    # Grouped into exactly two groups (e.g. "123 456") is a strong OTP signal.
    if sep_count == 1:
        score += 15
    # Standalone on its own line.
    line_start = src.rfind("\n", 0, start) + 1
    line_end = src.find("\n", end)
    if line_end == -1:
        line_end = len(src)
    if src[line_start:line_end].strip() == raw.strip():
        score += 25

    return (score, (digits if is_numeric else digits.upper()))


def _extract_otp(subject: str, text: str, html: Optional[str], cfg: dict) -> Optional[Dict[str, str]]:
    subject = subject or ""
    text = text or ""
    links = _extract_links(text, html)

    # 1) Explicit per-persona regex override — exact, deterministic, wins.
    #
    # The pattern is operator-supplied (otp_extract_config) and the haystack is an
    # INBOUND EMAIL BODY, i.e. attacker-influenced content arriving on a polling
    # loop. That is the classic stored-ReDoS shape, so it runs under the same caps
    # and hard timeout as every other user pattern; a timeout yields no match and
    # extraction falls through to the heuristics below.
    override = cfg.get("code_regex")
    if override:
        from security.validation import InputValidator

        for src in (subject, text):
            m = InputValidator.safe_regex_search(override, src)
            if m:
                return {"type": "code", "value": (m.group(1) if m.groups() else m.group(0))}

    # 2) Apple domain-bound format: "@example.com #123456"
    m = re.search(r"@[\w.-]+\s+#([A-Za-z0-9-]{4,10})", f"{subject}\n{text}")
    if m:
        return {"type": "code", "value": m.group(1)}

    prefer_link = cfg.get("prefer") == "magic_link"

    # 3) Scored code extraction (unless caller explicitly prefers a link)
    if not prefer_link:
        def _clean(s: str) -> str:
            s = _URL_RE.sub(" ", s)          # strip URLs (query-string digits)
            s = _EMAIL_RE.sub(" ", s)        # strip email addresses (local-part digits)
            return s

        best = None  # (score, value)
        for raw_src, in_subject in ((subject, True), (text, False)):
            src = _clean(raw_src)
            low = src.lower()
            for mt in _CODE_TOKEN_RE.finditer(src):
                tok = mt.group(1)
                s, e = mt.start(1), mt.end(1)
                # Keyword-proximity window. Widened (was ±18) so it catches the
                # very common SMS shape where the code comes FIRST and the keyword
                # trails it, e.g. "123456 is your verification code".
                context = low[max(0, s - 40):min(len(low), e + 40)]
                scored = _score_otp_candidate(tok, context, src, s, e, in_subject)
                if scored is None:
                    continue
                if best is None or scored[0] > best[0]:
                    best = scored
        if best and best[0] >= 45:
            return {"type": "code", "value": best[1]}

    # 4) Magic-link fallback
    link = _pick_magic_link(links)
    if link:
        return {"type": "magic_link", "value": link}
    return None
