"""
MCP Endpoints management router.

CRUD for MCP endpoint configuration. The actual MCP protocol is handled
by the mcp-service microservice — this router manages the configuration
and proxies the MCP connection URL to clients.
"""
import logging
import os
import re
import secrets
import unicodedata
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.mcp_endpoint import McpEndpoint
from security.dependencies import get_auth_context, AuthContext

logger = logging.getLogger(__name__)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _slugify(text: str, max_len: int = 100) -> str:
    """ascii-fold -> lower -> non-alnum runs to '-' -> trim."""
    s = (
        unicodedata.normalize("NFKD", text or "")
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    s = _NON_ALNUM.sub("-", s).strip("-")
    return s[:max_len].strip("-")


async def _insert_with_unique_slug(db, obj, field: str, source: str, max_len: int = 100):
    """Assign a URL-safe slug derived from ``source`` to ``obj.<field>`` and
    INSERT, retrying with a random suffix on the slug UNIQUE-index collision.

    Single-user coordinator: slug is globally unique. The DB UNIQUE index is
    the authoritative race guard.
    """
    base = _slugify(source, max_len=max_len) or "endpoint"
    candidate = base
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    for attempt in range(8):
        setattr(obj, field, candidate)
        db.add(obj)
        try:
            async with db.begin_nested():
                await db.flush()
            return obj
        except IntegrityError as exc:
            # Only retry on the slug unique index; re-raise anything else.
            if field not in str(getattr(exc, "orig", exc)) and "slug" not in str(
                getattr(exc, "orig", exc)
            ):
                raise
            suffix = "".join(secrets.choice(alphabet) for _ in range(6))
            room = max(1, max_len - (len(suffix) + 1))
            candidate = f"{base[:room].rstrip('-')}-{suffix}"
    # Final fallback: fully random slug.
    setattr(obj, field, "".join(secrets.choice(alphabet) for _ in range(12)))
    db.add(obj)
    await db.flush()
    return obj

router = APIRouter(prefix="/mcp-endpoints", tags=["MCP Endpoints"])

# Customer-facing read-only MCP overview (connection info, not config CRUD)
overview_router = APIRouter(prefix="/mcp", tags=["MCP Endpoints"])

MCP_SERVICE_URL = os.getenv("MCP_SERVICE_URL", "http://mcp-service:8084")


# ── Schemas ──────────────────────────────────────────────────────────────

class ToolConfig(BaseModel):
    workflow_id: int
    tool_name: str = Field(..., max_length=100, pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    tool_description: str = Field(default="", max_length=500)
    input_schema: Optional[dict] = None  # null → auto-derived from workflow
    handler_name: Optional[str] = None  # streaming: which handler to invoke
    auto_start: bool = False  # auto-start session on MCP call
    timeout_seconds: int = Field(default=30, ge=5, le=300)


class CreateMcpEndpoint(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    # slug is ALWAYS auto-derived from name (no user-custom slugs) — see
    # _insert_with_unique_slug below. A client-sent slug is ignored.
    description: Optional[str] = None
    tools_config: List[ToolConfig] = Field(default_factory=list)
    api_key_id: Optional[int] = None
    auto_start_sessions: bool = False
    server_version: str = "1.0.0"


class UpdateMcpEndpoint(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    tools_config: Optional[List[ToolConfig]] = None
    api_key_id: Optional[int] = None
    auto_start_sessions: Optional[bool] = None
    enabled: Optional[bool] = None
    server_version: Optional[str] = None


class McpToolInfo(BaseModel):
    """A workflow exposed as an MCP tool."""
    tool_name: str
    description: str
    workflow_id: int
    workflow_name: Optional[str] = None


class McpAuthInfo(BaseModel):
    """How MCP clients authenticate against this endpoint."""
    header: str = "Authorization"
    scheme: str = "Bearer"
    credential_type: str = "api_key"
    required_api_key_id: Optional[int] = None
    instructions: str


class McpServerOverview(BaseModel):
    """Customer-facing connection info for one MCP server."""
    id: int
    name: str
    slug: str
    description: Optional[str]
    enabled: bool
    connection_url: str
    transport: str = "streamable-http"
    auth: McpAuthInfo
    tools: List[McpToolInfo]


class McpOverviewResponse(BaseModel):
    servers: List[McpServerOverview]
    total: int


class McpEndpointResponse(BaseModel):
    id: int
    name: str
    slug: str
    description: Optional[str]
    tools_config: list
    api_key_id: Optional[int]
    enabled: bool
    auto_start_sessions: bool
    server_version: str
    connection_url: str  # The URL MCP clients use to connect
    created_at: Optional[str]
    updated_at: Optional[str]


# ── Routes ───────────────────────────────────────────────────────────────

async def _validate_api_key_id(db, api_key_id):
    """Reject a client-supplied api_key_id that doesn't reference a real key —
    prevents pinning an endpoint to an arbitrary/guessed key id (audit #21)."""
    if api_key_id is None:
        return
    from models.api_key import APIKey
    if await db.get(APIKey, api_key_id) is None:
        raise HTTPException(status_code=400, detail="Unknown api_key_id")


@router.post("", response_model=McpEndpointResponse)
async def create_endpoint(
    req: CreateMcpEndpoint,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Create a new MCP endpoint."""
    auth.require_scope("workflows", "write")
    await _validate_api_key_id(db, req.api_key_id)
    # Validate workflow references
    from models.automation_workflow import AutomationWorkflow
    for tc in req.tools_config:
        wf = await db.get(AutomationWorkflow, tc.workflow_id)
        if not wf:
            raise HTTPException(status_code=400, detail=f"Workflow {tc.workflow_id} not found")

    endpoint = McpEndpoint(
        name=req.name,
        description=req.description,
        tools_config=[tc.model_dump() for tc in req.tools_config],
        api_key_id=req.api_key_id,
        auto_start_sessions=req.auto_start_sessions,
        server_version=req.server_version,
        capabilities={"tools": {"listChanged": False}},
        enabled=True,
    )
    # slug auto-derived from name. KEPT GLOBAL (scope=None) to match the
    # ix_mcp_endpoints_slug unique index and keep the /mcp/{slug} route key
    # globally unambiguous. Race-safe via the DB UNIQUE index.
    await _insert_with_unique_slug(db, endpoint, "slug", req.name, max_len=100)
    await db.commit()

    return _to_response(endpoint)


@router.get("", response_model=List[McpEndpointResponse])
async def list_endpoints(
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """List all MCP endpoints."""
    result = await db.execute(
        select(McpEndpoint)
        .order_by(McpEndpoint.created_at.desc())
    )
    return [_to_response(ep) for ep in result.scalars().all()]


@router.get("/{endpoint_id}", response_model=McpEndpointResponse)
async def get_endpoint(
    endpoint_id: int,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Get MCP endpoint details."""
    ep = await _get_owned(db, endpoint_id)
    return _to_response(ep)


@router.patch("/{endpoint_id}", response_model=McpEndpointResponse)
async def update_endpoint(
    endpoint_id: int,
    req: UpdateMcpEndpoint,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Update an MCP endpoint."""
    auth.require_scope("workflows", "write")
    ep = await _get_owned(db, endpoint_id)

    if req.name is not None:
        ep.name = req.name
    if req.description is not None:
        ep.description = req.description
    if req.tools_config is not None:
        # Validate workflow references
        from models.automation_workflow import AutomationWorkflow
        for tc in req.tools_config:
            wf = await db.get(AutomationWorkflow, tc.workflow_id)
            if not wf:
                raise HTTPException(status_code=400, detail=f"Workflow {tc.workflow_id} not found")
        ep.tools_config = [tc.model_dump() for tc in req.tools_config]
    if req.api_key_id is not None:
        await _validate_api_key_id(db, req.api_key_id)
        ep.api_key_id = req.api_key_id
    if req.auto_start_sessions is not None:
        ep.auto_start_sessions = req.auto_start_sessions
    if req.enabled is not None:
        ep.enabled = req.enabled
    if req.server_version is not None:
        ep.server_version = req.server_version

    await db.flush()
    await db.commit()
    return _to_response(ep)


@router.delete("/{endpoint_id}")
async def delete_endpoint(
    endpoint_id: int,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Delete an MCP endpoint."""
    auth.require_scope("workflows", "write")
    ep = await _get_owned(db, endpoint_id)
    await db.delete(ep)
    await db.commit()
    return {"status": "deleted", "id": endpoint_id}


# ── Customer-facing overview (read-only) ─────────────────────────────────

@overview_router.get("/overview", response_model=McpOverviewResponse)
async def mcp_overview(
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """
    Read-only MCP connection overview: server URL, auth instructions, and
    the workflows exposed as MCP tools.
    """
    result = await db.execute(
        select(McpEndpoint)
        .order_by(McpEndpoint.created_at.desc())
    )
    endpoints = result.scalars().all()

    # Resolve workflow names for all tools in one query
    workflow_ids = {
        tc.get("workflow_id")
        for ep in endpoints
        for tc in (ep.tools_config or [])
        if tc.get("workflow_id") is not None
    }
    workflow_names: dict = {}
    if workflow_ids:
        from models.automation_workflow import AutomationWorkflow
        rows = await db.execute(
            select(AutomationWorkflow.id, AutomationWorkflow.name).where(
                AutomationWorkflow.id.in_(workflow_ids),
            )
        )
        workflow_names = dict(rows.all())

    base_url = os.getenv("PUBLIC_URL", "")
    servers: List[McpServerOverview] = []
    for ep in endpoints:
        connection_url = f"{base_url}/mcp/{ep.slug}" if base_url else f"/mcp/{ep.slug}"
        if ep.api_key_id:
            instructions = (
                "Connect with an MCP client over Streamable HTTP and send "
                "'Authorization: Bearer <API key>' on every request. This endpoint "
                f"is pinned to API key id {ep.api_key_id} — other keys are rejected."
            )
        else:
            instructions = (
                "Connect with an MCP client over Streamable HTTP and send "
                "'Authorization: Bearer <API key>' on every request. Any active "
                "API key is accepted."
            )
        servers.append(McpServerOverview(
            id=ep.id,
            name=ep.name,
            slug=ep.slug,
            description=ep.description,
            enabled=ep.enabled,
            connection_url=connection_url,
            auth=McpAuthInfo(
                required_api_key_id=ep.api_key_id,
                instructions=instructions,
            ),
            tools=[
                McpToolInfo(
                    tool_name=tc.get("tool_name", ""),
                    description=tc.get("tool_description", "") or "",
                    workflow_id=tc.get("workflow_id"),
                    workflow_name=workflow_names.get(tc.get("workflow_id")),
                )
                for tc in (ep.tools_config or [])
                if tc.get("workflow_id") is not None
            ],
        ))

    return McpOverviewResponse(servers=servers, total=len(servers))


# ── Helpers ──────────────────────────────────────────────────────────────

async def _get_owned(db: AsyncSession, endpoint_id: int) -> McpEndpoint:
    """Get an endpoint by id (single-owner coordinator)."""
    ep = await db.get(McpEndpoint, endpoint_id)
    if not ep:
        raise HTTPException(status_code=404, detail="MCP endpoint not found")
    return ep


def _to_response(ep: McpEndpoint) -> McpEndpointResponse:
    """Convert DB model to response with connection URL."""
    base_url = os.getenv("PUBLIC_URL", "")
    connection_url = f"{base_url}/mcp/{ep.slug}" if base_url else f"/mcp/{ep.slug}"

    return McpEndpointResponse(
        id=ep.id,
        name=ep.name,
        slug=ep.slug,
        description=ep.description,
        tools_config=ep.tools_config or [],
        api_key_id=ep.api_key_id,
        enabled=ep.enabled,
        auto_start_sessions=ep.auto_start_sessions,
        server_version=ep.server_version or "1.0.0",
        connection_url=connection_url,
        created_at=ep.created_at.isoformat() if ep.created_at else None,
        updated_at=ep.updated_at.isoformat() if ep.updated_at else None,
    )
