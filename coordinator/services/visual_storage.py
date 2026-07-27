"""
Visual snapshot + file-asset storage — region screenshots, diff overlays and
StoredFile bytes.

Binary blobs are stored OUTSIDE the database whenever possible so change history
and file uploads don't bloat the coordinator's SQLite file. Stored *references*
are opaque strings:

  - ``minio:<bucket>/<key>``  → an object in MinIO/S3; the API presigns a GET URL.
  - ``file:<relative/path>``  → a file under the local WRIT_FILES_DIR root; the
                                API streams the bytes through its own
                                (auth-checked) proxy routes.
  - ``<raw base64>``          → legacy / fallback when no object store is
                                configured; the API turns it into a ``data:`` URL
                                or decodes it inline.

BACKEND SELECTION (resolved once at startup, see ``storage_backend()``):

  1. Explicit MinIO env config (MINIO_ENDPOINT + MINIO_ACCESS_KEY +
     MINIO_SECRET_KEY all set)          → MinIO/S3 object storage.
  2. WRIT_FILES_DIR set (the shipped single-container Docker setup sets it to
     /data/files)                        → local-filesystem backend.
  3. Neither                             → base64-in-DB fallback, with a ONE-TIME
     startup warning (large files land inside SQLite; set WRIT_FILES_DIR).

No MinIO client is ever constructed — and therefore no ``minio:9000`` DNS
lookup ever happens — unless MinIO is explicitly configured.

BACKWARD COMPATIBILITY: rows already holding raw-base64 blobs (the historical
fallback) keep working forever — every read path (``fetch_snapshot_bytes``,
``fetch_file_bytes``, ``url_for``) still decodes a base64 ref inline. No
migration is required; new stores go to disk (or MinIO), old blobs stay
readable in place.

Everything here is best-effort and fails soft: if the chosen store or Pillow is
missing or errors, we fall back to base64 so visual change detection is never
broken by a storage hiccup. For MinIO, two clients are used — an INTERNAL one
(``MINIO_ENDPOINT``) for put/get inside the cluster, and a PUBLIC one
(``MINIO_PUBLIC_ENDPOINT``) used only to sign URLs the browser can reach.
"""
from __future__ import annotations

import base64
import binascii
import io
import logging
import mimetypes
import os
import re
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_BUCKET = os.getenv("MINIO_VISUAL_BUCKET", "visual-snapshots")
# Dedicated bucket for file assets (StoredFile bytes) — kept separate from
# visual snapshots so its lifecycle/policy is independent. Read from env directly
# (mirrors _BUCKET) with the same default as Settings.minio_files_bucket.
_FILES_BUCKET = os.getenv("MINIO_FILES_BUCKET", "tenant-files")
_REF_PREFIX = "minio:"
_FS_REF_PREFIX = "file:"

_internal_client = None  # for put/get (in-cluster endpoint)
_public_client = None    # for presigning (browser-reachable endpoint)
_bucket_ready = False
_files_bucket_ready = False  # the files bucket make_bucket guard
_unavailable = False     # set once MinIO is found unusable; skip retries

_backend_choice: Optional[str] = None  # resolved once: "minio" | "fs" | "db"


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------
def _fs_root() -> Optional[Path]:
    """Root directory of the local-filesystem backend (WRIT_FILES_DIR), or None
    when unset. Prefers the validated Settings value; falls back to the raw env
    var so this module stays usable standalone."""
    raw = ""
    try:
        from config import settings
        raw = (settings.writ_files_dir or "").strip()
    except Exception:  # noqa: BLE001 — settings unavailable (tests/tools)
        pass
    if not raw:
        raw = (os.getenv("WRIT_FILES_DIR") or "").strip()
    return Path(raw) if raw else None


def storage_backend() -> str:
    """Resolve the active storage backend ONCE per process.

    Order: explicit MinIO env config (MINIO_ENDPOINT **and** both credential
    keys present — the ``minio:9000`` default is never assumed) → ``"minio"``;
    else WRIT_FILES_DIR set → ``"fs"``; else ``"db"`` (base64 blobs inside
    SQLite rows) with a one-time startup WARNING. Because no MinIO client is
    built outside the ``"minio"`` branch, an unconfigured deployment never
    performs a failing per-call DNS lookup."""
    global _backend_choice
    if _backend_choice is not None:
        return _backend_choice
    endpoint = (os.getenv("MINIO_ENDPOINT") or "").strip()
    access = (os.getenv("MINIO_ACCESS_KEY") or "").strip()
    secret = (os.getenv("MINIO_SECRET_KEY") or "").strip()
    if endpoint and access and secret:
        _backend_choice = "minio"
    elif _fs_root() is not None:
        _backend_choice = "fs"
    else:
        _backend_choice = "db"
        logger.warning(
            "visual_storage: no object store configured — screenshots and file "
            "uploads will be stored as base64 blobs INSIDE the SQLite database "
            "and will bloat writ.db over time. Set WRIT_FILES_DIR to a writable "
            "directory (the shipped Docker setup uses /data/files) to store "
            "them on disk instead, or configure MINIO_ENDPOINT + "
            "MINIO_ACCESS_KEY + MINIO_SECRET_KEY for object storage."
        )
    return _backend_choice


