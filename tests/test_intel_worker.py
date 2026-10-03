"""The intel worker loop (task 11, spec §4.1): a real Postgres, a fake fetcher and
a fake model -- never the network. Fixtures and fakes are shared with the
pipeline tests (``tests/conftest.py``'s ``intel_pool``, ``tests/intel_fakes.py``).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from psycopg_pool import AsyncConnectionPool

from imageshield.intel.bounds import MAX_RUN_ATTEMPTS
from imageshield.intel.question_store import PostgresQuestionStore
from imageshield.intel.run_log import RunLog, current_run_log
from imageshield.intel.store import PostgresIntelStore
from imageshield.intel.worker import fill, serve, tick
from tests.intel_fakes import (
    NOW,
    POLICY,
    FakeFetcher,
    FakeModel,
    GatedFetcher,
    make_deps,
    make_page,
    make_signal,
)

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


# ── the lease heartbeat (spec 2026-10-03-intel-throughput §2) ─────────────────────────────────


def _real_clock() -> datetime:
    return datetime.now(UTC)


def _counting_renewals(deps: Any) -> list[bool]:
    """Wrap the store's renew_lease so a test can count the heartbeat's beats and their answers."""
    beats: list[bool] = []
    renew = deps.store.renew_lease

    async def counted(*args: Any, **kwargs: Any) -> bool:
        held: bool = await renew(*args, **kwargs)
        beats.append(held)
        return held

    deps.store.renew_lease = counted
    return beats


async def test_a_run_longer_than_its_lease_is_renewed_and_never_claimed_twice(
    intel_pool: AsyncConnectionPool,
) -> None:
    """A one-second lease on a run held for two and a half seconds: the heartbeat keeps it, so a
    second claimer finds nothing, and the run finishes on its first attempt."""
    store = PostgresIntelStore(intel_pool)
    await store.queue_adhoc(URL, operator="a")
    fetcher = GatedFetcher({URL: make_page(POLICY, URL)})
    deps = make_deps(intel_pool, fetcher, FakeModel(make_signal()), clock=_real_clock)
    beats = _counting_renewals(deps)
    worker = asyncio.create_task(tick(deps, lease_seconds=1))
    await fetcher.until_waiting(1)
    await asyncio.sleep(2.5)
    assert await store.claim_next(_real_clock(), lease_seconds=1) is None  # still held
    fetcher.release.set()
    assert await worker is True
    assert len(beats) >= 2 and all(beats)
    (run,) = await store.list_runs(cursor=None, limit=1)
    assert (run.status, run.attempts) == ("completed", 1)
    assert await _scalar(intel_pool, "SELECT lease_expires_at FROM intel_runs") is None


async def test_the_heartbeat_stops_when_the_run_finishes(intel_pool: AsyncConnectionPool) -> None:
    store = PostgresIntelStore(intel_pool)
    await store.queue_adhoc(URL, operator="a")
    fetcher = GatedFetcher({URL: make_page(POLICY, URL)})
    deps = make_deps(intel_pool, fetcher, FakeModel(make_signal()), clock=_real_clock)
    beats = _counting_renewals(deps)
    worker = asyncio.create_task(tick(deps, lease_seconds=1))
    await fetcher.until_waiting(1)
    await asyncio.sleep(0.8)
    fetcher.release.set()
    assert await worker is True
    counted = len(beats)
    assert counted >= 1
    await asyncio.sleep(1.0)  # three beats' worth
    assert len(beats) == counted


async def test_the_heartbeat_stops_when_the_run_crashes(intel_pool: AsyncConnectionPool) -> None:
    """A crash leaves the run leased for a reclaim, exactly as before; the lease is simply no
    longer renewed, so it expires and the run can be reclaimed."""
    store = PostgresIntelStore(intel_pool)
    await store.queue_adhoc(URL, operator="a")
    deps = make_deps(intel_pool, FakeFetcher({}), FakeModel(), clock=_real_clock)
    beats = _counting_renewals(deps)

    async def broken(_: Any, **__: Any) -> Any:
        await asyncio.sleep(0.6)  # long enough for one beat
        raise RuntimeError("bug")

    deps.fetcher.fetch_text = broken  # type: ignore[method-assign,assignment]
    assert await tick(deps, lease_seconds=1) is True
    counted = len(beats)
    assert counted >= 1
    await asyncio.sleep(1.0)
    assert len(beats) == counted
    (run,) = await store.list_runs(cursor=None, limit=1)
    assert run.status == "running"
    reclaimed = await store.claim_next(_real_clock(), lease_seconds=900)
    assert reclaimed is not None and reclaimed.attempts == 2


async def test_a_lost_lease_is_logged_and_never_overwrites_the_new_holder(
    intel_pool: AsyncConnectionPool,
) -> None:
    """Another claimer takes the run over while it is still executing (its lease looked lapsed
    to that claimer). The first holder's next beat finds the run no longer its own and logs it;
    the run still finishes, but its result is dropped and the newer holder's row is untouched."""
    store = PostgresIntelStore(intel_pool)
    run_id = await store.queue_adhoc(URL, operator="a")
    fetcher = GatedFetcher({URL: make_page(POLICY, URL)})
    deps = make_deps(intel_pool, fetcher, FakeModel(make_signal()), clock=_real_clock)
    with structlog.testing.capture_logs() as logs:
        worker = asyncio.create_task(tick(deps, lease_seconds=1))
        await fetcher.until_waiting(1)
        taken = await store.claim_next(_real_clock() + timedelta(hours=1), lease_seconds=900)
        assert taken is not None and taken.attempts == 2
        await asyncio.sleep(0.8)  # at least one beat after the takeover
        fetcher.release.set()
        assert await worker is True
    lost = [e for e in logs if e["event"] == "intel.run_lease_lost"]
    assert len(lost) == 1 and lost[0]["run_id"] == str(run_id) and lost[0]["attempt"] == 1
    assert [e["event"] for e in logs if e["event"] == "intel.run_result_dropped"] == [
        "intel.run_result_dropped"
    ]
    assert "intel.run_finished" not in {e["event"] for e in logs}
    (run,) = await store.list_runs(cursor=None, limit=1)
    assert (run.status, run.attempts, run.outcome) == ("running", 2, {})
    read = await store.run_events(run_id)
    assert read is not None and "run_finished" not in {e.kind for e in read.events}


