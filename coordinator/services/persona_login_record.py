"""Persona login RECORDING by AI — "let AI sign in and record it", self-host edition.

A persona could previously only get a login workflow if the user recorded one BY
HAND or attached one they already had. Here the coordinator asks a connected fleet
agent to sign in with the persona's credentials and record the flow, then turns the
recording into a coordinator-side workflow and wires it onto
`personas.login_workflow_id` — which `persona_login.ensure_fresh_session` replays
on every session expiry.

## Why this is not a copy of the cloud service
The cloud runs the AI loop itself (`AISessionRunner`) and can assemble the
workflow in-process. The coordinator runs NO brain: it dispatches one
`ai_session_start` frame and the AGENT owns the loop (see `models/ai_session.py`).
Two consequences shape this module:

  1. **The recipe has to travel home.** The agent records into ITS OWN `workflows`
     table, whose ids live in a different namespace from
     `automation_workflows.id`. Pointing `login_workflow_id` at an agent-side id
     would dangle. So the agent's terminal frame carries the recorded STEPS
     (`workflow_steps`, added to the wire contract for exactly this), and
     `wire_login_record_result` materializes the coordinator's own workflow row.
  2. **Credentials ride the frame, not a persona handle.** The frame's
     `persona_id` names a persona already DEPLOYED to that agent, and nothing
     tracks which coordinator persona maps to which agent-side row. Instead the
     persona's login credentials are sealed under the agent's channel key
     (`_seal_plaintext_for_agent`) exactly as the fleet deploy does, and the
     agent merges them into its fill data. The model still never sees a value —
     the agent masks them to `[SECURE:key]` and records `{{secret:key}}`.

## 2FA is refused up front, deliberately
Minting a one-time code needs the persona's TOTP seed on the machine running the
browser (or a mailbox reader). Neither is true for a credentials-only frame, so a
2FA persona would drive a browse that dead-ends at the code prompt and bank a
half-recording. Better to say so before spending the run — see
`_refuse_reason_for_twofa`.
"""
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.ai_session import AiSession
from models.persona import Persona

logger = logging.getLogger(__name__)

# A login is a SHORT browse: find the form, fill, verify, stop. Well under the
# default budget so a lost run cannot wander an account area.
LOGIN_RECORD_MAX_STEPS = 30


def build_login_record_goal(persona: Persona, entry_url: str) -> str:
    """The canned goal for a login-recording browse.

    Scope discipline matters more here than anywhere else: this recording is
    REPLAYED on every session expiry, so a browse that wanders past the login
    bakes that detour into every future re-login.
    """
    where = persona.target_domain or entry_url
    return (
        f"Sign in to {where} as the saved account, then stop.\n"
        "This browse exists ONLY to record a reusable sign-in workflow:\n"
        "1. From the entry page, find and open the sign-in form (follow a "
        "'Sign in' / 'Log in' link if the form is not already visible).\n"
        "2. Fill the form using the provided secure fields — always reference "
        "the placeholders, never type a literal credential value.\n"
        "3. Verify you actually ARE signed in: an account menu / avatar / "
        "dashboard is visible and no login form remains.\n"
        "4. Then finish immediately. Do not browse further, do not extract "
        "data, and do not change any account setting."
    )


def normalize_secret_placeholders(steps, credential_keys) -> list:
    """Rewrite bare credential placeholders onto the `{{secret:...}}` channel.

    LOAD-BEARING, not tidying. The agent's replay resolver reads `{{secret:key}}`
    from the credentials channel and a bare `{{key}}` from form_data ONLY,
    resolving a miss to the EMPTY STRING (`util/value_resolver.rs`). A recorded
    login left in the bare form would type nothing on every re-login and land
    logged-out silently — invisible until a crawl quietly returns signed-out
    pages. The agent now spells these correctly at record time; this is the
    belt-and-braces pass for any recording that predates that, or that the model
    wrote by hand.

    Only keys this persona actually supplies are rewritten — a placeholder naming
    a genuine form_data input is left alone. Pure; returns a new list.
    """
    import re

    keys = [str(k) for k in (credential_keys or []) if k]
    if not steps or not keys:
        return steps
    # Both the plain key and the explorer's `login_`-prefixed alias name the same
    # credential. Longest first so `login_password` can't be partially matched.
    alias_to_key = {}
    for k in keys:
        alias_to_key[k] = k
        alias_to_key[f"login_{k}"] = k
    pattern = re.compile(
        r"\{\{\s*("
        + "|".join(re.escape(a) for a in sorted(alias_to_key, key=len, reverse=True))
        + r")\s*\}\}"
    )

    def _sub(node):
        if isinstance(node, str):
            return pattern.sub(lambda m: "{{secret:" + alias_to_key[m.group(1)] + "}}", node)
        if isinstance(node, list):
            return [_sub(x) for x in node]
        if isinstance(node, dict):
            return {k: _sub(v) for k, v in node.items()}
        return node

    return _sub(steps)


