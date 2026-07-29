"""
Fleet — the self-host coordinator's view of its connected agent fleet.

Three surfaces (all admin-scoped, single owner, DB-verified via
require_platform_admin → get_auth_context):

  * GET  /api/fleet/agents        — the live fleet: DB Agent rows joined with the
                                     in-process connected-recorder registry
                                     (get_all_connected_recorders) for online /
                                     capacity truth.
  * GET  /api/fleet/connect-info  — how to point a new writ-agent here: the agent
                                     WS URL (from WRIT_PUBLIC_URL), the GitHub
                                     releases URL, and the docker image ref.
  * POST /api/fleet/tokens        — mint a long-lived infrastructure service token
                                     (generate_service_token) bound to a fresh
                                     agent id + channel key; raw token returned
                                     ONCE. GET lists minted tokens; DELETE revokes.

Minted service tokens are stateless JWTs (no DB row). We record their metadata +
a revocation flag in the ``config`` KV table so the operator can list and revoke
them; revocation both blacklists the token id and evicts a live connection.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import get_db
from models.agent import Agent, AgentStatus
from models.config import Config
from security.dependencies import require_platform_admin
from security.rate_limit import rate_limit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/fleet", tags=["fleet"])

# KV key holding the minted-fleet-token registry (metadata only, never the token).
_TOKENS_KEY = "fleet_service_tokens"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _http_base() -> str:
    """The coordinator's HTTP base URL the agent points ``saas.url`` at.

    The stock ``writ-agent`` binary discovers the gateway by POSTing
    ``<saas.url>/api/recorder/connect`` and dials the returned WS URL — so
    ``saas.url`` is the plain HTTP(S) base, NOT the ``/ws/ai-gateway`` WS URL.
    """
    return (settings.writ_public_url or "").strip().rstrip("/")


def _agent_repo() -> str:
    """``owner/name`` of the agent source repo. Override for a fork."""
    return (os.getenv("WRIT_AGENT_REPO") or "usewrit/writ-agent").strip().strip("/")


def _base_or_default() -> str:
    """The URL to build copy-paste commands against.

    ``WRIT_PUBLIC_URL`` when configured; otherwise the default a local install
    listens on, so a coordinator that has not been told its address still emits
    something runnable on the same host rather than an empty string. The UI
    warns separately when the public URL is unset — see ``/connect-info``.
    """
    return _http_base() or "http://localhost:8000"


# Where /agent.sh installs the binary. Referenced by the manual run command so
# the two steps compose: download with the one-liner, then run THAT binary.
AGENT_INSTALL_PATH = "~/.writ/writ-agent-fleet"


def _build_install_commands() -> dict:
    """Complete, copy-pasteable "get the agent onto this machine" commands.

    The connect commands below assume a ``writ-agent-fleet`` that already exists
    — which is the one thing a new operator does NOT have. Every path here ends
    with a runnable binary, so the fleet modal and the recorder's connect gate
    can show acquisition and connection as one continuous story instead of
    linking out to a releases page and hoping.

    The unix path is the coordinator's OWN installer in ``--download-only``
    mode. It used to be a fifteen-line blob (a ``uname`` case statement, a
    GitHub releases API call piped through grep/cut, an untar) pasted into the
    modal — unreadable, and a second copy of resolution logic that ``/agent.sh``
    already implements with checksum verification. One line, one implementation.
    """
    repo = _agent_repo()
    releases_api = f"https://api.github.com/repos/{repo}/releases/latest"

    unix = f"curl -fsSL {_base_or_default()}/agent.sh | sh -s -- --download-only"

    windows = (
        "# PowerShell — pull the matching asset from the latest release.\n"
        '# The release publishes a single Windows build (x86_64); it runs on ARM\n'
        '# devices under emulation.\n'
        '$target = "windows-x86_64"\n'
        f'$rel = Invoke-RestMethod {releases_api}\n'
        '$asset = $rel.assets | Where-Object { $_.name -like "*$target*" -and $_.name -notlike "*.sha256" } | Select-Object -First 1\n'
        'if (-not $asset) { throw "no release asset for $target — build from source" }\n'
        'Invoke-WebRequest $asset.browser_download_url -OutFile writ-agent-fleet.zip\n'
        'Expand-Archive writ-agent-fleet.zip -DestinationPath .'
    )

    source = (
        f"git clone https://github.com/{repo}.git\n"
        "cd writ-agent\n"
        "cargo build --release\n"
        "cp target/release/writ-agent ."
    )

    return {"unix": unix, "windows": windows, "source": source}


def _doc_extract_env() -> dict[str, str]:
    """The document/OCR extraction settings an agent needs, or ``{}``.

    The agent reads ``DOC_EXTRACT_URL`` / ``DOC_EXTRACT_SECRET`` from its own
    environment and treats an unset URL as "skip non-HTML content" — a silent
    no-op. That default is why this lane used to stay dark on real installs: it
    worked exactly as designed, and nothing ever told the operator that PDFs
    were being dropped.

    So the coordinator hands the settings over as part of connecting an agent.
    Every generated command below carries them inline, which makes PDF, office
    document and OCR coverage a property of having connected an agent at all,
    rather than a second setup step nobody knows to perform.

    Returns ``{}`` when no URL is configured, so an operator who deliberately
    runs without the service gets clean commands with no dead variables in them.
    """
    url = (settings.doc_extract_url or "").strip().rstrip("/")
    if not url:
        return {}
    env = {"DOC_EXTRACT_URL": url}
    secret = (settings.doc_extract_secret or "").strip()
    if secret:
        env["DOC_EXTRACT_SECRET"] = secret
    return env


def _build_connect_commands(raw_token: str) -> dict:
    """Build the ACTUAL runnable invocations for the agent this coordinator installs.

    The binary the install step above produces is ``writ-agent-fleet``, and it is
    configured ENTIRELY BY ENVIRONMENT — token in ``WRIT_SERVICE_TOKEN``,
    coordinator in ``WRIT_COORDINATOR_URL``, plaintext opt-in in
    ``WRIT_FLEET_ALLOW_INSECURE``. It has no ``config`` subcommand and no
    ``start`` subcommand; those belong to the DESKTOP ``writ-agent`` binary, and
    emitting them here handed the operator a two-line command that could not run
    against the binary the line above it had just downloaded. ``main.py``'s
    ``/agent.sh`` is the reference for the contract used here — it launches the
    same binary the same way.

      * binary — one line: the env the agent reads, then the installed path.
      * docker — the published image ships the desktop-style CLI, so the URL is
        still set as a config value inside the container before ``start``.

    ``allow_insecure`` is set only for a non-loopback plaintext ``http://``
    base, because the agent refuses to send its bearer token over plaintext to a
    non-loopback host unless it is opted in (see the agent's
    ``require_secure_url``). Loopback and ``https://`` need no such opt-in.

    Loopback addresses are rewritten to ``host.docker.internal`` in the DOCKER
    variant only. Inside a container ``127.0.0.1`` is the container itself, so
    the otherwise-correct localhost URL would send the agent looking for a
    coordinator in its own namespace and fail to connect with nothing obviously
    wrong. The ``--add-host`` line makes that alias resolve on Linux too, where
    Docker does not provide it for free.
    """
    base = _http_base()
    url = base or "<PUBLIC_URL>"

    # Does the agent need the plaintext opt-in? Only for a real (non-loopback)
    # http:// host; https:// and localhost/127.0.0.1/::1 are accepted as-is.
    needs_insecure = False
    if base.startswith("http://"):
        host = base[len("http://"):].split("/", 1)[0].split(":", 1)[0].lower()
        if host not in ("localhost", "127.0.0.1", "::1", "[::1]"):
            needs_insecure = True

    # Document/OCR settings ride along in the same env prefix as the token, so
    # the operator never has to know this lane exists to get it working.
    doc_env = _doc_extract_env()

    # One line, in the same env-var form `/agent.sh` uses to launch it, ending in
    # the path that installer writes to — so "download" then "run" compose.
    binary_env = [f"WRIT_SERVICE_TOKEN={raw_token}", f"WRIT_COORDINATOR_URL={url}"]
    if needs_insecure:
        binary_env.append("WRIT_FLEET_ALLOW_INSECURE=1")
    binary_env += [f"{k}={v}" for k, v in doc_env.items()]
    binary = " ".join(binary_env + [AGENT_INSTALL_PATH])

    docker_image = os.getenv("WRIT_AGENT_DOCKER_IMAGE", "ghcr.io/usewrit/writ-agent:latest")

    # A container reaching a service published on the HOST's loopback needs the
    # host gateway alias — inside the container, 127.0.0.1 is the container.
    # This applies to BOTH URLs the agent is handed: the coordinator it dials
    # (saas.url) and the extraction service it forwards documents to.
    needs_host_alias = False

    def _from_container(u: str) -> str:
        nonlocal needs_host_alias
        for loopback in ("127.0.0.1", "localhost", "[::1]", "::1"):
            if f"//{loopback}:" in u or u.endswith(f"//{loopback}"):
                needs_host_alias = True
                return u.replace(f"//{loopback}", "//host.docker.internal", 1)
        return u

    docker_url = _from_container(url)
    doc_docker_lines = "".join(
        f"  -e {k}={_from_container(v) if k == 'DOC_EXTRACT_URL' else v} \\\n"
        for k, v in doc_env.items()
    )

    # Recompute the plaintext opt-in against the REWRITTEN url: host.docker.internal
    # is not loopback, so an http:// coordinator that needed no opt-in from the
    # host does need one from inside a container, or the agent refuses to send
    # its bearer token and the connection dies on the first request.
    insecure_docker = ""
    if needs_insecure or (
        docker_url.startswith("http://")
        and docker_url != url  # rewritten, i.e. was loopback and no longer is
    ):
        insecure_docker = " && writ-agent config set saas.allow_insecure true"

    host_alias_line = (
        "  --add-host host.docker.internal:host-gateway \\\n" if needs_host_alias else ""
    )
    docker = (
        "docker run -d --name writ-agent \\\n"
        f"  -e WRIT_SERVICE_TOKEN={raw_token} \\\n"
        f"{doc_docker_lines}"
        f"{host_alias_line}"
        f"  {docker_image} \\\n"
        f"  sh -c \"writ-agent config set saas.url {docker_url}{insecure_docker} "
        "&& writ-agent start --headless\""
    )

    return {"connect_command": binary, "docker_command": docker}


def _operator_names(registry: dict) -> tuple[dict, dict]:
    """Operator-chosen agent labels from the token registry, keyed two ways.

    An agent never learns the name it was minted with — nothing on the wire
    carries one — so without this lookup the connect modal's "Name" field wrote
    a label into the registry that no surface ever read, and every machine
    listed as its raw ``writ-xxxxxxxx`` id.

    Keyed by BOTH agent id and token prefix because the two can diverge: the
    token pins an agent_id, but a reconnecting agent may claim its own stored id
    and land on a different row (see ``_register_agent``'s reuse paths). The
    token prefix is stamped onto that row's meta at registration and identifies
    the mint exactly.

    Only ``display_name`` counts. ``name`` always holds something — a generated
    ``agent-<timestamp>`` for pairing codes, or the ``fleet-agent`` default — and
    promoting those would relabel every unnamed machine with a placeholder
    nobody chose.
    """
    by_agent: dict[str, str] = {}
    by_prefix: dict[str, str] = {}
    for tk in (registry or {}).get("tokens", []) or []:
        label = (tk.get("display_name") or "").strip()
        # A revoked token's label belongs to a machine that was torn out.
        if not label or tk.get("revoked_at"):
            continue
        if tk.get("agent_id"):
            by_agent[tk["agent_id"]] = label
        if tk.get("token_prefix"):
            by_prefix[tk["token_prefix"]] = label
    return by_agent, by_prefix


def _agent_display_name(
    agent_id: str,
    meta: dict,
    by_agent: dict,
    by_prefix: dict,
) -> str:
    """What to call this agent in the fleet list, best source first."""
    return (
        (meta or {}).get("name")
        or (meta or {}).get("hostname")
        or by_agent.get(agent_id)
        or by_prefix.get((meta or {}).get("oauth_token_prefix") or "")
        or agent_id
    )


# ============================================================
# GET /api/fleet/agents — the live fleet
# ============================================================
@router.get("/agents")
async def list_fleet_agents(
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_platform_admin),
):
    """Return every registered agent, marking online + reporting capacity from the
    in-process connected-recorder registry (the WS layer is source of truth for
    live state; the DB row is the durable identity)."""
    from routers.user_recorder_ws import (
        get_all_connected_recorders,
        capacity_block,
        OPERATOR_MAX_SESSIONS_LIMIT,
    )

    connected = {r["agent_id"]: r for r in get_all_connected_recorders()}

    try:
        names = _operator_names(await _load_token_registry(db))
    except Exception as e:  # a naming nicety must never break the fleet list
        logger.warning("Could not read fleet token names: %s", e)
        names = ({}, {})
    names_by_agent, names_by_prefix = names

    # REVOKED agents were explicitly torn out of the fleet (token revoke / remove)
    # — they must not keep haunting the list, which is the whole "surfaces many
    # old agents" complaint. Everything else (active / inactive / suspended) still
    # shows so the operator can see — and prune — stale offline machines.
    result = await db.execute(
        select(Agent)
        .where(Agent.status != AgentStatus.REVOKED)
        .order_by(Agent.created_at.desc())
    )
    rows = result.scalars().all()

    agents = []
    seen: set[str] = set()
    for a in rows:
        seen.add(a.agent_id)
        live = connected.get(a.agent_id)
        meta = a.meta or {}
        name = _agent_display_name(a.agent_id, meta, names_by_agent, names_by_prefix)
        # An OFFLINE agent has no live registry entry, but its operator override
        # is on the durable row — surface it so the Fleet page can still show
        # (and edit) the pin for a machine that is currently down, instead of
        # blanking the control until it reconnects.
        agents.append({
            "id": a.agent_id,
            "name": name,
            "platform": a.platform.value if a.platform else "unknown",
            "online": a.agent_id in connected,
            "last_seen": a.last_seen_at.isoformat() if a.last_seen_at else None,
            "capacity": capacity_block(live) if live else {
                "max_sessions": None,
                "active_sessions": None,
                "free_slots": None,
                "agent_reported": meta.get("agent_reported_max_sessions"),
                "token_ceiling": None,
                "operator_override": meta.get("operator_max_sessions"),
                "limit": OPERATOR_MAX_SESSIONS_LIMIT,
            },
            "status": a.status.value if a.status else None,
            "is_trusted": a.is_trusted,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        })

    # Include any live-connected agent that has no DB row yet (connected but not
    # persisted) so the fleet view never hides a working connection.
    for agent_id, live in connected.items():
        if agent_id in seen:
            continue
        agents.append({
            "id": agent_id,
            "name": live.get("name") or names_by_agent.get(agent_id) or agent_id,
            "platform": live.get("platform", "unknown"),
            "online": True,
            "last_seen": live.get("connected_at"),
            "capacity": capacity_block(live),
            "status": "active",
            "is_trusted": False,
            "created_at": None,
        })

    return {"agents": agents, "online_count": len(connected), "total": len(agents)}


# ============================================================
# DELETE /api/fleet/agents/{agent_id}  ·  POST /api/fleet/agents/prune
# Remove stale / offline agents from the fleet — single + bulk. Removal is
# DURABLE: any bound fleet token is revoked (so the agent can't silently
# reconnect and reappear), the live socket is evicted, the published snapshot /
# secret / channel-key Redis state is dropped, and the durable Agent row is
# deleted. This is what backs "bulk revoke" + inline remove on the Fleet page.
# ============================================================
async def _redis_cleanup_agent(request: Request, agent_id: str, token_prefix: Optional[str]) -> None:
    """Drop the agent's channel keys + its published config snapshot from Redis."""
    try:
        redis_client = getattr(request.app.state, "redis", None)
        if redis_client is None:
            from utils.redis_client import get_redis
            redis_client = get_redis()
        if token_prefix:
            await redis_client.delete(f"agent_channel_key:{token_prefix}")
        await redis_client.delete(f"agent_channel_key:{agent_id}")
    except Exception as e:
        logger.warning("fleet: channel-key cleanup failed for %s: %s", agent_id, e)
    # Evict the published secret + snapshot + version + node map so no relay node
    # keeps serving the removed agent_id (AGENT POLL CONTRACT §3).
    try:
        from services.node_config_publisher import NodeConfigPublisher
        from utils.redis_client import get_redis
        await NodeConfigPublisher(get_redis()).revoke_agent(agent_id)
    except Exception as e:
        logger.debug("fleet: snapshot revoke skipped for %s: %s", agent_id, e)


async def _evict_agent_socket(agent_id: str) -> bool:
    """Close the agent's live WS connection if present. True if one was evicted."""
    try:
        from routers.user_recorder_ws import _connections
        ws = _connections.get(agent_id)
        if ws is not None:
            await ws.close(code=4403)
            return True
    except Exception as e:
        logger.warning("fleet: socket eviction failed for %s: %s", agent_id, e)
    return False


async def _remove_agent_completely(
    db: AsyncSession, request: Request, agent_id: str, reg: dict
) -> bool:
    """Tear an agent out of the fleet everywhere.

    Mutates ``reg`` in place (marks a bound, still-active token revoked); the
    CALLER persists ``reg`` and commits so a bulk prune is one transaction + one
    registry write. Returns True if anything was actually removed (an unknown id
    ⇒ False ⇒ 404 on the single-remove path).
    """
    removed = False

    # Revoke a bound, still-active fleet token so this identity can't reconnect
    # with it and silently reappear in the fleet.
    token_prefix: Optional[str] = None
    entry = next(
        (t for t in reg["tokens"] if t.get("agent_id") == agent_id and not t.get("revoked_at")),
        None,
    )
    if entry:
        entry["revoked_at"] = _now_iso()
        token_prefix = entry.get("token_prefix")
        removed = True

    await _redis_cleanup_agent(request, agent_id, token_prefix)
    if await _evict_agent_socket(agent_id):
        removed = True

    row = (
        await db.execute(select(Agent).where(Agent.agent_id == agent_id))
    ).scalar_one_or_none()
    if row is not None:
        await db.delete(row)
        removed = True

    return removed


async def _redistribute_after_removal(db: AsyncSession) -> None:
    """Best-effort rebalance of monitor/target assignments after agents leave."""
    try:
        from routers.agents import trigger_auto_redistribution
        await trigger_auto_redistribution(db, "fleet_agent_removed")
    except Exception as e:
        logger.debug("fleet: post-removal redistribution skipped: %s", e)


# ============================================================
# PATCH /api/fleet/agents/{agent_id}/capacity
# How many concurrent sessions this agent may be given.
# ============================================================
class AgentCapacityRequest(BaseModel):
    """Pin an agent's concurrent-session count, or clear the pin.

    ``max_sessions=None`` removes the override and hands control back to whatever
    the agent reports about itself.
    """
    max_sessions: Optional[int] = None


@router.patch("/agents/{agent_id}/capacity")
async def set_fleet_agent_capacity(
    agent_id: str,
    body: AgentCapacityRequest,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_platform_admin),
):
    """Set (or clear) the operator's concurrent-session override for one agent.

    Exists because the stock ``writ-agent`` self-limits to 2 sessions in its
    heartbeat, and the coordinator honoured that unconditionally — so a machine
    that could comfortably run eight browsers sat at two, with nothing in the UI
    saying where the number came from and no way to change it.

    The override is written to the agent's durable row so it survives reconnects
    (the live registry entry is rebuilt from scratch on every socket), and applied
    to the live entry immediately so it takes effect without a restart.
    """
    from routers.user_recorder_ws import (
        set_operator_max_sessions,
        get_connected_recorder_meta,
        OPERATOR_MAX_SESSIONS_LIMIT,
    )

    value = body.max_sessions
    if value is not None:
        if value < 1 or value > OPERATOR_MAX_SESSIONS_LIMIT:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"max_sessions must be between 1 and {OPERATOR_MAX_SESSIONS_LIMIT}, "
                    "or null to follow the agent's own report."
                ),
            )
        value = int(value)

    result = await db.execute(select(Agent).where(Agent.agent_id == agent_id))
    agent = result.scalar_one_or_none()
    if agent is None and get_connected_recorder_meta(agent_id) is None:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    if agent is not None:
        # SQLAlchemy tracks JSON columns by identity, so mutating `agent.meta` in
        # place would not mark the row dirty and the override would vanish on the
        # next restart. Rebind a new dict.
        meta = dict(agent.meta or {})
        if value is None:
            meta.pop("operator_max_sessions", None)
        else:
            meta["operator_max_sessions"] = value
        agent.meta = meta
        await db.commit()

    live = set_operator_max_sessions(agent_id, value)
    if live is not None:
        return {"agent_id": agent_id, "capacity": live, "applied": "live"}

    # Offline agent: persisted only, and it will be picked up at next connect.
    return {
        "agent_id": agent_id,
        "capacity": {
            "max_sessions": None,
            "active_sessions": None,
            "free_slots": None,
            "agent_reported": (agent.meta or {}).get("agent_reported_max_sessions") if agent else None,
            "token_ceiling": None,
            "operator_override": value,
            "limit": OPERATOR_MAX_SESSIONS_LIMIT,
        },
        "applied": "on_next_connect",
    }


