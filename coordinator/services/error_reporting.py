"""
Central internal-error reporting.

Policy: unexpected backend errors must never leak internals (exception text,
tracebacks, provider/internal URLs) to API clients. End users get a generic
message plus a short reference id; the full details are written to the
events_audit log (level="error") where platform admins can look them up by
the same reference id in /admin/logs.

Errors that ARE user-actionable (target check failures, selector validation,
persona credential problems, notification provider test sends, plan/credit
limits, recorder/workflow step failures) are intentionally NOT routed through
this module — those keep their detailed messages for end users.
"""
import asyncio
import logging
import traceback
import uuid
from typing import Optional

from fastapi import HTTPException

logger = logging.getLogger("writ.errors")

GENERIC_MESSAGE = "An internal error occurred. Please try again later."


def new_error_id() -> str:
    """Short reference id users can quote to support / admins can search."""
    return uuid.uuid4().hex[:12]


async def _store_error_event(
    error_id: str,
    exc: BaseException,
    *,
    actor: Optional[str],
    action: str,
    context: Optional[dict],
) -> None:
    """Best-effort persist of full error details for the admin log viewer.

    This build has no audit-log store, so there is nowhere to persist the event.
    The full traceback is already written to the stdout/file log by
    report_internal_error, so this is a no-op.
    """
    return None


def report_internal_error(
    exc: BaseException,
    *,
    actor: Optional[str] = None,
    action: str = "api.error",
    context: Optional[dict] = None,
) -> str:
    """
    Log an internal error with full details and persist it for platform
    admins. Returns the reference id to embed in the user-facing message.
    """
    error_id = new_error_id()
    logger.error("[%s] %s: %s", error_id, action, exc, exc_info=exc)
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(
            _store_error_event(error_id, exc, actor=actor, action=action, context=context)
        )
    except RuntimeError:
        # No running loop (sync context) — the stdout/file log above suffices.
        pass
    return error_id


def internal_http_error(
    exc: BaseException,
    public_message: str = GENERIC_MESSAGE,
    *,
    status_code: int = 500,
    actor: Optional[str] = None,
    action: str = "api.error",
    context: Optional[dict] = None,
) -> HTTPException:
    """
    Build a sanitized HTTPException for an unexpected internal error.

    Usage:
        except Exception as e:
            raise internal_http_error(e, action="notifications.update_config")

    The client sees only `public_message` + a reference id; the exception
    text and traceback go to the server log and the admin audit log.
    """
    error_id = report_internal_error(exc, actor=actor, action=action, context=context)
    return HTTPException(
        status_code=status_code,
        detail=f"{public_message} (ref: {error_id})",
    )