def _refuse_reason_for_twofa(persona: Persona) -> Optional[str]:
    """Why an AI recording can't handle this persona's 2FA, or None when it can.

    The agent mints one-time codes only for a persona DEPLOYED to it (its own
    sealed TOTP seed); email/SMS OTP reading is a cloud capability it does not
    have at all. A credentials-only frame therefore cannot pass a code prompt.
    """
    method = (persona.twofa_method or "none").strip().lower()
    if method in ("", "none"):
        return None
    if method == "totp":
        return (
            "This persona uses an authenticator code, which the AI recorder can't "
            "enter on its own. Record the sign-in yourself once — the saved code "
            "seed is used automatically on every replay after that."
        )
    return (
        "This persona receives its one-time code by email or text, which the "
        "self-host agent can't read. Record the sign-in yourself once instead."
    )


def _entry_url(persona: Persona, login_url: Optional[str]) -> str:
    """The URL a login recording starts from: the caller's, else the domain root."""
    url = (login_url or "").strip()
    if url:
        return url if url.lower().startswith(("http://", "https://")) else f"https://{url}"
    if not persona.target_domain:
        raise HTTPException(
            status_code=422,
            detail=(
                "This persona has no site domain. Set its domain (or pass a login "
                "URL) so the AI knows where to sign in."
            ),
        )
    return f"https://{persona.target_domain}"


async def _running_recording(db: AsyncSession, persona_id: int) -> Optional[AiSession]:
    """The in-flight login recording for this persona, if any.

    Single-flight is enforced off the DB rather than a lock: dispatch is
    fire-and-forget and the reply lands minutes later on the agent's socket, so
    the durable row is the only state that survives a coordinator restart in
    between. Two concurrent AI logins against one account is how a site locks it.
    """
    return (
        await db.execute(
            select(AiSession)
            .where(
                AiSession.login_for_persona_id == persona_id,
                AiSession.status == "running",
            )
            .order_by(AiSession.id.desc())
        )
    ).scalars().first()


