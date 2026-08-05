"""A blocked crawl URL must retry from a DIFFERENT agent/IP.

When a host refuses an agent, the orchestrator requeues those URLs and tags each
with `avoid_agent`, unioned onto the retry shard's trigger_context as
`_avoid_agents`. The whole point is that the retry leaves the refused IP — and for
a long time nothing read that tag: `_pick_recorder` had no notion of it, so the
retry could land straight back on the agent the host had just walled off, burning
the per-URL retry budget three times over.

The exclusion is deliberately a PREFERENCE, not a filter. A single-agent self-host
has nowhere else to send the work, and stranding a shard until its 2h queue expiry
is worse than one more attempt behind the host cooldown.
"""
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from routers.automation import _order_run_candidates  # noqa: E402


def _agent(agent_id, available=5, speed="balanced", perf=50):
    return {"agent_id": agent_id, "available": available, "max_sessions": 5,
            "active_sessions": 5 - available, "speed_class": speed, "perf_score": perf}


def _pick(cands, **kw):
    got = _order_run_candidates(cands, traffic_type="scheduled", fast_eligible=False, **kw)
    return got and got["agent_id"]


def test_a_refused_agent_is_skipped_when_another_is_free():
    pool = [_agent("blocked-me"), _agent("fresh-ip")]
    assert _pick(pool, exclude_agents={"blocked-me"}) == "fresh-ip"


def test_every_other_agent_is_eligible_not_just_the_next_one():
    pool = [_agent("blocked-a"), _agent("blocked-b"), _agent("fresh-ip")]
    assert _pick(pool, exclude_agents={"blocked-a", "blocked-b"}) == "fresh-ip"


def test_the_refused_agent_is_used_when_it_is_all_there_is():
    """A one-agent fleet must still make progress — the per-URL retry budget and
    the host cooldown bound the futile attempts, an indefinite stall does not."""
    assert _pick([_agent("only-one")], exclude_agents={"only-one"}) == "only-one"


def test_exclusion_never_invents_capacity():
    """A busy agent stays unusable whether or not it is excluded."""
    assert _pick([_agent("busy", available=0)], exclude_agents={"other"}) is None
    assert _pick([_agent("busy", available=0)]) is None


def test_no_exclusion_set_changes_nothing():
    pool = [_agent("a"), _agent("b")]
    assert _pick(pool, exclude_agents=None) == _pick(pool)
    assert _pick(pool, exclude_agents=set()) == _pick(pool)


def test_affinity_does_not_override_a_refusal():
    """Warm-session affinity normally wins outright. It must not drag the retry
    back onto the very agent the host refused while another IP is free."""
    pool = [_agent("blocked-me"), _agent("fresh-ip")]
    assert _pick(pool, preferred_agent_id="blocked-me",
                 exclude_agents={"blocked-me"}) == "fresh-ip"


# --- the wiring, end to end ---------------------------------------------------

def test_the_queue_processor_passes_the_tag_into_selection():
    """`_avoid_agents` is set by the orchestrator on every requeued shard; the
    dispatcher is the only thing that can act on it."""
    import inspect
    from services import workflow_queue

    src = inspect.getsource(workflow_queue)
    assert '_ctx0.get("_avoid_agents")' in src, "the queue must read the tag"
    assert "exclude_agents=_avoid_agents or None" in src, "…and pass it to selection"


def test_selection_threads_exclusion_through_every_tier():
    """WS user-hosted, WS infra and the HTTP pool are three separate candidate
    sources; an exclusion honoured by only some of them is a coin flip."""
    import inspect
    from routers import automation

    picker = inspect.getsource(automation._pick_recorder)
    assert picker.count("exclude_agents=exclude_agents") >= 4, (
        "every candidate source must receive the exclusion")
    http_pool = inspect.getsource(automation._pick_http_pool_recorder)
    assert "exclude_agents" in inspect.signature(automation._pick_http_pool_recorder).parameters
    assert "exclude_agents=exclude_agents" in http_pool

    from services.recorder_capacity_manager import RecorderCapacityManager
    assert "exclude_agents" in inspect.signature(RecorderCapacityManager.acquire_slot).parameters
