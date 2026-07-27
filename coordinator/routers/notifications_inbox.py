"""
In-app notification inbox router.

CRUD over the `notifications` table backing the frontend bell + inbox. Distinct
from routers/notifications.py (which configures notification PROVIDERS for
target-change alerts) — this is the user-facing message inbox.

Endpoints (prefix "/inbox"):
  GET  /inbox                 paginated list (newest first)
  GET  /inbox/unread-count    unread badge count
  POST /inbox/{id}/read       mark one read (idempotent)
  POST /inbox/read-all        mark all read
  DELETE /inbox/{id}          dismiss (permanently delete) one
"""
import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from security.dependencies import get_auth_context, AuthContext
from services import notification_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/inbox", tags=["Inbox"])


# ============================================================================
# Response models
# ============================================================================

class NotificationItem(BaseModel):
    id: str
    type: str
    title: str
    body: Optional[str] = None
    link: Optional[str] = None
    payload: Optional[dict] = None
    read_at: Optional[str] = None
    created_at: Optional[str] = None


class NotificationListResponse(BaseModel):
    items: list[NotificationItem]
    limit: int
    offset: int


class UnreadCountResponse(BaseModel):
    unread: int


def _serialize(n) -> NotificationItem:
    return NotificationItem(
        id=str(n.id),
        type=n.type,
        title=n.title,
        body=n.body,
        link=n.link,
        payload=n.payload,
        read_at=n.read_at.isoformat() if n.read_at else None,
        created_at=n.created_at.isoformat() if n.created_at else None,
    )


# ============================================================================
# Endpoints
# ============================================================================

@router.get("", response_model=NotificationListResponse)
async def list_inbox(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    unread_only: bool = Query(False),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """List the owner's notifications, newest first."""
    notifications = await notification_service.list_notifications(
        db,
        user_id=auth.user_id,
        limit=limit,
        offset=offset,
        unread_only=unread_only,
    )
    return NotificationListResponse(
        items=[_serialize(n) for n in notifications],
        limit=limit,
        offset=offset,
    )


@router.get("/unread-count", response_model=UnreadCountResponse)
async def unread_count(
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Unread notification count for the bell badge."""
    count = await notification_service.get_unread_count(
        db, user_id=auth.user_id
    )
    return UnreadCountResponse(unread=count)


@router.post("/{notification_id}/read")
async def mark_read(
    notification_id: UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Mark a single notification read (idempotent)."""
    ok = await notification_service.mark_read(
        db,
        notification_id=notification_id,
        user_id=auth.user_id,
    )
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")
    await db.commit()
    return {"success": True}


@router.post("/read-all")
async def mark_all_read(
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Mark all of the owner's notifications read."""
    updated = await notification_service.mark_all_read(
        db, user_id=auth.user_id
    )
    await db.commit()
    return {"success": True, "updated": updated}


@router.delete("/{notification_id}")
async def dismiss(
    notification_id: UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Dismiss (permanently delete) a single notification."""
    ok = await notification_service.delete_notification(
        db,
        notification_id=notification_id,
        user_id=auth.user_id,
    )
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")
    await db.commit()
    return {"success": True}
