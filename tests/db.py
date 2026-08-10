"""DB test harness: a throwaway Postgres database for migration tests.

Connects to ``TEST_DATABASE_URL`` (the ``postgres`` maintenance database on
the local compose Postgres by default — see ``docker-compose.local.yml``),
creates a uniquely named scratch database for the test session, and drops it
on teardown. Tests that need real Postgres depend on the ``throwaway_db``
fixture; when the server is unreachable they are skipped with a clear
message, unless ``REQUIRE_DB=1`` is set, in which case unreachability is a
hard failure rather than a skip.

``run_migrate`` shells out to the real ``scripts/migrate.py`` CLI via
subprocess — that's the artifact under test, not a reimplementation of its
transaction/checksum logic.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql
from psycopg_pool import AsyncConnectionPool

from imageshield.types import UserRef

ROOT = Path(__file__).resolve().parents[1]
MIGRATE_SCRIPT = ROOT / "scripts" / "migrate.py"

DEFAULT_MAINTENANCE_URL = "postgresql://imageshield:imageshield@localhost:15433/postgres"

# Windows subprocess env: pass through only what the interpreter/OS need to
# start, same allowlist as tests/test_boot.py, so a developer's .env.local
# can't leak into the migration subprocess either.
_PASSTHROUGH = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP", "PATHEXT", "COMSPEC")


def _maintenance_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", DEFAULT_MAINTENANCE_URL)


def _with_database(url: str, database: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", parts.query, parts.fragment))


def _server_reachable(url: str) -> bool:
    try:
        with psycopg.connect(url, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
    except psycopg.OperationalError:
        return False
    return True


def _create_scratch_database(maintenance_url: str) -> tuple[str, str]:
    """Create a uniquely named scratch database; return ``(db_name, url)``."""
    db_name = f"imageshield_test_{uuid.uuid4().hex[:16]}"
    create_stmt = sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name))
    with psycopg.connect(maintenance_url, autocommit=True) as admin:
        admin.execute(create_stmt)
    return db_name, _with_database(maintenance_url, db_name)


def _drop_scratch_database(maintenance_url: str, db_name: str) -> None:
    drop_stmt = sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(db_name))
    with psycopg.connect(maintenance_url, autocommit=True) as admin:
        admin.execute(drop_stmt)


@pytest.fixture(scope="session")
def throwaway_db() -> Iterator[str]:
    """Yield the DATABASE_URL of a uniquely named, disposable database.

    Skips the requesting tests (or fails, under ``REQUIRE_DB=1``) when the
    maintenance server is unreachable.
    """
    maintenance_url = _maintenance_url()

    if not _server_reachable(maintenance_url):
        message = (
            f"Postgres unreachable at {maintenance_url!r}; start it with "
            "`docker compose -f docker-compose.local.yml up -d`"
        )
        if os.environ.get("REQUIRE_DB") == "1":
            pytest.fail(message)
        pytest.skip(message)

    db_name, db_url = _create_scratch_database(maintenance_url)
    try:
        yield db_url
    finally:
        _drop_scratch_database(maintenance_url, db_name)


@pytest.fixture
def second_throwaway_db(throwaway_db: str) -> Iterator[str]:
    """A second, independent scratch database on the same maintenance server
    as :func:`throwaway_db`.

    Function-scoped (unlike the session-scoped ``throwaway_db``): it exists
    only for tests that specifically need two live databases at once on one
    server — e.g. proving that a cluster-global role (``imageshield_app``)
    can be torn down from one database without breaking the other. Depending
    on ``throwaway_db`` reuses its reachability check rather than duplicating
    it.
    """
    maintenance_url = _maintenance_url()
    db_name, db_url = _create_scratch_database(maintenance_url)
    try:
        yield db_url
    finally:
        _drop_scratch_database(maintenance_url, db_name)


async def ensure_subject(
    pool: AsyncConnectionPool, user_ref: UserRef, *, adult: bool = True
) -> None:
    """Register ``user_ref`` as a subject so a seed can reference it.

    Step 8 gave ``search_seeds.user_ref`` a foreign key to ``subjects``, which
    means an unparented seed is no longer creatable — the point of the
    constraint. Production writes this row inside the enrolment transaction;
    tests that start from a seed rather than an enrolment call this instead.
    """
    from imageshield.subjects.eligibility import eligibility_for
    from imageshield.subjects.store import PostgresSubjectStore

    await PostgresSubjectStore(pool).upsert_subject(user_ref, eligibility_for(adult))


def run_migrate(database_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run `python scripts/migrate.py <args>` against `database_url`.

    Subprocess, not an import, so the real CLI entry point is under test.
    """
    env = {key: os.environ[key] for key in _PASSTHROUGH if key in os.environ}
    env["DATABASE_URL"] = database_url
    return subprocess.run(
        [sys.executable, str(MIGRATE_SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
