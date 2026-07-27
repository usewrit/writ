"""Translate API key scopes from the per-resource CRUD matrix to v2 scope strings.

Revision ID: 0013_api_key_scopes_v2
Revises: 0012_crawl_targeting_content_reconcile
Create Date: 2026-07-26

The old shape was a per-resource permission matrix::

    {"workflows": {"permissions": ["read", "write"], "ids": null},
     "checks":    {"permissions": ["read"],          "ids": [4, 5]}}

The new one is a flat scope list with per-resource object pins (see
``security/api_scopes.py``)::

    {"v": 2,
     "scopes": ["workflows:read", "workflows:write", "monitors:read"],
     "ids": {"monitors": [4, 5]}}

WITHOUT this, a key written under the old shape still AUTHENTICATES but grants
nothing: `granted_scopes()` reads `blob["scopes"]`, finds no list, and returns an
empty set. Every call then fails — and the id-pinned ones fail with the
particularly confusing "api key not scoped to checks #4", because the pin lookup
also comes back empty. The keys look fine in the UI and work for nothing.

Deliberately NOT a widening: each granted action maps to its equivalent, and
nothing is invented. In particular no key gains `*:execute` here. Under the old
model `execute`/`run` was not expressible at all — every routers-side check for it
failed — so granting it now would hand out a capability the key never had. A key
that needs to trigger runs must be granted `workflows:execute` explicitly, which
is possible for the first time.
"""
import json
import logging

import sqlalchemy as sa
from alembic import op

revision = "0013_api_key_scopes_v2"
down_revision = "0012_crawl_targeting_content_reconcile"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

# Old resource name -> new resource. `checks`/`targets`/`signals` were three names
# for the monitoring surface; `automations` and `workflows` were two for one.
_RESOURCE = {
    "workflows": "workflows",
    "automations": "workflows",
    "checks": "monitors",
    "targets": "monitors",
    "signals": "monitors",
    "streaming": "streaming",
    "files": "files",
}

# Old action -> new action, PER new resource. `streaming` has no `write` in the new
# vocabulary — the routes an old streaming:write key could reach (session mutation)
# are `streaming:execute` now, so that is the faithful mapping, not a widening.
_ACTION_BY_RESOURCE = {
    "workflows": {"read": "read", "write": "write", "delete": "delete"},
    "monitors": {"read": "read", "write": "write", "delete": "delete"},
    "streaming": {"read": "read", "write": "execute", "delete": "delete"},
    "files": {"read": "read", "write": "write", "delete": "delete"},
}

# Only these carry object pins in the new model.
_PINNABLE = {"workflows", "monitors", "datasets"}


def _translate(blob):
    """v1 matrix -> v2 blob. Returns None when there is nothing to change."""
    if not isinstance(blob, dict):
        return None
    if blob.get("v") == 2:
        return None  # already migrated

    scopes: set[str] = set()
    ids: dict[str, list[int]] = {}
    platform_admin = False

    for raw_resource, raw_scope in blob.items():
        # The old platform-admin marker was a pseudo-resource in the same dict.
        if raw_resource == "platform":
            perms = (raw_scope or {}).get("permissions") if isinstance(raw_scope, dict) else None
            if perms and "admin" in perms:
                platform_admin = True
            continue
        resource = _RESOURCE.get(raw_resource)
        if resource is None or not isinstance(raw_scope, dict):
            continue
        action_map = _ACTION_BY_RESOURCE.get(resource, {})
        for perm in raw_scope.get("permissions") or []:
            action = action_map.get(perm)
            if action:
                scopes.add(f"{resource}:{action}")
        pinned = raw_scope.get("ids")
        if isinstance(pinned, list) and pinned and resource in _PINNABLE:
            merged = set(ids.get(resource, [])) | {int(i) for i in pinned}
            ids[resource] = sorted(merged)

    # Drop pins for resources the key ended up holding no scope on — they would
    # read as a restriction on the detail screen while granting nothing.
    ids = {r: v for r, v in ids.items() if any(s.startswith(f"{r}:") for s in scopes)}

    out = {"v": 2, "scopes": sorted(scopes), "ids": ids}
    if platform_admin:
        out["platform_admin"] = True
    return out


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, label, scopes FROM api_keys WHERE scopes IS NOT NULL")
    ).fetchall()

    migrated = 0
    emptied = []
    for row in rows:
        blob = row.scopes
        if isinstance(blob, str):  # SQLite / JSON-as-text
            try:
                blob = json.loads(blob)
            except (TypeError, ValueError):
                continue
        translated = _translate(blob)
        if translated is None:
            continue
        conn.execute(
            sa.text("UPDATE api_keys SET scopes = :s WHERE id = :id"),
            {"s": json.dumps(translated), "id": row.id},
        )
        migrated += 1
        if not translated["scopes"] and not translated.get("platform_admin"):
            emptied.append(f"#{row.id} {row.label!r}")

    logger.info("api_key scopes: migrated %d key(s) to the v2 vocabulary", migrated)
    if emptied:
        # Loud on purpose: these authenticate but can now do nothing, and the
        # operator has to decide what to grant them.
        logger.warning(
            "api_key scopes: %d key(s) had NO translatable permissions and now grant "
            "nothing — re-scope or delete them: %s",
            len(emptied),
            ", ".join(emptied),
        )


def downgrade() -> None:
    # One-way: the v1 matrix cannot represent the v2 vocabulary (no `execute`, no
    # crawl/secrets/personas/... resources), so rolling back would silently drop
    # grants. Re-create the keys instead.
    pass