class PruneAgentsRequest(BaseModel):
    """Bulk-remove a set of fleet agents by id (the ids the operator selected)."""
    agent_ids: list[str]


@router.delete("/agents/{agent_id}")
async def remove_fleet_agent(
    agent_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_platform_admin),
):
    """Remove one agent from the fleet (durable — see the section note)."""
    reg = await _load_token_registry(db)
    removed = await _remove_agent_completely(db, request, agent_id, reg)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
    await _save_token_registry(db, reg)
    await db.commit()
    await _redistribute_after_removal(db)
    return {"success": True, "agent_id": agent_id}


@router.post("/agents/prune")
async def prune_fleet_agents(
    body: PruneAgentsRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_platform_admin),
):
    """Bulk-remove the given agents from the fleet in ONE call. Unknown ids are
    skipped; returns how many were actually removed."""
    reg = await _load_token_registry(db)
    removed = 0
    for aid in dict.fromkeys(body.agent_ids):  # de-dup, preserve order
        if await _remove_agent_completely(db, request, aid, reg):
            removed += 1
    await _save_token_registry(db, reg)
    await db.commit()
    if removed:
        await _redistribute_after_removal(db)
    return {"removed": removed, "requested": len(body.agent_ids)}


# ============================================================
# GET /api/fleet/connect-info — how to dial a new agent in
# ============================================================
@router.get("/connect-info")
async def get_connect_info(
    _admin=Depends(require_platform_admin),
):
    """Return the coordinator's agent WS URL + where to get the writ-agent binary."""
    public_url = _http_base()

    ws_url = None
    if public_url:
        if public_url.startswith("https://"):
            ws_url = "wss://" + public_url[len("https://"):] + "/ws/ai-gateway"
        elif public_url.startswith("http://"):
            ws_url = "ws://" + public_url[len("http://"):] + "/ws/ai-gateway"
        else:
            ws_url = public_url + "/ws/ai-gateway"

    repo = _agent_repo()
    github_url = os.getenv(
        "WRIT_AGENT_RELEASES_URL",
        f"https://github.com/{repo}/releases",
    )
    docker_image = os.getenv("WRIT_AGENT_DOCKER_IMAGE", "ghcr.io/usewrit/writ-agent:latest")

    return {
        "ws_url": ws_url,
        "public_url": public_url or None,
        "github_url": github_url,
        # Where the agent comes FROM, so the UI can show acquisition and
        # connection together instead of linking out mid-onboarding.
        "repo": repo,
        "repo_url": f"https://github.com/{repo}",
        "install_commands": _build_install_commands(),
        "docker_image": docker_image,
        # The agent points `saas.url` (config) at this HTTP base and reads its token
        # from WRIT_SERVICE_TOKEN — it discovers the WS gateway from here itself.
        "saas_url": public_url or None,
        # Document/OCR extraction. Reported so the UI can say plainly whether a
        # crawl will read PDFs and scanned pages or quietly drop them — the
        # agent's own default is to skip them without complaint. The URL is
        # informational; the secret is NEVER returned here (it travels only in
        # the one-time connect command, next to the token it is paired with).
        "doc_extract": {
            "enabled": bool(_doc_extract_env()),
            "url": (settings.doc_extract_url or "").strip().rstrip("/") or None,
            "authenticated": bool((settings.doc_extract_secret or "").strip()),
        },
    }