async def test_a_finish_is_refused_once_another_claim_holds_the_run(
    intel_pool: AsyncConnectionPool,
) -> None:
    """The guard on the write itself, for a takeover the heartbeat had not seen yet."""
    store = PostgresIntelStore(intel_pool)
    await store.queue_adhoc(URL, operator="a")
    first = await store.claim_next(NOW, lease_seconds=60)
    assert first is not None
    second = await store.claim_next(NOW + timedelta(seconds=61), lease_seconds=60)
    assert second is not None and second.attempts == 2
    stale = await store.finish_run(
        first.run_id, status="completed", outcome={"x": 1}, attempts=first.attempts
    )
    assert stale is False
    assert (
        await store.renew_lease(first.run_id, attempts=first.attempts, now=NOW, lease_seconds=60)
        is False
    )
    assert (
        await store.finish_run(
            second.run_id, status="completed", outcome={"x": 2}, attempts=second.attempts
        )
        is True
    )
    (run,) = await store.list_runs(cursor=None, limit=1)
    assert (run.status, run.outcome) == ("completed", {"x": 2})


# ── several runs at once (spec 2026-10-03-intel-throughput §3) ───────────────────────────────

URL_B = "https://n.example/b"


class _BrokenFor(GatedFetcher):
    """A GatedFetcher whose fetch of ``broken`` raises once released: a bug in one run."""

    def __init__(self, pages: dict[str, Any], *, broken: str) -> None:
        super().__init__(pages)
        self.broken = broken

    async def fetch_text(self, url: str, *, respect_robots: bool = False) -> Any:
        fetched = await super().fetch_text(url, respect_robots=respect_robots)
        if url == self.broken:
            raise RuntimeError("bug")
        return fetched


async def _run_row(pool: AsyncConnectionPool, run_id: UUID) -> tuple[str, int]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT status, attempts FROM intel_runs WHERE run_id = %s", (run_id,)
        )
        row = await cur.fetchone()
    assert row is not None
    return row[0], row[1]


async def test_two_runs_execute_at_once_and_neither_is_claimed_twice(
    intel_pool: AsyncConnectionPool,
) -> None:
    store = PostgresIntelStore(intel_pool)
    first = await store.queue_adhoc(URL, operator="a")
    second = await store.queue_adhoc(URL_B, operator="a")
    fetcher = GatedFetcher({u: make_page(POLICY, u) for u in (URL, URL_B)})
    deps = make_deps(intel_pool, fetcher, FakeModel(make_signal()))
    running: set[asyncio.Task[None]] = set()
    assert await fill(deps, running, concurrency=2, lease_seconds=900) == 2
    await fetcher.until_waiting(2)  # both runs are mid-read at the same moment
    # Every slot is busy, and with a third slot there is nothing left to claim: both are held.
    assert await fill(deps, running, concurrency=2, lease_seconds=900) == 0
    assert await fill(deps, running, concurrency=3, lease_seconds=900) == 0
    tasks = list(running)
    fetcher.release.set()
    await asyncio.gather(*tasks)
    assert running == set()
    assert await _run_row(intel_pool, first) == ("completed", 1)
    assert await _run_row(intel_pool, second) == ("completed", 1)
    assert await _documents(intel_pool) == 2
    # Each run wrote its own log, and only its own: the same work, the same rows.
    logs = [await store.run_events(run_id) for run_id in (first, second)]
    kinds = [[e.kind for e in read.events] for read in logs if read is not None]
    assert len(kinds) == 2 and kinds[0] == kinds[1]
    assert kinds[0][0] == "run_started" and kinds[0][-1] == "run_finished"
    assert kinds[0].count("run_started") == 1 and kinds[0].count("run_finished") == 1
    assert current_run_log.get() is None


