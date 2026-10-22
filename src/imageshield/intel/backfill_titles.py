"""Give a title to every cited document read before the fetcher reported one (2026-10-22, spec
2026-10-15-intel-lessons-and-evidence §13).

A one-shot CLI (``python -m imageshield.intel.backfill_titles``), the shape of
intel/backfill_evidence.py. Until 2026-10-22 only a feed item carried a title, so every evidence
panel named its publisher alone and a lesson's link label could not be checked against its page.
Only documents some proposal cites are read (published evidence is a subset of those), each page
once, through the isolated fetcher like any other read (INVARIANTS #11). The title is masked as a
page-derived title always is. Idempotent: a titled document is never read again, and a failed
read leaves the document untitled for the next run.
"""

from __future__ import annotations

import asyncio
import sys
from uuid import UUID

import httpx
import structlog
from psycopg_pool import AsyncConnectionPool

from imageshield.config import Config, ConfigError, load_config
from imageshield.db.connection import make_async_pool
from imageshield.http.logging import configure_logging
from imageshield.intel.fetch_client import HttpTextFetcher, TextFetch, TextFetcher
from imageshield.intel.pii import mask

log = structlog.get_logger("imageshield.intel")

# Pages read at once: a one-off, kept well under the fetcher's own concurrency gate.
_CONCURRENCY = 4

_UNTITLED_SQL = """
    SELECT d.final_url, array_agg(d.document_id ORDER BY d.document_id)
      FROM intel_documents d
     WHERE d.title = ''
       AND EXISTS (SELECT 1 FROM intel_signals s
                     JOIN intel_proposal_signals ps ON ps.signal_id = s.signal_id
                    WHERE s.document_id = d.document_id)
     GROUP BY d.final_url
     ORDER BY d.final_url
"""


async def backfill_titles(pool: AsyncConnectionPool, fetcher: TextFetcher) -> int:
    """Title every untitled cited document whose page now reports one; returns the pages titled."""
    async with pool.connection() as conn:
        cur = await conn.execute(_UNTITLED_SQL)
        pages: list[tuple[str, list[UUID]]] = [(row[0], row[1]) for row in await cur.fetchall()]
    gate = asyncio.Semaphore(_CONCURRENCY)

    async def read(url: str) -> str | None:
        async with gate:
            fetched = await fetcher.fetch_text(url)
        if not isinstance(fetched, TextFetch) or fetched.title is None:
            return None
        title, _ = mask(fetched.title)
        return title

    titles = await asyncio.gather(*(read(url) for url, _ in pages))
    titled = 0
    async with pool.connection() as conn, conn.transaction():
        for (_, documents), title in zip(pages, titles, strict=True):
            if title is None:
                continue
            await conn.execute(
                "UPDATE intel_documents SET title = %s"
                " WHERE document_id = ANY(%s::uuid[]) AND title = ''",
                (title, documents),
            )
            titled += 1
    log.info("intel.titles_backfilled", pages=len(pages), titled=titled)
    return titled


async def run_once(config: Config) -> None:
    pool = make_async_pool(config.database_url, min_size=1, max_size=1)
    await pool.open()
    try:
        async with httpx.AsyncClient() as client:
            fetcher = HttpTextFetcher(client, config.fetcher_base_url, config.fetcher_token)
            await backfill_titles(pool, fetcher)
    finally:
        await pool.close()


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
