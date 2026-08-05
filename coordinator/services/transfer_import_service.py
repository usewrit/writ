"""
transfer_import_service — stage → plan → commit → undo for an incoming `.writ`
(self-host edition).

Spec: `DATA_PORTABILITY_SPEC.md` §10, §12. `transfer_codec` opens the container,
`transfer_bundle` validates the body; this module decides what happens to this
install's database.

NOT a twin of the cloud edition's import service. Same algorithm, same wizard
contract, three structural differences that come from the data model:

  * **no tenant.** Single-owner coordinator, so nothing is tenant-scoped.
  * **no plan limits.** There is no `PlanEnforcer` here, so the only capability
    blocks are dependency completeness and kinds this edition cannot create.
  * **kinds cloud has and self-host does not** (`endpoints`, `ai_sessions`) are
    reported as blocked with a reason — a cloud package imports its workflows and
    monitors and TELLS the user what did not come across (spec §2.6).

THE SHAPE OF THE FLOW
---------------------
`stage()`   decrypt once, derive everything the wizard's middle steps render
            (names, collisions, capability blocks, slot requirements), park the
            body out-of-line, return a `TransferImport`.
`plan()`    validate the user's resolutions + bindings against that staged summary.
            Idempotent, called once per wizard step, never mutates an asset.
`commit()`  apply, in dependency order, recording per-asset outcomes and the ids
            created so `undo()` knows exactly what to remove.
`undo()`    delete what this import created, refusing anything since used.

RULES THAT ARE NOT NEGOTIABLE HERE
----------------------------------
* **Nothing is created before commit.** Steps 1-6 of the wizard are pure reads.
* **Per-asset failure is recorded, not fatal** (§10). A package of 40 workflows
  where one has a malformed step imports 39 and tells the user about the 40th. The
  whole commit rolls back only on infrastructure failure, because a partial import
  that is *reported accurately* is more useful than an all-or-nothing one.
* **Schedules land disarmed and automations land disabled**, whatever the package
  says. Arming is a separate, explicit act after commit.
* **Webhook tokens and secrets are minted fresh.** A package never carries them,
  so an import can never resurrect the sender's inbound URL.
* **An unbound required slot does not block the import.** The asset is created
  `is_active=False` with a needs-attention reason, which is the existing repair
  surface. Someone importing 40 workflows at 11pm should not have to produce 12 API
  keys before anything lands.
* **Plan limits are checked in `stage()`, not discovered in `commit()`** — the
  wizard shows "this would exceed your plan" in step 3, before the user commits to
  anything.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import secrets as pysecrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select

from services import transfer_bundle as B
from services import transfer_codec as C
from services import transfer_store

logger = logging.getLogger(__name__)

#: Wizard-visible collision strategies (step 3).
RESOLUTIONS = ("import", "skip", "rename", "replace")

#: Reasons an asset cannot be imported. Every one is surfaced to the user with a
#: sentence; none is ever a silent drop (§2.6).
BLOCK_REASONS = {
    "plan_limit": "Your plan does not have room for this.",
    "unknown_kind": "Made by a newer version of Writ than this one understands.",
    "unsupported_engine": "This install cannot run this kind of asset.",
    "missing_dependency": "Something it depends on is not in the package.",
    "duplicate_in_package": "The package contains two assets with this identity.",
}


class ImportError_(Exception):
    """Import-level failure with a `code` the REST edge maps to a status."""

    code = "IMPORT_ERROR"

    def __init__(self, message: str, *, code: Optional[str] = None):
        super().__init__(message)
        if code:
            self.code = code


class StaleImportPlan(ImportError_):
    code = "STALE_PLAN"


class PlanIncomplete(ImportError_):
    code = "PLAN_INCOMPLETE"


# ---------------------------------------------------------------------------
# stage
# ---------------------------------------------------------------------------

def open_package_sync(spooled: io.BufferedIOBase, passphrase: str) -> tuple[dict, bytes, Optional[bytes]]:
    """Open a package end to end. **CPU-bound** (Argon2id at 64 MiB) — always call
    this via `asyncio.to_thread`, never on the event loop."""
    reader = C.PackageReader(spooled, passphrase)
    body = reader.read_body_bytes()
    secrets_blob = reader.read_secrets_bytes()
    return reader.header, body, secrets_blob


async def stage(
    db,
    *,
    user_id,
    spooled: io.BufferedIOBase,
    passphrase: str,
):
    """Unlock a package and park it as a `TransferImport`.

    The expensive parts run off the event loop: Argon2id, the AEAD stream and gzip
    inflation all happen in a worker thread. Everything after that is bounded
    metadata work.
    """
    from models.transfer_import import STAGE_TTL_MINUTES, TransferImport

    header, body_raw, secrets_raw = await asyncio.to_thread(open_package_sync, spooled, passphrase)
    body = B.parse_body(body_raw)

    import_id = uuid.uuid4()
    summary = await build_summary(db, body)

    payload_ref, payload_inline = transfer_store.put(import_id, "body", body_raw)
    secrets_ref = None
    if secrets_raw:
        # The credentials lane is re-wrapped at rest and deleted at commit.
        s_ref, s_inline = transfer_store.put(import_id, "secrets", secrets_raw)
        secrets_ref = s_ref or f"inline:{s_inline}"

    row = TransferImport(
        id=import_id,
        created_by_user_id=user_id,
        bundle_id=_as_uuid(header.get("bundle_id")),
        label=header.get("label"),
        producer_app=(header.get("producer") or {}).get("app"),
        producer_version=(header.get("producer") or {}).get("version"),
        producer_edition=(header.get("producer") or {}).get("edition"),
        status="staged",
        header_json=header,
        counts_json=B.body_counts(body),
        summary_json=summary,
        requirements_json=body.get("requirements") or {},
        payload_ref=payload_ref,
        payload_inline=payload_inline,
        payload_bytes=len(body_raw),
        secrets_ref=secrets_ref,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=STAGE_TTL_MINUTES),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def build_summary(db, body: dict) -> dict:
    """What steps 3-6 render: every asset by name, its collision state, and any
    capability block — with a reason.

    Computed ONCE at stage time and stored, so the wizard's steps are cheap reads
    of one row instead of repeated scans of this install's assets.
    """
    items: list[dict] = []
    blocks = _capability_blocks(body)
    existing = await _existing_identities(db, body)

    for kind in B.KIND_ORDER:
        for asset in B.iter_assets(body, kind):
            ref = asset.get("ref")
            identity = _identity_of(kind, asset)
            collides = identity is not None and identity in existing.get(kind, set())
            entry = {
                "ref": ref,
                "kind": kind,
                "name": _display_name(kind, asset),
                "identity": identity,
                "collides": collides,
                # A colliding asset defaults to `rename` rather than `replace`:
                # silently overwriting an asset the user already has is the one
                # outcome they cannot undo by deleting something.
                "default_resolution": "rename" if collides else "import",
                "block": blocks.get(ref),
                "detail": _detail_of(kind, asset),
                "needs": _needs_of(asset),
                "dropped": _dropped_of(asset),
            }
            items.append(entry)

    data_sections = []
    for ref, section in (body.get("data") or {}).items():
        if isinstance(section, dict):
            data_sections.append({
                "ref": ref,
                "row_count": int(section.get("row_count") or 0),
                "run_count": int(section.get("run_count") or 0),
                "truncated": bool(section.get("truncated")),
            })

    return {
        "items": items,
        "data": data_sections,
        "marketplace_refs": body.get("marketplace_refs") or [],
        # Anything the PRODUCER already declined to include, forwarded verbatim so
        # the user sees one list of "what did not come across", not two.
        "skipped_by_producer": body.get("skipped") or [],
        "unknown_kinds": B.unknown_kinds(body),
        # Kinds this EDITION cannot create (cloud has them, self-host does not).
        # Separate from `unknown_kinds`, which means "no Writ this old knows it".
        "unsupported_kinds": B.unsupported_kinds(body),
        "counts": B.body_counts(body),
    }


async def _existing_identities(db, body: dict) -> dict[str, set]:
    """This install's current identities per kind, for collision detection.

    One narrow query per kind the package actually contains — never a full asset
    dump, and nothing at all for kinds the package does not carry.
    """
    from models.automation_workflow import AutomationWorkflow
    from models.crawl_definition import CrawlDefinition
    from models.persona import Persona
    from models.target import Target
    from models.trigger_rule import TriggerRule

    columns = {
        "workflows": (AutomationWorkflow, AutomationWorkflow.name),
        "automations": (TriggerRule, TriggerRule.name),
        "monitors": (Target, Target.url),
        "crawls": (CrawlDefinition, CrawlDefinition.slug),
        "personas": (Persona, Persona.name),
    }
    out: dict[str, set] = {}
    for kind, (model, column) in columns.items():
        if not B.iter_assets(body, kind):
            continue
        res = await db.execute(
            select(column).where(column.isnot(None))
        )
        out[kind] = {_norm(v) for v in res.scalars().all()}
    return out


def _capability_blocks(body: dict) -> dict[str, dict]:
    """Per-ref reasons an asset cannot be imported, resolved BEFORE commit.

    Two sources on this edition:

      1. **kinds this install has no table for** — a cloud package's managed
         endpoints and AI sessions. Reported with a sentence rather than dropped, so
         "it imported" never quietly means "most of it imported".
      2. **dependency completeness** — an asset whose required wiring points at
         something the package does not contain would import half-wired.

    There is deliberately no plan-limit probe: a self-host install has no plan.
    """
    blocks: dict[str, dict] = {}

    for kind, message in B.UNSUPPORTED_KINDS.items():
        for asset in B.iter_assets(body, kind):
            blocks[asset.get("ref")] = {"reason": "unsupported_engine", "message": message}

    present = {
        a.get("ref")
        for kind in list(B.KIND_ORDER) + list(B.UNSUPPORTED_KINDS)
        for a in B.iter_assets(body, kind)
    }
    for kind in B.KIND_ORDER:
        for asset in B.iter_assets(body, kind):
            for key, value in (asset.get("wiring") or {}).items():
                if isinstance(value, str) and value and value not in present:
                    blocks.setdefault(asset.get("ref"), {
                        "reason": "missing_dependency",
                        "message": f"{BLOCK_REASONS['missing_dependency']} ({key})",
                    })
    return blocks


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------

async def save_plan(db, row, plan_patch: dict) -> dict:
    """Merge a wizard step's choices into the stored plan and re-derive readiness.

    A PATCH, not a PUT: each step sends only what it owns, so a later step cannot
    wipe an earlier one's bindings by omitting them.
    """
    if row.status not in ("staged", "planned"):
        raise ImportError_(f"this import is {row.status}; it can no longer be planned", code="STALE_PLAN")

    plan = dict(row.plan_json or {})
    for section in ("resolutions", "personas", "secrets", "inputs", "files", "notify", "webhooks", "schedules", "credentials"):
        if section in plan_patch:
            merged = dict(plan.get(section) or {})
            incoming = plan_patch.get(section) or {}
            if not isinstance(incoming, dict):
                raise ImportError_(f"{section} must be an object keyed by ref or slot")
            merged.update(incoming)
            plan[section] = merged
    if "include_data" in plan_patch:
        plan["include_data"] = bool(plan_patch["include_data"])
    if "arm_schedules" in plan_patch:
        plan["arm_schedules"] = bool(plan_patch["arm_schedules"])

    _validate_resolutions(row, plan)
    row.plan_json = plan
    row.status = "planned"
    await db.commit()
    await db.refresh(row)
    return readiness(row)


def _validate_resolutions(row, plan: dict) -> None:
    summary = row.summary_json or {}
    known = {i["ref"] for i in summary.get("items") or []}
    for ref, choice in (plan.get("resolutions") or {}).items():
        if ref not in known:
            raise StaleImportPlan(f"{ref} is not in this package")
        value = choice.get("action") if isinstance(choice, dict) else choice
        if value not in RESOLUTIONS:
            raise ImportError_(f"{ref}: unknown resolution {value!r}")
        if value == "rename" and isinstance(choice, dict) and not str(choice.get("name") or "").strip():
            raise PlanIncomplete(f"{ref}: rename needs a new name")


def readiness(row) -> dict:
    """What step 7 shows before the user commits: exactly what will happen.

    "Paused" is a first-class outcome, not a failure — an asset whose required slot
    is unbound still imports, disabled, with a reason the repair surface picks up.
    """
    summary = row.summary_json or {}
    plan = row.plan_json or {}
    resolutions = plan.get("resolutions") or {}
    requirements = row.requirements_json or {}

    will_create: list[dict] = []
    will_skip: list[dict] = []
    will_replace: list[dict] = []
    blocked: list[dict] = []

    for item in summary.get("items") or []:
        ref = item["ref"]
        if item.get("block"):
            blocked.append({**item, "reason": item["block"]})
            continue
        choice = resolutions.get(ref) or {}
        action = (choice.get("action") if isinstance(choice, dict) else choice) or item.get("default_resolution")
        record = {"ref": ref, "kind": item["kind"], "name": item["name"]}
        if action == "skip":
            will_skip.append(record)
        elif action == "replace":
            will_replace.append(record)
        elif action == "rename":
            will_create.append({**record, "name": (choice or {}).get("name") or item["name"]})
        else:
            will_create.append(record)

    bound_secrets = {k for k, v in (plan.get("secrets") or {}).items() if v}
    unbound_secrets = [
        s["key"] for s in requirements.get("secret_slots") or []
        if s.get("required") and s.get("key") not in bound_secrets
    ]
    bound_personas = {k for k, v in (plan.get("personas") or {}).items() if v}
    unbound_personas = [
        s.get("slot") for s in requirements.get("persona_slots") or []
        if s.get("slot") not in bound_personas
    ]

    paused_refs = _refs_needing(requirements, unbound_secrets, unbound_personas)
    return {
        "will_create": will_create,
        "will_replace": will_replace,
        "will_skip": will_skip,
        "blocked": blocked,
        "unbound_secrets": unbound_secrets,
        "unbound_personas": unbound_personas,
        "paused_refs": sorted(paused_refs),
        "data_rows": sum(d.get("row_count") or 0 for d in summary.get("data") or []) if plan.get("include_data", True) else 0,
        "arm_schedules": bool(plan.get("arm_schedules")),
        # Never a blocker: an import with unbound slots still lands, paused.
        "ready": bool(will_create or will_replace),
    }


def _refs_needing(requirements: dict, unbound_secrets: list, unbound_personas: list) -> set[str]:
    refs: set[str] = set()
    for slot in requirements.get("secret_slots") or []:
        if slot.get("key") in set(unbound_secrets):
            refs.update(slot.get("used_by") or [])
    for slot in requirements.get("persona_slots") or []:
        if slot.get("slot") in set(unbound_personas):
            refs.update(slot.get("used_by") or [])
    return refs


# ---------------------------------------------------------------------------
# commit
# ---------------------------------------------------------------------------

async def commit(db, row, *, user_id=None, idempotency_key: Optional[str] = None) -> dict:
    """Apply the plan. Returns the per-asset result.

    Idempotent: a retried commit (double-click, proxy retry, flaky mobile network)
    returns the first commit's result instead of importing the package a second
    time — which would land as a set of renamed duplicates, the worst outcome here.
    """
    if row.status == "committed":
        if idempotency_key and row.idempotency_key and idempotency_key == row.idempotency_key:
            return row.result_json or {}
        raise ImportError_("this package has already been imported", code="ALREADY_COMMITTED")
    if row.status not in ("staged", "planned", "committing"):
        raise ImportError_(f"this import is {row.status} and cannot be committed", code="STALE_PLAN")

    body_raw = transfer_store.get(ref=row.payload_ref, inline=row.payload_inline)
    body = B.parse_body(body_raw)
    plan = row.plan_json or {}

    row.status = "committing"
    row.idempotency_key = idempotency_key or row.idempotency_key
    await db.commit()

    ctx = _CommitContext(db, user_id, body, plan, row)
    try:
        await ctx.run()
    except Exception:
        row.status = "failed"
        row.result_json = {"error": "the import failed partway; see the per-asset results",
                           "assets": ctx.results}
        row.created_ids_json = ctx.created
        await db.commit()
        raise

    row.status = "committed"
    row.committed_at = datetime.now(timezone.utc)
    row.result_json = {"assets": ctx.results, "counts": ctx.counts()}
    row.created_ids_json = ctx.created
    row.progress_json = {"done": len(ctx.results), "total": len(ctx.results), "phase": "done"}
    # The staged plaintext (and the credentials lane especially) has no reason to
    # outlive the import (§10).
    stale_payload, stale_secrets = row.payload_ref, row.secrets_ref
    row.scrub_payload()
    await db.commit()
    transfer_store.delete(stale_payload)
    if stale_secrets and not str(stale_secrets).startswith("inline:"):
        transfer_store.delete(stale_secrets)
    transfer_store.delete_import(row.id)
    await db.refresh(row)
    return row.result_json


class _CommitContext:
    """Carries the ref→new-id map while assets are created in dependency order.

    Order is `B.KIND_ORDER` — personas before the workflows that reference them,
    monitors before the automations that watch them — so every ref a projector needs
    has already been resolved by the time it is read.
    """

    def __init__(self, db, user_id, body: dict, plan: dict, row):
        self.db = db
        self.user_id = user_id
        self.body = body
        self.plan = plan
        self.row = row
        self.ids: dict[str, int] = {}            # ref -> new id
        self.created: dict[str, list[int]] = {}  # kind -> [id]  (undo reads this)
        self.results: list[dict] = []
        self.paused: set[str] = set(readiness(row).get("paused_refs") or [])

    def counts(self) -> dict:
        return {kind: len(ids) for kind, ids in self.created.items()}

    def _record(self, kind: str, ref: str, name: str, outcome: str, *, new_id=None, reason: str = "") -> None:
        if new_id is not None:
            self.created.setdefault(kind, []).append(int(new_id))
            self.ids[ref] = int(new_id)
        self.results.append({
            "ref": ref, "kind": kind, "name": name, "outcome": outcome,
            "id": new_id, "reason": reason,
        })

    def _action_for(self, ref: str) -> tuple[str, Optional[str]]:
        item = next((i for i in (self.row.summary_json or {}).get("items") or [] if i["ref"] == ref), {})
        if item.get("block"):
            return "blocked", (item["block"] or {}).get("message")
        choice = ((self.plan.get("resolutions") or {}).get(ref)) or {}
        action = (choice.get("action") if isinstance(choice, dict) else choice) or item.get("default_resolution") or "import"
        rename = choice.get("name") if isinstance(choice, dict) else None
        return action, rename

    async def run(self) -> None:
        for kind in B.KIND_ORDER:
            for asset in B.iter_assets(self.body, kind):
                ref = asset.get("ref") or ""
                name = _display_name(kind, asset)
                action, rename = self._action_for(ref)
                if action == "blocked":
                    self._record(kind, ref, name, "blocked", reason=rename or "")
                    continue
                if action == "skip":
                    self._record(kind, ref, name, "skipped")
                    continue
                try:
                    await self._create(kind, asset, ref, name, action, rename)
                except Exception as exc:  # noqa: BLE001
                    # Per-asset failure is data, not an abort (§10).
                    logger.exception("transfer import: %s %s failed", kind, ref)
                    self._record(kind, ref, name, "failed", reason=str(exc)[:300])
                    await self.db.rollback()
            await self.db.commit()
            self.row.progress_json = {"done": len(self.results), "total": None, "phase": kind}
            await self.db.commit()

        if self.plan.get("include_data", True):
            await self._import_data()
        await self._apply_sealed_credentials()

    # -- per-kind creation --------------------------------------------------

    async def _create(self, kind: str, asset: dict, ref: str, name: str, action: str, rename: Optional[str]) -> None:
        final_name = (rename or name) if action == "rename" else name
        if action == "replace":
            removed = await self._delete_existing(kind, asset)
            if removed:
                logger.info("transfer import: replaced %s %s", kind, removed)
        handler = {
            "personas": self._create_persona,
            "workflows": self._create_workflow,
            "monitors": self._create_monitor,
            "crawls": self._create_crawl,
            "automations": self._create_automation,
            "webhooks": self._create_webhook,
        }[kind]
        new_id = await handler(asset, final_name)
        outcome = "paused" if ref in self.paused else ("replaced" if action == "replace" else "created")
        reason = "waiting on a login or key you did not attach" if ref in self.paused else ""
        self._record(kind, ref, final_name, outcome, new_id=new_id, reason=reason)

    async def _create_persona(self, asset: dict, name: str) -> int:
        from models.persona import Persona

        persona = Persona(
            name=await self._unique(Persona, Persona.name, name),
            description=asset.get("description"),
            target_domain=asset.get("target_domain"),
            twofa_method=asset.get("twofa_method") or "none",
            totp_digits=asset.get("totp_digits") or 6,
            totp_period_seconds=asset.get("totp_period_seconds") or 30,
            totp_algorithm=asset.get("totp_algorithm") or "SHA1",
            email_otp_mode=asset.get("email_otp_mode"),
            otp_extract_config=asset.get("otp_extract_config"),
            fingerprint=asset.get("fingerprint"),
            # No credential VALUES: they arrive only through the sealed lane, and
            # only if the user accepted them.
            validation_status="unknown",
            is_active=True,
        )
        self.db.add(persona)
        await self.db.flush()
        return persona.id

    async def _create_workflow(self, asset: dict, name: str) -> int:
        from models.automation_workflow import AutomationWorkflow

        recipe = asset.get("recipe") or {}
        knobs = asset.get("knobs") or {}
        wiring = asset.get("wiring") or {}
        persona_id = self.ids.get(wiring.get("persona_ref") or "")
        paused = (asset.get("ref") or "") in self.paused

        wf = AutomationWorkflow(
            name=await self._unique(AutomationWorkflow, AutomationWorkflow.name, name),
            description=asset.get("description"),
            workflow_type=asset.get("kind") or "recorded",
            steps=recipe.get("steps") or [],
            raw_replay=recipe.get("raw_replay") or [],
            functions=recipe.get("functions") or [],
            entry_url=recipe.get("entry_url"),
            exit_condition=recipe.get("exit_condition"),
            timeout_ms=recipe.get("timeout_ms") or 30000,
            retry_count=recipe.get("retry_count") if recipe.get("retry_count") is not None else 2,
            streaming_config=recipe.get("streaming_config"),
            input_rules=knobs.get("input_rules") or {},
            api_functions=knobs.get("api_functions") or [],
            headless=bool(knobs.get("headless", True)),
            fast_mode=bool(knobs.get("fast_mode", True)),
            session_persistence=bool(knobs.get("session_persistence")),
            session_ttl_seconds=knobs.get("session_ttl_seconds"),
            login_url_patterns=knobs.get("login_url_patterns") or [],
            relogin_max_retries=knobs.get("relogin_max_retries") or 1,
            http_capable=bool(knobs.get("http_capable")),
            ai_repair_enabled=bool(knobs.get("ai_repair_enabled")),
            auth_config=knobs.get("auth_config") or None,
            default_persona_id=persona_id,
            # Disarmed and inactive-if-paused (§6.3). Schedule SETTINGS are kept so
            # arming later restores the user's cadence rather than a default.
            schedule_enabled=False,
            next_scheduled_at=None,
            schedule_kind=(asset.get("schedule") or {}).get("kind"),
            schedule_interval_ms=(asset.get("schedule") or {}).get("interval_ms"),
            schedule_days=(asset.get("schedule") or {}).get("days"),
            schedule_tz=(asset.get("schedule") or {}).get("tz"),
            is_active=not paused,
        )
        self.db.add(wf)
        await self.db.flush()
        return wf.id

    async def _create_monitor(self, asset: dict, name: str) -> int:
        from models.selector_extractor import SelectorExtractor
        from models.target import Target
        from models.target_selector import TargetSelector

        knobs = asset.get("knobs") or {}
        wiring = asset.get("wiring") or {}
        schedule = asset.get("schedule") or {}
        target = Target(
            url=asset.get("url"),
            check_type=asset.get("check_type") or "content",
            execution_mode=asset.get("execution_mode"),
            selector=asset.get("selector"),
            ignore_regex=asset.get("ignore_regex"),
            check_period_ms=schedule.get("check_period_ms"),
            schedule_kind=schedule.get("kind"),
            schedule_days=schedule.get("days"),
            schedule_tz=schedule.get("tz"),
            expected_status_code=knobs.get("expected_status_code") or 200,
            timeout_ms=knobs.get("timeout_ms") or 10000,
            max_response_time_ms=knobs.get("max_response_time_ms") or 5000,
            check_ssl=bool(knobs.get("check_ssl", True)),
            requires_playwright=bool(knobs.get("requires_playwright")),
            on_change_enabled=bool(knobs.get("on_change_enabled")),
            on_change_in_session=bool(knobs.get("on_change_in_session")),
            on_change_conditions=knobs.get("on_change_conditions") or {},
            setup_steps=knobs.get("setup_steps") or [],
            preferred_region=knobs.get("preferred_region"),
            notification_title=(asset.get("presentation") or {}).get("notification_title"),
            notification_message=(asset.get("presentation") or {}).get("notification_message"),
            notification_priority=(asset.get("presentation") or {}).get("notification_priority"),
            persona_id=self.ids.get(wiring.get("persona_ref") or ""),
            pre_check_workflow_id=self.ids.get(wiring.get("pre_check_workflow_ref") or ""),
            on_change_workflow_id=self.ids.get(wiring.get("on_change_workflow_ref") or ""),
            # Recipients are bound in step 6; channels alone travel, so an imported
            # monitor never notifies the sender's contacts.
            notification_providers=self._notify_for(asset.get("ref"), asset.get("notify") or []),
            # Disabled until the user arms it: a monitor with no baseline that starts
            # checking immediately reports a change that did not happen (§6.4).
            enabled=False,
        )
        self.db.add(target)
        await self.db.flush()

        for sel in asset.get("selectors") or []:
            selector = TargetSelector(
                target_id=target.id,
                name=sel.get("name"),
                selector=sel.get("selector"),
                description=sel.get("description"),
                content_type=sel.get("content_type") or "text",
                visual_region=sel.get("visual_region"),
                ignore_regex=sel.get("ignore_regex"),
                priority=sel.get("priority") or 0,
                enabled=bool(sel.get("enabled", True)),
            )
            self.db.add(selector)
            await self.db.flush()
            if sel.get("ref"):
                self.ids[sel["ref"]] = selector.id
                self.created.setdefault("selectors", []).append(selector.id)
            for ext in sel.get("extractors") or []:
                extractor = SelectorExtractor(
                    target_selector_id=selector.id,
                    name=ext.get("name"),
                    output_name=ext.get("output_name"),
                    extract_type=ext.get("extract_type") or "text",
                    config=ext.get("config") or {},
                    is_array=bool(ext.get("is_array")),
                    default_value=ext.get("default_value"),
                    enabled=bool(ext.get("enabled", True)),
                )
                self.db.add(extractor)
                await self.db.flush()
                if ext.get("ref"):
                    self.ids[ext["ref"]] = extractor.id
                    self.created.setdefault("extractors", []).append(extractor.id)
        return target.id

    async def _create_crawl(self, asset: dict, name: str) -> int:
        from models.crawl_definition import CrawlDefinition

        crawl = CrawlDefinition(
            name=name,
            slug=await self._unique_slug(CrawlDefinition, CrawlDefinition.slug, asset.get("slug") or name),
            description=asset.get("description"),
            config=asset.get("config") or {},
            seed_url=asset.get("seed_url"),
            default_max_age_seconds=asset.get("default_max_age_seconds"),
        )
        self.db.add(crawl)
        await self.db.flush()
        return crawl.id

    async def _create_automation(self, asset: dict, name: str) -> int:
        from models.trigger_rule import TriggerRule

        wiring = asset.get("wiring") or {}
        rule = TriggerRule(
            name=name,
            description=asset.get("description"),
            event_type=asset.get("event_type") or "change_detected",
            priority=asset.get("priority") or 0,
            conditions=asset.get("conditions") or {},
            actions=asset.get("actions") or [],
            blocks=self._rehydrate_blocks(asset),
            target_id=self.ids.get(wiring.get("monitor_ref") or ""),
            target_selector_id=self.ids.get(wiring.get("selector_ref") or ""),
            workflow_id=self.ids.get(wiring.get("workflow_ref") or ""),
            webhook_trigger_id=self.ids.get(wiring.get("webhook_ref") or ""),
            # Always disabled on arrival, whatever the package says: an automation
            # that fires the moment it lands can act on a monitor with no baseline.
            enabled=False,
        )
        self.db.add(rule)
        await self.db.flush()
        return rule.id

    def _rehydrate_blocks(self, asset: dict) -> list:
        """Turn ref tokens back into this install's ids, and bound notification
        slots back into this user's recipients."""
        out = []
        notify_plan = self.plan.get("notify") or {}
        for block in asset.get("blocks") or []:
            if not isinstance(block, dict):
                out.append(block)
                continue
            copy = {k: v for k, v in block.items() if k not in ("_dropped",)}
            config = dict(copy.get("config") or {})
            for key in list(config.keys()):
                if key.endswith("_ref") and isinstance(config[key], str):
                    target_key = _ID_KEY_FOR_REF.get(key)
                    resolved = self.ids.get(config.pop(key))
                    if target_key and resolved:
                        config[target_key] = resolved
                elif key.endswith("_refs") and isinstance(config[key], list):
                    target_key = _ID_KEY_FOR_REF.get(key[:-1])
                    resolved = [self.ids[r] for r in config.pop(key) if r in self.ids]
                    if target_key and resolved:
                        config[target_key + "s"] = resolved
            slot = config.pop("recipient_slot", None)
            if slot:
                bound = notify_plan.get(slot)
                if bound:
                    config["recipients"] = bound if isinstance(bound, list) else [bound]
            copy["config"] = config
            out.append(copy)
        return out

    async def _create_webhook(self, asset: dict, name: str) -> int:
        from models.webhook_trigger import WebhookTrigger
        from security.encryption import SecretEncryption

        # Fresh credentials, always: a package carries the SHAPE of a webhook, never
        # a live token, so importing one can never resurrect the sender's URL.
        hook = WebhookTrigger(
            token=pysecrets.token_urlsafe(32),
            secret=SecretEncryption.encrypt_secret(pysecrets.token_urlsafe(32)),
            name=name,
            action=asset.get("action") or "run_workflow",
            payload_mapping=asset.get("payload_mapping") or {},
            conditions=asset.get("conditions"),
            wait_for_result=bool(asset.get("wait_for_result")),
            wait_timeout=asset.get("wait_timeout") or 120,
            custom_path=await self._unique_webhook_path(asset.get("custom_path")),
            function_name=asset.get("function_name"),
            workflow_id=self.ids.get((asset.get("workflow_ref") or "")),
            target_id=self.ids.get((asset.get("target_ref") or "")),
            enabled=True,
        )
        self.db.add(hook)
        await self.db.flush()
        return hook.id

    async def _import_data(self) -> None:
        """Materialize collected data as one task per SOURCE RUN.

        Run grouping is what keeps the `latest` / `run` data lenses meaningful after
        an import — collapsing every record into one synthetic run would silently
        destroy change-tracking history. Inserts are batched; nothing accumulates.
        """
        from models.automation_task import AutomationTask

        for ref, section in (self.body.get("data") or {}).items():
            workflow_id = self.ids.get(ref)
            if not workflow_id or not isinstance(section, dict):
                continue
            runs = section.get("runs")
            if runs is None and isinstance(section.get("rows"), list):
                # A producer that emitted the flat `records` shape (spec §6.5) —
                # accepted, as one imported run.
                runs = [{"completed_at": None, "rows": section["rows"]}]
            imported_rows = 0
            batch: list[AutomationTask] = []
            for run in runs or []:
                rows = run.get("rows") if isinstance(run, dict) else None
                if not rows:
                    continue
                batch.append(AutomationTask(
                    workflow_id=workflow_id,
                    status="success",
                    success=True,
                    trigger_type="import",
                    completed_at=_parse_ts(run.get("completed_at")),
                    result_data={
                        "extracted_data": rows,
                        "_imported": {"import_id": str(self.row.id), "bundle_id": str(self.row.bundle_id or "")},
                    },
                ))
                imported_rows += len(rows)
                if len(batch) >= 500:
                    self.db.add_all(batch)
                    await self.db.commit()
                    self.created.setdefault("data_tasks", []).extend(
                        [t.id for t in batch if t.id is not None]
                    )
                    batch = []
            if batch:
                self.db.add_all(batch)
                await self.db.commit()
                self.created.setdefault("data_tasks", []).extend([t.id for t in batch if t.id is not None])
            if imported_rows:
                self.results.append({
                    "ref": ref, "kind": "data", "name": f"{imported_rows} rows",
                    "outcome": "created", "id": workflow_id, "reason": "",
                })

    # -- sealed credentials -------------------------------------------------

    async def _apply_sealed_credentials(self) -> None:
        """Write ACCEPTED sealed-lane values into the importer's OWN vault/personas.

        Per-item opt-in: `plan["credentials"]` lists exactly what the user ticked.
        Values go to the vault and to persona credential columns — never into a step
        and never into `form_data`, so they stay resolvable-by-reference the way
        every other credential in this install is.
        """
        if not self.row.secrets_ref:
            return
        accepted = self.plan.get("credentials") or {}
        if not any(accepted.values()):
            return
        try:
            raw = self._read_secrets_blob()
        except Exception:
            logger.warning("transfer import: sealed credentials could not be read", exc_info=True)
            self.results.append({"ref": "credentials", "kind": "credentials", "name": "sealed credentials",
                                 "outcome": "failed", "reason": "could not be read"})
            return
        payload = json.loads(raw)

        from models.persona import Persona
        from models.vault_secret import VaultSecret
        from security.encryption import SecretEncryption

        for entry in payload.get("vault") or []:
            key = entry.get("key")
            if not key or not accepted.get(f"vault:{key}"):
                continue
            existing = await self.db.execute(
                select(VaultSecret).where(VaultSecret.key == key)
            )
            secret = existing.scalar_one_or_none()
            encrypted = SecretEncryption.encrypt_secret(str(entry.get("value") or ""))
            if secret:
                secret.value_encrypted = encrypted
            else:
                secret = VaultSecret( key=key, value_encrypted=encrypted,
                    description=entry.get("description"), category=entry.get("category"),
                )
                self.db.add(secret)
                await self.db.flush()
                self.created.setdefault("vault_secrets", []).append(secret.id)

        for entry in payload.get("personas") or []:
            ref = entry.get("ref")
            persona_id = self.ids.get(ref or "")
            if not persona_id or not accepted.get(f"persona:{ref}"):
                continue
            persona = await self.db.get(Persona, persona_id)
            if not persona:
                continue
            if entry.get("login_username"):
                persona.login_username = entry["login_username"]
            if entry.get("password"):
                persona.credentials_encrypted = SecretEncryption.encrypt_secret(
                    json.dumps({"password": entry["password"]})
                )
            if entry.get("totp_seed"):
                persona.totp_seed_encrypted = SecretEncryption.encrypt_secret(entry["totp_seed"])
        await self.db.commit()

    def _read_secrets_blob(self) -> bytes:
        ref = self.row.secrets_ref or ""
        if ref.startswith("inline:"):
            return transfer_store.unwrap(ref[len("inline:"):].encode("ascii"))
        return transfer_store.get(ref=ref, inline=None)

    # -- helpers ------------------------------------------------------------

    def _notify_for(self, ref: Optional[str], notify: list) -> dict:
        """Channels the user bound for this monitor, as the providers map."""
        bound = (self.plan.get("notify") or {}).get(f"{ref}/change")
        if not bound:
            return {}
        if isinstance(bound, dict):
            return bound
        if isinstance(bound, list):
            return {str(channel): True for channel in bound}
        return {str(bound): True}

    async def _unique(self, model, column, name: str) -> str:
        """`name`, or `name (2)`, `name (3)`… — never a silent overwrite."""
        base = (name or "Imported").strip()[:180]
        candidate = base
        for suffix in range(2, 60):
            res = await self.db.execute(
                select(func.count()).select_from(model)
                .where(column == candidate)
            )
            if not int(res.scalar() or 0):
                return candidate
            candidate = f"{base} ({suffix})"
        return f"{base} ({pysecrets.token_hex(3)})"

    async def _unique_slug(self, model, column, base: str) -> str:
        slug = _slugify(base)
        candidate = slug
        for suffix in range(2, 60):
            res = await self.db.execute(
                select(func.count()).select_from(model)
                .where(column == candidate)
            )
            if not int(res.scalar() or 0):
                return candidate
            candidate = f"{slug}-{suffix}"
        return f"{slug}-{pysecrets.token_hex(3)}"

    async def _unique_webhook_path(self, path: Optional[str]) -> Optional[str]:
        """`custom_path` is globally unique, so a collision must be resolved rather
        than raising mid-commit."""
        if not path:
            return None
        from models.webhook_trigger import WebhookTrigger

        candidate = str(path)[:90]
        for suffix in range(2, 60):
            res = await self.db.execute(
                select(func.count()).select_from(WebhookTrigger)
                .where(WebhookTrigger.custom_path == candidate)
            )
            if not int(res.scalar() or 0):
                return candidate
            candidate = f"{str(path)[:88]}-{suffix}"
        return f"{str(path)[:80]}-{pysecrets.token_hex(3)}"

    async def _delete_existing(self, kind: str, asset: dict) -> Optional[int]:
        """`replace`: remove the colliding row so the imported one takes its place.

        Personas are deliberately EXEMPT — a persona can be wired to assets that are
        NOT part of this package, so replacing one would silently re-point work the
        user did not ask about (spec §14). A persona always creates new.
        """
        if kind == "personas":
            return None
        model, column = _IDENTITY_COLUMN[kind]
        identity = _identity_of(kind, asset)
        if identity is None:
            return None
        res = await self.db.execute(
            select(model).where(column == identity).limit(1)
        )
        row = res.scalar_one_or_none()
        if not row:
            return None
        removed = row.id
        await self.db.delete(row)
        await self.db.flush()
        return removed


