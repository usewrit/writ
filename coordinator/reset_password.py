#!/usr/bin/env python3
"""Reset the admin password from the command line — no email server needed.

The self-host coordinator is single-owner and email is optional, so the cloud
"forgot password → email a reset link" flow doesn't apply. Instead, whoever has
shell/file access to the server (i.e. the owner) resets the password directly
against the SQLite database with this script. This is the same pattern used by
self-hosted tools like Gitea (`gitea admin user change-password`) and Django
(`changepassword`).

Usage (from the coordinator/ directory, with the same env as the running app so
WRIT_DB_PATH points at the right database):

    python reset_password.py                       # reset the owner to a generated password (printed)
    python reset_password.py --password 'MyNewPass1'   # set a specific password
    python reset_password.py --email admin@local       # pick an account by email (if more than one)
    python reset_password.py --list                    # list accounts

Convenience wrappers:
    bash reset-admin-password.sh [args]                # local (sources .local/env.sh)
    docker compose -f docker/docker-compose.yml exec coordinator python reset_password.py [args]
"""
import argparse
import asyncio
import os
import re
import sys

# Make the app modules importable when run as a bare script from coordinator/.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def generate_password(length: int = 16) -> str:
    """Strong readable password (upper + lower + digit, no ambiguous chars)."""
    import secrets
    lower = "abcdefghijkmnpqrstuvwxyz"
    upper = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    digits = "23456789"
    alphabet = lower + upper + digits
    chars = [secrets.choice(upper), secrets.choice(lower), secrets.choice(digits)]
    chars += [secrets.choice(alphabet) for _ in range(length - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def validate_strength(pw: str) -> None:
    if len(pw) < 8:
        raise SystemExit("Password must be at least 8 characters.")
    if not re.search(r"[A-Z]", pw) or not re.search(r"[a-z]", pw) or not re.search(r"[0-9]", pw):
        raise SystemExit("Password must include an uppercase letter, a lowercase letter, and a number.")


async def _run(args) -> None:
    from sqlalchemy import select, func
    from database import AsyncSessionLocal
    from models.user import User

    async with AsyncSessionLocal() as db:
        total = (await db.execute(select(func.count(User.id)))).scalar() or 0
        if total == 0:
            raise SystemExit(
                "No account exists yet — open the app and complete the first-run setup instead."
            )

        if args.list:
            rows = (await db.execute(select(User).order_by(User.created_at.asc()))).scalars().all()
            print(f"{'EMAIL':40}  {'ADMIN':6}  {'ACTIVE':6}")
            for u in rows:
                print(f"{u.email:40}  {str(bool(u.is_platform_admin)):6}  {str(bool(u.is_active)):6}")
            return

        # Resolve the target account.
        if args.email:
            user = (await db.execute(select(User).where(User.email == args.email.strip().lower()))).scalar_one_or_none()
            if user is None:
                raise SystemExit(f"No account with email {args.email!r}. Run with --list to see accounts.")
        else:
            if total > 1:
                raise SystemExit(
                    "More than one account exists — specify which with --email (see --list)."
                )
            user = (await db.execute(select(User).limit(1))).scalar_one()

        # Determine the new password.
        generated = False
        if args.password:
            new_pw = args.password
            validate_strength(new_pw)
        else:
            new_pw = generate_password()
            generated = True

        from argon2 import PasswordHasher
        user.password_hash = PasswordHasher().hash(new_pw)
        # Recovery should also un-stick a deactivated owner so they can log back in.
        user.is_active = True
        user.reset_token = None
        user.reset_token_expires = None
        await db.commit()

        print(f"\n✓ Password reset for {user.email}")
        if generated:
            print(f"\n  New password:  {new_pw}\n")
            print("  Save it now — it is not stored anywhere in plaintext.")
        else:
            print("  (using the password you supplied)")
        print("\nSign in at your coordinator URL with the email above and this password.\n")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Reset the self-host admin password (no email server required).",
    )
    p.add_argument("--email", help="Account email (only needed if more than one account exists).")
    p.add_argument("--password", help="Set this exact password (must be 8+ chars with upper, lower, digit). Omit to auto-generate a strong one.")
    p.add_argument("--list", action="store_true", help="List accounts and exit.")
    args = p.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
