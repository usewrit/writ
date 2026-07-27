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
