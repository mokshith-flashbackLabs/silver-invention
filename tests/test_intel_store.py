"""The intel store against real Postgres (tests/db.py harness)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from psycopg_pool import AsyncConnectionPool

from imageshield.db.connection import make_async_pool
from imageshield.intel.store import PostgresIntelStore
from tests.db import run_migrate

# The real clock, not a fixed date: next_check_at defaults to the database's now(),
# so a fixed past date would make schedule_due find nothing due on any later day.
NOW = datetime.now(UTC)


@pytest.fixture
def migrated_db(throwaway_db: str) -> str:
    assert run_migrate(throwaway_db, "down", "--all").returncode == 0
    assert run_migrate(throwaway_db, "up").returncode == 0
    return throwaway_db


@pytest.fixture
async def store(migrated_db: str) -> AsyncIterator[PostgresIntelStore]:
    pool: AsyncConnectionPool = make_async_pool(migrated_db, min_size=1, max_size=2)
    await pool.open()
    try:
        yield PostgresIntelStore(pool)
    finally:
        await pool.close()


async def _policy(store: PostgresIntelStore, url: str = "https://p.example/terms"):
    return await store.create_source(
        kind="policy_page",
        source_url=url,
        query_text=None,
        tags=("instagram",),
        check_every_hours=24,
        terms_note="automated access permitted",
        operator="alice",
    )


async def test_create_source_canonicalises_and_hashes(store: PostgresIntelStore) -> None:
    source = await _policy(store, "HTTPS://P.Example/terms?utm_source=x")
    assert source.source_url == "https://p.example/terms"
    assert source.url_hash is not None and len(source.url_hash) == 64


async def test_queue_check_returns_the_open_run(store: PostgresIntelStore) -> None:
    source = await _policy(store)
    first = await store.queue_source_check(source.source_id, operator="alice")
    second = await store.queue_source_check(source.source_id, operator="bob")
    assert first is not None and first == second


async def test_schedule_due_creates_one_run_and_advances(store: PostgresIntelStore) -> None:
    source = await _policy(store)
    runs = await store.schedule_due(NOW + timedelta(hours=1))
    assert len(runs) == 1
    assert await store.schedule_due(NOW + timedelta(hours=1)) == []  # advanced, and open run exists
    refreshed = await store.get_source(source.source_id)
    assert refreshed is not None and refreshed.next_check_at > NOW


async def test_claim_is_leased_and_reclaimed_after_expiry(store: PostgresIntelStore) -> None:
    await store.queue_adhoc("https://n.example/a", operator="alice")
    claimed = await store.claim_next(NOW, lease_seconds=900)
    assert claimed is not None and claimed.attempts == 1
    assert await store.claim_next(NOW + timedelta(seconds=10), lease_seconds=900) is None
    again = await store.claim_next(NOW + timedelta(seconds=901), lease_seconds=900)
    assert again is not None and again.run_id == claimed.run_id and again.attempts == 2


async def test_a_run_at_the_attempt_cap_fails_for_good(store: PostgresIntelStore) -> None:
    await store.queue_adhoc("https://n.example/a", operator="alice")
    t = NOW
    for _ in range(3):
        assert await store.claim_next(t, lease_seconds=60) is not None
        t += timedelta(seconds=61)
    assert await store.claim_next(t, lease_seconds=60) is None
    assert await store.expire_exhausted(t) == 1
    (run,) = await store.list_runs(cursor=None, limit=10)
    assert run.status == "failed" and run.error_code == "attempts_exhausted"


async def test_vocabulary_is_ordered_by_the_pair(store: PostgresIntelStore) -> None:
    doc = {"tags": [], "questions": [], "option_tags": [], "renamed": []}
    assert await store.put_vocabulary(
        release_no=5, map_version=3, scoring_version="s5", quiz_version="q3", document=doc
    )
    assert not await store.put_vocabulary(
        release_no=5, map_version=2, scoring_version="s5", quiz_version="q3", document=doc
    )
    assert not await store.put_vocabulary(
        release_no=4, map_version=9, scoring_version="s4", quiz_version="q3", document=doc
    )
    assert await store.put_vocabulary(
        release_no=5,
        map_version=3,
        scoring_version="s5",
        quiz_version="q3",
        document={**doc, "x": 1},
    )  # equal overwrites
    assert await store.put_vocabulary(
        release_no=5, map_version=4, scoring_version="s5", quiz_version="q3", document=doc
    )
    vocab = await store.load_vocabulary()
    assert vocab is not None and (vocab.release_no, vocab.map_version) == (5, 4)


async def test_enabling_clears_the_disabled_reason(store: PostgresIntelStore) -> None:
    source = await _policy(store)
    # Drive the source into a genuinely disabled-with-a-reason state directly by SQL —
    # patch_source itself never *sets* disabled_reason (only clears it), so asserting the
    # clear without first putting a real reason on the row would prove nothing.
    async with store._pool.connection() as conn:
        await conn.execute(
            "UPDATE intel_sources SET enabled = false, disabled_reason = 'unreachable',"
            " consecutive_failures = 3 WHERE source_id = %s",
            (source.source_id,),
        )
    disabled = await store.get_source(source.source_id)
    assert disabled is not None and disabled.disabled_reason == "unreachable"
    enabled = await store.patch_source(source.source_id, operator="alice", enabled=True)
    assert enabled is not None and enabled.enabled and enabled.disabled_reason is None
    assert enabled.consecutive_failures == 0
