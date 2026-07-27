"""
Brute force protection with progressive lockout.
Uses Redis sorted sets for tracking failed attempts.
Falls back to in-memory tracking when Redis is unavailable.
"""
import logging
import time
from typing import Optional, Tuple
from redis.asyncio import Redis

from security.memory_fallback import MemoryFallback

logger = logging.getLogger(__name__)

LOCKOUT_TIERS = [
    (5, 60),       # 5 failures → 1 min lockout
    (10, 300),     # 10 failures → 5 min lockout
    (20, 1800),    # 20 failures → 30 min lockout
    (50, 3600),    # 50 failures → 1 hour lockout
]


async def check_brute_force(redis: Optional[Redis], identifier: str) -> Tuple[bool, int]:
    """Check if identifier is locked out. Returns (allowed, retry_after_seconds)."""
    if not redis:
        logger.warning("Redis unavailable for brute force check — using in-memory fallback")
        return MemoryFallback.check_brute_force(identifier)
    try:
        key = f"bf:{identifier}"
        now = time.time()
        # Count failures in the last hour
        await redis.zremrangebyscore(key, 0, now - 3600)
        failures = await redis.zcard(key)
        for threshold, lockout in reversed(LOCKOUT_TIERS):
            if failures >= threshold:
                # Check if still in lockout window
                last_failure = await redis.zrange(key, -1, -1, withscores=True)
                if last_failure:
                    last_time = last_failure[0][1]
                    elapsed = now - last_time
                    if elapsed < lockout:
                        return False, int(lockout - elapsed)
        return True, 0
    except Exception as e:
        logger.error(f"Brute force check error (Redis failed, using memory fallback): {e}")
        return MemoryFallback.check_brute_force(identifier)


async def record_failure(redis: Optional[Redis], identifier: str):
    """Record a failed attempt."""
    if not redis:
        MemoryFallback.record_brute_force_failure(identifier)
        return
    try:
        key = f"bf:{identifier}"
        now = time.time()
        await redis.zadd(key, {f"{now}": now})
        await redis.expire(key, 3600)
    except Exception as e:
        logger.error(f"Brute force record error (using memory fallback): {e}")
        MemoryFallback.record_brute_force_failure(identifier)


async def record_success(redis: Optional[Redis], identifier: str):
    """Clear failures on successful auth."""
    if not redis:
        MemoryFallback.record_brute_force_success(identifier)
        return
    try:
        key = f"bf:{identifier}"
        await redis.delete(key)
    except Exception as e:
        logger.error(f"Brute force clear error (using memory fallback): {e}")
        MemoryFallback.record_brute_force_success(identifier)


async def check_brute_force_multi(redis: Optional[Redis], identifiers: list[str]) -> Tuple[bool, int]:
    """Check several identifiers; blocked if ANY is locked out.

    Login should pass BOTH a per-IP key (catches one IP spraying many accounts)
    and a per-account key (catches many IPs hammering one account). Keying on
    only ip:email lets an attacker bypass the lockout by varying either field.
    Returns (allowed, max_retry_after_seconds).
    """
    worst_retry = 0
    for ident in identifiers:
        allowed, retry_after = await check_brute_force(redis, ident)
        if not allowed:
            worst_retry = max(worst_retry, retry_after)
    return (worst_retry == 0), worst_retry


async def record_failure_multi(redis: Optional[Redis], identifiers: list[str]):
    """Record a failed attempt against every identifier."""
    for ident in identifiers:
        await record_failure(redis, ident)


async def record_success_multi(redis: Optional[Redis], identifiers: list[str]):
    """Clear failures for every identifier on a successful auth."""
    for ident in identifiers:
        await record_success(redis, ident)
