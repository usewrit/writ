"""Pure-function tests for the agent brain's step-edit / advanced-script protocol.

These cover the "AI can see & modify existing steps + advanced script" additions:
  - summarize_steps surfaces each step's id (so the model can target step_edits),
  - build_user_message injects the CURRENT ADVANCED SCRIPT section,
  - coerce_decision passes through step_edits (dropping malformed ops, sanitizing
    embedded JS) and clamps script_mode to append|replace.

No DB/gateway — the brain's message/coercion helpers are pure.
"""
import json

from services import agent_brain as ab


def test_summarize_steps_includes_ids():
    out = ab.summarize_steps([
        {"id": "abc123", "type": "click", "selector": "#go"},
        {"id": "d2", "type": "fill", "selector": "#q", "value": "hi"},
    ])
    assert "[id=abc123]" in out
    assert "[id=d2]" in out


def test_build_user_message_injects_current_script_and_ids():
    msgs = ab.build_user_message(
        instruction="edit it",
        conversation=[],
        page_url="https://x",
        observation=None,
        steps=[{"id": "s1", "type": "click"}],
        history=[],
        network_calls=[],
        iteration=0,
        max_iterations=12,
        advanced_script='ps.fn("x", async () => {})',
    )
    text = msgs[0]["content"][-1]["text"]
    assert "CURRENT ADVANCED SCRIPT:" in text
    assert "ps.fn" in text
    assert "[id=s1]" in text


def test_build_user_message_omits_script_section_when_empty():
    msgs = ab.build_user_message(
        instruction="do it", conversation=[], page_url="u", observation=None,
        steps=[], history=[], network_calls=[], iteration=0, max_iterations=12,
        advanced_script="",
    )
    assert "CURRENT ADVANCED SCRIPT:" not in msgs[0]["content"][-1]["text"]


def test_coerce_passes_step_edits_and_replace_mode():
    parsed = {
        "action": "done",
        "summary": "edit + replace",
        "step_edits": [
            {"op": "update", "id": "s1", "step": {"config": {"script": "a='b\nc'"}}},
            {"op": "delete", "index": 3},
            {"op": "move", "id": "s2", "to": 0},
            {"op": "bogus", "id": "z"},   # dropped: unknown op
            {"op": "delete"},              # dropped: no target
            {"op": "update", "id": "s3"},  # dropped: no step
            {"op": "move", "id": "s4"},    # dropped: no `to`
        ],
        "script": "ps.fn()",
        "script_mode": "replace",
    }
    d = ab.coerce_decision(parsed)
    assert d["action"] == "done"
    assert d["script_mode"] == "replace"
    edits = d["step_edits"]
    assert len(edits) == 3, edits
    assert edits[0] == {"op": "update", "id": "s1", "step": {"config": {"script": "a='b\\nc'"}}}
    assert edits[1] == {"op": "delete", "index": 3}
    assert edits[2] == {"op": "move", "id": "s2", "to": 0}
    # embedded JS in an update's config.script is sanitized (real newline re-escaped)
    assert "\\n" in json.dumps(edits[0]["step"]["config"]["script"])


def test_coerce_defaults_script_mode_and_infers_done_from_edits():
    d = ab.coerce_decision({"step_edits": [{"op": "delete", "id": "s1"}], "script_mode": "weird"})
    assert d["action"] == "done"          # inferred from presence of step_edits
    assert d["script_mode"] == "append"   # invalid mode falls back to append
    assert len(d["step_edits"]) == 1


def test_coerce_empty_edits_when_absent():
    d = ab.coerce_decision({"action": "ask", "message": "hi"})
    assert d["step_edits"] == []
    assert d["script_mode"] == "append"
