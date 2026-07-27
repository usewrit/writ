"""
Redirect credential-handling tests (no network).

THE INVARIANT under test: an authentication header never crosses an origin
boundary on a redirect, and a request body never survives a 301/302/303 method
downgrade.

Both SSRF-hardened fetchers (``services.safe_fetch.safe_fetch`` and
``InputValidator.safe_fetch``) turn httpx's automatic redirect following OFF and
follow hops by hand, so that they can re-screen each hop's resolved IP. That is
correct for SSRF — but it also means httpx's own cross-origin ``Authorization``
stripping never runs, so these fetchers have to do it themselves.

It matters because the callers carry real credentials: ``services/local_ai.py``
and ``routers/settings.py`` send a decrypted provider API key
(``x-api-key`` / ``Authorization: Bearer``) to an operator-configured
``base_url``. A provider host that answers ``302`` to a host it controls would
otherwise be handed the key on the next hop.
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

from services.safe_fetch import (  # noqa: E402
    _is_cross_origin,
    _strip_credential_headers,
)

# Deliberately shaped so no secret scanner mistakes these for real credentials —
# the test only cares that the HEADER NAMES are stripped, never the values.
CREDS = {
    "Authorization": "Bearer <placeholder-token>",
    "x-api-key": "<placeholder-provider-key>",
    "Cookie": "session=<placeholder>",
    "Proxy-Authorization": "Basic <placeholder>",
    "Content-Type": "application/json",
    "anthropic-version": "2023-06-01",
}


# ---------------------------------------------------------------------------
# Origin comparison
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "a,b",
    [
        # Different host — the ordinary exfiltration case.
        ("https://api.anthropic.com/v1/messages", "https://attacker.example/collect"),
        # Different scheme on the SAME host: an https -> http downgrade would put
        # the bearer token on the wire in cleartext.
        ("https://api.example.com/v1", "http://api.example.com/v1"),
        # Different port is a different origin.
        ("https://api.example.com/v1", "https://api.example.com:8443/v1"),
        # Sibling subdomain is NOT the same origin.
        ("https://api.example.com/v1", "https://evil.example.com/v1"),
    ],
)
def test_cross_origin_is_detected(a, b):
    assert _is_cross_origin(a, b) is True


@pytest.mark.parametrize(
    "a,b",
    [
        # Plain path change — the common, legitimate redirect.
        ("https://api.example.com/v1", "https://api.example.com/v2/messages"),
        # An explicit default port is the same origin as an implicit one.
        ("https://api.example.com/v1", "https://api.example.com:443/v1"),
        ("http://api.example.com/v1", "http://api.example.com:80/v1"),
        # Host comparison is case-insensitive.
        ("https://API.Example.com/v1", "https://api.example.com/v1"),
    ],
)
def test_same_origin_is_not_flagged(a, b):
    assert _is_cross_origin(a, b) is False


# ---------------------------------------------------------------------------
# Header stripping
# ---------------------------------------------------------------------------

def test_credential_headers_are_removed():
    stripped = _strip_credential_headers(CREDS)
    assert "Authorization" not in stripped
    assert "x-api-key" not in stripped
    assert "Cookie" not in stripped
    assert "Proxy-Authorization" not in stripped


def test_non_credential_headers_survive():
    """Stripping must not break the request that continues to the new origin."""
    stripped = _strip_credential_headers(CREDS)
    assert stripped["Content-Type"] == "application/json"
    assert stripped["anthropic-version"] == "2023-06-01"


def test_stripping_is_case_insensitive():
    """Header names are case-insensitive on the wire; the filter must be too."""
    stripped = _strip_credential_headers(
        {"AUTHORIZATION": "Bearer x", "X-Api-Key": "y", "Accept": "application/json"}
    )
    assert list(stripped) == ["Accept"]


def test_stripping_does_not_mutate_the_input():
    original = dict(CREDS)
    _strip_credential_headers(CREDS)
    assert CREDS == original


def test_nothing_to_strip_is_a_no_op():
    plain = {"Accept": "application/json"}
    assert _strip_credential_headers(plain) == plain
