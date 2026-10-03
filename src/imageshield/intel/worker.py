"""The intel worker (spec §4.1). ``python -m imageshield.intel.worker`` -- a third
container in services-worker; a polled loop like ``recheck/worker.py``, no queue.

Each tick: expire runs at the attempt cap, schedule due sources, claim ONE run
under a lease, execute it, finish it. One worker (``services-worker`` runs
desired count 1); the lease protects against a crash, not against two live
workers -- ``claim_next``'s ``FOR UPDATE SKIP LOCKED`` is what would make a
second one safe if that ever changed.

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
    log.info("intel.started", enabled=config.intel_enabled, provider=config.intel_model_provider)
    try:
        while not stopping.is_set():
            worked = False
            if config.intel_enabled:
                try:
                    worked = await tick(deps, lease_seconds=config.intel_lease_seconds)
                except Exception:
                    # One bad tick must not kill the loop. Whatever it was
                    # working on is still queued/leased and picked up later.
                    log.exception("intel.tick_failed")
            if not worked:
                # Sleep, but wake immediately on SIGTERM rather than finishing
                # the interval -- a deploy should not wait for a poll to elapse.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stopping.wait(), timeout=config.intel_poll_seconds)
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
