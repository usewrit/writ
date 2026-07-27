"""
Redis-based rate limiting for API endpoints.
Implements sliding window rate limiting algorithm.

FAIL-OPEN decision: this generic API rate limiter is an abuse/DoS
control on read/write API paths, NOT a money or authz gate. On a Redis outage we
deliberately fail OPEN (`fail_open=True`, the default) — availability of the API
wins over throttling, and a brief unthrottled window is an acceptable abuse risk.
Contrast the marketplace per-listing limiter in
`services.marketplace_billing._enforce_listing_rate_limit`, which gates PAID runs
and therefore fails CLOSED (429) on the same Redis outage. Callers that wrap a
spend-sensitive path may opt into fail-closed behaviour by passing
`fail_open=False` to `is_allowed`.
"""
import logging
import time
from typing import Optional
from redis.asyncio import Redis
from fastapi import Request, HTTPException, status
from config import settings

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    Redis-based rate limiter using sliding window algorithm.

    Tracks request counts per identifier (IP, API key, etc.) within
    a time window and enforces configurable limits.
    """

    def __init__(
        self,
        redis_client: Redis,
        max_requests: int = 100,
        window_seconds: int = 60,
        prefix: str = "ratelimit",
    ):
        """
        Initialize rate limiter.

        Args:
            redis_client: Redis client instance
            max_requests: Maximum requests allowed in window
            window_seconds: Time window in seconds
            prefix: Redis key prefix
        """
        self.redis = redis_client
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.prefix = prefix

    async def is_allowed(
        self,
        identifier: str,
        cost: int = 1,
        fail_open: bool = True
    ) -> tuple[bool, dict]:
        """
        Check if request is allowed under rate limit.

        Args:
            identifier: Unique identifier (IP, API key, etc.)
            cost: Request cost (default 1, higher for expensive operations)

        Returns:
            Tuple of (allowed: bool, info: dict)
            info contains: remaining, reset_at, total

        Example:
            >>> allowed, info = await limiter.is_allowed("192.168.1.1")
            >>> if not allowed:
            ...     raise HTTPException(429, "Rate limit exceeded")
        """
        key = f"{self.prefix}:{identifier}"
        now = int(time.time())
        window_start = now - self.window_seconds

        try:
            # Use Redis pipeline for atomic operations
            pipe = self.redis.pipeline()

            # Remove old entries outside the window
            pipe.zremrangebyscore(key, 0, window_start)

            # Count requests in current window
            pipe.zcard(key)

            # Add current request
            pipe.zadd(key, {f"{now}:{time.time_ns()}": now})

            # Set expiry on the key
            pipe.expire(key, self.window_seconds + 10)

            results = await pipe.execute()

            # results[1] is the count before adding current request
            current_count = results[1]

            # Check if limit exceeded
            allowed = (current_count + cost) <= self.max_requests
            remaining = max(0, self.max_requests - current_count - cost)
            reset_at = now + self.window_seconds

            info = {
                "remaining": remaining,
                "reset_at": reset_at,
                "limit": self.max_requests,
                "window": self.window_seconds,
            }

            if not allowed:
                logger.warning(
                    f"Rate limit exceeded for {identifier}: "
                    f"{current_count}/{self.max_requests}"
                )
                # Remove the request we just added since it's not allowed
                await self.redis.zremrangebyscore(key, now, now)

            return allowed, info

        except Exception as e:
            logger.error(f"Rate limiter error: {e}")
            if fail_open:
                logger.warning(
                    f"Rate limiter failing OPEN for {identifier} — "
                    f"Redis unavailable, requests are unthrottled"
                )
                return True, {
                    "remaining": self.max_requests,
                    "reset_at": now + self.window_seconds,
                    "limit": self.max_requests,
                    "window": self.window_seconds,
                }
            else:
                return False, {"remaining": 0, "error": "rate_limiter_unavailable"}

    async def reset(self, identifier: str) -> None:
        """
        Reset rate limit for an identifier.

        Args:
            identifier: Identifier to reset
        """
        key = f"{self.prefix}:{identifier}"
        await self.redis.delete(key)
        logger.info(f"Rate limit reset for {identifier}")


async def rate_limit(
    request: Request,
    redis_client: Redis,
    max_requests: Optional[int] = None,
    window_seconds: Optional[int] = None,
) -> None:
    """
    Rate limiting dependency for FastAPI routes.

    Args:
        request: FastAPI request object
        redis_client: Redis client (will be injected)
        max_requests: Max requests per window (uses settings default if None)
        window_seconds: Window size in seconds (uses settings default if None)

    Raises:
        HTTPException: 429 if rate limit exceeded

    Example:
        @app.get("/api/endpoint")
        async def endpoint(
            _rate_limit: None = Depends(rate_limit)
        ):
            return {"status": "ok"}
    """
    # Use settings defaults if not specified
    max_requests = max_requests or settings.rate_limit_requests
    window_seconds = window_seconds or settings.rate_limit_window

    # Create rate limiter instance
    limiter = RateLimiter(
        redis_client=redis_client,
        max_requests=max_requests,
        window_seconds=window_seconds,
    )

    # Use API key if available, otherwise use IP address
    identifier = None

    # Try to get API key from request state (set by auth middleware)
    if hasattr(request.state, "api_key"):
        identifier = f"apikey:{request.state.api_key.get('id')}"
    else:
        # Fall back to IP address
        client_ip = request.client.host if request.client else "unknown"
        identifier = f"ip:{client_ip}"

    # Check rate limit
    allowed, info = await limiter.is_allowed(identifier)

    # Add rate limit headers to response
    request.state.rate_limit_info = info

    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "Rate limit exceeded",
                "limit": info["limit"],
                "window": info["window"],
                "reset_at": info["reset_at"],
            },
            headers={
                "X-RateLimit-Limit": str(info["limit"]),
                "X-RateLimit-Remaining": str(info["remaining"]),
                "X-RateLimit-Reset": str(info["reset_at"]),
                "Retry-After": str(window_seconds),
            },
        )


def get_rate_limiter(redis_client: Redis) -> RateLimiter:
    """
    Factory function to create rate limiter instance.

    Args:
        redis_client: Redis client

    Returns:
        Configured RateLimiter instance
    """
    return RateLimiter(
        redis_client=redis_client,
        max_requests=settings.rate_limit_requests,
        window_seconds=settings.rate_limit_window,
    )
