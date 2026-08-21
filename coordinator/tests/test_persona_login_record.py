"""AI-recorded persona sign-in (self-host): the agent records, the coordinator owns it.

The self-host shape differs from cloud in ways worth locking down: the loop runs
on a fleet agent, the recorded recipe travels home on the terminal frame, and the
coordinator materializes its OWN workflow from it (an agent-side workflow id is a
foreign namespace and can never satisfy the FK).
"""
import pytest

from services.persona_login_record import (
    _refuse_reason_for_twofa,
    build_login_record_goal,
    normalize_secret_placeholders,
)


class _P:
    """Minimal persona stand-in — these helpers read a handful of columns."""
    def __init__(self, **kw):
        self.id = kw.get("id", 1)
        self.name = kw.get("name", "Acme prod")
        self.target_domain = kw.get("target_domain", "acme.test")
        self.twofa_method = kw.get("twofa_method", "none")


def test_goal_scopes_the_browse_to_signing_in_only():
    # Replayed on every session expiry — a wandering recording is a permanent tax.
    g = build_login_record_goal(_P(), "https://acme.test")
    assert "acme.test" in g
    assert "then stop" in g
    assert "Do not browse further" in g
    assert "never type a literal credential value" in g


def test_goal_falls_back_to_the_entry_url_without_a_domain():
    g = build_login_record_goal(_P(target_domain=None), "https://shop.example/login")
    assert "https://shop.example/login" in g


# --- The placeholder channel: the silent-logout defect ----------------------
def test_bare_credential_placeholders_are_moved_onto_the_secret_channel():
    # A bare {{password}} resolves against form_data at replay, misses, and types
    # the EMPTY STRING — a login that "runs" and lands logged out.
    steps = [{"type": "fill", "config": {"selector": "#p", "value": "{{password}}"}}]
    out = normalize_secret_placeholders(steps, ["username", "password"])
    assert out[0]["config"]["value"] == "{{secret:password}}"


def test_the_explorer_login_prefixed_alias_is_rewritten_too():
    steps = [{"type": "fill", "config": {"value": "{{login_password}}"}}]
    out = normalize_secret_placeholders(steps, ["password"])
    # Longest-alias-first: `login_password` must not be partially matched by `password`.
    assert out[0]["config"]["value"] == "{{secret:password}}"


def test_already_correct_references_are_left_alone():
    steps = [{"type": "fill", "config": {"value": "{{secret:password}}"}}]
    assert normalize_secret_placeholders(steps, ["password"]) == steps


def test_a_placeholder_naming_a_real_input_is_not_hijacked():
    # `search_term` is a run input, not a credential — rewriting it would break it.
    steps = [{"type": "fill", "config": {"value": "{{search_term}}"}}]
    out = normalize_secret_placeholders(steps, ["username", "password"])
    assert out[0]["config"]["value"] == "{{search_term}}"


def test_rewrite_reaches_nested_lists_and_dicts():
    steps = [{"type": "api_call", "config": {"body": {"auth": [{"pw": "{{password}}"}]}}}]
    out = normalize_secret_placeholders(steps, ["password"])
    assert out[0]["config"]["body"]["auth"][0]["pw"] == "{{secret:password}}"


def test_normalize_is_pure_and_no_ops_without_keys():
    steps = [{"type": "fill", "config": {"value": "{{password}}"}}]
    assert normalize_secret_placeholders(steps, []) == steps
    assert steps[0]["config"]["value"] == "{{password}}", "input must not be mutated"


# --- 2FA refusal: say so before spending a run ------------------------------
@pytest.mark.parametrize("method", ["none", "", None])
def test_a_persona_without_2fa_is_recordable(method):
    assert _refuse_reason_for_twofa(_P(twofa_method=method)) is None


def test_totp_is_refused_with_the_manual_route_offered():
    reason = _refuse_reason_for_twofa(_P(twofa_method="totp"))
    assert reason and "authenticator code" in reason
    assert "Record the sign-in yourself" in reason


@pytest.mark.parametrize("method", ["email_otp", "sms"])
def test_delivered_codes_are_refused_because_the_agent_cannot_read_them(method):
    reason = _refuse_reason_for_twofa(_P(twofa_method=method))
    assert reason and "can't read" in reason
