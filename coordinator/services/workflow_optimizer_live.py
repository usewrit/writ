"""Live workflow optimizer (self-host coordinator) — coordinator twin of the cloud
`services.workflow_optimizer_live`. Replays the workflow on a fleet agent WITH network capture,
runs one OPTIMIZE_LIVE pass over the real trace, trace-verifies each proposed substitution, and
returns the diff. The coordinator is single-user (no tenant scoping); the fleet agent (Rust) reveals
held credentials as {{placeholders}} before returning the trace.

The pure logic (risky gate, trace verify, deterministic assemble, OPTIMIZE_LIVE prompt) is identical
to the cloud service; only the workflow load, the AI call signature, and the replay dispatch differ.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Bound the replay below the caller's patience: the browser gives up on this request long
# before the dispatch primitive does, so an uncapped wait would leave the button looking
# dead while the run continued. Leaves room for the AI pass that follows.
_REPLAY_TIMEOUT_S = 110

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


def _secret_keys_in_steps(steps) -> list:
    """Credential key names the steps already reference as `{{secret:key}}`.

    A step that fills `{{secret:password}}` names the credential the sign-in uses,
    so a bare `{{password}}` the capture left in a proposed body means that same
    credential and belongs on the same channel.
    """
    import json
    import re

    try:
        blob = json.dumps(steps or [], default=str)
    except Exception:
        return []
    return sorted(set(re.findall(r"\{\{\s*secret:\s*(\w+)\s*\}\}", blob)))


def _steps_perform_login(steps, credential_keys) -> bool:
    """True when these steps type the persona's credentials themselves.

    Such a workflow signs in on its own, so replaying it COLD reproduces the sign-in (and
    captures its request) with no login recipe prepended. Matches only the
    `{{secret:<key>}}` channel, which is where the agent reads credentials from — a bare
    `{{key}}` resolves against form data, so it names ordinary run input.
    """
    import json
    import re

    keys = [str(k) for k in (credential_keys or []) if k]
    if not steps or not keys:
        return False
    try:
        blob = json.dumps(steps, default=str)
    except Exception:
        return False
    pattern = re.compile(
        r"\{\{\s*secret:\s*(" + "|".join(re.escape(k) for k in keys) + r")\s*\}\}")
    return bool(pattern.search(blob))


def _normalize_credential_placeholders(node, credential_keys):
    """Rewrite bare `{{key}}` onto the `{{secret:key}}` channel for credential keys.

    The agent reveals a held credential inside a captured request as a BARE
    `{{key}}` placeholder, and the AI quotes the trace verbatim — so a proposed
    login_post carries `password={{password}}`. Replayed, a bare placeholder reads
    FORM_DATA, never the credentials channel, so the applied optimization signs in
    with no password and lands signed-out — silently, since the request succeeds.

    Only keys the steps actually use as credentials are rewritten; a bare
    placeholder naming genuine run input keeps its form_data meaning. Pure.
    """
    import re

    keys = [str(k) for k in (credential_keys or []) if k]
    if not keys:
        return node
    pattern = re.compile(
        r"\{\{\s*(" + "|".join(re.escape(k) for k in sorted(keys, key=len, reverse=True)) + r")\s*\}\}")

    def _sub(n):
        if isinstance(n, str):
            return pattern.sub(lambda m: "{{secret:" + m.group(1) + "}}", n)
        if isinstance(n, list):
            return [_sub(x) for x in n]
        if isinstance(n, dict):
            return {k: _sub(v) for k, v in n.items()}
        return n

    return _sub(node)


def assemble_optimized(original: list, proposal: dict, network_calls: list,
                       credential_keys=None) -> tuple:
    from services.agent_brain import prune_navigates_before_api_only

    # The proposal quotes the captured trace, where held credentials appear as BARE
    # placeholders. Move them onto the credentials channel before anything is verified
    # or assembled, so the substitution bodies and headers carry the form that resolves.
    # The replay reports which keys it actually supplied; without that (an in-recorder
    # draft) the steps' own `{{secret:...}}` usages name the same credentials.
    keys = list(credential_keys or []) or _secret_keys_in_steps(original)
    proposal = _normalize_credential_placeholders(proposal, keys)
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

    network_calls, final_url, credential_keys = await _replay_and_capture(db, row)
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

    steps, changes, warnings, removed, verified = assemble_optimized(
        original, proposal, network_calls, credential_keys)
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


async def _load_login_steps(db, login_workflow_id: int) -> list:
    """The persona's login workflow steps, or [] when it is unusable here.

    A row with no steps is [] rather than "found": prepending nothing would leave the cold
    replay signed out while looking like it had signed in.
    """
    try:
        from sqlalchemy import select
        from models.automation_workflow import AutomationWorkflow

        row = (await db.execute(
            select(AutomationWorkflow).where(AutomationWorkflow.id == login_workflow_id)
        )).scalar_one_or_none()
    except Exception as e:  # noqa: BLE001 — never block optimizing
        logger.info("optimize-live login workflow load failed: %s", e)
        return []
    if row is None or not (row.steps or []):
        return []
    return list(row.steps)


async def _replay_and_capture(db, workflow) -> tuple:
    """Replay the workflow on a fleet agent with network capture ON and return
    (network_calls, final_url, credential_keys).

    Best-effort — returns ([], "", []) when no fleet agent is available, the build/dispatch
    fails, or the replay yields no calls, so the endpoint always returns a valid envelope
    and this NEVER raises. The agent reveals held credentials as {{placeholders}} before
    returning, so no plaintext reaches the coordinator; `credential_keys` is the NAMES of
    the credentials this run supplied, which is what tells the caller which bare
    placeholders in the trace belong on the secrets channel.

    THE SIGN-IN MUST HAPPEN DURING THE REPLAY, NOT BEFORE IT. Restoring the persona's warm
    session makes the browser arrive already authenticated: the sign-in form never renders,
    the credential fills type into nothing, and no auth request enters the trace — so an
    authenticated workflow could never become an API-shaped one, which is the whole point.
    We keep the persona (credentials, 2FA, identity) and drop the session: either this
    workflow types the persona's credentials itself, or the persona's login workflow is
    PREPENDED so the cold browser authenticates first. Only when neither exists do we hand
    over the stored session — a cold replay would then just trace the login wall.

    BILLING / METERING — avoided by construction: the task_id is a SYNTHETIC, non-persisted
    id, so the completion handler resolves the in-memory future and returns before any DB
    lookup or run accounting. `push_to_recorder` itself never touches the DB.
    """
    credential_keys: list = []
    try:
        from routers.ai_sessions import _pick_agent
        from routers.automation import build_execute_workflow_msg
        from routers.user_recorder_ws import push_to_recorder
    except Exception as e:  # pragma: no cover - wiring differs across deployments
        logger.info("optimize-live replay unavailable (dispatch imports): %s", e)
        return [], "", credential_keys
    try:
        agent_id = _pick_agent(None)
    except Exception:
        agent_id = None
    if not agent_id:
        logger.info("optimize-live replay: no fleet agent available")
        return [], "", credential_keys

    # --- Resolve the persona + the sign-in the replay must perform --------------------
    persona_cfg = None
    session_state = None
    login_steps: list = []
    _orig_creds = getattr(workflow, "credentials_encrypted", None)
    _creds_folded = False
    try:
        if getattr(workflow, "default_persona_id", None):
            from sqlalchemy import select
            from models.persona import Persona
            from routers.automation import encrypt_credentials, decrypt_credentials
            from services.persona_service import PersonaService

            persona = (await db.execute(
                select(Persona).where(Persona.id == workflow.default_persona_id)
            )).scalar_one_or_none()
            if persona is not None and getattr(persona, "is_active", True):
                try:
                    persona_creds = PersonaService.resolve_login_credentials(persona)
                except Exception:
                    persona_creds = None
                login_wf = None
                if not _steps_perform_login(getattr(workflow, "steps", None), persona_creds):
                    login_wf_id = getattr(persona, "login_workflow_id", None)
                    if login_wf_id and login_wf_id != getattr(workflow, "id", None):
                        login_steps = await _load_login_steps(db, login_wf_id)
                    if not login_steps:
                        session_state = PersonaService.load_session(persona)
                    else:
                        login_wf = login_wf_id
                # TRANSIENTLY fold the credentials the replay needs into the run
                # credentials for the frame build; restored in the finally below. This
                # function never commits, so no mutation is persisted.
                all_creds = {}
                if _orig_creds:
                    try:
                        all_creds.update(decrypt_credentials(_orig_creds) or {})
                    except Exception:
                        all_creds = {}
                if persona_creds:
                    all_creds.update(persona_creds)
                if all_creds:
                    workflow.credentials_encrypted = encrypt_credentials(all_creds)
                    _creds_folded = True
                    credential_keys = sorted(all_creds.keys())
                from config import settings
                persona_cfg = {
                    "persona_id": persona.id,
                    "twofa_method": getattr(persona, "twofa_method", None) or "none",
                    "otp_extract_config": getattr(persona, "otp_extract_config", None) or {},
                    "otp_token": PersonaService.make_otp_token(persona.id, None),
                    "coordinator_url": settings.coordinator_url,
                }
                logger.info(
                    "optimize-live replay: persona %s, cold=%s, login steps prepended=%s",
                    persona.id, session_state is None, bool(login_wf),
                )
    except Exception as e:  # noqa: BLE001 — an unauthenticated replay still traces public pages
        logger.info("optimize-live replay persona resolution skipped: %s", e)

    # --- Build the execute_workflow frame + enable capture ---------------------------
    import random
    # Synthetic, non-persisted task_id in a reserved high range so it cannot collide with a
    # real row in flight; push_to_recorder correlates purely by this, in memory.
    task_id = random.randint(2_000_000_000, 2_147_483_000)
    _orig_steps = getattr(workflow, "steps", None)
    _steps_folded = False
    if login_steps:
        try:
            workflow.steps = login_steps + list(_orig_steps or [])
            _steps_folded = True
        except Exception as e:  # noqa: BLE001 — replay signed-out rather than not at all
            logger.info("optimize-live login step prepend skipped: %s", e)
    try:
        frame = build_execute_workflow_msg(
            task_id=task_id,
            workflow=workflow,
            form_data=(getattr(workflow, "form_data", None) or {}),
            session_state=session_state,
            persona_cfg=persona_cfg,
        )
    except Exception as e:
        logger.info("optimize-live replay frame build failed: %s", e)
        return [], "", credential_keys
    finally:
        # Restore the ORM row exactly as found — this function never commits, and restoring
        # here keeps a later caller commit non-destructive.
        if _creds_folded:
            try:
                workflow.credentials_encrypted = _orig_creds
            except Exception:
                pass
        if _steps_folded:
            try:
                workflow.steps = _orig_steps
            except Exception:
                pass

    if not isinstance(frame, dict):
        return [], "", credential_keys
    # Thread capture ON via the config key the agent reads. The builder does not take this
    # kwarg, so stamp the built frame; guard the config type so this can never raise.
    _cfg = frame.get("config")
    if not isinstance(_cfg, dict):
        _cfg = {}
        frame["config"] = _cfg
    _cfg["capture_network"] = True

    # --- Dispatch + await -------------------------------------------------------------
    # BOUNDED BELOW THE CALLER'S PATIENCE: push_to_recorder waits far longer than the
    # browser will, so a slow replay would outlive the request and the button would look
    # dead. Capping here degrades a silent agent into the honest "couldn't capture live API
    # calls" warning, INSIDE the window the UI is waiting, and leaves room for the AI pass.
    try:
        reply = await asyncio.wait_for(
            push_to_recorder(agent_id, frame), timeout=_REPLAY_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        logger.info("optimize-live replay exceeded %ss on %s", _REPLAY_TIMEOUT_S, agent_id)
        return [], "", credential_keys
    except Exception as e:
        logger.info("optimize-live replay dispatch failed: %s", e)
        return [], "", credential_keys
    if not isinstance(reply, dict) or reply.get("error"):
        if isinstance(reply, dict):
            logger.info("optimize-live replay returned error: %s", reply.get("error"))
        return [], "", credential_keys

    data = reply.get("result_data")
    if not isinstance(data, dict):
        data = reply.get("result") if isinstance(reply.get("result"), dict) else reply
    network_calls = (data.get("network_calls") if isinstance(data, dict) else None) or []
    final_url = (
        (data.get("final_url") if isinstance(data, dict) else None)
        or reply.get("final_url")
        or ""
    )
    return network_calls, final_url, credential_keys
