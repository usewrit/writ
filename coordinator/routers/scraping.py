"""
Scraping router - Distributed web scraping work type.

Handles:
- CRUD for ScrapeJobs (what to scrape and when)
- Task dispatch to agents (worker-templates, desktop, recorder)
- Result collection and storage
- ShiftSync integration (popular-times query endpoint)
"""
import logging
import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import httpx
from utils.http_client import http_session
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.scrape_job import ScrapeJob
from models.scrape_result import ScrapeResult
from models.automation_task import AutomationTask
from security.api_key import get_current_api_key
from security.infra_redaction import redact_infra

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scraping", tags=["Scraping"])


# ============================================================================
# Pydantic Schemas
# ============================================================================

class ScrapeJobCreate(BaseModel):
    """Request to create a new scrape job."""
    name: str = Field(..., max_length=200)
    description: Optional[str] = None
    scrape_type: str = Field(default="popular_times")
    target_config: dict = Field(..., description="Target configuration (varies by scrape_type)")
    schedule_enabled: bool = Field(default=True)
    schedule_interval_ms: int = Field(default=3600000, gt=60000)
    preferred_tier: str = Field(default="auto")
    anti_detection_config: Optional[dict] = None
    rate_limit_group: str = Field(default="google_maps")
    max_concurrent_per_group: int = Field(default=2, ge=1, le=10)


class ScrapeJobUpdate(BaseModel):
    """Request to update a scrape job."""
    name: Optional[str] = Field(None, max_length=200)
    description: Optional[str] = None
    target_config: Optional[dict] = None
    schedule_enabled: Optional[bool] = None
    schedule_interval_ms: Optional[int] = Field(None, gt=60000)
    preferred_tier: Optional[str] = None
    anti_detection_config: Optional[dict] = None
    rate_limit_group: Optional[str] = None
    max_concurrent_per_group: Optional[int] = Field(None, ge=1, le=10)
    is_active: Optional[bool] = None


class ScrapeJobResponse(BaseModel):
    id: int
    name: str
    description: Optional[str]
    scrape_type: str
    target_config: dict
    schedule_enabled: bool
    schedule_interval_ms: int
    preferred_tier: str
    anti_detection_config: Optional[dict]
    rate_limit_group: str
    max_concurrent_per_group: int
    last_result: Optional[dict]
    last_result_at: Optional[str]
    last_error: Optional[str]
    is_active: bool
    run_count: int
    success_count: int
    failure_count: int
    success_rate: float
    effective_tier: str
    created_at: str
    updated_at: Optional[str]

    class Config:
        from_attributes = True


class ScrapeResultResponse(BaseModel):
    id: int
    scrape_job_id: int
    data: dict
    executor_agent_id: Optional[str]
    executor_tier: Optional[str]
    duration_ms: Optional[int]
    success: bool
    error_message: Optional[str]
    scraped_at: str

    class Config:
        from_attributes = True


class TaskClaimResponse(BaseModel):
    id: int
    scrape_job_id: int
    scrape_type: str
    target_config: dict
    anti_detection_config: dict
    trigger_context: Optional[dict]


class TaskCompleteRequest(BaseModel):
    success: bool
    data: Optional[dict] = None
    error: Optional[str] = None
    duration_ms: Optional[int] = None
    executor_tier: Optional[str] = None


class PopularTimesQueryRequest(BaseModel):
    """Ad-hoc query for ShiftSync integration."""
    places: List[dict] = Field(
        ...,
        description="[{query: 'Starbucks Montreal', lat: 45.5, lng: -73.6}]"
    )
    use_cache_minutes: int = Field(default=30, ge=1, le=1440)


class PopularTimesQueryResponse(BaseModel):
    request_id: str
    status: str  # "cached" | "pending" | "partial"
    results: Optional[List[dict]] = None


# ============================================================================
# ScrapeJob CRUD
# ============================================================================

