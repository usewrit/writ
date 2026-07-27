"""
Self-host call contract: run DELIVERY (`?wait=`) and MCP FRESHNESS (`max_age`).

The coordinator's run endpoint was fire-and-forget only, so every script had to
hand-roll a poll loop against `/api/automation/tasks/{id}/results`. And the MCP server
honoured `wait`/`timeout_seconds` without ever advertising them, so no agent could
discover them.

These are dependency-light (pure decision functions + schema shaping) so they run
without Postgres, a browser, or a live coordinator.
"""
import sys
import time
from pathlib import Path

import pytest

COORDINATOR_DIR = Path(__file__).resolve().parents[1]
if str(COORDINATOR_DIR) not in sys.path:
    sys.path.insert(0, str(COORDINATOR_DIR))


class _FakeRequest:
    """Just enough of a Starlette Request for the delivery parsers."""

    def __init__(self, query=None):
        self.query_params = query or {}


# ── Delivery: how long is the caller willing to wait? ───────────────────────────

def test_run_is_fire_and_forget_by_default():
    """REGRESSION GUARD: the dashboard depends on getting the task id straight back."""
    from routers.automation import _wants_wait
    assert _wants_wait(_FakeRequest()) is False


@pytest.mark.parametrize("value", ["true", "1", "yes", "TRUE", "Yes"])
def test_wait_opt_in_forms(value):
    from routers.automation import _wants_wait
    assert _wants_wait(_FakeRequest({"wait": value})) is True


@pytest.mark.parametrize("value", ["false", "0", "no", "", "maybe"])
def test_non_affirmative_wait_stays_async(value):
    from routers.automation import _wants_wait
    assert _wants_wait(_FakeRequest({"wait": value})) is False


def test_wait_budget_defaults_and_clamps():
    """Clamped so a wedged run cannot pin a worker connection indefinitely."""
    from routers.automation import (
        _wait_budget, _WAIT_DEFAULT_SECS, _WAIT_MAX_SECS,
    )
    assert _wait_budget(_FakeRequest()) == _WAIT_DEFAULT_SECS
    assert _wait_budget(_FakeRequest({"timeout": "60"})) == 60
    assert _wait_budget(_FakeRequest({"timeout": "0"})) == 1
    assert _wait_budget(_FakeRequest({"timeout": "999999"})) == _WAIT_MAX_SECS
    # Malformed degrades to the default rather than failing the run.
    assert _wait_budget(_FakeRequest({"timeout": "abc"})) == _WAIT_DEFAULT_SECS
    assert _wait_budget(_FakeRequest({"timeout": "-5"})) == _WAIT_DEFAULT_SECS


def test_terminal_vocabulary_matches_the_rest_of_the_platform():
    """One set of words describes a run wherever it executed."""
    from routers.automation import _TERMINAL_TASK_STATUSES
    assert set(_TERMINAL_TASK_STATUSES) == {"success", "failed", "timeout", "cancelled"}


# ── MCP freshness + discoverability ────────────────────────────────────────────

def test_run_controls_are_advertised():
    """They were honoured but undocumented, so no client could know they existed."""
    from routers.mcp_server import RUN_CONTROL_PROPERTIES, FRESHNESS_ARG
    assert set(RUN_CONTROL_PROPERTIES) == {"wait", "timeout_seconds", FRESHNESS_ARG}
    freshness = RUN_CONTROL_PROPERTIES[FRESHNESS_ARG]
    assert freshness["type"] == "integer"
    assert freshness["minimum"] == 0
    # The description must tell an agent WHEN to use it, or it never will.
    assert "younger than" in freshness["description"]


def test_derived_run_tools_expose_the_controls():
    from routers.mcp_server import _derived_run_tools, RUN_CONTROL_PROPERTIES

    tools = _derived_run_tools([
        {"id": 7, "name": "Price check", "form_data": {"sku": ""}},
    ])

    assert len(tools) == 1
    props = tools[0]["inputSchema"]["properties"]
    assert "sku" in props, "the workflow's own inputs survive"
    for ctl in RUN_CONTROL_PROPERTIES:
        assert ctl in props, f"{ctl} must be discoverable"


