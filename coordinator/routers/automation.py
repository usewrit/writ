"""
Automation router - Browser automation workflow management endpoints.

Handles:
- CRUD operations for automation workflows
- Task queue management for on-change automation
- Workflow testing and execution
"""
import asyncio
import json
import logging
import os
import httpx
from utils.http_client import http_session
from utils.redis_client import get_redis
from datetime import datetime, timezone
from typing import Optional, List, Any, Dict, Literal, Union
from fastapi import APIRouter, Depends, HTTPException, Request, status, Query, BackgroundTasks
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, func, update, or_
from cryptography.fernet import Fernet

from database import get_db
from models.automation_workflow import AutomationWorkflow
from models.automation_task import AutomationTask
from models.target import Target
from models.agent import Agent, AgentStatus
from security.api_key import get_current_api_key
from security.dependencies import get_auth_context, AuthContext, check_api_key_scope, filter_by_scope, require_platform_admin
from security.feature_gate import require_feature
from security.infra_redaction import redact_infra, redact_result_data
from config import settings
from utils.recorder_auth import generate_push_jwt
from services import dataset_formats
from services.brand import CRAWL_WORKFLOW_TYPE, CRAWL_TRIGGER_TYPE

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/automation", tags=["Automation"])


def _is_crawl_dataset(workflow) -> bool:
    """True for the SYNTHETIC per-crawl workflow a crawl's shard results aggregate
    under (crawl_orchestrator._mint_crawl_workflow).

    A crawl is not a workflow. It only borrows the automation_workflows /
    automation_tasks tables as storage, and every recipe surface below must treat
    that row as if it did not exist.
    """
    return getattr(workflow, "workflow_type", None) == CRAWL_WORKFLOW_TYPE


def _reject_crawl_dataset(workflow, workflow_id: int) -> None:
    """404 a crawl's synthetic dataset row on the WORKFLOW (recipe) surfaces.

    Without this the crawl shows up in the workflow library, can be opened, run,
    edited, duplicated and deleted as though it were a recorded workflow — and
    opening one 500s, since a shard's extracted_data is a list of pages, not a
    workflow result. Its data stays reachable through the Outputs explorer and
    /crawls/{id}, which is where a crawl dataset belongs.
    """
    if _is_crawl_dataset(workflow):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow {workflow_id} not found",
        )


async def _screen_entry_url(entry_url: Optional[str]) -> None:
    """Reject an entry_url that points at a private, internal or non-http target.

    A workflow's entry_url is navigated to by a fleet agent's real browser, and
    the resulting page content flows back through extraction — so an unscreened
    value is an SSRF primitive with a read channel, even though the coordinator
    process never fetches it. Templated URLs (``{{var}}``) are passed through by
    url_policy and re-screened at dispatch when the real value is known.
    """
    if not entry_url:
        return

    from services import url_policy

    verdict = await url_policy.check_url(entry_url)
    if not verdict.allowed:
        raise HTTPException(
            status_code=400,
            detail=verdict.message or "That entry URL is not an allowed target.",
        )


# ============================================================================
# Pydantic Schemas
# ============================================================================

class WorkflowStepCreate(BaseModel):
    """A single step in a workflow."""
    id: str = Field(..., description="Unique step ID")
    type: str = Field(..., description="Step type: navigate, click, fill, ai_fill_form, wait, screenshot, extract, advanced_script")
    config: dict = Field(default_factory=dict, description="Step configuration")
    enabled: bool = Field(default=True, description="Whether step is enabled")
    selector: Optional[str] = Field(None, exclude=True)
    url: Optional[str] = Field(None, exclude=True)
    value: Optional[str] = Field(None, exclude=True)
    description: Optional[str] = Field(None, exclude=True)
    coordinates: Optional[dict] = Field(None, exclude=True)
    viewport: Optional[dict] = Field(None, exclude=True)
    options: Optional[dict] = Field(None, exclude=True)
    timestamp: Optional[float] = Field(None, exclude=True)

    def model_post_init(self, __context):
        """Merge top-level RecordedStep fields into config."""
        for key in ('selector', 'url', 'value', 'coordinates', 'viewport', 'options'):
            val = getattr(self, key, None)
            if val is not None and key not in self.config:
                self.config[key] = val


class WorkflowCreate(BaseModel):
    """Request model for creating a workflow."""
    name: str = Field(..., max_length=100, description="Workflow name")
    description: Optional[str] = Field(None, max_length=5000, description="Workflow description")
    workflow_type: str = Field(default="pre_check", description="Type: 'pre_check', 'on_change', 'recorded', or 'scheduled'")
    steps: List[WorkflowStepCreate] = Field(default_factory=list, max_length=500, description="Workflow steps (max 500)")
    raw_replay: Optional[List[dict]] = Field(default_factory=list, max_length=1000, description="Raw replay steps (max 1000)")
    form_data: Optional[dict] = Field(default_factory=dict, description="Form data for AI to fill")
    credentials: Optional[dict] = Field(None, description="Credentials (will be encrypted)")
    # Entry and exit points
    entry_url: Optional[str] = Field(None, description="URL where workflow starts")
    exit_condition: Optional[dict] = Field(None, description="Success condition: {type: 'url_contains'|'url_equals'|'element_exists', value: '...'}")
    timeout_ms: int = Field(default=30000, gt=0, description="Timeout in milliseconds")
    retry_count: int = Field(default=2, ge=0, le=10, description="Number of retries")
    headless: bool = Field(default=True, description="Run browser in headless mode")
    fast_mode: bool = Field(default=True, description="Fast execution (True) vs human-like anti-bot mode (False)")
    # Schedule settings
    schedule_enabled: bool = Field(default=False, description="Enable scheduled execution")
    schedule_interval_ms: Optional[int] = Field(None, gt=0, description="Interval between executions in ms")
    # Structured recurrence (SPEC §1a/§2): absent schedule_kind ⇒ 'interval' (back-compat).
    schedule_kind: Optional[Literal["interval", "daily", "weekly"]] = Field(None, description="Recurrence kind: 'interval' | 'daily' | 'weekly'")
    schedule_time: Optional[str] = Field(None, description="'HH:MM' local wall-clock time (daily/weekly)")
    schedule_days: Optional[List[int]] = Field(None, description="ISO weekday ints 1=Mon..7=Sun (weekly)")
    schedule_tz: Optional[str] = Field(None, description="IANA tz name (daily/weekly); absent ⇒ UTC")
    # Default sign-in identity: run-time persona (login creds + 2FA minting) unless a
    # per-run override is given. Existence is validated in the handler.
    default_persona_id: Optional[int] = Field(None, description="Persona this workflow signs in with by default")
    # Agent trust restriction
    trusted_agents_only: bool = Field(default=False, description="Only execute on trusted agents")
    # Session persistence
    session_persistence: bool = Field(default=False, description="Save browser session for reuse")
    session_ttl_seconds: Optional[int] = Field(None, gt=0, description="Max session age in seconds")
    login_url_patterns: Optional[List[str]] = Field(default_factory=list, description="URL patterns indicating login page")
    relogin_max_retries: int = Field(default=1, ge=0, le=5, description="Max re-login attempts")
    # Streaming config
    streaming_config: Optional[dict] = Field(None, description="Streaming mode: handlers, advanced_script, openai_compat")
    # Callable functions (step-groups / script / extraction) created from recorded steps
    functions: Optional[List[dict]] = Field(None, description="Callable functions: [{name, type, description, step_range, step_indices, input_variables, output_fields, ...}]")


class WorkflowUpdate(BaseModel):
    """Request model for updating a workflow."""
    name: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = None
    workflow_type: Optional[str] = None
    steps: Optional[List[WorkflowStepCreate]] = None
    raw_replay: Optional[List[dict]] = None
    form_data: Optional[dict] = None
    credentials: Optional[dict] = None
    # Entry and exit points
    entry_url: Optional[str] = None
    exit_condition: Optional[dict] = None
    timeout_ms: Optional[int] = Field(None, gt=0)
    retry_count: Optional[int] = Field(None, ge=0, le=10)
    headless: Optional[bool] = None
    fast_mode: Optional[bool] = None
    # Schedule settings
    schedule_enabled: Optional[bool] = None
    schedule_interval_ms: Optional[int] = Field(None, gt=0)
    # Structured recurrence (SPEC §1a/§2)
    schedule_kind: Optional[Literal["interval", "daily", "weekly"]] = None
    schedule_time: Optional[str] = None
    schedule_days: Optional[List[int]] = None
    schedule_tz: Optional[str] = None
    # Agent trust restriction
    trusted_agents_only: Optional[bool] = None
    # Default sign-in identity. Explicit null DETACHES (distinguished from "absent"
    # via model_fields_set in the handler); an id must reference an existing persona.
    default_persona_id: Optional[int] = None
    # Session persistence
    session_persistence: Optional[bool] = None
    session_ttl_seconds: Optional[int] = Field(None, gt=0)
    login_url_patterns: Optional[List[str]] = None
    relogin_max_retries: Optional[int] = Field(None, ge=0, le=5)
    # AI Repair
    ai_repair_enabled: Optional[bool] = None
    # Streaming config
    streaming_config: Optional[dict] = None
    # Callable functions (step-groups / script / extraction)
    functions: Optional[List[dict]] = None


class WorkflowResponse(BaseModel):
    """Response model for a workflow."""
    id: int
    name: str
    description: Optional[str]
    workflow_type: str
    steps: List[dict]
    # Lightweight count so the polled list can show "N steps" without shipping
    # the full step blobs (the list sends summary=True → steps=[], step_count set).
    step_count: int = 0
    raw_replay: Optional[List[dict]] = None  # Raw coordinate-based replay for fallback
    form_data: dict
    has_credentials: bool  # Never expose actual credentials
    # Entry and exit points
    entry_url: Optional[str]
    exit_condition: Optional[dict]
    timeout_ms: int
    retry_count: int
    headless: bool
    fast_mode: bool
    # Schedule settings
    schedule_enabled: bool
    schedule_interval_ms: Optional[int]
    # Structured recurrence (SPEC §1a/§2)
    schedule_kind: str = "interval"
    schedule_time: Optional[str] = None
    schedule_days: Optional[List[int]] = None
    schedule_tz: Optional[str] = None
    last_scheduled_at: Optional[datetime]
    next_scheduled_at: Optional[datetime]
    # Metadata
    created_at: datetime
    updated_at: Optional[datetime]
    last_run_at: Optional[datetime]
    usage_count: int
    # Failure tracking
    total_run_count: int = 0
    total_failure_count: int = 0
    consecutive_failures: int = 0
    last_failure_at: Optional[datetime] = None
    # Last run details
    last_run_duration_ms: Optional[int] = None
    last_run_status: Optional[str] = None
    last_run_task_id: Optional[int] = None
    last_run_error: Optional[str] = None
    # A run's extracted_data is EITHER a single record (dict) OR a list of them —
    # a recorded workflow that scrapes a listing page returns the list form, and so
    # does every crawl shard. Typing this `dict` made the detail endpoint 500 on any
    # such run (pydantic: "Input should be a valid dictionary"), which took the whole
    # workflow page down. Mirrors services/extracted_data_table.py, which has always
    # accepted both shapes.
    last_run_extracted_data: Optional[Union[dict, list]] = None
    # Presence flag so the list can show the "view data" affordance without
    # shipping the (potentially multi-MB) extracted dataset on every poll;
    # the actual data is lazy-fetched from GET /workflows/{id}.
    last_run_has_extracted_data: bool = False
    # Captcha handling
    captcha_blocked: bool
    last_captcha_at: Optional[datetime]
    # Agent trust restriction
    trusted_agents_only: bool = False
    # Execution stats
    estimated_duration_ms: Optional[int] = None
    # Session persistence
    session_persistence: bool = False
    session_ttl_seconds: Optional[int] = None
    login_url_patterns: Optional[List[str]] = None
    relogin_max_retries: int = 1
    has_saved_session: bool = False
    session_agent_id: Optional[str] = None
    # Streaming
    streaming_config: Optional[dict] = None
    # AI Repair
    ai_repair_enabled: bool = False
    ai_repair_history: Optional[List[dict]] = None
    last_repaired_at: Optional[datetime] = None
    repair_count: int = 0
    session_expires_at: Optional[datetime] = None
    session_status: Optional[str] = None
    # API Recorder
    api_functions: Optional[dict] = None
    # Callable functions (step-groups / script / extraction)
    functions: Optional[List[dict]] = None
    # Detected placeholders from steps (for flow builder input mapping)
    placeholders: List[dict] = []
    # FILE ASSETS (§7.3): the file inputs a RUNNER may bind their own stored file to
    # before a run — `{slot, label, is_multiple, default_file_id, default_filename,
    # declared}` for EVERY upload step, plus an installed proxy's manifest file_slots.
    # A step with a pinned file is included with that file as its DEFAULT (it still
    # runs untouched); one that only `declared` a slot has no default and must be
    # bound. Computed from steps even in summary mode (like `placeholders`), so the
    # run modal can render a file picker per slot regardless of list/detail view.
    file_slots: List[dict] = []
    # Output keys produced by the workflow (api_call variables, extractions).
    # Legacy/light shape kept for back-compat (config.variable/output_key only).
    outputs: List[dict] = []
    # Exhaustive data-LESS output contract ("what data you get") — field
    # NAMES/TYPES/DESCRIPTIONS only, unioned across every recorder/AI/desktop step
    # shape (THE INVERSION; never a value/JS body/JSONPath/selector). Display-only;
    # SEPARATE from the strict billing conformance set (_declared_output_fields).
    output_fields: List[dict] = []
    # Persona (auth identity)
    default_persona_id: Optional[int] = None
    has_login: bool = False  # True if the workflow authenticates (login/2FA detected) — gates persona UI
    has_twofa: bool = False  # True if a step enters a one-time code — runs need a persona with a 2FA method
    # Marketplace install PROXY (read-only mirror of an install). When
    # is_installed is True the recipe logic (steps/raw_replay/function code) is
    # OMITTED from the response — only the manifest + sanitized signatures are returned.
    is_installed: bool = False
    source_listing_id: Optional[int] = None
    creator_name: Optional[str] = None
    installed_status: Optional[str] = None
    data_manifest: Optional[dict] = None


def _workflow_has_login(steps, form_data, credentials_encrypted) -> bool:
    """Deterministic (no-AI) detection of whether a workflow authenticates, so the
    persona UI only appears where a login/password/email input was detected."""
    import re as _re
    import json as _json
    if credentials_encrypted:
        return True
    fd = form_data or {}
    if any(str(k).startswith("__secret_") for k in fd):
        return True
    if any(str(k).lower() in ("password", "email", "username", "login", "user") for k in fd):
        return True
    for s in (steps or []):
        if isinstance(s, dict) and s.get("type") == "twofa":
            return True
    blob = _json.dumps(steps or [])
    # {{secret:password}} / {{secret:email}} / etc. — credential placeholders
    if _re.search(r"\{\{\s*secret:\s*(password|username|email|login|user)", blob, _re.I):
        return True
    # password/email/username field hints in selectors / recognition / attributes
    if _re.search(
        r"type=['\"]?password|autocomplete=['\"]?(current-password|new-password|username|email)"
        r"|name=['\"]?(password|email|username|login)|\bpassword\b|input\[type=.{0,3}password",
        blob, _re.I,
    ):
        return True
    return False


class AuthSession(BaseModel):
    """Universal auth session extracted from workflow execution."""
    cookies: Optional[List[dict]] = Field(default_factory=list, description="Browser cookies")
    headers: Optional[dict] = Field(default_factory=dict, description="Auth headers (Bearer, X-Auth, etc.)")
    localStorage: Optional[dict] = Field(default_factory=dict, description="localStorage tokens")
    sessionStorage: Optional[dict] = Field(default_factory=dict, description="sessionStorage tokens")
    fingerprint: Optional[dict] = Field(None, description="Browser fingerprint (user_agent/locale/timezone) used when auth was captured, restored on warm runs")
    extracted_at: Optional[str] = None
    expires_at: Optional[str] = None


class TaskResult(BaseModel):
    """Result of task execution."""
    success: bool
    result_data: Optional[dict] = None
    error: Optional[str] = None
    screenshots: Optional[List[dict]] = None
    auth_session: Optional[AuthSession] = Field(None, description="Extracted auth session (for pre_check workflows)")
    agent_id: Optional[str] = Field(
        None,
        description="Reporting agent id — must match the task's dispatched executor (anti-spoof / billing-attribution binding).",
    )
    # FILE ASSETS (§4.4): captured download artifacts the agent finalized via the
    # direct-to-storage two-step (artifact-init/finalize). Each entry is
    # {file_id, filename, size, content_type, output_key?}. The BYTES never travel
    # through the backend or this payload — only the finalized handles do.
    output_files: Optional[List[dict]] = Field(
        None,
        description="Finalized captured file handles {file_id, filename, size, output_key} (bytes uploaded direct-to-storage, never through the backend).",
    )


class ArtifactInitRequest(BaseModel):
    """Agent request to begin a direct-to-storage capture of a downloaded file
    (§4.4). The backend mints a scoped, size-capped presigned upload; the agent
    uploads the bytes DIRECTLY to object storage (never through the backend)."""
    filename: str
    content_type: Optional[str] = "application/octet-stream"
    output_key: Optional[str] = Field(None, description="Optional name to bind this file under for output/chaining.")
    agent_id: Optional[str] = Field(None, description="Reporting agent id (anti-spoof, must match the task's executor).")


class ArtifactFinalizeRequest(BaseModel):
    """Agent request to finalize a captured file after the direct upload. The
    backend HEADs the object for the REAL size/type (storage is the source of
    truth — the agent's claimed size is never trusted) and validates + quotas."""
    file_id: str
    sha256: Optional[str] = None
    agent_id: Optional[str] = Field(None, description="Reporting agent id (anti-spoof, must match the task's executor).")


class TaskResponse(BaseModel):
    """Response model for an automation task."""
    id: int
    target_id: Optional[int]  # Nullable for scheduled workflows not tied to a target
    workflow_id: Optional[int]  # Nullable for AI navigation tasks
    detected_change_id: Optional[int]
    status: str
    trigger_type: str
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    executor_agent_id: Optional[str]
    success: Optional[bool]
    result_data: Optional[dict]
    error_message: Optional[str]
    screenshots: Optional[List[dict]]
    attempt_count: int
    max_attempts: int
    created_at: datetime
    duration_ms: Optional[int]
    # Slim auto-buy approval summary (amount + trigger name) for awaiting_approval
    # tasks — lets the UI show what's being held without exposing trigger_context.
    approval: Optional[dict] = None


class TargetAutomationCreate(BaseModel):
    """Request to assign automation to a target."""
    pre_check_workflow_id: Optional[int] = None
    on_change_workflow_id: Optional[int] = None
    on_change_enabled: bool = False
    on_change_conditions: Optional[dict] = None
    on_change_in_session: bool = False


# ============================================================================
# Helper Functions
# ============================================================================

def get_fernet():
    """Get Fernet instance for encryption/decryption."""
    from security.encryption import SecretEncryption
    return SecretEncryption._get_cipher()


def encrypt_credentials(credentials: dict) -> str:
    """Encrypt credentials dict to string."""
    import json
    fernet = get_fernet()
    return fernet.encrypt(json.dumps(credentials).encode()).decode()


def decrypt_credentials(encrypted: str) -> dict:
    """Decrypt credentials string to dict."""
    import json
    fernet = get_fernet()
    return json.loads(fernet.decrypt(encrypted.encode()).decode())


def _fold_vcard_into_credentials(credentials_encrypted, trigger_context):
    """Fold an issued single-use virtual card into the agent credentials.

    For payment_mode=virtual_card the coordinator issues a card and carries it as
    a master-key Fernet blob in trigger_context['vcard_enc']. Here we merge its
    fields ({{secret:vcard_number}} etc.) into the credentials the agent receives,
    just before dispatch. The PAN is only ever an encrypted blob until the agent.
    """
    tc = trigger_context or {}
    blob = tc.get("vcard_enc")
    if not blob:
        return credentials_encrypted
    try:
        import json
        from security.encryption import SecretEncryption
        base = decrypt_credentials(credentials_encrypted) if credentials_encrypted else {}
        base.update(json.loads(SecretEncryption.decrypt_secret(blob)))
        return encrypt_credentials(base)
    except Exception as e:
        logger.warning(f"[VirtualCard] failed to fold vcard into credentials: {e}")
        return credentials_encrypted


def _deep_find_placeholders(obj, seen: set, found: list, context_label: str = ""):
    """Recursively find {{key}} patterns in any string value within a dict/list."""
    import re
    if isinstance(obj, str):
        for m in re.finditer(r"\{\{([^}]+)\}\}", obj):
            key = m.group(1)
            if key not in seen:
                seen.add(key)
                found.append({"key": key, "label": context_label or key, "field_type": None})
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _deep_find_placeholders(v, seen, found, context_label)
    elif isinstance(obj, list):
        for item in obj:
            _deep_find_placeholders(item, seen, found, context_label)


def _extract_placeholders(steps: list, form_data: dict = None) -> list:
    """Extract {{key}} placeholders from workflow steps and form_data keys.

    Step structure varies:
      { type, config: { value, selector, options: { label, ... }, body: {...}, variable: "output_key" } }
    Placeholders can be nested deep (e.g., in api_call body.variables).
    """
    found = []
    seen = set()

    # 1. form_data keys are declared inputs (skip __secret_ keys)
    if form_data and isinstance(form_data, dict):
        for key in form_data:
            if not key.startswith("__") and key not in seen:
                seen.add(key)
                found.append({"key": key, "label": key.replace("_", " "), "field_type": None})

    # 2. Scan all steps deeply for {{key}} patterns
    for step in (steps or []):
        if not isinstance(step, dict):
            continue
        config = step.get("config") or {}
        opts = config.get("options") or step.get("options") or {}
        label = opts.get("label") or opts.get("field_name") or step.get("description") or ""

        # Deep scan the entire config for {{key}} patterns
        _deep_find_placeholders(config, seen, found, label)
        # Also scan step-level value (older format)
        _deep_find_placeholders(step.get("value"), seen, found, label)

        # Check options.data_key explicitly
        dk = opts.get("data_key")
        if dk and dk not in seen:
            seen.add(dk)
            found.append({
                "key": dk,
                "label": label or dk,
                "field_type": opts.get("field_type") or opts.get("field_category"),
            })
    return found


def _extract_outputs(steps: list) -> list:
    """Extract output keys from workflow steps (variables produced by api_call, evaluate, etc.)."""
    outputs = []
    seen = set()
    for step in (steps or []):
        if not isinstance(step, dict):
            continue
        config = step.get("config") or {}
        # api_call and evaluate steps store output in config.variable
        var = config.get("variable")
        if var and isinstance(var, str) and var not in seen:
            seen.add(var)
            outputs.append({
                "key": var,
                "step_type": step.get("type"),
                "description": step.get("description") or config.get("url") or var,
            })
        # extract steps may use config.output_key
        ok = config.get("output_key")
        if ok and isinstance(ok, str) and ok not in seen:
            seen.add(ok)
            outputs.append({
                "key": ok,
                "step_type": step.get("type"),
                "description": step.get("description") or ok,
            })
    return outputs


def _collect_upload_step_slots(steps: list) -> tuple[list, list]:
    """Scan replay steps for file-input UPLOAD steps and return the file ids /
    slots they reference: ``(concrete_file_ids, slot_names)``.

    An upload step carries either a concrete ``config.file_id`` (own runs / a
    private workflow) OR a ``config.file_slot`` (a recipe slot
    the BUYER binds to their own file — creators never ship a concrete id, §4.2).
    ``{{file:slot}}`` references inside any string field also surface their slot so
    the run files-map covers them. De-duped, order-preserving."""
    file_ids: list = []
    slots: list = []
    seen_ids: set = set()
    seen_slots: set = set()
    for step in (steps or []):
        if not isinstance(step, dict):
            continue
        cfg = step.get("config") or {}
        if step.get("type") == "upload":
            # A step carries its binding in `config` when the EDITOR wrote it and in
            # `options` when the RECORDER did (the file picked while recording). Both
            # shapes are canonical for a saved workflow — the UI already tolerates the
            # step.x / config.x / options.x spread — so read both here too. Reading only
            # `config` left every RECORDED upload out of the run's files map, and the
            # step then failed at replay with "no file is bound" despite the operator
            # having picked one. `config` wins: it is the explicit later edit.
            opts = step.get("options") or {}
            fid = cfg.get("file_id") or opts.get("file_id")
            if fid and isinstance(fid, str) and fid not in seen_ids:
                seen_ids.add(fid)
                file_ids.append(fid)
            slot = cfg.get("file_slot") or opts.get("file_slot")
            if slot and isinstance(slot, str) and slot not in seen_slots:
                seen_slots.add(slot)
                slots.append(slot)
    # {{file:slot}} bindings anywhere in the recipe (deep scan of the steps blob).
    try:
        import json as _json
        import re as _re
        blob = _json.dumps(steps or [])
        for m in _re.finditer(r"\{\{\s*file:\s*([A-Za-z0-9_.\-]+)\s*\}\}", blob):
            slot = m.group(1)
            if slot and slot not in seen_slots:
                seen_slots.add(slot)
                slots.append(slot)
    except Exception:
        pass
    return file_ids, slots


def _declared_file_slots(steps: list) -> list:
    """Labeled file inputs a RUNNER may bind their own file to at run time (§7.3).

    Returns one entry per upload step:
    ``[{slot, label, is_multiple, default_file_id, default_filename, declared}]``.

    A step that ``declared`` an abstract ``config.file_slot`` ships no bytes and MUST
    be bound by the runner. A step carrying a concrete ``file_id`` (pinned while
    recording or in the editor) resolves server-side, so the workflow still runs
    untouched — that file is simply the input's DEFAULT, and the runner can swap it
    for one run without editing the workflow.

    A pinned step used to be EXCLUDED here as "already bound". That is why the run
    form never offered a file: the only upload most workflows have is a pinned one.
    An unslotted step is keyed on its own stable step id, so a binding survives
    reordering or inserting steps (an ordinal would not).

    De-duped by slot name, order-preserving. Mirrors services.workflow_manifest's
    file_slots shape so the run form, the install/attach UI, and the manifest agree.
    """
    slots: list = []
    seen: set = set()
    n = 0
    for step in (steps or []):
        if not isinstance(step, dict) or step.get("type") != "upload":
            continue
        cfg = step.get("config") or {}
        opts = step.get("options") or {}
        n += 1
        slot = cfg.get("file_slot") or opts.get("file_slot")
        default_id = cfg.get("file_id") or opts.get("file_id")
        default_name = cfg.get("file_name") or opts.get("filename") or opts.get("file_name")
        declared = bool(slot and isinstance(slot, str))
        if not declared:
            sid = step.get("id")
            slot = f"step:{sid}" if sid else f"upload:{n}"
        if slot in seen:
            continue
        seen.add(slot)
        slots.append({
            "slot": slot,
            "label": (cfg.get("label") or opts.get("label") or default_name
                      or (slot.replace("_", " ") if declared else f"File {n}")),
            "is_multiple": bool(cfg.get("is_multiple") or opts.get("is_multiple")),
            # Pre-selected in the run form; sent back unchanged unless the runner
            # picks another. None for a data-less recipe slot.
            "default_file_id": default_id,
            "default_filename": default_name,
            # True = the creator declared an abstract slot the runner MUST bind.
            "declared": declared,
        })
    return slots


async def _resolve_run_files_map(
    db: "AsyncSession",
    workflow,
    *,
    request_files: dict = None,
    ttl_seconds: int = None,
) -> dict:
    """Build the run-level files map (§4.1) for a dispatch — resolved in the ASYNC
    caller BEFORE the synchronous build_execute_workflow_msg.

    Returns ``{ file_id: {file_id, url, filename, content_type, size} }`` for every
    file this run references, resolved via file_service.resolve_for_run. EVERY id is
    ownership-checked + short-TTL signed there, so a bad reference fails the run
    (fail-closed 404) rather than leaking.

    Sources:
      * concrete ``config.file_id`` on upload steps;
      * the request ``files`` map ``{slot_or_input_key: file_id}`` (§4.5) — used to
        pass a file by id into a run / API call. Slot→step binding is keyed by slot
        name; the agent matches a step's ``config.file_slot`` to the same slot.
    """
    from services import file_service as _fs

    ttl = int(ttl_seconds or settings.file_signed_url_ttl_seconds or 600)
    files_map: dict = {}

    # Concrete file ids referenced by upload steps (own runs).
    step_file_ids, _step_slots = _collect_upload_step_slots(getattr(workflow, "steps", None) or [])

    # Request-supplied files map: { slot_or_input_key: file_id }. Each value is a
    # file id the CALLER must own. Bound by slot so the agent can match an
    # upload step's file_slot to the resolved entry.
    slot_bindings: dict = {}
    req = request_files or {}
    if isinstance(req, dict):
        for slot, fid in req.items():
            if isinstance(fid, str) and fid:
                slot_bindings[str(slot)] = fid

    # Union of all referenced ids (concrete + slot-bound) — resolve each ONCE.
    all_ids = list(dict.fromkeys(list(step_file_ids) + list(slot_bindings.values())))
    for fid in all_ids:
        # resolve_for_run raises 404 if the file doesn't exist — let it propagate so
        # a bad reference fails the run rather than leaking.
        desc = await _fs.resolve_for_run(db, fid, ttl=ttl)
        files_map[fid] = desc

    # Stamp slot→file_id bindings into each resolved descriptor so the agent can
    # resolve {{file:slot}} / config.file_slot without trusting caller input again.
    if slot_bindings:
        for slot, fid in slot_bindings.items():
            if fid in files_map:
                files_map[fid].setdefault("slots", []).append(slot)

    return files_map


def _run_references_files(workflow, request_files: dict = None) -> bool:
    """True when a dispatch references ANY stored file (an upload step with a
    file_id/file_slot, a {{file:slot}} binding, or a request files map). Used by the
    sensitive-routing guard (§4.3): a file-bearing run is SENSITIVE and must never
    be routed to a foreign BYO supply agent."""
    if request_files and isinstance(request_files, dict) and any(
        isinstance(v, str) and v for v in request_files.values()
    ):
        return True
    ids, slots = _collect_upload_step_slots(getattr(workflow, "steps", None) or [])
    return bool(ids or slots)


def _sync_advanced_script_functions(workflow) -> None:
    """Reconcile declared functions from any advanced_script step into workflow.functions.

    `workflow.functions` is the SINGLE source of truth for the callable surface
    (MCP tools-list / Managed-API / output-manifest all read it), so a streaming
    advanced_script STEP that declares typed callables (name/description/
    input_variables/output_fields) must surface them there. They are stamped
    type="script" so the MCP layer routes them as direct named handler
    invocations (action=<fn name>) — exactly how invoke_handler dispatches them.

    This sync OWNS the subset of functions stamped source=="advanced_script_step":
    declared callables are merged in (de-duped by name, the advanced_script
    declaration WINS over a same-named existing function), AND any previously-synced
    entry the current step config no longer declares is PRUNED — whether a single
    function was deleted from config.functions or the whole step was removed (which
    leaves `declared` empty). This keeps the synced subset an exact mirror of the
    live step config, so a deleted function stops being advertised instead of
    lingering as a dead callable that would hit the "no handler registered" path.

    Functions added by other means — step-group / extraction callables, or script
    functions without this source tag — are left untouched. Idempotent — safe to
    call on every save.
    """
    steps = workflow.steps or []
    declared = []
    for s in steps:
        if (s or {}).get("type") == "advanced_script":
            for fn in (((s.get("config") or {}).get("functions")) or []):
                if isinstance(fn, dict) and fn.get("name"):
                    declared.append({**fn, "type": "script", "source": "advanced_script_step"})
    declared_names = {fn["name"] for fn in declared}
    # Carry over existing functions, but drop any previously-synced advanced_script
    # entry the current config no longer declares (handles per-function deletion
    # AND whole-step removal). Everything else — including script functions added by
    # other means — is preserved; the merge below lets fresh declarations override.
    by_name = {
        f.get("name"): f
        for f in (workflow.functions or [])
        if isinstance(f, dict)
        and f.get("name")
        and not (
            f.get("source") == "advanced_script_step"
            and f.get("name") not in declared_names
        )
    }
    for fn in declared:
        by_name[fn["name"]] = fn
    workflow.functions = list(by_name.values())


