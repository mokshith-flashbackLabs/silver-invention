"""The intel worker loop (task 11, spec §4.1): a real Postgres, a fake fetcher and
a fake model -- never the network. Fixtures and fakes are shared with the
pipeline tests (``tests/conftest.py``'s ``intel_pool``, ``tests/intel_fakes.py``).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from psycopg_pool import AsyncConnectionPool

from imageshield.intel.bounds import MAX_RUN_ATTEMPTS
from imageshield.intel.store import PostgresIntelStore
from imageshield.intel.worker import tick
from tests.intel_fakes import NOW, POLICY, FakeFetcher, FakeModel, make_deps, make_page, make_signal

URL = "https://n.example/a"


async def _scalar(pool: AsyncConnectionPool, query: str) -> Any:
    async with pool.connection() as conn:
        cur = await conn.execute(query)
        row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _documents(pool: AsyncConnectionPool) -> int:
    return int(await _scalar(pool, "SELECT count(*) FROM intel_documents"))


async def _signals(pool: AsyncConnectionPool) -> int:
    return int(await _scalar(pool, "SELECT count(*) FROM intel_signals"))


async def test_tick_runs_one_queued_run_and_finishes_it(intel_pool: AsyncConnectionPool) -> None:
    store = PostgresIntelStore(intel_pool)
    await store.queue_adhoc(URL, operator="a")
    deps = make_deps(
        intel_pool, FakeFetcher({URL: make_page(POLICY, URL)}), FakeModel(make_signal())
    )
    assert await tick(deps, lease_seconds=900) is True
    (run,) = await store.list_runs(cursor=None, limit=5)
    assert run.status == "completed" and run.outcome["signals_kept"] == 1
    # Nothing queued or due: the second tick finds no work and claims nothing.
    assert await tick(deps, lease_seconds=900) is False


async def test_a_reclaimed_run_resumes_without_duplicates(
    intel_pool: AsyncConnectionPool,
) -> None:
    """A worker killed mid-run: the lease expires, another tick reclaims the run
    and finishes it -- without re-recording the unit (the ``ON CONFLICT`` in
    ``record_unit``) or re-billing it (the pipeline's ``recorded_in_run`` guard,
    task 10). Pinned on the actual rows, not only on ``tick``'s boolean: a bug
    that let the reclaim double-write would still return ``True``/``False`` in
    the right shape while leaving two documents behind."""
    store = PostgresIntelStore(intel_pool)
    await store.queue_adhoc(URL, operator="a")
    claimed = await store.claim_next(NOW, lease_seconds=60)  # a worker that died after claiming
    assert claimed is not None
    later = NOW + timedelta(seconds=61)
    deps = make_deps(
        intel_pool, FakeFetcher({URL: make_page(POLICY, URL)}), FakeModel(make_signal())
    )
    deps.clock = lambda: later
    assert await tick(deps, lease_seconds=900) is True
    assert await tick(deps, lease_seconds=900) is False
    assert await _documents(intel_pool) == 1
    assert await _signals(intel_pool) == 1


async def test_a_run_exhausted_at_max_attempts_fails_for_good(
    intel_pool: AsyncConnectionPool,
) -> None:
    """A run that never finishes -- every worker that claims it dies before
    calling ``finish_run`` -- stops being reclaimable once ``MAX_RUN_ATTEMPTS``
    is spent (``claim_next``'s ``attempts < max_attempts``). It ends up
    ``failed``/``attempts_exhausted`` via ``expire_exhausted``, not stuck
    ``running`` and not retried forever."""
    store = PostgresIntelStore(intel_pool)
    await store.queue_adhoc(URL, operator="a")
    when = NOW
    for _ in range(MAX_RUN_ATTEMPTS):
        claimed = await store.claim_next(when, lease_seconds=60)
        assert claimed is not None
        when = when + timedelta(seconds=61)  # simulate the lease expiring, unfinished
    assert await store.claim_next(when, lease_seconds=60) is None  # attempts spent: unclaimable

    deps = make_deps(intel_pool, FakeFetcher({}), FakeModel())
    deps.clock = lambda: when
    assert await tick(deps, lease_seconds=900) is False  # nothing left to claim
    (run,) = await store.list_runs(cursor=None, limit=5)
    assert run.status == "failed" and run.error_code == "attempts_exhausted"
