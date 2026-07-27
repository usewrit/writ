"""
OAuth 2.0 models for third-party integrations.

Supports Authorization Code flow with PKCE for Zapier, n8n, Make, etc.
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
    Uuid,
    JSON,
)
from database import Base


class OAuthApplication(Base):
    """
    Registered OAuth applications (third-party integrations).

    Each application gets a client_id and client_secret for the
    OAuth 2.0 Authorization Code flow.
    """
    __tablename__ = "oauth_applications"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"),
                     nullable=True)
    name = Column(String(255), nullable=False)
    client_id = Column(String(64), unique=True, nullable=False, index=True)
    client_secret_hash = Column(String(255), nullable=False)
    redirect_uris = Column(JSON, nullable=False, default=list)
    scopes = Column(JSON, nullable=False, default=list)
    description = Column(Text, nullable=True)
    logo_url = Column(String(500), nullable=True)
    is_confidential = Column(Boolean, default=True, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=True, onupdate=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "client_id": self.client_id,
            "redirect_uris": self.redirect_uris or [],
            "scopes": self.scopes or [],
            "description": self.description,
            "logo_url": self.logo_url,
            "is_confidential": self.is_confidential,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class OAuthAuthorizationCode(Base):
    """
    Short-lived authorization codes for the OAuth code exchange flow.
    Single-use, expires in 10 minutes per RFC 6749.
    """
    __tablename__ = "oauth_authorization_codes"

    id = Column(Integer, primary_key=True)
    code = Column(String(128), unique=True, nullable=False, index=True)
    client_id = Column(String(64), ForeignKey("oauth_applications.client_id", ondelete="CASCADE"),
                       nullable=False)
    user_id = Column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False)
    redirect_uri = Column(String(2048), nullable=False)
    scopes = Column(JSON, nullable=False, default=list)
    code_challenge = Column(String(128), nullable=True)
    code_challenge_method = Column(String(10), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    used = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)


class OAuthAccessToken(Base):
    """
    OAuth access and refresh tokens issued to third-party applications.

    Tokens are Argon2 hashed at rest. A token_prefix (first 8 chars of SHA-256)
    enables O(1) lookup instead of scanning all tokens.
    """
    __tablename__ = "oauth_access_tokens"

    id = Column(Integer, primary_key=True)
    token_prefix = Column(String(16), nullable=False, index=True)
    token_hash = Column(String(255), unique=True, nullable=False)
    refresh_token_prefix = Column(String(16), nullable=True, index=True)
    refresh_token_hash = Column(String(255), unique=True, nullable=True)
    client_id = Column(String(64), ForeignKey("oauth_applications.client_id", ondelete="CASCADE"),
                       nullable=False, index=True)
    user_id = Column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False)
    scopes = Column(JSON, nullable=False, default=list)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    refresh_expires_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    # For device-flow (writ-agent) tokens: the recorder agent_id this token is
    # bound to, stamped by /internal/agent/connected once the agent connects and
    # carried forward across refresh rotations. Enables per-machine revocation —
    # disconnecting one agent revokes only its token, not the user's other agents.
    agent_id = Column(String(255), nullable=True, index=True)

    __table_args__ = (
        Index("ix_oauth_tokens_client_user", "client_id", "user_id"),
    )

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "client_id": self.client_id,
            "scopes": self.scopes or [],
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
        }