@router.get("/capacity")
async def get_fleet_capacity(
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_platform_admin),
):
    """Fleet-capacity advisory: how the number of connected agents bounds the
    minimum monitor check interval, plus per-preset agent requirements and a
    human explanation. Backs the "advertise the limit" UI in the check-interval
    picker so users understand why a fast interval needs more agents."""
    from services.fleet_capacity import compute_capacity
    return await compute_capacity(db)


# ============================================================
# POST /api/fleet/agents/{agent_id}/deploy — send a workflow/secret/persona
# to a fleet-local-capable agent (Mirror or Move disposition)
# ============================================================
class DeployRequest(BaseModel):
    """Deploy a coordinator entity to a fleet-local-capable agent.

    kind        — what to send (a whole workflow, a standalone secret, a persona).
    id          — the coordinator-owned integer id of that entity (NEVER trusted
                  from any agent payload; it identifies a coordinator DB row).
    include_deps — for a workflow, also bundle its referenced secrets + persona.
    mode        — 'mirror' keeps the coordinator copy; 'move' deletes it (and its
                  EXCLUSIVELY-used secrets/personas, ref-counted) AFTER the ack.
    """
    kind: Literal["workflow", "secret", "persona"]
    id: int
    include_deps: bool = True
    mode: Literal["mirror", "move"] = "mirror"


