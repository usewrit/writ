"""
Config model - key-value store for system configuration.
"""
from sqlalchemy import Column, String, JSON
from database import Base


class Config(Base):
    """
    Config table - flexible key-value configuration store.

    Stores runtime configuration that can be updated without redeployment.
    Values are stored as JSON for flexibility.
    """
    __tablename__ = "config"

    key = Column(
        String(255),
        primary_key=True,
        comment="Configuration key"
    )
    value = Column(
        JSON,
        nullable=False,
        comment="Configuration value (JSON)"
    )

    def __repr__(self) -> str:
        return f"<Config(key='{self.key}')>"

    def to_dict(self) -> dict:
        """Convert config to dictionary representation."""
        return {
            "key": self.key,
            "value": self.value,
        }
