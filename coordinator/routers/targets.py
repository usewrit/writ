"""
Targets router - URL monitoring target management endpoints.
"""
import json
import logging
from datetime import datetime, timezone
from typing import Optional, Union, List, Literal
from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel, Field, HttpUrl, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, and_, or_, distinct, func
from sqlalchemy.orm import load_only
import re
from urllib.parse import urljoin, urlparse

from database import get_db
from models.target import Target
Report = None  # no per-run reports store in this build
from security.api_key import get_current_api_key
from security.dependencies import check_api_key_scope, filter_by_scope
from security.feature_gate import require_feature
from utils.diff_parser import parse_diff_snippet
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/targets", tags=["Targets"])


async def _get_target_for_owner(db: AsyncSession, target_id: int, api_key: dict) -> "Target":
    """Fetch a target by ID. Raises 404 if not found."""
    result = await db.execute(
        select(Target).where(Target.id == target_id)
    )
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target not found")
    return target


def _snapshot_proxy_path(target_id: int, change_id: int, kind: str, ref: Optional[str]) -> Optional[str]:
    """Same-origin API path the frontend blob-fetches (with its Bearer token) to
    load a visual-zone snapshot — served by `get_change_snapshot`. Returns None
    when this change has no stored image of that kind, so the UI shows "n/a"
    instead of a broken tile. Path is relative to the client's `/api` base."""
    if not ref:
        return None
    return f"/targets/{target_id}/changes/{change_id}/snapshot/{kind}"


async def trigger_auto_redistribution(db: AsyncSession, reason: str):
    """
    Trigger automatic target redistribution using capacity-aware distributor.

    Called when topology changes (targets added/removed, agents join/leave).
    """
    try:
        from services.capacity_aware_distributor import CapacityAwareDistributor
        from models.config import Config

        # Get global period for capacity calculations
        global_period_result = await db.execute(
            select(Config).where(Config.key == "global_period_ms")
        )
        global_period_config = global_period_result.scalar_one_or_none()
        global_period_ms = int(global_period_config.value) if global_period_config else 10000

        distributor = CapacityAwareDistributor(db)
        stats = await distributor.distribute_timeslots_and_targets(global_period_ms)
        logger.info(f"Auto-redistribution triggered ({reason}): {stats}")
        return stats
    except Exception as e:
        logger.error(f"Auto-redistribution failed ({reason}): {e}")
        return None


# Pydantic models
def _coerce_setup_steps_to_json(v):
    """Normalize an inline setup_steps manifest to a JSON string for storage.

    Accepts a JSON object (dict) or a JSON string. A dict is serialized; a string
    is validated as JSON and stored verbatim (trimmed). Empty / "null" clears it
    (stored as NULL). Raises ValueError on non-JSON strings so the API rejects junk
    rather than persisting an unparseable manifest the recorder would silently drop.
    """
    if v is None:
        return None
    if isinstance(v, dict):
        return json.dumps(v)
    if isinstance(v, str):
        s = v.strip()
        if s == "" or s.lower() == "null":
            return None
        try:
            json.loads(s)
        except (ValueError, TypeError) as e:
            raise ValueError(f"setup_steps must be valid JSON: {e}")
        return s
    raise ValueError("setup_steps must be a JSON object or JSON string")


class CreateTargetRequest(BaseModel):
    """Request model for creating a target."""
    url: str = Field(..., max_length=2048, description="URL to monitor")
    check_type: str = Field(default="content", description="Type of check: 'content' or 'uptime'")
    selector: Optional[str] = Field(None, max_length=512, description="CSS selector for content extraction (optional - use target_selectors for multi-selector)")
    ignore_regex: Optional[str] = Field(None, description="Regex pattern to ignore in content")
    check_period_ms: Optional[int] = Field(None, gt=0, description="Custom check period in milliseconds (null = use global period)")
    # Structured recurrence (SPEC §1a/§2): absent schedule_kind ⇒ 'interval' (back-compat).
    schedule_kind: Optional[Literal["interval", "daily", "weekly"]] = Field(None, description="Recurrence kind: 'interval' | 'daily' | 'weekly'")
    schedule_time: Optional[str] = Field(None, description="'HH:MM' local wall-clock time (daily/weekly)")
    schedule_days: Optional[List[int]] = Field(None, description="ISO weekday ints 1=Mon..7=Sun (weekly)")
    schedule_tz: Optional[str] = Field(None, description="IANA tz name (daily/weekly); absent ⇒ UTC")
    enabled: bool = Field(default=True, description="Whether target is active")
    requires_playwright: bool = Field(default=False, description="Whether target requires Playwright for JavaScript rendering")
    preferred_region: Optional[str] = Field(None, max_length=50, description="Preferred geo region to run the check from (null = any)")
    # Inline setup-steps manifest ({steps, credentials}) replayed in the browser
    # BEFORE the content check. Accepts a JSON object or a JSON string; stored as a
    # JSON string and dispatched to the recorder as pre_check_workflow.
    setup_steps: Optional[Union[dict, str]] = Field(None, description="Inline {steps, credentials} setup-steps manifest replayed before the check")
    # Uptime monitoring fields
    expected_status_code: Optional[int] = Field(None, description="Expected HTTP status code for uptime checks")
    timeout_ms: Optional[int] = Field(None, description="Timeout for uptime checks in milliseconds")
    max_response_time_ms: Optional[int] = Field(None, description="Alert if response time exceeds this value")
    check_ssl: Optional[bool] = Field(None, description="Check SSL certificate for uptime monitors")

    _normalize_setup_steps = field_validator('setup_steps')(_coerce_setup_steps_to_json)


class UpdateTargetRequest(BaseModel):
    """Request model for updating a target."""
    url: Optional[str] = Field(None, max_length=2048)
    check_type: Optional[str] = Field(None, description="Type of check: 'content' or 'uptime'")
    selector: Optional[str] = Field(None, max_length=512)
    ignore_regex: Optional[str] = None
    check_period_ms: Optional[int] = Field(default=None, description="Custom check period in milliseconds (null = use global period)")
    # Structured recurrence (SPEC §1a/§2)
    schedule_kind: Optional[Literal["interval", "daily", "weekly"]] = None
    schedule_time: Optional[str] = None
    schedule_days: Optional[List[int]] = None
    schedule_tz: Optional[str] = None
    enabled: Optional[bool] = None
    requires_playwright: Optional[bool] = Field(None, description="Whether target requires Playwright for JavaScript rendering")
    preferred_region: Optional[str] = Field(None, max_length=50, description="Preferred geo region to run the check from (null = any)")
    # Inline setup-steps manifest ({steps, credentials}) replayed before the check.
    # Accepts a JSON object or string; stored as a JSON string. Send "" / "null" to clear.
    setup_steps: Optional[Union[dict, str]] = Field(None, description="Inline {steps, credentials} setup-steps manifest replayed before the check")
    # Uptime monitoring fields
    expected_status_code: Optional[int] = Field(None, description="Expected HTTP status code for uptime checks")
    timeout_ms: Optional[int] = Field(None, description="Timeout for uptime checks in milliseconds")
    max_response_time_ms: Optional[int] = Field(None, description="Alert if response time exceeds this value")
    check_ssl: Optional[bool] = Field(None, description="Check SSL certificate for uptime monitors")

    _normalize_setup_steps = field_validator('setup_steps')(_coerce_setup_steps_to_json)

    @field_validator('check_period_ms')
    @classmethod
    def validate_check_period_ms(cls, v):
        """Allow None (reset to global default) or positive integers."""
        if v is not None and v <= 0:
            raise ValueError('check_period_ms must be greater than 0')
        return v


