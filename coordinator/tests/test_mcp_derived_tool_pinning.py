"""Derived run_<name> MCP tools are OPT-IN (pinned), capped, and stale-name safe.

The coordinator's MCP server used to mint one run_<name> tool for EVERY saved
workflow. Clients inject every advertised tool schema into model context on
every request and several enforce hard tool caps, so at many workflows the
server either burned thousands of tokens per turn or got truncated arbitrarily.
Three behaviours replace that, and each breaks silently if it regresses, so
each is pinned here (mirrors the cloud suite):

  * exposure is opt-in — only workflows with ``mcp_tool_pinned`` derive a tool,
    and the derived list is capped (most recently touched win);
  * every workflow, pinned or not, stays callable through writ_run_workflow and
    through the stale-name fallback, so a client that cached the old tool list
    (every client, the day the opt-in flip lands) keeps working;
  * the fallback is CONSERVATIVE — exact slug only (plus a trailing ``_N``
    de-dup suffix), because anything fuzzier can run the WRONG workflow.
"""
import asyncio

import pytest

from routers import mcp_server


def _wf(i, name, pinned=False, updated_at="2026-08-01T00:00:00Z", **kw):
    return {"id": i, "name": name, "mcp_tool_pinned": pinned,
            "updated_at": updated_at, **kw}


@pytest.fixture(autouse=True)
def _fresh_result_cache():
    mcp_server._RESULT_CACHE.clear()
    yield
    mcp_server._RESULT_CACHE.clear()


# --------------------------------------------------------------------------- #
# Derivation: opt-in + cap                                                     #
# --------------------------------------------------------------------------- #
def test_unpinned_workflows_derive_no_tools():
    rows = [_wf(1, "hacker news front"), _wf(2, "korben all posts")]
    assert mcp_server._derived_run_tools(rows) == []


def test_pinned_workflow_derives_a_tool_with_declared_inputs():
    rows = [
        _wf(1, "Grafikart Profil", pinned=True,
            form_data={"email": "x"}, placeholders=["password"]),
        _wf(2, "unpinned neighbour"),
    ]
    tools = mcp_server._derived_run_tools(rows)
    assert [t["name"] for t in tools] == ["run_grafikart_profil"]
    props = tools[0]["inputSchema"]["properties"]
    assert "email" in props and "password" in props
    # The shared delivery/freshness controls stay advertised on derived tools.
    for control in mcp_server.RUN_CONTROL_PROPERTIES:
        assert control in props


def test_cap_keeps_most_recently_touched_and_is_deterministic(monkeypatch):
    monkeypatch.setattr(mcp_server, "MCP_DERIVED_TOOL_CAP", 2)
    rows = [
        _wf(1, "oldest", pinned=True, updated_at="2026-08-01T00:00:00Z"),
        _wf(2, "newest", pinned=True, updated_at="2026-08-19T00:00:00Z"),
        _wf(3, "middle", pinned=True, updated_at="2026-08-10T00:00:00Z"),
    ]
    names = {t["name"] for t in mcp_server._derived_run_tools(rows)}
    assert names == {"run_newest", "run_middle"}
    # Same input, same answer — the cap must not depend on row order.
    assert names == {t["name"] for t in mcp_server._derived_run_tools(list(reversed(rows)))}


def test_cap_zero_disables_derived_tools(monkeypatch):
    monkeypatch.setattr(mcp_server, "MCP_DERIVED_TOOL_CAP", 0)
    assert mcp_server._derived_run_tools([_wf(1, "x", pinned=True)]) == []


def test_duplicate_names_still_dedup_with_suffix():
    rows = [_wf(1, "google test", pinned=True), _wf(2, "google test", pinned=True)]
    names = [t["name"] for t in mcp_server._derived_run_tools(rows)]
    assert sorted(names) == ["run_google_test", "run_google_test_2"]


# --------------------------------------------------------------------------- #
# Stale-name fallback matching                                                 #
# --------------------------------------------------------------------------- #
def test_match_finds_unpinned_workflow_by_exact_slug():
    rows = [_wf(7, "Korben All Posts")]  # NOT pinned — the whole point
    assert [w["id"] for w in mcp_server._match_run_tool_name(rows, "run_korben_all_posts")] == [7]


def test_match_strips_trailing_dedup_suffix():
    rows = [_wf(3, "grafikart login")]
    assert [w["id"] for w in mcp_server._match_run_tool_name(rows, "run_grafikart_login_2")] == [3]


def test_exact_slug_beats_suffix_stripped():
    rows = [_wf(1, "test"), _wf(2, "test 2")]
    # "run_test_2" is EXACTLY workflow "test 2"'s slug; never the stripped "test".
    assert [w["id"] for w in mcp_server._match_run_tool_name(rows, "run_test_2")] == [2]


def test_ambiguous_slug_returns_all_candidates_and_miss_returns_none():
    rows = [_wf(1, "google test"), _wf(2, "Google Test")]
    assert len(mcp_server._match_run_tool_name(rows, "run_google_test")) == 2
    assert mcp_server._match_run_tool_name(rows, "run_never_saved") == []


