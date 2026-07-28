"""
Target Selectors router - manage multiple CSS selectors per target.
"""
import logging
import hashlib
from typing import List, Optional
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, computed_field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import httpx
from utils.http_client import http_session
from security.dependencies import check_api_key_scope
from bs4 import BeautifulSoup

from database import get_db
from models.target_selector import TargetSelector
from models.target import Target
from security.api_key import get_current_api_key
from security.validation import InputValidator

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/targets/{target_id}/selectors",
    tags=["Target Selectors"],
    dependencies=[Depends(get_current_api_key)]
)


async def _verify_target(target_id: int, db: AsyncSession = Depends(get_db), api_key: dict = Depends(get_current_api_key)):
    """Verify the target exists and the key is scoped to it (audit #35)."""
    from security.ownership import verify_target_ownership
    check_api_key_scope(api_key, "checks", "read", target_id)
    return await verify_target_ownership(db, target_id)


# ============== Pydantic Models ==============

class RegionViewport(BaseModel):
    """The frame size a visual region's coordinates were measured in."""
    width: int = Field(..., gt=0, description="Capture-frame width in CSS pixels")
    height: int = Field(..., gt=0, description="Capture-frame height in CSS pixels")


class VisualRegion(BaseModel):
    """Visual region coordinates for screenshot comparison.

    x/y are VIEWPORT-relative; scroll_x/scroll_y record how far the page was
    scrolled when the zone was drawn so the agent can restore that scroll before
    clipping (else a below-the-fold zone watches the wrong pixels). Default 0 =
    top-of-page (back-compat for rows saved before scroll was captured).

    `viewport` is the frame SIZE those coordinates were measured at, and it is what
    makes x/y mean anything: the recorder preview runs at 1920x1080 while checks used
    to pin 1280x800, so a zone stored without it clipped ~1.5x off and at the wrong
    aspect ratio. The agent now opens its check context at this size (and rescales
    onto it if it can't) — see `monitor/visual_region.rs`. Absent → the agent assumes
    1280x800, the size checks have always used, so pre-existing rows keep clipping
    what their baseline hash was computed from. Producers MUST send it.
    """
    x: int = Field(..., ge=0, description="X coordinate of the region (viewport-relative)")
    y: int = Field(..., ge=0, description="Y coordinate of the region (viewport-relative)")
    width: int = Field(..., gt=0, description="Width of the region")
    height: int = Field(..., gt=0, description="Height of the region")
    scroll_x: int = Field(0, ge=0, description="Page scrollX when the zone was drawn")
    scroll_y: int = Field(0, ge=0, description="Page scrollY when the zone was drawn")
    viewport: Optional[RegionViewport] = Field(
        None, description="Frame size the coordinates were captured at (default 1280x800)"
    )


class SelectorCreate(BaseModel):
    """Request to create a new selector."""
    name: str = Field(..., min_length=1, max_length=255, description="Human-readable name")
    selector: str = Field(..., min_length=1, max_length=512, description="CSS selector")
    description: Optional[str] = None
    enabled: bool = True
    content_type: str = Field("text", description="Content type: text, html, or visual")
    visual_region: Optional[VisualRegion] = Field(None, description="For visual type: screenshot region coordinates")
    ignore_regex: Optional[str] = None
    priority: int = Field(0, ge=-100, le=100)


class SelectorUpdate(BaseModel):
    """Request to update a selector."""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    selector: Optional[str] = Field(None, min_length=1, max_length=512)
    description: Optional[str] = None
    enabled: Optional[bool] = None
    content_type: Optional[str] = Field(None, description="Content type: text, html, or visual")
    visual_region: Optional[VisualRegion] = Field(None, description="For visual type: screenshot region coordinates")
    ignore_regex: Optional[str] = None
    priority: Optional[int] = Field(None, ge=-100, le=100)


