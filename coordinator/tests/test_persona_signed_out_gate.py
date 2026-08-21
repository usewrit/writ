"""A persona crawl must not fan out on a session that is only SHAPED like a login.

Anonymous visitors are handed HttpOnly session cookies too, so `session_is_usable`
answers "yes" for a jar minted while logged OUT — and the crawl then banks copies
of the sign-in page while reporting success. The only reliable question is the one
the site answers, so the gate probes a GATED url before fanning out.

These pin the three-valued contract: only a proven signed-OUT verdict blocks.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

COORD_DIR = Path(__file__).resolve().parents[1]
if str(COORD_DIR) not in sys.path:
    sys.path.insert(0, str(COORD_DIR))

# Throwaway SQLite + the documented local-trial escape hatch, set BEFORE importing
# anything that builds settings/the engine at import time (mirrors
# test_crawl_orchestrator_loop.py).
_TMP_DB = tempfile.NamedTemporaryFile(prefix="writ-persona-gate-", suffix=".db", delete=False)
_TMP_DB.close()
os.environ["WRIT_DB_PATH"] = _TMP_DB.name
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("ALLOW_INSECURE_DEV", "true")

from models.crawl_job import CrawlJob  # noqa: E402
from services import crawl_orchestrator as co  # noqa: E402

pytestmark = pytest.mark.asyncio


class _Persona:
    id = 7
    is_active = True
    login_workflow_id = None
    fingerprint = None
    validation_status = "valid"
    last_login_error = None


class _DB:
    """Enough session surface for the gate: it only flushes bookkeeping here."""
    async def flush(self):
        return None


def _crawl():
    return CrawlJob(id=1, name="t", seed_url="https://example.com", persona_id=7)


def _patch_persona(monkeypatch, persona, session):
    from services.persona_service import PersonaService

    async def _get_owned(db, pid):
        return persona

    monkeypatch.setattr(PersonaService, "get_owned", staticmethod(_get_owned))
    monkeypatch.setattr(PersonaService, "load_session", staticmethod(lambda p: session))


# A jar that passes the shape check — exactly the anonymous-but-plausible case.
_USABLE = {"cookies": [{"name": "session", "value": "x", "domain": "example.com"}]}


async def test_proven_signed_out_blocks_the_crawl(monkeypatch):
    """The site says signed out and there is no way to re-login -> refuse."""
    persona = _Persona()
    _patch_persona(monkeypatch, persona, _USABLE)

    async def _verify(db, crawl, p, session):
        return False, "redirected to /login"

    monkeypatch.setattr(co, "_verify_persona_session", _verify)

    ok, err, session = await co._ensure_persona_session(_DB(), _crawl())
    assert ok is False
    assert session is None
    assert "not actually signed in" in err
    # The identity itself is flagged, not just this one crawl.
    assert persona.validation_status == "invalid"


async def test_unknown_verdict_proceeds(monkeypatch):
    """No gated URL to ask about is NOT evidence of a dead session — proceed."""
    _patch_persona(monkeypatch, _Persona(), _USABLE)

    async def _verify(db, crawl, p, session):
        return None, None

    monkeypatch.setattr(co, "_verify_persona_session", _verify)

    ok, err, session = await co._ensure_persona_session(_DB(), _crawl())
    assert (ok, err) == (True, None)
    assert session == _USABLE


async def test_confirmed_signed_in_proceeds(monkeypatch):
    _patch_persona(monkeypatch, _Persona(), _USABLE)

    async def _verify(db, crawl, p, session):
        return True, None

    monkeypatch.setattr(co, "_verify_persona_session", _verify)

    ok, err, session = await co._ensure_persona_session(_DB(), _crawl())
    assert (ok, err) == (True, None)
    assert session == _USABLE


async def test_signed_out_session_is_re_logged_in_when_possible(monkeypatch):
    """A signed-out warm session is recoverable when the persona can sign itself
    in: re-login, then verify THAT session too."""
    persona = _Persona()
    persona.login_workflow_id = 42
    _patch_persona(monkeypatch, persona, _USABLE)

    fresh = {"cookies": [{"name": "session", "value": "fresh", "domain": "example.com"}]}
    verdicts = iter([(False, "redirected to /login"), (True, None)])

    async def _verify(db, crawl, p, session):
        return next(verdicts)

    async def _ensure_fresh(pid):
        return True, None, fresh

    monkeypatch.setattr(co, "_verify_persona_session", _verify)
    import services.persona_login as pl
    monkeypatch.setattr(pl, "ensure_fresh_session", _ensure_fresh)

    ok, err, session = await co._ensure_persona_session(_DB(), _crawl())
    assert (ok, err) == (True, None)
    assert session == fresh


async def test_relogin_that_is_still_signed_out_stops_instead_of_looping(monkeypatch):
    """If a just-completed login still probes signed out, running it again would
    only repeat the result — refuse with the explanatory message."""
    persona = _Persona()
    persona.login_workflow_id = 42
    _patch_persona(monkeypatch, persona, _USABLE)

    async def _verify(db, crawl, p, session):
        return False, "redirected to /login"

    async def _ensure_fresh(pid):
        return True, None, {"cookies": [{"name": "s", "value": "2", "domain": "example.com"}]}

    monkeypatch.setattr(co, "_verify_persona_session", _verify)
    import services.persona_login as pl
    monkeypatch.setattr(pl, "ensure_fresh_session", _ensure_fresh)

    ok, err, session = await co._ensure_persona_session(_DB(), _crawl())
    assert ok is False and session is None
    assert "not actually signed in" in err


async def test_gated_url_prefers_the_login_workflows_landing_page():
    """The probe must ask a page that REQUIRES the login. Probing the public seed
    proves nothing: it answers "signed in" for an anonymous jar too."""
    from services import persona_session_probe as probe

    class _Row:
        def first(self):
            return ("https://example.com/login",
                    [{"type": "navigate", "config": {"url": "https://example.com/login"}},
                     {"type": "click", "config": {}},
                     {"type": "navigate", "config": {"url": "https://example.com/profile"}}])

    class _DBQ:
        async def execute(self, *a, **k):
            return _Row()

    class _P:
        login_workflow_id = 42

    url = await probe.gated_url_for_persona(_DBQ(), _P())
    # The LAST navigate is where the sign-in lands — and the /login page itself is
    # never a valid probe target (it is meant to render signed-out).
    assert url == "https://example.com/profile"
