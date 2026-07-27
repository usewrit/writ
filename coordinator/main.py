"""
Writ Self-Hosted Coordinator — Main FastAPI Application.

A single-user, single-container coordinator. It exposes the coordinator surface
(workflows, monitors, personas, runs, files, vault, agents/fleet, settings) plus
the agent wire contract (oauth device-flow, recorder_proxy, user_recorder(+ws)).

Redis is not an external dependency: a single process backs all Redis-style
usage (rate-limit, presence, dedup, agent-state, pub/sub, report/dispatch
streams) with an in-process fakeredis client (``utils.inproc_redis``) sharing
one keyspace. No Redis server is required to boot or run.
"""
import asyncio
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Any

from fastapi import Depends, FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import settings, should_expose_openapi
from database import init_db, close_db, engine

# --- Surviving routers --------------------------------------------------------
from routers.auth import router as auth_router
from routers.agents import router as agents_router
from routers.notifications import router as notifications_router
from routers.automation import router as automation_router
from routers.settings import router as settings_router
from routers.settings_extra import router as settings_extra_router
from routers.fleet import router as fleet_router
from routers.triggers import router as triggers_router
from routers.webhooks import router as webhooks_router
from routers.oauth import router as oauth_router
from routers.notifications_inbox import router as notifications_inbox_router
from routers.notification_preferences import router as notification_preferences_router
from routers.vault import router as vault_router
from routers.personas import router as personas_router
from routers.files import router as files_router, v1_router as files_v1_router
from routers.local_workflows import router as local_workflows_router
from routers.targets import router as targets_router
from routers.target_selectors import router as target_selectors_router
from routers.extractors import router as extractors_router
from routers.uptime import router as uptime_router
from routers.schedule import router as schedule_router
from routers.scraping import router as scraping_router
from routers.logs import router as logs_router
from routers.ai_assist import router as ai_assist_router
from routers.recorder_proxy import router as recorder_proxy_router
from routers.user_recorder import router as user_recorder_router
from routers.user_recorder_ws import router as user_recorder_ws_router
from routers.streaming import router as streaming_router
from routers.mcp_endpoints import router as mcp_router, overview_router as mcp_overview_router
from routers.mcp_server import router as mcp_server_router, connect_router as mcp_connect_router
from routers.runs import router as runs_router
from routers.ai_sessions import router as ai_sessions_router
from routers.crawl import router as crawl_router

from security.dependencies import require_platform_admin


# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper()),
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "name": "%(name)s", "message": "%(message)s"}',
    stream=sys.stdout,
)

logger = logging.getLogger(__name__)

# Global Redis client (in-process fakeredis; see utils.inproc_redis).
redis_client = None

# Background task handles (self-host keeps only the monitor/scraping loops).
scraping_scheduler_task: asyncio.Task = None
streaming_queue_task: asyncio.Task = None
workflow_queue_task: asyncio.Task = None

# APScheduler handle (monitor dispatch / staleness sweep / scheduled workflows /
# housekeeping). Single-process coordinator → no distributed lock. Started in the
# lifespan below and shut down on exit.
aps_scheduler = None


