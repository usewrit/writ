"""
RecorderCapacityManager — manages recorder slot allocation with reserved capacity.

Two orthogonal partitions of the recorder fleet:

1. ISOLATION TIER (Phase 3b) — the fleet is split into two physically distinct
   pools keyed by each agent's advertised tier: the warm-context SHARED pool and
   the gVisor ISOLATED pool. A sensitive run is admitted against the isolated
   bucket, a non-sensitive run against the shared bucket. When no isolated agents
   are connected, both collapse to one flat bucket (= pre-tier behavior).

2. TRAFFIC CLASS — WITHIN each tier, slots are reserved across direct (owner
   dashboard), called (marketplace/API/webhook), and scheduled traffic using a
   configurable ratio. Each class gets a guaranteed reservation but may borrow
   idle slots no other class needs.
"""
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from services.workflow_router import TrafficType

logger = logging.getLogger(__name__)


def _ratio(env_name: str, default: float) -> float:
    try:
        v = float(os.getenv(env_name, ""))
        return v if 0 < v < 1 else default
    except (TypeError, ValueError):
        return default


# Per-class share of total recorder capacity. Each class gets a GUARANTEED
# reservation (it can always start up to this many concurrent runs) but may also
# BORROW slots other classes aren't currently using (see acquire_slot). The
# scheduled share is the remainder so the three always sum to 1.0. Tunable via env.
DIRECT_RATIO = _ratio("CAPACITY_DIRECT_RATIO", 0.40)      # owner dashboard runs
CALLED_RATIO = _ratio("CAPACITY_CALLED_RATIO", 0.40)      # marketplace/API/webhook
# scheduled = 1 - direct - called

# --- Isolation tiers ----------------------------------------------------------
# Cloud capacity is partitioned into two physically distinct pools, keyed by each
# agent's advertised tier (Phase 3a): the warm-context SHARED pool and the gVisor
# ISOLATED pool. The DIRECT/CALLED/SCHEDULED reservations apply *within* each
# pool, so a sensitive (isolated) run and a non-sensitive (shared) run draw from
# their own slot budgets and never starve each other. An agent's tier comes from
# DB Agent.meta['tier'] (HTTP recorders) or the ws-gateway registry entry's
# 'tier' field (gateway agents); ABSENT ⇒ 'shared' (safe default).
TIER_SHARED = "shared"
TIER_ISOLATED = "isolated"
_TIERS = (TIER_SHARED, TIER_ISOLATED)


def _normalize_tier(value) -> str:
    """Map any tier value (DB meta / registry blob / None) to a known bucket.

    Anything that isn't an explicit 'isolated' falls back to 'shared' — the
    fail-safe default (pre-tier agents, missing data, typos) so a missing tier
    never accidentally counts as scarce isolated capacity.
    """
    return TIER_ISOLATED if str(value or "").strip().lower() == TIER_ISOLATED else TIER_SHARED


def _class_reservations(total_slots: int) -> dict:
    """Split ``total_slots`` into per-class guaranteed reservations.

    Floor each class's share, then hand any leftover slots out in priority
    order (direct → called → scheduled). Giving the remainder to scheduled
    (as a naive ``total - others`` would) is wrong: on a tiny fleet ``int()``
    truncation zeroes direct/called and dumps the WHOLE fleet into scheduled,
    so its reservation then blocks every interactive/called run from borrowing.
    Priority-ordered leftover keeps small fleets usable.

        _class_reservations(0)  == {'direct': 0, 'called': 0, 'scheduled': 0}
        _class_reservations(1)  == {'direct': 1, 'called': 0, 'scheduled': 0}
        _class_reservations(2)  == {'direct': 1, 'called': 1, 'scheduled': 0}
        # @ DIRECT=CALLED=0.40 → scheduled=0.20:
        _class_reservations(10) == {'direct': 4, 'called': 4, 'scheduled': 2}
        _class_reservations(5)  == {'direct': 2, 'called': 2, 'scheduled': 1}
    """
    reserved = {
        'direct': int(total_slots * DIRECT_RATIO),
        'called': int(total_slots * CALLED_RATIO),
        'scheduled': int(total_slots * max(0.0, 1.0 - DIRECT_RATIO - CALLED_RATIO)),
    }
    leftover = total_slots - sum(reserved.values())
    for k in ('direct', 'called', 'scheduled'):
        if leftover <= 0:
            break
        reserved[k] += 1
        leftover -= 1
    return reserved