# ---------------------------------------------------------------------------
# undo
# ---------------------------------------------------------------------------

async def undo(db, row) -> dict:
    """Delete exactly what this import created, newest kind first.

    Refuses — and lists — anything that has since run or collected data: undoing
    those would destroy real results, which is not what "undo an import" means. The
    refusals are returned, not raised, so the user sees a partial undo with reasons.
    """
    from models.transfer_import import UNDO_TTL_HOURS

    if row.status != "committed":
        raise ImportError_(f"this import is {row.status} and cannot be undone", code="NOT_UNDOABLE")
    if row.committed_at and datetime.now(timezone.utc) - _aware(row.committed_at) > timedelta(hours=UNDO_TTL_HOURS):
        raise ImportError_(
            f"an import can only be undone within {UNDO_TTL_HOURS} hours", code="UNDO_EXPIRED"
        )

    created = row.created_ids_json or {}
    deleted: dict[str, int] = {}
    kept: list[dict] = []

    # Reverse dependency order: the things that point at others go first.
    for kind in reversed(list(B.KIND_ORDER) + ["selectors", "extractors", "data_tasks", "vault_secrets"]):
        ids = created.get(kind) or []
        if not ids:
            continue
        model = _UNDO_MODEL.get(kind)
        if model is None:
            continue
        for asset_id in ids:
            row_obj = await db.get(model, asset_id)
            if row_obj is None:
                continue
            reason = await _undo_blocked_reason(db, kind, row_obj)
            if reason:
                kept.append({"kind": kind, "id": asset_id,
                             "name": getattr(row_obj, "name", None) or str(asset_id), "reason": reason})
                continue
            await db.delete(row_obj)
            deleted[kind] = deleted.get(kind, 0) + 1
        await db.commit()

    row.status = "undone" if not kept else "committed"
    row.undone_at = datetime.now(timezone.utc) if not kept else None
    row.result_json = {**(row.result_json or {}), "undo": {"deleted": deleted, "kept": kept}}
    await db.commit()
    return {"deleted": deleted, "kept": kept, "fully_undone": not kept}


