"""
Authenticator import — turn EXISTING TOTP secrets into personas without
re-enrolling 2FA on every account.

TOTP is stateless and seed-derived: the secret your phone's authenticator holds
produces the same codes everywhere, on any number of devices, with no
"enrollment" and no conflict. So if we can read the seeds your authenticator
already has, Writ can mint the same codes server-side (see
persona_service.generate_totp) — the user never disables/re-adds 2FA per site.

Two ingest formats, both parsed with stdlib only (no protobuf dependency):

  1. otpauth:// URIs        — one account each. Produced by a QR scan (decoded
                              client-side) or pasted by hand, and by the JSON
                              exports of 2FAS / Aegis / Bitwarden / Raivo.
  2. otpauth-migration://   — Google Authenticator "Export accounts". A single
                              QR encodes a base64 protobuf holding EVERY enrolled
                              account at once (secret/name/issuer/algo/digits).

Security:
- Parsing returns a PREVIEW WITHOUT secrets. The full entries (incl. seeds) are
  staged in Redis under a per-token-namespaced, short-lived, single-use token,
  and only the user-selected entries are committed to personas (Fernet-encrypted).
- A Google export contains ALL of the user's authenticator accounts, many
  unrelated to Writ — we never auto-import; the user picks which to keep.
"""
from __future__ import annotations

import base64
import json
import logging
import uuid
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, parse_qs, unquote

logger = logging.getLogger(__name__)

# Redis staging: namespaced per token so one token can never load another
# token's parsed seeds. Short TTL — the preview→commit hop is seconds.
_STAGE_PREFIX = "authimport:"
_STAGE_TTL_SECONDS = 600  # 10 min
_MAX_ENTRIES = 200        # sanity cap on a single import payload

_ALGO_BY_ENUM = {0: "SHA1", 1: "SHA1", 2: "SHA256", 3: "SHA512"}  # 4=MD5 unsupported
_DIGITS_BY_ENUM = {0: 6, 1: 6, 2: 8}

# Light issuer→domain hints so the preview can pre-fill a sensible target_domain.
# Intentionally tiny — anything unknown stays None and the user fills it in.
_ISSUER_DOMAIN_HINTS = {
    "google": "google.com", "github": "github.com", "gitlab": "gitlab.com",
    "amazon": "amazon.com", "amazon web services": "aws.amazon.com", "aws": "aws.amazon.com",
    "microsoft": "microsoft.com", "facebook": "facebook.com", "instagram": "instagram.com",
    "twitter": "twitter.com", "x": "x.com", "dropbox": "dropbox.com", "slack": "slack.com",
    "discord": "discord.com", "cloudflare": "cloudflare.com", "digitalocean": "digitalocean.com",
    "stripe": "stripe.com", "paypal": "paypal.com", "coinbase": "coinbase.com",
    "linkedin": "linkedin.com", "shopify": "shopify.com", "namecheap": "namecheap.com",
}


class AuthenticatorImportError(Exception):
    """Bad/unparseable import payload."""


@dataclass
class ImportEntry:
    """One parsed TOTP account. `secret` is the raw base32 seed — NEVER serialized
    into a preview; it lives only in the Redis staging blob."""
    secret: str                 # base32, unpadded
    issuer: Optional[str]
    label: Optional[str]        # account name, e.g. "user@example.com"
    algorithm: str = "SHA1"
    digits: int = 6
    period: int = 30

    def preview(self, idx: int) -> Dict[str, Any]:
        """Secret-free view shown to the user before they pick what to import."""
        return {
            "idx": idx,
            "issuer": self.issuer,
            "label": self.label,
            "suggested_name": _suggested_name(self.issuer, self.label),
            "suggested_domain": _guess_domain(self.issuer, self.label),
            "algorithm": self.algorithm,
            "digits": self.digits,
            "period": self.period,
        }


