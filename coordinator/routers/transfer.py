"""
/api/transfer/* — portable import and export of this install's work (self-host).

Spec: `DATA_PORTABILITY_SPEC.md` §10. `transfer_codec` owns the bytes (a
byte-identical twin of the cloud module, so packages move both ways),
`transfer_bundle` the body, `transfer_import_service` the database side; this module
is the HTTP edge and the place the operational limits live.

Same paths and same request/response bodies as the cloud router ON PURPOSE — a
package, and the wizard that drives it, must behave identically in both editions.
What differs is what this edition has: no tenant to scope by, no plan to gate on,
and no managed endpoints or AI-session assets (a cloud package carrying them
imports the rest and says what it left out).

EXPORT streams. The response body is produced frame by frame straight out of the
database — assets one at a time, data rows in keyset-paginated batches — so a
400 MiB export never exists in memory and never needs a spool file. That is only
possible because the container is self-delimiting (no length or digest in the
header to compute up front).

IMPORT is staged. `inspect` reads the cleartext header with NO passphrase, so the
wizard can show provenance and counts before asking for anything. `stage` unlocks
once and parks the body out-of-line; `plan` is a PATCH the middle steps call;
`commit` applies; `undo` reverses. Nothing touches a tenant asset before `commit`.

CPU DISCIPLINE. Argon2id at 64 MiB blocks for ~100ms. Both `stage` and the export
key derivation run in a worker thread; nothing derives a key on the event loop.

WHAT THIS EDGE ENFORCES (and the service deliberately does not):
  * per-tenant concurrency, so a burst queues instead of exhausting workers;
  * export rate limiting + an audit event, because bulk export is the
    exfiltration path a tenant most needs a record of;
  * package size ceiling, applied WHILE the upload streams;
  * unlock-attempt throttling on top of Argon2id's own cost;
  * `include_credentials` re-authentication.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from security.dependencies import AuthContext, get_auth_context
from services import transfer_bundle as B
from services import transfer_codec as C
from services import transfer_import_service as I
from services import transfer_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/transfer", tags=["transfer"])

#: Ceiling on an uploaded package, enforced while streaming to disk.
MAX_UPLOAD_BYTES = int(os.getenv("TRANSFER_MAX_UPLOAD_BYTES", str(2 * 1024 * 1024 * 1024)))

#: Concurrent stage-or-commit operations. A semaphore rather than a rejection so a
#: burst queues; each one holds a worker thread and a chunk of RAM, and a self-host
#: box is usually much smaller than a cloud node.
MAX_CONCURRENT = 2

#: Exports per hour, and unlock attempts per hour, for the whole install.
EXPORT_RATE_PER_HOUR = 20
UNLOCK_ATTEMPTS_PER_HOUR = 30

#: Failed unlocks per staged package before it stops accepting attempts.
MAX_UNLOCK_FAILURES = 5

_gate: Optional[asyncio.Semaphore] = None
_rate_buckets: dict[str, list[float]] = {}


def _install_gate() -> asyncio.Semaphore:
    global _gate
    if _gate is None:
        _gate = asyncio.Semaphore(MAX_CONCURRENT)
    return _gate


def _rate_check(bucket: str, limit: int, window_seconds: int = 3600) -> None:
    """In-process sliding window.

    Deliberately simple: this is a back-pressure guard on an expensive, rarely-used
    operation. A self-host coordinator is a single process, so a per-process window
    is the whole install, and Argon2id already caps the offline rate (spec §13).
    """
    now = time.monotonic()
    hits = [t for t in _rate_buckets.get(bucket, []) if now - t < window_seconds]
    if len(hits) >= limit:
        raise HTTPException(
            status_code=429,
            detail={"code": "RATE_LIMITED",
                    "message": "Too many transfer operations. Try again in a little while."},
        )
    hits.append(now)
    _rate_buckets[bucket] = hits


# ---------------------------------------------------------------------------
# request models
# ---------------------------------------------------------------------------

class ExportRequest(BaseModel):
    label: Optional[str] = Field(None, max_length=120)
    select: dict = Field(default_factory=dict)
    include_data: dict = Field(default_factory=dict)
    include_credentials: bool = False
    reauth: Optional[dict] = None
    passphrase: str = Field(..., min_length=1)


class PreviewRequest(BaseModel):
    select: dict = Field(default_factory=dict)
    include_data: dict = Field(default_factory=dict)


class PlanRequest(BaseModel):
    resolutions: Optional[dict] = None
    personas: Optional[dict] = None
    secrets: Optional[dict] = None
    inputs: Optional[dict] = None
    files: Optional[dict] = None
    notify: Optional[dict] = None
    webhooks: Optional[dict] = None
    schedules: Optional[dict] = None
    credentials: Optional[dict] = None
    include_data: Optional[bool] = None
    arm_schedules: Optional[bool] = None


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

@router.post("/export/preview")
async def preview_export(
    body: PreviewRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """What an export WOULD contain — no file, no passphrase.

    Shares `BundleBuilder.plan()` with the real export, so the preview cannot
    promise something the export then declines to include.
    """
    selection = _selection(body.model_dump())
    builder = B.BundleBuilder(db, auth.user_id)
    plan = await builder.plan(selection)
    return {
        "counts": plan.counts(),
        "requires": plan.requires(),
        "requirements": plan.requirements,
        "skipped": [s.as_dict() for s in plan.skipped],
        "marketplace_refs": plan.marketplace_refs,
        "data": [
            {"workflow_id": wf_id, "rows": plan.data_row_counts.get(wf_id, 0),
             "runs": plan.data_run_counts.get(wf_id, 0),
             "truncated": plan.data_row_counts.get(wf_id, 0) > B.MAX_DATA_ROWS_PER_ASSET}
            for wf_id in sorted(plan.data_for)
        ],
        "personas_included": [p.name for p in plan.rows.get("personas", [])],
    }


@router.post("/export")
async def export_package(
    body: ExportRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Build and stream a `.writ` package.

    The sealed-credentials lane requires re-authentication IN THIS REQUEST — a live
    session is not enough, because bulk credential egress should cost the person
    doing it a deliberate act (§8).
    """
    if not body.passphrase.strip():
        raise HTTPException(
            status_code=400,
            detail={"code": "WEAK_PASSPHRASE",
                    "message": "Choose a passphrase — without one the package cannot be encrypted."},
        )
    _rate_check("export", EXPORT_RATE_PER_HOUR)

    selection = _selection(body.model_dump())
    builder = B.BundleBuilder(db, auth.user_id)
    plan = await builder.plan(selection)

    secrets_blob: Optional[bytes] = None
    if body.include_credentials:
        await _require_reauth(db, auth, body.reauth)
        secrets_blob = await _build_secrets_lane(db, plan)

    created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    salt = C.new_salt()
    header = C.build_header(
        salt=salt,
        producer_app="selfhost",
        producer_version=os.getenv("WRIT_VERSION", os.getenv("APP_VERSION", "0.0.0")),
        producer_edition="oss",
        producer_schema=None,
        bundle_id=str(uuid.uuid4()),
        created_at=created_at,
        contents=plan.counts(),
        requires=plan.requires(),
        secrets_count=_secrets_count(secrets_blob),
        label=body.label,
    )
    # Argon2id is ~100ms of CPU: never on the event loop.
    keys = await asyncio.to_thread(C.derive_keys, body.passphrase, salt)
    writer = C.PackageWriter(header, keys)

    await _audit_export(db, auth, plan, with_credentials=bool(secrets_blob))

    async def stream():
        """Header → framed body → credentials lane. Memory stays at O(one frame)
        because each piece is handed to the client as it is produced."""
        try:
            yield writer.begin()
            async for piece in builder.stream_body(plan):
                out = writer.write_body(piece)
                if out:
                    yield out
            yield writer.finish_body()
            yield writer.write_secrets(secrets_blob) if secrets_blob else writer.no_secrets()
        except Exception:
            # A mid-stream failure cannot become a 500 — the status line is long
            # gone. Log it and cut the stream so the client's package fails its
            # final-frame check rather than looking complete.
            #
            # Identify the owner by user_id: this coordinator is single-tenant, so
            # AuthContext carries no tenant_id — reading one here raised
            # AttributeError *inside the handler*, replacing the real failure with
            # a bogus one and losing the traceback that explains the broken export.
            logger.exception("transfer export failed mid-stream for user %s", auth.user_id)
            raise

    filename = C.package_filename(created_at)
    return StreamingResponse(
        stream(),
        media_type="application/vnd.writ.package",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Writ-Package-Version": str(C.PACKAGE_VERSION),
        },
    )