async def _undo_blocked_reason(db, kind: str, row_obj) -> Optional[str]:
    """An imported asset that has been USED is no longer just an import."""
    if kind == "workflows":
        from models.automation_task import AutomationTask

        res = await db.execute(
            select(func.count()).select_from(AutomationTask)
            .where(AutomationTask.workflow_id == row_obj.id)
            .where(AutomationTask.trigger_type != "import")
        )
        if int(res.scalar() or 0):
            return "it has been run since the import"
    if kind == "monitors":
        from models.detected_change import DetectedChange

        res = await db.execute(
            select(func.count()).select_from(DetectedChange).where(DetectedChange.target_id == row_obj.id)
        )
        if int(res.scalar() or 0):
            return "it has detected changes since the import"
    return None


# ---------------------------------------------------------------------------
# sweep
# ---------------------------------------------------------------------------

async def sweep_expired(db, *, limit: int = 200) -> int:
    """Scrub staged plaintext past its TTL. Runs in the housekeeping scheduler.

    Keeps the row (it is history) and drops the bytes, then removes any object left
    behind — including ones no row points at, which is the crash-between-write-and-
    commit case. Without that second step a failed stage would leak decrypted work into
    the bucket indefinitely.
    """
    from models.transfer_import import TransferImport

    now = datetime.now(timezone.utc)
    res = await db.execute(
        select(TransferImport)
        .where(TransferImport.status.in_(("staged", "planned", "committing")))
        .where(TransferImport.expires_at.isnot(None))
        .where(TransferImport.expires_at < now)
        .limit(limit)
    )
    rows = list(res.scalars().all())
    for row in rows:
        payload_ref, secrets_ref = row.payload_ref, row.secrets_ref
        row.scrub_payload()
        row.status = "expired"
        transfer_store.delete(payload_ref)
        if secrets_ref and not str(secrets_ref).startswith("inline:"):
            transfer_store.delete(secrets_ref)
        transfer_store.delete_import(row.id)
    if rows:
        await db.commit()
        logger.info("transfer: swept %d expired staged import(s)", len(rows))
    return len(rows)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

