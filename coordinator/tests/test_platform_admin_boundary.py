"""
Platform-admin privilege-boundary tests (no DB server, no network).

THE INVARIANT under test: administrative endpoints require the operator's own
first-party browser session. Not an API key, not an OAuth token — even though on
a single-owner coordinator all three resolve to the same ``user_id``.

That last part is the whole point. API keys are minted with ``user_id`` set to the
owner, and OAuth grants carry the owner's ``user_id`` too, so a dependency that
only re-reads the User row would happily admit a deliberately read-only scoped key
to every admin route — including ``POST /api/fleet/tokens`` (mints a long-lived
fleet service token) and the vault-decrypting deploy path. The admin routers do
not use ``RequireScope``, so this dependency is the only gate that stands between
a narrowly-scoped token and full administration.

The dependency is exercised directly with fabricated ``AuthContext`` values and a
stub session, so no database is needed.
"""
import asyncio
import os
import sys
import uuid

import pytest

COORDINATOR_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if COORDINATOR_DIR not in sys.path:
    sys.path.insert(0, COORDINATOR_DIR)

os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("API_SECRET_KEY", "test-api-secret-0123456789abcdefABCDEF0123456789")
os.environ.setdefault("HMAC_SECRET_KEY", "test-hmac-secret-0123456789abcdefABCDEF0123456789")
os.environ.setdefault("RECORDER_AUTH_SECRET", "test-recorder-secret-0123456789abcdefABCDEF")

from fastapi import HTTPException  # noqa: E402

from security.dependencies import AuthContext, require_platform_admin  # noqa: E402

OWNER_ID = uuid.uuid4()


class _StubUser:
    """The single owner row, exactly as the real query would return it."""

    def __init__(self, is_platform_admin=True, is_active=True):
        self.id = OWNER_ID
        self.is_platform_admin = is_platform_admin
        self.is_active = is_active
        self.is_verified = True


class _StubResult:
    def __init__(self, user):
        self._user = user

    def scalar_one_or_none(self):
        return self._user


class _StubSession:
    """Minimal AsyncSession stand-in: always returns the owner row."""

    def __init__(self, user=None):
        self._user = user if user is not None else _StubUser()
        self.executed = False

    async def execute(self, _query):
        self.executed = True
        return _StubResult(self._user)


def _call(auth, db=None):
    return asyncio.run(require_platform_admin(auth=auth, db=db or _StubSession()))


# ---------------------------------------------------------------------------
# The regression this file exists for
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("auth_method", ["api_key", "oauth"])
def test_non_session_credentials_are_refused(auth_method):
    """An API key or OAuth token for the owner must NOT be platform admin."""
    auth = AuthContext(
        user_id=OWNER_ID,
        role="owner",
        is_platform_admin=True,   # even when the context claims it
        auth_method=auth_method,
    )
    with pytest.raises(HTTPException) as exc:
        _call(auth)
    assert exc.value.status_code == 403
    assert "first-party session" in str(exc.value.detail)


def test_read_only_scoped_api_key_cannot_administer():
    """The concrete escalation: a deliberately narrow key reaching admin routes."""
    auth = AuthContext(
        user_id=OWNER_ID,
        role="client",
        auth_method="api_key",
        api_key_scopes={"workflows": {"permissions": ["read"]}},
        api_key_id=1,
    )
    with pytest.raises(HTTPException) as exc:
        _call(auth)
    assert exc.value.status_code == 403


def test_refusal_happens_before_the_database_is_touched():
    """Fail closed on the credential type without needing the User row at all."""
    db = _StubSession()
    auth = AuthContext(user_id=OWNER_ID, auth_method="api_key")
    with pytest.raises(HTTPException):
        _call(auth, db)
    assert db.executed is False


# ---------------------------------------------------------------------------
# The legitimate path must keep working
# ---------------------------------------------------------------------------

def test_owner_session_is_admitted():
    """The documented flow (POST /api/auth/login -> JWT) still works."""
    auth = AuthContext(user_id=OWNER_ID, role="owner", auth_method="jwt")
    assert _call(auth) is auth


def test_session_is_still_rechecked_against_the_user_row():
    """A stale JWT claim must not survive rights being revoked in the database."""
    auth = AuthContext(
        user_id=OWNER_ID, role="owner", is_platform_admin=True, auth_method="jwt"
    )
    db = _StubSession(_StubUser(is_platform_admin=False))
    with pytest.raises(HTTPException) as exc:
        _call(auth, db)
    assert exc.value.status_code == 403
    assert db.executed is True


def test_anonymous_is_401_not_403():
    auth = AuthContext(user_id=None, auth_method="none")
    with pytest.raises(HTTPException) as exc:
        _call(auth)
    assert exc.value.status_code == 401
