"""
MailConnection — a connected IMAP mailbox used to read email-OTP / magic-links
during a Persona run.

Self-host is IMAP-only: there is no OAuth mailbox (Gmail/Outlook) flow and no
inbound forwarding relay on the coordinator, so a persona's email-OTP is read
directly from the user's own IMAP mailbox (app password / self-hosted mail). One
mailbox can back multiple personas.

The coordinator is single-owner, so this table is NOT tenant-scoped. The IMAP
app password is Fernet-encrypted (PersonaService.encrypt) and is NEVER returned
via the API.
"""
from sqlalchemy import (
    Column, String, Text, Integer, Boolean, DateTime, UniqueConstraint,
)
from sqlalchemy.sql import func

from database import Base


class MailConnection(Base):
    __tablename__ = "mail_connections"

    id = Column(Integer, primary_key=True, autoincrement=True)

    provider = Column(
        String(50), nullable=False, server_default="imap",
        comment="Always 'imap' on self-host (no OAuth mailbox providers).",
    )
    email = Column(String(320), nullable=False, comment="Connected mailbox address")

    # IMAP (provider == 'imap') — app password stored Fernet-encrypted, never
    # returned via the API.
    imap_host = Column(String(255), nullable=True)
    imap_port = Column(Integer, nullable=True)
    imap_username = Column(String(320), nullable=True)
    imap_password_encrypted = Column(Text, nullable=True)
    imap_use_ssl = Column(Boolean, nullable=False, server_default="true")
    imap_mailbox = Column(String(120), nullable=True, comment="Folder to search, default INBOX")

    is_active = Column(String(10), nullable=False, server_default="active", comment="active | revoked")

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    last_used_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # A mailbox is identified by (provider, email); a persona pointing at an
        # already-connected mailbox reuses this row rather than inserting a dup.
        UniqueConstraint("provider", "email", name="uq_mail_connection_provider_email"),
    )

    def __repr__(self) -> str:
        return f"<MailConnection(id={self.id}, provider='{self.provider}', email='{self.email}')>"
