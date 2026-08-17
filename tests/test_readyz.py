"""/readyz — the deploy gate for the svc contract.

Unlike /health (always 200, because a degraded DB must not look like "service
absent" to the proxy's retry logic), /readyz returns 503 when the contract is
broken. A deploy must not succeed into a broken contract: the proxy's own views
JOIN against ours, so a dropped column breaks them at runtime while breaking
nothing here.
"""

from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from imageshield.http.svc_contract import EXPECTED_VIEWS, check_svc_contract
from tests.db import run_migrate


@pytest.fixture
def migrated_db(throwaway_db: str) -> str:
    down_result = run_migrate(throwaway_db, "down", "--all")
    assert down_result.returncode == 0, down_result.stderr
    up_result = run_migrate(throwaway_db, "up")
    assert up_result.returncode == 0, up_result.stderr
    return throwaway_db


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
    proxy. The check asserts expected-subset-of-actual, not equality."""
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
        assert await check_svc_contract(conn) == []


def test_readyz_returns_503_when_the_database_is_down(client: TestClient) -> None:
    """No db_check wired means the pool is not open."""
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["db"] == "degraded"


def test_readyz_needs_no_service_token(client: TestClient) -> None:
    """Same posture as /health — a readiness probe cannot carry a secret."""
    response = client.get("/readyz")
    assert response.status_code in (200, 503)
