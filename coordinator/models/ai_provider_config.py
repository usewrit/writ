"""
AI Provider configuration — stores LLM provider settings for the coordinator.

Single-owner coordinator: configs are global. ``priority`` orders fallback.
"""
import uuid
from datetime import datetime
from sqlalchemy import Column, String, Boolean, Integer, DateTime, ForeignKey, Text
from sqlalchemy.sql import func
from database import Base


class AIProviderConfig(Base):
    __tablename__ = "ai_provider_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)

    provider = Column(
        String(50), nullable=False,
        comment="anthropic, openai, openrouter, local"
    )
    api_key_encrypted = Column(
        Text, nullable=True,
        comment="Encrypted API key (Fernet)"
    )
    base_url = Column(
        String(500), nullable=True,
        comment="Custom base URL for OpenRouter/local LLM"
    )
    model = Column(
        String(200), nullable=True,
        comment="Default model override (e.g. claude-sonnet-4-20250514)"
    )

    is_active = Column(Boolean, nullable=False, default=True)
    priority = Column(
        Integer, nullable=False, default=0,
        comment="Lower = higher priority. Used for fallback ordering."
    )

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
