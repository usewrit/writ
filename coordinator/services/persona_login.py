"""Persona sign-in — run a persona's login workflow to establish its warm session.

THE GAP THIS CLOSES. A persona's warm session (`session_state_encrypted`) could
only ever arrive by CAPTURE from something that had already signed in
(/personas/from-workflow, /personas/from-task, /personas/from-ai-session, or the
run-completion write-back). Nothing could make a persona sign IN. A persona created
with credentials alone therefore never had a session, and every authenticated crawl
using it was rejected at creation with "That persona has no live login session" —
permanently, with no route out of the error.

HOW IT WORKS. `personas.login_workflow_id` names a workflow that performs the login.
Dispatching it with `persona_id` folds the persona's credentials and 2FA into the
run (routers/automation.py `_dispatch_to_recorder_or_queue`), and the existing
completion write-back — keyed on `trigger_context._persona_id` — persists the
captured `auth_session` back onto the persona. So this module owns no capture code
of its own; it dispatches, waits, and re-reads.

STAMPEDE CONTROL is the reason this is a service and not three lines at each call
site. A crawl fans out to many shards, and they all notice the same expired session
within milliseconds of each other. Without a lock that is N concurrent logins
against the same account — which is exactly the pattern that trips a site's own
abuse defenses and can get the account locked. The lock (in-process fakeredis on
self-host — one keyspace shared by the whole coordinator, which is single-process,
so it is exactly as strong as it needs to be) makes one caller log in while the
rest WAIT for that result and then reuse it.

This is the single-owner variant of the sign-in path: ownership is resolved with
`get_owned` and there is no tenant scoping, and dispatch carries no queue source.
The hosted edition has a counterpart that differs in exactly those two respects, so
a behavioural change here usually belongs in both.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Tuple

from database import AsyncSessionLocal
from models.persona import Persona
from services.persona_service import PersonaService

logger = logging.getLogger(__name__)

# How long a sign-in run may take before we give up on it. Login flows with an
# email-OTP hop are genuinely slow (mailbox poll + link/code extraction), so this
# is well above a normal run's budget.
LOGIN_TIMEOUT_SECONDS = 240
# Lock outlives the timeout so a worker killed mid-login can't leave a second
# caller free to immediately re-dispatch against the same account.
_LOCK_TTL_SECONDS = LOGIN_TIMEOUT_SECONDS + 60
_LOCK_PREFIX = "persona:login:"
_POLL_INTERVAL_SECONDS = 2


def session_is_usable(session: Optional[dict]) -> bool:
    """Does this persona session carry anything that can actually authenticate?

    Checks EVERY shape a session can arrive in — `cookies`, the Writ camelCase
    `localStorage`/`sessionStorage` maps, captured auth `headers`, the HTTP-lane
    `tokens` store, and Playwright's `origins[]`. Checking only `cookies`/`origins`
    would report a token-auth SPA (whose whole session is a localStorage JWT — a very
    common shape here) as expired no matter how fresh it was.

    This is the canonical definition; crawl_orchestrator delegates to it so the
    creation-time check, the pre-fanout guard and the sign-in path can never
    disagree about what counts as a session.
    """
    if not isinstance(session, dict):
        return False
    for key in ("cookies", "origins", "localStorage", "sessionStorage", "headers", "tokens"):
        if session.get(key):
            return True
    return False


# Cookie NAME fragments that mark a real authentication/session cookie across the
# common stacks. Deliberately BROAD — the goal is to recognize EVERY auth shape
# (server sessions, framework cookies, OAuth/JWT, "remember me", SSO) so a real
# login is never misread as anonymous. Matched case-insensitively as substrings.
_AUTH_COOKIE_HINTS = (
    "session", "sess", "sid", "auth", "token", "jwt", "login", "logged", "loggedin",
    "account", "identity", "credential", "access", "refresh", "remember", "rememberme",
    "oauth", "openid", "saml", "sso", "bearer", "apikey", "api_key", "passport",
    "phpsessid", "jsessionid", "asp.net", "aspxauth", "connect.sid", "csrftoken",
    "_session", "user_session", "auth_token", "id_token", "access_token", "ss-id",
    "ss-pid", "laravel_session", "django", "rails", "_forum_session", "wordpress_logged_in",
)
# Cookie names that are NEVER, on their own, evidence of a login — analytics,
# consent banners, CDN/anti-bot, A/B tests. A session made of ONLY these is the
# anonymous logged-OUT state a failed login leaves behind.
_ANON_COOKIE_HINTS = (
    "consent", "cookieconsent", "cookie_consent", "gdpr", "optanon", "onetrust",
    "_ga", "_gid", "_gat", "_gcl", "_fbp", "_fbc", "fr", "_hj", "hotjar", "amplitude",
    "mixpanel", "segment", "intercom", "_pk_", "matomo", "analytics", "utm",
    "__cf", "cf_", "cf-", "_cfuvid", "__cflb", "ak_bmsc", "bm_", "_abck", "datadome",
    "incap_", "visid_incap", "nr_", "newrelic", "ab_test", "experiment", "gtm",
)


def _cookie_is_auth_like(cookie: dict) -> bool:
    """Does ONE cookie look like it carries a login? True when it is HttpOnly (a
    server-set session/auth cookie the page's JS can't read — analytics/consent
    cookies are readable, so never HttpOnly), OR its name matches an auth hint and
    is not a known-anonymous name. Broad by design (see the hint lists)."""
    if not isinstance(cookie, dict):
        return False
    name = str(cookie.get("name") or "").lower()
    if not name:
        return False
    # A known analytics/consent/anti-bot cookie is not auth on its own — but an
    # HttpOnly one still is (some anti-bot names collide; HttpOnly wins as the
    # stronger signal). Check the anonymous list only for the name-based branch.
    http_only = bool(cookie.get("httpOnly") or cookie.get("http_only"))
    if http_only:
        return True
    if any(h in name for h in _ANON_COOKIE_HINTS):
        return False
    return any(h in name for h in _AUTH_COOKIE_HINTS)


def session_has_auth_material(session: Optional[dict]) -> bool:
    """STRICTER than session_is_usable: does the session carry material that
    actually proves a LOGIN, rather than merely SOME state?

    session_is_usable stays permissive (any cookie/storage counts) because the
    crawl gate must not block a working persona over a heuristic. This is the
    verification the "Sign in now" TEST reports: after a login run, a session made
    of ONLY anonymous cookies (consent/analytics, no HttpOnly, no auth-named
    cookie, no token store) means the login did NOT take — the workflow ran but
    landed logged-out — and the user must be told the truth, not "signed in".

    Recognizes EVERY auth shape so a real login is never misread as anonymous:
      • a token store — localStorage / sessionStorage / captured auth `headers` /
        the HTTP-lane `tokens` map (SPA JWT auth lives here, never in a cookie);
      • Playwright `origins[]` carrying localStorage (storage_state SPA shape);
      • at least one auth-like COOKIE (HttpOnly, or an auth-named non-anonymous one).
    """
    if not isinstance(session, dict):
        return False

    # 1. Any token store is auth material — a logged-out page rarely writes one,
    #    and token-auth SPAs keep their WHOLE session here with no auth cookie.
    for key in ("localStorage", "sessionStorage", "headers", "tokens"):
        val = session.get(key)
        if isinstance(val, dict) and val:
            return True
        if isinstance(val, list) and val:
            return True

    # 2. Playwright storage_state origins[] with localStorage entries.
    origins = session.get("origins")
    if isinstance(origins, list):
        for o in origins:
            if isinstance(o, dict) and (o.get("localStorage") or o.get("sessionStorage")):
                return True

    # 3. An auth-like cookie among the jar (HttpOnly or auth-named, not anonymous).
    cookies = session.get("cookies")
    if isinstance(cookies, list):
        if any(_cookie_is_auth_like(c) for c in cookies):
            return True

    return False


async def _load_persona_session(persona_id: int) -> Optional[dict]:
    """Re-read the persona on a FRESH session and return its usable warm session.

    Fresh because the write-back happens in the completing worker's session, not
    ours: AsyncSessionLocal is built with expire_on_commit=False, so re-selecting
    on a session that already loaded this persona returns the identity-mapped copy
    with its stale `session_state_encrypted` — we would never observe the login we
    just waited for.
    """
    async with AsyncSessionLocal() as db:
        persona = await PersonaService.get_owned(db, persona_id)
        if persona is None:
            return None
        session = PersonaService.load_session(persona)
        return session if session_is_usable(session) else None


async def _record_login_error(persona_id: int, error: Optional[str]) -> None:
    """Stamp (or clear) `last_login_error` so the UI can explain a failed sign-in
    without the operator digging through run history. Best-effort: never let
    bookkeeping turn a successful login into a failure."""
    try:
        async with AsyncSessionLocal() as db:
            persona = await PersonaService.get_owned(db, persona_id)
            if persona is None:
                return
            persona.last_login_error = error
            if error is None:
                persona.validation_status = "valid"
                persona.last_login_at = datetime.now(timezone.utc)
            await db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[PersonaLogin] could not record login error for {persona_id}: {e}")


async def _wait_for_another_login(persona_id: int) -> Optional[dict]:
    """Another caller holds the lock: poll for the session IT establishes.

    Returns the session, or None if that login never produced one within the
    timeout — we simply time out and the caller reports a normal failure.
    """
    waited = 0
    while waited < LOGIN_TIMEOUT_SECONDS:
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        waited += _POLL_INTERVAL_SECONDS
        session = await _load_persona_session(persona_id)
        if session is not None:
            logger.info(
                f"[PersonaLogin] persona {persona_id}: reusing session from a "
                f"concurrent sign-in (waited {waited}s)"
            )
            return session
    return None


async def _dispatch_login_run(persona: Persona) -> Tuple[bool, Optional[str], Optional[int]]:
    """Dispatch the persona's login workflow. Returns (dispatched, error, task_id).

    Runs in its own session and COMMITS: the queue/recorder must be able to see the
    task row immediately, and the caller's session may be mid-transaction.
    """
    from sqlalchemy import select
    from models.automation_workflow import AutomationWorkflow
    from routers.automation import _dispatch_to_recorder_or_queue

    async with AsyncSessionLocal() as db:
        wf = (await db.execute(
            select(AutomationWorkflow).where(
                AutomationWorkflow.id == persona.login_workflow_id,
            )
        )).scalar_one_or_none()
        if wf is None:
            return False, (
                "This persona's login workflow no longer exists. Attach or record a "
                "login workflow for it, then sign in again."
            ), None
        if not (wf.steps or []):
            return False, (
                "This persona's login workflow has no steps recorded. Record the "
                "sign-in once, then try again."
            ), None
        try:
            task = await _dispatch_to_recorder_or_queue(
                db=db, workflow=wf,
                trigger_type="manual",
                # The tag the completion write-back keys on. Passing persona_id is
                # what makes this a persona login rather than an ordinary run: the
                # dispatcher folds in credentials + 2FA and stamps
                # trigger_context._persona_id, and the completion handler then
                # saves the captured auth_session onto the persona.
                persona_id=persona.id,
                trigger_context={"_persona_login": True},
            )
            await db.commit()
        except Exception as e:  # noqa: BLE001 — surface as a normal login failure
            logger.warning(f"[PersonaLogin] dispatch failed for persona {persona.id}: {e}")
            return False, f"Could not start the login run: {e}", None
        return True, None, getattr(task, "id", None)


async def _await_login_task(task_id: int) -> Tuple[bool, Optional[str]]:
    """Poll the login run to a terminal state. Returns (success, error).

    Each tick uses a SHORT-LIVED session so no pooled connection is held across the
    sleep — a 240s wait would otherwise pin a connection for its whole duration.
    """
    from sqlalchemy import select
    from models.automation_task import AutomationTask

    elapsed = 0
    while elapsed < LOGIN_TIMEOUT_SECONDS:
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        elapsed += _POLL_INTERVAL_SECONDS
        try:
            async with AsyncSessionLocal() as db:
                task = (await db.execute(
                    select(AutomationTask).where(AutomationTask.id == task_id)
                )).scalar_one_or_none()
                if task is None:
                    return False, "The login run disappeared before it completed."
                if task.status in ("success", "failed", "cancelled"):
                    if task.status == "success" and task.success is not False:
                        return True, None
                    return False, (task.error_message or f"The login run {task.status}.")
        except Exception as poll_e:  # noqa: BLE001
            # Retry next tick rather than reporting a healthy in-flight login as
            # failed; a persistent failure degrades to the timeout below.
            logger.warning(f"[PersonaLogin] poll for task {task_id} failed, retrying: {poll_e}")
            continue
    return False, f"The login run did not finish within {LOGIN_TIMEOUT_SECONDS} seconds."


def has_login_credentials(persona) -> bool:
    """Does this persona carry anything an agent could type into a login form?"""
    return bool(
        getattr(persona, "login_username", None)
        or getattr(persona, "credentials_encrypted", None)
    )


async def _session_is_signed_out(persona_id: int, session: dict) -> bool:
    """True only when the SITE proves this session is signed out.

    Three-valued underneath (see persona_session_probe) and collapsed to a single
    actionable bool: unknown, unprobeable, or transport-failed all mean "carry on
    with the session we have", because a re-login costs a real agent run and a
    flaky probe must never trigger one. Never raises.
    """
    try:
        from services import persona_session_probe

        async with AsyncSessionLocal() as db:
            persona = await PersonaService.get_owned(db, persona_id)
            if persona is None:
                return False
            probe_url = await persona_session_probe.gated_url_for_persona(db, persona)
            if not probe_url:
                return False
            fingerprint = persona.fingerprint or {}
        verdict, detail = await persona_session_probe.probe_session_authenticated(
            url=probe_url, session=session, user_agent=fingerprint.get("user_agent"),
        )
        if verdict is False:
            logger.info(
                f"[PersonaLogin] persona {persona_id}: site says signed out ({detail})"
            )
            return True
        return False
    except Exception as e:  # noqa: BLE001 — verification is a safety net, never a gate
        logger.warning(
            f"[PersonaLogin] persona {persona_id}: session verification errored "
            f"({e}); reusing the existing session"
        )
        return False


async def _bootstrap_login_via_ai(persona) -> Tuple[bool, Optional[str], Optional[dict]]:
    """Sign a credentials-only persona in for the FIRST time, via the AI recorder.

    Reuses services.persona_login_record: the coordinator dispatches one
    `ai_session_start` to a fleet agent, which opens a real browser, fills the
    site's own sign-in form from the sealed credentials as `{{secret:...}}`
    placeholders (the model sees only `[SECURE:key]`), and returns the recorded
    recipe — which the terminal-frame handler materializes as a coordinator
    workflow and wires on as `login_workflow_id`. Runs ONCE per persona.

    Returns (ok, error, session). Never raises.
    """
    from models.ai_session import AiSession
    from sqlalchemy import select

    try:
        from services.persona_login_record import start_login_record_session

        async with AsyncSessionLocal() as db:
            fresh = await PersonaService.get_owned(db, persona.id)
            if fresh is None:
                return False, "The persona disappeared before its first sign-in.", None
            session_pk, already = await start_login_record_session(db, fresh)
        logger.info(
            f"[PersonaLogin] persona {persona.id}: bootstrapping first sign-in via the "
            f"AI recorder (ai session {session_pk}{', already running' if already else ''})"
        )
    except Exception as e:  # noqa: BLE001 — surface as a normal login failure
        detail = getattr(e, "detail", None) or str(e)
        return False, f"Could not start the first sign-in for this persona: {detail}", None

    waited = 0
    while waited < LOGIN_TIMEOUT_SECONDS:
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        waited += _POLL_INTERVAL_SECONDS
        try:
            async with AsyncSessionLocal() as db:
                status = (await db.execute(
                    select(AiSession.status).where(AiSession.id == session_pk)
                )).scalar_one_or_none()
        except Exception as poll_e:  # noqa: BLE001 — retry next tick
            logger.warning(f"[PersonaLogin] bootstrap poll failed: {poll_e}")
            continue
        # Agent vocabulary: complete | blocked | max_steps | stuck | error | cancelled.
        if status and status != "running":
            break

    session = await _load_persona_session(persona.id)
    if session is None:
        return False, (
            "The AI tried to sign this persona in but no logged-in session was "
            "captured. Check the credentials, or record the sign-in manually."
        ), None
    return True, None, session


async def ensure_fresh_session(
    persona_id: int,
    *,
    force: bool = False,
) -> Tuple[bool, Optional[str], Optional[dict]]:
    """Guarantee the persona has a usable warm session, signing it in if needed.

    Returns (ok, error, session). `force=True` re-runs the login even when the
    current session still looks usable (the "Sign in now" button).

    Safe to call from many shards at once: only one caller runs the login, the rest
    wait on the lock and reuse its result.
    """
    if not force:
        session = await _load_persona_session(persona_id)
        if session is not None:
            # SHAPE IS NOT PROOF — ASK THE SITE. A jar can look perfect and still be
            # anonymous: plenty of stacks (Laravel/Symfony and friends) hand a
            # logged-OUT visitor an HttpOnly session cookie, which sails through both
            # session_is_usable and session_has_auth_material. Reusing it means the
            # run proceeds signed out and quietly returns public pages.
            #
            # Only a PROVEN sign-in bounce (False) re-logins; unknown/transport
            # problems (None) reuse the session exactly as before, so a flaky probe
            # can never cost an extra login. The probe needs a GATED url — a public
            # page answers "signed in" for anyone — so a persona with no known gated
            # page keeps the old behavior.
            if not await _session_is_signed_out(persona_id, session):
                return True, None, session
            logger.info(
                f"[PersonaLogin] persona {persona_id}: warm session is signed OUT — "
                f"signing in again before the run uses it"
            )

    async with AsyncSessionLocal() as db:
        persona = await PersonaService.get_owned(db, persona_id)
        if persona is None or not getattr(persona, "is_active", True):
            return False, (
                "The login identity for this crawl is missing or inactive. "
                "Re-link a persona for the site, then start the crawl."
            ), None
        # NO LOGIN WORKFLOW IS NOT A DEAD END — the credentials are enough. A
        # persona carrying a username/password can be signed in by the AI recorder
        # (it drives a fleet agent's browser, fills the site's own form with
        # {{secret:...}} placeholders, and wires the recorded recipe back on as
        # login_workflow_id), so the FIRST sign-in bootstraps the workflow and every
        # later one replays it.
        needs_bootstrap = not persona.login_workflow_id
        if needs_bootstrap and not has_login_credentials(persona):
            return False, (
                "This persona has no login workflow and no stored credentials, so "
                "it can't sign itself in. Add its username and password (or record "
                "a login workflow), then try again."
            ), None
        persona_snapshot = persona

    lock_key = f"{_LOCK_PREFIX}{persona_id}"
    have_lock = False
    redis = None
    try:
        from utils.redis_client import get_redis
        redis = get_redis()
        have_lock = bool(await redis.set(lock_key, "1", ex=_LOCK_TTL_SECONDS, nx=True))
    except Exception as e:  # noqa: BLE001
        # No lock backend: proceed WITHOUT the lock rather than refusing to log in.
        logger.warning(f"[PersonaLogin] lock unavailable for persona {persona_id}: {e}")
        have_lock = True
        redis = None

    if not have_lock:
        # Someone else is already signing this persona in — wait for THEIR session
        # instead of hammering the site with a second concurrent login.
        session = await _wait_for_another_login(persona_id)
        if session is not None:
            return True, None, session
        return False, (
            "Another sign-in for this persona is already running and didn't "
            "finish in time. Try again in a moment."
        ), None

    try:
        if needs_bootstrap:
            # First sign-in for a credentials-only persona: let the AI recorder
            # establish BOTH the session and the reusable login workflow. Held under
            # this lock like any other login, so shards can't stampede it.
            ok, err, session = await _bootstrap_login_via_ai(persona_snapshot)
            if not ok:
                await _record_login_error(persona_id, err)
                return False, err, None
            return True, None, session

        logger.info(
            f"[PersonaLogin] persona {persona_id}: running login workflow "
            f"{persona_snapshot.login_workflow_id}"
        )
        dispatched, err, task_id = await _dispatch_login_run(persona_snapshot)
        if not dispatched or task_id is None:
            await _record_login_error(persona_id, err)
            return False, err, None

        ok, run_err = await _await_login_task(task_id)
        if not ok:
            await _record_login_error(persona_id, run_err)
            return False, (
                f"The login workflow for this persona failed: {run_err}"
            ), None

        # The run succeeded — but success only means the STEPS ran. If the workflow
        # never actually authenticated (or captured nothing), there is still no
        # session, and letting the crawl proceed here is exactly the silent
        # logged-out crawl this whole path exists to prevent.
        session = await _load_persona_session(persona_id)
        if session is None:
            msg = (
                "The login workflow ran but captured no session. Check that it "
                "actually signs in and ends on a logged-in page, then try again."
            )
            await _record_login_error(persona_id, msg)
            return False, msg, None

        await _record_login_error(persona_id, None)
        logger.info(f"[PersonaLogin] persona {persona_id}: signed in, session captured")
        return True, None, session
    finally:
        if redis is not None:
            try:
                await redis.delete(lock_key)
            except Exception:  # noqa: BLE001
                pass  # TTL reaps it.
