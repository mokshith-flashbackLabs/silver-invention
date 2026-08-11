"""Retention: null ``provider_calls.raw_response`` past the window while
keeping the metadata row.

One row per (run, provider) is bounded by runs, but the JSONB is the large,
unbounded part. Recalibration over history (CLAUDE.md §7.2) needs RECENT
payloads, not all of them — so the payload expires and the row does not.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from imageshield.db.connection import make_async_pool
from imageshield.providers.store import PostgresProviderControlStore
from imageshield.search.provider import ProviderResult
from imageshield.search.retention import null_expired_raw_responses
from imageshield.search.store import PostgresSearchStore
from imageshield.types import ProviderId, UserRef
from tests.db import ensure_subject, run_migrate

HIVE = ProviderId("hive")


@pytest.fixture
def migrated_db(throwaway_db: str) -> str:
    run_migrate(throwaway_db, "down", "--all")
    up_result = run_migrate(throwaway_db, "up")
    assert up_result.returncode == 0, up_result.stderr
    return throwaway_db


def _query(db_url: str, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(db_url, autocommit=True) as conn:
        return conn.execute(sql, params).fetchall()


async def test_nulls_only_rows_past_the_window_and_is_idempotent(
    migrated_db: str,
) -> None:
    pool = make_async_pool(migrated_db, min_size=1, max_size=2)
    await pool.open()
    try:
        store = PostgresSearchStore(pool)
        user_ref = UserRef(uuid4())
        await ensure_subject(pool, user_ref)
        seed_id = await store.create_seed(user_ref, "user_supplied", "https://s3/i.jpg")
        run_id = await store.create_run(
            user_ref, seed_id, (HIVE,), seed_url="https://proxy-s3.example/run.jpg?X-Amz-Signature=fixture"
        )
        # provider_calls is written by the control store from step 8 onward, so
        # it shares a transaction with the spend upsert and the breaker.
        control = PostgresProviderControlStore(
            pool,
            cache_seconds=0.0,
            failure_threshold=100,
            default_cooldown_seconds=300,
            max_cooldown_seconds=1200,
        )
        for _ in range(2):
            await control.record_outcome(
                run_id,
                ProviderResult(
                    provider_id=HIVE,
                    status="ok",
                    matches=[],
                    raw_response={"big": "payload"},
                    http_status=200,
                    latency_ms=5,
                ),
                cost_usd=None,
                spend_date=datetime.now(UTC).date(),
            )

        # Age ONE of the two rows past the window.
        _query(
            migrated_db,
            "UPDATE provider_calls SET created_at = now() - interval '91 days'"
            " WHERE call_id = (SELECT call_id FROM provider_calls LIMIT 1)"
            " RETURNING call_id",
        )

        assert await null_expired_raw_responses(pool, retention_days=90) == 1

        rows = _query(
            migrated_db,
            "SELECT raw_response, status, http_status, latency_ms FROM provider_calls"
            " ORDER BY created_at",
        )
        assert rows[0][0] is None  # payload gone
        assert rows[0][1:] == ("ok", 200, 5)  # metadata row intact
        assert rows[1][0] == {"big": "payload"}  # inside the window: untouched

        # A second pass finds nothing left to null.
        assert await null_expired_raw_responses(pool, retention_days=90) == 0
    finally:
        await pool.close()
