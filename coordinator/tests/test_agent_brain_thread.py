"""Multi-turn transcript assembly for the recorder's AI agent.

The loop used to flatten the task, the chat, every prior decision and every action
result into ONE `user` message rebuilt from scratch each iteration. These tests pin
the properties that fixed:

  * prior decisions come back as real `assistant` turns,
  * the volatile state (steps / observation / captured calls) is the LAST turn, so
    the prefix is append-only and cacheable,
  * compaction happens on whole-turn boundaries and never mid-JSON,
  * the thread strictly alternates roles.
"""
import importlib.util
import json
import os

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "agent_brain_under_test",
    os.path.join(os.path.dirname(__file__), "..", "services", "agent_brain.py"),
)
ab = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ab)


def _turn(i, *, payload=200, assistant=None):
    t = {
        "thought": f"thought {i}",
        "actions": [{"action": "evaluate_js", "script": "x" * payload}],
        "results": [{"action": "evaluate_js", "eval_result": "y" * payload}],
    }
    if assistant is not None:
        t["assistant"] = assistant
    return t


def _build(history, **kw):
    kw.setdefault("instruction", "scrape the products")
    kw.setdefault("conversation", [])
    kw.setdefault("page_url", "https://shop.test")
    kw.setdefault("observation", {"current_url": "https://shop.test"})
    kw.setdefault("steps", [])
    kw.setdefault("network_calls", [])
    kw.setdefault("iteration", len(history))
    kw.setdefault("max_iterations", 12)
    return ab.build_agent_messages(history=history, **kw)


def _text(message):
    c = message["content"]
    if isinstance(c, str):
        return c
    return "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")


def test_first_turn_is_task_then_state():
    msgs = _build([])
    assert len(msgs) == 1, "no history → opening and state collapse into one user turn"
    body = _text(msgs[0])
    assert body.startswith("TASK: scrape the products")
    assert "CURRENT STATE" in body


def test_prior_decisions_come_back_as_assistant_turns():
    msgs = _build([_turn(0), _turn(1)])
    roles = [m["role"] for m in msgs]
    assert roles.count("assistant") == 2, roles
    # The model's own decision, not a prose retelling of it.
    first_decision = json.loads(_text(msgs[1]))
    assert first_decision["action"] == "run_actions"
    assert first_decision["actions"][0]["action"] == "evaluate_js"


def test_verbatim_reply_is_replayed_when_the_loop_stored_it():
    raw = '{"thought":"probing","action":"run_actions","actions":[{"action":"read_text"}]}'
    msgs = _build([_turn(0, assistant=raw)])
    assert _text(msgs[1]) == raw


def test_roles_strictly_alternate():
    for n in (0, 1, 2, 5, 9):
        roles = [m["role"] for m in _build([_turn(i) for i in range(n)])]
        assert all(roles[i] != roles[i + 1] for i in range(len(roles) - 1)), (n, roles)
        assert roles[0] == "user" and roles[-1] == "user"


def test_volatile_state_is_the_final_turn_only():
    msgs = _build([_turn(0), _turn(1)], steps=[{"id": "s1", "type": "navigate"}])
    assert "CURRENT STATE" in _text(msgs[-1])
    for m in msgs[:-1]:
        assert "CURRENT STATE" not in _text(m), "state must not leak into the cacheable prefix"


def test_cache_breakpoint_sits_before_the_volatile_turn():
    msgs = _build([_turn(i) for i in range(3)])
    marked = [
        (i, b) for i, m in enumerate(msgs) if isinstance(m["content"], list)
        for b in m["content"] if isinstance(b, dict) and b.get("cache_control")
    ]
    assert len(marked) == 1, marked
    idx = marked[0][0]
    assert idx < len(msgs) - 1, "a breakpoint on the volatile turn would never hit"
    assert msgs[idx]["role"] == "user"


def test_compaction_is_whole_turn_and_bounded():
    # 40 fat turns: far past any budget.
    history = [_turn(i, payload=6000) for i in range(40)]
    msgs = _build(history, thread_char_budget=30_000)
    total = sum(len(_text(m)) for m in msgs)
    # The budget covers the replayed transcript; the final state turn sits outside it.
    assert total < 30_000 + 20_000, total
    # Every replayed assistant turn must still be complete JSON — the old character
    # tail-cut left the oldest turn as a fragment of a serialized blob.
    for m in msgs:
        if m["role"] == "assistant":
            json.loads(_text(m))


def test_dropped_turns_are_announced_not_silently_lost():
    # Condensing alone absorbs almost everything (30 fat turns condense to a few kB),
    # so dropping is genuinely a last resort — squeeze the budget below even that to
    # reach it. When it does happen the model must be TOLD, or it reads a partial
    # transcript as if it were the whole task.
    history = [_turn(i, payload=20000) for i in range(30)]
    msgs = _build(history, thread_char_budget=3_000)
    joined = "\n".join(_text(m) for m in msgs)
    assert "earlier turn(s) of this task were dropped" in joined


def test_newest_turns_keep_their_full_results():
    history = [_turn(i, payload=400) for i in range(10)]
    msgs = _build(history)
    joined = "\n".join(_text(m) for m in msgs)
    assert "RESULTS:" in joined                      # newest kept verbatim
    assert "RESULTS (condensed):" in joined          # oldest condensed
    # The newest turn's real payload survives.
    assert "y" * 400 in joined


def test_screenshot_rides_the_final_turn():
    msgs = _build([_turn(0)], screenshot_b64="AAAA")
    images = [
        b for m in msgs if isinstance(m["content"], list)
        for b in m["content"] if isinstance(b, dict) and b.get("type") == "image"
    ]
    assert len(images) == 1
    assert any(
        isinstance(b, dict) and b.get("type") == "image"
        for b in msgs[-1]["content"]
    ), "the screenshot is the CURRENT page — it belongs to the volatile turn"


def test_conversation_entries_are_individually_clamped():
    huge = [{"role": "user", "content": "z" * 50_000}]
    msgs = _build([], conversation=huge)
    assert len(_text(msgs[0])) < 20_000


@pytest.mark.parametrize("message,expected", [
    ("Anthropic returned 400: prompt is too long: 250000 tokens > 200000 maximum", True),
    ("openai returned 400: This model's maximum context length is 8192 tokens", True),
    ("local returned 400: context_length_exceeded", True),
    ("Anthropic returned 529: overloaded_error", False),
    ("connection reset by peer", False),
])
def test_context_overflow_is_distinguished_from_other_failures(message, expected):
    assert ab.is_context_overflow(Exception(message)) is expected


def test_summarize_scraper_history_drops_whole_turns():
    history = [_turn(i, payload=4000) for i in range(10)]
    out = ab.summarize_scraper_history(history, char_budget=6000)
    assert "omitted to fit the context budget" in out
    # Never starts mid-record: the first content line is a complete `- thought:` entry.
    body = [ln for ln in out.splitlines() if not ln.startswith("- (")]
    assert body[0].startswith("- thought:"), body[0][:120]
