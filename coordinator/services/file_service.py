"""
File service — the single entry point for every StoredFile mutation and resolve.

ALL file create / read / delete / resolve flows funnel through here so the
non-negotiable invariants (FILE_ASSETS_IMPLEMENTATION_PLAN §10) live in ONE place
rather than being re-implemented at each call site:

  1. Ownership is fail-closed — ``get_file`` / ``resolve_for_run`` always filter by
     ``deleted_at IS NULL`` and 404 otherwise.
  2. Per-file size + content-type are validated backend-authoritatively at EVERY
     create path (dashboard upload, dev /v1 upload, agent download-capture,
     AI / streaming attachment).
  3. Signed GET URLs are short-TTL (``settings.file_signed_url_ttl_seconds``) and
     single-object scoped; storage creds/host are never exposed (presign or the
     same-origin proxy in the router).

Single-owner coordinator: there is exactly one owner and every file belongs to
them (``created_by_user_id`` records who uploaded it). Storage bytes live via
``services.visual_storage`` — the coordinator writes to the env-configured object
store or the local-disk fallback, so ``provider`` is always the default (None).

Convention: this service FLUSHES (never commits) — the calling router owns the
transaction. On a store-then-insert failure the just-written object is best-effort
deleted so storage can't leak orphans.
"""
from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.stored_file import StoredFile
from services import visual_storage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Content-type policy
# ---------------------------------------------------------------------------
# Conservative denylist of obviously dangerous executable / script types. Files
# are only ever handed to a browser file-input or the LLM and are NEVER executed
# by us (§10.7), but we still refuse to STORE executables by default to keep the
# library from becoming a malware drop. When settings.file_allowed_content_types
# is non-empty it flips to a strict ALLOWLIST (denylist ignored).
_DENY_CONTENT_TYPES = {
    "application/x-msdownload",          # .exe / .dll
    "application/x-msdos-program",
    "application/x-dosexec",
    "application/x-sh",                  # shell script
    "application/x-shellscript",
    "application/x-csh",
    "application/x-bat",
    "application/x-msi",
    "application/vnd.microsoft.portable-executable",
    "application/x-mach-binary",
    "application/x-elf",
    "application/x-executable",
    "application/java-archive",
    "application/x-java-archive",
}


def _content_type_allowed(content_type: str) -> bool:
    """True when this content type may be stored under the active policy.

    Allowlist mode (settings.file_allowed_content_types non-empty): only an exact,
    case-insensitive match passes. Default mode: anything EXCEPT the conservative
    executable denylist passes."""
    ct = (content_type or "").split(";")[0].strip().lower()
    allow = [c.strip().lower() for c in (settings.file_allowed_content_types or []) if c]
    if allow:
        return ct in allow
    return ct not in _DENY_CONTENT_TYPES


_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._ \-]+")


def _sanitize_filename(name: Optional[str]) -> str:
    """Produce a safe, display-friendly original filename.

    Strips any path component (anti-traversal), normalizes unicode, removes
    control / unsafe characters, collapses whitespace and bounds the length. Never
    returns empty — falls back to ``file``. This is metadata only (the storage key
    is the opaque file_id), but a sanitized name is what ends up in
    ``Content-Disposition`` and on the page's file input, so keep it clean."""
    raw = (name or "").strip()
    # Drop any directory component a client may have sent (foo/../bar, C:\x\y).
    raw = raw.replace("\\", "/").split("/")[-1]
    raw = unicodedata.normalize("NFKC", raw)
    raw = "".join(ch for ch in raw if ord(ch) >= 32)  # strip control chars
    raw = _FILENAME_SAFE.sub("_", raw).strip(" .")
    raw = re.sub(r"\s+", " ", raw)
    if not raw:
        raw = "file"
    return raw[:255]


def _storage_key(file_id: str) -> str:
    """Object key for a file. Single-owner: a flat ``files/{file_id}`` namespace
    (the file_id is an unguessable ``file_<uuid4hex>``)."""
    return f"files/{file_id}"