# --------------------------------------------------------------------------- #
# Dispatch wiring: tools/list and tools/call                                   #
# --------------------------------------------------------------------------- #
def _dispatch(monkeypatch, rows, body, run_result=None, run_calls=None):
    async def fake_list(_token):
        return rows

    async def fake_run(_tok, wf, inputs, wait, timeout_s, files, persona_id):
        if run_calls is not None:
            run_calls.append({"workflow": wf, "inputs": inputs, "wait": wait})
        return run_result or {"status": "success"}

    monkeypatch.setattr(mcp_server, "_list_workflows", fake_list)
    monkeypatch.setattr(mcp_server, "_run_workflow_id", fake_run)
    return asyncio.run(mcp_server._dispatch(body, "Bearer wt_test"))


def test_tools_list_advertises_static_plus_only_pinned(monkeypatch):
    rows = [_wf(1, "pinned one", pinned=True), _wf(2, "not exposed")]
    res = _dispatch(monkeypatch, rows,
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in res["result"]["tools"]}
    assert "run_pinned_one" in names
    assert "run_not_exposed" not in names
    assert "writ_pin_workflow_tool" in names
    # tools/list must never leak the internal _workflow_id routing key.
    assert all(not k.startswith("_") for t in res["result"]["tools"] for k in t)


def test_stale_run_name_falls_back_to_the_unpinned_workflow(monkeypatch):
    calls = []
    rows = [_wf(9, "korben all posts")]  # unpinned: not advertised, must still run
    res = _dispatch(monkeypatch, rows,
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "run_korben_all_posts", "arguments": {}}},
                    run_calls=calls)
    assert "error" not in res
    assert not res["result"].get("isError")
    assert calls and calls[0]["workflow"]["id"] == 9


def test_unknown_run_name_teaches_the_generic_lane(monkeypatch):
    res = _dispatch(monkeypatch, [],
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": "run_never_saved", "arguments": {}}})
    # A tool-result error (recoverable guidance), NOT a bare JSON-RPC error.
    assert "error" not in res
    assert res["result"]["isError"] is True
    text = res["result"]["content"][0]["text"]
    assert "writ_run_workflow" in text and "writ_list_workflows" in text


def test_ambiguous_stale_name_refuses_to_guess(monkeypatch):
    calls = []
    rows = [_wf(1, "google test"), _wf(2, "Google Test")]
    res = _dispatch(monkeypatch, rows,
                    {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                     "params": {"name": "run_google_test", "arguments": {}}},
                    run_calls=calls)
    assert res["result"]["isError"] is True
    assert "workflow_id" in res["result"]["content"][0]["text"]
    assert calls == []  # never ran either candidate


def test_non_run_unknown_tool_stays_a_protocol_error(monkeypatch):
    res = _dispatch(monkeypatch, [],
                    {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                     "params": {"name": "writ_no_such_tool", "arguments": {}}})
    assert res["error"]["code"] == mcp_server.INVALID_PARAMS


# --------------------------------------------------------------------------- #
# The pin tool itself                                                          #
# --------------------------------------------------------------------------- #
def test_pin_tool_is_in_the_catalog():
    tool = next((t for t in mcp_server._STATIC_TOOLS
                 if t["name"] == "writ_pin_workflow_tool"), None)
    assert tool is not None and callable(tool["_handler"])
    assert set(tool["inputSchema"]["properties"]) == {"workflow", "workflow_id", "pinned"}


def test_pin_handler_puts_the_flag_and_reports_the_tool_name(monkeypatch):
    rows = [_wf(5, "grafikart login", pinned=True)]
    puts = []

    async def fake_list(_token):
        return rows

    async def fake_call(method, path, token, **kw):
        puts.append((method, path, kw.get("json_body")))
        return {}

    monkeypatch.setattr(mcp_server, "_list_workflows", fake_list)
    monkeypatch.setattr(mcp_server, "_call", fake_call)
    res = asyncio.run(mcp_server._tool_pin_workflow_tool(
        "Bearer wt_test", {"workflow_id": 5}))
    assert puts == [("PUT", "/api/automation/workflows/5", {"mcp_tool_pinned": True})]
    assert '"tool": "run_grafikart_login"' in res["content"][0]["text"]


def test_unpin_handler_sends_false(monkeypatch):
    rows = [_wf(5, "grafikart login", pinned=True)]
    puts = []

    async def fake_list(_token):
        return rows

    async def fake_call(method, path, token, **kw):
        puts.append((method, path, kw.get("json_body")))
        return {}

    monkeypatch.setattr(mcp_server, "_list_workflows", fake_list)
    monkeypatch.setattr(mcp_server, "_call", fake_call)
    asyncio.run(mcp_server._tool_pin_workflow_tool(
        "Bearer wt_test", {"workflow_id": 5, "pinned": False}))
    assert puts == [("PUT", "/api/automation/workflows/5", {"mcp_tool_pinned": False})]


def test_list_workflows_marks_pinned_rows(monkeypatch):
    rows = [_wf(1, "pinned one", pinned=True), _wf(2, "plain one")]

    async def fake_list(_token):
        return rows

    monkeypatch.setattr(mcp_server, "_list_workflows", fake_list)
    res = asyncio.run(mcp_server._tool_list_workflows("Bearer wt_test", {}))
    import json
    out = {w["name"]: w for w in json.loads(res["content"][0]["text"])["workflows"]}
    assert out["pinned one"].get("mcp_tool_pinned") is True
    assert "mcp_tool_pinned" not in out["plain one"]
