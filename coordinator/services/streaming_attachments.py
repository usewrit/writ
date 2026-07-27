"""
Streaming attachment resolution — folds a chat turn's file inputs into model context.

A chat / responses turn on the streaming surface can carry two kinds of file input:

  1. **Inline attachments** — base64 / data-URL / http image, file, audio and
     document parts, already parsed out of the message content by
     ``services.streaming_tool_bridge.extract_attachments``.
  2. **File references** — content parts that point at a stored ``file_id`` in the
     file library (``file_<uuid4hex>``) instead of inlining the bytes.

This module turns both into the two lists a turn needs:

  - ``images``  : data / URL strings handed to the model as multimodal (vision)
    context.
  - ``files``   : per-session file descriptors ``{file_id, filename,
    content_type, size, url}`` usable for ``set_input_files`` on the page (each
    carries a short-TTL signed ``url`` the agent fetches the bytes from).

Resolution
----------
The streaming surface runs **in-process** with database access, so we resolve
files directly through ``services.file_service`` against the request's
``AsyncSession``:

  - resolve ``file_id`` → ``file_service.get_file`` + ``file_service.resolve_for_run``
    (fail-closed 404 if the id is unknown or soft-deleted);
  - persist an inline attachment → ``file_service.create_file(source="streaming")``
    (size / content-type validated backend-authoritatively there);
  - inline a small image into vision context → fetch the stored bytes via
    ``services.visual_storage`` and base64-encode them (the coordinator's
    ``resolve_for_run`` hands back a signed url, not inline ``data``).

All resolution is best-effort: any single failure (missing id, storage hiccup,
oversize / disallowed type) is logged and the turn proceeds without that file —
attachments are grounding, never a hard dependency.
"""
from __future__ import annotations

import base64
import logging
from typing import Any, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from services import file_service, visual_storage

logger = logging.getLogger(__name__)

# Inline base64 ceiling for a referenced file_id we fold into the model context as
# an image. Larger files are passed by signed url only (the agent fetches them for
# set_input_files); we never base64 a large payload into the prompt.
_INLINE_MAX_BYTES = 6 * 1024 * 1024
_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def extract_file_id_refs(messages: List[dict]) -> List[str]:
    """Collect ``file_id`` references from the LAST user message's content parts.

    Supports the shapes a caller may use to point at a library file instead of
    inlining bytes:
      - Responses API:  {"type": "input_file", "file_id": "file_..."}
      - Chat file part: {"type": "file", "file": {"file_id": "file_..."}}
      - Image-by-id:    {"type": "input_image", "file_id": "file_..."}
                        {"type": "image_url", "image_url": {"file_id": "file_..."}}
    Only the most recent user turn is scanned (mirrors extract_attachments)."""
    last_user: Optional[dict] = None
    for msg in reversed(messages or []):
        if msg.get("role") == "user":
            last_user = msg
            break
    if not last_user:
        return []
    content = last_user.get("content")
    if not isinstance(content, list):
        return []

    ids: List[str] = []

    def _add(v: Any) -> None:
        if isinstance(v, str) and v.startswith("file_") and v not in ids:
            ids.append(v)

    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type", "")
        _add(part.get("file_id"))
        if ptype == "file":
            fi = part.get("file", {})
            if isinstance(fi, dict):
                _add(fi.get("file_id"))
        if ptype in ("image_url", "input_image"):
            img = part.get("image_url", {})
            if isinstance(img, dict):
                _add(img.get("file_id"))
    return ids


def _inline_image_data_url(f, content_type: str) -> Optional[str]:
    """Base64 a small stored IMAGE into a ``data:`` URL for vision context.

    Returns None when the file is not an image, exceeds the inline ceiling, or the
    bytes can't be fetched — the caller then relies on the signed-url descriptor
    only. Never raises."""
    if content_type not in _IMAGE_TYPES:
        return None
    if int(getattr(f, "size_bytes", 0) or 0) > _INLINE_MAX_BYTES:
        return None
    try:
        raw, _ct = visual_storage.fetch_file_bytes(f.storage_key, provider=None)
    except Exception as e:  # storage hiccup — skip inlining, keep the signed url
        logger.debug("streaming_attachments: inline fetch failed for %s: %s", f.id, e)
        return None
    if not raw:
        return None
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{content_type};base64,{b64}"


async def resolve_file_id(
    db: AsyncSession, file_id: str, *, inline: bool,
) -> Optional[dict]:
    """Resolve one ``file_id`` via ``file_service``.

    Returns a descriptor ``{file_id, filename, content_type, size, url, data?}`` or
    None when the id isn't owned / resolvable (fail-closed 404 in file_service).
    When ``inline`` is set and the file is a small image, ``data`` carries a base64
    ``data:`` URL for vision context. Never raises — a bad id just yields no
    attachment."""
    if not file_id:
        return None
    try:
        f = await file_service.get_file(db, file_id)
    except HTTPException as e:  # 404 fail-closed for unknown / deleted ids
        logger.debug("streaming_attachments: file resolve %s → %s (skipped)", file_id, e.status_code)
        return None
    except Exception as e:  # unexpected — degrade, don't fail the turn
        logger.warning("streaming_attachments: file resolve %s failed: %s", file_id, e)
        return None

    try:
        ttl = int(settings.file_signed_url_ttl_seconds or 600)
    except Exception:
        ttl = 600
    signed = visual_storage.presigned_file_get(f.storage_key, expires_seconds=ttl, provider=None)

    descriptor = {
        "file_id": f.id,
        "filename": f.filename,
        "content_type": f.content_type,
        "size": int(f.size_bytes or 0),
        "url": signed,
    }
    if inline:
        ct = (f.content_type or "").split(";")[0].strip().lower()
        data_url = _inline_image_data_url(f, ct)
        if data_url:
            descriptor["data"] = data_url
    return descriptor