class SelectorResponse(BaseModel):
    """Response for a selector."""
    id: int
    target_id: int
    name: str
    selector: str
    description: Optional[str]
    enabled: bool
    content_type: str
    visual_region: Optional[dict]
    ignore_regex: Optional[str]
    priority: int
    baseline_hash: Optional[str]
    baseline_fetched_at: Optional[datetime]
    last_content_hash: Optional[str]
    last_checked_at: Optional[datetime]
    change_count: int
    created_at: Optional[datetime]
    updated_at: Optional[datetime]
    # Raw storage ref — read from the ORM but NEVER serialized (excluded), so the
    # internal bucket/key never leaks; it only powers baseline_image_url below.
    baseline_screenshot: Optional[str] = Field(None, exclude=True)

    @computed_field
    @property
    def baseline_image_url(self) -> Optional[str]:
        """Same-origin API path the UI blob-fetches (with its Bearer token) to show
        a visual zone's current baseline image — served by `get_selector_baseline_image`.
        None when this selector has no stored baseline image (text/HTML selectors, or
        a visual zone not yet captured). Relative to the client's `/api` base."""
        if not self.baseline_screenshot:
            return None
        return f"/targets/{self.target_id}/selectors/{self.id}/baseline-image"

    class Config:
        from_attributes = True


class SelectorTestResponse(BaseModel):
    """Response from testing a selector."""
    selector: str
    status: str  # success, no_match, error
    matched_count: int
    content_preview: Optional[str]
    error: Optional[str]


class SelectorBaselineResponse(BaseModel):
    """Response from setting a selector's baseline."""
    selector_id: int
    # Null for visual (screenshot-region) checks — they have no HTML text baseline.
    baseline_hash: Optional[str] = None
    content_preview: Optional[str] = None
    fetched_at: datetime


# ============== Endpoints ==============

@router.get("", response_model=List[SelectorResponse])
async def list_selectors(
    target_id: int,
    enabled_only: bool = False,
    db: AsyncSession = Depends(get_db),
    _target = Depends(_verify_target),
):
    """List all selectors for a target."""

    query = (
        select(TargetSelector)
        .where(TargetSelector.target_id == target_id)
    )

    if enabled_only:
        query = query.where(TargetSelector.enabled == True)

    query = query.order_by(TargetSelector.priority.desc(), TargetSelector.created_at)

    result = await db.execute(query)
    selectors = result.scalars().all()

    return selectors


@router.post("", response_model=SelectorResponse)
async def create_selector(
    target_id: int,
    selector: SelectorCreate,
    db: AsyncSession = Depends(get_db),
    _target = Depends(_verify_target),
):
    """Add a new selector to a target."""
    # Check for duplicate selector
    existing_result = await db.execute(
        select(TargetSelector).where(
            TargetSelector.target_id == target_id,
            TargetSelector.selector == selector.selector
        )
    )
    if existing_result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Selector '{selector.selector}' already exists for this target"
        )

    # Reject a ReDoS-prone ignore_regex at the door. Storing it is the dangerous
    # step: once persisted it runs on every baseline capture and every check,
    # unattended, on the event loop.
    InputValidator.validate_regex(selector.ignore_regex)

    # Create selector
    db_selector = TargetSelector(
        target_id=target_id,
        name=selector.name,
        selector=selector.selector,
        description=selector.description,
        enabled=selector.enabled,
        content_type=selector.content_type,
        visual_region=selector.visual_region.model_dump() if selector.visual_region else None,
        ignore_regex=selector.ignore_regex,
        priority=selector.priority,
    )
    db.add(db_selector)
    # A visual (screenshot-region) selector can only be captured in a real
    # browser, so the target must be routed to a Playwright-capable agent and
    # checked on the browser path. Mark it here (source of truth) — otherwise the
    # distributor may assign it to an HTTP-only agent and the check errors out.
    if db_selector.content_type == "visual" and not _target.requires_playwright:
        _target.requires_playwright = True
    await db.commit()
    await db.refresh(db_selector)

    # Re-push assignments so the fleet gets this new selector NOW. The dispatch builds
    # each check's selector list from target.selectors; without a redistribute the
    # agents keep the previous (possibly zero-selector → never-checked) assignment
    # until the next topology change.
    try:
        from routers.targets import trigger_auto_redistribution
        if getattr(_target, "enabled", True):
            await trigger_auto_redistribution(db, "selector_created")
    except Exception as _e:
        logger.warning(f"selector_created redistribution failed for target {target_id}: {_e}")

    logger.info(f"Created selector {db_selector.id} ({db_selector.name}) for target {target_id}")
    return db_selector