def workflow_to_response(
    workflow: AutomationWorkflow,
    last_task: Optional[AutomationTask] = None,
    preferred_affinity=None,
    summary: bool = False,
    secret_key_names=None,
) -> WorkflowResponse:
    """Convert workflow model to response.

    When ``summary`` is True (the polled list endpoint), heavy fields that the
    list/expand/run UI never reads are omitted to shrink the payload:
    ``raw_replay`` (up to 1000 coordinate entries) and ``ai_repair_history``.
    The detail endpoint (GET /workflows/{id}) calls with summary=False and
    returns the full object.
    """
    last_run_duration_ms = None
    last_run_status = None
    last_run_task_id = None
    last_run_extracted_data = None
    last_run_error = None
    if last_task:
        last_run_status = last_task.status
        last_run_task_id = last_task.id
        last_run_duration_ms = last_task.duration_ms
        # Run errors are user-facing by policy (the user's own workflow
        # against their own target) — expose the message, minus platform
        # infra identifiers (same redaction as TaskResponse.error_message).
        last_run_error = redact_infra(last_task.error_message)
        rd = last_task.result_data or {}
        extracted = rd.get("extracted_data")
        if extracted:
            last_run_extracted_data = extracted

    # Session persistence info
    has_saved_session = False
    session_agent_id = None
    session_expires_at = None
    session_status = None
    if preferred_affinity:
        has_saved_session = bool(preferred_affinity.session_state_encrypted)
        session_agent_id = preferred_affinity.agent_id
        session_expires_at = preferred_affinity.expires_at
        session_status = preferred_affinity.validation_status

    # Self-host: single-user, no marketplace install proxies. Every workflow is the
    # owner's own recipe — functions/streaming_config are served intact.
    _inst = False
    _safe_functions = workflow.functions or None
    _streaming_config = workflow.streaming_config
    _creator_name = None
    _data_manifest = None
    _source_listing_id = None
    _installed_status = None

    # Exhaustive data-LESS output contract ("what data you get"), derived live from
    # the recipe. Never executes scripts / never a value.
    _output_fields = []
    try:
        from services.workflow_manifest import derive_output_manifest as _dom
        _output_fields = _dom(workflow).get("output_fields") or []
    except Exception:
        _output_fields = []

    # FILE ASSETS (§7.3): declared file input slots for the run form, derived from
    # upload-step config.file_slot.
    _file_slots = []
    try:
        _file_slots = _declared_file_slots(workflow.steps or [])
    except Exception:
        _file_slots = []

    return WorkflowResponse(
        id=workflow.id,
        name=workflow.name,
        description=workflow.description,
        workflow_type=workflow.workflow_type,
        steps=[] if (summary or _inst) else (workflow.steps or []),
        step_count=len(workflow.steps or []),
        raw_replay=[] if (summary or _inst) else (workflow.raw_replay or []),
        form_data=_build_safe_form_data(workflow, secret_key_names=secret_key_names),
        has_credentials=bool(workflow.credentials_encrypted),
        entry_url=workflow.entry_url,
        exit_condition=workflow.exit_condition,
        timeout_ms=workflow.timeout_ms,
        retry_count=workflow.retry_count,
        headless=workflow.headless,
        fast_mode=workflow.fast_mode,
        schedule_enabled=workflow.schedule_enabled,
        schedule_interval_ms=workflow.schedule_interval_ms,
        schedule_kind=workflow.schedule_kind or "interval",
        schedule_time=workflow.schedule_time,
        schedule_days=workflow.schedule_days,
        schedule_tz=workflow.schedule_tz,
        last_scheduled_at=workflow.last_scheduled_at,
        next_scheduled_at=workflow.next_scheduled_at,
        created_at=workflow.created_at,
        updated_at=workflow.updated_at,
        last_run_at=workflow.last_run_at,
        usage_count=workflow.usage_count,
        total_run_count=workflow.total_run_count or 0,
        total_failure_count=workflow.total_failure_count or 0,
        consecutive_failures=workflow.consecutive_failures or 0,
        last_failure_at=workflow.last_failure_at,
        last_run_duration_ms=last_run_duration_ms,
        last_run_status=last_run_status,
        last_run_task_id=last_run_task_id,
        last_run_error=last_run_error,
        last_run_extracted_data=None if summary else last_run_extracted_data,
        last_run_has_extracted_data=bool(last_run_extracted_data),
        captcha_blocked=workflow.captcha_blocked,
        last_captcha_at=workflow.last_captcha_at,
        trusted_agents_only=workflow.trusted_agents_only,
        estimated_duration_ms=workflow.estimated_duration_ms,
        session_persistence=workflow.session_persistence,
        session_ttl_seconds=workflow.session_ttl_seconds,
        login_url_patterns=workflow.login_url_patterns,
        relogin_max_retries=workflow.relogin_max_retries,
        has_saved_session=has_saved_session,
        session_agent_id=session_agent_id,
        session_expires_at=session_expires_at,
        session_status=session_status,
        streaming_config=_streaming_config,
        api_functions=workflow.api_functions,
        functions=_safe_functions,
        placeholders=_extract_placeholders(workflow.steps, workflow.form_data),
        file_slots=_file_slots,
        outputs=_extract_outputs(workflow.steps),
        output_fields=_output_fields,
        ai_repair_enabled=getattr(workflow, 'ai_repair_enabled', False),
        ai_repair_history=None if summary else getattr(workflow, 'ai_repair_history', None),
        last_repaired_at=getattr(workflow, 'last_repaired_at', None),
        repair_count=getattr(workflow, 'repair_count', 0),
        default_persona_id=getattr(workflow, 'default_persona_id', None),
        has_login=_workflow_has_login(workflow.steps, workflow.form_data, workflow.credentials_encrypted),
        has_twofa=any(isinstance(s, dict) and s.get("type") == "twofa" for s in (workflow.steps or [])),
        is_installed=_inst,
        source_listing_id=_source_listing_id,
        creator_name=_creator_name,
        installed_status=_installed_status,
        data_manifest=_data_manifest,
    )


async def _redistribute_scheduled_workflows(db: AsyncSession):
    """
    Redistribute scheduled workflows to Playwright-capable agents.

    Called when workflows are created, updated (schedule settings), or deleted.
    This ensures agents have the latest workflow assignments without requiring
    a full target redistribution.
    """
    try:
        from services.capacity_aware_distributor import CapacityAwareDistributor
        from models.config import Config

        # Get global period for distributor
        config_result = await db.execute(
            select(Config).where(Config.key == "global_period_ms")
        )
        config = config_result.scalar_one_or_none()
        global_period_ms = int(config.value) if config else 10000

        # Get active agents for workflow distribution
        agents_result = await db.execute(
            select(Agent).where(Agent.status == AgentStatus.ACTIVE)
        )
        agents = list(agents_result.scalars().all())

        if not agents:
            logger.info("No active agents for workflow redistribution")
            return

        # Build agent_infos similar to what distributor expects
        agent_infos = []
        for agent in agents:
            capacity_per_slot = agent.capacity_per_timeslot
            agent_infos.append({
                'agent': agent,
                'capacity_per_slot': capacity_per_slot,
            })

        # Create distributor and redistribute just workflows
        distributor = CapacityAwareDistributor(db)
        stats = await distributor._distribute_scheduled_workflows(agent_infos)

        # Republish FULL snapshots for the agents whose workflow assignments just
        # changed so the new scheduled_workflows reach Redis (contract §6/§9.2-4);
        # this path bypasses the report-consumer flag-drain entirely.
        try:
            from routers.agents import publish_agent_snapshots
            await publish_agent_snapshots(db, [a.agent_id for a in agents])
        except Exception as e:  # pragma: no cover - best-effort republish
            logger.warning(f"Failed to republish snapshots after workflow redistribution: {e}")

        logger.info(f"Workflow redistribution complete: {stats}")

    except Exception as e:
        logger.error(f"Failed to redistribute scheduled workflows: {e}", exc_info=True)


def task_to_response(task: AutomationTask, summary: bool = False) -> TaskResponse:
    """Convert task model to response.

    When summary=True, the heavy `result_data` (can be multiple MB of extracted
    data) and `screenshots` are omitted — list views don't render them, so this
    keeps the list payload small.
    """
    # Defense-in-depth: scrub internal IPs / recorder & service hosts from the
    # caller-facing error string (and error/message keys inside result_data) on
    # the way OUT. This does NOT mutate the stored DB value — redact_* return
    # copies / unchanged input.
    safe_error = redact_infra(task.error_message)
    safe_result_data = None if summary else redact_result_data(task.result_data)
    # Surface a slim approval summary for held auto-buys (no secrets, no full ctx).
    approval = None
    if task.status == "awaiting_approval" and isinstance(task.trigger_context, dict):
        _pb = task.trigger_context.get("pending_buy") or {}
        approval = {
            "amount": _pb.get("amount"),
            "trigger_name": task.trigger_context.get("trigger_name"),
        }
    return TaskResponse(
        id=task.id,
        target_id=task.target_id,
        workflow_id=task.workflow_id,
        detected_change_id=task.detected_change_id,
        status=task.status,
        trigger_type=task.trigger_type,
        started_at=task.started_at,
        completed_at=task.completed_at,
        executor_agent_id=task.executor_agent_id,
        success=task.success,
        result_data=safe_result_data,
        error_message=safe_error,
        screenshots=[] if summary else (task.screenshots or []),
        attempt_count=task.attempt_count,
        max_attempts=task.max_attempts,
        created_at=task.created_at,
        duration_ms=task.duration_ms,
        approval=approval,
    )


# ============================================================================
# Workflow CRUD Endpoints
# ============================================================================

@router.get("/workflows", response_model=List[WorkflowResponse])
async def list_workflows(
    workflow_type: Optional[str] = Query(None, description="Filter by type: pre_check or on_change"),
    search: Optional[str] = Query(None, description="Search by name"),
    db: AsyncSession = Depends(get_db),
    _api_key: str = Depends(get_current_api_key),
):
    """List automation workflows for the owner."""
    check_api_key_scope(_api_key, "workflows", "read")
    from sqlalchemy.orm import selectinload, load_only

    # Eager-load install-PROXY relationships so the "by {creator}" badge +
    # data_manifest resolve in the serializer without async lazy-loads.
    #
    # load_only: the summary serializer (workflow_to_response(summary=True)) never
    # reads the heavy `raw_replay` (up to ~1000 coordinate entries → blanked to []
    # in the response), `ai_repair_history`, or `last_failure_error` columns. Skip
    # fetching them in this list query to shrink the row payload. Every other column
    # the serializer (and its helpers _build_safe_form_data / _extract_placeholders /
    # _extract_outputs / _workflow_has_login) touches is enumerated here so no lazy
    # load is triggered. (`steps`/`form_data` ARE read by the helpers even in summary
    # mode, so they stay loaded.) FK columns for the eager relationships are included.
    query = (
        select(AutomationWorkflow)
        .options(
            load_only(
                AutomationWorkflow.id,
                AutomationWorkflow.name,
                AutomationWorkflow.description,
                AutomationWorkflow.workflow_type,
                AutomationWorkflow.steps,
                AutomationWorkflow.form_data,
                AutomationWorkflow.credentials_encrypted,
                AutomationWorkflow.entry_url,
                AutomationWorkflow.exit_condition,
                AutomationWorkflow.timeout_ms,
                AutomationWorkflow.retry_count,
                AutomationWorkflow.headless,
                AutomationWorkflow.fast_mode,
                AutomationWorkflow.schedule_enabled,
                AutomationWorkflow.schedule_interval_ms,
                AutomationWorkflow.schedule_kind,
                AutomationWorkflow.schedule_time,
                AutomationWorkflow.schedule_days,
                AutomationWorkflow.schedule_tz,
                AutomationWorkflow.last_scheduled_at,
                AutomationWorkflow.next_scheduled_at,
                AutomationWorkflow.created_at,
                AutomationWorkflow.updated_at,
                AutomationWorkflow.last_run_at,
                AutomationWorkflow.usage_count,
                AutomationWorkflow.total_run_count,
                AutomationWorkflow.total_failure_count,
                AutomationWorkflow.consecutive_failures,
                AutomationWorkflow.last_failure_at,
                AutomationWorkflow.captcha_blocked,
                AutomationWorkflow.last_captcha_at,
                AutomationWorkflow.trusted_agents_only,
                AutomationWorkflow.estimated_duration_ms,
                AutomationWorkflow.session_persistence,
                AutomationWorkflow.session_ttl_seconds,
                AutomationWorkflow.login_url_patterns,
                AutomationWorkflow.relogin_max_retries,
                AutomationWorkflow.streaming_config,
                AutomationWorkflow.api_functions,
                AutomationWorkflow.functions,
                AutomationWorkflow.ai_repair_enabled,
                AutomationWorkflow.last_repaired_at,
                AutomationWorkflow.repair_count,
                AutomationWorkflow.default_persona_id,
            ),
        )
        # Every workflow is the owner's own recipe: there are no marketplace
        # install-proxy or soft-archive columns / relationships to load here.
    )

    # A crawl's synthetic dataset workflow is storage, not a recipe — never list it
    # as one (see _reject_crawl_dataset). Crawl datasets are reached from /crawls
    # and the Outputs explorer.
    query = query.where(AutomationWorkflow.workflow_type != CRAWL_WORKFLOW_TYPE)

    allowed_ids = filter_by_scope(_api_key, "workflows")
    if allowed_ids is not None and len(allowed_ids) > 0:
        query = query.where(AutomationWorkflow.id.in_(allowed_ids))
    elif allowed_ids is not None:
        return []

    if workflow_type:
        query = query.where(AutomationWorkflow.workflow_type == workflow_type)

    if search:
        from security.validation import escape_like
        query = query.where(AutomationWorkflow.name.ilike(f"%{escape_like(search)}%", escape="\\"))

    query = query.order_by(AutomationWorkflow.created_at.desc())

    result = await db.execute(query)
    workflows = result.scalars().all()

    workflow_ids = [w.id for w in workflows]
    last_tasks: dict[int, AutomationTask] = {}
    if workflow_ids:
        latest_subq = (
            select(
                AutomationTask.workflow_id,
                func.max(AutomationTask.created_at).label("max_created"),
            )
            .where(
                AutomationTask.workflow_id.in_(workflow_ids),
                # Crawl shards are not runs of this workflow — see the same guard on
                # the detail endpoint.
                AutomationTask.trigger_type != CRAWL_TRIGGER_TYPE,
            )
            .group_by(AutomationTask.workflow_id)
            .subquery()
        )
        tasks_result = await db.execute(
            select(AutomationTask)
            .join(
                latest_subq,
                (AutomationTask.workflow_id == latest_subq.c.workflow_id)
                & (AutomationTask.created_at == latest_subq.c.max_created),
            )
            .where(AutomationTask.trigger_type != CRAWL_TRIGGER_TYPE)
        )
        for task in tasks_result.scalars().all():
            last_tasks[task.workflow_id] = task

    # Pre-resolve credential KEY NAMES (values are always blanked in the response)
    # for every workflow that has them, decrypting OFF the event loop in a single
    # thread hop. Previously _build_safe_form_data ran a synchronous Fernet decrypt
    # per workflow inside the serialization loop, blocking the loop for the whole
    # list. The resulting shape is identical (same __secret_<name> keys).
    secret_names_by_id: dict[int, list] = {}
    _enc_by_id = {
        w.id: w.credentials_encrypted for w in workflows if w.credentials_encrypted
    }
    if _enc_by_id:
        def _decrypt_secret_names(enc_map):
            names = {}
            for wid, enc in enc_map.items():
                try:
                    names[wid] = list(decrypt_credentials(enc).keys())
                except Exception:
                    names[wid] = []
            return names

        secret_names_by_id = await asyncio.to_thread(_decrypt_secret_names, _enc_by_id)

    return [
        workflow_to_response(
            w,
            last_tasks.get(w.id),
            summary=True,
            secret_key_names=secret_names_by_id.get(w.id, []),
        )
        for w in workflows
    ]


def _build_safe_form_data(workflow, secret_key_names=None) -> dict:
    """
    Build form_data for API response.

    Returns plain fields as-is plus secret field names (with __secret_ prefix)
    but with empty values — so the frontend knows which secret fields exist
    without exposing the actual encrypted values.
    Also includes credential keys from credentials_encrypted.

    ``secret_key_names`` lets a caller (e.g. the list endpoint) pass the already-
    resolved credential key names so the per-row Fernet decrypt does not run on the
    event loop inside the serialization loop. When ``None`` the names are decrypted
    inline (detail path / single-workflow callers), preserving prior behavior.
    """
    result = {}

    # Plain form_data fields
    for k, v in (workflow.form_data or {}).items():
        if not k.startswith('__secret_'):
            result[k] = v

    # Secret field names from credentials_encrypted (values blanked)
    if secret_key_names is not None:
        for key in secret_key_names:
            result[f"__secret_{key}"] = ""
    elif workflow.credentials_encrypted:
        try:
            creds = decrypt_credentials(workflow.credentials_encrypted)
            for key in creds:
                result[f"__secret_{key}"] = ""
        except Exception:
            pass

    return result


def extract_secrets_from_form_data(form_data: dict) -> tuple[dict, dict]:
    """
    Extract __secret_ prefixed fields from form_data.
    Returns (regular_data, secrets).
    """
    if not form_data:
        return {}, {}

    regular_data = {}
    secrets = {}

    for key, value in form_data.items():
        if key.startswith('__secret_'):
            # Store without prefix for cleaner access
            clean_key = key.replace('__secret_', '')
            secrets[clean_key] = value
        else:
            regular_data[key] = value

    return regular_data, secrets


@router.post("/workflows", response_model=WorkflowResponse, status_code=status.HTTP_201_CREATED)
async def create_workflow(
    request: WorkflowCreate,
    db: AsyncSession = Depends(get_db),
    _api_key: str = Depends(get_current_api_key),
):
    """Create a new automation workflow."""
    check_api_key_scope(_api_key, "workflows", "write")
    # Validate workflow type
    if request.workflow_type not in ("pre_check", "on_change", "ai_navigate", "recorded", "scheduled", "api_recorded", "streaming"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="workflow_type must be 'pre_check', 'on_change', 'ai_navigate', 'recorded', 'scheduled', 'api_recorded', or 'streaming'"
        )

    # Extract secrets from form_data (fields starting with __secret_)
    regular_form_data, secrets_from_form = extract_secrets_from_form_data(request.form_data)

    # Merge with explicit credentials if provided
    all_credentials = {**(request.credentials or {}), **secrets_from_form}

    # Encrypt credentials if any
    credentials_encrypted = None
    if all_credentials:
        credentials_encrypted = encrypt_credentials(all_credentials)

    # Convert steps to dicts
    steps = [step.model_dump() for step in request.steps]

    # Structured recurrence (SPEC §2/§3): validate + normalize the schedule fields.
    from services.schedule_recurrence import (
        normalize_schedule,
        compute_next_run,
        ScheduleValidationError,
    )
    try:
        norm_kind, norm_time, norm_days, norm_tz = normalize_schedule(
            request.schedule_kind, request.schedule_time, request.schedule_days, request.schedule_tz
        )
    except ScheduleValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    # Calculate next_scheduled_at if scheduling is enabled (recurrence-aware).
    next_scheduled_at = None
    if request.schedule_enabled and (
        request.schedule_interval_ms or norm_kind in ("daily", "weekly")
    ):
        next_scheduled_at = compute_next_run(
            norm_kind,
            datetime.now(timezone.utc),
            request.schedule_interval_ms,
            norm_time,
            norm_days,
            norm_tz,
        )

    # Check if workflow contains a captcha step (recorded during user interaction)
    # If so, auto-mark as captcha_blocked to route to trusted agents
    has_captcha_step = any(
        step.get('type') == 'captcha'
        for step in steps
    )

    # SSRF screen. The coordinator never fetches entry_url itself, but it hands it
    # to a fleet agent that navigates there with a real browser and streams the
    # content back — so `file:///etc/passwd` or `http://169.254.169.254/...` is an
    # agent-side SSRF / local-file read. The three guards below it are all no-ops
    # on this build (domain_guard ships an empty blocklist, robots_guard returns
    # immediately for a single-owner coordinator, and enforce_prohibited_category
    # screens content categories, not addresses), so this is the address check.
    await _screen_entry_url(request.entry_url)

    # Domain blocklist (abuse control) + prohibited-category screen
    from services import domain_guard, target_rate_limit, robots_guard
    target_rate_limit.enforce_prohibited_category(request.entry_url)
    await domain_guard.enforce(db, request.entry_url, actor=f"apikey:{_api_key.get('id')}")
    # robots.txt posture: single-owner coordinator has no per-org opt-in,
    # so it is always a no-op. Backend-only.
    await robots_guard.enforce(request.entry_url, False)

    # Default persona must exist — a dangling id would 404 every run at dispatch.
    # Rejected loudly: attaching the persona is the request's point (frontends
    # sent this field for months while Pydantic silently ate it).
    if request.default_persona_id is not None:
        from models.persona import Persona
        if (await db.get(Persona, request.default_persona_id)) is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="default_persona_id does not reference an existing persona",
            )

    workflow = AutomationWorkflow(
        name=request.name,
        description=request.description,
        workflow_type=request.workflow_type,
        steps=steps,
        raw_replay=request.raw_replay or [],  # Raw coordinate-based replay for fallback
        form_data=regular_form_data,
        credentials_encrypted=credentials_encrypted,
        entry_url=request.entry_url,
        exit_condition=request.exit_condition,
        timeout_ms=request.timeout_ms,
        retry_count=request.retry_count,
        headless=request.headless,
        fast_mode=request.fast_mode,
        captcha_blocked=has_captcha_step,
        default_persona_id=request.default_persona_id,
        trusted_agents_only=request.trusted_agents_only,
        session_persistence=request.session_persistence,
        session_ttl_seconds=request.session_ttl_seconds,
        login_url_patterns=request.login_url_patterns or [],
        relogin_max_retries=request.relogin_max_retries,
        schedule_enabled=request.schedule_enabled,
        schedule_interval_ms=request.schedule_interval_ms,
        schedule_kind=norm_kind,
        schedule_time=norm_time,
        schedule_days=norm_days,
        schedule_tz=norm_tz,
        next_scheduled_at=next_scheduled_at,
        streaming_config=request.streaming_config,
        functions=request.functions,
    )

    # Feature #2: lift any advanced_script-step declared functions into
    # workflow.functions so MCP / Managed-API / output-manifest pick them up.
    _sync_advanced_script_functions(workflow)

    db.add(workflow)
    await db.commit()
    await db.refresh(workflow)

    # Trigger workflow redistribution if schedule is enabled
    if workflow.schedule_enabled:
        await _redistribute_scheduled_workflows(db)

    logger.info(f"Created workflow: {workflow.name} (id={workflow.id})")

    return workflow_to_response(workflow)


class ApiFunctionRequest(BaseModel):
    """A single API function definition from the recorder."""
    label: str
    is_auth: bool = False
    order: int = 0
    request: dict  # {method, url, headers, body_template}
    response_sample: Optional[dict] = None  # Recorded response {status, content_type, body}
    response_extractions: dict = Field(default_factory=dict)
    parameters: List[str] = Field(default_factory=list)
    secrets: List[str] = Field(default_factory=list)


class ApiRecordedWorkflowCreate(BaseModel):
    """Request model for creating an API-recorded workflow."""
    name: str = Field(..., max_length=100)
    description: Optional[str] = None
    functions: Dict[str, ApiFunctionRequest]
    credentials: Optional[dict] = None
    entry_url: Optional[str] = None
    create_webhooks: bool = Field(default=True, description="Auto-create webhook trigger per function")
    custom_path_prefix: Optional[str] = Field(None, description="Prefix for custom paths, e.g. 'myapp' -> /api/v1/webhooks/myapp/functionName")


class ApiRecordedWorkflowResponse(WorkflowResponse):
    """A created api_recorded workflow, plus the endpoints minted for it.

    `endpoints` is ADDITIVE (optional) so existing clients reading the plain workflow
    fields are unaffected. It exists because the per-function webhook endpoints were
    being built and then dropped on the floor: the caller asked for callable functions
    and got back no way to call them.
    """
    endpoints: Optional[List[dict]] = None


@router.post(
    "/workflows/api-recorded",
    response_model=ApiRecordedWorkflowResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create API-Recorded Workflow",
    description="Create a workflow from recorded HTTP API calls with named functions.",
)
async def create_api_recorded_workflow(
    request: ApiRecordedWorkflowCreate,
    db: AsyncSession = Depends(get_db),
    _api_key: str = Depends(get_current_api_key),
):
    """Create an api_recorded workflow with per-function webhook triggers."""
    check_api_key_scope(_api_key, "workflows", "write")
    import uuid as uuid_mod
    from models.webhook_trigger import WebhookTrigger

    # Convert functions to api_call steps
    sorted_funcs = sorted(request.functions.items(), key=lambda x: x[1].order)
    steps = []
    for func_name, func_def in sorted_funcs:
        steps.append({
            "id": str(uuid_mod.uuid4())[:8],
            "type": "api_call",
            "enabled": True,
            "config": {
                "function_name": func_name,
                "method": func_def.request.get("method", "POST"),
                "url": func_def.request.get("url"),
                "headers": func_def.request.get("headers", {}),
                "body_template": func_def.request.get("body_template", {}),
                "response_extractions": func_def.response_extractions,
                "timeout_ms": 30000,
            },
        })

    # Build form_data defaults from all function parameters
    form_data = {}
    for func_def in request.functions.values():
        for param in func_def.parameters:
            if param not in form_data:
                form_data[param] = ""

    # Build api_functions dict for storage
    api_functions = {
        name: func.model_dump() for name, func in request.functions.items()
    }

    # Encrypt credentials if provided
    credentials_encrypted = None
    if request.credentials:
        credentials_encrypted = encrypt_credentials(request.credentials)

    workflow = AutomationWorkflow(
        name=request.name,
        description=request.description,
        workflow_type="api_recorded",
        steps=steps,
        api_functions=api_functions,
        form_data=form_data,
        credentials_encrypted=credentials_encrypted,
        entry_url=request.entry_url,
        timeout_ms=30000,
        retry_count=0,
        headless=True,
        fast_mode=True,
    )
    db.add(workflow)
    await db.flush()

    # Auto-create webhook triggers per function
    created_triggers = []
    if request.create_webhooks:
        import secrets as secrets_mod
        from security.encryption import SecretEncryption

        prefix = request.custom_path_prefix or request.name.lower().replace(" ", "-")
        # Clean prefix. `/` is stripped too: the path is built as `{prefix}/{func}`
        # below, so a slash inside the prefix would produce a deeper path than the
        # stored-value pattern allows (and than the caller was told about).
        import re as re_mod
        prefix = re_mod.sub(r'[^a-z0-9_-]', '', prefix)
        if not prefix:
            # An all-punctuation name would otherwise yield paths like "/getUser",
            # which no lookup can match (stored paths never start with a slash).
            prefix = f"api-{workflow.id}"

        for func_name, func_def in request.functions.items():
            custom_path = f"{prefix}/{func_name}"
            trigger = WebhookTrigger(
                name=f"{request.name} - {func_def.label}",
                workflow_id=workflow.id,
                action="run_workflow",
                function_name=func_name,
                custom_path=custom_path,
                # Mint a signing secret like every other trigger-creating path does
                # (webhooks.create_webhook_trigger). Without one these triggers were
                # unusable over `/api/webhooks/hook/{token}` at all: _process_webhook
                # fails closed on a secret-less trigger, so the token URL 401'd while
                # the custom path had no route — the endpoints reported back to the
                # caller here could not be called by ANY means.
                secret=SecretEncryption.encrypt_secret(secrets_mod.token_urlsafe(32)),
                wait_for_result=True,
                wait_timeout=120,
                enabled=True,
            )
            db.add(trigger)
            created_triggers.append({
                "function": func_name,
                "custom_path": custom_path,
                # The callable URL, not just the path fragment: the caller has no way
                # to know what to prefix `custom_path` with, and the two webhook routes
                # authenticate differently.
                "url": f"/api/v1/webhooks/{custom_path}",
                "method": "POST",
            })

    await db.commit()
    await db.refresh(workflow)

    logger.info(
        f"Created api_recorded workflow {workflow.id} '{request.name}' "
        f"with {len(steps)} functions, {len(created_triggers)} webhook triggers"
    )

    response = ApiRecordedWorkflowResponse(
        **workflow_to_response(workflow).model_dump(),
        endpoints=created_triggers or None,
    )
    return response


@router.get("/workflows/{workflow_id}", response_model=WorkflowResponse)
async def get_workflow(
    workflow_id: int,
    db: AsyncSession = Depends(get_db),
    _api_key: str = Depends(get_current_api_key),
):
    """Get a workflow by ID."""
    check_api_key_scope(_api_key, "workflows", "read", workflow_id)

    # Every workflow is the owner's own recipe, so there are no marketplace
    # install-proxy relationships to eager-load.
    result = await db.execute(
        select(AutomationWorkflow)
        .where(
            AutomationWorkflow.id == workflow_id,
        )
    )
    workflow = result.scalar_one_or_none()

    if not workflow:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow {workflow_id} not found"
        )
    _reject_crawl_dataset(workflow, workflow_id)

    # The latest run, so `last_run_status` is populated here exactly as it is by the
    # LIST endpoint. Omitting it left the field permanently None on the detail
    # payload, and the detail page derives "is this workflow running?" from it to
    # decide whether to poll — so the page never polled, and a run in flight stayed
    # pinned at "running" until a manual reload.
    last_task_result = await db.execute(
        select(AutomationTask)
        .where(AutomationTask.workflow_id == workflow_id)
        # Never let a crawl shard pose as this workflow's last run. automation_workflows.id
        # is a bare SQLite rowid alias (no AUTOINCREMENT), so a deleted crawl's id is handed
        # to the NEXT workflow created — and any shard row that outlived its crawl would
        # otherwise resurface here as that workflow's run.
        .where(AutomationTask.trigger_type != CRAWL_TRIGGER_TYPE)
        .order_by(AutomationTask.created_at.desc(), AutomationTask.id.desc())
        .limit(1)
    )
    last_task = last_task_result.scalar_one_or_none()

    return workflow_to_response(workflow, last_task=last_task)


@router.get("/workflows/{workflow_id}/deps")
async def get_workflow_deps(
    workflow_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_platform_admin),
):
    """Lightweight dependency preview for the "Send to agent" modal.

    Returns the vault secret NAMES this workflow references ({{secret:NAME}} /
    {{vault:NAME}}, base name only — never a value) and its default persona name,
    so the frontend can show what a Mirror/Move would bundle. Admin-only; no
    secret VALUES ever leave here.
    """
    from services.secret_resolver import VAULT_REF

    result = await db.execute(
        select(AutomationWorkflow).where(AutomationWorkflow.id == workflow_id)
    )
    workflow = result.scalar_one_or_none()
    if not workflow:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow {workflow_id} not found",
        )
    _reject_crawl_dataset(workflow, workflow_id)

    steps = workflow.steps or []
    form_data = workflow.form_data or {}

    secret_keys: set = set()
    for p in _extract_placeholders(steps, form_data):
        k = p.get("key") or ""
        if k.startswith("secret:"):
            name = k[len("secret:"):]
            secret_keys.add(name.rsplit(".", 1)[0] if "." in name else name)
    blob = json.dumps({"steps": steps, "form_data": form_data})
    for name in VAULT_REF.findall(blob):
        secret_keys.add(name.rsplit(".", 1)[0] if "." in name else name)

    persona_name = None
    if workflow.default_persona_id:
        from models.persona import Persona
        prow = await db.execute(
            select(Persona.name).where(Persona.id == workflow.default_persona_id)
        )
        persona_name = prow.scalar_one_or_none()

    return {
        "workflow_id": workflow_id,
        "secret_keys": sorted(secret_keys),
        "persona": persona_name,
    }