def _resolve_deploy_target(agent_id: str) -> str:
    """Resolve a connected, fleet-local-capable agent's channel key, or raise.

    Fail-closed at every step so we NEVER push plaintext secret material:
      * 404 if the agent holds no live socket on this coordinator.
      * 409 if it did not advertise `local_workflows=1` (can't host local wf).
      * 409 if its per-agent channel key is missing (reconnect the agent) — the
        deploy seals every secret under that key, so its absence must abort.

    Reads only the in-process live-socket registries (the same source the
    dispatcher uses); the channel key is authoritative on ``_agent_meta`` for a
    live socket (set at connect).
    """
    from routers.user_recorder_ws import _agent_meta, _connections

    if agent_id not in _connections:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} is not connected")
    meta = _agent_meta.get(agent_id) or {}
    if not meta.get("local_workflows_capable"):
        raise HTTPException(
            status_code=409,
            detail=f"Agent {agent_id} is not fleet-local-capable "
                   "(reconnect it with the local-workflows capability enabled)",
        )
    channel_key = meta.get("channel_key")
    if not channel_key:
        raise HTTPException(
            status_code=409,
            detail=f"No channel key for agent {agent_id} — reconnect the agent",
        )
    return channel_key


def _seal_plaintext_for_agent(plaintext: str, channel_key: str) -> str:
    """Fernet-seal a plaintext string under the agent's channel key.

    Routes through the master key first (encrypt→_reencrypt_for_agent) so there is
    exactly ONE sealing path shared with dispatch (_reencrypt_for_agent expects a
    master-Fernet ciphertext input). The plaintext never leaves this process
    unsealed.
    """
    from security.encryption import SecretEncryption
    from routers.user_recorder_ws import _reencrypt_for_agent

    master_blob = SecretEncryption.encrypt_secret(plaintext)
    return _reencrypt_for_agent(master_blob, channel_key)


def _reseal_master_blob_for_agent(master_blob: Optional[str], channel_key: str) -> Optional[str]:
    """Re-seal an already-master-Fernet blob under the channel key, preserving the
    inner byte format (gzip layer for session_state, base32 for a TOTP seed, JSON
    for creds). ``None`` in → ``None`` out."""
    if not master_blob:
        return None
    from routers.user_recorder_ws import _reencrypt_for_agent

    return _reencrypt_for_agent(master_blob, channel_key)


def _workflow_secret_keys(steps: list, form_data: Optional[dict]) -> set[str]:
    """Collect the vault secret NAMES a workflow references.

    Two placeholder shapes both map to a coordinator VaultSecret row:
      * ``{{secret:NAME}}`` — surfaces via _extract_placeholders as key 'secret:NAME'.
      * ``{{vault:NAME}}``  — surfaces via the secret_resolver VAULT_REF regex.
    A dotted subfield ({{vault:card.number}}) resolves against the BASE row, so we
    key on the base name.
    """
    from routers.automation import _extract_placeholders
    from services.secret_resolver import VAULT_REF

    keys: set[str] = set()
    for p in _extract_placeholders(steps or [], form_data or {}):
        k = p.get("key") or ""
        if k.startswith("secret:"):
            name = k[len("secret:"):]
            keys.add(name.rsplit(".", 1)[0] if "." in name else name)

    import json as _json
    blob = _json.dumps({"steps": steps or [], "form_data": form_data or {}})
    for name in VAULT_REF.findall(blob):
        keys.add(name.rsplit(".", 1)[0] if "." in name else name)
    return keys


async def _build_persona_blob(db: AsyncSession, persona, channel_key: str) -> dict:
    """Build the sealed inline persona blob for the frozen wire contract.

    Every secret sub-field is re-sealed under the agent channel key; fingerprint
    is plaintext display metadata. Login creds + proxy are packaged as sealed JSON
    maps; session_state/totp keep their existing inner framing (gzip / base32)
    inside the channel-key seal so the agent re-seals the SAME inner format.
    """
    from services.persona_service import PersonaService

    login_creds = PersonaService.resolve_login_credentials(persona)
    creds_encrypted = (
        _seal_plaintext_for_agent(json.dumps(login_creds), channel_key)
        if login_creds else None
    )

    proxy = PersonaService.resolve_proxy(persona)
    proxy_out = None
    if proxy:
        # Seal the whole proxy dict (it carries username/password) under the channel key.
        proxy_out = _seal_plaintext_for_agent(json.dumps(proxy), channel_key)

    return {
        "name": persona.name,
        "creds_encrypted": creds_encrypted,
        "fingerprint": persona.fingerprint or {},
        "proxy": proxy_out,
        "session_state_encrypted": _reseal_master_blob_for_agent(
            persona.session_state_encrypted, channel_key
        ),
        "totp_seed_encrypted": _reseal_master_blob_for_agent(
            persona.totp_seed_encrypted, channel_key
        ),
    }