@router.get("/{selector_id}", response_model=SelectorResponse)
async def get_selector(
    target_id: int,
    selector_id: int,
    db: AsyncSession = Depends(get_db),
    _target = Depends(_verify_target),
):
    """Get a specific selector."""
    result = await db.execute(
        select(TargetSelector).where(
            TargetSelector.id == selector_id,
            TargetSelector.target_id == target_id
        )
    )
    selector = result.scalar_one_or_none()

    if not selector:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Selector {selector_id} not found for target {target_id}"
        )

    return selector


@router.get("/{selector_id}/baseline-image")
async def get_selector_baseline_image(
    target_id: int,
    selector_id: int,
    db: AsyncSession = Depends(get_db),
    _target = Depends(_verify_target),
):
    """Stream a visual zone's current baseline region image (same-origin, owner
    scoped via `_verify_target`). The frontend blob-fetches this through the
    Bearer-authenticated client — an `<img>` can't carry the token and a presigned
    MinIO URL is unreachable from the browser. Resolves both MinIO refs and the
    inline-base64 fallback. 404 when the selector has no baseline image yet."""
    from fastapi.responses import Response
    from services import visual_storage

    result = await db.execute(
        select(TargetSelector).where(
            TargetSelector.id == selector_id,
            TargetSelector.target_id == target_id,
        )
    )
    selector = result.scalar_one_or_none()
    if not selector:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Selector not found")

    ref = getattr(selector, "baseline_screenshot", None)
    raw = visual_storage.fetch_snapshot_bytes(ref) if ref else None
    if not raw:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Baseline image not available")
    return Response(
        content=raw,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.put("/{selector_id}", response_model=SelectorResponse)
async def update_selector(
    target_id: int,
    selector_id: int,
    selector: SelectorUpdate,
    db: AsyncSession = Depends(get_db),
    _target = Depends(_verify_target),
):
    """Update a selector."""
    result = await db.execute(
        select(TargetSelector).where(
            TargetSelector.id == selector_id,
            TargetSelector.target_id == target_id
        )
    )
    db_selector = result.scalar_one_or_none()

    if not db_selector:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Selector {selector_id} not found for target {target_id}"
        )

    # Check for duplicate if selector string is being changed
    update_data = selector.model_dump(exclude_unset=True)
    # Validate here too, not only on create — otherwise create-then-update is a
    # trivial bypass of the create-path check.
    if 'ignore_regex' in update_data:
        InputValidator.validate_regex(update_data['ignore_regex'])
    # Convert visual_region to dict if present
    if 'visual_region' in update_data and update_data['visual_region'] is not None:
        update_data['visual_region'] = update_data['visual_region'] if isinstance(update_data['visual_region'], dict) else update_data['visual_region'].model_dump()
    if 'selector' in update_data and update_data['selector'] != db_selector.selector:
        existing_result = await db.execute(
            select(TargetSelector).where(
                TargetSelector.target_id == target_id,
                TargetSelector.selector == update_data['selector'],
                TargetSelector.id != selector_id
            )
        )
        if existing_result.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Selector '{update_data['selector']}' already exists for this target"
            )

    # Update fields
    for key, value in update_data.items():
        setattr(db_selector, key, value)

    # If this selector is (now) visual, the target must run on the browser path
    # via a Playwright-capable agent (see create_selector). Promote the flag.
    if db_selector.content_type == "visual" and not _target.requires_playwright:
        _target.requires_playwright = True

    await db.commit()
    await db.refresh(db_selector)

    logger.info(f"Updated selector {selector_id}")
    return db_selector


@router.delete("/{selector_id}")
async def delete_selector(
    target_id: int,
    selector_id: int,
    db: AsyncSession = Depends(get_db),
    _target = Depends(_verify_target),
):
    """Delete a selector."""
    result = await db.execute(
        select(TargetSelector).where(
            TargetSelector.id == selector_id,
            TargetSelector.target_id == target_id
        )
    )
    db_selector = result.scalar_one_or_none()

    if db_selector:
        await db.delete(db_selector)
        await db.commit()
        logger.info(f"Deleted selector {selector_id}")

    return {"deleted": True, "selector_id": selector_id}


