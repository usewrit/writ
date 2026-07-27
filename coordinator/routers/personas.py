"""
Personas router — CRUD for authenticated identities + the agent OTP-mint callback.

Security:
- Management endpoints require auth (get_auth_context) AND the 'personas'
  feature flag (require_feature('personas')).
- Secret material (password, TOTP seed, proxy creds, mail tokens) is NEVER
  returned by any endpoint (mirrors the vault router rule).
- The agent OTP callback (POST /personas/{id}/otp) is authorized by a
  short-lived dispatch-minted token, NOT a user session.
"""
import logging
from typing import Optional, List
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from database import get_db
from security.dependencies import get_auth_context, AuthContext
from security.feature_gate import require_feature
from models.persona import Persona
from models.mail_connection import MailConnection
from services.persona_service import PersonaService, PersonaError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/personas", tags=["Personas"])

_FEATURE = Depends(require_feature("personas"))
# Self-host 2FA: TOTP, or a connected IMAP mailbox for email-OTP. No SMS and no
# forwarding relay — the coordinator has no inbound relay ingress.
TWOFA_METHODS = {"none", "totp", "email_otp"}
EMAIL_OTP_MODES = {"oauth_mailbox"}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class PersonaCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: Optional[str] = None
    target_domain: Optional[str] = Field(None, max_length=255)
    login_username: Optional[str] = Field(None, max_length=255)
    password: Optional[str] = Field(None, description="Write-only; stored encrypted")
    extra_login_fields: Optional[dict] = Field(None, description="Additional {{secret:key}} login values")
    twofa_method: str = Field("none")
    totp_seed: Optional[str] = Field(None, description="Write-only base32 TOTP secret")
    totp_digits: int = 6
    totp_period_seconds: int = 30
    totp_algorithm: str = "SHA1"
    email_otp_mode: Optional[str] = None
    mail_connection_id: Optional[int] = None
    relay_address: Optional[str] = Field(None, max_length=320)
    otp_extract_config: Optional[dict] = None
    fingerprint: Optional[dict] = None
    preferred_agent_id: Optional[str] = None
    # --- BYO residential proxy (write-only creds) + lawful-use warrant ---
    proxy_server: Optional[str] = Field(None, max_length=512, description="Proxy URL/host:port, e.g. http://host:port")
    proxy_username: Optional[str] = Field(None, max_length=255, description="Write-only; stored encrypted")
    proxy_password: Optional[str] = Field(None, description="Write-only; stored encrypted")
    proxy_lawful_use_ack: bool = Field(
        False,
        description="Required (true) when proxy_server is set: the owner acknowledges lawful use of this proxy.",
    )


class PersonaUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    description: Optional[str] = None
    target_domain: Optional[str] = None
    login_username: Optional[str] = None
    password: Optional[str] = None
    extra_login_fields: Optional[dict] = None
    twofa_method: Optional[str] = None
    totp_seed: Optional[str] = None
    totp_digits: Optional[int] = None
    totp_period_seconds: Optional[int] = None
    totp_algorithm: Optional[str] = None
    email_otp_mode: Optional[str] = None
    mail_connection_id: Optional[int] = None
    relay_address: Optional[str] = None
    otp_extract_config: Optional[dict] = None
    fingerprint: Optional[dict] = None
    preferred_agent_id: Optional[str] = None
    is_active: Optional[bool] = None
    # --- BYO residential proxy (write-only creds) + lawful-use warrant ---
    proxy_server: Optional[str] = Field(None, max_length=512)
    proxy_username: Optional[str] = Field(None, max_length=255)
    proxy_password: Optional[str] = None
    proxy_lawful_use_ack: Optional[bool] = None


class PersonaResponse(BaseModel):
    id: int
    name: str
    description: Optional[str]
    target_domain: Optional[str]
    login_username: Optional[str]
    has_password: bool
    twofa_method: str
    has_totp_seed: bool
    email_otp_mode: Optional[str]
    mail_connection_id: Optional[int]
    connected_mailbox: Optional[str]
    relay_address: Optional[str]
    has_fingerprint: bool
    preferred_agent_id: Optional[str]
    has_proxy: bool
    proxy_lawful_use_ack_at: Optional[datetime]
    is_active: bool
    validation_status: str
    has_warm_session: bool
    session_expires_at: Optional[datetime]
    last_login_at: Optional[datetime]
    last_used_at: Optional[datetime]
    created_at: Optional[datetime]
    updated_at: Optional[datetime]
    linked_workflows: List[dict] = []
    # {credential_field: vault_secret_key} — only names, never values.
    linked_secrets: dict = {}
    # NOTE: no password / totp_seed / tokens / session — never exposed.


