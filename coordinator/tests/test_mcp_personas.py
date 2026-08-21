"""
The self-host MCP persona surface — read + operate, never create.

Personas are the coordinator's saved sign-in identities. The MCP tool must let a
connected model handle them END TO END (discover, inspect, refresh the warm
session, teach one to self-login, run AS one) while two boundaries hold:

  1. No credential crosses the surface in either direction — the projection is a
     strict subset of the REST response (has_* booleans only), and there is no
     create/update/delete action at all.
  2. Every call goes through the scope-enforced /api/personas endpoints, so an
     API key needs the personas:* scopes exactly as it would over REST — the tool
     is deliberately NOT in _PRIVILEGED_HANDLERS.

Also covers the two parity gaps this wave closed: writ_scrape exists here now
(it always existed on cloud), and writ_crawl_site's schema finally DECLARES the
config keys it was already forwarding (persona_id above all — a knob a client
cannot discover is a knob that does not exist).
"""
import asyncio
import json

import pytest

from security.api_scopes import required_scope
from routers import mcp_server


def _run(coro):
    return asyncio.run(coro)


def _payload(result: dict) -> dict:
    assert result["content"][0]["type"] == "text"
    return json.loads(result["content"][0]["text"])


class _CallRecorder:
    """Stub for the loopback REST hop: records the request, returns a canned body."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __call__(self, method, path, token, *, params=None, json_body=None,
                       timeout=None):
        self.calls.append({"method": method, "path": path, "token": token,
                           "params": params, "json_body": json_body,
                           "timeout": timeout})
        return self.responses.pop(0)


_REST_ROW = {
    "id": 7, "name": "Grafikart", "description": "Forum account",
    "target_domain": "grafikart.fr", "login_username": "ada@example.com",
    "has_password": True, "twofa_method": "totp", "has_totp_seed": True,
    "email_otp_mode": None, "mail_connection_id": 3,
    "connected_mailbox": "ada@example.com", "relay_address": "otp@relay.example",
    "has_fingerprint": True, "preferred_agent_id": "agent-9",
    "has_proxy": True, "proxy_lawful_use_ack_at": "2026-08-01T00:00:00Z",
    "is_active": True, "validation_status": "valid", "has_warm_session": True,
    "session_expires_at": "2026-08-21T00:00:00Z", "last_login_at": "2026-08-19T00:00:00Z",
    "login_workflow_id": 12, "login_workflow_name": "Grafikart login",
    "last_login_error": None, "can_self_login": True,
    "last_used_at": "2026-08-19T12:00:00Z", "created_at": "2026-07-01T00:00:00Z",
    "updated_at": "2026-08-19T00:00:00Z",
    "linked_workflows": [{"id": 4, "name": "Forum watch"}], "linked_secrets": {},
}


# ── the projection ───────────────────────────────────────────────────────────

def test_projection_strips_mailbox_relay_and_fleet_detail():
    view = mcp_server._persona_view(_REST_ROW)
    for leaked in ("relay_address", "mail_connection_id", "connected_mailbox",
                   "preferred_agent_id", "has_fingerprint", "linked_secrets",
                   "proxy_lawful_use_ack_at", "created_at", "updated_at"):
        assert leaked not in view, leaked


def test_projection_never_carries_a_secret_even_if_rest_regressed():
    """Defense in depth: if the REST layer ever leaked a raw secret column, the
    fixed-field projection must still drop it (unknown keys do not pass)."""
    poisoned = dict(_REST_ROW, password="hunter2", totp_seed="JBSWY3DP",
                    session_state="{...}")
    dumped = json.dumps(mcp_server._persona_view(poisoned))
    assert "hunter2" not in dumped
    assert "JBSWY3DP" not in dumped


# ── the actions ──────────────────────────────────────────────────────────────

def test_list_teaches_the_selfhost_usage_lanes(monkeypatch):
    """The guidance must name only lanes that actually accept a persona HERE:
    crawl/scrape/run. The interactive browser lane does not restore persona
    sessions on self-host, so advertising it would be a lie."""
    monkeypatch.setattr(mcp_server, "_call", _CallRecorder([[_REST_ROW]]))
    body = _payload(_run(mcp_server._tool_personas("tok", {"action": "list"})))
    for tool in ("writ_crawl_site", "writ_scrape", "writ_run_workflow"):
        assert tool in body["next"]
    assert "writ_browser_use" not in body["next"]


def test_list_filters_by_domain(monkeypatch):
    rec = _CallRecorder([[_REST_ROW]])
    monkeypatch.setattr(mcp_server, "_call", rec)
    body = _payload(_run(mcp_server._tool_personas(
        "tok", {"action": "list", "domain": "grafikart.fr"})))
    assert rec.calls[0]["params"] == {"domain": "grafikart.fr"}
    assert body["total"] == 1


def test_sign_in_outlives_the_login_timeout(monkeypatch):
    rec = _CallRecorder([{"ok": True, "authenticated": True}])
    monkeypatch.setattr(mcp_server, "_call", rec)
    _run(mcp_server._tool_personas("tok", {"action": "sign_in", "persona_id": 7}))
    call = rec.calls[0]
    assert call["path"] == "/api/personas/7/sign-in"
    assert call["timeout"] is not None and call["timeout"] > 240


def test_record_login_returns_the_poll_contract(monkeypatch):
    rec = _CallRecorder([{"session_id": 55, "already_running": False}])
    monkeypatch.setattr(mcp_server, "_call", rec)
    body = _payload(_run(mcp_server._tool_personas(
        "tok", {"action": "record_login", "persona_id": 7})))
    assert rec.calls[0]["path"] == "/api/personas/7/record-login-ai"
    assert "can_self_login" in body["next"]


@pytest.mark.parametrize("action", ["get", "sign_in", "record_login"])
def test_id_actions_refuse_a_missing_or_garbage_persona_id(action, monkeypatch):
    monkeypatch.setattr(mcp_server, "_call", _CallRecorder([]))
    for bad in ({}, {"persona_id": "grafikart"}):
        res = _run(mcp_server._tool_personas("tok", dict(bad, action=action)))
        assert res.get("isError") is True


def test_no_lifecycle_mutation_is_reachable():
    schema = mcp_server._STATIC_BY_NAME["writ_personas"]["inputSchema"]
    actions = set(schema["properties"]["action"]["enum"])
    assert actions == {"list", "get", "sign_in", "record_login"}
    for forbidden in ("password", "totp_seed", "extra_login_fields", "proxy_password"):
        assert forbidden not in schema["properties"]


def test_personas_is_not_a_privileged_handler():
    """It must ride the loopback REST hop so API-key scopes are enforced at the
    endpoint — a privileged classification would bypass exactly that."""
    assert mcp_server._tool_personas not in mcp_server._PRIVILEGED_HANDLERS
    assert mcp_server._tool_scrape not in mcp_server._PRIVILEGED_HANDLERS


# ── run-as-persona threading ─────────────────────────────────────────────────

def test_run_workflow_carries_the_persona_to_the_run_body(monkeypatch):
    captured = {}

    async def fake_call(method, path, token, *, params=None, json_body=None, timeout=None):
        if path.endswith("/run"):
            captured.update(json_body or {})
            return {"task_id": 1}
        return []

    monkeypatch.setattr(mcp_server, "_call", fake_call)
    _run(mcp_server._run_workflow_id("tok", {"id": 4, "name": "Forum watch"},
                                     {"q": "x"}, False, 5, None, 7))
    assert captured["persona_id"] == 7
    assert "persona_id" not in captured["form_data"]


def test_persona_id_is_reserved_out_of_run_inputs():
    assert mcp_server._inputs_from_args({"persona_id": 7, "q": "x"}) == {"q": "x"}


def test_garbage_persona_id_fails_the_run_instead_of_running_anonymously():
    with pytest.raises(mcp_server._Upstream):
        mcp_server._run_persona_id({"persona_id": "grafikart"})
    assert mcp_server._run_persona_id({}) is None
    assert mcp_server._run_persona_id({"persona_id": "7"}) == 7


def test_freshness_key_separates_identities():
    anon = mcp_server._freshness_key(4, {"q": "x"}, None, None)
    as_7 = mcp_server._freshness_key(4, {"q": "x"}, None, 7)
    as_9 = mcp_server._freshness_key(4, {"q": "x"}, None, 9)
    assert len({anon, as_7, as_9}) == 3


def test_run_tools_advertise_the_persona_control():
    assert "persona_id" in mcp_server.RUN_CONTROL_PROPERTIES
    props = mcp_server._STATIC_BY_NAME["writ_run_workflow"]["inputSchema"]["properties"]
    assert "persona_id" in props


# ── scrape + crawl parity ────────────────────────────────────────────────────

def test_scrape_tool_exists_and_forwards_the_persona(monkeypatch):
    rec = _CallRecorder([{"url": "https://grafikart.fr/x", "markdown": "# hi"}])
    monkeypatch.setattr(mcp_server, "_call", rec)
    _run(mcp_server._tool_scrape("tok", {"url": "https://grafikart.fr/x",
                                         "persona_id": 7}))
    call = rec.calls[0]
    assert call["path"] == "/api/crawl/scrape"
    assert call["json_body"] == {"url": "https://grafikart.fr/x", "persona_id": 7}
    assert "writ_scrape" in mcp_server._STATIC_BY_NAME


def test_crawl_schema_declares_every_forwarded_config_key():
    """_CRAWL_CONFIG_KEYS members were forwarded to POST /api/crawl but half were
    absent from the inputSchema — undiscoverable, therefore unusable by any MCP
    client. The schema must declare them all (url rides separately)."""
    schema = mcp_server._STATIC_BY_NAME["writ_crawl_site"]["inputSchema"]["properties"]
    for key in mcp_server._CRAWL_CONFIG_KEYS:
        assert key in schema, f"writ_crawl_site forwards '{key}' but does not declare it"


# ── the advertised catalog + scopes ──────────────────────────────────────────

def test_writ_personas_is_advertised_and_well_formed():
    tool = mcp_server._STATIC_BY_NAME.get("writ_personas")
    assert tool is not None, "writ_personas missing from the self-host MCP catalog"
    assert callable(tool["_handler"])
    json.dumps(tool["inputSchema"])
    assert "action" in tool["inputSchema"]["required"]


@pytest.mark.parametrize("method,path,scope", [
    ("GET",  "/api/personas",                    "personas:read"),
    ("GET",  "/api/personas/7",                  "personas:read"),
    ("GET",  "/api/personas/7/runs",             "personas:read"),
    ("POST", "/api/personas/7/sign-in",          "personas:write"),
    ("POST", "/api/personas/7/record-login-ai",  "personas:write"),
])
def test_every_loopback_route_the_tool_uses_is_scope_mapped(method, path, scope):
    assert required_scope(method, path) == scope
