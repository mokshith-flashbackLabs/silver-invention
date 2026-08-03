"""Migration 0001 tests, run against a real, disposable Postgres database.

Every up/down call goes through ``tests.db.run_migrate``, which shells out to
the real ``scripts/migrate.py`` CLI — the artifact under test, not a
reimplementation of its transaction/checksum logic. Each test starts with an
explicit ``down --all`` (a no-op on an already-empty database) so tests don't
depend on execution order, even though they share one throwaway database for
the session.
"""

from __future__ import annotations

import psycopg
import pytest

from tests.db import run_migrate  # re-exported
from tests.db import second_throwaway_db as second_throwaway_db

CORE_TABLES = {
    "liveness_sessions",
    "enrolments",
    "providers",
    "search_seeds",
    "search_runs",
    "content_urls",
    "provider_calls",
    "search_matches",
    "outbox",
    "audit_log",
}
CUSTOM_TYPES = {"liveness_status", "provider_kind"}


def _table_names(conn: psycopg.Connection[tuple[str]]) -> set[str]:
    rows = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()
    return {row[0] for row in rows}


def _type_names(conn: psycopg.Connection[tuple[str]]) -> set[str]:
    rows = conn.execute(
        "SELECT typname FROM pg_type WHERE typname = ANY(%s)",
        (list(CUSTOM_TYPES),),
    ).fetchall()
    return {row[0] for row in rows}


def _role_exists(conn: psycopg.Connection[tuple[str]]) -> bool:
    row = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'imageshield_app'").fetchone()
    return row is not None


def test_up_from_empty_applies_0001_cleanly(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")

    result = run_migrate(throwaway_db, "up")

    assert result.returncode == 0, result.stderr
    assert "applied 0001_initial_schema.up.sql" in result.stdout

    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        tables = _table_names(conn)
        types = _type_names(conn)
        role_exists = _role_exists(conn)

    assert tables >= CORE_TABLES
    assert types == CUSTOM_TYPES
    assert role_exists is True


def test_down_all_after_up_leaves_no_phase2_objects(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    up_result = run_migrate(throwaway_db, "up")
    assert up_result.returncode == 0, up_result.stderr

    down_result = run_migrate(throwaway_db, "down", "--all")

    assert down_result.returncode == 0, down_result.stderr

    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        tables = _table_names(conn)
        types = _type_names(conn)
        role_exists = _role_exists(conn)

    assert tables == {"schema_migrations"}
    assert types == set()
    assert role_exists is False


def test_up_down_up_round_trip(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")

    first_up = run_migrate(throwaway_db, "up")
    down = run_migrate(throwaway_db, "down", "--all")
    second_up = run_migrate(throwaway_db, "up")

    assert first_up.returncode == 0, first_up.stderr
    assert down.returncode == 0, down.stderr
    assert second_up.returncode == 0, second_up.stderr

    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        tables = _table_names(conn)
    assert tables >= CORE_TABLES


def test_imageshield_app_role_is_insert_only_on_audit_log(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    up_result = run_migrate(throwaway_db, "up")
    assert up_result.returncode == 0, up_result.stderr

    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        conn.execute("SET ROLE imageshield_app")

        # INSERT is the one grant this role has; the role is never granted
        # SELECT either, so verification below happens after RESET ROLE.
        conn.execute("INSERT INTO audit_log (actor_type, action) VALUES ('system', 'test')")

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE audit_log SET action = 'changed'")

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM audit_log")

        conn.execute("RESET ROLE")
        (count,) = conn.execute("SELECT count(*) FROM audit_log").fetchone()  # type: ignore[misc]
        assert count == 1


def test_down_all_on_one_db_does_not_break_role_for_sibling_db(
    throwaway_db: str, second_throwaway_db: str
) -> None:
    """The cluster-global ``imageshield_app`` role must survive tearing down
    ONE of two databases on the same server that both have 0001 applied —
    mirrors two concurrent pytest sessions against one compose Postgres, or
    a second app database sharing the cluster. Before the down-migration fix
    this failed with "role ... cannot be dropped because some objects
    depend on it" (the role is still granted on db B's audit_log) and left
    db A's own teardown broken.
    """
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(second_throwaway_db, "down", "--all")

    up_a = run_migrate(throwaway_db, "up")
    up_b = run_migrate(second_throwaway_db, "up")
    assert up_a.returncode == 0, up_a.stderr
    assert up_b.returncode == 0, up_b.stderr

    down_a = run_migrate(throwaway_db, "down", "--all")
    assert down_a.returncode == 0, down_a.stderr

    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        assert _table_names(conn) == {"schema_migrations"}

    # db B is untouched: same assertions as
    # test_imageshield_app_role_is_insert_only_on_audit_log, run against db B
    # *after* db A's teardown, proving the role and its grants there are
    # unaffected by whatever db A's down migration did to the role.
    with psycopg.connect(second_throwaway_db, autocommit=True) as conn:
        conn.execute("SET ROLE imageshield_app")
        conn.execute("INSERT INTO audit_log (actor_type, action) VALUES ('system', 'test')")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE audit_log SET action = 'changed'")
        conn.execute("RESET ROLE")
        (count,) = conn.execute("SELECT count(*) FROM audit_log").fetchone()  # type: ignore[misc]
        assert count == 1

    # db B was the last remaining dependant on the role: tearing it down too
    # must succeed and this time actually remove the role from the cluster.
    down_b = run_migrate(second_throwaway_db, "down", "--all")
    assert down_b.returncode == 0, down_b.stderr

    with psycopg.connect(second_throwaway_db, autocommit=True) as conn:
        assert _role_exists(conn) is False