class RecorderCapacityManager:

    def __init__(self, db: AsyncSession, direct_ratio: float = None):
        self.db = db
        # `direct_ratio` kept for backward-compat callers; ratios are otherwise
        # the module-level class shares.
        self.direct_ratio = direct_ratio if direct_ratio is not None else DIRECT_RATIO

    async def get_total_capacity(self) -> dict:
        """Query all active recorders and return capacity snapshot."""
        from models.agent import Agent, AgentStatus, Platform

        result = await self.db.execute(
            select(Agent)
            .where(Agent.platform == Platform.RECORDER)
            .where(Agent.status == AgentStatus.ACTIVE)
        )
        recorders = result.scalars().all()

        now = datetime.now(timezone.utc)
        total_slots = 0
        used_slots = 0
        active_recorders = 0
        counted_ids = set()

        # Per-tier slot tallies (shared vs isolated). Each agent contributes its
        # slots to exactly ONE bucket, chosen by its advertised tier. ``agent_tier``
        # maps agent_id → tier so class_can_start can bucket RUNNING tasks (by their
        # executor) the same way it bucketed CAPACITY here.
        tier_total = {TIER_SHARED: 0, TIER_ISOLATED: 0}
        tier_used = {TIER_SHARED: 0, TIER_ISOLATED: 0}
        agent_tier: dict = {}

        for r in recorders:
            if r.last_seen_at:
                last = r.last_seen_at.replace(tzinfo=timezone.utc) if r.last_seen_at.tzinfo is None else r.last_seen_at
                if (now - last).total_seconds() > 120:
                    continue
            meta = r.meta or {}
            if not meta.get('recorder_url'):
                continue
            active_recorders += 1
            counted_ids.add(r.agent_id)
            r_max = meta.get('max_sessions', 5)
            r_used = meta.get('active_sessions', 0)
            total_slots += r_max
            used_slots += r_used
            # DB recorder tier lives in Agent.meta['tier'] (recorder_proxy stamps it;
            # only an infrastructure agent can be 'isolated').
            t = _normalize_tier(meta.get('tier'))
            tier_total[t] += r_max
            tier_used[t] += r_used
            agent_tier[r.agent_id] = t

        # Directly-connected fleet: agents hold a persistent socket on THIS
        # coordinator (/ws/ai-gateway) instead of registering an HTTP recorder_url,
        # so the DB loop above (which requires meta['recorder_url']) can't see them.
        # Without counting them here they'd contribute ZERO capacity, acquire_slot()
        # would short-circuit to None, and QUEUED tasks would never drain to the
        # cloud fleet — even though _pick_recorder (reading the SAME in-process
        # fleet) would happily pick them. Mirror _pick_recorder: count each connected
        # agent's slots, de-duped against the DB-counted agents. Liveness = an open
        # socket in _connections (a stronger signal than the old registry's
        # heartbeatAt window). The external ws-gateway Redis registry no longer
        # exists — this replaces its now-always-empty read.
        try:
            from routers.user_recorder_ws import get_connected_recorders
            for rec in get_connected_recorders():
                agent_id = rec.get("agent_id")
                if not agent_id or agent_id in counted_ids:
                    continue
                active_recorders += 1
                counted_ids.add(agent_id)
                g_max = rec.get("max_sessions", 2)
                g_used = rec.get("active_sessions", 0)
                total_slots += g_max
                used_slots += g_used
                # Direct-socket agents don't advertise an isolation tier (that lived
                # in the ws-gateway registry blob); absent ⇒ shared, the fail-safe
                # default (_normalize_tier).
                t = _normalize_tier(rec.get("tier"))
                tier_total[t] += g_max
                tier_used[t] += g_used
                agent_tier[agent_id] = t
        except Exception as e:
            logger.warning(f"[capacity] connected-fleet capacity read failed: {e}")

        # Flat (whole-fleet) per-class reservations — UNCHANGED behavior for any
        # reader that ignores tiers (stats endpoint, free-tier ceiling, the queue
        # processor's slots_left cap). class_can_start uses the PER-TIER buckets
        # below when a required_tier is given.
        reserved = _class_reservations(total_slots)
        direct_reserved = reserved['direct']
        called_reserved = reserved['called']
        scheduled_reserved = reserved['scheduled']

        # Per-tier view: each pool gets its own total/used/available + its own
        # direct/called/scheduled reservation split. ``by_tier`` is the
        # authoritative structure for tier-aware admission.
        by_tier = {}
        for t in _TIERS:
            t_total = tier_total[t]
            t_used = tier_used[t]
            by_tier[t] = {
                'total_slots': t_total,
                'used_slots': t_used,
                'available_slots': max(0, t_total - t_used),
                'reserved': _class_reservations(t_total),
            }

        # ``has_isolated_pool`` distinguishes "no isolated agents are connected"
        # (treat everything as one shared bucket — fail-safe = current behavior)
        # from "an isolated pool exists" (partition the buckets). When the pool is
        # absent, an isolated run still admits against the shared bucket, matching
        # _pick_recorder's prefer-isolated-fallback-shared behavior.
        has_isolated_pool = tier_total[TIER_ISOLATED] > 0

        # Per-class QUEUED demand. A reservation exists to protect a class under
        # CONTENTION; without knowing who is actually waiting, the borrow test in
        # `_admit` treats every other class's full reservation as spoken-for even on
        # a completely idle fleet, which deadlocks small fleets (see _admit).
        waiting = await self._count_queued_by_traffic()

        return {
            'total_slots': total_slots,
            'used_slots': used_slots,
            'available_slots': max(0, total_slots - used_slots),
            'active_recorders': active_recorders,
            # Per-class guaranteed reservations (slots a class can always use).
            'reserved': {
                'direct': direct_reserved,
                'called': called_reserved,
                'scheduled': scheduled_reserved,
            },
            # Back-compat flat keys (stats endpoint / older readers).
            'direct_reserved': direct_reserved,
            'called_reserved': called_reserved,
            'scheduled_reserved': scheduled_reserved,
            # --- Tier partition (Phase 3b) ---
            'by_tier': by_tier,
            'has_isolated_pool': has_isolated_pool,
            # agent_id → 'shared'|'isolated', so RUNNING tasks can be bucketed by
            # their executor exactly as CAPACITY was bucketed above.
            'agent_tier': agent_tier,
            # Per-class queued-task counts — which classes actually have demand.
            'waiting': waiting,
        }

    async def _count_queued_by_traffic(self) -> dict:
        """Queued (not yet dispatched) task counts per traffic class.

        One GROUP BY per capacity snapshot — the snapshot is computed once per
        dispatch cycle, so this is not a per-task cost. A legacy/NULL
        queue_traffic_type counts as 'direct', matching dequeue_batch.
        """
        from models.automation_task import AutomationTask

        out = {'direct': 0, 'called': 0, 'scheduled': 0}
        try:
            rows = (await self.db.execute(
                select(AutomationTask.queue_traffic_type, func.count(AutomationTask.id))
                .where(AutomationTask.status == "queued")
                .where(AutomationTask.queue_expires_at > datetime.utcnow())
                .group_by(AutomationTask.queue_traffic_type)
            )).all()
        except Exception as e:  # noqa: BLE001 — admission must never wedge on this
            logger.warning(f"queued-demand tally failed, assuming no waiters: {e}")
            return out
        for traffic, n in rows:
            key = (traffic or 'direct')
            if key in out:
                out[key] += int(n or 0)
        return out

    async def _count_running_tasks_by_traffic(self, traffic_type: str) -> int:
        """Count currently running tasks by traffic type."""
        from models.automation_task import AutomationTask

        result = await self.db.scalar(
            select(func.count(AutomationTask.id))
            .where(AutomationTask.status == "running")
            .where(AutomationTask.queue_traffic_type == traffic_type)
        )
        return result or 0

    async def _running_by_class_and_tier(self, agent_tier: dict) -> dict:
        """Bucket currently-running tasks by (traffic class × isolation tier).

        AutomationTask has no tier column, so we derive each running task's tier
        from its executor: ``executor_agent_id`` → ``agent_tier[...]`` (the same
        map get_total_capacity built when it bucketed CAPACITY). Any task whose
        executor isn't in the map — a stale executor, a just-finished agent, an
        own-LOCAL run, or a pre-tier row — buckets to SHARED, the fail-safe
        default (matches _normalize_tier and never inflates scarce isolated
        running counts).

        Returns ``{tier: {'direct': n, 'called': n, 'scheduled': n}}``.

        Example (agent_tier = {A:'isolated', B:'shared'}; running rows:
        2×direct@A, 1×called@A, 3×direct@B, 1×scheduled@unknown):
            → {'isolated': {'direct':2,'called':1,'scheduled':0},
               'shared':   {'direct':3,'called':0,'scheduled':1}}
        """
        from models.automation_task import AutomationTask

        rows = await self.db.execute(
            select(
                AutomationTask.queue_traffic_type,
                AutomationTask.executor_agent_id,
                func.count(AutomationTask.id),
            )
            .where(AutomationTask.status == "running")
            .group_by(
                AutomationTask.queue_traffic_type,
                AutomationTask.executor_agent_id,
            )
        )
        out = {
            TIER_SHARED: {'direct': 0, 'called': 0, 'scheduled': 0},
            TIER_ISOLATED: {'direct': 0, 'called': 0, 'scheduled': 0},
        }
        for traffic, exec_id, cnt in rows.all():
            c = str(traffic or "").strip().lower()
            if c not in ('direct', 'called', 'scheduled'):
                # A NULL/unknown traffic type can't be charged against any class's
                # fairness budget; skip it (it still counts against the registry's
                # real free-slot ceiling, which is the hard limit).
                continue
            t = agent_tier.get(exec_id, TIER_SHARED)
            t = t if t in out else TIER_SHARED
            out[t][c] += int(cnt or 0)
        return out

    async def seed_running_tally(self, capacity: dict) -> dict:
        """Snapshot the running-task tally a batch caller can carry across one
        dispatch cycle (instead of re-running the running-count query per task).

        Returns ``{'flat': {direct,called,scheduled},
                   'by_tier': {shared:{...}, isolated:{...}}}``.

        ``flat`` mirrors ``_count_running_tasks_by_traffic`` for each class (the
        running counts the FLAT admission path reads). ``by_tier`` mirrors
        ``_running_by_class_and_tier`` (what the TIER-aware path reads). The
        caller increments the relevant entry by 1 as each task is dispatched —
        exactly what the per-task DB re-query saw after autoflush of the prior
        task's status="running" — then passes the right slice to acquire_slot as
        ``running_override``.
        """
        by_tier = await self._running_by_class_and_tier(capacity.get('agent_tier') or {})
        flat = {
            'direct': await self._count_running_tasks_by_traffic('direct'),
            'called': await self._count_running_tasks_by_traffic('called'),
            'scheduled': await self._count_running_tasks_by_traffic('scheduled'),
        }
        return {'flat': flat, 'by_tier': by_tier}

    @staticmethod
    def tally_increment(tally: dict, traffic_class: str, executor_tier: str) -> None:
        """Increment the in-memory running tally for a just-dispatched task.

        Mirrors what the DB would show on the NEXT per-task admission query:
        ``_count_running_tasks_by_traffic`` counts by class regardless of tier
        (flat view), while ``_running_by_class_and_tier`` buckets by the EXECUTOR
        agent's tier. ``executor_tier`` therefore comes from the chosen recorder's
        agent_id via ``capacity['agent_tier']`` (SHARED fail-safe when absent),
        not from the run's requested tier.
        """
        c = str(traffic_class or "").strip().lower()
        if c not in ('direct', 'called', 'scheduled'):
            return
        flat = tally.get('flat')
        if isinstance(flat, dict):
            flat[c] = flat.get(c, 0) + 1
        t = executor_tier if executor_tier in (TIER_SHARED, TIER_ISOLATED) else TIER_SHARED
        bucket = (tally.get('by_tier') or {}).get(t)
        if isinstance(bucket, dict):
            bucket[c] = bucket.get(c, 0) + 1

    @staticmethod
    def tier_slot_decrement(capacity: dict, executor_tier: str) -> None:
        """Shrink the per-cycle per-tier free-slot snapshot as a task dispatches.

        The flat admission path is bounded by the caller's in-memory ``slots_left``
        ceiling, but the TIER-aware path in ``class_can_start`` tests
        ``by_tier[tier]['available_slots']`` from the frozen per-cycle snapshot.
        Without decrementing it, a burst of same-tier runs all admit against the
        same frozen count and over-pile one pool (e.g. every isolated run forced
        onto a single isolated box while shared boxes sit idle, because the
        gateway registry's active-session counts lag within the cycle). Decrement
        the chosen EXECUTOR tier's snapshot slot here so the next admission in the
        same cycle sees the shrinking budget — exactly as the flat ``slots_left``
        ceiling already does. Bucket by executor tier (SHARED fail-safe), matching
        ``tally_increment``.
        """
        by_tier = capacity.get('by_tier')
        if not isinstance(by_tier, dict):
            return
        t = executor_tier if executor_tier in (TIER_SHARED, TIER_ISOLATED) else TIER_SHARED
        bucket = by_tier.get(t)
        if isinstance(bucket, dict):
            bucket['available_slots'] = max(0, bucket.get('available_slots', 0) - 1)

    @staticmethod
    def running_override_for(tally: dict, capacity: dict, required_tier: str) -> dict:
        """Pick the running-tally slice acquire_slot should use this iteration.

        Matches class_can_start's branch selection: the TIER-aware path (an
        isolated pool is connected) reads the requested tier's by_tier slice; the
        FLAT path reads the flat slice. Returns the dict to pass as
        ``running_override``.
        """
        if capacity.get('by_tier') and capacity.get('has_isolated_pool', False):
            want_tier = _normalize_tier(required_tier)
            return (tally.get('by_tier') or {}).get(want_tier, {})
        return tally.get('flat') or {}

    @staticmethod
    def _admit(c: str, available: int, reserved: dict, running: dict,
               waiting: dict = None) -> bool:
        """Reserve-and-borrow admission test for one class within ONE slot budget.

        A class may start a run when EITHER:
          - it is still under its own guaranteed reservation
            (running[C] < reserved[C]), OR
          - there is a genuinely idle slot to BORROW — a free slot that is not
            needed to satisfy any OTHER class's unmet reservation *for work that
            class actually has waiting*.

        This guarantees each class its share under contention while allowing full
        utilization of slots no one else is using.

        ``waiting`` is the per-class count of QUEUED tasks. It is what makes a
        reservation protect against CONTENTION rather than fence off idle
        capacity, and without it small fleets deadlock:

            reserved = _class_reservations(2) = {direct:1, called:1, scheduled:0}
            a crawl shard (scheduled) on a COMPLETELY IDLE 2-slot fleet:
              rule 1 — running 0 < reserved 0                      → False
              rule 2 — headroom_others = 1 + 1 = 2, borrowable = 0 → False
            → denied forever. Any fleet under 6 slots gives `scheduled` a
            reservation of 0 (the leftover is handed out direct→called→scheduled),
            so on a typical self-host — one agent, a handful of slots — scheduled
            work could NEVER dispatch even with nothing running. Dragnet crawl
            shards are minted as `scheduled`, which is why a crawl would sit
            queued until it expired: "the crawl never launches".

        With ``waiting``, an idle fleet has no unmet demand to protect, so the
        idle slot is borrowable and the shard starts. Under real contention the
        guarantees behave exactly as before.

        ``waiting=None`` preserves the original (contention-blind) behavior for
        any caller that has no queue snapshot.

        Worked examples (reserved={direct:4,called:4,scheduled:2}):
          - direct under guarantee: available=1,running={direct:2,...} → True
          - scheduled AT its guarantee, 'called' work QUEUED: available=2,
            running={direct:8,called:0,scheduled:2}, waiting={called:3}
            → rule 1: 2<2 false; others' unmet headroom = called 4
            → borrowable=2-4<0 → False (the 2 free slots are held for 'called')
          - same, but NOTHING queued for 'called': waiting={direct:0,called:0}
            → no other class has demand → headroom_others=0 → borrowable=2 → True
          - scheduled with reservation 0 on an idle 2-slot fleet (the crawl case):
            rule 1 can never pass, and with no waiters rule 2 now yields
            borrowable=2 → True
        """
        if available <= 0:
            return False
        # Under this class's own guarantee → always admit (a free agent exists).
        if running.get(c, 0) < reserved.get(c, 0):
            return True
        # Otherwise only borrow a free slot that no OTHER under-served class needs
        # to meet its guarantee — and only count a class as needing one when it
        # actually has work waiting for it.
        reserved_headroom_others = sum(
            max(0, reserved.get(x, 0) - running.get(x, 0))
            for x in ('direct', 'called', 'scheduled')
            if x != c and (waiting is None or waiting.get(x, 0) > 0)
        )
        borrowable = max(0, available - reserved_headroom_others)
        return borrowable > 0

    async def class_can_start(
        self,
        traffic_type: TrafficType,
        capacity: dict = None,
        required_tier: str = None,
        running_override: dict = None,
    ) -> bool:
        """Reserve-and-borrow admission test for one traffic class within the
        run's isolation tier.

        ``required_tier`` selects which slot budget the run is admitted against:
        an isolated run draws from the ISOLATED bucket, a shared/None run from
        the SHARED bucket. The direct/called/scheduled reservations apply WITHIN
        the chosen bucket, so the two pools never trade fairness.

        ``running_override`` (a ``{direct,called,scheduled}`` dict) lets a batch
        caller supply the per-class running tally for the run's tier that it
        maintains in memory across a single dispatch cycle (incremented as each
        task starts), so we skip the per-task running-count query. When omitted,
        the running counts are queried from the DB — the exact pre-batch behavior.

        FAIL-SAFE: when no isolated agents are connected (``has_isolated_pool``
        is False) — or tier data is otherwise missing — we collapse to the single
        flat fleet budget, i.e. the EXACT pre-tier behavior. An isolated run then
        admits against the whole fleet, mirroring _pick_recorder's
        prefer-isolated-fallback-shared (the run still executes ephemeral, just
        without gVisor until the pool exists).
        """
        if capacity is None:
            capacity = await self.get_total_capacity()
        if capacity['total_slots'] <= 0:
            return False

        c = traffic_type.value if isinstance(traffic_type, TrafficType) else str(traffic_type)
        want_tier = _normalize_tier(required_tier)
        by_tier = capacity.get('by_tier')
        has_isolated_pool = capacity.get('has_isolated_pool', False)

        # --- Tier-aware path: a real two-pool fleet ----------------------------
        # Only partition when an isolated pool actually exists AND we have the
        # per-tier structures. Otherwise fall through to the flat budget so shared
        # behavior is byte-for-byte unchanged.
        if by_tier and has_isolated_pool:
            bucket = by_tier.get(want_tier) or by_tier.get(TIER_SHARED)
            # The REGISTRY-derived per-tier free count is the real ceiling for this
            # pool (idle agents right now); stale "running" rows can't fabricate or
            # starve it. DB per-class running counts only govern FAIRNESS.
            available = bucket.get('available_slots', 0)
            if available <= 0:
                return False
            reserved = bucket.get('reserved') or {}
            if running_override is not None:
                running = running_override
            else:
                agent_tier = capacity.get('agent_tier') or {}
                running_by_tier = await self._running_by_class_and_tier(agent_tier)
                running = running_by_tier.get(want_tier, {})
            # Queued demand is fleet-wide (a queued task has no tier yet), so the
            # same tally gates borrowing in both pools.
            return self._admit(c, available, reserved, running, capacity.get('waiting'))

        # --- Flat path (no isolated pool): identical to pre-tier behavior ------
        # The REGISTRY's free-slot count is the real ceiling — it reflects agents
        # actually idle right now, so stale/zombie "running" DB rows can't fabricate
        # or starve capacity. DB per-class running counts only govern FAIRNESS
        # (which waiting class gets the next genuinely free slot).
        available = capacity['available_slots']
        if available <= 0:
            return False
        reserved = capacity['reserved']
        if running_override is not None:
            running = running_override
        else:
            running = {
                'direct': await self._count_running_tasks_by_traffic('direct'),
                'called': await self._count_running_tasks_by_traffic('called'),
                'scheduled': await self._count_running_tasks_by_traffic('scheduled'),
            }
        return self._admit(c, available, reserved, running, capacity.get('waiting'))

    async def acquire_slot(
        self,
        workflow,
        traffic_type: TrafficType,
        preferred_agent_id: str = None,
        consumer_tenant_id=None,
        required_tier: str = None,
        capacity: dict = None,
        running_override: dict = None,
    ) -> Optional[dict]:
        """Try to acquire a recorder slot respecting the per-class reservation
        split (direct / called / scheduled), with idle-slot borrowing.

        Returns recorder dict or None if this class has no admissible slot.

        ``consumer_tenant_id`` scopes the user-hosted (local) tier to a marketplace
        BUYER's own agents, so a queued consumer run never lands on the creator's
        local agent (its credentials would otherwise execute on the creator's
        machine). Trusted cloud remains tenant-agnostic.

        ``capacity`` lets a batch caller (the queue processor / central scheduler)
        pass a capacity snapshot it already computed this cycle, so we don't
        re-run the full Agent scan + ws-gateway registry read once PER task.
        ``running_override`` similarly lets the caller pass the per-class running
        tally (for the run's tier) it maintains in memory and increments as each
        task dispatches, avoiding the per-task COUNT GROUP BY. When either is
        omitted the method computes it itself — the exact pre-batch behavior.
        """
        from routers.automation import _pick_recorder

        if capacity is None:
            capacity = await self.get_total_capacity()
        if capacity['available_slots'] <= 0:
            return None
        # Admit this run against ITS tier's slot budget (isolated runs draw from
        # the isolated bucket, shared/None from the shared bucket). Without an
        # isolated pool connected this collapses to the flat fleet budget — the
        # exact pre-tier admission test. The downstream _pick_recorder still does
        # the prefer-isolated-fallback-shared selection with this same
        # required_tier, so admission and selection agree on the bucket.
        if not await self.class_can_start(
            traffic_type, capacity, required_tier=required_tier,
            running_override=running_override,
        ):
            return None
        # Pass the traffic class through so the selector routes by speed: a
        # SCHEDULED/CALLED queue drain takes throughput boxes; a queued DIRECT
        # run keeps its fast-if-idle eligibility.
        tt = traffic_type.value if isinstance(traffic_type, TrafficType) else str(traffic_type)
        # Self-host is single-owner: _pick_recorder has NO consumer/tenant scoping
        # (no marketplace buyers), so consumer_tenant_id (always None here) is not
        # forwarded. Forwarding it would raise TypeError (unexpected kwarg) which —
        # inside the queue drainer's per-cycle try/except — silently stalls the drain
        # so queued overflow never dispatches.
        return await _pick_recorder(
            self.db, workflow, preferred_agent_id,
            is_scheduled=(tt == "scheduled"),
            traffic_type=tt,
            required_tier=required_tier,
        )


