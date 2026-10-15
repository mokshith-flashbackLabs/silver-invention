"""Snapshot the evidence of every event approved before 0055 (spec
2026-10-15-intel-lessons-and-evidence §6).

A one-shot CLI (``python -m imageshield.intel.backfill_evidence``), run once per environment after
the migration, the shape of imageshield/search/retention.py. Idempotent: an event whose snapshot
exists is skipped, so a second run writes nothing. A hand-created threat has no proposal and so no
evidence; it is never snapshotted.
"""

from __future__ import annotations

import asyncio
import sys

import structlog
from psycopg_pool import AsyncConnectionPool

from imageshield.config import Config, ConfigError, load_config
from imageshield.db.connection import make_async_pool
from imageshield.http.logging import configure_logging
from imageshield.intel.lesson_store import snapshot_evidence
from imageshield.intel.proposal_store import fetch_linked_signals

_MISSING_SQL = """
    SELECT event_id, proposal_id FROM threat_events
     WHERE proposal_id IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM intel_published_owners o
                        WHERE o.owner_kind = 'event' AND o.owner_id = threat_events.event_id)
    UNION ALL
    SELECT event_id, proposal_id FROM protection_events
     WHERE NOT EXISTS (SELECT 1 FROM intel_published_owners o
                        WHERE o.owner_kind = 'event' AND o.owner_id = protection_events.event_id)
"""


async def backfill(pool: AsyncConnectionPool) -> int:
    """Snapshot every approved event that has none yet; returns how many were written."""
    written = 0
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(_MISSING_SQL)
        rows = await cur.fetchall()
        linked = await fetch_linked_signals(conn, list({row[1] for row in rows}))
        for event_id, proposal_id in rows:
            active = [s for s in linked[proposal_id] if s.status == "active"]
            await snapshot_evidence(
                conn, owner_kind="event", owner_id=event_id, proposal_id=proposal_id, active=active
            )
            written += 1
    return written


async def run_once(config: Config) -> None:
    log = structlog.get_logger("imageshield.intel")
    pool = make_async_pool(config.database_url, min_size=1, max_size=1)
    await pool.open()
    try:
        written = await backfill(pool)
    finally:
        await pool.close()
    log.info("intel.evidence_backfilled", events=written)


def main() -> int:
    configure_logging()
    try:
        config = load_config()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if sys.platform == "win32":
        # psycopg's async pool cannot run on Windows' default Proactor loop (search/retention.py).
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
