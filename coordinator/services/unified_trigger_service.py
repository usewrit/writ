"""
Unified Trigger Service - evaluates TriggerRule conditions and dispatches actions.

This service:
1. Runs extractors on selector content to produce structured data
2. Evaluates trigger conditions against extracted data
3. Dispatches actions (notifications, AI sessions, workflows) when conditions match
4. Processes block chains for visual workflow builder triggers
"""
import re
import json
import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from models.trigger_rule import TriggerRule, TriggerExecution
from models.target_selector import TargetSelector
from models.selector_extractor import SelectorExtractor
from models.detected_change import DetectedChange
from models.target import Target
from services.extractor import extractor_service
from security.validation import InputValidator

logger = logging.getLogger(__name__)


# Block type constants
BLOCK_TYPE_EVENT = 'event'
BLOCK_TYPE_CONDITION = 'condition'
BLOCK_TYPE_ACTION = 'action'

# Template transforms — bound the `match` filter's pattern (ReDoS guard, mirrors condition regexes).
_MAX_TEMPLATE_REGEX_LEN = 512


def _to_render_str(value: Any) -> str:
    """Stringify a resolved placeholder value the way the daemon does (objects → JSON)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _fmt_num(n: float) -> str:
    """Integers without a decimal point; fractionals trimmed of trailing zeros."""
    if float(n).is_integer() and abs(n) < 1e15:
        return str(int(n))
    return f"{n:.6f}".rstrip("0").rstrip(".")


def _apply_template_filter(value: str, spec: str) -> str:
    """Apply one `name:arg` transform to an already-stringified value. Kept in sync with the
    desktop daemon's `flow.rs::apply_filter`. Unknown filters pass the value through."""
    name, _, arg = spec.partition(":")
    name = name.strip().lower()
    if name in ("default", "or"):
        return arg if value.strip() == "" else value
    if name == "upper":
        return value.upper()
    if name == "lower":
        return value.lower()
    if name == "trim":
        return value.strip()
    if name == "truncate":
        try:
            return value[: int(arg.strip())]
        except ValueError:
            return value
    if name == "replace":
        a, _, b = arg.partition(":")
        return value.replace(a, b)
    if name == "round":
        try:
            n = float(value.strip())
        except ValueError:
            return value
        try:
            digits = int(arg.strip()) if arg.strip() else 0
        except ValueError:
            digits = 0
        return str(int(round(n))) if digits <= 0 else f"{n:.{digits}f}"
    if name in ("add", "sub", "mul", "div"):
        try:
            a = float(value.strip())
            b = float(arg.strip())
        except ValueError:
            return value
        if name == "add":
            r = a + b
        elif name == "sub":
            r = a - b
        elif name == "mul":
            r = a * b
        else:
            r = a / b if b != 0 else a
        return _fmt_num(r)
    if name == "match":
        if len(arg) > _MAX_TEMPLATE_REGEX_LEN:
            return ""
        try:
            m = re.search(arg, value)
        except re.error:
            return ""
        if not m:
            return ""
        return m.group(1) if m.groups() else m.group(0)
    return value


