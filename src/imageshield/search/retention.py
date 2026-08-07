"""Null ``provider_calls.raw_response`` past ``RAW_RESPONSE_RETENTION_DAYS``.

A one-shot CLI (``python -m imageshield.search.retention``) meant for a
scheduler — cron, or an ECS scheduled task alongside the relay and the search
worker. It is not a daemon: there is nothing to poll for.

The metadata row survives — status, http_status, latency, cost, and the run
it belonged to. Only the JSONB payload is dropped. That payload is what makes
recalibration over history possible when a provider retunes (CLAUDE.md §7.2),
and 90 days of it is enough for that; keeping it forever is how one row per
(run, provider) turns into the largest table in the database.

Idempotent by construction: the ``raw_response IS NOT NULL`` predicate means
a second pass over the same window finds nothing, so a scheduler that
double-fires costs one cheap query.
"""

from __future__ import annotations

import asyncio
import sys

import structlog
from psycopg_pool import AsyncConnectionPool

from imageshield.config import Config, ConfigError, load_config
from imageshield.db.connection import make_async_pool
from imageshield.http.logging import configure_logging

_NULL_EXPIRED_SQL = """
    UPDATE provider_calls
    SET raw_response = NULL
    WHERE raw_response IS NOT NULL
      AND created_at < now() - make_interval(days => %(days)s)
"""


async def null_expired_raw_responses(
    pool: AsyncConnectionPool, *, retention_days: int
) -> int:
    """Returns the number of payloads dropped."""
    async with pool.connection() as conn:
        cur = await conn.execute(_NULL_EXPIRED_SQL, {"days": retention_days})
        return cur.rowcount


async def run_once(config: Config) -> None:
    log = structlog.get_logger("imageshield.search.retention")
    pool = make_async_pool(config.database_url, min_size=1, max_size=1)
    await pool.open()
    try:
        nulled = await null_expired_raw_responses(
            pool, retention_days=config.raw_response_retention_days
        )
    finally:
        await pool.close()
    log.info(
        "retention.raw_responses_nulled",
        nulled=nulled,
        retention_days=config.raw_response_retention_days,
    )


def main() -> int:
    configure_logging()
    try:
        config = load_config()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if sys.platform == "win32":
        # psycopg's async pool cannot run on Windows' default Proactor loop —
        # same constraint (and same fix) as imageshield/search/worker.py.
        # Local dev only; the deployed container is Linux.
        import selectors

        with asyncio.Runner(
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())
        ) as runner:
            runner.run(run_once(config))
        return 0

    asyncio.run(run_once(config))
    return 0


if __name__ == "__main__":
    sys.exit(main())
