"""
transfer_bundle — build and read the BODY of a `.writ` transfer package (self-host).

Spec: `DATA_PORTABILITY_SPEC.md` §6-§9. `transfer_codec.py` (a byte-identical twin
of the cloud module) owns the container; this module owns the meaning — which rows
travel, what gets stripped, what becomes a slot.

NOT A TWIN of the cloud edition's bundle module, and deliberately so. The two
editions differ in the data model, not in the wire format:

  * **no tenant.** A self-host coordinator is single-owner, so there is no
    `tenant_id` to scope by and no cross-tenant guard to enforce.
  * **no managed endpoints, no marketplace, no plan limits.** Those cloud concepts
    have no table here.
  * **`AiSession` is a RUN RECORD here, not a recipe.** In the cloud an AI session
    is a reusable, schedulable asset; on self-host the row records one execution.
    A run record is not portable work, so AI sessions are not exported.

Both editions must still READ each other's packages — that is the whole point of
the format — so the kinds this edition cannot create are recognised and REPORTED
(`UNSUPPORTED_KINDS`), never silently skipped and never a parse failure. A cloud
package with three managed endpoints imports its workflows and monitors and tells
the user, in words, that the endpoints did not come across.

STREAMING. `stream_body()` emits JSON incrementally — one asset at a time, data
rows in keyset-paginated batches — so a large export never exists as a Python
object.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable, Optional, Union

from sqlalchemy import func, select

logger = logging.getLogger(__name__)

#: Body schema generation. MUST match the cloud module: the two editions exchange
#: packages, so this number is a shared contract, not a local choice.
PAYLOAD_VERSION = 1

#: Kinds this edition can EXPORT and IMPORT, in commit dependency order.
KIND_ORDER = (
    "personas",
    "workflows",
    "monitors",
    "crawls",
    "automations",
    "webhooks",
)

#: Kinds a CLOUD package may contain that this edition has no table for. Recognised
#: so they are reported with a reason (spec §2.6) instead of vanishing.
UNSUPPORTED_KINDS = {
    "endpoints": "This install does not have managed API endpoints — the workflow behind it still imports.",
    "ai_sessions": "AI sessions are run records here, not reusable assets, so they cannot be imported.",
}

#: ref-token prefixes. Identical to the cloud map — a ref written by one edition is
#: resolved by the other.
REF_PREFIX = {
    "workflows": "wf",
    "automations": "auto",
    "monitors": "mon",
    "selectors": "sel",
    "extractors": "ext",
    "crawls": "crawl",
    "personas": "pers",
    "ai_sessions": "ai",
    "endpoints": "ep",
    "webhooks": "hook",
}

DATA_PAGE_SIZE = 2_000
MAX_DATA_ROWS_PER_ASSET = 500_000

#: Columns that must NEVER appear in a package body (spec §9). Same list as cloud,
#: minus the columns this edition does not have.
BANNED_FIELD_NAMES = frozenset({
    "credentials_encrypted", "totp_seed_encrypted", "session_state_encrypted",
    "proxy_config_encrypted", "auth_session_encrypted", "value_encrypted",
    "secret_encrypted", "relay_token", "relay_address", "fetch_key",
    "access_token", "refresh_token", "oauth_access_token", "oauth_refresh_token",
    "api_key", "api_key_hash", "webhook_trigger_token",
    "token", "secret", "password", "passphrase",
})

STRIP_LITERAL_KEYS = frozenset({
    "recipients", "url", "email_subject", "title",
    "entry_url", "start_url", "target_url", "page_url",
    "credentials", "form_data", "user_context",
})

BLOCK_ID_KEYS = {
    "workflow_id": ("workflows", "workflow_ref"),
    "target_id": ("monitors", "target_ref"),
    "target_selector_id": ("selectors", "selector_ref"),
    "selector_id": ("selectors", "selector_ref"),
    "webhook_trigger_id": ("webhooks", "webhook_ref"),
    "persona_id": ("personas", "persona_ref"),
    "crawl_definition_id": ("crawls", "crawl_ref"),
    # Recognised so a cloud package's session reference is DROPPED rather than
    # carried as a dangling id into a table that means something else here.
    "ai_session_id": ("ai_sessions", "ai_session_ref"),
}

#: Streaming-recipe fields, carried so a streaming workflow round-trips.
_STREAMING_FIELDS = ("streaming_config",)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)\+?\d[\d\-\s().]{7,}\d(?!\d)")
_LONG_SECRET_RE = re.compile(r"\b(sk|pk|rk|ghp|xox[bap]|AKIA|wto|wlt)[-_][A-Za-z0-9_\-]{12,}", re.I)
_PLACEHOLDER_RE = re.compile(r"\{\{\s*(secret|vault|input)\s*:\s*([A-Za-z_][A-Za-z0-9_.\-]*)\s*\}\}")


class BundleError(Exception):
    code = "BUNDLE_ERROR"


class MalformedBundle(BundleError):
    code = "MALFORMED"


class BundleNotClean(BundleError):
    """The export guard found something that must never leave. Always a BUG in a
    projector, never user error — loud, and it aborts the export."""

    code = "NOT_CLEAN"


# ---------------------------------------------------------------------------
# Selection / plan
# ---------------------------------------------------------------------------

@dataclass
class BundleSelection:
    workflows: list[int] = field(default_factory=list)
    automations: list[int] = field(default_factory=list)
    monitors: list[int] = field(default_factory=list)
    crawls: list[int] = field(default_factory=list)
    personas: Union[str, list[int]] = "referenced"
    include_data: dict[str, list[int]] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict) -> "BundleSelection":
        payload = payload or {}
        personas_raw = payload.get("personas", "referenced")
        if isinstance(personas_raw, str):
            if personas_raw not in ("referenced", "none", "all"):
                raise MalformedBundle("personas must be 'referenced', 'all', 'none', or a list of ids")
            personas: Union[str, list[int]] = personas_raw
        else:
            personas = _ids(personas_raw, "personas")

        include_raw = payload.get("include_data") or {}
        include: dict[str, list[int]] = {}
        if include_raw:
            if not isinstance(include_raw, dict):
                raise MalformedBundle("include_data must be an object keyed by asset kind")
            for kind, id_list in include_raw.items():
                if kind != "workflows":
                    raise MalformedBundle(f"data export is not supported for {kind}")
                include[kind] = _ids(id_list, kind)

        # Kinds this edition cannot export: refuse loudly rather than returning an
        # empty section the caller reads as "there were none".
        for kind in UNSUPPORTED_KINDS:
            if payload.get(kind):
                raise MalformedBundle(f"this install has no {kind} to export")

        sel = cls(
            workflows=_ids(payload.get("workflows"), "workflows"),
            automations=_ids(payload.get("automations"), "automations"),
            monitors=_ids(payload.get("monitors"), "monitors"),
            crawls=_ids(payload.get("crawls"), "crawls"),
            personas=personas,
            include_data=include,
        )
        if not sel.any_selected():
            raise MalformedBundle("select at least one asset to export")
        return sel

    def any_selected(self) -> bool:
        return bool(
            self.workflows or self.automations or self.monitors or self.crawls
            or (isinstance(self.personas, list) and self.personas)
            or self.personas == "all"
        )


def _ids(raw: Any, label: str) -> list[int]:
    raw = raw or []
    if not isinstance(raw, (list, tuple)):
        raise MalformedBundle(f"{label} must be a list of ids")
    out: list[int] = []
    for v in raw:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            raise MalformedBundle(f"{label} contains a non-numeric id")
    return list(dict.fromkeys(out))


@dataclass
class SkippedAsset:
    kind: str
    name: str
    reason: str
    detail: str = ""
    source_id: Optional[int] = None

    def as_dict(self) -> dict:
        return {"kind": self.kind, "name": self.name, "reason": self.reason, "detail": self.detail}


@dataclass
class BundlePlan:
    rows: dict[str, list[Any]] = field(default_factory=dict)
    refs: dict[tuple[str, int], str] = field(default_factory=dict)
    selectors: dict[int, list[Any]] = field(default_factory=dict)
    extractors: dict[int, list[Any]] = field(default_factory=dict)
    data_for: set[int] = field(default_factory=set)
    data_row_counts: dict[int, int] = field(default_factory=dict)
    data_run_counts: dict[int, int] = field(default_factory=dict)
    skipped: list[SkippedAsset] = field(default_factory=list)
    requirements: dict[str, list[dict]] = field(default_factory=dict)

    def ref_for(self, kind: str, source_id: Optional[int]) -> Optional[str]:
        if source_id is None:
            return None
        return self.refs.get((kind, int(source_id)))

    def counts(self) -> dict:
        out = {kind: len(self.rows.get(kind, [])) for kind in KIND_ORDER}
        out["data_rows"] = sum(self.data_row_counts.values())
        out["data_runs"] = sum(self.data_run_counts.values())
        return out

    def requires(self) -> dict:
        return {
            "logins": len(self.requirements.get("persona_slots", [])),
            "keys": len(self.requirements.get("secret_slots", [])),
            "inputs": len(self.requirements.get("input_slots", [])),
            "files": len(self.requirements.get("file_slots", [])),
        }


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

class BundleBuilder:
    """Resolves a selection into a plan, then streams the body.

    No tenant scoping: this coordinator is single-owner, and inventing a filter
    that always matches would just be misleading.
    """

    def __init__(self, db, owner_user_id=None):
        self.db = db
        self.owner_user_id = owner_user_id
        self._seq: dict[str, int] = {}

    def _alloc(self, plan: BundlePlan, kind: str, source_id: int) -> str:
        key = (kind, int(source_id))
        if key in plan.refs:
            return plan.refs[key]
        self._seq[kind] = self._seq.get(kind, 0) + 1
        token = f"{REF_PREFIX[kind]}:{self._seq[kind]}"
        plan.refs[key] = token
        return token

    async def plan(self, selection: BundleSelection) -> BundlePlan:
        from models.automation_workflow import AutomationWorkflow
        from models.crawl_definition import CrawlDefinition
        from models.persona import Persona
        from models.selector_extractor import SelectorExtractor
        from models.target import Target
        from models.target_selector import TargetSelector
        from models.trigger_rule import TriggerRule
        from models.webhook_trigger import WebhookTrigger

        plan = BundlePlan()
        plan.rows["workflows"] = await self._load(AutomationWorkflow, selection.workflows)
        plan.rows["monitors"] = await self._load(Target, selection.monitors)
        plan.rows["automations"] = await self._load(TriggerRule, selection.automations)
        plan.rows["crawls"] = await self._load(CrawlDefinition, selection.crawls)

        monitors = plan.rows["monitors"]
        if monitors:
            sel_res = await self.db.execute(
                select(TargetSelector)
                .where(TargetSelector.target_id.in_([m.id for m in monitors]))
                .order_by(TargetSelector.target_id, TargetSelector.priority, TargetSelector.id)
            )
            for s in sel_res.scalars().all():
                plan.selectors.setdefault(s.target_id, []).append(s)
            sel_ids = [s.id for group in plan.selectors.values() for s in group]
            if sel_ids:
                ext_res = await self.db.execute(
                    select(SelectorExtractor)
                    .where(SelectorExtractor.target_selector_id.in_(sel_ids))
                    .order_by(SelectorExtractor.target_selector_id, SelectorExtractor.id)
                )
                for e in ext_res.scalars().all():
                    plan.extractors.setdefault(e.target_selector_id, []).append(e)

        # Webhooks travel by reference only — never selected directly.
        hook_ids: set[int] = {
            int(a.webhook_trigger_id) for a in plan.rows["automations"]
            if getattr(a, "webhook_trigger_id", None)
        }
        wf_ids = {w.id for w in plan.rows["workflows"]}
        if wf_ids:
            res = await self.db.execute(select(WebhookTrigger).where(WebhookTrigger.workflow_id.in_(wf_ids)))
            hook_ids.update(int(h.id) for h in res.scalars().all())
        plan.rows["webhooks"] = await self._load(WebhookTrigger, sorted(hook_ids)) if hook_ids else []

        # Personas: exactly the ones the selection is wired to, by default.
        persona_ids: set[int] = set()
        if selection.personas == "referenced":
            for wf in plan.rows["workflows"]:
                if getattr(wf, "default_persona_id", None):
                    persona_ids.add(int(wf.default_persona_id))
            for mon in monitors:
                if getattr(mon, "persona_id", None):
                    persona_ids.add(int(mon.persona_id))
        elif selection.personas == "all":
            res = await self.db.execute(select(Persona.id))
            persona_ids = {int(r) for r in res.scalars().all()}
        elif isinstance(selection.personas, list):
            persona_ids = {int(p) for p in selection.personas}
        plan.rows["personas"] = await self._load(Persona, sorted(persona_ids)) if persona_ids else []

        for kind in KIND_ORDER:
            for row in plan.rows.get(kind, []):
                self._alloc(plan, kind, row.id)
        for sels in plan.selectors.values():
            for s in sels:
                self._alloc(plan, "selectors", s.id)
                for e in plan.extractors.get(s.id, []):
                    self._alloc(plan, "extractors", e.id)

        for wf_id in selection.include_data.get("workflows", []):
            if ("workflows", int(wf_id)) not in plan.refs:
                plan.skipped.append(SkippedAsset(
                    "data", f"workflow #{wf_id}", "asset_not_included",
                    "Data can only be exported alongside its workflow.",
                ))
                continue
            plan.data_for.add(int(wf_id))
            runs, records = await self._count_data_rows(int(wf_id))
            plan.data_run_counts[int(wf_id)] = runs
            plan.data_row_counts[int(wf_id)] = records

        plan.requirements = self._collect_requirements(plan)
        return plan

    async def _load(self, model, ids: Iterable[int]) -> list[Any]:
        ids = [int(i) for i in (ids or [])]
        if not ids:
            return []
        res = await self.db.execute(select(model).where(model.id.in_(ids)).order_by(model.id))
        return list(res.scalars().all())

    async def _count_data_rows(self, workflow_id: int) -> tuple[int, int]:
        """`(runs, records)`. Records are summed IN SQLite (`json_array_length`)
        rather than by reading every blob, so the export preview's size is the real
        one at a cost that does not grow with the data. Falls back to the run count
        if the JSON1 extension is unavailable."""
        from sqlalchemy import text

        from models.automation_task import AutomationTask

        runs_res = await self.db.execute(
            select(func.count()).select_from(AutomationTask)
            .where(AutomationTask.workflow_id == workflow_id)
            .where(func.json_extract(AutomationTask.result_data, "$.extracted_data").isnot(None))
        )
        runs = int(runs_res.scalar() or 0)
        if not runs:
            return 0, 0
        try:
            rec = await self.db.execute(
                text(
                    "SELECT COALESCE(SUM(CASE "
                    "  WHEN json_type(result_data, '$.extracted_data') = 'array' "
                    "  THEN json_array_length(result_data, '$.extracted_data') "
                    "  ELSE 1 END), 0) "
                    "FROM automation_tasks "
                    "WHERE workflow_id = :wf "
                    "  AND json_extract(result_data, '$.extracted_data') IS NOT NULL"
                ),
                {"wf": workflow_id},
            )
            return runs, int(rec.scalar() or 0)
        except Exception:
            logger.debug("transfer: record-count aggregate unavailable; reporting runs", exc_info=True)
            return runs, runs

    def _collect_requirements(self, plan: BundlePlan) -> dict[str, list[dict]]:
        """Union every asset's slots, annotated with the refs that need them, so the
        wizard can say "3 workflows need this key" instead of listing it four times."""
        from services.workflow_manifest import derive_data_manifest

        persona_slots: dict[str, dict] = {}
        secret_slots: dict[str, dict] = {}
        input_slots: dict[str, dict] = {}
        file_slots: dict[str, dict] = {}

        def merge(bucket: dict, key: str, payload: dict, ref: str) -> None:
            entry = bucket.setdefault(key, {**payload, "used_by": []})
            if ref not in entry["used_by"]:
                entry["used_by"].append(ref)

        for wf in plan.rows.get("workflows", []):
            ref = plan.ref_for("workflows", wf.id)
            try:
                manifest = derive_data_manifest(wf)
            except Exception:
                logger.exception("transfer: manifest derivation failed for workflow %s", wf.id)
                manifest = {}
            for slot in manifest.get("persona_slots") or []:
                merge(persona_slots, f"login:{slot.get('domain') or ''}", dict(slot), ref)
            for slot in manifest.get("secret_slots") or []:
                merge(secret_slots, str(slot.get("key")), {**dict(slot), "kind": "vault", "required": True}, ref)
            for slot in manifest.get("input_slots") or []:
                merge(input_slots, str(slot.get("key")), dict(slot), ref)
            for slot in manifest.get("file_slots") or []:
                merge(file_slots, str(slot.get("slot")), dict(slot), ref)

        for persona in plan.rows.get("personas", []):
            ref = plan.ref_for("personas", persona.id)
            for name in sorted(_persona_secret_refs(persona)):
                merge(secret_slots, name, {"key": name, "kind": "vault", "required": True,
                                           "persona_satisfiable": True}, ref)

        notify_slots: list[dict] = []
        webhook_slots: list[dict] = []
        for auto in plan.rows.get("automations", []):
            ref = plan.ref_for("automations", auto.id)
            for idx, block in enumerate(auto.blocks or []):
                if isinstance(block, dict) and (block.get("blockType") or block.get("type")) in ("notification", "notify"):
                    notify_slots.append({
                        "slot": f"{ref}/notify.{idx}",
                        "channels": _notify_channels(block.get("config") or {}),
                        "used_by": [ref],
                    })
            hook_ref = plan.ref_for("webhooks", getattr(auto, "webhook_trigger_id", None))
            if hook_ref:
                webhook_slots.append({"slot": hook_ref, "direction": "in", "used_by": [ref]})
        for hook in plan.rows.get("webhooks", []):
            hook_ref = plan.ref_for("webhooks", hook.id)
            if hook_ref and not any(w["slot"] == hook_ref for w in webhook_slots):
                webhook_slots.append({"slot": hook_ref, "direction": "in", "used_by": []})

        for mon in plan.rows.get("monitors", []):
            ref = plan.ref_for("monitors", mon.id)
            providers = getattr(mon, "notification_providers", None)
            channels = sorted(providers.keys()) if isinstance(providers, dict) else []
            if channels:
                notify_slots.append({"slot": f"{ref}/change", "channels": channels, "used_by": [ref]})

        return {
            "persona_slots": list(persona_slots.values()),
            "secret_slots": list(secret_slots.values()),
            "input_slots": list(input_slots.values()),
            "file_slots": list(file_slots.values()),
            "notify_slots": notify_slots,
            "webhook_slots": webhook_slots,
            "monitor_url_slots": [],
        }

    # -- body streaming ----------------------------------------------------

    async def stream_body(self, plan: BundlePlan) -> AsyncIterator[bytes]:
        yield b'{"payload_version":' + str(PAYLOAD_VERSION).encode() + b',"assets":{'
        first_kind = True
        for kind in KIND_ORDER:
            if not first_kind:
                yield b","
            first_kind = False
            yield b'"' + kind.encode() + b'":['
            first = True
            for row in plan.rows.get(kind, []):
                projected = self._project(kind, row, plan)
                if projected is None:
                    continue
                if not first:
                    yield b","
                first = False
                yield _dump(projected)
            yield b"]"
        yield b"}"

        yield b',"requirements":' + _dump(plan.requirements)
        # Emitted (empty) so a cloud reader finds the keys it expects rather than
        # branching on their absence.
        yield b',"marketplace_refs":[]'
        yield b',"skipped":' + _dump([s.as_dict() for s in plan.skipped])

        yield b',"data":{'
        first = True
        for wf_id in sorted(plan.data_for):
            ref = plan.ref_for("workflows", wf_id)
            if not ref:
                continue
            if not first:
                yield b","
            first = False
            async for piece in self._stream_data_section(wf_id, ref, plan.data_row_counts.get(wf_id, 0)):
                yield piece
        yield b"}}"

    async def _stream_data_section(self, workflow_id: int, ref: str, known_records: int) -> AsyncIterator[bytes]:
        """One workflow's collected data, grouped by SOURCE RUN — the `latest`/`run`
        lenses are defined over runs, so flattening would destroy change history."""
        from models.automation_task import AutomationTask

        truncated = known_records > MAX_DATA_ROWS_PER_ASSET
        yield b'"' + ref.encode() + b'":{"format":"runs","truncated":'
        yield (b"true" if truncated else b"false") + b',"runs":['

        emitted_rows = 0
        emitted_runs = 0
        last_id = 0
        first = True
        while emitted_rows < MAX_DATA_ROWS_PER_ASSET:
            res = await self.db.execute(
                select(
                    AutomationTask.id, AutomationTask.completed_at, AutomationTask.created_at,
                    AutomationTask.trigger_type, AutomationTask.result_data,
                )
                .where(AutomationTask.workflow_id == workflow_id)
                .where(func.json_extract(AutomationTask.result_data, "$.extracted_data").isnot(None))
                .where(AutomationTask.id > last_id)
                .order_by(AutomationTask.id)          # keyset, never OFFSET
                .limit(DATA_PAGE_SIZE)
            )
            page = res.all()
            if not page:
                break
            for row in page:
                last_id = int(row.id)
                extracted = (row.result_data or {}).get("extracted_data")
                records = extracted if isinstance(extracted, list) else [extracted]
                records = [r for r in records if r is not None]
                if not records:
                    continue
                if emitted_rows + len(records) > MAX_DATA_ROWS_PER_ASSET:
                    records = records[: MAX_DATA_ROWS_PER_ASSET - emitted_rows]
                    truncated = True
                if not records:
                    break
                if not first:
                    yield b","
                first = False
                stamp = row.completed_at or row.created_at
                yield _dump({
                    "completed_at": stamp.isoformat() if stamp else None,
                    "trigger_type": row.trigger_type,
                    "rows": records,
                })
                emitted_rows += len(records)
                emitted_runs += 1
            if len(page) < DATA_PAGE_SIZE:
                break

        yield b'],"row_count":' + str(emitted_rows).encode()
        yield b',"run_count":' + str(emitted_runs).encode() + b"}"

    # -- projectors --------------------------------------------------------

    def _project(self, kind: str, row, plan: BundlePlan) -> Optional[dict]:
        return {
            "workflows": self._project_workflow,
            "automations": self._project_automation,
            "monitors": self._project_monitor,
            "crawls": self._project_crawl,
            "personas": self._project_persona,
            "webhooks": self._project_webhook,
        }[kind](row, plan)

    def _serve_recipe(self, wf) -> dict:
        """The data-LESS projection of a workflow, matching the cloud's
        `marketplace_service.serve_recipe()` field-for-field.

        This edition has no marketplace, so there is no `serve_recipe` to call — but
        the SHAPE has to be identical or a package written here would not hydrate in
        the cloud. Steps keep their `{{...}}` placeholders verbatim; credentials,
        `form_data` values and session state are structurally absent.
        """
        recipe = {
            "steps": getattr(wf, "steps", None) or [],
            "raw_replay": getattr(wf, "raw_replay", None) or [],
            "entry_url": getattr(wf, "entry_url", None),
            "exit_condition": getattr(wf, "exit_condition", None),
            "timeout_ms": getattr(wf, "timeout_ms", None),
            "retry_count": getattr(wf, "retry_count", None),
            "functions": _sanitize_functions(getattr(wf, "functions", None)),
        }
        if getattr(wf, "workflow_type", None) == "streaming":
            for f in _STREAMING_FIELDS:
                recipe[f] = getattr(wf, f, None)
        else:
            recipe["streaming_config"] = None
        return recipe

    def _project_workflow(self, wf, plan: BundlePlan) -> dict:
        from services.workflow_manifest import derive_data_manifest, recipe_hash

        recipe = self._serve_recipe(wf)
        return {
            "ref": plan.ref_for("workflows", wf.id),
            "name": wf.name,
            "description": wf.description,
            "kind": getattr(wf, "workflow_type", None) or "recorded",
            "recipe": recipe,
            "knobs": {
                "headless": bool(wf.headless),
                "fast_mode": bool(wf.fast_mode),
                "session_persistence": bool(getattr(wf, "session_persistence", False)),
                "session_ttl_seconds": getattr(wf, "session_ttl_seconds", None),
                "login_url_patterns": getattr(wf, "login_url_patterns", None) or [],
                "relogin_max_retries": getattr(wf, "relogin_max_retries", None),
                "http_capable": bool(getattr(wf, "http_capable", False)),
                "ai_repair_enabled": bool(getattr(wf, "ai_repair_enabled", False)),
                "input_rules": getattr(wf, "input_rules", None) or {},
                "api_functions": getattr(wf, "api_functions", None) or [],
                "auth_config": _strip_literals(getattr(wf, "auth_config", None) or {}),
                # `execution_target` is a cloud concept; a self-host workflow always
                # runs on this install's own agents, so it is omitted rather than
                # invented.
            },
            # Schedules ALWAYS travel disarmed (spec §6.3).
            "schedule": {
                "enabled": False,
                "kind": getattr(wf, "schedule_kind", None),
                "interval_ms": getattr(wf, "schedule_interval_ms", None),
                "time": _timestr(getattr(wf, "schedule_time", None)),
                "days": getattr(wf, "schedule_days", None),
                "tz": getattr(wf, "schedule_tz", None),
            },
            "wiring": {
                "persona_ref": plan.ref_for("personas", getattr(wf, "default_persona_id", None)),
                "ai_session_ref": None,
            },
            "manifest": _safe(derive_data_manifest, wf) or {},
            "recipe_hash": _safe(recipe_hash, recipe),
        }

    def _project_automation(self, auto, plan: BundlePlan) -> dict:
        auto_ref = plan.ref_for("automations", auto.id)
        return {
            "ref": auto_ref,
            "name": auto.name,
            "description": auto.description,
            "event_type": auto.event_type,
            "enabled": False,
            "priority": auto.priority,
            "conditions": auto.conditions or {},
            "blocks": [
                self._project_block(b, plan, auto_ref, i) for i, b in enumerate(auto.blocks or [])
            ],
            "actions": [_strip_literals(a) for a in (auto.actions or [])],
            "input_rules": {},
            "wiring": {
                "monitor_ref": plan.ref_for("monitors", getattr(auto, "target_id", None)),
                "selector_ref": plan.ref_for("selectors", getattr(auto, "target_selector_id", None)),
                "workflow_ref": plan.ref_for("workflows", getattr(auto, "workflow_id", None)),
                "ai_session_refs": [],
                "webhook_ref": plan.ref_for("webhooks", getattr(auto, "webhook_trigger_id", None)),
            },
        }

    def _project_block(self, block, plan: BundlePlan, auto_ref: Optional[str], index: int) -> Any:
        """Substitute ref tokens for asset ids; drop creator literals.

        An id-shaped key with no mapping is DROPPED and recorded in `_dropped` —
        carrying it would silently point at whatever row holds that id on the
        importing install, and raising would fail the whole export over one knob.
        """
        if not isinstance(block, dict):
            return block
        out = {k: v for k, v in block.items() if k != "config"}
        config = dict(block.get("config") or {})
        clean: dict[str, Any] = {}
        dropped: list[str] = []
        for key, value in config.items():
            if key in BLOCK_ID_KEYS:
                kind, ref_key = BLOCK_ID_KEYS[key]
                if isinstance(value, list):
                    refs = [plan.ref_for(kind, v) for v in value]
                    resolved = [r for r in refs if r]
                    if resolved:
                        clean[ref_key + "s"] = resolved
                    else:
                        dropped.append(key)
                else:
                    ref = plan.ref_for(kind, value)
                    if ref:
                        clean[ref_key] = ref
                    else:
                        dropped.append(key)
                continue
            if key in STRIP_LITERAL_KEYS or str(key).lower() in BANNED_FIELD_NAMES:
                dropped.append(key)
                continue
            if str(key).endswith("_id") and isinstance(value, int) and not isinstance(value, bool):
                dropped.append(key)
                continue
            clean[key] = value
        if (block.get("blockType") or block.get("type")) in ("notification", "notify") and auto_ref:
            clean["recipient_slot"] = f"{auto_ref}/notify.{index}"
        if dropped:
            out["_dropped"] = sorted(set(dropped))
        out["config"] = clean
        return out

    def _project_monitor(self, mon, plan: BundlePlan) -> dict:
        ref = plan.ref_for("monitors", mon.id)
        providers = getattr(mon, "notification_providers", None)
        return {
            "ref": ref,
            "url": mon.url,
            "check_type": mon.check_type,
            "execution_mode": getattr(mon, "execution_mode", None),
            "selector": mon.selector,
            "ignore_regex": mon.ignore_regex,
            "schedule": {
                "check_period_ms": mon.check_period_ms,
                "kind": getattr(mon, "schedule_kind", None),
                "time": _timestr(getattr(mon, "schedule_time", None)),
                "days": getattr(mon, "schedule_days", None),
                "tz": getattr(mon, "schedule_tz", None),
            },
            "enabled": False,
            "knobs": {
                "expected_status_code": mon.expected_status_code,
                "timeout_ms": mon.timeout_ms,
                "max_response_time_ms": mon.max_response_time_ms,
                "check_ssl": bool(mon.check_ssl),
                "requires_playwright": bool(getattr(mon, "requires_playwright", False)),
                "on_change_enabled": bool(getattr(mon, "on_change_enabled", False)),
                "on_change_in_session": bool(getattr(mon, "on_change_in_session", False)),
                "on_change_conditions": getattr(mon, "on_change_conditions", None) or {},
                "setup_steps": getattr(mon, "setup_steps", None) or [],
                "preferred_region": getattr(mon, "preferred_region", None),
            },
            "presentation": {
                "notification_title": getattr(mon, "notification_title", None),
                "notification_message": getattr(mon, "notification_message", None),
                "notification_priority": getattr(mon, "notification_priority", None),
                "notification_sound": getattr(mon, "notification_sound", None),
            },
            "wiring": {
                "persona_ref": plan.ref_for("personas", getattr(mon, "persona_id", None)),
                "pre_check_workflow_ref": plan.ref_for("workflows", getattr(mon, "pre_check_workflow_id", None)),
                "on_change_workflow_ref": plan.ref_for("workflows", getattr(mon, "on_change_workflow_id", None)),
            },
            # Channel NAMES only; recipients are slots the importer binds.
            "notify": [{
                "slot": f"{ref}/change",
                "channels": sorted(providers.keys()) if isinstance(providers, dict) else [],
            }],
            "selectors": [self._project_selector(s, plan) for s in plan.selectors.get(mon.id, [])],
        }

    def _project_selector(self, sel, plan: BundlePlan) -> dict:
        # Baselines never travel (spec §6.4): an imported monitor establishes its
        # own, and baseline_content is page content — frequently PII.
        return {
            "ref": plan.ref_for("selectors", sel.id),
            "name": sel.name,
            "selector": sel.selector,
            "description": sel.description,
            "content_type": sel.content_type,
            "visual_region": sel.visual_region,
            "ignore_regex": sel.ignore_regex,
            "priority": sel.priority,
            "enabled": bool(sel.enabled),
            "extractors": [{
                "ref": plan.ref_for("extractors", e.id),
                "name": e.name,
                "output_name": e.output_name,
                "extract_type": e.extract_type,
                "config": e.config or {},
                "is_array": bool(e.is_array),
                "default_value": e.default_value,
                "enabled": bool(e.enabled),
            } for e in plan.extractors.get(sel.id, [])],
        }

    def _project_crawl(self, crawl, plan: BundlePlan) -> dict:
        return {
            "ref": plan.ref_for("crawls", crawl.id),
            "name": crawl.name,
            "slug": crawl.slug,
            "description": crawl.description,
            "seed_url": crawl.seed_url,
            "config": crawl.config or {},
            "default_max_age_seconds": crawl.default_max_age_seconds,
        }

    def _project_persona(self, persona, plan: BundlePlan) -> dict:
        """Persona METADATA only. Credential columns are the sealed lane's business
        (§8); session state never travels at all."""
        return {
            "ref": plan.ref_for("personas", persona.id),
            "name": persona.name,
            "description": persona.description,
            "target_domain": persona.target_domain,
            "twofa_method": persona.twofa_method,
            "totp_digits": persona.totp_digits,
            "totp_period_seconds": persona.totp_period_seconds,
            "totp_algorithm": persona.totp_algorithm,
            "email_otp_mode": persona.email_otp_mode,
            "otp_extract_config": getattr(persona, "otp_extract_config", None),
            "fingerprint": getattr(persona, "fingerprint", None),
            "secret_refs": sorted(_persona_secret_refs(persona)),
            "needs": {
                "username": bool(persona.login_username),
                "password": bool(persona.credentials_encrypted),
                "totp_seed": bool(persona.totp_seed_encrypted),
                "proxy": bool(getattr(persona, "proxy_config_encrypted", None)),
            },
        }

    def _project_webhook(self, hook, plan: BundlePlan) -> dict:
        """SHAPE only. `token`/`secret` are minted fresh on import — carrying them
        would hand the recipient a live inbound URL of the sender's install."""
        return {
            "ref": plan.ref_for("webhooks", hook.id),
            "name": hook.name,
            "action": hook.action,
            "payload_mapping": hook.payload_mapping or {},
            "conditions": hook.conditions,
            "wait_for_result": bool(hook.wait_for_result),
            "wait_timeout": hook.wait_timeout,
            "custom_path": hook.custom_path,
            "function_name": hook.function_name,
            "target_ref": plan.ref_for("monitors", getattr(hook, "target_id", None)),
            "workflow_ref": plan.ref_for("workflows", getattr(hook, "workflow_id", None)),
        }