def _validate_size(size_bytes: int) -> None:
    cap = int(settings.file_max_bytes or 0)
    if size_bytes <= 0:
        raise HTTPException(
            status_code=400,
            detail={"code": "empty_file", "message": "The file is empty."},
        )
    if cap and size_bytes > cap:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "file_too_large",
                "message": (
                    f"File exceeds the {cap // (1024 * 1024)}MB per-file limit."
                ),
            },
        )


def _validate_content_type(content_type: str) -> None:
    if not _content_type_allowed(content_type):
        raise HTTPException(
            status_code=415,
            detail={
                "code": "unsupported_file_type",
                "message": f"Files of type '{content_type}' are not allowed.",
            },
        )


def _ephemeral_expiry(source: str) -> Optional[datetime]:
    """Ephemeral sources (workflow_output / ai_session / streaming) default to a
    TTL so captured/attached files don't accumulate as library clutter; library
    sources (upload / api) are persistent (NULL) until deleted."""
    if source in ("workflow_output", "ai_session", "streaming"):
        ttl = int(settings.file_ephemeral_ttl_seconds or 0)
        if ttl > 0:
            return datetime.now(timezone.utc) + timedelta(seconds=ttl)
    return None


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
async def create_file(
    db: AsyncSession,
    *,
    raw: bytes,
    filename: str,
    content_type: str,
    source: str = "upload",
    user_id=None,
    source_run_id: Optional[int] = None,
    purpose: Optional[str] = None,
    expires_at: Optional[datetime] = None,
    is_admin: bool = False,
) -> StoredFile:
    """Validate, store and register a new StoredFile for the owner.

    Order matters for fail-closed safety:
      1. size cap (413), 2. content-type policy (415),
      3. sha256 + filename sanitize, 4. write bytes to object storage,
      5. insert the row (status=ready).

    Raises HTTPException (413/415/400). The caller commits; this flushes. If the
    row insert fails after the object is written, the object is best-effort deleted
    to avoid an orphan."""
    raw = raw or b""
    size_bytes = len(raw)
    _validate_size(size_bytes)
    ct = (content_type or "application/octet-stream").split(";")[0].strip() or "application/octet-stream"
    _validate_content_type(ct)

    sha = hashlib.sha256(raw).hexdigest()
    safe_name = _sanitize_filename(filename)

    f = StoredFile(
        created_by_user_id=user_id,
        filename=safe_name,
        content_type=ct,
        size_bytes=size_bytes,
        sha256=sha,
        source=source,
        source_run_id=source_run_id,
        purpose=purpose,
        status="ready",
        expires_at=expires_at if expires_at is not None else _ephemeral_expiry(source),
    )
    # The model's PK default only runs at flush; mint the handle here (same
    # "file_<uuid4hex>" shape) so the storage key can reference it before insert.
    if f.id is None:
        from uuid import uuid4
        f.id = "file_" + uuid4().hex
    key = _storage_key(f.id)

    # Write to the env-configured object store / local-disk fallback. A NULL
    # provider stamp reflects the default store.
    ref = visual_storage.store_file_bytes(raw, key, ct, provider=None)
    f.storage_key = ref
    f.storage_provider_id = None

    try:
        db.add(f)
        await db.flush()
    except Exception:
        # Roll the object back so a failed insert can't leak storage.
        try:
            visual_storage.delete_file_object(ref, provider=None)
        except Exception:
            logger.warning("file_service: failed to clean up object after insert error (%s)", key)
        raise
    return f


# ---------------------------------------------------------------------------
# Read / list
# ---------------------------------------------------------------------------
async def get_file(db: AsyncSession, file_id: str) -> StoredFile:
    """Fetch one non-deleted file. Fail-closed: 404 whether the id is unknown or
    deleted (no existence leak)."""
    if not file_id:
        raise HTTPException(status_code=404, detail="File not found")
    res = await db.execute(
        select(StoredFile).where(
            StoredFile.id == file_id,
            StoredFile.deleted_at.is_(None),
        )
    )
    f = res.scalar_one_or_none()
    if f is None:
        raise HTTPException(status_code=404, detail="File not found")
    return f


