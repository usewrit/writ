"""
First-boot single-admin bootstrap for the self-hosted coordinator.

The OSS coordinator is single-owner: there is exactly ONE user account, and that
account is the platform admin. This module creates that owner on first boot from
environment variables so a fresh, unattended container comes up with a usable
admin without any manual HTTP registration step.

Behaviour (called from the FastAPI lifespan, or runnable standalone):

  * If a user already exists  -> no-op (idempotent; never a second owner).
  * If no user AND WRIT_ADMIN_EMAIL + WRIT_ADMIN_PASSWORD are set
                              -> create exactly one admin User (argon2 hash,
                                 is_platform_admin=True).
  * If no user AND the envs are absent
                              -> log a clear, actionable message and return
                                 without raising. The register() endpoint in
                                 routers/auth.py stays the first-boot fallback:
                                 the operator can register once via the UI/API.

This never crashes the process: any failure is logged and swallowed so the
coordinator still boots (the register() fallback remains available). It also
never creates a second account — the existence check + the unique email
constraint make repeated runs safe.
"""
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.user import User

logger = logging.getLogger(__name__)

ENV_EMAIL = "WRIT_ADMIN_EMAIL"
ENV_PASSWORD = "WRIT_ADMIN_PASSWORD"
ENV_NAME = "WRIT_ADMIN_NAME"


async def _user_count(db: AsyncSession) -> int:
    return (await db.execute(select(func.count(User.id)))).scalar() or 0


async def ensure_admin_user(db: AsyncSession) -> Optional[User]:
    """Create the single admin owner from env if none exists yet.

    Returns the created ``User`` when a new owner was provisioned, otherwise
    ``None`` (already had a user, envs absent, or a swallowed failure). Safe to
    call on every boot — it is idempotent.
    """
    # Idempotent: never provision a second owner.
    try:
        existing = await _user_count(db)
    except Exception as e:  # pragma: no cover - defensive, do not block boot
        logger.warning("Admin bootstrap: could not query users (%s); skipping", e)
        return None

    if existing > 0:
        logger.debug("Admin bootstrap: an account already exists; nothing to do")
        return None

    email = (os.getenv(ENV_EMAIL) or "").strip().lower()
    password = os.getenv(ENV_PASSWORD) or ""

    if not email or not password:
        logger.warning(
            "No admin account yet, and %s/%s are not set. Set both env vars to "
            "provision the owner automatically on boot, or register once via the "
            "app/API (POST /api/auth/register) to create the first-boot owner.",
            ENV_EMAIL,
            ENV_PASSWORD,
        )
        return None

    # Basic sanity on the supplied password — warn (don't crash) so a fresh
    # container still boots; the operator controls this value.
    if len(password) < 8:
        logger.warning(
            "%s is shorter than 8 characters — creating the admin anyway, but "
            "consider a stronger password.",
            ENV_PASSWORD,
        )

    from argon2 import PasswordHasher

    ph = PasswordHasher()
    now = datetime.now(timezone.utc)

    user = User(
        email=email,
        password_hash=ph.hash(password),
        name=(os.getenv(ENV_NAME) or "").strip() or None,
        is_active=True,
        # First-boot owner of a self-hosted coordinator: pre-verified, platform admin.
        is_verified=True,
        is_platform_admin=True,
        terms_accepted_at=now,
        terms_version=settings.terms_version,
        privacy_version=settings.privacy_version,
        age_confirmed_at=now,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        # A concurrent register()/bootstrap won the race, or the email already
        # exists. Roll back and treat as already-provisioned — never double-create.
        await db.rollback()
        logger.info("Admin bootstrap: an account already exists (race); skipping")
        return None
    except Exception as e:  # pragma: no cover - defensive, do not block boot
        await db.rollback()
        logger.warning("Admin bootstrap: failed to create admin (%s); skipping", e)
        return None

    await db.refresh(user)
    logger.info("Admin owner account created from %s (first-boot bootstrap): %s",
                ENV_EMAIL, user.email)
    return user


async def run_bootstrap() -> Optional[User]:
    """Open a session and run :func:`ensure_admin_user`.

    Convenience entry point for the lifespan hook and for standalone execution.
    Never raises — a bootstrap failure must not prevent the coordinator booting.
    """
    try:
        from database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            return await ensure_admin_user(db)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Admin bootstrap skipped due to error: %s", e)
        return None


if __name__ == "__main__":
    import asyncio

    logging.basicConfig(level=logging.INFO)
    created = asyncio.run(run_bootstrap())
    if created is not None:
        print(f"Created admin: {created.email} (is_platform_admin={created.is_platform_admin})")
    else:
        print("No admin created (already exists, or env not set).")