# ---------------------------------------------------------------------------
# The export guard (spec §9) — identical rules to the cloud edition
# ---------------------------------------------------------------------------

def assert_bundle_clean(body: dict, *, allow_placeholders: bool = True) -> None:
    """Fail the export if anything that must never leave survived a projector.

    `{{secret:NAME}}` / `{{input:NAME}}` placeholders are EXPECTED and allowed —
    they are the mechanism, not a leak.
    """
    problems: list[str] = []

    def walk(node: Any, path: str) -> None:
        if len(problems) > 40:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                here = f"{path}.{key}" if path else str(key)
                if str(key).lower() in BANNED_FIELD_NAMES and value not in (None, "", {}, []):
                    problems.append(f"{here}: banned field carries a value")
                    continue
                if str(key).endswith("_id") and isinstance(value, int) and str(key) != "bundle_id":
                    problems.append(f"{here}: source id {value} was not replaced by a ref")
                walk(value, here)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]")
        elif isinstance(node, str):
            _check_string(node, path, problems, allow_placeholders)

    walk(body.get("assets") or {}, "assets")
    walk(body.get("requirements") or {}, "requirements")
    if problems:
        raise BundleNotClean(
            "refusing to export: the package body still contains data that must never "
            "leave this install — " + "; ".join(problems[:10])
        )