class UpdateTargetNotificationsRequest(BaseModel):
    """Request model for updating target notification settings."""
    recipient_ids: list[int] = Field(default_factory=list, description="List of Pushover recipient IDs to notify")
    notification_title: Optional[str] = Field(None, max_length=250, description="Custom notification title for this target")
    notification_message: Optional[str] = Field(None, max_length=1024, description="Custom notification message for this target")
    notification_priority: Optional[int] = Field(None, ge=-2, le=2, description="Custom priority: -2 to 2")
    notification_sound: Optional[str] = Field(None, max_length=50, description="Custom notification sound")
    notification_providers: Optional[dict] = Field(None, description="Enabled notification providers {provider: true/false}")
    provider_notification_settings: Optional[dict] = Field(None, description="Per-provider notification customization {provider: {title, message, etc}}")


class TargetInfo(BaseModel):
    """Response model for target information."""
    id: int
    url: str
    check_type: str = Field(default="content", serialization_alias="checkType")
    selector: Optional[str] = None
    ignore_regex: Optional[str] = Field(None, serialization_alias="ignoreRegex")
    check_period_ms: Optional[int] = Field(None, serialization_alias="checkPeriodMs")
    # Structured recurrence (SPEC §1a/§2)
    schedule_kind: Optional[str] = Field(None, serialization_alias="scheduleKind")
    schedule_time: Optional[str] = Field(None, serialization_alias="scheduleTime")
    schedule_days: Optional[List[int]] = Field(None, serialization_alias="scheduleDays")
    schedule_tz: Optional[str] = Field(None, serialization_alias="scheduleTz")
    enabled: bool
    # Uptime monitoring fields
    expected_status_code: Optional[int] = Field(None, serialization_alias="expectedStatusCode")
    timeout_ms: Optional[int] = Field(None, serialization_alias="timeoutMs")
    max_response_time_ms: Optional[int] = Field(None, serialization_alias="maxResponseTimeMs")
    check_ssl: Optional[bool] = Field(None, serialization_alias="checkSsl")
    requires_playwright: bool = Field(default=False, serialization_alias="requiresPlaywright")
    preferred_region: Optional[str] = Field(None, serialization_alias="preferredRegion")
    # Advisory (set on create/update): the current fleet can't meet this monitor's
    # configured interval — it will run slower until more agents connect. None when fine.
    capacity_warning: Optional[str] = Field(None, serialization_alias="capacityWarning")
    # Metadata
    created_at: str = Field(..., serialization_alias="createdAt")
    updated_at: Optional[str] = Field(None, serialization_alias="updatedAt")
    # Field names match what the Monitors list UI reads (last_checked_at /
    # changes_count). check_count and assigned_agents were dropped — nothing
    # consumed them, and they were the sole reason for an extra per-poll query.
    last_checked_at: Optional[str] = Field(None, serialization_alias="lastCheckedAt")
    changes_count: int = Field(default=0, serialization_alias="changesCount")
    # Live state from Redis (steady-state checks don't write the DB). The DB only
    # records changes, so "last check" + current health come from monitoring_state.
    state: Optional[str] = Field(None, serialization_alias="state")  # up|down|ok|stale|never
    status_code: Optional[int] = Field(None, serialization_alias="statusCode")
    last_change_at: Optional[str] = Field(None, serialization_alias="lastChangeAt")

    class Config:
        populate_by_name = True


async def _live_fields_for(db: AsyncSession, targets) -> dict[int, dict]:
    """Compute the live monitoring overlay (last-check time, change count,
    health state, status code) for a set of targets.

    Steady-state checks don't touch the DB — they live in Redis — and the DB
    only records CHANGES, so the true "last check" + current health come from
    monitoring_state. The Monitors list and the single-target detail view both
    call this so they never disagree about freshness/health.
    """
    # DetectedChange is the authoritative change ledger (one row per detected
    # change) and supplies the last-CHANGE time + distinct-hash count the overlay
    # needs.
    from models.detected_change import DetectedChange
    from datetime import datetime, timezone
    from services.monitoring_state import get_states

    target_ids = [t.id for t in targets]
    if not target_ids:
        return {}

    # One GROUP BY over the change ledger: last-CHANGE time + distinct content
    # hashes (→ change count). A row only lands on a detected change.
    report_stats_result = await db.execute(
        select(
            DetectedChange.target_id,
            func.max(DetectedChange.last_detected_at),
            func.count(distinct(DetectedChange.content_hash)),
        )
        .where(DetectedChange.target_id.in_(target_ids))
        .group_by(DetectedChange.target_id)
    )
    report_stats = {row[0]: (row[1], row[2] or 0) for row in report_stats_result.all()}

    # Per-target live state lives in Redis. It's a DECORATION (freshness/health)
    # over the authoritative DB rows — a Redis blip must never blank the monitors
    # list, so degrade to a DB-only overlay ("never" state, last-change from the
    # reports aggregate above) instead of letting the whole listing 500.
    try:
        live = await get_states(target_ids)
    except Exception:
        logger.warning(
            "monitoring live-state fetch failed; serving monitors without the live overlay",
            exc_info=True,
        )
        live = {}
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    out: dict[int, dict] = {}
    for target in targets:
        last_checked, distinct_hashes = report_stats.get(target.id, (None, 0))
        # Each DetectedChange row is already a real change (the baseline snapshot is
        # never written to the ledger), so the distinct-hash count IS the change count.
        changes_count = max(0, distinct_hashes)

        st = live.get(target.id)
        live_checked_ms = st.get("checked_at") if st else None
        state = (st.get("state") if st else None) or "never"
        status_code = st.get("status_code") if st else None
        last_change_ms = st.get("last_change_at") if st else None
        # Prefer the Redis last-check time; fall back to the DB change time.
        if live_checked_ms is not None:
            last_checked_iso = datetime.fromtimestamp(
                live_checked_ms / 1000, tz=timezone.utc
            ).isoformat()
            # Stale = past the EXPECTED cadence, not the configured interval.
            # No single agent checks faster than the 60s anti-detection floor,
            # so a fast (e.g. 10s) target legitimately reports ~every 60s with a
            # small fleet — comparing against 10s would falsely flag it stale.
            expected = max(target.check_period_ms or 60000, 60000)
            if (now_ms - live_checked_ms) > expected * 2.5 and state in ("up", "ok"):
                state = "stale"
        else:
            last_checked_iso = last_checked.isoformat() if last_checked else None

        out[target.id] = {
            "last_checked_at": last_checked_iso,
            "changes_count": changes_count,
            "state": state,
            "status_code": status_code,
            "last_change_at": (
                datetime.fromtimestamp(last_change_ms / 1000, tz=timezone.utc).isoformat()
                if last_change_ms else None
            ),
        }
    return out


