"""Admin CLI for API-key management.

Run as ``python -m api.admin <command>``:

    python -m api.admin create --name "acme-prod" --quota 1000
    python -m api.admin list
    python -m api.admin revoke --id <key-id>

The plaintext key is shown exactly once at creation time; only its SHA-256 hash is
stored, so it cannot be recovered later -- revoke and re-create if it is lost.
"""

from __future__ import annotations

import argparse
import uuid

from glassdoor_scraper.config import get_settings

from .db import Database
from .security import generate_api_key, hash_api_key


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="api.admin", description="Manage Glassdoor scraper API keys."
    )
    parser.add_argument("--db", default="", help="SQLite path (default: from settings).")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="Create a new API key.")
    create.add_argument("--name", required=True, help="Human-readable label for the key.")
    create.add_argument("--quota", type=int, default=None, help="Daily search quota.")
    create.add_argument("--rate", type=int, default=None, help="Requests/minute limit.")
    create.add_argument(
        "--concurrency", type=int, default=None, help="Max concurrent jobs."
    )

    sub.add_parser("list", help="List all API keys (hashes are never shown).")

    revoke = sub.add_parser("revoke", help="Revoke (deactivate) an API key.")
    revoke.add_argument("--id", required=True, help="The key id to revoke.")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = get_settings()
    db = Database(args.db or settings.db_path)

    if args.command == "create":
        key_id = uuid.uuid4().hex[:12]
        plaintext = generate_api_key()
        db.create_api_key(
            key_id=key_id,
            key_hash=hash_api_key(plaintext),
            name=args.name,
            daily_quota=args.quota if args.quota is not None else settings.default_daily_quota,
            rate_limit_per_min=args.rate if args.rate is not None else settings.default_rate_limit_per_min,
            max_concurrent_jobs=args.concurrency
            if args.concurrency is not None
            else settings.default_max_concurrent_jobs,
        )
        print("API key created. Store this now -- it will not be shown again:")
        print(f"  id:   {key_id}")
        print(f"  name: {args.name}")
        print(f"  key:  {plaintext}")
        return 0

    if args.command == "list":
        keys = db.list_api_keys()
        if not keys:
            print("No API keys.")
            return 0
        print(f"{'id':<14} {'name':<24} {'quota':>7} {'rate/min':>9} {'concurrency':>12} {'active':>7}")
        print("-" * 80)
        for k in keys:
            print(
                f"{k['id']:<14} {k['name'][:24]:<24} {k['daily_quota']:>7} "
                f"{k['rate_limit_per_min']:>9} {k['max_concurrent_jobs']:>12} "
                f"{'yes' if k['active'] else 'no':>7}"
            )
        return 0

    if args.command == "revoke":
        ok = db.revoke_api_key(args.id)
        print(f"Revoked key {args.id}." if ok else f"No key with id {args.id}.")
        return 0 if ok else 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