@router.get("/jobs", response_model=List[ScrapeJobResponse])
async def list_scrape_jobs(
    scrape_type: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _api_key: str = Depends(get_current_api_key),
):
    """List scrape jobs."""
    query = select(ScrapeJob)

    if scrape_type:
        query = query.where(ScrapeJob.scrape_type == scrape_type)
    if is_active is not None:
        query = query.where(ScrapeJob.is_active == is_active)

    query = query.order_by(ScrapeJob.created_at.desc()).limit(limit)
    result = await db.execute(query)
    jobs = result.scalars().all()

    return [_job_to_response(j) for j in jobs]


@router.post("/jobs", response_model=ScrapeJobResponse, status_code=201)
async def create_scrape_job(
    data: ScrapeJobCreate,
    db: AsyncSession = Depends(get_db),
    _api_key: str = Depends(get_current_api_key),
):
    """Create a new scrape job."""
    valid_types = {"popular_times", "custom_extract"}
    if data.scrape_type not in valid_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid scrape_type: {data.scrape_type}. Valid: {', '.join(valid_types)}",
        )

    valid_tiers = {"auto", "worker", "desktop", "recorder"}
    if data.preferred_tier not in valid_tiers:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid preferred_tier: {data.preferred_tier}. Valid: {', '.join(valid_tiers)}",
        )

    # Validate and build target_config based on scrape_type
    if data.scrape_type == "popular_times":
        tc = data.target_config
        place_name = tc.get("place_name", "").strip()
        address = tc.get("address", "").strip()
        city = tc.get("city", "").strip()

        if not place_name and not tc.get("place_query"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="target_config.place_name is required for popular_times scrape type",
            )

        # Build place_query in LivePopularTimes format:
        # "(Place Name) Address, City"
        # e.g. "(Costco) 605 Expo Blvd, Vancouver, BC V6B 1V4, Canada"
        if not tc.get("place_query") and place_name:
            query_parts = [f"({place_name})"]
            if address:
                query_parts.append(address + ",")
            if city:
                query_parts.append(city)
            data.target_config["place_query"] = " ".join(query_parts)

    # Single-user coordinator: no plan gate (scraping always allowed).

    now = datetime.now(timezone.utc)
    job = ScrapeJob(
        name=data.name,
        description=data.description,
        scrape_type=data.scrape_type,
        target_config=data.target_config,
        schedule_enabled=data.schedule_enabled,
        schedule_interval_ms=data.schedule_interval_ms,
        preferred_tier=data.preferred_tier,
        anti_detection_config=data.anti_detection_config or {},
        rate_limit_group=data.rate_limit_group,
        max_concurrent_per_group=data.max_concurrent_per_group,
        next_scheduled_at=now + timedelta(milliseconds=data.schedule_interval_ms) if data.schedule_enabled else None,
    )
    db.add(job)
    await db.flush()
    await db.refresh(job)

    logger.info(f"Created ScrapeJob {job.id}: {job.name} (type={job.scrape_type})")
    return _job_to_response(job)


@router.get("/jobs/{job_id}", response_model=ScrapeJobResponse)
async def get_scrape_job(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    _api_key: str = Depends(get_current_api_key),
):
    """Get scrape job details."""
    job = await _get_job_or_404(job_id, db)
    return _job_to_response(job)


@router.patch("/jobs/{job_id}", response_model=ScrapeJobResponse)
async def update_scrape_job(
    job_id: int,
    data: ScrapeJobUpdate,
    db: AsyncSession = Depends(get_db),
    _api_key: str = Depends(get_current_api_key),
):
    """Update a scrape job."""
    job = await _get_job_or_404(job_id, db)

    update_data = data.model_dump(exclude_unset=True)

    if "preferred_tier" in update_data:
        valid_tiers = {"auto", "worker", "desktop", "recorder"}
        if update_data["preferred_tier"] not in valid_tiers:
            raise HTTPException(status_code=400, detail=f"Invalid preferred_tier")

    for field, value in update_data.items():
        setattr(job, field, value)

    # Recalculate next_scheduled_at if schedule changed
    if "schedule_enabled" in update_data or "schedule_interval_ms" in update_data:
        if job.schedule_enabled:
            job.next_scheduled_at = datetime.now(timezone.utc) + timedelta(
                milliseconds=job.schedule_interval_ms
            )
        else:
            job.next_scheduled_at = None

    job.updated_at = datetime.now(timezone.utc)
    await db.commit()

    logger.info(f"Updated ScrapeJob {job_id}")
    return _job_to_response(job)