def _build_target_info(target, live: Optional[dict] = None) -> "TargetInfo":
    """Build a TargetInfo from a Target row + its live overlay (from
    _live_fields_for). One constructor so list + detail stay in lockstep."""
    live = live or {}
    return TargetInfo(
        id=target.id,
        url=target.url,
        check_type=target.check_type or "content",
        selector=target.selector,
        ignore_regex=target.ignore_regex,
        check_period_ms=target.check_period_ms,
        schedule_kind=target.schedule_kind or "interval",
        schedule_time=target.schedule_time,
        schedule_days=target.schedule_days,
        schedule_tz=target.schedule_tz,
        enabled=target.enabled,
        expected_status_code=target.expected_status_code,
        timeout_ms=target.timeout_ms,
        max_response_time_ms=target.max_response_time_ms,
        check_ssl=target.check_ssl,
        requires_playwright=target.requires_playwright,
        preferred_region=target.preferred_region,
        created_at=target.created_at.isoformat(),
        updated_at=target.updated_at.isoformat() if target.updated_at else None,
        # Prefer the live Redis overlay (freshest) but fall back to the DURABLE
        # DB stamp (targets.last_checked_at, set on every agent check-report) so
        # "last checked" survives a coordinator restart / fakeredis reset and
        # reflects EVERY check — not just the last detected change.
        last_checked_at=(
            live.get("last_checked_at")
            or (target.last_checked_at.isoformat() if target.last_checked_at else None)
        ),
        changes_count=live.get("changes_count", 0),
        state=live.get("state"),
        status_code=live.get("status_code"),
        last_change_at=live.get("last_change_at"),
    )


