"""Operator-only migration and first release, run locally on the policy host."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import psycopg

from .publication import activate_policy, load_reviewed_policy


def main() -> int:
    parser = argparse.ArgumentParser()
    command = parser.add_subparsers(dest="command", required=True)
    command.add_parser("migrate")
    first = command.add_parser("bootstrap")
    first.add_argument("--commit", required=True)
    first.add_argument("--publisher", required=True)
    command.add_parser("status")
    args = parser.parse_args()
    dsn = os.environ["PANKY_POLICY_DSN"]
    if args.command == "migrate":
        sql = (Path(__file__).parent / "migrations/001_panky_policy.sql").read_text()
        with psycopg.connect(dsn) as conn:
            conn.execute(sql)
        print("Policy schema created")
    elif args.command == "bootstrap":
        policy, registry = load_reviewed_policy(
            Path(os.environ["PANKY_CORE_CHECKOUT"]), args.commit
        )
        activate_policy(dsn, policy, registry, args.commit, args.publisher)
        print("Reviewed Core policy activated")
    else:
        with psycopg.connect(dsn) as conn:
            row = conn.execute(
                "SELECT core_commit, status FROM panky_policy_releases "
                "WHERE status='active'"
            ).fetchone()
        if row is None:
            print("No active policy")
            return 1
        print(f"Active Core commit: {row[0].strip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