@router.delete("/jobs/{job_id}", status_code=204)
async def delete_scrape_job(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    _api_key: str = Depends(get_current_api_key),
):
    """Delete a scrape job and all its results."""
    job = await _get_job_or_404(job_id, db)
    await db.delete(job)
    await db.commit()
    logger.info(f"Deleted ScrapeJob {job_id}: {job.name}")


# ============================================================================
# Execution
# ============================================================================

@router.post("/jobs/{job_id}/run")
async def run_scrape_job(
    job_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    _api_key: str = Depends(get_current_api_key),
):
    """
    Trigger an immediate scrape run.

    Dispatches to the Recorder service (Patchright with persistent profile
    and stealth anti-detection) — the same pipeline used for AI sessions.
    The Recorder navigates Google Maps in a real Chrome browser and extracts
    popular times data from the rendered DOM.
    """
    job = await _get_job_or_404(job_id, db)

    if not job.is_active:
        raise HTTPException(status_code=400, detail="Job is not active")

    # Single-user coordinator: no plan gate (running scrape jobs always allowed).

    # Check rate limit group concurrency
    running_count = await _get_group_concurrency(job.rate_limit_group, db)
    if running_count >= job.max_concurrent_per_group:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit group '{job.rate_limit_group}' concurrency limit reached "
                   f"({running_count}/{job.max_concurrent_per_group})"
        )

    # Create AutomationTask to track execution
    task = AutomationTask(
        target_id=None,
        workflow_id=None,
        scrape_job_id=job.id,
        trigger_type="scraping",
        status="running",
        started_at=datetime.now(timezone.utc),
        max_attempts=3,
        trigger_context={
            "scrape_type": job.scrape_type,
            "target_config": job.target_config,
            "anti_detection_config": job.anti_detection_config or {},
        },
    )
    db.add(task)
    await db.flush()
    task_id = task.id
    await db.commit()

    # Dispatch to Recorder in background
    background_tasks.add_task(
        _execute_scrape_on_recorder, task_id, job.id, job.target_config
    )

    logger.info(f"Dispatched scraping task {task_id} to Recorder for job {job_id}")
    return {"task_id": task_id, "status": "running"}


