"""
Uptime monitoring endpoints for Writ coordinator.

Handles uptime check reports, statistics, and SSL certificate monitoring.
"""
import logging
from datetime import datetime, timedelta
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import Target, UptimeCheck
from security.api_key import get_current_api_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/uptime", tags=["uptime"])


@router.get("/{target_id}/history")
async def get_uptime_history(
    target_id: int,
    hours: int = Query(default=24, ge=1, le=168),  # 1 hour to 1 week
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key)
):
    """
    Get uptime check history for a target.

    Args:
        target_id: Target ID
        hours: Hours of history to fetch (default: 24, max: 168)

    Returns:
        List of uptime checks with timing and SSL data
    """
    # Verify target exists
    query = select(Target).where(Target.id == target_id)
    target_result = await db.execute(query)
    target = target_result.scalar_one_or_none()

    if not target:
        raise HTTPException(status_code=404, detail="Target not found")

    if target.check_type != 'uptime':
        raise HTTPException(status_code=400, detail="Target is not an uptime check")

    # Get recent checks
    since = datetime.utcnow() - timedelta(hours=hours)

    result = await db.execute(
        select(UptimeCheck)
        .where(
            and_(
                UptimeCheck.target_id == target_id,
                UptimeCheck.checked_at >= since
            )
        )
        .order_by(desc(UptimeCheck.checked_at))
        .limit(1000)  # Safety limit
    )

    checks = result.scalars().all()

    return {
        "target_id": target_id,
        "target_url": target.url,
        "hours": hours,
        "check_count": len(checks),
        "checks": [check.to_dict() for check in checks]
    }


