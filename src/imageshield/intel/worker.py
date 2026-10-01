"""The intel worker (spec §4.1). ``python -m imageshield.intel.worker`` -- a third
container in services-worker; a polled loop like ``recheck/worker.py``, no queue.

Each tick: expire runs at the attempt cap, schedule due sources, claim ONE run
under a lease, execute it, finish it. One worker (``services-worker`` runs
desired count 1); the lease protects against a crash, not against two live
workers -- ``claim_next``'s ``FOR UPDATE SKIP LOCKED`` is what would make a
second one safe if that ever changed.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from datetime import UTC, datetime

import httpx
import structlog

from imageshield.config import ConfigError
from imageshield.db.connection import make_async_pool
from imageshield.http.logging import configure_logging
from imageshield.intel.config import IntelConfig, load_intel_config
from imageshield.intel.evidence_store import PostgresEvidenceStore
from imageshield.intel.fetch_client import HttpTextFetcher
from imageshield.intel.model import ClaudeIntelModel, IntelModel
from imageshield.intel.pipeline import PipelineDeps, run
from imageshield.intel.proposal_store import PostgresProposalStore
from imageshield.intel.question_store import PostgresQuestionStore
from imageshield.intel.reconcile import PostgresReconciler
from imageshield.intel.store import PostgresIntelStore
from imageshield.providers.store import PostgresProviderControlStore

log = structlog.get_logger("imageshield.intel.worker")


def _build_model(config: IntelConfig) -> IntelModel:
    if config.intel_model_provider == "stub":
        from imageshield.intel.stub import StubIntelModel

        return StubIntelModel()
    return ClaudeIntelModel(config)


async def tick(deps: PipelineDeps, *, lease_seconds: int) -> bool:
    """One pass: reconcile a new vocabulary, expire exhausted runs, schedule due
    sources, claim at most one run, execute it, finish it. Returns whether a run was executed,
    so the caller can poll again immediately while there is work and back off once
    the queue is empty."""
    now = deps.clock()
    # spec §4.9: react to a new vocabulary within one poll, before any run loads it.
    await deps.reconciler.reconcile()
    await deps.store.expire_exhausted(now)
    # spec §4.10: sources follow the quiz. State-based, and before scheduling, so a source whose
    # tags all left the live quiz is paused before it can be queued.
    await deps.store.pause_unmapped_sources()
    await deps.store.schedule_due(now)
    claimed = await deps.store.claim_next(now, lease_seconds=lease_seconds)
    if claimed is None:
        return False
    try:
        result = await run(claimed, deps)
    except Exception:
        # A bug or a DB error -- never a page/feed/model outcome, which `run()`
        # always turns into a RunResult instead of raising. Leave the run
        # leased: it is reclaimed once the lease expires, capped at
        # MAX_RUN_ATTEMPTS (`expire_exhausted` then fails it for good rather
        # than retrying forever).
        log.exception("intel.run_crashed", run_id=str(claimed.run_id), kind=claimed.kind)
        return True
    await deps.store.finish_run(
        claimed.run_id, status=result.status, outcome=result.outcome, error_code=result.error_code
    )
    log.info(
        "intel.run_finished",
        run_id=str(claimed.run_id),
        kind=claimed.kind,
        status=result.status,
        outcome=result.outcome,
    )
    return True


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
        clock=lambda: datetime.now(UTC),
        # No default on PipelineDeps for either of these (task 10) -- a second
        # default beside IntelConfig's would be a second source of truth.
        max_calls_per_run=config.intel_max_calls_per_run,
        max_document_chars=config.intel_max_document_chars,
        questions=PostgresQuestionStore(pool),
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