async def _execute_scrape_on_recorder(task_id: int, job_id: int, target_config: dict):
    """
    Background task: send scraping job to the Recorder service.

    The Recorder uses Patchright + persistent Chrome profile to load
    Google Maps and extract popular times from the rendered DOM.
    """
    from database import async_session_maker
    from models.config import Config

    # Get recorder URL
    recorder_url = os.getenv("RECORDER_URL", "http://localhost:8081")
    try:
        async with async_session_maker() as db:
            result = await db.execute(
                select(Config).where(Config.key == "recorder_url")
            )
            cfg = result.scalar_one_or_none()
            if cfg and cfg.value and cfg.value.get("value"):
                recorder_url = cfg.value["value"].rstrip("/")
    except Exception as e:
        logger.warning(f"Failed to get recorder URL from DB: {e}")

    place_query = target_config.get("place_query", "")
    import urllib.parse
    maps_url = f"https://www.google.com/maps/search/{urllib.parse.quote(place_query)}"

    logger.info(f"Scrape task {task_id}: sending to Recorder at {recorder_url} (query={place_query[:60]})")

    try:
        async with http_session() as client:
            # Call Recorder's dedicated scrape endpoint (no AI, just navigate + extract)
            response = await client.post(
                f"{recorder_url}/scrape/popular-times",
                json={
                    "task_id": task_id,
                    "place_query": place_query,
                },
                timeout=120.0,
            )

            result_data = response.json()
            logger.info(f"Scrape task {task_id}: Recorder returned status {response.status_code}")

    except Exception as e:
        # Log full detail (recorder host + exception) server-side only; do not
        # leak the internal recorder address to the caller via task.error_message.
        logger.error(f"Scrape task {task_id}: Recorder call failed (url={recorder_url}): {e}", exc_info=True)
        result_data = {"success": False, "error": "Execution agent temporarily unavailable. Please retry."}

    # Update task and job with results
    try:
        async with async_session_maker() as db:
            task_result = await db.execute(
                select(AutomationTask).where(AutomationTask.id == task_id)
            )
            task = task_result.scalar_one_or_none()
            job_result = await db.execute(
                select(ScrapeJob).where(ScrapeJob.id == job_id)
            )
            job = job_result.scalar_one_or_none()

            if not task:
                return

            now = datetime.now(timezone.utc)
            success = result_data.get("success", False)
            scraped_data = result_data.get("result_data") or result_data.get("data") or {}

            task.completed_at = now
            task.status = "success" if success else "failed"
            task.success = success
            task.result_data = scraped_data
            task.error_message = result_data.get("error") or result_data.get("message")

            if job:
                job.run_count += 1
                if success and scraped_data:
                    job.success_count += 1
                    job.last_result = scraped_data
                    job.last_result_at = now
                    job.last_error = None
                else:
                    job.failure_count += 1
                    job.last_error = task.error_message

                # Store result
                scrape_result = ScrapeResult(
                    scrape_job_id=job.id,
                    task_id=task.id,
                    data=scraped_data or {},
                    executor_tier="recorder",
                    success=success,
                    error_message=task.error_message,
                    scraped_at=now,
                )
                db.add(scrape_result)

            await db.commit()
            logger.info(f"Scrape task {task_id}: completed (success={success})")

    except Exception as e:
        logger.error(f"Scrape task {task_id}: failed to update DB: {e}")


@router.get("/jobs/{job_id}/results", response_model=List[ScrapeResultResponse])
async def get_scrape_results(
    job_id: int,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _api_key: str = Depends(get_current_api_key),
):
    """Get historical scraping results for a job."""
    await _get_job_or_404(job_id, db)

    result = await db.execute(
        select(ScrapeResult)
        .where(ScrapeResult.scrape_job_id == job_id)
        .order_by(ScrapeResult.scraped_at.desc())
        .offset(offset)
        .limit(limit)
    )
    results = result.scalars().all()
    return [_result_to_response(r) for r in results]


@router.get("/jobs/{job_id}/results/latest", response_model=Optional[ScrapeResultResponse])
async def get_latest_result(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    _api_key: str = Depends(get_current_api_key),
):
    """Get the most recent result for a scrape job."""
    await _get_job_or_404(job_id, db)

    result = await db.execute(
        select(ScrapeResult)
        .where(ScrapeResult.scrape_job_id == job_id)
        .where(ScrapeResult.success == True)
        .order_by(ScrapeResult.scraped_at.desc())
        .limit(1)
    )
    latest = result.scalar_one_or_none()
    if not latest:
        return None
    return _result_to_response(latest)


# ============================================================================
# Agent Task Interface
# ============================================================================