async def _build_secrets_lane(db, plan: B.BundlePlan) -> Optional[bytes]:
    """The opt-in sealed lane (§8): vault values for the keys these assets need,
    plus persona credentials for the personas travelling with them.

    Excluded even here: session state (device- and IP-bound cookies — importing them
    is a downgrade and they are stale anyway), proxy credentials, OAuth tokens.
    """
    from models.persona import Persona
    from models.vault_secret import VaultSecret
    from security.encryption import SecretEncryption

    wanted = {s.get("key") for s in plan.requirements.get("secret_slots") or [] if s.get("key")}
    vault: list[dict] = []
    if wanted:
        res = await db.execute(select(VaultSecret).where(VaultSecret.key.in_(sorted(wanted))))
        for secret in res.scalars().all():
            try:
                value = SecretEncryption.decrypt_secret(secret.value_encrypted)
            except Exception:
                logger.warning("transfer export: vault secret %s could not be decrypted", secret.key)
                continue
            vault.append({"key": secret.key, "value": value,
                          "category": secret.category, "description": secret.description})

    personas: list[dict] = []
    for persona in plan.rows.get("personas", []):
        entry: dict = {"ref": plan.ref_for("personas", persona.id)}
        if persona.login_username:
            entry["login_username"] = persona.login_username
        for column, key in (("credentials_encrypted", "password"), ("totp_seed_encrypted", "totp_seed")):
            blob = getattr(persona, column, None)
            if not blob:
                continue
            try:
                plain = SecretEncryption.decrypt_secret(blob)
            except Exception:
                continue
            if key == "password":
                try:
                    parsed = json.loads(plain)
                    plain = parsed.get("password") if isinstance(parsed, dict) else plain
                except Exception:
                    pass
            if plain:
                entry[key] = plain
        if len(entry) > 1:
            personas.append(entry)

    if not vault and not personas:
        return None
    return json.dumps({"secrets_version": 1, "vault": vault, "personas": personas}).encode("utf-8")


