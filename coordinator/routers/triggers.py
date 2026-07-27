"""
Trigger Rules API - CRUD operations for unified trigger rules.
"""
import logging
import secrets
from typing import List, Optional, Dict, Any
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

from database import get_db
from models.trigger_rule import TriggerRule, TriggerExecution
from models.target import Target
from models.target_selector import TargetSelector
from models.webhook_trigger import WebhookTrigger
from security.api_key import get_current_api_key
from security.dependencies import check_api_key_scope
from security.encryption import SecretEncryption
from security.validation import InputValidator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/triggers", tags=["Triggers"])


def _new_webhook_trigger(name: str) -> WebhookTrigger:
    """Build a WebhookTrigger that is ALWAYS signature-protected.

    SECURITY (#7): the inbound webhook routes (/webhooks/trigger/{id},
    /webhooks/hook/{token}) are unauthenticated and refuse to execute a trigger
    with no secret. Every auto-created/backfilled webhook trigger must therefore
    ship with a freshly generated HMAC secret (encrypted at rest, mirroring the
    /webhooks POST create path) so it is never left secret-less. The unguessable
    token remains the recommended entry point; the secret is the enforced one.
    """
    raw_secret = secrets.token_urlsafe(32)
    return WebhookTrigger(
        name=name or "Webhook",
        action="trigger_automation",
        token=secrets.token_urlsafe(32),
        secret=SecretEncryption.encrypt_secret(raw_secret),
        enabled=True,
    )


def _validate_condition_regexes(
    conditions: Optional[Dict[str, Any]],
    blocks: Optional[List[Any]],
) -> None:
    """Validate every ``matches``-operator regex in a rule before persisting.

    ReDoS guard: the ``matches`` operator runs the user-supplied ``value`` as a
    regex against scraped/extracted content at evaluation time. Rejecting unsafe
    patterns here (length / nesting / structural backtracking heuristic +
    timeout-bounded probe via ``InputValidator.validate_regex``) means a
    catastrophic pattern can never be stored. Patterns live in two places:

      * ``conditions`` JSONB: ``{key: {"operator": "matches", "value": <re>}}``
      * visual builder ``blocks``: a condition block whose
        ``config`` holds ``{"operator": "matches", "value": <re>}``

    Raises HTTPException(400) on an unsafe/invalid pattern.
    """
    # Legacy / data conditions dict.
    if isinstance(conditions, dict):
        for spec in conditions.values():
            if isinstance(spec, dict) and spec.get("operator") == "matches":
                pattern = spec.get("value")
                if isinstance(pattern, str) and pattern:
                    InputValidator.validate_regex(pattern)

    # Visual builder condition blocks.
    if blocks:
        for block in blocks:
            # Blocks may be Pydantic FlowBlock models (create/update request) or
            # plain dicts (already-persisted rows); handle both.
            block_type = getattr(block, "type", None)
            cfg = getattr(block, "config", None)
            if block_type is None and isinstance(block, dict):
                block_type = block.get("type")
                cfg = block.get("config")
            if block_type != "condition" or not isinstance(cfg, dict):
                continue
            if cfg.get("operator") == "matches":
                pattern = cfg.get("value")
                if isinstance(pattern, str) and pattern:
                    InputValidator.validate_regex(pattern)


# Pydantic models
class ConditionSpec(BaseModel):
    """Condition specification for a trigger rule."""
    operator: str = Field(..., description="Operator: equals, not_equals, contains, not_contains, gt, gte, lt, lte, changed, matches")
    value: Optional[Any] = Field(None, description="Value to compare against")


class ScheduleSpec(BaseModel):
    """Schedule specification for time-based conditions."""
    time_window: Optional[Dict[str, str]] = Field(None, description="Time window: {start: '09:00', end: '18:00'}")
    days_of_week: Optional[List[int]] = Field(None, description="Days of week (1=Monday, 7=Sunday)")
    cooldown_minutes: Optional[int] = Field(None, description="Minimum minutes between triggers")


class ActionConfig(BaseModel):
    """Action configuration within a trigger rule."""
    type: str = Field(..., description="Action type: notification, ai_session, workflow")
    config: Dict[str, Any] = Field(default_factory=dict, description="Type-specific configuration")


