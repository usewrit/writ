"""
ScrapeResult model - stores historical scraping results.

Each row represents one scrape execution's extracted data.
For popular_times, this includes current_popularity, weekly histogram, rating, etc.
"""
from datetime import datetime
from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    Text,
    Boolean,
    ForeignKey,
    Index,
    JSON,
)
from sqlalchemy.orm import relationship
from database import Base


class ScrapeResult(Base):
    """
    ScrapeResult table - historical scraping results (time series).

    Each result is tied to a ScrapeJob and optionally to the AutomationTask
    that produced it. Schema of the `data` field varies by scrape_type.
    """
    __tablename__ = "scrape_results"

    id = Column(Integer, primary_key=True, index=True)
    scrape_job_id = Column(
        Integer,
        ForeignKey("scrape_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Parent scrape job"
    )
    task_id = Column(
        Integer,
        ForeignKey("automation_tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="The AutomationTask that produced this result"
    )

    # Extracted data (schema varies by scrape_type)
    # For popular_times:
    # {
    #   "place_name": "Starbucks",
    #   "place_address": "123 Main St, Montreal",
    #   "current_popularity": 67,
    #   "popular_times": [{day: 0, data: [0,0,...24 values]}, ...7 days],
    #   "time_spent": [15, 30],
    #   "rating": 4.2,
    #   "rating_count": 1234,
    #   "place_id": "ChIJ..."
    # }
    data = Column(
        JSON,
        nullable=False,
        comment="Extracted scraping data (schema varies by scrape_type)"
    )

    # Execution metadata
    executor_agent_id = Column(
        String(100),
        nullable=True,
        comment="Agent that executed this scrape"
    )
    executor_tier = Column(
        String(20),
        nullable=True,
        comment="Execution tier: worker, desktop_http, desktop_playwright, recorder"
    )
    duration_ms = Column(
        Integer,
        nullable=True,
        comment="Execution duration in milliseconds"
    )
    success = Column(
        Boolean,
        nullable=False,
        default=True,
        comment="Whether scraping succeeded"
    )
    error_message = Column(
        Text,
        nullable=True,
        comment="Error message if scraping failed"
    )

    # Timestamp
    scraped_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        index=True,
        comment="When the scrape was executed"
    )

    # Relationships
    scrape_job = relationship("ScrapeJob", back_populates="scrape_results")
    task = relationship("AutomationTask")

    __table_args__ = (
        Index('ix_scrape_results_job_time', 'scrape_job_id', 'scraped_at'),
    )

    def __repr__(self) -> str:
        return (
            f"<ScrapeResult(id={self.id}, job_id={self.scrape_job_id}, "
            f"tier='{self.executor_tier}', success={self.success})>"
        )
