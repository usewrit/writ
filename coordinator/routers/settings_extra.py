"""
Self-host coordinator settings — the NEW sections the desktop-modeled Settings
surface needs that had no cloud analog: Runtime, Network/Public-URL, Security,
Preferences, Data & Retention.

All routes are admin-scoped (single owner, DB-verified via require_platform_admin,
which itself resolves through get_auth_context). Values are stored in the existing
``config`` KV table via services.coordinator_settings — no new table.

Mounted at /api/settings (self-prefixed router).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from security.dependencies import require_platform_admin
from services import coordinator_settings as cs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])


# ============================================================
# Runtime (local execution governor)
# ============================================================
class RuntimeSettings(BaseModel):
    # All optional so PUT is a partial merge; omitted keys are left unchanged.
    #
    # Only the knob that governs something this process actually does. The other
    # five mirrored the desktop daemon (browser headless-ness, its RSS watermark,
    # a fg/bg run split, monitor interval floors) and were read by nothing — the
    # coordinator launches no browsers. See coordinator_settings.RUNTIME_DEFAULTS.
    max_concurrent_runs: Optional[int] = None


@router.get("/runtime")
async def get_runtime_settings(
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_platform_admin),
):
    return await cs.get_runtime(db)


@router.put("/runtime")
async def update_runtime_settings(
    body: RuntimeSettings,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_platform_admin),
):
    result = await cs.set_runtime(db, body.model_dump(exclude_unset=True))
    await db.commit()
    return result


# ============================================================
# Network & Public URL
# ============================================================
class NetworkSettings(BaseModel):
    public_url: Optional[str] = None
    trusted_hosts: Optional[List[str]] = None


@router.get("/network")
async def get_network_settings(
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_platform_admin),
):
    return await cs.get_network(db)


@router.put("/network")
async def update_network_settings(
    body: NetworkSettings,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_platform_admin),
):
    result = await cs.set_network(db, body.model_dump(exclude_unset=True))
    await db.commit()
    return result


# ============================================================
# Security (session/JWT policy + active sessions)
# ============================================================
class SecuritySettings(BaseModel):
    # `idle_timeout_min` and `require_mfa` are gone: neither was ever enforced,
    # and self-host has no MFA enrolment path for the latter to demand — a toggle
    # promising a second factor could only lock the sole owner out or (as it did)
    # do nothing. See coordinator_settings.SECURITY_DEFAULTS.
    session_ttl_min: Optional[int] = None
    refresh_ttl_days: Optional[int] = None


@router.get("/security")
async def get_security_settings(
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_platform_admin),
):
    data = await cs.get_security(db)
    # Encryption-key status indicator (never the key itself).
    from config import settings as _s
    data["encryption_key_configured"] = bool(_s.secret_encryption_key)
    return data


@router.put("/security")
async def update_security_settings(
    body: SecuritySettings,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_platform_admin),
):
    result = await cs.set_security(db, body.model_dump(exclude_unset=True))
    await db.commit()
    from config import settings as _s
    result["encryption_key_configured"] = bool(_s.secret_encryption_key)
    return result


# ============================================================
# Preferences (per-owner UI)
# ============================================================
class PreferencesSettings(BaseModel):
    # No analytics/telemetry field: a self-hosted coordinator sends nothing anywhere, and
    # the `analytics_enabled` flag this used to accept was never read by anything. An older
    # UI still PATCHing it is harmless — `set_preferences` ignores keys absent from
    # PREFERENCES_DEFAULTS.
    language: Optional[str] = None
    theme: Optional[str] = None


@router.get("/preferences")
async def get_preferences_settings(
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_platform_admin),
):
    return await cs.get_preferences(db)


@router.put("/preferences")
async def update_preferences_settings(
    body: PreferencesSettings,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_platform_admin),
):
    result = await cs.set_preferences(db, body.model_dump(exclude_unset=True))
    await db.commit()
    return result


# ============================================================
# Data & Retention
# ============================================================
class DataSettings(BaseModel):
    retention_days: Optional[int] = None


@router.get("/data")
async def get_data_settings(
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_platform_admin),
):
    return await cs.get_data(db)


@router.put("/data")
async def update_data_settings(
    body: DataSettings,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_platform_admin),
):
    result = await cs.set_data(db, body.model_dump(exclude_unset=True))
    await db.commit()
    return result


@router.post("/data/purge")
async def purge_old_data(
    body: DataSettings | None = None,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_platform_admin),
):
    """Delete runs / logs / detected changes older than the retention window.

    Uses the body's ``retention_days`` when provided (and persists it), otherwise
    the stored setting. retention_days == 0 means "keep everything" → no-op.
    """
    from models.automation_task import AutomationTask
    from models.detected_change import DetectedChange
    from models.notification_log import NotificationLog
    from models.uptime_check import UptimeCheck

    if body is not None and body.retention_days is not None:
        settings_row = await cs.set_data(db, {"retention_days": body.retention_days})
    else:
        settings_row = await cs.get_data(db)
    retention_days = int(settings_row["retention_days"])

    if retention_days <= 0:
        await db.commit()
        return {"success": True, "retention_days": 0, "purged": {}, "skipped": "keep-all"}

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    purged: dict[str, int] = {}

    async def _purge(model, column) -> int:
        cnt = (
            await db.execute(select(func.count()).select_from(model).where(column < cutoff))
        ).scalar_one()
        if cnt:
            await db.execute(delete(model).where(column < cutoff))
        return int(cnt or 0)

    # Runs (workflow task history), health/change logs, notification log, uptime.
    purged["runs"] = await _purge(AutomationTask, AutomationTask.created_at)
    purged["detected_changes"] = await _purge(DetectedChange, DetectedChange.last_detected_at)
    purged["notification_logs"] = await _purge(NotificationLog, NotificationLog.sent_at)
    purged["uptime_checks"] = await _purge(UptimeCheck, UptimeCheck.checked_at)

    await db.commit()
    logger.info("Data purge (retention=%sd): %s", retention_days, purged)
    return {"success": True, "retention_days": retention_days, "purged": purged}