def _target_secret_keys(setup_steps_json: Optional[str]) -> set[str]:
    """Collect the vault secret NAMES a Target's inline ``setup_steps`` manifest
    references.

    ``setup_steps`` is a JSON *text* column shaped ``{steps, credentials}`` (a
    recorded login/navigate/click manifest dispatched as the pre_check_workflow).
    It can carry {{secret:NAME}} / {{vault:NAME}} placeholders exactly like an
    AutomationWorkflow, so we reuse the same scanner (steps + the credentials map
    treated as form_data) to keep the placeholder handling identical."""
    if not setup_steps_json:
        return set()
    try:
        manifest = json.loads(setup_steps_json)
    except (ValueError, TypeError):
        return set()
    if not isinstance(manifest, dict):
        return set()
    steps = manifest.get("steps") or []
    creds = manifest.get("credentials") or {}
    if not isinstance(steps, list):
        steps = []
    if not isinstance(creds, dict):
        creds = {}
    return _workflow_secret_keys(steps, creds)


async def _refcount_secret_in_use(db: AsyncSession, key: str, exclude_workflow_id: int) -> bool:
    """True if ANY other AutomationWorkflow OR Target references vault secret ``key``.

    Scans every OTHER workflow's steps+form_data AND every Target's inline
    ``setup_steps`` manifest for {{secret:key}} / {{vault:key}} (base name match)
    so a Move only deletes secrets used EXCLUSIVELY by the moved workflow
    (Decision D1); secrets shared with another workflow or a live monitor stay on
    the coordinator."""
    from models.automation_workflow import AutomationWorkflow
    from models.target import Target

    rows = (
        await db.execute(
            select(AutomationWorkflow).where(AutomationWorkflow.id != exclude_workflow_id)
        )
    ).scalars().all()
    for wf in rows:
        if key in _workflow_secret_keys(wf.steps or [], wf.form_data or {}):
            return True

    # A monitor's inline setup_steps manifest can also reference the secret at
    # check time — those are NOT AutomationWorkflow rows, so scan them too.
    targets = (
        await db.execute(select(Target).where(Target.setup_steps.isnot(None)))
    ).scalars().all()
    for tgt in targets:
        if key in _target_secret_keys(tgt.setup_steps):
            return True
    return False


async def _refcount_persona_in_use(
    db: AsyncSession, persona_id: int, exclude_workflow_id: Optional[int] = None
) -> bool:
    """True if ANY AutomationWorkflow (other than ``exclude_workflow_id``) defaults
    to persona ``persona_id`` OR any Target references it via ``persona_id``.

    Target.persona_id is ondelete=SET NULL, so deleting a persona a live monitor
    depends on would silently NULL the monitor's auth — a Move must never do that
    for a shared persona (Decision D1)."""
    from models.automation_workflow import AutomationWorkflow
    from models.target import Target

    wf_q = select(AutomationWorkflow.id).where(
        AutomationWorkflow.default_persona_id == persona_id
    )
    if exclude_workflow_id is not None:
        wf_q = wf_q.where(AutomationWorkflow.id != exclude_workflow_id)
    if (await db.execute(wf_q)).first() is not None:
        return True

    tgt = (
        await db.execute(
            select(Target.id).where(Target.persona_id == persona_id)
        )
    ).first()
    return tgt is not None