# ---------------------------------------------------------------------------
# Local-filesystem backend (WRIT_FILES_DIR)
#
# Objects live under two separated subtrees: ``visuals/`` (region screenshots,
# diff overlays, covers) and ``uploads/`` (StoredFile bytes). Keys are ALWAYS
# server-generated — an opaque UUID with a two-level fan-out
# (``visuals/ab/abcdef….png``) so no directory grows unboundedly and no
# caller-supplied path ever reaches the filesystem. Refs are still re-validated
# for containment on every read/write/delete (defense in depth against a
# tampered DB ref). Writes are atomic: tmp file in the target dir + rename.
# ---------------------------------------------------------------------------
_FS_SUFFIX_RE = re.compile(r"\.[A-Za-z0-9]{1,8}$")


def _fs_suffix(key: str = "", content_type: str = "") -> str:
    """Pick a safe file suffix from the (server-generated) object key or the
    content type; ``.bin`` when neither yields one."""
    m = _FS_SUFFIX_RE.search(key or "")
    if m:
        return m.group(0).lower()
    if content_type:
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if ext:
            return ext
    return ".bin"


def _fs_resolve(relpath: str) -> Optional[Path]:
    """Resolve a relative object path against the WRIT_FILES_DIR root and VERIFY
    containment. Returns None (never raises) for an unset root, an absolute
    path, any ``..`` component, or a resolved path escaping the root."""
    root = _fs_root()
    if root is None or not relpath:
        return None
    if relpath.startswith(("/", "\\")) or ".." in relpath.replace("\\", "/").split("/"):
        return None
    try:
        root_r = root.resolve()
        p = (root_r / relpath).resolve()
        if not p.is_relative_to(root_r):
            return None
        return p
    except (OSError, ValueError):
        return None


def _fs_store(raw: bytes, category: str, suffix: str) -> Optional[str]:
    """Atomically write ``raw`` under WRIT_FILES_DIR and return a ``file:`` ref.

    The key is server-generated (UUID hex) with a two-level fan-out directory
    (``<category>/ab/abcdef….<suffix>``). Write is tmp-file + ``os.replace`` in
    the same directory, so a crash never leaves a half-written object at the
    final path. Returns None on any I/O failure (caller falls back)."""
    name = uuid.uuid4().hex
    rel = f"{category}/{name[:2]}/{name}{suffix}"
    path = _fs_resolve(rel)
    if path is None:
        return None
    tmp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex[:8]}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)  # atomic within one directory
        return f"{_FS_REF_PREFIX}{rel}"
    except OSError as e:
        logger.warning(f"visual_storage: filesystem store failed for {rel} ({e})")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def _fs_read(ref: str) -> Optional[bytes]:
    """Read a ``file:`` ref back to bytes; None when missing/invalid."""
    path = _fs_resolve(ref[len(_FS_REF_PREFIX):])
    if path is None:
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def _fs_delete(ref: str) -> None:
    """Best-effort delete of a ``file:`` ref (missing file is a no-op)."""
    path = _fs_resolve(ref[len(_FS_REF_PREFIX):])
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        logger.warning(f"visual_storage: filesystem delete failed for {ref} ({e})")


def _make_client(endpoint: str):
    from minio import Minio
    secure = endpoint.startswith("https://")
    host = endpoint.replace("https://", "").replace("http://", "")
    # Pin the region so presigned_get_object signs locally instead of probing
    # GetBucketLocation — the public endpoint (e.g. localhost:9000) isn't
    # reachable from inside the cluster, only from the browser.
    # No shipped credential defaults — MinIO is an OPTIONAL external store for
    # visual-diff screenshots (the module fails soft when unset/unreachable). A
    # baked-in default secret would be a well-known credential in a public repo,
    # so require the operator to supply both keys via env when they opt in.
    return Minio(
        host,
        access_key=os.getenv("MINIO_ACCESS_KEY") or "",
        secret_key=os.getenv("MINIO_SECRET_KEY") or "",
        secure=secure,
        region=os.getenv("MINIO_REGION", "us-east-1"),
    )


def _clients():
    """Return (internal, public) clients or (None, None) if MinIO is unusable.

    Hard-gated on the resolved backend: when MinIO is not EXPLICITLY configured
    (backend "fs"/"db") no client is ever constructed, so the historical
    per-call failing DNS lookup of ``minio:9000`` cannot happen."""
    global _internal_client, _public_client, _bucket_ready, _unavailable
    if storage_backend() != "minio":
        return None, None
    if _unavailable:
        return None, None
    try:
        if _internal_client is None:
            # Backend "minio" guarantees MINIO_ENDPOINT is explicitly set (see
            # storage_backend) — no shipped default endpoint is ever assumed.
            endpoint = os.getenv("MINIO_ENDPOINT", "")
            _internal_client = _make_client(endpoint)
            # Public endpoint for presigned URLs the browser can reach. Defaults
            # to localhost:9000 (dev) — set MINIO_PUBLIC_ENDPOINT in prod.
            public = os.getenv("MINIO_PUBLIC_ENDPOINT", "localhost:9000")
            _public_client = _make_client(public) if public != endpoint else _internal_client
        if not _bucket_ready:
            if not _internal_client.bucket_exists(_BUCKET):
                _internal_client.make_bucket(_BUCKET)
            _bucket_ready = True
        return _internal_client, _public_client
    except Exception as e:  # noqa: BLE001 — any failure → base64 fallback
        logger.warning(f"visual_storage: MinIO unavailable, falling back to base64 ({e})")
        _unavailable = True
        return None, None


