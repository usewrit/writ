"""BYO (bring-your-own) AI routing — run backend-originated AI on the owner's OWN
agent keys instead of the managed AI gateway.

When the user configures AI provider keys inside their Writ agent(s), the
agent advertises ``ai_keys_configured`` on its direct WS connection (recorded on
``user_recorder_ws._agent_meta``). This module lets the backend AI chokepoint
(:func:`services.ai_gateway_client.complete`) route a completion DOWN to one of
those connected agents — the keys never leave the user's machine and no managed
AI is used.

Scale / "multiple clients" handling
-----------------------------------
The owner can have many agents connected at once (laptop + desktop + a VM), only
some of which have keys. :func:`route` therefore:

  * lists the owner's user-hosted agents that advertise keys and hold a live WS
    socket (infra/shared agents don't hold the user's keys);
  * orders them LEAST-LOADED first using a shared Redis in-flight counter (so load
    spreads across the clients instead of hammering one box), fastest-first on ties;
  * dispatches to the best agent and FAILS OVER to the next on timeout / error /
    ``agent_busy`` (the agent rejects-when-busy as the authoritative cap);
  * applies the configured policy when no agent can serve the request.

Trust boundary
--------------
Per the "never trust BYO agents" rule, the agent's self-reported ``usage`` is
informational only. The forgery guard on the direct socket
(``user_recorder_ws._handle_task_result`` / ``_dispatched_tasks``) ensures one
agent cannot resolve another agent's in-flight request.
"""
import logging
import os
import uuid
from typing import Any, Dict, List, Optional, Tuple

from services.ai_gateway_client import AIGatewayError

logger = logging.getLogger(__name__)

# Per-attempt wait for an agent's reply. Vision / long generations can be slow;
# the candidate list is pre-filtered to live agents, so a dead box is excluded
# rather than eating this timeout.
BYO_AI_ATTEMPT_TIMEOUT = float(os.getenv("BYO_AI_ATTEMPT_TIMEOUT", "120"))
# Max distinct agents to try before giving up (failover breadth).
BYO_AI_MAX_ATTEMPTS = int(os.getenv("BYO_AI_MAX_ATTEMPTS", "3"))

# Shared Redis in-flight counter per agent — a soft load-balancing hint (the agent
# semaphore is the hard cap). TTL self-heals a counter orphaned by a crash.
_INFLIGHT_KEY = "byo-ai:inflight:"
_INFLIGHT_TTL = 300

# BYO routing mode from the environment: ``off`` | ``prefer`` | ``strict``.
# Single-user coordinator: one owner-level policy, no per-org DB lookup.
_BYO_MODE = (os.getenv("BYO_AI_MODE", "off") or "off").strip().lower()

# Defense-in-depth: never BYO-route marketplace/consumer purposes (belt-and-suspenders
# guard; the coordinator has no such flows, but the deny list stays as a safety net).
_DENY_PURPOSE_PREFIXES = ("marketplace", "consumer", "buyer")


class BYOUnavailable(AIGatewayError):
    """Raised in ``strict`` mode when no BYO agent could serve the request.

    Subclasses AIGatewayError so existing AI error handling (e.g. agent_brain)
    surfaces it as an AI failure rather than a 500.
    """


def invalidate_mode_cache(*_args, **_kwargs) -> None:
    """No-op: the BYO mode is read from the environment, so there is no cache to
    invalidate. Kept for call-site compatibility."""
    return


def _purpose_denied(purpose: Optional[str]) -> bool:
    p = (purpose or "").lower()
    return any(p.startswith(d) for d in _DENY_PURPOSE_PREFIXES)


async def get_byo_mode(*_args, **_kwargs) -> str:
    """Return the BYO routing mode: ``off`` | ``prefer`` | ``strict``.

    Single-user coordinator: a single owner-level policy from ``BYO_AI_MODE``
    (default ``off`` = use the managed gateway)."""
    mode = _BYO_MODE
    return mode if mode in ("off", "prefer", "strict") else "off"


