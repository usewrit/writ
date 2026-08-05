"""
MCP record-build sessions — UN-GUIDED browser recording driven by the connected
MCP client.

The connected client (Claude, via the writ-mcp connector) is the brain: it
decides each browser action itself and asks the user for clarifications directly
in the chat. The coordinator provides only the mechanism — it opens a live record
session on a fleet agent, relays the client's ``act`` calls (which the agent
executes AND records as workflow steps), accumulates the recorded steps + page
observation + captured API calls, and finally persists them as a workflow.

There is NO coordinator-side AI, no discovery/mission guidance, and no autonomous
loop. This is the ``writ_record_*`` / ``writ_browser_*`` tool family — the
self-host analogue of the desktop app's record-a-website build tools, un-guided.

State is IN-PROCESS here, unlike Writ Cloud's DB-backed twin: a coordinator is a
single process owning its own fleet sockets, so a dict is the honest
representation. What it does share with cloud is the surface contract — action
vocabulary, ``inputs``/``data_key`` credential holding, policy paging, stable
network indices — so one connector behaves the same against either.

Wire protocol (confirmed against the Rust fleet agent, ``saas_bridge.rs``):
  → ``{type:"start", url, options:{record_wait_steps, capture_api}}``  begin recording
  → ``{type:"agent_action", request_id, actions:[…]}``                 act (records steps)
  ← ``{type:"agent_action_result", request_id, results, observation}`` per-act reply
  ← ``{type:"step_recorded", step:{…}}`` / ``{type:"step_updated", id, step}``
  ← ``{type:"navigation", url}`` / ``{type:"api_captured", call:{…}}``
Each recorded ``step`` maps directly onto the coordinator's ``WorkflowStepCreate``.

SECURITY: a value the client sends under ``inputs`` — or on a ``fill`` carrying a
``data_key`` — is held HERE, never echoed back. It is replaced by its placeholder
in captured network detail (so the model cannot read a credential back out of a
request it just made) and in the saved steps (so the workflow re-substitutes per
run instead of baking the value in).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Bound the number of concurrent record sessions so a client that starts sessions
# and never saves can't pin fleet browsers.
_MAX_SESSIONS = 8

# A connected session is driven by a remote model; between two tool calls there
# may be a long think/user pause. Reclaim on IDLENESS (not age — a session being
# actively driven must never be pulled out from under the client), with a hard
# lifetime ceiling as a backstop.
_IDLE_TTL_S = 60 * 30
_MAX_LIFETIME_S = 60 * 60 * 4

# Retained captured calls, and the per-body character cap applied before storage.
_NETWORK_MAX_CALLS = 150
_NETWORK_BODY_CHARS = 8_000


class RecordError(Exception):
    """A caller-safe record-session error (surfaced as an MCP tool error)."""


# Session modes. `use` is the task-oriented "Writ is your browser" front door
# where saving is on demand; `record` and `api` exist to produce a saved workflow.
MODE_USE = "use"
MODE_RECORD = "record"
MODE_API = "api"

# Per-mode guidance folded into every observation so the connected client keeps
# the right posture as it drives.
_USE_NEXT = (
    "Writ is your browser. Drive the task with writ_browser_act "
    "(navigate/click/fill/select/press_key/scroll/evaluate_js/read_text/extract_data/…); "
    "the page observation returns each turn and on demand via writ_browser_context, and "
    "captured API/XHR calls via writ_browser_network. Ask the user in chat for any decision, "
    "value, credential, or 2FA code — never guess (send a sensitive value under `inputs`, or "
    "on the fill with `data_key`, so the saved step keeps a placeholder). You do NOT have to "
    "save — just finish the task. Only if the user wants to REUSE it, call writ_browser_save "
    "to store a clean, replayable workflow (zero-AI-cost replay); writ_browser_cancel closes "
    "the browser without saving."
)
_RECORD_NEXT = (
    "You are the brain: look at the observation, decide the next actions, and call "
    "writ_browser_act again. Your structured interactions are recorded as reusable steps. "
    "The full recording policy is available page-by-page through "
    "writ_browser_context(section=explorer). Confirm every selector on the LIVE page before "
    "relying on it. Ask the user directly for any clarification, and pass sensitive values "
    "under `inputs` / `data_key` so they never land in a saved step. Finish with "
    "writ_browser_save to persist the workflow, or writ_browser_cancel to discard."
)
_API_NEXT = (
    "Goal: a callable API, not a click-through. Read the API-builder policy with "
    "writ_browser_context(section=concierge_api) first. Drive the page so it issues the "
    "requests you care about, then inspect them with writ_browser_network "
    "(operation=search, then operation=detail on the interesting index) — a matching JSON "
    "endpoint usually beats scraping the DOM. VERIFY with evaluate_js before you commit. "
    "Then writ_browser_save, and writ_expose_workflow_api to hand back a REST URL."
)
_NEXT_BY_MODE = {MODE_USE: _USE_NEXT, MODE_RECORD: _RECORD_NEXT, MODE_API: _API_NEXT}


def _sensitive_key(key: str) -> bool:
    k = (key or "").lower()
    return any(
        marker in k for marker in
        ("password", "passwd", "secret", "token", "api_key", "apikey", "credential", "pin", "otp")
    )


class RecordSession:
    def __init__(self, session_id: str, agent_id: str, entry_url: str, mode: str = MODE_RECORD,
                 goal: str = ""):
        self.session_id = session_id
        self.agent_id = agent_id
        self.entry_url = entry_url
        self.current_url = entry_url
        self.mode = mode
        self.goal = goal
        self.steps: list[dict] = []
        # Captured calls, de-duplicated and bounded. `network_base` is the ABSOLUTE
        # index of network[0] so an index handed out by a search keeps meaning the
        # same call after the window slides (see _ingest).
        self.network: list[dict] = []
        self.network_base = 0
        self.network_seen: list[str] = []
        self.observation = None
        # Values the client handed us for {{placeholder}} substitution. Held here,
        # never echoed back — see _placeholders / _scrub.
        self.fill_data: dict[str, str] = {}
        self.secret_refs: dict[str, str] = {}
        self.created = time.time()
        self.last_used = time.time()
        self.status = "recording"  # recording | closed
        # Serialize concurrent tool calls against ONE session: two interleaved acts
        # would race on both the live page and the accumulated state.
        self.lock = asyncio.Lock()

    def touch(self) -> None:
        self.last_used = time.time()

    def view(self, **extra) -> dict:
        out = {
            "session_id": self.session_id,
            "status": self.status,
            "mode": "browser_use" if self.mode == MODE_USE else self.mode,
            "url": self.current_url,
            "recorded_steps": len(self.steps),
            "next": _NEXT_BY_MODE.get(self.mode, _RECORD_NEXT),
        }
        out.update(extra)
        return out

    def placeholders(self) -> dict:
        """``{raw held value: placeholder}``.

        Each value is held under both the bare and ``input.``-prefixed name so both
        spellings resolve at run time; for OUTPUT we want exactly ONE canonical
        replacement, so the prefixed alias is dropped and a sensitive key resolves
        to its ``{{secret:…}}`` reference. Deterministic regardless of dict order.
        """
        out: dict = {}
        for key, value in self.fill_data.items():
            if key.startswith("input.") or not isinstance(value, str) or len(value) < 3:
                continue
            out[value] = self.secret_refs.get(key) or "{{" + key + "}}"
        return out

    def hold(self, key: str, value: str) -> None:
        """Hold one caller-supplied value under both placeholder spellings."""
        bare = key[len("input."):] if key.startswith("input.") else key
        bare = bare.strip()
        if not bare or not isinstance(value, str) or not value:
            return
        self.fill_data[bare] = value
        self.fill_data[f"input.{bare}"] = value
        if _sensitive_key(bare):
            safe = "".join(c for c in bare if c.isalnum() or c == "_")
            self.secret_refs[bare] = f"{{{{secret:{safe}}}}}"


_SESSIONS: dict[str, RecordSession] = {}


def _ws():
    # Lazy import to avoid any router<->service import-order coupling.
    from routers import user_recorder_ws as ws
    return ws


def _scrub(value: Any, placeholders: dict) -> Any:
    """Replace held values with their placeholder anywhere in a payload.

    A credential the client sent must not come back out of a captured request —
    the model would then be holding it in context — and must never be baked into a
    saved step. Longest match first so a short value that is a substring of a
    longer one (a username inside its own email address) cannot shadow it.
    """
    if not placeholders:
        return value
    if isinstance(value, str):
        out = value
        for raw in sorted(placeholders, key=len, reverse=True):
            if raw in out:
                out = out.replace(raw, placeholders[raw])
        return out
    if isinstance(value, dict):
        return {k: _scrub(v, placeholders) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, placeholders) for v in value]
    return value


def _trim_call(call: dict) -> dict:
    """Bound one captured call before it is retained."""
    out = {}
    for k, v in (call or {}).items():
        if isinstance(v, str) and len(v) > _NETWORK_BODY_CHARS:
            out[k] = v[:_NETWORK_BODY_CHARS] + "…[truncated]"
        else:
            out[k] = v
    return out


def _call_key(call: dict) -> str:
    return f"{call.get('method')}|{call.get('url')}|{call.get('response_status')}"


def _record_call(sess: RecordSession, call: dict) -> None:
    """Retain one captured call — de-duplicated, bounded, absolutely indexed.

    The seen-key ledger outlives the retained window on purpose: without it a call
    the cap already evicted would be re-added the next time the agent reports it,
    churning the list and silently shifting every index a previous search handed
    out. ``network_base`` records how many have been evicted, so a reported index
    stays valid for the life of the session.
    """
    if not isinstance(call, dict) or not call.get("url"):
        return
    key = _call_key(call)
    if key in sess.network_seen:
        return
    sess.network_seen.append(key)
    sess.network.append(_trim_call(call))
    dropped = max(0, len(sess.network) - _NETWORK_MAX_CALLS)
    if dropped:
        sess.network = sess.network[dropped:]
        sess.network_base += dropped
    if len(sess.network_seen) > _NETWORK_MAX_CALLS * 4:
        sess.network_seen = sess.network_seen[-(_NETWORK_MAX_CALLS * 4):]


def _ingest(sess: RecordSession, frame: dict) -> None:
    """Fold one agent session frame into the accumulated session state."""
    t = frame.get("type")
    if t == "step_recorded":
        step = frame.get("step")
        if isinstance(step, dict):
            sess.steps.append(step)
    elif t == "step_updated":
        step = frame.get("step")
        sid = frame.get("id") or (step.get("id") if isinstance(step, dict) else None)
        if isinstance(step, dict):
            for i, s in enumerate(sess.steps):
                if s.get("id") == sid:
                    sess.steps[i] = step
                    break
            else:
                sess.steps.append(step)
    elif t == "step_removed":
        sid = frame.get("id")
        sess.steps = [s for s in sess.steps if s.get("id") != sid]
    elif t == "navigation":
        u = frame.get("url")
        if u:
            sess.current_url = u
    elif t in ("network", "network_call", "api_captured", "api_endpoint"):
        # The agent sends the full NetworkCall under `call` (saas_bridge.rs);
        # tolerate a flat frame from an older build.
        call = frame.get("call")
        _record_call(sess, call if isinstance(call, dict) else frame)


async def _drain_until(
    sess: RecordSession, match_type: str, request_id: Optional[str], timeout: float
) -> Optional[dict]:
    """Read agent frames — folding recorded steps/nav/network as they arrive —
    until a frame of ``match_type`` (and ``request_id`` if given). Returns that
    frame, or None on timeout / session close."""
    ws = _ws()
    while True:
        frame = await ws.server_record_recv(sess.session_id, timeout=timeout)
        if frame is None:
            return None
        if frame.get("type") == "session_closed":
            sess.status = "closed"
            return None
        _ingest(sess, frame)
        if frame.get("type") == match_type and (
            request_id is None or frame.get("request_id") == request_id
        ):
            return frame


async def _drain_trailing(sess: RecordSession, idle: float = 1.5) -> None:
    """Fold any frames already queued (e.g. steps that trailed the last reply)
    until the queue goes idle for ``idle`` seconds."""
    ws = _ws()
    while True:
        frame = await ws.server_record_recv(sess.session_id, timeout=idle)
        if frame is None:
            return
        if frame.get("type") == "session_closed":
            sess.status = "closed"
            return
        _ingest(sess, frame)


async def _observe(sess: RecordSession, timeout: float = 45.0):
    """Fetch a fresh page observation via an empty agent_action (no page change)."""
    ws = _ws()
    req = uuid.uuid4().hex
    await ws.server_record_send(
        sess.session_id, {"type": "agent_action", "request_id": req, "actions": []}
    )
    res = await _drain_until(sess, "agent_action_result", req, timeout)
    if res is not None:
        sess.observation = res.get("observation")
    return sess.observation


# ── Reclaiming abandoned sessions ────────────────────────────────────────────

async def reap_stale() -> int:
    """Close sessions idle past ``_IDLE_TTL_S`` or older than ``_MAX_LIFETIME_S``.

    An abandoned session holds a real fleet browser open, so it must actually be
    CLOSED — dropping the dict entry alone would leak the agent-side session for
    the life of the process. Idleness (not age) is the trigger: a session being
    actively driven must never be pulled out from under the client mid-task.

    Called from ``start()`` and from the coordinator's background scheduler.
    """
    now = time.time()
    stale = [
        sid for sid, s in list(_SESSIONS.items())
        if s.status != "recording"
        or now - s.last_used > _IDLE_TTL_S
        or now - s.created > _MAX_LIFETIME_S
    ]
    closed = 0
    for sid in stale:
        try:
            await cancel(sid)
            closed += 1
        except Exception as e:  # noqa: BLE001 — housekeeping is best-effort
            logger.warning(f"[mcp_record {sid}] reap failed: {e}")
    if closed:
        logger.info(f"Reclaimed {closed} abandoned browser session(s)")
    return closed


# ── Public API (called by the writ_record_* / writ_browser_* MCP tools) ──────

async def start(
    url: str, preferred_agent: Optional[str] = None, mode: str = MODE_RECORD, goal: str = "",
) -> dict:
    """Open a live browser session on a fleet agent and navigate to ``url``.

    ``mode='record'`` frames it as building a workflow, ``mode='api'`` as turning
    the site into a callable API, ``mode='use'`` as doing a task through Writ's
    browser where saving is on demand. The mechanism is identical — every
    interaction is recorded either way, so a ``use`` session can still be saved as
    a clean workflow whenever the user wants one.
    """
    await reap_stale()
    if not url or not isinstance(url, str):
        raise RecordError("A `url` to open is required.")
    if len([s for s in _SESSIONS.values() if s.status == "recording"]) >= _MAX_SESSIONS:
        raise RecordError("Too many active browser sessions — save or cancel one first.")
    ws = _ws()
    opened = await ws.open_server_record_session(preferred_agent)
    if not opened:
        raise RecordError(
            "No fleet agent is connected to open a browser on. Connect a Writ agent "
            "(Fleet page) and try again."
        )
    session_id, agent_id = opened
    sess = RecordSession(session_id, agent_id, url, mode=mode, goal=goal)
    _SESSIONS[session_id] = sess
    await ws.server_record_send(
        session_id,
        {"type": "start", "url": url, "options": {"record_wait_steps": True, "capture_api": True}},
    )
    obs = await _observe(sess)
    sess.touch()
    return sess.view(observation=obs)


def _get(session_id: str) -> RecordSession:
    sess = _SESSIONS.get(session_id)
    if not sess:
        raise RecordError(
            "Unknown browser session — start one with writ_browser_use, "
            "writ_record_website, writ_build or writ_website_to_api."
        )
    ws = _ws()
    if sess.status != "recording" or not ws.server_record_is_open(session_id):
        raise RecordError("This browser session has closed. Start a new one.")
    return sess


def _reject_action(action: dict) -> Optional[str]:
    """Refuse an action that would fabricate a credential (desktop parity).

    A ``fill`` whose ``data_key`` names a credential must carry a real value the
    client obtained from the user; the saved step keeps only the placeholder.
    """
    kind = action.get("action") or action.get("type")
    if kind != "fill":
        return None
    data_key = str(action.get("data_key") or "").strip()
    if data_key and _sensitive_key(data_key) and not action.get("value"):
        return (
            f"fill for '{data_key}' carries no value — ask the user for the credential "
            "in chat and resend it with data_key set; never invent one."
        )
    return None


async def act(session_id: str, actions: list, inputs: Optional[dict] = None) -> dict:
    """Perform one batch of browser actions in the session. Structured
    interactions (navigate/click/fill/select/press_key/…) are recorded as steps.

    Values passed under ``inputs``, or on a ``fill`` with ``data_key``, are HELD
    here: they reach the page, but come back out of results/observation/network as
    their ``{{placeholder}}``, and the saved step keeps the placeholder too.
    """
    sess = _get(session_id)
    if not isinstance(actions, list) or not actions:
        raise RecordError("`actions` must be a non-empty list of action objects.")

    async with sess.lock:
        # Re-check under the lock: a concurrent cancel may have closed it.
        sess = _get(session_id)
        for key, value in (inputs or {}).items():
            if isinstance(value, str):
                sess.hold(key, value)

        dispatch: list = []
        for action in actions:
            if not isinstance(action, dict):
                raise RecordError("Each action must be an object.")
            reason = _reject_action(action)
            if reason:
                raise RecordError(reason)
            data_key = str(action.get("data_key") or "").strip()
            value = action.get("value")
            if (action.get("action") or action.get("type")) == "fill" and data_key \
                    and isinstance(value, str) and value:
                sess.hold(data_key, value)
            dispatch.append(action)

        ws = _ws()
        req = uuid.uuid4().hex
        sent = await ws.server_record_send(
            session_id, {"type": "agent_action", "request_id": req, "actions": dispatch}
        )
        if not sent:
            raise RecordError("The recording agent is no longer reachable.")
        res = await _drain_until(sess, "agent_action_result", req, timeout=120.0)
        if res is None:
            raise RecordError("The recording agent did not respond (timed out or closed).")
        sess.observation = res.get("observation")
        sess.touch()

        # Built AFTER this call's values landed, so a credential sent on THIS call
        # is already scrubbed out of the results it produced.
        ph = sess.placeholders()
        return sess.view(
            results=_scrub(res.get("results"), ph),
            observation=_scrub(sess.observation, ph),
        )


# ── context ─────────────────────────────────────────────────────────────────

def _policy_text(section: str) -> str:
    """The recording policy the connected client should follow — the SAME prompt
    text this coordinator's own brain runs on, so an un-guided session and a
    guided one record to identical conventions."""
    from services import agent_brain

    if section == "explorer":
        return agent_brain.AGENT_BASE + "\n\n" + agent_brain.AGENT_MODE_PROMPTS["manual"]
    if section == "concierge_api":
        return agent_brain.AGENT_BASE + "\n\n" + agent_brain.AGENT_MODE_PROMPTS["api"]
    raise RecordError("section must be page, explorer or concierge_api.")


async def context(
    session_id: str, section: str = "page", offset: int = 0, max_chars: int = 8000,
) -> dict:
    """Read the live page (``page``) or a page of the recording policy
    (``explorer`` / ``concierge_api``)."""
    sess = _get(session_id)
    if section == "page":
        async with sess.lock:
            sess = _get(session_id)
            obs = await _observe(sess)
            sess.touch()
            return sess.view(section="page", observation=_scrub(obs, sess.placeholders()))

    source = _policy_text(section)
    offset = max(0, int(offset or 0))
    span = min(max(int(max_chars or 8000), 1000), 10000)
    end = min(offset + span, len(source))
    sess.touch()
    return {
        "session_id": session_id,
        "section": section,
        "offset": offset,
        "end": end,
        "total_chars": len(source),
        "content": source[min(offset, len(source)):end],
        "has_more": end < len(source),
        "next_offset": end if end < len(source) else None,
    }


# ── network ─────────────────────────────────────────────────────────────────

async def network(
    session_id: str, operation: str = "search", query: str = "",
    method: Optional[str] = None, index: Optional[int] = None,
    offset: int = 0, max_chars: int = 8000,
) -> dict:
    """Search or read the requests the live page has made.

    Capture is passive on the agent side and arrives as the session runs.
    ``operation=search`` lists matching calls; ``operation=detail`` returns one in
    full by its (stable, absolute) ``index``. Held credential values come back as
    their placeholder.
    """
    sess = _get(session_id)
    await _drain_trailing(sess, idle=0.5)
    sess.touch()

    operation = {"list": "search", "get": "detail"}.get(operation, operation)
    if operation not in ("search", "detail"):
        raise RecordError("operation must be search or detail (list/get are aliases).")

    calls = sess.network
    base = sess.network_base
    needle = (query or "").strip().lower()
    want_method = (method or "").strip().upper() or None
    matched: list[tuple[int, dict]] = []
    for i, call in enumerate(calls):
        haystack = " ".join(
            str(call.get(k) or "") for k in
            ("method", "url", "response_status", "request_content_type",
             "request_body", "response_body")
        ).lower()
        if needle and needle not in haystack:
            continue
        if want_method and str(call.get("method") or "").upper() != want_method:
            continue
        matched.append((base + i, call))

    if operation == "search":
        return {
            "session_id": session_id,
            "operation": "search",
            "captured": base + len(calls),
            "retained": len(calls),
            "matched": len(matched),
            "calls": [{
                "index": i,
                "method": c.get("method"),
                "url": c.get("url"),
                "status": c.get("response_status"),
                "resource_type": c.get("resource_type"),
                "request_content_type": c.get("request_content_type"),
                "response_content_type": c.get("response_content_type"),
                "request_body_chars": len(str(c.get("request_body") or "")),
                "response_body_chars": len(str(c.get("response_body") or "")),
            } for i, c in matched[-100:]],
            "next": (
                "Pick the relevant call and read it with operation=detail and its index."
                if matched else
                "Nothing captured yet — drive the page (navigate/click) so it issues its "
                "requests, then search again."
            ),
        }

    if index is not None:
        position = int(index) - base
        if position < 0:
            raise RecordError(
                f"Network call {index} has aged out of this session's retained capture "
                f"(the most recent {_NETWORK_MAX_CALLS} are kept). Search again for a "
                "current index."
            )
        if position >= len(calls):
            raise RecordError(f"Network call index {index} does not exist.")
        chosen_index, chosen = int(index), calls[position]
    elif matched:
        chosen_index, chosen = matched[-1]
    else:
        raise RecordError("No matching network call — search first, or pass a valid index.")

    detail = _scrub(chosen, sess.placeholders())
    rendered = json.dumps(detail, default=str)
    offset = max(0, int(offset or 0))
    span = min(max(int(max_chars or 8000), 1000), 10000)
    start = min(offset, len(rendered))
    end = min(start + span, len(rendered))
    complete = start == 0 and end == len(rendered)
    return {
        "session_id": session_id,
        "operation": "detail",
        "index": chosen_index,
        "matched": len(matched),
        "offset": start,
        "end": end,
        "total_chars": len(rendered),
        "network_call": detail if complete else None,
        "content_page": None if complete else rendered[start:end],
        "has_more": end < len(rendered),
        "next_offset": end if end < len(rendered) else None,
    }


# ── save / cancel ───────────────────────────────────────────────────────────

async def finalize(session_id: str) -> dict:
    """Collect the recorded steps (folding any trailing frames) for persistence.

    Held values are replaced by their placeholder so the saved workflow
    re-substitutes per run — a credential is never baked into a step. Does NOT
    close the session: the caller closes it after a successful save, so a failed
    save can be retried.
    """
    sess = _get(session_id)
    async with sess.lock:
        sess = _get(session_id)
        await _drain_trailing(sess, idle=1.5)
        sess.touch()
        if not sess.steps:
            raise RecordError(
                "Nothing was recorded yet — act in the session (navigate/click/fill/…) "
                "before saving."
            )
        return {
            "entry_url": sess.entry_url,
            "steps": _scrub(list(sess.steps), sess.placeholders()),
            "goal": sess.goal,
            "mode": sess.mode,
        }


async def cancel(session_id: str) -> dict:
    sess = _SESSIONS.pop(session_id, None)
    if sess:
        sess.status = "closed"
    ws = _ws()
    await ws.close_server_record_session(session_id)
    return {"session_id": session_id, "status": "cancelled"}


def session_goal(session_id: str) -> str:
    """The goal a session was started with (used to name a save that omits one)."""
    sess = _SESSIONS.get(session_id)
    return (sess.goal if sess else "") or ""
