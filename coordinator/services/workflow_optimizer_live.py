"""Live workflow optimizer (self-host coordinator) — coordinator twin of the cloud
`services.workflow_optimizer_live`. Replays the workflow on a fleet agent WITH network capture,
runs one OPTIMIZE_LIVE pass over the real trace, trace-verifies each proposed substitution, and
returns the diff. The coordinator is single-user (no tenant scoping); the fleet agent (Rust) reveals
held credentials as {{placeholders}} before returning the trace.

The pure logic (risky gate, trace verify, deterministic assemble, OPTIMIZE_LIVE prompt) is identical
to the cloud service; only the workflow load, the AI call signature, and the replay dispatch differ.
"""
from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_RISKY_TERMS = (
    "buy", "order", "checkout", "pay", "purchase", "submit", "delete", "remove", "send", "transfer",
    "confirm", "cart",
)
_AUTH_URL_HINTS = ("login", "auth", "token", "session", "signin", "sign-in")


def risky_side_effect(steps: list) -> Optional[str]:
    hits: list = []

    def note(h: str) -> None:
        if h not in hits:
            hits.append(h)

    for s in steps or []:
        if not isinstance(s, dict):
            continue
        ty = s.get("type") or ""
        cfg = s.get("config") or {}
        if ty in ("click", "submit", "check"):
            hay = f"{cfg.get('selector') or ''} {cfg.get('text') or ''}".lower()
            for t in _RISKY_TERMS:
                if t in hay:
                    note(t)
        elif ty == "navigate":
            url = str(cfg.get("url") or "").lower()
            for t in ("checkout", "cart", "payment", "order"):
                if t in url:
                    note(t)
        elif ty == "api_call":
            method = str(cfg.get("method") or "GET").upper()
            url = str(cfg.get("url") or "").lower()
            if method in ("POST", "PUT", "PATCH", "DELETE") and not any(a in url for a in _AUTH_URL_HINTS):
                note("a data-changing API call")
    return ", ".join(hits) if hits else None


OPTIMIZE_LIVE_SYSTEM = """You are an expert browser-automation engineer. You are given a recorded workflow's STEPS (0-indexed) and the REAL backend API calls it made when it was just replayed in a live browser (any credentials you hold appear as {{placeholders}}). Propose how to make the workflow more robust by replacing fragile DOM steps with direct API calls and by removing dead steps. You do NOT rewrite the steps yourself — you emit structured PROPOSALS and the system verifies + applies them.

1) SUBSTITUTIONS — fold a run of DOM steps that produced a captured call into ONE request step:
   - A data read -> an "api_call" step. The SIGN-IN (a captured login POST whose BODY shows your held credentials as {{placeholders}}, NO csrf/nonce/authenticity token you don't hold) -> a "login_post" step.
   {"replace_indices":[i,j,...],"with":{"type":"api_call"|"login_post","config":{"url":"<exact URL>","method":"GET|POST","headers":{...with {{placeholders}}...},"body_template":"<exact {{placeholder}} body>","variable":"snake_case_name"}},"description":"...","reason":"...","risk":"safe|caution|high"}
   - Copy url/method/Content-Type/body EXACTLY as the trace shows. Only propose when a captured call clearly corresponds.
2) REMOVALS — dead steps: {"indices":[k,...],"reason":"...","risk":"safe|caution|high"}

HARD RULES: replace_indices contiguous; NEVER fold/remove navigate/extract/evaluate/return; NEVER login_post when the body has an unheld token; no overlapping indices.

Return ONLY JSON: {"substitutions":[...],"removals":[...],"warnings":[...]}"""


def _norm_url(url: str) -> str:
    try:
        p = urlparse(url or "")
        return f"{p.scheme}://{p.netloc}{p.path}".rstrip("/").lower()
    except Exception:
        return (url or "").split("?")[0].rstrip("/").lower()


