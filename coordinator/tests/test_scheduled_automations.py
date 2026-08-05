"""Scheduled automations, end to end: accepted at create, stamped into the due
index, fired by the scheduler lane, advanced so they can't double-fire.

THE REGRESSION: the automation builder has always offered a "scheduled" root
block, and `POST /triggers` rejected it — `valid_event_types` never listed
`scheduled`. And acceptance alone would have been worse than the 400: nothing
stamped a next-fire time and no scheduler lane scanned for one, so the automation
would have been created and then silently never run. This suite pins every link
of the chain the cloud already has (VALID_EVENT_TYPES → _recompute_next_scheduled_at
→ trigger_rules.next_scheduled_at → scheduler due-scan → UnifiedTriggerService.
process_scheduled_automation), because any single missing link reproduces one of
those two failure modes.
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("WRIT_DB_PATH", os.path.join(tempfile.gettempdir(), "writ-sched-test.db"))


@pytest.fixture(scope="module")
def loop():
    lp = asyncio.new_event_loop()
    yield lp
    lp.close()


async def _fresh_schema():
    from database import Base, engine
    import models  # noqa: F401 — registers every table on Base.metadata

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)


def _aware(dt):
    """SQLite returns naive datetimes even for DateTime(timezone=True) columns;
    the stored values are UTC (compute_next_run emits aware-UTC), so pin the
    zone before comparing — the scheduler itself compares in SQL and never hits
    this."""
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def _scheduled_blocks(interval_ms: int = 60_000, mode: str = "interval") -> list[dict]:
    """The block tree the self-host builder actually submits: a scheduled ROOT
    event block (mode/interval_ms in config) with an action hanging off it."""
    return [
        {"id": "root", "type": "event", "blockType": "scheduled",
         "config": {"mode": mode, "interval_ms": interval_ms}, "parentId": None},
        {"id": "act", "type": "action", "blockType": "notification",
         "config": {"channels": ["email"]}, "parentId": "root"},
    ]


def _create_request(**over):
    from routers.triggers import TriggerRuleCreate, FlowBlock

    blocks = over.pop("blocks", _scheduled_blocks())
    payload = dict(
        event_type="scheduled",
        name="hourly report",
        enabled=True,
        blocks=[FlowBlock(**b) for b in blocks],
    )
    payload.update(over)
    return TriggerRuleCreate(**payload)


# ── create: accepted AND armed ────────────────────────────────────────────────

def test_create_accepts_scheduled_and_stamps_the_due_index(loop):
    """Both halves of the regression: the 400, and the silent never-fire that
    plain whitelisting would have left behind."""
    from database import AsyncSessionLocal
    from routers.triggers import create_trigger_rule
    from models.trigger_rule import TriggerRule
    from sqlalchemy import select

    async def main():
        await _fresh_schema()
        async with AsyncSessionLocal() as db:
            resp = await create_trigger_rule(
                _create_request(), db=db, current_api_key={"user_id": "owner"})
            rule = (await db.execute(
                select(TriggerRule).where(TriggerRule.id == resp.id))).scalar_one()
            return resp, rule.next_scheduled_at

    resp, next_at = loop.run_until_complete(main())
    assert resp.event_type == "scheduled"
    assert next_at is not None, "a scheduled-root rule must enter the due index at create"
    # An interval rule's first fire is one interval out, never "immediately at epoch".
    assert _aware(next_at) > datetime.now(timezone.utc)
    assert _aware(next_at) < datetime.now(timezone.utc) + timedelta(minutes=2)


def test_create_leaves_event_driven_rules_out_of_the_due_index(loop):
    """NULL is what keeps the due scan free for every non-scheduled automation."""
    from database import AsyncSessionLocal
    from routers.triggers import create_trigger_rule
    from models.trigger_rule import TriggerRule
    from sqlalchemy import select

    async def main():
        await _fresh_schema()
        async with AsyncSessionLocal() as db:
            resp = await create_trigger_rule(
                _create_request(
                    event_type="workflow_completed",
                    blocks=[
                        {"id": "root", "type": "event", "blockType": "workflow_completed",
                         "config": {}, "parentId": None},
                    ],
                ),
                db=db, current_api_key={"user_id": "owner"})
            rule = (await db.execute(
                select(TriggerRule).where(TriggerRule.id == resp.id))).scalar_one()
            return rule.next_scheduled_at

    assert loop.run_until_complete(main()) is None


def test_update_validates_event_type_like_create(loop):
    """The cloud closed this exact gap: update skipping validation lets an
    unfireable rule in through the side door."""
    from fastapi import HTTPException
    from database import AsyncSessionLocal
    from routers.triggers import create_trigger_rule, update_trigger_rule, TriggerRuleUpdate

    async def main():
        await _fresh_schema()
        async with AsyncSessionLocal() as db:
            resp = await create_trigger_rule(
                _create_request(), db=db, current_api_key={"user_id": "owner"})
            with pytest.raises(HTTPException) as exc:
                await update_trigger_rule(
                    resp.id, TriggerRuleUpdate(event_type="not_a_real_event"),
                    db=db, current_api_key={"user_id": "owner"})
            return exc.value.status_code

    assert loop.run_until_complete(main()) == 400


def test_toggle_is_the_due_index_switch(loop):
    """Disabling must clear next_scheduled_at (a paused rule never fires);
    re-enabling must re-arm it from the block recurrence."""
    from database import AsyncSessionLocal
    from routers.triggers import create_trigger_rule, toggle_trigger_rule
    from models.trigger_rule import TriggerRule
    from sqlalchemy import select

    async def main():
        await _fresh_schema()
        async with AsyncSessionLocal() as db:
            resp = await create_trigger_rule(
                _create_request(), db=db, current_api_key={"user_id": "owner"})

            async def stamp():
                return (await db.execute(
                    select(TriggerRule.next_scheduled_at)
                    .where(TriggerRule.id == resp.id))).scalar_one()

            await toggle_trigger_rule(resp.id, db=db, current_api_key={"user_id": "owner"})
            off = await stamp()
            await toggle_trigger_rule(resp.id, db=db, current_api_key={"user_id": "owner"})
            on = await stamp()
            return off, on

    off, on = loop.run_until_complete(main())
    assert off is None, "a disabled scheduled rule must leave the due index"
    assert on is not None, "re-enabling must re-arm the cadence"


# ── the scheduler lane ────────────────────────────────────────────────────────

def _arm_due(rule):
    """Backdate the stamp so the lane sees the rule as due NOW."""
    rule.next_scheduled_at = datetime.now(timezone.utc) - timedelta(seconds=5)


def test_due_rule_fires_once_and_advances(loop, monkeypatch):
    """The whole lane: due-scan → advance-first → fire through the shared trigger
    service → execution recorded → not due again until the next occurrence."""
    from database import AsyncSessionLocal
    from routers.triggers import create_trigger_rule
    from services import scheduler as sched
    from services.unified_trigger_service import UnifiedTriggerService
    from models.trigger_rule import TriggerRule, TriggerExecution
    from sqlalchemy import select

    dispatched = []

    async def _fake_dispatch(self, rule, context, execution):
        dispatched.append({"rule_id": rule.id, "event": context.get("event_type")})
        return [{"action": "notification", "status": "ok"}]

    monkeypatch.setattr(UnifiedTriggerService, "_dispatch_actions", _fake_dispatch)

    async def main():
        await _fresh_schema()
        async with AsyncSessionLocal() as db:
            resp = await create_trigger_rule(
                _create_request(), db=db, current_api_key={"user_id": "owner"})
            rule = (await db.execute(
                select(TriggerRule).where(TriggerRule.id == resp.id))).scalar_one()
            _arm_due(rule)
            await db.commit()

        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as db:
            fired_1 = await sched._dispatch_due_scheduled_automations(db, now)
            # A second sweep at the SAME instant must fire nothing — the cadence
            # was advanced before the fire (advance-first is the double-fire guard).
            fired_2 = await sched._dispatch_due_scheduled_automations(db, now)

        async with AsyncSessionLocal() as db:
            rule = (await db.execute(
                select(TriggerRule).where(TriggerRule.id == resp.id))).scalar_one()
            executions = (await db.execute(
                select(TriggerExecution)
                .where(TriggerExecution.trigger_rule_id == resp.id))).scalars().all()
            return fired_1, fired_2, rule, executions

    fired_1, fired_2, rule, executions = loop.run_until_complete(main())

    assert fired_1 == 1 and fired_2 == 0
    assert dispatched == [{"rule_id": rule.id, "event": "scheduled"}], (
        "the fire must go through the shared _dispatch_actions path")
    assert len(executions) == 1 and executions[0].status == "completed"
    assert rule.trigger_count == 1
    assert rule.last_triggered_at is not None, "an ACTUAL fire stamps the last-fire time"
    assert _aware(rule.next_scheduled_at) > datetime.now(timezone.utc), "cadence advanced"


def test_cooldown_measures_from_the_last_real_fire_not_the_advance(loop, monkeypatch):
    """The latent bug found while porting the cloud lane: stamping
    last_triggered_at during the ADVANCE made every cooldown-bearing scheduled
    automation suppress itself, forever and silently. A cooldown that already
    elapsed must not block the fire."""
    from database import AsyncSessionLocal
    from routers.triggers import create_trigger_rule
    from services import scheduler as sched
    from services.unified_trigger_service import UnifiedTriggerService
    from models.trigger_rule import TriggerRule
    from sqlalchemy import select

    async def _fake_dispatch(self, rule, context, execution):
        return [{"action": "notification", "status": "ok"}]

    monkeypatch.setattr(UnifiedTriggerService, "_dispatch_actions", _fake_dispatch)

    async def main():
        await _fresh_schema()
        async with AsyncSessionLocal() as db:
            resp = await create_trigger_rule(
                _create_request(conditions={"schedule": {"cooldown_minutes": 30}}),
                db=db, current_api_key={"user_id": "owner"})
            rule = (await db.execute(
                select(TriggerRule).where(TriggerRule.id == resp.id))).scalar_one()
            # Last REAL fire was an hour ago — the 30-minute cooldown has elapsed.
            rule.last_triggered_at = datetime.now(timezone.utc) - timedelta(hours=1)
            _arm_due(rule)
            await db.commit()

        async with AsyncSessionLocal() as db:
            return await sched._dispatch_due_scheduled_automations(
                db, datetime.now(timezone.utc))

    assert loop.run_until_complete(main()) == 1, (
        "an elapsed cooldown must not suppress the fire — if this is 0, the lane "
        "stamped last_triggered_at before firing again")


def test_stale_due_stamp_on_a_rerooted_rule_is_cleared_not_fired(loop, monkeypatch):
    """Blocks rewritten away from a scheduled root while next_scheduled_at
    lingered: the lane must drop it from the due index, not fire it every tick."""
    from database import AsyncSessionLocal
    from routers.triggers import create_trigger_rule
    from services import scheduler as sched
    from services.unified_trigger_service import UnifiedTriggerService
    from models.trigger_rule import TriggerRule
    from sqlalchemy import select

    async def _fake_dispatch(self, rule, context, execution):  # pragma: no cover
        raise AssertionError("a non-scheduled-root rule must never fire from this lane")

    monkeypatch.setattr(UnifiedTriggerService, "_dispatch_actions", _fake_dispatch)

    async def main():
        await _fresh_schema()
        async with AsyncSessionLocal() as db:
            resp = await create_trigger_rule(
                _create_request(), db=db, current_api_key={"user_id": "owner"})
            rule = (await db.execute(
                select(TriggerRule).where(TriggerRule.id == resp.id))).scalar_one()
            # Simulate the drift: root rewritten, stale stamp left behind.
            rule.blocks = [
                {"id": "root", "type": "event", "blockType": "workflow_completed",
                 "config": {}, "parentId": None},
            ]
            _arm_due(rule)
            await db.commit()

        async with AsyncSessionLocal() as db:
            fired = await sched._dispatch_due_scheduled_automations(
                db, datetime.now(timezone.utc))

        async with AsyncSessionLocal() as db:
            stamp = (await db.execute(
                select(TriggerRule.next_scheduled_at)
                .where(TriggerRule.id == resp.id))).scalar_one()
            return fired, stamp

    fired, stamp = loop.run_until_complete(main())
    assert fired == 0
    assert stamp is None, "the stale stamp must be cleared so it stops being due"


# ── recurrence parsing parity ─────────────────────────────────────────────────

def test_recurrence_parser_reads_the_builder_block_shape():
    """mode/interval_ms/time/days/tz is the ONE block shape shared by the
    self-host builder, the cloud scheduler and the desktop daemon. interval_minutes
    is the back-compat shim (×60000)."""
    from services.scheduler import scheduled_recurrence_from_blocks as parse

    assert parse(_scheduled_blocks(interval_ms=300_000)) == {
        "kind": "interval", "interval_ms": 300_000, "time": None, "days": None, "tz": None,
    }
    assert parse([
        {"id": "r", "type": "event", "blockType": "scheduled",
         "config": {"mode": "daily", "time": "09:00", "tz": "Europe/Paris"},
         "parentId": None},
    ]) == {"kind": "daily", "interval_ms": None, "time": "09:00", "days": None,
           "tz": "Europe/Paris"}
    assert parse([
        {"id": "r", "type": "event", "blockType": "scheduled",
         "config": {"interval_minutes": 5}, "parentId": None},
    ])["interval_ms"] == 300_000
    # Non-scheduled root, child scheduled block, and junk all parse to None.
    assert parse([{"id": "r", "type": "event", "blockType": "change_detected",
                   "config": {}, "parentId": None}]) is None
    assert parse([{"id": "c", "type": "event", "blockType": "scheduled",
                   "config": {}, "parentId": "r"}]) is None
    assert parse("not-a-tree") is None
    assert parse(None) is None
