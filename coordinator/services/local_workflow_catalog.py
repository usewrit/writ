"""Local workflow catalog — ingest the daemon-advertised catalog of cloud-callable
LOCAL workflows and answer "what can the cloud call?" queries.

A linked desktop daemon (a user-hosted Agent) advertises its OWN local workflows
over the agent WebSocket as a ``local_catalog`` frame. The ws-gateway STAMPS the
authenticated agent_id; the backend calls ``handle_local_catalog`` with that
TRUSTED agent_id (single-user coordinator — one owner, no tenant scoping).

SECURITY INVARIANT (feedback_never_trust_byo_agents): the only authority for
WHO advertises a local workflow is the authenticated agent identity. The catalog
payload is treated as untrusted display metadata:

  * ``agent_id`` is an argument supplied by the trusted caller and is NEVER read
    off the catalog entries.
  * ``input_schema`` is sanitized to metadata only — any ``steps`` / ``recipe`` /
    ``credentials`` / ``secrets`` keys a misbehaving/compromised daemon tries to
    smuggle in are dropped before persistence.
  * Strings are length-clamped; non-conforming entries are skipped, not fatal.

The catalog is authoritative for the agent: every workflow present is upserted
(active); every previously-stored workflow for that agent that is ABSENT from the
new catalog is marked ``status="withdrawn"`` so it can no longer be invoked.
"""
import logging
import uuid as _uuid
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

from sqlalchemy import select

from models.local_workflow import LocalWorkflow

logger = logging.getLogger(__name__)

# Keys that must NEVER survive into input_schema — they would carry recipe/secrets
# off the device into the cloud, breaking the local-first invariant.
_FORBIDDEN_SCHEMA_KEYS = {
    "steps", "recipe", "credentials", "credentials_encrypted",
    "secrets", "secret", "config", "actions", "script",
}

_MAX_CATALOG_ENTRIES = 500
_MAX_STR = 2000
_MAX_LOCAL_ID = 255
_MAX_NAME = 500


def _clamp(value: Any, limit: int) -> str:
    if value is None:
        return ""
    return str(value)[:limit]


def _sanitize_input_schema(raw: Any) -> Optional[dict]:
    """Return a metadata-only dict, dropping any forbidden (recipe/credential) keys.

    The daemon's declared inputs/output_fields are display metadata; anything
    resembling a recipe or secret is stripped so it can never be persisted cloud-
    side. Non-dict payloads are discarded entirely (stored as NULL)."""
    if not isinstance(raw, dict):
        return None
    cleaned = {
        k: v for k, v in raw.items()
        if isinstance(k, str) and k.lower() not in _FORBIDDEN_SCHEMA_KEYS
    }
    return cleaned or None


