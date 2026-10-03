"""The intel worker (spec §4.1). ``python -m imageshield.intel.worker`` -- a third
container in services-worker; a polled loop like ``recheck/worker.py``, no queue.

*Amended 2026-10-03 (throughput, spec 2026-10-03-intel-throughput):* it used to claim ONE run
per tick and execute it before the next tick, so a fifteen-minute Suggest-points run held the
whole queue. Now each loop pass (``serve``) does housekeeping ONCE -- reconcile, resolve gaps,
expire exhausted runs, pause unmapped sources, schedule due sources and renewals -- then claims
runs while one of ``INTEL_RUN_CONCURRENCY`` slots is free, each executed in its own task. A pass
runs again as soon as a run ends (a slot frees), and at least every ``INTEL_POLL_SECONDS``.

- **Claims.** ``claim_next``'s ``FOR UPDATE SKIP LOCKED`` keeps any two claimers -- two slots
  here, or two services-worker tasks (prod runs two) -- off one run. Each claimed run's lease is
  RENEWED while it executes (``_Lease``), so a run longer than ``INTEL_LEASE_SECONDS`` is never
  claimed a second time while still running; a lease does lapse when its worker dies, and a
  reclaim follows as before. The finish write is guarded on the claim (``attempts``).
- **Runs are independent.** Each run's task binds its OWN ``current_run_log`` (a task runs in a
  copy of the context), writes its own ``run_started`` and ``run_finished``, and crashes alone:
  a crash is logged and leaves that run leased, and the loop and the other runs carry on.
- **Shutdown.** SIGTERM stops claiming. In-flight runs get ``_DRAIN_SECONDS`` to finish (inside
  ECS's default 30 s stop timeout); any still running are cancelled and left leased, to be
  reclaimed by the next worker once their lease lapses.

Each executed run gets a run log (spec 2026-10-03 §3.3): ``run_started`` before it,
``run_finished`` after it and BEFORE ``finish_run``, so a poll that sees the run's terminal
status always sees its whole log. ``current_run_log`` is bound for the duration of
``run(claimed, deps)``, which is how the model seam and ``metered()`` reach it.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from imageshield.config import ConfigError
from imageshield.db.connection import make_async_pool
from imageshield.http.logging import configure_logging
from imageshield.intel.config import IntelConfig, load_intel_config
from imageshield.intel.evidence_store import PostgresEvidenceStore
from imageshield.intel.fetch_client import HttpTextFetcher
from imageshield.intel.model import ClaudeIntelModel, IntelModel
from imageshield.intel.models import Run
from imageshield.intel.pipeline import PipelineDeps, run
from imageshield.intel.proposal_store import PostgresProposalStore
from imageshield.intel.protection_store import PostgresProtectionStore
from imageshield.intel.question_store import PostgresQuestionStore
from imageshield.intel.reconcile import PostgresReconciler
from imageshield.intel.run_log import (
    RunLog,
    current_run_log,
    run_finished_event,
    run_started_event,
)
from imageshield.intel.store import PostgresIntelStore
from imageshield.providers.store import PostgresProviderControlStore

log = structlog.get_logger("imageshield.intel.worker")


def loggable_outcome(outcome: dict[str, Any]) -> dict[str, Any]:
    """The scalar half of a run's outcome: its counts, cost and verdict words. A question run's
    outcome also carries lists -- the candidates it judged, the sources it proposed -- and those
    stay in intel_runs, never in a log line, where the structlog processor masks phone shapes
    only (final review I1, 2026-10-01)."""
    return {
        key: value
        for key, value in outcome.items()
        if value is None or isinstance(value, (bool, int, float, str))
    }


def _build_model(config: IntelConfig) -> IntelModel:
    if config.intel_model_provider == "stub":
        from imageshield.intel.stub import StubIntelModel

        return StubIntelModel()
    return ClaudeIntelModel(config)


async def housekeeping(deps: PipelineDeps) -> None:
    """Everything a loop pass does before it claims: reconcile a new vocabulary, resolve newly
    mapped gaps, expire exhausted runs, pause sources whose tags left the quiz, schedule due
    sources and due renewals. Once per pass, however many runs the pass then claims."""
    now = deps.clock()
    # spec §4.9: react to a new vocabulary within one poll, before any run loads it; then
    # resolve every pending gap the live quiz now maps (state-based), so its regeneration run
    # is claimable on this same tick.
    await deps.reconciler.reconcile()
    await deps.reconciler.resolve_gaps(now)
    await deps.store.expire_exhausted(now)
    # spec §4.10: sources follow the quiz. State-based, and before scheduling, so a source whose
    # tags all left the live quiz is paused before it can be queued.
    await deps.store.pause_unmapped_sources()
    await deps.store.schedule_due(now)
    # spec §4.8: one renewal check for each credit near its review date. No model call.
    await deps.protections.schedule_renewals(now)


async def tick(deps: PipelineDeps, *, lease_seconds: int) -> bool:
    """One pass with one slot: housekeeping, then claim at most one run, execute it, finish it.
    Returns whether a run was claimed. The deployed loop is ``serve``, which keeps several runs
    in flight; this is the same pass for exactly one, kept for scripts and tests."""
    await housekeeping(deps)
    claimed = await deps.store.claim_next(deps.clock(), lease_seconds=lease_seconds)
    if claimed is None:
        return False
    await execute(claimed, deps, lease_seconds=lease_seconds)
    return True


class _Lease:
    """A claimed run's heartbeat (spec 2026-10-03-intel-throughput §2): while the run executes,
    its lease is renewed every third of ``lease_seconds``, so a run longer than its lease is never
    claimed a second time while it is still running. Only the claim that holds the run renews it
    (``renew_lease`` is guarded on ``attempts``). A renewal that finds the run taken over sets
    ``lost``, is logged, and stops; one that errors is logged and tried again next beat."""

    def __init__(self, claimed: Run, deps: PipelineDeps, *, lease_seconds: int) -> None:
        self._claimed = claimed
        self._deps = deps
        self._lease_seconds = lease_seconds
        self.lost = False
        self._task = asyncio.create_task(self._beat(), name=f"intel-lease-{claimed.run_id}")

    async def _beat(self) -> None:
        interval = self._lease_seconds / 3
        run_id = str(self._claimed.run_id)
        while True:
            await asyncio.sleep(interval)
            try:
                held = await self._deps.store.renew_lease(
                    self._claimed.run_id,
                    attempts=self._claimed.attempts,
                    now=self._deps.clock(),
                    lease_seconds=self._lease_seconds,
                )
            except Exception:
                log.warning("intel.run_lease_renewal_failed", run_id=run_id)
                continue
            if not held:
                self.lost = True
                log.warning(
                    "intel.run_lease_lost",
                    run_id=run_id,
                    kind=self._claimed.kind,
                    attempt=self._claimed.attempts,
                )
                return

    async def stop(self) -> None:
        """Stop renewing. Idempotent. Waits for the beat to end without ever raising its
        cancellation into the caller (``asyncio.wait`` returns rather than raising)."""
        self._task.cancel()
        await asyncio.wait({self._task})


async def execute(claimed: Run, deps: PipelineDeps, *, lease_seconds: int) -> None:
    """Execute ONE claimed run: its run log, its lease heartbeat, ``run()``, its finish.

    Each run's log is bound for the duration of ``run(claimed, deps)`` (spec 2026-10-03 §3.3):
    in its own task under ``serve`` (a task runs in a copy of the context, so concurrent runs
    never see each other's log), inline under ``tick``. ``run_started`` comes before the run,
    ``run_finished`` after it and BEFORE ``finish_run``, so a poll that sees the run's terminal
    status always sees its whole log. The heartbeat stops before the finish write; the finish
    is guarded on this claim, so a holder that lost its lease writes nothing over the newer
    holder's result."""
    lease = _Lease(claimed, deps, lease_seconds=lease_seconds)
    run_id = str(claimed.run_id)
    try:
        run_log = RunLog(claimed.run_id, deps.store)
        started = time.monotonic()
        await run_log.append("run_started", *run_started_event(claimed))
        bound = current_run_log.set(run_log)
        try:
            result = await run(claimed, deps)
        except Exception:
            # A bug or a DB error -- never a page/feed/model outcome, which `run()`
            # always turns into a RunResult instead of raising. Leave the run
            # leased: it is reclaimed once the lease expires, capped at
            # MAX_RUN_ATTEMPTS (`expire_exhausted` then fails it for good rather
            # than retrying forever). Its log stops here; a reclaim appends to it.
            log.exception("intel.run_crashed", run_id=run_id, kind=claimed.kind)
            return
        finally:
            current_run_log.reset(bound)
        await lease.stop()
        if lease.lost:
            # Another claimer holds the run now (or expire_exhausted ended it): its result is
            # the one that stands. This attempt's log stops without run_finished, like a crash.
            log.warning(
                "intel.run_result_dropped", run_id=run_id, kind=claimed.kind, status=result.status
            )
            return
        await run_log.append(
            "run_finished",
            *run_finished_event(
                result.status,
                result.error_code,
                result.outcome.get("cost_usd"),
                int((time.monotonic() - started) * 1000),
            ),
        )
        finished = await deps.store.finish_run(
            claimed.run_id,
            status=result.status,
            outcome=result.outcome,
            error_code=result.error_code,
            attempts=claimed.attempts,
        )
        if not finished:
            log.warning(
                "intel.run_result_dropped", run_id=run_id, kind=claimed.kind, status=result.status
            )
            return
        log.info(
            "intel.run_finished",
            run_id=run_id,
            kind=claimed.kind,
            status=result.status,
            outcome=loggable_outcome(result.outcome),
        )
    finally:
        await lease.stop()


async def _execute_alone(claimed: Run, deps: PipelineDeps, *, lease_seconds: int) -> None:
    """``execute`` in its own task: whatever escapes it (a DB error writing the finish, say) is
    logged here and ends this run's task only. The run stays leased and is reclaimed."""
    try:
        await execute(claimed, deps, lease_seconds=lease_seconds)
    except Exception:
        log.exception("intel.run_task_failed", run_id=str(claimed.run_id), kind=claimed.kind)


async def fill(
    deps: PipelineDeps,
    running: set[asyncio.Task[None]],
    *,
    concurrency: int,
    lease_seconds: int,
) -> int:
    """One loop pass: housekeeping once, then claim runs while ``running`` holds fewer than
    ``concurrency``, each executed in its own task (added to ``running``, removed when it ends).
    Returns how many runs this pass claimed."""
    await housekeeping(deps)
    claimed_count = 0
    while len(running) < concurrency:
        claimed = await deps.store.claim_next(deps.clock(), lease_seconds=lease_seconds)
        if claimed is None:
            break
        # A task runs in a COPY of this context, where no run log is bound; execute() binds
        # this run's own inside it, so concurrent runs never write to each other's log.
        task = asyncio.create_task(
            _execute_alone(claimed, deps, lease_seconds=lease_seconds),
            name=f"intel-run-{claimed.run_id}",
        )
        running.add(task)
        task.add_done_callback(running.discard)
        claimed_count += 1
    return claimed_count


# How long SIGTERM waits for in-flight runs before cancelling them, leaving them leased. Inside
# ECS's default 30 s stop timeout (the task definitions set none), so the pool and the HTTP client
# still close cleanly before the container is killed.
_DRAIN_SECONDS = 20.0


async def serve(
    deps: PipelineDeps,
    *,
    stopping: asyncio.Event,
    enabled: bool,
    run_concurrency: int,
    lease_seconds: int,
    poll_seconds: float,
    drain_seconds: float = _DRAIN_SECONDS,
) -> None:
    """The loop: a pass (``fill``), then sleep until a run ends, ``stopping`` is set, or
    ``poll_seconds`` pass, whichever is first -- so a freed slot is refilled at once and
    housekeeping still runs every poll while every slot is busy. On ``stopping``: claim nothing
    more, give in-flight runs ``drain_seconds``, then cancel what is left (it stays leased)."""
    running: set[asyncio.Task[None]] = set()
    stop = asyncio.ensure_future(stopping.wait())
    try:
        while not stopping.is_set():
            if enabled:
                try:
                    await fill(
                        deps, running, concurrency=run_concurrency, lease_seconds=lease_seconds
                    )
                except Exception:
                    # One bad pass must not kill the loop. Whatever it was working on is still
                    # queued/leased and picked up later; in-flight runs are untouched.
                    log.exception("intel.tick_failed")
            await asyncio.wait(
                {stop, *running}, timeout=poll_seconds, return_when=asyncio.FIRST_COMPLETED
            )
        await _drain(running, drain_seconds)
    finally:
        stop.cancel()


async def _drain(running: set[asyncio.Task[None]], drain_seconds: float) -> None:
    if not running:
        return
    log.info("intel.draining", runs=len(running))
    _, pending = await asyncio.wait(set(running), timeout=drain_seconds)
    if pending:
        # Cancelled mid-run: each one's lease heartbeat stops with it, and the run is reclaimed
        # by the next worker once its lease lapses, exactly as after a crash.
        log.warning("intel.runs_left_leased", runs=len(pending))
        for task in pending:
            task.cancel()
        await asyncio.wait(pending)


async def run_forever(config: IntelConfig) -> None:
    stopping = asyncio.Event()

    def _stop() -> None:
        log.info("intel.stopping")
        stopping.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Windows dev: no signal handlers on the selector loop. Ctrl-C still
        # raises KeyboardInterrupt out of the runner.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _stop)

    pool = make_async_pool(config.database_url, min_size=1, max_size=config.db_pool_max_size)
    await pool.open()
    http_client = httpx.AsyncClient()
    deps = PipelineDeps(
        store=PostgresIntelStore(pool),
        evidence=PostgresEvidenceStore(pool),
        fetcher=HttpTextFetcher(http_client, config.fetcher_base_url, config.fetcher_token),
        model=_build_model(config),
        control=PostgresProviderControlStore(
            pool,
            cache_seconds=config.provider_config_cache_seconds,
            failure_threshold=config.provider_failure_threshold,
            default_cooldown_seconds=config.breaker_cooldown_seconds,
            max_cooldown_seconds=config.breaker_cooldown_max_seconds,
        ),
        proposals=PostgresProposalStore(pool),
        reconciler=PostgresReconciler(pool),
        protections=PostgresProtectionStore(pool),
        clock=lambda: datetime.now(UTC),
        # No default on PipelineDeps for either of these (task 10) -- a second
        # default beside IntelConfig's would be a second source of truth.
        max_calls_per_run=config.intel_max_calls_per_run,
        max_document_chars=config.intel_max_document_chars,
        questions=PostgresQuestionStore(pool),
        max_calls_per_suggestion_run=config.intel_max_calls_per_suggestion_run,
    )
    log.info(
        "intel.started",
        enabled=config.intel_enabled,
        provider=config.intel_model_provider,
        run_concurrency=config.intel_run_concurrency,
        source_read_concurrency=config.intel_source_read_concurrency,
    )
    if config.db_pool_max_size < config.pool_size_needed():
        # Not a refusal: statements are short, so a smaller pool queues rather than fails. But a
        # read waiting on a connection is a read not running, so say so where it can be seen.
        log.warning(
            "intel.pool_below_concurrency",
            db_pool_max_size=config.db_pool_max_size,
            needed=config.pool_size_needed(),
        )
    try:
        await serve(
            deps,
            stopping=stopping,
            enabled=config.intel_enabled,
            run_concurrency=config.intel_run_concurrency,
            lease_seconds=config.intel_lease_seconds,
            poll_seconds=config.intel_poll_seconds,
        )
    finally:
        await http_client.aclose()
        await pool.close()
    log.info("intel.stopped")


def main() -> int:
    configure_logging()
    try:
        config = load_intel_config()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if sys.platform == "win32":
        # psycopg's async pool cannot run on Windows' default Proactor loop --
        # same constraint (and same fix) as recheck/worker.py and
        # search/worker.py. Local dev only; the deployed container is Linux.
        import selectors

        with asyncio.Runner(
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())
        ) as runner:
            runner.run(run_forever(config))
        return 0

    asyncio.run(run_forever(config))
    return 0


if __name__ == "__main__":
    sys.exit(main())
