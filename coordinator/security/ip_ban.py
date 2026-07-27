"""
IP banning mechanism with auto-ban for persistent offenders.
Uses Redis for storage with automatic expiry.
Falls back to in-memory ban set when Redis is unavailable.
"""
import logging
from typing import Optional
from redis.asyncio import Redis

from security.memory_fallback import MemoryFallback

logger = logging.getLogger(__name__)

AUTO_BAN_THRESHOLD = 100  # offenses per hour triggers auto-ban
DEFAULT_BAN_DURATION = 86400  # 24 hours


async def is_banned(redis: Optional[Redis], ip: str) -> bool:
    """Check if an IP is banned."""
    if not redis:
        logger.warning("Redis unavailable for IP ban check — using in-memory fallback")
        return MemoryFallback.is_ip_banned(ip)
    try:
        return await redis.exists(f"ip:ban:{ip}") > 0
    except Exception:
        return MemoryFallback.is_ip_banned(ip)


async def ban_ip(redis: Optional[Redis], ip: str, duration: int = DEFAULT_BAN_DURATION, reason: str = ""):
    """Ban an IP for a specified duration."""
    if not redis:
        MemoryFallback.ban_ip(ip, duration)
        logger.warning(f"IP banned (in-memory): {ip} for {duration}s reason={reason}")
        return
    try:
        await redis.setex(f"ip:ban:{ip}", duration, reason or "banned")
        logger.warning(f"IP banned: {ip} for {duration}s reason={reason}")
    except Exception as e:
        logger.error(f"IP ban error (using memory fallback): {e}")
        MemoryFallback.ban_ip(ip, duration)


async def record_offense(redis: Optional[Redis], ip: str) -> int:
    """Record an offense. Auto-bans if threshold exceeded. Returns offense count."""
    if not redis:
        return 0
    try:
        key = f"ip:offenses:{ip}"
        count = await redis.incr(key)
        await redis.expire(key, 3600)
        if count >= AUTO_BAN_THRESHOLD:
            await ban_ip(redis, ip, DEFAULT_BAN_DURATION, f"auto-ban: {count} offenses/hour")
        return count
    except Exception as e:
        logger.error(f"IP offense record error: {e}")
        return 0


async def unban_ip(redis: Optional[Redis], ip: str):
    """Remove an IP ban."""
    if not redis:
        return
    try:
        await redis.delete(f"ip:ban:{ip}")
        logger.info(f"IP unbanned: {ip}")
    except Exception as e:
        logger.error(f"IP unban error: {e}")