@router.get("/{target_id}/stats")
async def get_uptime_stats(
    target_id: int,
    hours: int = Query(default=24, ge=1, le=720),  # Up to 30 days
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key)
):
    """
    Get uptime statistics for a target.

    Returns:
        - Uptime percentage
        - Average response time
        - P95/P99 response times
        - Downtime incidents
        - SSL certificate status
    """
    try:
        # Verify target exists
        query = select(Target).where(Target.id == target_id)
        target_result = await db.execute(query)
        target = target_result.scalar_one_or_none()

        if not target:
            raise HTTPException(status_code=404, detail="Target not found")

        if target.check_type != 'uptime':
            raise HTTPException(status_code=400, detail="Target is not an uptime check")

        since = datetime.utcnow() - timedelta(hours=hours)

        # Get all checks in period
        result = await db.execute(
            select(UptimeCheck)
            .where(
                and_(
                    UptimeCheck.target_id == target_id,
                    UptimeCheck.checked_at >= since
                )
            )
            .order_by(UptimeCheck.checked_at)
        )
        checks = list(result.scalars().all())

        if not checks:
            return {
                "target_id": target_id,
                "target_url": target.url,
                "hours": hours,
                "no_data": True
            }

        # Calculate uptime percentage
        total_checks = len(checks)
        up_checks = sum(1 for c in checks if c.is_up)
        uptime_percentage = (up_checks / total_checks * 100) if total_checks > 0 else 0

        # Calculate response time stats
        response_times = [c.response_time_ms for c in checks if c.response_time_ms is not None]

        avg_response_time = None
        p95_response_time = None
        p99_response_time = None

        if response_times:
            sorted_times = sorted(response_times)
            avg_response_time = sum(sorted_times) / len(sorted_times)

            if len(sorted_times) >= 20:
                p95_index = int(len(sorted_times) * 0.95)
                p99_index = int(len(sorted_times) * 0.99)
                p95_response_time = sorted_times[p95_index]
                p99_response_time = sorted_times[p99_index]

        # Find downtime incidents (consecutive down checks)
        downtime_incidents = []
        in_downtime = False
        downtime_start = None
        downtime_checks = 0

        for check in checks:
            if not check.is_up:
                if not in_downtime:
                    in_downtime = True
                    downtime_start = check.checked_at
                    downtime_checks = 1
                else:
                    downtime_checks += 1
            else:
                if in_downtime:
                    # Downtime ended
                    downtime_incidents.append({
                        "started_at": downtime_start.isoformat(),
                        "ended_at": check.checked_at.isoformat(),
                        "duration_minutes": int((check.checked_at - downtime_start).total_seconds() / 60),
                        "failed_checks": downtime_checks
                    })
                    in_downtime = False
                    downtime_checks = 0

        # If still in downtime
        if in_downtime:
            downtime_incidents.append({
                "started_at": downtime_start.isoformat(),
                "ended_at": None,  # Still down
                "duration_minutes": int((datetime.utcnow() - downtime_start).total_seconds() / 60),
                "failed_checks": downtime_checks,
                "ongoing": True
            })

        # Get latest SSL certificate info
        latest_ssl = None
        latest_check_with_ssl = next(
            (c for c in reversed(checks) if c.ssl_cert_expires_at is not None),
            None
        )

        if latest_check_with_ssl:
            # Determine warning level
            days_until_expiry = latest_check_with_ssl.ssl_cert_days_until_expiry or 0
            warning_level = None

            if days_until_expiry < 0:
                warning_level = "expired"
            elif days_until_expiry <= 7:
                warning_level = "critical"
            elif days_until_expiry <= 30:
                warning_level = "warning"
            else:
                warning_level = "ok"

            latest_ssl = {
                "valid": latest_check_with_ssl.ssl_cert_valid,
                "expires_at": latest_check_with_ssl.ssl_cert_expires_at.isoformat() if latest_check_with_ssl.ssl_cert_expires_at else None,
                "days_until_expiry": latest_check_with_ssl.ssl_cert_days_until_expiry,
                "issuer": latest_check_with_ssl.ssl_cert_issuer,
                "subject": latest_check_with_ssl.ssl_cert_subject,
                "error": latest_check_with_ssl.ssl_error,
                "warning_level": warning_level
            }

        # Get current status (latest check)
        current_status = {
            "is_up": checks[-1].is_up,
            "last_checked": checks[-1].checked_at.isoformat(),
            "status_code": checks[-1].status_code,
            "response_time_ms": checks[-1].response_time_ms,
            "error_message": checks[-1].error_message
        }

        return {
            "target_id": target_id,
            "target_url": target.url,
            "hours": hours,
            "total_checks": total_checks,
            "uptime_percentage": round(uptime_percentage, 2),
            "downtime_incidents": downtime_incidents,
            "current_status": current_status,
            "response_time": {
                "avg_ms": round(avg_response_time) if avg_response_time else None,
                "p95_ms": p95_response_time,
                "p99_ms": p99_response_time
            },
            "ssl_certificate": latest_ssl,
            "period": {
                "from": since.isoformat(),
                "to": datetime.utcnow().isoformat()
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        from services.error_reporting import internal_http_error
        raise internal_http_error(
            e, "Failed to calculate uptime statistics.",
            action="uptime.stats", context={"target_id": target_id},
        )


@router.get("/{target_id}/ssl")
async def get_ssl_status(
    target_id: int,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key)
):
    """
    Get SSL certificate status for a target.

    Returns detailed SSL certificate information including expiry warnings.
    """
    # Verify target exists
    query = select(Target).where(Target.id == target_id)
    target_result = await db.execute(query)
    target = target_result.scalar_one_or_none()

    if not target:
        raise HTTPException(status_code=404, detail="Target not found")

    if target.check_type != 'uptime':
        raise HTTPException(status_code=400, detail="Target is not an uptime check")

    # Get latest check with SSL data
    result = await db.execute(
        select(UptimeCheck)
        .where(
            and_(
                UptimeCheck.target_id == target_id,
                UptimeCheck.ssl_cert_expires_at.isnot(None)
            )
        )
        .order_by(desc(UptimeCheck.checked_at))
        .limit(1)
    )

    latest_check = result.scalar_one_or_none()

    if not latest_check:
        return {
            "target_id": target_id,
            "target_url": target.url,
            "no_ssl_data": True,
            "message": "No SSL certificate data available yet"
        }

    # Determine warning level
    days_until_expiry = latest_check.ssl_cert_days_until_expiry or 0
    warning_level = None

    if days_until_expiry < 0:
        warning_level = "expired"
    elif days_until_expiry <= 7:
        warning_level = "critical"
    elif days_until_expiry <= 30:
        warning_level = "warning"
    else:
        warning_level = "ok"

    return {
        "target_id": target_id,
        "target_url": target.url,
        "certificate": {
            "valid": latest_check.ssl_cert_valid,
            "expires_at": latest_check.ssl_cert_expires_at.isoformat() if latest_check.ssl_cert_expires_at else None,
            "days_until_expiry": days_until_expiry,
            "issuer": latest_check.ssl_cert_issuer,
            "subject": latest_check.ssl_cert_subject,
            "error": latest_check.ssl_error,
            "warning_level": warning_level
        },
        "checked_at": latest_check.checked_at.isoformat()
    }


@router.get("/overview")
async def get_all_uptime_overview(
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key)
):
    """
    Get overview of all uptime monitors.

    Returns summary status for all uptime targets.
    """
    # Get all enabled uptime targets (single-owner coordinator)
    targets_result = await db.execute(
        select(Target).where(
            and_(
                Target.check_type == 'uptime',
                Target.enabled == True,
            )
        )
    )
    targets = list(targets_result.scalars().all())

    if not targets:
        return {
            "total_targets": 0,
            "targets": []
        }

    # Get latest check for each target
    overview = []

    for target in targets:
        result = await db.execute(
            select(UptimeCheck)
            .where(UptimeCheck.target_id == target.id)
            .order_by(desc(UptimeCheck.checked_at))
            .limit(1)
        )
        latest_check = result.scalar_one_or_none()

        if latest_check:
            overview.append({
                "target_id": target.id,
                "url": target.url,
                "is_up": latest_check.is_up,
                "status_code": latest_check.status_code,
                "response_time_ms": latest_check.response_time_ms,
                "last_checked": latest_check.checked_at.isoformat(),
                "ssl_days_until_expiry": latest_check.ssl_cert_days_until_expiry,
                "ssl_warning": latest_check.ssl_cert_days_until_expiry is not None and latest_check.ssl_cert_days_until_expiry < 30
            })

    return {
        "total_targets": len(targets),
        "targets": overview
    }