@router.put("/workflows/{workflow_id}", response_model=WorkflowResponse)
async def update_workflow(
    workflow_id: int,
    request: WorkflowUpdate,
    db: AsyncSession = Depends(get_db),
    _api_key: str = Depends(get_current_api_key),
):
    """Update a workflow."""
    check_api_key_scope(_api_key, "workflows", "write", workflow_id)

    result = await db.execute(
        select(AutomationWorkflow).where(
            AutomationWorkflow.id == workflow_id,
        )
    )
    workflow = result.scalar_one_or_none()

    if not workflow:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow {workflow_id} not found"
        )
    _reject_crawl_dataset(workflow, workflow_id)

    # Update fields if provided
    if request.name is not None:
        workflow.name = request.name
    if request.description is not None:
        workflow.description = request.description
    if request.workflow_type is not None:
        if request.workflow_type not in ("pre_check", "on_change", "ai_navigate", "recorded", "scheduled", "api_recorded"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="workflow_type must be 'pre_check', 'on_change', 'ai_navigate', 'recorded', 'scheduled', or 'api_recorded'"
            )
        workflow.workflow_type = request.workflow_type
    if request.steps is not None:
        workflow.steps = [step.model_dump() for step in request.steps]
    if request.raw_replay is not None:
        workflow.raw_replay = request.raw_replay

    # Handle form_data with __secret_ extraction
    if request.form_data is not None:
        regular_form_data, secrets_from_form = extract_secrets_from_form_data(request.form_data)
        workflow.form_data = regular_form_data

        # Filter out empty secret values (preserve existing encrypted ones)
        secrets_from_form = {k: v for k, v in secrets_from_form.items() if v}

        # Merge with explicit credentials
        all_credentials = {**(request.credentials or {}), **secrets_from_form}
        if all_credentials:
            # Merge with existing credentials (don't lose ones not re-sent)
            existing_creds = {}
            if workflow.credentials_encrypted:
                try:
                    existing_creds = decrypt_credentials(workflow.credentials_encrypted)
                except Exception:
                    pass
            merged = {**existing_creds, **all_credentials}
            workflow.credentials_encrypted = encrypt_credentials(merged)
    elif request.credentials is not None:
        workflow.credentials_encrypted = encrypt_credentials(request.credentials)

    # Handle entry/exit points
    if request.entry_url is not None:
        # Same screen as the create path — otherwise create-then-update bypasses it.
        await _screen_entry_url(request.entry_url)

        from services import domain_guard, target_rate_limit, robots_guard
        target_rate_limit.enforce_prohibited_category(request.entry_url)
        await domain_guard.enforce(db, request.entry_url, actor=f"apikey:{_api_key.get('id')}")
        # robots.txt posture: single-owner coordinator has no per-org
        # opt-in, so it is always a no-op.
        await robots_guard.enforce(request.entry_url, False)
        workflow.entry_url = request.entry_url
    if request.exit_condition is not None:
        workflow.exit_condition = request.exit_condition

    if request.timeout_ms is not None:
        workflow.timeout_ms = request.timeout_ms
    if request.retry_count is not None:
        workflow.retry_count = request.retry_count
    if request.headless is not None:
        workflow.headless = request.headless
    if request.fast_mode is not None:
        workflow.fast_mode = request.fast_mode

    # Default persona attach/detach. `model_fields_set` distinguishes an explicit
    # null (detach) from the field being absent; an id must reference an existing persona.
    if "default_persona_id" in request.model_fields_set:
        if request.default_persona_id is not None:
            from models.persona import Persona
            if (await db.get(Persona, request.default_persona_id)) is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="default_persona_id does not reference an existing persona",
                )
        workflow.default_persona_id = request.default_persona_id

    # Handle schedule settings (structured recurrence, SPEC §2/§3).
    from services.schedule_recurrence import (
        normalize_schedule,
        compute_next_run,
        ScheduleValidationError,
    )

    if request.schedule_enabled is not None:
        workflow.schedule_enabled = request.schedule_enabled
    if request.schedule_interval_ms is not None:
        workflow.schedule_interval_ms = request.schedule_interval_ms

    # Recompute the structured recurrence from the (possibly partially-updated)
    # combination of request fields + persisted values. A field the request omits
    # falls back to what's on the workflow, so a lone schedule_time change re-uses
    # the stored kind/days/tz.
    _kind = request.schedule_kind if request.schedule_kind is not None else (workflow.schedule_kind or "interval")
    _time = request.schedule_time if request.schedule_time is not None else workflow.schedule_time
    _days = request.schedule_days if request.schedule_days is not None else workflow.schedule_days
    _tz = request.schedule_tz if request.schedule_tz is not None else workflow.schedule_tz
    try:
        norm_kind, norm_time, norm_days, norm_tz = normalize_schedule(_kind, _time, _days, _tz)
    except ScheduleValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    workflow.schedule_kind = norm_kind
    workflow.schedule_time = norm_time
    workflow.schedule_days = norm_days
    workflow.schedule_tz = norm_tz

    # Advance next_scheduled_at whenever scheduling is on and any schedule input
    # was touched; clear it when disabled.
    _schedule_input_touched = (
        request.schedule_enabled is not None
        or request.schedule_interval_ms is not None
        or request.schedule_kind is not None
        or request.schedule_time is not None
        or request.schedule_days is not None
        or request.schedule_tz is not None
    )
    if not workflow.schedule_enabled:
        workflow.next_scheduled_at = None
    elif _schedule_input_touched and (
        workflow.schedule_interval_ms or norm_kind in ("daily", "weekly")
    ):
        workflow.next_scheduled_at = compute_next_run(
            norm_kind,
            datetime.now(timezone.utc),
            workflow.schedule_interval_ms,
            norm_time,
            norm_days,
            norm_tz,
        )

    if request.trusted_agents_only is not None:
        workflow.trusted_agents_only = request.trusted_agents_only
    if request.session_persistence is not None:
        workflow.session_persistence = request.session_persistence
    if request.session_ttl_seconds is not None:
        workflow.session_ttl_seconds = request.session_ttl_seconds
    if request.login_url_patterns is not None:
        workflow.login_url_patterns = request.login_url_patterns
    if request.relogin_max_retries is not None:
        workflow.relogin_max_retries = request.relogin_max_retries
    if request.ai_repair_enabled is not None:
        workflow.ai_repair_enabled = request.ai_repair_enabled
    if request.streaming_config is not None:
        workflow.streaming_config = request.streaming_config
    if request.functions is not None:
        workflow.functions = request.functions

    # Feature #2: re-sync advanced_script-step declared functions into
    # workflow.functions AFTER steps/functions are assigned.
    _sync_advanced_script_functions(workflow)

    # Track if schedule settings changed
    schedule_changed = (
        request.schedule_enabled is not None or
        request.schedule_interval_ms is not None or
        request.schedule_kind is not None or
        request.schedule_time is not None or
        request.schedule_days is not None or
        request.schedule_tz is not None
    )

    await db.commit()
    await db.refresh(workflow)

    # Trigger workflow redistribution if schedule settings changed
    if schedule_changed:
        await _redistribute_scheduled_workflows(db)

    logger.info(f"Updated workflow: {workflow.name} (id={workflow.id})")
    return workflow_to_response(workflow)


@router.delete("/workflows/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workflow(
    workflow_id: int,
    db: AsyncSession = Depends(get_db),
    _api_key: str = Depends(get_current_api_key),
):
    """Delete a workflow."""
    check_api_key_scope(_api_key, "workflows", "delete", workflow_id)

    result = await db.execute(
        select(AutomationWorkflow).where(
            AutomationWorkflow.id == workflow_id,
        )
    )
    workflow = result.scalar_one_or_none()

    if not workflow:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow {workflow_id} not found"
        )
    # Deleting a crawl's dataset row from here would drop the crawl's pages while
    # leaving the CrawlJob pointing at nothing. Remove the crawl instead
    # (DELETE /crawl/{id}), which tears down both halves together.
    _reject_crawl_dataset(workflow, workflow_id)

    was_scheduled = workflow.schedule_enabled

    await db.delete(workflow)
    await db.commit()

    # Trigger redistribution if this was a scheduled workflow
    if was_scheduled:
        await _redistribute_scheduled_workflows(db)

    logger.info(f"Deleted workflow: {workflow.name} (id={workflow_id})")


@router.post("/workflows/{workflow_id}/duplicate", response_model=WorkflowResponse)
async def duplicate_workflow(
    workflow_id: int,
    db: AsyncSession = Depends(get_db),
    _api_key: str = Depends(get_current_api_key),
):
    """Duplicate a workflow."""
    result = await db.execute(
        select(AutomationWorkflow).where(
            AutomationWorkflow.id == workflow_id,
        )
    )
    workflow = result.scalar_one_or_none()

    if not workflow:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow {workflow_id} not found"
        )
    # Copying a crawl's dataset row would mint a second synthetic workflow with a
    # crawl_batch step and no crawl behind it — a run of it does nothing.
    _reject_crawl_dataset(workflow, workflow_id)

    # Installed PROXY = read-only. Duplicating would copy the creator's recipe
    # logic into a fully editable own workflow (IP exfiltration). Forbid.
    if getattr(workflow, "is_installed", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Installed workflows are read-only and cannot be duplicated.",
        )

    new_workflow = AutomationWorkflow(
        name=f"{workflow.name} (Copy)",
        description=workflow.description,
        workflow_type=workflow.workflow_type,
        steps=workflow.steps,
        form_data=workflow.form_data,
        credentials_encrypted=workflow.credentials_encrypted,
        entry_url=workflow.entry_url,
        exit_condition=workflow.exit_condition,
        timeout_ms=workflow.timeout_ms,
        retry_count=workflow.retry_count,
        headless=workflow.headless,
        fast_mode=workflow.fast_mode,
    )

    db.add(new_workflow)
    await db.commit()
    await db.refresh(new_workflow)

    logger.info(f"Duplicated workflow: {workflow.name} -> {new_workflow.name}")
    return workflow_to_response(new_workflow)


# ============================================================================
# Task Queue Endpoints
# ============================================================================

@router.get("/tasks", response_model=List[TaskResponse])
async def list_tasks(
    target_id: Optional[int] = Query(None, description="Filter by target"),
    workflow_id: Optional[int] = Query(None, description="Filter by workflow"),
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status"),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of tasks"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    summary: bool = Query(False, description="Omit heavy result_data/screenshots (list view)"),
    db: AsyncSession = Depends(get_db),
    _api_key: dict = Depends(get_current_api_key),
):
    """List automation tasks."""
    query = select(AutomationTask)

    if target_id:
        query = query.where(AutomationTask.target_id == target_id)
    if workflow_id:
        query = query.where(AutomationTask.workflow_id == workflow_id)
    if status_filter:
        query = query.where(AutomationTask.status == status_filter)

    query = query.order_by(AutomationTask.created_at.desc()).offset(offset).limit(limit)

    result = await db.execute(query)
    tasks = result.scalars().all()

    return [task_to_response(t, summary=summary) for t in tasks]


@router.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    _api_key: dict = Depends(get_current_api_key),
):
    """Get a single automation task by ID."""
    result = await db.execute(
        select(AutomationTask).where(AutomationTask.id == task_id)
    )
    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found"
        )

    return task_to_response(task)


async def _verify_executor_agent(
    db: AsyncSession,
    agent_id: str,
    api_key: dict,
):
    """
    Resolve the caller-supplied `agent_id` to its Agent row.

    The `agent_id` arrives as a Query parameter (agent-reported) and is stamped as
    `task.executor_agent_id`. Single-owner coordinator: every agent belongs to the
    one owner, so there is no spoof surface to guard.

    Returns the resolved Agent row (or None if no row exists — an unknown id is
    allowed to be stamped but is treated as non-trusted).
    """
    agent_result = await db.execute(
        select(Agent).where(Agent.agent_id == agent_id)
    )
    return agent_result.scalar_one_or_none()


@router.get("/tasks/pending")
async def get_pending_task(
    agent_id: str = Query(..., description="Agent ID requesting a task"),
    db: AsyncSession = Depends(get_db),
    _api_key: str = Depends(get_current_api_key),
):
    """
    Get next pending automation task for an executor agent.

    Called by desktop agents with Playwright to pick up tasks.
    """
    # Bind the supplied agent_id to the authenticated identity (anti-spoof) before
    # it is stamped as the executor / used for venue classification.
    agent = await _verify_executor_agent(db, agent_id, _api_key)
    agent_is_trusted = getattr(agent, 'is_trusted', False) if agent else False

    # Find oldest pending task that can be retried
    # Exclude AI session/navigation tasks - they use dedicated endpoints
    # If agent is not trusted, also exclude tasks from trusted_agents_only workflows
    from sqlalchemy.orm import contains_eager
    query = (
        select(AutomationTask)
        .join(AutomationWorkflow, AutomationTask.workflow_id == AutomationWorkflow.id)
        # The JOINed workflow is needed below for dispatch details — hydrate the
        # already-joined row into task.workflow (contains_eager) instead of issuing
        # a second SELECT(AutomationWorkflow) after the commit. expire_on_commit is
        # False, so the eager-loaded relationship survives the assign-commit.
        .options(contains_eager(AutomationTask.workflow))
        .where(
            AutomationTask.status == "pending",
            AutomationTask.attempt_count < AutomationTask.max_attempts,
            AutomationTask.trigger_type.notin_(
                ["ai_session", "ai_navigate", "scheduled_ai_session"]
            ),
        )
    )
    if not agent_is_trusted:
        query = query.where(
            (AutomationWorkflow.trusted_agents_only == False) | (AutomationWorkflow.trusted_agents_only == None)
        )
    query = query.order_by(AutomationTask.created_at).limit(1)

    result = await db.execute(query)
    task = result.scalar_one_or_none()

    if not task:
        return None

    # Mark as assigned
    task.status = "assigned"
    task.executor_agent_id = agent_id
    task.attempt_count += 1
    await db.commit()

    # Get workflow details (hydrated via contains_eager on the join above).
    workflow = task.workflow

    if not workflow:
        task.status = "failed"
        task.error_message = "Workflow not found"
        await db.commit()
        return None

    logger.info(f"Assigned task {task.id} to agent {agent_id}")

    # Merge dynamic form_data from webhook trigger_context over static workflow form_data
    trigger_context = task.trigger_context or {}
    dynamic_form_data = trigger_context.get("form_data", {})
    merged_form_data = {**(workflow.form_data or {}), **dynamic_form_data}

    # Effective credentials shipped to the polled agent. For an OWN run this is the
    # workflow's own (creator==owner) credentials; a CONSUMER run REPLACES it below
    # with the buyer's folded credentials via the inversion. vcard folding happens
    # at return time so all paths are identical.
    _effective_credentials_encrypted = workflow.credentials_encrypted
    # Buyer-resolved persona/session for parity with the WS/sync dispatch (the
    # current desktop executor folds persona creds into credentials, but pass these
    # so future executor versions can honor 2FA config + a warm session).
    _poll_persona_cfg = None
    _poll_session_state = None

    # Self-host: single-user, no marketplace/consumer runs.
    _consumer_recipe = False

    _task_steps = _strip_recipe_metadata(workflow.steps) if _consumer_recipe else workflow.steps
    _task_raw_replay = _strip_recipe_metadata(workflow.raw_replay) if _consumer_recipe else workflow.raw_replay

    # FILE ASSETS (§4.1): resolve the run-level files map for the polled desktop
    # agent. Short-TTL, ownership-checked descriptors, same as the WS path.
    _poll_req_files = (trigger_context or {}).get("files") if isinstance(trigger_context, dict) else None
    try:
        _poll_files_map = await _resolve_run_files_map(
            db, workflow,
            request_files=_poll_req_files,
            ttl_seconds=max(
                int(settings.file_signed_url_ttl_seconds or 600),
                int((getattr(workflow, "timeout_ms", None) or 0) // 1000) + 60,
            ),
        )
    except HTTPException:
        # A bad file reference must fail the run, not silently drop the file.
        task.status = "failed"
        task.error_message = "File resolution failed: a referenced file is missing or not owned."
        await db.commit()
        return None

    return {
        "id": task.id,
        "target_id": task.target_id,
        "workflow": {
            "id": workflow.id,
            "name": workflow.name,
            "workflow_type": workflow.workflow_type,
            "steps": _task_steps,
            "raw_replay": _task_raw_replay,  # Coordinate-based fallback steps
            "form_data": merged_form_data,
            "entry_url": workflow.entry_url,
            "timeout_ms": workflow.timeout_ms,
            "headless": workflow.headless,
            "fast_mode": workflow.fast_mode,
            # FILE ASSETS (§4.1): run-level files map { file_id -> {url, filename,
            # content_type, size, slots?} } for the agent's upload steps.
            "files": _poll_files_map or {},
            # CONSUMER run parity with the WS/sync dispatch: the buyer's resolved
            # persona config + warm session. None on an own run. The current desktop
            # executor folds persona credentials into credentials_encrypted (below),
            # but ship these so a 2FA-capable / session-restoring executor can honor
            # the buyer's persona without re-reading any creator data.
            "persona": _poll_persona_cfg,
            "session_state": _poll_session_state,
            # Auto-buy: the polling (desktop) agent reads these off the workflow dict.
            # dry_run → stop before the commit step; payment_mode → use a saved method
            # / autofill and never type a stored card number.
            "dry_run": bool(trigger_context.get("dry_run", False)),
            "payment_mode": trigger_context.get("payment_mode"),
        },
        # OWN run → workflow's own credentials; CONSUMER run → the buyer's folded
        # credentials from the inversion (never the creator's — those were nulled).
        # vcard folding is identical on both paths.
        "credentials_encrypted": _fold_vcard_into_credentials(
            _effective_credentials_encrypted, trigger_context),
        "trigger_type": task.trigger_type,
        "trigger_context": trigger_context,
        "is_validation": task.trigger_type == "validation",  # Flag for validation tasks
    }


@router.post("/tasks/{task_id}/start")
async def start_task(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    _api_key: dict = Depends(get_current_api_key),
):
    """Mark task as running."""
    query = select(AutomationTask).where(AutomationTask.id == task_id)

    result = await db.execute(query)
    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found"
        )

    task.status = "running"
    task.started_at = datetime.utcnow()
    await db.commit()

    # Realtime: surface this just-started run in Live activity / the runs feed
    # instantly (best-effort — never block the dispatch path).
    try:
        from services.run_events import emit_run_event
        await emit_run_event(
            run_type="workflow", row_id=task.id,
            status="running", event="started",
        )
    except Exception:
        pass

    # Fire workflow_started trigger
    if task.workflow_id:
        try:
            from services.unified_trigger_service import get_unified_trigger_service
            trigger_service = get_unified_trigger_service(db)
            wf_result = await db.execute(
                select(AutomationWorkflow).where(AutomationWorkflow.id == task.workflow_id)
            )
            wf = wf_result.scalar_one_or_none()
            if wf:
                await trigger_service.process_workflow_event(
                    event_type="workflow_started",
                    workflow_id=wf.id,
                    workflow_name=wf.name,
                    task_id=task.id,
                    target_id=task.target_id,
                    status="running",
                )
        except Exception as e:
            logger.error(f"Failed to dispatch workflow_started triggers: {e}")

    return {"status": "running"}


# ============================================================================
# BYO-trust boundary — gateway-attested, NOT agent-asserted
# ----------------------------------------------------------------------------
# The ws-gateway + backend are authoritative; the BYO executor agent is
# UNTRUSTED. Anything that moves money or feeds ranking (success, duration,
# latency) must be derived from backend-observed signals, never from fields the
# agent put in its result payload. The agent's `success` flag and any agent-
# supplied timing are advisory transport claims only.
# ============================================================================

def _result_data_is_nonempty(result_data: Optional[dict]) -> bool:
    """A result is non-empty if it carries any extracted payload. We accept
    either a populated top-level `extracted_data` dict/list, populated `steps`,
    or any other non-bookkeeping key with a truthy value."""
    if not result_data or not isinstance(result_data, dict):
        return False
    ed = result_data.get("extracted_data")
    if isinstance(ed, dict) and len(ed) > 0:
        return True
    if isinstance(ed, (list, str)) and len(ed) > 0:
        return True
    steps = result_data.get("steps")
    if isinstance(steps, list) and len(steps) > 0:
        return True
    # Any other meaningful (non-bookkeeping) key with a truthy value counts.
    _bookkeeping = {
        "dry_run", "stopped_before_commit", "captcha_detected",
        "needs_reassignment", "ai_session_id", "workflow_id", "extracted_data",
        "steps",
    }
    for k, v in result_data.items():
        if k in _bookkeeping:
            continue
        if v:
            return True
    return False


def _declared_output_fields(workflow) -> list:
    """The workflow's declared output = the union of `output_fields` across its
    callable `functions` (automation_workflow.py functions JSON). There is no
    separate output_schema column. Returns a flat list of field-name strings."""
    fns = getattr(workflow, "functions", None) or []
    fields: list = []
    if not isinstance(fns, list):
        return fields
    for fn in fns:
        if not isinstance(fn, dict):
            continue
        ofs = fn.get("output_fields") or []
        if isinstance(ofs, list):
            for f in ofs:
                if isinstance(f, str):
                    fields.append(f)
                elif isinstance(f, dict) and f.get("name"):
                    fields.append(str(f["name"]))
    return fields


def _result_conforms_to_output_fields(result_data: Optional[dict], workflow) -> bool:
    """STRICT conformance: when the workflow declares output_fields, EVERY declared
    field must be present AND carry a non-empty value (under extracted_data or at
    top level). The previous `any(...)` rule let a run that delivered a single
    declared key — while every other declared field was missing/empty — count as a
    billable success; a seller could forge a near-empty "success" and still be
    paid. When nothing is declared, conformance reduces to non-emptiness (checked
    separately by the caller)."""
    declared = _declared_output_fields(workflow)
    if not declared:
        # No declared schema — non-emptiness (checked separately) is the bar.
        return True
    if not result_data or not isinstance(result_data, dict):
        return False

    def _nonempty(v) -> bool:
        # A field "satisfies" the schema only with a meaningful value. None / empty
        # string / empty container do NOT count. 0 and False ARE meaningful values.
        if v is None:
            return False
        if isinstance(v, str):
            return v.strip() != ""
        if isinstance(v, (list, dict, set, tuple)):
            return len(v) > 0
        return True

    def _record_conforms(rec: dict) -> bool:
        # Every declared field must be present in this record with a non-empty value.
        # Also consider top-level result_data keys (a workflow may surface a declared
        # field at the top level rather than inside extracted_data).
        return all(
            (f in rec and _nonempty(rec.get(f)))
            or (f in result_data and _nonempty(result_data.get(f)))
            for f in declared
        )

    ed = result_data.get("extracted_data")
    if isinstance(ed, list):
        # Canonical scraper output is a LIST of records (list/detail/pagination
        # workflows): the declared fields live inside each record. Require a
        # non-empty list AND that EVERY record carries ALL declared fields, so a
        # batch padded with empty/partial records can't pass as a full delivery.
        records = [item for item in ed if isinstance(item, dict)]
        if not records:
            return False
        return all(_record_conforms(rec) for rec in records)

    # Single-record output: declared fields live under extracted_data and/or at the
    # top level of result_data. Require ALL of them present and non-empty.
    base = ed if isinstance(ed, dict) else {}
    return _record_conforms(base)


def _compute_delivered_ok(
    workflow,
    *,
    error: Optional[str],
    result_data: Optional[dict],
    agent_reported_success: bool,
) -> bool:
    """BACKEND-DETERMINED success (the authoritative billable/ranking signal).

    delivered_ok is TRUE iff, from backend-observed signals only:
      - no error was reported, AND
      - the result_data is non-empty, AND
      - the result_data conforms to the workflow's declared output_fields.

    The agent's `success` flag is ADVISORY: a backend-observed failure (error
    set, or empty/non-conforming result) overrides an agent that claims success.
    We never *promote* a run to success the agent didn't claim — the agent flag
    is a necessary-but-not-sufficient precondition — but the authoritative
    truth is the observed delivery, not the agent's word.
    """
    if error:
        return False
    if not agent_reported_success:
        # The transport claim is a precondition; without it we don't bill.
        return False
    if not _result_data_is_nonempty(result_data):
        return False
    if not _result_conforms_to_output_fields(result_data, workflow):
        return False
    return True


async def _reconcile_task_output_files(db: "AsyncSession", task, reported) -> list:
    """Reconcile the agent-reported ``result_data.output_files`` against the
    StoredFile rows ACTUALLY finalized for this task (source_run_id == task.id,
    status == ready). The DB is authoritative — a captured file only counts if it
    went through artifact-init/finalize (which validated + quota'd + counted it), so
    a hostile agent can't fabricate output handles or reference another task's
    files. Stamps the authoritative list into task.result_data and exposes
    each named output_key as a {{output_key}} variable in the result for chaining
    (§4.5). Returns the authoritative list."""
    from models.stored_file import StoredFile as _SF

    rows = (await db.execute(
        select(_SF).where(
            _SF.source_run_id == task.id,
            _SF.status == "ready",
            _SF.deleted_at.is_(None),
        ).order_by(_SF.created_at.asc())
    )).scalars().all()
    if not rows:
        return []

    # Map reported output_key by file_id (advisory only — the value the agent named
    # it; the file id itself is the source of truth and already DB-bound to the run).
    reported_keys = {}
    if isinstance(reported, list):
        for it in reported:
            if isinstance(it, dict) and it.get("file_id"):
                ok = it.get("output_key")
                if isinstance(ok, str) and ok:
                    reported_keys[str(it["file_id"])] = ok

    out = []
    out_vars = {}
    for r in rows:
        ok = (r.meta or {}).get("output_key") or reported_keys.get(r.id)
        entry = {
            "file_id": r.id,
            "filename": r.filename,
            "content_type": r.content_type,
            "size": int(r.size_bytes or 0),
        }
        if ok:
            entry["output_key"] = ok
            # Expose the file id under its named output key so input_mapping in a
            # chained workflow can resolve {{result.<output_key>}} → file_id.
            out_vars[ok] = r.id
        out.append(entry)

    # Persist onto the task result (authoritative list + named output variables).
    rd = dict(task.result_data or {})
    rd["output_files"] = out
    if out_vars:
        _ov = dict(rd.get("output_variables") or {})
        _ov.update(out_vars)
        rd["output_variables"] = _ov
    task.result_data = rd
    try:
        from sqlalchemy.orm.attributes import flag_modified as _fm
        _fm(task, "result_data")
    except Exception:
        pass
    return out


def _aware_utc(dt):
    """Coerce a datetime to tz-aware UTC.

    SQLite (self-host) returns NAIVE datetimes for ``DateTime(timezone=True)``
    columns, while Postgres returns tz-aware ones. Subtracting a naive
    ``started_at`` from a tz-aware ``datetime.now(timezone.utc)`` raises
    ``TypeError: can't subtract offset-naive and offset-aware datetimes`` — which,
    inside the fire-and-forget completion task, silently aborts finalization and
    leaves every run stuck in ``running``. Stored naive values ARE UTC, so tag
    them as such. No-op on Postgres (already aware) and on ``None``.
    """
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def _process_task_completion(
    db: AsyncSession,
    task: AutomationTask,
    success: bool,
    result_data: dict = None,
    error: str = None,
    screenshots: list = None,
    auth_session: dict = None,
):
    """
    Core task completion logic — used by both the HTTP endpoint and WS handler.

    Updates task status, workflow stats, duration estimate, session persistence,
    validation status, and dispatches workflow_completed triggers.

    BYO-TRUST: the `success` argument is the AGENT-REPORTED (advisory) success
    flag — the executor agent is untrusted, so we do NOT take it as truth for
    billing/earnings/metrics. The authoritative `task.success` is computed
    backend-side as `delivered_ok` (no error + non-empty result conforming to
    the workflow's declared output_fields). The agent flag is necessary (we
    don't promote a run the agent never claimed) but not sufficient.
    """
    # Use timezone-aware datetime to match what PostgreSQL returns for started_at
    # (DateTime(timezone=True) columns return aware datetimes from SQLAlchemy)
    now = datetime.now(timezone.utc)

    # Free the coordinator-reserved capacity slot for this run (idempotent; no-op for
    # HTTP-pool runs or if the executor already disconnected). Reserved at pick time
    # in _pick_and_reserve / the queue drainer; this is the run's terminal event.
    try:
        from routers.user_recorder_ws import release_agent_slot
        release_agent_slot(task.id)
    except Exception:
        pass

    agent_reported_success = bool(success)

    if result_data is not None:
        task.result_data = result_data
    if error:
        task.error_message = str(error)[:2000]
    if screenshots:
        task.screenshots = screenshots

    # FILE ASSETS (§4.4/§4.5): persist captured output files. The agent already
    # uploaded the BYTES direct-to-storage and finalized each via artifact-finalize
    # (which set status=ready + bumped quota); here we only RECONCILE the reported
    # handles to the StoredFile rows actually finalized for THIS task — never trust
    # the agent-reported list as-is. Each kept entry exposes {file_id, filename,
    # size, content_type, output_key} in the task result so the API response (§4.5)
    # and automation chaining can reference it. An output_key surfaces the file as a
    # named output variable for workflow→workflow chaining.
    try:
        await _reconcile_task_output_files(db, task, (result_data or {}).get("output_files"))
    except Exception as _of_e:
        logger.warning("[FileAssets] output_files reconcile failed for task %s: %s", task.id, _of_e)

    # Fetch workflow EARLY: needed both for the backend success determination
    # (output_fields conformance) and for trigger dispatch / counters below.
    workflow_result = await db.execute(
        select(AutomationWorkflow).where(AutomationWorkflow.id == task.workflow_id)
    )
    workflow = workflow_result.scalar_one_or_none()

    # --- BACKEND-DETERMINED success (authoritative for money + ranking) ---
    # delivered_ok is computed from backend-observed signals (error, result
    # payload, declared output_fields), NOT from the agent's success flag.
    #
    # Take the agent-reported flag as success, BUT never report
    # success when an error was reported. A buggy agent that mis-flags a failed
    # step as success (e.g. derived success from "some data was extracted" while
    # also returning a step error) must still surface to the user as FAILED — an
    # error and a green "success" are contradictory. The agent flag stays a
    # precondition; the reported error is an authoritative backend-observed
    # failure signal.
    success = agent_reported_success and not error
    if agent_reported_success and error:
        logger.info(
            "[BYO-Trust] Task %s: agent reported success but also returned an "
            "error — marking FAILED (error overrides advisory success flag).",
            task.id,
        )

    task.status = "success" if success else "failed"
    task.completed_at = now
    task.success = success

    # DRAGNET: a crawl shard is an AutomationTask under the crawl's SYNTHETIC dataset
    # workflow. It is not a run OF that row and must not write to it — a 30-shard crawl
    # would otherwise count as 30 workflow runs, stamp last_run_at, and skew the rolling
    # duration estimate with per-shard timings. Crawl progress lives on the CrawlJob
    # (shards_done), which on_shard_complete below advances. The row is still needed
    # below (persona TTL, failure classification), so flag it rather than dropping it.
    _is_crawl_shard = _is_crawl_dataset(workflow)

    # Update workflow run counters and failure tracking
    if workflow and not _is_crawl_shard:
        workflow.total_run_count = (workflow.total_run_count or 0) + 1
        if success:
            workflow.consecutive_failures = 0
        else:
            workflow.consecutive_failures = (workflow.consecutive_failures or 0) + 1
            workflow.total_failure_count = (workflow.total_failure_count or 0) + 1
            workflow.last_failure_at = now
            workflow.last_failure_error = error

    # Update workflow usage count, duration estimate
    if success and workflow and not _is_crawl_shard:
        workflow.usage_count = (workflow.usage_count or 0) + 1
        workflow.last_run_at = now

        # Update rolling average execution duration
        if task.started_at and task.completed_at:
            duration = int((_aware_utc(task.completed_at) - _aware_utc(task.started_at)).total_seconds() * 1000)
            if workflow.estimated_duration_ms:
                workflow.estimated_duration_ms = int(
                    0.3 * duration + 0.7 * workflow.estimated_duration_ms
                )
            else:
                workflow.estimated_duration_ms = duration
    elif not success and workflow and not _is_crawl_shard:
        workflow.last_run_at = now

    if not success:
        # ── WHY did it fail? (backend-determined, never agent-reported) ─────────
        # Classify the failure into a fault domain (creator/infra/buyer/unknown)
        # for analytics + display. (services/failure_classifier.py.) Persisted on
        # the task. Never breaks completion.
        try:
            from services import failure_classifier
            _cls = failure_classifier.classify_failure(
                workflow, task, error=error,
                result_data=result_data if result_data is not None else task.result_data,
            )
            task.fault_domain = _cls.fault_domain
            task.failure_category = _cls.failure_category
            logger.info(
                "[Run] task %s FAILED: fault=%s category=%s (conf=%.2f) — %s",
                task.id, _cls.fault_domain, _cls.failure_category,
                _cls.confidence, _cls.reason,
            )
        except Exception as cls_e:
            logger.warning(
                "[Run] failure classification failed for task %s: %s", task.id, cls_e,
            )

        # In-app `run_failed` notification to the owner. Skipped for internal
        # validation / AI-generation tasks (they surface their own status on the
        # AI session, not the inbox). Best-effort: the whole block is guarded so a
        # notification can never break task completion.
        # "crawl": one shard failing is not a run the owner should be paged about —
        # a page-level failure is normal crawl attrition, and a 30-shard crawl would
        # fill the inbox. The crawl surfaces its own outcome when it finalizes.
        if task.trigger_type not in ("validation", CRAWL_TRIGGER_TYPE):
            try:
                from services import platform_notifier
                _wf_name = (workflow.name if workflow else None) or "workflow"
                _err = (error or "").strip()
                _body = f"Run of \"{_wf_name}\" failed."
                if _err:
                    _body += f" {_err[:300]}"
                await platform_notifier.notify(
                    db,
                    event="run_failed",
                    title="Run failed",
                    body=_body,
                    link="/runs",
                    payload={
                        "task_id": task.id,
                        "workflow_id": task.workflow_id,
                        "trigger_type": task.trigger_type,
                    },
                )
            except Exception as notif_e:
                logger.warning(
                    f"[Notify] run_failed notification failed for task "
                    f"{task.id}: {notif_e}"
                )

    # Check if this is a validation task and update workflow verification status
    if task.trigger_type == "validation" and task.result_data:
        workflow_id_to_verify = task.result_data.get("workflow_id") or task.workflow_id

        if workflow_id_to_verify:
            wf_verify_result = await db.execute(
                select(AutomationWorkflow).where(AutomationWorkflow.id == workflow_id_to_verify)
            )
            wf_verify = wf_verify_result.scalar_one_or_none()
            if wf_verify:
                if success:
                    wf_verify.is_verified = True
                    wf_verify.is_active = True
                else:
                    wf_verify.is_verified = False

    # Persist the warm session affinity (cookies/storage + fingerprint) so the next
    # run reuses it. The fingerprint is carried inside auth_session — restoring it
    # keeps the returning-user identity (same UA/locale/timezone) and avoids logout.
    if success and auth_session and workflow and workflow.session_persistence \
            and task.executor_agent_id:
        try:
            from services.session_state_service import SessionStateService
            await SessionStateService.save_session(
                db, workflow.id, task.executor_agent_id,
                auth_session,
                ttl_seconds=workflow.session_ttl_seconds,
            )
            logger.info(f"Saved persistent warm session for workflow {workflow.id} (task {task.id})")
        except Exception as sess_e:
            logger.warning(f"Failed to save persistent session for task {task.id}: {sess_e}")

    # Persist the warm session back onto the persona used for this run (if any).
    persona_id_used = (task.trigger_context or {}).get("_persona_id")
    if success and persona_id_used and auth_session:
        try:
            from services.persona_service import PersonaService
            from models.persona import Persona
            p = await db.get(Persona, persona_id_used)
            if p:
                ttl = workflow.session_ttl_seconds if workflow else None
                await PersonaService.save_session(db, p, auth_session, ttl_seconds=ttl)
                logger.info(f"Saved warm session to persona {p.id} (task {task.id})")
        except Exception as p_e:
            logger.warning(f"Failed to save persona session for task {task.id}: {p_e}")

    await db.commit()
    await db.refresh(task)

    logger.info(f"Task {task.id} completed: success={success}")

    # Wake any blocking trigger waiting on this task — pub/sub fast-path so the
    # waiter resolves in ~ms instead of waiting up to a full poll interval. The
    # waiter still polls as a fallback, so a missed publish is non-fatal.
    try:
        r = get_redis()
        await r.publish(f"task:completed:{task.id}", task.status or "")
        if task.workflow_id:
            await r.publish(f"workflow:completed:{task.workflow_id}", str(task.id))
    except Exception:
        pass

    # Realtime run-state push: clear this finished run from Live activity / the
    # runs feed instantly instead of on the next poll (best-effort).
    try:
        from services.run_events import emit_run_event
        await emit_run_event(
            run_type="workflow", row_id=task.id,
            status=task.status, event="ended",
        )
    except Exception:
        pass

    # Dispatch workflow_completed triggers
    #
    # DRAGNET guard: a crawl shard must not masquerade as a workflow run, or a global
    # `workflow_completed` automation fires once per shard (dozens of times per crawl).
    # The crawl emits a single `crawl_completed` on convergence instead
    # (crawl_orchestrator._finalize). on_shard_complete below still runs.
    if workflow and not _is_crawl_shard:
        try:
            from services.unified_trigger_service import get_unified_trigger_service

            trigger_service = get_unified_trigger_service(db)

            duration_ms = 0
            if task.started_at and task.completed_at:
                duration_ms = int((_aware_utc(task.completed_at) - _aware_utc(task.started_at)).total_seconds() * 1000)

            inherited_context = task.trigger_context or {}

            await trigger_service.process_workflow_event(
                event_type="workflow_completed",
                workflow_id=workflow.id,
                workflow_name=workflow.name,
                task_id=task.id,
                target_id=task.target_id,
                status=task.status,
                error=error,
                steps_completed=len((result_data or {}).get("steps", [])),
                duration_ms=duration_ms,
                # Pass the TASK's result_data (not the raw agent dict): it carries the
                # backend-reconciled output_files + output_variables so a chained
                # workflow can map a captured file into its upload slot (§4.5).
                result_data=task.result_data,
                inherited_context=inherited_context,
                consecutive_failures=workflow.consecutive_failures or 0,
                total_failure_count=workflow.total_failure_count or 0,
                total_run_count=workflow.total_run_count or 0,
            )
        except Exception as e:
            logger.error(f"Failed to dispatch workflow_completed triggers: {e}")

    # DRAGNET: a crawl-shard task carries `_crawl_id` in its trigger_context.
    # Feed its pages + newly-discovered URLs back to the crawl coordinator so it
    # admits the next frontier level and cuts fresh shards across the fleet. Runs
    # in its own db session; never blocks/raises into completion.
    try:
        if (task.trigger_context or {}).get("_crawl_id"):
            from services import crawl_orchestrator
            await crawl_orchestrator.on_shard_complete(task, result_data)
    except Exception as e:
        logger.warning(f"[Dragnet] shard-complete hook failed for task {task.id}: {e}")

    return task