class UnifiedTriggerService:
    """
    Evaluates unified trigger rules and dispatches actions (async version).
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    async def process_change(
        self,
        target: Target,
        selector: Optional[TargetSelector],
        detected_change: DetectedChange,
        content: str,
        change_context: Dict[str, Any],
        fired_triggers: Optional[set] = None  # For deduplication across multiple selectors
    ) -> List[TriggerExecution]:
        """
        Process a detected change through the trigger system.

        1. Run extractors on content to get structured data
        2. Evaluate all matching trigger rules
        3. Dispatch actions for matching triggers
        4. Record executions

        Args:
            target: The target that changed
            selector: The specific selector that detected the change (may be None)
            detected_change: The detected change record
            content: The raw content (text or HTML)
            change_context: Additional context about the change
            fired_triggers: Optional set to track already-fired (trigger_id, target_id) pairs
                           Used to prevent duplicate fires when multiple selectors detect changes

        Returns:
            List of TriggerExecution records for dispatched triggers
        """
        executions = []
        if fired_triggers is None:
            fired_triggers = set()

        # Step 1: Run extractors if selector has them
        extracted_data = {}
        if selector and selector.extractors:
            content_type = selector.content_type or 'text'
            extractor_configs = [e.to_dict() for e in selector.extractors if e.enabled]
            if extractor_configs:
                extracted_data = extractor_service.extract_all(
                    content=content,
                    extractors=extractor_configs,
                    content_type=content_type
                )
                logger.info(f"[UnifiedTrigger] Extracted data for selector {selector.id}: {extracted_data}")

        # Step 2: Get matching triggers
        triggers = await self._get_applicable_triggers(target, selector)
        if not triggers:
            logger.info(f"[UnifiedTrigger] No enabled triggers found for target {target.id} (selector_id={selector.id if selector else None})")
            return []

        logger.info(f"[UnifiedTrigger] Evaluating {len(triggers)} triggers for target {target.id}")

        # Build evaluation context with comprehensive placeholders
        now = datetime.now(timezone.utc)
        eval_context = {
            **change_context,
            "extracted": extracted_data,
            "content": content,
            # Selector info for multi-selector targets
            "selector_id": selector.id if selector else None,
            "selector_name": selector.name if selector else None,
            "selector": selector.selector if selector else (target.selector or 'Full page'),
            # Timestamp placeholders
            "now": now.isoformat(),
            "now_date": now.strftime("%Y-%m-%d"),
            "now_time": now.strftime("%H:%M:%S"),
            "now_datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
            "now_timestamp": int(now.timestamp()),
            "change_detected_at": now.isoformat(),
            "change_detected_date": now.strftime("%Y-%m-%d"),
            "change_detected_time": now.strftime("%H:%M:%S"),
            # Target info
            "target_id": target.id,
            "target_name": target.name if hasattr(target, 'name') else None,
            "url": target.url,
        }

        # Step 3: Evaluate each trigger
        for trigger in triggers:
            # Deduplication: Skip triggers that have no selector_id and already fired for this target
            # This prevents the same trigger from firing multiple times when multiple selectors detect changes
            if not trigger.target_selector_id:
                dedup_key = (trigger.id, target.id)
                if dedup_key in fired_triggers:
                    logger.info(f"[UnifiedTrigger] Trigger {trigger.id} ({trigger.name}): SKIPPED - already fired for target {target.id} in this batch")
                    continue

            match, reason = self._evaluate_conditions(trigger, detected_change, eval_context)

            if not match:
                logger.debug(f"[UnifiedTrigger] Trigger {trigger.id} ({trigger.name}): conditions not met - {reason}")
                continue

            # Mark as fired for deduplication
            if not trigger.target_selector_id:
                fired_triggers.add((trigger.id, target.id))

            logger.info(f"[UnifiedTrigger] Trigger {trigger.id} ({trigger.name}): MATCHED - {reason}")

            # Create execution record
            execution = TriggerExecution(
                trigger_rule_id=trigger.id,
                detected_change_id=detected_change.id if detected_change else None,
                status="pending",
                trigger_context={
                    "extracted_data": extracted_data,
                    "change_context": {
                        k: v for k, v in change_context.items()
                        if k not in ['new_content']  # Don't store full content
                    },
                    "match_reason": reason,
                }
            )
            self.db.add(execution)

            # Dispatch actions
            try:
                action_results = await self._dispatch_actions(trigger, eval_context, execution)
                execution.action_results = action_results
                execution.status = "completed"
                execution.completed_at = datetime.now(timezone.utc)

                # Update trigger stats
                trigger.trigger_count = (trigger.trigger_count or 0) + 1
                trigger.last_triggered_at = datetime.now(timezone.utc)

            except Exception as e:
                logger.error(f"[UnifiedTrigger] Error dispatching actions for trigger {trigger.id}: {e}")
                execution.status = "failed"
                execution.error_message = str(e)
                execution.completed_at = datetime.now(timezone.utc)

            executions.append(execution)

        await self.db.commit()
        return executions

    async def process_monitor_health(
        self,
        target: Target,
        event_type: str,  # 'monitor_down' | 'monitor_stale' | 'monitor_recovered'
        health_context: Optional[Dict[str, Any]] = None,
    ) -> List[TriggerExecution]:
        """Dispatch automations that react to a monitor's HEALTH (not its content).

        Unlike `process_change` (selector/content-diff driven), health events fire
        on the edge between reachable and unreachable/stale — see
        services.monitoring_state._health_transition / sweep_stale. A rule matches
        when it listens for `event_type` on this target (via the target_id column
        OR a health event block in its block tree), scoped to the owner.
        """
        health_context = health_context or {}
        triggers = await self._get_health_triggers(target, event_type)
        if not triggers:
            return []

        logger.info(
            f"[UnifiedTrigger] {event_type} on target {target.id}: "
            f"evaluating {len(triggers)} trigger(s)"
        )

        now = datetime.now(timezone.utc)
        eval_context = {
            **health_context,
            "event_type": event_type,
            # State the monitor is now in (down|stale|ok) — down/stale for the
            # unhealthy edge, ok for recovery.
            "state": health_context.get("state")
            or ("ok" if event_type == "monitor_recovered" else event_type.replace("monitor_", "")),
            "target_id": target.id,
            "target_name": target.name if hasattr(target, "name") else None,
            "url": target.url,
            "checked_at": health_context.get("checked_at") or now.isoformat(),
            "now": now.isoformat(),
            "now_date": now.strftime("%Y-%m-%d"),
            "now_time": now.strftime("%H:%M:%S"),
            "now_datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
            "now_timestamp": int(now.timestamp()),
        }

        executions: List[TriggerExecution] = []
        for trigger in triggers:
            # Guardrails (cooldown / max_fires) apply to health events too so a
            # flapping monitor can't spam actions.
            ok, reason = self._check_guardrails(trigger, trigger.conditions or {})
            if not ok:
                logger.info(f"[UnifiedTrigger] Trigger {trigger.id} ({trigger.name}): {reason}")
                continue

            execution = TriggerExecution(
                trigger_rule_id=trigger.id,
                status="pending",
                trigger_context={
                    "event_type": event_type,
                    "target_id": target.id,
                    "health_context": health_context,
                },
            )
            self.db.add(execution)

            try:
                action_results = await self._dispatch_actions(trigger, eval_context, execution)
                execution.action_results = action_results
                execution.status = "completed"
                execution.completed_at = datetime.now(timezone.utc)
                trigger.trigger_count = (trigger.trigger_count or 0) + 1
                trigger.last_triggered_at = datetime.now(timezone.utc)
            except Exception as e:
                logger.error(
                    f"[UnifiedTrigger] Error dispatching {event_type} actions for "
                    f"trigger {trigger.id}: {e}"
                )
                execution.status = "failed"
                execution.error_message = str(e)
                execution.completed_at = datetime.now(timezone.utc)

            executions.append(execution)

        await self.db.commit()
        return executions

    async def _get_health_triggers(
        self, target: Target, event_type: str
    ) -> List[TriggerRule]:
        """Enabled monitor-health triggers for this target + event, owner-scoped.

        Matches the target_id COLUMN path (single-monitor automations) plus any
        rule whose block tree pins this target on a health event block (mirrors the
        multi-monitor block-tree matching used for change_detected)."""
        from sqlalchemy import or_

        result = await self.db.execute(
            select(TriggerRule).where(
                TriggerRule.enabled == True,  # noqa: E712
                TriggerRule.event_type == event_type,
            ).order_by(TriggerRule.priority.desc())
        )
        rules = result.scalars().all()

        matched: List[TriggerRule] = []
        seen = set()
        for r in rules:
            if r.target_id == target.id:
                matched.append(r)
                seen.add(r.id)
        # Block-tree matches (2+ monitors watched by one automation).
        for r in rules:
            if r.id in seen:
                continue
            if self._block_references_health_target(r, target.id, event_type):
                matched.append(r)
                seen.add(r.id)
        return matched

    @staticmethod
    def _block_references_health_target(
        rule: TriggerRule, target_id: int, event_type: str
    ) -> bool:
        """True iff the rule's block tree has an EVENT block of `event_type` whose
        config binds `target_id` (multi-monitor health automations)."""
        blocks = rule.blocks
        if not isinstance(blocks, list):
            return False
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") != BLOCK_TYPE_EVENT:
                continue
            if (block.get("blockType") or block.get("block_type")) != event_type:
                continue
            cfg = block.get("config") or {}
            if isinstance(cfg, dict) and cfg.get("target_id") == target_id:
                return True
        return False

    async def process_ai_session_event(
        self,
        event_type: str,  # 'ai_session_started' or 'ai_session_completed'
        session_id: int,
        session_name: str,
        target_id: Optional[int],
        status: str,
        error: Optional[str] = None,
        steps_taken: int = 0,
        duration_ms: int = 0,
        result_data: Optional[Dict[str, Any]] = None,
        inherited_context: Optional[Dict[str, Any]] = None,
        run_tenant_id: Optional[UUID] = None,
    ) -> List[TriggerExecution]:
        """Process an AI session lifecycle event through the trigger system.

        ``run_tenant_id`` is accepted for call-site compatibility and ignored;
        all triggers belong to the single owner."""
        logger.info(f"[UnifiedTrigger] Processing {event_type} for session {session_id} ({session_name})")
        executions = []

        # Build event context with placeholders
        # Start with inherited context so original data is available for chained triggers
        context = {}
        if inherited_context:
            context.update(inherited_context)
            # Preserve original extracted data under 'extracted' key
            if "extracted_data" in inherited_context:
                context["extracted"] = inherited_context["extracted_data"]

        # Add/override with current event data
        now = datetime.now(timezone.utc)
        context.update({
            "session_id": session_id,
            "session_name": session_name,
            "target_id": target_id,
            "status": status,
            "error": error or "",
            "steps_taken": steps_taken,
            "duration_ms": duration_ms,
            "duration_seconds": duration_ms / 1000 if duration_ms else 0,
            "success": status == "completed",
            "result": result_data or {},
            # Timestamp placeholders
            "now": now.isoformat(),
            "now_date": now.strftime("%Y-%m-%d"),
            "now_time": now.strftime("%H:%M:%S"),
            "now_datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
            "now_timestamp": int(now.timestamp()),
            "session_completed_at": now.isoformat() if event_type == "ai_session_completed" else None,
            "session_started_at": now.isoformat() if event_type == "ai_session_started" else None,
            "session_duration_seconds": duration_ms / 1000 if duration_ms else 0,
            "session_steps_taken": steps_taken,
            "session_status": status,
            "session_error": error or "",
        })

        # Get applicable triggers (owner-scoped — §3b isolation)
        triggers = await self._get_triggers_for_event(
            event_type=event_type,
            target_id=target_id,
            ai_session_id=session_id,
            run_tenant_id=run_tenant_id,
        )

        if triggers:
            logger.info(f"[UnifiedTrigger] Evaluating {len(triggers)} triggers for {event_type} on session {session_id}")

        for trigger in triggers:
            match, reason = self._evaluate_conditions(trigger, None, context)

            if not match:
                logger.debug(f"[UnifiedTrigger] Trigger {trigger.id} ({trigger.name}): conditions not met - {reason}")
                continue

            logger.info(f"[UnifiedTrigger] Trigger {trigger.id} ({trigger.name}): MATCHED - {reason}")

            # Create execution record
            execution = TriggerExecution(
                trigger_rule_id=trigger.id,
                status="pending",
                trigger_context={
                    "event_type": event_type,
                    "session_id": session_id,
                    "session_name": session_name,
                    "status": status,
                    "match_reason": reason,
                }
            )
            self.db.add(execution)

            # Dispatch actions
            try:
                action_results = await self._dispatch_actions(trigger, context, execution)
                execution.action_results = action_results
                execution.status = "completed"
                execution.completed_at = datetime.now(timezone.utc)

                trigger.trigger_count = (trigger.trigger_count or 0) + 1
                trigger.last_triggered_at = datetime.now(timezone.utc)

            except Exception as e:
                logger.error(f"[UnifiedTrigger] Error dispatching actions for trigger {trigger.id}: {e}")
                execution.status = "failed"
                execution.error_message = str(e)
                execution.completed_at = datetime.now(timezone.utc)

            executions.append(execution)

        # Also check for pending block chain continuations from previous triggers
        await self._process_pending_continuations(
            event_type=event_type,
            event_context=context,
            filter_key="ai_session_id",
            filter_value=session_id
        )

        await self.db.commit()
        return executions

    async def process_workflow_event(
        self,
        event_type: str,  # 'workflow_started' or 'workflow_completed'
        workflow_id: int,
        workflow_name: str,
        task_id: int,
        target_id: Optional[int],
        status: str,
        error: Optional[str] = None,
        steps_completed: int = 0,
        duration_ms: int = 0,
        result_data: Optional[Dict[str, Any]] = None,
        inherited_context: Optional[Dict[str, Any]] = None,
        consecutive_failures: int = 0,
        total_failure_count: int = 0,
        total_run_count: int = 0,
        run_tenant_id: Optional[UUID] = None,
    ) -> List[TriggerExecution]:
        """
        Process a workflow lifecycle event through the trigger system.

        ``run_tenant_id`` is accepted for call-site compatibility and ignored;
        all triggers belong to the single owner.

        Args:
            event_type: 'workflow_started' or 'workflow_completed'
            workflow_id: ID of the workflow
            workflow_name: Name of the workflow
            task_id: ID of the automation task
            target_id: Optional target ID
            status: Task status (running, success, failed, timeout)
            error: Error message if failed
            steps_completed: Number of steps completed
            duration_ms: Duration in milliseconds
            result_data: Any result data from the workflow
            inherited_context: Context inherited from the original triggering event
                              (e.g., extracted data from the change that started the workflow)

        Returns:
            List of TriggerExecution records for dispatched triggers
        """
        logger.info(f"[UnifiedTrigger] process_workflow_event called: event={event_type}, workflow_id={workflow_id}, workflow_name={workflow_name}, task_id={task_id}, status={status}")
        executions = []

        # Build event context with placeholders
        # Start with inherited context so original data is available for chained triggers
        context = {}
        if inherited_context:
            context.update(inherited_context)
            # Preserve original extracted data under 'extracted' key
            if "extracted_data" in inherited_context:
                context["extracted"] = inherited_context["extracted_data"]

        # Add/override with current event data
        now = datetime.now(timezone.utc)
        context.update({
            "workflow_id": workflow_id,
            "workflow_name": workflow_name,
            "task_id": task_id,
            "target_id": target_id,
            "status": status,
            "error": error or "",
            "steps_completed": steps_completed,
            "duration_ms": duration_ms,
            "duration_seconds": duration_ms / 1000 if duration_ms else 0,
            "success": status == "success",
            "result": result_data or {},
            # Timestamp placeholders
            "now": now.isoformat(),
            "now_date": now.strftime("%Y-%m-%d"),
            "now_time": now.strftime("%H:%M:%S"),
            "now_datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
            "now_timestamp": int(now.timestamp()),
            "workflow_completed_at": now.isoformat() if event_type == "workflow_completed" else None,
            "workflow_started_at": now.isoformat() if event_type == "workflow_started" else None,
            "workflow_duration_seconds": duration_ms / 1000 if duration_ms else 0,
            "workflow_steps_completed": steps_completed,
            "workflow_status": status,
            "workflow_error": error or "",
            # Failure tracking
            "consecutive_failures": consecutive_failures,
            "total_failure_count": total_failure_count,
            "total_run_count": total_run_count,
            "failure_rate": round((total_failure_count / total_run_count) * 100, 1) if total_run_count > 0 else 0,
        })

        # Get applicable triggers (owner-scoped — §3b isolation)
        triggers = await self._get_triggers_for_event(
            event_type=event_type,
            target_id=target_id,
            workflow_id=workflow_id,
            run_tenant_id=run_tenant_id,
        )

        # Always check for pending block chain continuations, even if no standalone triggers
        # This handles workflow_completed events from block chains
        await self._process_pending_continuations(
            event_type=event_type,
            event_context=context,
            filter_key="workflow_id",
            filter_value=workflow_id,
            task_id=task_id  # Pass task_id to match specific task
        )

        if not triggers:
            logger.debug(f"[UnifiedTrigger] No standalone triggers for {event_type} on workflow {workflow_id}")
            await self.db.commit()
            return []

        logger.info(f"[UnifiedTrigger] Evaluating {len(triggers)} triggers for {event_type} on workflow {workflow_id}")

        for trigger in triggers:
            match, reason = self._evaluate_conditions(trigger, None, context)

            if not match:
                logger.debug(f"[UnifiedTrigger] Trigger {trigger.id} ({trigger.name}): conditions not met - {reason}")
                continue

            logger.info(f"[UnifiedTrigger] Trigger {trigger.id} ({trigger.name}): MATCHED - {reason}")

            # Create execution record
            execution = TriggerExecution(
                trigger_rule_id=trigger.id,
                status="pending",
                trigger_context={
                    "event_type": event_type,
                    "workflow_id": workflow_id,
                    "workflow_name": workflow_name,
                    "task_id": task_id,
                    "status": status,
                    "match_reason": reason,
                }
            )
            self.db.add(execution)

            # Dispatch actions
            try:
                action_results = await self._dispatch_actions(trigger, context, execution)
                execution.action_results = action_results
                execution.status = "completed"
                execution.completed_at = datetime.now(timezone.utc)

                trigger.trigger_count = (trigger.trigger_count or 0) + 1
                trigger.last_triggered_at = datetime.now(timezone.utc)

            except Exception as e:
                logger.error(f"[UnifiedTrigger] Error dispatching actions for trigger {trigger.id}: {e}")
                execution.status = "failed"
                execution.error_message = str(e)
                execution.completed_at = datetime.now(timezone.utc)

            executions.append(execution)

        await self.db.commit()
        return executions

    async def process_crawl_event(
        self,
        event_type: str,  # 'crawl_started' | 'crawl_completed' | 'crawl_failed'
        crawl,  # models.crawl_job.CrawlJob
    ) -> List[TriggerExecution]:
        """Process a Dragnet crawl lifecycle event through the trigger system.

        A crawl converges asynchronously (the frontier drains across the fleet), so
        its terminal state — NOT the per-shard task completions — is the correct
        "done" signal. This is emitted from crawl_orchestrator._finalize
        (completed/failed) and _seed (started). An author writes one
        `crawl_completed` automation and filters with conditions on {{seed_host}} /
        {{crawl_name}} / {{status}}. Collected pages are read via the Workflow Data
        API keyed on {{data_workflow_id}}.
        """
        from urllib.parse import urlparse

        executions: List[TriggerExecution] = []
        now = datetime.now(timezone.utc)
        try:
            seed_host = urlparse(crawl.seed_url or "").netloc
        except Exception:
            seed_host = ""

        status = crawl.status
        context = {
            "event_type": event_type,
            "crawl_id": crawl.id,
            "crawl_name": crawl.name,
            "seed_url": crawl.seed_url,
            "seed_host": seed_host,
            "status": status,
            "success": status == "completed",
            "pages_done": crawl.pages_done or 0,
            "pages_failed": crawl.pages_failed or 0,
            "pages_discovered": crawl.pages_discovered or 0,
            "error": crawl.error or "",
            # The synthetic workflow the collected pages aggregate under — feed this
            # to a downstream 'workflow' action's input_mapping or the Workflow Data API.
            "data_workflow_id": crawl.workflow_id,
            "crawl_workflow_id": crawl.workflow_id,
            # Timestamp placeholders (parity with the other event builders)
            "now": now.isoformat(),
            "now_date": now.strftime("%Y-%m-%d"),
            "now_time": now.strftime("%H:%M:%S"),
            "now_datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
            "now_timestamp": int(now.timestamp()),
        }

        # Crawl events aren't entity-scoped, so no target_id/workflow_id filter —
        # every enabled crawl_* trigger is a candidate, then conditions narrow it.
        triggers = await self._get_triggers_for_event(event_type=event_type)

        # Resume any block chain waiting on this crawl (crawl action → … → continue
        # on crawl_completed). No-op until a crawl block registers a continuation.
        await self._process_pending_continuations(
            event_type=event_type,
            event_context=context,
            filter_key="crawl_id",
            filter_value=crawl.id,
        )

        if not triggers:
            logger.debug(f"[UnifiedTrigger] No triggers for {event_type} on crawl {crawl.id}")
            await self.db.commit()
            return []

        logger.info(f"[UnifiedTrigger] Evaluating {len(triggers)} triggers for {event_type} on crawl {crawl.id}")
        for trigger in triggers:
            match, reason = self._evaluate_conditions(trigger, None, context)
            if not match:
                continue
            logger.info(f"[UnifiedTrigger] Trigger {trigger.id} ({trigger.name}): MATCHED - {reason}")
            execution = TriggerExecution(
                trigger_rule_id=trigger.id,
                status="pending",
                trigger_context={
                    "event_type": event_type,
                    "crawl_id": crawl.id,
                    "data_workflow_id": crawl.workflow_id,
                    "status": status,
                    "match_reason": reason,
                },
            )
            self.db.add(execution)
            try:
                action_results = await self._dispatch_actions(trigger, context, execution)
                execution.action_results = action_results
                execution.status = "completed"
                execution.completed_at = datetime.now(timezone.utc)
                trigger.trigger_count = (trigger.trigger_count or 0) + 1
                trigger.last_triggered_at = datetime.now(timezone.utc)
            except Exception as e:
                logger.error(f"[UnifiedTrigger] Error dispatching actions for crawl trigger {trigger.id}: {e}")
                execution.status = "failed"
                execution.error_message = str(e)
                execution.completed_at = datetime.now(timezone.utc)
            executions.append(execution)

        await self.db.commit()
        return executions

    async def _process_pending_continuations(
        self,
        event_type: str,
        event_context: Dict[str, Any],
        filter_key: str,
        filter_value: int,
        task_id: Optional[int] = None  # Specific task ID for precise matching
    ) -> None:
        """
        Check for and process any pending block chain continuations that are waiting
        for this event type.

        The continuation data is stored in TriggerExecution.trigger_context when an AI session
        or workflow is dispatched as part of a block chain.

        Supports both single continuations and multi-branch continuations.
        Uses task_id for precise matching to prevent duplicate triggers.
        """
        logger.info(f"[BlockChain] Looking for pending continuations: event={event_type}, {filter_key}={filter_value}, task_id={task_id}")

        # Look for trigger executions with pending continuations matching this event
        result = await self.db.execute(
            select(TriggerExecution).where(
                TriggerExecution.trigger_context.isnot(None),
                TriggerExecution.status == 'completed'
            )
        )
        executions = result.scalars().all()
        logger.info(f"[BlockChain] Found {len(executions)} completed executions with trigger_context")

        for execution in executions:
            trigger_context = execution.trigger_context or {}
            pending = trigger_context.get('pending_continuation')

            if not pending:
                continue

            # Handle multi-branch continuations
            if pending.get('multi_branch'):
                continuations = pending.get('continuations', [])
                logger.info(f"[BlockChain] Execution {execution.id} has {len(continuations)} multi-branch continuations")

                matched_continuation = None
                matched_index = -1

                for idx, cont in enumerate(continuations):
                    cont_event = cont.get('continuation_event')
                    cont_filter = cont.get('continuation_filter', {})
                    cont_filter_id = cont_filter.get(filter_key)
                    cont_task_id = cont_filter.get('task_id')

                    logger.info(f"[BlockChain]   Branch {idx}: event={cont_event}, {filter_key}={cont_filter_id}, task_id={cont_task_id}")

                    # Match event type and filter_key (workflow_id or ai_session_id)
                    if cont_event != event_type:
                        continue
                    if cont_filter_id and cont_filter_id != filter_value:
                        continue
                    # If continuation has task_id filter, it MUST match
                    if cont_task_id and task_id and cont_task_id != task_id:
                        logger.info(f"[BlockChain]   Branch {idx}: task_id mismatch ({cont_task_id} != {task_id})")
                        continue

                    matched_continuation = cont
                    matched_index = idx
                    logger.info(f"[BlockChain]   Branch {idx} MATCHED!")
                    break

                if not matched_continuation:
                    logger.info(f"[BlockChain] No matching branch continuation found")
                    continue

                # Process the matched continuation
                trigger_rule_id = pending.get('trigger_rule_id')
                if not trigger_rule_id:
                    logger.warning(f"[BlockChain] No trigger_rule_id in multi-branch continuation")
                    continue

                # Run-failure RETRY for THIS branch: each parallel branch dispatched its own
                # workflow, so its retry budget lives in that branch's context. On a failed
                # completion with budget remaining, re-dispatch + re-point this branch's
                # continuation and leave it in the list (armed for the retried run).
                _branch_name = matched_continuation.get('branch_name')
                _branch_ctx = (pending.get('branch_contexts', {}) or {}).get(_branch_name, {}) or {}
                _branch_retry_meta = _branch_ctx.get('_wf_retry_meta')
                if (
                    event_type == 'workflow_completed'
                    and _branch_retry_meta
                    and int(_branch_retry_meta.get('budget', 0)) > 0
                    and self._completion_failed(event_context)
                ):
                    _retry_ctx = {**(pending.get('context_snapshot', {}) or {}), **_branch_ctx}
                    if await self._retry_workflow_continuation(trigger_rule_id, matched_continuation, _branch_retry_meta, _retry_ctx):
                        from sqlalchemy.orm.attributes import flag_modified
                        flag_modified(execution, 'trigger_context')
                        logger.info(
                            f"[BlockChain] Re-dispatched failed branch '{_branch_name}' workflow for "
                            f"execution {execution.id}; {_branch_retry_meta.get('budget')} retries left"
                        )
                        continue  # branch stays in `continuations`, re-armed for the retried run

                # Add shared context to the matched continuation
                matched_continuation['context_snapshot'] = pending.get('context_snapshot', {})
                matched_continuation['branch_contexts'] = pending.get('branch_contexts', {})

                results = await self.process_block_continuation(
                    trigger_rule_id=trigger_rule_id,
                    event_type=event_type,
                    event_context=event_context,
                    continuation_data=matched_continuation
                )

                # Remove the processed continuation from the list
                remaining_continuations = [c for i, c in enumerate(continuations) if i != matched_index]

                if remaining_continuations:
                    # Still have other branches waiting - update the pending continuation
                    execution.trigger_context['pending_continuation']['continuations'] = remaining_continuations
                    logger.info(f"[BlockChain] {len(remaining_continuations)} branch continuations still pending")
                else:
                    # All branches processed - clear the pending continuation
                    execution.trigger_context['pending_continuation'] = None

                execution.trigger_context['continuation_processed'] = execution.trigger_context.get('continuation_processed', [])
                if not isinstance(execution.trigger_context['continuation_processed'], list):
                    execution.trigger_context['continuation_processed'] = []
                execution.trigger_context['continuation_processed'].append({
                    'event_type': event_type,
                    'branch': matched_continuation.get('branch_name'),
                    'processed_at': datetime.now(timezone.utc).isoformat(),
                    'results': results,
                })

                from sqlalchemy.orm.attributes import flag_modified
                flag_modified(execution, 'trigger_context')
                logger.info(f"[BlockChain] Multi-branch continuation processed for execution {execution.id}: {len(results)} results")
                continue

            # Handle single continuation (original logic)
            continuation_event = pending.get('continuation_event')
            logger.info(f"[BlockChain] Execution {execution.id} has pending continuation for {continuation_event}")

            if continuation_event != event_type:
                logger.info(f"[BlockChain] Skipping - event type mismatch: {continuation_event} != {event_type}")
                continue

            # Check filter match (e.g., specific ai_session_id or workflow_id)
            continuation_filter = pending.get('continuation_filter', {})
            filter_id = continuation_filter.get(filter_key)
            cont_task_id = continuation_filter.get('task_id')
            logger.info(f"[BlockChain] Filter check: {filter_key}={filter_id}, task_id={cont_task_id}, need {filter_value}/{task_id}")

            # If filter specifies a specific ID, it must match
            if filter_id and filter_id != filter_value:
                logger.info(f"[BlockChain] Skipping - filter mismatch")
                continue

            # If continuation has task_id filter, it MUST match
            if cont_task_id and task_id and cont_task_id != task_id:
                logger.info(f"[BlockChain] Skipping - task_id mismatch ({cont_task_id} != {task_id})")
                continue

            # Match found - process the continuation
            logger.info(f"[BlockChain] Processing continuation for execution {execution.id}, event {event_type}")

            trigger_rule_id = trigger_context.get('trigger_rule_id')
            if not trigger_rule_id:
                logger.warning(f"[BlockChain] No trigger_rule_id in continuation for execution {execution.id}")
                continue

            # Run-failure RETRY (parity with desktop's inline retry): when the workflow reported
            # FAILURE and a retry budget remains (carried in context_snapshot by the workflow
            # action), re-dispatch the workflow and re-point THIS continuation at the new task
            # instead of proceeding down the chain.
            retry_meta = (pending.get('context_snapshot', {}) or {}).get('_wf_retry_meta')
            if (
                event_type == 'workflow_completed'
                and retry_meta
                and int(retry_meta.get('budget', 0)) > 0
                and self._completion_failed(event_context)
            ):
                if await self._retry_workflow_continuation(trigger_rule_id, pending, retry_meta, pending.get('context_snapshot', {}) or {}):
                    from sqlalchemy.orm.attributes import flag_modified
                    flag_modified(execution, 'trigger_context')
                    logger.info(
                        f"[BlockChain] Re-dispatched failed workflow for execution {execution.id}; "
                        f"{retry_meta.get('budget')} retries left"
                    )
                    continue  # keep the continuation armed for the retried run's completion

            results = await self.process_block_continuation(
                trigger_rule_id=trigger_rule_id,
                event_type=event_type,
                event_context=event_context,
                continuation_data=pending
            )

            # Clear the pending continuation from the execution
            execution.trigger_context['pending_continuation'] = None
            execution.trigger_context['continuation_processed'] = {
                'event_type': event_type,
                'processed_at': datetime.now(timezone.utc).isoformat(),
                'results': results,
            }
            # Mark trigger_context as modified for SQLAlchemy
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(execution, 'trigger_context')

            logger.info(f"[BlockChain] Continuation processed for execution {execution.id}: {len(results)} results")

    @staticmethod
    def _completion_failed(event_context: Dict[str, Any]) -> bool:
        """Whether a workflow/ai-session completion event represents a FAILED run."""
        if event_context.get('success') is False:
            return True
        status = str(event_context.get('status') or '').lower()
        return status in ('failed', 'error')

    async def _retry_workflow_continuation(
        self,
        trigger_rule_id: int,
        cont: Dict[str, Any],
        retry_meta: Dict[str, Any],
        ctx: Dict[str, Any],
    ) -> bool:
        """Re-dispatch a failed workflow and re-point continuation `cont` at the new task, consuming
        one unit of the retry budget. Returns True when re-dispatched (caller keeps the continuation
        armed), False to fall through to the normal (failed) continuation processing.

        Works for BOTH the single pending continuation and a single branch entry of a multi-branch
        continuation. `cont` and `retry_meta` are live references into `execution.trigger_context`,
        so the mutations here (task_id filter + decremented budget) are persisted by the caller's
        flag_modified. `ctx` is the context the retried run's inputs are rendered against."""
        trig = (await self.db.execute(
            select(TriggerRule).where(TriggerRule.id == trigger_rule_id)
        )).scalar_one_or_none()
        if not trig:
            return False
        config = retry_meta.get('config', {}) or {}
        try:
            backoff_ms = int(retry_meta.get('backoff_ms', 0) or 0)
        except (TypeError, ValueError):
            backoff_ms = 0
        try:
            if backoff_ms > 0:
                await asyncio.sleep(backoff_ms / 1000.0)
            dispatch_result = await self._dispatch_workflow(config, ctx, trig)
        except Exception as e:
            logger.error(f"[BlockChain] Workflow retry re-dispatch failed: {e}")
            return False
        new_task_id = dispatch_result.get('task_id')
        if not new_task_id:
            return False
        # Consume one unit of budget (mutated in place → persisted) and re-point at the retried run.
        retry_meta['budget'] = int(retry_meta.get('budget', 0)) - 1
        filt = cont.setdefault('continuation_filter', {})
        filt['task_id'] = new_task_id
        if dispatch_result.get('workflow_id'):
            filt['workflow_id'] = dispatch_result.get('workflow_id')
        return True

    async def _get_triggers_for_event(
        self,
        event_type: str,
        target_id: Optional[int] = None,
        ai_session_id: Optional[int] = None,
        workflow_id: Optional[int] = None,
        run_tenant_id: Optional[UUID] = None,
    ) -> List[TriggerRule]:
        """Get all enabled triggers for a specific event type.

        All triggers belong to the single owner. ``run_tenant_id`` is accepted for
        call-site compatibility and ignored.
        """
        from sqlalchemy import or_

        conditions = [
            TriggerRule.event_type == event_type,
            TriggerRule.enabled == True
        ]

        # Build OR conditions for matching triggers
        or_conditions = []

        # Global triggers (no specific entity).
        # ai_session_* event types cannot be scoped by session id.
        if event_type.startswith("workflow"):
            or_conditions.append(TriggerRule.workflow_id.is_(None))
            if workflow_id:
                or_conditions.append(TriggerRule.workflow_id == workflow_id)

        # Also filter by target if provided
        if target_id:
            conditions.append(
                or_(
                    TriggerRule.target_id.is_(None),
                    TriggerRule.target_id == target_id
                )
            )

        if or_conditions:
            conditions.append(or_(*or_conditions))

        result = await self.db.execute(
            select(TriggerRule).where(*conditions).order_by(TriggerRule.priority.desc())
        )
        return result.scalars().all()

    @staticmethod
    def _block_references_target(
        rule: TriggerRule,
        target_id: int,
        selector_id: Optional[int],
    ) -> bool:
        """True iff the rule's block tree has a `change_detected` EVENT block whose
        config references `target_id` (MULTI-MONITOR support, plan §4/§11).

        The scheduler wakes a rule when the changed target equals the rule's
        `target_id` COLUMN, but a block-tree automation can watch 2+ pages via
        several `change_detected` blocks; only the first slot's target lives in the
        column, so the other blocks would never fire. This inspects the data-carrying
        `blocks` so a change on ANY watched target wakes the rule.

        Selector scoping mirrors the column path: a block that pins a `selector_id`
        only matches a change on THAT selector; a block with no selector matches any
        change on its target (including full-page / no-selector changes)."""
        blocks = rule.blocks
        if not isinstance(blocks, list):
            return False
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") != BLOCK_TYPE_EVENT:
                continue
            if (block.get("blockType") or block.get("block_type")) != "change_detected":
                continue
            cfg = block.get("config") or {}
            if not isinstance(cfg, dict):
                continue
            if cfg.get("target_id") != target_id:
                continue
            block_sel = cfg.get("selector_id")
            if block_sel is None:
                return True  # block watches the whole target → any change matches
            if selector_id is not None and block_sel == selector_id:
                return True
        return False

    async def _get_applicable_triggers(
        self,
        target: Target,
        selector: Optional[TargetSelector]
    ) -> List[TriggerRule]:
        """Get all enabled triggers that apply to this target/selector for change_detected events."""
        # First get all triggers for this target to debug
        all_result = await self.db.execute(
            select(TriggerRule).where(TriggerRule.target_id == target.id)
        )
        all_triggers = all_result.scalars().all()
        logger.info(f"[UnifiedTrigger] Target {target.id} has {len(all_triggers)} total triggers")
        for t in all_triggers:
            logger.info(f"[UnifiedTrigger]   - Trigger {t.id} '{t.name}': enabled={t.enabled}, event_type={t.event_type}, selector_id={t.target_selector_id}")

        result = await self.db.execute(
            select(TriggerRule).where(
                TriggerRule.target_id == target.id,
                TriggerRule.enabled == True,
                TriggerRule.event_type == "change_detected"
            ).order_by(TriggerRule.priority.desc())
        )
        triggers = result.scalars().all()
        logger.info(f"[UnifiedTrigger] Found {len(triggers)} enabled change_detected triggers for target {target.id}")

        # Filter by selector if trigger specifies one
        if selector:
            logger.info(f"[UnifiedTrigger] Filtering for selector_id={selector.id}")
            triggers = [
                t for t in triggers
                if t.target_selector_id is None or t.target_selector_id == selector.id
            ]
        else:
            # No selector, only return triggers without selector restriction
            triggers = [t for t in triggers if t.target_selector_id is None]

        logger.info(f"[UnifiedTrigger] After selector filtering: {len(triggers)} triggers")

        # MULTI-MONITOR (plan §4/§11): ALSO wake enabled change_detected rules whose
        # block tree references this target in a `change_detected` block, even when the
        # rule's target_id COLUMN points at a DIFFERENT (first) slot. A block-tree
        # automation with 2+ change_detected blocks otherwise only fires on the one
        # target that owns the column. Candidates are scoped to the single owner and the
        # changed target and the block target/selector is matched in Python (rule
        # counts are small; no fragile JSONB predicate).
        # Rules already matched via the column are excluded here, so a native
        # single-monitor automation (column set, one block) is UNCHANGED and fires once.
        seen_ids = {t.id for t in triggers}
        selector_id = selector.id if selector else None
        block_conditions = [
            TriggerRule.enabled == True,
            TriggerRule.event_type == "change_detected",
            TriggerRule.blocks.isnot(None),
        ]
        block_result = await self.db.execute(
            select(TriggerRule).where(*block_conditions).order_by(TriggerRule.priority.desc())
        )
        for t in block_result.scalars().all():
            if t.id in seen_ids:
                continue  # already matched via the target_id column (dedupe)
            if self._block_references_target(t, target.id, selector_id):
                logger.info(
                    f"[UnifiedTrigger] Trigger {t.id} ('{t.name}') matched via block-tree "
                    f"change_detected block for target {target.id} (column target_id={t.target_id})"
                )
                triggers.append(t)
                seen_ids.add(t.id)

        return triggers

    def _evaluate_conditions(
        self,
        trigger: TriggerRule,
        change: DetectedChange,
        context: Dict[str, Any]
    ) -> Tuple[bool, str]:
        """
        Evaluate all conditions for a trigger.

        Returns:
            Tuple of (matched, reason)
        """
        conditions = trigger.conditions or {}

        # Guardrails are enforced even when there are no data conditions
        # (e.g. block-based triggers keep their real conditions inside `blocks`).
        ok, reason = self._check_guardrails(trigger, conditions)
        if not ok:
            return False, reason

        if not conditions:
            return True, "no conditions (always match)"

        # Track matched conditions for logging
        matched = []
        failed = []

        for condition_key, condition_spec in conditions.items():
            # Guardrail keys are handled by _check_guardrails, not as data conditions
            if condition_key == "max_fires":
                continue

            # Handle schedule conditions separately
            if condition_key == "schedule":
                ok, reason = self._evaluate_schedule(condition_spec)
                if not ok:
                    failed.append(f"schedule: {reason}")
                else:
                    matched.append("schedule")
                continue

            # Handle extracted data conditions (extracted.price, extracted.url, etc.)
            if condition_key.startswith("extracted."):
                field_name = condition_key.split(".", 1)[1]
                extracted = context.get("extracted", {})
                value = extracted.get(field_name)
                ok, reason = self._evaluate_condition(field_name, value, condition_spec)
                if not ok:
                    failed.append(f"{condition_key}: {reason}")
                else:
                    matched.append(condition_key)
                continue

            # Handle change metadata conditions
            if condition_key == "content":
                value = context.get("content", "")
            elif condition_key == "diff_size":
                value = len(change.diff_snippet or "") if change else 0
            elif condition_key == "diff_snippet":
                value = change.diff_snippet if change else ""
            else:
                # Unknown condition, skip
                logger.warning(f"[UnifiedTrigger] Unknown condition key: {condition_key}")
                continue

            ok, reason = self._evaluate_condition(condition_key, value, condition_spec)
            if not ok:
                failed.append(f"{condition_key}: {reason}")
            else:
                matched.append(condition_key)

        if failed:
            return False, f"failed: {', '.join(failed)}"

        return True, f"matched: {', '.join(matched) if matched else 'all'}"

    def _check_guardrails(
        self,
        trigger: TriggerRule,
        conditions: Dict[str, Any]
    ) -> Tuple[bool, str]:
        """
        Enforce firing guardrails before evaluating data conditions.

        These protect "act on every change" automations (e.g. restock auto-buy)
        from re-firing on every poll. Both are read from the trigger's
        `conditions` JSONB so they apply to legacy AND block-based triggers:

          - conditions.max_fires            -> stop (and auto-disable) after N fires
          - conditions.schedule.cooldown_minutes -> ignore fires within the window
        """
        # Fire limit: stop once the trigger has fired its allotted number of times.
        max_fires = conditions.get("max_fires")
        if isinstance(max_fires, int) and max_fires > 0:
            fired = trigger.trigger_count or 0
            if fired >= max_fires:
                # Auto-disable so the UI reflects that it's spent (committed by caller).
                if trigger.enabled:
                    trigger.enabled = False
                    logger.info(
                        f"[UnifiedTrigger] Trigger {trigger.id} reached max_fires "
                        f"({fired}/{max_fires}) - auto-disabling"
                    )
                return False, f"max_fires reached ({fired}/{max_fires})"

        # Cooldown: suppress fires that land inside the cooldown window.
        schedule = conditions.get("schedule") or {}
        cooldown_minutes = schedule.get("cooldown_minutes")
        if cooldown_minutes and trigger.last_triggered_at:
            last = trigger.last_triggered_at
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            elapsed_min = (datetime.now(timezone.utc) - last).total_seconds() / 60
            if elapsed_min < float(cooldown_minutes):
                return False, (
                    f"cooldown active ({elapsed_min:.1f}/{cooldown_minutes} min)"
                )

        return True, "guardrails ok"

    def _evaluate_condition(
        self,
        field_name: str,
        value: Any,
        spec: Dict[str, Any]
    ) -> Tuple[bool, str]:
        """
        Evaluate a single condition.

        Spec format:
        {
            "operator": "equals|not_equals|contains|not_contains|gt|gte|lt|lte|changed|matches",
            "value": <comparison_value>
        }
        """
        operator = spec.get("operator", "equals")
        compare_value = spec.get("value")

        # Special case: "changed" operator (just checks if value exists and is different)
        if operator == "changed":
            if value is not None and value != "":
                return True, "value present"
            return False, "no value"

        # "exists" -> value present and non-empty (offered by the condition UI)
        if operator == "exists":
            if value is not None and str(value).strip() != "":
                return True, "value exists"
            return False, "value missing/empty"

        # String comparisons
        if operator == "equals":
            if str(value).lower() == str(compare_value).lower():
                return True, f"equals '{compare_value}'"
            return False, f"'{value}' != '{compare_value}'"

        if operator == "not_equals":
            if str(value).lower() != str(compare_value).lower():
                return True, f"not equals '{compare_value}'"
            return False, f"'{value}' == '{compare_value}'"

        if operator == "contains":
            if compare_value.lower() in str(value).lower():
                return True, f"contains '{compare_value}'"
            return False, f"does not contain '{compare_value}'"

        if operator == "not_contains":
            if compare_value.lower() not in str(value).lower():
                return True, f"does not contain '{compare_value}'"
            return False, f"contains '{compare_value}'"

        if operator == "matches":
            pattern = compare_value
            # ReDoS defense: execute the user-supplied pattern under a hard
            # wall-clock timeout with a capped input length. validate=True
            # re-validates defensively (rejecting/aborting catastrophic patterns
            # even if the rule was persisted before validation existed). On an
            # invalid pattern or timeout this fails closed (no match).
            try:
                if InputValidator.safe_regex_search(
                    pattern, str(value), flags=re.IGNORECASE
                ):
                    return True, f"matches pattern '{pattern}'"
                return False, f"does not match pattern '{pattern}'"
            except HTTPException:
                logger.warning(
                    "Trigger 'matches' rejected unsafe regex pattern: %r", pattern
                )
                return False, f"invalid/unsafe pattern '{pattern}'"

        # Numeric comparisons
        try:
            num_value = float(value) if value is not None else 0
            num_compare = float(compare_value) if compare_value is not None else 0

            if operator == "gt":
                if num_value > num_compare:
                    return True, f"{num_value} > {num_compare}"
                return False, f"{num_value} <= {num_compare}"

            if operator == "gte":
                if num_value >= num_compare:
                    return True, f"{num_value} >= {num_compare}"
                return False, f"{num_value} < {num_compare}"

            if operator == "lt":
                if num_value < num_compare:
                    return True, f"{num_value} < {num_compare}"
                return False, f"{num_value} >= {num_compare}"

            if operator == "lte":
                if num_value <= num_compare:
                    return True, f"{num_value} <= {num_compare}"
                return False, f"{num_value} > {num_compare}"

        except (ValueError, TypeError):
            return False, f"cannot compare: {value} vs {compare_value}"

        return False, f"unknown operator: {operator}"

    def _resolve_field_value(self, field: str, ctx: Dict[str, Any]) -> Any:
        """
        Resolve a (possibly dotted) condition field against the evaluation context.

        Supports flat keys ("content", "success", "status") and nested paths
        ("extracted.price", "result.order_id", "payload.user.email"). Returns
        None when any segment is missing.
        """
        if not field:
            return None
        # Exact (flat) key first — covers top-level keys and any literal-dot keys.
        if field in ctx:
            return ctx[field]
        current: Any = ctx
        for part in field.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return None
        return current

    def _evaluate_single_condition(
        self,
        field: str,
        operator: str,
        value: Any,
        ctx: Dict[str, Any]
    ) -> Tuple[bool, str]:
        """
        Evaluate one condition block (visual builder) against the flow context.

        Resolves `field` from the context, then reuses the shared operator
        logic in `_evaluate_condition`.
        """
        resolved = self._resolve_field_value(field, ctx)
        return self._evaluate_condition(
            field, resolved, {"operator": operator, "value": value}
        )

    def _evaluate_schedule(self, schedule: Dict[str, Any]) -> Tuple[bool, str]:
        """Evaluate schedule constraints."""
        now = datetime.now()

        # Time window check
        if "time_window" in schedule:
            window = schedule["time_window"]
            start = datetime.strptime(window.get("start", "00:00"), "%H:%M").time()
            end = datetime.strptime(window.get("end", "23:59"), "%H:%M").time()
            current = now.time()

            if start <= end:
                if not (start <= current <= end):
                    return False, f"outside time window {start}-{end}"
            else:
                # Overnight window
                if not (current >= start or current <= end):
                    return False, f"outside overnight window {start}-{end}"

        # Day of week check (1=Monday, 7=Sunday)
        if "days_of_week" in schedule:
            today = now.isoweekday()
            allowed_days = schedule["days_of_week"]
            if today not in allowed_days:
                return False, f"today ({today}) not in allowed days"

        # Cooldown is handled at the trigger level (this schedule check has no
        # access to the trigger's last_triggered_at).

        return True, "schedule OK"

    async def _dispatch_actions(
        self,
        trigger: TriggerRule,
        context: Dict[str, Any],
        execution: TriggerExecution,
        blocking: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Dispatch all actions for a matched trigger.

        If the trigger has blocks (visual workflow builder format), process those.
        Otherwise, fall back to the legacy actions array.

        Args:
            blocking: If True, wait for continuations (workflow completion) inline
                      instead of storing them for async processing. Used by /api/v1/webhooks/ endpoint.

        Returns list of action results.
        """
        # Check if trigger uses new block format
        if trigger.blocks:
            results, continuation = await self._process_block_chain(
                trigger=trigger,
                blocks=trigger.blocks,
                context=context,
                start_index=0
            )

            # If there's a continuation (waiting for completion event)
            if continuation:
                if blocking:
                    # Blocking mode: poll the task and resume continuation inline
                    results = await self._wait_and_resume_continuation(
                        trigger, execution, continuation, context, results
                    )
                else:
                    # Async mode: store continuation for later processing
                    execution.trigger_context = execution.trigger_context or {}
                    execution.trigger_context["pending_continuation"] = continuation
                    execution.trigger_context["trigger_rule_id"] = trigger.id
                    from sqlalchemy.orm.attributes import flag_modified
                    flag_modified(execution, 'trigger_context')
                    if continuation.get('multi_branch'):
                        events = [c.get('continuation_event') for c in continuation.get('continuations', [])]
                        logger.info(f"[UnifiedTrigger] Block chain has {len(events)} pending branch continuations: {events}")
                    else:
                        logger.info(f"[UnifiedTrigger] Block chain has pending continuation: {continuation.get('continuation_event')}")

            return results

        # Legacy actions array processing
        actions = trigger.actions or []
        results = []

        for i, action in enumerate(actions):
            action_type = action.get("type")
            config = action.get("config", {})

            result = {
                "index": i,
                "type": action_type,
                "status": "pending",
            }

            try:
                if action_type == "notification":
                    result.update(await self._dispatch_notification(config, context, trigger))
                elif action_type == "ai_session":
                    result.update(await self._dispatch_ai_session(config, context, trigger))
                elif action_type == "workflow":
                    result.update(await self._dispatch_workflow(config, context, trigger))
                elif action_type == "crawl":
                    result.update(await self._dispatch_crawl(config, context, trigger))
                elif action_type == "create_persona":
                    result.update(await self._create_persona_action(config, context, trigger))
                else:
                    result["status"] = "skipped"
                    result["reason"] = f"unknown action type: {action_type}"

            except Exception as e:
                result["status"] = "failed"
                result["error"] = str(e)
                logger.error(f"[UnifiedTrigger] Action {action_type} failed: {e}")

            results.append(result)

        return results

    async def _dispatch_crawl(
        self,
        config: Dict[str, Any],
        context: Dict[str, Any],
        trigger: TriggerRule
    ) -> Dict[str, Any]:
        """Dispatch a 'crawl' action — kick off a Dragnet whole-site crawl in
        response to any event (a monitor change, a schedule, a webhook, another
        workflow finishing, …). The crawl runs asynchronously; its own
        `crawl_started`/`crawl_completed`/`crawl_failed` events (process_crawl_event)
        drive any downstream automation. Collected pages are read via the Workflow
        Data API keyed on the returned `data_workflow_id`.

        Seed resolution (matches the flow-builder input convention):
          1. An explicit `seed_url`/`url` in config, rendered through the {{token}}
             template engine — this covers a prefilled URL AND an upstream value
             (e.g. {{extracted.link}}, {{result.output_variables.next_url}}).
          2. If that resolves empty, fall back to the triggering monitor's own URL
             ({{target_url}} / {{url}}) — the natural default for
             "this page changed → re-crawl its site".
        """
        from services import crawl_orchestrator
        from models.crawl_job import CrawlJob, ACTIVE_STATUSES

        # 1) Resolve the seed URL.
        raw_seed = config.get("seed_url") or config.get("url") or ""
        seed_url = self._render_template(str(raw_seed), context).strip() if raw_seed else ""
        if not seed_url:
            seed_url = (context.get("target_url") or context.get("url") or "").strip()
        if not seed_url:
            return {"status": "skipped", "reason": "no seed_url resolved (set a URL or run from a monitor)"}

        # 2) Cost guard — never launch a second crawl of the same site while one is
        # still in flight. A flapping monitor must not spawn a fleet of overlapping
        # page-budget crawls. (Cooldown/max_fires guardrails apply on top of this at
        # the trigger level.)
        try:
            norm_seed = seed_url if seed_url.startswith(("http://", "https://")) else "https://" + seed_url
            existing = (await self.db.execute(
                select(CrawlJob).where(
                    CrawlJob.seed_url == norm_seed,
                    CrawlJob.status.in_(ACTIVE_STATUSES),
                ).limit(1)
            )).scalar_one_or_none()
            if existing:
                return {
                    "status": "skipped",
                    "reason": f"a crawl of {norm_seed} is already running",
                    "crawl_id": existing.id,
                    "data_workflow_id": existing.workflow_id,
                    "seed_url": norm_seed,
                    "crawl_name": existing.name,
                }
        except Exception as e:
            # Dedup is best-effort; a lookup failure must not block the crawl.
            logger.warning(f"[UnifiedTrigger] crawl dedup lookup failed: {e}")

        # 3) Map config → start_crawl kwargs (all optional, sane Dragnet defaults).
        def _int(key, default):
            try:
                return int(config.get(key, default))
            except (TypeError, ValueError):
                return default

        name_cfg = config.get("name")
        crawl_name = self._render_template(str(name_cfg), context).strip() if name_cfg else None

        # The coordinator runs DETERMINISTIC crawls only, so `extract_mode` is the
        # single output axis (there is no AI executor here). A block authored on the
        # cloud with "ai" collapses to markdown rather than being rejected.
        extract_mode = config.get("extract_mode") or "markdown"
        if extract_mode == "ai":
            extract_mode = "schema" if config.get("extract_schema") else "markdown"
        if extract_mode not in ("markdown", "schema"):
            extract_mode = "markdown"

        persona_id = config.get("persona_id")
        try:
            persona_id = int(persona_id) if persona_id not in (None, "") else None
        except (TypeError, ValueError):
            persona_id = None

        try:
            crawl = await crawl_orchestrator.start_crawl(
                self.db,
                seed_url=seed_url,
                name=crawl_name,
                extract_mode=extract_mode,
                extract_schema=config.get("extract_schema"),
                content_spec=config.get("content_spec") or config.get("content"),
                render_mode=config.get("render_mode") or "auto",
                ocr_mode=config.get("ocr_mode") or "auto",
                persona_id=persona_id,
                # Targeting: a plain-English goal derives scope + ranks the frontier
                # (no-op when the block supplies explicit include/exclude paths).
                intent=(self._render_template(str(config.get("intent")), context).strip()
                        if config.get("intent") else None),
                seed_urls=config.get("seed_urls") or None,
                relevance_threshold=float(config.get("relevance_threshold") or 0.0),
                include_paths=config.get("include_paths") or None,
                exclude_paths=config.get("exclude_paths") or None,
                max_depth=_int("max_depth", 4),
                page_budget=_int("page_budget", 1000),
                max_concurrent_shards=_int("max_concurrent_shards", 6),
                shard_size=_int("shard_size", 25),
                delay_ms=_int("delay_ms", 250),
                respect_robots=bool(config.get("respect_robots", True)),
                same_domain=bool(config.get("same_domain", True)),
                allow_subdomains=bool(config.get("allow_subdomains", True)),
            )
        except HTTPException as he:  # blocked seed (SSRF/blocklist), …
            return {"status": "failed", "reason": getattr(he, "detail", "crawl rejected")}
        except Exception as e:
            logger.error(f"[UnifiedTrigger] crawl dispatch failed: {e}")
            return {"status": "failed", "error": str(e)}

        logger.info(
            f"[UnifiedTrigger] Trigger {trigger.id} started crawl {crawl.id} of "
            f"{crawl.seed_url} (data_workflow_id={crawl.workflow_id})"
        )
        return {
            "status": "started",
            "crawl_id": crawl.id,
            "data_workflow_id": crawl.workflow_id,
            "seed_url": crawl.seed_url,
            "crawl_name": crawl.name,
        }

    async def _dispatch_notification(
        self,
        config: Dict[str, Any],
        context: Dict[str, Any],
        trigger: TriggerRule
    ) -> Dict[str, Any]:
        """Dispatch notification action using the notification dispatcher."""
        from notifications.dispatcher import NotificationDispatcher

        # Get notification config
        template = config.get("template", "Change detected!")
        channels = config.get("channels", [])
        recipients = config.get("recipients", [])  # Format: ["pushover:1", "email:3", ...]
        title = config.get("title")
        priority = config.get("priority")
        sound = config.get("sound")
        url = config.get("url")
        email_subject = config.get("email_subject")

        # Single-user coordinator: no marketplace installed-automation proxy
        # notifications. A normal notification's concrete `recipients` flow through
        # unchanged (there are no abstract recipient_slots to resolve).

        # Render the template
        rendered = self._render_template(template, context)

        if not channels:
            logger.info(f"[UnifiedTrigger] No channels configured for notification")
            return {
                "status": "skipped",
                "reason": "no channels configured",
            }

        logger.info(f"[UnifiedTrigger] Dispatching notification to channels: {channels}, recipients: {recipients}")

        # Get target for notification context
        target = None
        if trigger.target_id:
            result = await self.db.execute(
                select(Target).where(Target.id == trigger.target_id)
            )
            target = result.scalar_one_or_none()

        try:
            dispatcher = NotificationDispatcher(self.db)
            results = await dispatcher.dispatch_trigger_notification(
                target=target,
                context=context,
                channels=channels,
                recipients=recipients if recipients else None,
                title=title,
                template=rendered,
                priority=priority,
                sound=sound,
                url=url,
                email_subject=email_subject,
            )

            # Count totals
            total_sent = sum(r.get('sent', 0) for r in results.values())
            total_failed = sum(r.get('failed', 0) for r in results.values())

            logger.info(f"[UnifiedTrigger] Notification dispatch complete: {total_sent} sent, {total_failed} failed")

            return {
                "status": "completed" if total_sent > 0 else ("failed" if total_failed > 0 else "skipped"),
                "message": rendered[:200],
                "channels": channels,
                "recipients": recipients,
                "results": results,
                "total_sent": total_sent,
                "total_failed": total_failed,
            }

        except Exception as e:
            logger.error(f"[UnifiedTrigger] Notification dispatch failed: {e}")
            return {
                "status": "failed",
                "error": str(e),
                "message": rendered[:200],
                "channels": channels,
            }

    async def _dispatch_ai_session(
        self,
        config: Dict[str, Any],
        context: Dict[str, Any],
        trigger: TriggerRule
    ) -> Dict[str, Any]:
        """Fire one autonomous AI session at a connected fleet agent.

        This used to return `{"status": "skipped", "reason": "AI sessions are not
        available in this build"}` — a stale claim. Self-host DOES have AI sessions
        (models/ai_session.py, routers/ai_sessions.py, alembic 0006, and the agent's
        `ai_session_start` frame). The block stayed in the catalog as `platforms:
        BOTH`, so an operator could add "Run AI Agent" to an automation and it
        silently did nothing.

        SHAPE. The cloud block is ID-shaped: `session_ids` referencing saved
        `AIWorkflowSession` RECIPES. Self-host has no such recipe table — its
        `AiSession` rows are RUN RECORDS (session_id, agent_id, goal, status, …) and
        `POST /ai-sessions/start` takes the goal INLINE. So this block is GOAL-shaped
        and `session_ids` is meaningless here; porting cloud's dispatch verbatim
        could never work.

        This deliberately does NOT call the HTTP route: a trigger fires with no admin
        session, so it reuses the same internals the route does (pick agent → resolve
        deploy target → seal creds → persist → fire-and-forget frame). Never raises:
        a trigger action reports failure as a result, it does not abort the chain.
        """
        import uuid
        from datetime import datetime, timezone

        from models.ai_session import AiSession

        goal = self._render_template(str(config.get("goal") or ""), context).strip()
        if not goal:
            return {"status": "skipped", "reason": "no goal configured"}

        entry_url = config.get("entry_url")
        entry_url = self._render_template(str(entry_url), context).strip() if entry_url else None

        # `user_context` is what the operator threads in from upstream blocks; it is
        # additive to the goal rather than a separate agent input, because the
        # self-host start frame has no user_context field.
        user_context = config.get("user_context")
        if user_context:
            rendered = self._render_template(str(user_context), context).strip()
            if rendered:
                goal = f"{goal}\n\nContext:\n{rendered}"

        # Render the form values the agent may fill with.
        available_data: Dict[str, Any] = {}
        for key, value in (config.get("form_data") or {}).items():
            if key:
                available_data[key] = self._render_template(str(value), context) if value else ""

        try:
            from routers import user_recorder_ws
            from routers.ai_sessions import _pick_agent
            from routers.fleet import _resolve_deploy_target, _seal_plaintext_for_agent
        except Exception as e:  # pragma: no cover - import guard
            logger.warning(f"[UnifiedTrigger] ai_session dispatch unavailable: {e}")
            return {"status": "failed", "reason": f"ai_session dispatch unavailable: {e}"}

        # Agent selection + capability check. `_resolve_deploy_target` is the real
        # enforcement: it raises unless the agent is local-AI-capable and has a
        # channel key. Both raise HTTPException, which must not escape into the
        # trigger chain — convert to a result.
        try:
            agent_id = _pick_agent(config.get("agent_id"))
            channel_key = _resolve_deploy_target(agent_id)
        except Exception as e:
            reason = getattr(e, "detail", None) or str(e)
            logger.info(f"[UnifiedTrigger] ai_session not dispatched: {reason}")
            return {"status": "failed", "reason": str(reason)}

        credentials_encrypted = None
        creds = {k: v for k, v in (config.get("credentials") or {}).items() if k}
        if creds:
            rendered_creds = {
                k: self._render_template(str(v), context) if v else "" for k, v in creds.items()
            }
            credentials_encrypted = _seal_plaintext_for_agent(
                json.dumps(rendered_creds), channel_key
            )

        session_id = str(uuid.uuid4())
        name = f"AI: {goal}"[:500]
        generate_workflow = bool(config.get("generate_workflow", True))
        try:
            max_steps = int(config.get("max_steps") or 20)
        except (TypeError, ValueError):
            max_steps = 20

        # Persist BEFORE sending, exactly as the route does: a crash between the
        # send and the terminal frame must still leave a durable row.
        row = AiSession(
            session_id=session_id,
            agent_id=agent_id,
            name=name,
            goal=goal,
            entry_url=entry_url,
            status="running",
            generate_workflow=generate_workflow,
        )
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)

        frame = {
            "type": "ai_session_start",
            "session_id": session_id,
            "request_id": str(uuid.uuid4()),
            "name": name,
            "goal": goal,
            "entry_url": entry_url,
            "available_data": available_data,
            "credentials_encrypted": credentials_encrypted,
            "persona_id": config.get("persona_id"),
            "max_steps": max_steps,
            "generate_workflow": generate_workflow,
        }

        sent = await user_recorder_ws.push_fire_and_forget(agent_id, frame)
        if not sent:
            # The agent vanished between the pick and the send. Mark the row terminal
            # so it is not stuck "running" forever (same recovery as the route).
            row.status = "error"
            row.error = "agent_disconnected"
            row.completed_at = datetime.now(timezone.utc)
            await self.db.commit()
            return {
                "status": "failed",
                "reason": "the chosen agent disconnected before the AI session could start",
                "ai_session_id": row.id,
            }

        logger.info(
            f"[UnifiedTrigger] Started AI session {row.id} ({session_id}) on agent "
            f"{agent_id} for trigger {trigger.id}"
        )
        return {
            "status": "completed",
            "ai_session_id": row.id,
            "session_id": session_id,
            "agent_id": agent_id,
            "goal": goal[:500],
            # The block-chain reader keys continuation off these (see the
            # `ai_session_completed` continuation filter).
            "tasks_created": [{"ai_session_id": row.id, "ai_session_name": name}],
        }

    async def _wait_and_resume_continuation(
        self,
        trigger: TriggerRule,
        execution: TriggerExecution,
        continuation: Dict[str, Any],
        context: Dict[str, Any],
        results_so_far: List[Dict[str, Any]],
        max_wait: int = 300,
    ) -> List[Dict[str, Any]]:
        """
        Blocking wait for a continuation event (e.g., workflow completion),
        then resume the block chain with the completion data.

        Used by /api/v1/webhooks/ endpoint for synchronous execution.
        """
        import asyncio
        from models.automation_task import AutomationTask
        from database import AsyncSessionLocal

        continuation_event = continuation.get('continuation_event')
        continuation_filter = continuation.get('continuation_filter', {})
        task_id = continuation_filter.get('task_id')
        workflow_id = continuation_filter.get('workflow_id')

        # Also check context_snapshot for task_id if not in filter
        if not task_id:
            ctx_snapshot = continuation.get('context_snapshot', {})
            task_id = ctx_snapshot.get('workflow_task_id')

        logger.info(
            f"[BlockingTrigger] Waiting for {continuation_event} "
            f"(task_id={task_id}, workflow_id={workflow_id}, max_wait={max_wait}s)"
        )

        # Commit current session state so the task is visible to other sessions
        await self.db.commit()

        # Wait for task completion. Fast path: subscribe to the Redis channel
        # that _process_task_completion publishes to and wake in ~ms. Fallback:
        # a long-interval poll (was every 2s → now every POLL_INTERVAL s) so a
        # missed publish still resolves. This cuts blocking-trigger DB load by
        # ~80% while keeping the original robustness.
        from utils.redis_client import get_redis

        async def _check_done():
            async with AsyncSessionLocal() as poll_db:
                if task_id:
                    tr = await poll_db.execute(
                        select(AutomationTask).where(AutomationTask.id == task_id)
                    )
                    t = tr.scalar_one_or_none()
                    if t and t.status in ("success", "failed", "cancelled"):
                        return t
                elif workflow_id:
                    tr = await poll_db.execute(
                        select(AutomationTask).where(
                            AutomationTask.workflow_id == workflow_id,
                            AutomationTask.status.in_(["success", "failed", "cancelled"]),
                        ).order_by(AutomationTask.created_at.desc()).limit(1)
                    )
                    t = tr.scalar_one_or_none()
                    if t:
                        return t
                return None

        POLL_INTERVAL = 10  # fallback poll; pub/sub provides the fast path
        channel = (
            f"task:completed:{task_id}" if task_id
            else (f"workflow:completed:{workflow_id}" if workflow_id else None)
        )

        completed_task_data = None
        pubsub = None
        try:
            if channel:
                try:
                    pubsub = get_redis().pubsub()
                    await pubsub.subscribe(channel)
                except Exception as e:
                    logger.warning(f"[BlockingTrigger] pubsub subscribe failed, polling only: {e}")
                    pubsub = None

            loop = asyncio.get_running_loop()
            deadline = loop.time() + max_wait

            # Immediate check — the task may already be done before we subscribed.
            task = await _check_done()
            while task is None and loop.time() < deadline:
                remaining = deadline - loop.time()
                wait_for = min(POLL_INTERVAL, max(0.0, remaining))
                if pubsub is not None:
                    # Blocks up to wait_for, returns early when a wakeup arrives.
                    try:
                        await pubsub.get_message(ignore_subscribe_messages=True, timeout=wait_for)
                    except Exception:
                        await asyncio.sleep(min(2.0, wait_for))
                else:
                    await asyncio.sleep(wait_for)
                task = await _check_done()

            if task is not None:
                completed_task_data = {
                    "id": task.id,
                    "status": task.status,
                    "success": task.success,
                    "result_data": task.result_data,
                    "error_message": task.error_message,
                    "workflow_id": task.workflow_id,
                }
        finally:
            if pubsub is not None:
                try:
                    await pubsub.unsubscribe(channel)
                    await pubsub.aclose()
                except Exception:
                    pass

        if not completed_task_data:
            logger.warning(f"[BlockingTrigger] Timeout waiting for task {task_id}")
            results_so_far.append({
                "type": "timeout",
                "error": f"Task {task_id} did not complete within {max_wait}s",
            })
            return results_so_far

        logger.info(
            f"[BlockingTrigger] Task {completed_task_data['id']} completed with status={completed_task_data['status']}"
        )

        # Build event context from completed task
        result_data = completed_task_data.get("result_data") or {}
        event_context = {
            "task_id": completed_task_data["id"],
            "status": completed_task_data["status"],
            "success": completed_task_data.get("success"),
            "result": result_data,
            "extracted_data": result_data.get("extracted_data", {}),
            "error": completed_task_data.get("error_message"),
            "workflow_id": completed_task_data.get("workflow_id"),
        }

        # Resume the block chain continuation
        continuation_results = await self.process_block_continuation(
            trigger_rule_id=trigger.id,
            event_type=continuation_event,
            event_context=event_context,
            continuation_data=continuation,
        )

        results_so_far.extend(continuation_results)
        return results_so_far

    async def _create_persona_action(
        self,
        config: Dict[str, Any],
        context: Dict[str, Any],
        trigger: TriggerRule
    ) -> Dict[str, Any]:
        """Action: create a persona from a workflow's captured login (e.g. after an
        account-creation workflow completes). config = {workflow_id?, name?}; falls back
        to the triggering workflow in context."""
        from urllib.parse import urlparse
        from models.automation_workflow import AutomationWorkflow
        from services.session_state_service import SessionStateService
        from services.persona_service import PersonaService

        workflow_id = config.get("workflow_id") or context.get("workflow_id")
        if not workflow_id:
            return {"status": "skipped", "reason": "no workflow_id for create_persona"}
        # This action reads the workflow's captured credentials/session and mints
        # a persona from them.
        wf = (await self.db.execute(
            select(AutomationWorkflow).where(
                AutomationWorkflow.id == int(workflow_id),
            )
        )).scalar_one_or_none()
        if not wf:
            return {"status": "failed", "reason": f"workflow {workflow_id} not found"}

        domain = None
        if wf.entry_url:
            try:
                domain = urlparse(wf.entry_url).hostname
            except Exception:
                domain = None
        creds: Dict[str, Any] = {}
        if wf.credentials_encrypted:
            try:
                creds = PersonaService.decrypt_json(wf.credentials_encrypted)
            except Exception:
                creds = {}
        username = creds.get("username") or creds.get("email") or creds.get("user")

        auth_session = None
        try:
            aff = await SessionStateService.get_preferred_affinity(self.db, wf.id)
            if aff:
                auth_session = await SessionStateService.load_session(self.db, wf.id, aff.agent_id, wf.session_ttl_seconds)
        except Exception:
            auth_session = None

        if not creds and not auth_session:
            return {"status": "skipped", "reason": "no captured login to create persona"}

        name = config.get("name") or f"{wf.name} account"
        persona = await PersonaService.create_from_capture(
            self.db, name=name, domain=domain,
            login_username=username, creds=creds, auth_session=auth_session,
        )
        logger.info(f"[UnifiedTrigger] create_persona action made persona {persona.id} from workflow {wf.id}")
        return {"status": "completed", "persona_id": persona.id}

    async def _dispatch_workflow(
        self,
        config: Dict[str, Any],
        context: Dict[str, Any],
        trigger: TriggerRule
    ) -> Dict[str, Any]:
        """Dispatch workflow action — pushes directly to a recorder if available,
        otherwise falls back to the task queue for desktop agent pickup."""
        from models.automation_workflow import AutomationWorkflow
        from models.automation_task import AutomationTask

        # Single-user coordinator: no marketplace installed-automation proxy
        # workflows (no nested_ref / installed sidecars). A workflow block always
        # carries a concrete owned workflow_id.
        workflow_id = config.get("workflow_id")

        if not workflow_id:
            return {"status": "skipped", "reason": "no workflow_id configured"}

        # The workflow lookup is by id alone.
        result = await self.db.execute(
            select(AutomationWorkflow).where(
                AutomationWorkflow.id == workflow_id,
            )
        )
        workflow = result.scalar_one_or_none()

        if not workflow:
            return {"status": "failed", "reason": f"Workflow {workflow_id} not found"}

        if not workflow.is_active:
            return {"status": "skipped", "reason": f"Workflow {workflow_id} is inactive"}

        # ── Auto-buy safety rails ──────────────────────────────────────────────
        # When this workflow action carries payment config (a "buy"), enforce
        # idempotency, the spend cap, and the human-confirmation threshold BEFORE
        # anything is dispatched. Non-buy workflow actions skip this entirely.
        payment_mode = config.get("payment_mode")
        dry_run = bool(config.get("dry_run", False))
        is_buy = ("payment_mode" in config) or dry_run or bool(config.get("require_confirmation"))
        buy_amount = self._extract_amount(context) if is_buy else None
        spend_reservation = None
        if is_buy:
            guard = await self._enforce_buy_safety(trigger, workflow, config, context, buy_amount)
            if guard is not None:
                return guard
            # Atomically RESERVE the buy's amount against the rolling spend cap
            # BEFORE issuing the card, so concurrent buys can't each slip past the
            # (now advisory) read-only cap gate. Counts the spend exactly once, at
            # issue time; completion reconciles/releases it. Aborts here if the
            # reservation would breach the cap.
            spend_reservation = await self._reserve_spend(trigger, config, buy_amount)
            if isinstance(spend_reservation, dict) and spend_reservation.get("status"):
                await self._notify_buy_event(
                    trigger, context,
                    title="Auto-buy blocked — spend cap reached",
                    message=(
                        "Skipped buying {{target_name}} — it would exceed your "
                        f"{spend_reservation.get('period') or (((trigger.conditions or {}).get('spend_cap') or {}).get('period') or 'day')} spend cap."
                    ),
                )
                return spend_reservation

        # Render input mapping
        input_mapping = config.get("input_mapping", {})
        rendered_inputs = {}
        for key, template in input_mapping.items():
            rendered_inputs[key] = self._render_template(str(template), context)

        # FILE ASSETS chaining (§4.5): map a prior run's output FILE into THIS
        # workflow's upload-step slot. Two deterministic, explicit sources (no
        # "latest file" magic):
        #   1. config.files_mapping = { slot: "{{result.output_variables.<key>}}" }
        #      or { slot: "{{result.output_files.0.file_id}}" } — rendered against the
        #      triggering run's result, yielding a concrete file_id.
        #   2. input_mapping keys prefixed "file:" — e.g. "file:resume" → the rendered
        #      value is treated as a file_id and routed to slot "resume".
        # The resolved file_id is bound by file_service.resolve_for_run downstream,
        # so ownership is enforced there. Each value must be a non-empty
        # file_<...> handle.
        rendered_files = {}
        # A files map carried in the triggering context (e.g. a webhook / managed-API
        # caller that passed { slot: file_id }) flows straight through, ownership-
        # checked downstream. Stamped first so an explicit files_mapping can override.
        _ctx_files = context.get("files")
        if isinstance(_ctx_files, dict):
            for slot, fid in _ctx_files.items():
                if isinstance(fid, str) and fid.startswith("file_"):
                    rendered_files[str(slot)] = fid
        files_mapping = config.get("files_mapping") or {}
        if isinstance(files_mapping, dict):
            for slot, template in files_mapping.items():
                val = self._render_template(str(template), context)
                if isinstance(val, str) and val.startswith("file_"):
                    rendered_files[str(slot)] = val
        for key in list(rendered_inputs.keys()):
            if isinstance(key, str) and key.startswith("file:"):
                slot = key[len("file:"):]
                val = rendered_inputs.pop(key)
                if slot and isinstance(val, str) and val.startswith("file_"):
                    rendered_files[slot] = val

        logger.info(f"[UnifiedTrigger] Dispatching workflow {workflow_id} with inputs: {rendered_inputs}")
        if rendered_files:
            logger.info(f"[UnifiedTrigger] Chaining files into workflow {workflow_id}: {list(rendered_files.keys())}")

        # Build trigger_context with all relevant data for chain propagation
        trigger_context = {
            "trigger_name": trigger.name,
            "trigger_id": trigger.id,
            "selector_name": context.get("selector_name"),
            "input_mapping": rendered_inputs,
            # FILE ASSETS chaining (§4.5): { slot: file_id } bound from a prior run's
            # output_files. Resolved + ownership-checked at the dispatch choke point.
            **({"files": rendered_files} if rendered_files else {}),
            # Webhook form_data (query params + POST body from caller)
            "form_data": {**context.get("form_data", {}), **rendered_inputs},
            # Original extracted data from the triggering event (for chain propagation)
            "extracted_data": context.get("extracted", {}),
            # Additional context from the original event for chain propagation
            "change_context": context.get("change_context", {}),
            "diff_snippet": context.get("diff_snippet"),
            "target_url": context.get("target_url"),
            # Encrypted secrets from $secret: webhook fields (Fernet blob, never plaintext)
            "encrypted_secrets": context.get("encrypted_secrets"),
            # Auto-buy: how it pays + whether to stop before the irreversible commit step.
            "dry_run": dry_run if is_buy else False,
            "payment_mode": payment_mode if is_buy else None,
            # Carried to task completion so a confirmed, non-dry buy meters the spend cap.
            #
            # The spend was already RESERVED atomically at issue time (see
            # _reserve_spend) — `reserved` is the held amount and `reserve_key` the
            # rolling-cap counter it was added to. Completion uses these to RELEASE
            # the reservation if the buy didn't actually commit (so it nets to zero)
            # and to reconcile an estimated→actual delta on a real commit. Absence
            # of `reserved` means nothing was pre-counted (no cap / unpriced / dry).
            "buy_meta": ({
                "amount": buy_amount,
                "trigger_id": trigger.id,
                "spend_period": str(((trigger.conditions or {}).get("spend_cap") or {}).get("period") or "day"),
                "dry_run": dry_run,
                **(
                    {
                        "reserved": spend_reservation["amount"],
                        "reserve_key": spend_reservation["key"],
                        "spend_period": spend_reservation.get("period")
                        or str(((trigger.conditions or {}).get("spend_cap") or {}).get("period") or "day"),
                    }
                    if isinstance(spend_reservation, dict) and "amount" in spend_reservation
                    else {}
                ),
            } if is_buy else None),
        }

        # Virtual-card auto-buy was a cloud monetization feature (a platform card
        # issuer). The self-host coordinator has no card provider, so a virtual_card
        # buy cannot be funded — block it (releasing any pre-issue reservation).
        if is_buy and payment_mode == "virtual_card":
            logger.info(
                f"[BuySafety] Trigger {trigger.id}: virtual-card auto-buy is not "
                f"available on the self-host coordinator — blocking buy"
            )
            await self._release_reservation(spend_reservation)
            await self._notify_buy_event(
                trigger, context,
                title="Auto-buy blocked — virtual card unavailable",
                message="Virtual-card auto-buy for {{target_name}} isn't available on this deployment.",
            )
            return {"status": "skipped", "reason": "no_funding"}

        # Dispatch via shared helper — pushes to recorder or queues for desktop agent
        from routers.automation import _dispatch_to_recorder_or_queue

        # Optional per-action persona override; falls back to workflow.default_persona_id.
        persona_id = config.get("persona_id")

        try:
            task = await _dispatch_to_recorder_or_queue(
                db=self.db,
                workflow=workflow,
                target_id=trigger.target_id,
                trigger_type="on_change",
                trigger_rule_id=trigger.id,
                trigger_context=trigger_context,
                # Pass full form_data: webhook params + rendered input_mapping
                form_data=trigger_context.get("form_data", {}),
                persona_id=int(persona_id) if persona_id else None,
            )
        except Exception:
            # Dispatch failed before a task row exists to reconcile the reservation
            # at completion — release it so the aborted buy nets to zero spend.
            await self._release_reservation(spend_reservation)
            raise

        return {
            "status": "completed",
            "task_id": task.id,
            "workflow_id": workflow_id,
            "workflow_name": workflow.name,
            "inputs": rendered_inputs,
            "dispatch_mode": "recorder_push" if task.status == "running" else "agent_queue",
        }

    # ── Auto-buy safety helpers ────────────────────────────────────────────────

    @staticmethod
    def _extract_amount(context: Dict[str, Any]) -> Optional[float]:
        """Best-effort numeric price from the triggering event (extracted.price)."""
        raw = (context.get("extracted") or {}).get("price")
        if raw is None:
            return None
        try:
            cleaned = (
                str(raw).replace(",", "").replace("$", "").replace("€", "").replace("£", "").strip()
            )
            return float(cleaned)
        except (ValueError, TypeError):
            return None

    async def _notify_buy_event(self, trigger, context, title, message, url=None):
        """Best-effort user notification for a buy event (blocked / needs approval)."""
        try:
            cfg = {"channels": ["email"], "title": title, "template": message}
            if url:
                cfg["url"] = url
            await self._dispatch_notification(cfg, context, trigger)
        except Exception as e:
            logger.warning(f"[BuySafety] notification failed for trigger {trigger.id}: {e}")

    async def _create_awaiting_approval_task(self, trigger, workflow, config, context, amount):
        """Create a held task an operator must approve before the purchase runs."""
        from models.automation_task import AutomationTask
        pending = {
            "workflow_id": workflow.id,
            "form_data": context.get("form_data", {}),
            "persona_id": config.get("persona_id"),
            "payment_mode": config.get("payment_mode"),
            "dry_run": bool(config.get("dry_run", False)),
            "amount": amount,
            "extracted_data": context.get("extracted", {}),
            "target_url": context.get("target_url"),
            "encrypted_secrets": context.get("encrypted_secrets"),
            "spend_period": str(((trigger.conditions or {}).get("spend_cap") or {}).get("period") or "day"),
        }
        task = AutomationTask(
            target_id=trigger.target_id,
            workflow_id=workflow.id,
            trigger_type="on_change",
            trigger_rule_id=trigger.id,
            status="awaiting_approval",
            trigger_context={
                "pending_buy": pending,
                "trigger_name": trigger.name,
                "extracted_data": context.get("extracted", {}),
            },
            max_attempts=(workflow.retry_count or 0) + 1,
        )
        self.db.add(task)
        await self.db.flush()
        return task

    async def _enforce_buy_safety(self, trigger, workflow, config, context, amount):
        """
        Gate a buy before dispatch. Returns a short-circuit result dict to ABORT,
        or None to proceed.

          1. Idempotency  — one restock event must not dispatch multiple buys.
          2. Spend cap    — block if the rolling period spend would exceed the cap.
          3. Confirmation — hold for human approval over a threshold (or when unpriced).
        """
        import hashlib
        from utils.redis_client import get_redis

        conditions = trigger.conditions or {}
        extracted = context.get("extracted") or {}
        r = get_redis()

        # 1) Idempotency — dedupe rapid double-detections of the same restock event.
        try:
            sig_src = f"{trigger.id}:{trigger.target_id}:{extracted.get('price')}:{extracted.get('status', '')}"
            sig = hashlib.sha256(sig_src.encode()).hexdigest()[:16]
            cooldown_min = float((conditions.get("schedule") or {}).get("cooldown_minutes") or 0)
            idem_ttl = int(max(cooldown_min * 60, 300))  # at least 5 minutes
            was_set = await r.set(f"buy:idem:{trigger.id}:{sig}", "1", ex=idem_ttl, nx=True)
            if not was_set:
                logger.info(f"[BuySafety] Trigger {trigger.id}: duplicate buy suppressed (idempotent)")
                return {"status": "skipped", "reason": "idempotent_duplicate"}
        except Exception as e:
            logger.warning(f"[BuySafety] idempotency check failed for trigger {trigger.id} (allowing): {e}")

        # 2) Spend cap — rolling period spend, metered by the watched price.
        spend_cap = conditions.get("spend_cap") or {}
        cap_amount = spend_cap.get("amount")
        if cap_amount and amount is not None:
            try:
                cap_amount = float(cap_amount)
                period = str(spend_cap.get("period") or "day")
                spent_raw = await r.get(f"buy:spend:{trigger.id}:{period}")
                spent = float(spent_raw) if spent_raw else 0.0
                if spent + amount > cap_amount:
                    logger.info(
                        f"[BuySafety] Trigger {trigger.id}: spend cap reached "
                        f"({spent:.2f}+{amount:.2f} > {cap_amount:.2f}/{period}) — blocking buy"
                    )
                    await self._notify_buy_event(
                        trigger, context,
                        title="Auto-buy blocked — spend cap reached",
                        message=f"Skipped buying {{target_name}} — it would exceed your {period} spend cap of {cap_amount:.0f}.",
                    )
                    return {"status": "skipped", "reason": "spend_cap_exceeded", "spent": spent, "cap": cap_amount}
            except (ValueError, TypeError) as e:
                logger.warning(f"[BuySafety] spend cap parse failed for trigger {trigger.id} (allowing): {e}")

        # 3) Human confirmation over a threshold (or when we can't price the buy).
        if config.get("require_confirmation"):
            raw_over = config.get("confirm_over")
            try:
                over = float(raw_over) if raw_over is not None else None
            except (ValueError, TypeError):
                over = None
            if over is None or amount is None or amount > over:
                task = await self._create_awaiting_approval_task(trigger, workflow, config, context, amount)
                await self._notify_buy_event(
                    trigger, context,
                    title="Approve purchase",
                    message=(
                        "{{target_name}} is ready to buy"
                        + (f" at {amount:.2f}" if amount is not None else "")
                        + " — approve it in the app to place the order."
                    ),
                    url=f"/automations/{trigger.id}",
                )
                logger.info(f"[BuySafety] Trigger {trigger.id}: buy held for approval (task {task.id})")
                return {"status": "awaiting_approval", "task_id": task.id, "amount": amount}

        return None

    async def _reserve_spend(self, trigger, config, amount):
        """Atomically RESERVE `amount` against the trigger's rolling spend cap
        BEFORE the card is issued, closing the read-check TOCTOU race.

        The old gate only READ the rolling counter and rejected if
        spent+amount > cap; the counter was incremented at completion. Two
        concurrent buys could both pass that read before either completed and
        together blow the cap. Here we INCRBYFLOAT first (atomic) and only keep
        the reservation if the new total is within the cap — otherwise we release
        it (INCRBYFLOAT by -amount) and reject. Each buy thus claims its slice of
        the cap exactly once, at issue time.

        Returns:
          - a short-circuit result dict (status="skipped", reason="spend_cap_exceeded")
            to ABORT the buy, OR
          - a dict {"key": ..., "amount": ...} describing the held reservation so
            completion can release/reconcile it, OR
          - None when there is nothing to reserve (no cap configured / no priced
            amount / dry run): the buy proceeds without touching the counter.
        """
        from utils.redis_client import get_redis
        from services.buy_logic import spend_period_seconds

        # Only reserve for a real, priced buy. A backend-decided dry run never
        # issues a spendable card, so it must never consume cap budget. (We rely on
        # the BACKEND's dry_run decision here, never an agent-reported flag.)
        if bool(config.get("dry_run", False)):
            return None
        conditions = trigger.conditions or {}
        spend_cap = conditions.get("spend_cap") or {}
        cap_amount = spend_cap.get("amount")
        if not cap_amount or amount is None:
            return None

        try:
            cap_amount = float(cap_amount)
            amount = float(amount)
        except (ValueError, TypeError) as e:
            logger.warning(f"[BuySafety] reserve parse failed for trigger {trigger.id} (allowing): {e}")
            return None
        if amount <= 0:
            return None

        period = str(spend_cap.get("period") or "day")
        period_secs = spend_period_seconds(period)
        spend_key = f"buy:spend:{trigger.id}:{period}"
        r = get_redis()

        try:
            new_total = await r.incrbyfloat(spend_key, amount)
            # Refresh the rolling window TTL so the period bucket expires together.
            await r.expire(spend_key, period_secs)
        except Exception as e:
            # Redis unavailable: fail OPEN (don't block a buy on a metering outage),
            # consistent with the prior best-effort cap gate. No reservation to track.
            logger.warning(f"[BuySafety] spend reserve failed for trigger {trigger.id} (allowing): {e}")
            return None

        if new_total > cap_amount:
            # Over cap — release our reservation and reject BEFORE issuing the card.
            try:
                await r.incrbyfloat(spend_key, -amount)
                await r.expire(spend_key, period_secs)
            except Exception as e:
                logger.warning(f"[BuySafety] spend reserve release failed for trigger {trigger.id}: {e}")
            logger.info(
                f"[BuySafety] Trigger {trigger.id}: spend cap reached on reserve "
                f"({new_total:.2f} > {cap_amount:.2f}/{period}) — blocking buy"
            )
            return {
                "status": "skipped", "reason": "spend_cap_exceeded",
                "spent": new_total, "cap": cap_amount, "period": period,
            }

        logger.info(
            f"[BuySafety] Trigger {trigger.id}: reserved {amount:.2f} against cap "
            f"({new_total:.2f}/{cap_amount:.2f} {period})"
        )
        return {"key": spend_key, "amount": amount, "period": period}

    @staticmethod
    async def _release_reservation(reservation):
        """Release a held spend reservation (INCRBYFLOAT by -amount) when a buy is
        aborted AFTER reserving but BEFORE a task exists to reconcile it at
        completion (e.g. card issuance failed). No-op if there's no reservation."""
        if not (isinstance(reservation, dict) and "amount" in reservation and reservation.get("key")):
            return
        from utils.redis_client import get_redis
        from services.buy_logic import spend_period_seconds
        try:
            r = get_redis()
            await r.incrbyfloat(reservation["key"], -float(reservation["amount"]))
            await r.expire(reservation["key"], spend_period_seconds(reservation.get("period")))
        except Exception as e:
            logger.warning(f"[BuySafety] spend reservation release failed: {e}")

    # _pick_recorder_for_workflow removed — now using shared _dispatch_to_recorder_or_queue from automation.py

    async def _process_block_chain(
        self,
        trigger: TriggerRule,
        blocks: List[Dict[str, Any]],
        context: Dict[str, Any],
        start_index: int = 0,
        branch_contexts: Optional[Dict[str, Dict[str, Any]]] = None,
        current_branch: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """
        Process a chain of blocks with support for parallel branches and branch-scoped placeholders.

        Branch-scoped placeholder system:
        - Global context (from triggering event) is always available: {{diff_snippet}}, {{target_url}}
        - Each parallel branch gets an index: branch1, branch2, etc.
        - Within a branch, unprefixed placeholders resolve from that branch's context
        - Cross-branch access via: {{branch1.session_name}}, {{branch2.session_name}}

        Returns:
            - List of action results
            - Optional pending continuation (if we hit a completion event block)
        """
        import asyncio

        results = []

        # Initialize branch contexts if not provided (first call)
        if branch_contexts is None:
            branch_contexts = {}

        # Base context contains global event data
        base_context = context.copy()

        # Build parent-child mapping for parallel branch detection
        children_by_parent: Dict[str, List[Dict[str, Any]]] = {}
        block_by_id: Dict[str, Dict[str, Any]] = {}
        root_blocks: List[Dict[str, Any]] = []

        for block in blocks:
            block_id = block.get('id', '')
            parent_id = block.get('parentId')
            block_by_id[block_id] = block

            if parent_id:
                if parent_id not in children_by_parent:
                    children_by_parent[parent_id] = []
                children_by_parent[parent_id].append(block)
            else:
                root_blocks.append(block)

        def build_context_for_branch(branch_name: Optional[str]) -> Dict[str, Any]:
            """Build context with branch-scoped placeholders."""
            ctx = base_context.copy()

            # Add all branch contexts under 'branchX' keys for cross-branch access
            for br_name, br_ctx in branch_contexts.items():
                ctx[br_name] = br_ctx.copy()

            # If we're in a specific branch, merge its values at top level for unprefixed access
            if branch_name and branch_name in branch_contexts:
                # Local branch values override global (but don't overwrite branch contexts)
                for key, value in branch_contexts[branch_name].items():
                    if not key.startswith('branch'):
                        ctx[key] = value

            return ctx

        async def process_block(block: Dict[str, Any], branch_name: Optional[str]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]], Dict[str, Any]]:
            """Process a single block and return result + optional continuation + result data for context."""
            block_type = block.get('type')
            block_subtype = block.get('blockType')
            config = block.get('config', {})
            block_id = block.get('id', f'block_unknown')
            block_idx = blocks.index(block) if block in blocks else -1

            # Build context with branch scope
            ctx = build_context_for_branch(branch_name)

            logger.debug(f"[BlockChain] Processing block ({block_id}): {block_type}/{block_subtype} in {branch_name or 'global'}")

            # Event blocks (non-root) are continuation points
            if block_type == BLOCK_TYPE_EVENT and block.get('parentId'):
                continuation_filter = config.copy()
                if block_subtype == 'ai_session_completed' and not continuation_filter.get('ai_session_id'):
                    linked_block_id = config.get('linked_to_block')
                    if linked_block_id and linked_block_id in block_by_id:
                        prev_block = block_by_id[linked_block_id]
                        continuation_filter['ai_session_id'] = prev_block.get('config', {}).get('session_ids', [None])[0]

                if block_subtype == 'workflow_completed' and not continuation_filter.get('workflow_id'):
                    linked_block_id = config.get('linked_to_block')
                    if linked_block_id and linked_block_id in block_by_id:
                        prev_block = block_by_id[linked_block_id]
                        continuation_filter['workflow_id'] = prev_block.get('config', {}).get('workflow_id')
                    # Get task_id from branch context or base context (set when workflow was dispatched)
                    branch_ctx = branch_contexts.get(branch_name, {}) if branch_name else {}
                    task_id = branch_ctx.get('workflow_task_id') or base_context.get('workflow_task_id')
                    if task_id:
                        continuation_filter['task_id'] = task_id
                        logger.info(f"[BlockChain] Added task_id={task_id} to workflow_completed filter")

                remaining_blocks = [b for b in blocks if blocks.index(b) >= block_idx]

                continuation = {
                    "continuation_event": block_subtype,
                    "continuation_filter": continuation_filter,
                    "remaining_blocks": remaining_blocks,
                    "context_snapshot": base_context.copy(),
                    "branch_contexts": branch_contexts.copy(),
                    "current_branch": branch_name,
                    "trigger_rule_id": trigger.id,
                }
                logger.info(f"[BlockChain] Pausing at block {block_id}, waiting for {block_subtype} (branch: {branch_name})")
                return {"block_id": block_id, "type": "continuation_pause", "event": block_subtype}, continuation, {}

            # Condition blocks
            if block_type == BLOCK_TYPE_CONDITION:
                field = config.get('field', '')
                operator = config.get('operator', 'contains')
                value = config.get('value', '')

                if field:
                    match, reason = self._evaluate_single_condition(field, operator, value, ctx)
                    return {
                        "block_id": block_id,
                        "type": "condition",
                        "field": field,
                        "matched": match,
                        "reason": reason,
                    }, None if match else {"stop": True}, {}

            # Action blocks
            if block_type == BLOCK_TYPE_ACTION:
                action_result = {
                    "block_id": block_id,
                    "type": block_subtype,
                    "status": "pending",
                }
                result_data = {}  # Data to add to branch context

                try:
                    if block_subtype == 'notification':
                        dispatch_result = await self._dispatch_notification(config, ctx, trigger)
                        action_result.update(dispatch_result)
                        # All notification placeholders
                        result_data = {
                            "notification_status": dispatch_result.get("status"),
                            "notification_sent": dispatch_result.get("total_sent", 0),
                            "notification_failed": dispatch_result.get("total_failed", 0),
                            "notification_message": dispatch_result.get("message", ""),
                            "notification_channels": dispatch_result.get("channels", []),
                        }
                    elif block_subtype == 'ai_session':
                        dispatch_result = await self._dispatch_ai_session(config, ctx, trigger)
                        action_result.update(dispatch_result)
                        # All AI session dispatch placeholders
                        tasks = dispatch_result.get('tasks_created', [])
                        if tasks:
                            result_data = {
                                "task_id": tasks[0].get("task_id"),
                                "ai_session_id": tasks[0].get("ai_session_id"),
                                "ai_session_name": tasks[0].get("ai_session_name"),
                                "session_id": tasks[0].get("ai_session_id"),
                                "session_name": tasks[0].get("ai_session_name"),
                            }
                        result_data["ai_session_status"] = dispatch_result.get("status")
                        result_data["ai_session_user_context"] = dispatch_result.get("user_context", "")
                    elif block_subtype == 'workflow':
                        # Error handling: retry the workflow action up to `config.retry` extra
                        # times when the dispatch fails, optionally pausing `retry_backoff_ms`
                        # between attempts. Same config contract as the desktop interpreter.
                        try:
                            retries = int(config.get('retry') or 0)
                        except (TypeError, ValueError):
                            retries = 0
                        try:
                            backoff_ms = int(config.get('retry_backoff_ms') or 0)
                        except (TypeError, ValueError):
                            backoff_ms = 0
                        attempt = 0
                        failed = False
                        while True:
                            try:
                                dispatch_result = await self._dispatch_workflow(config, ctx, trigger)
                                failed = str(dispatch_result.get("status")) in ("failed", "error")
                            except Exception as _we:
                                dispatch_result = {"status": "failed", "error": str(_we)}
                                failed = True
                            if not failed or attempt >= max(0, retries):
                                break
                            attempt += 1
                            if backoff_ms > 0:
                                await asyncio.sleep(backoff_ms / 1000.0)
                        action_result.update(dispatch_result)
                        action_result["attempt"] = attempt + 1
                        # All workflow dispatch placeholders
                        result_data = {
                            "workflow_task_id": dispatch_result.get("task_id"),
                            "workflow_id": dispatch_result.get("workflow_id"),
                            "workflow_name": dispatch_result.get("workflow_name", ""),
                            "workflow_status": dispatch_result.get("status"),
                        }
                        # Run-failure RETRY (parity with the desktop inline retry): the dispatch
                        # succeeded async, so any remaining budget is carried into the continuation's
                        # context_snapshot. When the workflow_completed event later reports failure,
                        # _process_pending_continuations re-dispatches this workflow (see
                        # _retry_workflow_continuation) until the budget is spent.
                        remaining_budget = max(0, retries - attempt)
                        if not failed and remaining_budget > 0:
                            result_data["_wf_retry_meta"] = {
                                "budget": remaining_budget,
                                "backoff_ms": backoff_ms,
                                "config": config,
                                "on_error": config.get("on_error"),
                            }
                        # on_error=stop: skip this action's dependent children when it failed.
                        if failed and config.get('on_error') == 'stop':
                            action_result["status"] = action_result.get("status") or "failed"
                            return action_result, {"stop": True}, result_data
                    elif block_subtype == 'crawl':
                        dispatch_result = await self._dispatch_crawl(config, ctx, trigger)
                        action_result.update(dispatch_result)
                        # Crawl placeholders — downstream blocks (e.g. a notification or a
                        # workflow that reads the collected pages) chain off these. The
                        # crawl runs async; `crawl_completed`/`crawl_failed` triggers fire
                        # when it converges (see process_crawl_event).
                        result_data = {
                            "crawl_id": dispatch_result.get("crawl_id"),
                            "crawl_workflow_id": dispatch_result.get("data_workflow_id"),
                            "crawl_data_workflow_id": dispatch_result.get("data_workflow_id"),
                            "crawl_seed_url": dispatch_result.get("seed_url", ""),
                            "crawl_name": dispatch_result.get("crawl_name", ""),
                            "crawl_status": dispatch_result.get("status"),
                        }
                        if dispatch_result.get("status") in ("failed", "error") and config.get('on_error') == 'stop':
                            action_result["status"] = action_result.get("status") or "failed"
                            return action_result, {"stop": True}, result_data
                    elif block_subtype == 'create_persona':
                        persona_result = await self._create_persona_action(config, ctx, trigger)
                        action_result.update(persona_result)
                        result_data = {"persona_id": persona_result.get("persona_id")}
                    elif block_subtype == 'return_data':
                        # Return data to caller - collect extracted_data from context
                        extracted = ctx.get("extracted_data") or ctx.get("extracted", {})
                        # Also check completion_event for workflow result data
                        completion = ctx.get("completion_event", {})
                        if completion:
                            completion_result = completion.get("result", {})
                            completion_extracted = completion.get("extracted_data", {}) or completion_result.get("extracted_data", {})
                            if completion_extracted:
                                extracted = {**extracted, **completion_extracted}
                            # If no extractions, use the full workflow result
                            if not extracted and completion_result:
                                extracted = completion_result
                        action_result["status"] = "completed"
                        action_result["extracted_data"] = extracted
                        action_result["_is_return"] = True
                        result_data = {"return_data": extracted}
                    else:
                        action_result["status"] = "skipped"
                        action_result["reason"] = f"unknown action: {block_subtype}"
                except Exception as e:
                    action_result["status"] = "failed"
                    action_result["error"] = str(e)
                    result_data["action_error"] = str(e)
                    logger.error(f"[BlockChain] Action {block_subtype} failed at block {block_id}: {e}")

                return action_result, None, result_data

            return {"block_id": block_id, "type": "unknown", "skipped": True}, None, {}

        async def process_tree_level(
            block_list: List[Dict[str, Any]],
            branch_name: Optional[str]
        ) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
            """Process a list of blocks at the same level (potentially parallel)."""
            level_results = []
            continuation = None

            action_blocks = [b for b in block_list if b.get('type') == BLOCK_TYPE_ACTION]
            other_blocks = [b for b in block_list if b.get('type') != BLOCK_TYPE_ACTION]

            # Loop blocks (for_each) run their CHILDREN once per element of an upstream array,
            # binding {{item}} / {{item_index}} per iteration. Pulled out of the normal action
            # walk so their children aren't ALSO run once by the generic child recursion below.
            loop_blocks = [b for b in action_blocks if b.get('blockType') == 'for_each']
            action_blocks = [b for b in action_blocks if b.get('blockType') != 'for_each']
            for lb in loop_blocks:
                lb_id = lb.get('id', '')
                cfg = lb.get('config', {}) or {}
                source = cfg.get('source', '') or ''
                # Resolve the source dot-path against this branch's merged context.
                cur = build_context_for_branch(branch_name)
                for part in [p.strip() for p in source.split('.')] if source else ['']:
                    if isinstance(cur, dict):
                        cur = cur.get(part, None)
                    elif isinstance(cur, (list, tuple)) and part.isdigit():
                        idx = int(part)
                        cur = cur[idx] if 0 <= idx < len(cur) else None
                    else:
                        cur = None
                        break
                if isinstance(cur, list):
                    items = cur
                elif cur in (None, ''):
                    items = []
                else:
                    items = [cur]
                try:
                    max_items = int(cfg.get('max_items') or 1000)
                except (TypeError, ValueError):
                    max_items = 1000
                count = min(len(items), max(0, max_items))
                loop_children = children_by_parent.get(lb_id, [])
                for i, item in enumerate(items[:count]):
                    base_context['item'] = item
                    base_context['item_index'] = i
                    if branch_name:
                        branch_contexts.setdefault(branch_name, {}).update({'item': item, 'item_index': i})
                    if loop_children:
                        child_results, _child_cont = await process_tree_level(loop_children, branch_name)
                        level_results.extend(child_results)
                level_results.append({"block_id": lb_id, "type": "for_each", "iterations": count})

            # Process non-action blocks first (conditions, events)
            for block in other_blocks:
                result, cont, result_data = await process_block(block, branch_name)
                level_results.append(result)

                # Update context with result data
                if result_data:
                    base_context.update(result_data)
                    if branch_name:
                        if branch_name not in branch_contexts:
                            branch_contexts[branch_name] = {}
                        branch_contexts[branch_name].update(result_data)

                if cont:
                    if cont.get('stop'):
                        continue
                    else:
                        continuation = cont
                        return level_results, continuation

                block_id = block.get('id', '')
                if block_id in children_by_parent:
                    child_results, child_cont = await process_tree_level(children_by_parent[block_id], branch_name)
                    level_results.extend(child_results)
                    if child_cont and not child_cont.get('stop'):
                        continuation = child_cont
                        return level_results, continuation

            # Process action blocks - detect parallel branches
            if len(action_blocks) > 1:
                # Multiple action blocks at same level = parallel branches
                # NOTE: We execute sequentially to avoid SQLAlchemy AsyncSession concurrency issues
                # The branches are still logically parallel (all start from the same event)
                logger.info(f"[BlockChain] Executing {len(action_blocks)} parallel branches (sequentially for db safety)")

                all_continuations = []  # Track ALL branch continuations

                for branch_idx, block in enumerate(action_blocks, start=1):
                    br_name = f"branch{branch_idx}"

                    # Initialize branch context
                    if br_name not in branch_contexts:
                        branch_contexts[br_name] = {}

                    try:
                        result, cont, result_data = await process_block(block, br_name)
                        level_results.append(result)

                        # Store result data in branch context
                        if result_data:
                            branch_contexts[br_name].update(result_data)

                        block_id = block.get('id', '')
                        if cont and cont.get('stop'):
                            # on_error=stop → skip this branch's dependent children.
                            pass
                        elif block_id in children_by_parent:
                            child_results, child_cont = await process_tree_level(children_by_parent[block_id], br_name)
                            level_results.extend(child_results)
                            if child_cont and not child_cont.get('stop'):
                                # Store this branch's continuation with its branch name
                                child_cont['branch_name'] = br_name
                                all_continuations.append(child_cont)
                        elif cont and not cont.get('stop'):
                            cont['branch_name'] = br_name
                            all_continuations.append(cont)

                        logger.info(f"[BlockChain] Branch {br_name} completed successfully")

                    except Exception as e:
                        block_id = block.get('id', 'unknown')
                        logger.error(f"[BlockChain] Branch {br_name} ({block_id}) failed: {e}")
                        level_results.append({
                            "block_id": block_id,
                            "type": block.get('blockType', 'unknown'),
                            "status": "failed",
                            "error": str(e)
                        })
                        branch_contexts[br_name]['action_error'] = str(e)

                # If we have multiple continuations, we need to handle them specially
                if all_continuations:
                    if len(all_continuations) == 1:
                        continuation = all_continuations[0]
                    else:
                        # Multiple continuations - store them all in a combined continuation
                        # Each will be processed when its respective completion event fires
                        continuation = {
                            "multi_branch": True,
                            "continuations": all_continuations,
                            "context_snapshot": base_context.copy(),
                            "branch_contexts": branch_contexts.copy(),
                            "trigger_rule_id": trigger.id,
                        }
                        logger.info(f"[BlockChain] Stored {len(all_continuations)} branch continuations")

                logger.info(f"[BlockChain] All {len(action_blocks)} parallel branches completed")
                logger.debug(f"[BlockChain] Branch contexts: {list(branch_contexts.keys())}")
            else:
                # Single action block - process in current branch context
                for block in action_blocks:
                    result, cont, result_data = await process_block(block, branch_name)
                    level_results.append(result)

                    # Update context with result data
                    if result_data:
                        base_context.update(result_data)
                        if branch_name:
                            if branch_name not in branch_contexts:
                                branch_contexts[branch_name] = {}
                            branch_contexts[branch_name].update(result_data)

                    # on_error=stop → skip this action's dependent children.
                    if cont and cont.get('stop'):
                        continue

                    block_id = block.get('id', '')
                    if block_id in children_by_parent:
                        child_results, child_cont = await process_tree_level(children_by_parent[block_id], branch_name)
                        level_results.extend(child_results)
                        if child_cont and not child_cont.get('stop'):
                            continuation = child_cont

            return level_results, continuation

        # Process starting from root blocks (no branch initially)
        results, continuation = await process_tree_level(root_blocks, current_branch)

        return results, continuation

    async def process_block_continuation(
        self,
        trigger_rule_id: int,
        event_type: str,
        event_context: Dict[str, Any],
        continuation_data: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        Process a block chain continuation when a completion event fires.

        Context handling:
        - context_snapshot contains all data from the original trigger event (extracted.*, etc.)
        - event_context contains data from the completion event (session_name, status, result, etc.)
        - Both are merged so later blocks can access ALL data from the entire chain

        Args:
            trigger_rule_id: ID of the trigger rule
            event_type: The event that fired (e.g., 'ai_session_completed')
            event_context: Context from the completion event
            continuation_data: Data from the pending continuation

        Returns:
            Action results from processing remaining blocks
        """
        result = await self.db.execute(
            select(TriggerRule).where(TriggerRule.id == trigger_rule_id)
        )
        trigger = result.scalar_one_or_none()
        if not trigger:
            logger.error(f"[BlockChain] Trigger {trigger_rule_id} not found for continuation")
            return []

        remaining_blocks = continuation_data.get('remaining_blocks', [])
        context_snapshot = continuation_data.get('context_snapshot', {})
        branch_contexts = continuation_data.get('branch_contexts', {})
        current_branch = continuation_data.get('current_branch')

        # Skip the event block (first block) and prepare remaining blocks for processing
        # Make all direct children of the event block into root blocks
        if len(remaining_blocks) > 1:
            event_block_id = remaining_blocks[0].get('id')
            blocks_to_process = []
            for block in remaining_blocks[1:]:
                if block.get('parentId') == event_block_id:
                    # Direct child of the event block → promote to root
                    blocks_to_process.append({**block, 'parentId': None})
                else:
                    blocks_to_process.append(block)
        else:
            blocks_to_process = []

        # Merge contexts:
        # 1. Start with the original context (contains extracted.* from initial event)
        # 2. Add completion event data under a namespaced key to avoid collisions
        # 3. Also add commonly used fields at top level for convenience
        merged_context = context_snapshot.copy()

        # Add completion event data under namespace
        merged_context['completion_event'] = event_context.copy()

        # Add commonly used completion event fields at top level for easy access
        # These will be available as {{status}}, {{error}}, {{session_name}}, etc.
        completion_fields = [
            # Status and result
            'status', 'error', 'success', 'result',
            # AI session specific
            'session_id', 'session_name', 'session_completed_at', 'session_started_at',
            'session_duration_seconds', 'session_steps_taken', 'session_status', 'session_error',
            # Workflow specific
            'workflow_id', 'workflow_name', 'task_id',
            # Metrics
            'steps_taken', 'steps_completed', 'duration_ms', 'duration_seconds',
            # Timestamps (from completion event)
            'now', 'now_date', 'now_time', 'now_datetime', 'now_timestamp',
        ]
        for key in completion_fields:
            if key in event_context:
                merged_context[key] = event_context[key]

        # If completion event has result data with extracted values, make them available
        if isinstance(event_context.get('result'), dict):
            result_data = event_context['result']
            if 'extracted_data' in result_data:
                # Merge new extracted data with existing, prefixed to avoid collision
                merged_context['ai_result'] = result_data
                # Also make extracted data from AI session available
                for k, v in result_data.get('extracted_data', {}).items():
                    merged_context[f'ai_extracted.{k}'] = v

        # Update branch context with completion event data (for branch-scoped access)
        if current_branch:
            if current_branch not in branch_contexts:
                branch_contexts[current_branch] = {}
            # Add all completion data to the branch context
            completion_data = {
                # Status placeholders
                'status': event_context.get('status'),
                'success': event_context.get('success'),
                'error': event_context.get('error'),
                # AI Session specific
                'session_status': event_context.get('status'),
                'session_name': event_context.get('session_name'),
                'session_id': event_context.get('session_id'),
                'session_success': event_context.get('success'),
                'session_error': event_context.get('error'),
                'session_duration_ms': event_context.get('duration_ms'),
                'session_duration_seconds': event_context.get('duration_seconds'),
                'session_steps_taken': event_context.get('steps_taken'),
                'session_completed_at': event_context.get('session_completed_at'),
                # Workflow specific
                'workflow_status': event_context.get('status'),
                'workflow_name': event_context.get('workflow_name'),
                'workflow_id': event_context.get('workflow_id'),
                'workflow_task_id': event_context.get('task_id'),
                'workflow_steps_completed': event_context.get('steps_completed'),
                'workflow_error': event_context.get('error'),
                # Metrics
                'duration_ms': event_context.get('duration_ms'),
                'duration_seconds': event_context.get('duration_seconds'),
                'steps_taken': event_context.get('steps_taken'),
                'steps_completed': event_context.get('steps_completed'),
            }
            # Also add result data if present
            if isinstance(event_context.get('result'), dict):
                result = event_context['result']
                completion_data['final_url'] = result.get('final_url')
                completion_data['workflow_id'] = result.get('workflow_id')
                # Add extracted data from AI result
                if 'extracted_data' in result:
                    for k, v in result['extracted_data'].items():
                        completion_data[f'extracted_{k}'] = v

            # Filter out None values
            branch_contexts[current_branch].update({k: v for k, v in completion_data.items() if v is not None})

        logger.info(f"[BlockChain] Continuing chain with {len(blocks_to_process)} blocks in branch {current_branch}, context keys: {list(merged_context.keys())}")

        if not blocks_to_process:
            logger.info(f"[BlockChain] No action blocks to process after event block")
            return []

        # Process the action blocks with restored branch context
        results, next_continuation = await self._process_block_chain(
            trigger=trigger,
            blocks=blocks_to_process,
            context=merged_context,
            start_index=0,
            branch_contexts=branch_contexts,
            current_branch=current_branch
        )

        # If there's another continuation, we need to handle it
        # This happens if there's another completion event later in the chain
        if next_continuation:
            if next_continuation.get('multi_branch'):
                events = [c.get('continuation_event') for c in next_continuation.get('continuations', [])]
                logger.info(f"[BlockChain] Another multi-branch continuation pending: {events}")
            else:
                logger.info(f"[BlockChain] Another continuation pending: {next_continuation.get('continuation_event')}")
            # The next_continuation already has the updated context_snapshot from _process_block_chain

        return results

    def _render_template(self, template: str, context: Dict[str, Any]) -> str:
        """
        Render a template with {{ path | filter:arg | ... }} placeholders.

        Dot notation ({{extracted.price}}, {{result.output_files.0.file_id}}) plus an optional
        left-to-right chain of transform filters so templates can COMPUTE, not just substitute:
          {{ result.price | mul:1.1 | round:2 }}
          {{ payload.name | default:friend | upper }}
          {{ extracted.sku | match:([A-Z]+) }}
        Kept in sync with the desktop daemon's flow.rs::render_template.
        """
        def resolve_path(path: str):
            parts = path.split(".")
            value = context
            for part in parts:
                part = part.strip()
                if isinstance(value, dict):
                    value = value.get(part, "")
                elif isinstance(value, (list, tuple)) and part.isdigit():
                    # Numeric list index, e.g. {{result.output_files.0.file_id}} so a
                    # chained workflow can map a captured file by position (§4.5).
                    idx = int(part)
                    value = value[idx] if 0 <= idx < len(value) else ""
                else:
                    value = ""
                    break
            return value

        def replace_placeholder(match):
            segments = match.group(1).split("|")
            value = _to_render_str(resolve_path(segments[0].strip()))
            for spec in segments[1:]:
                value = _apply_template_filter(value, spec.strip())
            return value

        return re.sub(r"\{\{([^}]+)\}\}", replace_placeholder, template)

    def run_extractors(
        self,
        selector: TargetSelector,
        content: str
    ) -> Dict[str, Any]:
        """
        Run all extractors for a selector on given content.

        Useful for testing extractors without a full change detection cycle.
        """
        if not selector.extractors:
            return {}

        content_type = selector.content_type or 'text'
        extractor_configs = [e.to_dict() for e in selector.extractors if e.enabled]

        return extractor_service.extract_all(
            content=content,
            extractors=extractor_configs,
            content_type=content_type
        )

    def test_trigger(
        self,
        trigger: TriggerRule,
        test_content: str,
        test_extracted: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Test a trigger with mock data without actually dispatching actions.
        """
        mock_change = DetectedChange(
            target_id=trigger.target_id,
            diff_snippet=test_content[:500],
            content_hash="test_hash",
        )

        context = {
            "content": test_content,
            "extracted": test_extracted or {},
            "diff_snippet": test_content[:500],
        }

        match, reason = self._evaluate_conditions(trigger, mock_change, context)

        return {
            "would_fire": match,
            "match_reason": reason,
            "trigger_name": trigger.name,
            "trigger_enabled": trigger.enabled,
            "conditions": trigger.conditions,
            "actions_count": len(trigger.actions or []),
        }


# Create service factory function
def get_unified_trigger_service(db: AsyncSession) -> UnifiedTriggerService:
    return UnifiedTriggerService(db)
