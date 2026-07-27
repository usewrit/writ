"""Uvicorn entrypoint for the self-host coordinator.

Launches with WebSocket protocol-level keepalive DISABLED (``ws_ping_interval=None``).

Why: the light ``writ-agent`` (the Rust writ-agent) answers a WS-protocol Ping
with an application-level BINARY frame — its own pong convention — NOT a protocol
Pong. uvicorn's ``websockets`` server keepalive only accepts a matching Pong, so it
would treat a perfectly healthy, heartbeating agent as unresponsive and close it
with ``1011 keepalive ping timeout`` after ~1 minute. In the hosted product the
agent spoke to a separate ws-gateway, so uvicorn never kept-alive the agent socket
directly; the single-container self-host coordinator IS the gateway, so we must
turn the redundant protocol keepalive off here.

Liveness is handled at the APPLICATION layer instead: the agent heartbeats every
25s and answers the coordinator's ``{"type":"ping"}`` frames, both of which advance
``last_heartbeat``; ``routers.user_recorder_ws._ping_loop`` closes any socket that
goes silent past ``AGENT_SILENCE_TIMEOUT``. The CLI can't express ``None`` for the
ping interval (0.0 would ping continuously), so we start uvicorn programmatically.
"""
import os
import sys

import uvicorn

from config import settings


def _require_single_worker() -> None:
    """Fail fast if the operator asks for >1 worker.

    The coordinator's JWT blacklist, rate limits, presence and scheduler live
    in IN-PROCESS fakeredis over a single SQLite file. With multiple workers
    each process gets its own empty keyspace, so token revocation silently
    stops working and the scheduler double-fires. main.py's lifespan enforces
    the same invariant for people who bypass serve.py; this check catches the
    misconfiguration before uvicorn even forks.
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
            sys.exit(
                f"Refusing to start: {var}={workers}. The self-host coordinator must "
                "run as a SINGLE worker process (SQLite + in-process fakeredis for the "
                "JWT blacklist/rate limits/scheduler — extra workers silently break "
                "token revocation). Remove the setting or set it to 1; scale browser "
                "work by connecting more fleet agents instead."
            )


if __name__ == "__main__":
    _require_single_worker()
    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        # Disable protocol-level WS keepalive (see module docstring). App-level
        # liveness in user_recorder_ws._ping_loop replaces it.
        ws_ping_interval=None,
        ws_ping_timeout=None,
        # Match the plain `uvicorn main:app` defaults otherwise (access log on,
        # uvicorn's own logging; the app installs its JSON handlers on import).
        proxy_headers=True,
        # Which upstream peers may set X-Forwarded-For. Defaults to loopback
        # ("127.0.0.1") so a direct client cannot spoof its source IP and defeat
        # per-IP controls; operators behind a reverse proxy set FORWARDED_ALLOW_IPS
        # (settings.forwarded_allow_ips) to that proxy's address/CIDR. NEVER "*"
        # on an internet-reachable deployment.
        forwarded_allow_ips=settings.forwarded_allow_ips,
    )
