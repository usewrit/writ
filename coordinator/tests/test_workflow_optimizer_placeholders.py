"""The optimizer must propose credentials on the channel that actually resolves them,
and it must SIGN IN during the replay rather than restore a session."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The replay path reads `config.settings` (coordinator URL for the persona's OTP
# callback), and the coordinator refuses to build Settings without a real secret.
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("ALLOW_INSECURE_DEV", "true")

from services.workflow_optimizer_live import (  # noqa: E402
    _normalize_credential_placeholders,
    _secret_keys_in_steps,
    assemble_optimized,
)


def _login_steps():
    return [
        {"type": "fill", "enabled": True, "config": {"selector": "#u", "value": "{{secret:username}}"}},
        {"type": "fill", "enabled": True, "config": {"selector": "#p", "value": "{{secret:password}}"}},
        {"type": "click", "enabled": True, "config": {"selector": "#go"}},
    ]


def test_secret_keys_are_read_off_the_steps():
    assert _secret_keys_in_steps(_login_steps()) == ["password", "username"]
    assert _secret_keys_in_steps([]) == []


def test_bare_credential_placeholders_move_to_the_secret_channel():
    node = {"body_template": "user={{username}}&pass={{password}}&page={{page}}"}
    out = _normalize_credential_placeholders(node, ["username", "password"])
    # Credentials move; genuine run input keeps its form_data meaning.
    assert out["body_template"] == "user={{secret:username}}&pass={{secret:password}}&page={{page}}"
    assert _normalize_credential_placeholders(node, []) is node


def test_assemble_normalizes_a_proposed_login_post():
    proposal = {"substitutions": [{
        "replace_indices": [0, 1, 2],
        "with": {"type": "login_post", "config": {
            "url": "https://x.com/api/login", "method": "POST",
            "body_template": "user={{username}}&pass={{password}}", "variable": "login"}},
        "description": "Sign in over HTTP", "reason": "captured", "risk": "caution",
    }]}
    calls = [{"method": "POST", "url": "https://x.com/api/login", "response_status": 200}]
    steps, changes, _w, _r, verified = assemble_optimized(_login_steps(), proposal, calls)
    assert verified and changes
    body = steps[0]["config"]["body_template"]
    assert body == "user={{secret:username}}&pass={{secret:password}}"


def test_steps_perform_login_matches_only_the_secret_channel():
    """A bare `{{password}}` reads form data, so it names run input, not a credential."""
    from services.workflow_optimizer_live import _steps_perform_login

    creds = {"username": "u", "password": "p"}
    assert _steps_perform_login(_login_steps(), creds) is True
    bare = [{"type": "fill", "config": {"selector": "#p", "value": "{{password}}"}}]
    assert _steps_perform_login(bare, creds) is False
    assert _steps_perform_login(_login_steps(), {}) is False
    assert _steps_perform_login(None, creds) is False


def test_replay_credential_keys_win_over_the_steps():
    """The replay reports what it actually supplied; the draft heuristic is the fallback."""
    proposal = {"substitutions": [{
        "replace_indices": [0, 1, 2],
        "with": {"type": "login_post", "config": {
            "url": "https://x.com/api/login", "method": "POST",
            "body_template": "user={{email}}", "variable": "login"}},
        "description": "Sign in over HTTP", "reason": "captured", "risk": "caution",
    }]}
    calls = [{"method": "POST", "url": "https://x.com/api/login", "response_status": 200}]
    # `email` is not referenced by the steps, so only the replay's key list can move it.
    steps, _c, _w, _r, _v = assemble_optimized(_login_steps(), proposal, calls, ["email"])
    assert steps[0]["config"]["body_template"] == "user={{secret:email}}"


class _FakePersonaService:
    """Stands in for PersonaService; records whether the stored session was read."""

    loaded = False

    @staticmethod
    def resolve_login_credentials(persona):
        return {"username": "u", "password": "p"}

    @staticmethod
    def load_session(persona):
        _FakePersonaService.loaded = True
        return {"cookies": [{"name": "sid", "value": "warm"}]}

    @staticmethod
    def make_otp_token(persona_id, task_id=None):
        return "otp"


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDb:
    """Answers the persona SELECT, then the login-workflow SELECT, in that order."""

    def __init__(self, rows):
        self._rows = list(rows)

    async def execute(self, _stmt):
        return _FakeResult(self._rows.pop(0) if self._rows else None)


async def _run_replay(monkeypatch, *, login_workflow, workflow_steps):
    import services.persona_service as persona_service_mod
    import routers.ai_sessions as ai_sessions_mod
    import routers.automation as automation_mod
    import routers.user_recorder_ws as ws_mod
    from services import workflow_optimizer_live as wol

    _FakePersonaService.loaded = False
    monkeypatch.setattr(persona_service_mod, "PersonaService", _FakePersonaService)
    monkeypatch.setattr(ai_sessions_mod, "_pick_agent", lambda _a: "agent-1")
    monkeypatch.setattr(automation_mod, "encrypt_credentials", lambda d: {"enc": dict(d)})
    monkeypatch.setattr(automation_mod, "decrypt_credentials", lambda b: dict((b or {}).get("enc") or {}))

    built = {}

    def _build(**kw):
        built.update(kw)
        built["steps"] = list(kw["workflow"].steps or [])
        built["creds"] = dict((kw["workflow"].credentials_encrypted or {}).get("enc") or {})
        return {"type": "execute_workflow", "config": {}}
    monkeypatch.setattr(automation_mod, "build_execute_workflow_msg", _build)

    async def _push(agent_id, frame):
        built["capture"] = (frame.get("config") or {}).get("capture_network")
        return {"success": True, "result_data": {"network_calls": [], "final_url": ""}}
    monkeypatch.setattr(ws_mod, "push_to_recorder", _push)

    persona = _Obj(id=7, is_active=True, twofa_method=None, otp_extract_config=None,
                   login_workflow_id=(login_workflow.id if login_workflow else None))
    db = _FakeDb([persona, login_workflow])
    workflow = _Obj(id=99, default_persona_id=7, steps=workflow_steps,
                    credentials_encrypted={"enc": {"other": "x"}}, form_data={})
    out = await wol._replay_and_capture(db, workflow)
    return built, workflow, out


def test_replay_signs_in_live_instead_of_restoring_the_session(monkeypatch):
    """A restored session means the sign-in form never renders, so its POST never lands
    in the trace and the optimizer has nothing to fold the login steps into."""
    import asyncio

    login_wf = _Obj(id=5, steps=[{"type": "navigate", "config": {"url": "https://x.com/login"}}])
    data_steps = [{"type": "extract", "enabled": True, "config": {"selector": ".r", "variable": "rows"}}]
    built, workflow, out = asyncio.run(_run_replay(
        monkeypatch, login_workflow=login_wf, workflow_steps=data_steps))

    assert built["session_state"] is None
    assert _FakePersonaService.loaded is False
    assert built["steps"][0] == login_wf.steps[0]  # sign-in runs first
    assert built["steps"][1:] == data_steps
    assert built["capture"] is True
    assert built["creds"] == {"other": "x", "username": "u", "password": "p"}
    # The ORM row is left exactly as found — this function never commits.
    assert workflow.steps == data_steps
    assert workflow.credentials_encrypted == {"enc": {"other": "x"}}
    # Credential NAMES travel back, never values.
    assert out[2] == ["other", "password", "username"]


def test_replay_falls_back_to_the_stored_session_without_a_login_recipe(monkeypatch):
    """With no way to sign in live, a cold replay would only trace the login wall."""
    import asyncio

    steps = [{"type": "extract", "enabled": True, "config": {"selector": ".r", "variable": "rows"}}]
    built, _wf, _out = asyncio.run(_run_replay(
        monkeypatch, login_workflow=None, workflow_steps=steps))
    assert built["session_state"] == {"cookies": [{"name": "sid", "value": "warm"}]}
    assert built["steps"] == steps
