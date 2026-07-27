"""
/ws/record Origin gate unit tests (no DB, no network, no socket).

THE INVARIANT under test: the CSWSH gate in front of `/ws/record` must reject a
cross-site Origin while accepting the coordinator's OWN origin. Self-host serves
the SPA from the coordinator process itself (StaticFiles mount + SPA fallback in
main.py), so the recorder page is SAME-ORIGIN with the socket — and the operator
has no reason to have listed that URL in CORS_ORIGINS, since same-origin HTTP
never hits CORS at all. Before the same-origin allowance, a stock `docker
compose up` install rejected every local recording with
"disallowed Origin 'http://localhost:8000'".

Runnable with plain ``python3 coordinator/tests/test_record_ws_origin.py``.
"""
import os
import sys

COORDINATOR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if COORDINATOR not in sys.path:
    sys.path.insert(0, COORDINATOR)

# Settings refuses to construct with shipped-default secrets; give it throwaway
# ones so importing the router is possible without a configured install.
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("ALLOW_INSECURE_DEV", "true")

from config import settings  # noqa: E402
from routers.user_recorder_ws import _browser_ws_origin_allowed  # noqa: E402


class _FakeURL:
    def __init__(self, scheme: str):
        self.scheme = scheme


class _FakeWebSocket:
    """Only the two attributes the Origin gate touches: headers and url.scheme."""

    def __init__(self, headers: dict, scheme: str = "ws"):
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.url = _FakeURL(scheme)


def _ws(origin=None, host="localhost:8000", scheme="ws", **extra):
    headers = {"host": host, **extra}
    if origin is not None:
        headers["origin"] = origin
    return _FakeWebSocket(headers, scheme=scheme)


# --- accepted ---------------------------------------------------------------

def test_same_origin_is_allowed():
    # The stock single-container case: SPA and socket both on :8000.
    assert _browser_ws_origin_allowed(_ws("http://localhost:8000")) is True


def test_same_origin_via_loopback_ip_is_allowed():
    assert _browser_ws_origin_allowed(
        _ws("http://127.0.0.1:8000", host="127.0.0.1:8000")
    ) is True


def test_same_origin_behind_tls_terminator_is_allowed():
    # A proxy terminates TLS: the app sees ws://, the browser sent https://.
    assert _browser_ws_origin_allowed(
        _ws(
            "https://writ.example.com",
            host="writ.example.com",
            **{"x-forwarded-proto": "https"},
        )
    ) is True


def test_configured_cors_origin_is_allowed():
    # An origin that is NOT the coordinator's own, allowed purely by config.
    original = settings.cors_origins
    settings.cors_origins = "https://app.example.com"
    try:
        assert _browser_ws_origin_allowed(
            _ws("https://app.example.com", host="localhost:8000")
        ) is True
    finally:
        settings.cors_origins = original


def test_frontend_url_origin_is_allowed():
    # A split dev setup (Vite on another port) named via FRONTEND_URL is an app
    # origin even when the operator never mirrored it into CORS_ORIGINS.
    original = settings.frontend_url
    settings.frontend_url = "http://localhost:5173"
    try:
        assert _browser_ws_origin_allowed(
            _ws("http://localhost:5173", host="localhost:8000")
        ) is True
    finally:
        settings.frontend_url = original


# --- rejected (the CSWSH gate must stay closed) -----------------------------

def test_cross_site_origin_is_rejected():
    assert _browser_ws_origin_allowed(_ws("https://evil.example")) is False


def test_scheme_mismatch_is_rejected():
    # Same host, wrong scheme => a different origin.
    assert _browser_ws_origin_allowed(_ws("https://localhost:8000")) is False


def test_port_mismatch_is_rejected():
    assert _browser_ws_origin_allowed(_ws("http://localhost:9999")) is False


def test_missing_origin_is_rejected():
    # Fail closed: a real browser recorder always sends Origin.
    assert _browser_ws_origin_allowed(_ws(None)) is False


def test_wildcard_disables_the_check():
    original = settings.cors_origins
    settings.cors_origins = "*"
    try:
        assert _browser_ws_origin_allowed(_ws("https://evil.example")) is True
    finally:
        settings.cors_origins = original


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
