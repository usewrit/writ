"""
Startup secret-validation tests (no DB, no network).

THE INVARIANT under test: the coordinator refuses to boot with a token-signing
secret that anyone could guess — and "blank" is the worst such value, not an
absent one. ``python-jose`` signs and verifies HS256 with a zero-length key
without complaint, so an empty ``JWT_SECRET_KEY`` is not a misconfiguration that
fails loudly at first use; it is a *publicly known* signing key that lets any
anonymous caller mint an ``is_platform_admin`` session.

That matters here specifically because ``.env.example`` ships every secret as a
bare ``NAME=`` line. An operator who copies the template and fills it in only
partially — or an orchestrator that interpolates an unset variable to ``""`` —
must not get a running instance.

Each case constructs a fresh ``Settings`` against a controlled environment;
``Settings`` reads ``os.environ`` at instantiation, so no subprocess is needed.
"""
import os
import sys

import pytest

COORDINATOR_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if COORDINATOR_DIR not in sys.path:
    sys.path.insert(0, COORDINATOR_DIR)

# `import config` constructs the module-level `settings` singleton, which runs the
# very validator under test. Give it a valid environment first (the same pattern
# the other suites use) so importing the module cannot fail; every assertion below
# builds its own isolated Settings instead of touching this one.
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("API_SECRET_KEY", "test-api-secret-0123456789abcdefABCDEF0123456789")
os.environ.setdefault("HMAC_SECRET_KEY", "test-hmac-secret-0123456789abcdefABCDEF0123456789")
os.environ.setdefault("RECORDER_AUTH_SECRET", "test-recorder-secret-0123456789abcdefABCDEF")

from config import Settings  # noqa: E402

# 64 hex chars, i.e. exactly what `openssl rand -hex 32` produces.
STRONG = "a" * 64
STRONG_ALT = "b" * 64
FERNET = "aGVsbG8td29ybGQtdGhpcy1pcy0zMi1ieXRlcy1rZXk="

