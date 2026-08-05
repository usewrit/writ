"""
API key scopes — one vocabulary, enforced fail-closed.

WHAT REPLACED WHAT
------------------
API keys used to carry a per-resource CRUD matrix in JSONB::

    {"workflows": {"permissions": ["read", "write"], "ids": [12]}}

with resources ``workflows|checks|automations|targets|signals|streaming`` and
actions ``read|write|delete``. Three problems killed it:

1. It described six resources out of ~thirty reachable surfaces. Crawls, vault
   secrets, personas, datasets, agents and MCP had NO representation, and the
   modern auth path (``get_auth_context``) applied no path confinement, so a key
   minted as "Monitors: read" reached every one of them unchecked.
2. The three UIs each exposed a different subset of the six, so "Full access"
   meant something different in each app.
3. Router code asked for permissions the vocabulary could not express —
   ``workflows:run`` (automation.py), ``workflows:execute`` (local_workflows.py)
   and the whole ``files`` resource — which meant *triggering a workflow run with
   an API key was impossible*: the check could never pass.

The model here is ``resource:action`` strings over the surfaces that actually
exist, plus an ordered (method, path) -> required-scope map so coverage is a
property of the router table rather than of whether someone remembered to add a
check. Anything not in that map is DENIED to API keys.

These are user/app access keys. They carry authorization only — no billing,
budget or credit semantics live here.

STORAGE (``api_keys.scopes``, JSONB)::

    {"v": 2,
     "scopes": ["workflows:read", "workflows:execute", "datasets:read"],
     "ids": {"workflows": [12, 15]}}

``ids`` is optional and per-resource: absent (or null) = every object of that
resource; a list = only those object ids. Object-level pinning is checked by the
endpoint (it needs the id from the path); the resource/action pair is checked at
the choke point.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

# ─────────────────────────────── vocabulary ────────────────────────────────

READ, WRITE, EXECUTE, DELETE = "read", "write", "execute", "delete"

# resource -> (label, description, allowed actions)
#
# `actions` is the authority for what a UI may offer: a resource that cannot be
# executed must not render an Execute toggle, and a resource with no delete
# surface must not render Delete. Keep this in sync with ROUTE_MAP below — the
# self-test at the bottom of this module asserts every scope the route map can
# demand is declared here.
RESOURCES: dict[str, dict] = {
    "workflows": {
        "label": "Workflows",
        "description": "Automations you have built, and running them",
        "actions": (READ, WRITE, EXECUTE, DELETE),
        "pinnable": True,
    },
    "runs": {
        "label": "Runs",
        "description": "Execution history, results and live run state",
        "actions": (READ,),
        "pinnable": False,
    },
    "monitors": {
        "label": "Monitors",
        "description": "Watched pages, their selectors and detected changes",
        "actions": (READ, WRITE, EXECUTE, DELETE),
        "pinnable": True,
    },
    "datasets": {
        "label": "Datasets",
        "description": "Extracted records from workflow runs",
        "actions": (READ, DELETE),
        "pinnable": True,
    },
    "transfer": {
        "label": "Import & export",
        "description": (
            "Packaging this install's assets into a portable file, and importing one. "
            "Deliberately NOT in any preset: an export is a bulk read of everything the "
            "install owns, so a key must be granted it explicitly."
        ),
        "actions": (READ, WRITE),
        "pinnable": False,
    },
    "crawl": {
        "label": "Crawls",
        "description": "Site-wide crawls — starting one consumes account usage",
        "actions": (READ, EXECUTE, DELETE),
        "pinnable": False,
    },
    "scrape": {
        "label": "Scrape & map",
        "description": "One-off page scrapes and site maps",
        "actions": (EXECUTE,),
        "pinnable": False,
    },
    "files": {
        "label": "Files",
        "description": "Uploaded and produced file assets",
        "actions": (READ, WRITE, DELETE),
        "pinnable": False,
    },
    "personas": {
        "label": "Personas",
        "description": "Saved browser identities and their sessions",
        "actions": (READ, WRITE, DELETE),
        "pinnable": False,
    },
    "secrets": {
        "label": "Secrets",
        "description": "Vault entries. Values are never returned — only names and metadata",
        "actions": (READ, WRITE, DELETE),
        "pinnable": False,
    },
    "agents": {
        "label": "Agents",
        "description": "Your own devices and fleet agents",
        "actions": (READ, WRITE, EXECUTE),
        "pinnable": False,
    },
    "triggers": {
        "label": "Triggers & webhooks",
        "description": "Trigger rules, webhook endpoints, and firing them",
        "actions": (READ, WRITE, EXECUTE, DELETE),
        "pinnable": False,
    },
    "recorder": {
        "label": "Recorder",
        "description": "Live record and browse sessions — what the record/browser tools drive",
        "actions": (READ, EXECUTE),
        "pinnable": False,
    },
    "streaming": {
        "label": "Streaming",
        "description": "Streaming sessions and OpenAI-compatible endpoints",
        "actions": (READ, EXECUTE, DELETE),
        "pinnable": False,
    },
    "mcp": {
        "label": "MCP",
        "description": "The MCP server endpoint, its tool catalogue, and exposing tools",
        "actions": (READ, WRITE, EXECUTE, DELETE),
        "pinnable": False,
    },
    "account": {
        "label": "Account",
        "description": "Read-only account, plan and usage summary",
        "actions": (READ,),
        "pinnable": False,
    },
}

ACTIONS: dict[str, str] = {
    READ: "View",
    WRITE: "Create and modify",
    EXECUTE: "Run",
    DELETE: "Delete",
}

# Order the UI should render resources in. Anything not listed sorts last,
# alphabetically, so adding a resource above cannot silently vanish from a list.
RESOURCE_ORDER = (
    "workflows", "runs", "monitors", "datasets", "crawl", "scrape",
    "files", "personas", "secrets", "agents", "triggers", "streaming",
    "recorder", "mcp", "account",
)

_SCOPE_RE = re.compile(r"^([a-z_]+):(read|write|execute|delete|\*)$")


def all_scopes() -> list[str]:
    """Every concrete (non-wildcard) scope string, in UI order."""
    out: list[str] = []
    for resource in sorted(RESOURCES, key=_resource_sort_key):
        for action in RESOURCES[resource]["actions"]:
            out.append(f"{resource}:{action}")
    return out


def _resource_sort_key(resource: str) -> tuple:
    try:
        return (0, RESOURCE_ORDER.index(resource))
    except ValueError:
        return (1, resource)


def is_valid_scope(scope: str) -> bool:
    """True if `scope` is a well-formed scope naming a real resource+action."""
    m = _SCOPE_RE.match(scope or "")
    if not m:
        return False
    resource, action = m.group(1), m.group(2)
    spec = RESOURCES.get(resource)
    if spec is None:
        return False
    return action == "*" or action in spec["actions"]


def expand(scopes: Iterable[str]) -> set[str]:
    """Expand ``resource:*`` into its concrete scopes; drop anything invalid.

    Expansion happens once, at grant time, so what is stored is exactly what was
    granted: a wildcard minted today must not silently widen when a new action is
    added to a resource tomorrow.
    """
    out: set[str] = set()
    for scope in scopes or ():
        m = _SCOPE_RE.match((scope or "").strip())
        if not m:
            continue
        resource, action = m.group(1), m.group(2)
        spec = RESOURCES.get(resource)
        if spec is None:
            continue
        if action == "*":
            out.update(f"{resource}:{a}" for a in spec["actions"])
        elif action in spec["actions"]:
            out.add(f"{resource}:{action}")
    return out


# ──────────────────────────────── presets ──────────────────────────────────
#
# Intent-shaped, not resource-shaped. A person minting a key is answering "what
# is this for?", not "which of fifteen resources times four verbs?". Custom is
# still there for the case the presets do not cover.

#: Resources a PRESET never grants, even when the preset's rule would sweep them
#: in. `transfer:read` is not "read a thing" — it packages every asset the install
#: owns into one downloadable file, so a "Read-only" or "Run automations" key
#: picking it up by pattern would hand bulk exfiltration to the least-privileged
#: preset in the product. It stays available under Custom and Full access, where
#: choosing it is a deliberate act.
_PRESET_EXCLUDED_RESOURCES = frozenset({"transfer"})


def _preset_scopes(*actions: str) -> list[str]:
    """Scopes for a preset: every resource offering one of `actions`, minus the
    resources presets must never sweep in."""
    return sorted(
        f"{r}:{a}"
        for r, spec in RESOURCES.items()
        if r not in _PRESET_EXCLUDED_RESOURCES
        for a in actions
        if a in spec["actions"]
    )


PRESETS: dict[str, dict] = {
    "read_only": {
        "label": "Read-only",
        "description": "See everything, change nothing. Safe for dashboards and reporting.",
        "scopes": _preset_scopes(READ),
    },
    "run": {
        "label": "Run automations",
        "description": "Read your data and trigger runs, crawls and scrapes — but not edit or delete anything.",
        "scopes": _preset_scopes(READ, EXECUTE),
    },
    "full": {
        "label": "Full access",
        "description": "Everything this account can do over the API, including delete.",
        "scopes": sorted(all_scopes()),
    },
}


def preset_scopes(preset_id: str) -> Optional[list[str]]:
    preset = PRESETS.get(preset_id)
    return list(preset["scopes"]) if preset else None


def match_preset(granted: Iterable[str]) -> Optional[str]:
    """Name the preset a grant is exactly equal to, else None (= custom)."""
    have = set(granted or ())
    for preset_id, preset in PRESETS.items():
        if have == set(preset["scopes"]):
            return preset_id
    return None


def catalog() -> dict:
    """The whole vocabulary, shaped for a UI to render without hardcoding it.

    The three apps diverged in the first place because each hardcoded its own
    resource list. They now fetch this.
    """
    return {
        "resources": [
            {
                "key": key,
                "label": RESOURCES[key]["label"],
                "description": RESOURCES[key]["description"],
                "actions": list(RESOURCES[key]["actions"]),
                "pinnable": RESOURCES[key]["pinnable"],
            }
            for key in sorted(RESOURCES, key=_resource_sort_key)
        ],
        "actions": [{"key": k, "label": v} for k, v in ACTIONS.items()],
        "presets": [
            {
                "key": key,
                "label": preset["label"],
                "description": preset["description"],
                "scopes": list(preset["scopes"]),
            }
            for key, preset in PRESETS.items()
        ],
    }


# ───────────────────────────── stored representation ────────────────────────

SCOPES_VERSION = 2


def build_scopes_blob(scopes: Iterable[str], ids: Optional[dict] = None) -> dict:
    """Normalize a grant into the JSONB shape stored on ``api_keys.scopes``."""
    granted = expand(scopes)
    pinned: dict[str, list[int]] = {}
    for resource, id_list in (ids or {}).items():
        if resource not in RESOURCES or not RESOURCES[resource]["pinnable"]:
            continue
        if id_list is None:
            continue
        # Only pin a resource the key actually holds a scope on — otherwise the
        # pin is dead weight that reads like a restriction on the detail screen.
        if not any(s.startswith(f"{resource}:") for s in granted):
            continue
        cleaned = sorted({int(i) for i in id_list})
        if cleaned:
            pinned[resource] = cleaned
    return {"v": SCOPES_VERSION, "scopes": sorted(granted), "ids": pinned}


def granted_scopes(blob: Optional[dict]) -> set[str]:
    """Read the granted scope set out of a stored blob. Unknown shape = nothing."""
    if not isinstance(blob, dict):
        return set()
    scopes = blob.get("scopes")
    if not isinstance(scopes, list):
        return set()
    return {s for s in scopes if isinstance(s, str) and is_valid_scope(s)}


def pinned_ids(blob: Optional[dict], resource: str) -> Optional[list[int]]:
    """Object ids this key is pinned to for `resource`; None = all of them."""
    if not isinstance(blob, dict):
        return None
    ids = blob.get("ids")
    if not isinstance(ids, dict):
        return None
    value = ids.get(resource)
    return value if isinstance(value, list) else None


def has(blob: Optional[dict], scope: str) -> bool:
    return scope in granted_scopes(blob)


def summarize(blob: Optional[dict]) -> str:
    """One-line, human-readable grant summary (audit logs, CLI, key list rows)."""
    granted = granted_scopes(blob)
    if not granted:
        return "No access"
    preset = match_preset(granted)
    if preset:
        return PRESETS[preset]["label"]
    by_resource: dict[str, list[str]] = {}
    for scope in granted:
        resource, action = scope.split(":", 1)
        by_resource.setdefault(resource, []).append(action)
    parts = []
    for resource in sorted(by_resource, key=_resource_sort_key):
        actions = ",".join(sorted(by_resource[resource]))
        pins = pinned_ids(blob, resource)
        suffix = f" ({len(pins)} selected)" if pins else ""
        parts.append(f"{RESOURCES[resource]['label']}: {actions}{suffix}")
    return "; ".join(parts)


# ─────────────────────── (method, path) -> required scope ───────────────────
#
# Ordered, first match wins — put the specific patterns before the general ones.
# A path an API key may reach MUST appear here; everything else is refused by
# `required_scope` returning None. That inversion is the point: the old model
# opted endpoints IN to being checked and 89 routers were never opted in.
#
# Patterns are matched against the full request path with `{}` standing for one
# path segment and a trailing `*` for "anything below here".

_ROUTE_RULES: tuple[tuple[str, str, str], ...] = (
    # ── portable import/export (/api/transfer/*) ──
    # An export reads EVERY selected asset, so it is gated on its own resource
    # rather than on workflows:read — see DATA_PORTABILITY_SPEC §10. Most specific
    # first: /imports/{}/commit must not be shadowed by /imports/{}.
    ("POST",   "/api/transfer/export/preview",                "transfer:read"),
    ("POST",   "/api/transfer/export",                        "transfer:read"),
    ("POST",   "/api/transfer/inspect",                       "transfer:write"),
    ("POST",   "/api/transfer/stage",                         "transfer:write"),
    ("PUT",    "/api/transfer/imports/{}/plan",               "transfer:write"),
    ("POST",   "/api/transfer/imports/{}/commit",             "transfer:write"),
    ("POST",   "/api/transfer/imports/{}/undo",               "transfer:write"),
    ("DELETE", "/api/transfer/imports/{}",                    "transfer:write"),
    ("GET",    "/api/transfer/imports/{}",                    "transfer:read"),
    ("GET",    "/api/transfer/imports",                       "transfer:read"),

    # ── public versioned API (/api/v1/*) ──
    ("POST",   "/api/v1/local-workflows/{}/run",              "workflows:execute"),
    ("GET",    "/api/v1/local-workflows",                     "workflows:read"),
    ("POST",   "/api/v1/files",                               "files:write"),
    ("GET",    "/api/v1/files/*",                             "files:read"),
    ("GET",    "/api/v1/files",                               "files:read"),
    ("DELETE", "/api/v1/files/*",                             "files:delete"),

    # ── workflows / automation ──
    #
    # Extracted records live under the automation router here (there is no
    # separate /api/v1/datasets surface in the self-host build), so the data
    # subtree is carved out BEFORE the general automation read rule — otherwise
    # `datasets:read` would be unreachable and `workflows:read` would silently
    # carry the records too.
    ("POST",   "/api/automation/workflows/{}/run",            "workflows:execute"),
    ("POST",   "/api/automation/workflows/{}/cancel",         "workflows:execute"),
    ("POST",   "/api/automation/ai-sessions/{}/run",          "workflows:execute"),
    ("POST",   "/api/automation/ai-sessions/{}/cancel",       "workflows:execute"),
    ("GET",    "/api/automation/workflows/{}/data/*",         "datasets:read"),
    ("GET",    "/api/automation/workflows/{}/data",           "datasets:read"),
    ("DELETE", "/api/automation/workflows/{}/data",           "datasets:delete"),
    ("GET",    "/api/automation/*",                           "workflows:read"),
    ("POST",   "/api/automation/*",                           "workflows:write"),
    ("PUT",    "/api/automation/*",                           "workflows:write"),
    ("PATCH",  "/api/automation/*",                           "workflows:write"),
    ("DELETE", "/api/automation/*",                           "workflows:delete"),

    # ── runs ──
    ("GET",    "/api/runs/*",                                 "runs:read"),
    ("GET",    "/api/runs",                                   "runs:read"),

    # ── monitors (targets, selectors, extractors, schedules) ──
    ("POST",   "/api/targets/{}/run",                         "monitors:execute"),
    ("GET",    "/api/targets/*",                              "monitors:read"),
    ("GET",    "/api/targets",                                "monitors:read"),
    ("POST",   "/api/targets/*",                              "monitors:write"),
    ("POST",   "/api/targets",                                "monitors:write"),
    ("PUT",    "/api/targets/*",                              "monitors:write"),
    ("PATCH",  "/api/targets/*",                              "monitors:write"),
    ("DELETE", "/api/targets/*",                              "monitors:delete"),
    ("GET",    "/api/extractors/*",                           "monitors:read"),
    ("GET",    "/api/extractors",                             "monitors:read"),
    ("POST",   "/api/extractors/*",                           "monitors:write"),
    ("POST",   "/api/extractors",                             "monitors:write"),
    ("PUT",    "/api/extractors/*",                           "monitors:write"),
    ("PATCH",  "/api/extractors/*",                           "monitors:write"),
    ("DELETE", "/api/extractors/*",                           "monitors:delete"),
    ("GET",    "/api/schedule/*",                             "monitors:read"),
    ("GET",    "/api/schedule",                               "monitors:read"),
    ("POST",   "/api/schedule/*",                             "monitors:write"),
    ("POST",   "/api/schedule",                               "monitors:write"),
    ("PUT",    "/api/schedule/*",                             "monitors:write"),
    ("PATCH",  "/api/schedule/*",                             "monitors:write"),
    ("DELETE", "/api/schedule/*",                             "monitors:delete"),

    # ── custom-path webhooks ──
    #
    # POST /api/v1/webhooks/{custom_path} FIRES a webhook trigger, so it takes the same
    # scope as its sibling `POST /api/webhooks/trigger/*` — one vocabulary for one act,
    # rather than a second spelling for the same thing.
    #
    # This is only the outer gate. The handler additionally requires the key to be
    # scoped to the TARGET workflow (`key_can_run_workflow`, which pins on
    # `workflows:read` for that id) and enforces the key's own run budgets, exactly as
    # the direct run endpoint does — so `triggers:execute` alone cannot run a workflow
    # the key was never given.
    ("POST",   "/api/v1/webhooks/*",                          "triggers:execute"),

    # ── crawl / scrape ──
    #
    # /api/crawl/scrape and /api/crawl/map are one-shot fetches, not crawls, and
    # are the cheapest useful thing a key can do — they get their own resource so
    # "let this script scrape a page" does not also authorise a full site crawl.
    ("POST",   "/api/crawl/scrape",                           "scrape:execute"),
    ("POST",   "/api/crawl/map",                              "scrape:execute"),
    ("POST",   "/api/crawl/preview",                          "scrape:execute"),
    # Saved crawls (the callable surface). Running one dispatches a real crawl,
    # so it needs crawl:execute — the same authority as POST /api/crawl. Saving
    # or editing a definition carries that authority in deferred form (it decides
    # what a later run does), so it is deliberately NOT a weaker scope. Declared
    # ABOVE the generic crawl rules: first match wins.
    ("POST",   "/api/crawl/definitions/{}/run",               "crawl:execute"),
    ("POST",   "/api/crawl/definitions",                      "crawl:execute"),
    ("PATCH",  "/api/crawl/definitions/{}",                   "crawl:execute"),
    ("POST",   "/api/crawl/{}/cancel",                        "crawl:execute"),
    ("POST",   "/api/crawl",                                  "crawl:execute"),
    ("GET",    "/api/crawl/*",                                "crawl:read"),
    ("GET",    "/api/crawl",                                  "crawl:read"),
    ("DELETE", "/api/crawl/*",                                "crawl:delete"),
    ("GET",    "/api/scraping/*",                             "scrape:execute"),
    ("POST",   "/api/scraping/*",                             "scrape:execute"),

    # ── files ──
    ("GET",    "/api/files/*",                                "files:read"),
    ("GET",    "/api/files",                                  "files:read"),
    ("POST",   "/api/files/*",                                "files:write"),
    ("POST",   "/api/files",                                  "files:write"),
    ("PATCH",  "/api/files/*",                                "files:write"),
    ("DELETE", "/api/files/*",                                "files:delete"),

    # ── personas ──
    ("GET",    "/api/personas/*",                             "personas:read"),
    ("GET",    "/api/personas",                               "personas:read"),
    ("POST",   "/api/personas/*",                             "personas:write"),
    ("POST",   "/api/personas",                               "personas:write"),
    ("PUT",    "/api/personas/*",                             "personas:write"),
    ("PATCH",  "/api/personas/*",                             "personas:write"),
    ("DELETE", "/api/personas/*",                             "personas:delete"),

    # ── vault / secrets ──
    #
    # Reading a secret's VALUE is not a route — the vault only ever returns names
    # and metadata to a caller — so secrets:read is metadata-only by construction.
    ("GET",    "/api/vault/secrets/*",                        "secrets:read"),
    ("GET",    "/api/vault/secrets",                          "secrets:read"),
    ("POST",   "/api/vault/secrets",                          "secrets:write"),
    ("PATCH",  "/api/vault/secrets/*",                        "secrets:write"),
    ("DELETE", "/api/vault/secrets/*",                        "secrets:delete"),

    # ── agents / fleet ──
    #
    # ENUMERATED, NOT WILDCARDED. /api/agents also carries the agent daemon's own
    # enrolment protocol (register, poll, heartbeat, report, deregister), which
    # authenticates with a device secret. A blanket `POST /api/agents/*` rule would
    # have let any key holding agents:write speak that protocol and impersonate a
    # device, so those paths are deliberately absent and therefore denied.
    ("GET",    "/api/agents",                                 "agents:read"),
    ("GET",    "/api/agents/config",                          "agents:read"),
    ("GET",    "/api/agents/{}/errors",                       "agents:read"),
    ("POST",   "/api/agents/{}/force-check",                  "agents:execute"),
    ("POST",   "/api/agents/{}/revoke",                       "agents:write"),
    ("POST",   "/api/agents/{}/rotate-secret",                "agents:write"),
    ("PATCH",  "/api/agents/{}/weight",                       "agents:write"),
    ("PATCH",  "/api/agents/{}/trusted",                      "agents:write"),
    ("PATCH",  "/api/agents/{}/captcha-trusted",              "agents:write"),
    ("GET",    "/api/fleet/workflows/{}/mirrors",             "agents:read"),
    ("POST",   "/api/fleet/agents/{}/deploy",                 "agents:execute"),

    # ── triggers / webhooks ──
    ("POST",   "/api/webhooks/trigger/*",                     "triggers:execute"),
    ("GET",    "/api/triggers/*",                             "triggers:read"),
    ("GET",    "/api/triggers",                               "triggers:read"),
    ("POST",   "/api/triggers/*",                             "triggers:write"),
    ("POST",   "/api/triggers",                               "triggers:write"),
    ("PUT",    "/api/triggers/*",                             "triggers:write"),
    ("PATCH",  "/api/triggers/*",                             "triggers:write"),
    ("DELETE", "/api/triggers/*",                             "triggers:delete"),
    ("GET",    "/api/webhooks/*",                             "triggers:read"),
    ("POST",   "/api/webhooks/*",                             "triggers:write"),
    ("DELETE", "/api/webhooks/*",                             "triggers:delete"),

    # ── streaming ──
    ("POST",   "/api/streaming/sessions/start",               "streaming:execute"),
    ("POST",   "/api/streaming/sessions/{}/invoke/*",         "streaming:execute"),
    ("POST",   "/api/streaming/*/chat/completions",           "streaming:execute"),
    ("POST",   "/api/streaming/*/responses",                  "streaming:execute"),
    ("POST",   "/api/streaming/sessions/{}/end",              "streaming:execute"),
    ("GET",    "/api/streaming/*",                            "streaming:read"),
    ("POST",   "/api/streaming/*",                            "streaming:execute"),
    ("DELETE", "/api/streaming/*",                            "streaming:delete"),

    # ── recorder (live record / browse sessions) ──
    #
    # The MCP server reaches these by calling this API over loopback with the
    # CALLER's bearer, so a key that may run the record/browser tools must hold
    # recorder:execute — otherwise the advertised MCP integration 403s.
    ("GET",    "/api/recorder/health",                        "recorder:read"),
    ("GET",    "/api/recorder/recorders",                     "recorder:read"),
    ("POST",   "/api/recorder/assign",                        "recorder:execute"),
    ("POST",   "/api/recorder/connect",                       "recorder:execute"),
    ("POST",   "/api/recorder/convert/python",                "recorder:execute"),
    ("GET",    "/api/recorder/api/*",                         "recorder:read"),
    ("POST",   "/api/recorder/api/*",                         "recorder:execute"),
    ("PUT",    "/api/recorder/api/*",                         "recorder:execute"),
    ("DELETE", "/api/recorder/api/*",                         "recorder:execute"),
    ("GET",    "/api/user-recorder/*",                        "recorder:read"),
    ("POST",   "/api/user-recorder/*",                        "recorder:execute"),

    # ── MCP ──
    ("POST",   "/mcp",                                        "mcp:execute"),
    ("GET",    "/mcp",                                        "mcp:read"),
    ("GET",    "/api/mcp/*",                                  "mcp:read"),
    ("POST",   "/api/mcp/*",                                  "mcp:execute"),
    ("GET",    "/api/mcp-endpoints/*",                        "mcp:read"),
    ("GET",    "/api/mcp-endpoints",                          "mcp:read"),
    ("POST",   "/api/mcp-endpoints/*",                        "mcp:write"),
    ("POST",   "/api/mcp-endpoints",                          "mcp:write"),
    ("PATCH",  "/api/mcp-endpoints/*",                        "mcp:write"),
    ("DELETE", "/api/mcp-endpoints/*",                        "mcp:delete"),

    # ── account (read-only) ──
    ("GET",    "/api/logs/*",                                 "account:read"),
    ("GET",    "/api/logs",                                   "account:read"),
    ("GET",    "/api/auth/me",                                "account:read"),
)

# Paths every authenticated caller may reach regardless of grant. Deliberately
# tiny: identity self-check and liveness only. `/api/auth/api-keys*` is NOT here
# — a key must never be able to enumerate, mint or widen keys.
_ALWAYS_ALLOWED: frozenset[str] = frozenset({
    "/health", "/healthz", "/api/health", "/api/auth/whoami",
})


def _path_matches(pattern: str, path: str) -> bool:
    if pattern.endswith("/*"):
        head = pattern[:-2]
        if not (path == head or path.startswith(head + "/")):
            return False
        pattern_segments = head.split("/")
        path_segments = path.split("/")[: len(head.split("/"))]
    else:
        pattern_segments = pattern.split("/")
        path_segments = path.split("/")
        if len(pattern_segments) != len(path_segments):
            return False
    for expected, actual in zip(pattern_segments, path_segments):
        if expected == "{}":
            if not actual:
                return False
            continue
        if expected == "*":
            continue
        if expected != actual:
            return False
    return True


def required_scope(method: str, path: str) -> Optional[str]:
    """The scope an API key must hold to call ``method path``.

    Returns None when the path is not part of the API-key surface at all. The
    caller must treat None as DENY, not as "unrestricted" — that inversion is the
    whole reason this map exists.
    """
    method = (method or "").upper()
    if method == "HEAD":
        method = "GET"
    if method == "OPTIONS":
        return None
    path = (path or "").rstrip("/") or "/"
    for rule_method, pattern, scope in _ROUTE_RULES:
        if rule_method != method:
            continue
        if _path_matches(pattern, path):
            return scope
    return None


def is_always_allowed(path: str) -> bool:
    return ((path or "").rstrip("/") or "/") in _ALWAYS_ALLOWED


# ───────────────────── in-endpoint checks (object-level) ────────────────────
#
# The route map answers "may this key call this endpoint at all". It cannot
# answer "may this key touch workflow #12", because the id lives in the path or
# the body. Endpoints keep asking that question through `require_scope` /
# `check_api_key_scope`, which call in here.
#
# Those ~80 call sites speak the OLD resource names. Rather than rewrite each one
# (and get a merge conflict per site), the names are translated centrally. The
# aliases are the reason `checks`, `targets` and `automations` no longer exist as
# separate grantable things: they were three names for two surfaces.

_RESOURCE_ALIASES = {
    "checks": "monitors",
    "targets": "monitors",
    "signals": "monitors",
    "automations": "workflows",
}

# `run` was what automation.py asked for, `execute` was what local_workflows.py
# asked for, and neither existed. They are the same verb.
_ACTION_ALIASES = {"run": EXECUTE}


def canonical(resource: str, action: str) -> Optional[str]:
    """Translate a possibly-legacy (resource, action) pair into a scope string."""
    resource = _RESOURCE_ALIASES.get(resource, resource)
    action = _ACTION_ALIASES.get(action, action)
    scope = f"{resource}:{action}"
    return scope if is_valid_scope(scope) else None


def check(
    blob: Optional[dict],
    resource: str,
    action: str,
    resource_id: Optional[int] = None,
) -> bool:
    """Does this stored grant permit (resource, action) — and object `resource_id`?"""
    scope = canonical(resource, action)
    if scope is None:
        return False
    if scope not in granted_scopes(blob):
        return False
    if resource_id is None:
        return True
    pins = pinned_ids(blob, scope.split(":", 1)[0])
    return pins is None or resource_id in pins


def allowed_ids(blob: Optional[dict], resource: str) -> Optional[list[int]]:
    """Object ids a key may see for `resource`; None = all, [] = none at all."""
    canonical_resource = _RESOURCE_ALIASES.get(resource, resource)
    if not any(
        s.startswith(f"{canonical_resource}:") for s in granted_scopes(blob)
    ):
        return []
    return pinned_ids(blob, canonical_resource)


# The route map is only trustworthy if every scope it can demand is a scope the
# vocabulary can actually grant — the previous model failed exactly here, asking
# for `workflows:run` and `files:*` that no key could ever hold. Import-time, so
# a typo is a startup failure rather than a 403 in production.
_undeclared = sorted({s for _, _, s in _ROUTE_RULES if not is_valid_scope(s)})
if _undeclared:  # pragma: no cover - guards a developer mistake
    raise RuntimeError(
        f"api_scopes: route map demands scopes that no key can hold: {_undeclared}"
    )

# ...and the converse. A scope no route ever demands is a checkbox in three UIs
# that grants nothing — which is how `files:*` and `workflows:run` ended up on
# opposite sides of an unbridgeable gap. If you add a resource here, map a route
# to it in the same commit.
_ungated = sorted(set(all_scopes()) - {s for _, _, s in _ROUTE_RULES})
if _ungated:  # pragma: no cover - guards a developer mistake
    raise RuntimeError(
        f"api_scopes: these scopes are grantable but no route requires them: {_ungated}"
    )
