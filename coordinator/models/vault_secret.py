"""
Vault Secret — encrypted key/value entry in the built-in secrets vault.
"""
from sqlalchemy import (
    Column, String, Text, Integer, DateTime, ForeignKey,
    UniqueConstraint, Index,
)
from sqlalchemy.sql import func
from database import Base


class VaultSecret(Base):
    __tablename__ = "vault_secrets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(255), nullable=False, comment="Secret name (unique)")
    value_encrypted = Column(Text, nullable=False, comment="Fernet-encrypted value")
    description = Column(Text, nullable=True)
    category = Column(String(50), nullable=True, comment="Optional grouping: credentials, api_keys, tokens")

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    use_count = Column(Integer, default=0)

    __table_args__ = (
        UniqueConstraint("key", name="uq_vault_secret_key"),
        Index("ix_vault_secrets_key", "key"),
    )
