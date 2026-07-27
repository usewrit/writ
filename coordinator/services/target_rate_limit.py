"""
Scraping-abuse guardrails for the run path.

Three distinct guards, all applied at the central run choke point alongside the
`domain_guard` blocklist check:

1. Per-target-domain rate limiting — a Redis sliding-window throttle keyed
   by target host so a single workflow can't hammer one third-party site harder
   than a configurable ceiling, independent of the 60s anti-detection floor
   (which only spaces *one* recorder's checks; it does nothing about many
   workflows/agents fanning at the same domain). FAIL-OPEN: this is an
   abuse/courtesy control, not a money or authz gate — on a Redis outage we admit
   the run rather than block legitimate traffic, mirroring the `domain_guard`
   blocklist and the generic API `security.rate_limit` limiter.

1b. Aggregate run-rate cap (DDoS-amplifier guard) — the per-domain throttle above
   only bounds traffic to ONE host; the browser fleet could still be weaponised as
   a distributed-DoS amplifier by fanning a high run rate ACROSS many distinct
   target domains (each individually under the per-domain ceiling). This second
   sliding-window throttle bounds the TOTAL dispatch rate regardless of target so
   the fleet can't be turned into an amplification cannon. Same FAIL-OPEN posture
   as guard 1 (abuse control, not a money/authz gate). Deliberately generous by
   default so it never bites legitimate fleets — a ceiling against pathological
   abuse, not a quota.

2. Prohibited-category screening — a synchronous, no-DB static screen for
   clearly-illegal target categories (CSAM, fraud/carding, etc.) that the
   reactive admin `domain_guard` blocklist can't be relied on to have caught yet.
   This is a legality guardrail (CFAA / acceptable-use), so it FAILS CLOSED by
   construction: a host/URL matching a prohibited pattern is refused (403). It is
   intentionally conservative (high-precision patterns only) to avoid blocking
   legitimate sites — the broad, admin-curated blocklist remains the main tool.

Both are config-tunable via `settings` with safe baked-in defaults so the module
is self-contained even if the settings fields are absent.
"""
import logging
import time
from typing import Optional
from urllib.parse import urlparse

from fastapi import HTTPException, status

from config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config (read defensively so this module works even if settings lack the field)
# ---------------------------------------------------------------------------
def _target_rate_limit() -> tuple[int, int]:
    """Return (max_runs, window_seconds) for the per-domain throttle.

    max_runs <= 0 disables the throttle. Defaults: 30 runs / 60s per domain.
    """
    max_runs = int(getattr(settings, "target_domain_rate_limit", 30) or 0)
    window = int(getattr(settings, "target_domain_rate_window_secs", 60) or 60)
    return max_runs, window


def _run_rate_limit() -> tuple[int, int]:
    """Return (max_runs, window_seconds) for the AGGREGATE run-rate throttle.

    This is the DDoS-amplifier guard: it bounds the total dispatch rate across
    ALL target domains, catching the fan-out-across-many-hosts pattern the
    per-domain cap can't see. max_runs <= 0 disables it. Default is deliberately
    generous (300 runs / 60s) so it only trips on pathological abuse — comfortably
    above the per-domain ceiling so normal single-domain bursts never hit it.
    """
    max_runs = int(getattr(settings, "run_rate_limit", 300) or 0)
    window = int(getattr(settings, "run_rate_window_secs", 60) or 60)
    return max_runs, window


def _extract_host(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    try:
        host = urlparse(url).hostname
    except Exception:
        return None
    return host.lower().strip() if host else None


# ---------------------------------------------------------------------------
# Guard 1 — per-target-domain rate limiting
# ---------------------------------------------------------------------------
async def enforce_target_rate_limit(
    scope: Optional[str], url: Optional[str]
) -> None:
    """Throttle runs against one target host. 429 when exceeded.

    No-op when disabled or when there is no resolvable host. FAIL-OPEN on a Redis
    outage — see module docstring. ``scope`` is an optional key namespace
    (kept for call-site compatibility; unused in the single-owner coordinator).
    """
    max_runs, window = _target_rate_limit()
    host = _extract_host(url)
    if max_runs <= 0 or not host:
        return

    try:
        from utils.redis_client import get_redis
        r = get_redis()
        key = f"target:rate:{host}"
        now = time.time()
        window_start = now - window

        pipe = r.pipeline()
        pipe.zremrangebyscore(key, 0, window_start)
        pipe.zcard(key)
        pipe.zadd(key, {f"{now}:{time.time_ns()}": now})
        pipe.expire(key, window + 10)
        results = await pipe.execute()

        # results[1] is the count BEFORE adding the current run.
        current = int(results[1] or 0)
        if current >= max_runs:
            # Undo the run we just recorded so a rejected attempt doesn't count.
            try:
                await r.zremrangebyscore(key, now, now)
            except Exception:
                pass
            logger.warning(
                "[TargetRateLimit] host=%s over cap (%s/%s in %ss)",
                host, current, max_runs, window,
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "target_rate_limited",
                    "host": host,
                    "limit": max_runs,
                    "window_seconds": window,
                },
            )
    except HTTPException:
        raise
    except Exception as e:  # Redis outage → FAIL OPEN (abuse control, not money)
        logger.warning(
            "[TargetRateLimit] check failed for host=%s (fail-open, allowing): %s",
            host, e,
        )