# ---------------------------------------------------------------------------
# Public parse entry point
# ---------------------------------------------------------------------------
def parse_payloads(otpauth_uris: Optional[List[str]] = None,
                   migration_payload: Optional[str] = None) -> List[ImportEntry]:
    """Parse any mix of single otpauth:// URIs and a Google migration blob into a
    deduped list of TOTP entries. Non-TOTP (HOTP) and malformed entries are
    skipped. Raises AuthenticatorImportError only if NOTHING usable was found."""
    entries: List[ImportEntry] = []
    errors: List[str] = []

    for uri in (otpauth_uris or []):
        uri = (uri or "").strip()
        if not uri:
            continue
        if uri.lower().startswith("otpauth-migration://"):
            # Someone pasted a migration URI into the per-account box — handle it.
            try:
                entries.extend(parse_migration_payload(uri))
            except AuthenticatorImportError as e:
                errors.append(str(e))
            continue
        try:
            entry = parse_otpauth_uri(uri)
            if entry is not None:
                entries.append(entry)
        except AuthenticatorImportError as e:
            errors.append(str(e))

    if migration_payload and migration_payload.strip():
        try:
            entries.extend(parse_migration_payload(migration_payload.strip()))
        except AuthenticatorImportError as e:
            errors.append(str(e))

    deduped = _dedupe(entries)
    if not deduped:
        detail = "; ".join(errors[:3]) if errors else "no TOTP accounts found in the import"
        raise AuthenticatorImportError(detail)
    if len(deduped) > _MAX_ENTRIES:
        raise AuthenticatorImportError(f"import has too many accounts ({len(deduped)} > {_MAX_ENTRIES})")
    return deduped


# ---------------------------------------------------------------------------
# otpauth:// URI  (single account)
# ---------------------------------------------------------------------------
def parse_otpauth_uri(uri: str) -> Optional[ImportEntry]:
    """Parse `otpauth://totp/Label?secret=...&issuer=...&...`.

    Returns None for HOTP (counter-based — we mint time-based codes, so HOTP
    can't be mirrored). Raises on a malformed TOTP URI."""
    parts = urlsplit(uri)
    if parts.scheme.lower() != "otpauth":
        raise AuthenticatorImportError("not an otpauth:// URI")
    otp_type = (parts.netloc or "").lower()
    if otp_type == "hotp":
        return None  # counter-based; unsupported by server-side time minting
    if otp_type != "totp":
        raise AuthenticatorImportError(f"unsupported otpauth type '{otp_type}'")

    q = parse_qs(parts.query)
    secret_raw = (q.get("secret", [None])[0] or "").strip()
    if not secret_raw:
        raise AuthenticatorImportError("otpauth URI missing secret")
    secret = _normalize_base32(secret_raw)
    if not secret:
        raise AuthenticatorImportError("otpauth secret is not valid base32")

    # Label is the path: "/Issuer:account" or "/account". Issuer query param wins.
    label_raw = unquote(parts.path.lstrip("/")) or None
    issuer = (q.get("issuer", [None])[0] or "").strip() or None
    label = label_raw
    if label_raw and ":" in label_raw:
        prefix, _, rest = label_raw.partition(":")
        if not issuer and prefix.strip():
            issuer = prefix.strip()
        label = rest.strip() or label_raw

    algorithm = (q.get("algorithm", ["SHA1"])[0] or "SHA1").upper()
    if algorithm not in {"SHA1", "SHA256", "SHA512"}:
        algorithm = "SHA1"
    digits = _safe_int(q.get("digits", [6])[0], 6)
    digits = digits if digits in (6, 8) else 6
    period = _safe_int(q.get("period", [30])[0], 30) or 30

    return ImportEntry(secret=secret, issuer=issuer, label=label,
                       algorithm=algorithm, digits=digits, period=period)