def _check_string(value: str, path: str, problems: list[str], allow_placeholders: bool) -> None:
    if not value or len(value) > 100_000:
        return
    scan = _PLACEHOLDER_RE.sub("", value) if allow_placeholders else value
    if _LONG_SECRET_RE.search(scan):
        problems.append(f"{path}: looks like a live API token")
        return
    if path.endswith(("recipients", "recipient", "email", "phone", "to")) and (
        _EMAIL_RE.search(scan) or _PHONE_RE.search(scan)
    ):
        problems.append(f"{path}: carries a contact literal")


# ---------------------------------------------------------------------------
# Reading — must accept a CLOUD-written package, including kinds we cannot create
# ---------------------------------------------------------------------------

def parse_body(raw: bytes) -> dict:
    try:
        body = json.loads(raw)
    except Exception:
        raise MalformedBundle("package body is not valid JSON")
    if not isinstance(body, dict):
        raise MalformedBundle("package body is not an object")
    version = body.get("payload_version")
    if not isinstance(version, int) or version < 1:
        raise MalformedBundle("package body has no usable payload_version")
    if version > PAYLOAD_VERSION:
        raise MalformedBundle(
            f"package body is version {version}; this install understands up to {PAYLOAD_VERSION}"
        )
    assets = body.get("assets")
    if not isinstance(assets, dict):
        raise MalformedBundle("package body has no assets")

    known_refs: set[str] = set()
    for kind, items in assets.items():
        if not isinstance(items, list):
            raise MalformedBundle(f"assets.{kind} is not a list")
        for item in items:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("ref"), str):
                known_refs.add(item["ref"])
            for sel in item.get("selectors") or []:
                if isinstance(sel, dict) and isinstance(sel.get("ref"), str):
                    known_refs.add(sel["ref"])
                    for ext in sel.get("extractors") or []:
                        if isinstance(ext, dict) and isinstance(ext.get("ref"), str):
                            known_refs.add(ext["ref"])

    dangling = sorted(_collect_refs(assets) - known_refs)
    if dangling:
        raise MalformedBundle("package refers to assets it does not contain: " + ", ".join(dangling[:8]))
    return body