def _decode_b64(b64: str) -> Optional[bytes]:
    try:
        return base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return None


def _public_base_url() -> str:
    """Browser-reachable base URL (scheme + host) for the MinIO public endpoint.

    Mirrors the scheme handling in ``_make_client``: ``MINIO_PUBLIC_ENDPOINT`` may
    carry a scheme (prod) or be a bare host:port (dev, defaults to localhost:9000).
    """
    public = os.getenv("MINIO_PUBLIC_ENDPOINT", "localhost:9000")
    if public.startswith("http://") or public.startswith("https://"):
        return public.rstrip("/")
    return f"http://{public.rstrip('/')}"


def store_png_b64(b64: Optional[str], key: str) -> Optional[str]:
    """Store a base64 PNG and return a reference.

    Backend "minio" → uploads and returns ``minio:bucket/key``. Backend "fs" →
    writes under ``WRIT_FILES_DIR/visuals/`` (server-generated UUID fan-out key;
    the caller's ``key`` only informs the suffix) and returns ``file:<relpath>``.
    Otherwise returns the raw base64 unchanged (DB-blob fallback — and the read
    path keeps decoding such legacy values forever)."""
    if not b64:
        return None
    if storage_backend() == "fs":
        raw = _decode_b64(b64)
        if raw is None:
            return b64
        return _fs_store(raw, "visuals", _fs_suffix(key, "image/png")) or b64
    internal, _ = _clients()
    if internal is None:
        return b64  # fallback: keep base64 inline
    raw = _decode_b64(b64)
    if raw is None:
        return b64
    try:
        internal.put_object(
            _BUCKET, key, io.BytesIO(raw), length=len(raw), content_type="image/png",
        )
        return f"{_REF_PREFIX}{_BUCKET}/{key}"
    except Exception as e:  # noqa: BLE001
        logger.warning(f"visual_storage: put_object failed for {key} ({e})")
        return b64


def store_public_image(raw: bytes, key: str, content_type: str) -> Optional[str]:
    """Store raw image bytes (e.g. a listing cover screenshot) and return a STABLE,
    public ``https://`` URL the browser can load directly.

    Unlike ``store_png_b64`` (which returns an opaque ``minio:`` ref that the API
    later presigns with a short TTL), this returns a durable URL built from the
    PUBLIC endpoint + bucket + key. The cover image is non-sensitive marketing
    content meant to be embedded long-term in a listing card, so it's served from
    a bucket reachable by the browser (set MINIO bucket policy to public-read, or
    front it with a CDN, in prod). Returns None when MinIO is unavailable — the
    caller should surface a failure rather than store inline base64 here (a cover
    image embedded as base64 in the listing would bloat every browse payload).
    MinIO-only: the fs/db backends have no stable public URL to return (no
    callers in the self-host coordinator rely on this helper)."""
    if not raw:
        return None
    internal, _ = _clients()
    if internal is None:
        return None
    try:
        internal.put_object(
            _BUCKET, key, io.BytesIO(raw), length=len(raw),
            content_type=content_type or "application/octet-stream",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"visual_storage: put_object failed for public image {key} ({e})")
        return None
    # Build a stable public URL from the browser-reachable endpoint.
    return f"{_public_base_url()}/{_BUCKET}/{key}"


def fetch_public_image(key: str):
    """Read a public-image object (e.g. a listing cover) back from MinIO via the
    INTERNAL client and return ``(raw_bytes, content_type)`` — or ``None`` when the
    object is missing or storage is unavailable.

    Used by the cover-image PROXY route so the browser loads covers from a clean
    same-origin app URL (no presign, no X-Amz tokens, no public-bucket requirement
    — the API reaches MinIO in-cluster). Unlike ``_fetch_bytes`` this takes a BARE
    object key (not a ``minio:`` ref) and preserves the stored content-type rather
    than assuming PNG."""
    if not key:
        return None
    internal, _ = _clients()
    if internal is None:
        return None
    resp = None
    try:
        resp = internal.get_object(_BUCKET, key)
        raw = resp.read()
        ctype = None
        try:
            ctype = resp.headers.get("Content-Type") or resp.headers.get("content-type")
        except Exception:
            ctype = None
        return (raw, ctype or "application/octet-stream")
    except Exception as e:  # noqa: BLE001 — missing object / storage hiccup
        logger.warning(f"visual_storage: fetch_public_image failed for {key} ({e})")
        return None
    finally:
        try:
            if resp is not None:
                resp.close()
                resp.release_conn()
        except Exception:
            pass


