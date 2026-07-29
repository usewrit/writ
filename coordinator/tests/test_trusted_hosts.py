"""
Host-header allowlist tests (no DB, no network).

THE BUG these lock down: a production install on a real domain used to answer
**400 Invalid host header** to every one of its own users. ``main.py`` built the
allowlist from ``settings.frontend_url``, whose default is
``http://localhost:3000``, plus an ``ALLOWED_HOSTS`` env that ``.env.example``
ships blank and that nothing validated. So the documented production path —
set ``ENVIRONMENT=production``, put a proxy on ``writ.example.com`` — produced a
coordinator that trusted only ``localhost`` and rejected everything else, while
the "no hosts resolved, skipping enforcement" branch could never fire.

Two invariants follow, and both are tested here:

  1. The hostname of ``WRIT_PUBLIC_URL`` is ALWAYS trusted. It is derived, never
     configured, so the common "I set my public URL" path cannot self-reject.
  2. Loopback is ALWAYS trusted. The container healthcheck is
     ``curl -f http://localhost:8000/health`` from inside the container; evicting
     localhost would make Docker declare a healthy coordinator unhealthy and
     restart it forever.

Plus the live-apply contract: Settings → Network's "Trusted hosts" field used to
persist a value that nothing read, because Starlette's TrustedHostMiddleware
captures its allowlist once at startup.
"""
import os
import sys

import pytest

COORDINATOR_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if COORDINATOR_DIR not in sys.path:
    sys.path.insert(0, COORDINATOR_DIR)

# `import config` builds the module-level settings singleton and runs its
# validator; give it a valid environment first. Every assertion below builds an
# isolated Settings of its own.
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("API_SECRET_KEY", "test-api-secret-0123456789abcdefABCDEF0123456789")
os.environ.setdefault("HMAC_SECRET_KEY", "test-hmac-secret-0123456789abcdefABCDEF0123456789")
os.environ.setdefault("RECORDER_AUTH_SECRET", "test-recorder-secret-0123456789abcdefABCDEF")

from config import Settings  # noqa: E402
from security import trusted_hosts  # noqa: E402

STRONG = "a" * 64
PROD_ENV = {
    "ENVIRONMENT": "production",
    "API_SECRET_KEY": STRONG,
    "HMAC_SECRET_KEY": STRONG,
    "JWT_SECRET_KEY": STRONG,
    "SECRET_ENCRYPTION_KEY": "b" * 43 + "=",
    "INTERNAL_API_SECRET": STRONG,
    "GATEWAY_SECRET": STRONG,
    "RECORDER_AUTH_SECRET": STRONG,
}


def _prod_settings(monkeypatch, **overrides) -> Settings:
    """A Settings instance that passes production validation, plus overrides."""
    for key, value in {**PROD_ENV, **overrides}.items():
        monkeypatch.setenv(key, value)
    # Settings reads os.environ at construction — no subprocess needed.
    return Settings()


@pytest.fixture(autouse=True)
def _reset_allowlist():
    """Each test owns the process-global allowlist; restore it afterwards."""
    before, enforced = trusted_hosts.current(), trusted_hosts.is_enforced()
    yield
    trusted_hosts.configure(before, enforced=enforced)


# ---------------------------------------------------------------------------
# Derivation: WRIT_PUBLIC_URL is the source of truth
# ---------------------------------------------------------------------------
def test_public_url_host_is_trusted_without_allowed_hosts(monkeypatch):
    """The regression itself: a domain deploy that sets ONLY the public URL."""
    s = _prod_settings(monkeypatch, WRIT_PUBLIC_URL="https://writ.example.com", ALLOWED_HOSTS="")
    assert "writ.example.com" in s.allowed_hosts_list

    trusted_hosts.configure(s.allowed_hosts_list, enforced=True)
    assert trusted_hosts.is_allowed("writ.example.com")
    # Browsers send the port for a non-default one; it must not defeat the match.
    assert trusted_hosts.is_allowed("writ.example.com:443")
    assert not trusted_hosts.is_allowed("evil.example.net")


def test_loopback_always_trusted_so_the_healthcheck_survives(monkeypatch):
    """`curl -f http://localhost:8000/health` runs INSIDE the container."""
    s = _prod_settings(monkeypatch, WRIT_PUBLIC_URL="https://writ.example.com", ALLOWED_HOSTS="")
    trusted_hosts.configure(s.allowed_hosts_list, enforced=True)
    assert trusted_hosts.is_allowed("localhost:8000")
    assert trusted_hosts.is_allowed("127.0.0.1:8000")


def test_allowed_hosts_is_additive_not_a_replacement(monkeypatch):
    s = _prod_settings(
        monkeypatch,
        WRIT_PUBLIC_URL="https://writ.example.com",
        ALLOWED_HOSTS="alias.example.com, *.tenant.example.com",
    )
    trusted_hosts.configure(s.allowed_hosts_list, enforced=True)
    assert trusted_hosts.is_allowed("writ.example.com")     # still derived
    assert trusted_hosts.is_allowed("alias.example.com")
    assert trusted_hosts.is_allowed("a.tenant.example.com")
    # A leading-wildcard entry covers the bare domain too, as Starlette's did.
    assert trusted_hosts.is_allowed("tenant.example.com")
    assert not trusted_hosts.is_allowed("tenant.example.com.evil.net")