def _to_response(p: Persona, mailbox_email: Optional[str] = None,
                 linked_workflows: Optional[List[dict]] = None,
                 has_warm_session: Optional[bool] = None,
                 has_totp_seed: Optional[bool] = None,
                 has_proxy: Optional[bool] = None) -> PersonaResponse:
    # When the username is linked to a vault secret it's stored as a
    # "{{vault:...}}" template — never surface that raw ref (linked_secrets
    # carries the link instead).
    display_username = p.login_username
    if display_username and "{{vault:" in display_username:
        display_username = None
    # has_warm_session / has_totp_seed default to deriving from the (loaded)
    # encrypted blobs. The list endpoint passes precomputed booleans so it can
    # defer those large columns and never load the warm-session blob.
    if has_warm_session is None:
        has_warm_session = bool(p.session_state_encrypted)
    if has_totp_seed is None:
        has_totp_seed = bool(p.totp_seed_encrypted)
    if has_proxy is None:
        has_proxy = bool(p.proxy_config_encrypted)
    return PersonaResponse(
        id=p.id, name=p.name, description=p.description, target_domain=p.target_domain,
        login_username=display_username, has_password=bool(p.credentials_encrypted),
        twofa_method=p.twofa_method, has_totp_seed=has_totp_seed,
        email_otp_mode=p.email_otp_mode, mail_connection_id=p.mail_connection_id,
        connected_mailbox=mailbox_email, relay_address=p.relay_address,
        has_fingerprint=bool(p.fingerprint), preferred_agent_id=p.preferred_agent_id,
        has_proxy=has_proxy, proxy_lawful_use_ack_at=p.proxy_lawful_use_ack_at,
        is_active=bool(p.is_active), validation_status=p.validation_status,
        has_warm_session=has_warm_session,
        session_expires_at=p.expires_at,
        last_login_at=p.last_login_at, last_used_at=p.last_used_at,
        created_at=p.created_at, updated_at=p.updated_at,
        linked_workflows=linked_workflows or [],
        linked_secrets=PersonaService.linked_secret_refs(p),
    )


async def _linked_workflows_map(db: AsyncSession, persona_ids: List[int]) -> dict:
    """Return {persona_id: [{id, name}]} for workflows whose default persona is set."""
    if not persona_ids:
        return {}
    from models.automation_workflow import AutomationWorkflow
    rows = (await db.execute(
        select(AutomationWorkflow.id, AutomationWorkflow.name, AutomationWorkflow.default_persona_id)
        .where(AutomationWorkflow.default_persona_id.in_(persona_ids))
    )).all()
    out: dict = {}
    for wid, wname, pid in rows:
        out.setdefault(pid, []).append({"id": wid, "name": wname})
    return out


async def _mailbox_email(db: AsyncSession, persona: Persona) -> Optional[str]:
    """The connected IMAP mailbox address for the persona, or None."""
    if not persona.mail_connection_id:
        return None
    conn = await db.get(MailConnection, persona.mail_connection_id)
    return conn.email if conn else None