def _trace_confirms(cfg: dict, network_calls: list) -> bool:
    want_url = _norm_url(cfg.get("url") or "")
    want_method = str(cfg.get("method") or "GET").upper()
    if not want_url:
        return False
    for c in network_calls or []:
        if not isinstance(c, dict):
            continue
        if str(c.get("method") or "").upper() != want_method:
            continue
        if _norm_url(c.get("url") or "") != want_url:
            continue
        try:
            status = int(c.get("response_status"))
        except (TypeError, ValueError):
            status = 0
        return 200 <= status < 400
    return False


def assemble_optimized(original: list, proposal: dict, network_calls: list) -> tuple:
    from services.agent_brain import prune_navigates_before_api_only

    n = len(original)
    slots = [("keep",) for _ in range(n)]
    assigned = [False] * n
    changes: list = []
    warnings: list = []

    subs = proposal.get("substitutions") if isinstance(proposal, dict) else None
    for sub in sorted(subs or [], key=lambda s: (s.get("replace_indices") or [n])[0] if isinstance(s, dict) else n):
        if not isinstance(sub, dict):
            continue
        idxs = [i for i in (sub.get("replace_indices") or []) if isinstance(i, int)]
        if not idxs or any(i < 0 or i >= n or assigned[i] for i in idxs):
            continue
        if any(idxs[k + 1] != idxs[k] + 1 for k in range(len(idxs) - 1)):
            continue
        with_step = sub.get("with") or {}
        cfg = with_step.get("config") or {}
        ty = with_step.get("type") or "api_call"
        desc = sub.get("description") or ""
        if not _trace_confirms(cfg, network_calls):
            warnings.append(
                f"Kept the original steps for \"{desc or 'a step'}\" — the proposed {ty} did not "
                f"match a successful call in the captured trace."
            )
            continue
        slots[idxs[0]] = ("insert", {"type": ty, "enabled": True, "config": cfg})
        for i in idxs[1:]:
            slots[i] = ("drop",)
        for i in idxs:
            assigned[i] = True
        changes.append({
            "action": "replaced", "step_indices": idxs, "description": desc,
            "reason": sub.get("reason") or "", "risk": sub.get("risk") or "caution",
        })

    _PROTECTED = ("navigate", "extract", "evaluate", "return", "api_call", "login_post", "fill", "select")
    for rem in (proposal.get("removals") if isinstance(proposal, dict) else None) or []:
        if not isinstance(rem, dict):
            continue
        dropped = []
        for i in rem.get("indices") or []:
            if not isinstance(i, int) or i < 0 or i >= n or assigned[i]:
                continue
            if (original[i].get("type") if isinstance(original[i], dict) else "") in _PROTECTED:
                continue
            slots[i] = ("drop",)
            assigned[i] = True
            dropped.append(i)
        if dropped:
            changes.append({
                "action": "removed", "step_indices": dropped,
                "description": rem.get("reason") or "Removed a redundant step",
                "reason": rem.get("reason") or "", "risk": rem.get("risk") or "safe",
            })

    out: list = []
    for i, slot in enumerate(slots):
        if slot[0] == "keep":
            out.append(original[i])
        elif slot[0] == "insert":
            out.append(slot[1])
    out = prune_navigates_before_api_only(out)

    for w in (proposal.get("warnings") if isinstance(proposal, dict) else None) or []:
        if isinstance(w, str):
            warnings.append(w)

    removed = max(0, n - len(out))
    verified_any = any(c.get("action") == "replaced" for c in changes)
    return out, changes, warnings, removed, verified_any


def _envelope(steps, changes, warnings, removed, requires_confirm, verified) -> dict:
    return {
        "steps": steps, "changes": changes, "warnings": warnings, "removed_count": removed,
        "requires_confirm": requires_confirm, "verified": verified, "credits_used": 0,
    }


