"""
FastAPI dependencies for request handling.

Provides reusable dependencies for authentication, rate limiting, and resource injection.
"""
import logging
from fastapi import Request

logger = logging.getLogger(__name__)


async def get_redis_client(request: Request):
    """
    Dependency to get Redis client from app state.

    Args:
        request: FastAPI request object

    Returns:
        Redis client instance

    Example:
        @app.get("/endpoint")
        async def endpoint(redis: redis.Redis = Depends(get_redis_client)):
            await redis.set("key", "value")
    """
    # Redis client is stored in app state by main.py's lifespan. If a request
    # somehow arrives before/without lifespan (scripts/tests importing routers),
    # fall back to the shared in-process client rather than 503 — there is no
    # external Redis server to be "unavailable".
    client = getattr(request.app.state, "redis", None)
    if client is None:
        from utils.redis_client import get_redis
        client = get_redis()
    return client
