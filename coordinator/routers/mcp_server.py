"""
Native MCP server for the self-hosted coordinator.

Exposes the OSS **operate** surface — the same replay / run / data / schedule /
crawl capabilities the desktop daemon serves at ``POST /mcp`` — over a single
Streamable-HTTP MCP endpoint. There are NO build / AI / concierge / marketplace
tools here (those are cloud- or desktop-AI features): this server only drives
workflows the owner already recorded and saved. It turns the self-host
coordinator into an MCP tool provider that any MCP client (Claude Code, Claude
Desktop, Cursor, …) can attach to, via the bundled ``writ-mcp`` Node connector
or directly over Streamable HTTP.

Public surfaces
---------------
    POST /mcp                  JSON-RPC 2.0 (MCP 2025-03-26). Bearer API key.
    GET  /mcp                  Capability probe / human hint (no protocol state).
    GET  /api/mcp/connect-info Copy-paste snippets for `claude mcp add`, env, etc.

Design: tool execution reuses the coordinator's OWN REST endpoints over loopback,
forwarding the caller's bearer so scope checks, validation, and the local Runtime
governor all apply unchanged. No business logic is duplicated here — this module
is a thin MCP <-> REST translation layer, mirroring the cloud ``mcp-service``
architecture but self-contained and single-process.
"""
from __future__ import annotations

import json
from collections import OrderedDict
import logging
import os
import re
import time
import unicodedata
import uuid
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from security.dependencies import get_auth_context, AuthContext

logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2025-03-26"
# Registration slug used in client configs (`claude mcp add <slug>`). Deliberately
# NOT "writ" so it can coexist with the official Writ desktop app, which registers
# under "writ" — a user may have BOTH connected to the same AI agent.
SERVER_NAME = "writ-selfhost"
# Human-facing name shown by MCP clients — the differentiator an agent reads.
SERVER_TITLE = "Writ Self-Host Coordinator"
SERVER_VERSION = "1.0.0"

# Terminal run statuses (RunItem.status normalization, routers/runs.py).
_TERMINAL = {"success", "failed", "cancelled", "skipped"}

# ── JSON-RPC helpers (MCP is JSON-RPC 2.0) ───────────────────────────────────

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _ok(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, message: str, data: Any = None) -> dict:
    e = {"code": code, "message": message}
    if data is not None:
        e["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": e}


def _content(obj: Any, is_error: bool = False) -> dict:
    """MCP tools/call result: a single text block carrying JSON (or plain text)."""
    if isinstance(obj, (dict, list)):
        text = json.dumps(obj, indent=2, default=str)
    else:
        text = str(obj)
    out: dict = {"content": [{"type": "text", "text": text}]}
    if is_error:
        out["isError"] = True
    return out


# ── Loopback plumbing ────────────────────────────────────────────────────────

def _loopback_base() -> str:
    """Base URL for calling THIS coordinator's REST API from inside a request.

    Prefer an explicit override, else localhost on the bound port. Never the
    public URL (avoids the local CA / hostname round-trip for an in-process hop).
    """
    b = os.getenv("MCP_LOOPBACK_BASE")
    if b:
        return b.rstrip("/")
    port = os.getenv("PORT") or os.getenv("WRIT_PORT") or "8000"
    return f"http://127.0.0.1:{port}"


_client: Optional[httpx.AsyncClient] = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=_loopback_base(),
            timeout=httpx.Timeout(30.0),
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=50),
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        finally:
            _client = None


class _Upstream(Exception):
    """A loopback REST call returned a non-2xx — carries a caller-safe message."""

    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(detail)


async def _call(method: str, path: str, token: str, *, params=None, json_body=None) -> Any:
    """Call the coordinator's own REST endpoint, forwarding the caller's bearer.

    Scope checks, validation, and metering happen in the target endpoint exactly
    as for any external caller — this hop adds no authority. Raises ``_Upstream``
    on a non-2xx so the tool layer can surface a clean error.
    """
    headers = {"Authorization": token} if token else {}
    resp = await _http().request(method, path, headers=headers, params=params, json=json_body)
    if resp.status_code >= 400:
        # Prefer the endpoint's own `detail`, but keep it terse and non-leaky.
        detail = resp.text[:300]
        try:
            j = resp.json()
            if isinstance(j, dict) and j.get("detail"):
                detail = str(j["detail"])[:300]
        except Exception:
            pass
        raise _Upstream(resp.status_code, detail)
    if resp.headers.get("content-type", "").startswith("application/json"):
        return resp.json()
    return resp.text


