"""Schema lint tests (INVARIANTS.md #9): no image bytes persisted, anywhere.

Two layers, per the task-2 brief:

- Unit cases call ``lint_columns`` directly with hand-built ``ColumnInfo``
  rows — no DB needed, fast, exercises the rule's edge cases.
- DB-backed cases run against the real, migrated throwaway database (Task 1
  harness): a blocking check over every column the migration actually creates
  (this is the test that fails the build if a ``thumbnail_blob`` column is
  ever added to ``infringements``), plus three fixture cases that each
  create a scratch table, lint it through real ``information_schema``, and
  drop it — proving the gate works against Postgres's own type reporting, not
  just against hand-typed fixtures.

Each DB-backed test runs its own ``down --all`` + ``up`` as its arrange step
(per the brief's coordination note), so these tests don't fight
``tests/test_migrations.py``'s down/up choreography for the same
session-scoped database, and don't depend on execution order either.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from imageshield.schema_lint import ColumnInfo, Violation, lint_columns
from tests.db import run_migrate

ColumnRow = tuple[str, str, str, str, str]

_COLUMNS_QUERY = """
    SELECT table_schema, table_name, column_name, data_type, udt_name
    FROM information_schema.columns
    WHERE {where}
    ORDER BY table_schema, table_name, ordinal_position
