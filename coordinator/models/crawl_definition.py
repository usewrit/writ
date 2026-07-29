"""
CrawlDefinition — a SAVED, re-runnable crawl configuration.

A CrawlJob is one *run*: its config lives on the row and its id dies with that
run, so a crawl had no stable handle and could not be exposed as an API the way
a workflow can — the URL would change on every re-crawl. This table is that
handle. It owns the settings, carries a slug, and every CrawlJob it launches
points back at it (``CrawlJob.definition_id``), so runs become its history.

That history is what makes ``max_age`` answerable: "has this saved crawl
completed recently enough that the caller can just have the data?"

Single-owner coordinator: no tenant column, exactly like ``crawl_jobs``. The
slug is globally unique here because there is only one operator.

The config is ONE JSON blob rather than a column mirror — it is literally a
validated ``StartCrawlRequest`` payload, so it round-trips through the same
pydantic model the live endpoint uses. One validation path, and a new crawl
option cannot silently go missing.
"""
from datetime import datetime

from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    Text,
    JSON,
)

from database import Base


class CrawlDefinition(Base):
    __tablename__ = "crawl_definitions"

    id = Column(Integer, primary_key=True, index=True)

    name = Column(String(200), nullable=False, comment="Human label, e.g. 'Docs — example.com'")
    slug = Column(String(120), nullable=False, unique=True, index=True,
                  comment="URL-safe stable ref used by the callable endpoint")
    description = Column(Text, nullable=True)

    config = Column(JSON, nullable=False,
                    comment="Validated StartCrawlRequest payload — the saved crawl settings")
    seed_url = Column(Text, nullable=False, comment="Seed URL, mirrored from config for listing")

    # Applied when a caller omits max_age. NULL = no default, i.e. an unqualified
    # call always re-crawls — the safe behavior for a caller who never opted in.
    default_max_age_seconds = Column(Integer, nullable=True,
                                     comment="Fallback freshness (seconds) when the caller omits max_age")

    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime(timezone=True), nullable=True, onupdate=datetime.utcnow)
    last_run_at = Column(DateTime(timezone=True), nullable=True,
                         comment="When this definition last dispatched a crawl (any outcome)")

    def __repr__(self) -> str:
        return f"<CrawlDefinition(id={self.id}, slug='{self.slug}', seed='{self.seed_url}')>"

    def summary(self) -> dict:
        return {
            "id": self.id,
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "seed_url": self.seed_url,
            "config": self.config or {},
            "default_max_age_seconds": self.default_max_age_seconds,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
        }