@router.post("/tasks/{task_id}/complete", response_model=TaskResponse)
async def complete_task(
    task_id: int,
    result: TaskResult,
    db: AsyncSession = Depends(get_db),
    _api_key: dict = Depends(get_current_api_key),
):
    """Report task completion."""
    query = select(AutomationTask).where(AutomationTask.id == task_id)

    task_result_row = await db.execute(query)
    task = task_result_row.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    # EXECUTOR BINDING (mirror the direct-socket completion path in
    # user_recorder_ws._handle_task_result).
    # A completion report MUST come from the dispatched executor — otherwise any
    # agent could mark ANOTHER agent's in-flight task success/failed (success-truth
    # forgery). The reporter id is the agent-reported `agent_id`, first bound to
    # the authenticated identity via _verify_executor_agent (anti-spoof), then
    # required to equal the stamped executor.
    reporter_agent_id = (result.agent_id or "").strip() or None
    if reporter_agent_id:
        # Bind the supplied id to the caller (403 if it isn't theirs / is a
        # borrowed platform agent). Platform callers pass through.
        await _verify_executor_agent(db, reporter_agent_id, _api_key)
    if task.executor_agent_id:
        # A dispatched task has a stamped executor — enforce the match. (No reporter
        # id, or an executor-less task, falls through to the scope already
        # applied above, preserving older agents during rollout.)
        if not reporter_agent_id or str(task.executor_agent_id) != reporter_agent_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the dispatched executor agent may report this task's completion.",
            )

    # Terminal-state re-entry guard (mirrors the WS path at _push_ws_and_handle_failure):
    # a duplicate completion report (agent retry / double-POST) must be a no-op,
    # otherwise the Phase-0 per-running-time billing rollup (no idempotency anchor)
    # double-counts compute usage and the run/usage counters get double-bumped.
    if task.status in ('success', 'failed', 'timeout', 'cancelled'):
        logger.info(
            f"[CompleteTask] Task {task_id} already terminal ({task.status}); "
            f"ignoring duplicate completion report"
        )
        return task_to_response(task)

    task = await _process_task_completion(
        db=db,
        task=task,
        success=result.success,
        result_data=result.result_data,
        error=result.error,
        screenshots=result.screenshots,
    )
    return task_to_response(task)


async def _load_task_for_executor(db: AsyncSession, task_id: int, api_key: dict, reporter_agent_id: str = None):
    """Load an in-flight task for an AGENT-AUTHENTICATED capture call (artifact
    init/finalize), enforcing the SAME executor-binding anti-spoof as
    /tasks/{id}/complete. Returns the task (still in a non-terminal state).
    Raises 404 (not found) or 403 (not the dispatched executor)."""
    query = select(AutomationTask).where(AutomationTask.id == task_id)
    task = (await db.execute(query)).scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    reporter = (reporter_agent_id or "").strip() or None
    if reporter:
        await _verify_executor_agent(db, reporter, api_key)
    if task.executor_agent_id:
        if not reporter or str(task.executor_agent_id) != reporter:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the dispatched executor agent may capture this task's artifacts.",
            )
    return task


@router.post("/tasks/{task_id}/artifact-init")
async def artifact_init(
    task_id: int,
    req: ArtifactInitRequest,
    db: AsyncSession = Depends(get_db),
    _api_key: dict = Depends(get_current_api_key),
):
    """Begin a DIRECT-TO-STORAGE capture of a file the replay downloaded (§4.4).

    SECURITY (§10.8): a (possibly untrusted BYO) agent must NEVER stream captured
    bytes through the backend. The backend only mints a SCOPED, SIZE-CAPPED,
    short-TTL presigned upload + creates a StoredFile row in ``status="processing"``;
    the agent uploads the bytes DIRECTLY to object storage and then finalizes.

    Agent-authenticated exactly like /tasks/{id}/complete (executor binding). The
    file is owned by the single coordinator owner."""
    task = await _load_task_for_executor(db, task_id, _api_key, req.agent_id)

    from services import file_service as _fs
    from services import visual_storage as _vs
    from models.stored_file import StoredFile

    # Pre-validate the declared content-type (the REAL type is re-checked at
    # finalize from the HEAD).
    ct = (req.content_type or "application/octet-stream").split(";")[0].strip() or "application/octet-stream"
    _fs._validate_content_type(ct)

    # Single-owner coordinator: files are written to the default local store.
    _provider = None
    _store_bucket = "tenant-files"

    # Mint the file id + key, insert a processing row, mint the scoped upload.
    from uuid import uuid4
    file_id = "file_" + uuid4().hex
    key = _fs._storage_key(file_id)
    from datetime import timedelta as _td
    expires_at = datetime.now(timezone.utc) + _td(seconds=int(settings.file_ephemeral_ttl_seconds or 86400))

    f = StoredFile(
        id=file_id,
        created_by_user_id=None,
        storage_key=f"minio:{_store_bucket}/{key}",
        filename=_fs._sanitize_filename(req.filename),
        content_type=ct,
        size_bytes=0,
        source="workflow_output",
        source_run_id=task.id,
        status="processing",
        expires_at=expires_at,
        meta={"output_key": req.output_key} if req.output_key else None,
    )
    db.add(f)
    await db.flush()

    # Storage hard-caps the upload size (content-length-range) so the backend never
    # trusts an agent-claimed length. Cap at the per-file ceiling; the FULL per-org
    # quota is authoritatively re-checked against the REAL HEAD size at finalize
    # (which fail-closes + deletes the object if it would exceed the quota), so an
    # over-quota upload is never admitted even though the presigned cap is per-file.
    cap = int(settings.file_max_bytes or (100 * 1024 * 1024))
    upload = _vs.presigned_file_put(
        key, max_bytes=cap,
        expires_seconds=int(settings.file_signed_url_ttl_seconds or 600),
        provider=_provider,
    )
    if not upload:
        # No object store available → fail closed (no base64 direct-upload path by
        # design). Drop the processing row so it doesn't linger.
        await db.delete(f)
        await db.flush()
        await db.commit()
        raise HTTPException(status_code=503, detail="File storage is unavailable; cannot capture downloads right now.")

    await db.commit()
    return {"file_id": file_id, "upload": upload}


@router.post("/tasks/{task_id}/artifact-finalize")
async def artifact_finalize(
    task_id: int,
    req: ArtifactFinalizeRequest,
    db: AsyncSession = Depends(get_db),
    _api_key: dict = Depends(get_current_api_key),
):
    """Finalize a captured file after the agent's direct upload (§4.4).

    The backend HEADs the uploaded object for the REAL size + content-type (storage
    is the source of truth — the agent's claimed size is never trusted), re-validates
    size/type against policy, and either marks the row ``ready`` or deletes the
    object + errors the row. Agent-authenticated + executor-bound like
    artifact-init / complete."""
    task = await _load_task_for_executor(db, task_id, _api_key, req.agent_id)

    from services import file_service as _fs
    from services import visual_storage as _vs
    from models.stored_file import StoredFile

    # Load the processing row. The row must belong to THIS run (source_run_id) so an
    # agent can't finalize an unrelated file id.
    f = (await db.execute(
        select(StoredFile).where(
            StoredFile.id == req.file_id,
            StoredFile.source_run_id == task.id,
            StoredFile.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if f is None:
        raise HTTPException(status_code=404, detail="Capture not found for this task")
    if f.status == "ready":
        # Idempotent: an agent retry of finalize is a no-op.
        return {"file_id": f.id, "status": "ready", "size": int(f.size_bytes or 0)}

    # Resolve the file's RECORDED provider so HEAD/delete hit where the agent
    # actually uploaded the bytes (§10.A.5).
    _provider = await _fs.provider_for_file(db, f)
    head = _vs.head_file_object(f.storage_key, provider=_provider)
    if not head or int(head.get("size") or 0) <= 0:
        # Object missing / empty → the direct upload never landed. Drop the row.
        try:
            _vs.delete_file_object(f.storage_key, provider=_provider)
        except Exception:
            pass
        f.status = "error"
        f.deleted_at = datetime.now(timezone.utc)
        await db.commit()
        raise HTTPException(status_code=409, detail="Uploaded object is missing or empty")

    real_size = int(head.get("size") or 0)
    real_ct = (head.get("content_type") or f.content_type or "application/octet-stream").split(";")[0].strip()

    # Re-validate the REAL size + type. On any violation: hard-delete the object +
    # error the row.
    try:
        _fs._validate_size(real_size)
        _fs._validate_content_type(real_ct)
    except HTTPException:
        try:
            _vs.delete_file_object(f.storage_key, provider=_provider)
        except Exception:
            pass
        f.status = "error"
        f.deleted_at = datetime.now(timezone.utc)
        await db.commit()
        raise
    except Exception:
        # Same cleanup, then re-raise to the global handler.
        try:
            _vs.delete_file_object(f.storage_key, provider=_provider)
        except Exception:
            pass
        f.status = "error"
        f.deleted_at = datetime.now(timezone.utc)
        await db.commit()
        raise

    # Success: stamp the REAL size/type, mark ready.
    f.size_bytes = real_size
    f.content_type = real_ct
    f.status = "ready"
    if req.sha256 and isinstance(req.sha256, str) and len(req.sha256) == 64:
        f.sha256 = req.sha256
    await db.commit()
    return {
        "file_id": f.id,
        "status": "ready",
        "size": real_size,
        "content_type": real_ct,
        "filename": f.filename,
        "output_key": (f.meta or {}).get("output_key"),
    }


@router.post("/tasks/{task_id}/retry", response_model=TaskResponse)
async def retry_task(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    _api_key: dict = Depends(get_current_api_key),
):
    """Manually retry a failed task."""
    query = select(AutomationTask).where(
        AutomationTask.id == task_id,
    )

    result = await db.execute(query)
    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found"
        )

    if task.status not in ("failed", "timeout"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Can only retry failed or timed out tasks, current status: {task.status}"
        )

    # Reset task for retry
    task.status = "pending"
    task.started_at = None
    task.completed_at = None
    task.executor_agent_id = None
    task.success = None
    task.result_data = None
    task.error_message = None
    task.screenshots = None
    # Don't reset attempt_count - keep tracking total attempts

    await db.commit()
    await db.refresh(task)

    logger.info(f"Task {task_id} queued for retry (attempt {task.attempt_count + 1})")
    return task_to_response(task)


@router.post("/tasks/{task_id}/approve", response_model=TaskResponse)
async def approve_task(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    _api_key: dict = Depends(get_current_api_key),
):
    """Approve a held auto-buy (awaiting_approval) and dispatch the real purchase.

    The buy was held by the confirmation gate because its amount exceeded the
    user's threshold. Approving overrides dry-run and places the order for real;
    the idempotency + spend-cap checks already passed when it was held."""
    task = (await db.execute(
        select(AutomationTask).where(
            AutomationTask.id == task_id,
        )
    )).scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Task {task_id} not found")
    if task.status != "awaiting_approval":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Task is not awaiting approval (status: {task.status})",
        )

    pending = (task.trigger_context or {}).get("pending_buy") or {}
    workflow = (await db.execute(
        select(AutomationWorkflow).where(AutomationWorkflow.id == pending.get("workflow_id"))
    )).scalar_one_or_none()
    if not workflow:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Original workflow no longer exists")

    new_ctx = {
        "trigger_name": (task.trigger_context or {}).get("trigger_name"),
        "trigger_id": task.trigger_rule_id,
        "form_data": pending.get("form_data", {}),
        "extracted_data": pending.get("extracted_data", {}),
        "target_url": pending.get("target_url"),
        "encrypted_secrets": pending.get("encrypted_secrets"),
        # Approval = buy for real, regardless of the held config's dry_run.
        "dry_run": False,
        "payment_mode": pending.get("payment_mode"),
        "approved_from_task": task.id,
        "buy_meta": {
            "amount": pending.get("amount"),
            "trigger_id": task.trigger_rule_id,
            "spend_period": pending.get("spend_period", "day"),
            "dry_run": False,
        },
    }
    new_task = await _dispatch_to_recorder_or_queue(
        db=db,
        workflow=workflow,
        target_id=task.target_id or 0,
        trigger_type="on_change",
        trigger_rule_id=task.trigger_rule_id,
        trigger_context=new_ctx,
        form_data=pending.get("form_data", {}),
        persona_id=int(pending["persona_id"]) if pending.get("persona_id") else None,
    )
    task.status = "approved"
    await db.commit()
    logger.info(f"Auto-buy task {task_id} approved → dispatched task {new_task.id}")
    return task_to_response(new_task)


@router.post("/tasks/{task_id}/reject", response_model=TaskResponse)
async def reject_task(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    _api_key: dict = Depends(get_current_api_key),
):
    """Reject a held auto-buy (awaiting_approval). No purchase is made."""
    task = (await db.execute(
        select(AutomationTask).where(
            AutomationTask.id == task_id,
        )
    )).scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Task {task_id} not found")
    if task.status != "awaiting_approval":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Task is not awaiting approval (status: {task.status})",
        )
    task.status = "cancelled"
    await db.commit()
    logger.info(f"Auto-buy task {task_id} rejected by user")
    return task_to_response(task)


@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_task(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    _api_key: dict = Depends(get_current_api_key),
):
    """Cancel a pending, assigned, or running task."""
    result = await db.execute(
        select(AutomationTask).where(AutomationTask.id == task_id)
    )
    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found"
        )

    # Allow cancelling queued, pending, assigned, or running tasks. "queued"
    # included so a user can cancel a run that's waiting for a free agent — the
    # queue processor only dequeues status="queued", so flipping it to
    # "cancelled" cleanly removes it from the queue.
    if task.status not in ("queued", "pending", "assigned", "running"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Can only cancel queued, pending, assigned, or running tasks, current status: {task.status}"
        )

    task.status = "cancelled"
    task.completed_at = datetime.utcnow()
    task.error_message = "Cancelled by user"

    await db.commit()

    logger.info(f"Task {task_id} cancelled")


# ============================================================================
# Manual Dispatch Endpoints
# ============================================================================

class DispatchRequest(BaseModel):
    """Request to manually dispatch a workflow."""
    workflow_id: int = Field(..., description="Workflow ID to dispatch")
    target_id: Optional[int] = Field(None, description="Target ID (optional, for pre_check/on_change)")
    form_data: Optional[dict] = Field(None, description="Input data for workflow placeholders")
    persona_id: Optional[int] = Field(None, description="Persona (auth identity) to run as; overrides the workflow default and forces cloud execution")
    files: Optional[dict] = Field(None, description="Map {slot_or_input_key: file_id} binding the caller's own stored files to upload steps (§4.5). Each file_id is ownership-checked against the owner.")


class DispatchResponse(BaseModel):
    """Response from workflow dispatch."""
    task_id: int
    status: str
    message: str


# Capacity/fastness-aware run routing. Runs default to THROUGHPUT (biggest/
# slowest) boxes so the FAST boxes stay free for live streaming/recording. A
# DIRECT dashboard run from a paid tier may borrow a fast box, but ONLY when
# that box is lightly loaded (below this utilisation), so interactive work isn't
# starved. Tunable.
FAST_DIRECT_MAX_UTIL = 0.5
# Single-user coordinator: DIRECT runs are always fast-eligible (no plan-tier
# gate). Retained for reference.
FAST_DIRECT_MAX_PLAN_RANK = 2


def _order_run_candidates(
    candidates: list,
    *,
    traffic_type: str,
    fast_eligible: bool,
    preferred_agent_id: Optional[str] = None,
    exclude_agents: Optional[set] = None,
) -> Optional[dict]:
    """Order recorder candidates for a WORKFLOW RUN by speed class + load.

    Policy:
      * Affinity-pinned agent always wins (warm-session reuse) if it has room.
      * DIRECT + paid tier: borrow the fastest box IFF one is lightly loaded
        (< FAST_DIRECT_MAX_UTIL), otherwise fall through.
      * Otherwise THROUGHPUT-first (biggest/slowest), degrading gracefully to
        balanced then fast so a run is never stranded when throughput is full.
    Candidate dicts use keys: agent_id, available, max_sessions, active_sessions,
    speed_class, perf_score.
    """
    from services.agent_speed import speed_rank as _speed_rank, FAST

    usable = [c for c in candidates if c.get('available', 0) > 0]
    if not usable:
        return None

    # ANTI-AFFINITY: a crawl shard requeued after a host BLOCKED a specific agent
    # carries that agent in exclude_agents — steer the retry to a DIFFERENT agent/IP.
    # Fall back to the full pool if excluding would leave nothing (a single-agent
    # fleet must still make progress rather than strand the shard forever).
    if exclude_agents:
        _remaining = [c for c in usable if c.get('agent_id') not in exclude_agents]
        if _remaining:
            usable = _remaining

    if preferred_agent_id:
        for c in usable:
            if c['agent_id'] == preferred_agent_id:
                return c

    def _util(c: dict) -> float:
        mx = c.get('max_sessions') or 0
        return (c.get('active_sessions', 0) / mx) if mx else 1.0

    def _cls(c: dict) -> str:
        return c.get('speed_class') or 'balanced'

    # Interactive work (live streaming) ALWAYS prefers the fastest box,
    # regardless of tier — low latency is the whole point. Degrades gracefully
    # to balanced/throughput when no fast box is free.
    if traffic_type == 'interactive':
        usable.sort(key=lambda c: (_speed_rank(_cls(c)), -(c.get('perf_score') or 0), -c['available']))
        return usable[0]

    if traffic_type == 'direct' and fast_eligible:
        fast_idle = [
            c for c in usable
            if _cls(c) == FAST and _util(c) < FAST_DIRECT_MAX_UTIL
        ]
        if fast_idle:
            fast_idle.sort(key=lambda c: (-(c.get('perf_score') or 0), -c['available']))
            return fast_idle[0]

    # THROUGHPUT-first: higher speed_rank (throughput=2) sorts first; fall back
    # to balanced(1) then fast(0). Within a class, most free capacity wins.
    usable.sort(key=lambda c: (-_speed_rank(_cls(c)), -c['available']))
    return usable[0]


async def _pick_recorder(
    db: AsyncSession,
    workflow=None,
    preferred_agent_id: str = None,
    is_scheduled: bool = False,
    traffic_type: str = "direct",
    required_tier: str = None,
    exclude_agents: Optional[set] = None,
) -> Optional[dict]:
    """Find best available recorder agent for workflow execution.

    Three-tier priority system (three fleets, one auth):

    Tier 1 — WS-connected recorders (from _connections registry):
      - User-hosted (role="user-hosted"): end-user machines via writ-agent
      - Direct fleet (role="infrastructure"): infra recorders with persistent WS

    Tier 2 — HTTP pool recorders (from DB heartbeat):
      - Infrastructure agents reachable via HTTP push (no persistent WS)
      - Preferred for scheduled/batch work and overflow

    Tier 3 — Queue (no capacity anywhere)

    Routing by execution_target:
      - 'local':  user-hosted WS only → queue if offline
      - 'cloud':  direct WS → HTTP pool
      - 'auto':   user-hosted WS → direct WS → HTTP pool (per-workflow default)
      - scheduled: HTTP pool → direct WS (if idle)

    ``exclude_agents`` are agents this work was just REFUSED on — a crawl shard
    carries the agents whose IPs a host blocked (`_avoid_agents`). They are a
    PREFERENCE, not a filter: retrying a blocked URL from the same IP is pointless,
    but stranding it because that IP is the only one connected is worse (a
    single-agent self-host would never retry at all). So they lose every tie and are
    picked only when nothing else is free.

    Returns dict with agent_id, recorder_url, via ('websocket' or 'http'),
    or None if no recorders available.
    """
    from models.agent import Agent as AgentModel, AgentStatus as AS, Platform

    # Resolve execution_target
    execution_target = getattr(workflow, '_runtime_execution_target', None)
    if not execution_target:
        try:
            execution_target = (workflow.execution_target if workflow else 'auto') or 'auto'
        except Exception:
            execution_target = 'auto'

    # AI AUTO-REPAIR IS CLOUD-ONLY. The repair brain runs on the platform (cloud
    # fleet / server-side) and is metered there; the user's OWN/BYO agents and the
    # OSS local daemon have NO repair capability at all. So ANY run of a workflow
    # with ai_repair_enabled is forced onto the CLOUD venue here — never 'local' or
    # 'auto' (which can land on the user's own machine / a foreign BYO supply agent).
    # This is the authoritative server-side routing gate (it NEVER trusts a client-
    # or agent-reported flag — only the stored workflow setting).
    try:
        _ai_repair_on = bool(getattr(workflow, 'ai_repair_enabled', False))
    except Exception:
        _ai_repair_on = False
    if _ai_repair_on and execution_target != 'cloud':
        logger.info(
            "[_pick_recorder] workflow %s has ai_repair_enabled → forcing cloud venue "
            "(was execution_target=%r)",
            getattr(workflow, 'id', '?'), execution_target,
        )
        execution_target = 'cloud'

    trusted_only = bool(getattr(workflow, '_runtime_trusted_only', False)) or (workflow and getattr(workflow, 'trusted_agents_only', False))

    # Self-host: single-user, no plan tiers. Fast-box eligibility for DIRECT runs
    # is always granted (decided further by load in _order_run_candidates).
    fast_eligible = (traffic_type == "direct")

    def _apply_tier_pref(infra: list) -> list:
        """Filter the infra candidate pool by the run's required isolation tier.

        - A SENSITIVE run (required_tier='isolated') prefers tier=isolated boxes
          (gVisor). If none are connected it falls back to the shared fleet — which
          still runs the workflow EPHEMERAL (fresh process per run), just without
          gVisor yet — so sensitive runs never strand before the isolated pool is
          provisioned. Set REQUIRE_ISOLATED_TIER=1 to enforce strict (no fallback)
          once the isolated pool is live.
        - A non-sensitive run prefers the shared fleet so it never wastes a scarce
          isolated box, but may borrow one if that is all that is free.
        Entries without a 'tier' (pre-tier gateway) count as 'shared'.
        """
        if not infra:
            return infra
        if required_tier == 'isolated':
            iso = [c for c in infra if (c.get('tier') or 'shared') == 'isolated']
            if iso:
                return iso
            if os.getenv("REQUIRE_ISOLATED_TIER", "").strip().lower() in ("1", "true", "yes", "on"):
                logger.info("[_pick_recorder] isolated run + no isolated box + strict mode → no candidate")
                return []
            logger.info("[_pick_recorder] isolated run: no isolated box, falling back to shared fleet (ephemeral, no gVisor)")
            return infra
        shared = [c for c in infra if (c.get('tier') or 'shared') != 'isolated']
        return shared or infra

    def _best_from_ws(candidates: list) -> Optional[dict]:
        """Pick best WS candidate: anti-affinity → affinity → speed-class/load policy."""
        return _order_run_candidates(
            candidates,
            traffic_type=traffic_type,
            fast_eligible=fast_eligible,
            preferred_agent_id=preferred_agent_id,
            exclude_agents=exclude_agents,
        )

    def _ws_to_result(rec: dict) -> dict:
        return {
            'agent_id': rec['agent_id'],
            'recorder_url': None,
            'via': 'websocket',
            'available': rec['max_sessions'] - rec['active_sessions'],
            # Venue attestation for the dispatcher: 'infrastructure' = the shared
            # coordinator fleet (tiering applies); anything else = a user-hosted
            # agent.
            'role': rec.get('role', 'user-hosted'),
        }

    try:
        from routers.user_recorder_ws import get_all_connected_recorders

        # ---------------------------------------------------------------
        # Build WS candidate lists by role — single global fleet.
        # ---------------------------------------------------------------
        ws_user_hosted = []
        ws_infra = []

        try:
            for rec in get_all_connected_recorders():
                avail = rec.get('max_sessions', 2) - rec.get('active_sessions', 0)
                entry = {**rec, 'available': avail}
                if rec.get('role') == 'infrastructure':
                    ws_infra.append(entry)
                else:
                    ws_user_hosted.append(entry)
        except Exception as e:
            logger.warning(f"[_pick_recorder] WS registry check failed: {e}")

        http_allowed = True

        # ---------------------------------------------------------------
        # Route based on execution_target + is_scheduled
        # ---------------------------------------------------------------

        # LOCAL: user-hosted WS only
        if execution_target == 'local':
            pick = _best_from_ws(ws_user_hosted)
            if pick:
                logger.info(f"[_pick_recorder] local → user-hosted WS: {pick['agent_id']}")
                return _ws_to_result(pick)
            logger.info("[_pick_recorder] local → no user-hosted capacity")
            return None  # Caller will queue

        # SCHEDULED: prefer HTTP pool, then the direct fleet, then user-hosted.
        if is_scheduled:
            # Try HTTP pool first (no need for persistent connection)
            http_pick = (await _pick_http_pool_recorder(db, preferred_agent_id, trusted_only, traffic_type=traffic_type, fast_eligible=fast_eligible, exclude_agents=exclude_agents)) if http_allowed else None
            if http_pick:
                logger.info(f"[_pick_recorder] scheduled → HTTP pool: {http_pick['agent_id']}")
                return http_pick
            # Fall back to direct fleet WS if idle
            pick = _best_from_ws(_apply_tier_pref(ws_infra))
            if pick:
                logger.info(f"[_pick_recorder] scheduled → direct WS fallback: {pick['agent_id']}")
                return _ws_to_result(pick)
            # SELF-HOST: the owner's own writ-agent IS the fleet. It connects as
            # role='user-hosted', so both pools above are permanently EMPTY here —
            # and without this fallback every scheduled task returned None forever:
            # Dragnet crawl shards (minted queue_traffic_type='scheduled') and
            # scheduled workflows both sat queued until their 2h expiry, which is
            # the "crawl never launches" symptom.
            #
            # Cloud deliberately keeps scheduled work OFF end-user machines because
            # it has an infra fleet to run it on. A single-owner coordinator has no
            # such separation to protect: there is exactly one operator, the agent
            # is theirs, and it is already where every other run executes.
            pick = _best_from_ws(ws_user_hosted)
            if pick:
                logger.info(f"[_pick_recorder] scheduled → user-hosted WS: {pick['agent_id']}")
                return _ws_to_result(pick)
            logger.info("[_pick_recorder] scheduled → no capacity in any pool")
            return None

        # CLOUD: direct WS → HTTP pool
        if execution_target == 'cloud':
            pick = _best_from_ws(_apply_tier_pref(ws_infra))
            if pick:
                logger.info(f"[_pick_recorder] cloud → direct WS: {pick['agent_id']}")
                return _ws_to_result(pick)
            http_pick = (await _pick_http_pool_recorder(db, preferred_agent_id, trusted_only, traffic_type=traffic_type, fast_eligible=fast_eligible, exclude_agents=exclude_agents)) if http_allowed else None
            if http_pick:
                logger.info(f"[_pick_recorder] cloud → HTTP pool: {http_pick['agent_id']}")
                return http_pick
            return None

        # AUTO (default): user-hosted WS → direct WS → HTTP pool
        pick = _best_from_ws(ws_user_hosted)
        if pick:
            logger.info(f"[_pick_recorder] auto → user-hosted WS: {pick['agent_id']}")
            return _ws_to_result(pick)

        pick = _best_from_ws(_apply_tier_pref(ws_infra))
        if pick:
            logger.info(f"[_pick_recorder] auto → direct WS: {pick['agent_id']}")
            return _ws_to_result(pick)

        http_pick = (await _pick_http_pool_recorder(db, preferred_agent_id, trusted_only, traffic_type=traffic_type, fast_eligible=fast_eligible, exclude_agents=exclude_agents)) if http_allowed else None
        if http_pick:
            logger.info(f"[_pick_recorder] auto → HTTP pool: {http_pick['agent_id']}")
            return http_pick

        logger.info("[_pick_recorder] No recorder available across all tiers")
        return None

    except Exception as e:
        logger.error(f"Error picking recorder: {e}", exc_info=True)
        return None