# ---------------------------------------------------------------------------
# otpauth-migration://  (Google Authenticator "Export accounts")
# ---------------------------------------------------------------------------
def parse_migration_payload(payload: str) -> List[ImportEntry]:
    """Parse a Google Authenticator export.

    Accepts the full `otpauth-migration://offline?data=<urlencoded-base64>` URI
    OR just the raw (url/base64) data blob. The blob is a protobuf:

        MigrationPayload { repeated OtpParameters otp_parameters = 1; ... }
        OtpParameters { bytes secret=1; string name=2; string issuer=3;
                        Algorithm algorithm=4; DigitCount digits=5;
                        OtpType type=6; int64 counter=7; }
    """
    data_b64 = payload.strip()
    if "otpauth-migration://" in data_b64.lower() or "data=" in data_b64:
        parts = urlsplit(data_b64)
        q = parse_qs(parts.query)
        data_b64 = (q.get("data", [None])[0] or "").strip()
        if not data_b64:
            raise AuthenticatorImportError("migration URI missing data= parameter")
    # parse_qs already url-decoded; be tolerant if a raw %-encoded blob was passed.
    data_b64 = unquote(data_b64)
    raw = _b64decode_loose(data_b64)
    if raw is None:
        raise AuthenticatorImportError("migration data is not valid base64")

    entries: List[ImportEntry] = []
    for field_num, wire_type, value in _iter_protobuf_fields(raw):
        if field_num == 1 and wire_type == 2 and isinstance(value, (bytes, bytearray)):
            entry = _parse_otp_parameters(bytes(value))
            if entry is not None:
                entries.append(entry)
    if not entries:
        raise AuthenticatorImportError("no TOTP accounts in the Google Authenticator export")
    return entries


def _parse_otp_parameters(buf: bytes) -> Optional[ImportEntry]:
    secret_bytes: Optional[bytes] = None
    name: Optional[str] = None
    issuer: Optional[str] = None
    algorithm = "SHA1"
    digits = 6
    otp_type = 2  # default totp
    try:
        for field_num, wire_type, value in _iter_protobuf_fields(buf):
            if field_num == 1 and wire_type == 2:
                secret_bytes = bytes(value)
            elif field_num == 2 and wire_type == 2:
                name = _decode_utf8(value)
            elif field_num == 3 and wire_type == 2:
                issuer = _decode_utf8(value)
            elif field_num == 4 and wire_type == 0:
                algorithm = _ALGO_BY_ENUM.get(int(value), "SHA1")
            elif field_num == 5 and wire_type == 0:
                digits = _DIGITS_BY_ENUM.get(int(value), 6)
            elif field_num == 6 and wire_type == 0:
                otp_type = int(value)
    except Exception as e:  # one bad sub-message shouldn't kill the whole import
        logger.warning("skipping unparseable migration OTP entry: %s", e)
        return None

    if otp_type != 2:  # 1=HOTP (counter-based) — can't be time-minted
        return None
    if not secret_bytes:
        return None
    secret = base64.b32encode(secret_bytes).decode("ascii").rstrip("=")
    if not secret:
        return None
    # Google Authenticator's migration export is always 30s period.
    return ImportEntry(secret=secret, issuer=(issuer or None), label=(name or None),
                       algorithm=algorithm, digits=digits, period=30)


# ---------------------------------------------------------------------------
# Redis staging (preview → commit)
# ---------------------------------------------------------------------------
async def stage_entries(entries: List[ImportEntry]) -> str:
    """Stash the full entries (incl. secrets) under a single-use token, return it."""
    from utils.redis_client import get_redis
    token = uuid.uuid4().hex
    blob = json.dumps([asdict(e) for e in entries])
    redis = get_redis()
    await redis.set(_stage_key(token), blob, ex=_STAGE_TTL_SECONDS)
    return token


async def load_staged(token: str) -> Optional[List[ImportEntry]]:
    from utils.redis_client import get_redis
    if not token:
        return None
    redis = get_redis()
    blob = await redis.get(_stage_key(token))
    if not blob:
        return None
    if isinstance(blob, (bytes, bytearray)):
        blob = blob.decode("utf-8")
    try:
        rows = json.loads(blob)
    except Exception:
        return None
    return [ImportEntry(**row) for row in rows]


