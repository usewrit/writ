"""
WorkflowRouter — determines where a workflow should execute based on its requirements.

Routing rules:
- AI workflows (steps contain ai_fill_form, ai_navigate, etc.) → recorder only
- Direct/API calls → recorder only
- Non-AI scheduled workflows → desktop agents preferred
"""
from dataclasses import dataclass
from enum import Enum
from typing import Union

AI_STEP_TYPES = frozenset({
    'ai_fill_form', 'ai_fill', 'ai_continue', 'ai_navigate',
})
AI_WORKFLOW_TYPES = frozenset({'ai_navigate'})


class TrafficType(str, Enum):
    DIRECT = "direct"        # owner-initiated dashboard "Run"
    CALLED = "called"        # programmatic: marketplace consumer + custom/managed API + webhooks
    SCHEDULED = "scheduled"  # central scheduler


class TargetAgent(str, Enum):
    RECORDER_ONLY = "recorder_only"
    DESKTOP_PREFERRED = "desktop_preferred"


class IsolationTier(str, Enum):
    """Isolation tier for a Writ-Cloud run. NOT a global property — it only
    applies when the venue resolves to the shared cloud fleet (platform-owned
    infrastructure agents). A run on the user's OWN agent has no tier (their
    machine, their trust domain, their data)."""
    SHARED = "shared"      # warm browser, fresh context per run — non-sensitive
    ISOLATED = "isolated"  # fresh browser process per run, destroyed after — sensitive


@dataclass
class RoutingDecision:
    target: TargetAgent
    traffic_type: TrafficType
    requires_ai: bool
    # Tier the run REQUIRES if it lands on Writ Cloud. None until classified /
    # when venue is the user's own agent (own-BYO never tiers).
    required_tier: 'IsolationTier | None' = None


class WorkflowRouter:

    @staticmethod
    def requires_ai(workflow) -> bool:
        wf_type = getattr(workflow, 'workflow_type', '') or ''
        if wf_type in AI_WORKFLOW_TYPES:
            return True
        steps = getattr(workflow, 'steps', None) or []
        for step in steps:
            step_type = step.get('type') if isinstance(step, dict) else None
            if step_type in AI_STEP_TYPES:
                return True
        return False

    @staticmethod
    def classify_traffic(trigger_type: str, trigger_context: dict = None) -> TrafficType:
        """Pick the capacity/queue class for a run.

        Three classes, each with its own reserved-but-borrowable capacity pool
        (see RecorderCapacityManager):
          - SCHEDULED: central-scheduler runs.
          - CALLED: programmatic invocations — marketplace consumer runs
            (trigger_context._data_source == "consumer"), incoming webhooks, and
            custom/managed API calls. These must not starve the owner's own
            interactive runs, so they live in their own pool.
          - DIRECT: an owner clicking "Run" in the dashboard (interactive).
        """
        if trigger_type == "scheduled":
            return TrafficType.SCHEDULED
        tc = trigger_context or {}
        if (
            trigger_type == "webhook"
            or tc.get("_data_source") == "consumer"
            or tc.get("type") in ("client_api", "managed_api", "webhook")
        ):
            return TrafficType.CALLED
        return TrafficType.DIRECT

    @staticmethod
    def classify_sensitivity(
        *,
        has_credentials: bool,
        has_persona: bool,
        workflow=None,
        trigger_context: dict = None,
    ) -> IsolationTier:
        """Decide whether a run is SENSITIVE — i.e. would require the isolated
        cloud tier IF it runs on Writ Cloud.

        Sensitive iff the run injects credentials / secrets / persona-auth, OR
        the workflow is unverified / drifted (a consumer marketplace run whose
        listing isn't admin-approved). Plain inputs (an address, a search term)
        are NOT sensitive — they stay on the cheap shared tier.

        Callers pass the EFFECTIVE-for-this-run signals (after consumer
        inversion): `has_credentials` (resolved credentials_encrypted present),
        `has_persona` (a login persona is attached). The venue (own-BYO vs
        cloud) is decided separately by `_pick_recorder`; this only answers
        "is the data sensitive?", which drives the cloud sub-tier.
        """
        if has_credentials or has_persona:
            return IsolationTier.ISOLATED

        tc = trigger_context or {}
        # A marketplace consumer run on a workflow that isn't admin-approved
        # (or whose recipe drifted) is treated as sensitive:
        # "default isolé en cas de doute" (verif expired / version changed). The
        # proxy workflow has no review_status column, so the approval/drift signal
        # is plumbed through trigger_context by _apply_consumer_run_inversion.
        if tc.get("_data_source") == "consumer" and tc.get("_recipe_unverified"):
            return IsolationTier.ISOLATED

        # Step-embedded login signals: a recipe may carry a literal login (a
        # {{secret:password}} placeholder, a password/email field, a twofa step)
        # without the caller passing has_credentials/has_persona. Treat that as
        # sensitive so a credential-bearing own-cloud run isolates. Reached only
        # when has_credentials and has_persona are both False (so the manifest's
        # secret-slot decrypt is a no-op — credentials_encrypted is empty here).
        if workflow is not None:
            try:
                from services.workflow_manifest import derive_data_manifest
                m = derive_data_manifest(workflow)
                if m.get("has_login") or m.get("secret_slots"):
                    return IsolationTier.ISOLATED
            except Exception:
                # Fail safe: on any manifest error prefer the isolated tier for a
                # cloud run (default isolé en cas de doute).
                return IsolationTier.ISOLATED

        return IsolationTier.SHARED

    @staticmethod
    def route(workflow, trigger_type: str, trigger_context: dict = None) -> RoutingDecision:
        is_ai = WorkflowRouter.requires_ai(workflow)
        traffic = WorkflowRouter.classify_traffic(trigger_type, trigger_context)

        # Interactive (direct/called) and AI runs need a full recorder; only plain
        # scheduled non-AI work can prefer desktop agents.
        if is_ai or traffic != TrafficType.SCHEDULED:
            target = TargetAgent.RECORDER_ONLY
        else:
            target = TargetAgent.DESKTOP_PREFERRED

        return RoutingDecision(target=target, traffic_type=traffic, requires_ai=is_ai)