# ── Workflow resolution + derived tools ──────────────────────────────────────

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _slug(text: str, max_len: int = 48) -> str:
    s = (
        unicodedata.normalize("NFKD", text or "")
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    s = _NON_ALNUM.sub("_", s).strip("_")
    return s[:max_len].strip("_") or "workflow"


async def _list_workflows(token: str) -> list[dict]:
    """The owner's saved automation workflows (summary rows)."""
    data = await _call("GET", "/api/automation/workflows", token)
    return data if isinstance(data, list) else []


async def _resolve_workflow(token: str, args: dict) -> dict:
    """Resolve a workflow from a tool call by id or (fuzzy) name. Raises on miss."""
    wid = args.get("workflow_id") or args.get("id")
    name = args.get("workflow") or args.get("name")
    rows = await _list_workflows(token)
    if wid is not None:
        try:
            wid = int(wid)
        except (TypeError, ValueError):
            wid = None
        for w in rows:
            if w.get("id") == wid:
                return w
    if name:
        nl = str(name).strip().lower()
        for w in rows:  # exact first
            if (w.get("name") or "").strip().lower() == nl:
                return w
        for w in rows:  # then contains
            if nl in (w.get("name") or "").strip().lower():
                return w
    raise _Upstream(404, f"No saved workflow matches {name or wid!r}. Use writ_list_workflows.")


def _derived_run_tools(rows: list[dict]) -> list[dict]:
    """One ``run_<workflow>`` tool per saved workflow (mirrors the desktop daemon).

    Lets an agent call a named tool instead of passing a workflow id to the
    generic runner. Input schema is derived from the workflow's declared inputs.
    Names are de-duped; the static ``writ_*`` names always win.
    """
    used = {t["name"] for t in _STATIC_TOOLS}
    tools: list[dict] = []
    for w in rows:
        if not w.get("id"):
            continue
        fallback = w.get("name") or ("workflow_%s" % w["id"])
        base = "run_" + _slug(fallback)
        name = base
        n = 2
        while name in used:
            name = f"{base}_{n}"
            n += 1
        used.add(name)
        # Declared inputs: form_data keys + extracted placeholders when present.
        props: dict = {}
        for key in list((w.get("form_data") or {}).keys()):
            props[str(key)] = {"type": "string", "description": f"Input: {key}"}
        for ph in (w.get("placeholders") or []):
            k = ph if isinstance(ph, str) else (ph.get("name") if isinstance(ph, dict) else None)
            if k and k not in props:
                props[str(k)] = {"type": "string", "description": f"Input: {k}"}
        # Advertise the delivery/freshness controls alongside the workflow's own inputs.
        # A caller-defined input of the same name always wins — shadowing it would
        # silently change what the workflow receives.
        for ctl, spec in RUN_CONTROL_PROPERTIES.items():
            props.setdefault(ctl, spec)
        tools.append({
            "name": name,
            "description": (w.get("description") or f"Run the saved workflow “{w.get('name')}” and return its extracted data.")[:400],
            "inputSchema": {"type": "object", "properties": props},
            "_workflow_id": w["id"],
        })
    return tools


# ── Tool handlers ────────────────────────────────────────────────────────────

def _inputs_from_args(args: dict) -> dict:
    """Everything that isn't a control key is treated as a run input."""
    reserved = {"workflow", "workflow_id", "id", "name", "wait", "timeout_seconds", FRESHNESS_ARG}
    if isinstance(args.get("inputs"), dict):
        return dict(args["inputs"])
    return {k: v for k, v in args.items() if k not in reserved}


# ── Result reuse (the `max_age` tool argument) ───────────────────────────────
# Running a workflow drives a real browser, so an agent that asks the same question
# twice in one session pays twice and waits twice. `max_age` lets the CALLER say a
# recent answer is acceptable. Opt-in: with no `max_age` (or 0) every call runs fresh,
# so no existing agent silently starts getting stale data.
#
# In-process and bounded rather than shared storage: this is a latency/cost
# optimisation on an explicitly-approximate request, so a miss after a restart just
# means the workflow runs — exactly what would have happened before. Nothing depends
# on it for correctness.
FRESHNESS_ARG = "max_age"
_RESULT_CACHE: "OrderedDict[tuple, tuple[float, dict]]" = OrderedDict()
_RESULT_CACHE_MAX = 256

#: The delivery/freshness controls every run tool accepts, advertised so an agent can
#: actually DISCOVER them. `wait`/`timeout_seconds` were honoured but never described,
#: so no client had any way to know they existed.
RUN_CONTROL_PROPERTIES = {
    "wait": {
        "type": "boolean",
        "description": "Wait for completion and return the data (default true).",
    },
    "timeout_seconds": {
        "type": "integer",
        "description": "Max seconds to wait for completion (default 120).",
    },
    FRESHNESS_ARG: {
        "type": "integer",
        "minimum": 0,
        "description": (
            "Optional. Reuse a previous result if it is younger than this many seconds, "
            "instead of running the workflow again. 0 (the default) always runs fresh. "
            "Use it when a recent answer is good enough — much faster and cheaper."
        ),
    },
}


def _requested_max_age(args: dict) -> int:
    """The caller's freshness ceiling in seconds. 0 (the default) means always run."""
    try:
        return max(0, int((args or {}).get(FRESHNESS_ARG)))
    except (TypeError, ValueError):
        # Malformed degrades to "run it fresh" rather than failing the call: freshness
        # is a hint, and refusing to answer would be the worse outcome.
        return 0


def _freshness_key(wf_id: int, inputs: dict) -> tuple:
    return (wf_id, json.dumps(inputs or {}, sort_keys=True, default=str))


def _cached_run(key: tuple, max_age: int) -> Optional[dict]:
    entry = _RESULT_CACHE.get(key)
    if not entry:
        return None
    stored_at, result = entry
    age = time.time() - stored_at
    if age > max_age:
        return None
    _RESULT_CACHE.move_to_end(key)
    out = dict(result)
    # Tell the agent what it got — it cannot reason about how current the data is
    # unless it can distinguish a reused answer from a fresh one.
    out["_cache"] = {"hit": True, "age_seconds": int(age)}
    return out


def _store_run(key: tuple, result: dict) -> None:
    # Only a SUCCESSFUL run is reusable; serving a stored failure back as if it were an
    # answer would be worse than re-running.
    if not isinstance(result, dict) or result.get("status") != "success":
        return
    _RESULT_CACHE[key] = (time.time(), result)
    _RESULT_CACHE.move_to_end(key)
    while len(_RESULT_CACHE) > _RESULT_CACHE_MAX:
        _RESULT_CACHE.popitem(last=False)


async def _run_workflow_id(token: str, wf: dict, inputs: dict, wait: bool, timeout_s: int) -> dict:
    wid = wf["id"]
    dispatch_ts = time.time()
    disp = await _call(
        "POST", f"/api/automation/workflows/{wid}/run", token,
        json_body={"form_data": inputs},
    )
    task_id = (disp or {}).get("task_id") if isinstance(disp, dict) else None
    if not wait:
        return {"workflow_id": wid, "name": wf.get("name"), "task_id": task_id,
                "status": "dispatched",
                "note": "Read results later with writ_workflow_data."}

    # Poll the unified runs feed for THIS workflow's newest run since dispatch.
    deadline = dispatch_ts + max(5, min(timeout_s, 600))
    run_row_id: Optional[int] = None
    status = "running"
    error = None
    while time.time() < deadline:
        feed = await _call("GET", "/api/runs", token,
                           params={"run_type": "workflow", "entity_id": wid, "limit": 5})
        newest = None
        for r in (feed or []):
            st = (r.get("started_at") or "")
            # RunItem.id is "workflow-<row id>"; keep the newest run at/after dispatch.
            if newest is None or st > (newest.get("started_at") or ""):
                newest = r
        if newest:
            try:
                run_row_id = int(str(newest["id"]).split("-")[-1])
            except (ValueError, KeyError):
                run_row_id = None
            status = newest.get("status") or "running"
            error = newest.get("error")
            if status in _TERMINAL:
                break
        await _sleep(2.0)

    result: dict = {"workflow_id": wid, "name": wf.get("name"), "task_id": task_id, "status": status}
    if error:
        result["error"] = error
    if status == "success" and run_row_id is not None:
        try:
            data = await _call("GET", f"/api/automation/workflows/{wid}/data", token,
                               params={"run_id": run_row_id, "view": "run", "limit": 200})
            result["columns"] = (data or {}).get("columns")
            result["rows"] = (data or {}).get("rows")
        except _Upstream:
            pass
    if status not in _TERMINAL:
        result["retryable"] = True
        result["note"] = (
            "Still running — it was NOT cancelled, and running this tool again would "
            "start a SECOND run. Poll writ_workflow_runs / writ_workflow_data instead, "
            f"or re-call with max_age={max(timeout_s * 2, 300)} to pick up this run's "
            "result once it lands."
        )
    return result


async def _sleep(sec: float) -> None:
    import asyncio
    await asyncio.sleep(sec)


async def _tool_list_workflows(token: str, args: dict) -> dict:
    rows = await _list_workflows(token)
    q = (args.get("search") or "").strip().lower()
    out = []
    for w in rows:
        if q and q not in (w.get("name") or "").lower() and q not in (w.get("description") or "").lower():
            continue
        out.append({
            "id": w.get("id"),
            "name": w.get("name"),
            "description": w.get("description"),
            "inputs": list((w.get("form_data") or {}).keys()),
            "schedule_enabled": w.get("schedule_enabled"),
            "schedule_kind": w.get("schedule_kind"),
        })
    return _content({"workflows": out, "total": len(out)})


async def _tool_run_workflow(token: str, args: dict) -> dict:
    wf = await _resolve_workflow(token, args)
    wait = args.get("wait", True) is not False
    timeout_s = int(args.get("timeout_seconds") or 120)
    inputs = _inputs_from_args(args)

    # FRESHNESS first: a reusable answer means no dispatch at all.
    max_age = _requested_max_age(args)
    key = _freshness_key(wf["id"], inputs)
    if max_age > 0:
        hit = _cached_run(key, max_age)
        if hit is not None:
            return _content(hit)

    res = await _run_workflow_id(token, wf, inputs, wait, timeout_s)
    _store_run(key, res)
    return _content(res)


async def _tool_workflow_data(token: str, args: dict) -> dict:
    wf = await _resolve_workflow(token, args)
    params = {"limit": int(args.get("limit") or 50)}
    if args.get("q"):
        params["q"] = args["q"]
    if args.get("run_id"):
        params["run_id"] = args["run_id"]
        params["view"] = args.get("view") or "run"
    elif args.get("view"):
        params["view"] = args["view"]
    data = await _call("GET", f"/api/automation/workflows/{wf['id']}/data", token, params=params)
    return _content(data)


async def _tool_export_data(token: str, args: dict) -> dict:
    wf = await _resolve_workflow(token, args)
    fmt = (args.get("format") or "csv").lower()
    params = {"format": "json" if fmt == "json" else "csv"}
    if args.get("q"):
        params["q"] = args["q"]
    if args.get("view"):
        params["view"] = args["view"]
    data = await _call("GET", f"/api/automation/workflows/{wf['id']}/data/export", token, params=params)
    return _content(data)


async def _tool_search_data(token: str, args: dict) -> dict:
    q = (args.get("q") or args.get("query") or "").strip()
    if not q:
        raise _Upstream(400, "writ_search_data requires a `q` search string.")
    limit = int(args.get("limit") or 25)
    # Scope: an explicit workflow, else fan out over data-producing workflows.
    if args.get("workflow") or args.get("workflow_id") or args.get("id"):
        targets = [await _resolve_workflow(token, args)]
    else:
        picker = await _call("GET", "/api/automation/data/workflows", token)
        targets = (picker or {}).get("workflows", [])[:8]
    hits = []
    for w in targets:
        # /api/automation/data/workflows returns workflow_id/workflow_name, while
        # _resolve_workflow (the explicitly-scoped branch above) returns id/name.
        # Reading only "id" made every fanned-out workflow fall through the skip
        # below, so an unscoped search — the default, and what an agent uses for
        # "search my data" — always reported zero matches even when the very same
        # query scoped to one workflow returned rows.
        wid = w.get("workflow_id") or w.get("id")
        if not wid:
            continue
        try:
            data = await _call("GET", f"/api/automation/workflows/{wid}/data", token,
                               params={"q": q, "limit": limit, "view": "latest"})
        except _Upstream:
            continue
        rows = (data or {}).get("rows") or []
        if rows:
            hits.append({"workflow_id": wid,
                         "name": w.get("workflow_name") or w.get("name"),
                         "columns": (data or {}).get("columns"), "rows": rows[:limit]})
    return _content({"query": q, "matches": hits})


async def _tool_workflow_runs(token: str, args: dict) -> dict:
    params = {"run_type": "workflow", "limit": int(args.get("limit") or 25)}
    if args.get("workflow") or args.get("workflow_id") or args.get("id"):
        wf = await _resolve_workflow(token, args)
        params["entity_id"] = wf["id"]
    if args.get("status"):
        params["status"] = args["status"]
    data = await _call("GET", "/api/runs", token, params=params)
    return _content({"runs": data})


async def _tool_set_schedule(token: str, args: dict) -> dict:
    wf = await _resolve_workflow(token, args)
    body: dict = {"schedule_enabled": args.get("enabled", True) is not False}
    kind = args.get("kind") or args.get("schedule_kind")
    if kind:
        body["schedule_kind"] = kind
    if args.get("interval_ms") or args.get("schedule_interval_ms"):
        body["schedule_interval_ms"] = int(args.get("interval_ms") or args.get("schedule_interval_ms"))
    if args.get("every_minutes"):
        body["schedule_kind"] = "interval"
        body["schedule_interval_ms"] = int(args["every_minutes"]) * 60_000
    if args.get("time") or args.get("schedule_time"):
        body["schedule_time"] = args.get("time") or args.get("schedule_time")
    if args.get("days") is not None:
        body["schedule_days"] = args["days"]
    if args.get("tz") or args.get("schedule_tz"):
        body["schedule_tz"] = args.get("tz") or args.get("schedule_tz")
    updated = await _call("PUT", f"/api/automation/workflows/{wf['id']}", token, json_body=body)
    return _content({
        "workflow_id": wf["id"], "name": wf.get("name"),
        "schedule_enabled": (updated or {}).get("schedule_enabled"),
        "schedule_kind": (updated or {}).get("schedule_kind"),
        "schedule_time": (updated or {}).get("schedule_time"),
        "schedule_interval_ms": (updated or {}).get("schedule_interval_ms"),
    })


async def _tool_expose_workflow_api(token: str, args: dict) -> dict:
    wf = await _resolve_workflow(token, args)
    body = {
        "name": args.get("label") or f"MCP: {wf.get('name')}",
        "workflow_id": wf["id"],
        "action": "run_workflow",
        "enabled": True,
        "wait_for_result": args.get("wait_for_result", True) is not False,
        "wait_timeout": int(args.get("wait_timeout") or 120),
    }
    trig = await _call("POST", "/api/webhooks/triggers", token, json_body=body)
    token_val = (trig or {}).get("token")
    path = (trig or {}).get("webhook_path") or (f"/api/webhooks/hook/{token_val}" if token_val else None)
    public = _public_url()
    url = f"{public}{path}" if (public and path) else path
    return _content({
        "workflow_id": wf["id"], "name": wf.get("name"),
        "rest_endpoint": url,
        "method": "POST",
        "call_hint": (
            f"POST {url} with a JSON body of the workflow's inputs; "
            "the response returns the extracted data (wait_for_result=true)."
        ),
        "trigger_id": (trig or {}).get("id"),
    })


# Crawl settings a saved definition accepts. One list, so the ad-hoc and save-as
# paths cannot drift into accepting different things.
_CRAWL_CONFIG_KEYS = (
    "name", "extract_mode", "extract_schema", "include_paths", "exclude_paths",
    "max_depth", "page_budget", "same_domain", "allow_subdomains", "respect_robots",
    "render_mode", "ocr_mode", "intent", "seed_urls", "relevance_threshold",
    "content_spec", "persona_id", "shard_size", "delay_ms", "max_concurrent_shards",
)


def _crawl_config_from_args(args: dict) -> dict:
    body = {"url": args.get("url")}
    for k in _CRAWL_CONFIG_KEYS:
        if args.get(k) is not None:
            body[k] = args[k]
    return body


async def _tool_crawl_site(token: str, args: dict) -> dict:
    url = args.get("url")
    if not url:
        raise _Upstream(400, "writ_crawl_site requires a `url` (seed).")
    config = _crawl_config_from_args(args)

    save_as = (args.get("save_as") or "").strip()
    if not save_as:
        crawl = await _call("POST", "/api/crawl", token, json_body=config)
        return _content(crawl)

    # save_as ⇒ persist these settings as a callable saved crawl, then run it
    # through the definition so this same call also gets the freshness contract.
    # Reusing a save_as updates that definition instead of minting a near-duplicate
    # every time an agent repeats itself.
    existing = await _call("GET", "/api/crawl/definitions", token, params={"limit": 200})
    match = None
    for d in ((existing or {}).get("definitions") or []):
        if d.get("slug") == save_as or (d.get("name") or "") == save_as:
            match = d
            break
    if match:
        defn = await _call("PATCH", f"/api/crawl/definitions/{match['slug']}", token,
                           json_body={"config": config})
    else:
        defn = await _call("POST", "/api/crawl/definitions", token,
                           json_body={"name": save_as, "slug": save_as, "config": config})

    run_body = {"max_age": _requested_max_age(args)}
    if args.get("wait") is not None:
        run_body["wait"] = args["wait"] is not False
        run_body["timeout"] = int(args.get("timeout_seconds") or 120)
    result = await _call("POST", f"/api/crawl/definitions/{defn['slug']}/run", token,
                         json_body=run_body)
    return _content(result)


async def _tool_list_saved_crawls(token: str, args: dict) -> dict:
    res = await _call("GET", "/api/crawl/definitions", token,
                      params={"limit": int(args.get("limit") or 50)})
    out = []
    for d in ((res or {}).get("definitions") or []):
        out.append({
            "id": d.get("id"), "slug": d.get("slug"), "name": d.get("name"),
            "seed_url": d.get("seed_url"), "last_run_at": d.get("last_run_at"),
            "default_max_age_seconds": d.get("default_max_age_seconds"),
            "run_url": d.get("run_url"),
        })
    return _content({"saved_crawls": out, "total": len(out)})


async def _tool_run_saved_crawl(token: str, args: dict) -> dict:
    ref = args.get("crawl") or args.get("slug") or args.get("definition_id")
    if not ref:
        raise _Upstream(400, "writ_run_saved_crawl requires `crawl` (a saved crawl slug, name or id).")
    body = {
        "max_age": _requested_max_age(args),
        "wait": args.get("wait") is True,
        "timeout": int(args.get("timeout_seconds") or 120),
        "limit": int(args.get("limit") or 50),
    }
    res = await _call("POST", f"/api/crawl/definitions/{ref}/run", token, json_body=body)
    return _content(res)


async def _tool_saved_crawl_data(token: str, args: dict) -> dict:
    ref = args.get("crawl") or args.get("slug") or args.get("definition_id")
    if not ref:
        raise _Upstream(400, "writ_saved_crawl_data requires `crawl` (a saved crawl slug, name or id).")
    res = await _call("GET", f"/api/crawl/definitions/{ref}/data", token,
                      params={"limit": int(args.get("limit") or 50)})
    return _content(res)


async def _tool_crawl_status(token: str, args: dict) -> dict:
    cid = args.get("crawl_id") or args.get("id")
    if not cid:
        raise _Upstream(400, "writ_crawl_status requires a `crawl_id`.")
    crawl = await _call("GET", f"/api/crawl/{int(cid)}", token)
    return _content(crawl)


_AUTOMATION_EVENTS = {
    "workflow_completed", "workflow_started",
    "ai_session_completed", "ai_session_started",
    "change_detected", "webhook_received",
}


async def _tool_create_automation(token: str, args: dict) -> dict:
    """Create a trigger-rule automation: on an EVENT, run a workflow and/or notify."""
    name = (args.get("name") or "").strip()
    if not name:
        raise _Upstream(400, "writ_create_automation requires a `name`.")
    event = args.get("when") or args.get("event") or "workflow_completed"
    if event not in _AUTOMATION_EVENTS:
        raise _Upstream(400, f"`when` must be one of {sorted(_AUTOMATION_EVENTS)}.")

    body: dict = {
        "name": name,
        "event_type": event,
        "enabled": args.get("enabled", True) is not False,
    }
    if args.get("description"):
        body["description"] = args["description"]
    if args.get("priority") is not None:
        body["priority"] = int(args["priority"])
    if isinstance(args.get("conditions"), dict):
        body["conditions"] = args["conditions"]

    # Scope a workflow_* event to a specific source workflow (recommended — an
    # unscoped workflow_completed fires for EVERY workflow).
    if args.get("on_workflow") or args.get("on_workflow_id"):
        src = await _resolve_workflow(token, {
            "workflow": args.get("on_workflow"), "workflow_id": args.get("on_workflow_id")})
        body["workflow_id"] = src["id"]
    elif event in ("workflow_completed", "workflow_started"):
        raise _Upstream(400, "For a workflow_* event, name the source workflow via `on_workflow`.")
    # Advanced event refs (webhook/change) pass through when supplied.
    for k in ("webhook_trigger_id", "target_selector_id", "target_id", "ai_session_id"):
        if args.get(k) is not None:
            body[k] = args[k]

    actions: list = []
    if args.get("run_workflow") or args.get("run_workflow_id"):
        tgt = await _resolve_workflow(token, {
            "workflow": args.get("run_workflow"), "workflow_id": args.get("run_workflow_id")})
        actions.append({"type": "workflow", "config": {"workflow_id": tgt["id"]}})
    if args.get("notify"):
        # The trigger runtime renders `template` and dispatches to `channels` (a
        # non-empty channels list is required, else delivery is skipped) — see
        # services.unified_trigger_service._dispatch_notification.
        msg = args["notify"] if isinstance(args["notify"], str) else (args.get("message") or "")
        cfg = {"template": msg, "title": args.get("title") or "Writ automation"}
        if isinstance(args.get("channels"), list):
            cfg["channels"] = args["channels"]
        if isinstance(args.get("recipients"), list):
            cfg["recipients"] = args["recipients"]
        actions.append({"type": "notification", "config": cfg})
    if isinstance(args.get("actions"), list):
        actions.extend(a for a in args["actions"] if isinstance(a, dict))
    if not actions:
        raise _Upstream(400, "Give the automation something to do: `run_workflow`, `notify`, or a raw `actions` list.")
    body["actions"] = actions

    created = await _call("POST", "/api/triggers", token, json_body=body)
    return _content({
        "automation_id": (created or {}).get("id"),
        "name": (created or {}).get("name") or name,
        "event": (created or {}).get("event_type") or event,
        "on_workflow_id": (created or {}).get("workflow_id"),
        "actions": [a.get("type") for a in (created or {}).get("actions", actions)],
        "enabled": (created or {}).get("enabled", True),
    })


async def _tool_create_monitor(token: str, args: dict) -> dict:
    """Create a monitoring TARGET (a 'monitor') that Writ checks on a schedule.

    A monitor watches a URL — optionally a specific CSS selector's text — and
    fires a `change_detected` event when it changes. Wire that event to an action
    with writ_wire_monitor. Backed by POST /api/targets (plan limits + the
    per-plan minimum interval are enforced there).
    """
    url = (args.get("url") or "").strip()
    if not url:
        raise _Upstream(400, "writ_create_monitor requires a `url`.")
    selector = (args.get("selector") or "").strip() or None
    body: dict = {
        "url": url,
        "check_type": "content" if selector else "uptime",
        "enabled": args.get("enabled", True) is not False,
        "requires_playwright": bool(args.get("requires_browser")),
    }
    if selector:
        body["selector"] = selector
    if args.get("interval_minutes") is not None:
        try:
            minutes = int(args["interval_minutes"])
        except (TypeError, ValueError):
            raise _Upstream(400, "`interval_minutes` must be an integer.")
        if minutes < 1:
            raise _Upstream(400, "`interval_minutes` must be >= 1.")
        body["check_period_ms"] = minutes * 60_000
    created = await _call("POST", "/api/targets", token, json_body=body)
    return _content({
        "monitor_id": (created or {}).get("id"),
        "url": (created or {}).get("url") or url,
        "check_type": body["check_type"],
        "selector": selector,
        "interval_ms": (created or {}).get("checkPeriodMs") or (created or {}).get("check_period_ms") or body.get("check_period_ms"),
        "requires_browser": body["requires_playwright"],
        "enabled": (created or {}).get("enabled", body["enabled"]),
        "next": "Call writ_wire_monitor with this monitor_id to choose what happens on a detected change (run a saved workflow, or notify).",
    })


async def _tool_wire_monitor(token: str, args: dict) -> dict:
    """Wire a monitor's `change_detected` event to an action via the trigger engine.

    action='workflow' runs a saved workflow when the monitored page changes;
    action='notify' sends a notification (needs `channels` + `recipients` the
    account has configured, e.g. channels=["pushover"], recipients=["pushover:1"]).
    Backed by POST /api/triggers (event_type=change_detected, scoped to target_id).
    """
    monitor_id = args.get("monitor_id") if args.get("monitor_id") is not None else args.get("target_id")
    if monitor_id is None:
        raise _Upstream(400, "writ_wire_monitor requires a `monitor_id` (from writ_create_monitor).")
    try:
        monitor_id = int(monitor_id)
    except (TypeError, ValueError):
        raise _Upstream(400, "`monitor_id` must be an integer.")
    action = (args.get("action") or "").strip().lower()
    name = (args.get("name") or "").strip() or "Monitor change automation"
    event_block = {
        "id": "evt", "type": "event", "blockType": "change_detected",
        "parentId": None, "config": {"target_id": monitor_id},
    }
    note = None
    if action == "workflow":
        wf = await _resolve_workflow(token, {
            "workflow": args.get("workflow"), "workflow_id": args.get("workflow_id")})
        act = {"type": "workflow", "config": {"workflow_id": wf["id"]}}
        action_block = {"id": "act", "type": "action", "blockType": "workflow",
                        "parentId": "evt", "config": {"workflow_id": wf["id"]}}
    elif action == "notify":
        channels = args.get("channels") if isinstance(args.get("channels"), list) else []
        recipients = args.get("recipients") if isinstance(args.get("recipients"), list) else []
        cfg = {
            "channels": channels,
            "recipients": recipients,
            "title": args.get("title") or "Writ detected a change",
            "template": args.get("message") or "The monitored page changed: {{event.url}}",
        }
        act = {"type": "notification", "config": cfg}
        action_block = {"id": "act", "type": "action", "blockType": "notification",
                        "parentId": "evt", "config": cfg}
        if not channels:
            note = ("No notification channels were set — the automation is created but will not "
                    "deliver until channels + recipients are configured (pass `channels` and "
                    "`recipients`, e.g. channels=[\"pushover\"], recipients=[\"pushover:1\"], or set "
                    "them on the automation in the Writ app).")
    else:
        raise _Upstream(400, "`action` must be 'workflow' or 'notify'.")
    body = {
        "name": name,
        "event_type": "change_detected",
        "target_id": monitor_id,
        "enabled": args.get("enabled", True) is not False,
        "actions": [act],
        "blocks": [event_block, action_block],
    }
    created = await _call("POST", "/api/triggers", token, json_body=body)
    return _content({
        "automation_id": (created or {}).get("id"),
        "monitor_id": monitor_id,
        "action": action,
        "event": "change_detected",
        "enabled": (created or {}).get("enabled", True),
        "note": note,
    })


# ── Un-guided record-build tools (client is the brain) ───────────────────────
# writ_record_* open a live browser recording session on a fleet agent and let
# the connected client drive it freely; structured interactions are recorded as
# workflow steps, then saved as a runnable workflow. No coordinator guidance.

async def _tool_record_start(token: str, args: dict) -> dict:
    from services import mcp_record
    try:
        return _content(await mcp_record.start(args.get("url"), args.get("agent_id")))
    except mcp_record.RecordError as e:
        return _content(f"Error: {e}", is_error=True)


async def _tool_record_act(token: str, args: dict) -> dict:
    from services import mcp_record
    try:
        return _content(await mcp_record.act(args.get("session_id"), args.get("actions")))
    except mcp_record.RecordError as e:
        return _content(f"Error: {e}", is_error=True)


async def _tool_record_context(token: str, args: dict) -> dict:
    from services import mcp_record
    try:
        return _content(await mcp_record.context(args.get("session_id")))
    except mcp_record.RecordError as e:
        return _content(f"Error: {e}", is_error=True)


async def _tool_record_network(token: str, args: dict) -> dict:
    from services import mcp_record
    try:
        return _content(await mcp_record.network(args.get("session_id")))
    except mcp_record.RecordError as e:
        return _content(f"Error: {e}", is_error=True)


async def _tool_record_save(token: str, args: dict) -> dict:
    from services import mcp_record
    sid = args.get("session_id")
    name = (args.get("name") or "").strip()
    if not name:
        return _content("Error: writ_record_save requires a `name`.", is_error=True)
    try:
        payload = await mcp_record.finalize(sid)
    except mcp_record.RecordError as e:
        return _content(f"Error: {e}", is_error=True)
    body = {
        "name": name,
        "workflow_type": "recorded",
        "entry_url": payload["entry_url"],
        "steps": payload["steps"],
    }
    try:
        created = await _call("POST", "/api/automation/workflows", token, json_body=body)
    except _Upstream as ue:
        return _content(f"Error saving workflow: {ue.detail}", is_error=True)
    await mcp_record.cancel(sid)  # close the session now that it's persisted
    return _content({
        "workflow_id": (created or {}).get("id"),
        "name": (created or {}).get("name") or name,
        "steps": len(payload["steps"]),
        "note": "Saved. Run it any time with writ_run_workflow or its run_<name> tool.",
    })


async def _tool_record_cancel(token: str, args: dict) -> dict:
    from services import mcp_record
    try:
        return _content(await mcp_record.cancel(args.get("session_id")))
    except mcp_record.RecordError as e:
        return _content(f"Error: {e}", is_error=True)


# ── Browser-use front door (Writ IS your browser) ────────────────────────────
# writ_browser_use opens the SAME un-guided fleet browser session as the record
# tools, but framed as "do a task": complete the user's request in the browser,
# ask them when needed, and save a clean workflow ONLY on demand. It shares the
# writ_browser_* act/context/network/save/cancel loop (parity with the desktop
# daemon), so every AI browsing need routes through Writ on this surface too.

async def _tool_browser_use(token: str, args: dict) -> dict:
    from services import mcp_record
    url = (args.get("url") or "").strip() or "about:blank"
    try:
        return _content(await mcp_record.start(url, args.get("agent_id"), mode="use"))
    except mcp_record.RecordError as e:
        return _content(f"Error: {e}", is_error=True)


# ── Static tool catalog ──────────────────────────────────────────────────────

_STATIC_TOOLS: list[dict] = [
    {
        "name": "writ_list_workflows",
        "description": "List the workflows you have already saved on this self-hosted Writ coordinator — each runs at zero AI cost. Returns id, name, declared inputs, and schedule.",
        "inputSchema": {"type": "object", "properties": {
            "search": {"type": "string", "description": "Optional name/description filter."}}},
        "_handler": _tool_list_workflows,
    },
    {
        "name": "writ_run_workflow",
        "description": "Run a saved workflow by id or name and (by default) wait for it to finish, returning the extracted data. Pass workflow inputs as top-level fields or under `inputs`.",
        "inputSchema": {"type": "object", "properties": {
            "workflow": {"type": "string", "description": "Workflow name (or use workflow_id)."},
            "workflow_id": {"type": "integer"},
            "inputs": {"type": "object", "description": "Run inputs (or pass them as top-level fields)."},
            **RUN_CONTROL_PROPERTIES}},
        "_handler": _tool_run_workflow,
    },
    {
        "name": "writ_workflow_data",
        "description": "Read the accumulated extracted data for a saved workflow as a table (columns + rows). Filter with `q`, or inspect one run with `run_id`.",
        "inputSchema": {"type": "object", "properties": {
            "workflow": {"type": "string"}, "workflow_id": {"type": "integer"},
            "q": {"type": "string", "description": "Substring filter across fields."},
            "run_id": {"type": "integer"}, "limit": {"type": "integer"},
            "view": {"type": "string", "description": "all | latest | run"}}},
        "_handler": _tool_workflow_data,
    },
    {
        "name": "writ_search_data",
        "description": "Search across everything already collected by your workflows for a term — answers data questions from past runs without running anything. Scopes to one workflow when given, else fans out.",
        "inputSchema": {"type": "object", "properties": {
            "q": {"type": "string", "description": "Search term (required)."},
            "workflow": {"type": "string"}, "workflow_id": {"type": "integer"},
            "limit": {"type": "integer"}}, "required": ["q"]},
        "_handler": _tool_search_data,
    },
    {
        "name": "writ_export_data",
        "description": "Export a workflow's full extracted-data table as CSV or JSON (search/filter applied, un-paginated).",
        "inputSchema": {"type": "object", "properties": {
            "workflow": {"type": "string"}, "workflow_id": {"type": "integer"},
            "format": {"type": "string", "description": "csv (default) or json"},
            "q": {"type": "string"}, "view": {"type": "string"}}},
        "_handler": _tool_export_data,
    },
    {
        "name": "writ_workflow_runs",
        "description": "Inspect run history — status, timing, errors — for one workflow or across all of them.",
        "inputSchema": {"type": "object", "properties": {
            "workflow": {"type": "string"}, "workflow_id": {"type": "integer"},
            "status": {"type": "string", "description": "pending|running|success|failed|cancelled|skipped"},
            "limit": {"type": "integer"}}},
        "_handler": _tool_workflow_runs,
    },
    {
        "name": "writ_set_schedule",
        "description": "Schedule a saved workflow to run automatically. Use `every_minutes` for an interval, or `kind`='daily'/'weekly' with `time` (HH:MM) and `days`.",
        "inputSchema": {"type": "object", "properties": {
            "workflow": {"type": "string"}, "workflow_id": {"type": "integer"},
            "enabled": {"type": "boolean", "description": "Turn the schedule on/off (default on)."},
            "every_minutes": {"type": "integer", "description": "Interval schedule: minutes between runs."},
            "kind": {"type": "string", "description": "interval | daily | weekly"},
            "time": {"type": "string", "description": "HH:MM local time for daily/weekly."},
            "days": {"type": "array", "items": {"type": "integer"}, "description": "0=Mon..6=Sun for weekly."},
            "tz": {"type": "string", "description": "IANA timezone for daily/weekly."}}},
        "_handler": _tool_set_schedule,
    },
    {
        "name": "writ_expose_workflow_api",
        "description": "Expose a saved workflow as a callable REST endpoint (a webhook that runs it and returns the extracted data). Returns the URL to call.",
        "inputSchema": {"type": "object", "properties": {
            "workflow": {"type": "string"}, "workflow_id": {"type": "integer"},
            "label": {"type": "string", "description": "Optional name for the endpoint."},
            "wait_for_result": {"type": "boolean", "description": "Block until the run finishes and return data (default true)."}}},
        "_handler": _tool_expose_workflow_api,
    },
    {
        "name": "writ_crawl_site",
        "description": (
            "Start a Dragnet distributed crawl of a website across your self-hosted agent fleet. "
            "Returns a crawl id; results land as a workflow dataset. No AI required. Pass `save_as` "
            "to also SAVE these settings as a reusable, callable crawl — then later runs can reuse "
            "recent data via `max_age` instead of crawling the site again."
        ),
        "inputSchema": {"type": "object", "properties": {
            "url": {"type": "string", "description": "Seed URL (required)."},
            "name": {"type": "string"},
            "extract_mode": {"type": "string", "description": "markdown (default) or schema"},
            "extract_schema": {"type": "object"},
            "max_depth": {"type": "integer"}, "page_budget": {"type": "integer"},
            "include_paths": {"type": "array", "items": {"type": "string"}},
            "exclude_paths": {"type": "array", "items": {"type": "string"}},
            "same_domain": {"type": "boolean"}, "allow_subdomains": {"type": "boolean"},
            "render_mode": {"type": "string", "description": "auto (default) | http | browser"},
            "ocr_mode": {"type": "string", "description": "auto (default) | off | force"},
            "intent": {"type": "string", "description": "Plain-English goal; scopes the crawl to matching pages."},
            "save_as": {"type": "string", "description": (
                "Save these settings under this name so the crawl becomes callable by API and "
                "re-runnable. Reusing the same name updates that saved crawl instead of "
                "creating a duplicate.")},
            FRESHNESS_ARG: {"type": "integer", "minimum": 0, "description": (
                "Only meaningful with `save_as`: if that saved crawl already completed within this "
                "many seconds, return its collected data instead of crawling again. 0 always crawls.")},
            "wait": {"type": "boolean", "description": (
                "With `save_as`: block until the crawl converges. A whole-site crawl usually "
                "outlives an HTTP call, so the default returns a crawl id to poll.")},
            "timeout_seconds": {"type": "integer", "description": "Max seconds to wait when wait=true."},
        }, "required": ["url"]},
        "_handler": _tool_crawl_site,
    },
    {
        "name": "writ_saved_crawls",
        "description": (
            "List saved, re-runnable crawls — each is callable by API and can return already-collected "
            "data. Check here BEFORE crawling a site again: a saved crawl with recent data answers "
            "instantly and costs nothing."
        ),
        "inputSchema": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "Max saved crawls to return (default 50)."}}},
        "_handler": _tool_list_saved_crawls,
    },
    {
        "name": "writ_run_saved_crawl",
        "description": (
            "Run a saved crawl with its stored settings. Pass `max_age` to get the data it already "
            "collected if that run is recent enough — the cheap path. Otherwise it re-crawls. The "
            "response carries `_cache.hit` and `_cache.age_seconds` so you can tell which happened."
        ),
        "inputSchema": {"type": "object", "properties": {
            "crawl": {"type": "string", "description": "Saved crawl slug, name, or id (from writ_saved_crawls)."},
            FRESHNESS_ARG: {"type": "integer", "minimum": 0, "description": (
                "Reuse the last completed crawl if it finished within this many seconds. "
                "0 (default) always re-crawls.")},
            "wait": {"type": "boolean", "description": "Block until the crawl converges (default false — a crawl is slow)."},
            "timeout_seconds": {"type": "integer", "description": "Max seconds to wait when wait=true."},
            "limit": {"type": "integer", "description": "Rows of collected data to include (default 50)."},
        }, "required": ["crawl"]},
        "_handler": _tool_run_saved_crawl,
    },
    {
        "name": "writ_saved_crawl_data",
        "description": (
            "Read the data a saved crawl already collected on its most recent completed run. Never "
            "starts a crawl — use this when you want what is already there, at any age."
        ),
        "inputSchema": {"type": "object", "properties": {
            "crawl": {"type": "string", "description": "Saved crawl slug, name, or id."},
            "limit": {"type": "integer", "description": "Rows to return (default 50)."},
        }, "required": ["crawl"]},
        "_handler": _tool_saved_crawl_data,
    },
    {
        "name": "writ_crawl_status",
        "description": "Poll a running or finished Dragnet crawl by its crawl id — page counts, status, and the dataset workflow id.",
        "inputSchema": {"type": "object", "properties": {
            "crawl_id": {"type": "integer", "description": "Crawl id from writ_crawl_site."}}, "required": ["crawl_id"]},
        "_handler": _tool_crawl_status,
    },
    {
        "name": "writ_create_automation",
        "description": (
            "Create an automation: on an EVENT, run a workflow and/or send a notification. "
            "Chain workflows (when workflow A completes → run workflow B), or alert on completion. "
            "Give a source workflow via `on_workflow` for workflow_* events, and at least one of `run_workflow` / `notify`."
        ),
        "inputSchema": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Name for the automation (required)."},
            "when": {"type": "string", "description": "Event: workflow_completed | workflow_started | ai_session_completed | ai_session_started | change_detected | webhook_received.",
                     "enum": ["workflow_completed", "workflow_started", "ai_session_completed", "ai_session_started", "change_detected", "webhook_received"]},
            "on_workflow": {"type": "string", "description": "Source workflow name whose event fires this (required for workflow_* events)."},
            "on_workflow_id": {"type": "integer"},
            "run_workflow": {"type": "string", "description": "Workflow to RUN when the event fires (by name)."},
            "run_workflow_id": {"type": "integer"},
            "notify": {"type": "string", "description": "Send a notification with this message."},
            "channels": {"type": "array", "items": {"type": "string"}, "description": "Notification channels for `notify`, e.g. [\"pushover\",\"email\"] (required for delivery)."},
            "recipients": {"type": "array", "items": {"type": "string"}, "description": "Notification recipients, e.g. [\"email:3\",\"pushover:1\"]."},
            "title": {"type": "string", "description": "Notification title (with `notify`)."},
            "enabled": {"type": "boolean"},
            "priority": {"type": "integer"},
            "description": {"type": "string"}}, "required": ["name"]},
        "_handler": _tool_create_automation,
    },
    {
        "name": "writ_create_monitor",
        "description": (
            "Create a MONITOR — a target Writ checks on a schedule and that fires a "
            "change_detected event when the page (or a specific CSS selector's text) changes. "
            "Use when the user wants to WATCH a URL for changes/updates. Returns the monitor id "
            "for writ_wire_monitor. The interval is subject to the account's plan minimum."
        ),
        "inputSchema": {"type": "object", "properties": {
            "url": {"type": "string", "description": "URL to monitor (required)."},
            "selector": {"type": "string", "description": "CSS selector for content-change monitoring; omit for uptime/status monitoring."},
            "interval_minutes": {"type": "integer", "description": "How often to check, in minutes (clamped up to the plan minimum)."},
            "requires_browser": {"type": "boolean", "description": "Render with a real browser (JS) instead of plain HTTP."},
            "enabled": {"type": "boolean", "description": "Start the monitor enabled (default true)."}}, "required": ["url"]},
        "_handler": _tool_create_monitor,
    },
    {
        "name": "writ_wire_monitor",
        "description": (
            "Wire a monitor's change_detected event to an action. action='workflow' runs a saved "
            "workflow when the monitored page changes; action='notify' sends a notification "
            "(provide `channels` + `recipients` the account has configured). Use after "
            "writ_create_monitor to make the monitor DO something on change."
        ),
        "inputSchema": {"type": "object", "properties": {
            "monitor_id": {"type": "integer", "description": "Monitor id from writ_create_monitor."},
            "action": {"type": "string", "enum": ["workflow", "notify"], "description": "What to do on a detected change."},
            "workflow": {"type": "string", "description": "Workflow to run (name) — required for action='workflow'."},
            "workflow_id": {"type": "integer"},
            "channels": {"type": "array", "items": {"type": "string"}, "description": "Notification channels for action='notify', e.g. [\"pushover\",\"email\"]."},
            "recipients": {"type": "array", "items": {"type": "string"}, "description": "Notification recipients, e.g. [\"pushover:1\",\"email:3\"]."},
            "title": {"type": "string"}, "message": {"type": "string", "description": "Notification body template (supports {{event.url}})."},
            "name": {"type": "string", "description": "Optional automation name."},
            "enabled": {"type": "boolean"}}, "required": ["monitor_id", "action"]},
        "_handler": _tool_wire_monitor,
    },
    {
        "name": "writ_record_start",
        "description": (
            "BUILD a new workflow by recording. Opens a live browser on a connected fleet agent at `url` and starts recording. "
            "YOU drive it — decide each action yourself and ask the user for any clarification (which value to type, whether to log in). "
            "Returns a session_id and the page observation (URL, fields, buttons, page text). Then use writ_record_act, and finish with writ_record_save."
        ),
        "inputSchema": {"type": "object", "properties": {
            "url": {"type": "string", "description": "URL to open and record from (required)."},
            "agent_id": {"type": "string", "description": "Optional specific fleet agent; omit to auto-pick."}}, "required": ["url"]},
        "_handler": _tool_record_start,
    },
    {
        "name": "writ_record_act",
        "description": (
            "Perform one ordered batch of browser actions in a record session, then get the new page observation. "
            "INTERACTION actions are RECORDED as reusable workflow steps; INSPECTION/CONTROL actions are not. "
            "Target an element by a robust CSS `selector`, or by `field_index`/`button_index` from the observation.\n"
            "INTERACTION (recorded): "
            "navigate{url} · back · click{selector|button_index} · check{selector} · fill/type_text{selector|field_index,value} · "
            "select{selector|field_index,value} · press_key{key} · submit{selector?} · hover{selector|field_index} · "
            "scroll{direction,amount} · scroll_to_field{field_index}.\n"
            "INSPECT / CONTROL (not recorded): "
            "evaluate_js{script} · read_text/get_text{selector?} · extract_data{selector,fields?,limit?} · "
            "inspect_field{field_index} · get_screenshot · "
            "wait{seconds|ms} · wait_for{selector} · wait_for_change · list_tabs · switch_tab{index} · wait_for_tab · close_tab · solve_captcha.\n"
            "extract_data returns STRUCTURED rows when you pass `fields`: {selector:'.quote', "
            "fields:{text:'.text', author:'.author'}} yields one row per matching element. Without "
            "`fields` it returns the plain text of `selector`.\n"
            "Batch a few related actions, look at the returned observation + results, then decide the next batch. "
            "For logins that demand a one-time 2FA code, tell the user to complete that step in the Writ app "
            "(un-guided recording has no server-side code minting)."
        ),
        "inputSchema": {"type": "object", "properties": {
            "session_id": {"type": "string"},
            "actions": {
                "type": "array",
                "description": "Ordered action objects — see the tool description for the full vocabulary.",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": [
                            "navigate", "back", "click", "check", "fill", "type_text", "select",
                            "press_key", "submit", "hover", "scroll", "scroll_to_field",
                            "evaluate_js", "read_text", "get_text", "extract_data", "inspect_field",
                            "get_screenshot", "wait", "wait_for", "wait_for_change",
                            "list_tabs", "switch_tab", "wait_for_tab", "close_tab", "solve_captcha",
                        ]},
                        "selector": {"type": "string", "description": "CSS selector for the target element."},
                        "value": {"type": "string", "description": "Value for fill/select/type_text."},
                        "url": {"type": "string", "description": "URL for navigate."},
                        "key": {"type": "string", "description": "Key for press_key (e.g. Enter, Tab)."},
                        "script": {"type": "string", "description": "JS for evaluate_js (an expression or IIFE returning JSON)."},
                        "field_index": {"type": "integer", "description": "Index into observation.fields[]."},
                        "button_index": {"type": "integer", "description": "Index into observation.buttons[]."},
                        "direction": {"type": "string", "description": "scroll: up | down."},
                        "amount": {"type": "number", "description": "scroll distance in px."},
                        "seconds": {"type": "number", "description": "wait duration in seconds (max 10)."},
                        "ms": {"type": "number", "description": "wait duration in MILLISECONDS — alias for `seconds` (max 10000)."},
                        "fields": {
                            "type": "object",
                            "description": (
                                "extract_data: {field_name: css_sub_selector} evaluated INSIDE each "
                                "element matching `selector`, yielding one row per element. Omit for "
                                "the plain text of `selector`."
                            ),
                            "additionalProperties": {"type": "string"},
                        },
                        "limit": {"type": "integer", "description": "extract_data: max rows to return (default 100)."},
                        "index": {"type": "integer", "description": "Tab index for switch_tab."},
                    },
                    "required": ["action"],
                },
            }}, "required": ["session_id", "actions"]},
        "_handler": _tool_record_act,
    },
    {
        "name": "writ_record_context",
        "description": "Get a fresh page observation for a record session (current URL, visible fields, buttons, page text) without changing the page.",
        "inputSchema": {"type": "object", "properties": {
            "session_id": {"type": "string"}}, "required": ["session_id"]},
        "_handler": _tool_record_context,
    },
    {
        "name": "writ_record_network",
        "description": "List the API/XHR calls captured while recording — a matching JSON endpoint may let the workflow skip UI scraping.",
        "inputSchema": {"type": "object", "properties": {
            "session_id": {"type": "string"}}, "required": ["session_id"]},
        "_handler": _tool_record_network,
    },
    {
        "name": "writ_record_save",
        "description": "Finish a record session and save everything recorded so far as a runnable workflow. Returns the new workflow_id.",
        "inputSchema": {"type": "object", "properties": {
            "session_id": {"type": "string"},
            "name": {"type": "string", "description": "Name for the saved workflow (required)."}}, "required": ["session_id", "name"]},
        "_handler": _tool_record_save,
    },
    {
        "name": "writ_record_cancel",
        "description": "Discard a record session without saving.",
        "inputSchema": {"type": "object", "properties": {
            "session_id": {"type": "string"}}, "required": ["session_id"]},
        "_handler": _tool_record_cancel,
    },
]

