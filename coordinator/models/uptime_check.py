"""
UptimeCheck model - stores uptime monitoring history.
"""
from datetime import datetime
from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    Text,
    ForeignKey,
    Index,
)
from sqlalchemy.orm import relationship
from database import Base


class UptimeCheck(Base):
    """
    Uptime Check table - stores uptime monitoring results.

    Each record represents a single uptime check performed by an agent,
    tracking availability, response time, and HTTP status codes.
    """
    __tablename__ = "uptime_checks"

    id = Column(Integer, primary_key=True, index=True)

    target_id = Column(
        Integer,
        ForeignKey("targets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Target being monitored"
    )

    agent_id = Column(
        String(255),
        nullable=False,
        index=True,
        comment="Agent that performed the check"
    )

    checked_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        index=True,
        comment="When the check was performed"
    )

    is_up = Column(
        Boolean,
        nullable=False,
        index=True,
        comment="Whether the site is up (True) or down (False)"
    )

    status_code = Column(
        Integer,
        nullable=True,
        comment="HTTP status code received (null if connection failed)"
    )

    response_time_ms = Column(
        Integer,
        nullable=True,
        comment="Response time in milliseconds (null if connection failed)"
    )

    error_message = Column(
        Text,
        nullable=True,
        comment="Error message if check failed"
    )

    dns_time_ms = Column(
        Integer,
        nullable=True,
        comment="DNS resolution time in milliseconds"
    )

    connect_time_ms = Column(
        Integer,
        nullable=True,
        comment="TCP connection time in milliseconds"
    )

    tls_time_ms = Column(
        Integer,
        nullable=True,
        comment="TLS handshake time in milliseconds (if HTTPS)"
    )

    # SSL Certificate information
    ssl_cert_valid = Column(
        Boolean,
        nullable=True,
        comment="Whether SSL certificate is valid"
    )

    ssl_cert_expires_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="SSL certificate expiration date"
    )

    ssl_cert_days_until_expiry = Column(
        Integer,
        nullable=True,
        comment="Days until SSL certificate expires"
    )

    ssl_cert_issuer = Column(
        String(500),
        nullable=True,
        comment="SSL certificate issuer"
    )

    ssl_cert_subject = Column(
        String(500),
        nullable=True,
        comment="SSL certificate subject (domain)"
    )

    ssl_error = Column(
        Text,
        nullable=True,
        comment="SSL certificate validation error (if any)"
    )

    # Relationships
    target = relationship("Target", foreign_keys=[target_id])

    # Indexes for efficient queries
    __table_args__ = (
        Index("ix_uptime_target_checked", "target_id", "checked_at"),
        Index("ix_uptime_target_status", "target_id", "is_up"),
        Index("ix_uptime_agent_checked", "agent_id", "checked_at"),
    )

    def __repr__(self) -> str:
        status = "UP" if self.is_up else "DOWN"
        return f"<UptimeCheck(target_id={self.target_id}, status={status}, checked_at={self.checked_at})>"

    def to_dict(self) -> dict:
        """Convert uptime check to dictionary representation."""
        return {
            "id": self.id,
            "target_id": self.target_id,
            "agent_id": self.agent_id,
            "checked_at": self.checked_at.isoformat(),
            "is_up": self.is_up,
            "status_code": self.status_code,
            "response_time_ms": self.response_time_ms,
            "error_message": self.error_message,
            "dns_time_ms": self.dns_time_ms,
            "connect_time_ms": self.connect_time_ms,
            "tls_time_ms": self.tls_time_ms,
            "ssl_cert_valid": self.ssl_cert_valid,
            "ssl_cert_expires_at": self.ssl_cert_expires_at.isoformat() if self.ssl_cert_expires_at else None,
            "ssl_cert_days_until_expiry": self.ssl_cert_days_until_expiry,
            "ssl_cert_issuer": self.ssl_cert_issuer,
            "ssl_cert_subject": self.ssl_cert_subject,
            "ssl_error": self.ssl_error,
        }
