"""
OAuth 2.0 scope definitions and validation.

Defines granular scopes for third-party integrations and maps them
to the existing RBAC role system for backward compatibility.
"""
from typing import Optional

# All available OAuth scopes with human-readable descriptions
OAUTH_SCOPES = {
    # Targets / Monitoring
    "targets:read": "View monitored targets and their status",
    "targets:write": "Create, update, and delete targets",
    "changes:read": "View detected changes and diffs",

    # Workflows / Automation
    "workflows:read": "View automation workflows",
    "workflows:write": "Create and modify workflows",
    "workflows:execute": "Trigger workflow execution",

    # Triggers
    "triggers:read": "View trigger rules and webhook configurations",
    "triggers:write": "Create and modify triggers",

    # Reports / Data
    "reports:read": "View scrape results and reports",

    # Vault / Secrets (metadata only — secret values are never returned by any API)
    "secrets:read": "View vault secret metadata",
    "secrets:write": "Create, update, and delete vault secrets",

    # Notifications
    "notifications:read": "View notification configurations",
    "notifications:write": "Manage notification settings",

    # Organization
    "org:read": "View organization info and team members",

    # User profile
    "profile:read": "View user profile information",
}


def validate_scopes(requested: list[str]) -> list[str]:
    """Filter to only valid scope strings."""
    return [s for s in requested if s in OAUTH_SCOPES]


def parse_scope_string(scope_string: str) -> list[str]:
    """Parse space-separated scope string into list."""
    if not scope_string:
        return []
    return [s.strip() for s in scope_string.split() if s.strip()]


def scopes_to_scope_string(scopes: list[str]) -> str:
    """Convert scope list to space-separated string."""
    return " ".join(scopes)


def scopes_to_role(scopes: list[str]) -> str:
    """
    Map a set of OAuth scopes to the nearest RBAC role equivalent.

    This allows OAuth-authenticated requests to work with the existing
    role-based permission system without modification.

    Content scopes NEVER map to "admin": the admin role additionally implies
    api-keys / config / audit access that an OAuth token was never granted, so
    collapsing a set of `*:write` content scopes into "admin" silently escalates
    the token far beyond its grant. The highest role a content-scope token maps to
    is a bounded "operator"; true platform-admin comes only from the signed-JWT web
    console (is_platform_admin), never from OAuth scopes.
    """
    if not scopes:
        return "viewer"

    has_write = any(s.endswith(":write") or s.endswith(":execute") for s in scopes)
    return "operator" if has_write else "viewer"


def check_scope(granted_scopes: list[str], required_scope: str) -> bool:
    """
    Check if the required scope is satisfied by the granted scopes.

    Supports wildcard matching: 'targets:*' satisfies 'targets:read'.
    """
    if required_scope in granted_scopes:
        return True

    # Check for resource wildcard
    resource = required_scope.split(":")[0]
    if f"{resource}:*" in granted_scopes:
        return True

    return False


def get_scope_descriptions(scopes: list[str]) -> list[dict]:
    """Get human-readable descriptions for a list of scopes."""
    return [
        {"scope": s, "description": OAUTH_SCOPES.get(s, s)}
        for s in scopes
        if s in OAUTH_SCOPES
    ]