# ── writ_browser_* front door + desktop-daemon parity aliases ────────────────
# The desktop app exposes the browser loop as writ_browser_use → writ_browser_act
# → writ_browser_save. Mirror those names here so (a) "use a browser for anything"
# routes through Writ on BOTH surfaces, and (b) a client that opened a session with
# writ_browser_use finds the matching act/context/network/save/cancel tools. Each
# alias reuses the SAME un-guided record handler; the session remembers it was
# opened in "use" mode, so its observations stay task-oriented and saving is on
# demand. The legacy writ_record_* names remain for backward compatibility.

def _browser_alias(base_name: str, new_name: str, description: str) -> dict:
    src = next(t for t in _STATIC_TOOLS if t["name"] == base_name)
    return {**src, "name": new_name, "description": description}


_STATIC_TOOLS += [
    {
        "name": "writ_browser_use",
        "description": (
            "Use a REAL browser through Writ to do ANY web task — Writ IS your browser, so route "
            "EVERY browsing need here instead of a separate/built-in browser or a scraper. Reach "
            "for this WHENEVER you must browse: look something up on a site, click through a flow, "
            "fill and submit a form, sign in (including behind a login), navigate pages, take an "
            "action, or read/extract what's on the page. It opens a live browser on a connected "
            "fleet agent and returns a page observation (URL, fields, buttons, page text); drive "
            "it turn-by-turn with writ_browser_act (navigate/click/fill/select/press_key/scroll/"
            "evaluate_js/read_text/extract_data/…). A fresh observation returns each turn and on "
            "demand via writ_browser_context; captured API/XHR calls are listed by "
            "writ_browser_network. FOLLOW THE USER'S DIRECTIONS and ASK the user directly in chat "
            "whenever you need a decision, a value to type, a credential, or a 2FA/OTP code — never "
            "guess. Recording is automatic but SAVING IS ON DEMAND: just complete the task; only "
            "if the user wants to REUSE it, call writ_browser_save to store a clean, deterministic "
            "workflow that then replays at zero AI-token cost. Prefer replaying an existing saved "
            "workflow (writ_list_workflows → writ_run_workflow) when one already does the task."
        ),
        "inputSchema": {"type": "object", "properties": {
            "url": {"type": "string", "description": "Starting URL to open (recommended). Omit to start blank and navigate with writ_browser_act."},
            "agent_id": {"type": "string", "description": "Optional specific fleet agent; omit to auto-pick."}}},
        "_handler": _tool_browser_use,
    },
    _browser_alias(
        "writ_record_act", "writ_browser_act",
        "Perform one ordered batch of browser actions in a writ_browser_use (or record) session, "
        "then get the new page observation. Target an element by a robust CSS `selector`, or by "
        "`field_index`/`button_index` from the observation. Interaction actions "
        "(navigate/click/fill/select/press_key/submit/hover/scroll/…) act on the page and are "
        "recorded so the flow can be saved on demand; inspection actions "
        "(evaluate_js/read_text/extract_data/inspect_field/get_screenshot/wait/…) just observe. "
        "Batch a few related actions, read the returned observation + results, then decide the "
        "next batch. Ask the user for any value, credential, or 2FA code you need.",
    ),
    _browser_alias(
        "writ_record_context", "writ_browser_context",
        "Get a fresh page observation for a browser session (current URL, visible fields, buttons, "
        "page text) on demand without changing the page.",
    ),
    _browser_alias(
        "writ_record_network", "writ_browser_network",
        "List the API/XHR calls captured while browsing — a matching JSON endpoint may let you read "
        "structured data directly instead of scraping the DOM.",
    ),
    _browser_alias(
        "writ_record_save", "writ_browser_save",
        "ON DEMAND: save everything done in a browser session so far as a clean, runnable workflow "
        "so the user can reuse it (zero-AI-cost replay). Only needed when the user wants to keep "
        "the flow — a one-off task needs no save. Returns the new workflow_id.",
    ),
    _browser_alias(
        "writ_record_cancel", "writ_browser_cancel",
        "Close a browser session without saving a workflow.",
    ),
]