def test_case_and_whitespace_are_normalised(monkeypatch):
    s = _prod_settings(monkeypatch, WRIT_PUBLIC_URL="https://Writ.Example.COM", ALLOWED_HOSTS="  Alias.Example.com  ")
    trusted_hosts.configure(s.allowed_hosts_list, enforced=True)
    assert trusted_hosts.is_allowed("WRIT.EXAMPLE.COM")
    assert trusted_hosts.is_allowed("alias.example.com")


def test_missing_host_header_is_rejected(monkeypatch):
    s = _prod_settings(monkeypatch, WRIT_PUBLIC_URL="https://writ.example.com")
    trusted_hosts.configure(s.allowed_hosts_list, enforced=True)
    assert not trusted_hosts.is_allowed(None)
    assert not trusted_hosts.is_allowed("")


def test_ipv6_literal_is_not_truncated_by_port_splitting():
    """`::1` contains colons; a naive rsplit(':') would reduce it to nothing."""
    trusted_hosts.configure(["[::1]", "writ.example.com"], enforced=True)
    assert trusted_hosts.is_allowed("[::1]")
    assert trusted_hosts.is_allowed("[::1]:8000")


# ---------------------------------------------------------------------------
# Enforcement posture
# ---------------------------------------------------------------------------
def test_development_accepts_any_host():
    """LAN testing, tunnels and container hostnames must all just work."""
    trusted_hosts.configure(["writ.example.com"], enforced=False)
    assert trusted_hosts.is_allowed("anything.at.all")
    assert not trusted_hosts.is_enforced()


def test_star_disables_the_check():
    trusted_hosts.configure(["*"], enforced=True)
    assert trusted_hosts.is_allowed("anything.at.all")
    assert not trusted_hosts.is_enforced()


# ---------------------------------------------------------------------------
# Live apply — the half that used to be missing entirely
# ---------------------------------------------------------------------------
def test_apply_merges_operator_hosts_over_the_derived_base(monkeypatch):
    """Settings → Network must take effect without a restart."""
    import config as config_module

    s = _prod_settings(monkeypatch, WRIT_PUBLIC_URL="https://writ.example.com", ALLOWED_HOSTS="")
    monkeypatch.setattr(config_module, "settings", s)

    trusted_hosts.configure(s.allowed_hosts_list, enforced=True)
    assert not trusted_hosts.is_allowed("vanity.example.org")

    effective = trusted_hosts.apply(["vanity.example.org"])

    assert trusted_hosts.is_allowed("vanity.example.org")
    # Merged, never replaced: saving the form cannot drop the derived entries
    # and lock the operator (or the healthcheck) out.
    assert "writ.example.com" in effective
    assert "localhost" in effective
    assert trusted_hosts.is_allowed("writ.example.com")
    assert trusted_hosts.is_allowed("localhost:8000")


def test_apply_with_an_empty_list_cannot_lock_you_out(monkeypatch):
    import config as config_module

    s = _prod_settings(monkeypatch, WRIT_PUBLIC_URL="https://writ.example.com")
    monkeypatch.setattr(config_module, "settings", s)

    trusted_hosts.apply([])
    assert trusted_hosts.is_allowed("writ.example.com")
    assert trusted_hosts.is_allowed("localhost")


# ---------------------------------------------------------------------------
# Production validation of the setting the allowlist is derived from
# ---------------------------------------------------------------------------
def test_production_requires_writ_public_url(monkeypatch):
    """Unset, it silently enrols agents against a URL that resolves nowhere."""
    for key, value in PROD_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("WRIT_PUBLIC_URL", raising=False)
    with pytest.raises(ValueError, match="WRIT_PUBLIC_URL must be set"):
        Settings()


def test_production_rejects_a_schemeless_public_url(monkeypatch):
    with pytest.raises(ValueError, match="must include a scheme"):
        _prod_settings(monkeypatch, WRIT_PUBLIC_URL="writ.example.com")


def test_localhost_public_url_still_boots_in_production(monkeypatch):
    """.env.example ships ENVIRONMENT=production with a localhost URL — the
    documented local trial must keep working."""
    s = _prod_settings(monkeypatch, WRIT_PUBLIC_URL="http://localhost:8000")
    assert s.public_hostname == "localhost"


def test_plaintext_on_a_routable_host_warns_but_boots(monkeypatch):
    """A trusted private network is a legitimate choice; it must be loud, not fatal.

    The warning is captured with a handler attached directly to the `config`
    logger rather than with pytest's `caplog`. Importing `main` (which other
    suites in this run do) installs the app's own JSON logging handlers on the
    root logger, and caplog's root-level capture stops seeing these records once
    that happens — so a caplog-based assertion passes alone and fails in a full
    run.
    """
    import logging

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("config")
    handler = _Capture(level=logging.WARNING)
    logger.addHandler(handler)
    previous_level, previous_disabled = logger.level, logger.disabled
    logger.setLevel(logging.WARNING)
    # `logging.config.fileConfig` defaults to disable_existing_loggers=True and
    # alembic/env.py calls it, so by the time a full-suite run reaches this test
    # the "config" logger can already be flagged disabled — at which point
    # `.warning()` short-circuits before any handler is consulted and this test
    # fails for a reason that has nothing to do with the code under test.
    logger.disabled = False
    try:
        s = _prod_settings(monkeypatch, WRIT_PUBLIC_URL="http://writ.example.com")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.disabled = previous_disabled

    assert s.public_hostname == "writ.example.com"
    assert any("plaintext http" in r.getMessage().lower() for r in records)
