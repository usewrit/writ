"""
API routers for the platform.
"""
from .auth import router as auth_router
from .agents import router as agents_router
from .notifications import router as notifications_router

__all__ = [
    "auth_router",
    "agents_router",
    "notifications_router",
]