def _fetch_bytes(ref: Optional[str]) -> Optional[bytes]:
    """Resolve a stored reference back to PNG bytes (``file:`` path, MinIO
    object, or legacy base64 — old base64-in-DB rows stay readable unchanged)."""
    if not ref:
        return None
    if ref.startswith(_FS_REF_PREFIX):
        return _fs_read(ref)
    if ref.startswith(_REF_PREFIX):
        internal, _ = _clients()
        if internal is None:
            return None
        bucket, _, key = ref[len(_REF_PREFIX):].partition("/")
        try:
            resp = internal.get_object(bucket, key)
            try:
                return resp.read()
            finally:
                resp.close()
                resp.release_conn()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"visual_storage: get_object failed for {ref} ({e})")
            return None
    return _decode_b64(ref)


# ---------------------------------------------------------------------------
# Crawl page thumbnails
#
# A crawl shard ships a light JPEG thumbnail per browser-rendered page as inline
# base64. We move it OUT of the wire payload into storage before the shard row is
# persisted, and serve it back through an authenticated same-origin route keyed by
# (crawl_id, token). The key is therefore DETERMINISTIC on both backends — unlike
# ``store_png_b64``'s UUID fan-out — so the proxy route can rebuild it from the URL
# without any ref ever landing in the crawl's result rows.
#
# The token is server-generated (``secrets.token_urlsafe``); it is re-validated
# here anyway so a tampered path can never escape the storage root.
# ---------------------------------------------------------------------------
_THUMB_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def _crawl_thumb_key(crawl_id, token: str) -> Optional[str]:
    """Object key / fs relpath for a crawl page thumbnail — namespaced by crawl id
    so the authenticated proxy can rebuild it from (crawl_id, token). None when
    either component is unusable (the caller then skips the thumbnail)."""
    try:
        cid = int(crawl_id)
    except (TypeError, ValueError):
        return None
    if cid <= 0 or not token or not _THUMB_TOKEN_RE.match(str(token)):
        return None
    return f"crawl/{cid}/{token}.jpg"


def store_crawl_thumbnail_b64(b64: Optional[str], crawl_id, token: str) -> bool:
    """Store a crawl page thumbnail (base64 JPEG) under its deterministic key.
    Returns True when the bytes are retrievable by ``fetch_crawl_thumbnail``.

    Best-effort by design: with no object store AND no WRIT_FILES_DIR (the "db"
    backend) we simply DECLINE the thumbnail rather than inlining base64 bloat
    into every crawl result row — the results UI falls back to the page favicon."""
    key = _crawl_thumb_key(crawl_id, token)
    if not b64 or not key:
        return False
    raw = _decode_b64(b64)
    if raw is None:
        return False

    backend = storage_backend()
    if backend == "fs":
        path = _fs_resolve(f"visuals/{key}")
        if path is None:
            return False
        tmp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex[:8]}")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "wb") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)  # atomic within one directory
            return True
        except OSError as e:  # noqa: BLE001
            logger.warning(f"visual_storage: crawl thumbnail write failed for {key} ({e})")
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    if backend != "minio":
        return False
    internal, _ = _clients()
    if internal is None:
        return False
    try:
        internal.put_object(
            _BUCKET, key, io.BytesIO(raw), length=len(raw), content_type="image/jpeg",
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"visual_storage: crawl thumbnail put failed for {key} ({e})")
        return False


def fetch_crawl_thumbnail(crawl_id, token: str) -> Optional[bytes]:
    """Read a stored crawl page thumbnail back (JPEG bytes) for the authenticated
    same-origin proxy route. None when the object is missing or storage is down."""
    key = _crawl_thumb_key(crawl_id, token)
    if not key:
        return None
    if storage_backend() == "fs":
        return _fs_read(f"{_FS_REF_PREFIX}visuals/{key}")
    return _fetch_bytes(f"{_REF_PREFIX}{_BUCKET}/{key}")


def _crawl_favicon_key(crawl_id) -> Optional[str]:
    """Object key / fs relpath for a crawl's SITE favicon — one per crawl (a crawl
    is one site), namespaced by crawl id so the authenticated proxy serves it
    same-origin without ever exposing the raw storage ref."""
    try:
        cid = int(crawl_id)
    except (TypeError, ValueError):
        return None
    return f"crawl/{cid}/favicon.img" if cid > 0 else None


def store_crawl_favicon(raw: Optional[bytes], content_type: Optional[str], crawl_id) -> bool:
    """Cache a crawl's site favicon (raw image bytes) under a crawl-namespaced key
    so subsequent loads serve from storage instead of re-fetching the site.

    Best-effort: a failed put just means the next request re-resolves + re-caches
    (or falls back to the globe glyph). Returns True on a successful store. With
    neither an object store nor WRIT_FILES_DIR the favicon simply isn't cached —
    it is re-resolved per request rather than bloating the database."""
    key = _crawl_favicon_key(crawl_id)
    if not raw or not key:
        return False

    backend = storage_backend()
    if backend == "fs":
        path = _fs_resolve(f"visuals/{key}")
        if path is None:
            return False
        tmp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex[:8]}")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "wb") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            return True
        except OSError as e:  # noqa: BLE001
            logger.warning(f"visual_storage: crawl favicon write failed for {key} ({e})")
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    if backend != "minio":
        return False
    internal, _ = _clients()
    if internal is None:
        return False
    try:
        internal.put_object(
            _BUCKET, key, io.BytesIO(raw), length=len(raw),
            content_type=(content_type or "image/x-icon"),
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"visual_storage: crawl favicon put failed for {key} ({e})")
        return False


