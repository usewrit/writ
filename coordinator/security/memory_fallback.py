"""In-memory fallback for security controls when Redis is unavailable."""
import logging
import time
import threading

logger = logging.getLogger(__name__)


class MemoryFallback:
    """Thread-safe in-memory fallback with conservative limits.

    Used when Redis is unavailable so security controls degrade gracefully
    instead of failing open. Limits are stricter than Redis since we cannot
    share state across multiple application instances.
    """

    _lock = threading.Lock()
    _brute_force: dict = {}   # key -> (count, first_seen)
    _ip_bans: dict = {}       # ip -> expiry_timestamp
    _user_invalidations: dict = {}  # user_id -> invalidated_at
    MAX_ENTRIES = 10000

    # Conservative limits (stricter than Redis defaults)
    BRUTE_FORCE_LIMIT = 3
    BRUTE_FORCE_WINDOW = 3600  # 1 hour

    @classmethod
    def _cleanup_expired(cls):
        """Remove expired entries to prevent unbounded memory growth."""
        now = time.time()
        # Clean brute force entries older than the window
        expired_bf = [
            k for k, (count, first_seen) in cls._brute_force.items()
            if now - first_seen > cls.BRUTE_FORCE_WINDOW
        ]
        for k in expired_bf:
            del cls._brute_force[k]

        # Clean expired IP bans
        expired_bans = [
            ip for ip, expiry in cls._ip_bans.items()
            if now > expiry
        ]
        for ip in expired_bans:
            del cls._ip_bans[ip]

    @classmethod
    def check_brute_force(cls, identifier: str) -> tuple:
        """Check brute force with conservative in-memory limit.

        Returns (allowed, retry_after_seconds).
        """
        with cls._lock:
            cls._cleanup_expired()
            now = time.time()
            entry = cls._brute_force.get(identifier)
            if not entry:
                return True, 0
            count, first_seen = entry
            # If window has expired, allow
            if now - first_seen > cls.BRUTE_FORCE_WINDOW:
                del cls._brute_force[identifier]
                return True, 0
            if count >= cls.BRUTE_FORCE_LIMIT:
                retry_after = int(cls.BRUTE_FORCE_WINDOW - (now - first_seen))
                return False, max(retry_after, 1)
            return True, 0

    @classmethod
    def record_brute_force_failure(cls, identifier: str):
        """Record a failed attempt in memory."""
        with cls._lock:
            if len(cls._brute_force) >= cls.MAX_ENTRIES:
                cls._cleanup_expired()
            now = time.time()
            entry = cls._brute_force.get(identifier)
            if entry:
                count, first_seen = entry
                if now - first_seen > cls.BRUTE_FORCE_WINDOW:
                    cls._brute_force[identifier] = (1, now)
                else:
                    cls._brute_force[identifier] = (count + 1, first_seen)
            else:
                cls._brute_force[identifier] = (1, now)

    @classmethod
    def record_brute_force_success(cls, identifier: str):
        """Clear failures on successful auth."""
        with cls._lock:
            cls._brute_force.pop(identifier, None)

    @classmethod
    def is_ip_banned(cls, ip: str) -> bool:
        """Check if IP is banned in memory."""
        with cls._lock:
            expiry = cls._ip_bans.get(ip)
            if expiry is None:
                return False
            if time.time() > expiry:
                del cls._ip_bans[ip]
                return False
            return True

    @classmethod
    def ban_ip(cls, ip: str, duration: int):
        """Ban an IP in memory."""
        with cls._lock:
            if len(cls._ip_bans) >= cls.MAX_ENTRIES:
                cls._cleanup_expired()
            cls._ip_bans[ip] = time.time() + duration

    @classmethod
    def invalidate_user_tokens(cls, user_id: str):
        """Record a user-level token invalidation timestamp."""
        with cls._lock:
            cls._user_invalidations[user_id] = int(time.time())

    @classmethod
    def is_user_token_invalidated(cls, user_id: str, iat: int) -> bool:
        """Check if a user's tokens issued before a certain time are invalid."""
        with cls._lock:
            invalidated_at = cls._user_invalidations.get(user_id)
            if invalidated_at is None:
                return False
            return iat < invalidated_at