@router.post("/agents/{agent_id}/deploy")
async def deploy_to_agent(
    agent_id: str,
    body: DeployRequest,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_platform_admin),
):
    """Send a coordinator workflow / secret / persona to a fleet-local agent.

    Seals all secret material under the agent's per-agent channel key, dispatches
    the frozen-contract save frame over the persistent WS, and blocks on the
    matching ``*_saved`` ack. On a workflow ack it stamps ``source_workflow_id`` on
    the LocalWorkflow row (deterministic origin correlation, independent of the
    async catalog re-emit). On ``mode=move`` it deletes the coordinator copy — and
    its exclusively-used deps — ONLY after the ack. Never pushes plaintext; never
    trusts an id in the reply (routing target is the authenticated socket).
    """
    from routers.user_recorder_ws import send_and_await
    from models.automation_workflow import AutomationWorkflow
    from models.persona import Persona
    from models.vault_secret import VaultSecret
    from models.local_workflow import LocalWorkflow
    from security.encryption import SecretEncryption
    from services.secret_resolver import resolve_secret

    channel_key = _resolve_deploy_target(agent_id)

    # ----------------------------------------------------------------- workflow
    if body.kind == "workflow":
        wf = (
            await db.execute(
                select(AutomationWorkflow).where(AutomationWorkflow.id == body.id)
            )
        ).scalar_one_or_none()
        if not wf:
            raise HTTPException(status_code=404, detail=f"Workflow {body.id} not found")

        steps = wf.steps or []
        form_data = wf.form_data or {}

        # Gather the plaintext {secretKey: value} map from the coordinator vault +
        # any stored workflow credentials, then seal the whole map for the agent.
        creds_map: dict = {}
        secret_keys: set[str] = set()
        if body.include_deps:
            secret_keys = _workflow_secret_keys(steps, form_data)
            for key in secret_keys:
                value = await resolve_secret(db, key)
                if value is not None:
                    creds_map[key] = value
        # Fold in any credentials stored directly on the workflow.
        if wf.credentials_encrypted:
            try:
                stored = json.loads(SecretEncryption.decrypt_secret(wf.credentials_encrypted))
                if isinstance(stored, dict):
                    for k, v in stored.items():
                        if isinstance(v, str):
                            creds_map.setdefault(k, v)
            except Exception as e:  # pragma: no cover - corrupted/rotated key
                logger.warning("deploy: workflow %s credential decrypt failed: %s", wf.id, e)

        credentials_encrypted = (
            _seal_plaintext_for_agent(json.dumps(creds_map), channel_key)
            if creds_map else None
        )

        # Bundle the default persona (sealed) if requested.
        persona_blob = None
        persona_obj = None
        if body.include_deps and wf.default_persona_id:
            persona_obj = (
                await db.execute(
                    select(Persona).where(Persona.id == wf.default_persona_id)
                )
            ).scalar_one_or_none()
            if persona_obj:
                persona_blob = await _build_persona_blob(db, persona_obj, channel_key)

        # Declared inputs = placeholders that are NOT secret references. Exclude
        # both {{secret:...}} and {{vault:...}} shapes (the latter surfaces as a
        # 'vault:'-prefixed key) as well as any base-name key we already bundled as
        # a vault secret, so the agent's input_schema never advertises an internal
        # secret name as a caller-supplied input.
        all_secret_keys = _workflow_secret_keys(steps, form_data)

        def _is_secret_placeholder(key: str) -> bool:
            if key.startswith("secret:") or key.startswith("vault:"):
                return True
            base = key.rsplit(".", 1)[0] if "." in key else key
            return base in all_secret_keys

        from routers.automation import _extract_placeholders
        declared_inputs = [
            p for p in _extract_placeholders(steps, form_data)
            if not _is_secret_placeholder(str(p.get("key", "")))
        ]

        frame = {
            "type": "save_local_workflow",
            "request_id": str(uuid.uuid4()),
            "name": wf.name,
            "description": wf.description or "",
            "steps": steps,
            "form_data": form_data,
            "declared_inputs": declared_inputs,
            "credentials_encrypted": credentials_encrypted,
            "persona": persona_blob,
            "execution_target": "local",
            "cloud_callable": True,
            "source_workflow_id": wf.id,
        }

        reply = await send_and_await(
            agent_id, frame,
            reply_type="local_workflow_saved",
            correlate_by="request_id",
            # A capable agent acks a local save near-instantly; cap the wait well
            # under the 120s default so a mistargeted / non-hosting agent surfaces
            # a clear error fast instead of appearing to hang.
            timeout=30,
        )
        if not reply or reply.get("error"):
            raise HTTPException(
                status_code=502,
                detail=f"Agent did not confirm workflow save: {(reply or {}).get('error', 'no reply')}",
            )
        local_id = reply.get("local_id")
        recipe_hash = reply.get("recipe_hash")
        if not local_id:
            raise HTTPException(status_code=502, detail="Agent ack missing local_id")

        # Stamp source_workflow_id directly (the catalog re-emit carries every
        # other field but NOT this correlation), so origin is deterministic and
        # does not race the async local_catalog refresh. Scoped to (agent_id,
        # local_id) — never an identity from the reply payload.
        row = (
            await db.execute(
                select(LocalWorkflow).where(
                    LocalWorkflow.agent_id == agent_id,
                    LocalWorkflow.local_id == str(local_id),
                )
            )
        ).scalar_one_or_none()
        now = datetime.now(timezone.utc)
        if row is None:
            row = LocalWorkflow(
                agent_id=agent_id,
                local_id=str(local_id),
                name=wf.name,
                description=wf.description,
                cloud_callable=True,
                status="active",
                last_advertised_at=now,
            )
            db.add(row)
        row.source_workflow_id = wf.id
        if recipe_hash:
            row.recipe_hash = str(recipe_hash)[:255]
        row.updated_at = now

        if body.mode == "move":
            # Delete the coordinator copy ONLY after a successful ack. Ref-count
            # each bundled dep across the REMAINING workflows and delete only the
            # ones used exclusively by this workflow (Decision D1). Deleting the
            # AutomationWorkflow cascades its tasks/webhooks/streaming.
            for key in secret_keys:
                if await _refcount_secret_in_use(db, key, exclude_workflow_id=wf.id):
                    continue
                sec = (
                    await db.execute(select(VaultSecret).where(VaultSecret.key == key))
                ).scalar_one_or_none()
                if sec is not None:
                    await db.delete(sec)

            if persona_obj is not None:
                # Delete the persona only if no OTHER workflow defaults to it AND
                # no Target (monitor) references it via persona_id (SET NULL would
                # silently break the monitor's auth otherwise).
                if not await _refcount_persona_in_use(
                    db, persona_obj.id, exclude_workflow_id=wf.id
                ):
                    # Detach the local handle's source FK first (SET NULL happens on
                    # flush, but we already re-stamped it above — clear it explicitly
                    # so the moved handle reads as local:<agent>, not a dangling ref).
                    row.source_workflow_id = None
                    await db.delete(persona_obj)

            await db.delete(wf)

        await db.commit()
        return {"local_id": local_id, "recipe_hash": recipe_hash, "mode": body.mode}

    # ------------------------------------------------------------------- secret
    if body.kind == "secret":
        sec = (
            await db.execute(select(VaultSecret).where(VaultSecret.id == body.id))
        ).scalar_one_or_none()
        if not sec:
            raise HTTPException(status_code=404, detail=f"Secret {body.id} not found")

        value = SecretEncryption.decrypt_secret(sec.value_encrypted)
        frame = {
            "type": "save_local_secret",
            "request_id": str(uuid.uuid4()),
            "key": sec.key,
            "value_encrypted": _seal_plaintext_for_agent(value, channel_key),
        }
        reply = await send_and_await(
            agent_id, frame,
            reply_type="local_secret_saved",
            correlate_by="request_id",
            # A capable agent acks a local save near-instantly; cap the wait well
            # under the 120s default so a mistargeted / non-hosting agent surfaces
            # a clear error fast instead of appearing to hang.
            timeout=30,
        )
        if not reply or reply.get("error"):
            raise HTTPException(
                status_code=502,
                detail=f"Agent did not confirm secret save: {(reply or {}).get('error', 'no reply')}",
            )

        if body.mode == "move":
            # A standalone explicit secret-move: ref-count against ALL workflows
            # (D1 safety default) and delete only if unused. exclude_workflow_id=0
            # excludes nothing (no real workflow has id 0).
            if not await _refcount_secret_in_use(db, sec.key, exclude_workflow_id=0):
                await db.delete(sec)
                await db.commit()

        return {"key": reply.get("key", sec.key), "mode": body.mode}

    # ------------------------------------------------------------------ persona
    if body.kind == "persona":
        persona = (
            await db.execute(select(Persona).where(Persona.id == body.id))
        ).scalar_one_or_none()
        if not persona:
            raise HTTPException(status_code=404, detail=f"Persona {body.id} not found")

        blob = await _build_persona_blob(db, persona, channel_key)
        frame = {
            "type": "save_local_persona",
            "request_id": str(uuid.uuid4()),
            "name": blob["name"],
            "creds_encrypted": blob["creds_encrypted"],
            "fingerprint": blob["fingerprint"],
            "proxy": blob["proxy"],
            "session_state_encrypted": blob["session_state_encrypted"],
            "totp_seed_encrypted": blob["totp_seed_encrypted"],
        }
        reply = await send_and_await(
            agent_id, frame,
            reply_type="local_persona_saved",
            correlate_by="request_id",
            # A capable agent acks a local save near-instantly; cap the wait well
            # under the 120s default so a mistargeted / non-hosting agent surfaces
            # a clear error fast instead of appearing to hang.
            timeout=30,
        )
        if not reply or reply.get("error"):
            raise HTTPException(
                status_code=502,
                detail=f"Agent did not confirm persona save: {(reply or {}).get('error', 'no reply')}",
            )

        if body.mode == "move":
            # Delete only if no workflow defaults to this persona AND no Target
            # (monitor) references it via persona_id.
            if not await _refcount_persona_in_use(db, persona.id):
                await db.delete(persona)
                await db.commit()

        return {"persona_local_id": reply.get("persona_local_id"), "mode": body.mode}

    raise HTTPException(status_code=400, detail=f"Unknown deploy kind: {body.kind}")


# ============================================================
# POST / GET / DELETE /api/fleet/tokens — mint fleet-connect tokens
# ============================================================
class MintTokenRequest(BaseModel):
    name: str


async def _load_token_registry(db: AsyncSession) -> dict:
    row = await db.execute(select(Config).where(Config.key == _TOKENS_KEY))
    cfg = row.scalar_one_or_none()
    reg = cfg.value if (cfg and isinstance(cfg.value, dict)) else {}
    if "tokens" not in reg or not isinstance(reg.get("tokens"), list):
        reg = {"tokens": []}
    return reg


# The registry is a read-modify-write over ONE JSON row, and every mint does
# load -> append -> save with awaits in between. Two overlapping mints therefore
# interleave: on a fresh install both see no row and both INSERT (UNIQUE
# constraint failed: config.key), and once the row exists both UPDATE and the
# first token silently vanishes from the list.
#
# A process-local lock is the right scope here precisely because the coordinator
# REFUSES to boot with more than one web worker (see main._enforce_single_worker)
# — there is no second process to coordinate with.
_TOKEN_REGISTRY_LOCK = asyncio.Lock()


async def _save_token_registry(db: AsyncSession, reg: dict) -> None:
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm.attributes import flag_modified

    row = await db.execute(select(Config).where(Config.key == _TOKENS_KEY))
    cfg = row.scalar_one_or_none()
    if cfg:
        cfg.value = reg
        # ``Config.value`` is a plain JSON column (not MutableDict). When callers
        # mutate the registry IN PLACE (append a token, flip revoked_at) and then
        # save, ``reg`` is often the SAME object already attached to ``cfg.value``,
        # so the reassignment produces no detectable diff and the change is silently
        # dropped on flush. flag_modified forces the attribute dirty — the codebase's
        # standard fix for in-place JSON edits (see agents.py / recorder_proxy.py).
        flag_modified(cfg, "value")
    else:
        # Defence in depth behind the lock: if anything ever creates this row
        # concurrently, adopt it rather than failing the caller's mint.
        try:
            async with db.begin_nested():
                db.add(Config(key=_TOKENS_KEY, value=reg))
        except IntegrityError:
            row = await db.execute(select(Config).where(Config.key == _TOKENS_KEY))
            cfg = row.scalar_one()
            cfg.value = reg
            flag_modified(cfg, "value")
    await db.flush()


