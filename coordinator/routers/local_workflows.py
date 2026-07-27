"""Public API — cloud-callable LOCAL workflows (/api/v1/local-workflows/*).

The owner links a desktop daemon (a user-hosted Agent). The daemon advertises its
OWN local workflows by id over the agent WS; the coordinator stores ONLY metadata
about each one (name/description/declared inputs, a recipe hash, a cloud_callable
flag — see models.local_workflow.LocalWorkflow). The recipe (steps) and the
credentials NEVER leave the device. A caller dispatches a run by the workflow's
canonical coordinator id and the daemon executes it locally (by its own
``local_id``, read off the resolved row), returning extracted data the same way a
normal task_result does.

This router exposes two surfaces, both gated by the SAME scoped-API-key dependency
the rest of /api/v1/* uses (security.api_key.get_current_api_key):

  GET  /api/v1/local-workflows               → metadata-only list of the owner's
                                                cloud-callable workflows
  POST /api/v1/local-workflows/{id}/run      → dispatch a run to the owner's
                                                connected daemon, await the result,
                                                return it

SECURITY INVARIANT (feedback_never_trust_byo_agents): the target daemon (agent_id)
is taken ONLY from the LocalWorkflow row (which the catalog upsert stamped from the
authenticated agent identity). NOTHING in the request body or the daemon's reply is
trusted for routing.
"""
from __future__ import annotations

import logging
import uuid as _uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from security.api_key import get_current_api_key
from security.dependencies import check_api_key_scope
from services import local_workflow_catalog

logger = logging.getLogger(__name__)

# Self-prefixed like the other /api/v1/* routers (custom_apis, files v1): included
# in main.py with NO prefix so the full path is exactly /api/v1/local-workflows/*.
router = APIRouter(tags=["Public API · Local Workflows"])

# Hard cap on how long a synchronous cloud→daemon run may block the request.
# Mirrors the public run endpoints' upper bound (custom_apis._poll_task / gateway
# dispatch default 300s) so a wedged daemon can never pin a worker indefinitely.
_RUN_TIMEOUT_S = 300


@router.get("/api/v1/local-workflows")
async def list_local_workflows(
    api_key: dict = Depends(get_current_api_key),
    db: AsyncSession = Depends(get_db),
):
    """List the owner's cloud-callable local workflows (METADATA ONLY).

    Never exposes steps/recipe/credentials — input_schema carries declared
    inputs/output_fields only, stamped by the catalog upsert. ``workflows:read``
    scope is enforced when the key carries scopes.
    """
    check_api_key_scope(api_key, "workflows", "read")
    rows = await local_workflow_catalog.list_callable(db)
    return {
        "object": "list",
        "data": [lw.to_dict() for lw in rows],
    }


