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
loop. This is the ``writ_record_*`` tool family — the self-host analogue of the
desktop app's record-a-website build tools, but un-guided.

Wire protocol (confirmed against the Rust fleet agent, ``saas_bridge.rs``):
  → ``{type:"start", url, options:{record_wait_steps, capture_api}}``  begin recording
  → ``{type:"agent_action", request_id, actions:[…]}``                 act (records steps)
  ← ``{type:"agent_action_result", request_id, results, observation}`` per-act reply
  ← ``{type:"step_recorded", step:{…}}`` / ``{type:"step_updated", id, step}``
  ← ``{type:"navigation", url}`` / network-capture frames
Each recorded ``step`` maps directly onto the coordinator's ``WorkflowStepCreate``.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

# Bound the number of concurrent record sessions and their lifetime so a client
# that starts sessions and never saves can't pin fleet browsers forever.
_MAX_SESSIONS = 8
_SESSION_TTL_S = 60 * 30


class RecordError(Exception):
    """A caller-safe record-session error (surfaced as an MCP tool error)."""


# Per-mode guidance folded into every observation so the connected client keeps
# the right posture as it drives — task-oriented + save-on-demand for a
# `writ_browser_use` session, build-a-workflow for a `writ_record_start` one.
_USE_NEXT = (
    "Writ is your browser. Drive the task with writ_browser_act "
    "(navigate/click/fill/select/press_key/scroll/evaluate_js/read_text/extract_data/…); "
    "the page observation returns each turn and on demand via writ_browser_context, and "
    "captured API/XHR calls via writ_browser_network. Ask the user in chat for any decision, "
    "value, credential, or 2FA code — never guess. You do NOT have to save — just finish the "
    "task. Only if the user wants to REUSE it, call writ_browser_save to store a clean, "
    "replayable workflow (zero-AI-cost replay); writ_browser_cancel closes the browser without saving."
)
_RECORD_NEXT = (
    "You are the brain: look at the observation, decide the next actions, and call "
    "writ_record_act again. Your structured interactions are recorded as reusable steps. "
    "Ask the user directly for any clarification. Finish with writ_record_save to persist the "
    "workflow, or writ_record_cancel to discard."
)


class RecordSession:
    def __init__(self, session_id: str, agent_id: str, entry_url: str, mode: str = "record"):
        self.session_id = session_id
        self.agent_id = agent_id
        self.entry_url = entry_url
        self.current_url = entry_url
        self.mode = mode  # "record" (build a workflow) | "use" (do a task, save on demand)
        self.steps: list[dict] = []
        self.network: list[dict] = []
        self.observation = None
        self.created = time.time()
        self.status = "recording"  # recording | closed

    def view(self, **extra) -> dict:
        out = {
            "session_id": self.session_id,
            "status": self.status,
            "mode": "browser_use" if self.mode == "use" else "record",
            "url": self.current_url,
            "recorded_steps": len(self.steps),
            "next": _USE_NEXT if self.mode == "use" else _RECORD_NEXT,
        }
        out.update(extra)
        return out


_SESSIONS: dict[str, RecordSession] = {}


def _ws():
    # Lazy import to avoid any router<->service import-order coupling.
    from routers import user_recorder_ws as ws
    return ws


def _gc() -> None:
    now = time.time()
    for sid, s in list(_SESSIONS.items()):
        if now - s.created > _SESSION_TTL_S:
            _SESSIONS.pop(sid, None)


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
        sess.network.append(frame)


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


# ── Public API (called by the writ_record_* MCP tools) ───────────────────────

async def start(url: str, preferred_agent: Optional[str] = None, mode: str = "record") -> dict:
    """Open a live browser session on a fleet agent and navigate to ``url``.

    ``mode='record'`` frames it as building a workflow (writ_record_*); ``mode='use'``
    frames it as doing a task through Writ's browser (writ_browser_use) where saving is
    on demand. The mechanism is identical — every interaction is recorded either way, so a
    ``use`` session can still be saved as a clean workflow whenever the user wants one.
    """
    _gc()
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
    sess = RecordSession(session_id, agent_id, url, mode=mode)
    _SESSIONS[session_id] = sess
    await ws.server_record_send(
        session_id,
        {"type": "start", "url": url, "options": {"record_wait_steps": True, "capture_api": True}},
    )
    obs = await _observe(sess)
    return sess.view(observation=obs)


def _get(session_id: str) -> RecordSession:
    sess = _SESSIONS.get(session_id)
    if not sess:
        raise RecordError("Unknown record session — start one with writ_record_start.")
    ws = _ws()
    if sess.status != "recording" or not ws.server_record_is_open(session_id):
        raise RecordError("This record session has closed. Start a new one.")
    return sess


async def act(session_id: str, actions: list) -> dict:
    """Perform one batch of browser actions in the record session. Structured
    interactions (navigate/click/fill/select/press_key/…) are recorded as steps."""
    sess = _get(session_id)
    if not isinstance(actions, list) or not actions:
        raise RecordError("`actions` must be a non-empty list of action objects.")
    ws = _ws()
    req = uuid.uuid4().hex
    sent = await ws.server_record_send(
        session_id, {"type": "agent_action", "request_id": req, "actions": actions}
    )
    if not sent:
        raise RecordError("The recording agent is no longer reachable.")
    res = await _drain_until(sess, "agent_action_result", req, timeout=120.0)
    if res is None:
        raise RecordError("The recording agent did not respond (timed out or closed).")
    sess.observation = res.get("observation")
    return sess.view(results=res.get("results"), observation=sess.observation)


async def context(session_id: str) -> dict:
    sess = _get(session_id)
    obs = await _observe(sess)
    return sess.view(observation=obs)


async def network(session_id: str) -> dict:
    sess = _get(session_id)
    await _drain_trailing(sess, idle=0.5)
    return {"session_id": session_id, "captured_api_calls": sess.network[-40:]}


async def finalize(session_id: str) -> dict:
    """Collect the recorded steps (folding any trailing frames) for persistence.
    Does NOT close the session — the caller closes it after a successful save so a
    failed save can be retried."""
    sess = _get(session_id)
    await _drain_trailing(sess, idle=1.5)
    if not sess.steps:
        raise RecordError(
            "Nothing was recorded yet — act in the session (navigate/click/fill/…) "
            "before saving."
        )
    return {"entry_url": sess.entry_url, "steps": list(sess.steps)}


async def cancel(session_id: str) -> dict:
    sess = _SESSIONS.pop(session_id, None)
    if sess:
        sess.status = "closed"
    ws = _ws()
    await ws.close_server_record_session(session_id)
    return {"session_id": session_id, "status": "cancelled"}
