"""
Selector Extractor model - defines data extraction rules for selectors.

Extractors run on selector content and produce structured data that can be:
1. Stored with each change detection
2. Used in trigger conditions
3. Passed to actions (notifications, AI sessions, workflows)
"""
from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    Text,
    ForeignKey,
    JSON,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base


class SelectorExtractor(Base):
    r"""
    Defines how to extract structured data from a selector's content.

    Extraction types:
    - text: Extract text content (default)
    - attribute: Extract an HTML attribute (e.g., href, src)
    - regex: Extract using regex capture groups
    - css: Extract using a sub-CSS selector
    - json_path: Extract from JSON content

    Examples:
    - Extract price: type=regex, pattern=r'\$(\d+\.?\d*)', output_name='price'
    - Extract link URL: type=attribute, attribute='href', output_name='url'
    - Extract all links: type=css, sub_selector='a', attribute='href', output_name='links', is_array=True
    """
    __tablename__ = "selector_extractors"

    id = Column(Integer, primary_key=True, index=True)
    target_selector_id = Column(
        Integer,
        ForeignKey("target_selectors.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Selector this extractor belongs to"
    )

    # Extractor identification
    name = Column(
        String(100),
        nullable=False,
        comment="Human-readable name for this extractor"
    )
    output_name = Column(
        String(100),
        nullable=False,
        comment="Key name in extracted data object (e.g., 'price', 'url')"
    )
    enabled = Column(
        Boolean,
        default=True,
        comment="Whether this extractor is active"
    )

    # Extraction type and configuration
    extract_type = Column(
        String(50),
        nullable=False,
        default='text',
        comment="Type: text, attribute, regex, css, json_path"
    )

    # Type-specific configuration stored as JSON
    config = Column(
        JSON,
        nullable=True,
        server_default='{}',
        comment="""Type-specific config:
        - text: {} (just get text content)
        - attribute: {"attribute": "href"}
        - regex: {"pattern": "\\$(\\d+)", "group": 1}
        - css: {"sub_selector": "a.link", "attribute": "href"}
        - json_path: {"path": "$.items[0].price"}
        """
    )

    # Output configuration
    is_array = Column(
        Boolean,
        default=False,
        comment="If True, extraction returns array of all matches"
    )
    default_value = Column(
        Text,
        nullable=True,
        comment="Default value if extraction fails or returns empty"
    )

    # Relationships
    target_selector = relationship(
        "TargetSelector",
        back_populates="extractors"
    )

    def __repr__(self) -> str:
        return f"<SelectorExtractor(id={self.id}, name='{self.name}', type='{self.extract_type}')>"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "target_selector_id": self.target_selector_id,
            "name": self.name,
            "output_name": self.output_name,
            "enabled": self.enabled,
            "extract_type": self.extract_type,
            "config": self.config,
            "is_array": self.is_array,
            "default_value": self.default_value,
        }
