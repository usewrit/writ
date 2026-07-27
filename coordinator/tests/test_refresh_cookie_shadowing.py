"""
Refresh-cookie scoping + duplicate-name tests (no DB, no network, no server).

THE INVARIANT under test: a second `refresh_token` cookie on the same origin
must not be able to wedge the session.

The browser sends ONE Cookie header that can carry the same NAME once per path
scope, ordered most-specific path first. `request.cookies` is a dict, so the
LAST duplicate wins — i.e. the BROADEST-path cookie, which is not necessarily
ours. The cloud backend's docker-compose publishes on host 8000:8000, the same
origin `run-local.sh` serves, and writes `refresh_token` at `/api/auth` under a
different signing key. That cookie outranked self-host's `/api/auth/refresh`
one, so every refresh 401'd, the SPA read it as "session expired" and bounced to
/login on each hard reload — and logging in again could not fix it, because
login only ever rewrote the narrow path.

Runnable with plain ``python3 coordinator/tests/test_refresh_cookie_shadowing.py``.
"""
import os
import sys

COORDINATOR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if COORDINATOR not in sys.path:
    sys.path.insert(0, COORDINATOR)

os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("ALLOW_INSECURE_DEV", "true")

from routers.auth import (  # noqa: E402
    LEGACY_REFRESH_COOKIE_PATH,
    REFRESH_COOKIE_PATH,
    _refresh_cookie_values,
)


class _FakeRequest:
    """Only what the parser touches: the raw Cookie header + the parsed dict."""

    def __init__(self, cookie_header: str):
        self.headers = {"cookie": cookie_header} if cookie_header else {}
        # Mirror Starlette's dict semantics: last duplicate wins.
        self.cookies = {}
        for part in cookie_header.split(";"):
            name, sep, value = part.strip().partition("=")
            if sep:
                self.cookies[name] = value


OURS = "ours.jwt.value"
FOREIGN = "foreign.jwt.value"


# --- the scoping that made logout a no-op ----------------------------------

def test_cookie_path_covers_both_refresh_and_logout():
    # At the old narrow scope the browser never sent the cookie to
    # /api/auth/logout, so logout could not blacklist the refresh jti.
    assert REFRESH_COOKIE_PATH == "/api/auth"
    for route in ("/api/auth/refresh", "/api/auth/logout"):
        assert route.startswith(REFRESH_COOKIE_PATH)
    assert LEGACY_REFRESH_COOKIE_PATH == "/api/auth/refresh"


# --- duplicate-name handling ------------------------------------------------

def test_dict_semantics_lose_the_real_cookie():
    # Documents the trap this module exists to work around: browsers order
    # most-specific first, so the dict's winner is the BROADEST-path cookie.
    req = _FakeRequest(f"refresh_token={OURS}; refresh_token={FOREIGN}")
    assert req.cookies["refresh_token"] == FOREIGN


def test_both_duplicates_are_returned_specific_first():
    req = _FakeRequest(f"refresh_token={OURS}; refresh_token={FOREIGN}")
    assert _refresh_cookie_values(req) == [OURS, FOREIGN]


def test_single_cookie_still_works():
    req = _FakeRequest(f"refresh_token={OURS}")
    assert _refresh_cookie_values(req) == [OURS]


def test_other_cookies_are_ignored():
    req = _FakeRequest(f"access_token=zzz; refresh_token={OURS}; theme=dark")
    assert _refresh_cookie_values(req) == [OURS]


def test_jwt_value_with_padding_is_not_truncated():
    # Values are split on the FIRST '=' only — a base64 '=' must survive.
    padded = "aGVhZGVy.cGF5bG9hZA==.c2ln"
    req = _FakeRequest(f"refresh_token={padded}")
    assert _refresh_cookie_values(req) == [padded]


def test_identical_duplicates_are_deduped():
    req = _FakeRequest(f"refresh_token={OURS}; refresh_token={OURS}")
    assert _refresh_cookie_values(req) == [OURS]


def test_no_cookie_header_yields_nothing():
    assert _refresh_cookie_values(_FakeRequest("")) == []


def test_falls_back_to_the_parsed_dict():
    # Defensive: some ASGI paths expose cookies without a raw header.
    req = _FakeRequest("")
    req.cookies = {"refresh_token": OURS}
    assert _refresh_cookie_values(req) == [OURS]


if __name__ == "__main__":  # pragma: no cover - script-style run
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError:
                failures += 1
                print(f"FAIL {name}")
    sys.exit(1 if failures else 0)
