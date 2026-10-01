"""The question runs against real Postgres (spec §4.10, §4.6): a fake fetcher and a fake model,
never the network. This task adds the source proposal; tasks 6 and 9 add validation and the
weight suggestion."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from imageshield.intel.question_store import PostgresQuestionStore
from imageshield.intel.schemas import ProposedOptionSources, ProposedSource, SourceProposalOutput
from imageshield.intel.store import PostgresIntelStore
from imageshield.search.urlhash import url_hash
from tests.intel_fakes import FakeFetcher, make_deps, run_once, seed_quiz_vocabulary
from tests.question_fakes import QUESTION, FakeQuestionModel

ABUSE = "https://abuse.example/x"


async def _scalar(pool: AsyncConnectionPool, query: str, *params: Any) -> Any:
    async with pool.connection() as conn:
        cur = await conn.execute(query, params or None)
        row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _known_hit(pool: AsyncConnectionPool, url: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO content_urls (url_hash, url, source_domain) VALUES (%s, %s, %s)",
            (url_hash(url), url, "abuse.example"),
        )


async def _registered(
    pool: AsyncConnectionPool, url: str, tags: tuple[str, ...], kind: str = "policy_page"
) -> UUID:
    source = await PostgresIntelStore(pool).create_source(
        kind=kind,
        source_url=url,
        query_text=None,
        tags=tags,
        check_every_hours=24,
        terms_note="automated access permitted",
        operator="alice",
    )
    return source.source_id


PROPOSED = SourceProposalOutput(
    options=[
        ProposedOptionSources(
            option="Instagram",
            candidates=[
                ProposedSource(
                    kind="policy_page",
                    source_url="https://p.example/instagram-terms",
                    reason="terms",
                ),
                ProposedSource(kind="news", source_url=ABUSE, reason="a known hit location"),
                ProposedSource(
                    kind="search_query",
                    query_text="Instagram leak call +44 20 7946 0958",
                    reason="names a phone",
                ),
                ProposedSource(
                    kind="search_query", query_text="Instagram privacy change", reason="news"
                ),
            ],
        ),
        ProposedOptionSources(
            option="Bumble",
            candidates=[
                ProposedSource(
                    kind="policy_page",
                    source_url="https://bumble.example/terms?utm_source=x",
                    reason="terms",
                )
            ],
        ),
    ]
)


# ── stage 1: the source proposal ──────────────────────────────────────────────


async def test_a_source_proposal_lists_existing_sources_first_and_registers_nothing(
    intel_pool: AsyncConnectionPool,
) -> None:
    """spec §10: a source proposal lists existing sources whose tags intersect an option's first,
    drops a known hit location and a PII-shaped query, and registers nothing."""
    await seed_quiz_vocabulary(intel_pool)
    existing = await _registered(intel_pool, "https://p.example/instagram-terms", ("instagram",))
    await _registered(intel_pool, "https://p.example/unrelated", ("linkedin",))
    await _known_hit(intel_pool, ABUSE)
    await PostgresQuestionStore(intel_pool).queue_source_proposal(QUESTION, operator="Ann Operator")
    model = FakeQuestionModel(sources=PROPOSED)
    result = await run_once(intel_pool, make_deps(intel_pool, FakeFetcher({}), model))
    assert result.status == "completed", result
    instagram, bumble = result.outcome["options"]
    assert (instagram["option"], instagram["tags"]) == ("Instagram", ["instagram"])
    assert instagram["existing"] == [str(existing)]
    assert instagram["proposed"] == [
        {
            "kind": "search_query",
            "source_url": None,
            "query_text": "Instagram privacy change",
            "reason": "news",
        }
    ]
    assert (bumble["existing"], bumble["tags"]) == ([], [])
    assert [c["source_url"] for c in bumble["proposed"]] == ["https://bumble.example/terms"]
    assert result.outcome["candidate_dropped_duplicate"] == 1  # the existing source's own URL
    assert result.outcome["candidate_dropped_known_hit_location"] == 1
    assert result.outcome["candidate_dropped_query_names_a_person"] == 1
    assert await _scalar(intel_pool, "SELECT count(*) FROM intel_sources") == 2
    assert model.source_proposal_calls == 1 and model.extract_calls == 0
    payload = json.loads(model.source_proposal_users[0])
    assert payload["question"]["options"][0] == {"option": "Instagram", "tags": ["instagram"]}
    assert "Ann Operator" not in model.source_proposal_users[0]  # never sent to the model


async def test_a_refused_source_proposal_still_lists_the_existing_sources(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    existing = await _registered(intel_pool, "https://p.example/instagram-terms", ("instagram",))
    await PostgresQuestionStore(intel_pool).queue_source_proposal(QUESTION, operator="ann")
    model = FakeQuestionModel(sources_outcome="refusal")
    result = await run_once(intel_pool, make_deps(intel_pool, FakeFetcher({}), model))
    assert (result.status, result.error_code) == ("failed", "source_proposal_refusal")
    first = result.outcome["options"][0]
    assert first["existing"] == [str(existing)] and first["proposed"] == []


async def test_a_gate_refusal_refuses_the_source_proposal(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    await PostgresQuestionStore(intel_pool).queue_source_proposal(QUESTION, operator="ann")
    async with intel_pool.connection() as conn:
        await conn.execute(
            "UPDATE providers SET enabled = false WHERE provider_id = 'claude_intel'"
        )
    model = FakeQuestionModel(sources=PROPOSED)
    result = await run_once(intel_pool, make_deps(intel_pool, FakeFetcher({}), model))
    assert (result.status, result.error_code) == ("refused", "provider_disabled")
    assert result.outcome["refused_by"] == "gate" and model.source_proposal_calls == 0


async def test_queueing_a_source_proposal_is_audited_with_the_operator(
    intel_pool: AsyncConnectionPool,
) -> None:
    run_id = await PostgresQuestionStore(intel_pool).queue_source_proposal(QUESTION, operator="ann")
    assert (
        await _scalar(
            intel_pool,
            "SELECT metadata->>'operator' FROM audit_log"
            " WHERE action = 'intel.source_proposal_queued' AND resource_id = %s",
            run_id,
        )
        == "ann"
    )
    assert (
        await _scalar(intel_pool, "SELECT requested_by FROM intel_runs WHERE run_id = %s", run_id)
        == "ann"
    )
