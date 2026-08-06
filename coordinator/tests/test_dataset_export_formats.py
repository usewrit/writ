"""The self-host dataset export must offer EVERY output format the cloud does.

Self-host previously served `?format=csv|json` while the cloud's Datasets API
also served `markdown` and `html` — the renderers were sitting in the twin
`services/extracted_data_table.py` with nothing on this side calling them. This
suite pins the parity so the gap cannot silently reopen:

  1. `services/dataset_formats.py` (one of two byte-identical copies, pinned by
     a twin test so they cannot drift) offers all four,
     with the right media type, download extension and security headers.
  2. The coordinator's export route delegates to it rather than hand-rolling a
     csv/json branch again.
  3. `main.py`'s security middleware does NOT overwrite a CSP a route already
     set. This one is self-host specific and load-bearing: the coordinator serves
     the SPA from the SAME origin as the API, so its global policy must allow
     `script-src 'self'`. An html render echoes SCRAPED third-party content back
     from that origin, so it ships its own `default-src 'none'`; a middleware
     that clobbered it would silently downgrade the render to a policy that
     permits same-origin script.

Dependency-light on purpose: `services.dataset_formats` pulls in fastapi and the
table service but never `config`, so this runs on a bare clone with no env set.
"""
import re
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi import HTTPException  # noqa: E402

from services import dataset_formats as df  # noqa: E402

#: A document-shaped row (a crawl page): long-form `markdown` + a title/url, which
#: is what flips the renderers into document mode.
_DOC_ROWS = [{
    "run_id": 1, "run_at": "2026-07-28T10:00:00Z", "status": "success",
    "fields": {
        "url": "https://example.test/a",
        "title": "First page",
        "markdown": "# Heading\n\nBody paragraph. " + ("filler " * 60),
    },
}]
_DOC_COLUMNS = ["url", "title", "markdown"]

#: A structured (non-document) row — renders as a table in every format.
_TABLE_ROWS = [
    {"run_id": 1, "run_at": "t1", "status": "success", "fields": {"store": "34008", "net": "$38.75"}},
    {"run_id": 2, "run_at": "t2", "status": "success", "fields": {"store": "34009", "net": "$12.00"}},
]
_TABLE_COLUMNS = ["store", "net"]


def test_every_cloud_format_is_offered():
    """The set itself — if the cloud gains a format, this twin gains it too."""
    assert df.DATASET_FORMATS == ("json", "csv", "markdown", "html")


@pytest.mark.parametrize("fmt,media,ext", [
    ("json", "application/json", "json"),
    ("csv", "text/csv", "csv"),
    ("markdown", "text/markdown", "md"),
    ("html", "text/html", "html"),
])
def test_render_media_type_and_download_extension(fmt, media, ext):
    resp = df.render_dataset(fmt, _TABLE_COLUMNS, _TABLE_ROWS, title="My WF", filename="my-wf-data")
    assert resp.status_code == 200
    assert resp.media_type.startswith(media)
    assert resp.headers["content-disposition"] == f'attachment; filename="my-wf-data.{ext}"'


@pytest.mark.parametrize("fmt", df.DATASET_FORMATS)
def test_every_format_carries_the_security_headers(fmt):
    resp = df.render_dataset(fmt, _DOC_COLUMNS, _DOC_ROWS, title="My WF")
    assert resp.headers["x-content-type-options"] == "nosniff"
    csp = resp.headers["content-security-policy"]
    directives = dict(
        (p.strip().split(" ", 1) + [""])[:2] for p in csp.split(";") if p.strip()
    )
    effective = directives.get("script-src", directives.get("default-src", "")).strip()
    assert effective == "'none'", f"{fmt} render must not be able to execute script; got {csp!r}"


def test_render_without_filename_is_not_a_download():
    """The read APIs render inline; only the export routes frame a download."""
    resp = df.render_dataset("html", _TABLE_COLUMNS, _TABLE_ROWS, title="My WF")
    assert "content-disposition" not in resp.headers


@pytest.mark.parametrize("fmt,default", [(None, "csv"), ("", "csv"), (None, "json")])
def test_absent_format_falls_back_to_the_caller_default(fmt, default):
    assert df.norm_format(fmt, default=default) == default


@pytest.mark.parametrize("raw,expected", [
    ("CSV", "csv"), (" markdown ", "markdown"), ("HTML", "html"), ("Json", "json"),
])
def test_format_is_case_and_space_insensitive(raw, expected):
    assert df.norm_format(raw, default="csv") == expected


@pytest.mark.parametrize("bad", ["xml", "yaml", "pdf", "htm", "md"])
def test_unknown_format_is_a_400_not_a_silent_fallback(bad):
    with pytest.raises(HTTPException) as exc:
        df.norm_format(bad, default="csv")
    assert exc.value.status_code == 400
    assert "Unsupported format" in exc.value.detail
    # The error names the alternatives, so a caller can self-correct.
    for f in df.DATASET_FORMATS:
        assert f in exc.value.detail


def test_markdown_renders_documents_for_a_document_shaped_dataset():
    body = df.render_body("markdown", _DOC_COLUMNS, _DOC_ROWS, title="My WF")
    assert body.startswith("# My WF")
    assert "## First page" in body           # heading from the title column
    assert "<https://example.test/a>" in body  # source link
    assert "|" not in body.split("\n")[0]    # documents, not a table grid


def test_markdown_renders_a_table_for_a_structured_dataset():
    body = df.render_body("markdown", _TABLE_COLUMNS, _TABLE_ROWS)
    header = body.splitlines()[0]
    assert header.startswith("| run_id | run_at | status | store | net |")