@router.post("/tokens")
async def mint_fleet_token(
    body: MintTokenRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_platform_admin),
):
    """Route wrapper. The work lives in `_mint_fleet_token` so the pairing-code
    flow mints through the identical path — same registry entry, same channel
    key, same revocation semantics — rather than a second implementation that
    could drift."""
    return await _mint_fleet_token(request, db, body.name, operator_named=True)


async def _mint_fleet_token(
    request: Request,
    db: AsyncSession,
    raw_name: str | None,
    *,
    operator_named: bool = False,
) -> dict:
    """Mint a long-lived infrastructure service token for a new fleet agent.

    Returns the raw token ONCE. The stock ``writ-agent`` binary reads its token
    from the ``WRIT_SERVICE_TOKEN`` environment variable and its coordinator URL
    from ``saas.url`` in its config file — there is NO ``--token`` flag and it does
    NOT read ``SAAS_URL``. So the runnable invocation is:
        writ-agent config set saas.url <http_base>
        WRIT_SERVICE_TOKEN=<raw> writ-agent start --headless
    The token is a stateless JWT (generate_service_token) bound to a fresh agent id
    and a per-agent Fernet channel key mirrored in Redis under both the token prefix
    and the agent id, matching the device-flow infrastructure path.
    """
    from utils.recorder_auth import generate_service_token
    from routers.oauth import _generate_token_prefix

    name = (raw_name or "").strip() or "fleet-agent"

    recorder_secret = os.getenv("RECORDER_AUTH_SECRET", "")
    if not recorder_secret:
        raise HTTPException(
            status_code=500,
            detail="RECORDER_AUTH_SECRET not configured — cannot mint a fleet token.",
        )

    agent_id = f"writ-{uuid.uuid4().hex[:12]}"
    # token_id doubles as the token's jti claim: revoking the registry entry
    # (revoked_at) kills the TOKEN itself in validate_recorder_token, not just
    # the agent row it was bound to at mint time.
    token_id = uuid.uuid4().hex[:16]
    token = generate_service_token(
        "",  # single-owner coordinator: no org scoping in the token
        max_sessions=5,
        secret=recorder_secret,
        agent_id=agent_id,
        ttl_hours=24 * 365,  # long-lived fleet token
        jti=token_id,
    )

    token_prefix = _generate_token_prefix(token)

    # Per-agent Fernet channel key (credential sealing over the WS), mirrored in
    # Redis under both the token prefix and the agent id — same as the device flow.
    channel_key_plaintext = None
    try:
        from cryptography.fernet import Fernet as _Fernet
        channel_key_raw = _Fernet.generate_key()
        channel_key_plaintext = channel_key_raw.decode()
        # Fail closed — see routers.oauth._seal_channel_key for why the old
        # plaintext fallback was a silent downgrade rather than a dev convenience.
        from routers.oauth import _seal_channel_key
        channel_key_encrypted = _seal_channel_key(channel_key_plaintext)
        redis_client = getattr(request.app.state, "redis", None)
        if redis_client is None:
            from utils.redis_client import get_redis
            redis_client = get_redis()
        await redis_client.set(f"agent_channel_key:{token_prefix}", channel_key_encrypted)
        await redis_client.set(f"agent_channel_key:{agent_id}", channel_key_encrypted)
    except Exception as e:  # channel key is best-effort; token still valid
        logger.warning("Failed to store channel key for fleet token: %s", e)

    # Serialised: load -> append -> save is a read-modify-write over one JSON
    # row, and two overlapping mints would otherwise race — both INSERTing the
    # row on a fresh install, or one silently overwriting the other's entry.
    # The setup wizard triggered exactly this by requesting a pairing code and a
    # raw token at the same moment.
    async with _TOKEN_REGISTRY_LOCK:
        reg = await _load_token_registry(db)
        reg["tokens"].append({
            "token_id": token_id,
            "name": name,
            # Only a name the OPERATOR typed labels the machine in the fleet
            # list. `name` always has a value (a generated `agent-<timestamp>`
            # for pairing codes, or the "fleet-agent" default), and promoting
            # those to display names would relabel every unnamed agent with a
            # timestamp instead of leaving its id visible.
            "display_name": (raw_name or "").strip() if operator_named else None,
            "agent_id": agent_id,
            "token_prefix": token_prefix,
            "created_at": _now_iso(),
            "revoked_at": None,
        })
        await _save_token_registry(db, reg)
    await db.commit()

    commands = _build_connect_commands(token)

    return {
        "token_id": token_id,
        "name": name,
        "agent_id": agent_id,
        # Raw token returned ONCE.
        "token": token,
        "channel_key": channel_key_plaintext,
        # The actual runnable invocation for the stock binary (WRIT_SERVICE_TOKEN
        # env + saas.url config), plus a docker variant using the image's env.
        "connect_command": commands["connect_command"],
        "docker_command": commands["docker_command"],
        # Acquisition, returned alongside the token so the modal can show the
        # whole path — download, then connect — without a second round trip.
        "install_commands": _build_install_commands(),
        "repo_url": f"https://github.com/{_agent_repo()}",
        "created_at": _now_iso(),
    }


# ============================================================
# Local agent — run one on the coordinator's OWN host
# ============================================================
#
# The token path above assumes a second machine. When the operator is sitting at
# the machine the coordinator runs on (the run-local.sh case, i.e. most first
# installs), the whole mint → install → launch sequence can just be done for them.
# The install step runs the SAME /agent.sh this router hands out above, in
# --download-only mode, so there is no second asset-resolution implementation to
# drift. See services/local_agent.py for the guard rails.


@router.get("/local-agent")
async def local_agent_status(_admin=Depends(require_platform_admin)):
    """Can this host run an agent itself, and is one already running?

    Called BEFORE offering the choice so the UI never proposes a path this host
    cannot take (unsupported platform, containerised coordinator).
    """
    from services import local_agent

    return local_agent.preflight()


@router.post("/local-agent")
async def start_local_agent(
    body: MintTokenRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_platform_admin),
):
    """Mint a token, install the agent on THIS host, and start it.

    The token is minted through the same path as the copy-paste flow (so it lands
    in the registry and stays revocable) and is handed straight to the child
    process's environment — it is never returned to the browser, because nobody
    needs to paste it anywhere.

    ``request`` is threaded through because the mint needs it (the per-agent
    channel key is written to the Redis client on ``app.state``). This route used
    to call the ROUTE FUNCTION ``mint_fleet_token(body, db=…, _admin=…)`` and omit
    it — outside FastAPI nothing fills a route's parameters in, so that call
    raised ``TypeError: missing 1 required positional argument: 'request'`` and
    every "run one on this machine" attempt 500'd before touching the host.
    """
    from services import local_agent

    pre = local_agent.preflight()
    if pre["running"]:
        return {"status": "already_running", **pre}
    if not pre["supported"]:
        raise HTTPException(status_code=400, detail=" ".join(pre["blockers"]))

    saas_url = _http_base()
    if not saas_url:
        raise HTTPException(
            status_code=400,
            detail=(
                "Set WRIT_PUBLIC_URL on the coordinator first — the agent needs a URL "
                "to dial back to."
            ),
        )

    label = (body.name or "").strip()
    minted = await _mint_fleet_token(
        request, db, label or "local-agent", operator_named=bool(label),
    )

    try:
        result = await local_agent.install_and_start(
            saas_url=saas_url,
            token=minted["token"],
            agent_name=minted["name"],
        )
    except local_agent.LocalAgentError as e:
        # The token is already minted and registered; leave it revocable rather
        # than silently orphaned, and tell the operator what actually failed.
        raise HTTPException(status_code=502, detail=str(e))

    return {"status": "started", "agent_id": minted["agent_id"], "name": minted["name"], **result}