@router.get("/tasks/pending")
async def get_pending_scraping_task(
    agent_id: str = Query(...),
    tier: Optional[str] = Query(None, description="Filter by tier: worker, desktop"),
    db: AsyncSession = Depends(get_db),
    _api_key: str = Depends(get_current_api_key),
):
    """
    Agent claims next pending scraping task.

    Workers filter for tier=worker tasks, desktop agents for tier=desktop.
    If no tier filter, returns any pending scraping task.
    """
    query = (
        select(AutomationTask)
        .where(
            AutomationTask.status == "pending",
            AutomationTask.trigger_type == "scraping",
            AutomationTask.attempt_count < AutomationTask.max_attempts,
        )
        .order_by(AutomationTask.created_at)
    )

    # Filter by tier if specified
    if tier:
        # Filter on the trigger_context JSON "tier" key (portable json_extract)
        query = query.where(
            func.json_extract(AutomationTask.trigger_context, "$.tier") == tier
        )

    query = query.limit(1)
    result = await db.execute(query)
    task = result.scalar_one_or_none()

    if not task:
        return None

    # Mark as assigned
    task.status = "assigned"
    task.executor_agent_id = agent_id
    task.attempt_count += 1
    await db.commit()

    # Load scrape job for full context
    job = await db.get(ScrapeJob, task.scrape_job_id)

    logger.info(f"Assigned scraping task {task.id} to agent {agent_id} (job={job.name})")

    return TaskClaimResponse(
        id=task.id,
        scrape_job_id=job.id,
        scrape_type=job.scrape_type,
        target_config=job.target_config,
        anti_detection_config=job.anti_detection_config or {},
        trigger_context=task.trigger_context,
    )


@router.post("/tasks/{task_id}/start")
async def start_scraping_task(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    _api_key: str = Depends(get_current_api_key),
):
    """Agent marks a scraping task as running."""
    task = await _get_task_or_404(task_id, db)

    if task.status not in ("assigned", "pending"):
        raise HTTPException(status_code=400, detail=f"Cannot start task with status '{task.status}'")

    task.status = "running"
    task.started_at = datetime.now(timezone.utc)
    await db.commit()

    return {"status": "running", "task_id": task_id}


@router.post("/tasks/{task_id}/complete")
async def complete_scraping_task(
    task_id: int,
    result: TaskCompleteRequest,
    db: AsyncSession = Depends(get_db),
    _api_key: str = Depends(get_current_api_key),
):
    """
    Agent reports scraping task completion with results.

    On success, creates a ScrapeResult row and updates job's last_result.
    """
    task = await _get_task_or_404(task_id, db)

    if task.status not in ("running", "assigned"):
        raise HTTPException(status_code=400, detail=f"Cannot complete task with status '{task.status}'")

    now = datetime.now(timezone.utc)
    task.status = "success" if result.success else "failed"
    task.completed_at = now
    task.success = result.success
    task.result_data = result.data
    task.error_message = result.error

    # Load the parent scrape job
    job = await db.get(ScrapeJob, task.scrape_job_id)
    if not job:
        await db.commit()
        return {"status": task.status}

    # Update job counters
    job.run_count += 1

    if result.success and result.data:
        job.success_count += 1
        job.last_result = result.data
        job.last_result_at = now
        job.last_error = None

        # Reset worker failure counter on success
        if result.executor_tier and result.executor_tier.startswith("worker"):
            job.consecutive_worker_failures = 0

        # Create ScrapeResult
        scrape_result = ScrapeResult(
            scrape_job_id=job.id,
            task_id=task.id,
            data=result.data,
            executor_agent_id=task.executor_agent_id,
            executor_tier=result.executor_tier,
            duration_ms=result.duration_ms,
            success=True,
            scraped_at=now,
        )
        db.add(scrape_result)

        logger.info(
            f"Scrape task {task_id} completed successfully "
            f"(job={job.name}, tier={result.executor_tier}, "
            f"duration={result.duration_ms}ms)"
        )
    else:
        job.failure_count += 1
        job.last_error = result.error

        # Track worker failures for tier promotion
        if result.executor_tier and result.executor_tier.startswith("worker"):
            job.consecutive_worker_failures += 1

        # Store failed result for debugging
        scrape_result = ScrapeResult(
            scrape_job_id=job.id,
            task_id=task.id,
            data=result.data or {},
            executor_agent_id=task.executor_agent_id,
            executor_tier=result.executor_tier,
            duration_ms=result.duration_ms,
            success=False,
            error_message=result.error,
            scraped_at=now,
        )
        db.add(scrape_result)

        logger.warning(
            f"Scrape task {task_id} failed (job={job.name}, "
            f"tier={result.executor_tier}, error={result.error})"
        )

    await db.commit()
    return {"status": task.status, "scrape_result_id": scrape_result.id}