def fetch_crawl_favicon(crawl_id) -> Optional[bytes]:
    """Read a crawl's cached site favicon back (raw image bytes) for the
    authenticated same-origin proxy. None when not yet cached or storage is down."""
    key = _crawl_favicon_key(crawl_id)
    if not key:
        return None
    if storage_backend() == "fs":
        return _fs_read(f"{_FS_REF_PREFIX}visuals/{key}")
    return _fetch_bytes(f"{_REF_PREFIX}{_BUCKET}/{key}")


def fetch_snapshot_bytes(ref: Optional[str]) -> Optional[bytes]:
    """Public resolver: a stored visual-snapshot ref (``minio:`` object OR raw
    base64 fallback) back to PNG bytes, for the same-origin snapshot PROXY route.

    The browser can't load a presigned MinIO URL (the public endpoint is often
    unreachable / HTTPS-mixed-content) and an ``<img>`` can't send the API's Bearer
    token — so visual snapshots are streamed through an authenticated API route
    that blob-fetches these bytes. Returns None when the ref is empty/unresolvable."""
    return _fetch_bytes(ref)


def compute_diff_overlay_b64(before_ref: Optional[str], after_b64: Optional[str]) -> Optional[str]:
    """Build a pixel-delta overlay: the "after" image with changed pixels tinted,
    returned as base64 PNG. Needs both images + Pillow; returns None otherwise."""
    after_raw = _decode_b64(after_b64) if after_b64 else None
    before_raw = _fetch_bytes(before_ref)
    if not after_raw or not before_raw:
        return None
    try:
        from PIL import Image, ImageChops
        before = Image.open(io.BytesIO(before_raw)).convert("RGB")
        after = Image.open(io.BytesIO(after_raw)).convert("RGB")
        if before.size != after.size:
            before = before.resize(after.size)
        # Per-pixel difference → grayscale magnitude → threshold to a change mask.
        diff = ImageChops.difference(before, after).convert("L")
        mask = diff.point(lambda p: 255 if p > 24 else 0)
        # Tint the changed pixels on top of the "after" image (monochrome accent).
        overlay = after.copy()
        tint = Image.new("RGB", after.size, (17, 17, 17))  # near-black "ink"
        overlay.paste(tint, (0, 0), mask)
        # Blend so the underlying content stays visible under the highlight.
        out = Image.blend(after, overlay, 0.45)
        buf = io.BytesIO()
        out.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"visual_storage: diff overlay failed ({e})")
        return None


def seed_baseline(after_b64: Optional[str], selector_id) -> Optional[str]:
    """First-run: store the baseline region image, return its reference."""
    if not after_b64:
        return None
    import uuid
    return store_png_b64(after_b64, f"sel-{selector_id}/baseline-{uuid.uuid4().hex[:12]}.png")


def process_change(before_ref: Optional[str], after_b64: Optional[str], selector_id):
    """On change: store the "after" image and a before/after pixel-delta overlay.
    Returns ``(after_ref, diff_ref)`` (either may be None)."""
    if not after_b64:
        return None, None
    import uuid
    sid = uuid.uuid4().hex[:12]
    diff_b64 = compute_diff_overlay_b64(before_ref, after_b64)
    after_ref = store_png_b64(after_b64, f"sel-{selector_id}/after-{sid}.png")
    diff_ref = store_png_b64(diff_b64, f"sel-{selector_id}/diff-{sid}.png") if diff_b64 else None
    return after_ref, diff_ref


def url_for(ref: Optional[str], expires_seconds: int = 3600) -> Optional[str]:
    """Resolve a stored reference to something an <img> can load: a presigned
    MinIO GET URL, or a ``data:`` URL for legacy base64 / local-filesystem refs
    (a ``file:`` object has no browser-reachable host to presign against — the
    preferred serving path is the authenticated byte-proxy routes that call
    ``fetch_snapshot_bytes``; this inline form is only for parity)."""
    if not ref:
        return None
    if ref.startswith(_FS_REF_PREFIX):
        raw = _fs_read(ref)
        if raw is None:
            return None
        return f"data:image/png;base64,{base64.b64encode(raw).decode('ascii')}"
    if ref.startswith(_REF_PREFIX):
        _, public = _clients()
        if public is None:
            return None
        bucket, _, key = ref[len(_REF_PREFIX):].partition("/")
        try:
            from datetime import timedelta
            return public.presigned_get_object(bucket, key, expires=timedelta(seconds=expires_seconds))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"visual_storage: presign failed for {ref} ({e})")
            return None
    # Legacy base64 → inline data URL.
    return f"data:image/png;base64,{ref}"