"""


def _to_columns(rows: list[ColumnRow]) -> list[ColumnInfo]:
    return [
        ColumnInfo(
            table_schema=row[0],
            table_name=row[1],
            column_name=row[2],
            data_type=row[3],
            udt_name=row[4],
        )
        for row in rows
    ]


# ── Unit cases: no DB, direct against lint_columns ──────────────────────────


def _column(name: str, data_type: str = "text", udt_name: str | None = None) -> ColumnInfo:
    return ColumnInfo(
        table_schema="public",
        table_name="fixture_table",
        column_name=name,
        data_type=data_type,
        udt_name=udt_name if udt_name is not None else data_type,
    )


def test_unit_source_object_uri_passes() -> None:
    assert lint_columns([_column("source_object_uri")]) == []


def test_unit_audit_image_uris_array_passes() -> None:
    # information_schema reports array columns as data_type='ARRAY',
    # udt_name='_text' — never match on the substring "image".
    column = _column("audit_image_uris", data_type="ARRAY", udt_name="_text")
    assert lint_columns([column]) == []


def test_unit_thumbnail_uri_fails() -> None:
    violations = lint_columns([_column("thumbnail_uri")])
    assert [v.column_name for v in violations] == ["thumbnail_uri"]


def test_unit_image_data_fails() -> None:
    violations = lint_columns([_column("image_data")])
    assert [v.column_name for v in violations] == ["image_data"]


def test_unit_raw_b64_fails() -> None:
    violations = lint_columns([_column("raw_b64")])
    assert [v.column_name for v in violations] == ["raw_b64"]


def test_unit_bytea_array_fails_regardless_of_name() -> None:
    # (a) is the real rule: udt_name '_bytea' is a violation no matter what
    # the column is named.
    column = _column("payload", data_type="ARRAY", udt_name="_bytea")
    assert [v.column_name for v in lint_columns([column])] == ["payload"]


def test_unit_violation_str_is_informative() -> None:
    violations = lint_columns([_column("thumbnail_uri")])
    text = str(violations[0])
    assert "thumbnail_uri" in text
    assert "public.fixture_table" in text


# ── DB-backed: the migrated schema, in full ──────────────────────────────────


def test_migrated_schema_has_zero_violations(throwaway_db: str) -> None:
    """The build-blocking check: every column in the real, migrated schema
    must pass the lint. This is what would fail if `thumbnail_blob` were
    added to `infringements` in migrations/0005_infringements_attestations.up.sql.
    """
    run_migrate(throwaway_db, "down", "--all")
    up_result = run_migrate(throwaway_db, "up")
    assert up_result.returncode == 0, up_result.stderr

    where = (
        "table_schema NOT IN ('pg_catalog', 'information_schema') "
        "AND table_schema NOT LIKE 'pg_temp_%' "
        "AND table_schema NOT LIKE 'pg_toast_temp_%'"
    )
    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        rows: list[ColumnRow] = conn.execute(_COLUMNS_QUERY.format(where=where)).fetchall()

    columns = _to_columns(rows)
    assert columns, "expected the migrated schema to contain columns"

    violations = lint_columns(columns)
    assert violations == [], "schema lint violations:\n" + "\n".join(str(v) for v in violations)


# ── DB-backed fixture cases: create a scratch table, lint, drop ─────────────


@pytest.fixture
def scratch_conn(throwaway_db: str) -> Iterator[psycopg.Connection[ColumnRow]]:
    """A connection to the migrated throwaway DB, with `schema_lint_fixture`
    dropped before and after each case so cases don't interact and don't
    depend on `tests/test_migrations.py`'s own down/up choreography.
    """
    run_migrate(throwaway_db, "down", "--all")
    up_result = run_migrate(throwaway_db, "up")
    assert up_result.returncode == 0, up_result.stderr

    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS schema_lint_fixture")
        try:
            yield conn
        finally:
            conn.execute("DROP TABLE IF EXISTS schema_lint_fixture")


def _lint_table(conn: psycopg.Connection[ColumnRow], table_name: str) -> list[Violation]:
    where = "table_schema = 'public' AND table_name = %(table_name)s"
    rows: list[ColumnRow] = conn.execute(
        _COLUMNS_QUERY.format(where=where), {"table_name": table_name}
    ).fetchall()
    return lint_columns(_to_columns(rows))


def test_fixture_photo_bytea_fails(scratch_conn: psycopg.Connection[ColumnRow]) -> None:
    scratch_conn.execute("CREATE TABLE schema_lint_fixture (photo bytea)")

    violations = _lint_table(scratch_conn, "schema_lint_fixture")

    assert [v.column_name for v in violations] == ["photo"]


def test_fixture_thumbnail_b64_text_fails(scratch_conn: psycopg.Connection[ColumnRow]) -> None:
    scratch_conn.execute("CREATE TABLE schema_lint_fixture (thumbnail_b64 TEXT)")

    violations = _lint_table(scratch_conn, "schema_lint_fixture")

    assert [v.column_name for v in violations] == ["thumbnail_b64"]


def test_fixture_reference_image_uri_text_passes(
    scratch_conn: psycopg.Connection[ColumnRow],
) -> None:
    scratch_conn.execute("CREATE TABLE schema_lint_fixture (reference_image_uri TEXT)")

    violations = _lint_table(scratch_conn, "schema_lint_fixture")

    assert violations == []


def test_fixture_thumbnail_uri_still_fails_despite_uri_suffix(
    scratch_conn: psycopg.Connection[ColumnRow],
) -> None:
    """The asymmetric case CLAUDE.md §8 step 2 calls out: the reject regex
    wins over the uri/url allowlist, proving the gate isn't a rubber stamp on
    any column merely because it ends in `_uri`.
    """
    scratch_conn.execute("CREATE TABLE schema_lint_fixture (thumbnail_uri TEXT)")

    violations = _lint_table(scratch_conn, "schema_lint_fixture")

    assert [v.column_name for v in violations] == ["thumbnail_uri"]


def test_fixture_smuggled_thumbnail_blob_on_infringements_fails(
    scratch_conn: psycopg.Connection[ColumnRow],
) -> None:
    """Demonstrates the gate against the exact scenario the brief names: a
    `thumbnail_blob` column smuggled onto the real `infringements` table
    (not `0001_initial_schema.up.sql` itself — this ALTERs a scratch copy of
    the already-migrated table and reverts it, leaving the migration file
    untouched).
    """
    scratch_conn.execute("ALTER TABLE infringements ADD COLUMN thumbnail_blob TEXT")
    try:
        violations = _lint_table(scratch_conn, "infringements")

        assert [v.column_name for v in violations] == ["thumbnail_blob"]
    finally:
        scratch_conn.execute("ALTER TABLE infringements DROP COLUMN thumbnail_blob")