# ============================================================================
# TIER-PARTITION MATH — tests in comments (Phase 3b)
# ============================================================================
# Notation: agents advertise tier via DB Agent.meta['tier'] (HTTP recorders) or
# the ws-gateway registry blob's 'tier' field (gateway agents); absent ⇒ shared.
# DIRECT_RATIO=CALLED_RATIO=0.40 → scheduled share = 0.20 (module defaults).
# _class_reservations(N) floors each share then hands leftover direct→called→
# scheduled, so for N=10 → {direct:4, called:4, scheduled:2}.
#
# ---------------------------------------------------------------------------
# CASE 1 — No isolated agents (fail-safe = pre-tier behavior)
#   Fleet: agent S (shared, max 10, used 3). No isolated agent.
#   get_total_capacity():
#     total_slots=10, used=3, available=7
#     tier_total={shared:10, isolated:0}; has_isolated_pool=False
#     by_tier['shared'] = {total:10, used:3, avail:7, reserved:{4,4,2}}
#     by_tier['isolated'] = {total:0, used:0, avail:0, reserved:{0,0,0}}
#   class_can_start(DIRECT, required_tier='isolated'):
#     has_isolated_pool False → FLAT path → admit against whole fleet (avail=7).
#     ⇒ identical to old behavior. An isolated run is NOT stranded by an empty
#       isolated bucket; _pick_recorder then falls back to shared (no gVisor yet).
#   class_can_start(DIRECT, required_tier=None): same flat path. ✓ unchanged.
#
# ---------------------------------------------------------------------------
# CASE 2 — Two-pool fleet, isolated run admitted against isolated budget
#   Fleet: agent S (shared, max 8, used 8 — FULL),
#          agent I (isolated, max 4, used 1).
#   get_total_capacity():
#     total=12, used=9, available=3 (flat)
#     tier_total={shared:8, isolated:4}; has_isolated_pool=True
#     by_tier['shared']   = {total:8, used:8, avail:0, reserved:{3,3,2}}*
#     by_tier['isolated'] = {total:4, used:1, avail:3, reserved:{2,2,0}}
#       *_class_reservations(8)={direct:3,called:3,scheduled:2}
#   class_can_start(DIRECT, required_tier='isolated'):
#     bucket=isolated, available=3>0. running_by_tier counts tasks whose executor
#     is agent I. Say isolated running={direct:1}. reserved.direct=2 → 1<2 → ADMIT.
#     ⇒ The isolated run starts even though the SHARED pool is saturated. The two
#       pools don't trade fairness. ✓
#   class_can_start(DIRECT, required_tier=None / 'shared'):
#     bucket=shared, available=0 → False. A shared run correctly queues because
#     the shared pool is full — it does NOT borrow the idle isolated slots
#     (isolated capacity is reserved for sensitive work). ✓
#
# ---------------------------------------------------------------------------
# CASE 3 — Within-tier reserve-and-borrow still holds
#   Isolated bucket: total=10 → reserved={direct:4,called:4,scheduled:2},
#   available=2, isolated running={direct:8, called:0, scheduled:0}.
#   class_can_start(SCHEDULED, 'isolated'):
#     running.scheduled=0 < reserved.scheduled=2 → ADMIT (under own guarantee). ✓
#   class_can_start(DIRECT, 'isolated'):
#     running.direct=8 ≥ reserved.direct=4 → not under guarantee.
#     reserved_headroom_others = max(0,4-0)+max(0,2-0) = 6 (called+scheduled unmet)
#     borrowable = max(0, available 2 − 6) = 0 → DENY.
#     ⇒ The 2 free isolated slots are held for the starved called/scheduled
#       classes; a greedy direct flood can't consume the whole isolated pool. ✓
#
# ---------------------------------------------------------------------------
# CASE 4 — Running-task bucketing by executor tier
#   agent_tier = {A:'isolated', B:'shared'}. Running rows:
#     2×direct@A, 1×called@A, 3×direct@B, 1×scheduled@(unknown executor).
#   _running_by_class_and_tier(agent_tier):
#     A→isolated: {direct:2, called:1}; B→shared: {direct:3};
#     unknown executor → SHARED fail-safe: {scheduled:1}.
#   ⇒ {'isolated': {direct:2,called:1,scheduled:0},
#      'shared':   {direct:3,called:0,scheduled:1}}
#   An own-LOCAL run or a just-finished agent (executor not in map) never inflates
#   the scarce isolated running count — it lands in shared. ✓
# ============================================================================
