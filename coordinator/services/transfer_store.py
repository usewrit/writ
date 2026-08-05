"""
transfer_store — where a STAGED transfer package's decrypted body lives between
the import wizard's unlock step and its commit step (self-host edition).

Spec: `DATA_PORTABILITY_SPEC.md` §10. Same contract as the cloud module, different
backing: the cloud parks staged bodies in MinIO, a self-host install parks them on
the filesystem under `writ_files_dir/transfers/`. A self-host box always has a disk
and may not have object storage, so requiring MinIO here would make import fail on
the simplest deployment.

NOT a byte-identical twin of the cloud edition's store on purpose — the storage
backend genuinely differs, so it is excluded from the twin check. What IS shared
is the interface (`put` / `get` / `delete` / `delete_import`) and the
invariant behind it: whatever the location, staged bytes are Fernet-wrapped before
they are written, because a staged import is a decrypted copy of a file the user
chose to encrypt.

Small bodies (< 256 KiB — most config-only packages) are inlined on the DB row and
never touch the disk at all.
"""
from __future__ import annotations

import base64
import logging
import os
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

#: Bodies at or below this size are inlined on the row instead of written to disk.
#: Mirrors `models.transfer_import.INLINE_PAYLOAD_MAX_BYTES`.
INLINE_MAX_BYTES = 256 * 1024


class TransferStoreUnavailable(RuntimeError):
    """The staging area is not writable/readable for this package."""


def _root() -> Path:
    """`<writ_files_dir>/transfers`, or a temp dir when no files dir is configured.

    Created 0o700: the contents are decrypted tenant work, and on a self-host box
    the process user's home is frequently shared with other things.
    """
    from config import settings

    base = getattr(settings, "writ_files_dir", None) or os.getenv("WRIT_FILES_DIR") or "/tmp/writ"
    root = Path(base).expanduser() / "transfers"
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:  # pragma: no cover — non-POSIX or already-restrictive mount
        pass
    return root


def _path(import_id, part: str) -> Path:
    directory = _root() / str(import_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{part}.bin"


def wrap(raw: bytes) -> bytes:
    """Fernet-wrap arbitrary bytes.

    base64 first because `encrypt_secret` takes a `str`: a latin-1 round-trip would
    also be lossless but would inflate every byte >= 0x80 to two bytes under utf-8,
    i.e. ~1.5x on already-gzipped data. base64 is a predictable 4/3.
    """
    from security.encryption import SecretEncryption

    return SecretEncryption.encrypt_secret(base64.b64encode(raw).decode("ascii")).encode("ascii")


def unwrap(wrapped: bytes) -> bytes:
    from security.encryption import SecretEncryption

    return base64.b64decode(SecretEncryption.decrypt_secret(wrapped.decode("ascii")))


def put(import_id, part: str, raw: bytes) -> tuple[Optional[str], Optional[str]]:
    """Store a staged part. Returns `(ref, inline_blob)` — exactly one is set."""
    wrapped = wrap(raw)
    if len(raw) <= INLINE_MAX_BYTES:
        return None, wrapped.decode("ascii")
    try:
        target = _path(import_id, part)
        # Write-then-rename so a crash mid-write cannot leave a half file that a
        # later commit would try to decrypt.
        tmp = target.with_suffix(".partial")
        tmp.write_bytes(wrapped)
        tmp.replace(target)
        target.chmod(0o600)
        return str(target), None
    except OSError as exc:
        raise TransferStoreUnavailable(
            f"This package is too large to keep in the database and the staging area "
            f"could not be written ({exc}). Check disk space and WRIT_FILES_DIR."
        )


def get(*, ref: Optional[str], inline: Optional[str]) -> bytes:
    """Read a staged part back. `ref` wins when both are somehow set."""
    if ref:
        try:
            return unwrap(Path(ref).read_bytes())
        except OSError as exc:
            raise TransferStoreUnavailable(
                f"The staged package could not be read back ({exc}). Upload it again to continue."
            )
    if inline:
        return unwrap(inline.encode("ascii"))
    raise TransferStoreUnavailable("the staged package body is missing")


def delete(ref: Optional[str]) -> None:
    """Best-effort removal. A failure is logged and swallowed: the row is already
    scrubbed, and `delete_import` reclaims whatever is left."""
    if not ref:
        return
    try:
        Path(ref).unlink(missing_ok=True)
    except OSError:
        logger.warning("transfer_store: could not delete %s", ref, exc_info=True)


def delete_import(import_id) -> None:
    """Remove every part of one staged import, including parts no row references
    (the crash-between-write-and-commit case). Without this a failed stage would
    leave decrypted work on disk indefinitely."""
    try:
        shutil.rmtree(_root() / str(import_id), ignore_errors=True)
    except OSError:
        logger.warning("transfer_store: could not clear staging dir for %s", import_id, exc_info=True)


__all__ = [
    "INLINE_MAX_BYTES",
    "TransferStoreUnavailable",
    "delete",
    "delete_import",
    "get",
    "put",
    "unwrap",
    "wrap",
]