async def _pick_http_pool_recorder(
    db: AsyncSession,
    preferred_agent_id: Optional[str],
    trusted_only: bool,
    traffic_type: str = "direct",
    fast_eligible: bool = False,
    exclude_agents: Optional[set] = None,
) -> Optional[dict]:
    """Pick best HTTP-pool infrastructure recorder from DB (Tier 2).

    These are infra recorders that register via heartbeat but don't maintain
    a persistent WS connection. They accept tasks via HTTP push with JWT auth.

    ``exclude_agents`` — agents this work was just refused on; see `_pick_recorder`.
    A preference, not a filter: they are dropped unless nothing else is free.
    """
    from models.agent import Agent as AgentModel, AgentStatus as AS, Platform
    from routers.user_recorder_ws import _connections  # To exclude WS-connected agents

    try:
        result = await db.execute(
            select(AgentModel)
            .where(AgentModel.platform == Platform.RECORDER)
            .where(AgentModel.status == AS.ACTIVE)
            .where(AgentModel.user_hosted.isnot(True))
        )
    except Exception:
        result = await db.execute(
            select(AgentModel)
            .where(AgentModel.platform == Platform.RECORDER)
            .where(AgentModel.status == AS.ACTIVE)
        )
    recorders = result.scalars().all()
    if not recorders:
        return None

    now = datetime.now(timezone.utc)
    candidates = []
    for r in recorders:
        # Skip agents that are WS-connected (they're in the direct fleet, not HTTP pool)
        if r.agent_id in _connections:
            continue
        meta = r.meta or {}
        url = meta.get('recorder_url', '')
        if not url:
            continue
        # Must have been seen in last 2 minutes (heartbeat freshness)
        if r.last_seen_at:
            last = r.last_seen_at.replace(tzinfo=timezone.utc) if r.last_seen_at.tzinfo is None else r.last_seen_at
            if (now - last).total_seconds() > 120:
                continue
        # Trust check
        if trusted_only and not r.is_trusted:
            continue
        from services.agent_speed import profile_from_meta
        prof = profile_from_meta(meta)
        candidates.append({
            'agent_id': r.agent_id,
            'recorder_url': url.rstrip('/'),
            'via': 'http',
            # HTTP pool = platform-owned infra recorders (shared cloud fleet).
            'role': 'infrastructure',
            'max_sessions': meta.get('max_sessions', 5),
            'active_sessions': meta.get('active_sessions', 0),
            'available': meta.get('max_sessions', 5) - meta.get('active_sessions', 0),
            'speed_class': prof['speed_class'],
            'perf_score': prof['perf_score'],
        })

    if not candidates:
        return None

    # Affinity → speed-class/load policy (throughput-first; fast-if-idle for
    # paid DIRECT runs).
    return _order_run_candidates(
        candidates,
        traffic_type=traffic_type,
        fast_eligible=fast_eligible,
        preferred_agent_id=preferred_agent_id,
        exclude_agents=exclude_agents,
    )


# ---------------------------------------------------------------------------
# Recipe metadata stripping — creator-IP best-effort hardening (consumer runs)
# ---------------------------------------------------------------------------
# A marketplace/installed run ships the creator's recipe (steps + raw_replay) to
# whatever agent executes it. For a CONSUMER run that machine may be the BUYER's
# own BYO agent or a foreign compute-supply agent — hardware the creator does NOT
# control — so anything we send is readable there. The executor only ever reads a
# small, fixed set of operational keys per step (type / selector / value / text /
# url / key / x / y / delta_y / duration / viewport / wait_before / enabled /
# config — verified across the Python desktop+writ engines AND the Rust
# agent, all of which read steps as dynamic JSON). Everything else a recorded step
# carries — human descriptions, step names/titles/labels, captured DOM text and
# aria/placeholder, screenshots, recording timestamps, AI rationale — is never
# executed but reveals the creator's INTENT and the page's STRUCTURE, turning the
# recipe from "bytes that run" into "a readable, cloneable spec". We drop those
# purely-descriptive TOP-LEVEL keys before the recipe leaves the backend on a
# consumer run, so what reaches a buyer-controlled machine is only what's needed
# to RUN, not to UNDERSTAND. Best-effort by design (a determined buyer can still
# read bare selectors/actions off their own agent); it touches no routing.
#
# Conservative BLOCKLIST, never a whitelist: we remove only keys proven to be read
# by no executor, so stripping can NEVER break a run. We never recurse into the
# nested `config` / `functions` payload — an advanced_script step's function code
# lives there and is execution-essential.
_RECIPE_METADATA_KEYS = frozenset({
    "description", "name", "title", "label",
    "screenshot", "thumbnail",
    "tag_name", "aria_label", "accessibility",
    "placeholder", "element_text",
    "timestamp", "recorded_at", "captured_at", "created_at",
    "rationale", "ai_rationale", "reasoning", "note", "notes", "comment",
})


def _strip_recipe_metadata(items):
    """Return a copy of a steps / raw_replay list with purely-descriptive
    top-level keys (see _RECIPE_METADATA_KEYS) removed. Used ONLY on consumer
    (marketplace/installed) runs so a creator's recipe reaches a buyer-controlled
    or foreign agent carrying operational fields only — not intent/structure.

    Never mutates the input (the in-memory workflow row): each dict is shallow-
    copied minus the blocklisted keys. Non-dict entries and a non-list input pass
    through unchanged. Nested `config`/`functions` are kept intact.
    """
    if not isinstance(items, list):
        return items
    out = []
    for it in items:
        if isinstance(it, dict):
            out.append({k: v for k, v in it.items() if k not in _RECIPE_METADATA_KEYS})
        else:
            out.append(it)
    return out


async def bind_relay_exit_for_task(
    db,
    *,
    task,
    workflow,
    trigger_context: dict,
    executor_role: Optional[str],
    wants_residential: bool,
) -> Optional[dict]:
    """Residential-IP relay rental was a cloud-only monetization feature (a platform
    broker renting creators' residential exits, billed to the run). The self-host
    coordinator has no relay broker, so this always returns None — the run egresses
    directly. Kept as a call-site-compatible no-op for the inline dispatch + queue
    processor."""
    return None


def build_execute_workflow_msg(
    *, task_id: int, workflow, form_data: dict, session_state, persona_cfg,
    trigger_context: dict = None, executor_role: str = None,
    relay_proxy: dict = None,
    byo_proxy: dict = None, wants_residential: bool = False,
    files: dict = None,
) -> dict:
    """Build the execute_workflow WS message. SYNCHRONOUS on purpose: it must
    snapshot the (possibly in-memory consumer-inverted) workflow state at call
    time, before any await — otherwise the queue processor, which inverts a fresh
    workflow per task in a loop, could let a deferred dispatch read another task's
    mutation (a consumer-data isolation hazard).

    FILE ASSETS (§4.1): `files` is the run-level files map
    ``{ "<file_id>": {"url": <short-TTL signed GET>, "filename", "content_type",
    "size"} }`` resolved by the ASYNC caller (the run endpoint / queue processor)
    BEFORE this synchronous builder, via file_service.resolve_for_run. The map is
    ownership-checked at resolution and every URL is single-object + short-TTL —
    the agent fetches the bytes
    directly from storage, never from the backend. None ⇒ no upload-step files for
    this run.

    The caller must have already set workflow.credentials_encrypted to the
    EFFECTIVE credentials for this run; vcard folding happens here so all paths
    are identical.

    `executor_role` is the picked agent's venue role ('infrastructure' = shared
    fleet; anything else = the user's OWN agent). Isolation tiering applies
    ONLY on the shared fleet — an own-BYO run is the user's machine/trust domain
    and never goes ephemeral, regardless of sensitivity.

    BYO PROXY (money-safe precedence) — a browser has ONE egress and the
    REVENUE-GENERATING creator-IP relay MUST win. `byo_proxy` (the run persona's
    resolved + acknowledged residential proxy) is injected as the reserved
    credentials key "__proxy__" (the agent already consumes it) ONLY when ALL of:
      (a) byo_proxy is truthy (persona has an ACKED proxy), AND
      (b) relay_proxy is None for this run (NO creator-IP exit was bound), AND
      (c) NOT wants_residential (a residential-intent run is a creator-IP run by
          design — even on the queue path where no exit is picked, BYO must NOT
          substitute for it, or the broker would attest ~0 bytes and the creator
          would be credited $0).
    Gated on the COMPUTED relay decision + the wants_residential signal — never on
    a re-derived tier/executor guess. When in doubt, do NOT inject (fail toward
    direct egress).
    """
    credentials_encrypted = _fold_vcard_into_credentials(
        workflow.credentials_encrypted, trigger_context)
    # Fold per-call webhook / custom-API secrets (trigger_context.encrypted_secrets)
    # into the run credentials BEFORE classification, so a sensitive webhook run on
    # the cloud fleet is recognized as ISOLATED (and ships its secrets on the WS
    # inline path, matching _push_workflow_to_recorder / _push_workflow_via_ws).
    _enc_secrets = (trigger_context or {}).get("encrypted_secrets")
    if _enc_secrets:
        try:
            import json as _json
            from security.encryption import decrypt_secrets_blob, SecretEncryption
            _merged = {}
            if credentials_encrypted:
                try:
                    _merged.update(_json.loads(SecretEncryption.decrypt_secret(credentials_encrypted)))
                except Exception:
                    pass
            _merged.update(decrypt_secrets_blob(_enc_secrets))
            credentials_encrypted = SecretEncryption.encrypt_secret(_json.dumps(_merged))
        except Exception as e:
            logger.error(f"[build_execute_workflow_msg] Failed to fold webhook secrets: {e}")

    # Decide the isolation tier the AGENT should honor for this run. Sensitivity
    # is a property of the run's DATA (credentials/secrets/persona, or an
    # unverified marketplace recipe); it only forces the ISOLATED tier when the
    # run also lands on Writ Cloud. On the user's own agent we never tier.
    isolation_tier = "shared"
    try:
        from services.workflow_router import WorkflowRouter, IsolationTier
        is_cloud_venue = (executor_role == "infrastructure")
        if is_cloud_venue:
            sensitivity = WorkflowRouter.classify_sensitivity(
                has_credentials=bool(credentials_encrypted),
                has_persona=bool(persona_cfg),
                workflow=workflow,
                trigger_context=trigger_context,
            )
            isolation_tier = sensitivity.value
    except Exception:
        # Fail CLOSED on the cloud fleet (default-isolated-in-doubt); an own-BYO
        # venue is the user's own machine and must never be forced ephemeral.
        isolation_tier = "isolated" if executor_role == "infrastructure" else "shared"

    # --- PER-RUN EGRESS PROXY (money-safe precedence) ------------------------
    # A browser context has ONE egress. We inject it as the reserved credentials
    # key "__proxy__" {server, username, password, bypass}, which the engine pops +
    # applies to the per-run CONTEXT (automation_engine.execute_workflow →
    # _create_stealth_context; a fresh context per run, even on the warm engine, so
    # there is no cross-run egress bleed). Priority:
    #   1. the REVENUE-GENERATING creator-IP RELAY (relay_proxy) on a cloud-fleet run
    #      — it WINS over any BYO proxy. The per-run routing_token rides as the proxy
    #      USERNAME: Chromium authenticates a CONNECT with Basic proxy-auth, and the
    #      broker reads the token from it (it also accepts a Bearer header). The
    #      egress points at the platform broker (chokepoint), never the creator box.
    #   2. else the run persona's acknowledged BYO residential proxy (byo_proxy), but
    #      ONLY when relay_proxy is None AND NOT wants_residential — a residential-
    #      intent run is a creator-IP run by design; BYO must never substitute for it
    #      (else the broker attests ~0 bytes and the creator is credited $0).
    # Gated on the COMPUTED relay decision + executor role + the wants_residential
    # signal, NEVER on a re-derived tier guess. When in doubt, do NOT inject (fail
    # toward direct egress).
    _ctx_proxy = None
    if relay_proxy and executor_role == "infrastructure" and relay_proxy.get("proxy_server"):
        _ctx_proxy = {
            "server": relay_proxy["proxy_server"],
            # routing_token as the proxy username → Basic proxy-auth the broker reads.
            "username": relay_proxy.get("routing_token") or "",
            "password": "",
            "bypass": "0.0.0.0/1,128.0.0.0/1,localhost,[::1]",
        }
    elif byo_proxy and relay_proxy is None and not wants_residential and byo_proxy.get("server"):
        _ctx_proxy = {
            "server": byo_proxy.get("server"),
            "username": byo_proxy.get("username"),
            "password": byo_proxy.get("password"),
            "bypass": byo_proxy.get("bypass")
            or "0.0.0.0/1,128.0.0.0/1,localhost,[::1]",
        }
    if _ctx_proxy:
        try:
            # Add __proxy__ to the run credentials BEFORE they ship encrypted. If a
            # creds blob already exists, decrypt (master key) -> add -> re-encrypt
            # (master key) so the existing secrets ride unchanged; otherwise create
            # a fresh blob carrying ONLY __proxy__.
            _creds = decrypt_credentials(credentials_encrypted) if credentials_encrypted else {}
            _creds["__proxy__"] = _ctx_proxy
            credentials_encrypted = encrypt_credentials(_creds)
        except Exception as e:
            logger.error(f"[build_execute_workflow_msg] Failed to inject __proxy__: {e}")

    # Strip creator-IP descriptive metadata from the recipe on CONSUMER runs only
    # (the executor may be the buyer's own / a foreign supply agent — hardware the
    # creator doesn't control). No-op for own runs. See _strip_recipe_metadata.
    _consumer_recipe = (trigger_context or {}).get("_data_source") == "consumer"
    _msg_steps = _strip_recipe_metadata(workflow.steps) if _consumer_recipe else workflow.steps
    _msg_raw_replay = _strip_recipe_metadata(workflow.raw_replay) if _consumer_recipe else workflow.raw_replay

    # DRAGNET: a crawl-shard task carries its URL batch + extraction spec in the
    # trigger_context. Those keys must ride INSIDE the execute_workflow message
    # (the rest of trigger_context is intentionally NOT shipped to the agent), so
    # the crawl executor receives its shard. Only attached for crawl shards.
    _crawl_ctx = None
    _tc = trigger_context or {}
    if _tc.get("_crawl_shard") is not None or _tc.get("_crawl_id") is not None:
        _crawl_ctx = {
            k: _tc[k] for k in ("_crawl_id", "_crawl_shard", "_crawl_extract")
            if k in _tc
        }

    return {
        "type": "execute_workflow",
        "task_id": task_id,
        "workflow_id": workflow.id,
        "config": {
            "entry_url": workflow.entry_url,
            "steps": _msg_steps,
            "raw_replay": _msg_raw_replay,
            "form_data": form_data,
            "credentials_encrypted": credentials_encrypted,
            "timeout_ms": workflow.timeout_ms,
            "headless": workflow.headless if workflow.headless is not None else True,
            "fast_mode": workflow.fast_mode if workflow.fast_mode is not None else True,
            "session_state": session_state,
            "session_persistence": bool(workflow.session_persistence),
            "login_url_patterns": workflow.login_url_patterns or [],
            "persona": persona_cfg,
            # Isolation tier the agent must honor: 'isolated' → fresh browser
            # PROCESS per run, no warm reuse, destroyed after (sensitive cloud
            # run); 'shared' → normal warm-browser/context behavior.
            "isolation_tier": isolation_tier,
            # IP-RELAY (Phase 6, doc §5): per-run residential-exit descriptor for a
            # CLOUD-FLEET run (ANY tier — residential egress is orthogonal to
            # isolation). OBSERVABILITY ONLY: the proxy is actually APPLIED above by
            # injecting it as the credentials "__proxy__" key (the engine pops it and
            # egresses the per-run context through it). Kept here so the agent log /
            # result can show which broker exit a run used. Egress goes through the
            # platform relay broker (non-terminating TLS — the creator's box never
            # sees plaintext). Stamped ONLY on a cloud-fleet run (executor_role
            # 'infrastructure'). None when no exit was rented/available.
            "relay_proxy": (
                relay_proxy if (relay_proxy and executor_role == "infrastructure")
                else None
            ),
            # Auto-buy: agent stops before the commit step when dry_run; payment_mode
            # tells it to use a saved method / autofill (never a stored card number).
            "dry_run": bool((trigger_context or {}).get("dry_run", False)),
            "payment_mode": (trigger_context or {}).get("payment_mode"),
            # FILE ASSETS (§4.1): run-level files map { file_id -> {url, filename,
            # content_type, size} }. The agent fetches each file's bytes from the
            # single-object, short-TTL signed `url` to feed an upload step's file
            # input (never from the backend). Empty when no files are referenced.
            "files": files or {},
            # DRAGNET crawl shard: {_crawl_id, _crawl_shard:[{url,depth}],
            # _crawl_extract:{mode,schema,delay_ms}}. None for non-crawl runs.
            "trigger_context": _crawl_ctx,
        },
    }


async def dispatch_ws_workflow_task(
    *,
    task_id: int,
    agent_id: str,
    message: dict,
    trigger_context: dict = None,
):
    """Push a prebuilt execute_workflow message to a WS/gateway-connected agent and
    process its completion. The SINGLE WS dispatch path shared by the synchronous
    run endpoint and the queue processor, so cloud (gateway) runs complete
    identically on both.

    push_to_recorder routes to a direct WS connection, or — for gateway-connected
    agents (which register with an empty recorder_url) — falls back to the
    ws-gateway dispatch. That covers the entire shared cloud fleet, which the old
    queue path (HTTP push by recorder_url) could never reach.
    """
    from routers.user_recorder_ws import push_to_recorder
    from database import AsyncSessionLocal

    # TOCTOU guard: refuse to push a run whose dispatch txn never committed or
    # whose marketplace hold failed/rolled back.
    if not await _await_task_dispatchable(task_id, trigger_context):
        return
    result = await push_to_recorder(agent_id, message)

    # CRAWL SHARD → crawl-native THIN completion: one atomic claim UPDATE + crawl
    # advance (crawl_orchestrator.complete_shard_task). A converging crawl wave is
    # a high-frequency fan-in, not a workflow run: the ceremony below neither
    # applies to shards nor survives that concurrency (each completion would hold a
    # pooled connection through a long transaction, starving the pool and LOSING
    # completions — shards then sit in 'running' until the sweep reaps them).
    _cid = (trigger_context or {}).get("_crawl_id")
    if _cid:
        from services.crawl_orchestrator import complete_shard_task
        if not result or (isinstance(result, dict) and not result.get("success", True)):
            error_msg = (result.get("error", "Push to agent failed")
                         if isinstance(result, dict) else "Agent not reachable")
            logger.error(f"[WorkflowPush-WS] Crawl shard task {task_id} failed: {error_msg}")
            await complete_shard_task(task_id, int(_cid), success=False,
                                      error=str(error_msg), reporter_agent=agent_id)
        elif isinstance(result, dict):
            await complete_shard_task(
                task_id, int(_cid),
                success=result.get("success", False),
                result_data=result.get("result_data"),
                error=result.get("error"),
                reporter_agent=agent_id,
            )
        return

    async with AsyncSessionLocal() as ws_db:
        r = await ws_db.execute(select(AutomationTask).where(AutomationTask.id == task_id))
        t = r.scalar_one_or_none()
        if not t or t.status not in ('running', 'pending', 'assigned', 'queued'):
            return
        if not result or (isinstance(result, dict) and not result.get("success", True)):
            error_msg = result.get("error", "Push to agent failed") if isinstance(result, dict) else "Agent not reachable"
            logger.error(f"[WorkflowPush-WS] Task {task_id} failed: {error_msg}")
            await _process_task_completion(
                db=ws_db, task=t, success=False, error=str(error_msg),
            )
        elif isinstance(result, dict):
            logger.info(f"[WorkflowPush-WS] Task {task_id} completed via WS")
            await _process_task_completion(
                db=ws_db, task=t,
                success=result.get("success", False),
                result_data=result.get("result_data"),
                error=result.get("error"),
                auth_session=result.get("auth_session"),
            )


# Serializes the pick→reserve critical section across concurrent dispatches so
# each pick sees prior in-flight reservations (spread across the fleet; queue once
# every agent is full) instead of all racing onto the same "least-loaded" agent.
_dispatch_pick_lock = asyncio.Lock()