def _list_candidates(owner_user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """The owner's live, key-bearing user-hosted agents from the in-process
    directly-connected fleet (``user_recorder_ws``).

    Liveness = an open socket in ``_connections``, so no separate staleness filter
    is needed.
    An agent advertises AI keys via the ``ai_keys_configured`` connect param, which
    the WS handler records on ``_agent_meta``.
    """
    from routers.user_recorder_ws import get_connected_recorders

    out: List[Dict[str, Any]] = []
    for rec in get_connected_recorders():
        # Infra/shared agents don't hold the user's keys.
        if (rec.get("role") or "") == "infrastructure":
            continue
        if not rec.get("ai_keys_configured"):
            continue
        # In a shared/multi-tenant fleet, only route to the requesting owner's
        # own agents (they hold that user's keys). Single-owner passes None and
        # keeps every candidate (audit #32).
        if owner_user_id is not None and str(rec.get("user_id") or "") != str(owner_user_id):
            continue
        out.append({
            "agent_id": str(rec.get("agent_id")),
            # The direct socket advertises no perf score; ordering falls back to the
            # in-flight counter then agent_id (deterministic).
            "perf": 0,
        })
    return out


async def _inflight(redis, agent_id: str) -> int:
    try:
        v = await redis.get(_INFLIGHT_KEY + agent_id)
        return max(0, int(v or 0))
    except Exception:
        return 0


async def online_byo_agents(*_args, **_kwargs) -> List[str]:
    """agent_ids of the owner's connected, live, key-bearing agents.

    Used by the settings UI to show how many of the user's agents can serve BYO
    AI right now (so a ``strict`` policy is understood when it would fail-closed).
    """
    return [c["agent_id"] for c in _list_candidates(_kwargs.get("owner_user_id"))]


async def _ordered_agents(redis, owner_user_id: Optional[str] = None) -> List[str]:
    """Candidate agent_ids, least-loaded first (fastest-first on ties)."""
    cands = _list_candidates(owner_user_id)
    scored = []
    for c in cands:
        infl = await _inflight(redis, c["agent_id"])
        # (in-flight asc, perf desc, agent_id) — deterministic, spreads load.
        scored.append((infl, -c["perf"], c["agent_id"]))
    scored.sort()
    return [a for _, _, a in scored]


async def route(
    *,
    messages: List[Dict[str, Any]],
    system: Optional[str] = None,
    max_tokens: int = 500,
    model: Optional[str] = None,
    purpose: Optional[str] = None,
    owner_user_id: Optional[str] = None,
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Try to satisfy a completion on the owner's own BYO agent(s).

    Returns ``(handled, result)``:
      * ``(True, result)``  — a BYO agent answered; ``result`` matches the
        ai_gateway_client.complete shape (``content`` / ``usage`` / ``model``).
      * ``(False, None)``   — caller should fall back to the managed gateway
        (mode ``off``/``prefer`` with no agent, or a denied purpose).

    Raises :class:`BYOUnavailable` in ``strict`` mode when no agent can serve the
    request, so the prompt never silently falls back to managed keys.
    """
    mode = await get_byo_mode()
    if mode == "off" or _purpose_denied(purpose):
        return False, None

    from utils.redis_client import get_redis
    redis = get_redis()

    agents = await _ordered_agents(redis, owner_user_id)
    if not agents:
        if mode == "strict":
            raise BYOUnavailable("No agent with AI keys is online")
        return False, None

    from routers.user_recorder_ws import send_and_await

    last_err: Optional[str] = None
    for agent_id in agents[:BYO_AI_MAX_ATTEMPTS]:
        try:
            await redis.incr(_INFLIGHT_KEY + agent_id)
            await redis.expire(_INFLIGHT_KEY + agent_id, _INFLIGHT_TTL)
        except Exception:
            pass

        reply: Optional[Dict[str, Any]] = None
        try:
            reply = await send_and_await(
                agent_id,
                {
                    "type": "ai_completion_request",
                    "request_id": str(uuid.uuid4()),
                    "payload": {
                        "messages": messages,
                        "system": system,
                        "max_tokens": max_tokens,
                        "model": model,
                    },
                },
                reply_type="ai_completion_response",
                correlate_by="request_id",
                timeout=BYO_AI_ATTEMPT_TIMEOUT,
            )
        except Exception as e:
            last_err = str(e)
            logger.warning("BYO AI dispatch to %s failed: %s", agent_id, e)
        finally:
            try:
                await redis.decr(_INFLIGHT_KEY + agent_id)
            except Exception:
                pass

        if reply and not reply.get("error"):
            usage = reply.get("usage") or {}
            return True, {
                "content": reply.get("content", ""),
                "usage": {
                    "input_tokens": usage.get("input_tokens", usage.get("prompt_tokens", 0)),
                    "output_tokens": usage.get("output_tokens", usage.get("completion_tokens", 0)),
                },
                "model": reply.get("model") or model,
                "byo": True,
            }

        # agent_busy / transient error / no reply → fail over to the next agent.
        last_err = (reply or {}).get("error") or last_err or "no reply"

    # Every candidate failed.
    if mode == "strict":
        raise BYOUnavailable(f"All BYO agents failed to serve the request ({last_err})")
    logger.info("BYO AI exhausted (%s) — falling back to managed", last_err)
    return False, None