async def handle_local_catalog(
    db,
    agent_id: str,
    catalog: Any,
) -> None:
    """UPSERT the daemon's advertised local workflows for the TRUSTED agent.

    ``agent_id`` is the authenticated identity resolved by the caller — it is
    authoritative and never overridden by the payload. ``catalog`` is the
    (untrusted) list of entries from the daemon's ``local_catalog`` frame.

    Every entry present becomes/stays ``active``; every previously-stored workflow
    for this agent absent from ``catalog`` is marked ``withdrawn``.
    Commits its own transaction (mirrors monitoring_ingest's self-contained units).
    """
    if not agent_id:
        logger.warning("local_catalog: refusing ingest with no agent identity")
        return
    if not isinstance(catalog, list):
        logger.warning("local_catalog from %s: payload not a list; ignoring", agent_id)
        return

    now = datetime.now(timezone.utc)

    # Load every existing row for this agent so we can upsert present
    # entries and withdraw absent ones in one pass.
    existing_rows = (
        await db.execute(
            select(LocalWorkflow).where(
                LocalWorkflow.agent_id == agent_id,
            )
        )
    ).scalars().all()
    by_local_id = {row.local_id: row for row in existing_rows}

    seen_local_ids: set = set()

    for entry in catalog[:_MAX_CATALOG_ENTRIES]:
        if not isinstance(entry, dict):
            continue
        local_id = _clamp(entry.get("local_id"), _MAX_LOCAL_ID)
        if not local_id:
            continue  # an entry without a daemon-side id is unrunnable
        if local_id in seen_local_ids:
            continue  # duplicate within one catalog — keep the first
        seen_local_ids.add(local_id)

        name = _clamp(entry.get("name"), _MAX_NAME) or local_id
        description = entry.get("description")
        description = _clamp(description, _MAX_STR) if description is not None else None
        input_schema = _sanitize_input_schema(entry.get("input_schema"))
        cloud_callable = bool(entry.get("cloud_callable", False))
        recipe_hash = entry.get("recipe_hash")
        recipe_hash = _clamp(recipe_hash, 255) if recipe_hash is not None else None

        row = by_local_id.get(local_id)
        if row is None:
            row = LocalWorkflow(
                agent_id=agent_id,
                local_id=local_id,
            )
            db.add(row)
        row.name = name
        row.description = description
        row.input_schema = input_schema
        row.cloud_callable = cloud_callable
        row.recipe_hash = recipe_hash
        row.status = "active"
        row.last_advertised_at = now
        row.updated_at = now

    # Withdraw anything we stored before that is no longer advertised. Never delete
    # (so price/history survive a transient disappearance); just make it un-invokable.
    for local_id, row in by_local_id.items():
        if local_id not in seen_local_ids and row.status != "withdrawn":
            row.status = "withdrawn"
            row.updated_at = now

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.error("local_catalog upsert failed for agent %s: %s", agent_id, e)
        return

    logger.info(
        "local_catalog: agent=%s advertised=%d (withdrew %d)",
        agent_id, len(seen_local_ids),
        sum(
            1 for lid, r in by_local_id.items()
            if lid not in seen_local_ids and r.status == "withdrawn"
        ),
    )


async def list_callable(db) -> List[LocalWorkflow]:
    """All cloud-callable, active local workflows (single-owner coordinator)."""
    rows = (
        await db.execute(
            select(LocalWorkflow).where(
                LocalWorkflow.cloud_callable.is_(True),
                LocalWorkflow.status == "active",
            )
        )
    ).scalars().all()
    return list(rows)


# SQLite/Postgres INTEGER upper bound. A ref larger than this cannot be a row id,
# so we skip the id lookup and treat it as a legacy local_id only.
_PG_INT_MAX = 2_147_483_647


async def resolve_callable_ref(
    db,
    ref: str,
) -> Tuple[Optional[LocalWorkflow], bool]:
    """Resolve a cloud-callable, active local workflow by its PUBLIC ref.

    Fail-closed. Returns ``(row, ambiguous)``:

      * The CANONICAL ref is the coordinator's own row id (``LocalWorkflow.id``) —
        globally unique and stable, so it always names exactly one workflow across
        the whole fleet. Tried first.
      * For back-compat we also accept the legacy daemon-side ``local_id``. That id
        is unique only per AGENT, so a fleet of several agents can have many
        workflows sharing one ``local_id`` and the legacy form can no longer name a
        specific one. When it matches >1 row we return ``(None, True)`` so the
        caller re-issues with the numeric id — never a 500 from
        ``scalar_one_or_none`` choking on multiple rows.
    """
    ref = str(ref or "").strip()
    if not ref:
        return None, False

    # 1) Canonical: the coordinator's own globally-unique row id.
    if ref.isdigit() and int(ref) <= _PG_INT_MAX:
        by_id = (
            await db.execute(
                select(LocalWorkflow).where(
                    LocalWorkflow.id == int(ref),
                    LocalWorkflow.cloud_callable.is_(True),
                    LocalWorkflow.status == "active",
                )
            )
        ).scalar_one_or_none()
        if by_id is not None:
            return by_id, False

    # 2) Back-compat: the legacy daemon-side local_id (unique only per agent).
    #    .limit(2) makes this a cheap "one row or many?" probe.
    legacy = (
        await db.execute(
            select(LocalWorkflow)
            .where(
                LocalWorkflow.local_id == ref,
                LocalWorkflow.cloud_callable.is_(True),
                LocalWorkflow.status == "active",
            )
            .limit(2)
        )
    ).scalars().all()
    if len(legacy) == 1:
        return legacy[0], False
    if len(legacy) > 1:
        return None, True
    return None, False