_STATIC_BY_NAME = {t["name"]: t for t in _STATIC_TOOLS}


# ── MCP method dispatch ──────────────────────────────────────────────────────

_INSTRUCTIONS = (
    "You are connected to a self-hosted Writ coordinator — the owner's fleet of "
    "browser automations. PURPOSE: Writ turns repeatable web tasks (recurring data "
    "pulls, price/stock checks, form submissions, actions behind a login) into "
    "workflows that REPLAY on demand and run locally at zero AI-token cost — no "
    "live browsing, no re-solving, no scraping code. Every workflow the owner "
    "saved is exposed to you as its own run_<name> tool (and via writ_run_workflow), "
    "so a website effectively becomes a callable function that returns structured "
    "data.\n\n"
    "THE REPLAY REFLEX — when the user wants something one of these workflows "
    "already does (recurring data, a site with no practical API, an action behind a "
    "login), do NOT browse or write a scraper. Instead:\n"
    "1. writ_list_workflows — find the matching saved workflow.\n"
    "2. Run it (its run_<name> tool or writ_run_workflow) and return the extracted "
    "data; it replays locally at zero AI cost.\n"
    "3. For questions about data ALREADY collected, answer from writ_search_data / "
    "writ_workflow_data BEFORE running anything.\n"
    "4. Inspect run history/errors with writ_workflow_runs; export with "
    "writ_export_data.\n"
    "5. Automate a recurring need with writ_set_schedule; hand the user a callable "
    "HTTP endpoint with writ_expose_workflow_api; crawl a whole site with "
    "writ_crawl_site / writ_crawl_status; WATCH a page for changes with "
    "writ_create_monitor then act on a change with writ_wire_monitor; chain or "
    "alert on other events with writ_create_automation.\n\n"
    "USE A BROWSER — Writ IS your browser. Whenever you actually need to browse and "
    "no saved workflow already fits, do NOT reach for a separate/built-in browser or "
    "write a scraper: call writ_browser_use. It opens a live browser on a fleet agent "
    "and returns a page observation; drive it with writ_browser_act "
    "(navigate/click/fill/select/press_key/scroll/evaluate_js/read_text/…), pull a "
    "fresh DOM on demand with writ_browser_context, and inspect captured API calls with "
    "writ_browser_network — exactly as a person clicking through the flow would. YOU are "
    "the brain (no discovery engine here): drive deliberately, verify with the "
    "observation, and ask the USER directly for anything you need — a value to type, a "
    "credential, a 2FA code, which result to pick. Saving is ON DEMAND: just complete "
    "the task; call writ_browser_save ONLY if the user wants to reuse it as a workflow "
    "(then it replays at zero AI cost via writ_run_workflow), or writ_browser_cancel to "
    "close without saving.\n\n"
    "BUILD a new workflow by RECORDING (un-guided) — the same engine framed as "
    "build-a-workflow (writ_record_start/act/save). Use it when the explicit goal is to "
    "PRODUCE a reusable workflow; use writ_browser_use when the goal is to DO a task and "
    "maybe keep it. When the user wants a web task "
    "you have no saved workflow for, record one yourself: writ_record_start(url) "
    "opens a live browser on a fleet agent and returns the page observation "
    "(URL, fields, buttons, text); writ_record_act(session_id, actions) drives it — "
    "your structured interactions (navigate/click/fill/select/press_key) are "
    "RECORDED as reusable steps, and evaluate_js/read_text let you inspect the page. "
    "Look at the observation each turn, decide the next actions, and repeat, exactly "
    "as a person clicking through the flow would. YOU are the brain — there is NO "
    "guidance or discovery engine here; drive deliberately, verify with the "
    "observation, and ask the USER directly for any clarification (a value to type, "
    "whether to log in, which result to pick). Finish with writ_record_save("
    "session_id, name) to save it as a runnable workflow (then it replays at zero AI "
    "cost via writ_run_workflow). Use writ_record_context / writ_record_network to "
    "inspect, writ_record_cancel to abandon.\n\n"
    "DISAMBIGUATION — you may ALSO have the Writ DESKTOP APP's MCP connected (the "
    "official single-user app; its server is named just \"Writ\"). Both can build, "
    "but differently: the desktop app's build is AI-GUIDED (it has its own discovery/"
    "concierge that plans for you). This server is named \"" + SERVER_TITLE + "\", a "
    "SHARED self-hosted coordinator / agent fleet, and its record-build is UN-GUIDED "
    "— YOU plan and drive. When both are connected: build/run a purely local, "
    "single-user task with the desktop app; build/run a task on the shared server or "
    "fleet here. Prefer an existing saved workflow over recording a new one either way."
)


