"""The recheck worker process.

``python -m imageshield.recheck.worker`` — its own process, like the relay and
the ``search:runs`` consumer, and **never** in the API process. Two reasons,
both structural:

- It makes outbound requests to hostile third-party hosts. That egress belongs
  on its own path with no VPC access to any internal service (INVARIANTS #11),
  which is a deployment property the API process cannot have.
- A pass is deliberately slow — sequential, paced per domain — and nothing
  about it should share a thread pool with a request handler.

There is no queue. This is a polled sweep over `infringements`, not an
event-driven consumer, because "which rows are due" is a question the table can
answer directly and an outbox row per infringement per week would be a queue of
work the database already knows about.

Idempotency is free here: a pass that is interrupted has recorded whatever
verdicts it reached, and the rows it did not get to are still due next pass.
Rechecking a URL twice costs one HEAD.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys

import structlog

from imageshield.config import Config, ConfigError, load_config
from imageshield.db.connection import make_async_pool
from imageshield.http.logging import configure_logging
from imageshield.recheck.client import UrlChecker
from imageshield.recheck.http import HttpxHeadTransport, make_client
from imageshield.recheck.loop import run_once
from imageshield.recheck.pacer import DomainPacer
from imageshield.recheck.store import PostgresRecheckStore

log = structlog.get_logger("imageshield.recheck.worker")


async def run_forever(config: Config) -> None:
    stopping = asyncio.Event()

    def _stop() -> None:
        log.info("recheck.stopping")
        stopping.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Windows dev: no signal handlers on the selector loop. Ctrl-C still
        # raises KeyboardInterrupt out of the runner.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _stop)

    pool = make_async_pool(config.database_url, min_size=1, max_size=4)
    await pool.open()
    store = PostgresRecheckStore(pool)
    http_client = make_client(max_redirects=config.recheck_max_redirects)
    checker = UrlChecker(
        HttpxHeadTransport(http_client),
        timeout_seconds=config.recheck_timeout_seconds,
        max_redirects=config.recheck_max_redirects,
    )
    # One pacer for the process lifetime, so its per-domain memory survives
    # across passes — otherwise every pass would get a free burst.
    pacer = DomainPacer(config.recheck_per_domain_interval_seconds)

    log.info(
        "recheck.started",
        interval_days=config.recheck_interval_days,
        batch_size=config.recheck_batch_size,
    )
    try:
        while not stopping.is_set():
            try:
                await run_once(
                    store,
                    checker,
                    pacer,
                    interval_days=config.recheck_interval_days,
                    batch_size=config.recheck_batch_size,
                )
            except Exception:
                # One bad pass must not kill the sweep. The rows it did not
                # reach are still due; the next pass picks them up.
                log.exception("recheck.pass_failed")
            # Sleep, but wake immediately on SIGTERM rather than finishing
            # the interval — a deploy should not wait five minutes.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    stopping.wait(), timeout=config.recheck_poll_interval_seconds
                )
    finally:
        await http_client.aclose()
        await pool.close()
    log.info("recheck.stopped")


def main() -> int:
    configure_logging()
    try:
        config = load_config()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if sys.platform == "win32":
        # psycopg's async pool cannot run on Windows' default Proactor loop —
        # same constraint (and same fix) as search/worker.py. Local dev only;
        # the deployed container is Linux.
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