async def persist_streaming_attachment(
    db: AsyncSession, *, data: str, filename: str, content_type: str,
) -> Optional[dict]:
    """Persist an inline base64 attachment as a StoredFile (source="streaming") so it
    gets a stable file_id usable for set_input_files and shows up in the library.

    ``data`` is either a bare base64 string or a ``data:...;base64,<payload>`` URL.
    Returns the resolved descriptor ``{file_id, filename, content_type, size, url}``
    or None on failure. Size / content-type are enforced backend-side in
    ``file_service.create_file`` (raises HTTPException 413/415, treated as skip)."""
    if not data:
        return None
    # Accept both a data: URL and a bare base64 payload.
    payload = data
    if payload.startswith("data:") and "," in payload:
        header, payload = payload.split(",", 1)
        # Prefer the mime declared in the data: URL header when the caller didn't
        # pass an explicit content_type.
        if (not content_type or content_type == "application/octet-stream") and ":" in header:
            declared = header.split(":", 1)[1].split(";", 1)[0].strip()
            if declared:
                content_type = declared
    try:
        raw = base64.b64decode(payload, validate=False)
    except Exception as e:
        logger.info("streaming_attachments: attachment persist skipped (bad base64: %s)", e)
        return None

    try:
        f = await file_service.create_file(
            db,
            raw=raw,
            filename=filename or "attachment",
            content_type=content_type or "application/octet-stream",
            source="streaming",
        )
        await db.commit()
    except HTTPException as e:  # 413/415/400 — oversize / disallowed type / empty
        detail = e.detail if isinstance(e.detail, (str, dict)) else str(e.detail)
        logger.info("streaming_attachments: attachment persist rejected (%s: %s)", e.status_code, detail)
        return None
    except Exception as e:
        logger.warning("streaming_attachments: attachment persist failed: %s", e)
        return None

    return {
        "file_id": f.id,
        "filename": f.filename,
        "content_type": f.content_type,
        "size": int(f.size_bytes or 0),
        "url": visual_storage.presigned_file_get(
            f.storage_key, expires_seconds=int(settings.file_signed_url_ttl_seconds or 600), provider=None,
        ),
    }


async def resolve_turn_attachments(
    db: AsyncSession,
    messages: List[dict],
    attachments: List[dict],
) -> Tuple[List[str], List[dict]]:
    """Build the per-turn (``images``, ``files``) lists from BOTH inline attachments
    (already parsed by ``streaming_tool_bridge.extract_attachments``) and ``file_id``
    references in the last user message.

    Returns:
      - ``image_urls``: data / URL strings to forward to the model as vision context.
      - ``session_files``: file descriptors ``{file_id, filename, content_type,
        size, url}`` for ``set_input_files`` on the page. Inline file / document /
        audio attachments are persisted (source="streaming") so they too get a
        stable file_id + signed url the agent can fetch.

    Best-effort throughout: any single resolution / persist failure is logged and
    the turn proceeds without that file."""
    image_urls: List[str] = []
    session_files: List[dict] = []
    seen_ids: set = set()

    # (1) Inline attachments parsed from the message content (data: / http URLs).
    for a in attachments or []:
        atype = a.get("type")
        url = a.get("url") or ""
        if atype == "image":
            if url:
                image_urls.append(url)
        elif atype in ("file", "audio"):
            # Persist so the file is usable for set_input_files (needs a real path on
            # the agent, fetched from a signed url) and lands in the library.
            if url.startswith("data:"):
                desc = await persist_streaming_attachment(
                    db, data=url, filename=a.get("name") or "attachment",
                    content_type=a.get("mime") or "application/octet-stream",
                )
                if desc:
                    session_files.append(desc)

    # (2) file_id references → resolve (inline images into vision; everything gets a
    # session file descriptor for upload steps).
    for fid in extract_file_id_refs(messages):
        if fid in seen_ids:
            continue
        seen_ids.add(fid)
        desc = await resolve_file_id(db, fid, inline=True)
        if not desc:
            continue
        data = desc.get("data")
        if data:  # already a data: URL from the inline path
            image_urls.append(data)
        # Hand the descriptor (sans bulky base64) to the agent for set_input_files.
        session_files.append({
            "file_id": desc.get("file_id"),
            "filename": desc.get("filename"),
            "content_type": desc.get("content_type"),
            "size": desc.get("size"),
            "url": desc.get("url"),
        })

    return image_urls, session_files


__all__ = [
    "extract_file_id_refs",
    "resolve_file_id",
    "persist_streaming_attachment",
    "resolve_turn_attachments",
]