async def test_one_run_crashing_leaves_the_other_finishing(
    intel_pool: AsyncConnectionPool,
) -> None:
    store = PostgresIntelStore(intel_pool)
    crashing = await store.queue_adhoc(URL, operator="a")
    fine = await store.queue_adhoc(URL_B, operator="a")
    fetcher = _BrokenFor({u: make_page(POLICY, u) for u in (URL, URL_B)}, broken=URL)
    deps = make_deps(intel_pool, fetcher, FakeModel(make_signal()))
    running: set[asyncio.Task[None]] = set()
    with structlog.testing.capture_logs() as logs:
        assert await fill(deps, running, concurrency=2, lease_seconds=900) == 2
        await fetcher.until_waiting(2)
        tasks = list(running)
        fetcher.release.set()
        await asyncio.gather(*tasks)  # neither task raises: the crash is that run's alone
    crashed = [e for e in logs if e["event"] == "intel.run_crashed"]
    assert [e["run_id"] for e in crashed] == [str(crashing)]
    assert await _run_row(intel_pool, crashing) == ("running", 1)  # leased for a reclaim
    assert await _run_row(intel_pool, fine) == ("completed", 1)
    read = await store.run_events(fine)
    assert read is not None and read.events[-1].kind == "run_finished"


def _serve(deps: Any, stopping: asyncio.Event, **overrides: Any) -> asyncio.Task[None]:
    settings: dict[str, Any] = {
        "enabled": True,
        "run_concurrency": 2,
        "lease_seconds": 900,
        "poll_seconds": 60,
        **overrides,
    }
    return asyncio.create_task(serve(deps, stopping=stopping, **settings))


async def test_serve_refills_a_slot_as_soon_as_a_run_ends(intel_pool: AsyncConnectionPool) -> None:
    """One slot, two queued runs, a poll interval far longer than the test: the second run is
    claimed the moment the first ends, not a poll later."""
    store = PostgresIntelStore(intel_pool)
    first = await store.queue_adhoc(URL, operator="a")
    second = await store.queue_adhoc(URL_B, operator="a")
    fetcher = GatedFetcher({u: make_page(POLICY, u) for u in (URL, URL_B)}, hold={URL})
    deps = make_deps(intel_pool, fetcher, FakeModel(make_signal()))
    stopping = asyncio.Event()
    loop = _serve(deps, stopping, run_concurrency=1)
    await fetcher.until_waiting(1)
    assert await _run_row(intel_pool, second) == ("queued", 0)  # the one slot is busy
    fetcher.release.set()
    for _ in range(100):
        if (await _run_row(intel_pool, second))[0] == "completed":
            break
        await asyncio.sleep(0.05)
    assert await _run_row(intel_pool, first) == ("completed", 1)
    assert await _run_row(intel_pool, second) == ("completed", 1)
    stopping.set()
    await asyncio.wait_for(loop, 5)


async def test_stopping_lets_in_flight_runs_finish_within_the_drain(
    intel_pool: AsyncConnectionPool,
) -> None:
    store = PostgresIntelStore(intel_pool)
    first = await store.queue_adhoc(URL, operator="a")
    second = await store.queue_adhoc(URL_B, operator="a")
    fetcher = GatedFetcher({u: make_page(POLICY, u) for u in (URL, URL_B)})
    deps = make_deps(intel_pool, fetcher, FakeModel(make_signal()))
    stopping = asyncio.Event()
    loop = _serve(deps, stopping, drain_seconds=10)
    await fetcher.until_waiting(2)
    stopping.set()
    await asyncio.sleep(0.2)
    await store.queue_adhoc("https://n.example/c", operator="a")  # stopping: never claimed
    fetcher.release.set()
    await asyncio.wait_for(loop, 10)
    assert await _run_row(intel_pool, first) == ("completed", 1)
    assert await _run_row(intel_pool, second) == ("completed", 1)
    queued = "SELECT count(*) FROM intel_runs WHERE status = 'queued'"
    assert await _scalar(intel_pool, queued) == 1


async def test_runs_still_going_after_the_drain_are_left_leased(
    intel_pool: AsyncConnectionPool,
) -> None:
    store = PostgresIntelStore(intel_pool)
    run_id = await store.queue_adhoc(URL, operator="a")
    fetcher = GatedFetcher({URL: make_page(POLICY, URL)})  # never released
    deps = make_deps(intel_pool, fetcher, FakeModel(make_signal()))
    stopping = asyncio.Event()
    with structlog.testing.capture_logs() as logs:
        loop = _serve(deps, stopping, drain_seconds=0.2)
        await fetcher.until_waiting(1)
        stopping.set()
        await asyncio.wait_for(loop, 5)
    assert "intel.runs_left_leased" in {e["event"] for e in logs}
    assert await _run_row(intel_pool, run_id) == ("running", 1)
    lease = "SELECT lease_expires_at IS NOT NULL FROM intel_runs"
    assert await _scalar(intel_pool, lease) is True
