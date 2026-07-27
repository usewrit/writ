"""The OpenAPI schema must not be readable by anonymous callers in production.

Disabling /docs and /redoc while still serving /openapi.json is only half a
decision. The schema is the part that actually enumerates every route, its
parameters and its response shapes — roughly 300 endpoints — and it answered
without any authentication. Turning off the human-readable viewer while leaving
the machine-readable map in place buys nothing.

An operator who genuinely wants it can opt in with WRIT_EXPOSE_OPENAPI=true.
"""
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from config import should_expose_openapi  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("WRIT_EXPOSE_OPENAPI", raising=False)


def test_production_hides_the_schema():
    assert should_expose_openapi(is_production=True) is False


def test_development_serves_the_schema():
    """Contributors need it; the point is only that production does not."""
    assert should_expose_openapi(is_production=False) is True


@pytest.mark.parametrize("value", ["true", "TRUE", "True", "1", "yes", " true "])
def test_production_opt_in_restores_the_schema(monkeypatch, value):
    monkeypatch.setenv("WRIT_EXPOSE_OPENAPI", value)
    assert should_expose_openapi(is_production=True) is True


@pytest.mark.parametrize("value", ["", "false", "0", "no", "off", "maybe"])
def test_only_an_affirmative_value_opts_in(monkeypatch, value):
    """Anything that is not clearly a yes must leave the schema closed."""
    monkeypatch.setenv("WRIT_EXPOSE_OPENAPI", value)
    assert should_expose_openapi(is_production=True) is False


def test_the_flag_cannot_turn_it_off_in_development(monkeypatch):
    """The variable only ever opens; development is unconditionally open."""
    monkeypatch.setenv("WRIT_EXPOSE_OPENAPI", "false")
    assert should_expose_openapi(is_production=False) is True


def test_the_app_applies_the_rule():
    """Guard against the wiring being dropped while the helper stays correct."""
    source = (BACKEND_DIR / "main.py").read_text()
    assert "should_expose_openapi" in source, "main.py no longer consults the rule"
    assert "openapi_url=" in source, (
        "main.py no longer sets openapi_url, so FastAPI falls back to serving "
        "/openapi.json unconditionally"
    )
