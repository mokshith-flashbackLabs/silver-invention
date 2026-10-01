"""0043 — sources chosen per question (spec §3.2, §3.4, §4.10). Privileges are asserted under
SET ROLE intel_rw, the only place a role's real grants show (test_intel_schema precedent)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from imageshield.db.connection import make_async_pool
from imageshield.intel.store import PostgresIntelStore
from tests.db import run_migrate

_SOURCE = (
    "INSERT INTO intel_sources (kind, source_url, url_hash, normalisation_version,"
    " check_every_hours, terms_note, created_by, origin, proposed_for, enabled, disabled_reason)"
    " VALUES ('policy_page', %s, %s, 'v1', 24, 'automated access permitted', 'alice', %s, %s,"
    " %s, %s) RETURNING source_id"
)


def _steps_through(version: str) -> str:
    """How many ``down --steps N`` roll back ``version`` and everything after it, counted from
    the files (a literal goes stale the day a migration lands on top)."""
    ups = sorted(p.name for p in (Path(__file__).parent.parent / "migrations").glob("*.up.sql"))
    return str(sum(1 for name in ups if name >= version))


@pytest.fixture
def migrated_db(throwaway_db: str) -> str:
    assert run_migrate(throwaway_db, "down", "--all").returncode == 0
    up = run_migrate(throwaway_db, "up")
    assert up.returncode == 0, up.stderr
    return throwaway_db


def _insert(
    conn: psycopg.Connection[Any],
    n: int,
    *,
    origin: str = "operator",
    proposed_for: Any = None,
    enabled: bool = True,
    reason: str | None = None,
) -> UUID:
    row = conn.execute(
        _SOURCE,
        (
            f"https://p.example/terms-{n}",
            format(n, "064x"),
            origin,
            Jsonb(proposed_for) if proposed_for is not None else None,
            enabled,
            reason,
        ),
    ).fetchone()
    assert row is not None
    source_id: UUID = row[0]
    return source_id


def _kind_check(conn: psycopg.Connection[Any]) -> tuple[str, bool]:
    """intel_runs' kind CHECK: its definition and whether it is validated. 'adhoc_url' appears in
    no other CHECK on the table, the same lookup 0043's DO block makes."""
    row = conn.execute(
        "SELECT pg_get_constraintdef(oid), convalidated FROM pg_constraint"
        " WHERE conrelid = 'intel_runs'::regclass AND contype = 'c'"
        " AND pg_get_constraintdef(oid) LIKE '%adhoc_url%'"
    ).fetchone()
    assert row is not None
    return row[0], row[1]