#: What a bundle-local ref token looks like: `<kind-prefix>:<n>` (spec §6). Matched
#: on the VALUE, not the key name, because `_refs`-suffixed keys are not all ref
#: lists — `personas[].secret_refs` holds vault KEY NAMES, and treating those as
#: refs made every package with a linked persona secret look malformed.
_REF_TOKEN_RE = re.compile(r"^(?:" + "|".join(sorted(set(REF_PREFIX.values()))) + r"):\d+$")


def _is_ref_token(value: Any) -> bool:
    return isinstance(value, str) and bool(_REF_TOKEN_RE.match(value))


def _collect_refs(node: Any, out: Optional[set[str]] = None) -> set[str]:
    """Every ref token used as a wiring value.

    Collected by TOKEN SHAPE, so a `_refs` key carrying something else (vault key
    names, marketplace slugs) is not mistaken for a dangling asset reference.
    """
    out = out if out is not None else set()
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key).endswith("_ref") and _is_ref_token(value):
                out.add(value)
            elif str(key).endswith("_refs") and isinstance(value, list):
                out.update(v for v in value if _is_ref_token(v))
            else:
                _collect_refs(value, out)
    elif isinstance(node, list):
        for item in node:
            _collect_refs(item, out)
    return out


def unknown_kinds(body: dict) -> list[str]:
    """Kinds no edition of Writ this old knows about (spec §6.6)."""
    return sorted(k for k in (body.get("assets") or {}) if k not in REF_PREFIX)


