#!/usr/bin/env python3
"""
Create a new admin API key.

Usage:
    python scripts/create_admin_key.py [label]

Creates a new admin API key with the specified label and prints the plaintext key.
Save this key securely - it won't be shown again.
"""
import asyncio
import sys
from pathlib import Path
from datetime import datetime

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from database import AsyncSessionLocal
from models.api_key import APIKey, Role
from security.api_key import generate_api_key, hash_api_key


async def create_admin_key(label: str = "Admin Key"):
    """Create a new admin API key."""
    async with AsyncSessionLocal() as db:
        # Generate new admin key
        plaintext_key = generate_api_key()
        key_hash = hash_api_key(plaintext_key)

        # Create admin API key
        admin_key = APIKey(
            label=label,
            key_hash=key_hash,
            role=Role.ADMIN,
            created_at=datetime.utcnow(),
        )

        db.add(admin_key)
        await db.commit()

        print("=" * 70)
        print("Admin API Key Created Successfully!")
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
        print("Example: Create a new target")
        print(f'  curl -X POST http://localhost:8000/api/targets \\')
        print(f'    -H "Authorization: Bearer {plaintext_key}" \\')
        print(f'    -H "Content-Type: application/json" \\')
        print(f'    -d \'{{"url": "https://example.com", "name": "Example Site", "enabled": true}}\'')
        print()

        return 0


if __name__ == "__main__":
    label = sys.argv[1] if len(sys.argv) > 1 else "Admin Key"
    exit_code = asyncio.run(create_admin_key(label))
    sys.exit(exit_code)
