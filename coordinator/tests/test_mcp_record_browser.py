"""
Un-guided record / browser MCP surface.

These cover the parts that are easy to get wrong and expensive to get wrong: the
scope gate (a read-only key must not be able to seize a fleet browser), the tool
catalog the connector advertises, the credential holding that keeps a value the
client handed us out of everything we hand back and out of the saved workflow,
and the capture bookkeeping that keeps network indices meaning the same call.

Pure unit level — no fleet agent, no DB: every helper under test is deliberately
side-effect free so it can be exercised exactly like this.
"""
import pytest

from services import mcp_record


# ── the advertised tool catalog ──────────────────────────────────────────────

def test_record_and_browser_tools_are_advertised():
    from routers.mcp_server import _STATIC_BY_NAME

    for name in (
        "writ_browser_use", "writ_record_website", "writ_build", "writ_website_to_api",
        "writ_browser_act", "writ_browser_context", "writ_browser_network",
        "writ_browser_save", "writ_browser_cancel",
    ):
        assert name in _STATIC_BY_NAME, f"{name} is missing from the coordinator MCP catalog"


def test_legacy_record_names_still_exist():
    """The writ_record_* family predates the writ_browser_* naming; a client
    configured against the old names must keep working."""
    from routers.mcp_server import _STATIC_BY_NAME

    for name in ("writ_record_start", "writ_record_act", "writ_record_context",
                 "writ_record_network", "writ_record_save", "writ_record_cancel"):
        assert name in _STATIC_BY_NAME


def test_every_static_tool_has_a_unique_name_and_a_valid_schema():
    import json

    from routers.mcp_server import _STATIC_TOOLS, _STATIC_BY_NAME

    assert len(_STATIC_BY_NAME) == len(_STATIC_TOOLS), "duplicate tool name"
    for tool in _STATIC_TOOLS:
        assert callable(tool["_handler"])
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        json.dumps(schema)  # must survive tools/list serialization
        for key in schema.get("required", []):
            assert key in schema["properties"], f"{tool['name']}: required '{key}' undeclared"


def test_browser_aliases_share_the_upgraded_schemas():
    """The writ_browser_* aliases are built from their writ_record_* source, so a
    capability added to one must appear on both — otherwise a client picking the
    alias silently loses the argument."""
    from routers.mcp_server import _STATIC_BY_NAME

    for base, alias in (
        ("writ_record_act", "writ_browser_act"),
        ("writ_record_context", "writ_browser_context"),
        ("writ_record_network", "writ_browser_network"),
        ("writ_record_save", "writ_browser_save"),
        ("writ_record_cancel", "writ_browser_cancel"),
    ):
        assert _STATIC_BY_NAME[base]["inputSchema"] == _STATIC_BY_NAME[alias]["inputSchema"]


def test_new_capabilities_are_actually_advertised():
    from routers.mcp_server import _STATIC_BY_NAME

    assert "inputs" in _STATIC_BY_NAME["writ_browser_act"]["inputSchema"]["properties"]
    assert "data_key" in (_STATIC_BY_NAME["writ_browser_act"]["inputSchema"]
                          ["properties"]["actions"]["items"]["properties"])
    assert "section" in _STATIC_BY_NAME["writ_browser_context"]["inputSchema"]["properties"]
    assert "operation" in _STATIC_BY_NAME["writ_browser_network"]["inputSchema"]["properties"]
    # A save no longer demands a name — the session goal fills in.
    assert _STATIC_BY_NAME["writ_browser_save"]["inputSchema"]["required"] == ["session_id"]


# ── the scope gate ───────────────────────────────────────────────────────────

def test_every_live_browser_tool_is_scope_gated():
    """A read-scoped API key must not be able to open or drive a fleet browser.
    A start tool that is merely NAMED differently is still a front door."""
    from routers.mcp_server import _STATIC_TOOLS, _PRIVILEGED_HANDLERS

    live = [t for t in _STATIC_TOOLS if t["name"].startswith(
        ("writ_browser", "writ_record", "writ_build", "writ_website_to_api"))]
    assert live, "no browser tools found — the check would be vacuous"
    ungated = [t["name"] for t in live if t["_handler"] not in _PRIVILEGED_HANDLERS]
    assert not ungated, f"these seize a browser without an execute scope: {ungated}"


# ── build routing ────────────────────────────────────────────────────────────

def test_api_intent_is_classified_from_the_goal():
    """A connected model does not always pick the specialized start tool; a generic
    build whose goal asks for an API must still get the API framing."""
    from routers.mcp_server import _goal_requests_api

    assert _goal_requests_api({"goal": "turn this website into an api for orders"})
    assert _goal_requests_api({"goal": "I want a callable API for their stock page"})
    assert not _goal_requests_api({"goal": "log in and download last month's invoices"})


def test_own_library_matching_is_conservative():
    from routers.mcp_server import _match_own_workflows

    rows = [
        {"id": 1, "name": "Acme invoices", "description": "Download invoices",
         "entry_url": "https://acme.example.com/billing", "form_data": {}},
        {"id": 2, "name": "Weather", "description": "", "entry_url": "https://weather.example",
         "form_data": {}},
    ]
    # A host match qualifies outright.
    assert [h["workflow_id"] for h in _match_own_workflows(
        rows, "get the invoices", "acme.example.com")] == [1]
    # An unrelated goal on an unrelated host matches nothing — a weak match must
    # never hijack a build.
    assert _match_own_workflows(rows, "book a flight to Lisbon", "flights.example") == []