_ID_KEY_FOR_REF = {
    "workflow_ref": "workflow_id",
    "target_ref": "target_id",
    "selector_ref": "target_selector_id",
    "ai_session_ref": "ai_session_id",
    "webhook_ref": "webhook_trigger_id",
    "persona_ref": "persona_id",
    "crawl_ref": "crawl_definition_id",
}


def _identity_of(kind: str, asset: dict) -> Optional[str]:
    """The field a collision is judged on — the same one this install's uniqueness is
    expressed in, so "already exists" means the same thing to us and to the user."""
    field = {
        "workflows": "name", "automations": "name", "personas": "name",
        "monitors": "url", "crawls": "slug", "webhooks": "custom_path",
    }.get(kind)
    if not field:
        return None
    value = asset.get(field)
    return _norm(value) if value else None


def _display_name(kind: str, asset: dict) -> str:
    return str(asset.get("name") or asset.get("url") or asset.get("slug") or asset.get("ref") or "Untitled")


def _detail_of(kind: str, asset: dict) -> Optional[str]:
    if kind == "monitors":
        return asset.get("url")
    if kind == "workflows":
        return (asset.get("recipe") or {}).get("entry_url")
    if kind == "crawls":
        return asset.get("seed_url")
    if kind == "personas":
        return asset.get("target_domain")
    return asset.get("description")


