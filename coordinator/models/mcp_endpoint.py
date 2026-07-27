"""
MCP Endpoint model — represents a user-configured MCP server.

Each endpoint exposes selected workflows as MCP tools. When an MCP client
connects, the server lists available tools (derived from workflow handlers,
extract fields, input variables) and routes tool calls to the appropriate
workflow or streaming session.
"""
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)

from database import Base


class McpEndpoint(Base):
    __tablename__ = "mcp_endpoints"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Identity
    name = Column(String(255), nullable=False, comment="Human-readable endpoint name")
    slug = Column(
        String(100),
        nullable=False,
        unique=True,
        index=True,
        comment="URL path component (e.g. 'my-tools' → /mcp/my-tools)",
    )
    description = Column(Text, nullable=True, comment="Server description shown to MCP clients")

    # Tool configuration — which workflows are exposed
    # [{
    #   "workflow_id": int,
    #   "tool_name": str,          — MCP tool name (alphanumeric + underscore)
    #   "tool_description": str,   — shown to the AI model
    #   "input_schema": dict,      — JSON Schema for tool input (auto-derived or custom)
    #   "auto_start": bool,        — auto-start workflow if not running (default false)
    #   "handler_name": str|null,  — streaming handler to invoke (null = default)
    #   "timeout_seconds": int,    — max wait for tool result (default 30)
    # }]
    tools_config = Column(
        JSON,
        nullable=False,
        default=[],
        comment="Array of workflow tool configurations",
    )

    # Server capabilities
    server_version = Column(String(20), nullable=False, default="1.0.0")
    capabilities = Column(
        JSON,
        nullable=True,
        default={},
        comment="MCP server capabilities (tools, resources, prompts, logging)",
    )

    # Auth — link to an API key for bearer auth
    api_key_id = Column(
        Integer,
        ForeignKey("api_keys.id"),
        nullable=True,
        comment="API key required to connect (null = open, NOT recommended)",
    )

    # Settings
    enabled = Column(Boolean, nullable=False, default=True)
    auto_start_sessions = Column(
        Boolean,
        nullable=False,
        default=False,
        comment="Auto-start streaming sessions on first MCP tool call if workflow not running",
    )

    # Timestamps
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=True,
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "slug": self.slug,
            "description": self.description,
            "tools_config": self.tools_config or [],
            "server_version": self.server_version,
            "capabilities": self.capabilities or {},
            "api_key_id": self.api_key_id,
            "enabled": self.enabled,
            "auto_start_sessions": self.auto_start_sessions,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