def _parse_change_cursor(since: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 change cursor into an aware datetime.

    Invalid input is REJECTED (422) rather than ignored. A cursor the server
    quietly drops silently downgrades an incremental poll into a full re-read,
    and the caller has no way to notice it just reprocessed its whole history.
    """
    if not since:
        return None
    raw = since.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid `since` cursor: expected an ISO-8601 timestamp",
        )
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _apply_change_cursor(query, since_dt: Optional[datetime], since_id: Optional[int]):
    """Order + filter a DetectedChange query for either read mode.

    Without a cursor: newest-first, the browsing order the UI wants.

    With a cursor: a keyset walk FORWARD in time, ordered by the very field the
    cursor advances on. `(last_detected_at, id)` is compared as a pair because
    `last_detected_at` is not unique — ordering on the timestamp alone lets two
    rows sharing a millisecond straddle the limit boundary, and the one left
    behind is never returned again.

    Note `last_detected_at` moves when a change is re-detected, so a row the
    caller already saw legitimately reappears with a later cursor value. That is
    a new detection, not a duplicate; clients de-duplicate on (id, cursor), which
    is exactly what the SDK `watch()` helpers do.
    """
    from models.detected_change import DetectedChange

    if since_dt is None:
        return query.order_by(
            DetectedChange.last_detected_at.desc(), DetectedChange.id.desc()
        )
    return query.where(
        or_(
            DetectedChange.last_detected_at > since_dt,
            and_(
                DetectedChange.last_detected_at == since_dt,
                DetectedChange.id > (since_id or 0),
            ),
        )
    ).order_by(DetectedChange.last_detected_at.asc(), DetectedChange.id.asc())


class TargetChangeInfo(BaseModel):
    """Response model for target change information."""
    id: str
    targetId: str
    timestamp: str
    oldContent: str
    newContent: str
    diff: str
    detectedBy: str
    # The two real timestamps behind `timestamp` (which is first_detected_at, kept
    # under its original name for existing callers). Both are exposed because the
    # feed is ORDERED by last_detected_at: without it a client re-sorting on
    # `timestamp` silently disagrees with the server's paging order, and cursor
    # paging has nothing to advance on.
    firstDetectedAt: str
    lastDetectedAt: str
    # Multi-selector support
    selectorId: Optional[int] = None
    selectorName: Optional[str] = None
    # Visual checks: loadable image URLs (presigned MinIO, or data: fallback)
    # for the before / after / pixel-delta-overlay of the change.
    screenshotBefore: Optional[str] = None
    screenshotAfter: Optional[str] = None
    screenshotDiff: Optional[str] = None


class RecentChangeInfo(BaseModel):
    """One row of the GLOBAL recent-changes feed (across all monitors).
    snake_case on purpose — it mirrors the desktop daemon's feed and
    is read field-for-field by the Monitors-list "what changed" card preview."""
    id: int
    target_id: int
    target_url: str
    target_selector_id: Optional[int] = None
    selector_name: Optional[str] = None
    diff_snippet: Optional[str] = None
    first_detected_at: str
    last_detected_at: str


@router.get(
    "",
    response_model=list[TargetInfo],
    summary="List Targets",
    description="List all monitoring targets.",
)
async def list_targets(
    enabled_only: bool = False,
    check_type: Optional[str] = Query(None, description="Filter by check type: 'content' or 'uptime'"),
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """
    List all monitoring targets.

    Can optionally filter by enabled status and check type.
    """
    try:
        check_api_key_scope(current_api_key, "checks", "read")

        # This endpoint is polled ~every 15s. Load ONLY the columns TargetInfo
        # serializes — never the heavy baseline_content (full page HTML) or the
        # auth_session_encrypted blob (and other unread columns), which the
        # default select(Target) would pull on every row, every poll.
        query = (
            select(Target)
            .options(load_only(
                Target.id,
                Target.url,
                Target.check_type,
                Target.selector,
                Target.ignore_regex,
                Target.check_period_ms,
                Target.schedule_kind,
                Target.schedule_time,
                Target.schedule_days,
                Target.schedule_tz,
                Target.enabled,
                Target.expected_status_code,
                Target.timeout_ms,
                Target.max_response_time_ms,
                Target.check_ssl,
                Target.requires_playwright,
                Target.preferred_region,
                Target.created_at,
                Target.updated_at,
                # Durable "last checked" stamp read by _build_target_info as the
                # fallback when the Redis overlay is absent. MUST be in load_only —
                # otherwise reading target.last_checked_at during (sync) serialization
                # triggers a deferred column load on the async session, raising
                # MissingGreenlet ("IO attempted in an unexpected place").
                Target.last_checked_at,
            ))
        )

        allowed_ids = filter_by_scope(current_api_key, "checks")
        if allowed_ids is not None and len(allowed_ids) > 0:
            query = query.where(Target.id.in_(allowed_ids))
        elif allowed_ids is not None:
            return []

        if enabled_only:
            query = query.where(Target.enabled == True)

        if check_type:
            query = query.where(Target.check_type == check_type)

        result = await db.execute(query.order_by(Target.created_at.desc()))
        targets = result.scalars().all()

        # Live last-check + health overlay (Redis state + report aggregate),
        # shared with the detail endpoint so the two never disagree. Best-effort:
        # the overlay is a decoration and must never blank the list — the desktop's
        # cloud-reflected Monitors dual-view reads this endpoint, and a 500 here
        # would wrongly surface as "no monitors" even when monitors exist.
        try:
            live_fields = await _live_fields_for(db, targets)
        except Exception:
            logger.warning(
                "live overlay failed; listing monitors without freshness", exc_info=True
            )
            live_fields = {}
        return [_build_target_info(t, live_fields.get(t.id)) for t in targets]

    except Exception:
        # Full traceback (not just str(e)) so the true trigger is visible. This
        # handler previously masked every failure as an opaque "Failed to list
        # targets", hiding e.g. a live-overlay/serialization error behind the 500.
        logger.exception("Error listing targets")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list targets",
        )


@router.get(
    "/{target_id}",
    response_model=TargetInfo,
    summary="Get Target",
    description="Get a specific target by ID.",
)
async def get_target(
    target_id: int,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """
    Get a specific target by ID.
    """
    try:
        check_api_key_scope(current_api_key, "checks", "read", target_id)
        target = await _get_target_for_owner(db, target_id, current_api_key)

        # Same live overlay the list uses, so the detail view shows the real
        # last-check time / health instead of a perpetual "Pending".
        live_fields = await _live_fields_for(db, [target])
        return _build_target_info(target, live_fields.get(target.id))

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting target: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get target",
        )


def _capacity_warning(check_period_ms: Optional[int]) -> Optional[str]:
    """Advisory string when the live fleet can't meet this monitor's configured
    interval (too few agents for the requested cadence). Best-effort; never raises."""
    try:
        from services.fleet_capacity import capacity_warning_for, current_agents_online
        return capacity_warning_for(check_period_ms, current_agents_online())
    except Exception:
        return None


@router.post(
    "",
    response_model=TargetInfo,
    status_code=status.HTTP_201_CREATED,
    summary="Create Target",
    description="Create a new monitoring target. Requires operator role or higher.",
)
async def create_target(
    request: CreateTargetRequest,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
    _gate=Depends(require_feature("targets")),
):
    """
    Create a new monitoring target.
    """
    check_api_key_scope(current_api_key, "checks", "write")

    # Validate inputs
    from security.validation import InputValidator

    # Validate URL — DNS-resolving check blocks encoded-IP / IPv6 / *.internal hosts
    # that the regex-only validate_url misses.
    validated_url = InputValidator.validate_url_with_dns(request.url, allow_private=settings.allow_private_targets)

    # Domain blocklist (abuse control)
    from services import domain_guard
    await domain_guard.enforce(db, validated_url, actor=f"apikey:{current_api_key.get('id')}")

    # robots.txt posture: single-owner coordinator has no per-org opt-in.
    from services import robots_guard
    await robots_guard.enforce(validated_url, False)

    # Validate check_type
    if request.check_type not in ["content", "uptime"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="check_type must be 'content' or 'uptime'",
        )

    # Validate regex if provided
    if request.ignore_regex:
        InputValidator.validate_regex(request.ignore_regex)

    # Validate CSS selector if provided (optional - can use target_selectors instead)
    if request.selector:
        InputValidator.validate_css_selector(request.selector)

    # Structured recurrence (SPEC §2/§3): validate + normalize the schedule fields.
    # A tenant-stripped coordinator has no plan-tier interval floor, so only the
    # structured-schedule validation applies (daily/weekly always run ≥ 1/day).
    from services.schedule_recurrence import normalize_schedule, ScheduleValidationError
    try:
        sched_kind, sched_time, sched_days, sched_tz = normalize_schedule(
            request.schedule_kind, request.schedule_time, request.schedule_days, request.schedule_tz
        )
    except ScheduleValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    try:
        # NOTE: no duplicate-URL rejection. Any URL may be monitored — and the
        # same URL multiple times (e.g. several visual zones). Identical
        # anonymous checks are coalesced into ONE physical fetch via fetch_key
        # (services.monitor_coalescing) at distribution time, so this never
        # multiplies slots. Users never see a collision.

        # The coordinator NEVER fetches a target URL. The baseline (hash + content)
        # is deferred to the first agent `target_check_batch` report — the monitoring
        # ingest baseline-init sets it from what a connected agent actually fetched.
        baseline_hash = None
        baseline_content = None
        baseline_fetched_at = None

        target = Target(
            url=validated_url,
            check_type=request.check_type,
            selector=request.selector,
            ignore_regex=request.ignore_regex,
            check_period_ms=request.check_period_ms,
            schedule_kind=sched_kind,
            schedule_time=sched_time,
            schedule_days=sched_days,
            schedule_tz=sched_tz,
            enabled=request.enabled,
            requires_playwright=request.requires_playwright,
            preferred_region=request.preferred_region,
            # Inline setup-steps manifest (already normalized to a JSON string by the
            # request validator); dispatched to the recorder as pre_check_workflow.
            setup_steps=request.setup_steps,
            baseline_hash=baseline_hash,
            baseline_content=baseline_content,
            baseline_fetched_at=baseline_fetched_at,
            # Uptime monitoring fields
            expected_status_code=request.expected_status_code,
            timeout_ms=request.timeout_ms,
            max_response_time_ms=request.max_response_time_ms,
            check_ssl=request.check_ssl,
            created_at=datetime.utcnow(),
        )

        # Coalescing key: identical anonymous checks share one physical fetch.
        from services.monitor_coalescing import fetch_key_for_target
        target.fetch_key = fetch_key_for_target(target)

        db.add(target)
        await db.flush()

        # A content target is only CHECKED if it has a TargetSelector row — the
        # dispatch builds each check's selector list from `target.selectors`, so the
        # `selector` COLUMN alone is inert (target shows "0 selectors", never checks).
        # Promote a `selector` provided on create into a real selector row so a target
        # made with a selector is checkable out of the box (mirrors POST .../selectors).
        if request.check_type != "uptime" and request.selector:
            from models.target_selector import TargetSelector
            db.add(TargetSelector(
                target_id=target.id,
                name=request.selector[:255],
                selector=request.selector,
                content_type="text",
                enabled=True,
                priority=0,
                ignore_regex=request.ignore_regex,
            ))
            await db.flush()

        await db.commit()

        logger.info(
            f"Target created: {request.url} by {current_api_key.get('label')}"
        )

        # Trigger redistribution to assign new target to agents
        # Hot-reload will handle this without rebuilding schedulers (offsets won't change)
        if request.enabled:
            await trigger_auto_redistribution(db, "target_created")

        return TargetInfo(
            id=target.id,
            url=target.url,
            check_type=target.check_type,
            selector=target.selector,
            ignore_regex=target.ignore_regex,
            check_period_ms=target.check_period_ms,
            schedule_kind=target.schedule_kind or "interval",
            schedule_time=target.schedule_time,
            schedule_days=target.schedule_days,
            schedule_tz=target.schedule_tz,
            enabled=target.enabled,
            expected_status_code=target.expected_status_code,
            timeout_ms=target.timeout_ms,
            max_response_time_ms=target.max_response_time_ms,
            check_ssl=target.check_ssl,
            requires_playwright=target.requires_playwright,
            preferred_region=target.preferred_region,
            capacity_warning=_capacity_warning(target.check_period_ms) if target.enabled else None,
            created_at=target.created_at.isoformat(),
            updated_at=None,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating target: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create target",
        )


@router.patch(
    "/{target_id}",
    response_model=TargetInfo,
    summary="Update Target",
    description="Update a target. Requires operator role or higher.",
)
async def update_target(
    target_id: int,
    request: UpdateTargetRequest,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """
    Update a monitoring target.
    """
    check_api_key_scope(current_api_key, "checks", "write", target_id)

    try:
        target = await _get_target_for_owner(db, target_id, current_api_key)

        # Update fields
        update_data = request.model_dump(exclude_unset=True)

        # Validate URL to prevent SSRF — DNS-resolving check (catches encoded-IP /
        # IPv6 / *.internal hosts the regex-only check misses).
        if 'url' in update_data:
            from security.validation import InputValidator
            update_data['url'] = InputValidator.validate_url_with_dns(
                update_data['url'],
                allow_private=settings.allow_private_targets
            )
            from services import domain_guard
            await domain_guard.enforce(db, update_data['url'], actor=f"apikey:{current_api_key.get('id')}")
            # robots.txt posture: single-owner coordinator has no per-org opt-in.
            from services import robots_guard
            await robots_guard.enforce(update_data['url'], False)

        # If check_type is being changed, validate the change
        if 'check_type' in update_data:
            if update_data['check_type'] not in ["content", "uptime"]:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="check_type must be 'content' or 'uptime'",
                )

        # The coordinator NEVER fetches a target URL, so a changed selector/URL is
        # NOT re-validated here — the next agent `target_check_batch` report
        # re-establishes the baseline for the new selector. The ignore_regex is
        # different: it gets executed, so it needs the SAME validation the create
        # path applies (validate_regex — length, nesting and structural ReDoS
        # caps). A bare `re.compile` only proves the syntax parses, and `(a+)+$`
        # compiles perfectly well, which made create-then-PATCH a clean bypass.
        if 'ignore_regex' in update_data and update_data['ignore_regex']:
            InputValidator.validate_regex(update_data['ignore_regex'])

        # Check if enabled status or check period changed (for redistribution)
        enabled_changed = 'enabled' in update_data and update_data['enabled'] != target.enabled
        period_changed = 'check_period_ms' in update_data and update_data['check_period_ms'] != target.check_period_ms

        # Structured recurrence (SPEC §2/§3): validate + normalize the schedule
        # fields as a unit, merging omitted keys with the persisted values, then
        # set them explicitly (removed from the blind setattr loop below).
        _sched_touched = any(
            k in update_data for k in ('schedule_kind', 'schedule_time', 'schedule_days', 'schedule_tz')
        )
        if _sched_touched:
            from services.schedule_recurrence import normalize_schedule, ScheduleValidationError
            _kind = update_data.get('schedule_kind', target.schedule_kind or "interval")
            _time = update_data.get('schedule_time', target.schedule_time)
            _days = update_data.get('schedule_days', target.schedule_days)
            _tz = update_data.get('schedule_tz', target.schedule_tz)
            try:
                s_kind, s_time, s_days, s_tz = normalize_schedule(_kind, _time, _days, _tz)
            except ScheduleValidationError as e:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
            target.schedule_kind = s_kind
            target.schedule_time = s_time
            target.schedule_days = s_days
            target.schedule_tz = s_tz
        for _k in ('schedule_kind', 'schedule_time', 'schedule_days', 'schedule_tz'):
            update_data.pop(_k, None)

        for field, value in update_data.items():
            setattr(target, field, value)

        target.updated_at = datetime.utcnow()

        # Recompute the coalescing key — url / interval / browser / region / auth
        # may have changed, which moves this check to a different physical fetch.
        from services.monitor_coalescing import fetch_key_for_target
        target.fetch_key = fetch_key_for_target(target)

        await db.commit()

        logger.info(
            f"Target updated: {target.url} by {current_api_key.get('label')}"
        )

        # Trigger redistribution when target is enabled/disabled
        # Hot-reload will handle this without rebuilding schedulers (offsets won't change)
        if enabled_changed:
            if update_data.get('enabled'):
                await trigger_auto_redistribution(db, "target_enabled")
            else:
                await trigger_auto_redistribution(db, "target_disabled")

        # Keep redistribution for period changes - agents need to move between period groups
        if period_changed:
            # When period changes, we need to redistribute both targets AND period-specific offsets
            # Use capacity-aware distributor to handle period-specific offset redistribution
            from services.capacity_aware_distributor import CapacityAwareDistributor
            from models.config import Config

            # Get global period
            global_period_result = await db.execute(
                select(Config).where(Config.key == "global_period_ms")
            )
            global_period_config = global_period_result.scalar_one_or_none()
            global_period_ms = int(global_period_config.value) if global_period_config else 10000

            # Redistribute timeslots and targets to handle new period group
            distributor = CapacityAwareDistributor(db)
            stats = await distributor.distribute_timeslots_and_targets(global_period_ms)
            logger.info(f"Period changed - redistributed timeslots and targets: {stats}")

        return TargetInfo(
            id=target.id,
            url=target.url,
            check_type=target.check_type or "content",
            selector=target.selector,
            ignore_regex=target.ignore_regex,
            check_period_ms=target.check_period_ms,
            schedule_kind=target.schedule_kind or "interval",
            schedule_time=target.schedule_time,
            schedule_days=target.schedule_days,
            schedule_tz=target.schedule_tz,
            enabled=target.enabled,
            capacity_warning=_capacity_warning(target.check_period_ms) if target.enabled else None,
            expected_status_code=target.expected_status_code,
            timeout_ms=target.timeout_ms,
            max_response_time_ms=target.max_response_time_ms,
            check_ssl=target.check_ssl,
            requires_playwright=target.requires_playwright,
            preferred_region=target.preferred_region,
            created_at=target.created_at.isoformat(),
            updated_at=target.updated_at.isoformat() if target.updated_at else None,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating target: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update target",
        )


class TargetRunResult(BaseModel):
    """Outcome of an out-of-schedule check request."""
    ok: bool
    dispatched: int
    detail: Optional[str] = None


@router.post(
    "/{target_id}/run",
    response_model=TargetRunResult,
    summary="Check a target now",
    description=(
        "Trigger an immediate, out-of-schedule check of this monitor. The request "
        "is queued for whichever agent(s) the target is assigned to and picked up "
        "on their next poll; the result — and its report — flow through the normal "
        "path. Monitors otherwise only run on their configured cadence."
    ),
)
async def run_target_check_now(
    target_id: int,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """On-demand "Check now".

    Delivery rides the poll lane, not a push: each assigned agent has a Redis set
    of target ids it should check immediately, which `POST /agents/poll` drains
    into `check_now_target_ids`. That is the only channel an HTTP-poll agent
    actually listens on — a DB flag never reaches it.

    A monitor with no agent assigned yet answers `ok: false` with a reason rather
    than pretending a check was queued.
    """
    check_api_key_scope(current_api_key, "checks", "write", target_id)
    target = await _get_target_for_owner(db, target_id, current_api_key)
    if target.enabled is False:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This monitor is paused — resume it before checking now.",
        )

    from models.target_assignment import TargetAssignment

    rows = await db.execute(
        select(TargetAssignment.agent_id).where(TargetAssignment.target_id == target_id)
    )
    agent_ids = [r[0] for r in rows.all()]
    if not agent_ids:
        return TargetRunResult(
            ok=False,
            dispatched=0,
            detail="No agent is assigned to this monitor yet — it will run on its next scheduled cycle.",
        )

    try:
        from utils.redis_client import get_redis
        redis = get_redis()
    except Exception as e:
        logger.error(f"check_now unavailable (no redis): {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Check-now queue is unavailable — the monitor will run on its next scheduled cycle.",
        )

    dispatched = 0
    for agent_id in agent_ids:
        try:
            key = f"check_now:{agent_id}"
            await redis.sadd(key, target_id)
            # A request nobody drains within the window is stale; expiring it
            # keeps a long-offline agent from running a burst of old checks the
            # moment it reconnects.
            await redis.expire(key, 300)
            dispatched += 1
        except Exception as e:
            logger.warning(f"check_now enqueue failed for agent {agent_id}: {e}")

    if dispatched == 0:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not queue the check — the monitor will run on its next scheduled cycle.",
        )
    return TargetRunResult(ok=True, dispatched=dispatched)


@router.get(
    "/changes/recent",
    response_model=list[RecentChangeInfo],
    summary="Recent Changes (all monitors)",
    description="Newest-first detected content changes across all monitors.",
)
async def list_recent_changes(
    limit: int = Query(50, ge=1, le=200),
    since: Optional[str] = Query(
        None,
        description=(
            "Keyset cursor: return only changes detected AFTER this ISO-8601 "
            "timestamp, oldest-first. Advance it to the last row's "
            "`last_detected_at` to walk the feed forward without gaps. Omit for "
            "the newest-first browsing view."
        ),
    ),
    since_id: Optional[int] = Query(
        None,
        ge=0,
        description="Tie-breaker for rows sharing the `since` timestamp — the last row's `id`.",
    ),
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Global change feed powering the Monitors-list "what changed" preview +
    the recently-changed group, and the cursor lane every SDK `watch()` polls.
    Honours per-key check scoping. Static path is declared BEFORE
    `/{target_id}/changes` so it never resolves as a target id.

    Errors are RAISED, not swallowed. This route used to answer [] on any
    exception so a hiccup could not blank the UI list; that made "nothing
    changed" and "the query blew up" indistinguishable on the wire, which is
    silent data loss for anything polling it. Presentation-layer fallback belongs
    in the caller."""
    check_api_key_scope(current_api_key, "checks", "read")
    from models.detected_change import DetectedChange
    from sqlalchemy.orm import selectinload

    since_dt = _parse_change_cursor(since)

    query = (
        select(DetectedChange, Target.url)
        .join(Target, Target.id == DetectedChange.target_id)
        .options(selectinload(DetectedChange.target_selector))
    )

    allowed_ids = filter_by_scope(current_api_key, "checks")
    if allowed_ids is not None and len(allowed_ids) > 0:
        query = query.where(DetectedChange.target_id.in_(allowed_ids))
    elif allowed_ids is not None:
        return []

    query = _apply_change_cursor(query, since_dt, since_id).limit(limit)
    rows = (await db.execute(query)).all()

    out: list[RecentChangeInfo] = []
    for change, url in rows:
        sel_name = change.target_selector.name if (
            change.target_selector_id and change.target_selector
        ) else None
        out.append(RecentChangeInfo(
            id=change.id,
            target_id=change.target_id,
            target_url=url,
            target_selector_id=change.target_selector_id,
            selector_name=sel_name,
            diff_snippet=(change.diff_snippet[:280] if change.diff_snippet else None),
            first_detected_at=change.first_detected_at.isoformat(),
            last_detected_at=change.last_detected_at.isoformat(),
        ))
    return out


@router.get(
    "/{target_id}/changes",
    response_model=list[TargetChangeInfo],
    summary="Get Target Changes",
    description="Get all changes detected for a specific target.",
)
async def get_target_changes(
    target_id: int,
    limit: int = Query(100, ge=1, le=1000),
    since: Optional[str] = Query(
        None,
        description=(
            "Keyset cursor: return only changes detected AFTER this ISO-8601 "
            "timestamp, oldest-first. Advance it to the last row's "
            "`lastDetectedAt`. Omit for the newest-first browsing view."
        ),
    ),
    since_id: Optional[int] = Query(
        None,
        ge=0,
        description="Tie-breaker for rows sharing the `since` timestamp — the last row's `id`.",
    ),
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """
    Get detected changes for a target, newest-first — or oldest-first from a
    cursor when `since` is supplied (see `/changes/recent` for the walk).
    """
    try:
        check_api_key_scope(current_api_key, "checks", "read", target_id)
        # Verify target exists
        target = await _get_target_for_owner(db, target_id, current_api_key)

        # Fetch detected changes (one row per unique change)
        from models.detected_change import DetectedChange
        from models.target_selector import TargetSelector
        from sqlalchemy.orm import selectinload

        since_dt = _parse_change_cursor(since)
        query = (
            select(DetectedChange)
            .options(selectinload(DetectedChange.target_selector))
            .where(DetectedChange.target_id == target_id)
        )
        query = _apply_change_cursor(query, since_dt, since_id).limit(limit)

        result = await db.execute(query)
        detected_changes = result.scalars().all()

        # Convert to response format
        changes = []
        for change in detected_changes:
            # Get before/after content from stored fields or parse from diff
            content_before = change.content_before
            content_after = change.content_after

            if not content_before and not content_after and change.diff_snippet:
                content_before, content_after = parse_diff_snippet(change.diff_snippet)

            # Show "X agents" in detectedBy
            detected_by = f"{change.agent_count} agent{'s' if change.agent_count > 1 else ''}"

            # If still no content available, provide helpful message
            if not content_before and not content_after:
                if not change.diff_snippet:
                    content_before = "(baseline content not available at time of detection)"
                    content_after = "(changed content not available)"
                else:
                    # Diff exists but couldn't be parsed - show it as-is
                    content_before = "(unable to parse diff)"
                    content_after = change.diff_snippet or ""

            # Get selector info if available
            selector_id = change.target_selector_id
            selector_name = None
            if selector_id and hasattr(change, 'target_selector') and change.target_selector:
                selector_name = change.target_selector.name

            changes.append(TargetChangeInfo(
                id=str(change.id),
                targetId=str(change.target_id),
                timestamp=change.first_detected_at.isoformat(),
                firstDetectedAt=change.first_detected_at.isoformat(),
                lastDetectedAt=change.last_detected_at.isoformat(),
                oldContent=content_before or "",
                newContent=content_after or "",
                diff=change.diff_snippet or "No diff available",
                detectedBy=detected_by,
                selectorId=selector_id,
                selectorName=selector_name,
                # Same-origin proxy paths (not presigned MinIO URLs): the browser
                # blob-fetches these through the Bearer-authenticated client, so
                # images load regardless of MINIO_PUBLIC_ENDPOINT / HTTPS. A path is
                # emitted only when that snapshot kind actually has stored bytes.
                screenshotBefore=_snapshot_proxy_path(target_id, change.id, 'before', getattr(change, 'screenshot_before', None)),
                screenshotAfter=_snapshot_proxy_path(target_id, change.id, 'after', getattr(change, 'screenshot_after', None)),
                screenshotDiff=_snapshot_proxy_path(target_id, change.id, 'diff', getattr(change, 'screenshot_diff', None)),
            ))

        return changes

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching target changes: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch target changes",
        )


# Map the URL `kind` to the DetectedChange column holding that snapshot's ref.
_SNAPSHOT_KIND_FIELDS = {
    "before": "screenshot_before",
    "after": "screenshot_after",
    "diff": "screenshot_diff",
}


@router.get(
    "/{target_id}/changes/{change_id}/snapshot/{kind}",
    summary="Get Visual Change Snapshot",
    description="Stream a visual-zone before/after/diff snapshot image for a change.",
)
async def get_change_snapshot(
    target_id: int,
    change_id: int,
    kind: str,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Same-origin, ownership-checked image proxy for visual-zone snapshots.

    Visual snapshots live in MinIO (or inline base64 when MinIO is down). A
    presigned MinIO URL is unusable from the browser — the public endpoint is
    typically unreachable (defaults to localhost:9000, mixed-content under HTTPS),
    and an ``<img>`` can't carry the API's Bearer token. So the frontend
    blob-fetches THIS authenticated route instead, and we stream the bytes after
    verifying the change belongs to a target the caller owns."""
    from fastapi.responses import Response
    from services import visual_storage

    field = _SNAPSHOT_KIND_FIELDS.get(kind)
    if field is None:
        raise HTTPException(status_code=404, detail="Unknown snapshot kind")

    # Ownership check: 404 (not 403) so a foreign target_id can't be probed.
    check_api_key_scope(current_api_key, "checks", "read", target_id)
    await _get_target_for_owner(db, target_id, current_api_key)

    from models.detected_change import DetectedChange
    result = await db.execute(
        select(DetectedChange).where(
            DetectedChange.id == change_id,
            DetectedChange.target_id == target_id,
        )
    )
    change = result.scalar_one_or_none()
    if change is None:
        raise HTTPException(status_code=404, detail="Change not found")

    ref = getattr(change, field, None)
    raw = visual_storage.fetch_snapshot_bytes(ref) if ref else None
    if not raw:
        # No image for this kind (e.g. diff couldn't be computed) or storage miss.
        raise HTTPException(status_code=404, detail="Snapshot not available")
    return Response(
        content=raw,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.get(
    "/{target_id}/errors",
    summary="Get Target Check Errors",
    description="Get recent check errors for a target with full details.",
)
async def get_target_errors(
    target_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """
    Recent agent-reported check errors for this target.

    Check errors are user-actionable (the user's own target failed to fetch,
    blocked, rate-limited, ...) so full details are returned to end users —
    unlike internal backend errors, which are sanitized.
    """
    check_api_key_scope(current_api_key, "checks", "read", target_id)
    target = await _get_target_for_owner(db, target_id, current_api_key)

    # There is no per-target check-error ledger in this build, so return an empty
    # list (the endpoint stays live for the Monitor detail "Errors" panel).
    return {
        "target_id": target.id,
        "target_url": target.url,
        "count": 0,
        "errors": [],
    }


@router.get(
    "/{target_id}/preview",
    summary="Get Target Preview",
    description="Get current content preview for a target.",
)
async def get_target_preview(
    target_id: int,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """
    Return the target's last-known content preview.

    The coordinator NEVER fetches a target URL — the content baseline is
    established by the connected agent's `target_check_batch` reports. This
    endpoint therefore returns the STORED baseline (what an agent last fetched),
    not a live in-process fetch.

    Returns:
        - content: last stored baseline content (None until the first agent report)
        - content_hash / baseline_hash: stored baseline hash
        - baseline_content: stored baseline content
        - changed: always False here (comparison happens agent-side at check time)
        - url: Target URL
        - fetched_at: when the stored baseline was last fetched by an agent
    """
    try:
        check_api_key_scope(current_api_key, "checks", "read", target_id)
        # Verify target exists
        target = await _get_target_for_owner(db, target_id, current_api_key)

        return {
            "content": target.baseline_content,
            "content_hash": target.baseline_hash,
            "baseline_hash": target.baseline_hash,
            "baseline_content": target.baseline_content,
            "changed": False,
            "url": target.url,
            "selector": target.selector,
            "fetched_at": (
                target.baseline_fetched_at.isoformat()
                if getattr(target, "baseline_fetched_at", None) else None
            ),
            "pending_baseline": target.baseline_hash is None,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error building target preview: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to build target preview",
        )


@router.patch(
    "/{target_id}/toggle",
    response_model=TargetInfo,
    summary="Toggle Target",
    description="Enable or disable a target. Requires operator role or higher.",
)
async def toggle_target(
    target_id: int,
    enabled: bool = Query(..., description="Enable or disable the target"),
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """
    Toggle a target's enabled state.
    """
    check_api_key_scope(current_api_key, "checks", "write", target_id)

    try:
        target = await _get_target_for_owner(db, target_id, current_api_key)

        # Update enabled state
        target.enabled = enabled
        target.updated_at = datetime.utcnow()

        await db.commit()

        logger.info(
            f"Target toggled: {target.url} enabled={enabled} by {current_api_key.get('label')}"
        )

        # Trigger redistribution when target is toggled
        # Hot-reload will handle this without rebuilding schedulers (offsets won't change)
        await trigger_auto_redistribution(db, "target_toggled")

        return TargetInfo(
            id=target.id,
            url=target.url,
            check_type=target.check_type or "content",
            selector=target.selector,
            ignore_regex=target.ignore_regex,
            check_period_ms=target.check_period_ms,
            schedule_kind=target.schedule_kind or "interval",
            schedule_time=target.schedule_time,
            schedule_days=target.schedule_days,
            schedule_tz=target.schedule_tz,
            enabled=target.enabled,
            expected_status_code=target.expected_status_code,
            timeout_ms=target.timeout_ms,
            max_response_time_ms=target.max_response_time_ms,
            check_ssl=target.check_ssl,
            requires_playwright=target.requires_playwright,
            preferred_region=target.preferred_region,
            created_at=target.created_at.isoformat(),
            updated_at=target.updated_at.isoformat() if target.updated_at else None,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error toggling target: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to toggle target",
        )


@router.delete(
    "/{target_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete Target",
    description="Delete a target. Requires operator role or higher.",
)
async def delete_target(
    target_id: int,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """
    Delete a monitoring target.
    """
    check_api_key_scope(current_api_key, "checks", "delete", target_id)

    try:
        target = await _get_target_for_owner(db, target_id, current_api_key)

        url = target.url

        # Manually delete related records (CASCADE may not work via SQLAlchemy for all relations)
        from models.target_assignment import TargetAssignment
        from models.target_selector import TargetSelector
        from models.trigger_rule import TriggerRule
        from models.notification_log import NotificationLog

        # Delete notification logs
        await db.execute(
            delete(NotificationLog).where(NotificationLog.target_id == target_id)
        )

        # Delete trigger rules
        await db.execute(
            delete(TriggerRule).where(TriggerRule.target_id == target_id)
        )

        # Delete target selectors
        await db.execute(
            delete(TargetSelector).where(TargetSelector.target_id == target_id)
        )

        # Delete assignments
        await db.execute(
            delete(TargetAssignment).where(TargetAssignment.target_id == target_id)
        )

        # Delete target (cascade will delete reports, detected_changes, etc.)
        await db.delete(target)

        await db.commit()

        logger.info(f"Target deleted: {url} by {current_api_key.get('label')}")

        # Trigger redistribution to remove target from agent assignments
        # Hot-reload will handle this without rebuilding schedulers (offsets won't change)
        await trigger_auto_redistribution(db, "target_deleted")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting target: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete target",
        )


# ============================================================================
# Target Notification Endpoints
# ============================================================================

@router.get(
    "/{target_id}/notifications",
    summary="Get Target Notification Recipients",
    description="Get list of Pushover recipients assigned to this target.",
)
async def get_target_notifications(
    target_id: int,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """
    Get Pushover recipients assigned to send notifications for this target.

    If no recipients are assigned, notifications are sent to all enabled recipients.
    """
    try:
        check_api_key_scope(current_api_key, "checks", "read", target_id)
        # Verify target exists
        target = await _get_target_for_owner(db, target_id, current_api_key)

        # Get assigned recipients
        from models.target_notification import TargetNotification
        from models.pushover_recipient import PushoverRecipient

        result = await db.execute(
            select(PushoverRecipient)
            .join(TargetNotification, TargetNotification.recipient_id == PushoverRecipient.id)
            .where(
                TargetNotification.target_id == target_id,
            )
            .order_by(PushoverRecipient.name)
        )
        recipients = result.scalars().all()

        return {
            "target_id": target_id,
            "target_url": target.url,
            "recipients": [
                {
                    "id": r.id,
                    "name": r.name,
                    "user_key": r.user_key[:10] + "..." if len(r.user_key) > 10 else r.user_key,
                    "enabled": r.enabled,
                }
                for r in recipients
            ],
            "count": len(recipients),
            "notification_title": target.notification_title,
            "notification_message": target.notification_message,
            "notification_priority": target.notification_priority,
            "notification_sound": target.notification_sound,
            "notification_providers": target.notification_providers or {},
            "provider_notification_settings": target.provider_notification_settings or {},
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting target notifications: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get target notifications",
        )


class SetTargetPersonaRequest(BaseModel):
    persona_id: Optional[int] = Field(None, description="Persona to supply check auth; null to detach")


@router.put(
    "/{target_id}/persona",
    summary="Attach/detach a persona to a check",
    description="Use a persona's warm session as this check's auth (alternative to a pre-check workflow).",
)
async def set_target_persona(
    target_id: int,
    request: SetTargetPersonaRequest,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    check_api_key_scope(current_api_key, "checks", "write", target_id)
    target = await _get_target_for_owner(db, target_id, current_api_key)

    if request.persona_id is None:
        target.persona_id = None
        await db.commit()
        return {"persona_id": None}

    # Resolve the persona and seed the check's auth from its warm session.
    import json
    from models.persona import Persona
    from services.persona_service import PersonaService
    from security.encryption import SecretEncryption

    persona = (await db.execute(
        select(Persona).where(Persona.id == request.persona_id)
    )).scalar_one_or_none()
    if not persona:
        raise HTTPException(status_code=404, detail="Persona not found")

    target.persona_id = persona.id
    session = PersonaService.load_session(persona)
    if session:
        # Match the Fernet(json) framing the check-auth path consumes.
        target.auth_session_encrypted = SecretEncryption.encrypt_secret(json.dumps(session))
    await db.commit()
    return {
        "persona_id": persona.id,
        "auth_seeded": bool(session),
        "detail": ("Check will use the persona's saved session." if session
                   else "Persona attached, but it has no saved session yet — run its login once."),
    }


@router.post(
    "/{target_id}/notifications",
    summary="Update Target Notification Settings",
    description="Update notification recipients and customization for this target. Replaces existing assignments.",
)
async def update_target_notifications(
    target_id: int,
    request: UpdateTargetNotificationsRequest,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """
    Update notification settings for a target.

    Updates both recipient assignments and per-target notification customization.
    Replaces all existing recipient assignments with the provided list.
    Pass empty list to remove all assignments (will send to all enabled recipients).
    """
    # Check role
    role = current_api_key.get("role", "").lower()
    if role not in ["operator", "admin"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires operator role or higher",
        )

    try:
        check_api_key_scope(current_api_key, "checks", "write", target_id)
        # Verify target exists
        target = await _get_target_for_owner(db, target_id, current_api_key)

        # Update per-target notification customization fields
        target.notification_title = request.notification_title
        target.notification_message = request.notification_message
        target.notification_priority = request.notification_priority
        target.notification_sound = request.notification_sound
        if request.notification_providers is not None:
            target.notification_providers = request.notification_providers
        if request.provider_notification_settings is not None:
            target.provider_notification_settings = request.provider_notification_settings

        # Verify all recipient IDs exist
        from models.pushover_recipient import PushoverRecipient
        from models.target_notification import TargetNotification

        if request.recipient_ids:
            recipients_result = await db.execute(
                select(PushoverRecipient).where(
                    PushoverRecipient.id.in_(request.recipient_ids),
                )
            )
            recipients = recipients_result.scalars().all()

            if len(recipients) != len(request.recipient_ids):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="One or more recipient IDs not found",
                )

        # Delete existing assignments
        await db.execute(
            delete(TargetNotification).where(TargetNotification.target_id == target_id)
        )

        # Create new assignments
        for recipient_id in request.recipient_ids:
            assignment = TargetNotification(
                target_id=target_id,
                recipient_id=recipient_id,
            )
            db.add(assignment)

        await db.commit()

        logger.info(f"Target notifications updated for {target.url}: {len(request.recipient_ids)} recipients assigned by {current_api_key.get('label')}")

        return {
            "status": "success",
            "message": f"Updated notifications for {target.url}",
            "target_id": target_id,
            "recipients_assigned": len(request.recipient_ids),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating target notifications: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update target notifications",
        )


@router.delete(
    "/{target_id}/notifications/{recipient_id}",
    summary="Remove Notification Recipient from Target",
    description="Remove a specific Pushover recipient assignment from this target.",
)
async def remove_target_notification(
    target_id: int,
    recipient_id: int,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Remove a notification recipient assignment from a target."""
    # Check role
    role = current_api_key.get("role", "").lower()
    if role not in ["operator", "admin"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires operator role or higher",
        )

    try:
        check_api_key_scope(current_api_key, "checks", "write", target_id)
        from models.target_notification import TargetNotification

        # Find and delete the assignment
        result = await db.execute(
            select(TargetNotification)
            .where(
                TargetNotification.target_id == target_id,
                TargetNotification.recipient_id == recipient_id,
            )
        )
        assignment = result.scalar_one_or_none()

        if not assignment:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Notification assignment not found",
            )

        await db.delete(assignment)

        await db.commit()

        logger.info(f"Removed notification recipient {recipient_id} from target {target_id} by {current_api_key.get('label')}")

        return {"status": "success", "message": "Recipient removed from target"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error removing target notification: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to remove target notification",
        )
