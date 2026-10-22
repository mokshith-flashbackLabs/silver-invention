"""The one-off title backfill (2026-10-22): documents read before the fetcher reported a page's
title get one, through the same fetcher, so the evidence people see names its document."""

from __future__ import annotations

from typing import Any

from psycopg_pool import AsyncConnectionPool

from imageshield.intel.backfill_titles import backfill_titles
from imageshield.intel.fetch_client import FetchFailure
from tests.intel_fakes import (
    FakeFetcher,
    make_page,
    seed_quiz_vocabulary,
    seed_signal,
    seed_threat_proposal,
)

CITED = "https://a.example/story"
UNREADABLE = "https://b.example/story"
UNCITED = "https://c.example/story"


async def _titles(pool: AsyncConnectionPool) -> dict[str, Any]:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT final_url, title FROM intel_documents")
        return {row[0]: row[1] for row in await cur.fetchall()}


async def test_only_cited_untitled_documents_are_fetched_and_titled_masked_once(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    cited = await seed_signal(intel_pool, tags=("instagram",), publisher="a.example", url=CITED)
    unreadable = await seed_signal(
        intel_pool, tags=("instagram",), publisher="b.example", url=UNREADABLE
    )
    await seed_signal(intel_pool, tags=("instagram",), publisher="c.example", url=UNCITED)
    await seed_threat_proposal(intel_pool, signal_ids=[cited, unreadable])
    fetcher = FakeFetcher(
        {
            CITED: make_page("text", CITED, title="A story, ask press@example.com"),
            UNREADABLE: FetchFailure(code="unfetchable"),
        }
    )

    assert await backfill_titles(intel_pool, fetcher) == 1
    titles = await _titles(intel_pool)
    assert titles[CITED] == "A story, ask «masked»"
    assert titles[UNREADABLE] == "" and titles[UNCITED] == ""
    assert sorted(fetcher.fetched) == [CITED, UNREADABLE]

    again = FakeFetcher({})
    assert await backfill_titles(intel_pool, again) == 0
    assert again.fetched == [UNREADABLE]
