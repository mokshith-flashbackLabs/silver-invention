"""Apply and revert SQL migrations.

Adapted from the Flashback agent service (AgentMeeMaw scripts/migrate.py:
checksummed ``schema_migrations`` ledger, BEGIN/COMMIT stripping, editing an
applied migration is a deploy-blocking error). Extended with a ``down``
command — this repo's migrations are required to be reversible and CI runs
them forward and backward (build spec Phase 1 §5).

Migrations are numbered pairs in ``migrations/``::

    0001_initial_schema.up.sql
    0001_initial_schema.down.sql

Usage:
    python scripts/migrate.py up [--dry-run]
    python scripts/migrate.py down [--steps N | --all] [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ROOT / "migrations"
MIGRATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename text PRIMARY KEY,
    checksum text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


def _database_url() -> str:
    value = os.environ.get("DATABASE_URL")
    if not value:
        raise SystemExit("DATABASE_URL is required")
    return value


def _up_migrations() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.up.sql"))


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _strip_transaction_markers(sql: str) -> str:
    """Remove bare BEGIN/COMMIT lines from migration SQL.

    Migration files are written for use with psql (which needs explicit
    transaction markers). Inside psycopg's conn.transaction() the script
    manages its own savepoints, so bare BEGIN/COMMIT would commit the outer
    transaction mid-flight. Lines inside $$-quoted blocks are left alone.
    """
    out = []
    in_dollar = False
    for line in sql.splitlines():
        if not in_dollar:
            upper = line.strip().rstrip(";").upper()
            if upper in {"BEGIN", "COMMIT", "ROLLBACK"}:
                continue
        if line.count("$$") % 2 == 1:
            in_dollar = not in_dollar
        out.append(line)
    return "\n".join(out)


def _run_sql_file(conn: psycopg.Connection, path: Path) -> None:
    sql = _strip_transaction_markers(path.read_text(encoding="utf-8-sig"))
    with conn.cursor() as cur:
        cur.execute(sql)


def cmd_up(conn: psycopg.Connection, *, dry_run: bool) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT filename, checksum FROM schema_migrations")
        applied = dict(cur.fetchall())

    pending: list[tuple[Path, str]] = []
    for path in _up_migrations():
        checksum = _checksum(path)
        previous = applied.get(path.name)
        if previous is None:
            pending.append((path, checksum))
            continue
        if previous != checksum:
            raise SystemExit(
                f"checksum mismatch for applied migration {path.name}; "
                "create a new migration instead of editing history"
            )

    if dry_run:
        for path, _ in pending:
            print(path.name)
        return 0

    for path, checksum in pending:
        down_path = path.with_name(path.name.replace(".up.sql", ".down.sql"))
        if not down_path.is_file():
            raise SystemExit(
                f"{path.name} has no matching {down_path.name}; "
                "every migration must be reversible"
            )
        with conn.transaction():
            _run_sql_file(conn, path)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO schema_migrations (filename, checksum) VALUES (%s, %s)",
                    (path.name, checksum),
                )
        print(f"applied {path.name}")
    return 0


def cmd_down(conn: psycopg.Connection, *, steps: int, dry_run: bool) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT filename FROM schema_migrations ORDER BY filename DESC")
        applied = [row[0] for row in cur.fetchall()]

    to_revert = applied if steps <= 0 else applied[:steps]
    if dry_run:
        for filename in to_revert:
            print(filename)
        return 0

    for filename in to_revert:
        down_path = MIGRATIONS_DIR / filename.replace(".up.sql", ".down.sql")
        if not down_path.is_file():
            raise SystemExit(f"missing down migration {down_path.name}; cannot revert")
        with conn.transaction():
            _run_sql_file(conn, down_path)
            with conn.cursor() as cur:
                cur.execute("DELETE FROM schema_migrations WHERE filename = %s", (filename,))
        print(f"reverted {filename}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply/revert ImageShield SQL migrations")
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("up", help="apply all pending migrations")
    up.add_argument("--dry-run", action="store_true", help="print pending migrations")

    down = sub.add_parser("down", help="revert applied migrations (latest first)")
    down.add_argument("--steps", type=int, default=1, help="how many to revert (default 1)")
    down.add_argument("--all", action="store_true", help="revert everything")
    down.add_argument("--dry-run", action="store_true", help="print what would be reverted")

    args = parser.parse_args()

    with psycopg.connect(_database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(MIGRATION_TABLE_SQL)
        if args.command == "up":
            return cmd_up(conn, dry_run=args.dry_run)
        steps = 0 if args.all else args.steps
        return cmd_down(conn, steps=steps, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