def test_0043_sources_carry_origin_and_provenance(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        plain = _insert(conn, 1)
        assert conn.execute(
            "SELECT origin, proposed_for FROM intel_sources WHERE source_id = %s", (plain,)
        ).fetchone() == ("operator", None)
        _insert(conn, 2, origin="suggested", proposed_for={"question_key": "q", "option": "Bumble"})
        for n, bad in enumerate(
            (
                {"origin": "suggested"},  # a suggested source names the option it was chosen for
                {"origin": "bogus"},
                {"proposed_for": {"question_key": 1, "option": "Bumble"}},
                {"proposed_for": {"option": "Bumble"}},  # a missing key must not read as NULL
                {"proposed_for": ["q", "Bumble"]},
            ),
            start=3,
        ):
            with pytest.raises(psycopg.errors.CheckViolation):
                _insert(conn, n, **bad)


def test_0043_unmapped_is_a_disabled_reason(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        _insert(conn, 1, enabled=False, reason="unmapped")
        # 0039: a reason only on a disabled source.
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert(conn, 2, enabled=True, reason="unmapped")
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert(conn, 3, enabled=False, reason="bogus")


def test_0043_intel_rw_writes_the_new_columns_and_kinds(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        conn.execute("SET ROLE intel_rw")
        for kind in ("source_proposal", "source_validation"):
            conn.execute("INSERT INTO intel_runs (kind, requested_by) VALUES (%s, 'ann')", (kind,))
        source_id = _insert(
            conn, 1, origin="suggested", proposed_for={"question_key": "q", "option": "o"}
        )
        conn.execute(
            "UPDATE intel_sources SET enabled = false, disabled_reason = 'unmapped'"
            " WHERE source_id = %s",
            (source_id,),
        )
        with pytest.raises(psycopg.errors.CheckViolation):  # 0039: only a check or a discovery
            conn.execute(  # names a source
                "INSERT INTO intel_runs (kind, source_id, requested_by)"
                " VALUES ('source_proposal', %s, 'ann')",
                (source_id,),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO intel_runs (kind, requested_by) VALUES ('bogus', 'ann')")


def test_0043_down_keeps_history_and_up_restores_it(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        done_row = conn.execute(
            "INSERT INTO intel_runs (kind, status, requested_by, completed_at)"
            " VALUES ('source_proposal', 'completed', 'ann', now()) RETURNING run_id"
        ).fetchone()
        waiting_row = conn.execute(
            "INSERT INTO intel_runs (kind, requested_by) VALUES ('source_validation', 'ann')"
            " RETURNING run_id"
        ).fetchone()
        assert done_row is not None and waiting_row is not None
        paused = _insert(conn, 1, enabled=False, reason="unmapped")
        _insert(conn, 2, origin="suggested", proposed_for={"question_key": "q", "option": "o"})
    down = run_migrate(migrated_db, "down", "--steps", _steps_through("0043_"))
    assert down.returncode == 0, down.stderr
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        columns = {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'intel_sources'"
            ).fetchall()
        }
        assert "origin" not in columns and "proposed_for" not in columns
        # Nothing re-enables a source on the way down: it stays off, as if an operator had
        # disabled it.
        assert conn.execute(
            "SELECT enabled, disabled_reason FROM intel_sources WHERE source_id = %s", (paused,)
        ).fetchone() == (False, None)
        # A question run nothing could finish any more is ended, so no later UPDATE of it ever
        # meets the restored CHECK (NOT VALID skips existing rows, never an update of one).
        assert conn.execute(
            "SELECT status, error_code FROM intel_runs WHERE run_id = %s", (waiting_row[0],)
        ).fetchone() == ("failed", "migration_down")
        assert conn.execute(
            "SELECT status FROM intel_runs WHERE run_id = %s", (done_row[0],)
        ).fetchone() == ("completed",)
        definition, validated = _kind_check(conn)
        assert "source_proposal" not in definition and validated is False  # history kept
        with pytest.raises(psycopg.errors.CheckViolation):  # but a NEW one is refused
            conn.execute(
                "INSERT INTO intel_runs (kind, requested_by) VALUES ('source_proposal', 'a')"
            )
    up = run_migrate(migrated_db, "up")
    assert up.returncode == 0, up.stderr
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        definition, validated = _kind_check(conn)
        assert "source_validation" in definition and validated is True
        assert conn.execute(
            "SELECT count(*) FROM intel_runs WHERE run_id = %s", (done_row[0],)
        ).fetchone() == (1,)


def test_0043_down_on_a_clean_database_validates_the_restored_check(migrated_db: str) -> None:
    down = run_migrate(migrated_db, "down", "--steps", _steps_through("0043_"))
    assert down.returncode == 0, down.stderr
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        assert _kind_check(conn)[1] is True
    assert run_migrate(migrated_db, "up").returncode == 0


@pytest.fixture
async def store(migrated_db: str) -> AsyncIterator[PostgresIntelStore]:
    pool: AsyncConnectionPool = make_async_pool(migrated_db, min_size=1, max_size=2)
    await pool.open()
    try:
        yield PostgresIntelStore(pool)
    finally:
        await pool.close()


async def test_a_registry_source_reads_back_as_an_operator_source(
    store: PostgresIntelStore,
) -> None:
    source = await store.create_source(
        kind="policy_page",
        source_url="https://p.example/terms",
        query_text=None,
        tags=("instagram",),
        check_every_hours=24,
        terms_note="automated access permitted",
        operator="alice",
    )
    assert (source.origin, source.proposed_for) == ("operator", None)
    (listed,) = await store.list_sources(cursor=None, limit=5)
    assert listed.origin == "operator" and listed.proposed_for is None
