#!/usr/bin/env python3
"""
Generate a JWT service token for infrastructure recorders.

Usage:
    python -m utils.generate_service_token <tenant_id> [--max-sessions 5] [--secret <key>]

Or from the backend directory:
    python utils/generate_service_token.py <tenant_id>

The output token should be set as WRIT_SERVICE_TOKEN in docker-compose.yml
or the recorder's environment.
"""
import argparse
import os
import sys
import time

# Ensure the backend package is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    parser = argparse.ArgumentParser(
        description="Generate a JWT service token for infrastructure recorders."
    )
    parser.add_argument("tenant_id", help="Tenant ID this recorder will serve")
    parser.add_argument(
        "--max-sessions", type=int, default=5,
        help="Maximum concurrent sessions (default: 5)",
    )
    parser.add_argument(
        "--secret", default=None,
        help="Signing secret (default: RECORDER_AUTH_SECRET env var)",
    )
    args = parser.parse_args()

    secret = args.secret or os.getenv("RECORDER_AUTH_SECRET", "")
    if not secret:
        print("Error: RECORDER_AUTH_SECRET env var not set and no --secret provided", file=sys.stderr)
        sys.exit(1)

    from utils.recorder_auth import generate_service_token

    token = generate_service_token(
        tenant_id=args.tenant_id,
        max_sessions=args.max_sessions,
        secret=secret,
    )

    print(f"\nService token generated for tenant {args.tenant_id}:")
    print(f"  Max sessions: {args.max_sessions}")
    print()
    print(f"WRIT_SERVICE_TOKEN={token}")
    print()
    print("Add this to your docker-compose.yml or recorder environment.")


if __name__ == "__main__":
    main()
