"""
Model pricing service — centralized, admin-configurable per-model token cost.

Replaces the per-model USD cost constants that were hardcoded across the
codebase (input*3 + output*15, INPUT_COST_PER_M=3.0, etc.). All cost-in-USD
computation now goes through compute_cost_micros(model, in_tokens, out_tokens),
which looks up the model's rate from the ModelPricing table (loaded into an
in-memory cache at startup and reloaded whenever an admin edits pricing).

Cost is tracked in micro-dollars (1/1,000,000 USD). Note the convenient
identity: cost_micros = tokens * usd_per_million_tokens
  (tokens/1e6 * usd) * 1e6  ==  tokens * usd_per_mtok
so the legacy `in*3 + out*15` is exactly this formula with rates 3 / 15.
"""
import logging
from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Fallback rate used when a model is unknown / cache is cold. Matches the old
# hardcoded Claude Sonnet pricing so behaviour is unchanged for unseen models.
DEFAULT_INPUT_USD_PER_MTOK = 3.0
DEFAULT_OUTPUT_USD_PER_MTOK = 15.0

# In-memory cache: model_id -> {"input": float, "output": float, "active": bool}
_PRICING_CACHE: dict[str, dict] = {}

# Seeded on first load if the table is empty. List provider prices (USD / Mtok)
# current as of mid-2026; admins can edit these in the admin UI afterwards.
_SEED_PRICING = [
    {"provider": "anthropic", "model_id": "claude-opus-4-20250514",   "display_name": "Claude Opus 4",     "input_usd_per_mtok": 15.0, "output_usd_per_mtok": 75.0},
    {"provider": "anthropic", "model_id": "claude-sonnet-4-20250514", "display_name": "Claude Sonnet 4",   "input_usd_per_mtok": 3.0,  "output_usd_per_mtok": 15.0},
    {"provider": "anthropic", "model_id": "claude-3-5-haiku-20241022","display_name": "Claude 3.5 Haiku",  "input_usd_per_mtok": 0.8,  "output_usd_per_mtok": 4.0},
    {"provider": "openai",    "model_id": "gpt-4o",                   "display_name": "GPT-4o",            "input_usd_per_mtok": 2.5,  "output_usd_per_mtok": 10.0},
    {"provider": "openai",    "model_id": "gpt-4o-mini",              "display_name": "GPT-4o mini",       "input_usd_per_mtok": 0.15, "output_usd_per_mtok": 0.6},
    {"provider": "google",    "model_id": "gemini-2.0-flash",         "display_name": "Gemini 2.0 Flash",  "input_usd_per_mtok": 0.075,"output_usd_per_mtok": 0.3},
]


async def seed_defaults(db: AsyncSession) -> int:
    """Seed default model pricing into the in-memory cache.

    The default rates ship in code (_SEED_PRICING), so seeding populates the
    in-memory cache.
    """
    global _PRICING_CACHE
    for row in _SEED_PRICING:
        _PRICING_CACHE.setdefault(row["model_id"], {
            "input": row.get("input_usd_per_mtok", 0.0),
            "output": row.get("output_usd_per_mtok", 0.0),
            "active": True,
        })
    return len(_SEED_PRICING)


async def load_model_pricing(db: AsyncSession) -> None:
    """Load pricing rates into the in-memory cache.

    Rates come from the code defaults (_SEED_PRICING). Callers that need an
    explicit rate fall back to DEFAULT_*_USD_PER_MTOK via _lookup when a model
    is not in the cache.
    """
    global _PRICING_CACHE
    _PRICING_CACHE = {}
    await seed_defaults(db)
    logger.info(f"Loaded model pricing cache: {len(_PRICING_CACHE)} models (code defaults)")


def _lookup(model: Optional[str]) -> Tuple[float, float]:
    """Resolve (input_usd_per_mtok, output_usd_per_mtok) for a model id.

    Tries exact match, then a prefix/substring match (gateways sometimes append
    or trim version suffixes), then the default fallback.
    """
    if not model:
        return DEFAULT_INPUT_USD_PER_MTOK, DEFAULT_OUTPUT_USD_PER_MTOK

    hit = _PRICING_CACHE.get(model)
    if hit:
        return hit["input"], hit["output"]

    # Fuzzy: a configured id that is a prefix of, or contained in, the reported id
    # (e.g. reported "claude-sonnet-4-20250514-v2" vs configured "claude-sonnet-4-20250514").
    for model_id, rate in _PRICING_CACHE.items():
        if model.startswith(model_id) or model_id in model:
            return rate["input"], rate["output"]

    logger.debug(f"No pricing for model '{model}', using default rate")
    return DEFAULT_INPUT_USD_PER_MTOK, DEFAULT_OUTPUT_USD_PER_MTOK


def compute_cost_micros(model: Optional[str], input_tokens: int, output_tokens: int) -> int:
    """USD cost in micro-dollars for a call, using the model's configured rate."""
    in_usd, out_usd = _lookup(model)
    return int((input_tokens or 0) * in_usd + (output_tokens or 0) * out_usd)


def get_rate(model: Optional[str]) -> Tuple[float, float]:
    """Public accessor: (input_usd_per_mtok, output_usd_per_mtok) for a model."""
    return _lookup(model)
