"""The score tick process.

``python -m imageshield.score.tick`` — its own process, like the recheck
worker and the relay. There is no queue here either, for the same reason as
``recheck/worker.py``: "which subjects need recomputing on a routine cadence"
is "all of them, periodically", not an event a producer could enqueue one of.

One pass does two things, in order: expire threat events whose ``expires_at``
has passed (nothing else does this — a threat event past its window has to
stop decaying scores and stop generating ``run_priority_scan``
recommendations even if nobody's recompute happens to touch it), then
recompute every known subject with ``cause_kind="tick"`` so a score that
would otherwise only move when *something happens* to a person (a hit, a
feedback signal, a new seed) still reflects time passing — a threat event
aging out, a scan going overdue, a recommendation crossing its soft-age
threshold.

Same loop skeleton as ``recheck/worker.py``: a stop event, signal handlers
suppressed with ``contextlib.suppress(NotImplementedError)`` (no signal
support on Windows' selector loop — Ctrl-C still raises ``KeyboardInterrupt``
out of the runner), ``asyncio.wait_for`` on the stop event standing in for
sleep so a deploy does not wait out a full interval, and one bad pass must
never kill the sweep — the subjects it did not reach this tick are still
there next tick.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from datetime import UTC, datetime

import structlog

from imageshield.config import Config, ConfigError, load_config
from imageshield.db.connection import make_async_pool
from imageshield.http.logging import configure_logging
from imageshield.score.engine import ScoreWeights
from imageshield.score.store import PostgresScoreStore, ScoreStore

log = structlog.get_logger("imageshield.score.tick")


async def run_once(store: ScoreStore) -> None:
    """One pass: expire due threat events, then recompute every subject.

    Factored out of ``run_forever`` so it can be exercised without the
    scheduler around it — same split as ``recheck/loop.py`` vs.
    ``recheck/worker.py``. No per-subject error isolation: an exception here
    aborts the pass exactly like any other, and ``run_forever``'s
    try/except is what keeps a single bad pass from killing the process.
    """
    now = datetime.now(UTC)
    expired = await store.expire_due_threat_events(now=now)
    subjects = await store.all_subject_refs()
    for user_ref in subjects:
        await store.recompute(user_ref, cause_kind="tick")
    log.info("score.tick_pass_completed", expired_events=expired, subjects=len(subjects))


async def run_forever(config: Config) -> None:
    stopping = asyncio.Event()

    def _stop() -> None:
        log.info("score.tick_stopping")
        stopping.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Windows dev: no signal handlers on the selector loop. Ctrl-C still
        # raises KeyboardInterrupt out of the runner.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _stop)

    pool = make_async_pool(config.database_url, min_size=1, max_size=4)
    await pool.open()
    store = PostgresScoreStore(
        pool,
        weights=ScoreWeights.from_config(config),
        config_version=config.score_config_version,
    )

    log.info("score.tick_started", interval_seconds=config.score_tick_interval_seconds)
    try:
        while not stopping.is_set():
            try:
                await run_once(store)
            except Exception:
                # One bad pass must not kill the sweep. Every subject it did
                # not reach is still there next tick.
                log.exception("score.tick_pass_failed")
            # Sleep, but wake immediately on SIGTERM rather than finishing
            # the interval — a deploy should not wait an hour.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    stopping.wait(), timeout=config.score_tick_interval_seconds
                )
    finally:
        await pool.close()
    log.info("score.tick_stopped")


def main() -> int:
    configure_logging()
    try:
        config = load_config()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if sys.platform == "win32":
        # psycopg's async pool cannot run on Windows' default Proactor loop —
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
    raise SystemExit(main())
