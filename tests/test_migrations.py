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


def _external_dependant_dbs(conn: psycopg.Connection[tuple[str]], own: set[str]) -> set[str]:
    """Databases OTHER than the test's own that hold grants on the
    cluster-global ``imageshield_app`` role.

    The role can only be dropped once no database on the cluster grants it
    anything (the down migration documents this). On a dev machine the
    persistent compose database (``imageshield``, used by the devtools
    harness) typically has 0001 applied, so the role legitimately survives a
    throwaway database's ``down --all``. Tests that assert the role is gone
    must therefore branch on this, or they fail on any machine where the
    harness has ever run.
    """
    rows = conn.execute(
        "SELECT DISTINCT d.datname"
        " FROM pg_shdepend s"
        " JOIN pg_database d ON d.oid = s.dbid"
        " JOIN pg_roles r ON r.oid = s.refobjid"
        " WHERE r.rolname = 'imageshield_app'"
    ).fetchall()
    return {row[0] for row in rows} - own


def _db_name(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).path.lstrip("/")


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
        external = _external_dependant_dbs(conn, {_db_name(throwaway_db)})

    assert tables == {"schema_migrations"}
    assert types == set()
    if external:
        # Another database on this cluster (e.g. the compose dev database the
        # harness migrated) still grants the role: the down migration's
        # documented behaviour is to leave it in place.
        assert role_exists is True
    else:
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


def test_0004_score_shape_check_constraint(throwaway_db: str) -> None:
    """A match row must carry a numeric score OR a category — never neither.

    Migration 0004 relaxes ``provider_score`` to nullable (Google Web
    Detection returns ``score: null`` and the adapter must not invent a
    number) and the CHECK constraint is what keeps the relaxation from
    admitting score-less numeric rows.
    """
    run_migrate(throwaway_db, "down", "--all")
    up = run_migrate(throwaway_db, "up")
    assert up.returncode == 0, up.stderr

    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO search_seeds (seed_id, user_ref, seed_kind, source_object_uri)"
            " VALUES ('00000000-0000-0000-0000-000000000001',"
            "  '00000000-0000-0000-0000-0000000000aa', 'user_supplied', 'https://x/img.jpg')"
        )
        conn.execute(
            "INSERT INTO search_runs (run_id, seed_id, user_ref, providers_attempted,"
            " threshold_config) VALUES ('00000000-0000-0000-0000-000000000002',"
            " '00000000-0000-0000-0000-000000000001',"
            " '00000000-0000-0000-0000-0000000000aa', '{hive}', '{}')"
        )
        conn.execute(
            "INSERT INTO content_urls (url_hash, url, source_domain)"
            f" VALUES ('{'a' * 64}', 'https://x/img.jpg', 'x')"
        )

        common = (
            "INSERT INTO search_matches (run_id, url_hash, user_ref, provider_id,"
            " image_url, score_version, band, score_kind, provider_score, provider_category)"
            " VALUES ('00000000-0000-0000-0000-000000000002', %s,"
            " '00000000-0000-0000-0000-0000000000aa', %s, 'https://x/img.jpg', 'v1',"
            " 'review', %s, %s, %s)"
        )
        # numeric with a score: fine
        conn.execute(common, ("a" * 64, "hive", "numeric", "0.8712", None))
        # categorical with a category and NULL score: fine
        conn.execute(common, ("a" * 64, "google", "categorical", None, "full_match"))
        # neither score nor category: rejected by the CHECK
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(common, ("a" * 64, "hive", "numeric", None, None))


def test_0004_providers_seeded_with_score_domain(throwaway_db: str) -> None:
    """Step 7 reads the score domain from here — Hive's floor is 0.5, and
    banding 0.5–1.0 as though it were 0–1 reads weak matches as moderate."""
    run_migrate(throwaway_db, "down", "--all")
    up = run_migrate(throwaway_db, "up")
    assert up.returncode == 0, up.stderr

    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        rows = dict(
            conn.execute(
                "SELECT provider_id, score_domain FROM providers"
                " WHERE provider_id IN ('hive', 'google')"
            ).fetchall()
        )
        (run_status,) = conn.execute(
            "SELECT column_default FROM information_schema.columns"
            " WHERE table_name = 'search_runs' AND column_name = 'status'"
        ).fetchone()  # type: ignore[misc]

    assert rows["hive"] == {"min": 0.5, "max": 1.0}
    assert rows["google"] == {"categories": ["full_match", "partial_match", "page_match"]}
    assert run_status == "'queued'::text"


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

    # db B was the last of the TEST databases depending on the role: tearing
    # it down too must succeed, and — unless some other database on the
    # cluster (e.g. the compose dev database) still grants the role — must
    # actually remove it from the cluster.
    down_b = run_migrate(second_throwaway_db, "down", "--all")
    assert down_b.returncode == 0, down_b.stderr

    own = {_db_name(throwaway_db), _db_name(second_throwaway_db)}
    with psycopg.connect(second_throwaway_db, autocommit=True) as conn:
        external = _external_dependant_dbs(conn, own)
        role_exists = _role_exists(conn)
    if external:
        assert role_exists is True
    else:
        assert role_exists is False
