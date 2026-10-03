"""The intel worker loop (task 11, spec §4.1): a real Postgres, a fake fetcher and
a fake model -- never the network. Fixtures and fakes are shared with the
pipeline tests (``tests/conftest.py``'s ``intel_pool``, ``tests/intel_fakes.py``).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

import structlog
from psycopg_pool import AsyncConnectionPool

from imageshield.intel.bounds import MAX_RUN_ATTEMPTS
from imageshield.intel.question_store import PostgresQuestionStore
from imageshield.intel.run_log import RunLog, current_run_log
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


async def test_the_finished_runs_log_line_carries_its_counts_and_never_its_lists(
    intel_pool: AsyncConnectionPool,
) -> None:
    """Final review I1: a question run's outcome carries lists -- here the candidates a
    validation judged -- and the log line takes only the outcome's scalars. The lists stay in
    intel_runs."""
    terms = "https://p.example/terms"
    candidate = {"option": "Instagram", "kind": "policy_page", "source_url": terms}
    await PostgresQuestionStore(intel_pool).queue_source_validation(
        {"candidates": [{**candidate, "query_text": None}]}, operator="ann"
    )
    deps = make_deps(intel_pool, FakeFetcher({terms: make_page(POLICY, terms)}), FakeModel())
    with structlog.testing.capture_logs() as logs:
        assert await tick(deps, lease_seconds=900) is True
    (finished,) = [e for e in logs if e["event"] == "intel.run_finished"]
    assert finished["kind"] == "source_validation" and finished["status"] == "completed"
    assert finished["outcome"]["candidate_ready"] == 1 and "results" not in finished["outcome"]
    assert terms not in repr(finished)
    stored = await _scalar(intel_pool, "SELECT outcome FROM intel_runs")
    assert stored["results"][0]["candidate"]["source_url"] == terms


# ── the run log (spec 2026-10-03 §3.3) ─────────────────────────────────────────────────────


async def test_a_run_is_logged_from_started_to_finished(intel_pool: AsyncConnectionPool) -> None:
    """`run_started` first, `run_finished` last and written before the run's own status, each
    call the run metered in between, `seq` from 1 without a gap -- and the log is unbound
    again once the tick returns."""
    store = PostgresIntelStore(intel_pool)
    run_id = await store.queue_adhoc(URL, operator="a")
    deps = make_deps(
        intel_pool, FakeFetcher({URL: make_page(POLICY, URL)}), FakeModel(make_signal())
    )
    assert await tick(deps, lease_seconds=900) is True
    assert current_run_log.get() is None
    read = await store.run_events(run_id)
    assert read is not None and read.status == "completed"
    events = read.events
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert events[0].kind == "run_started"
    assert events[0].text == "Started: read a pasted document"
    assert events[0].detail == {"run_kind": "adhoc_url", "attempt": 1}
    assert events[-1].kind == "run_finished"
    assert events[-1].text.startswith("Finished in ")
    assert events[-1].detail["status"] == "completed"
    assert events[-1].detail["error_code"] is None
    (run,) = await store.list_runs(cursor=None, limit=1)
    assert events[-1].detail["cost_usd"] == run.outcome["cost_usd"]
    assert "model_call_finished" in {e.kind for e in events[1:-1]}
    assert events[-1].at <= await _completed_at(intel_pool, run_id)


async def _completed_at(pool: AsyncConnectionPool, run_id: UUID) -> Any:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT completed_at FROM intel_runs WHERE run_id = %s", (run_id,))
        row = await cur.fetchone()
    assert row is not None and row[0] is not None
    return row[0]


async def test_a_refused_run_finishes_its_log_with_the_reason(
    intel_pool: AsyncConnectionPool,
) -> None:
    async with intel_pool.connection() as conn:
        await conn.execute(
            "UPDATE providers SET enabled = false WHERE provider_id = 'claude_intel'"
        )
    store = PostgresIntelStore(intel_pool)
    run_id = await store.queue_adhoc(URL, operator="a")
    deps = make_deps(
        intel_pool, FakeFetcher({URL: make_page(POLICY, URL)}), FakeModel(make_signal())
    )
    assert await tick(deps, lease_seconds=900) is True
    read = await store.run_events(run_id)
    assert read is not None and read.status == "refused"
    kinds = [e.kind for e in read.events]
    assert kinds[0] == "run_started" and kinds[-1] == "run_finished"
    assert "model_call_skipped" in kinds
    assert read.events[-1].text == "Refused: provider_disabled"


async def test_a_reclaimed_run_appends_to_its_log(intel_pool: AsyncConnectionPool) -> None:
    """A worker that died mid-run leaves its rows; the reclaim's `run_started` says attempt 2
    and continues the same `seq`, never colliding with them."""
    store = PostgresIntelStore(intel_pool)
    run_id = await store.queue_adhoc(URL, operator="a")
    claimed = await store.claim_next(NOW, lease_seconds=60)  # a worker that died after claiming
    assert claimed is not None
    dead = RunLog(run_id, store)
    await dead.append("run_started", "Started: read a pasted document", {"attempt": 1})
    await dead.append("model_call_started", "Asked claude-sonnet-5", {"model": "x"})
    later = NOW + timedelta(seconds=61)
    deps = make_deps(
        intel_pool, FakeFetcher({URL: make_page(POLICY, URL)}), FakeModel(make_signal())
    )
    deps.clock = lambda: later
    assert await tick(deps, lease_seconds=900) is True
    read = await store.run_events(run_id)
    assert read is not None and read.status == "completed"
    assert [e.seq for e in read.events] == list(range(1, len(read.events) + 1))
    third = read.events[2]
    assert third.kind == "run_started"
    assert third.text == "Started: read a pasted document (attempt 2)"
    assert third.detail == {"run_kind": "adhoc_url", "attempt": 2}
    assert read.events[-1].kind == "run_finished"


async def test_a_crashed_run_is_left_without_run_finished(
    intel_pool: AsyncConnectionPool,
) -> None:
    """A bug in run() is not a finish: the run stays leased for a reclaim, and its log stops
    where the crash was, unbound again."""
    store = PostgresIntelStore(intel_pool)
    run_id = await store.queue_adhoc(URL, operator="a")
    deps = make_deps(intel_pool, FakeFetcher({}), FakeModel())

    async def broken(_: Any) -> Any:
        raise RuntimeError("bug")

    deps.fetcher.fetch_text = broken  # type: ignore[method-assign]
    assert await tick(deps, lease_seconds=900) is True
    assert current_run_log.get() is None
    read = await store.run_events(run_id)
    assert read is not None and read.status == "running"
    assert [e.kind for e in read.events] == ["run_started"]
