"""Per-target live check state in Redis.

A check that finds NO change must not write to Postgres — otherwise every target ×
every agent × every interval is a row insert. Instead the hot path records the
last check (timestamp + state) here in Redis (cheap, TTL'd), and only writes the
DB (UptimeCheck / Report / DetectedChange + baseline) when the result actually
CHANGES. The admin UI / fleet check read "last check" and "state" from here.

State hash per target (`mon:state:<target_id>`):
  checked_at      ms epoch of the newest check
  state           up | down | ok | stale    (current health)
  is_up           "1"/"0"  (uptime targets, for change detection)
  status_code     last HTTP status (uptime)
  last_change_at  ms epoch of the last STATE CHANGE (→ also a DB row)
  stale_fired     "1" once sweep_stale has emitted monitor_stale for this edge
                  (cleared by the next fresh check, which then emits recovered)

Health transitions between the healthy ({up, ok}) and unhealthy ({down, stale})
sets are surfaced to the trigger engine as monitor_down / monitor_stale /
monitor_recovered events — see _health_transition / sweep_stale.
"""
import time
from typing import Any, Dict, List, Optional, Tuple

from utils.redis_client import get_redis_bytes

_PREFIX = "mon:state:"
_TTL_SECONDS = 7 * 24 * 3600  # a week of inactivity → drop

# Health-state semantics for monitor-health TRIGGER events (monitor_down /
# monitor_stale / monitor_recovered). "up"/"ok" = reachable & healthy; "down" =
# uptime probe failed; "stale" = no fresh check within the expected cadence
# (written by sweep_stale). A health trigger fires only on the EDGE between the
# healthy and unhealthy sets — never on every steady-state check.
_HEALTHY_STATES = {"up", "ok"}
_UNHEALTHY_STATES = {"down", "stale"}

# Event names dispatched to the trigger engine (mirror TriggerRule.event_type).
MONITOR_DOWN = "monitor_down"
MONITOR_STALE = "monitor_stale"
MONITOR_RECOVERED = "monitor_recovered"


def _key(target_id: int) -> str:
    return f"{_PREFIX}{target_id}"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _s(v: Any) -> Optional[str]:
    """Decode a Redis hash value (bytes|str|None) to str|None."""
    return v.decode() if isinstance(v, bytes) else v


def _health_transition(prev_state: Optional[str], new_state: str) -> Optional[str]:
    """The monitor-health event for a prev→new state edge, or None if no edge was
    crossed. The first-ever check (prev None) never fires — we react only to real
    transitions between the healthy and unhealthy sets."""
    if prev_state is None:
        return None
    was_healthy = prev_state in _HEALTHY_STATES
    now_healthy = new_state in _HEALTHY_STATES
    if was_healthy and not now_healthy:
        return MONITOR_DOWN
    if (not was_healthy) and now_healthy:
        return MONITOR_RECOVERED
    return None


async def record_uptime(
    target_id: int, is_up: bool, status_code: Optional[int]
) -> Tuple[bool, Optional[str]]:
    """Record an uptime check. Returns (changed, health_event):
      changed      — True if up/down or the status code CHANGED (caller writes a DB row).
      health_event — MONITOR_DOWN / MONITOR_RECOVERED if this check crossed a health
                     edge, else None (caller dispatches it as a trigger event).
    Always refreshes the Redis last-check and clears any pending stale marker."""
    r = get_redis_bytes()
    key = _key(target_id)
    prev = await r.hgetall(key)
    prev_up = _s(prev.get(b"is_up"))
    prev_code = _s(prev.get(b"status_code"))
    prev_state = _s(prev.get(b"state"))

    cur_up = "1" if is_up else "0"
    cur_code = str(status_code) if status_code is not None else None
    changed = (prev_up is None) or (cur_up != prev_up) or (cur_code != prev_code)

    new_state = "up" if is_up else "down"
    health_event = _health_transition(prev_state, new_state)

    now = _now_ms()
    mapping: Dict[str, Any] = {"checked_at": now, "is_up": cur_up, "state": new_state}
    if cur_code is not None:
        mapping["status_code"] = cur_code
    if changed or health_event:
        mapping["last_change_at"] = now
    await r.hset(key, mapping=mapping)
    # A fresh check clears the sweep's stale marker (see sweep_stale).
    await r.hdel(key, "stale_fired")
    await r.expire(key, _TTL_SECONDS)
    return changed, health_event


async def record_content(target_id: int, changed: bool) -> Optional[str]:
    """Record a content check (change already determined against the DB baseline).
    Returns MONITOR_RECOVERED when this successful check clears a prior down/stale
    state (a reachable page → healthy again), else None."""
    r = get_redis_bytes()
    key = _key(target_id)
    prev = await r.hgetall(key)
    prev_state = _s(prev.get(b"state"))
    now = _now_ms()
    health_event = _health_transition(prev_state, "ok")
    mapping: Dict[str, Any] = {"checked_at": now, "state": "ok"}
    if changed or health_event:
        mapping["last_change_at"] = now
    await r.hset(key, mapping=mapping)
    await r.hdel(key, "stale_fired")
    await r.expire(key, _TTL_SECONDS)
    return health_event


async def sweep_stale(targets: List[Tuple[int, Optional[int]]]) -> List[int]:
    """Given (target_id, check_period_ms) pairs, return the target_ids that have
    JUST crossed into 'stale' — no fresh check within the expected cadence × 2.5
    (mirrors the read-time staleness in routers/targets.py) and not already fired.

    Marks each newly-stale target so the edge fires exactly once; a later fresh
    check (record_uptime / record_content) clears the marker and yields a
    monitor_recovered. Targets already in an unhealthy state are skipped so a
    down monitor doesn't also fire stale."""
    if not targets:
        return []
    r = get_redis_bytes()
    now = _now_ms()
    fired: List[int] = []
    for tid, period in targets:
        key = _key(tid)
        h = await r.hgetall(key)
        if not h:
            continue
        checked = _s(h.get(b"checked_at"))
        if not checked:
            continue
        state = _s(h.get(b"state"))
        already = _s(h.get(b"stale_fired")) == "1"
        if already or state in _UNHEALTHY_STATES:
            continue  # already unhealthy → don't (re-)fire stale
        # Match targets.py: no agent checks faster than the 60s anti-detection
        # floor, so compare against the EXPECTED cadence, not the raw interval.
        expected = max(int(period or 60000), 60000)
        if (now - int(checked)) > expected * 2.5:
            await r.hset(key, mapping={"state": "stale", "stale_fired": "1", "last_change_at": now})
            await r.expire(key, _TTL_SECONDS)
            fired.append(tid)
    return fired


async def get_states(target_ids: List[int]) -> Dict[int, dict]:
    """Fetch live state for a set of targets (one pipeline). Missing → absent."""
    if not target_ids:
        return {}
    r = get_redis_bytes()
    pipe = r.pipeline()
    for tid in target_ids:
        pipe.hgetall(_key(tid))
    rows = await pipe.execute()
    out: Dict[int, dict] = {}
    for tid, h in zip(target_ids, rows):
        if not h:
            continue
        d = {}
        for k, v in h.items():
            k = k.decode() if isinstance(k, bytes) else k
            v = v.decode() if isinstance(v, bytes) else v
            d[k] = v
        out[tid] = {
            "checked_at": int(d["checked_at"]) if d.get("checked_at") else None,
            "state": d.get("state"),
            "status_code": int(d["status_code"]) if d.get("status_code") else None,
            "last_change_at": int(d["last_change_at"]) if d.get("last_change_at") else None,
        }
    return out
