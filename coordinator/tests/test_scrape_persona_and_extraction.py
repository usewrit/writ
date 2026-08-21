"""`/crawl/scrape` must be able to reach a page behind a login, and must return
the page rather than its chrome.

Before: the endpoint took only a URL, so it could never see a signed-in page —
its own error even told the caller "a persona-backed crawl can reach it" while
offering no way to ask for one. And its extractor was a regex tag stripper, so
every result led with nav and lost tables entirely.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

COORD_DIR = Path(__file__).resolve().parents[1]
if str(COORD_DIR) not in sys.path:
    sys.path.insert(0, str(COORD_DIR))

_TMP_DB = tempfile.NamedTemporaryFile(prefix="writ-scrape-test-", suffix=".db", delete=False)
_TMP_DB.close()
os.environ["WRIT_DB_PATH"] = _TMP_DB.name
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("ALLOW_INSECURE_DEV", "true")

from services import content_extract  # noqa: E402

pytestmark = pytest.mark.asyncio

# A realistic page. Deliberately not a one-row table: readability discards a
# table that small as boilerplate (a pre-existing limitation shared with cloud),
# so asserting on one would pin behaviour this change is not responsible for.
_PAGE = """<html><head><title>Pricing</title></head><body>
<nav><a href="/login">Sign in</a></nav>
<main><article>
<h1>Pricing</h1><p>Plans that scale with you across teams.</p>
<table><thead><tr><th>Plan</th><th>Price</th></tr></thead>
<tbody><tr><td>Starter</td><td>$19</td></tr><tr><td>Pro</td><td>$49</td></tr></tbody></table>
<ul><li>Unlimited crawls</li><li>Priority support</li></ul>
</article></main>
<footer>Privacy</footer></body></html>"""


def test_ladder_keeps_the_content_and_drops_the_chrome():
    md = content_extract.extract_main_markdown(_PAGE, "https://example.com/pricing")
    assert "Plans that scale with you" in md
    # The regex stripper this replaced lost data tables outright.
    assert "$49" in md and "|" in md
    assert "Sign in" not in md and "Privacy" not in md


def test_ladder_still_works_without_the_optional_engines():
    """Existing installs have no trafilatura/readability until they reinstall
    requirements, so the fallback leg must carry the page on its own."""
    saved_t = content_extract._trafilatura
    saved_r = content_extract._ReadabilityDocument
    try:
        content_extract._trafilatura = None
        md = content_extract.extract_main_markdown(_PAGE, "https://example.com/pricing")
        assert "$19" in md and "Sign in" not in md
        content_extract._ReadabilityDocument = None
        md = content_extract.extract_main_markdown(_PAGE, "https://example.com/pricing")
        assert "$19" in md and "Sign in" not in md
    finally:
        content_extract._trafilatura = saved_t
        content_extract._ReadabilityDocument = saved_r


def test_ladder_returns_empty_rather_than_garbage_on_junk():
    """An unparseable body must fall through so the caller's own fallback runs."""
    assert content_extract.extract_main_markdown("", "https://example.com") == ""


class _Persona:
    id = 3
    is_active = True
    login_workflow_id = 9
    fingerprint = {"userAgent": "persona-UA/1.0"}


def _install_persona(monkeypatch, *, session):
    from services.persona_service import PersonaService

    async def _get_owned(db, pid):
        return _Persona()

    monkeypatch.setattr(PersonaService, "get_owned", staticmethod(_get_owned))
    monkeypatch.setattr(PersonaService, "load_session", staticmethod(lambda p: session))
    import services.persona_login as pl

    async def _fresh(pid, **kw):
        return (True, None, session) if session else (False, "no session", None)

    monkeypatch.setattr(pl, "ensure_fresh_session", _fresh)


_SESSION = {
    "cookies": [{"name": "sid", "value": "abc", "domain": "example.com", "path": "/"}],
    "fingerprint": {"userAgent": "persona-UA/1.0"},
}


async def test_scrape_presents_the_persona_session(monkeypatch):
    """The persona's cookies AND its user agent must ride the request — a jar
    replayed by a different-looking visitor is refused by plenty of stacks."""
    from routers import crawl as crawl_router

    _install_persona(monkeypatch, session=_SESSION)

    async def _guard(db, u):
        return u
    monkeypatch.setattr(crawl_router, "_guard_seed", _guard)

    seen = {}

    class _Resp:
        status_code = 200
        text = _PAGE

    async def _safe_get(url, **kw):
        seen.update(kw.get("headers") or {})
        return _Resp()

    import services.safe_fetch as sf
    monkeypatch.setattr(sf, "safe_get", _safe_get)

    body = crawl_router.ScrapeCrawlRequest(url="https://example.com/pricing", persona_id=3)
    out = await crawl_router.scrape_page(body=body, db=None, _api_key={})

    assert "sid=abc" in seen.get("Cookie", "")
    assert seen.get("User-Agent") == "persona-UA/1.0"
    assert out["persona_id"] == 3
    assert out["title"] == "Pricing"
    assert "$49" in out["markdown"]


async def test_scrape_refuses_a_persona_with_no_session(monkeypatch):
    """Scraping signed-out under a persona would return the login wall AS the
    page — refuse instead, and say what to do."""
    from fastapi import HTTPException
    from routers import crawl as crawl_router

    _install_persona(monkeypatch, session=None)

    async def _guard(db, u):
        return u
    monkeypatch.setattr(crawl_router, "_guard_seed", _guard)

    body = crawl_router.ScrapeCrawlRequest(url="https://example.com/x", persona_id=3)
    with pytest.raises(HTTPException) as exc:
        await crawl_router.scrape_page(body=body, db=None, _api_key={})
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "persona_not_signed_in"


async def test_anonymous_scrape_still_works(monkeypatch):
    """No persona: unchanged public behaviour, minus the chrome."""
    from routers import crawl as crawl_router

    async def _guard(db, u):
        return u
    monkeypatch.setattr(crawl_router, "_guard_seed", _guard)

    class _Resp:
        status_code = 200
        text = _PAGE

    async def _safe_get(url, **kw):
        assert not kw.get("headers")  # no identity presented
        return _Resp()

    import services.safe_fetch as sf
    monkeypatch.setattr(sf, "safe_get", _safe_get)

    body = crawl_router.ScrapeCrawlRequest(url="https://example.com/pricing")
    out = await crawl_router.scrape_page(body=body, db=None, _api_key={})
    assert out["persona_id"] is None
    assert "Plans that scale with you" in out["markdown"]