# ============================================================================
# ShiftSync Integration (Popular Times Query)
# ============================================================================

@router.post("/popular-times/query", response_model=PopularTimesQueryResponse)
async def query_popular_times(
    request: PopularTimesQueryRequest,
    db: AsyncSession = Depends(get_db),
    _api_key: str = Depends(get_current_api_key),
):
    """
    Ad-hoc popular times query for ShiftSync predictive engine.

    Checks cache first; if fresh results exist, returns immediately.
    Otherwise triggers scrape tasks and returns a request_id for polling.
    """
    cache_threshold = datetime.now(timezone.utc) - timedelta(minutes=request.use_cache_minutes)
    cached_results = []
    pending_places = []

    for place in request.places:
        query_str = place.get("query", "")
        if not query_str:
            continue

        # Check if a ScrapeJob exists with fresh results for this place
        result = await db.execute(
            select(ScrapeJob)
            .where(
                ScrapeJob.scrape_type == "popular_times",
                func.json_extract(ScrapeJob.target_config, "$.place_query") == query_str,
                ScrapeJob.is_active == True,
            )
            .limit(1)
        )
        existing_job = result.scalar_one_or_none()

        if existing_job and existing_job.last_result_at and existing_job.last_result_at > cache_threshold:
            # Fresh cached result available
            cached_results.append(existing_job.last_result)
        else:
            pending_places.append(place)

    # If all results are cached, return immediately
    if not pending_places:
        return PopularTimesQueryResponse(
            request_id="cached",
            status="cached",
            results=cached_results,
        )

    # Generate a request_id for tracking this batch
    request_id = str(uuid.uuid4())[:12]

    # Create or trigger scrape jobs for pending places
    for place in pending_places:
        query_str = place["query"]
        lat = place.get("lat")
        lng = place.get("lng")

        # Find or create ScrapeJob
        result = await db.execute(
            select(ScrapeJob)
            .where(
                ScrapeJob.scrape_type == "popular_times",
                func.json_extract(ScrapeJob.target_config, "$.place_query") == query_str,
            )
            .limit(1)
        )
        job = result.scalar_one_or_none()

        if not job:
            # Create a new job for this place
            target_config = {"place_query": query_str}
            if lat and lng:
                target_config["coordinates"] = {"lat": lat, "lng": lng}

            job = ScrapeJob(
                name=f"Popular Times: {query_str[:80]}",
                scrape_type="popular_times",
                target_config=target_config,
                schedule_enabled=True,
                schedule_interval_ms=3600000,  # 1 hour default
                next_scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
            db.add(job)
            await db.flush()

        # Create an AutomationTask to scrape now
        tier = job.effective_tier
        task = AutomationTask(
            scrape_job_id=job.id,
            trigger_type="scraping",
            status="pending",
            max_attempts=3,
            trigger_context={
                "scrape_type": "popular_times",
                "target_config": job.target_config,
                "tier": tier,
                "anti_detection_config": job.anti_detection_config or {},
                "request_id": request_id,
            },
        )
        db.add(task)

    await db.commit()

    # Return partial results (cached) + pending status
    if cached_results:
        return PopularTimesQueryResponse(
            request_id=request_id,
            status="partial",
            results=cached_results,
        )

    return PopularTimesQueryResponse(
        request_id=request_id,
        status="pending",
        results=None,
    )


@router.get("/popular-times/{request_id}", response_model=PopularTimesQueryResponse)
async def poll_popular_times(
    request_id: str,
    db: AsyncSession = Depends(get_db),
    _api_key: str = Depends(get_current_api_key),
):
    """
    Poll for popular times query results.

    Returns completed results or current status.
    """
    if request_id == "cached":
        raise HTTPException(status_code=400, detail="Cached results were already returned")

    # Find all tasks with this request_id in trigger_context
    result = await db.execute(
        select(AutomationTask)
        .where(
            AutomationTask.trigger_type == "scraping",
            func.json_extract(AutomationTask.trigger_context, "$.request_id") == request_id,
        )
    )
    tasks = result.scalars().all()

    if not tasks:
        raise HTTPException(status_code=404, detail=f"Request {request_id} not found")

    # Check if all tasks are done
    all_done = all(t.status in ("success", "failed", "cancelled") for t in tasks)
    results = []

    for task in tasks:
        if task.status == "success" and task.result_data:
            results.append(task.result_data)

    if all_done:
        return PopularTimesQueryResponse(
            request_id=request_id,
            status="completed",
            results=results if results else None,
        )

    # Still pending
    return PopularTimesQueryResponse(
        request_id=request_id,
        status="pending" if not results else "partial",
        results=results if results else None,
    )


# ============================================================================
# Health
# ============================================================================

@router.get("/health")
async def scraping_health(
    _api_key: str = Depends(get_current_api_key),
):
    """Health check for scraping service."""
    return {"status": "ok", "service": "scraping"}


# ============================================================================
# Helpers
# ============================================================================

async def _get_job_or_404(job_id: int, db: AsyncSession) -> ScrapeJob:
    query = select(ScrapeJob).where(ScrapeJob.id == job_id)
    result = await db.execute(query)
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail=f"ScrapeJob {job_id} not found")
    return job