async def _dispatch_to_recorder_or_queue(
    db: AsyncSession,
    workflow: AutomationWorkflow,
    target_id: int = 0,
    trigger_type: str = "manual",
    trigger_rule_id: int = None,
    trigger_context: dict = None,
    form_data: dict = None,
    persona_id: int = None,
    _interactive: bool = False,
    _install_override=None,
    _recipe_snapshot_override: dict = None,
) -> AutomationTask:
    """Dispatch a workflow: push to recorder if available, otherwise queue for desktop agents.

    Routing rules:
    - scheduled: Always queue for desktop agents (distributed by capacity_aware_distributor)
    - manual/webhook/on_change: Try recorder first for instant execution, fall back to queue

    This is the central dispatch function used by all workflow execution paths:
    - /dispatch (manual)
    - /dispatch-and-wait (synchronous webhook)
    - UnifiedTriggerService._dispatch_workflow (trigger rules)
    - custom_apis.py (webhook triggers via trigger service)

    Enforces concurrent browser limits at the central dispatch point.
    """
    import asyncio

    # Run-time domain blocklist: a domain blocked AFTER a workflow was created
    # must still be stopped at dispatch. No-write check (we're inside the caller's
    # transaction) — refuse to run a workflow whose entry domain is now blocked.
    from services import domain_guard
    await domain_guard.ensure_loaded(db)
    _entry_url = getattr(workflow, "entry_url", None)
    _block_reason = domain_guard.url_block_reason(_entry_url)
    if _block_reason is not None:
        raise HTTPException(
            status_code=403,
            detail=f"This workflow's entry domain is blocked by the platform administrator"
                   + (f": {_block_reason}" if _block_reason else "."),
        )

    # Scraping-abuse guardrails, enforced at the central run choke point so
    # they cover every dispatch path (manual / scheduled / webhook / MCP / trigger):
    #   the prohibited-category screen (fail-CLOSED legality guard, no-DB),
    #   the per-target-domain rate limit (fail-OPEN abuse control), and
    #   the AGGREGATE run-rate cap (DDoS-amplifier guard): bounds the
    #   TOTAL dispatch rate across ALL hosts so the browser fleet can't
    #   be weaponised by fanning runs across many domains (each under the
    #   per-domain ceiling). Also fail-OPEN.
    from services import target_rate_limit
    # Single-owner coordinator: no per-owner key (these no-op on a None id); the
    # prohibited-category screen still applies.
    target_rate_limit.enforce_prohibited_category(_entry_url)
    await target_rate_limit.enforce_run_rate_limit(None)
    await target_rate_limit.enforce_target_rate_limit(None, _entry_url)

    # Single-user: every run is the owner's own workflow. _is_consumer_run stays
    # defined (always False) so the shared dispatch logic below is unchanged.
    _is_consumer_run = False

    persona = None
    persona_creds = {}
    persona_cfg = None
    persona_session = None
    merged_form_data = None  # own-run computes below
    # BYO residential proxy resolved from the RUN persona (the workflow owner's).
    byo_proxy_cfg = None

    # Self-host: no consumer/marketplace billing hold. _place_consumer_hold is a
    # no-op stub retained so downstream flush sites need no change.
    async def _place_consumer_hold(_task) -> None:
        return

    # --- Persona resolution (authenticated identity) ---
    # Effective persona = run-time override -> workflow default -> none.
    # A resolved persona forces cloud execution on a trusted residential agent,
    # supplies login credentials, and provides a 2FA OTP-mint token to the agent.
    if not _is_consumer_run:
        eff_persona_id = persona_id if persona_id is not None else getattr(workflow, "default_persona_id", None)
        if eff_persona_id:
            from services.persona_service import PersonaService, PersonaError
            from models.persona import Persona
            persona = await db.get(Persona, eff_persona_id)
            if not persona:
                raise HTTPException(status_code=404, detail="Persona not found")
            if not persona.is_active:
                raise HTTPException(status_code=409, detail="Persona is inactive")
            # Force cloud + trusted (runtime-only; never mutate the stored columns)
            workflow._runtime_execution_target = "cloud"
            workflow._runtime_trusted_only = True
            if not PersonaService.domain_matches(persona, workflow.entry_url):
                logger.warning(
                    f"Persona {persona.id} domain '{persona.target_domain}' does not match "
                    f"workflow {workflow.id} entry_url; proceeding anyway"
                )
            try:
                persona_creds = PersonaService.resolve_login_credentials(persona)
            except PersonaError as e:
                raise HTTPException(status_code=422, detail=f"Persona credential error: {e}")
            # Resolve any credentials linked to vault secrets ({{vault:key}}) — kept as
            # live references on the persona, swapped for the real value here.
            if persona_creds:
                from services.secret_resolver import resolve_vault_in_credentials
                persona_creds = await resolve_vault_in_credentials(db, persona_creds)
            persona_session = PersonaService.load_session(persona)
            persona_cfg = {
                "persona_id": persona.id,
                "twofa_method": persona.twofa_method or "none",
                "otp_extract_config": persona.otp_extract_config or {},
                # Short-lived token the agent presents to POST /api/personas/{id}/otp.
                # The TOTP seed / mailbox tokens never leave the backend.
                "otp_token": PersonaService.make_otp_token(persona.id, None),
                # Coordinator base URL the agent calls back to for OTP minting.
                "coordinator_url": settings.coordinator_url,
            }
            # Own-run persona's BYO proxy (acked only). Injection still gated by the
            # money-safe precedence in build_execute_workflow_msg.
            byo_proxy_cfg = PersonaService.resolve_proxy(persona)
            # Tag the task so completion can persist the warm session back onto the persona.
            trigger_context = {**(trigger_context or {}), "_persona_id": persona.id}

    # Self-host: single-user coordinator has no cloud plan tiers, so there is no
    # premium-feature gate (CAPTCHA / 2FA are ungated) and no plan-based concurrency
    # cap. Concurrency is governed by the local Runtime governor, not here.

    # --- 2FA pre-flight: a workflow that ENTERS a one-time code can only pass its
    # challenge with an OTP source (a persona minting TOTP / reading email).
    # INTERACTIVE runs get an instant, actionable 422 before an agent is engaged.
    # Background triggers (schedules/webhooks) deliberately fall through: the
    # engines' own pre-flight fails the run visibly, whereas raising here would be
    # swallowed by the scheduler loop.
    if _interactive and not persona_cfg:
        if any(
            isinstance(s, dict) and s.get("type") == "twofa"
            for s in (getattr(workflow, "steps", None) or [])
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    "This workflow enters a 2FA code when signing in, but no persona is "
                    "attached. Attach a persona with a 2FA method (or import its 2FA "
                    "secret) so runs can enter codes automatically."
                ),
            )

    # FORM_DATA + CREDENTIALS — OWN RUNS ONLY.
    # Consumer runs already resolved merged_form_data + folded their (consumer-only)
    # credentials into workflow.credentials_encrypted inside
    # _apply_consumer_run_inversion above, so this entire block is gated off for
    # them — it must NEVER read workflow.form_data / workflow.credentials_encrypted
    # (creator data) on a consumer run.
    if not _is_consumer_run:
        merged_form_data = {**(workflow.form_data or {}), **(form_data or {})}

        # Resolve {{vault:key}} references from the secrets vault (with Redis caching).
        vault_secrets = {}
        if any("{{vault:" in str(v) for v in merged_form_data.values()):
            from services.secret_resolver import resolve_form_data
            merged_form_data, vault_secrets = await resolve_form_data(db, merged_form_data)

        # Handle $secret: prefixed fields from API callers
        # e.g. {"$secret:password": "abc"} → encrypted, merged with workflow credentials
        inline_secrets_encrypted = None
        if any(k.startswith('$secret:') for k in merged_form_data):
            from security.encryption import extract_and_encrypt_secrets
            merged_form_data, inline_secrets_encrypted = extract_and_encrypt_secrets(merged_form_data)

        # CREDENTIAL ROUTE. Run-time inputs for {{secret:key}} placeholders arrive from
        # the UI under the placeholder's OWN key — "secret:key" — because
        # _extract_placeholders returns the whole text inside {{...}}. Route them into
        # the credentials channel under the BARE key and REMOVE them from form_data, so
        # secrets travel exactly ONE route (encrypted credentials, re-encrypted for the
        # agent's channel key on every dispatch path) and form_data carries only
        # non-sensitive {{key}} values. The engine resolves {{secret:key}} from
        # credentials and {{key}} from form_data — the two routes never mix.
        runtime_secrets = {}
        for k in [k for k in list(merged_form_data.keys()) if k.startswith('secret:')]:
            v = merged_form_data.pop(k)
            bare = k[len('secret:'):]
            if bare and v not in (None, ""):
                runtime_secrets[bare] = str(v)

        # Merge all credential sources: workflow stored → vault → inline → run-time → persona.
        all_creds = {}
        if workflow.credentials_encrypted:
            all_creds.update(decrypt_credentials(workflow.credentials_encrypted))
        if vault_secrets:
            all_creds.update(vault_secrets)
        if inline_secrets_encrypted:
            from security.encryption import decrypt_secrets_blob
            all_creds.update(decrypt_secrets_blob(inline_secrets_encrypted))
        if runtime_secrets:
            all_creds.update(runtime_secrets)
        if persona_creds:
            # Persona supplies the login identity, overriding manual placeholders.
            all_creds.update(persona_creds)
        if all_creds:
            workflow.credentials_encrypted = encrypt_credentials(all_creds)

        placeholders = _extract_placeholders(workflow.steps, workflow.form_data)
        if placeholders:
            # Keys now living in the encrypted credentials channel (workflow-stored,
            # vault, inline $secret:, run-time secret: inputs, or persona) satisfy both
            # {{key}} and {{secret:key}} placeholder forms.
            persona_supplied = set(persona_creds.keys())
            persona_supplied |= {f"secret:{k}" for k in persona_creds.keys()}
            persona_supplied |= set(all_creds.keys())
            persona_supplied |= {f"secret:{k}" for k in all_creds.keys()}
            missing = [
                p["key"] for p in placeholders
                if p["key"] not in persona_supplied
                and (
                    p["key"] not in merged_form_data
                    or merged_form_data[p["key"]] is None
                    or merged_form_data[p["key"]] == ""
                )
            ]
            if missing:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "message": f"Workflow '{workflow.name}' requires input data that was not provided.",
                        "missing_fields": missing,
                    },
                )

    # --- FILE ASSETS: run-level files map (§4.1/§4.5) ------------------------
    # Resolve every stored file this run references into a short-TTL, single-object
    # signed-URL descriptor map BEFORE the SYNCHRONOUS build_execute_workflow_msg
    # (resolve_for_run is async). The running owner owns the files.
    # resolve_for_run fail-closes (404) on a non-owned id, so a bad reference
    # fails the run rather than leaking another owner's file.
    # The map is snapshotted onto the workflow object so the sync builder reads the
    # per-task state (matching how steps/credentials are snapshotted), and is also
    # used by the sensitive-routing guard below.
    _request_files = (trigger_context or {}).get("files") if isinstance(trigger_context, dict) else None
    # `_files_referenced` drives the §4.3 sensitive-routing guard and gates the
    # (DB + presign) resolution so the common no-files run pays nothing. NOTE: a
    # QUEUED / desktop-deferred run re-resolves fresh signed URLs at actual dispatch
    # (queue processor / get_pending_task / _push_workflow_via_ws) so a short-TTL
    # URL minted here can never go stale before use.
    _files_referenced = _run_references_files(workflow, _request_files)
    workflow._run_files_map = {}
    if _files_referenced:
        workflow._run_files_map = await _resolve_run_files_map(
            db, workflow,
            request_files=_request_files,
            # Keep the signed-URL TTL >= the expected run duration so the agent can
            # fetch mid-run; the workflow timeout (ms) is the floor when it exceeds
            # the default file TTL.
            ttl_seconds=max(
                int(settings.file_signed_url_ttl_seconds or 600),
                int((getattr(workflow, "timeout_ms", None) or 0) // 1000) + 60,
            ),
        )

    # --- Intelligent routing ---
    from services.workflow_router import WorkflowRouter, TargetAgent, TrafficType
    from services.recorder_capacity_manager import RecorderCapacityManager
    from services.workflow_queue import WorkflowQueue

    # trigger_context already carries the consumer/marketplace marker (and api/
    # webhook type) by here, so the router can classify this as a CALLED run.
    routing = WorkflowRouter.route(workflow, trigger_type, trigger_context)

    # Non-AI scheduled workflows → desktop agent path (unchanged).
    # A persona forces cloud, so never let a persona run short-circuit to desktop.
    if routing.target == TargetAgent.DESKTOP_PREFERRED and not persona:
        task = AutomationTask(
            target_id=target_id,
            workflow_id=workflow.id,
            trigger_type=trigger_type,
            trigger_rule_id=trigger_rule_id,
            status="pending",
            trigger_context=trigger_context,
            queue_traffic_type=routing.traffic_type.value,
            max_attempts=(workflow.retry_count or 0) + 1,
        )
        db.add(task)
        await db.flush()
        await _place_consumer_hold(task)
        logger.info(
            f"Workflow '{workflow.name}' pending for desktop agents "
            f"(task_id={task.id}, requires_ai={routing.requires_ai})"
        )
        return task

    # Recorder-bound: load session affinity if applicable.
    # CONSUMER RUN: NEVER load the creator's per-workflow warm session (it would
    # bleed creator auth — or another consumer's auth — into this buyer's run). The
    # consumer's warm session lives ONLY on their bound persona (persona_session),
    # resolved above and applied below.
    session_state = None
    preferred_agent_id = None
    # BUYER AGENT PIN: a marketplace buyer chose a specific OWN agent in the run
    # UI (validated in run_workflow against the creator policy + their own online
    # agents). It takes precedence over session affinity (which is skipped for
    # consumer runs anyway). It is a PREFERENCE within the already-allowed class —
    # _pick_recorder still applies ownership scope / tier / capacity and falls
    # through to another allowed candidate if the pinned agent is full/offline.
    _buyer_pin = getattr(workflow, "_buyer_pin_agent_id", None)
    if _buyer_pin:
        preferred_agent_id = _buyer_pin
    if workflow.session_persistence and not _is_consumer_run and not preferred_agent_id:
        try:
            from services.session_state_service import SessionStateService
            preferred = await SessionStateService.get_preferred_affinity(db, workflow.id)
            if preferred and not SessionStateService.is_expired(preferred, workflow.session_ttl_seconds):
                preferred_agent_id = preferred.agent_id
                session_state = await SessionStateService.load_session(
                    db, workflow.id, preferred.agent_id, workflow.session_ttl_seconds,
                )
        except Exception as e:
            logger.warning(f"Failed to load session affinity for workflow {workflow.id}: {e}")

    # Unified three-tier recorder selection
    is_scheduled = trigger_type == "scheduled"
    # Classify traffic so the selector can route by speed class: DIRECT dashboard
    # runs may borrow a fast box (paid tiers), CALLED/SCHEDULED take throughput
    # boxes and leave fast capacity for live streaming/recording.
    try:
        from services.workflow_router import WorkflowRouter
        _traffic_type = WorkflowRouter.classify_traffic(trigger_type, locals().get("trigger_context")).value
    except Exception:
        _traffic_type = "scheduled" if is_scheduled else "direct"
    # Required isolation tier IF this lands on Writ Cloud — so a sensitive run
    # prefers a gVisor (isolated) box. Computed from the EFFECTIVE run signals
    # (credentials set by the consumer inversion + the resolved persona). The
    # precise per-run tier the agent honors is still re-derived in
    # build_execute_workflow_msg; this only steers the cloud pick.
    try:
        _required_tier = WorkflowRouter.classify_sensitivity(
            has_credentials=bool(getattr(workflow, "credentials_encrypted", None)),
            has_persona=bool(persona_cfg),
            workflow=workflow,
            trigger_context=locals().get("trigger_context"),
        ).value
    except Exception:
        # Fail CLOSED: an unclassifiable run is treated as sensitive (isolated) so it
        # is NEVER made supply-pool eligible and never reaches a foreign BYO agent
        # ('default isolé en cas de doute'). Matches classify_sensitivity's own
        # internal fail-safe (returns ISOLATED on error).
        _required_tier = "isolated"

    # --- PREMIUM / RESIDENTIAL EXIT runs on the SHARED CLOUD fleet -----------
    # Residential egress is ORTHOGONAL to the isolation tier. A non-sensitive
    # premium run KEEPS its natural 'shared' tier (cheap warm browser) — it just
    # egresses through a rented residential IP. What a residential run DOES require
    # is the platform CLOUD fleet (role 'infrastructure'): the relay attaches only
    # where the per-run PROXY_SERVER points at our broker chokepoint (egress
    # allowlist + broker-attested byte metering for billing). A foreign BYO supply
    # box is not a metered relay consumer, and the buyer's own agent has no broker
    # path — so we steer a residential run to the 'cloud' venue (unless the buyer
    # explicitly pinned 'local', which we honor by simply not attaching a relay).
    # Forcing the cloud venue also makes the run supply-pool INELIGIBLE below
    # (eligibility requires the effective target to be 'auto'). _wants_residential
    # is computed once here and reused as the single source of truth. A residential
    # exit is requested by the PREMIUM SKU only (set on a marketplace run whose
    # listing has sku_allowed='premium'; a non-premium-allowed listing clamps the
    # buyer's request to 'standard' — see run_workflow).
    _mkt_ctx_early = (trigger_context or {}).get("_marketplace") or {}
    # A residential exit is requested by EITHER the premium marketplace SKU or an
    # OWN run that opted in via use_residential_proxy. Both consume the shared
    # relay pool.
    _own_run_residential = bool((trigger_context or {}).get("use_residential_proxy"))
    _wants_residential = (_mkt_ctx_early.get("sku") == "premium") or _own_run_residential
    # VISIBILITY: a marketplace run that did NOT resolve to premium gets no relay —
    # log the resolved sku so a "why is my IP-relay earning $0" is diagnosable
    # without code-reading.
    if _mkt_ctx_early:
        logger.info(
            "[_dispatch] marketplace run workflow=%s sku=%s → wants_residential=%s",
            getattr(workflow, "id", None), _mkt_ctx_early.get("sku"), _wants_residential,
        )
    # There is no residential-IP relay broker here, so there is no proxy-credit
    # affordability gate. bind_relay_exit_for_task is a no-op and the run egresses
    # directly.
    if _wants_residential:
        _rt_target = getattr(workflow, "_runtime_execution_target", None) or "auto"
        if _rt_target != "local":
            workflow._runtime_execution_target = "cloud"
            logger.info(
                "[_dispatch] residential exit requested (premium SKU) — routing to "
                "shared CLOUD fleet (tier unchanged=%s) so the broker relay can "
                "attach (workflow=%s)",
                _required_tier, getattr(workflow, "id", None),
            )

    # A run lands on the owner's own fleet only. Pick → create task → RESERVE the
    # slot as ONE atomic critical section (under _dispatch_pick_lock) so N concurrent
    # dispatches each observe the others' in-flight load: they spread across the
    # fleet and, once every agent is at capacity, _pick_recorder returns None and the
    # run is QUEUED (instead of all piling onto one agent past its slot count).
    async def _pick_and_reserve():
        async with _dispatch_pick_lock:
            rec = await _pick_recorder(
                db, workflow,
                preferred_agent_id=preferred_agent_id,
                is_scheduled=is_scheduled,
                traffic_type=_traffic_type,
                required_tier=_required_tier,
            )
            et = getattr(workflow, '_runtime_execution_target', None)
            if not et:
                try:
                    et = workflow.execution_target or 'auto'
                except Exception:
                    et = 'auto'
            tk = None
            ds = None
            if rec:
                # A persona owns its warm session (identity-scoped, shared across
                # workflows); it takes precedence over per-workflow+agent affinity.
                if persona_session is not None:
                    ds = persona_session
                else:
                    ds = session_state if rec['agent_id'] == preferred_agent_id else None
                tk = AutomationTask(
                    target_id=target_id,
                    workflow_id=workflow.id,
                    trigger_type=trigger_type,
                    trigger_rule_id=trigger_rule_id,
                    status="running",
                    started_at=datetime.utcnow(),
                    executor_agent_id=rec['agent_id'],
                    trigger_context=trigger_context,
                    queue_traffic_type=routing.traffic_type.value,
                    max_attempts=(workflow.retry_count or 0) + 1,
                )
                db.add(tk)
                await db.flush()
                if rec.get('via') == 'websocket':
                    from routers.user_recorder_ws import reserve_agent_slot
                    reserve_agent_slot(rec['agent_id'], tk.id)
            return rec, tk, et, ds

    recorder, task, execution_target, dispatch_session = await _pick_and_reserve()

    # No agent available → queue and WAIT for a free agent (the user clicked run;
    # they want it to execute as soon as an agent is available, not fail). The
    # queue processor dispatches it the moment capacity frees. (The earlier
    # ~300s "hang" was a stale-registry ghost being picked, fixed separately.)
    if not recorder and execution_target == 'local':
        # Local-only but no user recorder — queue it and wait. Route through
        # WorkflowQueue.enqueue so it gets the correct traffic class, plan-based
        # priority, and an expiry (an inline task with NULL queue_expires_at would
        # never be dequeued).
        task = await WorkflowQueue.enqueue(
            db, workflow, target_id, trigger_type, trigger_rule_id,
            trigger_context, routing.traffic_type.value, merged_form_data,
        )
        await _place_consumer_hold(task)
        logger.info(f"Workflow '{workflow.name}' queued — waiting for local agent (task_id={task.id})")
        return task

    if recorder:
        # task + dispatch_session were created and the slot RESERVED atomically in
        # _pick_and_reserve() above.

        # --- IP-RELAY: bind a residential exit for a CLOUD-FLEET run (Phase 6) ---
        # Residential egress is orthogonal to the isolation tier; only the cloud
        # venue (role 'infrastructure') + the buyer's request are required. Shared
        # with the queue processor via bind_relay_exit_for_task (one source of
        # truth). Returns None (run egresses directly, never strands) unless an exit
        # is reserved + its broker run-auth minted + the binding stamped onto the
        # task. _wants_residential was resolved up-front (and steered the run to the
        # cloud venue so it could land on role 'infrastructure').
        relay_proxy_cfg = await bind_relay_exit_for_task(
            db,
            task=task,
            workflow=workflow,
            trigger_context=trigger_context,
            executor_role=recorder.get('role'),
            wants_residential=_wants_residential,
        )

        # Place the consumer billing HOLD (background installed runs) BEFORE the
        # agent dispatch fires — a paid recipe must never start unbilled. On an
        # insufficient-balance race _place_consumer_hold marks the task failed; we
        # then skip the agent dispatch entirely.
        await _place_consumer_hold(task)
        if task.status == "failed":
            # Self-host: no consumer/marketplace billing hold and no relay broker
            # (bind_relay_exit_for_task always returns None), so there is nothing to
            # settle/release here. This branch is effectively unreachable.
            return task

        # Fire workflow_started trigger
        try:
            from services.unified_trigger_service import get_unified_trigger_service
            trigger_service = get_unified_trigger_service(db)
            await trigger_service.process_workflow_event(
                event_type="workflow_started",
                workflow_id=workflow.id,
                workflow_name=workflow.name,
                task_id=task.id,
                target_id=target_id,
                status="running",
            )
        except Exception as e:
            logger.error(f"Failed to dispatch workflow_started triggers: {e}")

        if recorder.get('via') == 'websocket':
            # WS-connected recorder (user-hosted or gateway/cloud fleet): push via
            # the shared WS dispatch path (also used by the queue processor).
            # wants_residential = a residential-intent run (premium SKU, creator-IP
            # path by design). Recomputed from the contract definition directly (NOT
            # the relay try-block local) so the money-safe BYO precedence never
            # depends on whether the relay pick raised. BYO must never substitute.
            _wants_residential_for_msg = (
                ((trigger_context or {}).get("_marketplace") or {}).get("sku") == "premium"
            )
            _msg = build_execute_workflow_msg(
                task_id=task.id, workflow=workflow, form_data=merged_form_data,
                session_state=dispatch_session, persona_cfg=persona_cfg,
                trigger_context=trigger_context,
                executor_role=recorder.get('role'),
                relay_proxy=relay_proxy_cfg,
                byo_proxy=byo_proxy_cfg,
                wants_residential=_wants_residential_for_msg,
                # Run-level files map resolved above (§4.1); the agent fetches each
                # file's bytes from its single-object short-TTL signed URL.
                files=getattr(workflow, "_run_files_map", None),
            )
            asyncio.create_task(dispatch_ws_workflow_task(
                task_id=task.id,
                agent_id=recorder['agent_id'],
                message=_msg,
                trigger_context=trigger_context,
            ))
        else:
            # SaaS recorder: push via HTTP in background task
            asyncio.create_task(
                _push_workflow_to_recorder(
                    task_id=task.id,
                    workflow=workflow,
                    form_data=merged_form_data,
                    recorder_url=recorder['recorder_url'],
                    db_url=str(settings.database_url),
                    trigger_context=trigger_context,
                    session_state=dispatch_session,
                    persona=persona_cfg,
                )
            )
        logger.info(
            f"Workflow '{workflow.name}' pushed to recorder {recorder['agent_id']} "
            f"(task_id={task.id}, via={recorder.get('via', 'http')}, traffic={routing.traffic_type.value})"
        )
    else:
        # No recorder capacity — queue for later dispatch
        task = await WorkflowQueue.enqueue(
            db, workflow, target_id, trigger_type, trigger_rule_id,
            trigger_context, routing.traffic_type.value, merged_form_data,
        )
        await _place_consumer_hold(task)
        logger.info(
            f"Workflow '{workflow.name}' queued (task_id={task.id}, "
            f"traffic={routing.traffic_type.value})"
        )

    return task


@router.post("/dispatch", response_model=DispatchResponse)
async def dispatch_workflow(
    request: DispatchRequest,
    db: AsyncSession = Depends(get_db),
    _api_key: str = Depends(get_current_api_key),
    _gate=Depends(require_feature("workflows")),
):
    """
    Dispatch a workflow for execution.

    Pushes to an available recorder for immediate execution.
    Falls back to desktop agent queue if no recorder available.
    """
    # API-key scope: dispatch == run, and "read" is the baseline run verb on a
    # workflow id (mirrors run_workflow / key_can_run_workflow). JWT/OAuth pass.
    if isinstance(_api_key, dict):
        check_api_key_scope(_api_key, "workflows", "read", request.workflow_id)
    workflow_result = await db.execute(
        select(AutomationWorkflow).where(
            AutomationWorkflow.id == request.workflow_id,
        )
    )
    workflow = workflow_result.scalar_one_or_none()
    if not workflow:
        raise HTTPException(status_code=404, detail=f"Workflow {request.workflow_id} not found")

    # FILE ASSETS (§4.5): fold the request `files` map { slot: file_id } so the
    # dispatch choke point resolves it.
    _dw_ctx = None
    if isinstance(request.files, dict):
        _dw_files = {
            str(k): str(v) for k, v in request.files.items()
            if isinstance(v, str) and v
        }
        if _dw_files:
            _dw_ctx = {"files": _dw_files}

    task = await _dispatch_to_recorder_or_queue(
        db=db, workflow=workflow,
        target_id=request.target_id or 0,
        trigger_type="manual",
        trigger_context=_dw_ctx,
        form_data=request.form_data,
        persona_id=request.persona_id,
    )

    # Self-host: no cloud metering, so no per-run execution count.
    _queued = task.status != "running" or not task.executor_agent_id

    await db.commit()
    return DispatchResponse(
        task_id=task.id,
        status=task.status,
        message=(
            f"Workflow '{workflow.name}' is running."
            if not _queued
            else f"No agent available right now — '{workflow.name}' is queued and will run when capacity frees up."
        ),
    )


# ── Delivery: how long is the caller willing to wait? ────────────────────────────
# The run endpoint was fire-and-forget only, so every script had to hand-roll the same
# poll loop against /tasks/{id}/results. `?wait=true` mirrors the cloud gateway's
# contract exactly, including answering 504 (never a silent error) with the task id
# still valid so a slow run is collected instead of re-run.
_WAIT_DEFAULT_SECS = 120
_WAIT_MAX_SECS = 3600
_WAIT_POLL_SECS = 2
_TERMINAL_TASK_STATUSES = ("success", "failed", "timeout", "cancelled")


def _wants_wait(request: Request) -> bool:
    return str(request.query_params.get("wait", "")).strip().lower() in ("1", "true", "yes")


def _wait_budget(request: Request) -> int:
    """Seconds the caller may block for, clamped so a wedged run cannot pin a worker."""
    raw = str(request.query_params.get("timeout", "")).strip()
    if not raw.isdigit():
        return _WAIT_DEFAULT_SECS
    return max(1, min(_WAIT_MAX_SECS, int(raw)))


async def _await_task_result(db, task_id: int, budget_secs: int, accepted: dict):
    """Poll a dispatched task to a terminal state, then answer with its own result.

    Polls the persisted row rather than subscribing to anything: the row IS the
    authoritative terminal record, and a run that finished between dispatch and here
    would never emit another event to subscribe to.
    """
    import asyncio

    deadline = asyncio.get_event_loop().time() + budget_secs
    while True:
        row = (await db.execute(
            select(AutomationTask).where(AutomationTask.id == task_id)
        )).scalar_one_or_none()
        if row is not None and row.status in _TERMINAL_TASK_STATUSES:
            out = {
                "task_id": task_id,
                "status": row.status,
                "done": True,
                "execution_target": accepted.get("execution_target"),
            }
            if row.result_data:
                out["data"] = row.result_data
            if row.error_message:
                out["error"] = row.error_message
            # 200 even for a FAILED run: the REPORT succeeded. Failing the HTTP call
            # would leave a caller unable to distinguish that from a rejected request.
            return out
        if asyncio.get_event_loop().time() >= deadline:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=504,
                content={
                    **accepted,
                    "done": False,
                    "error": (
                        f"The run did not finish within {budget_secs}s and is still in "
                        f"progress. It was NOT cancelled — calling this again would start "
                        f"a SECOND run. Collect it at the results URL instead."
                    ),
                    "results_url": f"/api/automation/tasks/{task_id}/results",
                },
            )
        # Drop the row from the identity map so the next read sees the worker's commit.
        db.expire_all()
        await asyncio.sleep(_WAIT_POLL_SECS)


@router.post("/workflows/{workflow_id}/run")
async def run_workflow(
    workflow_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
    _gate=Depends(require_feature("workflows")),
):
    """
    Run a workflow immediately (JWT-authed, for the dashboard UI).

    DELIVERY is the caller's choice, mirroring the Writ cloud gateway and the desktop
    agent so one mental model covers every way a workflow is invoked:

    * default — fire-and-forget: returns ``{task_id, status}`` right away. Right for the
      dashboard, which shows progress itself.
    * ``?wait=true`` — block until the run reaches a terminal state and return its
      result inline. Right for a script, which just wants the answer and otherwise has
      to hand-roll a poll loop against ``/api/automation/tasks/{id}/results``.

    A run that FAILS is reported with ``200`` and ``status: "failed"`` — the call
    succeeded in reporting the outcome, and a caller must be able to tell that apart
    from a rejected request. Exceeding ``timeout`` answers ``504`` with the ``task_id``
    still valid: the run keeps going, so it is collected rather than started again.

    Optionally override execution_target to choose local agent vs cloud.
    """
    # Consumer-facing wait clock starts the instant the call enters the platform
    # (NOT created_at = queue-insert). Reconstructed at completion via the value
    # stamped into trigger_context._latency below (run is fire-and-forget).
    _received = datetime.now(timezone.utc)

    # API-key scope: allow if the key can run this workflow directly OR via an
    # automation it is scoped to (run-only cascade), OR if it is a public
    # marketplace workflow with an enabled listing. JWT/OAuth pass through.
    if auth.auth_method == "api_key":
        from services.api_key_scope_resolver import key_can_run_workflow, enforce_key_run_limits
        if not await key_can_run_workflow(db, auth.api_key, workflow_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="API key cannot run this workflow",
            )
        await enforce_key_run_limits(db, auth.api_key)

    # Self-host: single-user coordinator has no cloud plan tiers, so runs are
    # neither metered nor plan-gated. Concurrency is governed by the local Runtime
    # governor.
    workflow_result = await db.execute(
        select(AutomationWorkflow).where(
            AutomationWorkflow.id == workflow_id,
        )
    )
    workflow = workflow_result.scalar_one_or_none()

    marketplace_ctx = None
    install = None
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    # A crawl's dataset row is not runnable: its single crawl_batch step expects a
    # per-shard URL batch in trigger_context that only the orchestrator mints. Start
    # a crawl from /crawls instead.
    _reject_crawl_dataset(workflow, workflow_id)

    # Parse optional body (tolerant of empty or missing)
    try:
        body = await request.json()
    except Exception:
        body = {}
    override_target = body.get("execution_target") if isinstance(body, dict) else None
    if override_target and override_target in ("auto", "local", "cloud"):
        # Temporarily override on the workflow object for dispatch routing
        workflow._runtime_execution_target = override_target

    # AGENT PIN: pick a SPECIFIC own agent to run on. The pinned agent must be one
    # of the OWNER's connected user-hosted agents. Pinning forces a LOCAL venue;
    # the dispatch layer still enforces capacity authoritatively and falls through
    # if the pinned agent is full/offline.
    _pin = body.get("agent_id") if isinstance(body, dict) else None
    if _pin:
        from models.agent import Agent as _AgentM, AgentStatus as _ASM
        _own = (await db.execute(
            select(_AgentM.agent_id).where(
                _AgentM.agent_id == str(_pin),
                _AgentM.user_hosted.is_(True),
                _AgentM.status == _ASM.ACTIVE,
            )
        )).scalar_one_or_none()
        if _own is None:
            raise HTTPException(
                status_code=422,
                detail={"code": "agent_not_available",
                        "message": "That agent isn't one of your connected agents."},
            )
        workflow._buyer_pin_agent_id = str(_pin)
        workflow._runtime_execution_target = "local"

    # Pass form_data from body override or workflow's stored form_data
    form_data = body.get("form_data") if isinstance(body, dict) else None
    if not form_data:
        form_data = workflow.form_data

    # FILE ASSETS (§4.5): optional `files` map { slot_or_input_key: file_id } in the
    # run body. Each file_id is resolved + OWNERSHIP-CHECKED against the CALLER
    # inside _dispatch_to_recorder_or_queue (resolve_for_run fail-closes 404
    # on a non-owned id — a caller can never pass another owner's file). Carried via
    # trigger_context so the single dispatch choke point folds it into the run files
    # map and binds slots to upload steps. Validated/normalized to {str: str} here.
    _run_files_req = None
    if isinstance(body, dict) and isinstance(body.get("files"), dict):
        _run_files_req = {
            str(k): str(v) for k, v in body["files"].items()
            if isinstance(v, str) and v
        } or None

    # Optional run-as persona override (else falls back to workflow.default_persona_id).
    persona_id = body.get("persona_id") if isinstance(body, dict) else None

    trigger_context = None

    # FILE ASSETS (§4.5): fold the run-body `files` map into trigger_context so the
    # single dispatch choke point resolves it and binds slots to upload steps.
    if _run_files_req:
        if trigger_context is None:
            trigger_context = {}
        trigger_context["files"] = _run_files_req

    # RESIDENTIAL PROXY (own run): opt this run into a residential exit from the
    # relay pool. Seed the flag so the dispatch chokepoint requests an exit.
    if isinstance(body, dict) and body.get("use_residential_proxy"):
        if trigger_context is None:
            trigger_context = {}
        trigger_context["use_residential_proxy"] = True

    task = await _dispatch_to_recorder_or_queue(
        db=db, workflow=workflow,
        target_id=None,
        trigger_type="manual",
        trigger_context=trigger_context,
        form_data=form_data,
        persona_id=persona_id,
        _interactive=True,
    )

    # Attribute the run to the initiating API key.
    if auth.auth_method == "api_key" and auth.api_key is not None:
        task.api_key_id = auth.api_key_id
        auth.api_key.runs_used = (auth.api_key.runs_used or 0) + 1

    await db.commit()

    # Report the ACTUAL outcome. A run with no assigned executor is QUEUED (no
    # agent was free) — say so plainly instead of falsely claiming it dispatched
    # to cloud, so the user knows it's waiting for capacity rather than broken.
    queued = task.status in ("queued", "pending", "assigned") or not task.executor_agent_id
    if not queued and task.status == "running":
        ran_on = "your local agent" if "user-rec" in (task.executor_agent_id or "") else "cloud"
        message = f"Workflow '{workflow.name}' is running on {ran_on}."
    elif queued:
        message = (
            f"No agent is available right now — '{workflow.name}' is queued and will "
            f"run automatically as soon as an agent (cloud or your local agent) is free."
        )
    else:
        message = f"Workflow '{workflow.name}' dispatched."

    accepted = {
        "task_id": task.id,
        "status": task.status,
        "queued": queued,
        "message": message,
        "execution_target": override_target or "auto",
    }

    if not _wants_wait(request):
        return accepted

    return await _await_task_result(db, task.id, _wait_budget(request), accepted)


@router.get("/tasks/{task_id}/results")
async def get_task_results(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    _api_key: str = Depends(get_current_api_key),
):
    """
    Get task execution results including extracted data.

    Returns the full result_data JSONB field which contains:
    - steps_completed: number of workflow steps executed
    - extracted_data: data extracted by 'extract' steps (keyed by output_name)
    - Any other result metadata

    Useful for webhooks/integrations that dispatch a workflow and poll for results.
    """
    # Scope check: the key must be allowed to read tasks/workflows.
    from security.dependencies import check_api_key_scope
    if isinstance(_api_key, dict):
        check_api_key_scope(_api_key, "workflows", "read")

    result = await db.execute(
        select(AutomationTask).where(AutomationTask.id == task_id)
    )
    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    safe_result_data = redact_result_data(task.result_data)
    return {
        "task_id": task.id,
        "status": task.status,
        "success": task.success,
        "result_data": safe_result_data,
        "extracted_data": (safe_result_data or {}).get("extracted_data"),
        # FILE ASSETS (§4.5): captured output files {file_id, filename, content_type,
        # size, output_key} — the caller fetches the bytes via /v1/files/{id}/content.
        "output_files": (safe_result_data or {}).get("output_files") or [],
        "error": redact_infra(task.error_message),
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "duration_ms": task.duration_ms,
        "executor_agent_id": task.executor_agent_id,
    }


# ---------------------------------------------------------------------------
# Extracted-data table — the data a workflow's runs produced, aggregated into
# one sortable/searchable grid (per-workflow Data tab + global Data explorer).
# Every run already persists what it extracted in result_data.extracted_data;
# these endpoints flatten that across runs (see services/extracted_data_table).
# ---------------------------------------------------------------------------

# How many recent runs we scan when building the table. Bounded so a workflow
# with a huge history stays responsive; the response flags `truncated` when the
# scan hits this ceiling so the UI can say "showing the latest N runs".
_DATA_SCAN_CAP = 1000


def _safe_export_filename(name: str) -> str:
    import re
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip()).strip("-")
    return (base or "workflow")[:60]


async def _load_workflow_for_data(db: AsyncSession, workflow_id: int, _api_key: dict):
    """Load the workflow whose data is requested. 404 if it doesn't exist."""
    res = await db.execute(
        select(AutomationWorkflow).where(AutomationWorkflow.id == workflow_id)
    )
    wf = res.scalar_one_or_none()
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return wf


async def _scan_workflow_data_tasks(db: AsyncSession, workflow_id: int, *, workflow=None):
    """Most-recent runs of a workflow that produced a real extracted_data value,
    bounded by _DATA_SCAN_CAP. Returns (tasks, truncated). No row locks needed
    for DELETE's in-transaction uid resolution — the coordinator is a
    single-writer SQLite deployment.

    SUBSYSTEM SCOPING. Crawl pages and workflow runs share automation_tasks, and
    ``automation_workflows.id`` is a bare SQLite rowid alias — deleting a crawl frees
    its id for the NEXT workflow created. Matching on workflow_id alone therefore
    let a freshly recorded workflow serve a dead crawl's pages as its own dataset.
    Pass ``workflow`` so the scan is pinned to the right side: a crawl dataset reads
    only shard runs, a workflow reads only its own. (Omitting it keeps the legacy
    id-only behaviour for callers that have no workflow row in hand.)"""
    recency = func.coalesce(AutomationTask.completed_at, AutomationTask.created_at)
    q = (
        select(AutomationTask)
        .where(AutomationTask.workflow_id == workflow_id)
        .where(func.json_extract(AutomationTask.result_data, "$.extracted_data").isnot(None))
        .order_by(recency.desc())
        .limit(_DATA_SCAN_CAP)
    )
    if workflow is not None:
        # trigger_type is NOT NULL (defaults to on_change), so a plain != is exact.
        q = q.where(
            AutomationTask.trigger_type == CRAWL_TRIGGER_TYPE
            if _is_crawl_dataset(workflow)
            else AutomationTask.trigger_type != CRAWL_TRIGGER_TYPE
        )
    res = await db.execute(q)
    tasks = list(res.scalars().all())
    return tasks, len(tasks) >= _DATA_SCAN_CAP


def _validate_data_lens_params(view: str, run_id, collection) -> str:
    """Shared 400-validation for the lens params on GET data / facets / export.
    Messages are spec-pinned (identical across all three engines)."""
    view = (view or "all").strip().lower()
    if view not in ("all", "latest", "run"):
        raise HTTPException(status_code=400, detail="view must be one of: latest, run, all")
    if view != "all" and collection:
        raise HTTPException(status_code=400, detail="change tracking operates on top-level records")
    if view == "run" and run_id is None:
        raise HTTPException(status_code=400, detail="run_id is required when view=run")
    return view


def _parse_col_filters(filters: Optional[List[str]]) -> dict:
    """Parse repeated ``filter=column:substr`` query params into a dict. Splits
    on the FIRST colon so values may themselves contain colons."""
    out: dict = {}
    for raw in filters or []:
        if not isinstance(raw, str) or ":" not in raw:
            continue
        col, sub = raw.split(":", 1)
        col = col.strip()
        if col:
            out[col] = sub
    return out


def _parse_structured_filters(filters_json: Optional[str]) -> list:
    """Parse the structured smart-filter param: a JSON array of clauses like
    [{"col":"price","op":"between","min":10,"max":50},
     {"col":"city","op":"in","values":["Paris","Lyon"]}]. Returns [] on any
    problem (never raises) — a malformed filter must not 500 the data view."""
    if not filters_json:
        return []
    try:
        parsed = json.loads(filters_json)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [c for c in parsed if isinstance(c, dict) and c.get("col") and c.get("op")]


@router.get("/workflows/{workflow_id}/data/facets")
async def get_workflow_data_facets(
    workflow_id: int,
    include_inputs: bool = Query(False, description="Also surface run input values as input.<name> columns"),
    collection: Optional[str] = Query(None, description="Describe the columns of a nested array path (e.g. posts.items) instead"),
    view: str = Query("all", description="Lens to facet over: latest (deduped records), run (one snapshot), all (flat grid, default)"),
    run_id: Optional[int] = Query(None, description="Chain run to facet when view=run"),
    key: Optional[str] = Query(None, description="Comma-separated identity fields for the lineage lenses"),
    include_missing: bool = Query(False, description="view=latest: include records absent from the newest snapshot"),
    source: Optional[str] = Query(None, description="view=latest/run: facet only records from this originating list key ('' = untagged)"),
    db: AsyncSession = Depends(get_db),
    _api_key: dict = Depends(get_current_api_key),
):
    """Per-column facets over the workflow's present extracted data — inferred
    type, non-empty count, numeric min/max, and the distinct value set when low
    cardinality. Drives the grid's smart, data-aware column filters.
    Pass ``collection`` to describe a nested array's columns instead; pass
    ``view=latest``/``view=run`` to facet over exactly that lens's rowset."""
    from services import extracted_data_table as edt
    if isinstance(_api_key, dict):
        check_api_key_scope(_api_key, "workflows", "read", workflow_id)
    view = _validate_data_lens_params(view, run_id, collection)
    wf = await _load_workflow_for_data(db, workflow_id, _api_key)
    tasks, truncated = await _scan_workflow_data_tasks(db, workflow_id, workflow=wf)
    if view == "all":
        columns, rows = edt.flatten(
            tasks,
            declared=_declared_output_fields(wf),
            redactor=redact_result_data,
            include_inputs=include_inputs,
            collection=collection,
        )
        return {
            "workflow_id": wf.id,
            "columns": columns,
            "facets": edt.compute_facets(columns, rows),
            "collections": edt.discover_collections(rows),
            "row_count": len(rows),
            "scanned_runs": len(tasks),
            "truncated": truncated,
        }
    try:
        table = edt.build_lens_table(
            tasks,
            view=view,
            run_id=run_id,
            declared=_declared_output_fields(wf),
            redactor=redact_result_data,
            key=key,
            include_missing=include_missing,
            source=source,
            offset=0,
            limit=None,
        )
    except edt.LineageRunNotFound:
        raise HTTPException(status_code=404, detail="Run not found in this workflow's data history")
    return {
        "workflow_id": wf.id,
        "columns": table["columns"],
        "facets": edt.compute_facets(table["columns"], table["rows"]),
        "collections": table["collections"],
        "identity": table["identity"],
        "sources": table["sources"],
        "row_count": table["total"],
        "scanned_runs": len(tasks),
        "truncated": truncated,
    }


