"""
Pushover Configuration Model - stores Pushover API credentials.
"""
from sqlalchemy import Column, Integer, String, Boolean, DateTime
from datetime import datetime

from database import Base


class PushoverConfig(Base):
    """
    Pushover configuration table.

    Stores Pushover API credentials for notifications.
    Only one config record should exist (singleton pattern).
    """
    __tablename__ = "pushover_config"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)

    # Pushover credentials
    app_token = Column(
        String(255),
        nullable=False,
        comment="Pushover application API token"
    )

    # Note: We store a user_key here for backward compatibility,
    # but individual recipients are stored in pushover_recipients table
    user_key = Column(
        String(255),
        nullable=True,
        comment="Default Pushover user/group key (deprecated - use recipients)"
    )

    # Status
    enabled = Column(
        Boolean,
        nullable=False,
        default=True,
        comment="Whether Pushover notifications are enabled"
    )

    # Notification settings
    notification_title = Column(
        String(250),
        nullable=False,
        default="Writ: Change Detected",
        comment="Default notification title"
    )

    notification_message = Column(
        String(1024),
        nullable=True,
        comment="Default notification message body (supports placeholders: {url}, {target_id})"
    )

    notification_priority = Column(
        Integer,
        nullable=False,
        default=1,
        comment="Default priority: -2 (no alert), -1 (quiet), 0 (normal), 1 (high), 2 (emergency)"
    )

    notification_sound = Column(
        String(50),
        nullable=False,
        default="pushover",
        comment="Default notification sound"
    )

    url_title = Column(
        String(100),
        nullable=False,
        default="View Page",
        comment="Default URL button title"
    )

    html_enabled = Column(
        Boolean,
        nullable=False,
        default=True,
        comment="Whether to use HTML formatting in notifications"
    )

    # Metadata
    created_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        comment="When this config was created"
    )

    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        comment="When this config was last updated"
    )

    def __repr__(self):
        return f"<PushoverConfig(id={self.id}, enabled={self.enabled})>"
