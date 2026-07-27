"""resolve_callable_ref — canonical coordinator-id addressing + fleet-safe
back-compat for the legacy daemon-side local_id.

Regression guard for the fleet coordinator: two agents that each advertise a
workflow with the SAME daemon-side local_id must not crash resolution. The old
``resolve_callable(local_id)`` matched BOTH rows and ``scalar_one_or_none()``
raised ``MultipleResultsFound`` → HTTP 500. The public run endpoint now addresses
by the coordinator's own globally-unique row id; the legacy local_id is still
accepted but reported *ambiguous* (→ 409) when it names more than one agent's
workflow.

DB-backed: these skip cleanly when no Postgres is reachable (see conftest).
"""
from models.local_workflow import LocalWorkflow
from services import local_workflow_catalog

# local_ids kept well above the serial `id` range so a ref never accidentally
# matches BOTH an id and a local_id (that overlap is exactly the trap under test).
_LID_A = "900026"
_LID_SOLO = "900099"


async def _add(db, *, agent_id, local_id, cloud_callable=True, status="active"):
    row = LocalWorkflow(
        agent_id=agent_id,
        local_id=local_id,
        name=f"{agent_id}:{local_id}",
        cloud_callable=cloud_callable,
        status=status,
    )
    db.add(row)
    await db.flush()  # assign the serial id without committing (fixture rolls back)
    return row


async def test_resolve_by_coordinator_id_is_fleet_unambiguous(db_session):
    # Two DIFFERENT agents, each advertising a workflow numbered local_id=900026.
    a = await _add(db_session, agent_id="agent-A", local_id=_LID_A)
    b = await _add(db_session, agent_id="agent-B", local_id=_LID_A)

    got_a, amb_a = await local_workflow_catalog.resolve_callable_ref(db_session, str(a.id))
    got_b, amb_b = await local_workflow_catalog.resolve_callable_ref(db_session, str(b.id))

    # The canonical id names exactly one workflow, on the right agent.
    assert amb_a is False and got_a is not None and got_a.id == a.id
    assert got_a.agent_id == "agent-A"
    assert amb_b is False and got_b is not None and got_b.id == b.id
    assert got_b.agent_id == "agent-B"


async def test_legacy_local_id_shared_across_fleet_is_ambiguous_not_500(db_session):
    await _add(db_session, agent_id="agent-A", local_id=_LID_A)
    await _add(db_session, agent_id="agent-B", local_id=_LID_A)

    # The old form (address by the daemon local_id) now matches >1 agent →
    # reported ambiguous, NOT a MultipleResultsFound crash.
    row, ambiguous = await local_workflow_catalog.resolve_callable_ref(db_session, _LID_A)
    assert row is None
    assert ambiguous is True


async def test_legacy_local_id_unique_still_resolves(db_session):
    solo = await _add(db_session, agent_id="solo", local_id=_LID_SOLO)

    row, ambiguous = await local_workflow_catalog.resolve_callable_ref(db_session, _LID_SOLO)
    assert ambiguous is False
    assert row is not None and row.id == solo.id


async def test_unknown_and_uncallable_refs_fail_closed(db_session):
    withdrawn = await _add(
        db_session, agent_id="a", local_id="900001", status="withdrawn"
    )
    uncallable = await _add(
        db_session, agent_id="a", local_id="900002", cloud_callable=False
    )

    # Unknown / empty refs.
    assert await local_workflow_catalog.resolve_callable_ref(db_session, "424242") == (None, False)
    assert await local_workflow_catalog.resolve_callable_ref(db_session, "") == (None, False)

    # Withdrawn and not-cloud_callable never resolve — by canonical id OR local_id.
    assert await local_workflow_catalog.resolve_callable_ref(db_session, str(withdrawn.id)) == (None, False)
    assert await local_workflow_catalog.resolve_callable_ref(db_session, "900001") == (None, False)
    assert await local_workflow_catalog.resolve_callable_ref(db_session, str(uncallable.id)) == (None, False)
    assert await local_workflow_catalog.resolve_callable_ref(db_session, "900002") == (None, False)