def test_a_workflow_input_named_like_a_control_wins():
    """Shadowing a caller-defined input would silently change what the workflow gets."""
    from routers.mcp_server import _derived_run_tools, FRESHNESS_ARG

    tools = _derived_run_tools([
        {"id": 7, "name": "wf", "form_data": {FRESHNESS_ARG: ""}},
    ])

    prop = tools[0]["inputSchema"]["properties"][FRESHNESS_ARG]
    assert prop["type"] == "string", "the workflow's own declaration was preserved"


def test_control_args_are_not_passed_to_the_workflow_as_inputs():
    from routers.mcp_server import _inputs_from_args, FRESHNESS_ARG

    inputs = _inputs_from_args({
        "workflow_id": 7, "wait": True, "timeout_seconds": 60,
        FRESHNESS_ARG: 300, "sku": "B0C123",
    })

    assert inputs == {"sku": "B0C123"}


@pytest.mark.parametrize("args,expected", [
    ({}, 0),
    ({"max_age": 60}, 60),
    ({"max_age": "60"}, 60),
    ({"max_age": -5}, 0),
    ({"max_age": "abc"}, 0),
    ({"max_age": None}, 0),
])
def test_requested_max_age(args, expected):
    from routers.mcp_server import _requested_max_age
    assert _requested_max_age(args) == expected


@pytest.fixture(autouse=True)
def _clear_cache():
    from routers.mcp_server import _RESULT_CACHE
    _RESULT_CACHE.clear()
    yield
    _RESULT_CACHE.clear()


def test_a_fresh_enough_run_is_reused_and_reports_its_age():
    from routers.mcp_server import _freshness_key, _store_run, _cached_run

    key = _freshness_key(7, {"sku": "B0C123"})
    _store_run(key, {"status": "success", "rows": [[1]]})

    hit = _cached_run(key, max_age=600)

    assert hit is not None
    assert hit["rows"] == [[1]]
    # The agent must be able to tell a reused answer from a fresh one.
    assert hit["_cache"]["hit"] is True
    assert hit["_cache"]["age_seconds"] >= 0


def test_a_run_older_than_requested_is_not_reused():
    from routers.mcp_server import _freshness_key, _cached_run, _RESULT_CACHE

    key = _freshness_key(7, {"sku": "B0C123"})
    _RESULT_CACHE[key] = (time.time() - 900, {"status": "success"})

    assert _cached_run(key, max_age=60) is None


def test_only_successful_runs_are_reusable():
    """Serving a stored failure back as an answer would be worse than re-running."""
    from routers.mcp_server import _freshness_key, _store_run, _cached_run

    key = _freshness_key(7, {})
    for bad in ({"status": "failed"}, {"status": "running"}, {"error": "boom"}):
        _store_run(key, bad)
        assert _cached_run(key, max_age=600) is None


def test_different_inputs_and_workflows_are_different_entries():
    from routers.mcp_server import _freshness_key

    base = _freshness_key(7, {"sku": "A"})
    assert base != _freshness_key(7, {"sku": "B"})
    assert base != _freshness_key(8, {"sku": "A"})
    # Argument order must not split the cache.
    assert _freshness_key(7, {"a": 1, "b": 2}) == _freshness_key(7, {"b": 2, "a": 1})


def test_cache_is_bounded():
    from routers.mcp_server import (
        _freshness_key, _store_run, _cached_run, _RESULT_CACHE, _RESULT_CACHE_MAX,
    )

    for i in range(_RESULT_CACHE_MAX + 25):
        _store_run(_freshness_key(7, {"i": i}), {"status": "success", "i": i})

    assert len(_RESULT_CACHE) <= _RESULT_CACHE_MAX
    assert _cached_run(_freshness_key(7, {"i": 0}), max_age=600) is None, "oldest evicted"
    newest = _freshness_key(7, {"i": _RESULT_CACHE_MAX + 24})
    assert _cached_run(newest, max_age=600) is not None, "newest kept"