@router.get("/workflows/{workflow_id}/data")
async def get_workflow_data(
    workflow_id: int,
    q: Optional[str] = Query(None, description="Substring filter across extracted fields"),
    filter: Optional[List[str]] = Query(None, description="Per-column filter as 'column:substring' (repeatable, legacy)"),
    filters: Optional[str] = Query(None, description="Structured smart filters: JSON array of clauses"),
    sort_by: Optional[str] = Query(None, description="A data column or run_at/status/duration_ms (lenses also: first_seen_at/last_seen_at/versions)"),
    sort_dir: str = Query("desc", description="asc or desc"),
    format: Optional[str] = Query(None, description=dataset_formats.format_help("json")),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    include_inputs: bool = Query(False, description="Also surface run input values as input.<name> columns"),
    collection: Optional[str] = Query(None, description="Pivot to a nested array path (e.g. posts.items), one row per item; a numeric segment (posts.items.2) addresses one item"),
    view: str = Query("all", description="Lens: latest (deduped current records), run (one snapshot vs its predecessor), all (flat grid, default)"),
    run_id: Optional[int] = Query(None, description="Chain run to inspect when view=run"),
    key: Optional[str] = Query(None, description="Comma-separated identity fields for the lineage lenses"),
    include_missing: bool = Query(False, description="view=latest: include records absent from the newest snapshot"),
    source: Optional[str] = Query(None, description="view=latest/run: only records from this originating list key ('' = untagged); the response's sources counts stay unfiltered"),
    db: AsyncSession = Depends(get_db),
    _api_key: dict = Depends(get_current_api_key),
):
    """Aggregate every run's extracted_data for a workflow into one
    sortable/searchable table (columns + paginated rows).

    A run that extracted a LIST of records contributes one row per record; a run
    that extracted a single record contributes one row. Each row carries the run
    it came from (run_id/run_at/status) so values trace back to their run.

    Pass ``collection`` (a dot-path like ``posts.items``) to pivot the table to a
    nested array — one row per nested item, columns inferred from the items. The
    response always lists reachable nested paths under ``collections``.

    Lineage lenses: ``view=latest`` dedups to one row per unique record (rows
    gain ``_lineage``; response gains ``identity`` + ``counts``); ``view=run``
    shows one snapshot annotated vs its predecessor (response gains ``identity``,
    ``delta``, ``removed_records``, ``prev_run_id``). Both lenses echo a
    ``sources`` map (originating list key -> record count, "" = untagged) and
    accept ``source`` to filter to one list key before search/filters/pagination.
    ``collection`` composes only with ``view=all`` — change tracking operates on
    top-level records.

    ``format`` (``json`` default, or ``csv``/``markdown``/``html``) serves this
    same page as that format instead of the JSON envelope. markdown/html are
    content-aware: a document-shaped dataset (crawl pages) renders as documents,
    structured data as a table.
    """
    from services import extracted_data_table as edt
    if isinstance(_api_key, dict):
        check_api_key_scope(_api_key, "workflows", "read", workflow_id)
    fmt = dataset_formats.norm_format(format, default="json")
    view = _validate_data_lens_params(view, run_id, collection)
    wf = await _load_workflow_for_data(db, workflow_id, _api_key)
    tasks, truncated = await _scan_workflow_data_tasks(db, workflow_id, workflow=wf)
    if view == "all":
        table = edt.build_table(
            tasks,
            declared=_declared_output_fields(wf),
            q=q,
            col_filters=_parse_col_filters(filter),
            filters=_parse_structured_filters(filters),
            sort_by=sort_by,
            sort_dir=sort_dir,
            offset=offset,
            limit=limit,
            redactor=redact_result_data,
            include_inputs=include_inputs,
            collection=collection,
        )
    else:
        try:
            table = edt.build_lens_table(
                tasks,
                view=view,
                run_id=run_id,
                declared=_declared_output_fields(wf),
                redactor=redact_result_data,
                key=key,
                include_missing=include_missing,
                source=source,
                q=q,
                col_filters=_parse_col_filters(filter),
                filters=_parse_structured_filters(filters),
                sort_by=sort_by,
                sort_dir=sort_dir,
                offset=offset,
                limit=limit,
            )
        except edt.LineageRunNotFound:
            raise HTTPException(status_code=404, detail="Run not found in this workflow's data history")
    # `?format=` renders THIS page of the table directly instead of the JSON
    # envelope — same rows, same order, so a caller can page through csv/markdown/
    # html exactly as it pages through json. The envelope-only fields (pagination,
    # counts, identity) have no place in a flat render, which is why the render is
    # a replacement rather than an extra key.
    if fmt != "json":
        return dataset_formats.render_dataset(
            fmt,
            table["columns"],
            table["rows"],
            title=wf.name or f"Workflow {wf.id}",
            lineage=view != "all",
        )
    return {
        "workflow_id": wf.id,
        "workflow_name": wf.name,
        **table,
        "scanned_runs": len(tasks),
        "truncated": truncated,
        "limit": limit,
        "offset": offset,
    }


@router.get("/workflows/{workflow_id}/data/export")
async def export_workflow_data(
    workflow_id: int,
    format: str = Query("csv", description=dataset_formats.format_help("csv")),
    q: Optional[str] = Query(None),
    filter: Optional[List[str]] = Query(None, description="Per-column filter as 'column:substring' (repeatable, legacy)"),
    filters: Optional[str] = Query(None, description="Structured smart filters: JSON array of clauses"),
    sort_by: Optional[str] = Query(None),
    sort_dir: str = Query("desc"),
    include_inputs: bool = Query(False, description="Also surface run input values as input.<name> columns"),
    collection: Optional[str] = Query(None, description="Pivot to a nested array path (e.g. posts.items) before exporting"),
    view: str = Query("all", description="Lens to export: latest (deduped records), run (one snapshot), all (flat grid, default)"),
    run_id: Optional[int] = Query(None, description="Chain run to export when view=run"),
    key: Optional[str] = Query(None, description="Comma-separated identity fields for the lineage lenses"),
    include_missing: bool = Query(False, description="view=latest: include records absent from the newest snapshot"),
    db: AsyncSession = Depends(get_db),
    _api_key: dict = Depends(get_current_api_key),
):
    """Download the full (search/sort/filter applied, un-paginated) extracted-data
    table as ``csv`` (default), ``json``, ``markdown`` or ``html``. Bounded by the
    scan cap. Pass ``collection`` to export a nested array (e.g. ``posts.items``)
    instead. ``view=latest``/``view=run`` exports carry lineage columns (CSV:
    appended after the run-meta columns; JSON: a ``_lineage`` key per row).
    markdown/html are content-aware — a document-shaped dataset (crawl pages)
    renders as documents rather than a table."""
    from services import extracted_data_table as edt
    fmt = dataset_formats.norm_format(format, default="csv")
    if isinstance(_api_key, dict):
        check_api_key_scope(_api_key, "workflows", "read", workflow_id)
    view = _validate_data_lens_params(view, run_id, collection)
    wf = await _load_workflow_for_data(db, workflow_id, _api_key)
    tasks, _ = await _scan_workflow_data_tasks(db, workflow_id, workflow=wf)
    if view == "all":
        table = edt.build_table(
            tasks,
            declared=_declared_output_fields(wf),
            q=q,
            col_filters=_parse_col_filters(filter),
            filters=_parse_structured_filters(filters),
            sort_by=sort_by,
            sort_dir=sort_dir,
            offset=0,
            limit=None,
            redactor=redact_result_data,
            include_inputs=include_inputs,
            collection=collection,
        )
    else:
        try:
            table = edt.build_lens_table(
                tasks,
                view=view,
                run_id=run_id,
                declared=_declared_output_fields(wf),
                redactor=redact_result_data,
                key=key,
                include_missing=include_missing,
                q=q,
                col_filters=_parse_col_filters(filter),
                filters=_parse_structured_filters(filters),
                sort_by=sort_by,
                sort_dir=sort_dir,
                offset=0,
                limit=None,
            )
        except edt.LineageRunNotFound:
            raise HTTPException(status_code=404, detail="Run not found in this workflow's data history")
    lineage = view != "all"
    fname = _safe_export_filename(wf.name or f"workflow-{wf.id}")
    return dataset_formats.render_dataset(
        fmt,
        table["columns"],
        table["rows"],
        title=wf.name or f"Workflow {wf.id}",
        lineage=lineage,
        filename=f"{fname}-data",
    )


@router.get("/workflows/{workflow_id}/data/runs")
async def get_workflow_data_runs(
    workflow_id: int,
    key: Optional[str] = Query(None, description="Comma-separated identity fields for the lineage pass"),
    db: AsyncSession = Depends(get_db),
    _api_key: dict = Depends(get_current_api_key),
):
    """Snapshot index for the By-date lens: the workflow's successful
    data-bearing runs (newest first) with per-run record counts and vs-previous
    deltas (delta is null for the oldest chain member; explicit-empty runs are
    real snapshots that removed everything)."""
    from services import extracted_data_table as edt
    if isinstance(_api_key, dict):
        check_api_key_scope(_api_key, "workflows", "read")
    wf = await _load_workflow_for_data(db, workflow_id, _api_key)
    tasks, truncated = await _scan_workflow_data_tasks(db, workflow_id, workflow=wf)
    index = edt.build_runs_index(
        tasks, declared=_declared_output_fields(wf), redactor=redact_result_data, key=key
    )
    return {
        "workflow_id": wf.id,
        **index,
        "scanned_runs": len(tasks),
        "truncated": truncated,
    }


@router.get("/workflows/{workflow_id}/data/records/{record_uid}/history")
async def get_workflow_record_history(
    workflow_id: int,
    record_uid: str,
    key: Optional[str] = Query(None, description="Comma-separated identity fields for the lineage pass"),
    db: AsyncSession = Depends(get_db),
    _api_key: dict = Depends(get_current_api_key),
):
    """One record's CHANGE-POINT version history — its first appearance plus
    each appearance where content changed (identical re-extractions collapse).
    Unknown uid → 404 whose body includes the CURRENT identity so the FE can
    re-key and refetch."""
    from fastapi.responses import JSONResponse
    from services import extracted_data_table as edt
    if isinstance(_api_key, dict):
        check_api_key_scope(_api_key, "workflows", "read")
    wf = await _load_workflow_for_data(db, workflow_id, _api_key)
    tasks, truncated = await _scan_workflow_data_tasks(db, workflow_id, workflow=wf)
    history, identity = edt.build_record_history(
        tasks, record_uid, declared=_declared_output_fields(wf), redactor=redact_result_data, key=key
    )
    if history is None:
        return JSONResponse(
            status_code=404,
            content={"detail": "Record not found", "identity": identity},
        )
    return {
        "workflow_id": wf.id,
        **history,
        "scanned_runs": len(tasks),
        "truncated": truncated,
    }


@router.get("/data/workflows")
async def list_data_workflows(
    db: AsyncSession = Depends(get_db),
    _api_key: dict = Depends(get_current_api_key),
):
    """Workflows that have produced extracted data, each with a run count +
    last-data timestamp — drives the global Data explorer's picker.

    Only lists a workflow when its runs actually FLATTEN to >=1 data row: an empty
    ``{}`` / meta-only / AI-chat ``extracted_data`` passes the cheap non-null SQL
    check but yields zero rows, which would otherwise pad the picker with "N runs
    -> No extracted data yet" entries. run_count = DISTINCT runs that produced a
    row."""
    from services import extracted_data_table as edt
    if isinstance(_api_key, dict):
        check_api_key_scope(_api_key, "workflows", "read")
    # Cheap candidate set: workflows with >=1 non-null extracted_data run.
    res = await db.execute(
        select(AutomationTask.workflow_id)
        .where(AutomationTask.workflow_id.isnot(None))
        .where(func.json_extract(AutomationTask.result_data, "$.extracted_data").isnot(None))
        .group_by(AutomationTask.workflow_id)
    )
    candidate_ids = [r.workflow_id for r in res.all()]
    if not candidate_ids:
        return {"workflows": []}
    wf_res = await db.execute(
        select(AutomationWorkflow)
        .where(AutomationWorkflow.id.in_(candidate_ids))
    )
    out = []
    for w in wf_res.scalars().all():
        # Materialize the SAME per-run entries the picker's table opens to; skip
        # workflows whose runs all flatten to zero rows (the "empty runs" the
        # picker must hide). The entries also feed last_delta — no second scan.
        tasks, _ = await _scan_workflow_data_tasks(db, w.id, workflow=w)
        if not tasks:
            continue
        declared_w = _declared_output_fields(w)
        entries = edt.run_entries(tasks, declared=declared_w, redactor=redact_result_data)
        bearing = [e for e in entries if e["records"]]
        if not bearing:
            continue
        run_ids = {e["run_id"] for e in bearing if e["run_id"] is not None}
        last_data_at = max((e["run_at"] for e in bearing if e["run_at"]), default=None)
        out.append({
            "workflow_id": w.id,
            "workflow_name": w.name,
            # Lets the Data explorer lock a crawl dataset to the aggregated view
            # (its shards are one dataset, not temporal snapshots).
            "workflow_type": w.workflow_type,
            "run_count": len(run_ids),
            "last_data_at": last_data_at,
            "last_delta": edt.picker_last_delta(entries, declared=declared_w),
        })
    # A crawl is not a workflow: the Data explorer links its dataset back to the
    # crawl detail (/crawls/{crawl_id}), not the workflow page. Map each crawl
    # dataset's workflow_id → its crawl id in one query.
    crawl_wf_ids = [d["workflow_id"] for d in out if d.get("workflow_type") == CRAWL_WORKFLOW_TYPE]
    if crawl_wf_ids:
        from models.crawl_job import CrawlJob
        cj_res = await db.execute(
            select(CrawlJob.id, CrawlJob.workflow_id).where(CrawlJob.workflow_id.in_(crawl_wf_ids))
        )
        wf_to_crawl = {wf_id: cid for cid, wf_id in cj_res.all()}
        for d in out:
            cid = wf_to_crawl.get(d["workflow_id"])
            if cid is not None:
                d["crawl_id"] = cid
        # A crawl dataset whose CrawlJob is gone is an ORPHAN — the crawl it belonged
        # to was removed, so there is nothing to open and no owner to attribute the
        # pages to. Hide it rather than listing pages under a dead name; the startup
        # sweep (bootstrap._purge_orphaned_crawl_datasets) clears the rows.
        out = [
            d for d in out
            if d.get("workflow_type") != CRAWL_WORKFLOW_TYPE or d.get("crawl_id") is not None
        ]
    out.sort(key=lambda x: x["last_data_at"] or "", reverse=True)
    return {"workflows": out}


class ExtractedRowRef(BaseModel):
    """One (run_id, record_index) pair identifying an extracted-data row.

    Matches the shape returned by ``GET /workflows/{id}/data``. A row where
    ``record_index == 0`` on a run whose ``extracted_data`` is a single dict
    clears that run's ``extracted_data`` entirely."""
    run_id: int
    record_index: int = 0


class DeleteExtractedRowsRequest(BaseModel):
    records: List[ExtractedRowRef] = Field(default_factory=list)
    # Lineage deletes: each uid deletes ALL its versions across the scanned
    # chain; ``key`` pins the identity the uids were computed under (the FE
    # echoes identity.fields back — auto-pick may otherwise drift between the
    # GET and the DELETE).
    record_uids: List[str] = Field(default_factory=list)
    key: Optional[str] = None
    # Clear EVERY extracted record for this workflow — the Outputs picker's
    # bulk-remove, and what a whole-dataset DELETE resolves to. Ignores
    # ``records``/``record_uids`` when set.
    clear_all: bool = False


@router.delete("/workflows/{workflow_id}/data")
async def delete_workflow_data_rows(
    workflow_id: int,
    request: DeleteExtractedRowsRequest,
    db: AsyncSession = Depends(get_db),
    _api_key: dict = Depends(get_current_api_key),
):
    """Delete extracted-data rows for a workflow, addressed by
    ``(run_id, record_index)`` refs and/or ``record_uids`` (a uid resolves to
    every version of that record across the scanned chain). Index semantics
    mirror the flatten's coercion exactly (see
    ``extracted_data_table.pop_extracted_slots``): stored-slot indices for
    lists/tables, per-list offset arithmetic for multi-list dicts, clear-all
    only for single-record runs.

    uid resolution happens in the SAME transaction as the mutation (the
    coordinator is single-writer SQLite — no row locks needed). Requires the
    ``datasets:delete`` scope on the API key. Returns
    ``{deleted, resolved: {uid: n_versions}, unmatched: [uids]}``."""
    from security.dependencies import check_api_key_scope
    # `datasets:delete`, not `workflows:write`: this removes extracted RECORDS,
    # leaving the workflow itself untouched.
    if isinstance(_api_key, dict):
        check_api_key_scope(_api_key, "datasets", "delete")
    wf = await _load_workflow_for_data(db, workflow_id, _api_key)
    return await delete_dataset_records(db, wf, request)


async def delete_dataset_records(db: AsyncSession, wf, request: "DeleteExtractedRowsRequest") -> dict:
    """Core of the dataset delete, shared by the in-app endpoint above and the
    public dataset endpoints — so the two can never diverge on slot semantics or
    the events they emit. Callers do their own scope check and workflow load.

    Returns ``{deleted, resolved: {uid: n_versions}, unmatched: [uids]}``.
    """
    from services import extracted_data_table as edt

    # Bulk-remove: strip ``extracted_data`` from every visible run, which drops the
    # workflow out of the Outputs picker. No row locks — the coordinator is
    # single-writer SQLite.
    if request.clear_all:
        res = await db.execute(
            select(AutomationTask).where(AutomationTask.workflow_id == wf.id)
        )
        from sqlalchemy.orm.attributes import flag_modified as _flag_modified
        declared = _declared_output_fields(wf)
        deleted = 0
        for task in res.scalars().all():
            rd = task.result_data
            if not isinstance(rd, dict) or "extracted_data" not in rd:
                continue
            deleted += edt.record_count(rd, declared)
            rd = dict(rd)
            rd.pop("extracted_data", None)
            task.result_data = rd
            _flag_modified(task, "result_data")
        await db.commit()
        return {"deleted": deleted, "resolved": {}, "unmatched": []}

    if not request.records and not request.record_uids:
        return {"deleted": 0, "resolved": {}, "unmatched": []}

    # Group by run — one UPDATE per touched run row.
    by_run: dict[int, list[int]] = {}
    for r in request.records:
        by_run.setdefault(int(r.run_id), []).append(int(r.record_index))

    resolved: dict[str, int] = {}
    unmatched: list[str] = []
    if request.record_uids:
        scan_tasks, _truncated = await _scan_workflow_data_tasks(db, wf.id, workflow=wf)
        uid_by_run, resolved, unmatched, _identity = edt.resolve_record_uids(
            scan_tasks,
            request.record_uids,
            declared=_declared_output_fields(wf),
            redactor=redact_result_data,
            key=request.key,
        )
        for rid, idxs in uid_by_run.items():
            by_run.setdefault(rid, []).extend(idxs)

    if not by_run:
        return {"deleted": 0, "resolved": resolved, "unmatched": unmatched}

    res = await db.execute(
        select(AutomationTask)
        .where(AutomationTask.workflow_id == wf.id)
        .where(AutomationTask.id.in_(list(by_run.keys())))
    )
    tasks = list(res.scalars().all())
    if not tasks:
        return {"deleted": 0, "resolved": resolved, "unmatched": unmatched}

    from sqlalchemy.orm.attributes import flag_modified as _flag_modified

    deleted = 0
    for task in tasks:
        idxs = by_run.get(task.id) or []
        if not idxs:
            continue
        rd = task.result_data or {}
        if not isinstance(rd, dict) or "extracted_data" not in rd:
            continue
        n = edt.pop_extracted_slots(rd, idxs)
        if n:
            deleted += n
            task.result_data = rd
            _flag_modified(task, "result_data")

    await db.commit()
    return {"deleted": deleted, "resolved": resolved, "unmatched": unmatched}


@router.post("/dispatch-and-wait")
async def dispatch_workflow_and_wait(
    request: DispatchRequest,
    timeout_seconds: int = Query(default=120, ge=10, le=300),
    db: AsyncSession = Depends(get_db),
    _api_key: str = Depends(get_current_api_key),
    _gate=Depends(require_feature("workflows")),
):
    """
    Dispatch a workflow and wait for completion. Returns extracted data.

    Synchronous endpoint for webhook integrations:
    1. Creates an automation task
    2. Polls until task completes or timeout
    3. Returns full results including extracted_data

    Use this when you need extracted data as a direct HTTP response.
    """
    import asyncio
    from security.dependencies import check_api_key_scope

    # Consumer-facing wait anchor — this path IS synchronous (we poll until the
    # task completes), so it is a true wall-clock measurement, but we still stamp
    # it into trigger_context so completion reconstructs response_latency_ms the
    # same way as the async path (comparable across sync/async runs).
    _received = datetime.now(timezone.utc)

    # Scope check: the key must be allowed to run workflows.
    if isinstance(_api_key, dict):
        check_api_key_scope(_api_key, "workflows", "run")

    # Self-host: single-user coordinator has no cloud plan tiers, so runs are
    # neither metered nor plan-gated.
    workflow_result = await db.execute(
        select(AutomationWorkflow).where(
            AutomationWorkflow.id == request.workflow_id,
        )
    )
    workflow = workflow_result.scalar_one_or_none()

    trigger_context = None
    marketplace_ctx = None
    install = None
    if not workflow:
        raise HTTPException(status_code=404, detail=f"Workflow {request.workflow_id} not found")

    persona_id = request.persona_id

    # FILE ASSETS (§4.5): fold the request `files` map { slot: file_id } into
    # trigger_context so the dispatch choke point resolves it against the caller
    # (fail-closed ownership). Normalized to {str: str}.
    _df_files = None
    if isinstance(request.files, dict):
        _df_files = {
            str(k): str(v) for k, v in request.files.items()
            if isinstance(v, str) and v
        } or None
    if _df_files:
        if trigger_context is None:
            trigger_context = {}
        trigger_context["files"] = _df_files

    # Dispatch to recorder or queue
    task = await _dispatch_to_recorder_or_queue(
        db=db, workflow=workflow,
        target_id=request.target_id or 0,
        trigger_type="manual",
        trigger_context=trigger_context,
        form_data=request.form_data,
        persona_id=persona_id,
        _interactive=True,
    )

    # Attribute the run to the initiating API key.
    if isinstance(_api_key, dict) and _api_key.get("id"):
        task.api_key_id = _api_key.get("id")

    await db.commit()

    task_id = task.id
    logger.info(f"Dispatch-and-wait: workflow '{workflow.name}' (task_id={task_id}, status={task.status}, timeout={timeout_seconds}s)")

    # Poll for completion
    poll_interval = 2
    elapsed = 0

    while elapsed < timeout_seconds:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

        result = await db.execute(
            select(AutomationTask).where(AutomationTask.id == task_id)
        )
        task = result.scalar_one_or_none()

        if task and task.status in ("success", "failed", "cancelled"):
            safe_result_data = redact_result_data(task.result_data)
            return {
                "task_id": task.id,
                "status": task.status,
                "success": task.success,
                "result_data": safe_result_data,
                "extracted_data": (safe_result_data or {}).get("extracted_data"),
                # FILE ASSETS (§4.5): captured output files for the caller to fetch.
                "output_files": (safe_result_data or {}).get("output_files") or [],
                "error": redact_infra(task.error_message),
                "duration_ms": task.duration_ms,
            }

    # Timeout. The task is still 'running' and its hold would leak — transition it
    # to a terminal failed state via _process_task_completion (which RELEASEs the
    # hold and charges nothing). Use a fresh session so it commits independently of
    # this request's transaction. Guarded — a timeout response is returned either way.
    try:
        from database import AsyncSessionLocal
        async with AsyncSessionLocal() as to_db:
            r = await to_db.execute(
                select(AutomationTask).where(AutomationTask.id == task_id)
            )
            t = r.scalar_one_or_none()
            if t is not None and t.status in ("running", "pending", "assigned", "queued"):
                await _process_task_completion(
                    db=to_db, task=t,
                    success=False,
                    error=f"Task did not complete within {timeout_seconds} seconds",
                )
                await to_db.commit()
    except Exception as to_e:
        logger.warning(
            "[Marketplace] dispatch-and-wait timeout: failed to terminalize task %s "
            "(hold may be reaped later): %s", task_id, to_e,
        )

    return {
        "task_id": task_id,
        "status": "timeout",
        "success": False,
        "error": f"Task did not complete within {timeout_seconds} seconds",
        "result_data": None,
        "extracted_data": None,
    }


# ============================================================================
# Auth Session Management (Agent-reported)
# ============================================================================

class PreCheckCompleteRequest(BaseModel):
    """Request for pre-check workflow completion with auth session."""
    agent_id: str
    target_id: int
    workflow_id: int
    timestamp: str
    success: bool
    auth_session: Optional[AuthSession] = None
    result_data: Optional[dict] = None
    error: Optional[str] = None
    signature: str


@router.post("/precheck/complete")
async def complete_precheck_workflow(
    request: PreCheckCompleteRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Complete a pre-check workflow with auth session.

    Called by agents after running a pre-check workflow to save the
    extracted auth session (cookies, headers, tokens) to the target.
    Triggers a hot-reload so other agents receive the auth data.

    HMAC signature verification ensures only authorized agents can report.
    """
    # Verify agent exists and is active
    agent_result = await db.execute(
        select(Agent).where(Agent.agent_id == request.agent_id)
    )
    agent = agent_result.scalar_one_or_none()

    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found"
        )

    if agent.status != AgentStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Agent is {agent.status.value}"
        )

    # Verify HMAC signature
    from security.encryption import SecretEncryption
    from security.hmac import verify_fresh_signed_payload

    secret = None
    if agent.encrypted_secret:
        try:
            secret = SecretEncryption.decrypt_secret(agent.encrypted_secret)
        except Exception:
            pass

    if not secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Cannot verify signature - agent secret not available"
        )

    # Verify signature
    payload = {
        'agent_id': request.agent_id,
        'target_id': request.target_id,
        'workflow_id': request.workflow_id,
        'timestamp': request.timestamp,
        'success': request.success,
    }
    if request.auth_session:
        payload['auth_session'] = request.auth_session.model_dump()
    if request.result_data:
        payload['result_data'] = request.result_data

    # Agent-signed ingestion: constant-time HMAC match AND payload timestamp
    # freshness (replay window). Previously the args were SWAPPED
    # (verify_signature(payload, request.signature, secret)) so the secret was
    # compared as the signature — verification could never succeed correctly.
    if not verify_fresh_signed_payload(payload, secret, request.signature):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or stale HMAC signature"
        )

    # Replay dedupe: reject a signature we've already accepted within the freshness
    # window. Best-effort — a Redis outage degrades to HMAC+timestamp only (matches
    # the app's fail-open posture for the nonce store).
    try:
        import hashlib as _hashlib
        _r = get_redis()
        _nonce_key = f"precheck_sig_seen:{_hashlib.sha256(request.signature.encode()).hexdigest()}"
        if not await _r.set(_nonce_key, "1", ex=300, nx=True):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Duplicate signed request")
    except HTTPException:
        raise
    except Exception:
        pass

    # Get target
    target_result = await db.execute(
        select(Target).where(Target.id == request.target_id)
    )
    target = target_result.scalar_one_or_none()

    if not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Target {request.target_id} not found"
        )

    # Save auth session if provided
    if request.success and request.auth_session:
        auth_session_data = request.auth_session.model_dump()
        target.auth_session_encrypted = encrypt_credentials(auth_session_data)

        logger.info(
            f"Saved auth session for target {request.target_id}: "
            f"{len(request.auth_session.cookies or [])} cookies, "
            f"{len(request.auth_session.headers or {})} headers, "
            f"{len(request.auth_session.localStorage or {})} localStorage, "
            f"{len(request.auth_session.sessionStorage or {})} sessionStorage"
        )

        # Trigger full redistribution so target gets distributed to all agents
        from services.capacity_aware_distributor import CapacityAwareDistributor
        from models.config import Config

        # Get global period for redistribution
        global_period_result = await db.execute(
            select(Config).where(Config.key == "global_period_ms")
        )
        global_period_config = global_period_result.scalar_one_or_none()
        global_period_ms = int(global_period_config.value) if global_period_config else 60000

        # Redistribute targets (now target has auth, will go to all agents)
        distributor = CapacityAwareDistributor(db)
        await distributor.distribute_timeslots_and_targets(global_period_ms)

        # Set force_config_update on all agents to trigger hot-reload
        await db.execute(
            update(Agent)
            .where(Agent.status == AgentStatus.ACTIVE)
            .values(force_config_update=True)
        )
        logger.info(f"Redistributed targets and triggered hot-reload after pre-check auth saved for target {request.target_id}")

    # Update agent last_seen_at
    agent.last_seen_at = datetime.utcnow()

    await db.commit()

    return {
        "status": "ok",
        "message": "Pre-check workflow completed",
        "target_id": request.target_id,
        "auth_saved": bool(request.auth_session),
    }


class ScheduledWorkflowCompleteRequest(BaseModel):
    """Request for scheduled workflow completion (lightweight payload)."""
    agent_id: str
    workflow_id: int
    trigger_type: str = "scheduled"
    timestamp: str
    success: bool
    result_data: Optional[dict] = None
    error: Optional[str] = None
    started_at: Optional[str] = None  # ISO format timestamp
    completed_at: Optional[str] = None  # ISO format timestamp
    signature: str


@router.post("/scheduled/complete")
async def complete_scheduled_workflow(
    request: ScheduledWorkflowCompleteRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Complete a scheduled workflow execution.

    Called by agents after executing a scheduled workflow.
    Updates workflow last_scheduled_at and next_scheduled_at.

    HMAC signature verification ensures only authorized agents can report.
    """
    # Verify agent exists and is active
    agent_result = await db.execute(
        select(Agent).where(Agent.agent_id == request.agent_id)
    )
    agent = agent_result.scalar_one_or_none()

    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found"
        )

    if agent.status != AgentStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Agent is {agent.status.value}"
        )

    # Verify HMAC signature
    from security.encryption import SecretEncryption
    from security.hmac import verify_fresh_signed_payload

    secret = None
    if agent.encrypted_secret:
        try:
            secret = SecretEncryption.decrypt_secret(agent.encrypted_secret)
        except Exception:
            pass

    if not secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Cannot verify signature - agent secret not available"
        )

    # Verify signature (must match agent signing payload exactly)
    payload = {
        'agent_id': request.agent_id,
        'workflow_id': request.workflow_id,
        'trigger_type': request.trigger_type,
        'timestamp': request.timestamp,
        'success': request.success,
        'result_data': request.result_data,
        'started_at': request.started_at,
        'completed_at': request.completed_at,
    }
    if request.error:
        payload['error'] = request.error

    # Agent-signed ingestion: constant-time HMAC match AND payload timestamp
    # freshness (replay window).
    if not verify_fresh_signed_payload(payload, secret, request.signature):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or stale HMAC signature"
        )

    # Replay dedupe: reject a signature already accepted within the freshness
    # window. Best-effort — a Redis outage degrades to HMAC+timestamp only.
    try:
        import hashlib as _hashlib
        _r = get_redis()
        _nonce_key = f"scheduled_sig_seen:{_hashlib.sha256(request.signature.encode()).hexdigest()}"
        if not await _r.set(_nonce_key, "1", ex=300, nx=True):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Duplicate signed request")
    except HTTPException:
        raise
    except Exception:
        pass

    # Get workflow
    workflow_result = await db.execute(
        select(AutomationWorkflow).where(AutomationWorkflow.id == request.workflow_id)
    )
    workflow = workflow_result.scalar_one_or_none()

    if not workflow:
        # Workflow was deleted - just acknowledge and return OK
        # Agent will get updated workflow list on next registration/report
        logger.warning(
            f"Agent {request.agent_id} reported completion for deleted workflow {request.workflow_id}"
        )
        return {
            "status": "ok",
            "message": "Workflow no longer exists",
            "workflow_id": request.workflow_id,
            "deleted": True,
        }

    # Update workflow timestamps
    workflow.last_scheduled_at = datetime.utcnow()
    workflow.last_run_at = datetime.utcnow()
    workflow.usage_count += 1

    # Calculate next_scheduled_at (structured recurrence, SPEC §4).
    if workflow.schedule_enabled and (
        workflow.schedule_interval_ms or (workflow.schedule_kind or "interval") in ("daily", "weekly")
    ):
        from services.schedule_recurrence import compute_next_run
        workflow.next_scheduled_at = compute_next_run(
            workflow.schedule_kind,
            datetime.now(timezone.utc),
            workflow.schedule_interval_ms,
            workflow.schedule_time,
            workflow.schedule_days,
            workflow.schedule_tz,
        )

    # Check if captcha was detected and needs reassignment
    captcha_detected = False
    needs_reassignment = False
    if request.result_data:
        captcha_detected = request.result_data.get('captcha_detected', False)
        needs_reassignment = request.result_data.get('needs_reassignment', False)

    # If captcha detected and workflow failed, mark as captcha_blocked
    if captcha_detected and not request.success:
        workflow.captcha_blocked = True
        workflow.last_captcha_at = datetime.utcnow()
        logger.warning(
            f"Workflow {workflow.id} ({workflow.name}) blocked by captcha, "
            f"marking for redistribution to trusted (residential) agents only"
        )

    # Create task record for tracking in UI
    from models.automation_task import AutomationTask

    # BYO-TRUST: the executor agent is untrusted, so we do NOT read its
    # self-reported started_at/completed_at timing into the task — that is
    # agent-asserted and could backdate/postdate a run. We stamp completed_at
    # server-side at the instant the report lands. (request.started_at/
    # completed_at remain in the HMAC-signed payload for backward compat but are
    # NOT trusted as truth.) This scheduled-completion path never routes through
    # _process_task_completion's gateway-attested path.
    task_completed_at = datetime.utcnow()

    task = AutomationTask(
        target_id=None,  # Scheduled workflows aren't tied to a specific target
        workflow_id=request.workflow_id,
        status='success' if request.success else 'failed',
        trigger_type=request.trigger_type,
        completed_at=task_completed_at,
        executor_agent_id=request.agent_id,
        success=request.success,
        result_data=request.result_data,
        error_message=request.error,
        attempt_count=1,
    )
    db.add(task)

    # Update agent last_seen_at
    agent.last_seen_at = datetime.utcnow()

    await db.commit()
    await db.refresh(task)

    logger.info(
        f"Scheduled workflow {request.workflow_id} completed by agent {request.agent_id}: "
        f"success={request.success}, task_id={task.id}"
    )

    # If captcha was detected and needs reassignment, trigger redistribution
    # This will move the workflow to trusted (residential) agents only
    if captcha_detected and needs_reassignment:
        logger.info(
            f"Triggering workflow redistribution due to captcha detection - "
            f"workflow {workflow.id} will be assigned to trusted agents only"
        )
        await _redistribute_scheduled_workflows(db)

    return {
        "status": "ok",
        "message": "Scheduled workflow completed",
        "workflow_id": request.workflow_id,
        "task_id": task.id,
        "captcha_blocked": workflow.captcha_blocked,
        "redistributed": captcha_detected and needs_reassignment,
        "next_scheduled_at": workflow.next_scheduled_at.isoformat() if workflow.next_scheduled_at else None,
    }


