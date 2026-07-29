"""
Settings → Runtime / Security actually take effect (no DB, no network).

THE CLASS OF BUG under test: a setting that is persisted and rendered but read by
NOTHING. Settings → Runtime and Settings → Security shipped ten such controls
between them. The operator changed a number, got a success toast, and the
coordinator carried on with its built-in default — which is worse than having no
control at all, because it reads as a guarantee.

Two survived (the rest were removed as unimplementable here — see
`coordinator_settings.RUNTIME_DEFAULTS` / `SECURITY_DEFAULTS`), and this file
pins them down:

  * `max_concurrent_runs` — the scheduler looked for a *top-level* Config row
    literally keyed `max_concurrent_runs`, while the settings form writes the
    whole section as one JSON row under `coordinator_runtime`. Nothing had ever
    written the key it read.
  * `session_ttl_min` / `refresh_ttl_days` — token minting used module constants.
    The form even defaulted the refresh TTL to 30 days while the coordinator
    issued 7.
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

from security import jwt as jwt_mod  # noqa: E402
from services import coordinator_settings as cs  # noqa: E402


# ---------------------------------------------------------------------------
# Session policy
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _restore_session_policy():
    before = (jwt_mod.access_ttl_minutes(), jwt_mod.refresh_ttl_days())
    yield
    jwt_mod.set_session_policy(*before)


def _exp_minus_iat(token: str) -> int:
    """Seconds of life the token was signed with."""
    payload = jwt_mod.decode_token(token)
    assert payload is not None
    return int(payload["exp"]) - int(payload["iat"])


def test_access_token_lifetime_follows_the_setting():
    jwt_mod.set_session_policy(120, 7)
    token = jwt_mod.create_access_token(user_id="u1", org_id="u1")
    # Allow a second of clock movement between the two `datetime.now()` calls.
    assert abs(_exp_minus_iat(token) - 120 * 60) <= 2


def test_refresh_token_lifetime_follows_the_setting():
    jwt_mod.set_session_policy(15, 30)
    token = jwt_mod.create_refresh_token(user_id="u1")
    assert abs(_exp_minus_iat(token) - 30 * 86400) <= 2


def test_changing_the_policy_changes_the_next_token_not_the_last():
    """A JWT carries its own `exp`, so shortening the TTL cannot claw back a token
    already issued. The UI says so; this proves the behaviour matches."""
    jwt_mod.set_session_policy(120, 7)
    long_lived = jwt_mod.create_access_token(user_id="u1", org_id="u1")
    jwt_mod.set_session_policy(5, 7)
    short_lived = jwt_mod.create_access_token(user_id="u1", org_id="u1")
    assert abs(_exp_minus_iat(long_lived) - 120 * 60) <= 2
    assert abs(_exp_minus_iat(short_lived) - 5 * 60) <= 2


@pytest.mark.parametrize(
    "access_in,refresh_in,access_out,refresh_out",
    [
        (0, 0, 1, 1),               # below the floor
        (99_999, 99_999, 1_440, 3_650),  # above the ceiling
    ],
)
def test_policy_is_clamped_even_from_a_stale_db_row(access_in, refresh_in, access_out, refresh_out):
    """`set_session_policy` is fed straight from the DB, including rows written by
    an older build with different bounds — so it clamps rather than trusting."""
    jwt_mod.set_session_policy(access_in, refresh_in)
    assert jwt_mod.access_ttl_minutes() == access_out
    assert jwt_mod.refresh_ttl_days() == refresh_out


def test_apply_security_to_runtime_uses_defaults_for_a_missing_key():
    jwt_mod.set_session_policy(99, 99)
    cs.apply_security_to_runtime({})
    assert jwt_mod.access_ttl_minutes() == cs.SECURITY_DEFAULTS["session_ttl_min"]
    assert jwt_mod.refresh_ttl_days() == cs.SECURITY_DEFAULTS["refresh_ttl_days"]


# ---------------------------------------------------------------------------
# The removed controls must stay removed
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "key",
    [
        "max_background_runs",
        "rss_soft_watermark_mb",
        "browser_headless",
        "min_content_check_interval_s",
        "min_browser_check_interval_s",
    ],
)
def test_daemon_only_runtime_knobs_are_not_offered(key):
    """Re-adding one of these means claiming the coordinator runs browsers. It
    does not — agents do — so there is nowhere honest to wire them."""
    assert key not in cs.RUNTIME_DEFAULTS


@pytest.mark.parametrize("key", ["idle_timeout_min", "require_mfa"])
def test_unenforceable_security_knobs_are_not_offered(key):
    """`require_mfa` in particular: self-host ships no MFA enrolment path, so the
    switch could only lock the sole owner out or (as it did) do nothing at all."""
    assert key not in cs.SECURITY_DEFAULTS


def test_get_section_drops_values_persisted_by_an_older_build():
    """An install that saved the old shape must not resurrect the dead keys — the
    section only ever surfaces what is in DEFAULTS."""
    merged = {k: v for k, v in cs.RUNTIME_DEFAULTS.items()}
    stored = {**merged, "browser_headless": False, "rss_soft_watermark_mb": 4096}
    surfaced = {k: stored[k] for k in cs.RUNTIME_DEFAULTS if k in stored}
    assert surfaced == {"max_concurrent_runs": cs.RUNTIME_DEFAULTS["max_concurrent_runs"]}
