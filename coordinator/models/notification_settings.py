"""
Notification Settings model - global notification behavior configuration.
"""
from datetime import datetime
from sqlalchemy import Column, Integer, Boolean, String, DateTime, ForeignKey
from database import Base


class NotificationSettings(Base):
    """
    Notification Settings table - stores notification trigger settings.

    A single global row (id == 1) for the single-owner coordinator; its mutation
    is gated to the admin owner.
    """
    __tablename__ = "notification_settings"

    id = Column(Integer, primary_key=True, index=True)

    # Quiet Hours
    quiet_hours_enabled = Column(Boolean, default=False, nullable=False)
    quiet_hours_start = Column(String(5), nullable=True)  # HH:MM format
    quiet_hours_end = Column(String(5), nullable=True)    # HH:MM format

    # Rate Limiting
    rate_limiting_enabled = Column(Boolean, default=False, nullable=False)
    rate_limit_max = Column(Integer, default=5, nullable=False)
    rate_limit_period = Column(String(10), default='hour', nullable=False)  # 'hour', 'day', 'week'

    # Batch Notifications
    batch_enabled = Column(Boolean, default=False, nullable=False)
    batch_window_minutes = Column(Integer, default=5, nullable=False)

    # Agent Error Alerts
    agent_errors_enabled = Column(Boolean, default=False, nullable=False)
    agent_error_threshold = Column(Integer, default=3, nullable=False)
    agent_error_delay_minutes = Column(Integer, default=15, nullable=False)

    # Target Health Monitoring
    target_health_enabled = Column(Boolean, default=False, nullable=False)
    target_health_threshold = Column(Integer, default=5, nullable=False)
    target_health_check_interval = Column(Integer, default=10, nullable=False)

    # System Health
    system_health_enabled = Column(Boolean, default=False, nullable=False)
    system_health_agent_disconnect_delay = Column(Integer, default=5, nullable=False)

    # Metadata
    updated_at = Column(DateTime(timezone=True), nullable=True, onupdate=datetime.utcnow)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<NotificationSettings(id={self.id})>"
