"""Sources follow the quiz (spec §4.9, §4.10): the state-based pause pass, the PATCH rules around
it, and the tick that runs it before scheduling. Real Postgres."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from psycopg_pool import AsyncConnectionPool

from imageshield.db.connection import make_async_pool
from imageshield.intel.models import SourcePause
from imageshield.intel.store import PostgresIntelStore
from imageshield.intel.worker import tick
from tests.intel_fakes import FakeFetcher, FakeModel, make_deps, quiz_document, seed_quiz_vocabulary

# QUIZ_VOCABULARY maps instagram; linkedin is registered and unmapped.
LINKEDIN_MAPPED = [
    {"question_key": "platforms", "option": "Instagram", "tags": ["instagram"]},
    {"question_key": "platforms", "option": "LinkedIn", "tags": ["linkedin"]},
]


async def _source(store: PostgresIntelStore, n: int, tags: tuple[str, ...]) -> UUID:
    source = await store.create_source(
        kind="policy_page",
        source_url=f"https://p.example/terms-{n}",
        query_text=None,
        tags=tags,
        check_every_hours=24,
        terms_note="automated access permitted",
        operator="alice",
    )
    return source.source_id


async def _state(store: PostgresIntelStore, source_id: UUID) -> tuple[bool, str | None]:
    source = await store.get_source(source_id)
    assert source is not None
    return source.enabled, source.disabled_reason


async def _map_linkedin(pool: AsyncConnectionPool) -> None:
    await seed_quiz_vocabulary(
        pool, map_version=2, document=quiz_document(option_tags=LINKEDIN_MAPPED)
    )


async def test_a_source_pauses_when_its_tags_leave_the_quiz_and_resumes_when_one_returns(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    store = PostgresIntelStore(intel_pool)
    unmapped = await _source(store, 1, ("linkedin",))
    partly = await _source(store, 2, ("instagram", "linkedin"))  # one mapped tag is enough
    general = await _source(store, 3, ())  # an untagged source never pauses
    assert await store.pause_unmapped_sources() == SourcePause(paused=(unmapped,))
    assert await _state(store, unmapped) == (False, "unmapped")
    assert await _state(store, partly) == (True, None)
    assert await _state(store, general) == (True, None)
    assert await store.pause_unmapped_sources() == SourcePause()  # nothing left to move
    await _map_linkedin(intel_pool)
    assert await store.pause_unmapped_sources() == SourcePause(resumed=(unmapped,))
    assert await _state(store, unmapped) == (True, None)


async def test_an_operator_disable_is_never_undone_by_a_mapping(
    intel_pool: AsyncConnectionPool,
) -> None:
    """Review Focus 4: the operator's disable clears 'unmapped', so nothing resumes it."""
    await seed_quiz_vocabulary(intel_pool)
    store = PostgresIntelStore(intel_pool)
    source = await _source(store, 1, ("linkedin",))
    await store.pause_unmapped_sources()
    await store.patch_source(source, operator="bob", enabled=False)
    assert await _state(store, source) == (False, None)
    await _map_linkedin(intel_pool)
    assert await store.pause_unmapped_sources() == SourcePause()
    assert await _state(store, source) == (False, None)


async def test_a_source_disabled_for_another_reason_is_left_alone(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    store = PostgresIntelStore(intel_pool)
    source = await _source(store, 1, ("linkedin",))
    async with intel_pool.connection() as conn:
        await conn.execute(
            "UPDATE intel_sources SET enabled = false, disabled_reason = 'unreachable'"
            " WHERE source_id = %s",
            (source,),
        )
    await _map_linkedin(intel_pool)
    assert await store.pause_unmapped_sources() == SourcePause()
    assert await _state(store, source) == (False, "unreachable")


async def test_a_paused_source_whose_tags_are_cleared_resumes_as_a_general_one(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    store = PostgresIntelStore(intel_pool)
    source = await _source(store, 1, ("linkedin",))
    await store.pause_unmapped_sources()
    await store.patch_source(source, operator="bob", tags=())
    assert await store.pause_unmapped_sources() == SourcePause(resumed=(source,))


async def test_the_pass_is_audited_only_when_it_moves_something(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    store = PostgresIntelStore(intel_pool)
    source = await _source(store, 1, ("linkedin",))
    await store.pause_unmapped_sources()
    await store.pause_unmapped_sources()
    async with intel_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT actor_type, metadata FROM audit_log"
            " WHERE action = 'intel.sources_followed_quiz'"
        )
        rows = await cur.fetchall()
    assert len(rows) == 1
    actor_type, metadata = rows[0]
    assert actor_type == "service" and metadata["paused"] == [str(source)]


@pytest.fixture
async def bare_pool(intel_db: str) -> AsyncIterator[AsyncConnectionPool]:
    """``intel_db`` with no vocabulary pushed (``intel_pool`` seeds one)."""
    pool = make_async_pool(intel_db, min_size=1, max_size=2)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def test_no_vocabulary_pauses_nothing(bare_pool: AsyncConnectionPool) -> None:
    store = PostgresIntelStore(bare_pool)
    source = await _source(store, 1, ("linkedin",))
    assert await store.pause_unmapped_sources() == SourcePause()
    assert await _state(store, source) == (True, None)


async def test_the_tick_pauses_before_it_schedules(intel_pool: AsyncConnectionPool) -> None:
    await seed_quiz_vocabulary(intel_pool)
    store = PostgresIntelStore(intel_pool)
    paused = await _source(store, 1, ("linkedin",))
    running = await _source(store, 2, ("instagram",))  # the control: due, mapped, scheduled
    deps = make_deps(
        intel_pool,
        FakeFetcher({}),
        FakeModel(),
        clock=lambda: datetime.now(UTC) + timedelta(minutes=1),  # both are due
    )
    await tick(deps, lease_seconds=900)
    assert await _state(store, paused) == (False, "unmapped")
    async with intel_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT source_id, count(*) FROM intel_runs WHERE source_id IS NOT NULL"
            " GROUP BY source_id"
        )
        runs = dict(await cur.fetchall())
    assert runs == {running: 1}