# ---------------------------------------------------------------------------
# Aggregate run-rate cap (DDoS-amplifier guard)
# ---------------------------------------------------------------------------
async def enforce_run_rate_limit(scope: Optional[str] = None) -> None:
    """Bound the TOTAL run-dispatch rate across all target domains. 429 when
    exceeded.

    Complements `enforce_target_rate_limit`: that caps traffic to a SINGLE host,
    this caps the aggregate so the fleet can't be used as a DDoS amplifier by
    fanning runs across many distinct hosts (each under the per-domain ceiling).

    No-op when disabled. FAIL-OPEN on a Redis outage — see module docstring (abuse
    control, not a money/authz gate). ``scope`` is kept for call-site
    compatibility; unused in the single-owner coordinator.
    """
    max_runs, window = _run_rate_limit()
    if max_runs <= 0:
        return

    try:
        from utils.redis_client import get_redis
        r = get_redis()
        key = "run:runrate:global"
        now = time.time()
        window_start = now - window

        pipe = r.pipeline()
        pipe.zremrangebyscore(key, 0, window_start)
        pipe.zcard(key)
        pipe.zadd(key, {f"{now}:{time.time_ns()}": now})
        pipe.expire(key, window + 10)
        results = await pipe.execute()

        # results[1] is the count BEFORE adding the current run.
        current = int(results[1] or 0)
        if current >= max_runs:
            # Undo the run we just recorded so a rejected attempt doesn't count.
            try:
                await r.zremrangebyscore(key, now, now)
            except Exception:
                pass
            logger.warning(
                "[RunRate] over aggregate cap (%s/%s in %ss) — "
                "possible DDoS-amplifier abuse",
                current, max_runs, window,
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "run_rate_limited",
                    "limit": max_runs,
                    "window_seconds": window,
                },
            )
    except HTTPException:
        raise
    except Exception as e:  # Redis outage → FAIL OPEN (abuse control, not money)
        logger.warning(
            "[RunRate] check failed (fail-open, allowing): %s", e,
        )


# ---------------------------------------------------------------------------
# Guard 2 — prohibited-category screening
# ---------------------------------------------------------------------------
# High-precision host-substring patterns for categories we will never automate
# against (illegal/harmful). Kept deliberately narrow to avoid false positives;
# the admin-curated `domain_guard` blocklist remains the broad tool. Matching is
# substring-on-host (case-insensitive) — e.g. a host containing "childporn".
_PROHIBITED_HOST_PATTERNS: tuple[str, ...] = (
    # CSAM / child exploitation
    "childporn",
    "childp0rn",
    "cp-",
    "lolicon",
    "jailbait",
    # Fraud / carding / stolen-credential markets
    "carder",
    "carding",
    "cvvshop",
    "cvv-shop",
    "dumpsshop",
    "fullzshop",
    "stolencards",
    "fraudbazar",
    # Other clearly-illegal marketplaces
    "hitman",
    "killforhire",
)

# Category label per pattern prefix group, for the audit/refusal message.
def _prohibited_category(host: str) -> Optional[str]:
    h = host.lower()
    for pat in _PROHIBITED_HOST_PATTERNS:
        if pat in h:
            if pat in ("childporn", "childp0rn", "cp-", "lolicon", "jailbait"):
                return "child_exploitation"
            if pat in ("hitman", "killforhire"):
                return "violence_for_hire"
            return "fraud"
    return None


def prohibited_category_reason(url: Optional[str]) -> Optional[str]:
    """Synchronous, no-DB screen: return a category string if `url`'s host is a
    prohibited category, else None. Use on hot run paths (no transaction touch)."""
    host = _extract_host(url)
    if not host:
        return None
    return _prohibited_category(host)


def enforce_prohibited_category(url: Optional[str]) -> None:
    """Refuse (403) a run whose target host is a prohibited category. No-op when
    the URL is empty or doesn't match. FAILS CLOSED by construction (a static,
    no-dependency screen — there is nothing to be unavailable)."""
    category = prohibited_category_reason(url)
    if category is None:
        return
    host = _extract_host(url)
    logger.warning(
        "[ProhibitedCategory] refused run to host=%s (category=%s)", host, category
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": "prohibited_target",
            "category": category,
            "message": (
                "This target is in a prohibited category and cannot be automated "
                "under the Acceptable Use Policy."
            ),
        },
    )