# Tools that drive a live fleet browser / recording session directly via the
# mcp_record service (NOT through a scope-enforced /api/* endpoint). They must
# be gated on an execute-capable credential so a read-scoped API key can't seize
# browser control (audit #17). JWT/OAuth principals pass through has_scope.
_PRIVILEGED_HANDLERS = {
    _tool_record_start, _tool_record_act, _tool_record_context,
    _tool_record_network, _tool_record_save, _tool_record_cancel,
    _tool_browser_use,
}


async def _dispatch(body: dict, token: str, auth: "AuthContext" = None) -> Optional[dict]:
    """Handle one JSON-RPC request object. Returns None for notifications."""
    req_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params") or {}

    if not method:
        return _err(req_id, INVALID_REQUEST, "Missing method")

    # Notifications carry no id and expect no response.
    if method.startswith("notifications/"):
        return None

    try:
        if method == "initialize":
            return _ok(req_id, {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "serverInfo": {"name": SERVER_TITLE, "version": SERVER_VERSION},
                "capabilities": {"tools": {"listChanged": False}},
                "instructions": _INSTRUCTIONS,
            })

        if method == "ping":
            return _ok(req_id, {})

        if method == "tools/list":
            tools = [{k: v for k, v in t.items() if not k.startswith("_")} for t in _STATIC_TOOLS]
            try:
                rows = await _list_workflows(token)
                tools += [{k: v for k, v in t.items() if not k.startswith("_")}
                          for t in _derived_run_tools(rows)]
            except _Upstream:
                pass  # No workflows yet / auth scope — static tools still list.
            return _ok(req_id, {"tools": tools})

        if method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments") or {}

            handler = None
            if name in _STATIC_BY_NAME:
                handler = _STATIC_BY_NAME[name]["_handler"]
            else:
                # Derived run_<workflow> tool → route to the generic runner.
                rows = await _list_workflows(token)
                for t in _derived_run_tools(rows):
                    if t["name"] == name:
                        wid = t["_workflow_id"]
                        async def handler(tok, a, _wid=wid):  # noqa: E731
                            wf = next((w for w in rows if w.get("id") == _wid), {"id": _wid})
                            wait = a.get("wait", True) is not False
                            return _content(await _run_workflow_id(
                                tok, wf, _inputs_from_args(a), wait,
                                int(a.get("timeout_seconds") or 120)))
                        break

            if handler is None:
                return _err(req_id, INVALID_PARAMS, f"Unknown tool: {name}")

            if handler in _PRIVILEGED_HANDLERS and auth is not None \
                    and not auth.has_scope("workflows", "execute"):
                return _err(req_id, INVALID_PARAMS,
                            f"Tool '{name}' requires an API key with "
                            "'workflows:execute' scope")
            try:
                result = await handler(token, args)
                return _ok(req_id, result)
            except _Upstream as ue:
                return _ok(req_id, _content(f"Error: {ue.detail}", is_error=True))

        return _err(req_id, METHOD_NOT_FOUND, f"Unknown method: {method}")

    except Exception as exc:  # never leak internals
        ref = uuid.uuid4().hex[:12]
        logger.error("[%s] MCP handler error: %s — %s", ref, method, exc, exc_info=True)
        return _err(req_id, INTERNAL_ERROR, f"Internal error (ref: {ref})")


