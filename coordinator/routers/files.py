"""
Files router — the file-assets surface.

Single-owner coordinator: every file belongs to the one owner. Two surfaces
over ONE substrate
(services.file_service, which owns every ownership / size / content-type
invariant — §10):

  - DASHBOARD  ``/files``       (JWT, ``get_auth_context``)   — the file library
    the web UI uses: upload, list, metadata, download/preview, delete, usage bar.
  - DEVELOPER  ``/api/v1/files`` (API key, ``get_current_api_key``) — the
    OpenAI Files-API-compatible surface so an OpenAI SDK pointed at
    ``{ORIGIN}/api/v1`` can ``files.create`` / ``list`` / ``retrieve`` /
    ``content`` / ``delete``. CLIENT API keys are confined to ``/api/v1/*`` by
    ``get_current_api_key``, so this is the only file path they can reach.

Both paths funnel into file_service; only the auth dependency and the
``source`` tag differ (``upload`` vs ``api``). The byte transport is multipart in,
same-origin proxy / short-TTL presigned GET out, so storage creds/host are never
exposed to the browser.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from config import settings
from security.dependencies import get_auth_context, AuthContext, check_api_key_scope
from security.api_key import get_current_api_key
from services import file_service, visual_storage

logger = logging.getLogger(__name__)


# ===========================================================================
# Shared helpers
# ===========================================================================
async def _serve_content(db: AsyncSession, file_id: str) -> Response:
    """Serve a file's bytes to the caller (ownership-checked).

    PREFERS a 302 redirect to a short-TTL, single-object presigned GET so the bytes
    never flow through the backend (consistent with the no-backend-bytes principle).
    Falls back to a same-origin byte proxy when storage can't presign — e.g. the
    inline base64 fallback or a hidden storage host. Either way the storage
    host/creds are never exposed and the response carries a proper Content-Type +
    Content-Disposition: attachment."""
    f = await file_service.get_file(db, file_id)
    ttl = int(settings.file_signed_url_ttl_seconds or 600)
    provider = await file_service.provider_for_file(db, f)
    signed = visual_storage.presigned_file_get(f.storage_key, expires_seconds=ttl, provider=provider)
    if signed:
        # 302 — the browser/SDK fetches the bytes straight from object storage via
        # a scoped, expiring URL; the backend never ingests the payload.
        return RedirectResponse(url=signed, status_code=302)

    # Fallback: proxy the bytes through the API (storage degraded / base64 inline).
    raw, content_type = visual_storage.fetch_file_bytes(f.storage_key, provider=provider)
    if not raw:
        raise HTTPException(status_code=404, detail="File not found")
    # Quote the filename for a safe Content-Disposition header.
    safe = (f.filename or "file").replace('"', "")
    return Response(
        content=raw,
        media_type=f.content_type or content_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{safe}"',
            "Cache-Control": "private, no-store",
        },
    )


# ===========================================================================
# Dashboard surface — /files (JWT)
# ===========================================================================
router = APIRouter(prefix="/files", tags=["Files"])


@router.post("")
async def upload_file(
    file: UploadFile = File(...),
    purpose: Optional[str] = Form(None),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Upload a file to the file library (source=upload).

    Multipart UploadFile in, content-type/size/quota
    validated backend-authoritatively in file_service, OpenAI-shaped row out. The
    returned ``id`` (``file_<...>``) is the stable handle linked to an upload step,
    passed to a run, or fetched via GET /files/{id}/content."""
    raw = await file.read()
    f = await file_service.create_file(
        db,
        raw=raw,
        filename=file.filename or "file",
        content_type=file.content_type or "application/octet-stream",
        source="upload",
        user_id=auth.user_id,
        purpose=purpose,
        is_admin=auth.is_platform_admin,
    )
    await db.commit()
    return file_service.open_serialize(f)