# ── credential holding ───────────────────────────────────────────────────────

def _session(mode=mcp_record.MODE_RECORD):
    return mcp_record.RecordSession("s1", "agent-1", "https://x", mode=mode)


def test_a_held_credential_becomes_a_secret_placeholder():
    sess = _session()
    sess.hold("login_password", "hunter2000")
    sess.hold("city", "Paris")
    assert sess.placeholders() == {
        "hunter2000": "{{secret:login_password}}",
        "Paris": "{{city}}",
    }
    # Both spellings resolve at RUN time…
    assert sess.fill_data["input.login_password"] == "hunter2000"
    # …but only one canonical replacement is used for OUTPUT.
    assert "{{input.login_password}}" not in sess.placeholders().values()


def test_scrub_removes_a_held_credential_from_nested_output():
    sess = _session()
    sess.hold("token", "s3cr3t-value")
    payload = {
        "request_headers": {"Authorization": "Bearer s3cr3t-value"},
        "steps": [{"config": {"value": "s3cr3t-value"}}],
    }
    scrubbed = mcp_record._scrub(payload, sess.placeholders())
    assert "s3cr3t-value" not in repr(scrubbed)
    assert scrubbed["request_headers"]["Authorization"] == "Bearer {{secret:token}}"
    assert scrubbed["steps"][0]["config"]["value"] == "{{secret:token}}"


def test_scrub_replaces_the_longest_match_first():
    """A short held value that is a SUBSTRING of a longer one must not shadow it
    (a username inside the email address it belongs to)."""
    sess = _session()
    sess.hold("user", "ada")
    sess.hold("email", "ada@example.com")
    assert mcp_record._scrub("ada@example.com", sess.placeholders()) == "{{email}}"


def test_a_credential_fill_with_no_value_is_refused():
    """The model must ASK the user for a credential, never invent one."""
    assert mcp_record._reject_action(
        {"action": "fill", "selector": "#pw", "data_key": "login_password"})
    assert mcp_record._reject_action(
        {"action": "fill", "selector": "#pw", "data_key": "login_password", "value": "real"}
    ) is None
    assert mcp_record._reject_action({"action": "click", "selector": "#go"}) is None


# ── network capture bookkeeping ──────────────────────────────────────────────

def _api_frame(i):
    return {"type": "api_captured",
            "call": {"method": "GET", "url": f"https://x/{i}", "response_status": 200}}


def test_capture_unwraps_the_agent_frame():
    """The agent sends the NetworkCall under `call` — storing the envelope instead
    would make every search miss on url/method."""
    sess = _session()
    mcp_record._ingest(sess, _api_frame(1))
    assert sess.network[0]["url"] == "https://x/1"
    assert "type" not in sess.network[0]


def test_capture_dedupes_and_bounds_with_a_stable_base():
    sess = _session()
    overflow = 40
    for i in range(mcp_record._NETWORK_MAX_CALLS + overflow):
        mcp_record._ingest(sess, _api_frame(i))
    first = list(sess.network)
    # The agent re-reports the same calls as the session continues.
    for i in range(mcp_record._NETWORK_MAX_CALLS + overflow):
        mcp_record._ingest(sess, _api_frame(i))

    assert sess.network == first, "re-reporting the same calls must change nothing"
    assert len(sess.network) == mcp_record._NETWORK_MAX_CALLS
    assert sess.network[-1]["url"].endswith(str(mcp_record._NETWORK_MAX_CALLS + overflow - 1))
    # The evicted head is accounted for, so reported indices stay absolute.
    assert sess.network_base == overflow


def test_capture_truncates_oversized_bodies():
    sess = _session()
    huge = "x" * (mcp_record._NETWORK_BODY_CHARS * 3)
    mcp_record._ingest(sess, {"type": "api_captured", "call": {
        "method": "POST", "url": "https://x/api", "response_body": huge}})
    stored = sess.network[0]["response_body"]
    assert len(stored) < len(huge)
    assert stored.endswith("[truncated]")


def test_capture_ignores_a_frame_with_no_url():
    sess = _session()
    mcp_record._ingest(sess, {"type": "api_captured", "call": {"method": "GET"}})
    assert sess.network == []


# ── session posture ──────────────────────────────────────────────────────────

def test_each_mode_carries_its_own_guidance():
    """The `next` line is what keeps a connected client in the right posture, so
    every mode must actually have one (a missing key would silently fall back)."""
    for mode in (mcp_record.MODE_USE, mcp_record.MODE_RECORD, mcp_record.MODE_API):
        assert mode in mcp_record._NEXT_BY_MODE
        assert _session(mode).view()["next"] == mcp_record._NEXT_BY_MODE[mode]
    assert _session(mcp_record.MODE_USE).view()["mode"] == "browser_use"


def test_policy_sections_resolve_and_reject_junk():
    assert len(mcp_record._policy_text("explorer")) > 1000
    assert len(mcp_record._policy_text("concierge_api")) > 1000
    with pytest.raises(mcp_record.RecordError):
        mcp_record._policy_text("nonsense")
