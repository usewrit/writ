"""
Role-Based Access Control (RBAC) utilities.
Defines permissions for different API key roles.
"""
import logging
from typing import Callable, List
from functools import wraps
from fastapi import Depends, HTTPException, status
from security.api_key import get_current_api_key

logger = logging.getLogger(__name__)

# Role hierarchy: admin > operator > viewer
ROLE_HIERARCHY = {
    "viewer": 0,
    "operator": 1,
    "admin": 2,
}

# Permission matrix: defines what each role can do
PERMISSIONS = {
    "viewer": [
        "targets:read",
        "reports:read",
        "agents:read",
        "config:read",
        "health:read",
    ],
    "operator": [
        "targets:read",
        "targets:create",
        "targets:update",
        "targets:delete",
        "reports:read",
        "agents:read",
        "config:read",
        "schedule:read",
        "notifications:test",
        "health:read",
    ],
    "admin": [
        "targets:*",
        "reports:*",
        "agents:*",
        "config:*",
        "schedule:*",
        "notifications:*",
        "api_keys:*",
        "health:*",
        "audit:*",
    ],
}


def check_permission(role: str, permission: str) -> bool:
    """
    Check if a role has a specific permission.

    Args:
        role: Role name (viewer, operator, admin)
        permission: Permission string (e.g., "targets:create")

    Returns:
        True if role has permission, False otherwise

    Example:
        >>> check_permission("viewer", "targets:read")
        True
        >>> check_permission("viewer", "targets:create")
        False
        >>> check_permission("admin", "targets:create")
        True
    """
    role = role.lower()

    if role not in PERMISSIONS:
        logger.warning(f"Unknown role: {role}")
        return False

    role_permissions = PERMISSIONS[role]

    # Check for wildcard permission (admin has *:*)
    if "*:*" in role_permissions:
        return True

    # Check for resource wildcard (e.g., "targets:*")
    resource = permission.split(":")[0]
    if f"{resource}:*" in role_permissions:
        return True

    # Check for exact permission match
    return permission in role_permissions


def has_role_level(current_role: str, required_role: str) -> bool:
    """
    Check if current role meets or exceeds required role level.

    Args:
        current_role: Current user's role
        required_role: Minimum required role

    Returns:
        True if current role level >= required role level

    Example:
        >>> has_role_level("admin", "viewer")
        True
        >>> has_role_level("viewer", "admin")
        False
    """
    current_level = ROLE_HIERARCHY.get(current_role.lower(), -1)
    required_level = ROLE_HIERARCHY.get(required_role.lower(), 999)
    return current_level >= required_level


def require_role(required_role: str):
    """
    Decorator to require a minimum role level for an endpoint.

    Args:
        required_role: Minimum role required (viewer, operator, admin)

    Returns:
        Decorator function

    Example:
        @app.post("/admin/config")
        @require_role("admin")
        async def update_config(api_key: dict = Depends(get_current_api_key)):
            return {"status": "updated"}

    Raises:
        HTTPException: 403 if user doesn't have required role
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Extract api_key from kwargs (injected by dependency)
            api_key = kwargs.get("api_key") or kwargs.get("current_api_key")

            if not api_key:
                logger.error("No API key found in request context")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Authentication required",
                )

            current_role = api_key.get("role", "").lower()

            if not has_role_level(current_role, required_role):
                logger.warning(
                    f"Permission denied: {api_key.get('label')} "
                    f"(role: {current_role}) attempted to access "
                    f"endpoint requiring {required_role}"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Requires {required_role} role or higher",
                )

            return await func(*args, **kwargs)

        return wrapper
    return decorator


def require_permission(required_permission: str):
    """
    Decorator to require a specific permission for an endpoint.

    Args:
        required_permission: Required permission (e.g., "targets:create")

    Returns:
        Decorator function

    Example:
        @app.post("/targets")
        @require_permission("targets:create")
        async def create_target(api_key: dict = Depends(get_current_api_key)):
            return {"status": "created"}

    Raises:
        HTTPException: 403 if user doesn't have required permission
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Extract api_key from kwargs
            api_key = kwargs.get("api_key") or kwargs.get("current_api_key")

            if not api_key:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Authentication required",
                )

            current_role = api_key.get("role", "").lower()

            if not check_permission(current_role, required_permission):
                logger.warning(
                    f"Permission denied: {api_key.get('label')} "
                    f"(role: {current_role}) lacks permission: {required_permission}"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Requires permission: {required_permission}",
                )

            return await func(*args, **kwargs)

        return wrapper
    return decorator


# FastAPI dependency-based RBAC (preferred over decorators)
def RequireRole(minimum_role: str):
    """FastAPI dependency that enforces minimum role level.

    Usage:
        @router.post("/admin-only")
        async def admin_endpoint(api_key: dict = Depends(RequireRole("admin"))):
            ...
    """
    async def dependency(api_key: dict = Depends(get_current_api_key)):
        user_role = api_key.get("role", "")
        if not has_role_level(user_role, minimum_role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires {minimum_role} role or higher. Current role: {user_role}"
            )
        return api_key
    return dependency


def RequirePermission(permission: str):
    """FastAPI dependency that enforces specific permission.

    Usage:
        @router.post("/targets")
        async def create_target(api_key: dict = Depends(RequirePermission("targets:create"))):
            ...
    """
    async def dependency(api_key: dict = Depends(get_current_api_key)):
        user_role = api_key.get("role", "")
        if not check_permission(user_role, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing permission: {permission}"
            )
        return api_key
    return dependency