def _enforce_single_worker() -> None:
    """Refuse to boot with >1 web worker.

    The coordinator keeps the JWT blacklist, rate-limit counters, agent
    presence, pub/sub bus and the APScheduler in IN-PROCESS fakeredis, on top
    of a single SQLite file. A second worker process gets its own empty
    keyspace: token revocation silently stops working across workers, rate
    limits fragment, and the scheduler double-fires. Scale by adding fleet
    agents, not web workers.
    """
    for var in ("WEB_CONCURRENCY", "WORKERS", "UVICORN_WORKERS", "GUNICORN_WORKERS"):
        raw = (os.getenv(var) or "").strip()
        if not raw:
            continue
        try:
            workers = int(raw)
        except ValueError:
            continue
        if workers > 1:
            raise RuntimeError(
                f"Refusing to start: {var}={workers}. The self-host coordinator "
                "must run as a SINGLE worker process — its JWT blacklist, rate "
                "limits and scheduler live in in-process fakeredis and the "
                "database is a single SQLite file, so extra workers silently "
                "break token revocation and double-fire schedules. Remove the "
                f"{var} setting (or set it to 1); scale browser work by "
                "connecting more fleet agents instead."
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown for the coordinator."""
    _enforce_single_worker()
    logger.info("Starting Writ Coordinator...")
    logger.info(f"Environment: {settings.environment}")

    # Initialize database (dev: create tables; prod: alembic handles migrations).
    if settings.environment != "production":
        logger.info("Creating database tables (development mode)")
        await init_db()

    # First-boot single-admin bootstrap: provision the sole owner from
    # WRIT_ADMIN_EMAIL/WRIT_ADMIN_PASSWORD if no user exists yet. Idempotent and
    # never crashes boot — if the envs are absent, routers/auth.py register()
    # remains the first-boot fallback (see bootstrap.py).
    try:
        from bootstrap import run_bootstrap
        await run_bootstrap()
    except Exception as e:
        logger.warning(f"Admin bootstrap step skipped: {e}")

    # In-process Redis (fakeredis) — no external Redis server required. Shares
    # one keyspace/pub-sub bus across the process (see utils.inproc_redis).
    global redis_client
    from utils.inproc_redis import make_client as make_inproc_redis
    redis_client = make_inproc_redis(decode_responses=True)
    try:
        await redis_client.ping()
        logger.info("In-process Redis (fakeredis) ready — no external Redis required")
    except Exception as e:
        logger.error(f"Failed to initialize in-process Redis: {e}")

    app.state.redis = redis_client

    # Shared, pooled httpx client (reused by routers/services).
    from utils.http_client import init_http_client
    app.state.http_client = init_http_client()

    # Register the shared Redis clients so routers/services reuse them.
    from utils.redis_client import set_clients as set_shared_redis
    set_shared_redis(redis_client, make_inproc_redis(decode_responses=False))

    # JWT blacklist + platform controls (maintenance middleware) share Redis.
    from security.jwt import set_redis_client as set_jwt_redis
    set_jwt_redis(redis_client)
    from security.platform_controls import set_redis_client as set_controls_redis
    set_controls_redis(redis_client)

    # Agent-WS module needs Redis for per-agent channel-key retrieval.
    from routers.user_recorder_ws import set_redis_client as set_recorder_ws_redis
    set_recorder_ws_redis(redis_client)

    # Push AI provider configs to the AI gateway on startup (best-effort).
    try:
        from database import AsyncSessionLocal
        from routers.settings import _reload_gateway_config
        async with AsyncSessionLocal() as db:
            await _reload_gateway_config(db)
    except Exception as e:
        logger.warning(f"Could not push AI provider config to gateway: {e}")

    # Background loops for the coordinator: scraping scheduler + streaming
    # queue processor.
    global scraping_scheduler_task, streaming_queue_task, workflow_queue_task
    try:
        from services.scraping_scheduler import scraping_scheduler
        scraping_scheduler_task = asyncio.create_task(scraping_scheduler())
        logger.info("Background scraping scheduler task started")
    except Exception as e:
        logger.warning(f"Scraping scheduler not started: {e}")

    try:
        from routers.streaming import streaming_queue_processor
        streaming_queue_task = asyncio.create_task(streaming_queue_processor())
        logger.info("Streaming queue processor started")
    except Exception as e:
        logger.warning(f"Streaming queue processor not started: {e}")

    # Workflow queue drainer: dispatches runs that were QUEUED because every agent
    # was at capacity, the moment a slot frees. Without this, overflow runs sit in
    # 'queued' forever (the fleet distributes fine but the backlog never drains).
    try:
        from services.workflow_queue import queue_processor_loop
        workflow_queue_task = asyncio.create_task(queue_processor_loop())
        logger.info("Workflow queue processor started")
    except Exception as e:
        logger.warning(f"Workflow queue processor not started: {e}")

    # APScheduler: monitor dispatch/reconcile + staleness sweep + scheduled
    # workflow dispatch + daily housekeeping. Single-process (no distributed lock).
    global aps_scheduler
    try:
        from services.scheduler import build_scheduler
        aps_scheduler = build_scheduler()
        aps_scheduler.start()
        app.state.scheduler = aps_scheduler
        logger.info(
            "APScheduler started with jobs: %s",
            [j.id for j in aps_scheduler.get_jobs()],
        )
    except Exception as e:
        logger.warning(f"APScheduler not started: {e}")

    logger.info("Writ Coordinator started successfully")

    yield

    # Shutdown
    logger.info("Shutting down Writ Coordinator...")

    if aps_scheduler is not None:
        try:
            aps_scheduler.shutdown(wait=False)
            logger.info("APScheduler shut down")
        except Exception as e:
            logger.warning(f"APScheduler shutdown error: {e}")

    for _task in (scraping_scheduler_task, streaming_queue_task, workflow_queue_task):
        if _task and not _task.done():
            _task.cancel()
            try:
                await _task
            except asyncio.CancelledError:
                pass

    from utils.http_client import close_http_client
    await close_http_client()

    if redis_client:
        await redis_client.close()
        logger.info("Redis connection closed")

    await close_db()
    logger.info("Writ Coordinator shut down successfully")


# Create FastAPI app
#
# In production the interactive docs are off — and so is the schema that feeds
# them. Disabling /docs while still serving /openapi.json is only half a
# decision: the spec is the part that actually enumerates every route, its
# parameters and its shapes, and it was readable without authentication. That is
# a free reconnaissance map of ~300 endpoints for anyone who finds the host.
#
# Nothing in the product needs it: the SPA is written against typed clients, not
# generated from the schema at runtime. Developers who want it can read it from
# a development boot, or set WRIT_EXPOSE_OPENAPI=true deliberately.
_expose_openapi = should_expose_openapi(settings.is_production)
app = FastAPI(
    title="Writ Coordinator",
    description="Self-hosted backend orchestration service for distributed web monitoring",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if _expose_openapi else None,
    redoc_url="/redoc" if _expose_openapi else None,
    openapi_url="/openapi.json" if _expose_openapi else None,
    redirect_slashes=False,
)


# IP ban middleware - block banned IPs early in the request pipeline
@app.middleware("http")
async def ip_ban_middleware(request: Request, call_next):
    """Block banned IPs early in the request pipeline."""
    from security.ip_ban import is_banned
    ip = request.client.host if request.client else None
    if ip:
        _redis = getattr(app.state, 'redis', None)
        if _redis and await is_banned(_redis, ip):
            return JSONResponse(status_code=403, content={"detail": "Access denied"})
    return await call_next(request)


# Maintenance mode middleware - block all non-admin requests during maintenance
@app.middleware("http")
async def maintenance_mode_middleware(request: Request, call_next):
    """Return 503 for non-admin requests when maintenance mode is active."""
    from security.platform_controls import is_maintenance_mode, get_maintenance_message

    path = request.url.path
    if (
        # /api/about is the AGPL §13 source offer — it must stay reachable even
        # while the coordinator is in maintenance mode.
        path in ("/health", "/api/health", "/api/about", "/docs", "/openapi.json", "/metrics")
        or path.startswith("/api/admin/")
        or request.method == "OPTIONS"
    ):
        return await call_next(request)

    if await is_maintenance_mode():
        # Allow platform admins through, re-checking the DB (never trust the JWT claim).
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            from security.jwt import decode_access_token_with_blacklist
            payload = await decode_access_token_with_blacklist(auth_header[7:])
            sub = payload.get("sub") if payload else None
            if sub:
                from database import AsyncSessionLocal
                from models.user import User
                from sqlalchemy import select
                from uuid import UUID as _UUID
                try:
                    async with AsyncSessionLocal() as _db:
                        _res = await _db.execute(
                            select(User.is_platform_admin).where(User.id == _UUID(str(sub)))
                        )
                        _row = _res.first()
                    if _row and _row[0]:
                        return await call_next(request)
                except Exception:
                    logger.warning("Maintenance-mode admin re-check failed; denying bypass")

        message = await get_maintenance_message()
        return JSONResponse(
            status_code=503,
            content={"detail": message, "maintenance": True, "code": "maintenance"},
            headers={"Retry-After": "300"},
        )

    return await call_next(request)


# Prometheus metrics — OPT-IN via ENABLE_METRICS=true.
#
# requirements.txt has shipped prometheus-fastapi-instrumentator and
# prometheus-client since the carve, and both the auth-exempt path list and the
# rate-limit exempt list already name "/metrics" — but nothing ever instrumented
# the app, so the endpoint 404'd and the two dependencies were pure CVE surface.
# Wiring it makes the existing configuration truthful and gives operators real
# observability out of the box.
#
# Default OFF, and deliberately so: /metrics is unauthenticated (that is what
# Prometheus expects) and request-path metrics disclose your route topology and
# traffic shape. An operator who wants it should say so, and should scrape it
# over a private network or behind their reverse proxy's own auth.
if os.getenv("ENABLE_METRICS", "").strip().lower() in ("1", "true", "yes"):
    try:
        from prometheus_fastapi_instrumentator import Instrumentator

        Instrumentator(
            should_group_status_codes=True,
            # Never let a high-cardinality path (ids, slugs) explode the series
            # count — the instrumentator groups by route template, not raw path.
            should_ignore_untemplated=True,
            excluded_handlers=["/metrics", "/health"],
        ).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)
        logger.info("Prometheus metrics enabled at /metrics (ENABLE_METRICS=true)")
    except ImportError:
        logger.warning(
            "ENABLE_METRICS is set but prometheus-fastapi-instrumentator is not "
            "installed — /metrics will not be served."
        )

# TrustedHost middleware — reject spoofed Host headers (production only).
if settings.is_production:
    import os as _os
    from urllib.parse import urlparse as _urlparse
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    _allowed_hosts: list[str] = []
    try:
        _fe_host = _urlparse(settings.frontend_url).hostname
        if _fe_host:
            _allowed_hosts.append(_fe_host)
    except Exception:
        pass
    for _h in (_os.getenv("ALLOWED_HOSTS", "") or "").split(","):
        _h = _h.strip()
        if _h:
            _allowed_hosts.append(_h)
    if _allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts)
        logger.info("TrustedHost enforcement enabled for hosts: %s", _allowed_hosts)
    else:
        logger.warning(
            "TrustedHost enforcement skipped — no allowed hosts resolved "
            "(set frontend_url and/or ALLOWED_HOSTS)"
        )

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Agent-ID", "X-Signature", "X-Signature-Algorithm", "X-Timestamp", "X-Writ-Signature", "X-Hub-Signature-256"],
    expose_headers=["X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset", "Retry-After", "X-Cache"],
    max_age=600,
)

# GZip compression for large responses
from starlette.middleware.gzip import GZipMiddleware
app.add_middleware(GZipMiddleware, minimum_size=1000)


# Request size limit middleware
@app.middleware("http")
async def request_size_limit_middleware(request: Request, call_next):
    """Limit request body size to prevent DoS attacks."""
    content_length = request.headers.get("content-length")
    if content_length:
        content_length = int(content_length)
        path = request.url.path
        is_openai_compat = (
            "/v1/chat/completions" in path or "/v1/responses" in path
        )
        if path.startswith("/api/targets/") and "/preview" in path:
            max_size = 10 * 1024 * 1024  # 10MB
        elif path.startswith("/api/streaming/") or is_openai_compat:
            max_size = 50 * 1024 * 1024  # 50MB
        else:
            max_size = 1 * 1024 * 1024  # 1MB
        if content_length > max_size:
            return JSONResponse(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                content={
                    "error": "Request too large",
                    "max_size_mb": max_size / (1024 * 1024),
                    "your_size_mb": content_length / (1024 * 1024)
                }
            )
    return await call_next(request)


# Security headers middleware
@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Add security headers to all responses."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    if settings.is_production:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"
    # This process serves BOTH the JSON API and the same-origin SPA, so the CSP must
    # allow the app's own assets (script/style/img/fonts) — a blanket default-src 'none'
    # would intersect with the SPA's index.html <meta> CSP and blank the page.
    #
    # This header and the <meta> CSP in frontend/index.html must stay IDENTICAL in
    # substance. A browser enforces both policies simultaneously, taking the
    # intersection, so a directive that is stricter in one of them silently wins —
    # which makes a drifted pair very hard to debug. Two rules keep them honest:
    #
    #   - No external origins. Fonts are self-hosted and bundled (Inter and
    #     Schibsted Grotesk ship in the tree, see THIRD_PARTY_NOTICES.md), so
    #     fonts.googleapis.com / fonts.gstatic.com were dead grants that merely
    #     widened the policy. A self-hosted product must not phone a CDN.
    #   - frame-ancestors lives HERE only. Per CSP3 a browser IGNORES
    #     frame-ancestors delivered in a <meta> element, so the header is the one
    #     that actually denies framing ('none', matching X-Frame-Options: DENY).
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self' ws: wss:; "
        "frame-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'; form-action 'self'"
    )
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    return response


# Request logging middleware
@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    """Middleware for request logging (JSON)."""
    # The container healthcheck polls /health every 30s; logging it adds two
    # INFO lines per poll and drowns real traffic. Serve it without logging.
    if request.url.path == "/health":
        return await call_next(request)
    start_time = time.time()
    logger.info(
        f"Request started: {request.method} {request.url.path}",
        extra={
            "method": request.method,
            "path": request.url.path,
            "client": request.client.host if request.client else None,
        },
    )
    try:
        response = await call_next(request)
        duration = time.time() - start_time
        if hasattr(request.state, "rate_limit_info"):
            info = request.state.rate_limit_info
            response.headers["X-RateLimit-Limit"] = str(info["limit"])
            response.headers["X-RateLimit-Remaining"] = str(info["remaining"])
            response.headers["X-RateLimit-Reset"] = str(info["reset_at"])
        logger.info(
            f"Request completed: {request.method} {request.url.path} "
            f"- {response.status_code} - {duration:.3f}s",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration": duration,
            },
        )
        return response
    except Exception as e:
        duration = time.time() - start_time
        logger.error(
            f"Request failed: {request.method} {request.url.path} - {str(e)}",
            extra={
                "method": request.method,
                "path": request.url.path,
                "error": str(e),
                "duration": duration,
            },
            exc_info=True,
        )
        raise


# --- Include routers (28 survivors) ------------------------------------------
# Core user-facing
app.include_router(auth_router, prefix="/api")
app.include_router(oauth_router, prefix="/api")               # OAuth 2.0 + agent device-flow
app.include_router(automation_router, prefix="/api")          # Workflows + tasks
app.include_router(triggers_router, prefix="/api")            # Trigger rules
app.include_router(webhooks_router, prefix="/api")            # Webhook triggers
app.include_router(notifications_router, prefix="/api")       # Notification providers
app.include_router(notifications_inbox_router, prefix="/api") # In-app inbox
app.include_router(notification_preferences_router, prefix="/api")  # Owner platform-notification matrix
app.include_router(settings_router)                           # /api/settings (self-prefixed)
app.include_router(settings_extra_router)                     # /api/settings/{runtime,network,security,preferences,data} (self-prefixed)
app.include_router(fleet_router)                              # /api/fleet/{agents,connect-info,tokens} (self-prefixed)

# Vault / personas / files
app.include_router(vault_router, prefix="/api")
app.include_router(personas_router, prefix="/api")
app.include_router(files_router, prefix="/api")               # /api/files/*
app.include_router(files_v1_router)                           # /api/v1/files/* (self-prefixed)

# Cloud-callable LOCAL workflows (scoped API key surface, self-prefixed)
app.include_router(local_workflows_router)                    # /api/v1/local-workflows/*

# Targets / selectors / extractors
app.include_router(targets_router, prefix="/api")
app.include_router(target_selectors_router, prefix="/api")
app.include_router(extractors_router, prefix="/api")

# Monitoring / scheduling / scraping / logs
app.include_router(uptime_router)                             # /api/uptime (self-prefixed)
app.include_router(schedule_router, prefix="/api")
app.include_router(scraping_router, prefix="/api")
app.include_router(logs_router, prefix="/api")

# AI assist
app.include_router(ai_assist_router, prefix="/api")

# Agent / recorder wire contract (fleet dispatcher)
app.include_router(agents_router, prefix="/api")
app.include_router(recorder_proxy_router, prefix="/api")
app.include_router(user_recorder_router, prefix="/api")
app.include_router(user_recorder_ws_router, prefix="/api")

# Agent gateway ingress at the APP ROOT ("/ws/ai-gateway", NO /api prefix).
# The self-host coordinator is its own gateway — there is no separate ws-gateway
# process. /api/recorder/connect hands the light writ-agent a gateway_ws_url of
# "<this-coordinator>/ws/ai-gateway"; the agent opens its persistent WS there.
# Delegates to the SAME unified handler as /api/ws/recorder so a single ingress
# serves both infrastructure and user-hosted agents.
from routers.user_recorder_ws import _recorder_ws_handler as _agent_ws_handler
app.add_api_websocket_route("/ws/ai-gateway", _agent_ws_handler)

# Live-recording session relay at the APP ROOT ("/ws/record", NO /api prefix).
# The browser (BrowserRecorder / RecorderPreview / ApiRecorder) opens this socket;
# the coordinator picks a connected fleet agent and multiplexes the interactive
# recording/preview session over that agent's single persistent /ws/ai-gateway WS.
# This is the in-process replacement for the removed Node ws-gateway.
from routers.user_recorder_ws import browser_record_ws_handler as _browser_record_ws_handler
app.add_api_websocket_route("/ws/record", _browser_record_ws_handler)

# Streaming + MCP + runs
app.include_router(streaming_router, prefix="/api")
app.include_router(mcp_router, prefix="/api")
app.include_router(mcp_overview_router, prefix="/api")
# Native MCP server: POST /mcp (app root, like the desktop daemon) exposes the
# OSS operate tools (run/data/schedule/crawl/expose) to any MCP client. The
# bundled `writ-mcp` Node connector bridges stdio clients (Claude Code) to it.
app.include_router(mcp_server_router)                        # POST /mcp (no /api prefix)
app.include_router(mcp_connect_router, prefix="/api")        # /api/mcp/connect-info
app.include_router(runs_router, prefix="/api")
app.include_router(ai_sessions_router, prefix="/api")     # /api/ai-sessions/* (dispatch proxy)
app.include_router(crawl_router, prefix="/api")           # /api/crawl/* (Dragnet distributed crawl)


# Root endpoint (API-info JSON). When a built UI is present (see the SPA mount
# registered at the very end of this module), the "/" route below is overridden
# by the SPA shell handler — this stays as the API-only fallback.
_ui_dir_env = Path(os.getenv("WRIT_UI_DIR", "/app/ui")).expanduser()
if not (_ui_dir_env / "index.html").is_file():
    @app.get("/", tags=["Root"])
    async def root():
        """Root endpoint - API information (no bundled UI present)."""
        response = {"status": "ok", "service": "Writ Coordinator"}
        if settings.environment != "production":
            response["environment"] = settings.environment
        return response


# OAuth Server Metadata (must be at domain root per RFC 8414)
@app.get("/.well-known/oauth-authorization-server", tags=["OAuth"])
async def well_known_oauth(request: Request):
    """OAuth 2.0 Authorization Server Metadata discovery endpoint."""
    from routers.oauth import oauth_metadata
    return await oauth_metadata(request)


# ─────────────────────────────────────────────────────────────────────────────
# AGPL-3.0 §13 — the network source offer.
#
# Anyone who interacts with this coordinator remotely is entitled to the
# complete corresponding source of the version they are talking to. This
# endpoint is the machine-readable half of that offer (the UI renders the human
# half from it, on the login screen and in Settings → General), so it is
# deliberately PUBLIC and unauthenticated: a user who cannot log in is still a
# user interacting with the program over a network.
#
# Operators running a MODIFIED build must set WRIT_SOURCE_URL to their own
# repository — see config.Settings.writ_source_url.
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/api/about", tags=["Observability"])
async def about() -> Dict[str, Any]:
    """Identity, version, and the AGPL-3.0 source offer for this instance."""
    return {
        "name": "Writ Coordinator",
        "version": app.version,
        "license": "AGPL-3.0-only",
        "license_url": "https://www.gnu.org/licenses/agpl-3.0.html",
        "source_url": settings.writ_source_url,
        "modified": settings.writ_source_url.rstrip("/")
        != "https://github.com/usewrit/writ",
    }


# Health check endpoint
@app.get("/health", tags=["Observability"])
async def health_check() -> Dict[str, Any]:
    """Health check endpoint.

    Public and unauthenticated — the container HEALTHCHECK and any external
    monitor need it without credentials. It therefore reports only status, never
    detail: a raw exception string here would disclose the database path or the
    Redis DSN to any anonymous caller. Full detail is gated on
    ALLOW_INSECURE_DEV (an explicit local-trial opt-in) rather than on
    ``environment != "production"``, because development is the DEFAULT value —
    keying disclosure off it meant the leak was on unless someone opted out.
    The exception is always logged in full regardless.
    """
    health_status = {
        "status": "healthy",
        "timestamp": time.time(),
        "checks": {},
    }
    try:
        from sqlalchemy import text
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        health_status["checks"]["database"] = {"status": "healthy"}
    except Exception as e:
        logger.error(f"Health check database failed: {e}")
        details = str(e) if settings.allow_insecure_dev else "check failed"
        health_status["status"] = "unhealthy"
        health_status["checks"]["database"] = {"status": "unhealthy", "error": details}

    try:
        if redis_client:
            await redis_client.ping()
            health_status["checks"]["redis"] = {"status": "healthy"}
        else:
            raise Exception("Redis client not initialized")
    except Exception as e:
        logger.error(f"Health check redis failed: {e}")
        details = str(e) if settings.allow_insecure_dev else "check failed"
        health_status["status"] = "unhealthy"
        health_status["checks"]["redis"] = {"status": "unhealthy", "error": details}

    status_code = (
        status.HTTP_200_OK
        if health_status["status"] == "healthy"
        else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    return JSONResponse(content=health_status, status_code=status_code)


# Logs endpoint (admin only)
@app.get("/logs", tags=["Observability"])
async def get_logs(
    lines: int = 100,
    _admin=Depends(require_platform_admin),
):
    """Get recent log entries (placeholder)."""
    return {
        "message": "Log streaming not implemented yet",
        "hint": "Use your log aggregation service (e.g., CloudWatch, Datadog)",
    }


# --- Built self-host SPA (UI) -------------------------------------------------
# One container serves UI + API + WS. The coordinator runs NO browsers (fleet
# agents do), so the image is just Python + this app + the built Vite bundle.
#
# The built SPA lives at $WRIT_UI_DIR (default /app/ui in the container). Static
# assets are served from "<ui>/assets"; every other GET path that is NOT an
# API/WS/discovery route falls back to index.html so client-side routing works
# on a hard refresh / deep link.
#
# This block is registered LAST (after every /api/*, /ws/*, /.well-known/*,
# /docs, /openapi.json, /health, /logs route) so those real routes always win —
# Starlette matches routes in registration order and the "/{full_path:path}"
# catch-all here is the final entry. Reserved API/WS/discovery paths that still
# reach the fallback (i.e. did not match a real route) get an honest JSON 404,
# never the HTML shell.
#
# If the build dir is missing/empty we log a warning and serve API-only rather
# than crashing — the API + WS gateway stay fully functional headless.
_UI_DIR = Path(os.getenv("WRIT_UI_DIR", "/app/ui")).expanduser()
_UI_INDEX = _UI_DIR / "index.html"

# Path prefixes/paths that must NEVER be answered with the SPA shell. A GET to
# one of these that reached the fallback did not match a real route → JSON 404.
_UI_RESERVED_PREFIXES = ("/api/", "/ws/", "/.well-known/")
_UI_RESERVED_EXACT = {
    "/api", "/ws", "/docs", "/redoc", "/openapi.json",
    "/health", "/logs", "/metrics",
}

if _UI_INDEX.is_file():
    from fastapi.responses import FileResponse
    from starlette.staticfiles import StaticFiles

    # Hashed, immutable Vite build output. A missing asset yields a real 404
    # (not the HTML shell), which keeps broken <script>/<link> requests from
    # silently succeeding with text/html.
    _assets_dir = _UI_DIR / "assets"
    if _assets_dir.is_dir():
        app.mount(
            "/assets",
            StaticFiles(directory=str(_assets_dir)),
            name="assets",
        )

    @app.get("/", include_in_schema=False)
    async def spa_root():
        """Serve the built SPA shell at the site root."""
        return FileResponse(str(_UI_INDEX))

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        """SPA history fallback for client-side routes.

        Any path that falls through to here is either a real top-level static
        file (favicon, robots.txt, ...) or an unknown client-side route → return
        the SPA shell so the React router can render it. Reserved API/WS/discovery
        paths that slipped through get an honest JSON 404 instead of the shell.
        """
        path = "/" + full_path
        if path in _UI_RESERVED_EXACT or path.startswith(_UI_RESERVED_PREFIXES):
            return JSONResponse(
                status_code=404,
                content={
                    "error": "Not Found",
                    "message": f"The requested resource was not found: {path}",
                },
            )
        # Serve a real top-level static file if one exists (favicon.svg,
        # favicon-32.png, apple-touch-icon.png, robots.txt, ...); otherwise fall
        # back to the SPA shell. Resolve under the UI dir to block traversal.
        try:
            candidate = (_UI_DIR / full_path).resolve()
            candidate.relative_to(_UI_DIR.resolve())
            if candidate.is_file():
                return FileResponse(str(candidate))
        except (ValueError, OSError):
            pass
        return FileResponse(str(_UI_INDEX))

    logger.info("Serving built self-host UI from %s", _UI_DIR)
else:
    logger.warning(
        "WRIT_UI_DIR has no index.html at %s — serving API-only (no bundled UI). "
        "Build the SPA (npm run build) and point WRIT_UI_DIR at its dist/.",
        _UI_INDEX,
    )


# --- Error handlers -----------------------------------------------------------
@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    """Custom 404 handler."""
    return JSONResponse(
        status_code=404,
        content={
            "error": "Not Found",
            "message": f"The requested resource was not found: {request.url.path}",
        },
    )


@app.exception_handler(500)
async def internal_error_handler(request: Request, exc):
    """Custom 500 handler — never leaks internals; returns a reference id."""
    from services.error_reporting import report_internal_error
    error_id = report_internal_error(
        exc,
        action="api.unhandled_error",
        context={"path": request.url.path, "method": request.method},
    )
    public = f"An unexpected error occurred. Please try again later. (ref: {error_id})"
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal Server Error",
            "detail": public,
            "message": public,
            "error_id": error_id,
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Catch-all for arbitrary unhandled exceptions (leak-free, reference id)."""
    from starlette.exceptions import HTTPException as StarletteHTTPException
    if isinstance(exc, StarletteHTTPException):
        raise exc

    from services.error_reporting import report_internal_error
    error_id = report_internal_error(
        exc,
        action="api.unhandled_error",
        context={"path": request.url.path, "method": request.method},
    )
    public = f"An unexpected error occurred. Please try again later. (ref: {error_id})"
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal Server Error",
            "detail": public,
            "message": public,
            "error_id": error_id,
        },
    )