class FlowBlock(BaseModel):
    """A block in the visual workflow builder."""
    id: str = Field(..., description="Unique block ID")
    type: str = Field(..., description="Block type: event, condition, action")
    blockType: str = Field(..., description="Specific block type: change_detected, ai_session_completed, notification, etc.")
    config: Dict[str, Any] = Field(default_factory=dict, description="Block-specific configuration")
    parentId: Optional[str] = Field(None, description="Parent block ID for tree structure")


class TriggerRuleCreate(BaseModel):
    """Create a new trigger rule."""
    target_id: Optional[int] = Field(None, description="Target this trigger belongs to (optional for webhook/workflow triggers)")
    event_type: str = Field(default="change_detected", description="Event type: change_detected, webhook_received, ai_session_started, ai_session_completed, workflow_started, workflow_completed")
    target_selector_id: Optional[int] = Field(None, description="Optional: only fire for this selector (change_detected)")
    ai_session_id: Optional[int] = Field(None, description="Optional: only fire for this AI session (ai_session_*)")
    workflow_id: Optional[int] = Field(None, description="Optional: only fire for this workflow (workflow_*)")
    webhook_trigger_id: Optional[int] = Field(None, description="Optional: only fire for this webhook trigger (webhook_received)")
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    enabled: bool = True
    priority: int = Field(default=0, description="Execution order (higher = first)")
    conditions: Optional[Dict[str, Any]] = Field(default_factory=dict)
    actions: List[ActionConfig] = Field(default_factory=list)
    blocks: Optional[List[FlowBlock]] = Field(None, description="Visual workflow builder blocks")


class TriggerRuleUpdate(BaseModel):
    """Update an existing trigger rule."""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    event_type: Optional[str] = None
    target_selector_id: Optional[int] = None
    ai_session_id: Optional[int] = None
    workflow_id: Optional[int] = None
    webhook_trigger_id: Optional[int] = None
    enabled: Optional[bool] = None
    priority: Optional[int] = None
    conditions: Optional[Dict[str, Any]] = None
    actions: Optional[List[ActionConfig]] = None
    blocks: Optional[List[FlowBlock]] = None


class TriggerRuleResponse(BaseModel):
    """Trigger rule response model."""
    id: int
    target_id: Optional[int] = None
    event_type: str
    target_selector_id: Optional[int] = None
    ai_session_id: Optional[int] = None
    workflow_id: Optional[int] = None
    webhook_trigger_id: Optional[int] = None
    webhook_trigger_token: Optional[str] = None
    custom_path: Optional[str] = None
    name: str
    description: Optional[str] = None
    enabled: bool
    priority: int
    conditions: Optional[Dict[str, Any]] = None
    actions: List[Dict[str, Any]]
    blocks: Optional[List[Dict[str, Any]]] = None
    last_triggered_at: Optional[str] = None
    trigger_count: int
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    class Config:
        from_attributes = True


class TriggerExecutionResponse(BaseModel):
    """Trigger execution response model."""
    id: int
    trigger_rule_id: int
    detected_change_id: Optional[int]
    status: str
    trigger_context: Optional[Dict[str, Any]]
    action_results: Optional[List[Dict[str, Any]]]
    triggered_at: Optional[str]
    completed_at: Optional[str]
    error_message: Optional[str]

    class Config:
        from_attributes = True


class TestTriggerRequest(BaseModel):
    """Request to test a trigger with mock data."""
    test_content: str = Field(..., description="Test content to evaluate")
    test_extracted: Optional[Dict[str, Any]] = Field(None, description="Mock extracted data")


class TestTriggerResponse(BaseModel):
    """Response from testing a trigger."""
    would_fire: bool
    match_reason: str
    trigger_name: str
    trigger_enabled: bool
    conditions: Optional[Dict[str, Any]]
    actions_count: int