def _secrets_count(blob: Optional[bytes]) -> int:
    if not blob:
        return 0
    payload = json.loads(blob)
    return len(payload.get("vault") or []) + len(payload.get("personas") or [])


async def _require_reauth(db, auth: AuthContext, reauth: Optional[dict]) -> None:
    """Password re-entry for the credentials lane. A live session is not enough."""
    from argon2 import PasswordHasher

    from models.user import User

    password = (reauth or {}).get("password") or ""
    if not password:
        raise HTTPException(
            status_code=401,
            detail={"code": "REAUTH_REQUIRED",
                    "message": "Enter your account password to include credentials in this package."},
        )
    user = await db.get(User, auth.user_id) if auth.user_id else None
    if not user or not user.password_hash:
        # A social-only account has no password to re-enter. Rather than let that
        # skip the check, the credentials lane is simply unavailable to it.
        raise HTTPException(status_code=401, detail={
            "code": "REAUTH_UNAVAILABLE",
            "message": "This account signs in without a password, so credentials cannot be "
                       "included in a package. Export without them and attach your own on import.",
        })
    try:
        PasswordHasher().verify(user.password_hash, password)
    except Exception:
        raise HTTPException(
            status_code=401,
            detail={"code": "REAUTH_FAILED", "message": "That password is not right."},
        )


async def _audit_export(db, auth: AuthContext, plan: B.BundlePlan, *, with_credentials: bool) -> None:
    from security.audit import log_security_event

    # Bulk export is a recognized exfiltration vector, so it gets a record with
    # counts and a distinct event when credentials are included (§13). This edition
    # has `log_security_event` rather than the cloud's `record_audit`; both are
    # best-effort and swallow their own failures, so the export can never fail
    # because the audit sink is unhappy.
    await log_security_event(
        "transfer.export.credentials" if with_credentials else "transfer.export",
        actor=str(auth.user_id) if auth.user_id else None,
        details={"counts": plan.counts(), "requires": plan.requires(),
                 "with_credentials": with_credentials},
        severity="warning" if with_credentials else "info",
    )


