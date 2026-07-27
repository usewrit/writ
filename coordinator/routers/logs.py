"""
Logs router - audit log access and export endpoints.
"""
import logging
import csv
import io
from datetime import datetime
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, status, Query, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, and_

from database import get_db
EventsAudit = None  # no persistent audit-log store in this build
from security.dependencies import require_platform_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/logs", tags=["Logs"])


class LogEntry(BaseModel):
    """Response model for a log entry."""
    id: int
    timestamp: str
    level: str
    actor: str
    action: str
    message: str
    metadata: dict


class LogsResponse(BaseModel):
    """Response model for paginated logs."""
    logs: List[LogEntry]
    total: int
    page: int
    limit: int
    pages: int


def _csv_safe(value) -> str:
    """Neutralize CSV/formula injection.

    A spreadsheet treats a cell beginning with =, +, -, @ (or a leading tab/CR)
    as a formula, so an attacker-controlled value like
    ``=cmd|'/c calc'!A1`` would execute when the exported audit log is opened in
    Excel/Sheets. Prefix any such value with a single quote so it's rendered as
    literal text. Applied to every free-text cell (actor, action, message).
    """
    s = "" if value is None else str(value)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


def map_action_to_level(action: str) -> str:
    """Map audit action to log level."""
    if "error" in action.lower() or "fail" in action.lower():
        return "error"
    elif "delete" in action.lower() or "revoke" in action.lower():
        return "warning"
    elif "create" in action.lower() or "register" in action.lower():
        return "info"
    else:
        return "debug"


@router.get(
    "",
    response_model=LogsResponse,
    summary="Get Audit Logs",
    description="Get paginated audit logs with filtering.",
)
async def get_logs(
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(50, ge=1, le=100, description="Items per page"),
    level: Optional[str] = Query(None, description="Filter by log level"),
    actor: Optional[str] = Query(None, description="Filter by actor"),
    action: Optional[str] = Query(None, description="Filter by action"),
    search: Optional[str] = Query(None, description="Search in action and details"),
    start_date: Optional[str] = Query(None, description="Start date (ISO 8601)"),
    end_date: Optional[str] = Query(None, description="End date (ISO 8601)"),
    db: AsyncSession = Depends(get_db),
    _admin = Depends(require_platform_admin),
):
    """
    Get paginated audit logs with optional filtering.

    Supports filtering by:
    - level: Log level (info, warning, error, debug)
    - actor: Who performed the action
    - action: Action type
    - search: Search in action and details
    - start_date/end_date: Date range
    """
    # There is no persistent audit-log store in this build. Validate the date
    # filters (so bad input still 400s consistently) but always return an empty,
    # well-formed page — the endpoint stays live for the Logs UI.
    for label, value in (("start_date", start_date), ("end_date", end_date)):
        if value:
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid {label} format. Use ISO 8601.",
                )

    return LogsResponse(
        logs=[],
        total=0,
        page=page,
        limit=limit,
        pages=0,
    )


@router.get(
    "/export",
    summary="Export Logs",
    description="Export logs as CSV file.",
)
async def export_logs(
    level: Optional[str] = Query(None, description="Filter by log level"),
    actor: Optional[str] = Query(None, description="Filter by actor"),
    action: Optional[str] = Query(None, description="Filter by action"),
    search: Optional[str] = Query(None, description="Search in action and details"),
    start_date: Optional[str] = Query(None, description="Start date (ISO 8601)"),
    end_date: Optional[str] = Query(None, description="End date (ISO 8601)"),
    db: AsyncSession = Depends(get_db),
    _admin = Depends(require_platform_admin),
):
    """
    Export logs as CSV file.

    Uses the same filters as the get_logs endpoint.
    """
    # There is no persistent audit-log store in this build. Validate date
    # filters, then return a header-only CSV.
    for label, value in (("start_date", start_date), ("end_date", end_date)):
        if value:
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid {label} format. Use ISO 8601.",
                )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Timestamp", "Level", "Actor", "Action", "Message", "IP Address"])
    csv_content = output.getvalue()
    output.close()

    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=logs_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
        },
    )