def _needs_of(asset: dict) -> dict:
    return asset.get("needs") or {}


def _dropped_of(asset: dict) -> list:
    """Knobs the producer could not carry — surfaced so "it imported" never quietly
    means "it imported without that setting"."""
    dropped: list[str] = []
    for block in asset.get("blocks") or []:
        if isinstance(block, dict):
            dropped.extend(block.get("_dropped") or [])
    return sorted(set(dropped))


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _slugify(value: str) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return (slug or "imported")[:100]


def _as_uuid(value: Any):
    try:
        return uuid.UUID(str(value))
    except Exception:
        return None


def _parse_ts(value: Any):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _identity_columns():
    from models.automation_workflow import AutomationWorkflow
    from models.crawl_definition import CrawlDefinition
    from models.persona import Persona
    from models.target import Target
    from models.trigger_rule import TriggerRule

    return {
        "workflows": (AutomationWorkflow, AutomationWorkflow.name),
        "automations": (TriggerRule, TriggerRule.name),
        "monitors": (Target, Target.url),
        "crawls": (CrawlDefinition, CrawlDefinition.slug),
        "personas": (Persona, Persona.name),
    }


class _LazyMap(dict):
    """Model lookups resolved on first use: importing every model at module import
    time would make this service part of the app's import cycle."""

    def __init__(self, loader):
        super().__init__()
        self._loader = loader

    def _ensure(self):
        if not self:
            self.update(self._loader())

    def __getitem__(self, key):
        self._ensure()
        return super().__getitem__(key)

    def get(self, key, default=None):
        self._ensure()
        return super().get(key, default)