async def start_login_record_session(
    db: AsyncSession,
    persona: Persona,
    *,
    login_url: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> Tuple[int, bool]:
    """Dispatch the AI login recording for ``persona``.

    Returns ``(ai_session_pk, already_running)`` — already_running=True means a
    recording was already in flight and the id is THAT session's. Raises
    HTTPException on a failed pre-flight (no credentials, no domain, 2FA, no
    agent online), which the router surfaces verbatim.
    """
    from routers import user_recorder_ws
    from routers.ai_sessions import _pick_agent
    from routers.fleet import _resolve_deploy_target, _seal_plaintext_for_agent
    from services.persona_service import PersonaService

    if not (persona.login_username or persona.credentials_encrypted):
        raise HTTPException(
            status_code=422,
            detail=(
                "This persona has no login credentials, so the AI has nothing to "
                "sign in with. Add a username and password first."
            ),
        )
    refusal = _refuse_reason_for_twofa(persona)
    if refusal:
        raise HTTPException(status_code=422, detail=refusal)

    entry_url = _entry_url(persona, login_url)

    existing = await _running_recording(db, persona.id)
    if existing is not None:
        return existing.id, True

    credentials = PersonaService.resolve_login_credentials(persona)
    if not credentials:
        raise HTTPException(
            status_code=422,
            detail=(
                "This persona's credentials could not be read. Re-enter its "
                "username and password, then try again."
            ),
        )

    # Picks an ONLINE local-capable agent (409s when the fleet is empty) and
    # returns its channel key — the same seal the fleet deploy uses, so the
    # values only ever exist decrypted inside the agent process.
    picked_agent = _pick_agent(agent_id)
    channel_key = _resolve_deploy_target(picked_agent)
    credentials_encrypted = _seal_plaintext_for_agent(json.dumps(credentials), channel_key)

    session_id = str(uuid.uuid4())
    name = f"{persona.name} login"[:500]
    row = AiSession(
        session_id=session_id,
        agent_id=picked_agent,
        name=name,
        goal=build_login_record_goal(persona, entry_url),
        entry_url=entry_url,
        status="running",
        generate_workflow=True,
        # The durable marker the terminal frame's wiring keys on.
        login_for_persona_id=persona.id,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)

    frame = {
        "type": "ai_session_start",
        "session_id": session_id,
        "request_id": str(uuid.uuid4()),
        "name": name,
        "goal": row.goal,
        "entry_url": entry_url,
        # Key NAMES only — the agent masks the sealed values to [SECURE:key]
        # before the model ever sees them.
        "available_data": {},
        "credentials_encrypted": credentials_encrypted,
        # Deliberately absent: `persona_id` names an AGENT-side persona, and
        # nothing maps this coordinator persona to one. The credentials above are
        # what the agent signs in with.
        "persona_id": None,
        "max_steps": LOGIN_RECORD_MAX_STEPS,
        "generate_workflow": True,
    }

    sent = await user_recorder_ws.push_fire_and_forget(picked_agent, frame)
    if not sent:
        row.status = "error"
        row.error = "agent_disconnected"
        row.completed_at = datetime.now(timezone.utc)
        await db.commit()
        raise HTTPException(
            status_code=409,
            detail="The chosen agent disconnected before the login recording could start.",
        )

    logger.info(
        f"[PersonaLoginRecord] persona {persona.id}: AI login recording dispatched "
        f"to {picked_agent} (session {row.id})"
    )
    return row.id, False


async def wire_login_record_result(db: AsyncSession, row: AiSession, frame: dict) -> None:
    """Turn the agent's recorded recipe into this persona's login workflow.

    Called from the WS terminal-frame handler for any session carrying
    `login_for_persona_id`. Materializes a coordinator-side AutomationWorkflow
    from the frame's `workflow_steps` (the agent-side `workflow_id` is a foreign
    namespace and cannot be linked), points the persona at it, and records an
    HONEST outcome — a recording that captured no sign-in steps is reported as a
    failure rather than wired as a workflow that does nothing.

    Best-effort by contract: the caller must not lose the terminal frame if this
    fails, so every problem is recorded on the persona and swallowed.
    """
    from models.automation_workflow import AutomationWorkflow
    from services.persona_service import PersonaService

    persona_id = row.login_for_persona_id
    if not persona_id:
        return
    try:
        persona = (
            await db.execute(select(Persona).where(Persona.id == persona_id))
        ).scalar_one_or_none()
        if persona is None:
            return

        status = str(frame.get("status") or "")
        steps = frame.get("workflow_steps")
        if not isinstance(steps, list):
            steps = []

        if status != "complete" or not steps:
            err = frame.get("error") or frame.get("message")
            persona.last_login_error = (
                f"The AI could not record the sign-in: {str(err)[:300]}"
                if err
                else "The AI finished without recording any sign-in steps. Try "
                     "again, or record the login manually."
            )
            await db.commit()
            return

        # The agent spells credentials for the secret channel at record time; this
        # is the safety net for anything that arrived in the bare form (see
        # normalize_secret_placeholders — a bare {{password}} replays as EMPTY).
        credential_keys = list((PersonaService.resolve_login_credentials(persona) or {}).keys())
        steps = normalize_secret_placeholders(steps, credential_keys)

        wf = AutomationWorkflow(
            name=f"{persona.name} login"[:255],
            description=(
                f"Signs in as the persona '{persona.name}'. Recorded by AI on "
                f"{row.agent_id}; re-run automatically whenever the session expires."
            ),
            workflow_type="recorded",
            steps=steps,
            entry_url=frame.get("workflow_entry_url") or row.entry_url,
            is_active=True,
            default_persona_id=persona.id,
        )
        db.add(wf)
        await db.flush()
        persona.login_workflow_id = wf.id
        # "Recorded" is not "verified": the session state lives on the AGENT, so the
        # coordinator cannot confirm a real login here the way the cloud does. Say
        # exactly that instead of showing a green state we can't back up.
        persona.last_login_error = (
            "Sign-in recorded by AI. Use 'Sign in now' to run it once and confirm "
            "it really logs in."
        )
        await db.commit()
        logger.info(
            f"[PersonaLoginRecord] persona {persona_id}: login workflow {wf.id} "
            f"materialized from AI recording ({len(steps)} steps)"
        )
    except Exception as e:  # noqa: BLE001 — never lose the terminal frame to bookkeeping
        logger.error(f"[PersonaLoginRecord] wiring failed for persona {persona_id}: {e}")
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