async def optimize_workflow_live(db, workflow_id: int, confirm_side_effects: bool) -> dict:
    from sqlalchemy import select
    from models.automation_workflow import AutomationWorkflow

    row = (await db.execute(
        select(AutomationWorkflow).where(AutomationWorkflow.id == workflow_id)
    )).scalar_one_or_none()
    if row is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Workflow not found")

    original = row.steps if isinstance(row.steps, list) else []
    if len(original) < 2:
        return _envelope(original, [], ["This workflow has too few steps to optimize."], 0, False, False)
    if getattr(row, "workflow_type", "") == "streaming":
        return _envelope(original, [], ["Streaming workflows can't be live-optimized."], 0, False, False)

    if not confirm_side_effects:
        reason = risky_side_effect(original)
        if reason:
            return _envelope(
                original, [],
                [f"This workflow may have side effects ({reason}). Optimizing replays it in a real "
                 f"browser, which re-runs those actions."],
                0, True, False,
            )

    network_calls, final_url = await _replay_and_capture(db, row)
    if not network_calls:
        return _envelope(
            original, [],
            ["Could not capture live API calls for this workflow (no fleet agent available, or it made "
             "no backend calls). Nothing was changed."],
            0, False, False,
        )

    proposal = await _run_optimize_pass(original, network_calls, final_url)
    if proposal is None:
        return _envelope(original, [], ["AI optimization could not be parsed; the workflow is unchanged."], 0, False, False)

    steps, changes, warnings, removed, verified = assemble_optimized(original, proposal, network_calls)
    if not changes:
        warnings.insert(0, "No verified optimizations were found — the workflow is unchanged.")
    return _envelope(steps, changes, warnings, removed, False, verified)


async def _run_optimize_pass(original: list, network_calls: list, final_url: str):
    import json as _json
    from services.agent_brain import call_ai, loads_lenient, summarize_network_calls

    user = (
        "WORKFLOW STEPS (0-indexed):\n" + _json.dumps(original, default=str)[:30000] +
        "\n\nCAPTURED BACKEND CALLS (real — from replaying this workflow just now):\n" +
        summarize_network_calls(network_calls) +
        f"\n\nFINAL URL: {final_url or ''}"
    )
    try:
        text, _in, _out, _model = await call_ai(
            messages=[{"role": "user", "content": user}],
            system_prompt=OPTIMIZE_LIVE_SYSTEM,
            max_tokens=6000,
            purpose="optimize",
        )
        parsed = loads_lenient(text)
        return parsed if isinstance(parsed, dict) else None
    except Exception as e:
        logger.warning("optimize-live AI pass failed: %s", e)
        return None


async def _replay_and_capture(db, workflow) -> tuple:
    """Replay the workflow on a fleet agent with network capture and return (network_calls, final_url).
    Best-effort — returns ([], None) when no fleet agent is available or the replay yields no calls, so
    the endpoint always returns a valid envelope. The fleet agent (Rust) reveals held credentials as
    {{placeholders}} before returning, so no plaintext reaches the coordinator.
    """
    try:
        from routers.ai_sessions import _pick_agent
    except Exception as e:  # pragma: no cover
        logger.info("optimize-live replay unavailable (dispatch imports): %s", e)
        return [], None
    try:
        agent_id = _pick_agent(None)
    except Exception:
        agent_id = None
    if not agent_id:
        return [], None
    # The fleet-agent replay-with-capture dispatch is the remote seam; when it's wired the agent runs
    # the workflow with capture on and returns network_calls. Until then, degrade to no-capture.
    try:
        from services.fleet_dispatch import replay_workflow_capture  # optional, deployment-specific
    except Exception:
        return [], None
    try:
        result = await replay_workflow_capture(db, agent_id, workflow)
        return (result or {}).get("network_calls") or [], (result or {}).get("final_url") or ""
    except Exception as e:
        logger.info("optimize-live fleet replay failed: %s", e)
        return [], None
