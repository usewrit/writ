"""
Unified runs feed router.

Read-only aggregation over EXISTING models — no new tables. Merges run
history into one normalized feed:

- workflow executions    -> AutomationTask          (run_type="workflow")
- content-check runs     -> DetectedChange + Target (run_type="check")
- automation/trigger runs-> TriggerExecution + TriggerRule (run_type="automation")

Each source is queried separately (capped) and merged +
sorted in Python — no giant UNION SQL.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from services import extracted_data_table as edt
from services.run_events import RUN_EVENTS_CHANNEL
from utils.redis_client import get_redis
from models.automation_task import AutomationTask
from models.automation_workflow import AutomationWorkflow
from models.detected_change import DetectedChange
from models.target import Target
from models.trigger_rule import TriggerRule, TriggerExecution
from security.api_key import get_current_api_key
from security.dependencies import check_api_key_scope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/runs", tags=["Runs"])

# Cap each per-model sub-query so a deep offset can't pull unbounded rows.
SUBQUERY_CAP = 500

RUN_TYPES = ("workflow", "check", "automation", "crawl")
NORMALIZED_STATUSES = ("pending", "running", "success", "failed", "cancelled", "skipped")

# Raw model status -> normalized feed status. "queued" (waiting for a free agent)
# normalizes to "pending" so it surfaces in the feed AND is cancellable (the
# cancel endpoint accepts queued; the feed/isCancellable use the pending vocab).
TASK_STATUS_MAP = {
    "queued": "pending", "pending": "pending", "assigned": "pending", "running": "running",
    "success": "success", "completed": "success",
    "failed": "failed", "timeout": "failed", "cancelled": "cancelled",
}
SESSION_STATUS_MAP = {
    "pending": "pending", "running": "running", "completed": "success",
    "failed": "failed", "cancelled": "cancelled",
}
EXEC_STATUS_MAP = {
    "pending": "pending", "running": "running", "completed": "success",
    "failed": "failed", "skipped": "skipped",
}

# Normalized feed status -> raw model statuses (for SQL-level filtering).
# An empty list means the source has no rows with that status (skip source).
TASK_RAW_STATUSES = {
    "pending": ["pending", "assigned", "queued"], "running": ["running"],
    "success": ["success", "completed"], "failed": ["failed", "timeout"],
    "cancelled": ["cancelled"], "skipped": [],
}
SESSION_RAW_STATUSES = {
    "pending": ["pending"], "running": ["running"], "success": ["completed"],
    "failed": ["failed"], "cancelled": ["cancelled"], "skipped": [],
}
EXEC_RAW_STATUSES = {
    "pending": ["pending"], "running": ["running"], "success": ["completed"],
    "failed": ["failed"], "cancelled": [], "skipped": ["skipped"],
}
# Dragnet crawl (CrawlJob) status → normalized. queued=pending; mapping/crawling/
# stopping=running; completed=success. A crawl is ONE run (its shards are hidden).
CRAWL_STATUS_MAP = {
    "queued": "pending", "mapping": "running", "crawling": "running",
    "stopping": "running", "completed": "success", "failed": "failed",
    "cancelled": "cancelled",
}
CRAWL_RAW_STATUSES = {
    "pending": ["queued"], "running": ["mapping", "crawling", "stopping"],
    "success": ["completed"], "failed": ["failed"], "cancelled": ["cancelled"],
    "skipped": [],
}


# ============================================
# Response models
# ============================================

class RunItem(BaseModel):
    """One normalized run in the unified feed."""
    id: str  # composite, unique across run types: "<run_type>-<row id>"
    run_type: str  # workflow | check | automation
    entity_id: Optional[int]
    entity_name: Optional[str]
    status: str  # pending | running | success | failed | cancelled | skipped
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    duration_ms: Optional[int]
    trigger_source: Optional[str]
    error: Optional[str]
    credits_used: Optional[int]
    # Real-money price charged for THIS run (micro-USD), when one applies — e.g.
    # an installed per-success run. None for ordinary own runs.
    cost_micros: Optional[int] = None
    detail_url_hint: Optional[str]
    # Direct link to this run's extracted-data view, when one exists (non-streaming
    # workflow runs). None when the run type produces no tabular data.
    data_url_hint: Optional[str] = None


class WindowCounts(BaseModel):
    """Run counts for one time window."""
    total: int
    by_status: Dict[str, int]
    by_run_type: Dict[str, int]


class FailingEntity(BaseModel):
    run_type: str
    entity_id: Optional[int]
    entity_name: Optional[str]
    failure_count: int
    detail_url_hint: Optional[str]


class RunsSummary(BaseModel):
    last_24h: WindowCounts
    last_7d: WindowCounts
    top_failing: List[FailingEntity]


# ============================================
# Helpers
# ============================================

def _duration_ms(started_at, completed_at) -> Optional[int]:
    if started_at and completed_at:
        return int((completed_at - started_at).total_seconds() * 1000)
    return None


def _sort_key(item: RunItem):
    return item.started_at or datetime.min.replace(tzinfo=timezone.utc)


async def _fetch_workflow_runs(db, status_filter, since, until,
                               entity_id, fetch_n) -> List[RunItem]:
    """Workflow executions (AutomationTask) -> run_type='workflow'."""
    sort_ts = func.coalesce(AutomationTask.started_at, AutomationTask.created_at)
    query = (
        select(
            AutomationTask.id,
            AutomationTask.workflow_id,
            AutomationTask.status,
            AutomationTask.trigger_type,
            AutomationTask.started_at,
            AutomationTask.completed_at,
            AutomationTask.created_at,
            AutomationTask.error_message,
            AutomationTask.trigger_context,
            # Needed to gate the "View data" link on runs that actually produced
            # >=1 extracted-data row (hide empty runs, mirroring the desktop feed).
            AutomationTask.result_data,
            AutomationWorkflow.name.label("workflow_name"),
            AutomationWorkflow.workflow_type.label("workflow_type"),
            Target.url.label("target_url"),
        )
        .select_from(AutomationTask)
        .outerjoin(AutomationWorkflow, AutomationTask.workflow_id == AutomationWorkflow.id)
        .outerjoin(Target, AutomationTask.target_id == Target.id)
        # Defensive orphan filter: hide runs whose workflow was removed/uninstalled
        # (workflow_id set but no live workflow row). Keep AI-navigation/session
        # tasks that legitimately have no workflow (workflow_id IS NULL). The FK is
        # ON DELETE CASCADE today so deletes remove their tasks, but this guards
        # against orphans from older SET-NULL schemas or any path cascade missed.
        .where(or_(AutomationTask.workflow_id.is_(None), AutomationWorkflow.id.isnot(None)))
        # Hide Dragnet crawl-SHARD tasks (trigger_type='crawl'). A crawl fans out into
        # many shard AutomationTasks but surfaces as ONE row via _fetch_crawl_runs;
        # without this the feed (and live activity) shows one line per shard.
        .where(or_(AutomationTask.trigger_type.is_(None),
                   AutomationTask.trigger_type != "crawl"))
    )
    if status_filter:
        raw = TASK_RAW_STATUSES[status_filter]
        if not raw:
            return []
        query = query.where(AutomationTask.status.in_(raw))
    if since:
        query = query.where(sort_ts >= since)
    if until:
        query = query.where(sort_ts <= until)
    if entity_id is not None:
        query = query.where(AutomationTask.workflow_id == entity_id)
    query = query.order_by(sort_ts.desc()).limit(fetch_n)

    rows = (await db.execute(query)).all()
    items = []
    for r in rows:
        if r.workflow_id:
            detail_url_hint = f"/workflows/{r.workflow_id}"
        else:
            detail_url_hint = None
        # Streaming workflows are live sessions with no tabular data view; every
        # other workflow has a Data tab on its detail page — but only link it when
        # the run actually produced >=1 extracted-data row, so EMPTY runs get a
        # detail link but no "View data" affordance (matches the desktop feed).
        has_data = r.workflow_type != "streaming" and edt.record_count(r.result_data) > 0
        data_url_hint = (
            f"{detail_url_hint}?tab=data"
            if detail_url_hint and has_data
            else None
        )
        items.append(RunItem(
            id=f"workflow-{r.id}",
            run_type="workflow",
            entity_id=r.workflow_id,
            entity_name=r.workflow_name or r.target_url or f"Task {r.id}",
            status=TASK_STATUS_MAP.get(r.status, r.status),
            started_at=r.started_at or r.created_at,
            finished_at=r.completed_at,
            duration_ms=_duration_ms(r.started_at, r.completed_at),
            trigger_source=r.trigger_type,
            error=r.error_message,
            credits_used=None,
            detail_url_hint=detail_url_hint,
            data_url_hint=data_url_hint,
        ))
    return items


async def _fetch_check_runs(db, status_filter, since, until,
                            entity_id, fetch_n) -> List[RunItem]:
    """Content-check runs (DetectedChange) -> run_type='check'. Always 'success'."""
    if status_filter and status_filter != "success":
        return []
    query = (
        select(
            DetectedChange.id,
            DetectedChange.target_id,
            DetectedChange.first_detected_at,
            Target.url.label("target_url"),
        )
        .select_from(DetectedChange)
        .join(Target, DetectedChange.target_id == Target.id)
    )
    if since:
        query = query.where(DetectedChange.first_detected_at >= since)
    if until:
        query = query.where(DetectedChange.first_detected_at <= until)
    if entity_id is not None:
        query = query.where(DetectedChange.target_id == entity_id)
    query = query.order_by(DetectedChange.first_detected_at.desc()).limit(fetch_n)

    rows = (await db.execute(query)).all()
    return [
        RunItem(
            id=f"check-{r.id}",
            run_type="check",
            entity_id=r.target_id,
            entity_name=r.target_url,
            status="success",
            started_at=r.first_detected_at,
            finished_at=r.first_detected_at,
            duration_ms=None,
            trigger_source="monitor",
            error=None,
            credits_used=None,
            detail_url_hint=f"/checks/{r.target_id}",
        )
        for r in rows
    ]


async def _fetch_automation_runs(db, status_filter, since, until,
                                 entity_id, fetch_n) -> List[RunItem]:
    """Trigger-rule executions (TriggerExecution) -> run_type='automation'."""
    query = (
        select(
            TriggerExecution.id,
            TriggerExecution.trigger_rule_id,
            TriggerExecution.status,
            TriggerExecution.triggered_at,
            TriggerExecution.completed_at,
            TriggerExecution.error_message,
            TriggerRule.name.label("rule_name"),
            TriggerRule.event_type,
        )
        .select_from(TriggerExecution)
        .join(TriggerRule, TriggerExecution.trigger_rule_id == TriggerRule.id)
    )
    if status_filter:
        raw = EXEC_RAW_STATUSES[status_filter]
        if not raw:
            return []
        query = query.where(TriggerExecution.status.in_(raw))
    if since:
        query = query.where(TriggerExecution.triggered_at >= since)
    if until:
        query = query.where(TriggerExecution.triggered_at <= until)
    if entity_id is not None:
        query = query.where(TriggerExecution.trigger_rule_id == entity_id)
    query = query.order_by(TriggerExecution.triggered_at.desc()).limit(fetch_n)

    rows = (await db.execute(query)).all()
    return [
        RunItem(
            id=f"automation-{r.id}",
            run_type="automation",
            entity_id=r.trigger_rule_id,
            entity_name=r.rule_name,
            status=EXEC_STATUS_MAP.get(r.status, r.status),
            started_at=r.triggered_at,
            finished_at=r.completed_at,
            duration_ms=_duration_ms(r.triggered_at, r.completed_at),
            trigger_source=r.event_type,
            error=r.error_message,
            credits_used=None,
            detail_url_hint=f"/automations/{r.trigger_rule_id}/runs",
        )
        for r in rows
    ]


async def _fetch_crawl_runs(db, status_filter, since, until,
                            entity_id, fetch_n) -> List[RunItem]:
    """Dragnet crawls (CrawlJob) -> run_type='crawl'. ONE row per crawl.

    A crawl fans out into many shard AutomationTasks (hidden from the workflow feed);
    here it surfaces as a single run whose data link points at its synthetic workflow
    — where ALL shard pages aggregate into one dataset. So a crawl is one run + one
    dataset in the UI, not one-per-shard."""
    from models.crawl_job import CrawlJob
    sort_ts = func.coalesce(CrawlJob.started_at, CrawlJob.created_at)
    query = select(CrawlJob)
    if status_filter:
        raw = CRAWL_RAW_STATUSES.get(status_filter, [])
        if not raw:
            return []
        query = query.where(CrawlJob.status.in_(raw))
    if since:
        query = query.where(sort_ts >= since)
    if until:
        query = query.where(sort_ts <= until)
    if entity_id is not None:
        # Scope by the crawl's synthetic workflow id (its aggregated dataset).
        query = query.where(CrawlJob.workflow_id == entity_id)
    query = query.order_by(sort_ts.desc()).limit(fetch_n)

    rows = (await db.execute(query)).scalars().all()
    items = []
    for c in rows:
        data_url = (
            f"/workflows/{c.workflow_id}?tab=data"
            if (c.workflow_id and (c.pages_done or 0) > 0)
            else None
        )
        err = f"{c.pages_failed} page(s) failed" if c.status == "failed" else None
        items.append(RunItem(
            id=f"crawl-{c.id}",
            run_type="crawl",
            entity_id=c.workflow_id,
            entity_name=c.name or c.seed_url,
            status=CRAWL_STATUS_MAP.get(c.status, c.status),
            started_at=c.started_at or c.created_at,
            finished_at=c.completed_at,
            duration_ms=_duration_ms(c.started_at, c.completed_at),
            trigger_source="crawl",
            error=err,
            credits_used=None,
            detail_url_hint=f"/crawls/{c.id}",
            data_url_hint=data_url,
        ))
    return items


_FETCHERS = {
    "workflow": _fetch_workflow_runs,
    "check": _fetch_check_runs,
    "automation": _fetch_automation_runs,
    "crawl": _fetch_crawl_runs,
}


# ============================================
# Endpoints
# ============================================

@router.get("", response_model=List[RunItem])
async def list_runs(
    run_type: Optional[str] = Query(None, description="Filter: workflow | check | automation"),
    status_filter: Optional[str] = Query(
        None, alias="status",
        description="Normalized status: pending | running | success | failed | cancelled | skipped",
    ),
    since: Optional[datetime] = Query(None, description="Only runs started at/after this time (ISO 8601)"),
    until: Optional[datetime] = Query(None, description="Only runs started at/before this time (ISO 8601)"),
    entity_id: Optional[int] = Query(
        None,
        description="Filter by entity: workflow_id (workflow), target_id (check), "
                    "trigger_rule_id (automation)",
    ),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of runs"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    db: AsyncSession = Depends(get_db),
    _api_key: dict = Depends(get_current_api_key),
):
    """Unified runs feed, merged across all run sources, started_at desc."""
    # API-key scope: bind to the filtered entity when possible; otherwise require
    # read on at least one run-bearing resource so a zero-scope key can't read the
    # whole feed. JWT/OAuth (no scopes key) pass through (audit #34).
    if isinstance(_api_key, dict) and _api_key.get("scopes") is not None:
        _res = {"workflow": "workflows", "check": "checks", "automation": "automations"}.get(run_type)
        if _res:
            check_api_key_scope(_api_key, _res, "read", entity_id)
        else:
            _sc = _api_key.get("scopes") or {}
            if not any((_sc.get(r) or {}).get("permissions") for r in ("workflows", "checks", "automations")):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                    detail="API key lacks read scope for runs")
    if run_type is not None and run_type not in RUN_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid run_type. Must be one of: {', '.join(RUN_TYPES)}",
        )
    if status_filter is not None and status_filter not in NORMALIZED_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status. Must be one of: {', '.join(NORMALIZED_STATUSES)}",
        )

    # Each source only needs enough rows to fill the requested page.
    fetch_n = min(limit + offset, SUBQUERY_CAP)

    items: List[RunItem] = []
    for rt, fetcher in _FETCHERS.items():
        if run_type and rt != run_type:
            continue
        items.extend(
            await fetcher(db, status_filter, since, until, entity_id, fetch_n)
        )

    items.sort(key=_sort_key, reverse=True)

    # Monitor content-checks are noise in the unified feed — a detected change is
    # recorded as a 'success', and successful checks would swamp real executions.
    # In the default (unscoped) feed, surface only FAILED checks; an explicit
    # run_type='check' query (e.g. a monitor's own change history) is unaffected.
    if run_type is None:
        items = [it for it in items if it.run_type != "check" or it.status == "failed"]

    return items[offset:offset + limit]


async def _tenant_from_bearer(request: Request) -> UUID:
    """Resolve the caller's tenant from the JWT bearer token WITHOUT holding a DB
    session for the life of the (long-lived) SSE stream. The dashboard's JWT
    carries the tenant in its ``org_id`` claim; decoding it (signature + Redis
    blacklist) is enough to scope the stream, and avoids pinning a DB connection.
    """
    authz = request.headers.get("Authorization", "")
    if not authz.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Missing bearer token")
    token = authz[7:].strip()
    from security.jwt import decode_access_token_with_blacklist
    payload = await decode_access_token_with_blacklist(token)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid or expired token")
    org_id = payload.get("org_id")
    if not org_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Tenant context required")
    return UUID(str(org_id))


@router.get("/events")
async def runs_events(request: Request):
    """Server-Sent Events stream of run-state changes for this coordinator.

    Emits one compact JSON delta whenever a run starts or ends so the dashboard
    reacts instantly rather than on the next poll. The client treats any data
    frame as a nudge to refetch ``/runs`` and also polls while work is live, so
    a missed event (e.g. a cross-worker publish) is non-fatal.
    """
    await _tenant_from_bearer(request)  # auth only — single-owner instance
    channel = RUN_EVENTS_CHANNEL

    async def event_stream():
        pubsub = get_redis().pubsub()
        await pubsub.subscribe(channel)
        try:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=20.0
                )
                if message and message.get("type") == "message":
                    data = message["data"]
                    if isinstance(data, (bytes, bytearray)):
                        data = data.decode("utf-8", "ignore")
                    yield f"data: {data}\n\n"
                else:
                    yield ": ping\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.close()
            except Exception:
                pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/summary", response_model=RunsSummary)
async def runs_summary(
    db: AsyncSession = Depends(get_db),
    _api_key: dict = Depends(get_current_api_key),
):
    """Run counts by status (last 24h + last 7d) and top 5 failing entities (last 7d)."""
    now = datetime.utcnow()
    cutoff_24h = now - timedelta(hours=24)
    cutoff_7d = now - timedelta(days=7)

    async def window_counts(cutoff) -> WindowCounts:
        by_status = {s: 0 for s in NORMALIZED_STATUSES}
        by_run_type = {rt: 0 for rt in RUN_TYPES}

        # Workflow executions
        rows = (await db.execute(
            select(AutomationTask.status, func.count())
            .where(AutomationTask.created_at >= cutoff)
            .group_by(AutomationTask.status)
        )).all()
        for raw_status, count in rows:
            by_status[TASK_STATUS_MAP.get(raw_status, "failed")] += count
            by_run_type["workflow"] += count

        # Content-check runs (detected changes)
        change_count = (await db.execute(
            select(func.count())
            .select_from(DetectedChange)
            .join(Target, DetectedChange.target_id == Target.id)
            .where(DetectedChange.first_detected_at >= cutoff)
        )).scalar() or 0
        by_status["success"] += change_count
        by_run_type["check"] += change_count

        # Trigger executions
        rows = (await db.execute(
            select(TriggerExecution.status, func.count())
            .select_from(TriggerExecution)
            .join(TriggerRule, TriggerExecution.trigger_rule_id == TriggerRule.id)
            .where(TriggerExecution.triggered_at >= cutoff)
            .group_by(TriggerExecution.status)
        )).all()
        for raw_status, count in rows:
            by_status[EXEC_STATUS_MAP.get(raw_status, "failed")] += count
            by_run_type["automation"] += count

        return WindowCounts(
            total=sum(by_status.values()),
            by_status=by_status,
            by_run_type=by_run_type,
        )

    last_24h = await window_counts(cutoff_24h)
    last_7d = await window_counts(cutoff_7d)

    # ── Top failing entities (last 7d) ───────────────────────────────
    failing: List[FailingEntity] = []

    # Failing workflows (failed/timeout tasks grouped by workflow)
    rows = (await db.execute(
        select(AutomationTask.workflow_id, AutomationWorkflow.name,
               func.count().label("failures"))
        .select_from(AutomationTask)
        .join(AutomationWorkflow, AutomationTask.workflow_id == AutomationWorkflow.id)
        .where(AutomationTask.created_at >= cutoff_7d,
               AutomationTask.status.in_(["failed", "timeout"]))
        .group_by(AutomationTask.workflow_id, AutomationWorkflow.name)
        .order_by(func.count().desc())
        .limit(5)
    )).all()
    failing.extend(
        FailingEntity(
            run_type="workflow", entity_id=wf_id, entity_name=name,
            failure_count=failures, detail_url_hint=f"/workflows/{wf_id}",
        )
        for wf_id, name, failures in rows
    )

    # Failing trigger rules
    rows = (await db.execute(
        select(TriggerExecution.trigger_rule_id, TriggerRule.name,
               func.count().label("failures"))
        .select_from(TriggerExecution)
        .join(TriggerRule, TriggerExecution.trigger_rule_id == TriggerRule.id)
        .where(TriggerExecution.triggered_at >= cutoff_7d,
               TriggerExecution.status == "failed")
        .group_by(TriggerExecution.trigger_rule_id, TriggerRule.name)
        .order_by(func.count().desc())
        .limit(5)
    )).all()
    failing.extend(
        FailingEntity(
            run_type="automation", entity_id=rule_id, entity_name=name,
            failure_count=failures, detail_url_hint=f"/automations/{rule_id}/runs",
        )
        for rule_id, name, failures in rows
    )

    failing.sort(key=lambda f: f.failure_count, reverse=True)

    return RunsSummary(last_24h=last_24h, last_7d=last_7d, top_failing=failing[:5])
