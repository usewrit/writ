"""
User model - individual user accounts for the SaaS platform.
"""
import uuid
from datetime import datetime
from sqlalchemy import Column, String, Boolean, DateTime, Text, BigInteger, Uuid
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base


class User(Base):
    """User account for the platform."""
    __tablename__ = "users"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=True, comment="Argon2 hash (null for social-only accounts)")
    name = Column(String(255), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    is_verified = Column(Boolean, default=False, nullable=False)
    is_platform_admin = Column(Boolean, default=False, nullable=False,
                               comment="Platform-level admin (owner)")

    # Suspension
    suspended_at = Column(DateTime(timezone=True), nullable=True,
                          comment="When the account was suspended (null = not suspended)")
    suspended_reason = Column(Text, nullable=True,
                              comment="Admin-provided reason for suspension")
    suspended_by = Column(Uuid(as_uuid=True), nullable=True,
                          comment="Admin user who performed the suspension")

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_login_at = Column(DateTime(timezone=True), nullable=True)

    # Email verification
    verification_token = Column(String(255), nullable=True)
    verification_token_expires = Column(DateTime(timezone=True), nullable=True)
    reset_token = Column(String(255), nullable=True)
    reset_token_expires = Column(DateTime(timezone=True), nullable=True)

    # MFA (TOTP) — opt-in. Secret + recovery codes are Fernet-encrypted at rest.
    mfa_enabled = Column(Boolean, default=False, nullable=False,
                         comment="True once the user has confirmed TOTP enrollment")
    mfa_secret_encrypted = Column(Text, nullable=True,
                                  comment="Fernet-encrypted TOTP base32 secret (pending until mfa_enabled)")
    mfa_recovery_codes_encrypted = Column(Text, nullable=True,
                                          comment="Fernet-encrypted JSON list of sha256-hashed recovery codes")
    mfa_enrolled_at = Column(DateTime(timezone=True), nullable=True)
    # Replay protection that survives a Redis outage: the highest TOTP time-step
    # counter (unix_time // 30) that has been successfully consumed. A code is
    # rejected if its counter is <= this watermark, so a code can never be reused
    # even while the Redis replay cache is unavailable (defense in depth).
    mfa_last_used_counter = Column(BigInteger, nullable=True,
                                   comment="Highest consumed TOTP time-step (unix//30); DB-backed replay guard")

    # Legal consent. Captured at signup, re-prompted on version bump.
    terms_accepted_at = Column(DateTime(timezone=True), nullable=True,
                               comment="When the user accepted the Terms of Service")
    terms_version = Column(String(20), nullable=True,
                           comment="ToS version string accepted (Settings.terms_version)")
    privacy_version = Column(String(20), nullable=True,
                            comment="Privacy Policy version string accepted (Settings.privacy_version)")
    age_confirmed_at = Column(DateTime(timezone=True), nullable=True,
                              comment="When the user affirmed they meet the minimum age (16+)")
    marketing_consent = Column(Boolean, default=False, server_default="false", nullable=False,
                               comment="Opt-in for marketing email (separate from transactional)")
    marketing_consent_at = Column(DateTime(timezone=True), nullable=True,
                                  comment="When marketing_consent was last set")

    # Self-serve email change. Pending until the new address is verified.
    pending_email = Column(String(255), nullable=True,
                           comment="New email awaiting verification before it replaces email")
    email_change_token = Column(String(255), nullable=True,
                                comment="Token sent to pending_email for confirmation")
    email_change_token_expires = Column(DateTime(timezone=True), nullable=True)

    # The coordinator has a single owner and no organization membership.

    def __repr__(self):
        return f"<User(email='{self.email}', name='{self.name}')>"

    @property
    def is_suspended(self) -> bool:
        return self.suspended_at is not None

    def to_dict(self):
        return {
            "id": str(self.id),
            "email": self.email,
            "name": self.name,
            "is_active": self.is_active,
            "is_verified": self.is_verified,
            "is_platform_admin": self.is_platform_admin,
            "is_suspended": self.is_suspended,
            "suspended_at": self.suspended_at.isoformat() if self.suspended_at else None,
            "suspended_reason": self.suspended_reason,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
            "mfa_enabled": bool(self.mfa_enabled),
            "marketing_consent": bool(self.marketing_consent),
            "terms_accepted_at": self.terms_accepted_at.isoformat() if self.terms_accepted_at else None,
        }
