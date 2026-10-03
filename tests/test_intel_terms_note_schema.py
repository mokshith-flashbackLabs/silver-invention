"""0047 — a source's terms note is optional, and a source registered from a validation records
that evidence (owner decision 2026-10-03; spec 2026-09-27 §3.2, amended). Privileges are asserted
under SET ROLE intel_rw, the only place a role's real grants show (test_intel_schema precedent)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from tests.db import run_migrate

PLACEHOLDER = "recorded while terms notes were optional"

_SOURCE = (
    "INSERT INTO intel_sources (kind, source_url, url_hash, normalisation_version,"
    " check_every_hours, terms_note, created_by) VALUES ('policy_page', %s, %s, 'v1', 24, %s,"
    " 'alice') RETURNING source_id"
)
_EVIDENCED = (
    "INSERT INTO intel_sources (kind, source_url, url_hash, normalisation_version,"
    " check_every_hours, terms_note, created_by, validation_run_id, validated_at)"
    " VALUES ('policy_page', %s, %s, 'v1', 24, %s, 'alice', %s, %s) RETURNING source_id"
)
_VALIDATION = (
    "INSERT INTO intel_runs (kind, status, requested_by, completed_at)"
    " VALUES ('source_validation', 'completed', 'ann', now()) RETURNING run_id, completed_at"
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


def _insert(conn: psycopg.Connection[Any], n: int, note: str | None) -> UUID:
    row = conn.execute(
        _SOURCE, (f"https://p.example/terms-{n}", format(n, "064x"), note)
    ).fetchone()
    assert row is not None
    source_id: UUID = row[0]
    return source_id


def _note(conn: psycopg.Connection[Any], source_id: UUID) -> str | None:
    row = conn.execute(
        "SELECT terms_note FROM intel_sources WHERE source_id = %s", (source_id,)
    ).fetchone()
    assert row is not None
    note: str | None = row[0]
    return note


def _note_checks(conn: psycopg.Connection[Any]) -> dict[str, str]:
    """Every CHECK on intel_sources that names terms_note: name -> definition."""
    rows = conn.execute(
        "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint"
        " WHERE conrelid = 'intel_sources'::regclass AND contype = 'c'"
        " AND pg_get_constraintdef(oid) LIKE '%terms_note%'"
    ).fetchall()
    return {name: definition for name, definition in rows}


def _columns(conn: psycopg.Connection[Any]) -> dict[str, str]:
    rows = conn.execute(
        "SELECT column_name, is_nullable FROM information_schema.columns"
        " WHERE table_name = 'intel_sources'"
    ).fetchall()
    return {name: nullable for name, nullable in rows}


def test_0047_a_note_is_optional_and_a_given_one_is_one_to_five_hundred_characters(
    migrated_db: str,
) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        assert _note(conn, _insert(conn, 1, None)) is None
        assert _note(conn, _insert(conn, 2, "a")) == "a"
        assert _note(conn, _insert(conn, 3, "b" * 500)) == "b" * 500
        for n, bad in enumerate(("", "c" * 501), start=4):
            with pytest.raises(psycopg.errors.CheckViolation):
                _insert(conn, n, bad)
        assert list(_note_checks(conn)) == ["intel_sources_terms_note_length"]
        assert _columns(conn)["terms_note"] == "YES"


def test_0047_the_validation_evidence_is_paired_and_names_a_real_run(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        run = conn.execute(_VALIDATION).fetchone()
        assert run is not None
        run_id, completed_at = run
        source = conn.execute(
            _EVIDENCED, ("https://p.example/a", "a" * 64, None, run_id, completed_at)
        ).fetchone()
        assert source is not None
        assert conn.execute(
            "SELECT validation_run_id, validated_at FROM intel_sources WHERE source_id = %s",
            (source[0],),
        ).fetchone() == (run_id, completed_at)
        for n, (rid, at) in enumerate(((run_id, None), (None, completed_at)), start=1):
            with pytest.raises(psycopg.errors.CheckViolation):  # set together or not at all
                conn.execute(_EVIDENCED, (f"https://p.example/b{n}", f"{n}" * 64, None, rid, at))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(_EVIDENCED, ("https://p.example/c", "c" * 64, None, uuid4(), completed_at))


def test_0047_intel_rw_reads_and_writes_the_new_columns(migrated_db: str) -> None:
    """The app role (app_services holds intel_rw): 0039's table-level grant covers the new
    columns, and the FK check on intel_runs needs no grant of its own."""
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        conn.execute("SET ROLE intel_rw")
        run = conn.execute(_VALIDATION).fetchone()
        assert run is not None
        run_id, completed_at = run
        row = conn.execute(
            _EVIDENCED, ("https://p.example/a", "a" * 64, None, run_id, completed_at)
        ).fetchone()
        assert row is not None
        conn.execute(
            "UPDATE intel_sources SET terms_note = 'ok', validation_run_id = NULL,"
            " validated_at = NULL WHERE source_id = %s",
            (row[0],),
        )
        conn.execute(
            "UPDATE intel_sources SET validation_run_id = %s, validated_at = %s"
            " WHERE source_id = %s",
            (run_id, completed_at, row[0]),
        )
        assert conn.execute(
            "SELECT terms_note, validation_run_id, validated_at FROM intel_sources"
            " WHERE source_id = %s",
            (row[0],),
        ).fetchone() == ("ok", run_id, completed_at)
        conn.execute("RESET ROLE")


def test_0047_down_restores_the_required_note_and_up_makes_it_optional_again(
    migrated_db: str,
) -> None:
    down = run_migrate(migrated_db, "down", "--steps", _steps_through("0047_"))
    assert down.returncode == 0, down.stderr
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        # 0039's CHECK, under the name Postgres gave it, which is the name 0047's down restores.
        assert _note_checks(conn) == {
            "intel_sources_terms_note_check": "CHECK ((length(terms_note) >= 10))"
        }
        before = _insert(conn, 1, "automated access permitted")
    up = run_migrate(migrated_db, "up")
    assert up.returncode == 0, up.stderr
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        assert _note(conn, before) == "automated access permitted"  # an existing note survives
        no_note = _insert(conn, 2, None)
        short = _insert(conn, 3, "ok")
        run = conn.execute(_VALIDATION).fetchone()
        assert run is not None
        conn.execute(_EVIDENCED, ("https://p.example/e", "e" * 64, None, run[0], run[1]))
    down = run_migrate(migrated_db, "down", "--steps", _steps_through("0047_"))
    assert down.returncode == 0, down.stderr
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        columns = _columns(conn)
        assert "validation_run_id" not in columns and "validated_at" not in columns
        assert columns["terms_note"] == "NO"
        assert list(_note_checks(conn)) == ["intel_sources_terms_note_check"]
        assert _note(conn, before) == "automated access permitted"
        assert _note(conn, no_note) == PLACEHOLDER
        assert _note(conn, short) == f"ok ({PLACEHOLDER})"  # the operator's text is kept
        assert conn.execute(
            "SELECT count(*) FROM intel_sources WHERE terms_note IS NULL OR length(terms_note) < 10"
        ).fetchone() == (0,)
    up = run_migrate(migrated_db, "up")
    assert up.returncode == 0, up.stderr
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        assert list(_note_checks(conn)) == ["intel_sources_terms_note_length"]
        assert _columns(conn)["terms_note"] == "YES"
        assert _note(conn, no_note) == PLACEHOLDER  # a filled placeholder stays filled
        assert _note(conn, _insert(conn, 4, None)) is None
