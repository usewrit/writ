"""Naming a machine in the connect modal.

The Fleet page's "Connect agent" modal has always had a Name field, and it has
always been decorative: the name went into the fleet-token registry and nothing
ever read it back. The agent cannot supply it either — no connect frame, query
param or heartbeat carries a name — so every machine listed under its raw
`writ-xxxxxxxx` id no matter what was typed.

These pin the resolution that makes the field real, and the one distinction that
keeps it honest: only a name the OPERATOR typed labels an agent. Every mint
stores a `name` (a generated `agent-<timestamp>` for pairing codes, `fleet-agent`
by default), and promoting those would swap a stable id for a placeholder nobody
chose.
"""
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from routers.fleet import _agent_display_name, _operator_names  # noqa: E402


def _reg(*tokens):
    return {"tokens": list(tokens)}


def _tok(**over):
    base = {
        "token_id": "t1",
        "name": "laptop-1",
        "display_name": "laptop-1",
        "agent_id": "writ-aaaa1111",
        "token_prefix": "abc123def456",
        "created_at": "2026-07-29T00:00:00Z",
        "revoked_at": None,
    }
    base.update(over)
    return base


# --- what counts as a name ---------------------------------------------------

def test_operator_name_is_indexed_by_both_ids():
    by_agent, by_prefix = _operator_names(_reg(_tok()))
    assert by_agent == {"writ-aaaa1111": "laptop-1"}
    assert by_prefix == {"abc123def456": "laptop-1"}


@pytest.mark.parametrize("display_name", [None, "", "   "])
def test_generated_names_do_not_label_anything(display_name):
    """A pairing code with no operator name mints `agent-<timestamp>`.

    Showing that instead of the agent id would be a downgrade: it looks like a
    real name, but it identifies when the token was minted and nothing else.
    """
    by_agent, by_prefix = _operator_names(
        _reg(_tok(name="agent-20260729-101500", display_name=display_name))
    )
    assert by_agent == {} and by_prefix == {}


def test_revoked_tokens_do_not_label_anything():
    """Its machine was torn out of the fleet; the label went with it."""
    by_agent, by_prefix = _operator_names(_tok() and _reg(_tok(revoked_at="2026-07-29T01:00:00Z")))
    assert by_agent == {} and by_prefix == {}


@pytest.mark.parametrize("registry", [{}, None, {"tokens": None}, {"tokens": []}])
def test_missing_registry_is_not_an_error(registry):
    """A fresh install has no registry row at all."""
    assert _operator_names(registry) == ({}, {})


# --- resolution order --------------------------------------------------------

def test_falls_back_to_the_agent_id_when_unnamed():
    assert _agent_display_name("writ-aaaa1111", {}, {}, {}) == "writ-aaaa1111"


def test_operator_name_beats_the_raw_id():
    by_agent, by_prefix = _operator_names(_reg(_tok()))
    assert _agent_display_name("writ-aaaa1111", {}, by_agent, by_prefix) == "laptop-1"


def test_token_prefix_resolves_a_reconnect_onto_another_row():
    """The token pins an agent_id, but the row it lands on may have another.

    `_register_agent` reuses an existing row when the agent claims its own
    stored id — so the durable row's agent_id can differ from the one the mint
    bound. Its meta carries the token prefix (stamped by
    `_stamp_agent_identity`), which identifies the mint exactly.
    """
    by_agent, by_prefix = _operator_names(_reg(_tok()))
    meta = {"oauth_token_prefix": "abc123def456"}
    assert _agent_display_name("writ-bbbb2222", meta, by_agent, by_prefix) == "laptop-1"


def test_a_name_the_agent_reports_wins():
    """If an agent ever does advertise a name, it knows its own hostname best."""
    by_agent, by_prefix = _operator_names(_reg(_tok()))
    assert _agent_display_name(
        "writ-aaaa1111", {"name": "reported-name"}, by_agent, by_prefix
    ) == "reported-name"


# --- the mint side -----------------------------------------------------------

def test_only_operator_named_mints_record_a_display_name():
    """`_mint_fleet_token` gates the label behind an explicit flag.

    Without it, the pairing-code path (which always generates a name so the
    token list has something to show) would relabel every machine.
    """
    import inspect

    from routers import fleet

    sig = inspect.signature(fleet._mint_fleet_token)
    assert sig.parameters["operator_named"].default is False
    src = inspect.getsource(fleet._mint_fleet_token)
    assert '"display_name": (raw_name or "").strip() if operator_named else None' in src


def test_start_local_agent_threads_the_request_into_the_mint():
    """It used to call the ROUTE FUNCTION and omit its `request` parameter.

    Outside FastAPI nothing fills a route's parameters in, so
    `mint_fleet_token(body, db=…, _admin=…)` raised
    `TypeError: missing 1 required positional argument: 'request'` and every
    "run one on this machine" attempt 500'd before it touched the host.
    """
    import inspect

    from routers import fleet

    assert "request" in inspect.signature(fleet.start_local_agent).parameters
    src = inspect.getsource(fleet.start_local_agent)
    assert "_mint_fleet_token(" in src, "call the plain function, not the route"
    assert "mint_fleet_token(\n        MintTokenRequest" not in src