@router.delete("/local-agent")
async def stop_local_agent(_admin=Depends(require_platform_admin)):
    """Stop the coordinator-hosted agent (leaves the binary in place)."""
    from services import local_agent

    try:
        return await local_agent.stop()
    except local_agent.LocalAgentError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tokens")
async def list_fleet_tokens(
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_platform_admin),
):
    """List minted fleet tokens (metadata only — the raw token is never returned)."""
    reg = await _load_token_registry(db)
    return {"tokens": reg["tokens"]}


@router.delete("/tokens/{token_id}")
async def revoke_fleet_token(
    token_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_platform_admin),
):
    """Revoke a minted fleet token: mark it revoked, drop its channel key, and
    evict the bound agent's live WS connection if present."""
    reg = await _load_token_registry(db)
    entry = next((t for t in reg["tokens"] if t.get("token_id") == token_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="Fleet token not found")

    entry["revoked_at"] = _now_iso()
    await _save_token_registry(db, reg)

    agent_id = entry.get("agent_id")
    token_prefix = entry.get("token_prefix")

    # Drop the channel key so a re-connect with this token can't seal creds.
    try:
        redis_client = getattr(request.app.state, "redis", None)
        if redis_client is None:
            from utils.redis_client import get_redis
            redis_client = get_redis()
        if token_prefix:
            await redis_client.delete(f"agent_channel_key:{token_prefix}")
        if agent_id:
            await redis_client.delete(f"agent_channel_key:{agent_id}")
    except Exception as e:
        logger.warning("Failed to drop channel key on fleet-token revoke: %s", e)

    # Evict the live connection (best-effort).
    if agent_id:
        try:
            from routers.user_recorder_ws import _connections
            ws = _connections.get(agent_id)
            if ws is not None:
                await ws.close(code=4403)
        except Exception as e:
            logger.warning("Failed to evict agent %s on fleet-token revoke: %s", agent_id, e)

    # Mark the DB agent row revoked so it won't auto-reactivate.
    if agent_id:
        try:
            from models.agent import AgentStatus
            row = await db.execute(select(Agent).where(Agent.agent_id == agent_id))
            agent = row.scalar_one_or_none()
            if agent:
                agent.status = AgentStatus.REVOKED
        except Exception as e:
            logger.warning("Failed to mark agent %s revoked: %s", agent_id, e)

    await db.commit()
    return {"success": True, "token_id": token_id, "agent_id": agent_id}


# ============================================================
# One-line agent enrolment — pairing codes
# ============================================================
#
# The problem this solves: a coordinator running in Docker CANNOT launch an
# agent on your host (services/local_agent.preflight blocks it — a container has
# no host process table and no browser runtime). Since the documented install IS
# Docker, the "run one here" button is dead for essentially every self-host user,
# and the fallback was a ~450-character `docker run` carrying the token, the
# coordinator URL, the doc-extract address, its secret and a host-gateway alias.
#
# Instead the coordinator serves its own installer. It already knows the URL and
# the extraction settings, so none of that needs to travel in the command:
#
#     curl -fsSL http://<coordinator>/agent.sh | sh -s -- WRIT-4K2P-9XQ
#
# The code is the only thing a human handles, so it is short enough to read over
# a call. It is single-use and short-lived: it is exchanged ONCE for a real fleet
# token and deleted on read, exactly like the WS tickets above.

_PAIR_PREFIX = "agent_pair:"
_PAIR_TTL_SECONDS = 15 * 60

# Crockford-style alphabet: no I/L/O/U, so a code cannot be misread or misheard
# as another character when it is dictated or copied off a screen.
_PAIR_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _format_pair_code(raw: str) -> str:
    """`WRIT-XXXX-XXX` — prefixed so it is obvious what it belongs to."""
    return f"WRIT-{raw[:4]}-{raw[4:]}"


def _normalise_pair_code(code: str) -> str:
    """Accept what a human actually types: any case, dashes or not, `WRIT` or not."""
    cleaned = "".join(ch for ch in (code or "").upper() if ch.isalnum())
    if cleaned.startswith("WRIT"):
        cleaned = cleaned[4:]
    return cleaned


async def _pair_redis():
    from utils.redis_client import get_redis

    return get_redis()


class PairCodeResponse(BaseModel):
    code: str
    expires_in: int
    install_command: str
    # The manual fallbacks are returned from the SAME mint. The setup wizard
    # used to request a pairing code and a raw token separately to fill its
    # three tabs, which minted two fleet tokens — two agent identities — for one
    # connection, and raced two writers into the token registry.
    token: str
    agent_id: str
    connect_command: str
    docker_command: str
    install_commands: dict


class PairCodeRequest(BaseModel):
    """Optional label for the machine being enrolled.

    The body is optional as a whole — `POST /pair-code` with no body still
    works, which is what the setup wizard sends.
    """
    name: Optional[str] = None


@router.post("/pair-code", response_model=PairCodeResponse)
async def mint_pair_code(
    request: Request,
    body: Optional[PairCodeRequest] = None,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_platform_admin),
):
    """Mint a short single-use code that enrols one agent.

    Admin-only, like minting a token directly — the code IS an enrolment
    credential, just a short-lived one. It maps to a real fleet token that is
    minted here and now, so revoking the resulting agent works exactly as it
    does for a hand-minted token (the registry entry below is the same one).
    """
    label = (body.name or "").strip() if body else ""
    name = label or f"agent-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    minted = await _mint_fleet_token(request, db, name, operator_named=bool(label))

    redis = await _pair_redis()
    # Retry on collision rather than trusting 32^7 blindly — a silent overwrite
    # would hand two machines the same identity.
    for _ in range(5):
        raw = "".join(secrets.choice(_PAIR_ALPHABET) for _ in range(7))
        if await redis.set(f"{_PAIR_PREFIX}{raw}", json.dumps(minted),
                           ex=_PAIR_TTL_SECONDS, nx=True):
            break
    else:
        raise HTTPException(status_code=503, detail="Could not allocate a pairing code, try again")

    code = _format_pair_code(raw)
    base = _http_base() or "http://localhost:8000"
    return PairCodeResponse(
        code=code,
        expires_in=_PAIR_TTL_SECONDS,
        install_command=f"curl -fsSL {base}/agent.sh | sh -s -- {code}",
        token=minted["token"],
        agent_id=minted["agent_id"],
        connect_command=minted["connect_command"],
        docker_command=minted["docker_command"],
        install_commands=minted["install_commands"],
    )


class PairExchangeRequest(BaseModel):
    code: str


@router.post("/pair-code/exchange")
async def exchange_pair_code(request: Request):
    """Trade a pairing code for the real agent settings. Called by agent.sh.

    UNAUTHENTICATED by necessity — the machine being enrolled has no credential
    yet; the code is the credential. Which is why it is single-use (deleted on
    read, so a code captured from a shell history or a proxy log is already
    spent), short-lived, and rate-limited per IP against brute force. 32^7 is
    ~34 billion codes, and a spent or expired one is indistinguishable from a
    wrong one in the response.
    """
    redis = await _pair_redis()
    await rate_limit(request, redis, max_requests=10, window_seconds=60)

    try:
        body = PairExchangeRequest(**(await request.json()))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request body")

    raw = _normalise_pair_code(body.code)
    if not raw:
        raise HTTPException(status_code=400, detail="Missing pairing code")

    key = f"{_PAIR_PREFIX}{raw}"
    stored = await redis.get(key)
    # Delete BEFORE returning: two machines racing the same code must not both
    # win, and a crash after this point should spend the code rather than leave
    # it replayable.
    await redis.delete(key)
    if not stored:
        raise HTTPException(status_code=404, detail="That pairing code is invalid, already used, or expired")

    minted = json.loads(stored if isinstance(stored, str) else stored.decode())
    return {
        "token": minted["token"],
        "coordinator_url": _http_base() or "",
        "agent_id": minted.get("agent_id"),
        **_doc_extract_env(),
    }
