"""
Platform-global AI output-token ceilings (cost control).

A single global `Config` row (`ai_output_token_limits`) stores an optional cap
per feature *bucket*. A cap is a CEILING: the effective budget for any AI call is
``min(call_site_default, cap)`` — an admin cap can only LOWER token usage, never
raise it. A missing / null / non-positive cap means "use the built-in default".

Buckets group the individual AI endpoints so the admin sees a handful of knobs
instead of one-per-endpoint:

* ``assist``   — chat, generate-extract, find-selectors
* ``agent``    — build-scraper, agent loop, generate-streaming-script, generate-automation
* ``optimize`` — optimize-workflow
* ``repair``   — AI auto-repair (streaming script / function repair)
"""
from __future__ import annotations

from typing import Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.config import Config

# Config key holding the limits dict, e.g. {"assist": 1200, "agent": null, ...}
CONFIG_KEY = "ai_output_token_limits"

BUCKETS = ("assist", "agent", "optimize", "repair")

# Built-in per-bucket ceiling = max output-token default across the bucket's
# endpoints. Surfaced to the admin UI as the "built-in default" hint and the
# sensible upper bound for the input field. Keep in sync with the call sites.
DEFAULTS: Dict[str, int] = {
    "assist": 2000,
    "agent": 3000,
    "optimize": 6000,
    "repair": 4000,
}

# Hard sanity bounds for an admin-entered cap.
_MIN_CAP = 128
_MAX_CAP = 100_000


def _coerce_cap(value) -> Optional[int]:
    """Return a positive int cap, or None if the value is unset/degenerate."""
    if value is None:
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return max(_MIN_CAP, min(_MAX_CAP, n))


async def get_limits(db: AsyncSession) -> Dict[str, Optional[int]]:
    """Return the stored cap per bucket (None where no cap is set)."""
    row = await db.execute(select(Config).where(Config.key == CONFIG_KEY))
    cfg = row.scalar_one_or_none()
    raw = (cfg.value or {}) if cfg else {}
    if not isinstance(raw, dict):
        raw = {}
    return {b: _coerce_cap(raw.get(b)) for b in BUCKETS}


async def set_limits(db: AsyncSession, updates: Dict[str, Optional[int]]) -> Dict[str, Optional[int]]:
    """Merge caps into the stored config and persist. Returns the new limits.

    A key present with a null/non-positive value clears that bucket's cap.
    Callers own the surrounding transaction commit.
    """
    row = await db.execute(select(Config).where(Config.key == CONFIG_KEY))
    cfg = row.scalar_one_or_none()
    current = dict(cfg.value) if (cfg and isinstance(cfg.value, dict)) else {}

    for bucket, value in updates.items():
        if bucket not in BUCKETS:
            continue
        cap = _coerce_cap(value)
        if cap is None:
            current.pop(bucket, None)
        else:
            current[bucket] = cap

    if cfg is None:
        cfg = Config(key=CONFIG_KEY, value=current)
        db.add(cfg)
    else:
        cfg.value = current
    await db.commit()

    return {b: _coerce_cap(current.get(b)) for b in BUCKETS}


async def resolve_max_tokens(db: AsyncSession, bucket: str, default: int) -> int:
    """Effective output-token budget for a call = min(default, admin cap-if-any)."""
    limits = await get_limits(db)
    cap = limits.get(bucket)
    if cap and cap > 0:
        return min(default, cap)
    return default


# ---------------------------------------------------------------------------
# INPUT-side budget: how much replayed transcript the agent loop may carry.
# ---------------------------------------------------------------------------
# The output caps above bound what the model WRITES. Nothing bounded what it
# READS, so a multi-iteration agent loop grew its prompt until the provider
# rejected it ("prompt is too long" / "maximum context length"). This resolves a
# character budget for agent_brain's replayed transcript from the CONFIGURED
# provider, since a hosted frontier model and a local 8k-context model cannot
# carry remotely the same thread.
#
# Characters, not tokens: the brain is pure/stdlib and has no tokenizer. ~4
# chars/token is the standard English approximation and we stay well under the
# window regardless, because the transcript is only one part of the prompt (the
# system prompt, steps, observation and captured API calls sit outside it).

# Provider kind → transcript budget in characters.
#   local  = Ollama / llama.cpp on the operator's own box. Default context there
#            is commonly 4k–8k tokens, so the thread gets ~6k tokens and the rest
#            of the prompt still has room.
#   others = hosted models with ≥128k windows.
_THREAD_BUDGET_BY_PROVIDER: Dict[str, int] = {
    "local": 24_000,
    "anthropic": 120_000,
    "openai": 120_000,
    "openai_responses": 120_000,
    "openrouter": 120_000,
}
_THREAD_BUDGET_FALLBACK = 60_000

# Operator override, e.g. a local server started with a large `num_ctx`.
_THREAD_BUDGET_ENV = "AI_THREAD_CHAR_BUDGET"
_MIN_THREAD_BUDGET = 4_000
_MAX_THREAD_BUDGET = 600_000


async def resolve_thread_char_budget() -> int:
    """Character budget for the agent loop's replayed transcript.

    ``AI_THREAD_CHAR_BUDGET`` wins when set; otherwise it follows the active
    provider. Never raises — an unreadable provider row falls back to the
    conservative default rather than failing the turn.
    """
    import os

    raw = (os.getenv(_THREAD_BUDGET_ENV) or "").strip()
    if raw:
        try:
            return max(_MIN_THREAD_BUDGET, min(_MAX_THREAD_BUDGET, int(raw)))
        except ValueError:
            pass

    try:
        from services.local_ai import _active_provider
        provider = await _active_provider()
    except Exception:
        provider = None
    kind = getattr(provider, "provider", None)
    return _THREAD_BUDGET_BY_PROVIDER.get(kind or "", _THREAD_BUDGET_FALLBACK)
