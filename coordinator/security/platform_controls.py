"""
Platform-level controls for enterprise operations.

Provides runtime-toggleable controls stored in Redis:
- Maintenance mode: blocks all non-admin API access
- Registration toggle: enable/disable new user registration
- Login toggle: enable/disable authentication (emergency lockdown)

Controls persist in Redis so they survive app restarts and work across instances.
Falls back to config.py settings if Redis is unavailable.
"""
import logging
from typing import Optional
from config import settings

logger = logging.getLogger(__name__)

REDIS_KEY_PREFIX = "platform_control"
MAINTENANCE_KEY = f"{REDIS_KEY_PREFIX}:maintenance_mode"
REGISTRATION_KEY = f"{REDIS_KEY_PREFIX}:registration_enabled"
LOGIN_KEY = f"{REDIS_KEY_PREFIX}:login_enabled"
MAINTENANCE_MESSAGE_KEY = f"{REDIS_KEY_PREFIX}:maintenance_message"
REGISTRATION_MESSAGE_KEY = f"{REDIS_KEY_PREFIX}:registration_message"

_redis_client = None


def set_redis_client(redis):
    global _redis_client
    _redis_client = redis


async def is_maintenance_mode() -> bool:
    if _redis_client:
        try:
            val = await _redis_client.get(MAINTENANCE_KEY)
            if val is not None:
                return val == "1"
        except Exception:
            pass
    return settings.maintenance_mode


async def get_maintenance_message() -> str:
    if _redis_client:
        try:
            msg = await _redis_client.get(MAINTENANCE_MESSAGE_KEY)
            if msg:
                return msg
        except Exception:
            pass
    return "System is under maintenance. Please try again later."


async def set_maintenance_mode(enabled: bool, message: str = None):
    if _redis_client:
        try:
            await _redis_client.set(MAINTENANCE_KEY, "1" if enabled else "0")
            if message:
                await _redis_client.set(MAINTENANCE_MESSAGE_KEY, message)
            elif not enabled:
                await _redis_client.delete(MAINTENANCE_MESSAGE_KEY)
            logger.info(f"Maintenance mode {'enabled' if enabled else 'disabled'}")
        except Exception as e:
            logger.error(f"Failed to set maintenance mode: {e}")
            raise


async def is_registration_enabled() -> bool:
    if _redis_client:
        try:
            val = await _redis_client.get(REGISTRATION_KEY)
            if val is not None:
                return val == "1"
        except Exception:
            pass
    return settings.registration_enabled


async def set_registration_enabled(enabled: bool):
    if _redis_client:
        try:
            await _redis_client.set(REGISTRATION_KEY, "1" if enabled else "0")
            logger.info(f"Registration {'enabled' if enabled else 'disabled'}")
        except Exception as e:
            logger.error(f"Failed to set registration toggle: {e}")
            raise


async def get_registration_message() -> Optional[str]:
    """Admin-set custom "registration closed" page copy.

    Mirrors get_maintenance_message, but returns None when UNSET (the public
    front-door then falls back to its own built-in friendly string) rather than a
    canned default — there is no sensible default closed-registration sentence and
    a non-null default would always override the frontend's localized copy.
    """
    if _redis_client:
        try:
            msg = await _redis_client.get(REGISTRATION_MESSAGE_KEY)
            if msg:
                return msg
        except Exception:
            pass
    return None


async def set_registration_message(message: Optional[str]) -> None:
    """Set (or clear, when message is falsy) the custom registration-closed copy.

    Mirrors how set_maintenance_mode persists maintenance_message: a non-empty
    string is stored; an empty/None value deletes the key so the frontend reverts
    to its built-in string.
    """
    if _redis_client:
        try:
            if message:
                await _redis_client.set(REGISTRATION_MESSAGE_KEY, message)
            else:
                await _redis_client.delete(REGISTRATION_MESSAGE_KEY)
            logger.info("Registration-closed message %s", "set" if message else "cleared")
        except Exception as e:
            logger.error(f"Failed to set registration message: {e}")
            raise


async def is_login_enabled() -> bool:
    if _redis_client:
        try:
            val = await _redis_client.get(LOGIN_KEY)
            if val is not None:
                return val == "1"
        except Exception:
            pass
    return settings.login_enabled


async def set_login_enabled(enabled: bool):
    if _redis_client:
        try:
            await _redis_client.set(LOGIN_KEY, "1" if enabled else "0")
            logger.info(f"Login {'enabled' if enabled else 'disabled'}")
        except Exception as e:
            logger.error(f"Failed to set login toggle: {e}")
            raise


async def get_all_controls() -> dict:
    return {
        "maintenance_mode": await is_maintenance_mode(),
        "maintenance_message": await get_maintenance_message(),
        "registration_enabled": await is_registration_enabled(),
        "registration_message": await get_registration_message(),
        "login_enabled": await is_login_enabled(),
    }
