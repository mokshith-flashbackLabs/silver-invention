"""The daily news watch against real Postgres (spec 2026-10-09-intel-news-watch-design §2.2, §3)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from imageshield.intel.news_watch import NEWS_WATCH_CHECK_EVERY_HOURS, watched_tags, watches
from imageshield.intel.prompts import discovery_request
from imageshield.intel.store import PostgresIntelStore
from tests.db import run_migrate
from tests.intel_fakes import quiz_document, scoring, seed_quiz_vocabulary
from tests.test_intel_schema import _steps_through


@pytest.fixture
def migrated_db(throwaway_db: str) -> str:
    assert run_migrate(throwaway_db, "down", "--all").returncode == 0
    up = run_migrate(throwaway_db, "up")
    assert up.returncode == 0, up.stderr
    return throwaway_db


async def _rows(pool: AsyncConnectionPool, query: str, *params: Any) -> list[tuple[Any, ...]]:
    async with pool.connection() as conn:
        cur = await conn.execute(query, params or None)
        return list(await cur.fetchall())


_WATCHES = (
    "SELECT kind, query_text, tags, check_every_hours, enabled, disabled_reason, created_by,"
    " next_check_at <= now() FROM intel_sources WHERE origin = 'news_watch' ORDER BY tags[1]"
)


def test_only_a_mapped_unretired_platform_tag_is_watched() -> None:
    """instagram is a mapped platform; linkedin a platform no option maps; myspace retired; a
    mapped practice tag is not a platform."""
    document = quiz_document()
    document["tags"].append(
        {
            "slug": "scraping",
            "label": "Scraping",
            "description": "d",
            "kind": "practice",
            "retired": False,
        }
    )
    document["option_tags"].append(
        {"question_key": "platforms", "option": "Threads", "tags": ["scraping"]}
    )
    vocabulary = scoring(document)
    assert watched_tags(vocabulary) == ["instagram"]
    (watch,) = watches(vocabulary)
    assert watch.query_text.startswith("Instagram ")
    assert "deepfake" in watch.query_text and "impersonation" in watch.query_text


async def test_a_mapped_platform_gets_one_daily_watch_and_a_second_pass_adds_none(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    store = PostgresIntelStore(intel_pool)
    created = await store.ensure_news_watches()
    assert len(created) == 1
    ((kind, query, tags, every, enabled, reason, by, due),) = await _rows(intel_pool, _WATCHES)
    assert (kind, tags, every, enabled, reason) == (
        "search_query",
        ["instagram"],
        NEWS_WATCH_CHECK_EVERY_HOURS,
        True,
        None,
    )
    assert by == "system:news_watch" and query.startswith("Instagram ") and due is True
    assert await store.ensure_news_watches() == ()
    assert len(await _rows(intel_pool, _WATCHES)) == 1
    audit = await _rows(
        intel_pool, "SELECT metadata FROM audit_log WHERE action = 'intel.news_watch_created'"
    )
    assert [a[0]["tags"] for a in audit] == [["instagram"]]
    # It is an ordinary due source: the schedule queues its discovery run.
    queued = await store.schedule_due(datetime.now(UTC))
    assert len(queued) == 1
    ((run_kind, source_tags),) = await _rows(
        intel_pool,
        "SELECT r.kind, s.tags FROM intel_runs r JOIN intel_sources s USING (source_id)"
        " WHERE r.run_id = %s",
        queued[0],
    )
    assert (run_kind, source_tags) == ("discovery", ["instagram"])


async def test_an_operator_disabled_watch_is_never_recreated_and_unmapping_pauses_it(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    store = PostgresIntelStore(intel_pool)
    (watch_id,) = await store.ensure_news_watches()
    async with intel_pool.connection() as conn:
        await conn.execute(
            "UPDATE intel_sources SET enabled = false WHERE source_id = %s", (watch_id,)
        )
    assert await store.ensure_news_watches() == ()
    ((enabled,),) = await _rows(
        intel_pool, "SELECT enabled FROM intel_sources WHERE source_id = %s", watch_id
    )
    assert enabled is False  # the operator's choice stands
    async with intel_pool.connection() as conn:
        await conn.execute(
            "UPDATE intel_sources SET enabled = true WHERE source_id = %s", (watch_id,)
        )
    # The quiz stops mapping instagram: the existing pause follows it, and re-mapping resumes it.
    await seed_quiz_vocabulary(intel_pool, release_no=3, document=quiz_document(option_tags=[]))
    paused = await store.pause_unmapped_sources()
    assert watch_id in paused.paused
    await seed_quiz_vocabulary(intel_pool, release_no=4)
    resumed = await store.pause_unmapped_sources()
    assert watch_id in resumed.resumed
    assert await store.ensure_news_watches() == ()


async def test_no_vocabulary_creates_no_watch(intel_pool: AsyncConnectionPool) -> None:
    async with intel_pool.connection() as conn:
        await conn.execute("DELETE FROM intel_vocabulary")
    assert await PostgresIntelStore(intel_pool).ensure_news_watches() == ()


def test_a_saved_search_is_asked_for_recent_news_with_todays_date() -> None:
    system, user = discovery_request(
        "Instagram deepfake", registry_tags=[], today="2026-10-09", recent_days=90
    )
    payload = json.loads(user)
    assert payload == {"query": "Instagram deepfake", "today": "2026-10-09", "recent_days": 90}
    flat = " ".join(system.split())
    assert "within recent_days days of today" in flat
    assert "put the current month and year in the searches you run" in flat
    assert "not more than five years old" in flat


def test_0052_is_reversible_and_keeps_a_created_watch(migrated_db: str) -> None:
    insert = (
        "INSERT INTO intel_sources (kind, query_text, tags, check_every_hours, created_by, origin)"
        " VALUES ('search_query', 'X deepfake', ARRAY['x'], 24, 'system:news_watch', 'news_watch')"
    )
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        conn.execute(insert)
        try:
            conn.execute(insert)
        except psycopg.errors.UniqueViolation:
            pass
        else:
            raise AssertionError("a second watch for one tag was accepted")
        try:
            conn.execute(
                "INSERT INTO intel_sources (kind, query_text, tags, check_every_hours, created_by,"
                " origin) VALUES ('search_query', 'q', ARRAY['x','y'], 24, 's', 'news_watch')"
            )
        except psycopg.errors.CheckViolation:
            pass
        else:
            raise AssertionError("a watch naming two tags was accepted")
    down = run_migrate(migrated_db, "down", "--steps", _steps_through("0052_"))
    assert down.returncode == 0, down.stderr
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        origins = conn.execute("SELECT origin FROM intel_sources WHERE query_text = 'X deepfake'")
        assert origins.fetchall() == [("operator",)]
    assert run_migrate(migrated_db, "up").returncode == 0
