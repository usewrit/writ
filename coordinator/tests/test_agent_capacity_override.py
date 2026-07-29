"""
Agent concurrent-session capacity (no DB, no network).

THE COMPLAINT this fixes: an agent enrolled with the one-line installer showed
"2 slots" in Fleet and there was no way to change it. The number had three
possible sources and the UI showed none of them:

  * the pair-code path mints a fleet token with ``max_sessions=5``;
  * a brand-new DB row is created with ``5 if is_trusted else 2``;
  * the stock ``writ-agent`` then heartbeats ``max_sessions: 2``, and the
    coordinator honoured that unconditionally ("an agent may ask for FEWER").

So the operator saw 2, the token said 5, and nothing exposed the difference or
let them override it. Capacity is now three named inputs resolved by
``_effective_max_sessions``, all four numbers are returned by ``capacity_block``,
and ``set_operator_max_sessions`` pins the effective value.
"""
import os
import sys

import pytest

COORDINATOR_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if COORDINATOR_DIR not in sys.path:
    sys.path.insert(0, COORDINATOR_DIR)

os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("API_SECRET_KEY", "test-api-secret-0123456789abcdefABCDEF0123456789")
os.environ.setdefault("HMAC_SECRET_KEY", "test-hmac-secret-0123456789abcdefABCDEF0123456789")
os.environ.setdefault("RECORDER_AUTH_SECRET", "test-recorder-secret-0123456789abcdefABCDEF")

from routers import user_recorder_ws as ws  # noqa: E402


def _meta(**over):
    """A live-registry entry as the WS handler builds it at connect."""
    base = {
        "token_max_sessions": 5,
        "agent_reported_max_sessions": None,
        "operator_max_sessions": None,
        "max_sessions": 5,
        "active_sessions": 0,
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Resolution order
# ---------------------------------------------------------------------------
def test_token_ceiling_applies_before_any_heartbeat():
    assert ws._effective_max_sessions(_meta()) == 5


def test_agent_self_report_lowers_the_cap():
    """This is the '2' the operator was seeing."""
    assert ws._effective_max_sessions(_meta(agent_reported_max_sessions=2)) == 2


def test_agent_cannot_claim_more_than_its_token_grants():
    """An untrusted agent must not inflate its own dispatch share."""
    assert ws._effective_max_sessions(_meta(agent_reported_max_sessions=99)) == 5


def test_operator_override_beats_the_agent_self_report():
    meta = _meta(agent_reported_max_sessions=2, operator_max_sessions=8)
    assert ws._effective_max_sessions(meta) == 8


def test_operator_override_may_exceed_the_token_ceiling():
    """The ceiling bounds what the AGENT may claim, not what the operator may set.

    Clamping the operator to it would silently ignore a value the Fleet page had
    just confirmed as saved — on hardware the operator owns and we do not.
    """
    meta = _meta(token_max_sessions=2, agent_reported_max_sessions=2, operator_max_sessions=12)
    assert ws._effective_max_sessions(meta) == 12


def test_operator_override_is_still_bounded_by_the_hard_limit():
    meta = _meta(operator_max_sessions=10_000)
    assert ws._effective_max_sessions(meta) == ws.OPERATOR_MAX_SESSIONS_LIMIT


@pytest.mark.parametrize("junk", [None, 0, -3, "", "eight", {}])
def test_garbage_values_fall_through_instead_of_zeroing_capacity(junk):
    """A malformed value must never resolve to 0 — that would silently make the
    agent undispatchable rather than surfacing an error."""
    meta = _meta(operator_max_sessions=junk, agent_reported_max_sessions=junk)
    assert ws._effective_max_sessions(meta) == 5


# ---------------------------------------------------------------------------
# The shape the Fleet page renders
# ---------------------------------------------------------------------------
def test_capacity_block_exposes_every_input_not_just_the_result():
    meta = _meta(agent_reported_max_sessions=2, operator_max_sessions=8, active_sessions=3)
    block = ws.capacity_block(meta)
    assert block["max_sessions"] == 8
    assert block["agent_reported"] == 2
    assert block["token_ceiling"] == 5
    assert block["operator_override"] == 8
    assert block["active_sessions"] == 3
    assert block["free_slots"] == 5
    assert block["limit"] == ws.OPERATOR_MAX_SESSIONS_LIMIT


def test_free_slots_never_goes_negative():
    """Lowering the pin below the number of live sessions is legal; the running
    work finishes, but the UI must not render '-2 free'."""
    meta = _meta(operator_max_sessions=1, active_sessions=4)
    assert ws.capacity_block(meta)["free_slots"] == 0


# ---------------------------------------------------------------------------
# Live apply
# ---------------------------------------------------------------------------
def test_set_operator_max_sessions_applies_without_a_reconnect():
    agent_id = "writ-test-capacity"
    ws._agent_meta[agent_id] = _meta(agent_reported_max_sessions=2)
    try:
        block = ws.set_operator_max_sessions(agent_id, 8)
        assert block is not None
        assert block["max_sessions"] == 8
        # The scheduler reads meta['max_sessions'] directly, so the stored
        # effective value has to move too — not just the returned block.
        assert ws._agent_meta[agent_id]["max_sessions"] == 8
    finally:
        ws._agent_meta.pop(agent_id, None)


def test_clearing_the_override_returns_control_to_the_agent():
    agent_id = "writ-test-capacity-clear"
    ws._agent_meta[agent_id] = _meta(agent_reported_max_sessions=2, operator_max_sessions=8)
    try:
        block = ws.set_operator_max_sessions(agent_id, None)
        assert block["operator_override"] is None
        assert block["max_sessions"] == 2
    finally:
        ws._agent_meta.pop(agent_id, None)


def test_set_operator_max_sessions_on_an_offline_agent_reports_no_live_entry():
    """The router persists the override on the row and tells the operator it
    lands on next connect; it must not invent a live entry here."""
    assert ws.set_operator_max_sessions("writ-not-connected", 4) is None


def test_heartbeat_records_the_claim_without_overwriting_the_override():
    """A later heartbeat must not undo an operator's pin."""
    meta = _meta(operator_max_sessions=8)
    meta["agent_reported_max_sessions"] = 2      # as _handle_heartbeat would set it
    meta["max_sessions"] = ws._effective_max_sessions(meta)
    assert meta["max_sessions"] == 8
    assert meta["agent_reported_max_sessions"] == 2
