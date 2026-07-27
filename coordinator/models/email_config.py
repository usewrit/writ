"""
Email (SMTP) configuration model.
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime
from database import Base


class EmailConfig(Base):
    """
    Email SMTP configuration table.

    Stores SMTP server settings for email notifications.
    """
    __tablename__ = "email_config"

    id = Column(Integer, primary_key=True, index=True)
    smtp_host = Column(String, nullable=False, comment="SMTP server hostname")
    smtp_port = Column(Integer, nullable=False, default=587, comment="SMTP server port")
    smtp_username = Column(String, nullable=False, comment="SMTP username")
    smtp_password = Column(String, nullable=False, comment="SMTP password (encrypted)")
    from_email = Column(String, nullable=False, comment="Sender email address")
    from_name = Column(String, nullable=False, default="Writ", comment="Sender display name")
    use_tls = Column(Boolean, nullable=False, default=True, comment="Use TLS/STARTTLS")
    enabled = Column(Boolean, nullable=False, default=False, comment="Enable email notifications")
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<EmailConfig(host='{self.smtp_host}', from='{self.from_email}', enabled={self.enabled})>"