async def consume_staged(token: str) -> None:
    """Burn the staging token so an import payload can't be committed twice."""
    from utils.redis_client import get_redis
    try:
        redis = get_redis()
        await redis.delete(_stage_key(token))
    except Exception as e:
        logger.warning("failed to consume authenticator-import token: %s", e)


def _stage_key(token: str) -> str:
    return f"{_STAGE_PREFIX}{token}"


# ---------------------------------------------------------------------------
# Minimal protobuf wire reader (varint + length-delimited only)
# ---------------------------------------------------------------------------
def _iter_protobuf_fields(buf: bytes) -> List[Tuple[int, int, Any]]:
    """Yield (field_number, wire_type, value) for a protobuf message.

    value is an int for varint/fixed wire types and bytes for length-delimited.
    Only the wire types the migration schema uses are decoded in full; others are
    skipped so we stay forward-compatible with new fields."""
    out: List[Tuple[int, int, Any]] = []
    i, n = 0, len(buf)
    while i < n:
        tag, i = _read_varint(buf, i)
        field_num = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:            # varint
            val, i = _read_varint(buf, i)
            out.append((field_num, 0, val))
        elif wire_type == 2:          # length-delimited
            length, i = _read_varint(buf, i)
            if i + length > n:
                raise AuthenticatorImportError("truncated protobuf field")
            out.append((field_num, 2, buf[i:i + length]))
            i += length
        elif wire_type == 5:          # 32-bit
            out.append((field_num, 5, int.from_bytes(buf[i:i + 4], "little")))
            i += 4
        elif wire_type == 1:          # 64-bit
            out.append((field_num, 1, int.from_bytes(buf[i:i + 8], "little")))
            i += 8
        else:
            raise AuthenticatorImportError(f"unsupported protobuf wire type {wire_type}")
    return out


def _read_varint(buf: bytes, i: int) -> Tuple[int, int]:
    result = shift = 0
    while True:
        if i >= len(buf):
            raise AuthenticatorImportError("truncated varint")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7
        if shift > 63:
            raise AuthenticatorImportError("varint too long")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _normalize_base32(secret: str) -> Optional[str]:
    """Strip spaces/padding, upper-case, and verify it decodes as base32."""
    cleaned = secret.replace(" ", "").replace("-", "").upper().rstrip("=")
    if not cleaned:
        return None
    pad = "=" * ((8 - len(cleaned) % 8) % 8)
    try:
        base64.b32decode(cleaned + pad, casefold=True)
    except Exception:
        return None
    return cleaned


def _b64decode_loose(data: str) -> Optional[bytes]:
    """Decode standard or url-safe base64, tolerating missing padding."""
    s = data.strip().replace("\n", "").replace("\r", "")
    pad = "=" * ((4 - len(s) % 4) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            return decoder(s + pad)
        except Exception:
            continue
    return None


def _decode_utf8(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace") or None
    return str(value) or None


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _dedupe(entries: List[ImportEntry]) -> List[ImportEntry]:
    """Drop exact duplicate seeds (same secret+issuer+label) — common when a user
    pastes URIs and also uploads a migration that overlaps."""
    seen = set()
    out: List[ImportEntry] = []
    for e in entries:
        key = (e.secret, (e.issuer or "").lower(), (e.label or "").lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def _suggested_name(issuer: Optional[str], label: Optional[str]) -> str:
    issuer = (issuer or "").strip()
    label = (label or "").strip()
    if issuer and label:
        return f"{issuer} — {label}"
    return issuer or label or "Imported account"


def _guess_domain(issuer: Optional[str], label: Optional[str]) -> Optional[str]:
    """Best-effort target_domain for the preview; None when we can't tell."""
    for candidate in (issuer, label):
        c = (candidate or "").strip().lower()
        if not c:
            continue
        if "." in c and " " not in c:           # already looks like a domain/email host
            return c.split("@")[-1].split("/")[0]
        hint = _ISSUER_DOMAIN_HINTS.get(c)
        if hint:
            return hint
    return None