# ---------------------------------------------------------------------------
# File assets (StoredFile bytes) — local MinIO object store.
#
# These mirror the visual-snapshot helpers above but target a dedicated bucket
# and preserve the real content-type (visual snapshots are always PNG). The
# reference string format is identical: ``minio:<bucket>/<key>`` for an object,
# or a raw base64 string as the MinIO-down fallback (so file features degrade
# gracefully rather than break, matching store_png_b64).
#
# There is no external S3 provider selection, so every file helper stores in the
# local MinIO. The ``provider`` kwarg is accepted for call-site compatibility and
# is always ``None``.
# ---------------------------------------------------------------------------


def _files_clients():
    """Return (internal, public) clients with the ``tenant-files`` bucket ensured,
    or (None, None) if MinIO is unusable. Reuses the shared clients from
    ``_clients()`` (which also primes the visual bucket); only the extra
    make_bucket guard for the files bucket is added here.

    This is the LOCAL-MinIO leg of ``_files_store`` (provider is None and no env
    FILES_S3_* furnished)."""
    global _files_bucket_ready
    internal, public = _clients()
    if internal is None:
        return None, None
    if not _files_bucket_ready:
        try:
            if not internal.bucket_exists(_FILES_BUCKET):
                internal.make_bucket(_FILES_BUCKET)
            _files_bucket_ready = True
        except Exception as e:  # noqa: BLE001 — treat as unavailable for files
            logger.warning(
                f"visual_storage: tenant-files bucket unavailable ({e})"
            )
            return None, None
    return internal, public


def _files_store(provider=None):
    """Resolve the active storage backend for file assets.

    Returns ``(internal_client, public_client, bucket, public_base)``:
      - ``internal_client`` — put/get/head/delete client.
      - ``public_client``   — presigning client (browser-reachable host).
      - ``bucket``          — target bucket.
      - ``public_base``     — base URL (scheme+host, no bucket) for building
        presigned-POST URLs, or "" to use the public client's own host.

    Files always live in the local MinIO object store; there is no external S3
    provider selection. ``provider`` is accepted for call-site compatibility and
    is always ``None``.

    Returns ``(None, None, "", "")`` when the resolved backend is unusable — every
    helper then degrades the same way it did before (base64 fallback / None)."""
    # Local MinIO leg (the only backend).
    internal, public = _files_clients()
    if internal is None:
        return None, None, "", ""
    return internal, public, _FILES_BUCKET, ""


def store_file_bytes(raw: bytes, key: str, content_type: str, provider=None) -> str:
    """Store raw file bytes under ``key`` in the resolved storage backend and
    return an opaque reference ``minio:<bucket>/<key>``. ``provider`` (a resolved
    ProviderConfig) selects the backend; ``None`` falls back to env FILES_S3_* then
    local MinIO. Falls back to a raw base64 string when storage is unavailable (so
    file features degrade rather than fail), matching ``store_png_b64``.

    The returned ref embeds the resolving backend's bucket so the object is
    addressable; file_service also records the provider id alongside (the ref is a
    convenience, the recorded provider+key is authoritative — §10.A.5).

    Backend "fs": bytes land under ``WRIT_FILES_DIR/uploads/`` (separate from the
    ``visuals/`` subtree) with a server-generated UUID fan-out key; the returned
    ref is ``file:<relpath>``."""
    raw = raw or b""
    if storage_backend() == "fs":
        ref = _fs_store(raw, "uploads", _fs_suffix(key, content_type))
        if ref:
            return ref
        return base64.b64encode(raw).decode("ascii")
    internal, _public, bucket, _base = _files_store(provider)
    if internal is None or not bucket:
        # Fallback: inline base64 (degraded mode; no object store available).
        return base64.b64encode(raw).decode("ascii")
    try:
        internal.put_object(
            bucket, key, io.BytesIO(raw), length=len(raw),
            content_type=content_type or "application/octet-stream",
        )
        return f"{_REF_PREFIX}{bucket}/{key}"
    except Exception as e:  # noqa: BLE001
        logger.warning(f"visual_storage: store_file_bytes failed for {key} ({e})")
        return base64.b64encode(raw).decode("ascii")


def _split_files_ref(ref_or_key: str, default_bucket: str = _FILES_BUCKET) -> tuple[str, str]:
    """Resolve a ``minio:bucket/key`` ref OR a bare object key into
    ``(bucket, key)``. A ``minio:`` ref carries its own bucket (where the bytes
    were actually written); a bare key is assumed to live in ``default_bucket``
    (the resolved backend's bucket)."""
    if ref_or_key and ref_or_key.startswith(_REF_PREFIX):
        bucket, _, key = ref_or_key[len(_REF_PREFIX):].partition("/")
        return bucket, key
    return default_bucket, ref_or_key