# Endpoints
@router.get(
    "/all",
    response_model=List[TriggerRuleResponse],
    summary="List All Trigger Rules",
    description="Get all trigger rules across all targets and workflows.",
)
async def list_all_trigger_rules(
    enabled_only: bool = False,
    event_type: Optional[str] = None,
    workflow_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """List trigger rules."""
    query = select(TriggerRule)

    if enabled_only:
        query = query.where(TriggerRule.enabled == True)
    if event_type:
        query = query.where(TriggerRule.event_type == event_type)
    if workflow_id:
        query = query.where(TriggerRule.workflow_id == workflow_id)

    query = query.order_by(TriggerRule.priority.desc(), TriggerRule.id)
    result = await db.execute(query)
    rules = result.scalars().all()

    # NOTE (#30): this GET is a pure read — it must NOT create rows or commit.
    # webhook_received rules missing an endpoint are surfaced with a null token;
    # provisioning happens at create/update time (create_trigger_rule /
    # update_trigger_rule) or via the explicit POST /triggers/backfill-webhooks
    # write path below.

    # Fetch webhook tokens + custom paths
    webhook_trigger_ids = [r.webhook_trigger_id for r in rules if r.webhook_trigger_id]
    webhook_info = {}
    if webhook_trigger_ids:
        wh_result = await db.execute(
            select(WebhookTrigger).where(WebhookTrigger.id.in_(webhook_trigger_ids))
        )
        for wh in wh_result.scalars().all():
            webhook_info[wh.id] = {"token": wh.token, "custom_path": getattr(wh, 'custom_path', None)}

    return [
        TriggerRuleResponse(
            id=r.id,
            target_id=r.target_id,
            event_type=r.event_type or "change_detected",
            target_selector_id=r.target_selector_id,
            ai_session_id=None,
            workflow_id=r.workflow_id,
            webhook_trigger_id=r.webhook_trigger_id,
            webhook_trigger_token=webhook_info.get(r.webhook_trigger_id, {}).get("token") if r.webhook_trigger_id else None,
            custom_path=webhook_info.get(r.webhook_trigger_id, {}).get("custom_path") if r.webhook_trigger_id else None,
            name=r.name,
            description=r.description,
            enabled=r.enabled,
            priority=r.priority,
            conditions=r.conditions,
            actions=r.actions or [],
            blocks=r.blocks,
            last_triggered_at=r.last_triggered_at.isoformat() if r.last_triggered_at else None,
            trigger_count=r.trigger_count or 0,
            created_at=r.created_at.isoformat() if r.created_at else None,
        )
        for r in rules
    ]


@router.post(
    "/backfill-webhooks",
    summary="Provision missing webhook endpoints",
    description=(
        "Explicit write path (moved off the GET /triggers/all read, #30): for every "
        "webhook_received rule that lacks a webhook endpoint, create one — always with a "
        "generated HMAC secret (#7) — and wire it into the rule's blocks config."
    ),
)
async def backfill_webhook_endpoints(
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Create webhook endpoints for webhook_received rules missing one.

    This is the write counterpart of the (now read-only) GET /triggers/all. It is
    idempotent: rules that already have a webhook_trigger_id are skipped.
    """
    check_api_key_scope(current_api_key, "automations", "write")
    from sqlalchemy.orm.attributes import flag_modified

    result = await db.execute(
        select(TriggerRule).where(
            TriggerRule.event_type == "webhook_received",
            TriggerRule.webhook_trigger_id.is_(None),
        )
    )
    rules = result.scalars().all()

    backfilled = []
    for r in rules:
        wh = _new_webhook_trigger(r.name)
        db.add(wh)
        await db.flush()
        r.webhook_trigger_id = wh.id
        # Update blocks config — need flag_modified for JSONB mutation
        if r.blocks:
            updated_blocks = list(r.blocks)  # copy to trigger change detection
            for b in updated_blocks:
                if isinstance(b, dict) and b.get("blockType") == "webhook_received":
                    b.setdefault("config", {})["webhook_trigger_id"] = wh.id
                    b["config"]["webhook_trigger_token"] = wh.token
                    break
            r.blocks = updated_blocks
            flag_modified(r, "blocks")
        backfilled.append({"trigger_rule_id": r.id, "webhook_trigger_id": wh.id, "token": wh.token})
        logger.info(f"Backfilled webhook endpoint {wh.id} for trigger rule {r.id}")

    if backfilled:
        await db.commit()

    return {"backfilled": backfilled, "count": len(backfilled)}


@router.get(
    "/references",
    summary="Cross-reference map",
    description=(
        "Compact map of which flows reference each entity. Lets list pages show "
        "cross-ref badges without downloading every flow's full `blocks` JSONB."
    ),
)
async def list_flow_references(
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Return {entity_type: {entity_id: [{id, name}]}} for all flows.

    Replaces the previous client-side pattern where four list pages each fetched
    the entire `/triggers/all` payload (full blocks JSONB) just to compute these
    references. Here the block scan runs once, server-side, and only id+name per
    referencing flow crosses the wire.
    """
    # Only id, name, blocks are needed — skip the heavy rest of the row.
    result = await db.execute(
        select(TriggerRule.id, TriggerRule.name, TriggerRule.blocks)
    )

    refs = {"workflow": {}, "target": {}, "ai_session": {}, "webhook": {}}

    def _add(bucket: str, entity_id, flow_ref):
        if entity_id is None:
            return
        key = str(entity_id)
        refs[bucket].setdefault(key, [])
        # De-dup: a flow referencing the same entity in multiple blocks appears once.
        if not any(fr["id"] == flow_ref["id"] for fr in refs[bucket][key]):
            refs[bucket][key].append(flow_ref)

    for rule_id, rule_name, blocks in result.all():
        if not blocks:
            continue
        flow_ref = {"id": rule_id, "name": rule_name}
        for b in blocks:
            if not isinstance(b, dict):
                continue
            bt = b.get("blockType")
            cfg = b.get("config") or {}
            if bt == "workflow":
                _add("workflow", cfg.get("workflow_id"), flow_ref)
            elif bt == "change_detected":
                _add("target", cfg.get("target_id"), flow_ref)
            elif bt == "ai_session":
                for sid in (cfg.get("session_ids") or []):
                    _add("ai_session", sid, flow_ref)
            elif bt == "webhook_received":
                _add("webhook", cfg.get("webhook_trigger_id"), flow_ref)

    return refs


@router.get(
    "/target/{target_id}",
    response_model=List[TriggerRuleResponse],
    summary="List Trigger Rules",
    description="Get all trigger rules for a target.",
)
async def list_trigger_rules(
    target_id: int,
    enabled_only: bool = False,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """List trigger rules for a target."""
    query = select(TriggerRule).where(
        TriggerRule.target_id == target_id,
    )

    if enabled_only:
        query = query.where(TriggerRule.enabled == True)

    query = query.order_by(TriggerRule.priority.desc(), TriggerRule.id)

    result = await db.execute(query)
    rules = result.scalars().all()

    # Fetch webhook tokens for rules that have webhook_trigger_id
    webhook_trigger_ids = [r.webhook_trigger_id for r in rules if r.webhook_trigger_id]
    webhook_tokens = {}
    if webhook_trigger_ids:
        wh_result = await db.execute(
            select(WebhookTrigger.id, WebhookTrigger.token).where(WebhookTrigger.id.in_(webhook_trigger_ids))
        )
        for wh_id, wh_token in wh_result:
            webhook_tokens[wh_id] = wh_token

    return [
        TriggerRuleResponse(
            id=r.id,
            target_id=r.target_id,
            event_type=r.event_type or "change_detected",
            target_selector_id=r.target_selector_id,
            ai_session_id=None,
            workflow_id=r.workflow_id,
            webhook_trigger_id=r.webhook_trigger_id,
            webhook_trigger_token=webhook_tokens.get(r.webhook_trigger_id) if r.webhook_trigger_id else None,
            name=r.name,
            description=r.description,
            enabled=r.enabled,
            priority=r.priority,
            conditions=r.conditions,
            actions=r.actions or [],
            blocks=r.blocks,
            last_triggered_at=r.last_triggered_at.isoformat() if r.last_triggered_at else None,
            trigger_count=r.trigger_count or 0,
            created_at=r.created_at.isoformat() if r.created_at else None,
            updated_at=r.updated_at.isoformat() if r.updated_at else None,
        )
        for r in rules
    ]


@router.post(
    "",
    response_model=TriggerRuleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Trigger Rule",
    description="Create a new trigger rule for a target.",
)
async def create_trigger_rule(
    request: TriggerRuleCreate,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Create a new trigger rule."""
    check_api_key_scope(current_api_key, "automations", "write")
    from security.ownership import verify_target_ownership

    # Verify the target exists (if provided)
    if request.target_id:
        await verify_target_ownership(db, request.target_id)

    # Verify selector belongs to the target (if specified)
    if request.target_selector_id:
        selector_result = await db.execute(
            select(TargetSelector).where(
                and_(
                    TargetSelector.id == request.target_selector_id,
                    TargetSelector.target_id == request.target_id,
                )
            )
        )
        selector = selector_result.scalar_one_or_none()

        if not selector:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Selector {request.target_selector_id} not found for target {request.target_id}",
            )

    # Validate event_type
    valid_event_types = [
        "change_detected", "webhook_received",
        "ai_session_started", "ai_session_completed",
        "workflow_started", "workflow_completed",
        # Monitor-health edges (services.monitoring_state / dispatch_monitor_health).
        "monitor_down", "monitor_stale", "monitor_recovered",
        # Dragnet crawl lifecycle (services.crawl_orchestrator._finalize/_seed →
        # UnifiedTriggerService.process_crawl_event). A whole-site crawl converges
        # async; these fire once on the crawl's terminal/start state (NOT per shard).
        "crawl_started", "crawl_completed", "crawl_failed",
    ]
    if request.event_type not in valid_event_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid event_type. Must be one of: {valid_event_types}",
        )

    # ReDoS guard: reject unsafe 'matches' regex patterns before storing.
    _validate_condition_regexes(request.conditions, request.blocks)

    # Extract webhook_trigger_id from blocks config if present
    webhook_trigger_id = request.webhook_trigger_id
    if not webhook_trigger_id and request.blocks:
        for block in request.blocks:
            if block.blockType == 'webhook_received' and block.config.get('webhook_trigger_id'):
                webhook_trigger_id = block.config['webhook_trigger_id']
                break

    # Auto-create webhook endpoint for webhook_received automations.
    # Always ships with a generated HMAC secret (#7) via _new_webhook_trigger.
    if request.event_type == 'webhook_received' and not webhook_trigger_id:
        wh = _new_webhook_trigger(request.name)
        db.add(wh)
        await db.flush()
        webhook_trigger_id = wh.id
        # Inject token into first webhook_received block config
        if request.blocks:
            for block in request.blocks:
                if block.blockType == 'webhook_received':
                    block.config['webhook_trigger_id'] = wh.id
                    block.config['webhook_trigger_token'] = wh.token
                    break
        logger.info(f"Auto-created webhook endpoint {wh.id} for automation '{request.name}'")

    rule = TriggerRule(
        target_id=request.target_id,
        event_type=request.event_type,
        target_selector_id=request.target_selector_id,
        workflow_id=request.workflow_id,
        webhook_trigger_id=webhook_trigger_id,
        name=request.name,
        description=request.description,
        enabled=request.enabled,
        priority=request.priority,
        conditions=request.conditions,
        actions=[a.model_dump() for a in request.actions],
        blocks=[b.model_dump() for b in request.blocks] if request.blocks else None,
    )

    db.add(rule)
    await db.commit()
    await db.refresh(rule)

    logger.info(f"Created trigger rule {rule.id} ({rule.name}) for target {request.target_id}, event_type={request.event_type}")

    # Resolve webhook token for response
    response_wh_token = None
    if rule.webhook_trigger_id:
        from models.webhook_trigger import WebhookTrigger as WH
        wh_result = await db.execute(select(WH.token).where(WH.id == rule.webhook_trigger_id))
        response_wh_token = wh_result.scalar_one_or_none()

    return TriggerRuleResponse(
        id=rule.id,
        target_id=rule.target_id,
        event_type=rule.event_type,
        target_selector_id=rule.target_selector_id,
        ai_session_id=None,
        workflow_id=rule.workflow_id,
        webhook_trigger_id=rule.webhook_trigger_id,
        webhook_trigger_token=response_wh_token,
        name=rule.name,
        description=rule.description,
        enabled=rule.enabled,
        priority=rule.priority,
        conditions=rule.conditions,
        actions=rule.actions or [],
        blocks=rule.blocks,
        last_triggered_at=None,
        trigger_count=0,
        created_at=rule.created_at.isoformat() if rule.created_at else None,
        updated_at=None,
    )


@router.get(
    "/{trigger_id}",
    response_model=TriggerRuleResponse,
    summary="Get Trigger Rule",
    description="Get a specific trigger rule by ID.",
)
async def get_trigger_rule(
    trigger_id: int,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Get a trigger rule by ID."""
    check_api_key_scope(current_api_key, "automations", "read", trigger_id)
    result = await db.execute(
        select(TriggerRule).where(TriggerRule.id == trigger_id)
    )
    rule = result.scalar_one_or_none()

    if not rule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Trigger rule {trigger_id} not found",
        )

    # Resolve webhook token for the response
    get_wh_token = None
    if rule.webhook_trigger_id:
        from models.webhook_trigger import WebhookTrigger as WHGet
        wh_res = await db.execute(select(WHGet.token).where(WHGet.id == rule.webhook_trigger_id))
        get_wh_token = wh_res.scalar_one_or_none()

    return TriggerRuleResponse(
        id=rule.id,
        target_id=rule.target_id,
        event_type=rule.event_type or "change_detected",
        target_selector_id=rule.target_selector_id,
        ai_session_id=None,
        workflow_id=rule.workflow_id,
        webhook_trigger_id=rule.webhook_trigger_id,
        webhook_trigger_token=get_wh_token,
        name=rule.name,
        description=rule.description,
        enabled=rule.enabled,
        priority=rule.priority,
        conditions=rule.conditions,
        actions=rule.actions or [],
        blocks=rule.blocks,
        last_triggered_at=rule.last_triggered_at.isoformat() if rule.last_triggered_at else None,
        trigger_count=rule.trigger_count or 0,
        created_at=rule.created_at.isoformat() if rule.created_at else None,
        updated_at=rule.updated_at.isoformat() if rule.updated_at else None,
    )


@router.patch(
    "/{trigger_id}",
    response_model=TriggerRuleResponse,
    summary="Update Trigger Rule",
    description="Update an existing trigger rule.",
)
async def update_trigger_rule(
    trigger_id: int,
    request: TriggerRuleUpdate,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Update a trigger rule."""
    result = await db.execute(
        select(TriggerRule).where(TriggerRule.id == trigger_id)
    )
    rule = result.scalar_one_or_none()

    if not rule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Trigger rule {trigger_id} not found",
        )

    # ReDoS guard: validate any incoming 'matches' regex patterns (conditions /
    # blocks) before persisting them.
    if request.conditions is not None or request.blocks is not None:
        _validate_condition_regexes(
            request.conditions if request.conditions is not None else rule.conditions,
            request.blocks if request.blocks is not None else None,
        )

    # Update fields if provided
    if request.name is not None:
        rule.name = request.name
    if request.description is not None:
        rule.description = request.description
    if request.target_selector_id is not None:
        # Verify selector exists for this target
        if request.target_selector_id > 0:
            selector_result = await db.execute(
                select(TargetSelector).where(
                    and_(
                        TargetSelector.id == request.target_selector_id,
                        TargetSelector.target_id == rule.target_id,
                    )
                )
            )
            if not selector_result.scalar_one_or_none():
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Selector {request.target_selector_id} not found for target {rule.target_id}",
                )
        rule.target_selector_id = request.target_selector_id if request.target_selector_id > 0 else None
    if request.enabled is not None:
        rule.enabled = request.enabled
    if request.priority is not None:
        rule.priority = request.priority
    if request.conditions is not None:
        rule.conditions = request.conditions
    if request.actions is not None:
        rule.actions = [a.model_dump() for a in request.actions]
    if request.event_type is not None:
        rule.event_type = request.event_type
    if request.workflow_id is not None:
        rule.workflow_id = request.workflow_id if request.workflow_id > 0 else None
    if request.blocks is not None:
        rule.blocks = [b.model_dump() for b in request.blocks] if request.blocks else None
        # Extract webhook_trigger_id from blocks if not already set
        if not rule.webhook_trigger_id and request.blocks:
            for block in request.blocks:
                if block.blockType == 'webhook_received' and block.config.get('webhook_trigger_id'):
                    rule.webhook_trigger_id = block.config['webhook_trigger_id']
                    break

    # Auto-create webhook endpoint if event_type is webhook_received and no webhook exists.
    # Always ships with a generated HMAC secret (#7) via _new_webhook_trigger.
    effective_event = request.event_type or rule.event_type
    if effective_event == 'webhook_received' and not rule.webhook_trigger_id:
        wh = _new_webhook_trigger(rule.name)
        db.add(wh)
        await db.flush()
        rule.webhook_trigger_id = wh.id
        # Inject token into blocks
        if rule.blocks:
            for b in rule.blocks:
                if isinstance(b, dict) and b.get('blockType') == 'webhook_received':
                    b.setdefault('config', {})['webhook_trigger_id'] = wh.id
                    b['config']['webhook_trigger_token'] = wh.token
                    break
        logger.info(f"Auto-created webhook endpoint {wh.id} for automation update '{rule.name}'")

    await db.commit()
    await db.refresh(rule)

    logger.info(f"Updated trigger rule {trigger_id}")

    # Resolve webhook token for response
    response_wh_token = None
    if rule.webhook_trigger_id:
        from models.webhook_trigger import WebhookTrigger as WH
        wh_result = await db.execute(select(WH.token).where(WH.id == rule.webhook_trigger_id))
        response_wh_token = wh_result.scalar_one_or_none()

    return TriggerRuleResponse(
        id=rule.id,
        target_id=rule.target_id,
        event_type=rule.event_type or "change_detected",
        target_selector_id=rule.target_selector_id,
        ai_session_id=None,
        workflow_id=rule.workflow_id,
        webhook_trigger_id=rule.webhook_trigger_id,
        webhook_trigger_token=response_wh_token,
        name=rule.name,
        description=rule.description,
        enabled=rule.enabled,
        priority=rule.priority,
        conditions=rule.conditions,
        actions=rule.actions or [],
        blocks=rule.blocks,
        last_triggered_at=rule.last_triggered_at.isoformat() if rule.last_triggered_at else None,
        trigger_count=rule.trigger_count or 0,
        created_at=rule.created_at.isoformat() if rule.created_at else None,
        updated_at=rule.updated_at.isoformat() if rule.updated_at else None,
    )


@router.delete(
    "/{trigger_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete Trigger Rule",
    description="Delete a trigger rule.",
)
async def delete_trigger_rule(
    trigger_id: int,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Delete a trigger rule."""
    check_api_key_scope(current_api_key, "automations", "delete", trigger_id)
    result = await db.execute(
        select(TriggerRule).where(TriggerRule.id == trigger_id)
    )
    rule = result.scalar_one_or_none()

    if not rule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Trigger rule {trigger_id} not found",
        )

    await db.delete(rule)
    await db.commit()

    logger.info(f"Deleted trigger rule {trigger_id}")


@router.post(
    "/{trigger_id}/test",
    response_model=TestTriggerResponse,
    summary="Test Trigger Rule",
    description="Test a trigger rule with mock data without executing actions.",
)
async def test_trigger_rule(
    trigger_id: int,
    request: TestTriggerRequest,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Test a trigger rule with mock data."""
    check_api_key_scope(current_api_key, "automations", "read", trigger_id)
    result = await db.execute(
        select(TriggerRule).where(TriggerRule.id == trigger_id)
    )
    rule = result.scalar_one_or_none()

    if not rule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Trigger rule {trigger_id} not found",
        )

    from services.unified_trigger_service import get_unified_trigger_service

    trigger_service = get_unified_trigger_service(db)
    test_result = trigger_service.test_trigger(
        trigger=rule,
        test_content=request.test_content,
        test_extracted=request.test_extracted,
    )

    return TestTriggerResponse(**test_result)


@router.post(
    "/{trigger_id}/run",
    summary="Run Automation",
    description=(
        "Run an automation (trigger rule) on demand, executing its actions / block "
        "chain — including the workflows it references. An API key scoped to this "
        "automation may run it (and its workflows) without separate workflow scopes, "
        "but cannot modify them."
    ),
)
async def run_trigger_rule(
    trigger_id: int,
    request: Optional[Dict[str, Any]] = None,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Execute an automation's actions on demand (run-only cascade entrypoint)."""
    # `automations:read` = "may run this automation" under our run-only semantics.
    check_api_key_scope(current_api_key, "automations", "read", trigger_id)

    # Per-key run limits (execution count / concurrency), when called by an API key.
    key_obj = current_api_key.get("obj") if isinstance(current_api_key, dict) else None
    if key_obj is not None:
        from services.api_key_scope_resolver import enforce_key_run_limits, check_key_hourly_session_limit
        await enforce_key_run_limits(db, key_obj)
        await check_key_hourly_session_limit(db, key_obj)

    result = await db.execute(
        select(TriggerRule).where(
            TriggerRule.id == trigger_id,
        )
    )
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Trigger rule {trigger_id} not found",
        )

    from services.unified_trigger_service import get_unified_trigger_service

    trigger_service = get_unified_trigger_service(db)

    # Build an execution record + a manual context (any provided values are merged).
    context: Dict[str, Any] = {"trigger_type": "manual_run", "manual": True}
    if isinstance(request, dict):
        context.update(request.get("context") or {})

    execution = TriggerExecution(
        trigger_rule_id=rule.id,
        status="running",
        trigger_context=context,
    )
    db.add(execution)
    await db.flush()

    try:
        action_results = await trigger_service._dispatch_actions(
            trigger=rule, context=context, execution=execution, blocking=False,
        )
        execution.status = "completed"
        execution.action_results = action_results
        execution.completed_at = datetime.utcnow()
    except Exception as e:
        execution.status = "failed"
        execution.error_message = str(e)
        logger.error(f"Manual run of automation {trigger_id} failed: {e}")

    if key_obj is not None:
        key_obj.runs_used = (key_obj.runs_used or 0) + 1

    await db.commit()

    return {
        "trigger_id": trigger_id,
        "execution_id": execution.id,
        "status": execution.status,
        "action_results": execution.action_results,
        "error": execution.error_message,
    }


@router.get(
    "/{trigger_id}/executions",
    response_model=List[TriggerExecutionResponse],
    summary="List Trigger Executions",
    description="Get execution history for a trigger rule.",
)
async def list_trigger_executions(
    trigger_id: int,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Get execution history for a trigger rule."""
    # Resolve the rule + scope check before reading its execution history.
    check_api_key_scope(current_api_key, "automations", "read", trigger_id)
    rule_result = await db.execute(
        select(TriggerRule.id).where(
            TriggerRule.id == trigger_id,
        )
    )
    if rule_result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Trigger rule {trigger_id} not found",
        )

    result = await db.execute(
        select(TriggerExecution)
        .where(TriggerExecution.trigger_rule_id == trigger_id)
        .order_by(TriggerExecution.triggered_at.desc())
        .limit(limit)
    )
    executions = result.scalars().all()

    return [
        TriggerExecutionResponse(
            id=e.id,
            trigger_rule_id=e.trigger_rule_id,
            detected_change_id=e.detected_change_id,
            status=e.status,
            trigger_context=e.trigger_context,
            action_results=e.action_results,
            triggered_at=e.triggered_at.isoformat() if e.triggered_at else None,
            completed_at=e.completed_at.isoformat() if e.completed_at else None,
            error_message=e.error_message,
        )
        for e in executions
    ]


@router.patch(
    "/{trigger_id}/toggle",
    response_model=TriggerRuleResponse,
    summary="Toggle Trigger Rule",
    description="Toggle a trigger rule's enabled status.",
)
async def toggle_trigger_rule(
    trigger_id: int,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Toggle a trigger rule's enabled status."""
    check_api_key_scope(current_api_key, "automations", "write", trigger_id)
    result = await db.execute(
        select(TriggerRule).where(TriggerRule.id == trigger_id)
    )
    rule = result.scalar_one_or_none()

    if not rule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Trigger rule {trigger_id} not found",
        )

    rule.enabled = not rule.enabled
    await db.commit()
    await db.refresh(rule)

    logger.info(f"Toggled trigger rule {trigger_id} to enabled={rule.enabled}")

    return TriggerRuleResponse(
        id=rule.id,
        target_id=rule.target_id,
        event_type=rule.event_type or "change_detected",
        target_selector_id=rule.target_selector_id,
        ai_session_id=None,
        workflow_id=rule.workflow_id,
        webhook_trigger_id=rule.webhook_trigger_id,
        name=rule.name,
        description=rule.description,
        enabled=rule.enabled,
        priority=rule.priority,
        conditions=rule.conditions,
        actions=rule.actions or [],
        blocks=rule.blocks,
        last_triggered_at=rule.last_triggered_at.isoformat() if rule.last_triggered_at else None,
        trigger_count=rule.trigger_count or 0,
        created_at=rule.created_at.isoformat() if rule.created_at else None,
        updated_at=rule.updated_at.isoformat() if rule.updated_at else None,
    )
