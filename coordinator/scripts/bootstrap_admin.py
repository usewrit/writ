#!/usr/bin/env python3
"""
Bootstrap script to create the initial admin API key.

Usage:
    python scripts/bootstrap_admin.py

This will create an admin API key and print the plaintext key.
Save this key securely - it won't be shown again.
"""
import asyncio
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from database import AsyncSessionLocal
from models.api_key import APIKey, Role
from security.api_key import generate_api_key, hash_api_key
from datetime import datetime


async def bootstrap_admin():
    """Create initial admin API key."""
    async with AsyncSessionLocal() as db:
        # Check if any admin keys exist
        from sqlalchemy import select

        result = await db.execute(
            select(APIKey).where(APIKey.role == Role.ADMIN)
        )
        existing_admin = result.scalar_one_or_none()

        if existing_admin:
            print("Error: Admin API key already exists.")
            print(f"Existing key: {existing_admin.label}")
            print("\nUse the existing admin key to create additional keys via the API.")
            return 1

        # Generate new admin key
        plaintext_key = generate_api_key()
        key_hash = hash_api_key(plaintext_key)

        # Create admin API key
        admin_key = APIKey(
            label="Bootstrap Admin",
            key_hash=key_hash,
            role=Role.ADMIN,
            created_at=datetime.utcnow(),
        )

        db.add(admin_key)
        await db.commit()

        print("=" * 70)
        print("Bootstrap Admin API Key Created Successfully!")
        print("=" * 70)
        print()
        print(f"Label:   {admin_key.label}")
        print(f"Role:    {admin_key.role.value}")
        print(f"Created: {admin_key.created_at.isoformat()}")
        print()
        print("API Key (save this securely - it won't be shown again):")
        print("-" * 70)
        print(plaintext_key)
        print("-" * 70)
        print()
        print("Use this key to authenticate API requests:")
        print(f'  curl -H "Authorization: Bearer {plaintext_key}" http://localhost:8000/api/auth/me')
        print()
        print("Use this key to create additional API keys:")
        print(f'  curl -X POST http://localhost:8000/api/auth/api-keys \\')
        print(f'    -H "Authorization: Bearer {plaintext_key}" \\')
        print(f'    -H "Content-Type: application/json" \\')
        print(f'    -d \'{{"label": "My Key", "role": "operator"}}\'')
        print()

        return 0


if __name__ == "__main__":
    exit_code = asyncio.run(bootstrap_admin())
    sys.exit(exit_code)