def fetch_file_bytes(ref_or_key: str, provider=None) -> tuple[bytes, str]:
    """Read a stored file back and return ``(raw_bytes, content_type)``.

    ``provider`` (the file's RECORDED ProviderConfig, resolved upstream) selects
    the backend; ``None`` falls back to env FILES_S3_* then local MinIO. Accepts
    either a ``minio:bucket/key`` reference or a bare object key. When the ref is a
    raw base64 fallback string (no ``minio:`` prefix and not a resolvable object),
    the decoded bytes are returned with a generic content-type. Returns
    ``(b"", "application/octet-stream")`` when storage is unavailable / object
    missing."""
    if not ref_or_key:
        return b"", "application/octet-stream"
    if ref_or_key.startswith(_FS_REF_PREFIX):
        raw = _fs_read(ref_or_key)
        if raw is None:
            return b"", "application/octet-stream"
        ctype = mimetypes.guess_type(ref_or_key[len(_FS_REF_PREFIX):])[0]
        return raw, (ctype or "application/octet-stream")
    internal, _public, store_bucket, _base = _files_store(provider)
    # Non-ref strings that are not bucket keys may be the base64 fallback.
    if not ref_or_key.startswith(_REF_PREFIX):
        # Could be a bare object key OR an inline base64 fallback. Try storage
        # first (treating it as a key); if storage is down, decode as base64.
        if internal is None:
            decoded = _decode_b64(ref_or_key)
            if decoded is not None:
                return decoded, "application/octet-stream"
            return b"", "application/octet-stream"
    bucket, key = _split_files_ref(ref_or_key, store_bucket or _FILES_BUCKET)
    if internal is None:
        return b"", "application/octet-stream"
    resp = None
    try:
        resp = internal.get_object(bucket, key)
        raw = resp.read()
        ctype = None
        try:
            ctype = resp.headers.get("Content-Type") or resp.headers.get("content-type")
        except Exception:
            ctype = None
        return raw, (ctype or "application/octet-stream")
    except Exception as e:  # noqa: BLE001 — missing object / storage hiccup
        logger.warning(f"visual_storage: fetch_file_bytes failed for {ref_or_key} ({e})")
        # Last resort: the ref may have been a base64 fallback after all.
        if not ref_or_key.startswith(_REF_PREFIX):
            decoded = _decode_b64(ref_or_key)
            if decoded is not None:
                return decoded, "application/octet-stream"
        return b"", "application/octet-stream"
    finally:
        try:
            if resp is not None:
                resp.close()
                resp.release_conn()
        except Exception:
            pass


def presigned_file_get(ref_or_key: str, expires_seconds: int = 600, provider=None) -> Optional[str]:
    """Return a short-lived, single-object presigned GET URL for a stored file, or
    None when storage is unavailable / the ref is an inline base64 fallback.

    ``provider`` (the file's RECORDED ProviderConfig) selects the backend; ``None``
    falls back to env FILES_S3_* then local MinIO. Presigning uses the PUBLIC client
    (browser-reachable host); for an external provider the same client is public.

    A local-filesystem ``file:`` ref can't be presigned (no storage host) — returns
    None so the router serves the bytes through its same-origin, auth-checked
    proxy instead (files.py _serve_content fallback)."""
    if not ref_or_key:
        return None
    if ref_or_key.startswith(_FS_REF_PREFIX):
        return None
    if not ref_or_key.startswith(_REF_PREFIX):
        # Bare key → resolved backend bucket. A base64 fallback can't be presigned
        # (the caller should fall back to the same-origin content proxy instead).
        if _decode_b64(ref_or_key) is not None and "/" not in ref_or_key:
            return None
    _internal, public, store_bucket, _base = _files_store(provider)
    if public is None:
        return None
    bucket, key = _split_files_ref(ref_or_key, store_bucket or _FILES_BUCKET)
    if not key:
        return None
    try:
        from datetime import timedelta
        return public.presigned_get_object(
            bucket, key, expires=timedelta(seconds=expires_seconds),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"visual_storage: presigned_file_get failed for {ref_or_key} ({e})")
        return None


def delete_file_object(ref_or_key: str, provider=None) -> None:
    """Best-effort hard-delete of a stored file object. ``provider`` (the file's
    RECORDED ProviderConfig) selects the backend; ``None`` falls back to env
    FILES_S3_* then local MinIO. No-op for inline base64 fallback refs or when
    storage is unavailable. ``file:`` refs are unlinked from WRIT_FILES_DIR
    (missing file is a no-op)."""
    if not ref_or_key:
        return
    if ref_or_key.startswith(_FS_REF_PREFIX):
        _fs_delete(ref_or_key)
        return
    if not ref_or_key.startswith(_REF_PREFIX) and _decode_b64(ref_or_key) is not None and "/" not in ref_or_key:
        # Inline base64 fallback — nothing to remove from object storage.
        return
    internal, _public, store_bucket, _base = _files_store(provider)
    if internal is None:
        return
    bucket, key = _split_files_ref(ref_or_key, store_bucket or _FILES_BUCKET)
    if not key:
        return
    try:
        internal.remove_object(bucket, key)
    except Exception as e:  # noqa: BLE001 — already gone / storage hiccup
        logger.warning(f"visual_storage: delete_file_object failed for {ref_or_key} ({e})")