# Every environment variable the validator reads, so a case is never influenced
# by the developer's real shell or by an earlier test.
_MANAGED_VARS = (
    "ENVIRONMENT",
    "ALLOW_INSECURE_DEV",
    "API_SECRET_KEY",
    "JWT_SECRET_KEY",
    "SECRET_KEY",
    "HMAC_SECRET_KEY",
    "SECRET_ENCRYPTION_KEY",
    "INTERNAL_API_SECRET",
    "GATEWAY_SECRET",
    "RECORDER_AUTH_SECRET",
    "CORS_ORIGINS",
)


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Return a setter that builds a clean environment for one Settings() call."""
    for name in _MANAGED_VARS:
        monkeypatch.delenv(name, raising=False)
    # Settings has `env_file=".env"`; point the process at a directory with no
    # .env so a developer's real file can never leak into an assertion.
    monkeypatch.chdir(tmp_path)

    def _set(**overrides):
        for key, value in overrides.items():
            if value is None:
                monkeypatch.delenv(key.upper(), raising=False)
            else:
                monkeypatch.setenv(key.upper(), value)
        return Settings()

    return _set


def _production(**overrides):
    """A minimal, fully-valid production environment, before overrides."""
    base = {
        "environment": "production",
        "api_secret_key": STRONG,
        "hmac_secret_key": STRONG_ALT,
        "secret_encryption_key": FERNET,
        "internal_api_secret": STRONG,
        "gateway_secret": STRONG_ALT,
        "recorder_auth_secret": STRONG,
        "cors_origins": "https://writ.example.com",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# The regression this file exists for
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "blank_var",
    ["api_secret_key", "jwt_secret_key", "hmac_secret_key", "secret_encryption_key"],
)
def test_blank_secret_refuses_to_boot_in_production(env, blank_var):
    """An explicitly blank secret must fail closed, not sail through as 'unset'."""
    with pytest.raises(ValueError):
        env(**_production(**{blank_var: ""}))


def test_all_secrets_blank_refuses_to_boot(env):
    """`cp .env.example .env` with nothing filled in must never produce a server."""
    with pytest.raises(ValueError):
        env(
            environment="production",
            api_secret_key="",
            jwt_secret_key="",
            hmac_secret_key="",
            secret_encryption_key="",
            internal_api_secret="",
            gateway_secret="",
            recorder_auth_secret="",
        )


def test_blank_signing_secret_rejected_even_in_development(env):
    """The signing-key guard applies in EVERY environment, not just production."""
    with pytest.raises(ValueError):
        env(environment="development", api_secret_key="", jwt_secret_key="")


def test_whitespace_only_secret_is_rejected(env):
    with pytest.raises(ValueError):
        env(**_production(api_secret_key="        "))


# ---------------------------------------------------------------------------
# Length floor and known-bad values
# ---------------------------------------------------------------------------

def test_short_secret_is_rejected(env):
    """31 characters is below the floor; every doc generates 64."""
    with pytest.raises(ValueError):
        env(**_production(api_secret_key="a" * 31))


def test_secret_at_the_length_floor_is_accepted(env):
    settings = env(**_production(api_secret_key="a" * 32))
    assert settings.api_secret_key == "a" * 32


@pytest.mark.parametrize(
    "bad", ["change-this-in-production-use-openssl-rand-hex-32", "dev_" + "x" * 40]
)
def test_shipped_default_secret_is_rejected(env, bad):
    with pytest.raises(ValueError):
        env(**_production(api_secret_key=bad))


def test_compromised_fernet_key_is_rejected_in_every_environment(env):
    """The three keys that leaked into source control stay rejected forever.

    They are matched by SHA-256 digest — the literal key material must not be
    present in this repository, which is public.
    """
    leaked = "QUqv2NKlgTzwRHu-YsHHfVV0drq9AdFaDjBaz9px0ko="
    with pytest.raises(ValueError, match="compromised"):
        env(**_production(secret_encryption_key=leaked))
    with pytest.raises(ValueError, match="compromised"):
        env(
            environment="development",
            api_secret_key=STRONG,
            secret_encryption_key=leaked,
        )


# ---------------------------------------------------------------------------
# Service-to-service secrets (read straight from the environment)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "var", ["internal_api_secret", "gateway_secret", "recorder_auth_secret"]
)
def test_service_secret_must_be_set_in_production(env, var):
    """/internal/* can return decrypted provider keys — these are not optional."""
    with pytest.raises(ValueError, match=var.upper()):
        env(**_production(**{var: ""}))


@pytest.mark.parametrize(
    "var", ["internal_api_secret", "gateway_secret", "recorder_auth_secret"]
)
def test_service_secret_rejects_the_well_known_dev_default(env, var):
    dev_default = {
        "internal_api_secret": "dev_internal_secret",
        "gateway_secret": "dev_gateway_secret",
        "recorder_auth_secret": "dev_recorder_secret",
    }[var]
    with pytest.raises(ValueError, match=var.upper()):
        env(**_production(**{var: dev_default}))


# ---------------------------------------------------------------------------
# The happy paths must keep working
# ---------------------------------------------------------------------------

def test_valid_production_config_boots(env):
    settings = env(**_production())
    assert settings.is_production
    assert settings.api_secret_key == STRONG


def test_unset_jwt_secret_falls_back_to_api_secret(env):
    """JWT_SECRET_KEY is documented as optional — absent is fine, blank is not."""
    settings = env(**_production(jwt_secret_key=None))
    assert settings.jwt_secret_key is None
    assert settings.api_secret_key == STRONG


def test_insecure_dev_optin_still_boots_with_the_shipped_default(env):
    """The documented throwaway-local-trial escape hatch is preserved."""
    settings = env(
        environment="development",
        allow_insecure_dev="true",
        api_secret_key="change-this-in-production-use-openssl-rand-hex-32",
    )
    assert settings.environment == "development"
