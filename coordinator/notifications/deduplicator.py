"""
Notification deduplication using Redis.
Prevents sending duplicate alerts within a time window.
"""
import logging
import hashlib
from typing import Optional
from redis.asyncio import Redis

logger = logging.getLogger(__name__)


class NotificationDeduplicator:
    """
    Redis-based notification deduplication.

    Prevents sending duplicate notifications for the same event
    within a configurable time window.
    """

    def __init__(
        self,
        redis_client: Redis,
        ttl_seconds: int = 300,  # 5 minutes default
        prefix: str = "notif_dedup",
    ):
        """
        Initialize deduplicator.

        Args:
            redis_client: Redis client instance
            ttl_seconds: Time-to-live for deduplication keys
            prefix: Redis key prefix
        """
        self.redis = redis_client
        self.ttl_seconds = ttl_seconds
        self.prefix = prefix

    def _generate_key(self, target_url: str, content_hash: str) -> str:
        """
        Generate deduplication key.

        Args:
            target_url: URL that changed
            content_hash: Hash of the changed content

        Returns:
            Redis key for deduplication
        """
        # Create a hash of the notification content
        content = f"{target_url}:{content_hash}"
        key_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
        return f"{self.prefix}:{key_hash}"

    async def should_send(
        self,
        target_url: str,
        content_hash: str,
    ) -> bool:
        """
        Check if notification should be sent (not a duplicate).

        Args:
            target_url: URL that changed
            content_hash: Hash of the changed content

        Returns:
            True if notification should be sent, False if duplicate
        """
        key = self._generate_key(target_url, content_hash)

        try:
            # Try to set the key with NX (only if not exists)
            was_set = await self.redis.set(
                key,
                "1",
                ex=self.ttl_seconds,
                nx=True,
            )

            # If key was set, it's not a duplicate
            should_send = bool(was_set)

            if not should_send:
                logger.info(
                    f"Duplicate notification suppressed for {target_url} "
                    f"(hash: {content_hash[:8]}...)"
                )
            else:
                logger.debug(
                    f"New notification allowed for {target_url} "
                    f"(hash: {content_hash[:8]}...)"
                )

            return should_send

        except Exception as e:
            logger.error(f"Deduplication check error: {e}")
            # Fail open - allow notification if dedup check fails
            return True

    async def mark_sent(
        self,
        target_url: str,
        content_hash: str,
    ) -> None:
        """
        Manually mark a notification as sent.

        Useful when notification is sent through external means.

        Args:
            target_url: URL that changed
            content_hash: Hash of the changed content
        """
        key = self._generate_key(target_url, content_hash)

        try:
            await self.redis.set(key, "1", ex=self.ttl_seconds)
            logger.debug(f"Notification marked as sent: {key}")
        except Exception as e:
            logger.error(f"Error marking notification as sent: {e}")

    async def reset(
        self,
        target_url: str,
        content_hash: str,
    ) -> None:
        """
        Reset deduplication for a specific notification.

        Allows re-sending a previously sent notification.

        Args:
            target_url: URL that changed
            content_hash: Hash of the changed content
        """
        key = self._generate_key(target_url, content_hash)

        try:
            await self.redis.delete(key)
            logger.info(f"Deduplication reset for {target_url}")
        except Exception as e:
            logger.error(f"Error resetting deduplication: {e}")

    async def get_ttl(
        self,
        target_url: str,
        content_hash: str,
    ) -> Optional[int]:
        """
        Get remaining TTL for a deduplication key.

        Args:
            target_url: URL that changed
            content_hash: Hash of the changed content

        Returns:
            Remaining TTL in seconds, or None if key doesn't exist
        """
        key = self._generate_key(target_url, content_hash)

        try:
            ttl = await self.redis.ttl(key)
            # Redis returns -2 if key doesn't exist, -1 if no expiry
            return ttl if ttl > 0 else None
        except Exception as e:
            logger.error(f"Error getting TTL: {e}")
            return None