@router.post("/workflows/{workflow_id}/clear-captcha-block")
async def clear_captcha_block(
    workflow_id: int,
    db: AsyncSession = Depends(get_db),
    _api_key: str = Depends(get_current_api_key),
):
    """
    Clear captcha_blocked flag to retry workflow on regular agents.

    Use this to manually retry a captcha-blocked workflow on all agents.
    """
    result = await db.execute(
        select(AutomationWorkflow).where(
            AutomationWorkflow.id == workflow_id,
        )
    )
    workflow = result.scalar_one_or_none()

    if not workflow:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow {workflow_id} not found"
        )
    _reject_crawl_dataset(workflow, workflow_id)

    was_blocked = workflow.captcha_blocked
    workflow.captcha_blocked = False

    await db.commit()

    logger.info(f"Cleared captcha block for workflow {workflow_id} ({workflow.name})")

    return {
        "status": "success",
        "workflow_id": workflow_id,
        "was_blocked": was_blocked,
        "message": "Captcha block cleared, workflow will be distributed to all agents",
    }


@router.post("/workflows/{workflow_id}/repair")
async def trigger_manual_repair(
    workflow_id: int,
    task_id: int = Query(..., description="Failed task to repair from"),
    function_name: Optional[str] = Query(
        None,
        description="(Streaming only) repair this specific functions[] script entry "
                    "instead of the advanced_script handler",
    ),
    db: AsyncSession = Depends(get_db),
    _api_key: dict = Depends(get_current_api_key),
):
    """
    Manually trigger AI repair for a failed workflow task.

    STEP workflows: re-runs the workflow with ai_repair_enabled forced on (the agent
    re-derives broken recorded-step selectors at runtime), including session
    persistence if configured.

    STREAMING workflows (``streaming_config`` present): there are no recorded steps to
    re-derive — the logic lives in ``streaming_config.advanced_script`` (or a
    ``functions[]`` script entry). These route to the streaming repair path
    (services.streaming_repair), which gathers the failing JS + the runtime error +
    any console/network/DOM signal, calls the SAME AI gateway, VALIDATES the repaired
    JS compiles before applying, edits the script/function in place, and records it
    via the same RepairHistory mechanism (tagged repair_type="script"/"function").
    """
    wf_result = await db.execute(
        select(AutomationWorkflow).where(
            AutomationWorkflow.id == workflow_id,
        )
    )
    workflow = wf_result.scalar_one_or_none()
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    _reject_crawl_dataset(workflow, workflow_id)

    # Installed PROXY = read-only. AI-repair rewrites steps = recipe mutation;
    # re-syncing the recipe (re-install) is the sanctioned refresh path instead.
    if getattr(workflow, "is_installed", False):
        raise HTTPException(
            status_code=403,
            detail=(
                "Installed workflows are read-only; AI repair (which rewrites the "
                "recipe) is not permitted. Re-sync from the listing to refresh."
            ),
        )

    task_result = await db.execute(
        select(AutomationTask).where(AutomationTask.id == task_id)
    )
    failed_task = task_result.scalar_one_or_none()
    if not failed_task or failed_task.status != "failed":
        raise HTTPException(status_code=400, detail="Task not found or not in failed state")

    # Self-host: AI runs on the owner's BYO provider key with no credit ledger, so
    # there is no per-run credit/plan gate here.

    # --- STREAMING BRANCH ---------------------------------------------------------
    # A streaming workflow has no recorded steps to re-derive; repair its JS instead
    # of re-dispatching it to an agent. Owner-only (installed proxies already 403'd
    # above), server-validated (the repaired JS must compile before it is persisted),
    # AI billed by token use inside the service.
    if getattr(workflow, "streaming_config", None) or function_name:
        from services.streaming_repair import (
            repair_streaming_workflow, StreamingRepairError,
        )
        try:
            result = await repair_streaming_workflow(
                db, workflow, failed_task,
                function_name=function_name,
                api_key=_api_key,
            )
        except StreamingRepairError as e:
            # The AI call (and its token-billing ledger debit) may already have
            # happened before the repair was rejected (e.g. it didn't compile). The
            # tokens WERE spent at the provider, so we COMMIT the billing — but the
            # workflow itself is left UNCHANGED because the service only mutates the
            # recipe AFTER validation passes (no broken script is ever persisted).
            try:
                await db.commit()
            except Exception:
                await db.rollback()
            raise HTTPException(status_code=422, detail=str(e))
        except HTTPException:
            raise
        except Exception as e:
            from services.error_reporting import internal_http_error
            raise internal_http_error(
                e, "Streaming repair failed.", action="automation.streaming_repair"
            )
        # Re-sync any advanced_script-declared functions into workflow.functions so
        # the callable surface stays consistent after a script edit.
        try:
            _sync_advanced_script_functions(workflow)
        except Exception:
            pass
        await db.commit()
        logger.info(
            f"[Repair] Streaming repair applied for workflow {workflow_id} "
            f"(type={result.get('repair_type')}, target={result.get('target_name')})"
        )
        return {
            "status": "repaired",
            "workflow_id": workflow_id,
            "streaming": True,
            **result,
        }

    # Load persistent session (same pattern as normal dispatch)
    session_state = None
    preferred_agent_id = None
    if workflow.session_persistence:
        try:
            from services.session_state_service import SessionStateService
            preferred = await SessionStateService.get_preferred_affinity(db, workflow.id)
            if preferred and not SessionStateService.is_expired(preferred, workflow.session_ttl_seconds):
                preferred_agent_id = preferred.agent_id
                session_state = await SessionStateService.load_session(
                    db, workflow.id, preferred.agent_id, workflow.session_ttl_seconds,
                )
        except Exception as e:
            logger.warning(f"[Repair] Failed to load session for workflow {workflow_id}: {e}")

    # Pick recorder (with affinity preference)
    recorder = await _pick_recorder(db, workflow, preferred_agent_id)
    if not recorder:
        raise HTTPException(status_code=503, detail="No recorder available for repair")

    dispatch_session = session_state if recorder.get('agent_id') == preferred_agent_id else None

    # Create repair task
    repair_task = AutomationTask(
        target_id=failed_task.target_id,
        workflow_id=workflow_id,
        trigger_type="ai_session",
        status="running",
        started_at=datetime.utcnow(),
        executor_agent_id=recorder.get('agent_id'),
        max_attempts=1,
    )
    db.add(repair_task)
    await db.flush()

    # Fire workflow_started trigger for repair task
    try:
        from services.unified_trigger_service import get_unified_trigger_service
        trigger_service = get_unified_trigger_service(db)
        await trigger_service.process_workflow_event(
            event_type="workflow_started",
            workflow_id=workflow_id,
            workflow_name=workflow.name,
            task_id=repair_task.id,
            target_id=failed_task.target_id,
            status="running",
        )
    except Exception as e:
        logger.error(f"Failed to dispatch workflow_started triggers for repair: {e}")

    # Dispatch with session + repair forced on
    form_data = workflow.form_data or {}
    asyncio.create_task(
        _push_workflow_to_recorder(
            task_id=repair_task.id,
            workflow=workflow,
            form_data=form_data,
            recorder_url=recorder['recorder_url'],
            db_url=str(settings.database_url),
            trigger_context=None,
            session_state=dispatch_session,
            force_ai_repair=True,
        )
    )

    await db.commit()

    logger.info(
        f"[Repair] Started repair task {repair_task.id} for workflow {workflow_id} "
        f"(session={'loaded' if dispatch_session else 'fresh'})"
    )

    return {
        "status": "repair_started",
        "task_id": repair_task.id,
        "workflow_id": workflow_id,
        "session_restored": dispatch_session is not None,
    }


@router.get("/workflows/{workflow_id}/session")
async def get_workflow_session(
    workflow_id: int,
    db: AsyncSession = Depends(get_db),
    _api_key: str = Depends(get_current_api_key),
):
    """Get session persistence status for a workflow."""
    from services.session_state_service import SessionStateService
    result = await db.execute(
        select(AutomationWorkflow).where(
            AutomationWorkflow.id == workflow_id,
        )
    )
    workflow = result.scalar_one_or_none()
    if not workflow:
        raise HTTPException(status_code=404, detail=f"Workflow {workflow_id} not found")
    _reject_crawl_dataset(workflow, workflow_id)

    preferred = await SessionStateService.get_preferred_affinity(db, workflow_id)
    if not preferred:
        return {
            "workflow_id": workflow_id,
            "has_session": False,
            "session_persistence": workflow.session_persistence,
        }

    return {
        "workflow_id": workflow_id,
        "has_session": bool(preferred.session_state_encrypted),
        "session_persistence": workflow.session_persistence,
        "agent_id": preferred.agent_id,
        "validation_status": preferred.validation_status,
        "expires_at": preferred.expires_at.isoformat() if preferred.expires_at else None,
        "last_used_at": preferred.last_used_at.isoformat() if preferred.last_used_at else None,
        "is_expired": SessionStateService.is_expired(preferred, workflow.session_ttl_seconds),
    }


@router.delete("/workflows/{workflow_id}/session", status_code=status.HTTP_200_OK)
async def clear_workflow_session(
    workflow_id: int,
    db: AsyncSession = Depends(get_db),
    _api_key: str = Depends(get_current_api_key),
):
    """Clear all saved session state and agent affinities for a workflow."""
    from services.session_state_service import SessionStateService
    result = await db.execute(
        select(AutomationWorkflow).where(
            AutomationWorkflow.id == workflow_id,
        )
    )
    workflow = result.scalar_one_or_none()
    if not workflow:
        raise HTTPException(status_code=404, detail=f"Workflow {workflow_id} not found")
    _reject_crawl_dataset(workflow, workflow_id)

    deleted = await SessionStateService.delete_sessions(db, workflow_id)
    await db.commit()

    return {
        "workflow_id": workflow_id,
        "sessions_deleted": deleted,
        "message": "Session state and agent affinities cleared",
    }


@router.get("/queue/status")
async def queue_status(
    db: AsyncSession = Depends(get_db),
    _api_key: str = Depends(get_current_api_key),
):
    """Get workflow queue depth, capacity snapshot, and wait times."""
    from services.workflow_queue import WorkflowQueue
    from services.recorder_capacity_manager import RecorderCapacityManager

    queue_stats = await WorkflowQueue.get_queue_stats(db)
    cap_mgr = RecorderCapacityManager(db)
    capacity = await cap_mgr.get_total_capacity()

    return {
        **queue_stats,
        "capacity": capacity,
    }


# ============================================================================
# Target Automation Assignment
# ============================================================================

@router.get("/targets/{target_id}/automation")
async def get_target_automation(
    target_id: int,
    db: AsyncSession = Depends(get_db),
    _api_key: str = Depends(get_current_api_key),
):
    """Get automation settings for a target."""
    result = await db.execute(
        select(Target).where(Target.id == target_id)
    )
    target = result.scalar_one_or_none()

    if not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Target {target_id} not found"
        )

    return {
        "target_id": target.id,
        "pre_check_workflow_id": target.pre_check_workflow_id,
        "on_change_workflow_id": target.on_change_workflow_id,
        "on_change_enabled": target.on_change_enabled,
        "on_change_conditions": target.on_change_conditions,
        "on_change_in_session": getattr(target, "on_change_in_session", False),
        "has_auth_session": bool(target.auth_session_encrypted),
    }


@router.post("/targets/{target_id}/automation")
async def update_target_automation(
    target_id: int,
    request: TargetAutomationCreate,
    db: AsyncSession = Depends(get_db),
    _api_key: str = Depends(get_current_api_key),
):
    """Assign automation workflows to a target."""
    result = await db.execute(
        select(Target).where(Target.id == target_id)
    )
    target = result.scalar_one_or_none()

    if not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Target {target_id} not found"
        )

    # Validate workflow IDs if provided
    if request.pre_check_workflow_id:
        workflow_result = await db.execute(
            select(AutomationWorkflow).where(
                AutomationWorkflow.id == request.pre_check_workflow_id,
                AutomationWorkflow.workflow_type == "pre_check"
            )
        )
        if not workflow_result.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Pre-check workflow {request.pre_check_workflow_id} not found or wrong type"
            )

    if request.on_change_workflow_id:
        workflow_result = await db.execute(
            select(AutomationWorkflow).where(
                AutomationWorkflow.id == request.on_change_workflow_id,
                AutomationWorkflow.workflow_type == "on_change"
            )
        )
        if not workflow_result.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"On-change workflow {request.on_change_workflow_id} not found or wrong type"
            )

    # Update target
    target.pre_check_workflow_id = request.pre_check_workflow_id
    target.on_change_workflow_id = request.on_change_workflow_id
    target.on_change_enabled = request.on_change_enabled
    target.on_change_conditions = request.on_change_conditions
    target.on_change_in_session = request.on_change_in_session

    await db.commit()

    logger.info(
        f"Updated automation for target {target_id}: "
        f"pre_check={request.pre_check_workflow_id}, "
        f"on_change={request.on_change_workflow_id} (enabled={request.on_change_enabled})"
    )

    return {
        "target_id": target.id,
        "pre_check_workflow_id": target.pre_check_workflow_id,
        "on_change_workflow_id": target.on_change_workflow_id,
        "on_change_enabled": target.on_change_enabled,
        "on_change_conditions": target.on_change_conditions,
        "on_change_in_session": target.on_change_in_session,
    }


@router.delete("/targets/{target_id}/automation")
async def remove_target_automation(
    target_id: int,
    db: AsyncSession = Depends(get_db),
    _api_key: str = Depends(get_current_api_key),
):
    """Remove automation from a target."""
    result = await db.execute(
        select(Target).where(Target.id == target_id)
    )
    target = result.scalar_one_or_none()

    if not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Target {target_id} not found"
        )

    target.pre_check_workflow_id = None
    target.on_change_workflow_id = None
    target.on_change_enabled = False
    target.on_change_conditions = None
    target.on_change_in_session = False
    target.auth_session_encrypted = None

    await db.commit()

    logger.info(f"Removed automation from target {target_id}")
    return {"message": "Automation removed"}


@router.delete("/targets/{target_id}/auth-session")
async def clear_auth_session(
    target_id: int,
    db: AsyncSession = Depends(get_db),
    _api_key: str = Depends(get_current_api_key),
):
    """Clear auth session for a target (forces re-authentication)."""
    result = await db.execute(
        select(Target).where(Target.id == target_id)
    )
    target = result.scalar_one_or_none()

    if not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Target {target_id} not found"
        )

    target.auth_session_encrypted = None
    await db.commit()

    logger.info(f"Cleared auth session for target {target_id}")
    return {"message": "Auth session cleared"}


# ============================================================================
# AI Autonomous Navigation
# ============================================================================

class AINavigationRequest(BaseModel):
    """Request for AI autonomous navigation."""
    url: str = Field(..., description="Starting URL")
    goal: str = Field(..., description="What to accomplish (e.g., 'complete the registration form')")
    site_description: Optional[str] = Field(None, description="Context about the site")
    available_data: Optional[dict] = Field(default_factory=dict, description="Data for form filling")
    max_steps: int = Field(default=20, ge=1, le=50, description="Maximum steps")
    timeout_ms: int = Field(default=60000, ge=5000, le=300000, description="Timeout in ms")


class AINavigationResponse(BaseModel):
    """Response from AI autonomous navigation."""
    task_id: int
    status: str
    message: str


@router.post("/ai-navigate", response_model=AINavigationResponse)
async def create_ai_navigation_task(
    request: AINavigationRequest,
    db: AsyncSession = Depends(get_db),
    _api_key: str = Depends(get_current_api_key),
):
    """
    Create an AI autonomous navigation task.

    The AI agent will navigate to the URL and autonomously decide what actions
    to take to achieve the specified goal. If required data is missing, it will
    report what's needed.

    This creates a task in the queue that a desktop agent with Playwright will pick up.
    """
    # Create a special AI navigation task
    task = AutomationTask(
        target_id=None,  # No specific target for AI navigation
        workflow_id=None,  # No workflow - AI decides
        trigger_type="ai_navigate",
        status="pending",
        max_attempts=1,
        # Store AI navigation config in result_data temporarily
        result_data={
            "ai_config": {
                "url": request.url,
                "goal": request.goal,
                "site_description": request.site_description,
                "available_data": request.available_data,
                "max_steps": request.max_steps,
                "timeout_ms": request.timeout_ms,
            }
        },
    )

    db.add(task)
    await db.commit()
    await db.refresh(task)

    logger.info(f"Created AI navigation task {task.id}: {request.goal[:50]}...")

    return AINavigationResponse(
        task_id=task.id,
        status="pending",
        message="AI navigation task queued. A desktop agent will process it shortly.",
    )


@router.get("/ai-navigate/pending")
async def get_pending_ai_navigation(
    agent_id: str = Query(..., description="Agent ID requesting a task"),
    db: AsyncSession = Depends(get_db),
    _api_key: str = Depends(get_current_api_key),
):
    """
    Get next pending AI navigation task for an agent.

    Called by desktop agents to pick up AI navigation tasks.
    """
    # Bind agent_id to authenticated identity before it is stamped as executor.
    await _verify_executor_agent(db, agent_id, _api_key)
    # Find oldest pending AI navigation task
    result = await db.execute(
        select(AutomationTask)
        .where(
            AutomationTask.status == "pending",
            AutomationTask.trigger_type == "ai_navigate",
        )
        .order_by(AutomationTask.created_at)
        .limit(1)
    )
    task = result.scalar_one_or_none()

    if not task:
        return None

    # Mark as assigned
    task.status = "assigned"
    task.executor_agent_id = agent_id
    task.attempt_count += 1
    await db.commit()

    ai_config = task.result_data.get("ai_config", {})

    logger.info(f"Assigned AI navigation task {task.id} to agent {agent_id}")

    return {
        "id": task.id,
        "type": "ai_navigate",
        "url": ai_config.get("url"),
        "goal": ai_config.get("goal"),
        "site_description": ai_config.get("site_description"),
        "available_data": ai_config.get("available_data", {}),
        "max_steps": ai_config.get("max_steps", 20),
        "timeout_ms": ai_config.get("timeout_ms", 60000),
    }


class AINavigationResultRequest(BaseModel):
    """Result of AI autonomous navigation."""
    status: str = Field(..., description="completed, partial, failed, missing_data")
    steps_executed: List[dict] = Field(default_factory=list)
    form_data_used: Optional[dict] = None
    missing_data: Optional[List[str]] = None
    extracted_data: Optional[dict] = None
    final_url: Optional[str] = None
    message: Optional[str] = None
    error: Optional[str] = None
    screenshots: Optional[List[dict]] = None
    agent_id: Optional[str] = Field(
        None,
        description="Reporting agent id — must match the task's dispatched executor (anti-spoof / billing-attribution binding).",
    )


@router.post("/ai-navigate/{task_id}/complete")
async def complete_ai_navigation(
    task_id: int,
    result: AINavigationResultRequest,
    db: AsyncSession = Depends(get_db),
    _api_key: dict = Depends(get_current_api_key),
):
    """Report completion of an AI navigation task.

    Requires a valid API key (mirrors complete_task): previously this endpoint
    had NO auth at all, so any unauthenticated caller could mark an arbitrary
    task by id as success/failed.
    """
    query = select(AutomationTask).where(AutomationTask.id == task_id)
    task_result = await db.execute(query)
    task = task_result.scalar_one_or_none()

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found"
        )

    # EXECUTOR BINDING (mirror complete_task): a completion report MUST come from
    # the dispatched executor — otherwise any agent could mark ANOTHER agent's
    # in-flight task success/failed (success-truth forgery). The reporter id is the
    # agent-reported `agent_id`, first bound to the authenticated identity via
    # _verify_executor_agent (anti-spoof), then required to equal the stamped
    # executor.
    reporter_agent_id = (result.agent_id or "").strip() or None
    if reporter_agent_id:
        # Bind the supplied id to the caller (403 if it isn't theirs / is a
        # borrowed platform agent). Platform callers pass through.
        await _verify_executor_agent(db, reporter_agent_id, _api_key)
    if task.executor_agent_id:
        # A dispatched task has a stamped executor — enforce the match. (No reporter
        # id, or an executor-less task, falls through to the scope already
        # applied above, preserving older agents during rollout.)
        if not reporter_agent_id or str(task.executor_agent_id) != reporter_agent_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the dispatched executor agent may report this task's completion.",
            )

    # Terminal-state re-entry guard: a duplicate report is a no-op.
    if task.status in ('success', 'failed', 'timeout', 'cancelled'):
        logger.info(
            f"AI navigation task {task_id} already terminal ({task.status}); "
            f"ignoring duplicate completion report"
        )
        return {
            "task_id": task.id,
            "status": result.status,
            "steps_count": len(result.steps_executed),
            "missing_data": result.missing_data,
            "message": result.message or result.error,
        }

    # Determine success based on status
    success = result.status == "completed"

    task.status = "success" if success else "failed"
    task.completed_at = datetime.utcnow()
    task.success = success
    task.result_data = {
        "ai_navigation": {
            "status": result.status,
            "steps_executed": result.steps_executed,
            "form_data_used": result.form_data_used,
            "missing_data": result.missing_data,
            "extracted_data": result.extracted_data,
            "final_url": result.final_url,
            "message": result.message,
        }
    }
    task.error_message = result.error
    task.screenshots = result.screenshots

    await db.commit()
    await db.refresh(task)

    logger.info(f"AI navigation task {task_id} completed: status={result.status}")

    return {
        "task_id": task.id,
        "status": result.status,
        "steps_count": len(result.steps_executed),
        "missing_data": result.missing_data,
        "message": result.message or result.error,
    }


async def _await_task_dispatchable(task_id: int, trigger_context: dict = None) -> bool:
    """Background-push readiness guard (closes the gate↔hold TOCTOU).

    A push task is scheduled by the dispatch coroutine BEFORE that coroutine's
    transaction commits. A run whose dispatch txn rolled back must NEVER reach the
    agent. This re-loads the task in a fresh session and confirms:

      1. the task row is COMMITTED + visible (the dispatch txn succeeded), and
      2. it is still in a dispatchable state.

    Returns True when safe to push, False to abort. Polls briefly because the push
    task may be scheduled a tick before the caller's commit lands.
    """
    import asyncio
    from database import AsyncSessionLocal
    from models.automation_task import AutomationTask

    tc = trigger_context or {}
    mkt = tc.get("_marketplace") or {}
    # There is no hold/billing path, so a run never needs a hold and the
    # CreditHold check below is never reached.
    needs_hold = False

    for attempt in range(40):  # ~4s max (40 * 100ms), commits land in low ms
        async with AsyncSessionLocal() as g:
            r = await g.execute(
                select(AutomationTask).where(AutomationTask.id == task_id)
            )
            t = r.scalar_one_or_none()
            if t is None:
                # Not yet committed (or rolled back). Wait a tick, then retry.
                await asyncio.sleep(0.1)
                continue
            if t.status not in ("running", "pending", "assigned", "queued"):
                logger.error(
                    "[PushGuard] Task %s not dispatchable (status=%s); aborting push",
                    task_id, t.status,
                )
                return False
            if needs_hold:
                CreditHold = None  # no credit-hold store in this build
                hr = await g.execute(
                    select(CreditHold.id).where(
                        CreditHold.task_id == task_id,
                        CreditHold.status == "held",
                    ).limit(1)
                )
                if hr.scalar_one_or_none() is None:
                    # Either the hold never committed (402/rollback) or it was already
                    # released. Refuse to push a run without a live lien.
                    logger.error(
                        "[PushGuard] Task %s is monetized cross-tenant but has no "
                        "'held' hold; aborting push (hold failed or rolled back)",
                        task_id,
                    )
                    return False
            return True
    logger.error(
        "[PushGuard] Task %s never became dispatchable (commit never landed); "
        "aborting push", task_id,
    )
    return False


async def _push_workflow_to_recorder(
    task_id: int,
    workflow,
    form_data: dict,
    recorder_url: str,
    db_url: str,
    trigger_context: dict = None,
    session_state: dict = None,
    force_ai_repair: bool = False,
    persona: dict = None,
):
    """
    Background task: push a pre-recorded workflow to a recorder for execution.
    Updates the AutomationTask record with results when done.

    Merges webhook-provided secrets (from trigger_context.encrypted_secrets)
    with the workflow's static credentials so the recorder gets everything it needs.
    """
    from database import AsyncSessionLocal
    from models.automation_task import AutomationTask

    # TOCTOU guard: this background task was scheduled BEFORE the dispatch txn
    # committed. Confirm the run is committed and dispatchable before sending
    # anything to the agent — a rolled-back run must never start.
    if not await _await_task_dispatchable(task_id, trigger_context):
        return

    logger.info(f"[WorkflowPush] Pushing task {task_id} (workflow {workflow.id}) to recorder at {recorder_url}")

    # Check if this should go via WebSocket to a user-hosted recorder
    if not recorder_url:
        # WebSocket dispatch — find the agent_id from the task
        await _push_workflow_via_ws(
            task_id, workflow, form_data, trigger_context, session_state, db_url,
            persona=persona,
        )
        return

    # LEGACY HTTP-pool execution path REMOVED. No recorder serves
    # `{recorder_url}/workflows/execute` anywhere in this codebase (workflow runs go
    # over WS; only /ai-tasks/execute exists), so an HTTP-pool pick could only 404.
    # Workflow execution requires a WS-connected agent (handled above). Fail fast
    # and loud instead of POSTing into the void.
    logger.error(
        f"[WorkflowPush] Task {task_id}: HTTP-pool workflow execution is not "
        f"supported (no /workflows/execute endpoint); agent must be WS-connected "
        f"(recorder_url={recorder_url})."
    )
    async with AsyncSessionLocal() as _fail_db:
        _r = await _fail_db.execute(select(AutomationTask).where(AutomationTask.id == task_id))
        _t = _r.scalar_one_or_none()
        if _t and _t.status in ("running", "pending", "assigned", "queued"):
            await _process_task_completion(
                db=_fail_db, task=_t, success=False,
                error="HTTP-pool workflow execution is not supported (route via a WS-connected agent)",
            )
    return


async def _push_workflow_via_ws(
    task_id: int,
    workflow,
    form_data: dict,
    trigger_context: dict = None,
    session_state: dict = None,
    db_url: str = None,
    persona: dict = None,
):
    """Push a workflow to a user-hosted recorder via WebSocket."""
    from database import AsyncSessionLocal
    from models.automation_task import AutomationTask

    # Find which user-hosted agent is assigned to this task
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(AutomationTask).where(AutomationTask.id == task_id)
        )
        task = result.scalar_one_or_none()
        if not task or not task.executor_agent_id:
            logger.error(f"[WorkflowPush-WS] Task {task_id} has no executor_agent_id")
            return

        agent_id = task.executor_agent_id

    # Resolve the executor agent's venue role so a SENSITIVE run on the SHARED
    # cloud fleet (role=='infrastructure', reachable only via WS with an empty
    # recorder_url) still gets the isolated tier. The inline + queue paths stamp
    # this via build_execute_workflow_msg(executor_role=...); the url-less
    # callers (e.g. central_scheduler) funnel here, so fail-safe at the choke.
    _executor_role = None
    try:
        # The executor holds a persistent socket on THIS coordinator (we're about
        # to push a workflow to it), so its live role lives in the in-process fleet
        # registry — not the defunct ws-gateway Redis registry (always empty now).
        from routers.user_recorder_ws import get_connected_recorder_meta
        _meta = get_connected_recorder_meta(agent_id)
        if _meta:
            _executor_role = _meta.get("role")
    except Exception:
        _executor_role = None

    # Build config (decrypt credentials on server side, send plaintext over TLS WS)
    credentials_encrypted = workflow.credentials_encrypted
    trigger_ctx = trigger_context or {}

    if trigger_ctx.get('encrypted_secrets'):
        try:
            from security.encryption import decrypt_secrets_blob, SecretEncryption
            import json as _json
            merged_secrets = {}
            if credentials_encrypted:
                try:
                    wf_secrets = _json.loads(SecretEncryption.decrypt_secret(credentials_encrypted))
                    merged_secrets.update(wf_secrets)
                except Exception:
                    pass
            webhook_secrets = decrypt_secrets_blob(trigger_ctx['encrypted_secrets'])
            merged_secrets.update(webhook_secrets)
            credentials_encrypted = SecretEncryption.encrypt_secret(_json.dumps(merged_secrets))
        except Exception as e:
            logger.error(f"[WorkflowPush-WS] Failed to merge secrets: {e}")

    # Classify the isolation tier off the EFFECTIVE (post-secrets-merge)
    # credentials, but ONLY on the cloud (infrastructure) venue — an own-BYO /
    # user-hosted agent is the user's machine and must never be forced ephemeral.
    isolation_tier = "shared"
    try:
        from services.workflow_router import WorkflowRouter
        if _executor_role == "infrastructure":
            isolation_tier = WorkflowRouter.classify_sensitivity(
                has_credentials=bool(credentials_encrypted),
                has_persona=bool(persona),
                workflow=workflow,
                trigger_context=trigger_context,
            ).value
    except Exception:
        isolation_tier = "shared"

    # Strip creator-IP descriptive metadata on consumer runs (see
    # _strip_recipe_metadata) — the recipe may execute on a buyer/foreign agent.
    _consumer_recipe = (trigger_context or {}).get("_data_source") == "consumer"
    _msg_steps = _strip_recipe_metadata(workflow.steps) if _consumer_recipe else workflow.steps
    _msg_raw_replay = _strip_recipe_metadata(workflow.raw_replay) if _consumer_recipe else workflow.raw_replay

    # FILE ASSETS (§4.1): resolve the run-level files map for this url-less WS push
    # (central_scheduler / other deferred callers funnel here). Fail-closed on a bad
    # reference: mark the task failed rather than ship an upload step with no file.
    _ws_files_map = {}
    try:
        async with AsyncSessionLocal() as _ws_files_db:
            _ws_req_files = (trigger_context or {}).get("files") if isinstance(trigger_context, dict) else None
            _ws_files_map = await _resolve_run_files_map(
                _ws_files_db, workflow,
                request_files=_ws_req_files,
                ttl_seconds=max(
                    int(settings.file_signed_url_ttl_seconds or 600),
                    int((getattr(workflow, "timeout_ms", None) or 0) // 1000) + 60,
                ),
            )
    except HTTPException as _ws_files_e:
        logger.error(f"[WorkflowPush-WS] Task {task_id}: file resolution failed: {_ws_files_e.detail}")
        async with AsyncSessionLocal() as _fdb:
            _r = await _fdb.execute(select(AutomationTask).where(AutomationTask.id == task_id))
            _ft = _r.scalar_one_or_none()
            if _ft and _ft.status in ("running", "pending", "assigned", "queued"):
                await _process_task_completion(
                    db=_fdb, task=_ft, success=False,
                    error="File resolution failed: a referenced file is missing or not owned.",
                )
        return

    from routers.user_recorder_ws import push_to_recorder
    result = await push_to_recorder(agent_id, {
        "type": "execute_workflow",
        "task_id": task_id,
        "workflow_id": workflow.id,
        "config": {
            "entry_url": workflow.entry_url,
            "steps": _msg_steps,
            "raw_replay": _msg_raw_replay,
            "form_data": form_data,
            "credentials_encrypted": credentials_encrypted,
            "timeout_ms": workflow.timeout_ms,
            "headless": workflow.headless if workflow.headless is not None else True,
            "fast_mode": workflow.fast_mode if workflow.fast_mode is not None else True,
            "capture_screenshots": False,
            "session_state": session_state,
            "session_persistence": workflow.session_persistence,
            "login_url_patterns": workflow.login_url_patterns or [],
            "persona": persona,
            "isolation_tier": isolation_tier,
            # FILE ASSETS (§4.1): run-level files map for the agent's upload steps.
            "files": _ws_files_map or {},
        },
    })

    if not result:
        logger.error(f"[WorkflowPush-WS] Failed to push task {task_id} to agent {agent_id}")
        async with AsyncSessionLocal() as db:
            r = await db.execute(select(AutomationTask).where(AutomationTask.id == task_id))
            task = r.scalar_one_or_none()
            if task:
                task.status = "failed"
                task.error_message = "User recorder not available"
                task.completed_at = datetime.utcnow()
                await db.commit()