def _validate_twofa(method: Optional[str], email_otp_mode: Optional[str]):
    if method is not None and method not in TWOFA_METHODS:
        raise HTTPException(422, f"twofa_method must be one of {sorted(TWOFA_METHODS)}")
    if email_otp_mode is not None and email_otp_mode not in EMAIL_OTP_MODES:
        raise HTTPException(422, f"email_otp_mode must be one of {sorted(EMAIL_OTP_MODES)}")


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
@router.get("", response_model=List[PersonaResponse], dependencies=[_FEATURE])
async def list_personas(
    domain: Optional[str] = None,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """List personas. Pass ?domain= to suggest by host suffix (wizard)."""
    # Load only the columns the list response serializes — NEVER the large
    # warm-session blob (session_state_encrypted, gzip+Fernet), the TOTP seed,
    # or the proxy creds. has_warm_session / has_totp_seed need just a boolean,
    # so we select those as label expressions instead of loading the blobs.
    # credentials_encrypted IS needed (has_password + linked_secret_refs scans it).
    q = (
        select(
            Persona,
            (Persona.session_state_encrypted.isnot(None)).label("_has_warm_session"),
            (Persona.totp_seed_encrypted.isnot(None)).label("_has_totp_seed"),
            (Persona.proxy_config_encrypted.isnot(None)).label("_has_proxy"),
        )
        .options(load_only(
            Persona.id,
            Persona.name,
            Persona.description,
            Persona.target_domain,
            Persona.login_username,
            Persona.credentials_encrypted,
            Persona.twofa_method,
            Persona.email_otp_mode,
            Persona.mail_connection_id,
            Persona.relay_address,
            Persona.fingerprint,
            Persona.preferred_agent_id,
            Persona.proxy_lawful_use_ack_at,
            Persona.is_active,
            Persona.validation_status,
            Persona.expires_at,
            Persona.last_login_at,
            Persona.last_used_at,
            Persona.created_at,
            Persona.updated_at,
        ))
        .order_by(Persona.name)
    )
    rows = (await db.execute(q)).all()
    if domain:
        d = domain.lower().lstrip(".")
        rows = [r for r in rows if not r[0].target_domain or r[0].target_domain.lower().lstrip(".") == d or d.endswith("." + r[0].target_domain.lower().lstrip("."))]
    personas = [r[0] for r in rows]
    wf_map = await _linked_workflows_map(db, [p.id for p in personas])
    out = []
    for p, has_warm_session, has_totp_seed, has_proxy in rows:
        out.append(_to_response(
            p, None, wf_map.get(p.id, []),
            has_warm_session=bool(has_warm_session),
            has_totp_seed=bool(has_totp_seed),
            has_proxy=bool(has_proxy),
        ))
    return out


@router.post("", response_model=PersonaResponse, status_code=status.HTTP_201_CREATED, dependencies=[_FEATURE])
async def create_persona(
    body: PersonaCreate,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    _validate_twofa(body.twofa_method, body.email_otp_mode)
    existing = await db.execute(
        select(Persona).where(Persona.name == body.name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, f"Persona '{body.name}' already exists")

    creds = dict(body.extra_login_fields or {})
    if body.password:
        creds["password"] = body.password

    # BYO residential proxy: storing the proxy creds REQUIRES an explicit
    # lawful-use acknowledgment. We stamp the warrant time when set.
    proxy_config_encrypted = None
    proxy_ack_at = None
    if body.proxy_server:
        if not body.proxy_lawful_use_ack:
            raise HTTPException(422, "Configuring a residential proxy requires acknowledging lawful use (proxy_lawful_use_ack=true).")
        proxy_config_encrypted = PersonaService.encrypt_json(
            {"server": body.proxy_server, "username": body.proxy_username, "password": body.proxy_password},
        )
        proxy_ack_at = datetime.now(timezone.utc)

    p = Persona(
        name=body.name,
        description=body.description,
        target_domain=body.target_domain,
        login_username=body.login_username,
        credentials_encrypted=PersonaService.encrypt_json(creds) if creds else None,
        twofa_method=body.twofa_method or "none",
        totp_seed_encrypted=PersonaService.encrypt(body.totp_seed) if body.totp_seed else None,
        totp_digits=body.totp_digits, totp_period_seconds=body.totp_period_seconds,
        totp_algorithm=body.totp_algorithm,
        email_otp_mode=body.email_otp_mode,
        mail_connection_id=body.mail_connection_id,
        relay_address=body.relay_address,
        otp_extract_config=body.otp_extract_config,
        fingerprint=body.fingerprint,
        preferred_agent_id=body.preferred_agent_id,
        proxy_config_encrypted=proxy_config_encrypted,
        proxy_lawful_use_ack_at=proxy_ack_at,
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    logger.info("Persona created: %s", p.name)
    return _to_response(p, await _mailbox_email(db, p))


@router.get("/{persona_id}", response_model=PersonaResponse, dependencies=[_FEATURE])
async def get_persona(
    persona_id: int,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    p = await PersonaService.get_owned(db, persona_id)
    if not p:
        raise HTTPException(404, "Persona not found")
    wf_map = await _linked_workflows_map(db, [p.id])
    return _to_response(p, await _mailbox_email(db, p), wf_map.get(p.id, []))


class PersonaRun(BaseModel):
    task_id: int
    workflow_id: Optional[int]
    workflow_name: Optional[str]
    status: Optional[str]
    success: Optional[bool]
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    error: Optional[str]


@router.get("/{persona_id}/runs", response_model=List[PersonaRun], dependencies=[_FEATURE])
async def persona_runs(
    persona_id: int,
    limit: int = 20,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Recent runs that used this persona (tasks tagged with _persona_id at dispatch)."""
    persona = await PersonaService.get_owned(db, persona_id)
    if not persona:
        raise HTTPException(404, "Persona not found")
    from models.automation_task import AutomationTask
    from models.automation_workflow import AutomationWorkflow
    rows = (await db.execute(
        select(AutomationTask, AutomationWorkflow.name)
        .outerjoin(AutomationWorkflow, AutomationWorkflow.id == AutomationTask.workflow_id)
        .where(func.json_extract(AutomationTask.trigger_context, "$._persona_id") == str(persona_id))
        .order_by(AutomationTask.created_at.desc())
        .limit(min(max(limit, 1), 50))
    )).all()
    return [
        PersonaRun(
            task_id=t.id, workflow_id=t.workflow_id, workflow_name=wname,
            status=t.status, success=t.success,
            started_at=t.started_at, completed_at=t.completed_at,
            error=(t.error_message[:200] if t.error_message else None),
        )
        for t, wname in rows
    ]


@router.patch("/{persona_id}", response_model=PersonaResponse, dependencies=[_FEATURE])
async def update_persona(
    persona_id: int,
    body: PersonaUpdate,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    p = await PersonaService.get_owned(db, persona_id)
    if not p:
        raise HTTPException(404, "Persona not found")
    _validate_twofa(body.twofa_method, body.email_otp_mode)

    data = body.model_dump(exclude_unset=True)

    simple = {
        "name", "description", "target_domain", "login_username", "twofa_method",
        "totp_digits", "totp_period_seconds", "totp_algorithm", "email_otp_mode",
        "mail_connection_id", "relay_address", "otp_extract_config", "fingerprint",
        "preferred_agent_id", "is_active",
    }
    for field in simple:
        if field in data:
            setattr(p, field, data[field])

    # Secrets: only overwrite when explicitly provided.
    if "password" in data or "extra_login_fields" in data:
        creds = dict(data.get("extra_login_fields") or {})
        if data.get("password"):
            creds["password"] = data["password"]
        p.credentials_encrypted = PersonaService.encrypt_json(creds) if creds else None
    if "totp_seed" in data:
        p.totp_seed_encrypted = PersonaService.encrypt(data["totp_seed"]) if data["totp_seed"] else None

    # BYO residential proxy: setting/replacing the proxy creds REQUIRES an
    # explicit lawful-use acknowledgment and re-stamps the warrant time.
    if "proxy_server" in data:
        if data.get("proxy_server"):
            if not body.proxy_lawful_use_ack:
                raise HTTPException(422, "Configuring a residential proxy requires acknowledging lawful use (proxy_lawful_use_ack=true).")
            p.proxy_config_encrypted = PersonaService.encrypt_json(
                {"server": data.get("proxy_server"), "username": data.get("proxy_username"), "password": data.get("proxy_password")},
            )
            p.proxy_lawful_use_ack_at = datetime.now(timezone.utc)
        else:
            # Explicitly clearing the proxy (proxy_server="" / null).
            p.proxy_config_encrypted = None
            p.proxy_lawful_use_ack_at = None

    await db.commit()
    await db.refresh(p)
    return _to_response(p, await _mailbox_email(db, p))


class ImapConfig(BaseModel):
    host: str = Field(..., max_length=255)
    port: Optional[int] = None
    username: str = Field(..., max_length=320)
    password: str = Field(..., description="App password — stored encrypted, never returned")
    use_ssl: bool = True
    mailbox: Optional[str] = Field(None, max_length=120)


@router.post("/{persona_id}/imap", response_model=PersonaResponse, dependencies=[_FEATURE])
async def set_persona_imap(
    persona_id: int,
    body: ImapConfig,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Attach (or replace) this persona's IMAP mailbox for email-OTP reading.

    Self-host email-OTP is IMAP-only — the code is read directly from the user's own
    mailbox (there is no OAuth mailbox and no forwarding relay here). The app password
    is verified before saving and stored Fernet-encrypted; it is never returned.
    """
    persona = await PersonaService.get_owned(db, persona_id)
    if not persona:
        raise HTTPException(404, "Persona not found")

    # Verify the credentials before saving so bad config fails fast (also runs the
    # SSRF / plaintext-IMAP guard in test_imap_connection).
    try:
        await PersonaService.test_imap_connection(
            body.host, body.port, body.use_ssl, body.username, body.password, body.mailbox
        )
    except PersonaError as e:
        raise HTTPException(400, str(e))

    # Reuse an existing IMAP row for the same mailbox (one mailbox can back many
    # personas; (provider, email) is unique) rather than inserting a duplicate.
    conn = None
    if persona.mail_connection_id:
        conn = await db.get(MailConnection, persona.mail_connection_id)
    if conn is None:
        dup = await db.execute(
            select(MailConnection).where(
                MailConnection.provider == "imap",
                MailConnection.email == body.username,
            )
        )
        conn = dup.scalar_one_or_none()
    if conn is None:
        conn = MailConnection(provider="imap", email=body.username)
        db.add(conn)
        await db.flush()

    conn.provider = "imap"
    conn.email = body.username
    conn.imap_host = body.host
    conn.imap_port = body.port
    conn.imap_username = body.username
    conn.imap_password_encrypted = PersonaService.encrypt(body.password)
    conn.imap_use_ssl = body.use_ssl
    conn.imap_mailbox = body.mailbox
    conn.is_active = "active"

    persona.mail_connection_id = conn.id
    persona.email_otp_mode = "oauth_mailbox"
    if (persona.twofa_method or "none") == "none":
        persona.twofa_method = "email_otp"

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(400, "This mailbox is already connected — reuse it or disconnect it first.")
    await db.refresh(persona)
    return _to_response(persona, await _mailbox_email(db, persona))


@router.delete("/{persona_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[_FEATURE])
async def delete_persona(
    persona_id: int,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    p = await PersonaService.get_owned(db, persona_id)
    if not p:
        raise HTTPException(404, "Persona not found")
    await db.delete(p)
    await db.commit()
    logger.info("Persona deleted: %s", persona_id)


@router.post("/{persona_id}/test-2fa", dependencies=[_FEATURE])
async def test_persona_2fa(
    persona_id: int,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Owner-only smoke test: confirm the persona can mint a 2FA value. Does NOT return the value."""
    p = await PersonaService.get_owned(db, persona_id)
    if not p:
        raise HTTPException(404, "Persona not found")
    if (p.twofa_method or "none") == "none":
        return {"ok": True, "method": "none", "message": "No 2FA configured"}
    try:
        result = await PersonaService.mint_otp(db, p)
        return {"ok": True, "method": p.twofa_method, "kind": result.get("type")}
    except PersonaError as e:
        raise HTTPException(400, f"2FA test failed: {e}")


# ---------------------------------------------------------------------------
# Import EXISTING authenticator seeds → personas (no per-account re-enrollment).
# TOTP is seed-derived and stateless: the secret your phone already holds mints
# the same codes here. We parse otpauth:// URIs (QR/paste, and 2FAS/Aegis/
# Bitwarden exports) and the Google Authenticator "Export accounts" migration
# blob, preview WITHOUT secrets, and only commit the accounts the user picks.
# ---------------------------------------------------------------------------
class AuthImportParseRequest(BaseModel):
    otpauth_uris: Optional[List[str]] = Field(None, description="One or more otpauth://totp/... URIs (from a QR scan or paste)")
    migration_payload: Optional[str] = Field(None, description="A Google Authenticator otpauth-migration:// export (URI or raw data)")


class AuthImportEntryPreview(BaseModel):
    idx: int
    issuer: Optional[str]
    label: Optional[str]
    suggested_name: str
    suggested_domain: Optional[str]
    algorithm: str
    digits: int
    period: int


class AuthImportParseResponse(BaseModel):
    import_token: str
    count: int
    entries: List[AuthImportEntryPreview]


class AuthImportSelection(BaseModel):
    idx: int
    name: Optional[str] = Field(None, max_length=120)
    target_domain: Optional[str] = Field(None, max_length=255)
    login_username: Optional[str] = Field(None, max_length=255)


class AuthImportCommitRequest(BaseModel):
    import_token: str
    selections: List[AuthImportSelection]


class AuthImportCommitResponse(BaseModel):
    created: List[PersonaResponse]
    skipped: List[dict] = []  # [{idx, reason}]


@router.post("/import/authenticator/parse", response_model=AuthImportParseResponse, dependencies=[_FEATURE])
async def parse_authenticator_import(
    body: AuthImportParseRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Parse an authenticator import into a secret-free preview + a single-use
    token. Secrets are staged server-side only; the user picks which accounts to
    keep at /commit."""
    from services import authenticator_import as ai
    if not (body.otpauth_uris or body.migration_payload):
        raise HTTPException(422, "Provide otpauth_uris and/or a migration_payload")
    try:
        entries = ai.parse_payloads(body.otpauth_uris, body.migration_payload)
    except ai.AuthenticatorImportError as e:
        raise HTTPException(422, f"Could not read the authenticator import: {e}")
    token = await ai.stage_entries(entries)
    previews = [AuthImportEntryPreview(**e.preview(i)) for i, e in enumerate(entries)]
    logger.info("Authenticator import parsed: %d account(s)", len(entries))
    return AuthImportParseResponse(import_token=token, count=len(entries), entries=previews)


@router.post("/import/authenticator/commit", response_model=AuthImportCommitResponse, dependencies=[_FEATURE])
async def commit_authenticator_import(
    body: AuthImportCommitRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Create one TOTP persona per user-selected account from a staged import."""
    from services import authenticator_import as ai
    entries = await ai.load_staged(body.import_token)
    if entries is None:
        raise HTTPException(404, "Import session expired or not found — re-run the import")
    if not body.selections:
        raise HTTPException(422, "No accounts selected to import")

    # Names must be unique — collect existing + freshly-assigned names and
    # auto-suffix collisions so nothing is silently lost.
    existing = (await db.execute(
        select(Persona.name)
    )).scalars().all()
    taken = {n.lower() for n in existing}

    created: List[Persona] = []
    skipped: List[dict] = []
    for sel in body.selections:
        if sel.idx < 0 or sel.idx >= len(entries):
            skipped.append({"idx": sel.idx, "reason": "invalid index"})
            continue
        e = entries[sel.idx]
        base_name = (sel.name or ai._suggested_name(e.issuer, e.label) or "Imported account").strip()[:120]
        name = _unique_persona_name(base_name, taken)
        taken.add(name.lower())
        domain = (sel.target_domain or ai._guess_domain(e.issuer, e.label) or None)
        p = Persona(
            name=name,
            target_domain=domain,
            login_username=sel.login_username,
            twofa_method="totp",
            totp_seed_encrypted=PersonaService.encrypt(e.secret),
            totp_digits=e.digits or 6,
            totp_period_seconds=e.period or 30,
            totp_algorithm=e.algorithm or "SHA1",
        )
        db.add(p)
        created.append(p)

    if not created:
        raise HTTPException(422, "No valid accounts to import")
    await db.commit()
    for p in created:
        await db.refresh(p)
    # Single-use: burn the staged seeds so the same import can't be replayed.
    await ai.consume_staged(body.import_token)
    logger.info("Authenticator import committed: %d persona(s)", len(created))
    return AuthImportCommitResponse(created=[_to_response(p) for p in created], skipped=skipped)


def _unique_persona_name(base: str, taken: set) -> str:
    """Return `base`, or `base (2)`, `base (3)`... avoiding names already taken
    (case-insensitive). Keeps the result within the 120-char column limit."""
    base = base or "Imported account"
    if base.lower() not in taken:
        return base
    for i in range(2, 1000):
        suffix = f" ({i})"
        candidate = base[: 120 - len(suffix)] + suffix
        if candidate.lower() not in taken:
            return candidate
    return base[:116] + " (x)"


class ValidateTotpRequest(BaseModel):
    totp_seed: str = Field(..., description="Base32 TOTP secret to validate (write-only; never stored by this call)")
    code: Optional[str] = Field(None, description="Optional code the user just used — checked against the seed, never logged")
    algorithm: str = "SHA1"
    digits: int = 6
    period: int = 30


class ValidateTotpResponse(BaseModel):
    valid_base32: bool
    matches_code: Optional[bool] = None  # null when no code was supplied


@router.post("/validate-totp", response_model=ValidateTotpResponse, dependencies=[_FEATURE])
async def validate_totp_seed(
    body: ValidateTotpRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    """Confirm a pasted TOTP seed is well-formed base32 and (optionally) that it
    reproduces a code the user just entered during recording — so the UI can show
    a 'verified' check before the seed is saved. The code is never logged."""
    import base64 as _b64
    seed = (body.totp_seed or "").replace(" ", "").replace("-", "").upper().rstrip("=")
    valid = False
    if seed:
        try:
            pad = "=" * ((8 - len(seed) % 8) % 8)
            _b64.b32decode(seed + pad, casefold=True)
            valid = True
        except Exception:
            valid = False
    matches: Optional[bool] = None
    if valid and body.code and body.code.strip():
        try:
            import pyotp
            from services.persona_service import _totp_digest
            algo = (body.algorithm or "SHA1").upper()
            totp = pyotp.TOTP(
                seed, digits=body.digits or 6,
                interval=body.period or 30, digest=_totp_digest(algo),
            )
            # valid_window=1 tolerates a one-step clock skew between us and the site.
            matches = bool(totp.verify(body.code.strip(), valid_window=1))
        except Exception as e:
            logger.warning("validate-totp code check failed: %s", e)
            matches = None
    return ValidateTotpResponse(valid_base32=valid, matches_code=matches)


# ---------------------------------------------------------------------------
# Create a persona FROM an existing workflow's login (capture the identity +
# warm session you just established, so future runs skip the login).
# ---------------------------------------------------------------------------
class PersonaFromWorkflow(BaseModel):
    workflow_id: int
    name: Optional[str] = None
    attach_as_default: bool = True


@router.post("/from-workflow", response_model=PersonaResponse, status_code=status.HTTP_201_CREATED, dependencies=[_FEATURE])
async def create_persona_from_workflow(
    body: PersonaFromWorkflow,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Turn a workflow's login (creds + captured warm session + fingerprint) into a persona."""
    from urllib.parse import urlparse
    from models.automation_workflow import AutomationWorkflow
    from services.session_state_service import SessionStateService

    wf = (await db.execute(
        select(AutomationWorkflow).where(
            AutomationWorkflow.id == body.workflow_id,
        )
    )).scalar_one_or_none()
    if not wf:
        raise HTTPException(404, "Workflow not found")

    domain = None
    if wf.entry_url:
        try:
            domain = urlparse(wf.entry_url).hostname
        except Exception:
            domain = None

    creds: dict = {}
    if wf.credentials_encrypted:
        try:
            creds = PersonaService.decrypt_json(wf.credentials_encrypted)
        except Exception:
            creds = {}
    username = creds.get("username") or creds.get("email") or creds.get("user")

    # Pull the most recent warm auth-session captured for this workflow (if any).
    auth_session = None
    try:
        aff = await SessionStateService.get_preferred_affinity(db, wf.id)
        if aff:
            auth_session = await SessionStateService.load_session(
                db, wf.id, aff.agent_id, wf.session_ttl_seconds
            )
    except Exception:
        auth_session = None
    fingerprint = (auth_session or {}).get("fingerprint")

    if not creds and not auth_session:
        raise HTTPException(
            400,
            "This workflow has no captured login yet. Add credentials or run it once (with session persistence on), then save it as a persona.",
        )

    p = await PersonaService.create_from_capture(
        db, name=(body.name or f"{wf.name} login"),
        domain=domain, login_username=username, creds=creds, auth_session=auth_session,
    )
    linked = []
    if body.attach_as_default and not wf.default_persona_id:
        wf.default_persona_id = p.id
        await db.commit()
        linked = [{"id": wf.id, "name": wf.name}]
    logger.info("Persona %s created from workflow %s", p.id, wf.id)
    return _to_response(p, None, linked)


# ---------------------------------------------------------------------------
# Create a persona FROM a specific run result (account-creation workflows) or
# from an AI session — capture the identity a run just produced/established.
# ---------------------------------------------------------------------------
class PersonaFromTask(BaseModel):
    task_id: int
    name: Optional[str] = None


@router.post("/from-task", response_model=PersonaResponse, status_code=status.HTTP_201_CREATED, dependencies=[_FEATURE])
async def create_persona_from_task(
    body: PersonaFromTask,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Capture the identity a workflow RUN produced (e.g. an account-creation run): the
    extracted/used credentials + the warm session established during that run."""
    from urllib.parse import urlparse
    from models.automation_task import AutomationTask
    from models.automation_workflow import AutomationWorkflow
    from services.session_state_service import SessionStateService

    task = await db.get(AutomationTask, body.task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    wf = await db.get(AutomationWorkflow, task.workflow_id) if task.workflow_id else None
    if not wf:
        raise HTTPException(404, "Workflow not found for this task")

    domain = None
    if wf.entry_url:
        try:
            domain = urlparse(wf.entry_url).hostname
        except Exception:
            domain = None

    result = task.result_data or {}
    extracted = result.get("extracted_data") or {}
    # Credentials the run used + anything it produced/extracted (e.g. new account creds).
    creds: dict = {}
    if wf.credentials_encrypted:
        try:
            creds.update(PersonaService.decrypt_json(wf.credentials_encrypted))
        except Exception:
            pass
    for src in (task.trigger_context or {}).get("form_data", {}), extracted:
        for k, v in (src or {}).items():
            if k in ("username", "email", "user", "password", "login") and isinstance(v, str):
                creds[k] = v
    username = creds.get("username") or creds.get("email") or creds.get("user")

    # Warm session: prefer the run's returned auth_session, else the workflow affinity.
    auth_session = result.get("auth_session")
    if not auth_session:
        try:
            aff = await SessionStateService.get_preferred_affinity(db, wf.id)
            if aff:
                auth_session = await SessionStateService.load_session(db, wf.id, aff.agent_id, wf.session_ttl_seconds)
        except Exception:
            auth_session = None

    if not creds and not auth_session:
        raise HTTPException(400, "This run produced no login (no credentials or captured session).")

    p = await PersonaService.create_from_capture(
        db, name=(body.name or f"{wf.name} account"),
        domain=domain, login_username=username, creds=creds, auth_session=auth_session,
    )
    logger.info("Persona %s created from task %s", p.id, body.task_id)
    return _to_response(p, None)


class PersonaFromAiSession(BaseModel):
    session_id: int
    name: Optional[str] = None


@router.post("/from-ai-session", response_model=PersonaResponse, status_code=status.HTTP_201_CREATED, dependencies=[_FEATURE])
async def create_persona_from_ai_session(
    body: PersonaFromAiSession,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Capture the identity an AI session established (e.g. it signed up / logged in).

    AI sessions are not available in this build, so there is no session to
    capture from. The endpoint stays registered but reports the feature is
    unavailable instead of raising.
    """
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="AI sessions are not available in this build.",
    )



# ---------------------------------------------------------------------------
# This coordinator has no per-persona linked mailbox (MailConnection), so there
# are no IMAP-attach / mailbox-disconnect endpoints. Email-OTP flows through the
# relay path instead (relay_address + POST /api/relay/inbound).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Agent OTP-mint callback (dispatch-token authorized, NOT a user session)
# ---------------------------------------------------------------------------
class OtpRequest(BaseModel):
    token: str = Field(..., description="Dispatch-minted OTP token from the task config")
    since_ts: Optional[float] = Field(None, description="Submit timestamp; ignore older OTP emails")


@router.post("/{persona_id}/otp")
async def mint_persona_otp(
    persona_id: int,
    body: OtpRequest,
    db: AsyncSession = Depends(get_db),
):
    """Agent callback during a run: returns {type:'code'|'magic_link', value:...}.

    Authorized by the short-lived dispatch token (scoped to persona+task). The
    TOTP seed / mailbox tokens are read here and never sent to the agent.
    """
    try:
        claims = PersonaService.verify_otp_token(body.token)
    except PersonaError as e:
        raise HTTPException(401, str(e))
    if int(claims.get("persona_id", -1)) != int(persona_id):
        raise HTTPException(403, "Token does not authorize this persona")

    result = await db.execute(
        select(Persona).where(
            Persona.id == persona_id,
        )
    )
    persona = result.scalar_one_or_none()
    if not persona:
        raise HTTPException(404, "Persona not found")

    # Single-use is enforced on the SUCCESS path only. Email/SMS OTP minting is
    # naturally polled by the agent (it retries until the code arrives, getting
    # 409s in the meantime), so burning on a failed/empty poll would break the
    # legitimate retry loop. We instead atomically reserve the jti the instant a
    # code is actually produced: the first successful mint wins, any concurrent
    # or later replay of the same token returns 401. TOTP (deterministic, no
    # polling) is likewise covered — its first mint reserves the jti.
    try:
        otp = await PersonaService.mint_otp(db, persona, since_ts=body.since_ts)
    except PersonaError as e:
        raise HTTPException(409, str(e))

    # Burn the token now that a real code/link was produced. consume_otp_token
    # fails CLOSED, so if the single-use marker can't be set we do NOT return the
    # freshly-minted code (prevents handing out a code on a token we can't prove
    # is unused).
    try:
        await PersonaService.consume_otp_token(body.token)
    except PersonaError as e:
        raise HTTPException(401, str(e))

    persona.last_used_at = datetime.utcnow()
    await db.commit()
    return otp
