"""/readyz — the deploy gate for the svc contract.

Unlike /health (always 200, because a degraded DB must not look like "service
absent" to the proxy's retry logic), /readyz returns 503 when the contract is
broken. A deploy must not succeed into a broken contract: the proxy's own views
JOIN against ours, so a dropped column breaks them at runtime while breaking
nothing here.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql

from imageshield.http.svc_contract import (
    EXPECTED_VIEWS,
    PROXY_ROLE,
    check_svc_contract,
)
from tests.db import run_migrate


@pytest.fixture
def migrated_db(throwaway_db: str) -> str:
    down_result = run_migrate(throwaway_db, "down", "--all")
    assert down_result.returncode == 0, down_result.stderr
    up_result = run_migrate(throwaway_db, "up")
    assert up_result.returncode == 0, up_result.stderr
    return throwaway_db


@contextlib.contextmanager
def _unprivileged_login_role(db_url: str, name: str) -> Iterator[str]:
    """Yield a connection URL for a LOGIN role that holds nothing on ``svc``.

    This is what ``app_services`` actually is in the deployed environment:
    migration 0015 grants it USAGE on ``public`` and a handful of table grants,
    and migration 0016 grants ``svc`` to ``imageshield_proxy_ro`` alone. The
    views are owned by the migration runner, which is *not* who the service
    connects as.

    No explicit ``GRANT CONNECT``: PUBLIC already carries it, and granting it
    would create a database-level dependency that blocks the ``DROP ROLE`` on the
    way out — roles are cluster-wide, so a leaked one outlives the throwaway
    database and the next session inherits it.
    """
    identifier = sql.Identifier(name)
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(identifier))
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'probe'").format(identifier)
        )
    parts = urlsplit(db_url)
    host = parts.netloc.rsplit("@", 1)[-1]
    try:
        yield urlunsplit(
            (parts.scheme, f"{name}:probe@{host}", parts.path, parts.query, parts.fragment)
        )
    finally:
        with psycopg.connect(db_url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(identifier))


def test_the_four_views_are_all_declared() -> None:
    assert set(EXPECTED_VIEWS) == {
        "v_person_enrolment_state",
        "v_person_report_summary",
        "v_person_hits",
        "v_person_liveness_attempts",
    }


async def test_contract_holds_on_a_migrated_database(migrated_db: str) -> None:
    async with await psycopg.AsyncConnection.connect(migrated_db, autocommit=True) as conn:
        assert await check_svc_contract(conn) == []


async def test_a_dropped_view_is_reported_by_name(migrated_db: str) -> None:
    async with await psycopg.AsyncConnection.connect(migrated_db, autocommit=True) as conn:
        await conn.execute("DROP VIEW svc.v_person_hits")
        problems = await check_svc_contract(conn)
    assert any("v_person_hits" in p for p in problems)


async def test_a_retyped_column_is_reported_with_the_column(migrated_db: str) -> None:
    """The contract is types as well as names: the proxy JOINs on these."""
    async with await psycopg.AsyncConnection.connect(migrated_db, autocommit=True) as conn:
        await conn.execute("DROP VIEW svc.v_person_liveness_attempts")
        await conn.execute(
            """
            CREATE VIEW svc.v_person_liveness_attempts AS
            SELECT user_ref AS person_ref,
                   count(*)::bigint AS attempts_24h,
                   max(created_at) AS last_attempt_at
            FROM liveness_sessions
            WHERE created_at > now() - interval '24 hours'
            GROUP BY user_ref
            """
        )
        problems = await check_svc_contract(conn)
    assert any("attempts_24h" in p for p in problems)


async def test_an_added_column_is_not_a_problem(migrated_db: str) -> None:
    """Additions are safe by contract; only removals and retypes break the
    proxy. The check asserts expected-subset-of-actual, not equality.

    The GRANT is part of the arrangement, not of the assertion: ``DROP VIEW`` +
    ``CREATE VIEW`` discards the grant, where ``CREATE OR REPLACE VIEW`` keeps it.
    That is a live deploy hazard — a migration that widens a view the destructive
    way leaves the proxy locked out — and it is one the grant check now catches,
    so it has to be arranged away here to leave *column addition* as the only
    thing under test.
    """
    async with await psycopg.AsyncConnection.connect(migrated_db, autocommit=True) as conn:
        await conn.execute("DROP VIEW svc.v_person_enrolment_state")
        await conn.execute(
            """
            CREATE VIEW svc.v_person_enrolment_state AS
            SELECT e.user_ref AS person_ref, e.status, e.model_id,
                   e.created_at AS enrolled_at, 'extra'::text AS future_column
            FROM enrolments e WHERE e.status = 'active'
            """
        )
        await conn.execute(
            f"GRANT SELECT ON svc.v_person_enrolment_state TO {PROXY_ROLE}"
        )
        assert await check_svc_contract(conn) == []


# ── the blind spot: the probe's own connection role ──────────────────────────


async def test_the_contract_holds_for_a_role_with_no_privilege_on_svc(
    migrated_db: str,
) -> None:
    """The regression test whose absence let a 503-against-a-good-database ship.

    ``information_schema`` is **privilege-filtered** by the SQL standard: a role
    sees only the columns it owns or holds some privilege on. 0016 grants ``svc``
    to ``imageshield_proxy_ro`` and to nobody else; 0015 grants ``app_services``
    — the role this service connects as — nothing at all. Measured on a scratch
    database, that role reads **0 rows** from
    ``information_schema.columns WHERE table_schema = 'svc'`` while ``pg_views``
    reports 4. So the probe answered 503 with four ``missing_view`` entries
    against a perfectly correct contract, and ``docs/OPERATIONS.md`` then sent the
    operator to re-run a migration that had already succeeded.

    Every other DB test in this suite connects as the migrating superuser, which
    owns the views and therefore cannot see the bug. This one does not.
    """
    with _unprivileged_login_role(migrated_db, "readyz_probe_role") as probe_url:
        async with await psycopg.AsyncConnection.connect(
            probe_url, autocommit=True
        ) as conn:
            # Sanity: prove the role really is unprivileged, so a future grant
            # cannot quietly turn this test back into the owner-role case.
            cur = await conn.execute(
                "SELECT count(*) FROM information_schema.columns"
                " WHERE table_schema = 'svc'"
            )
            filtered = await cur.fetchone()
            assert filtered is not None and filtered[0] == 0, (
                "this role can see svc in information_schema, so it is no longer"
                " standing in for app_services"
            )

            assert await check_svc_contract(conn) == []


async def test_a_revoked_proxy_grant_is_reported(migrated_db: str) -> None:
    """The grant is half the contract, and it is the half that breaks *them*.

    Revoke it and the views are still present, still correctly shaped, and
    entirely unreadable by the only role that reads them. Without this check
    /readyz stays green while the proxy's report screen fails on its first read.
    """
    async with await psycopg.AsyncConnection.connect(migrated_db, autocommit=True) as conn:
        await conn.execute(
            f"REVOKE SELECT ON svc.v_person_hits FROM {PROXY_ROLE}"
        )
        problems = await check_svc_contract(conn)
    assert any("v_person_hits" in p and PROXY_ROLE in p for p in problems)
    # The other three are untouched, so exactly one problem is reported.
    assert len(problems) == 1, problems


async def test_a_revoked_schema_usage_is_reported(migrated_db: str) -> None:
    """USAGE on the schema is a separate grant from SELECT on the relations, and
    losing it denies every view at once while each individual SELECT grant still
    reads as present."""
    async with await psycopg.AsyncConnection.connect(migrated_db, autocommit=True) as conn:
        await conn.execute(f"REVOKE USAGE ON SCHEMA svc FROM {PROXY_ROLE}")
        problems = await check_svc_contract(conn)
    assert any("svc" in p and PROXY_ROLE in p for p in problems)


async def test_a_stub_table_wearing_a_view_name_is_reported(migrated_db: str) -> None:
    """``DEPLOY-DEV-HANDOFF.md`` §7: no ``svc._stub_*`` in any deployed
    environment, and /readyz is what is supposed to forbid it. A table with the
    right columns satisfies every name-and-type assertion and is still the wrong
    object — it holds a fixture's rows, not this database's, so a user's report
    would render someone else's data or nothing at all.
    """
    async with await psycopg.AsyncConnection.connect(migrated_db, autocommit=True) as conn:
        await conn.execute("DROP VIEW svc.v_person_enrolment_state")
        try:
            await conn.execute(
                "CREATE TABLE svc.v_person_enrolment_state ("
                " person_ref uuid, status text, model_id text,"
                " enrolled_at timestamptz)"
            )
            await conn.execute(
                f"GRANT SELECT ON svc.v_person_enrolment_state TO {PROXY_ROLE}"
            )
            problems = await check_svc_contract(conn)
        finally:
            # 0016's down leg says DROP VIEW, which errors on a table — leaving
            # this behind fails the NEXT test's `migrate down --all` rather than
            # this one, which is the worst kind of test debt.
            await conn.execute("DROP TABLE IF EXISTS svc.v_person_enrolment_state")
    # Names and types all match, and the grant is in place: relkind is the only
    # thing that can catch this.
    assert any("v_person_enrolment_state" in p for p in problems), problems
    assert any("view" in p for p in problems), problems


async def test_a_missing_proxy_role_is_reported_rather_than_crashing(
    migrated_db: str,
) -> None:
    """A role that does not exist must not raise out of a readiness probe (it
    would present as ``db: degraded``, which points at the wrong subsystem) and
    must not pass as healthy either. ``has_table_privilege`` on an unknown role
    raises, so the existence check has to come first.

    Checked against a name that is deliberately absent rather than by dropping
    the real role: roles are cluster-wide, and dropping ``imageshield_proxy_ro``
    out from under a session-scoped database would break every other test here.
    """
    async with await psycopg.AsyncConnection.connect(migrated_db, autocommit=True) as conn:
        problems = await check_svc_contract(
            conn, proxy_role="imageshield_proxy_ro_definitely_absent"
        )
    assert problems, "an absent grant target silently passed as healthy"
    assert any("imageshield_proxy_ro_definitely_absent" in p for p in problems)


def test_the_default_grant_target_is_the_role_0016_creates() -> None:
    """The parameter above is a test seam; the default is the deployed name. If
    these drift, every grant assertion here is about a role nothing uses."""
    assert PROXY_ROLE == "imageshield_proxy_ro"


def test_readyz_returns_503_when_the_database_is_down(client: TestClient) -> None:
    """No db_check wired means the pool is not open."""
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["db"] == "degraded"


def test_readyz_needs_no_service_token(client: TestClient) -> None:
    """Same posture as /health — a readiness probe cannot carry a secret."""
    response = client.get("/readyz")
    assert response.status_code in (200, 503)
