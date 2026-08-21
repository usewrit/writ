"""
Native MCP server for the self-hosted coordinator.

Exposes the OSS surface — the same capabilities the desktop daemon serves at
``POST /mcp`` — over a single Streamable-HTTP MCP endpoint, in two families:

  **build**    record / browser — ``writ_browser_use``, ``writ_record_website``,
               ``writ_build``, ``writ_website_to_api`` open a live browser on a
               fleet agent that the CONNECTED CLIENT drives turn-by-turn
               (``writ_browser_act`` / ``_context`` / ``_network``) and freezes
               into a saved workflow (``writ_browser_save``). The client is the
               brain: there is no coordinator-side AI, no concierge, no
               autonomous loop, and no model key of ours in the path.
  **operate**  replay / run / data / search / export / runs / schedule /
               expose-API / crawl / monitors / automations.

There is no marketplace here — that is a cloud feature — so a build proposes the
owner's OWN matching workflows before recording, and nothing else.

It turns the self-host coordinator into an MCP tool provider that any MCP client
(Claude Code, Claude Desktop, Cursor, …) can attach to, via the bundled
``writ-mcp`` Node connector or directly over Streamable HTTP.

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

# Ceiling on derived run_<workflow> tools (pinned workflows). MCP clients inject
# every advertised tool schema into model context on every request, and several
# cap the total tool count (ChatGPT/VS Code at 128, Cursor warns near 40-50) —
# with ~30 static writ_* tools, 20 derived keeps the whole surface inside those
# budgets. Workflows past the cap (and every unpinned one) stay fully callable
# through writ_run_workflow. 0 disables derived tools entirely.
MCP_DERIVED_TOOL_CAP = max(0, int(os.getenv("MCP_DERIVED_TOOL_CAP", "20")))

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


async def _call(
    method: str, path: str, token: str, *, params=None, json_body=None,
    timeout: Optional[float] = None,
) -> Any:
    """Call the coordinator's own REST endpoint, forwarding the caller's bearer.

    Scope checks, validation, and metering happen in the target endpoint exactly
    as for any external caller — this hop adds no authority. Raises ``_Upstream``
    on a non-2xx so the tool layer can surface a clean error.

    ``timeout`` overrides the shared client's 30s read timeout for the ONE call —
    required for wait-mode endpoints (a crawl run with wait=true holds the
    response up to its own `timeout` seconds; the default 30s client limit
    aborted those mid-wait with a ReadTimeout).
    """
    headers = {"Authorization": token} if token else {}
    kwargs: dict = {}
    if timeout is not None:
        kwargs["timeout"] = httpx.Timeout(timeout)
    resp = await _http().request(method, path, headers=headers, params=params, json=json_body, **kwargs)
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


# Generic `files` schema for the workflow-agnostic runner, where the callable slot
# names aren't known until a workflow is resolved.
_FILES_PROPERTY_GENERIC = {
    "type": "object",
    "additionalProperties": {"type": "string"},
    "description": (
        "Optional file inputs for this run, as {slot: file_id}. Slot names come from "
        "the workflow's `file_slots` (writ_list_workflows); file_ids come from the "
        "file library. A workflow whose upload step already has a file pinned runs "
        "fine with no `files` at all — pass it only to swap the file for THIS run."
    ),
}


def _file_slots_property(w: dict) -> Optional[dict]:
    """The `files` tool property for ONE workflow, or None when it takes no files.

    Names each slot and its pinned default in the description so a model can tell
    what the call will use if it passes nothing, and which slot it must supply when
    a step ships no file. Never exposes a file_id — only slot names, labels and the
    default's FILENAME.
    """
    slots = w.get("file_slots")
    if not isinstance(slots, list) or not slots:
        return None
    lines: list[str] = []
    for fs in slots:
        if not isinstance(fs, dict) or not fs.get("slot"):
            continue
        label = fs.get("label") or fs["slot"]
        if fs.get("default_file_id"):
            fname = fs.get("default_filename")
            lines.append(f"{fs['slot']} — {label} (optional; defaults to the pinned file"
                         + (f" “{fname}”" if fname else "") + ")")
        else:
            lines.append(f"{fs['slot']} — {label} (REQUIRED: this step ships no file)")
    if not lines:
        return None
    return {
        "type": "object",
        "additionalProperties": {"type": "string"},
        "description": (
            "Optional file inputs for this run, as {slot: file_id} using ids from the "
            "file library. Overrides the step's pinned file for THIS run only. "
            "Slots: " + "; ".join(lines)
        ),
    }


def _derived_run_tools(rows: list[dict]) -> list[dict]:
    """One ``run_<workflow>`` tool per PINNED saved workflow.

    Lets an agent call a named tool instead of passing a workflow id to the
    generic runner. Input schema is derived from the workflow's declared inputs.
    Names are de-duped; the static ``writ_*`` names always win.

    Exposure is OPT-IN (``mcp_tool_pinned``, default off) and capped at
    MCP_DERIVED_TOOL_CAP: clients inject every advertised schema into model
    context on every request, so an instance with many workflows must not
    advertise them all as tools. Every workflow — pinned or not — stays callable
    through writ_run_workflow, and a stale ``run_<name>`` call still resolves in
    tools/call via _match_run_tool_name.
    """
    pinned = [w for w in rows if w.get("id") and w.get("mcp_tool_pinned")]
    if len(pinned) > MCP_DERIVED_TOOL_CAP:
        # Deterministic under the cap: most recently touched first (ISO-8601
        # strings order lexicographically), id as the tie-break.
        pinned.sort(
            key=lambda w: (str(w.get("updated_at") or w.get("created_at") or ""), w.get("id") or 0),
            reverse=True,
        )
        pinned = pinned[:MCP_DERIVED_TOOL_CAP]
    used = {t["name"] for t in _STATIC_TOOLS}
    tools: list[dict] = []
    for w in pinned:
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
        # File inputs: every upload step is bindable at call time. A step with a
        # pinned file needs no argument (it resolves server-side), so `files` is
        # always OPTIONAL here — it exists so a caller can run the same workflow
        # against a DIFFERENT file without editing it.
        _fs = _file_slots_property(w)
        if _fs:
            props.setdefault("files", _fs)
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


def _match_run_tool_name(rows: list[dict], name: str) -> list[dict]:
    """Workflows whose derived tool name would be ``name`` — ALL rows, not just
    pinned ones, so a client that cached the tool list before a workflow was
    unpinned (or before exposure became opt-in) keeps working instead of 404ing.

    Deliberately conservative: a tool name is a machine identifier, so only an
    exact slug match counts, plus a retry with a trailing ``_N`` de-dup suffix
    stripped (those suffixes were assigned in list order and are not stable).
    Anything fuzzier risks silently running the WRONG workflow — a miss routes
    the caller to writ_run_workflow instead, where name resolution is explicit.
    """
    target = name[len("run_"):]
    base = re.sub(r"_\d+$", "", target)
    exact: list[dict] = []
    stripped: list[dict] = []
    for w in rows:
        if not w.get("id"):
            continue
        s = _slug(w.get("name") or ("workflow_%s" % w["id"]))
        if s == target:
            exact.append(w)
        elif base != target and s == base:
            stripped.append(w)
    return exact or stripped


def _derived_tool_handler(rows: list[dict], wid: int):
    """The tools/call handler for one workflow's derived ``run_<name>`` tool.

    Shared by the advertised (pinned) tools and the stale-name fallback so both
    lanes honour the same contract — notably `max_age` freshness, which the
    derived schemas advertise and must therefore honour.
    """
    async def handler(tok, a, _wid=wid):
        wf = next((w for w in rows if w.get("id") == _wid), {"id": _wid})
        wait = a.get("wait", True) is not False
        inputs = _inputs_from_args(a)
        files = _files_from_args(a)
        # persona_id is reserved out of inputs, so honour it here too — a
        # derived tool given one must run AS that identity, not drop it into
        # form_data or the void.
        persona_id = _run_persona_id(a)
        fkey = _freshness_key(_wid, inputs, files, persona_id)
        requested = _requested_max_age(a)
        if requested > 0:
            hit = _cached_run(fkey, requested)
            if hit is not None:
                return _content(hit)
        res = await _run_workflow_id(
            tok, wf, inputs, wait,
            int(a.get("timeout_seconds") or 120), files, persona_id)
        _store_run(fkey, res)
        return _content(res)
    return handler


# ── Tool handlers ────────────────────────────────────────────────────────────

def _inputs_from_args(args: dict) -> dict:
    """Everything that isn't a control key is treated as a run input."""
    reserved = {"workflow", "workflow_id", "id", "name", "wait", "timeout_seconds",
                "files", "persona_id", FRESHNESS_ARG}
    if isinstance(args.get("inputs"), dict):
        return dict(args["inputs"])
    return {k: v for k, v in args.items() if k not in reserved}