_IDENTITY_COLUMN = _LazyMap(_identity_columns)


def _undo_models():
    from models.automation_task import AutomationTask
    from models.automation_workflow import AutomationWorkflow
    from models.crawl_definition import CrawlDefinition
    from models.persona import Persona
    from models.selector_extractor import SelectorExtractor
    from models.target import Target
    from models.target_selector import TargetSelector
    from models.trigger_rule import TriggerRule
    from models.vault_secret import VaultSecret
    from models.webhook_trigger import WebhookTrigger

    return {
        "workflows": AutomationWorkflow,
        "automations": TriggerRule,
        "monitors": Target,
        "selectors": TargetSelector,
        "extractors": SelectorExtractor,
        "crawls": CrawlDefinition,
        "personas": Persona,
        "webhooks": WebhookTrigger,
        "data_tasks": AutomationTask,
        "vault_secrets": VaultSecret,
    }


_UNDO_MODEL = _LazyMap(_undo_models)


__all__ = [
    "BLOCK_REASONS",
    "RESOLUTIONS",
    "ImportError_",
    "PlanIncomplete",
    "StaleImportPlan",
    "build_summary",
    "commit",
    "open_package_sync",
    "readiness",
    "save_plan",
    "stage",
    "sweep_expired",
    "undo",
]
