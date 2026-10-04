"""Protection renewal end to end (spec §4.8, §10): the worker queues one check for a due credit,
fetches its cited pages again through the fetcher, re-verifies every excerpt and writes a pending
renewal an operator must approve. No model call, ever."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from imageshield.intel.bounds import RENEWAL_RETRY_HOURS
from imageshield.intel.decisions import PostgresDecisionStore
from imageshield.intel.fetch_client import FetchFailure
from imageshield.intel.models import Run
from imageshield.intel.proposal_store import PostgresProposalStore
from imageshield.intel.protection_store import PostgresProtectionStore
from imageshield.intel.store import PostgresIntelStore
from imageshield.intel.worker import tick
from imageshield.search.urlhash import url_hash
from tests.intel_fakes import (
    NOW,
    QUOTE,
    FakeFetcher,
    FakeModel,
    make_deps,
    make_page,
    run_once,
    seed_due_credit,
    seed_quiz_vocabulary,
)

URL = "https://newsroom.example.com/opt-out"


async def _scalar(pool: AsyncConnectionPool, query: str, *params: Any) -> Any:
    async with pool.connection() as conn:
        cur = await conn.execute(query, params)
        row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _renewal_runs(pool: AsyncConnectionPool) -> list[Run]:
    runs = await PostgresIntelStore(pool).list_runs(cursor=None, limit=50)
    return [r for r in runs if r.kind == "renewal_check"]


async def _pending_renewals(pool: AsyncConnectionPool) -> list[dict[str, Any]]:
    rows = await PostgresProposalStore(pool, threat_recency_days=90).list_proposals(
        statuses=["pending"], kinds=["protection_event"], cursor=None, limit=10
    )
    return [r for r in rows if "renews_event_id" in r["target"]]


async def test_a_due_credit_whose_excerpts_still_verify_gets_a_renewal_and_no_model_call(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    event, original = await seed_due_credit(intel_pool, url=URL)
    model = FakeModel()
    fetcher = FakeFetcher({URL: make_page("Updated intro. " + QUOTE, URL)})
    deps = make_deps(intel_pool, fetcher, model)
    assert await tick(deps, lease_seconds=900) is True
    (run,) = await _renewal_runs(intel_pool)
    assert run.status == "completed" and run.request == {"event_id": str(event)}
    assert run.outcome["renewal_proposed"] == 1 and run.outcome["renewal_excerpts_verified"] == 1
    assert (model.extract_calls, model.discover_calls, model.propose_calls) == (0, 0, 0)
    assert fetcher.fetched == [URL]
    assert await _scalar(intel_pool, "SELECT count(*) FROM provider_calls") == 0
    (renewal,) = await _pending_renewals(intel_pool)
    assert renewal["target"]["renews_event_id"] == str(event)
    assert renewal["signal_ids"] != [original] and renewal["approvable"] is True
    assert await tick(deps, lease_seconds=900) is False  # a renewal is pending: nothing to queue


async def test_a_renewal_whose_excerpts_no_longer_verify_writes_nothing_and_the_credit_lapses(
    intel_pool: AsyncConnectionPool,
) -> None:
    """spec §10."""
    await seed_quiz_vocabulary(intel_pool)
    event, _ = await seed_due_credit(intel_pool, url=URL)
    changed = make_page("The opt-out was withdrawn earlier this year. " * 5, URL)
    deps = make_deps(intel_pool, FakeFetcher({URL: changed}), FakeModel())
    assert await tick(deps, lease_seconds=900) is True
    (run,) = await _renewal_runs(intel_pool)
    assert run.outcome["renewal_evidence_gone"] == 1
    assert run.outcome["renewal_excerpt_dropped_not_a_substring"] == 1
    assert await _pending_renewals(intel_pool) == []
    assert (
        await _scalar(
            intel_pool, "SELECT count(*) FROM intel_documents WHERE run_id = %s", run.run_id
        )
        == 0
    )
    assert await tick(deps, lease_seconds=900) is False  # conclusive: never checked again
    rows = await PostgresProtectionStore(intel_pool).list_events(
        statuses=None, cursor=None, limit=10
    )
    (row,) = [r for r in rows if r["event_id"] == event]
    assert row["renewal"]["result"] == "evidence_gone" and row["state"] == "live"


async def test_an_unreachable_page_is_retried_the_next_day_not_lapsed(
    intel_pool: AsyncConnectionPool,
) -> None:
    """Review Focus 2, end to end."""
    await seed_quiz_vocabulary(intel_pool)
    await seed_due_credit(intel_pool, url=URL)
    fetcher = FakeFetcher({})  # every page unfetchable
    clock = [NOW]
    deps = make_deps(intel_pool, fetcher, FakeModel(), clock=lambda: clock[0])
    assert await tick(deps, lease_seconds=900) is True
    (run,) = await _renewal_runs(intel_pool)
    assert run.outcome["renewal_evidence_unreachable"] == 1
    assert run.outcome["renewal_excerpt_dropped_fetch_unfetchable"] == 1
    assert await tick(deps, lease_seconds=900) is False  # under a day: not yet
    clock[0] = NOW + timedelta(hours=RENEWAL_RETRY_HOURS + 1)
    fetcher.pages[URL] = make_page(QUOTE, URL)
    assert await tick(deps, lease_seconds=900) is True
    assert len(await _pending_renewals(intel_pool)) == 1


async def test_a_fetcher_outage_fails_the_check_and_it_is_queued_again(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    await seed_due_credit(intel_pool, url=URL)
    clock = [NOW]
    fetcher = FakeFetcher({URL: FetchFailure(code="fetcher_unreachable")})
    deps = make_deps(intel_pool, fetcher, FakeModel(), clock=lambda: clock[0])
    assert await tick(deps, lease_seconds=900) is True
    (run,) = await _renewal_runs(intel_pool)
    assert (run.status, run.error_code) == ("failed", "fetcher_unreachable")
    assert await tick(deps, lease_seconds=900) is False
    clock[0] = NOW + timedelta(hours=RENEWAL_RETRY_HOURS + 1)
    assert await tick(deps, lease_seconds=900) is True
    assert len(await _renewal_runs(intel_pool)) == 2


async def test_a_cited_page_that_became_a_known_hit_location_is_never_fetched(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    await seed_due_credit(intel_pool, url=URL)
    async with intel_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO content_urls (url_hash, url, source_domain) VALUES (%s, %s, %s)",
            (url_hash(URL), URL, "abuse.example"),
        )
    fetcher = FakeFetcher({URL: make_page(QUOTE, URL)})
    assert await tick(make_deps(intel_pool, fetcher, FakeModel()), lease_seconds=900) is True
    (run,) = await _renewal_runs(intel_pool)
    assert fetcher.fetched == [] and run.outcome["known_hit_location"] == 1
    assert run.outcome["renewal_evidence_gone"] == 1


async def test_an_approved_renewal_continues_the_credit_from_its_review_date(
    intel_pool: AsyncConnectionPool,
) -> None:
    """spec §10: an approved renewal starts exactly at the old review_by."""
    await seed_quiz_vocabulary(intel_pool)
    event, _ = await seed_due_credit(intel_pool, url=URL)
    deps = make_deps(intel_pool, FakeFetcher({URL: make_page(QUOTE, URL)}), FakeModel())
    await tick(deps, lease_seconds=900)
    (renewal,) = await _pending_renewals(intel_pool)
    decided = await PostgresDecisionStore(intel_pool, threat_recency_days=90).decide(
        renewal["proposal_id"],
        decision="approved",
        values=None,
        reason="re-checked the sources",
        operator="ann",
        applies_regardless_of_location=True,
    )
    assert decided.applied_ref is not None
    new = UUID(decided.applied_ref)
    old_review_by = await _scalar(
        intel_pool, "SELECT review_by FROM protection_events WHERE event_id = %s", event
    )
    assert (
        await _scalar(
            intel_pool, "SELECT starts_at FROM protection_events WHERE event_id = %s", new
        )
        == old_review_by
    )
    rows = {
        r["event_id"]: r
        for r in await PostgresProtectionStore(intel_pool).list_events(
            statuses=None, cursor=None, limit=10
        )
    }
    assert (rows[event]["state"], rows[event]["renewal_due"]) == ("live", False)
    assert rows[new]["state"] == "scheduled" and rows[new]["renews_event_id"] == event


async def test_a_credit_retracted_after_its_check_was_queued_is_not_renewed(
    intel_pool: AsyncConnectionPool,
) -> None:
    event, _ = await seed_due_credit(intel_pool, url=URL)
    store = PostgresProtectionStore(intel_pool)
    await store.schedule_renewals(NOW)
    await store.retract(event, operator="ann", reason="withdrawn")
    fetcher = FakeFetcher({URL: make_page(QUOTE, URL)})
    result = await run_once(intel_pool, make_deps(intel_pool, fetcher, FakeModel()))
    assert result.status == "completed" and result.outcome["renewal_not_due"] == 1
    assert fetcher.fetched == []


async def test_a_reclaimed_renewal_writes_once(intel_pool: AsyncConnectionPool) -> None:
    await seed_quiz_vocabulary(intel_pool)
    await seed_due_credit(intel_pool, url=URL)
    deps = make_deps(intel_pool, FakeFetcher({URL: make_page(QUOTE, URL)}), FakeModel())
    await tick(deps, lease_seconds=900)
    (run,) = await _renewal_runs(intel_pool)
    async with intel_pool.connection() as conn:  # a worker died after its commit
        await conn.execute(
            "UPDATE intel_runs SET status = 'running', completed_at = NULL,"
            " lease_expires_at = %s WHERE run_id = %s",
            (NOW - timedelta(minutes=1), run.run_id),
        )
    assert await tick(deps, lease_seconds=900) is True
    (again,) = await _renewal_runs(intel_pool)
    assert again.status == "completed" and again.outcome["proposals_already_written"] == 1
    assert len(await _pending_renewals(intel_pool)) == 1


async def test_an_unreadable_renewal_request_fails_the_run(
    intel_pool: AsyncConnectionPool,
) -> None:
    async with intel_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO intel_runs (kind, request, requested_by)"
            " VALUES ('renewal_check', '{}', 'schedule')"
        )
    result = await run_once(intel_pool, make_deps(intel_pool, FakeFetcher({}), FakeModel()))
    assert (result.status, result.error_code) == ("failed", "request_unreadable")