@router.post("/api/v1/local-workflows/{lw_ref}/run")
async def run_local_workflow(
    lw_ref: str,
    request: Request,
    api_key: dict = Depends(get_current_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Dispatch a run of a cloud-callable local workflow to the owner's daemon.

    ``lw_ref`` is the workflow's coordinator row id — the canonical, globally-unique
    ``id`` returned by GET /api/v1/local-workflows, which names exactly one workflow
    across the whole fleet. The legacy daemon-side ``local_id`` is still accepted
    for back-compat, but it is unique only per agent, so once the fleet has several
    agents it can name more than one workflow and is rejected as ambiguous (409) —
    callers must use the numeric id. Resolution is fail-closed: an unknown /
    withdrawn / not-cloud_callable ref yields 404. The target daemon (agent_id) is
    read ONLY from the resolved row — never the body. The run is dispatched with the
    FIXED wire contract:

        {"type":"run_local_workflow","task_id":<uuid>,
         "local_workflow_id":<local_id>,"inputs":{...}}

    and the daemon replies with a normal task_result (same shape/handling the
    dispatcher already stamps + the coordinator correlates by task_id).
    """
    check_api_key_scope(api_key, "workflows", "execute")

    # Inputs are METADATA-shaped declared inputs; pass through verbatim. The recipe
    # that consumes them lives on the device — the coordinator never sees/validates steps.
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    inputs = body.get("inputs") if isinstance(body, dict) else None
    if not isinstance(inputs, dict):
        inputs = {}

    # Resolve by the PUBLIC ref (coordinator id, canonical; legacy local_id for
    # back-compat). Fail-closed inside resolve_callable_ref: a legacy local_id that
    # now matches several fleet agents is ambiguous (409), and an unknown /
    # withdrawn / not-callable ref is 404.
    lw, ambiguous = await local_workflow_catalog.resolve_callable_ref(db, lw_ref)
    if ambiguous:
        raise HTTPException(
            status_code=409,
            detail=(
                "This id matches local workflows on more than one agent in your "
                "fleet. Call it by the numeric `id` from GET "
                "/api/v1/local-workflows instead."
            ),
        )
    if lw is None:
        raise HTTPException(status_code=404, detail="Local workflow not found or not callable.")

    target_agent_id = str(lw.agent_id or "")
    if not target_agent_id:
        raise HTTPException(status_code=409, detail="Local workflow has no owning agent.")

    # Pick the CONNECTED agent for THIS workflow from the live fleet registry,
    # filtered to the owning agent_id. Both agent roles qualify on self-host: a
    # linked desktop daemon connects as `user-hosted`, while a writ-agent-fleet
    # worker connects with an infrastructure-role fleet token — in the
    # single-owner deployment both are the owner's own machines, and the fleet
    # bridge handles run_local_workflow identically. Routing still trusts ONLY
    # the LocalWorkflow row's agent_id (see the security invariant above), so
    # widening the role filter cannot re-target a run. If that exact agent is
    # not currently connected, fail fast with 409 rather than queue indefinitely
    # (the caller is a synchronous API client).
    from routers.user_recorder_ws import get_connected_recorders

    connected = [
        r
        for r in get_connected_recorders()
        if r.get("role") in ("user-hosted", "infrastructure")
    ]
    if not any(str(r.get("agent_id")) == target_agent_id for r in connected):
        raise HTTPException(
            status_code=409,
            detail="The daemon for this local workflow is not connected.",
        )

    # Correlate by a fresh UUID task_id. The daemon echoes it on the task_result;
    # the direct-socket handler (_handle_task_result) resolves our in-process future
    # by task_id, enforcing that the reply came from the agent this task was
    # dispatched to (the forgery guard against _dispatched_tasks). The UUID is
    # non-numeric, so the DB-completion fallback (AutomationTask by int id) correctly
    # NO-OPs — these runs have no AutomationTask row.
    task_id = str(_uuid.uuid4())
    message = {
        "type": "run_local_workflow",
        "task_id": task_id,
        "local_workflow_id": lw.local_id,
        "inputs": inputs,
    }

    from routers.user_recorder_ws import push_to_recorder

    logger.info(
        "[LocalWorkflow] dispatching run task=%s local_id=%s agent=%s",
        task_id, lw.local_id, target_agent_id,
    )

    # push_to_recorder writes straight to the agent's socket and awaits its
    # `task_result` (run_local_workflow is a reply-awaited type). It caps the wait at
    # its own _RUN_TIMEOUT_S-equivalent (300s) internal future timeout. Returns None
    # when the agent is not connected or the wait times out; else the daemon's result
    # dict. The executor-reported success only chooses the metering branch below.
    result = await push_to_recorder(target_agent_id, message)
    timed_out = result is None
    success = bool(result.get("success")) if isinstance(result, dict) else False
    extracted_data = (result or {}).get("extracted_data") if isinstance(result, dict) else None
    error = (result or {}).get("error") if isinstance(result, dict) else None
    if timed_out:
        error = error or "timeout"
    duration_ms = 0
    if isinstance(result, dict):
        try:
            duration_ms = int(result.get("duration_ms") or 0)
        except (TypeError, ValueError):
            duration_ms = 0

    # Self-host coordinator: no billing/metering. The browser runs on the caller's
    # own linked daemon; the run already happened on the device. We simply surface
    # the daemon's reported result. (Billing paths are not part of this build.)
    return {
        "task_id": task_id,
        "success": success,
        "extracted_data": extracted_data,
        "error": error,
        "duration_ms": duration_ms,
    }