# ---------------------------------------------------------------------------
# import — inspect / stage
# ---------------------------------------------------------------------------

@router.post("/inspect")
async def inspect_package(
    file: UploadFile = File(...),
    auth: AuthContext = Depends(get_auth_context),
):
    """Wizard step 1: provenance and counts from the cleartext header.

    No passphrase, no staging, no database write — it reads the first few KiB.
    """
    head = await file.read(C.MAX_HEADER_BYTES + 64)
    try:
        header, _, _ = C.parse_header(head)
    except C.UnsupportedVersion as exc:
        raise HTTPException(status_code=400, detail={
            "code": exc.code, "message": str(exc),
            "producer": exc.producer, "package_version": exc.version,
        })
    except C.PackageError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "message": str(exc)})
    finally:
        await file.close()
    return {"header": C.header_summary(header), "compatible": True}


@router.post("/stage")
async def stage_package(
    request: Request,
    file: UploadFile = File(...),
    passphrase: str = Form(...),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Wizard step 2: unlock, validate, and park the package as a staged import.

    The upload is streamed to a temp file with the ceiling enforced AS IT ARRIVES,
    so a hostile 5 GiB body is cut off after 2 GiB of disk rather than being read
    into memory first.
    """
    _rate_check("unlock", UNLOCK_ATTEMPTS_PER_HOUR)
    spool = tempfile.NamedTemporaryFile(suffix=".writ", delete=False)
    try:
        total = 0
        while True:
            chunk = await file.read(1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail={
                    "code": "TOO_LARGE",
                    "message": f"That package is larger than this install accepts "
                               f"({MAX_UPLOAD_BYTES // (1024 * 1024)} MB).",
                })
            spool.write(chunk)
        spool.flush()
        spool.close()
        await file.close()

        async with _install_gate():
            with open(spool.name, "rb") as handle:
                try:
                    row = await I.stage(
                        db,
                        user_id=auth.user_id,
                        spooled=handle,
                        passphrase=passphrase,
                    )
                except C.BadPassphrase as exc:
                    raise HTTPException(status_code=401, detail={"code": exc.code, "message": str(exc)})
                except C.UnsupportedVersion as exc:
                    raise HTTPException(status_code=400, detail={
                        "code": exc.code, "message": str(exc), "producer": exc.producer})
                except (C.PackageError, B.BundleError) as exc:
                    raise HTTPException(status_code=400, detail={"code": exc.code, "message": str(exc)})
                except transfer_store.TransferStoreUnavailable as exc:
                    raise HTTPException(status_code=507, detail={
                        "code": "STORE_UNAVAILABLE", "message": str(exc)})
        return _import_response(row)
    finally:
        try:
            os.unlink(spool.name)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# import — plan / commit / undo
# ---------------------------------------------------------------------------

@router.get("/imports")
async def list_imports(
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    from models.transfer_import import TransferImport

    res = await db.execute(
        select(TransferImport).order_by(TransferImport.created_at.desc()).limit(50)
    )
    return {"imports": [_import_row_summary(r) for r in res.scalars().all()]}


@router.get("/imports/{import_id}")
async def get_import(
    import_id: str,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Wizard resume / refresh, and the progress poll for a backgrounded commit."""
    row = await _load_import(db, auth, import_id)
    return _import_response(row)


@router.put("/imports/{import_id}/plan")
async def update_plan(
    import_id: str,
    body: PlanRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Steps 3-6. A PATCH: each step sends only what it owns, so a later step
    cannot wipe an earlier one's bindings by omitting them."""
    row = await _load_import(db, auth, import_id)
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        ready = await I.save_plan(db, row, patch)
    except I.ImportError_ as exc:
        raise HTTPException(status_code=_status_for(exc), detail={"code": exc.code, "message": str(exc)})
    return {"import": _import_row_summary(row), "readiness": ready}


@router.post("/imports/{import_id}/commit")
async def commit_import(
    import_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Step 7. Applies the plan and returns per-asset outcomes.

    `Idempotency-Key` makes a retry safe: without it a double-click imports the
    package twice, which lands as a set of renamed duplicates — the worst outcome
    this endpoint can produce.
    """
    row = await _load_import(db, auth, import_id)
    idempotency_key = request.headers.get("Idempotency-Key") or request.headers.get("idempotency-key")
    async with _install_gate():
        try:
            result = await I.commit(db, row, user_id=auth.user_id, idempotency_key=idempotency_key)
        except I.ImportError_ as exc:
            raise HTTPException(status_code=_status_for(exc), detail={"code": exc.code, "message": str(exc)})
        except (B.BundleError, C.PackageError) as exc:
            raise HTTPException(status_code=400, detail={"code": exc.code, "message": str(exc)})
        except transfer_store.TransferStoreUnavailable as exc:
            raise HTTPException(status_code=507, detail={"code": "STORE_UNAVAILABLE", "message": str(exc)})
    return {"import": _import_row_summary(row), "result": result}


@router.post("/imports/{import_id}/undo")
async def undo_import(
    import_id: str,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Remove what this import created. Anything since used is kept, with a reason
    — a partial undo the user can see beats one that destroys real results."""
    row = await _load_import(db, auth, import_id)
    try:
        result = await I.undo(db, row)
    except I.ImportError_ as exc:
        raise HTTPException(status_code=_status_for(exc), detail={"code": exc.code, "message": str(exc)})
    return {"import": _import_row_summary(row), "undo": result}


@router.delete("/imports/{import_id}")
async def discard_import(
    import_id: str,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Abandon a staged import and destroy its plaintext NOW rather than at TTL."""
    row = await _load_import(db, auth, import_id)
    if row.status == "committed":
        raise HTTPException(status_code=409, detail={
            "code": "ALREADY_COMMITTED",
            "message": "This package was already imported. Use Undo instead.",
        })
    payload_ref, secrets_ref = row.payload_ref, row.secrets_ref
    row.scrub_payload()
    row.status = "expired"
    await db.commit()
    transfer_store.delete(payload_ref)
    if secrets_ref and not str(secrets_ref).startswith("inline:"):
        transfer_store.delete(secrets_ref)
    transfer_store.delete_import(row.id)
    return {"discarded": True}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _selection(payload: dict) -> B.BundleSelection:
    raw = dict(payload.get("select") or {})
    raw["include_data"] = payload.get("include_data") or {}
    try:
        return B.BundleSelection.from_payload(raw)
    except B.MalformedBundle as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "message": str(exc)})


async def _load_import(db, auth: AuthContext, import_id: str):
    from models.transfer_import import TransferImport

    try:
        # Validate the shape before hitting the DB so a hostile id cannot become a
        # wildcard lookup; stored as a string, since SQLite has no UUID type.
        parsed = str(uuid.UUID(str(import_id)))
    except ValueError:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "No such import"})
    row = await db.get(TransferImport, parsed)
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "No such import"})
    if row.status in ("staged", "planned") and row.expires_at:
        expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
        if expires < datetime.now(timezone.utc):
            raise HTTPException(status_code=410, detail={
                "code": "STAGE_EXPIRED",
                "message": "This staged package expired. Upload it again to continue.",
            })
    return row


def _import_row_summary(row) -> dict:
    return {
        "id": str(row.id),
        "status": row.status,
        "label": row.label,
        "bundle_id": str(row.bundle_id) if row.bundle_id else None,
        "producer": {
            "app": row.producer_app,
            "version": row.producer_version,
            "edition": row.producer_edition,
        },
        "counts": row.counts_json or {},
        "has_sealed_credentials": row.has_sealed_credentials,
        "payload_bytes": int(row.payload_bytes or 0),
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "committed_at": row.committed_at.isoformat() if row.committed_at else None,
        "undone_at": row.undone_at.isoformat() if row.undone_at else None,
        "progress": row.progress_json,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _import_response(row) -> dict:
    return {
        "import": _import_row_summary(row),
        "summary": row.summary_json or {},
        "requirements": row.requirements_json or {},
        "plan": row.plan_json or {},
        "readiness": I.readiness(row),
        "result": row.result_json,
    }


def _status_for(exc: I.ImportError_) -> int:
    return {
        "STALE_PLAN": 409,
        "PLAN_INCOMPLETE": 422,
        "ALREADY_COMMITTED": 409,
        "NOT_UNDOABLE": 409,
        "UNDO_EXPIRED": 409,
    }.get(exc.code, 400)