@router.get("")
async def list_files(
    source: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """List the owner's files (newest first), optionally filtered by source. Paged."""
    files = await file_service.list_files(
        db, source=source, limit=limit, offset=offset
    )
    total = await file_service.count_files(db, source=source)
    return {
        "object": "list",
        "data": [file_service.open_serialize(f) for f in files],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/usage")
async def files_usage(
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Usage snapshot for the library bar: bytes used + file count.

    Single-owner coordinator: storage limits are unlimited (-1). Usage is
    computed live from the non-deleted StoredFile rows."""
    from sqlalchemy import func, select
    from models.stored_file import StoredFile

    res = await db.execute(
        select(
            func.coalesce(func.sum(StoredFile.size_bytes), 0),
            func.count(StoredFile.id),
        ).where(StoredFile.deleted_at.is_(None))
    )
    row = res.one()
    bytes_used = int(row[0] or 0)
    file_count = int(row[1] or 0)

    return {
        "bytes_used": bytes_used,
        "bytes_limit": -1,
        "file_count": file_count,
        "file_limit": -1,
    }


@router.get("/{file_id}")
async def get_file_meta(
    file_id: str,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """File metadata (OpenAI-shaped). 404 if deleted (fail-closed)."""
    f = await file_service.get_file(db, file_id)
    return file_service.open_serialize(f)


@router.get("/{file_id}/content")
async def get_file_content(
    file_id: str,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Download / preview a file's bytes (ownership-checked). 302 → short-TTL
    presigned GET, or a same-origin byte proxy fallback."""
    return await _serve_content(db, file_id)


@router.delete("/{file_id}", status_code=204)
async def delete_file_route(
    file_id: str,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete a file (and hard-remove its bytes). 404 if not found."""
    await file_service.delete_file(db, file_id)
    await db.commit()


# ===========================================================================
# Developer surface — /api/v1/files (OpenAI-compatible, API key)
# ===========================================================================
# No prefix on the router: paths carry the full /api/v1 so an OpenAI SDK pointed at
# {ORIGIN}/api/v1 resolves files.* correctly (matches custom_apis.py's /api/v1/*).
v1_router = APIRouter(tags=["Public API · Files"])


@v1_router.post("/api/v1/files")
async def v1_upload_file(
    file: UploadFile = File(...),
    purpose: Optional[str] = Form(None),
    api_key: dict = Depends(get_current_api_key),
    db: AsyncSession = Depends(get_db),
):
    """OpenAI-compatible ``POST /v1/files`` (multipart ``file=`` + ``purpose=``).

    Returns ``{id, object:"file", bytes, created_at, filename, purpose, status}``.
    Same file_service substrate as the dashboard path (source=api). ``files:write``
    scope enforced when the key carries scopes."""
    check_api_key_scope(api_key, "files", "write")
    raw = await file.read()
    f = await file_service.create_file(
        db,
        raw=raw,
        filename=file.filename or "file",
        content_type=file.content_type or "application/octet-stream",
        source="api",
        user_id=api_key.get("user_id"),
        purpose=purpose,
        is_admin=False,
    )
    await db.commit()
    return file_service.open_serialize(f)


@v1_router.get("/api/v1/files")
async def v1_list_files(
    purpose: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    api_key: dict = Depends(get_current_api_key),
    db: AsyncSession = Depends(get_db),
):
    """OpenAI-compatible ``GET /v1/files`` — list the owner's files.

    ``purpose`` is accepted for OpenAI parity (it filters by the ``purpose`` tag)."""
    check_api_key_scope(api_key, "files", "read")
    files = await file_service.list_files(db, limit=limit, offset=offset)
    if purpose:
        files = [f for f in files if (f.purpose or "") == purpose]
    return {"object": "list", "data": [file_service.open_serialize(f) for f in files]}


@v1_router.get("/api/v1/files/{file_id}")
async def v1_get_file(
    file_id: str,
    api_key: dict = Depends(get_current_api_key),
    db: AsyncSession = Depends(get_db),
):
    """OpenAI-compatible ``GET /v1/files/{id}`` — file metadata."""
    check_api_key_scope(api_key, "files", "read")
    f = await file_service.get_file(db, file_id)
    return file_service.open_serialize(f)


@v1_router.get("/api/v1/files/{file_id}/content")
async def v1_get_file_content(
    file_id: str,
    api_key: dict = Depends(get_current_api_key),
    db: AsyncSession = Depends(get_db),
):
    """OpenAI-compatible ``GET /v1/files/{id}/content`` — the raw bytes (302 →
    presigned GET, or same-origin proxy fallback). Ownership-checked."""
    check_api_key_scope(api_key, "files", "read")
    return await _serve_content(db, file_id)


@v1_router.delete("/api/v1/files/{file_id}")
async def v1_delete_file(
    file_id: str,
    api_key: dict = Depends(get_current_api_key),
    db: AsyncSession = Depends(get_db),
):
    """OpenAI-compatible ``DELETE /v1/files/{id}`` →
    ``{id, object:"file", deleted:true}``."""
    check_api_key_scope(api_key, "files", "write")
    await file_service.delete_file(db, file_id)
    await db.commit()
    return {"id": file_id, "object": "file", "deleted": True}