def unsupported_kinds(body: dict) -> dict[str, int]:
    """Kinds this EDITION cannot create, with how many the package holds.

    The cloud/self-host asymmetry surfaced honestly: a cloud package's managed
    endpoints are recognised, counted and explained, never dropped in silence.
    """
    assets = body.get("assets") or {}
    return {k: len(assets.get(k) or []) for k in UNSUPPORTED_KINDS if assets.get(k)}


def body_counts(body: dict) -> dict:
    assets = body.get("assets") or {}
    counts = {kind: len(assets.get(kind) or []) for kind in KIND_ORDER}
    for kind in UNSUPPORTED_KINDS:
        if assets.get(kind):
            counts[kind] = len(assets[kind])
    rows = 0
    for section in (body.get("data") or {}).values():
        if isinstance(section, dict):
            rows += int(section.get("row_count") or 0)
    counts["data_rows"] = rows
    return counts


def iter_assets(body: dict, kind: str) -> list[dict]:
    return [i for i in ((body.get("assets") or {}).get(kind) or []) if isinstance(i, dict)]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _dump(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), default=str).encode("utf-8")


def _timestr(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception:
        return None


def _strip_literals(config: Any) -> Any:
    if not isinstance(config, dict):
        return config
    return {
        k: v for k, v in config.items()
        if k not in STRIP_LITERAL_KEYS and str(k).lower() not in BANNED_FIELD_NAMES
    }


def _sanitize_functions(functions: Any) -> list:
    """Function NAMES and descriptions only — never the body, step range or
    selectors. Mirrors the cloud's `_sanitize_functions`."""
    if not isinstance(functions, list):
        return []
    out = []
    for fn in functions:
        if not isinstance(fn, dict):
            continue
        out.append({
            "name": fn.get("name"),
            "description": fn.get("description"),
            "parameters": fn.get("parameters") if isinstance(fn.get("parameters"), dict) else None,
        })
    return out


def _persona_secret_refs(persona) -> set[str]:
    """Vault names this persona's fields are linked to, as `{{vault:NAME}}` refs.

    The cloud reads these via `PersonaService.linked_secret_refs`; this edition has
    no such helper, so the same pattern is applied to the same columns — names only,
    never a value.
    """
    pattern = re.compile(r"\{\{vault:([a-zA-Z_][a-zA-Z0-9_.-]*)\}\}")
    found: set[str] = set()
    for column in ("login_username", "credentials_encrypted", "totp_seed_encrypted"):
        value = getattr(persona, column, None)
        if isinstance(value, str):
            for match in pattern.finditer(value):
                found.add(match.group(1).split(".")[0])
    return found


def _notify_channels(config: dict) -> list[str]:
    channels = config.get("channels") or config.get("providers") or []
    if isinstance(channels, dict):
        return sorted(channels.keys())
    if isinstance(channels, list):
        return sorted(str(c) for c in channels)
    if isinstance(channels, str):
        return [channels]
    return []


__all__ = [
    "BANNED_FIELD_NAMES",
    "KIND_ORDER",
    "MAX_DATA_ROWS_PER_ASSET",
    "PAYLOAD_VERSION",
    "REF_PREFIX",
    "UNSUPPORTED_KINDS",
    "BundleBuilder",
    "BundleError",
    "BundleNotClean",
    "BundlePlan",
    "BundleSelection",
    "MalformedBundle",
    "SkippedAsset",
    "assert_bundle_clean",
    "body_counts",
    "iter_assets",
    "parse_body",
    "unknown_kinds",
    "unsupported_kinds",
]
