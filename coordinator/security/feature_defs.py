"""
Feature-gate registry — the canonical list of platform features a platform admin
can enable/disable from the admin "Feature Gates" panel.

This mirrors the LIMIT_DEFS pattern in models/platform_limit.py: a code-defined
registry drives the admin UI and the enforcement layer, so adding a new gate is a
one-line registry edit plus wiring require_feature() at the feature's choke point.

Gate state is stored on the existing FeatureFlag rows (security/feature_gate.py):
  - enabled  -> is_active=True  (+ enabled_plans=["*"] for GATE_TYPE_IDS)
  - disabled -> is_active=False (kill switch — forced OFF for everyone)
  - no row   -> open by default (check_feature_access returns allowed)

The single-owner coordinator has no monetization surface and its feature gate is
always-on, so only the local runtime capabilities are defined here.

Each entry:
  label         human-readable name shown in the admin UI
  group         "core"
  parent        feature_id of a master gate, else None. When the parent is
                disabled the child is denied regardless of its own state (see
                check_feature_access_with_parent). No masters remain today.
  supports_halt whether the "also halt background runs" checkbox applies (i.e. a
                background choke point is wired for feature_killed_for_background)
  help          one-line description
"""
from typing import Optional

FEATURE_GROUPS = ("core",)

FEATURE_DEFS: dict[str, dict] = {
    # ---- core ----
    "workflows": {
        "label": "Workflows (run & dispatch)",
        "group": "core",
        "parent": None,
        "supports_halt": True,
        "help": "Running and dispatching automation workflows.",
    },
    "targets": {
        "label": "Monitors & targets",
        "group": "core",
        "parent": None,
        "supports_halt": False,
        "help": "Creating monitoring targets and scheduled checks.",
    },
    "ai_sessions": {
        "label": "Autonomous AI sessions",
        "group": "core",
        "parent": None,
        "supports_halt": True,
        "help": "Backend-orchestrated autonomous AI agent sessions.",
    },
    "streaming": {
        "label": "Streaming sessions",
        "group": "core",
        "parent": None,
        "supports_halt": True,
        "help": "Interactive live streaming browser sessions.",
    },
    "ai_workflows": {
        "label": "AI assist",
        "group": "core",
        "parent": None,
        "supports_halt": False,
        "help": "AI-assisted authoring (existing plan-targeted flag).",
    },
    "personas": {
        "label": "Personas",
        "group": "core",
        "parent": None,
        "supports_halt": False,
        "help": "Persona credentials & sessions (existing plan-targeted flag).",
    },
    # NOTE: the "ip_relay" gate ("Residential proxy sharing — share your
    # connection to earn proxy credit") was removed. It described a hosted
    # monetization feature that does not exist in the self-host build: nothing in
    # this tree reads the gate, and there is no relay to share a connection with.
    # Leaving it defined both advertised a feature that cannot be enabled and
    # contradicted the README's "no feature gates on the self-host build".
}

# The legacy flags (ai_workflows, personas) are plan-TARGETED: their on/off is the
# is_active kill switch only, and we must NOT overwrite their concrete enabled_plans
# with the ["*"] wildcard. GATE_TYPE_IDS are the gates we own outright and may write
# the wildcard to when enabling.
_LEGACY_PLAN_FLAGS = {"ai_workflows", "personas"}
GATE_TYPE_IDS = frozenset(k for k in FEATURE_DEFS if k not in _LEGACY_PLAN_FLAGS)


def parent_of(feature_id: str) -> Optional[str]:
    """Registry parent (master) of a feature, or None."""
    return (FEATURE_DEFS.get(feature_id) or {}).get("parent")