# ---------------------------------------------------------------------------
# Buyer execution-target resolution for INSTALLED marketplace workflows.
#
# Given the creator's marketplace_exec_policy + whether the run is sensitive +
# the buyer's OWN connected agents, return the concrete set of targets the buyer
# may choose. AUTHORITATIVE: this is the single source the install-manifest
# endpoint surfaces to the UI AND the run-path validation uses, so the UI's
# options and the server enforcement can never diverge (never_trust_byo).
# ---------------------------------------------------------------------------
def resolve_allowed_targets(
    policy: str,
    is_sensitive: bool,
    buyer_agents: list,
    *,
    cloud_allowed: bool = True,
    cloud_disabled_reason=None,
) -> dict:
    """Resolve the buyer-selectable execution targets for an installed workflow.

    policy: the creator's marketplace_exec_policy — 'customer' | 'cloud' | 'local'
        | 'creator'. (A MONETIZED 'creator' policy is downgraded to buyer-choice at
        dispatch, so the caller passes 'customer' for that case.)
    is_sensitive: the run injects credentials/persona/secret (login). Only changes
        the cloud TIER label (shared→isolated); never removes the buyer's choice.
    buyer_agents: the buyer's OWN connected agents — [{agent_id, name, online}].

    Returns a descriptor:
      { policy, is_sensitive, locked, default, cloud:{allowed,tier,disabled_reason},
        creator_host, agents:[{agent_id,name,online}] }
    """
    policy = policy if policy in ("customer", "cloud", "local", "creator") else "customer"
    tier = "isolated" if is_sensitive else "shared"
    agents = [
        {
            "agent_id": a.get("agent_id"),
            "name": (a.get("name") or a.get("agent_id")),
            "online": bool(a.get("online")),
        }
        for a in (buyer_agents or [])
        if a.get("agent_id")
    ]

    if policy == "cloud":
        # Creator restricts buyer runs to trusted cloud — venue is fixed.
        return {
            "policy": policy, "is_sensitive": bool(is_sensitive), "locked": True,
            "default": "cloud",
            "cloud": {"allowed": True, "tier": tier, "disabled_reason": cloud_disabled_reason},
            "creator_host": False, "agents": [],
        }
    if policy == "local":
        # Creator restricts buyer runs to the buyer's OWN agent — no cloud. 'auto'
        # resolves to local; named own-agents are selectable.
        return {
            "policy": policy, "is_sensitive": bool(is_sensitive), "locked": True,
            "default": "auto",
            "cloud": {"allowed": False, "tier": tier, "disabled_reason": "Creator set this to run only on your own agent."},
            "creator_host": False, "agents": agents,
        }
    if policy == "creator":
        # Non-monetized creator-host: runs on the CREATOR's agents — no buyer choice.
        return {
            "policy": policy, "is_sensitive": bool(is_sensitive), "locked": True,
            "default": "creator",
            "cloud": {"allowed": False, "tier": tier, "disabled_reason": None},
            "creator_host": True, "agents": [],
        }
    # customer (default) — the buyer picks: Auto, Cloud, or one of their own agents.
    return {
        "policy": "customer", "is_sensitive": bool(is_sensitive), "locked": False,
        "default": "auto",
        "cloud": {"allowed": bool(cloud_allowed), "tier": tier, "disabled_reason": cloud_disabled_reason},
        "creator_host": False, "agents": agents,
    }


def agent_pin_allowed(policy: str) -> bool:
    """Whether a buyer may PIN a specific own agent under this policy (customer or
    local — i.e. policies where an own-agent run is permitted). cloud/creator fix
    the venue away from the buyer's machine, so no pin."""
    return (policy or "customer") in ("customer", "local")