async def _get_task_or_404(task_id: int, db: AsyncSession) -> AutomationTask:
    query = select(AutomationTask).where(
        AutomationTask.id == task_id,
        AutomationTask.trigger_type == "scraping",
    )
    result = await db.execute(query)
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail=f"Scraping task {task_id} not found")
    return task


async def _get_group_concurrency(rate_limit_group: str, db: AsyncSession) -> int:
    """Count active tasks in a rate limit group."""
    result = await db.execute(
        select(func.count(AutomationTask.id))
        .join(ScrapeJob, ScrapeJob.id == AutomationTask.scrape_job_id)
        .where(
            AutomationTask.trigger_type == "scraping",
            AutomationTask.status.in_(["pending", "assigned", "running"]),
            ScrapeJob.rate_limit_group == rate_limit_group,
        )
    )
    return result.scalar() or 0


def _job_to_response(job: ScrapeJob) -> ScrapeJobResponse:
    return ScrapeJobResponse(
        id=job.id,
        name=job.name,
        description=job.description,
        scrape_type=job.scrape_type,
        target_config=job.target_config,
        schedule_enabled=job.schedule_enabled,
        schedule_interval_ms=job.schedule_interval_ms,
        preferred_tier=job.preferred_tier,
        anti_detection_config=job.anti_detection_config,
        rate_limit_group=job.rate_limit_group,
        max_concurrent_per_group=job.max_concurrent_per_group,
        last_result=job.last_result,
        last_result_at=job.last_result_at.isoformat() if job.last_result_at else None,
        last_error=redact_infra(job.last_error),
        is_active=job.is_active,
        run_count=job.run_count,
        success_count=job.success_count,
        failure_count=job.failure_count,
        success_rate=job.success_rate,
        effective_tier=job.effective_tier,
        created_at=job.created_at.isoformat() if job.created_at else "",
        updated_at=job.updated_at.isoformat() if job.updated_at else None,
    )


def _result_to_response(result: ScrapeResult) -> ScrapeResultResponse:
    return ScrapeResultResponse(
        id=result.id,
        scrape_job_id=result.scrape_job_id,
        data=result.data,
        executor_agent_id=result.executor_agent_id,
        executor_tier=result.executor_tier,
        duration_ms=result.duration_ms,
        success=result.success,
        error_message=redact_infra(result.error_message),
        scraped_at=result.scraped_at.isoformat() if result.scraped_at else "",
    )