def test_html_render_escapes_scraped_markup():
    """The XSS backstop is the renderer, not just the CSP: raw HTML in scraped
    content is escaped to inert text and hostile URL schemes never reach an href."""
    hostile = [{
        "run_id": 1, "run_at": "t", "status": "success",
        "fields": {
            "url": "javascript:alert(1)",
            "title": "<script>alert(1)</script>",
            "markdown": "<script>alert(1)</script>\n\n[x](javascript:alert(1))\n\n" + ("word " * 60),
        },
    }]
    body = df.render_body("html", _DOC_COLUMNS, hostile, title="X")
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body
    assert not re.search(r'href\s*=\s*"javascript:', body, re.I)


def test_csv_export_shields_formula_injection():
    """The CSV path keeps its OWASP shield in every format-layer call."""
    rows = [{"run_id": 1, "run_at": "t", "status": "success",
             "fields": {"note": "=cmd|' /C calc'!A0"}}]
    body = df.render_body("csv", ["note"], rows)
    assert "'=cmd" in body


# ---------------------------------------------------------------------------
# Wiring guards — the format layer only helps if the route and the middleware
# actually let it through.
# ---------------------------------------------------------------------------

def test_export_route_delegates_to_the_shared_format_layer():
    src = (BACKEND_DIR / "routers" / "automation.py").read_text(encoding="utf-8")
    assert 'dataset_formats.norm_format(format, default="csv")' in src, (
        "the export route stopped validating ?format= through the shared layer"
    )
    assert src.count("dataset_formats.render_dataset(") >= 2, (
        "a data route hand-rolls its serialization again — markdown/html will "
        "silently disappear from self-host"
    )


def test_read_route_also_takes_a_format():
    """`/data` renders the current page too, not just `/data/export` — the API
    modal tells operators every read takes `?format=`, so it must be true."""
    src = (BACKEND_DIR / "routers" / "automation.py").read_text(encoding="utf-8")
    assert 'dataset_formats.norm_format(format, default="json")' in src, (
        "the /data read no longer accepts ?format= — json-only again"
    )


def test_security_middleware_does_not_clobber_a_route_csp():
    src = (BACKEND_DIR / "main.py").read_text(encoding="utf-8")
    m = re.search(
        r'if\s+"Content-Security-Policy"\s+not\s+in\s+response\.headers:\s*\n'
        r'\s+response\.headers\["Content-Security-Policy"\]',
        src,
    )
    assert m, (
        "main.py overwrites any route-set Content-Security-Policy. The dataset "
        "html render ships its own default-src 'none'; clobbering it with the "
        "SPA policy (script-src 'self') downgrades a stored-XSS backstop."
    )


# ---------------------------------------------------------------------------
# The routes themselves, driven through a real request. Importing the router
# pulls in `config`, which needs the same env CI already exports for this suite
# (ENVIRONMENT/ALLOW_INSECURE_DEV/API_SECRET_KEY — see .github/workflows/ci.yml).
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(monkeypatch):
    pytest.importorskip("fakeredis")
    try:
        import routers.automation as automation
        from database import get_db
        from security.api_key import get_current_api_key
        from services import extracted_data_table as edt
    except Exception as exc:  # pragma: no cover - env without the app deps
        pytest.skip(f"coordinator app not importable here: {exc}")

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    class _Wf:
        id, name, workflow_type = 42, "Hotel prices", "scrape"

    async def _load(db, workflow_id, api_key):
        return _Wf()

    # Signature mirrors the real helper: routes pass the loaded workflow row so the
    # scan can pin itself to the right subsystem (a workflow's dataset never serves
    # crawl shard rows, and vice versa).
    async def _scan(db, workflow_id, for_update=False, workflow=None):
        return [object()], False

    monkeypatch.setattr(automation, "check_api_key_scope", lambda *a, **kw: None)
    monkeypatch.setattr(automation, "_load_workflow_for_data", _load)
    monkeypatch.setattr(automation, "_scan_workflow_data_tasks", _scan)
    monkeypatch.setattr(edt, "build_table", lambda tasks, **kw: {
        "columns": list(_TABLE_COLUMNS), "rows": [dict(r) for r in _TABLE_ROWS],
        "declared": False, "collection": None, "collections": [], "total": 2,
    })

    app = FastAPI()
    app.include_router(automation.router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: None
    app.dependency_overrides[get_current_api_key] = lambda: {
        "is_platform_admin": True, "scopes": ["workflows:read"],
    }
    return TestClient(app)


_BASE = "/api/automation/workflows/42/data"


@pytest.mark.parametrize("fmt,media,ext", [
    ("csv", "text/csv", "csv"),
    ("json", "application/json", "json"),
    ("markdown", "text/markdown", "md"),
    ("html", "text/html", "html"),
])
def test_export_route_serves_every_format(client, fmt, media, ext):
    r = client.get(f"{_BASE}/export?format={fmt}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(media)
    assert r.headers["content-disposition"].endswith(f'.{ext}"')
    assert "34008" in r.text


def test_export_route_still_defaults_to_csv(client):
    r = client.get(f"{_BASE}/export")
    assert r.headers["content-type"].startswith("text/csv")


def test_read_route_serves_every_format(client):
    assert "workflow_id" in client.get(_BASE).json()      # default envelope
    assert client.get(f"{_BASE}?format=csv").text.startswith("run_id,")
    assert client.get(f"{_BASE}?format=html").text.startswith("<!doctype html>")
    assert "| run_id |" in client.get(f"{_BASE}?format=markdown").text


def test_routes_reject_an_unknown_format(client):
    for path in (f"{_BASE}?format=xml", f"{_BASE}/export?format=xml"):
        r = client.get(path)
        assert r.status_code == 400, path
        assert "Unsupported format" in r.json()["detail"]