def presigned_file_put(key: str, max_bytes: int, expires_seconds: int = 600, provider=None) -> Optional[dict]:
    """Mint a scoped, short-TTL DIRECT upload for a (possibly untrusted) agent so
    captured download bytes never stream through the backend (§10.8 invariant).

    ``provider`` (the owner's resolved ProviderConfig) selects the backend;
    ``None`` falls back to env FILES_S3_* then local MinIO. Prefers a presigned POST
    whose ``content_length_range`` is ``(1, max_bytes)`` — so OBJECT STORAGE ITSELF
    hard-caps the upload size; the backend never trusts an agent-claimed length.
    Returns ``{"url", "fields", "method": "POST", "key", "max_bytes", "bucket"}``.

    Falls back to a presigned PUT (``{"url", "method": "PUT", "key", "max_bytes",
    "bucket"}``) when the POST-policy API is unavailable; the size is then
    re-validated server-side from ``head_file_object`` at finalize. Returns None
    when storage is unavailable (the caller must fail closed — a base64 fallback
    direct-upload path does not exist by design). The local-filesystem backend
    also returns None: an agent direct-upload needs an HTTP object store, and
    minting an unauthenticated upload route into WRIT_FILES_DIR would violate
    the same no-bytes-through-backend design — download capture requires
    MinIO/S3."""
    if not key:
        return None
    internal, public, bucket, public_base = _files_store(provider)
    if public is None or not bucket:
        return None
    # Build the POST target host: the external provider's public base, else the
    # local MinIO public host. The bucket is appended to form the upload URL.
    base = public_base or _public_base_url()
    cap = max(1, int(max_bytes or 1))
    # Preferred: presigned POST with a storage-enforced content-length-range — POST
    # is the STRONG default because object storage itself hard-caps the upload size
    # (no trust in the agent's claimed length). The PUT branch below is only a
    # last-resort fallback when the POST-policy API is unavailable.
    try:
        from datetime import timedelta
        from minio.datatypes import PostPolicy

        policy = PostPolicy(bucket, timedelta(seconds=expires_seconds))
        # Bind the upload to EXACTLY this one object key (§10.A.3 single-object
        # scope). An exact equals condition is unambiguous on its own; a
        # redundant starts-with("key","") would needlessly widen the policy's
        # key match, so it is intentionally omitted.
        policy.add_equals_condition("key", key)
        policy.add_content_length_range_condition(1, cap)
        form = public.presigned_post_policy(policy)
        # The MinIO SDK returns the form fields (incl. signature) but not the URL;
        # the upload target is the bucket URL on the public host.
        url = f"{base}/{bucket}"
        fields = dict(form)
        fields.setdefault("key", key)
        return {"url": url, "fields": fields, "method": "POST",
                "key": key, "max_bytes": cap, "bucket": bucket}
    except Exception as e:  # noqa: BLE001 — fall back to a plain presigned PUT
        logger.warning(
            f"visual_storage: presigned POST unavailable for {key}, using PUT ({e})"
        )
    try:
        from datetime import timedelta
        url = public.presigned_put_object(
            bucket, key, expires=timedelta(seconds=expires_seconds),
        )
        # A bare presigned PUT has NO storage-side size cap (unlike the preferred
        # POST policy's content_length_range). Warn loudly so this degraded path is
        # visible in logs. The size contract is still upheld: finalize re-validates
        # the REAL size via head_file_object and deletes any over-cap object
        # (artifact-finalize, §4.4) — the backend never trusts the agent's claim.
        logger.warning(
            "visual_storage: presigned PUT fallback for %s — storage-side size cap "
            "UNAVAILABLE (cap=%d bytes); finalize HEAD will re-validate and delete "
            "any over-cap object", key, cap,
        )
        return {"url": url, "method": "PUT", "key": key, "max_bytes": cap, "bucket": bucket}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"visual_storage: presigned_file_put failed for {key} ({e})")
        return None


def head_file_object(key: str, provider=None) -> Optional[dict]:
    """Read an uploaded object's REAL ``{size, content_type}`` via a HEAD on the
    INTERNAL client — the source of truth used to finalize an agent-direct upload
    (never trust the agent's claimed size/type). ``provider`` (a resolved
    ProviderConfig) selects the backend; ``None`` falls back to env FILES_S3_* then
    local MinIO. Accepts a ``minio:bucket/key`` ref or a bare key.
    Returns None when storage is unavailable or the object is missing (caller fails
    closed). ``file:`` refs are stat'd on disk."""
    if not key:
        return None
    if key.startswith(_FS_REF_PREFIX):
        path = _fs_resolve(key[len(_FS_REF_PREFIX):])
        if path is None or not path.is_file():
            return None
        ctype = mimetypes.guess_type(key[len(_FS_REF_PREFIX):])[0]
        return {
            "size": int(path.stat().st_size),
            "content_type": ctype or "application/octet-stream",
        }
    internal, _public, store_bucket, _base = _files_store(provider)
    if internal is None:
        return None
    bucket, obj_key = _split_files_ref(key, store_bucket or _FILES_BUCKET)
    if not obj_key:
        return None
    try:
        st = internal.stat_object(bucket, obj_key)
        return {
            "size": int(getattr(st, "size", 0) or 0),
            "content_type": getattr(st, "content_type", None) or "application/octet-stream",
        }
    except Exception as e:  # noqa: BLE001 — object missing / storage hiccup
        logger.warning(f"visual_storage: head_file_object failed for {key} ({e})")
        return None


# Resolve the backend at import time so the DB-blob fallback WARNING fires ONCE
# at startup (not per store call) and the chosen backend is visible in boot logs.
storage_backend()