@router.post("/{selector_id}/toggle")
async def toggle_selector(
    target_id: int,
    selector_id: int,
    db: AsyncSession = Depends(get_db),
    _target = Depends(_verify_target),
):
    """Toggle a selector's enabled status."""
    result = await db.execute(
        select(TargetSelector).where(
            TargetSelector.id == selector_id,
            TargetSelector.target_id == target_id
        )
    )
    db_selector = result.scalar_one_or_none()

    if not db_selector:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Selector {selector_id} not found"
        )

    db_selector.enabled = not db_selector.enabled
    await db.commit()

    logger.info(f"Toggled selector {selector_id} to enabled={db_selector.enabled}")
    return {"selector_id": selector_id, "enabled": db_selector.enabled}


@router.post("/{selector_id}/test", response_model=SelectorTestResponse)
async def test_selector(
    target_id: int,
    selector_id: int,
    db: AsyncSession = Depends(get_db),
    _target = Depends(_verify_target),
):
    """Test a selector against the target URL."""
    # Get selector and target
    result = await db.execute(
        select(TargetSelector).where(
            TargetSelector.id == selector_id,
            TargetSelector.target_id == target_id
        )
    )
    db_selector = result.scalar_one_or_none()

    if not db_selector:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Selector {selector_id} not found"
        )

    target_result = await db.execute(
        select(Target).where(Target.id == target_id)
    )
    target = target_result.scalar_one_or_none()

    try:
        from security.validation import InputValidator
        from config import settings
        async with http_session() as client:
            # SSRF-hardened: validate + IP-pin + re-validate every redirect hop.
            response = await InputValidator.safe_fetch(
                client, target.url,
                allow_private=settings.allow_private_targets,
                timeout=30.0,
            )
            soup = BeautifulSoup(response.text, 'lxml')
            selected = soup.select(db_selector.selector)

            if selected:
                # Get combined text content
                content = '\n'.join(el.get_text(separator=' ', strip=True) for el in selected)
                return SelectorTestResponse(
                    selector=db_selector.selector,
                    status="success",
                    matched_count=len(selected),
                    content_preview=content[:500] if content else None,
                    error=None
                )
            else:
                return SelectorTestResponse(
                    selector=db_selector.selector,
                    status="no_match",
                    matched_count=0,
                    content_preview=None,
                    error="Selector did not match any elements"
                )

    except Exception as e:
        logger.error(f"Failed to test selector {selector_id}: {e}")
        return SelectorTestResponse(
            selector=db_selector.selector,
            status="error",
            matched_count=0,
            content_preview=None,
            error=str(e)
        )


@router.post("/{selector_id}/set-baseline", response_model=SelectorBaselineResponse)
async def set_selector_baseline(
    target_id: int,
    selector_id: int,
    db: AsyncSession = Depends(get_db),
    _target = Depends(_verify_target),
):
    """Fetch current content and set as baseline for this selector."""
    # Get selector and target
    result = await db.execute(
        select(TargetSelector).where(
            TargetSelector.id == selector_id,
            TargetSelector.target_id == target_id
        )
    )
    db_selector = result.scalar_one_or_none()

    if not db_selector:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Selector {selector_id} not found"
        )

    target_result = await db.execute(
        select(Target).where(Target.id == target_id)
    )
    target = target_result.scalar_one_or_none()

    # Visual (screenshot-region) checks have no HTML text baseline — the agent
    # captures a screenshot of the region at run time. Treat this as a no-op
    # success rather than failing with "Selector does not match any elements".
    if db_selector.content_type == "visual" or (db_selector.selector or "").startswith("viewport-zone"):
        db_selector.baseline_content = None
        db_selector.baseline_hash = None
        db_selector.baseline_fetched_at = datetime.now(timezone.utc)
        await db.commit()
        return SelectorBaselineResponse(
            selector_id=selector_id,
            baseline_hash=None,
            content_preview=None,
            fetched_at=db_selector.baseline_fetched_at,
        )

    try:
        from security.validation import InputValidator
        from config import settings
        async with http_session() as client:
            # SSRF-hardened: validate + IP-pin + re-validate every redirect hop.
            response = await InputValidator.safe_fetch(
                client, target.url,
                allow_private=settings.allow_private_targets,
                timeout=30.0,
            )
            soup = BeautifulSoup(response.text, 'lxml')
            selected = soup.select(db_selector.selector)

            if not selected:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Selector does not match any elements"
                )

            # Get combined text content
            content = '\n'.join(el.get_text(separator=' ', strip=True) for el in selected)

            # Apply ignore_regex if set. This runs synchronously inside an async
            # handler, so a catastrophically-backtracking pattern would stall the
            # whole single-worker process — every HTTP request, every agent
            # WebSocket, the scheduler. safe_regex_sub caps the input and enforces
            # a hard wall-clock timeout.
            if db_selector.ignore_regex:
                content = InputValidator.safe_regex_sub(
                    db_selector.ignore_regex, '', content
                )

            content_hash = hashlib.sha256(content.encode()).hexdigest()

            # Update selector baseline
            db_selector.baseline_content = content
            db_selector.baseline_hash = content_hash
            db_selector.baseline_fetched_at = datetime.now(timezone.utc)
            await db.commit()

            logger.info(f"Set baseline for selector {selector_id}: hash={content_hash[:16]}...")

            return SelectorBaselineResponse(
                selector_id=selector_id,
                baseline_hash=content_hash,
                content_preview=content[:500],
                fetched_at=db_selector.baseline_fetched_at
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to set baseline for selector {selector_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch content: {str(e)}"
        )


@router.post("/{selector_id}/clear-baseline")
async def clear_selector_baseline(
    target_id: int,
    selector_id: int,
    db: AsyncSession = Depends(get_db),
    _target = Depends(_verify_target),
):
    """Clear a selector's baseline."""
    result = await db.execute(
        select(TargetSelector).where(
            TargetSelector.id == selector_id,
            TargetSelector.target_id == target_id
        )
    )
    db_selector = result.scalar_one_or_none()

    if not db_selector:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Selector {selector_id} not found"
        )

    db_selector.baseline_content = None
    db_selector.baseline_hash = None
    db_selector.baseline_fetched_at = None
    await db.commit()

    logger.info(f"Cleared baseline for selector {selector_id}")
    return {"selector_id": selector_id, "baseline_cleared": True}


