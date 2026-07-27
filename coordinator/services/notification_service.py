"""
Notification service — in-app notification primitive.

A thin, best-effort layer over the `notifications` table. `create_notification`
NEVER raises: a notification is always a side-effect of some primary action
(review moderated, payout paid, run failed, ...) and must not break that action
if the insert fails. Read helpers (unread count, list, mark read) are owner-scoped
and meant to back the inbox API (routers/notifications_inbox.py).

NotificationType string values (canonical — keep in sync with the contract):
  review_approved, review_rejected, payout_paid, payout_failed, refund_issued,
  clawback_applied, run_failed, low_balance, update_available, account_security,
  workflow_removed
"""
import logging
from typing import Optional
from uuid import UUID

from sqlalchemy import select, func, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from models.notification import Notification

logger = logging.getLogger(__name__)


async def create_notification(
    db: AsyncSession,
    *,
    type: str,
    title: str,
    body: Optional[str] = None,
    user_id: Optional[UUID] = None,
    link: Optional[str] = None,
    payload: Optional[dict] = None,
) -> Optional[Notification]:
    """Create one in-app notification. Best-effort — never raises.

    Returns the created Notification, or None if the insert failed (the caller's
    primary action must proceed regardless). The single-user coordinator's
    Notification table has no organization column — only an optional ``user_id``.
    """
    try:
        notification = Notification(
            user_id=user_id,
            type=type,
            title=title,
            body=body,
            link=link,
            payload=payload,
        )
        db.add(notification)
        await db.flush()
        return notification
    except Exception as e:  # pragma: no cover - defensive, must not break caller
        logger.warning(f"create_notification failed (type={type}): {e}")
        return None


async def get_unread_count(
    db: AsyncSession,
    *,
    user_id: Optional[UUID] = None,
) -> int:
    """Count unread notifications (optionally a specific user).

    User-scoped notifications (user_id set) are only counted for that user;
    owner-wide notifications (user_id NULL) count for the single owner.
    """
    stmt = select(func.count(Notification.id)).where(
        Notification.read_at.is_(None),
    )
    if user_id is not None:
        stmt = stmt.where(
            (Notification.user_id == user_id) | (Notification.user_id.is_(None))
        )
    result = await db.execute(stmt)
    return int(result.scalar() or 0)


async def list_notifications(
    db: AsyncSession,
    *,
    user_id: Optional[UUID] = None,
    limit: int = 20,
    offset: int = 0,
    unread_only: bool = False,
) -> list[Notification]:
    """List notifications, newest first, paginated."""
    stmt = select(Notification)
    if user_id is not None:
        stmt = stmt.where(
            (Notification.user_id == user_id) | (Notification.user_id.is_(None))
        )
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    stmt = stmt.order_by(Notification.created_at.desc()).limit(limit).offset(offset)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def mark_read(
    db: AsyncSession,
    *,
    notification_id: UUID,
    user_id: Optional[UUID] = None,
) -> bool:
    """Mark a single notification read. Returns False if not found. Idempotent."""
    stmt = select(Notification).where(
        Notification.id == notification_id,
    )
    if user_id is not None:
        stmt = stmt.where(
            (Notification.user_id == user_id) | (Notification.user_id.is_(None))
        )
    result = await db.execute(stmt)
    notification = result.scalar_one_or_none()
    if notification is None:
        return False
    if notification.read_at is None:
        notification.read_at = func.now()
        await db.flush()
    return True


async def mark_all_read(
    db: AsyncSession,
    *,
    user_id: Optional[UUID] = None,
) -> int:
    """Mark all unread notifications read. Returns the count updated."""
    stmt = (
        update(Notification)
        .where(
            Notification.read_at.is_(None),
        )
        .values(read_at=func.now())
    )
    if user_id is not None:
        stmt = stmt.where(
            (Notification.user_id == user_id) | (Notification.user_id.is_(None))
        )
    result = await db.execute(stmt)
    await db.flush()
    return int(result.rowcount or 0)


async def delete_notification(
    db: AsyncSession,
    *,
    notification_id: UUID,
    user_id: Optional[UUID] = None,
) -> bool:
    """Permanently delete a single notification (the inbox "dismiss" action).

    Returns False if the notification does not exist. Idempotent from the
    caller's view: a second dismiss of the same id just returns False.
    """
    stmt = delete(Notification).where(
        Notification.id == notification_id,
    )
    if user_id is not None:
        stmt = stmt.where(
            (Notification.user_id == user_id) | (Notification.user_id.is_(None))
        )
    result = await db.execute(stmt)
    await db.flush()
    return int(result.rowcount or 0) > 0