async def list_files(
    db: AsyncSession,
    *,
    source: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[StoredFile]:
    """List the owner's non-deleted files (newest first), optionally filtered by
    source."""
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    stmt = (
        select(StoredFile)
        .where(StoredFile.deleted_at.is_(None))
        .order_by(StoredFile.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if source:
        stmt = stmt.where(StoredFile.source == source)
    res = await db.execute(stmt)
    return list(res.scalars().all())


async def count_files(db: AsyncSession, *, source: Optional[str] = None) -> int:
    """Total non-deleted file count (for paging metadata)."""
    from sqlalchemy import func as _func
    stmt = select(_func.count(StoredFile.id)).where(StoredFile.deleted_at.is_(None))
    if source:
        stmt = stmt.where(StoredFile.source == source)
    res = await db.execute(stmt)
    return int(res.scalar() or 0)


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------
async def delete_file(db: AsyncSession, file_id: str) -> None:
    """Soft-delete a file: stamp ``deleted_at`` and hard-remove the storage object.
    Fail-closed via get_file (404 if not found). Idempotent at the storage layer
    (delete is best-effort)."""
    f = await get_file(db, file_id)
    now = datetime.now(timezone.utc)
    ref = f.storage_key
    f.deleted_at = now
    await db.flush()
    # Remove the bytes AFTER the row is marked deleted (best-effort; a storage
    # hiccup must not roll back the soft-delete — a retention sweep reclaims it).
    try:
        visual_storage.delete_file_object(ref, provider=None)
    except Exception:
        logger.warning("file_service: delete_file_object failed for %s (will be swept later)", file_id)


# ---------------------------------------------------------------------------
# Resolve / sign
# ---------------------------------------------------------------------------
async def signed_get(db: AsyncSession, file_id: str, ttl: Optional[int] = None) -> Optional[str]:
    """Ownership-checked, short-TTL, single-object presigned GET URL. Returns None
    when storage is unavailable / the file is an inline base64 fallback (the router
    falls back to the same-origin byte proxy in that case)."""
    f = await get_file(db, file_id)
    ttl = int(ttl or settings.file_signed_url_ttl_seconds or 600)
    return visual_storage.presigned_file_get(f.storage_key, expires_seconds=ttl, provider=None)


async def resolve_for_run(db: AsyncSession, file_id: str, ttl: Optional[int] = None) -> dict:
    """Resolve one file to a dispatch descriptor for the run files-map (§4.1):
    ``{file_id, url, filename, content_type, size}``. Ownership-checked (404 if
    deleted). ``url`` is a short-TTL signed GET the agent fetches the bytes from;
    keep the TTL >= expected run duration (caller passes it). May be None when
    storage is degraded (caller decides whether to fail the run)."""
    f = await get_file(db, file_id)
    ttl = int(ttl or settings.file_signed_url_ttl_seconds or 600)
    url = visual_storage.presigned_file_get(f.storage_key, expires_seconds=ttl, provider=None)
    return {
        "file_id": f.id,
        "url": url,
        "filename": f.filename,
        "content_type": f.content_type,
        "size": int(f.size_bytes or 0),
    }


async def provider_for_file(db: AsyncSession, f: StoredFile):
    """Public accessor for a file's storage provider. Single-owner coordinator: the
    default store is always used, so this is always None. Kept for call-site
    compatibility (call sites pass it as the ``provider=`` kwarg to visual_storage)."""
    return None


def open_serialize(f: StoredFile) -> dict:
    """OpenAI Files-API-shaped serialization of a StoredFile row. ``created_at`` is
    a unix epoch int (OpenAI convention). Never leaks the storage key."""
    created = f.created_at
    if isinstance(created, datetime):
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        created_ts = int(created.timestamp())
    else:
        created_ts = int(datetime.now(timezone.utc).timestamp())
    return {
        "id": f.id,
        "object": "file",
        "bytes": int(f.size_bytes or 0),
        "created_at": created_ts,
        "filename": f.filename,
        "purpose": f.purpose or "user_data",
        "content_type": f.content_type,
        "status": f.status or "ready",
        "source": f.source,
    }


__all__ = [
    "create_file",
    "get_file",
    "list_files",
    "count_files",
    "delete_file",
    "signed_get",
    "resolve_for_run",
    "provider_for_file",
    "open_serialize",
]