# ============== Bulk Operations ==============

@router.post("/set-all-baselines")
async def set_all_selector_baselines(
    target_id: int,
    db: AsyncSession = Depends(get_db),
    _target = Depends(_verify_target),
):
    """Set baselines for all enabled selectors of a target."""
    # Get all enabled selectors
    result = await db.execute(
        select(TargetSelector).where(
            TargetSelector.target_id == target_id,
            TargetSelector.enabled == True
        )
    )
    selectors = result.scalars().all()

    if not selectors:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No enabled selectors found for target {target_id}"
        )

    # Get target
    target_result = await db.execute(
        select(Target).where(Target.id == target_id)
    )
    target = target_result.scalar_one_or_none()

    results = []
    try:
        from security.validation import InputValidator
        from config import settings
        async with http_session() as client:
            # SSRF-hardened: validate + IP-pin + re-validate every redirect hop.
            response = await InputValidator.safe_fetch(
                client, target.url,
                allow_private=settings.allow_private_targets,
                timeout=30.0,
            )
            soup = BeautifulSoup(response.text, 'lxml')

            for selector in selectors:
                try:
                    selected = soup.select(selector.selector)

                    if selected:
                        content = '\n'.join(el.get_text(separator=' ', strip=True) for el in selected)

                        if selector.ignore_regex:
                            content = InputValidator.safe_regex_sub(
                                selector.ignore_regex, '', content
                            )

                        content_hash = hashlib.sha256(content.encode()).hexdigest()

                        selector.baseline_content = content
                        selector.baseline_hash = content_hash
                        selector.baseline_fetched_at = datetime.now(timezone.utc)

                        results.append({
                            "selector_id": selector.id,
                            "name": selector.name,
                            "status": "success",
                            "baseline_hash": content_hash,
                        })
                    else:
                        results.append({
                            "selector_id": selector.id,
                            "name": selector.name,
                            "status": "no_match",
                            "error": "Selector did not match any elements",
                        })
                except Exception as e:
                    results.append({
                        "selector_id": selector.id,
                        "name": selector.name,
                        "status": "error",
                        "error": str(e),
                    })

            await db.commit()

    except Exception as e:
        logger.error(f"Failed to fetch page for target {target_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch page: {str(e)}"
        )

    success_count = sum(1 for r in results if r['status'] == 'success')
    logger.info(f"Set baselines for {success_count}/{len(results)} selectors of target {target_id}")

    return {
        "target_id": target_id,
        "total": len(results),
        "success": success_count,
        "results": results
    }