def _run_persona_id(args: dict) -> Optional[int]:
    """The run's persona override (run AS this saved identity).

    Malformed values HARD-fail rather than degrade to None — silently running
    anonymously when the caller asked for an identity is the worst outcome (the
    run "works" and returns the logged-out site).
    """
    raw = args.get("persona_id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise _Upstream(422, "persona_id must be the numeric id of one of your "
                             "personas — list them with writ_personas.")


def _files_from_args(args: dict) -> dict:
    """The run's file bindings: ``{slot: file_id}`` (§4.5).

    Kept OUT of form_data — the run endpoint reads files from a top-level `files`
    key, and a slot left in form_data would be treated as an ordinary text input and
    the file silently never bound. Only string→string pairs survive; the coordinator
    resolves every id fail-closed (resolve_for_run 404s on a bad reference), so
    nothing here is trusted beyond its shape.
    """
    raw = args.get("files")
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, str) and v}


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
    "persona_id": {
        "type": "integer",
        "description": (
            "Run AS this saved identity (see writ_personas) — the run signs in with "
            "the persona's warm session. Omit to use the workflow's default persona, "
            "if it has one."
        ),
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


def _freshness_key(wf_id: int, inputs: dict, files: dict = None,
                   persona_id: Optional[int] = None) -> tuple:
    # The FILES map is part of the key: uploading a different file is a different
    # question, so a run bound to file B must never be served file A's cached answer.
    # The PERSONA is part of it too: a run signed in as identity A sees different
    # pages than an anonymous run or identity B.
    return (wf_id,
            json.dumps(inputs or {}, sort_keys=True, default=str),
            json.dumps(files or {}, sort_keys=True, default=str),
            persona_id)


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


async def _run_workflow_id(token: str, wf: dict, inputs: dict, wait: bool, timeout_s: int,
                           files: dict = None, persona_id: Optional[int] = None) -> dict:
    wid = wf["id"]
    dispatch_ts = time.time()
    body: dict = {"form_data": inputs}
    # Files ride at the TOP LEVEL of the run body, not inside form_data — that is
    # where the run endpoint binds them from (§4.5). Omitted entirely when empty so
    # a workflow with a pinned file keeps resolving it server-side.
    if files:
        body["files"] = files
    # Same top-level rule for the run-as persona: the run endpoint reads it from
    # the body root, and omitting it keeps the workflow's default_persona_id in
    # charge server-side.
    if persona_id is not None:
        body["persona_id"] = persona_id
    disp = await _call(
        "POST", f"/api/automation/workflows/{wid}/run", token,
        json_body=body,
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
        row = {
            "id": w.get("id"),
            "name": w.get("name"),
            "description": w.get("description"),
            "inputs": list((w.get("form_data") or {}).keys()),
            "schedule_enabled": w.get("schedule_enabled"),
            "schedule_kind": w.get("schedule_kind"),
        }
        # Marker only when set — the common (unpinned) case stays compact.
        if w.get("mcp_tool_pinned"):
            row["mcp_tool_pinned"] = True
        # File inputs, so a caller can discover the slot names it may bind (and
        # which ones it MUST) without a second round-trip. Names only — the pinned
        # file's id stays server-side; only its filename is descriptive.
        fslots = [
            {"slot": fs.get("slot"), "label": fs.get("label"),
             "required": not fs.get("default_file_id"),
             "default_filename": fs.get("default_filename")}
            for fs in (w.get("file_slots") or [])
            if isinstance(fs, dict) and fs.get("slot")
        ]
        if fslots:
            row["file_slots"] = fslots
        out.append(row)
    return _content({"workflows": out, "total": len(out)})


async def _tool_run_workflow(token: str, args: dict) -> dict:
    wf = await _resolve_workflow(token, args)
    wait = args.get("wait", True) is not False
    timeout_s = int(args.get("timeout_seconds") or 120)
    inputs = _inputs_from_args(args)
    files = _files_from_args(args)
    persona_id = _run_persona_id(args)

    # FRESHNESS first: a reusable answer means no dispatch at all.
    max_age = _requested_max_age(args)
    key = _freshness_key(wf["id"], inputs, files, persona_id)
    if max_age > 0:
        hit = _cached_run(key, max_age)
        if hit is not None:
            return _content(hit)

    res = await _run_workflow_id(token, wf, inputs, wait, timeout_s, files, persona_id)
    _store_run(key, res)
    return _content(res)


async def _tool_pin_workflow_tool(token: str, args: dict) -> dict:
    """Pin/unpin one workflow as its own derived run_<name> tool.

    Thin translation onto the workflows PUT endpoint (scope-checked there). After
    pinning, re-derive the tool list to report the exact tool name — or that the
    cap is full, in which case the pin is stored but not advertised.
    """
    wf = await _resolve_workflow(token, args)
    pinned = args.get("pinned", True) is not False
    await _call("PUT", f"/api/automation/workflows/{wf['id']}", token,
                json_body={"mcp_tool_pinned": pinned})
    out: dict = {"workflow_id": wf["id"], "name": wf.get("name"), "mcp_tool_pinned": pinned}
    if pinned:
        rows = await _list_workflows(token)
        tool_name = next(
            (t["name"] for t in _derived_run_tools(rows) if t["_workflow_id"] == wf["id"]), None)
        if tool_name:
            out["tool"] = tool_name
            out["note"] = "Clients pick the new tool up on their next tool listing."
        else:
            out["note"] = (
                f"Pinned, but the {MCP_DERIVED_TOOL_CAP}-tool cap is full, so no tool is "
                "advertised for it — it still runs via writ_run_workflow. Unpin a less-used "
                "workflow to free a slot.")
    return _content(out)


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
    "name", "executor", "extract_mode", "extract_schema", "extract_prompt",
    "include_paths", "exclude_paths",
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
    call_timeout: Optional[float] = None
    if args.get("wait") is not None:
        run_body["wait"] = args["wait"] is not False
        run_body["timeout"] = int(args.get("timeout_seconds") or 120)
        if run_body["wait"]:
            # The run endpoint holds the response up to `timeout` seconds — give the
            # loopback client that long plus margin, or it ReadTimeouts at 30s.
            call_timeout = run_body["timeout"] + 15
    result = await _call("POST", f"/api/crawl/definitions/{defn['slug']}/run", token,
                         json_body=run_body, timeout=call_timeout)
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
    wait = args.get("wait") is True
    body = {
        "max_age": _requested_max_age(args),
        "wait": wait,
        "timeout": int(args.get("timeout_seconds") or 120),
        "limit": int(args.get("limit") or 50),
    }
    # A held wait outlives the loopback client's default 30s read timeout — give
    # it the wait budget plus margin, or the tool dies in a ReadTimeout mid-wait.
    call_timeout: Optional[float] = (body["timeout"] + 15) if wait else None
    res = await _call("POST", f"/api/crawl/definitions/{ref}/run", token,
                      json_body=body, timeout=call_timeout)
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


async def _tool_scrape(token: str, args: dict) -> dict:
    """One page → clean markdown. The single-page twin of writ_crawl_site
    (name/behaviour parity with the cloud connector)."""
    url = (args.get("url") or "").strip()
    if not url:
        raise _Upstream(400, "writ_scrape requires a `url`.")
    body: dict = {"url": url}
    if args.get("persona_id") is not None:
        body["persona_id"] = args["persona_id"]
    # A single page can still ride a real browser render on an agent, which
    # routinely exceeds the shared 30s loopback limit.
    res = await _call("POST", "/api/crawl/scrape", token, json_body=body, timeout=75)
    return _content(res)


# ── Personas (saved sign-in identities) ──────────────────────────────────────

#: The agent-facing projection of one persona. A deliberate SUBSET of the REST
#: response: mailbox/relay plumbing has no read use in an agent loop, so it never
#: enters the model's context. Secrets are already absent at the source — the
#: REST layer only ever returns has_* booleans for them.
_PERSONA_ALWAYS = ("id", "name", "is_active", "twofa_method", "has_password",
                   "has_warm_session", "can_self_login")
_PERSONA_OPTIONAL = ("description", "target_domain", "login_username",
                     "email_otp_mode", "validation_status", "has_totp_seed",
                     "session_expires_at", "login_workflow_id", "login_workflow_name",
                     "last_login_at", "last_login_error", "last_used_at",
                     "has_proxy", "linked_workflows")


def _persona_view(row: dict) -> dict:
    out = {k: row.get(k) for k in _PERSONA_ALWAYS}
    out.update({k: row[k] for k in _PERSONA_OPTIONAL if row.get(k) not in (None, [], {})})
    return out


def _personas_usage_note(rows: list) -> str:
    """What to do next — the part a connected model gets wrong without guidance:
    personas are USED via persona_id on the run/crawl/scrape tools, and they are
    CREATED only in the Writ dashboard (credentials must never transit MCP)."""
    if not rows:
        return (
            "No personas saved. A persona is a saved sign-in identity (username + "
            "credentials sealed server-side, optional 2FA) that lets runs act behind a "
            "login. Creating one requires credentials, which never pass through this "
            "connection — ask the user to add it in the Writ dashboard on the Personas "
            "page, then use it here by persona_id."
        )
    stale = [r["id"] for r in rows if r.get("is_active") and not r.get("has_warm_session")]
    note = (
        "Use a persona by passing its persona_id to writ_crawl_site, writ_scrape or "
        "writ_run_workflow — the run then acts signed in as that identity, with any "
        "2FA code minted server-side. "
    )
    if stale:
        note += (
            f"Personas {stale} have no warm session right now: action='sign_in' "
            "refreshes one that can_self_login; otherwise action='record_login' has "
            "the AI record its sign-in once. "
        )
    note += (
        "Credentials are managed only in the Writ dashboard (Personas page) — this "
        "tool cannot create, edit or delete a persona."
    )
    return note


async def _tool_personas(token: str, args: dict) -> dict:
    """Operate the coordinator's saved sign-in identities, end to end minus creation.

    list/get answer "which identity can I act as, and is it ready?"; sign_in
    establishes/refreshes the warm session by running the persona's login workflow;
    record_login makes an existing persona ABLE to sign itself in by having a
    local AI session record the flow once. Lifecycle mutations (create / edit /
    delete) stay in the dashboard on purpose: they carry credentials, and no
    secret may transit the MCP surface in either direction.
    """
    action = str(args.get("action") or "list").strip().lower()

    if action == "list":
        params = {}
        if args.get("domain"):
            params["domain"] = str(args["domain"]).strip()
        rows = await _call("GET", "/api/personas", token, params=params or None)
        out = [_persona_view(r) for r in (rows or []) if isinstance(r, dict)]
        return _content({
            "personas": out,
            "total": len(out),
            "next": _personas_usage_note(out),
        })

    try:
        pid = int(args.get("persona_id"))
    except (TypeError, ValueError):
        return _content(
            f"Error: action '{action}' needs a numeric persona_id — find it with "
            "writ_personas action='list'.", is_error=True)

    if action == "get":
        row = await _call("GET", f"/api/personas/{pid}", token)
        out = _persona_view(row if isinstance(row, dict) else {})
        if args.get("include_runs") is True:
            runs = await _call("GET", f"/api/personas/{pid}/runs", token,
                               params={"limit": 10})
            out["recent_runs"] = runs or []
        return _content(out)

    if action == "sign_in":
        # The REST endpoint runs the persona's login workflow synchronously and holds
        # the response up to its own LOGIN_TIMEOUT_SECONDS (240s) — the shared 30s
        # loopback limit would abort every real login mid-run.
        res = await _call("POST", f"/api/personas/{pid}/sign-in", token,
                          json_body={"force": args.get("force") is True}, timeout=270)
        res = res if isinstance(res, dict) else {}
        if not res.get("ok"):
            res["next"] = (
                "The persona is not signed in. If it cannot self-login (no login "
                "workflow), run writ_personas action='record_login' so the AI records "
                "the sign-in once; if the error points at wrong credentials, the user "
                "must fix them in the Writ dashboard (Personas page)."
            )
        return _content(res)

    if action == "record_login":
        body = {}
        if args.get("login_url"):
            body["login_url"] = str(args["login_url"])
        res = await _call("POST", f"/api/personas/{pid}/record-login-ai", token,
                          json_body=body)
        res = res if isinstance(res, dict) else {}
        res["next"] = (
            "An AI session is signing in as this persona and recording the flow "
            "(credentials stay masked; it never needs you). Poll writ_personas "
            "action='get' for this persona_id: when can_self_login turns true the "
            "recording became its login workflow — then action='sign_in' establishes "
            "the warm session. A last_login_error instead means the attempt failed."
        )
        return _content(res)

    return _content(
        f"Error: unknown action '{action}'. Use one of: list, get, sign_in, "
        "record_login.", is_error=True)


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
    if args.get("ai_prompt"):
        # Wake an AI agent when the event fires. The goal supports the same
        # {{placeholders}} as notification templates; entry_url falls back to
        # the event's page (the monitored URL on change_detected) — for
        # workflow_*/webhook events give `ai_entry_url` explicitly or the wake
        # is skipped with a clear reason.
        ai_cfg: dict = {"goal": str(args["ai_prompt"]).strip()}
        if args.get("ai_entry_url"):
            ai_cfg["entry_url"] = str(args["ai_entry_url"]).strip()
        if args.get("cooldown_minutes") is not None:
            try:
                ai_cfg["cooldown_minutes"] = max(0, int(args["cooldown_minutes"]))
            except (TypeError, ValueError):
                raise _Upstream(400, "`cooldown_minutes` must be an integer.")
        actions.append({"type": "ai_session", "config": ai_cfg})
    if isinstance(args.get("actions"), list):
        actions.extend(a for a in args["actions"] if isinstance(a, dict))
    if not actions:
        raise _Upstream(400, "Give the automation something to do: `run_workflow`, `notify`, `ai_prompt`, or a raw `actions` list.")
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
    with writ_wire_monitor. Backed by POST /api/targets; selfhost enforces no
    plan interval floor — an omitted interval falls back to the instance's
    global check period.
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
    out = {
        "monitor_id": (created or {}).get("id"),
        "url": (created or {}).get("url") or url,
        "check_type": body["check_type"],
        "selector": selector,
        "interval_ms": (created or {}).get("checkPeriodMs") or (created or {}).get("check_period_ms") or body.get("check_period_ms"),
        "requires_browser": body["requires_playwright"],
        "enabled": (created or {}).get("enabled", body["enabled"]),
        "next": "Call writ_wire_monitor with this monitor_id to choose what happens on a detected change (run a saved workflow, or notify).",
    }
    if selector and body["requires_playwright"]:
        # Browser-mode selectors are validated against the rendered,
        # frame-flattened DOM on the first check — not against a raw-HTML fetch
        # at create time — so a mistyped selector surfaces there, not here.
        out["note"] = (
            "Browser-rendered monitor: the selector is verified against the "
            "rendered (frame-flattened) DOM on the first check, not the raw HTML."
        )
    return _content(out)


async def _tool_wire_monitor(token: str, args: dict) -> dict:
    """Wire a monitor's `change_detected` event to an action via the trigger engine.

    action='workflow' runs a saved workflow when the monitored page changes;
    action='notify' sends a notification (needs `channels` + `recipients` the
    account has configured, e.g. channels=["pushover"], recipients=["pushover:1"]);
    action='ai_task' WAKES AN AI AGENT with a task `prompt` — a fleet agent with
    local AI opens the monitored page with the change context (diff, extracted
    values) and works the prompt autonomously (self-host is goal-only: there are
    no saved AI-session recipes to re-run).
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
    extra_actions: list = []
    extra_blocks: list = []
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
    elif action in ("ai_task", "ai", "wake_agent"):
        prompt = str(args.get("prompt") or args.get("goal") or "").strip()
        if not prompt:
            raise _Upstream(400, (
                "action='ai_task' requires a `prompt` — what the AI agent should do when "
                "the monitor fires."
            ))
        cfg = {"goal": prompt}
        if args.get("entry_url"):
            cfg["entry_url"] = str(args["entry_url"]).strip()
        if args.get("max_steps") is not None:
            try:
                cfg["max_steps"] = max(1, min(100, int(args["max_steps"])))
            except (TypeError, ValueError):
                raise _Upstream(400, "`max_steps` must be an integer.")
        if args.get("cooldown_minutes") is not None:
            try:
                cfg["cooldown_minutes"] = max(0, int(args["cooldown_minutes"]))
            except (TypeError, ValueError):
                raise _Upstream(400, "`cooldown_minutes` must be an integer.")
        act = {"type": "ai_session", "config": cfg}
        action_block = {"id": "act", "type": "action", "blockType": "ai_session",
                        "parentId": "evt", "config": cfg}
        # Optional finish alert: channels given → chain change → agent →
        # (ai_session_completed) → notification.
        channels = args.get("channels") if isinstance(args.get("channels"), list) else []
        recipients = args.get("recipients") if isinstance(args.get("recipients"), list) else []
        if channels:
            notify_cfg = {
                "channels": channels,
                "recipients": recipients,
                "title": args.get("title") or "Your AI agent finished",
                "template": args.get("message") or "Status: {{session_status}}",
            }
            extra_blocks.extend([
                {"id": "wait", "type": "event", "blockType": "ai_session_completed",
                 "parentId": "act", "config": {"linked_to_block": "act"}},
                {"id": "act2", "type": "action", "blockType": "notification",
                 "parentId": "wait", "config": notify_cfg},
            ])
            extra_actions.append({"type": "notification", "config": notify_cfg})
        note = (
            "The agent wakes with the monitor's change context (page URL, diff snippet, "
            "extracted values) and starts on the monitored page unless `entry_url` overrides it. "
            "It runs on a fleet agent with local AI configured (Settings → AI); "
            "`cooldown_minutes` suppresses re-wakes within the window."
        )
    else:
        raise _Upstream(400, "`action` must be 'workflow', 'notify' or 'ai_task'.")
    body = {
        "name": name,
        "event_type": "change_detected",
        "target_id": monitor_id,
        "enabled": args.get("enabled", True) is not False,
        "actions": [act] + extra_actions,
        "blocks": [event_block, action_block] + extra_blocks,
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


# ── Un-guided record / browser tools (the client is the brain) ───────────────
# These open a live browser session on a fleet agent and let the connected client
# drive it freely; structured interactions are recorded as workflow steps, then
# saved as a runnable workflow. No coordinator guidance, no coordinator AI.
#
# FOUR front doors, same loop — the name tells the model what it is FOR:
#   writ_browser_use      do a web task now; saving is optional
#   writ_record_website   record a repeatable task on a site
#   writ_build            build a reusable workflow (generic)
#   writ_website_to_api   turn a site with no practical API into a callable one
# They differ only in framing and in the ladder they walk before recording.

# Request-phrasing words that carry no signal when matching a build goal against
# the owner's own library. Mirrors the cloud/desktop tokenizer so all three
# surfaces rank an existing workflow the same way.
_GOAL_STOPWORDS = {
    "the", "and", "for", "that", "this", "with", "from", "api", "apis", "get",
    "data", "rest", "call", "want", "une", "les", "des", "pour", "avec", "que",
    "qui", "veux",
}


def _goal_terms(goal: str) -> list[str]:
    """Up to six distinctive lowercase terms from a build goal."""
    out: list[str] = []
    for raw in re.split(r"[^0-9A-Za-z.\-]+", goal or ""):
        term = raw.strip(".-").lower()
        if len(term) < 3 or term in _GOAL_STOPWORDS or term in out:
            continue
        out.append(term)
        if len(out) == 6:
            break
    return out


def _url_host(url: str) -> Optional[str]:
    from urllib.parse import urlsplit
    try:
        host = (urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return None
    return host[4:] if host.startswith("www.") else (host or None)


def _match_own_workflows(rows: list[dict], goal: str, host: Optional[str]) -> list[dict]:
    """The owner's OWN saved workflows that already match this goal.

    An entry-url HOST match qualifies outright; otherwise most of the goal's
    distinctive terms must appear in the name/description/entry-url. Deliberately
    conservative — a weak match must never hijack a build.
    """
    terms = _goal_terms(goal)
    scored: list[tuple[float, dict]] = []
    for w in rows:
        hay = " ".join(str(w.get(k) or "").lower()
                       for k in ("name", "description", "entry_url"))
        matched = sum(1 for t in terms if t in hay)
        coverage = (matched / len(terms)) if terms else 0.0
        host_match = bool(host and host in str(w.get("entry_url") or "").lower())
        if not (host_match or (coverage >= 0.6 and matched >= 2)):
            continue
        scored.append((coverage * 3.0 + (2.0 if host_match else 0.0), {
            "workflow_id": w.get("id"),
            "name": w.get("name"),
            "description": w.get("description"),
            "inputs": list((w.get("form_data") or {}).keys()),
        }))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _score, item in scored[:3]]


_API_INTENT_PHRASES = (
    "website to api", "site to api", "turn this website into an api",
    "turn the website into an api", "transform this website to api",
    "transform the website to api", "build an api", "create an api",
    "make an api", "expose as an api", "expose it as an api",
    "callable api", "api endpoint", "structured api",
)


def _goal_requests_api(args: dict) -> bool:
    """Classify API-builder intent from the goal itself.

    A connected model does not always pick the specialized start tool, so a
    generic writ_build / writ_record_website call must not silently skip the API
    framing. Mirrors the cloud/desktop classifier.
    """
    goal = str(args.get("goal") or "").lower()
    return any(phrase in goal for phrase in _API_INTENT_PHRASES)


async def _browser_start(token: str, args: dict, mode: str, *, api: bool) -> dict:
    """Shared front door for the four start tools.

    BUILD LADDER for API intent — cheapest answer first, recording last:
      1. the owner's OWN saved workflows (instant, free) — `skip_existing` skips;
      2. open a browser and record.
    Checked BEFORE the browser opens, so a declined proposal costs nothing. There
    is no marketplace rung on a self-hosted coordinator — that is a cloud feature.
    """
    from services import mcp_record

    goal = str(args.get("goal") or "").strip()
    url = (args.get("url") or "").strip()
    if mode != mcp_record.MODE_USE and not goal:
        raise _Upstream(400, "`goal` is required for a record/build session.")
    if not url:
        if mode != mcp_record.MODE_USE:
            raise _Upstream(400, "`url` is required for a record/build session.")
        # writ_browser_use may start blank and navigate — this browser is the
        # owner's own fleet, so there is no origin to pre-screen against.
        url = "about:blank"

    if api and not args.get("skip_existing"):
        rows = await _list_workflows(token)
        matches = _match_own_workflows(rows, goal, _url_host(url))
        if matches:
            return _content({
                "status": "existing_workflows",
                "message": ("This coordinator already has workflows matching the goal — "
                            "replaying one is instant and needs no browser."),
                "goal": goal,
                "workflows": matches,
                "next": ("Propose these to the user FIRST. If one fits, run it with "
                         "writ_run_workflow, or read what it already collected with "
                         "writ_workflow_data. If none fits, call this tool again with "
                         "skip_existing=true to record a new one."),
            })

    try:
        res = await mcp_record.start(url, args.get("agent_id"), mode=mode, goal=goal)
    except mcp_record.RecordError as e:
        return _content(f"Error: {e}", is_error=True)
    return _content(res)


async def _tool_browser_use(token: str, args: dict) -> dict:
    from services import mcp_record
    return await _browser_start(token, args, mcp_record.MODE_USE, api=False)


async def _tool_record_website(token: str, args: dict) -> dict:
    from services import mcp_record
    return await _browser_start(token, args, mcp_record.MODE_RECORD, api=_goal_requests_api(args))


async def _tool_build(token: str, args: dict) -> dict:
    from services import mcp_record
    return await _browser_start(token, args, mcp_record.MODE_RECORD, api=_goal_requests_api(args))


async def _tool_website_to_api(token: str, args: dict) -> dict:
    from services import mcp_record
    return await _browser_start(token, args, mcp_record.MODE_API, api=True)


async def _tool_record_start(token: str, args: dict) -> dict:
    """Legacy front door: same as writ_record_website, but its schema only ever
    required `url`, so it must keep working without a `goal`."""
    from services import mcp_record
    try:
        return _content(await mcp_record.start(
            args.get("url"), args.get("agent_id"),
            mode=mcp_record.MODE_RECORD, goal=str(args.get("goal") or "").strip(),
        ))
    except mcp_record.RecordError as e:
        return _content(f"Error: {e}", is_error=True)


async def _tool_record_act(token: str, args: dict) -> dict:
    from services import mcp_record
    try:
        return _content(await mcp_record.act(
            args.get("session_id"), args.get("actions"),
            inputs=args.get("inputs") if isinstance(args.get("inputs"), dict) else None,
        ))
    except mcp_record.RecordError as e:
        return _content(f"Error: {e}", is_error=True)


async def _tool_record_context(token: str, args: dict) -> dict:
    from services import mcp_record
    try:
        return _content(await mcp_record.context(
            args.get("session_id"),
            section=args.get("section") or "page",
            offset=int(args.get("offset") or 0),
            max_chars=int(args.get("max_chars") or 8000),
        ))
    except mcp_record.RecordError as e:
        return _content(f"Error: {e}", is_error=True)


async def _tool_record_network(token: str, args: dict) -> dict:
    from services import mcp_record
    try:
        return _content(await mcp_record.network(
            args.get("session_id"),
            operation=args.get("operation") or "search",
            query=args.get("query") or args.get("url") or "",
            method=args.get("method"),
            index=int(args["index"]) if args.get("index") is not None else None,
            offset=int(args.get("offset") or 0),
            max_chars=int(args.get("max_chars") or 8000),
        ))
    except mcp_record.RecordError as e:
        return _content(f"Error: {e}", is_error=True)


def _concise_name(goal: str) -> str:
    """A short, human workflow name from the session goal (desktop parity)."""
    text = " ".join((goal or "").split())
    if not text:
        return "Browser workflow"
    if len(text) <= 60:
        return text[:1].upper() + text[1:]
    cut = text[:60].rsplit(" ", 1)[0]
    return (cut[:1].upper() + cut[1:]) or "Browser workflow"


async def _tool_record_save(token: str, args: dict) -> dict:
    from services import mcp_record
    sid = args.get("session_id")
    try:
        payload = await mcp_record.finalize(sid)
    except mcp_record.RecordError as e:
        return _content(f"Error: {e}", is_error=True)
    # A name is no longer mandatory: fall back to the goal the session was opened
    # with, so a client that just finished a task can save without inventing one.
    name = (args.get("name") or "").strip() or _concise_name(payload.get("goal") or "")
    body = {
        "name": name,
        "workflow_type": "recorded",
        "entry_url": payload["entry_url"],
        "steps": payload["steps"],
    }
    if args.get("description"):
        body["description"] = str(args["description"])[:2000]
    try:
        created = await _call("POST", "/api/automation/workflows", token, json_body=body)
    except _Upstream as ue:
        return _content(f"Error saving workflow: {ue.detail}", is_error=True)
    if args.get("keep_open") is not True:
        await mcp_record.cancel(sid)  # close the session now that it's persisted
    return _content({
        "workflow_id": (created or {}).get("id"),
        "name": (created or {}).get("name") or name,
        "steps": len(payload["steps"]),
        "browser": ("still open — call writ_browser_cancel when finished"
                    if args.get("keep_open") is True else "closed"),
        "next": (
            "Saved. It now runs on demand with writ_run_workflow (pin it with "
            "writ_pin_workflow_tool for its own run_<name> tool) with no model in the loop, "
            "can be scheduled with writ_set_schedule, and can be exposed as a REST endpoint "
            "with writ_expose_workflow_api."
        ),
    })


async def _tool_record_cancel(token: str, args: dict) -> dict:
    from services import mcp_record
    try:
        return _content(await mcp_record.cancel(args.get("session_id")))
    except mcp_record.RecordError as e:
        return _content(f"Error: {e}", is_error=True)


# ── Static tool catalog ──────────────────────────────────────────────────────

_STATIC_TOOLS: list[dict] = [
    {
        "name": "writ_list_workflows",
        "description": "List the workflows you have already saved on this self-hosted Writ coordinator — each runs at zero AI cost. Returns id, name, declared inputs, schedule, and whether it is pinned as its own run_<name> tool.",
        "inputSchema": {"type": "object", "properties": {
            "search": {"type": "string", "description": "Optional name/description filter."}}},
        "_handler": _tool_list_workflows,
    },
    {
        "name": "writ_run_workflow",
        "description": "Run a saved workflow by id or name and (by default) wait for it to finish, returning the extracted data. Pass workflow inputs as top-level fields or under `inputs`, and any file inputs under `files`.",
        "inputSchema": {"type": "object", "properties": {
            "workflow": {"type": "string", "description": "Workflow name (or use workflow_id)."},
            "workflow_id": {"type": "integer"},
            "inputs": {"type": "object", "description": "Run inputs (or pass them as top-level fields)."},
            "files": _FILES_PROPERTY_GENERIC,
            **RUN_CONTROL_PROPERTIES}},
        "_handler": _tool_run_workflow,
    },
    {
        "name": "writ_pin_workflow_tool",
        "description": (
            "Pin (or unpin) a saved workflow as its own run_<name> tool on this server. "
            "Workflows are NOT exposed as individual tools by default — every one is always "
            "callable via writ_run_workflow — so pin only the few the user runs often enough "
            "to deserve a first-class tool (the derived list is capped). Do this when the "
            "user asks for it, or after saving a workflow the user clearly intends to call "
            "as a tool from here."),
        "inputSchema": {"type": "object", "properties": {
            "workflow": {"type": "string", "description": "Workflow name (or use workflow_id)."},
            "workflow_id": {"type": "integer"},
            "pinned": {"type": "boolean", "description": "true (default) pins; false unpins."}}},
        "_handler": _tool_pin_workflow_tool,
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
        "name": "writ_personas",
        "description": (
            "See and operate this coordinator's saved sign-in identities (personas) so "
            "tasks behind a login run unattended. A persona holds a site's username plus "
            "credentials sealed server-side (never readable here), optional 2FA whose "
            "codes are minted server-side, and a warm signed-in session. USE one by "
            "passing its persona_id to writ_crawl_site, writ_scrape or "
            "writ_run_workflow. BEFORE asking the user for credentials for a site, call "
            "action='list' (filter by domain) — an existing persona already answers a "
            "login-gated task. action='get' inspects one persona (include_runs adds its "
            "recent runs); action='sign_in' runs its login workflow NOW to establish or "
            "refresh the warm session (force=true re-logs-in even when the session still "
            "looks usable); action='record_login' launches an AI session that signs in "
            "as the persona once and RECORDS the flow as its login workflow, after which "
            "it can always sign itself back in. This tool can NOT create, edit or delete "
            "personas, and no credential or one-time code ever passes through it — the "
            "user manages those in the Writ dashboard on the Personas page; send them "
            "there when no persona fits."
        ),
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "get", "sign_in", "record_login"],
                       "description": "What to do (default list)."},
            "persona_id": {"type": "integer", "description": (
                "Which persona — required for get / sign_in / record_login.")},
            "domain": {"type": "string", "description": (
                "list: only personas usable on this host (suffix match), e.g. 'github.com'.")},
            "include_runs": {"type": "boolean", "description": (
                "get: include the persona's recent runs (which workflows acted as it, "
                "and whether they succeeded).")},
            "force": {"type": "boolean", "description": (
                "sign_in: re-run the login even when the current session still looks usable.")},
            "login_url": {"type": "string", "description": (
                "record_login: exact sign-in page URL when known; defaults to the "
                "persona's domain root (the AI finds the form from there).")},
        }, "required": ["action"]},
        "_handler": _tool_personas,
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
            "executor": {"type": "string", "description": (
                "regular (default) = deterministic crawl, no AI. ai = every page is read "
                "against `extract_prompt` by the AI provider configured in Settings → AI "
                "(or a connected agent's own keys) — for data with no clean CSS selector. "
                "Refused when no provider is configured.")},
            "extract_prompt": {"type": "string", "description": (
                "Required with executor=ai: what every page should yield, in plain language "
                "(e.g. 'the product name, price and SKU').")},
            "max_depth": {"type": "integer"}, "page_budget": {"type": "integer"},
            "include_paths": {"type": "array", "items": {"type": "string"}},
            "exclude_paths": {"type": "array", "items": {"type": "string"}},
            "same_domain": {"type": "boolean"}, "allow_subdomains": {"type": "boolean"},
            "render_mode": {"type": "string", "description": (
                "How each page is FETCHED — independent of `executor`, which decides who READS "
                "it. auto (default) = plain HTTP first, warm browser only for JS-challenge or "
                "near-empty pages; http = never open a browser (fastest, static HTML); browser = "
                "warm-render every page (JS/SPA sites). executor=ai works on either lane.")},
            "ocr_mode": {"type": "string", "description": "auto (default) | off | force"},
            "intent": {"type": "string", "description": "Plain-English goal; scopes the crawl to matching pages."},
            "persona_id": {"type": "integer", "description": (
                "Saved identity to crawl AS (list them with writ_personas) — for pages "
                "behind a login. The crawl replays the persona's signed-in session; 2FA "
                "is minted server-side.")},
            # These knobs were always FORWARDED to POST /api/crawl (see
            # _CRAWL_CONFIG_KEYS) but never declared, so no MCP client could
            # discover them — the same advertised-vs-honoured drift the cloud
            # schema fixed. Descriptions mirror the cloud connector's.
            "seed_urls": {"type": "array", "items": {"type": "string"}, "description": (
                "Exact pages to start from, when you already know them — the crawl "
                "collects these instead of discovering its own. Cheapest way to scrape "
                "a known set.")},
            "relevance_threshold": {"type": "number", "minimum": 0, "maximum": 1, "description": (
                "0-1. Score every discovered page against `intent` and SKIP anything "
                "below the bar, so a broad crawl collects only what the goal needs "
                "(≈0.3 for 'the pricing and docs pages'). Leave unset for a whole-site "
                "sweep.")},
            "content_spec": {"type": "object", "description": (
                "Which ELEMENTS of each page to keep: {preset: 'full'|'main', "
                "include_comments: bool, exclude_selectors: [css], include_selectors: "
                "[css], keep: {images: bool}}. 'main' = article body only; 'full' = the "
                "whole page INCLUDING comment and discussion threads — use 'full' with "
                "include_comments when the ask mentions comments, replies or "
                "discussion, or they will be stripped out.")},
            "respect_robots": {"type": "boolean", "description": "Honor robots.txt (default true)."},
            "delay_ms": {"type": "integer", "description": "Politeness delay between fetches per host (default 250)."},
            "shard_size": {"type": "integer", "description": "Pages per dispatched shard (default 25)."},
            "max_concurrent_shards": {"type": "integer", "description": (
                "Concurrent shard cap; unset = a conservative default. Your own fleet "
                "size is the real ceiling.")},
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
        "name": "writ_scrape",
        "description": (
            "Scrape ONE page to clean markdown — the single-page twin of writ_crawl_site "
            "when you only need one URL, not a whole site. Pass persona_id to scrape a "
            "page behind a login as that saved identity."
        ),
        "inputSchema": {"type": "object", "properties": {
            "url": {"type": "string", "description": "Page URL (required)."},
            "persona_id": {"type": "integer", "description": (
                "Saved identity to scrape AS (list them with writ_personas) — for a page "
                "behind a login.")},
        }, "required": ["url"]},
        "_handler": _tool_scrape,
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
            "Create an automation: on an EVENT, run a workflow, send a notification, and/or wake an "
            "AI agent. Chain workflows (when workflow A completes → run workflow B), alert on "
            "completion, or have an agent act on the event (`ai_prompt`). "
            "Give a source workflow via `on_workflow` for workflow_* events, and at least one of "
            "`run_workflow` / `notify` / `ai_prompt`."
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
            "ai_prompt": {"type": "string", "description": "Wake an AI agent with this task when the event fires. The agent gets the event context (page URL, diff, extracted values) and works the task on a fleet agent with local AI. Supports {{placeholders}}."},
            "ai_entry_url": {"type": "string", "description": "Page the woken agent starts on. Defaults to the event's page (the monitored URL on change_detected); required in practice for workflow_*/webhook events."},
            "cooldown_minutes": {"type": "integer", "description": "Minimum minutes between AI wakes for `ai_prompt` (0 disables)."},
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
            "for writ_wire_monitor. Omit interval_minutes to use the instance's global check period."
        ),
        "inputSchema": {"type": "object", "properties": {
            "url": {"type": "string", "description": "URL to monitor (required)."},
            "selector": {"type": "string", "description": "CSS selector for content-change monitoring; omit for uptime/status monitoring."},
            "interval_minutes": {"type": "integer", "description": "How often to check, in minutes (minimum 1; omit to use the instance's global check period)."},
            "requires_browser": {"type": "boolean", "description": "Render with a real browser (JS) instead of plain HTTP. Set it for JS-rendered/SPA pages and framed pages (framesets/iframes): the check matches the rendered, frame-flattened DOM, and selector validation is deferred to the first browser render instead of a raw-HTML fetch."},
            "enabled": {"type": "boolean", "description": "Start the monitor enabled (default true)."}}, "required": ["url"]},
        "_handler": _tool_create_monitor,
    },
    {
        "name": "writ_wire_monitor",
        "description": (
            "Wire a monitor's change_detected event to an action. action='workflow' runs a saved "
            "workflow when the monitored page changes; action='notify' sends a notification "
            "(provide `channels` + `recipients` the account has configured); action='ai_task' "
            "WAKES AN AI AGENT with a task `prompt` — a fleet agent with local AI opens the "
            "monitored page, sees what changed (diff + extracted values) and works the prompt "
            "autonomously (add `channels`/`recipients` to also get notified when it finishes). "
            "Use after writ_create_monitor to make the monitor DO something on change."
        ),
        "inputSchema": {"type": "object", "properties": {
            "monitor_id": {"type": "integer", "description": "Monitor id from writ_create_monitor."},
            "action": {"type": "string", "enum": ["workflow", "notify", "ai_task"], "description": "What to do on a detected change."},
            "workflow": {"type": "string", "description": "Workflow to run (name) — required for action='workflow'."},
            "workflow_id": {"type": "integer"},
            "channels": {"type": "array", "items": {"type": "string"}, "description": "Notification channels, e.g. [\"pushover\",\"email\"] — required for action='notify'; optional with action='ai_task' (finish alert)."},
            "recipients": {"type": "array", "items": {"type": "string"}, "description": "Notification recipients, e.g. [\"pushover:1\",\"email:3\"]."},
            "title": {"type": "string"}, "message": {"type": "string", "description": "Notification body template (supports {{event.url}})."},
            "prompt": {"type": "string", "description": "action='ai_task': what the agent should do when the monitor fires, e.g. \"Check whether the price dropped below $500 and summarize what changed\". Supports {{placeholders}} like {{diff_snippet}} and {{extracted.price}}."},
            "entry_url": {"type": "string", "description": "action='ai_task': page the agent starts on (defaults to the monitored URL)."},
            "max_steps": {"type": "integer", "description": "action='ai_task': cap on agent steps per wake (default 20, max 100)."},
            "cooldown_minutes": {"type": "integer", "description": "action='ai_task': minimum minutes between wakes (0 disables)."},
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
            "SECRETS: send any user-supplied value under `inputs` — or on the fill itself with `data_key` — "
            "and it is held server-side: it reaches the page, but comes back to you as its {{placeholder}}, "
            "and the SAVED step keeps the placeholder instead of the value, so the workflow re-substitutes "
            "per run. Never invent a credential; ask the user in chat. "
            "For logins that demand a one-time 2FA code, tell the user to complete that step in the Writ app "
            "(un-guided recording has no server-side code minting)."
        ),
        "inputSchema": {"type": "object", "properties": {
            "session_id": {"type": "string"},
            "inputs": {
                "type": "object",
                "description": (
                    "Values held server-side for {{placeholder}} substitution, e.g. "
                    "{\"city\":\"Paris\"}. Secrets belong here or on a fill's data_key — never "
                    "hardcoded into a step."
                ),
                "additionalProperties": {"type": "string"},
            },
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
                        "data_key": {
                            "type": "string",
                            "description": (
                                "fill: name this value instead of hardcoding it. The value is held "
                                "server-side and the saved step keeps {{data_key}} (or "
                                "{{secret:data_key}} for a credential-looking name)."
                            ),
                        },
                    },
                    "required": ["action"],
                },
            }}, "required": ["session_id", "actions"]},
        "_handler": _tool_record_act,
    },
    {
        "name": "writ_record_context",
        "description": (
            "Read context for a browser session. section=page (default) re-reads the LIVE page — "
            "current URL, visible fields, buttons, page text — without changing it. "
            "section=explorer pages through Writ's full recording policy; section=concierge_api "
            "pages through the API-builder policy. Read the policy before committing to a "
            "workflow shape."
        ),
        "inputSchema": {"type": "object", "properties": {
            "session_id": {"type": "string"},
            "section": {"type": "string", "enum": ["page", "explorer", "concierge_api"],
                        "description": "page (default) | explorer | concierge_api"},
            "offset": {"type": "integer", "minimum": 0, "description": "Paging offset for the policy sections."},
            "max_chars": {"type": "integer", "description": "Characters per page (1000–10000, default 8000)."},
        }, "required": ["session_id"]},
        "_handler": _tool_record_context,
    },
    {
        "name": "writ_record_network",
        "description": (
            "Search or read the API/XHR calls captured while browsing — how you find a site's "
            "real backend instead of scraping its HTML. operation=search lists matching calls "
            "(filter with `query` / `method`); operation=detail returns one call in full by "
            "`index`, including request/response headers and bodies. Indices are stable for the "
            "session, so one from an earlier search still resolves later — only the oldest calls "
            "age out of the retained window, and asking for one of those says so rather than "
            "returning a different call. Held credential values are replaced with their "
            "placeholder in the output."
        ),
        "inputSchema": {"type": "object", "properties": {
            "session_id": {"type": "string"},
            "operation": {"type": "string", "enum": ["search", "detail"],
                          "description": "search (default) | detail. list/get are aliases."},
            "query": {"type": "string", "description": "Substring filter across method, url, status, and bodies."},
            "method": {"type": "string", "description": "Filter by HTTP method."},
            "index": {"type": "integer", "minimum": 0, "description": "Which call to read, for operation=detail."},
            "offset": {"type": "integer", "minimum": 0},
            "max_chars": {"type": "integer"},
        }, "required": ["session_id"]},
        "_handler": _tool_record_network,
    },
    {
        "name": "writ_record_save",
        "description": (
            "Finish a browser session and save everything recorded so far as a runnable "
            "workflow. It then replays on demand with no model in the loop via "
            "writ_run_workflow (pin it with writ_pin_workflow_tool for its own run_<name> "
            "tool), and can be scheduled or exposed as REST. Only save once the task "
            "actually worked on the live page — verify first. Returns the new workflow_id."
        ),
        "inputSchema": {"type": "object", "properties": {
            "session_id": {"type": "string"},
            "name": {"type": "string", "description": "Name for the saved workflow (defaults to the session goal)."},
            "description": {"type": "string"},
            "keep_open": {"type": "boolean", "description": (
                "Leave the browser open after saving (default false — saving closes it).")},
        }, "required": ["session_id"]},
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


def _build_schema(goal_description: str) -> dict:
    """Shared schema for the three workflow-producing start tools."""
    return {"type": "object", "properties": {
        "goal": {"type": "string", "description": goal_description},
        "url": {"type": "string", "description": "Website URL to start on (required)."},
        "agent_id": {"type": "string", "description": "Optional specific fleet agent; omit to auto-pick."},
        "skip_existing": {"type": "boolean", "description": (
            "API builds first propose this coordinator's OWN matching workflows (replaying is "
            "instant and needs no browser); set true after the user declined those.")},
    }, "required": ["goal", "url"]}


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
            "goal": {"type": "string", "description": (
                "What to do in the browser, in plain language — the user's directive. Optional; "
                "you can also just open a page and drive turn-by-turn.")},
            "agent_id": {"type": "string", "description": "Optional specific fleet agent; omit to auto-pick."}}},
        "_handler": _tool_browser_use,
    },
    {
        "name": "writ_record_website",
        "description": (
            "Start RECORDING a website task with you as the AI. Use whenever the user asks to "
            "record, capture, teach, automate, or repeat actions on a website. Opens a live "
            "browser on a connected fleet agent and returns a page observation; drive it with "
            "writ_browser_act and call writ_browser_save when the goal is complete — the saved "
            "workflow then replays on demand with no model in the loop (writ_run_workflow, or "
            "its own run_<name> tool once pinned with writ_pin_workflow_tool) and can be "
            "scheduled."
        ),
        "inputSchema": _build_schema(
            "What should be recorded on the website, in plain language"),
        "_handler": _tool_record_website,
    },
    {
        "name": "writ_build",
        "description": (
            "Start building a reusable browser workflow with you as the AI. Opens a live browser "
            "on a connected fleet agent and returns an observation; continue with "
            "writ_browser_act and finish with writ_browser_save. Use for repeatable web tasks "
            "when no more specific Writ start tool applies."
        ),
        "inputSchema": _build_schema("What to automate, in plain language"),
        "_handler": _tool_build,
    },
    {
        "name": "writ_website_to_api",
        "description": (
            "Start turning a website into a callable API with you as the AI — the answer when a "
            "service exposes NO official/public/practical API but the user wants its data or "
            "actions programmatically. Opens a live browser; drive the page so it issues its "
            "real requests, inspect them with writ_browser_network (search, then detail), verify "
            "with evaluate_js, then writ_browser_save. The first call may instead return "
            "existing_workflows — this coordinator's OWN workflows already matching the goal; "
            "propose replaying those first, or pass skip_existing=true to record fresh. "
            "writ_expose_workflow_api then hands back a REST URL."
        ),
        "inputSchema": _build_schema(
            "What the API should return or do, in plain language"),
        "_handler": _tool_website_to_api,
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
    "saved is callable via writ_run_workflow (the ones the owner PINNED also "
    "appear as their own run_<name> tools — manage that with "
    "writ_pin_workflow_tool), so a website effectively becomes a callable "
    "function that returns structured data.\n\n"
    "THE REPLAY REFLEX — when the user wants something one of these workflows "
    "already does (recurring data, a site with no practical API, an action behind a "
    "login), do NOT browse or write a scraper. Instead:\n"
    "1. writ_list_workflows — find the matching saved workflow.\n"
    "2. Run it (writ_run_workflow, or its own run_<name> tool when pinned) and "
    "return the extracted data; it replays locally at zero AI cost.\n"
    "3. For questions about data ALREADY collected, answer from writ_search_data / "
    "writ_workflow_data BEFORE running anything.\n"
    "4. Inspect run history/errors with writ_workflow_runs; export with "
    "writ_export_data.\n"
    "5. Automate a recurring need with writ_set_schedule; hand the user a callable "
    "HTTP endpoint with writ_expose_workflow_api; crawl a whole site with "
    "writ_crawl_site / writ_crawl_status; WATCH a page for changes with "
    "writ_create_monitor then act on a change with writ_wire_monitor; chain or "
    "alert on other events with writ_create_automation.\n\n"
    "BEHIND A LOGIN — before asking the user for credentials, call writ_personas: it "
    "lists the coordinator's saved sign-in identities. Pass a persona_id to "
    "writ_crawl_site / writ_scrape / writ_run_workflow and the task runs signed in, "
    "with any 2FA code minted server-side; writ_personas also refreshes a persona's "
    "session (sign_in) and teaches one to sign itself in (record_login). Credentials "
    "themselves never transit this connection — personas are created and edited only "
    "in the Writ dashboard.\n\n"
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
    "BUILD a new workflow by RECORDING (un-guided) — the SAME live browser, framed "
    "around producing something reusable. Pick the front door by intent: "
    "writ_record_website (repeat a task on a site), writ_website_to_api (a site with "
    "no practical API — it proposes the owner's own matching workflows before "
    "recording), writ_build (anything else). Each takes a goal + url, opens the "
    "browser, and hands you the page; drive it with writ_browser_act, read the "
    "recording policy page-by-page with writ_browser_context(section=explorer) (or "
    "section=concierge_api for an API build), inspect the site's real backend calls "
    "with writ_browser_network, and finish with writ_browser_save. YOU are the brain "
    "— there is NO guidance or discovery engine here; drive deliberately, confirm "
    "every selector against the observation before relying on it, and ask the USER "
    "directly for any clarification (a value to type, whether to log in, which result "
    "to pick). Pass user-supplied values under `inputs`, or on a fill with `data_key`, "
    "so they are held server-side and the SAVED step keeps a {{placeholder}} rather "
    "than the value — never hardcode a credential into a step, and never invent one. "
    "ALWAYS close a session you are finished with (writ_browser_save or "
    "writ_browser_cancel): an open session holds a real fleet browser. The legacy "
    "writ_record_* names remain as aliases of the same loop.\n\n"
    "ORDER OF PREFERENCE — replay a saved workflow > record a new one > browse ad "
    "hoc. The build tools walk that ladder for you and will propose the cheaper rung "
    "before opening a browser.\n\n"
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
    # Every front door onto a live browser belongs here — a start tool that is
    # merely NAMED differently still seizes a fleet browser.
    _tool_browser_use, _tool_record_website, _tool_build, _tool_website_to_api,
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
                # Derived run_<workflow> tool (pinned) → route to the generic runner.
                rows = await _list_workflows(token)
                for t in _derived_run_tools(rows):
                    if t["name"] == name:
                        handler = _derived_tool_handler(rows, t["_workflow_id"])
                        break
                if handler is None and name.startswith("run_"):
                    # Stale-name fallback: the caller's tool list predates an
                    # unpin/rename (or the opt-in flip itself). Resolve by exact
                    # slug over ALL workflows so the cached name keeps working.
                    matches = _match_run_tool_name(rows, name)
                    if len(matches) == 1:
                        handler = _derived_tool_handler(rows, matches[0]["id"])
                    elif len(matches) > 1:
                        opts = "; ".join(
                            f"“{w.get('name')}” (workflow_id {w.get('id')})" for w in matches[:6])
                        return _ok(req_id, _content(
                            f"Tool name {name!r} matches several saved workflows: {opts}. "
                            "Call writ_run_workflow with the workflow_id instead.",
                            is_error=True))

            if handler is None:
                if name.startswith("run_"):
                    # Guidance, not a bare protocol error: the name plausibly came
                    # from a cached tool list, and the caller's next move matters.
                    return _ok(req_id, _content(
                        f"No tool or saved workflow matches {name!r}. Saved workflows appear "
                        "as run_<name> tools only when pinned (writ_pin_workflow_tool); every "
                        "workflow runs via writ_run_workflow — find it with writ_list_workflows.",
                        is_error=True))
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