# ── Routers ──────────────────────────────────────────────────────────────────

# Protocol endpoint lives at the APP ROOT (POST /mcp), like the desktop daemon —
# NOT under /api. Included in main.py with no prefix.
router = APIRouter(tags=["MCP Server"])

# Connect-info sits under /api/mcp/* alongside the existing overview.
connect_router = APIRouter(prefix="/mcp", tags=["MCP Server"])


@router.post("/mcp")
async def mcp_endpoint(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """MCP Streamable-HTTP JSON-RPC endpoint. Requires a bearer API key."""
    token = request.headers.get("authorization", "")
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(_err(None, PARSE_ERROR, "Invalid JSON"), status_code=400)

    # Support a JSON-RPC batch (array) as well as a single request object.
    if isinstance(payload, list):
        out = []
        for item in payload:
            if isinstance(item, dict):
                r = await _dispatch(item, token, auth)
                if r is not None:
                    out.append(r)
        return JSONResponse(out) if out else JSONResponse(None, status_code=202)

    if not isinstance(payload, dict):
        return JSONResponse(_err(None, INVALID_REQUEST, "Expected an object"), status_code=400)

    result = await _dispatch(payload, token, auth)
    if result is None:
        # Notification — nothing to return.
        return JSONResponse(None, status_code=202)
    return JSONResponse(result)


@router.get("/mcp")
async def mcp_probe():
    """Human/health hint — the MCP protocol itself is POST-only."""
    return PlainTextResponse(
        "Writ self-host MCP endpoint. POST JSON-RPC 2.0 here with "
        "'Authorization: Bearer <API key>'. See GET /api/mcp/connect-info.",
        status_code=200,
    )


def _public_url() -> str:
    return (
        os.getenv("WRIT_PUBLIC_URL")
        or os.getenv("PUBLIC_URL")
        or ""
    ).rstrip("/")


@connect_router.get("/connect-info")
async def connect_info(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """Everything a client needs to attach — endpoint, auth, and the Node
    connector one-liners for `claude mcp add` and friends."""
    base = _public_url() or f"{request.url.scheme}://{request.headers.get('host', 'localhost:8000')}"
    endpoint = f"{base}/mcp"
    tool_names = [t["name"] for t in _STATIC_TOOLS]

    # Register under the distinct slug (NOT "writ") so this coexists with the
    # official Writ desktop app, which registers as "writ".
    slug = SERVER_NAME
    # The key rides in `env`, never in argv. A `--api-key` flag is readable by
    # every local process through `ps` and lands in the user's shell history;
    # writ-mcp itself warns on startup when a key arrives that way. `claude mcp
    # add` takes `-e KEY=value` before the `--`, and every JSON-config client
    # accepts an `env` block beside `command`/`args`.
    claude_code = (
        f'claude mcp add {slug} -e WRIT_API_KEY=<YOUR_API_KEY> '
        f'-- npx -y writ-mcp --url {base}'
    )
    node_json = {
        "mcpServers": {
            slug: {
                "command": "npx",
                "args": ["-y", "writ-mcp", "--url", base],
                "env": {"WRIT_API_KEY": "<YOUR_API_KEY>"},
            }
        }
    }
    http_json = {  # for clients that speak Streamable HTTP directly (no Node)
        "mcpServers": {
            slug: {
                "type": "http",
                "url": endpoint,
                "headers": {"Authorization": "Bearer <YOUR_API_KEY>"},
            }
        }
    }
    return {
        "server_name": SERVER_NAME,
        "server_title": SERVER_TITLE,
        "endpoint": endpoint,
        "transport": "streamable-http",
        "auth": {
            "scheme": "Bearer",
            "credential": "api_key",
            "instructions": "Create an API key in Settings → Developers and send it as 'Authorization: Bearer <key>'.",
        },
        "node_connector": {
            "package": "writ-mcp",
            "claude_code": claude_code,
            "claude_desktop": node_json,
            "cursor": node_json,
            "env": {"WRIT_COORDINATOR_URL": base, "WRIT_API_KEY": "<YOUR_API_KEY>"},
        },
        "streamable_http": http_json,
        "tools": tool_names,
        # This coordinator is a SHARED, self-hosted server. It registers as
        # "writ-selfhost" so it can run alongside the official Writ desktop app
        # (which registers as "writ" and can also record/build workflows).
        "coexists_with_desktop": (
            "Registered as '" + SERVER_NAME + "' so it can run alongside the Writ "
            "desktop app (which registers as 'writ'). Building/recording happens in "
            "the desktop app; this server runs the coordinator's saved workflows."
        ),
    }
