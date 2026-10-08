"""The question runs against real Postgres (spec §4.10, §4.6): a fake fetcher and a fake model,
never the network. This task adds the source proposal; tasks 6 and 9 add validation and the
weight suggestion."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from imageshield.intel.bounds import SUGGESTION_WAIT_MAX_SECONDS, SUGGESTION_WAIT_RETRY_SECONDS
from imageshield.intel.evidence_store import PostgresEvidenceStore
from imageshield.intel.fetch_client import FetchFailure
from imageshield.intel.pipeline import RunResult, run
from imageshield.intel.question_store import PostgresQuestionStore
from imageshield.intel.schemas import (
    DiscoveryCandidate,
    DiscoveryOutput,
    ProposedOptionSources,
    ProposedSource,
    SourceProposalOutput,
    SuggestedOptionWeight,
    SuggestionOutput,
)
from imageshield.intel.source_choice import NewSource
from imageshield.intel.store import PostgresIntelStore
from imageshield.search.urlhash import url_hash
from tests.intel_fakes import (
    NOW,
    POLICY,
    FakeFetcher,
    GatedFetcher,
    claim,
    make_deps,
    make_page,
    make_signal,
    run_once,
    seed_quiz_vocabulary,
    seed_signal,
)
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


# ── stage 3: validation ──────────────────────────────────────────────────────

TERMS = "https://p.example/terms"
SHELL = "https://app.example/terms"
HOPS_TO_HTTP = "https://p.example/moved"
DISALLOWED = "https://p.example/private"
FEED = "https://n.example/feed"
EMPTY_FEED = "https://n.example/empty"
ARTICLE = "https://n.example/article"


def _candidate(
    kind: str, url: str | None = None, query: str | None = None, option: str = "Instagram"
) -> dict[str, Any]:
    return {"option": option, "kind": kind, "source_url": url, "query_text": query}


async def _validate(
    pool: AsyncConnectionPool,
    candidates: list[dict[str, Any]],
    model: FakeQuestionModel,
    fetcher: FakeFetcher,
) -> RunResult:
    await PostgresQuestionStore(pool).queue_source_validation(
        {"candidates": candidates}, operator="ann"
    )
    return await run_once(pool, make_deps(pool, fetcher, model))


async def test_validation_blocks_each_failure_with_its_reason_and_makes_no_model_call(
    intel_pool: AsyncConnectionPool,
) -> None:
    """spec §10: validation blocks a known hit location, a non-https final URL, an app shell under
    the text floor, a robots.txt-disallowed path, a feed with no items, and a search query whose
    search returns no fetchable page, each with its reason, and makes no model call."""
    await _known_hit(intel_pool, ABUSE)
    one_item = [{"title": "t", "link": "https://n.example/1", "published": None}]
    fetcher = FakeFetcher(
        {
            TERMS: make_page(POLICY, TERMS),
            SHELL: make_page("Loading...", SHELL),
            HOPS_TO_HTTP: FetchFailure(code="not_https"),
            FEED: make_page("t", FEED, items=one_item),
            EMPTY_FEED: make_page("t", EMPTY_FEED, items=[]),
        },
        robots_disallowed={DISALLOWED},
    )
    unfetchable = DiscoveryOutput(
        candidates=[DiscoveryCandidate(url="https://gone.example/a", reason="r")]
    )
    model = FakeQuestionModel(searches={"nothing fetchable": unfetchable})
    candidates = [
        _candidate("policy_page", TERMS),
        _candidate("news", ABUSE),
        _candidate("news", HOPS_TO_HTTP),
        _candidate("policy_page", SHELL),
        _candidate("policy_page", DISALLOWED),
        _candidate("feed", FEED),
        _candidate("feed", EMPTY_FEED),
        _candidate("search_query", query="nothing fetchable"),
    ]
    result = await _validate(intel_pool, candidates, model, fetcher)
    assert result.status == "completed"
    assert [(r["status"], r["reason"]) for r in result.outcome["results"]] == [
        ("ready", None),
        ("blocked", "known_hit_location"),
        ("blocked", "not_https"),
        ("blocked", "too_short"),
        ("blocked", "robots_disallowed"),
        ("ready", None),
        ("blocked", "no_items"),
        ("blocked", "no_results"),
    ]
    # One result per candidate, in the submitted order, each echoing its candidate verbatim.
    assert [r["candidate"] for r in result.outcome["results"]] == candidates
    calls = (
        model.extract_calls,
        model.propose_calls,
        model.suggest_calls,
        model.source_proposal_calls,
    )
    assert calls == (0, 0, 0, 0) and model.search_calls == 1  # one search, no other request
    assert ABUSE not in fetcher.fetched  # a known hit location is never fetched
    assert {TERMS, DISALLOWED} <= set(fetcher.robots_checked)


async def test_a_search_query_is_ready_once_one_result_page_passes(
    intel_pool: AsyncConnectionPool,
) -> None:
    fetcher = FakeFetcher({ARTICLE: make_page(POLICY, ARTICLE)})
    found = DiscoveryOutput(
        candidates=[
            DiscoveryCandidate(url="http://n.example/plain", reason="not https"),
            DiscoveryCandidate(url="https://gone.example/a", reason="unfetchable"),
            DiscoveryCandidate(url=ARTICLE, reason="an article"),
        ]
    )
    model = FakeQuestionModel(searches={"Instagram privacy change": found})
    candidate = _candidate("search_query", query="Instagram privacy change")
    result = await _validate(intel_pool, [candidate], model, fetcher)
    assert [(r["status"], r["reason"]) for r in result.outcome["results"]] == [("ready", None)]
    assert fetcher.fetched == ["https://gone.example/a", ARTICLE]  # the http page never


async def test_a_person_shaped_query_is_blocked_without_a_search(
    intel_pool: AsyncConnectionPool,
) -> None:
    model = FakeQuestionModel()
    candidate = _candidate("search_query", query="leaks about jane@example.com")
    result = await _validate(intel_pool, [candidate], model, FakeFetcher({}))
    assert result.outcome["results"][0]["reason"] == "query_names_a_person"
    assert model.search_calls == 0


async def test_a_gate_refusal_blocks_every_later_search_and_urls_are_still_judged(
    intel_pool: AsyncConnectionPool,
) -> None:
    """Review Focus 2: once the gate refuses, no further search is even asked for, and every
    candidate still gets a verdict."""
    async with intel_pool.connection() as conn:
        await conn.execute(
            "UPDATE providers SET enabled = false WHERE provider_id = 'claude_intel'"
        )
    fetcher = FakeFetcher({TERMS: make_page(POLICY, TERMS)})
    model = FakeQuestionModel()
    candidates = [
        _candidate("search_query", query="first query"),
        _candidate("policy_page", TERMS),
        _candidate("search_query", query="second query"),
    ]
    result = await _validate(intel_pool, candidates, model, fetcher)
    assert result.status == "completed"
    assert [r["reason"] for r in result.outcome["results"]] == [
        "provider_disabled",
        None,
        "provider_disabled",
    ]
    assert model.search_calls == 0
    assert (
        await _scalar(
            intel_pool, "SELECT count(*) FROM provider_calls WHERE provider_id = 'claude_intel'"
        )
        == 1  # the gate was asked once, not once per search candidate
    )


async def test_a_fetcher_outage_blocks_the_candidate_as_transient(
    intel_pool: AsyncConnectionPool,
) -> None:
    fetcher = FakeFetcher({TERMS: FetchFailure(code="fetcher_unreachable")})
    result = await _validate(
        intel_pool, [_candidate("policy_page", TERMS)], FakeQuestionModel(), fetcher
    )
    assert result.outcome["results"][0]["reason"] == "fetcher_unavailable"


# ── the weight suggestion ────────────────────────────────────────────────────

SUGGEST_REQUEST: dict[str, Any] = {**QUESTION, "type": "mutable", "cap": 8}
NEW_TERMS = "https://p.example/instagram-terms"
NEW_SAFETY = "https://p.example/instagram-safety"
NEW_HELP = "https://p.example/instagram-help"


async def _rows(pool: AsyncConnectionPool, query: str, *params: Any) -> list[tuple[Any, ...]]:
    async with pool.connection() as conn:
        cur = await conn.execute(query, params or None)
        return list(await cur.fetchall())


async def _done_run(pool: AsyncConnectionPool) -> UUID:
    """A finished run to hang seeded signals on. seed_signal's own adhoc run would be queued,
    and run_once claims the oldest queued run."""
    ((run_id,),) = await _rows(
        pool,
        "INSERT INTO intel_runs (kind, status, requested_by, completed_at)"
        " VALUES ('adhoc_url', 'completed', 'seed', now()) RETURNING run_id",
    )
    return run_id


def _chosen(
    url: str = NEW_TERMS, *, tags: tuple[str, ...] = ("instagram",), option: str = "Instagram"
) -> NewSource:
    return NewSource(
        kind="policy_page",
        source_url=url,
        query_text=None,
        tags=tags,
        check_every_hours=168,
        terms_note="public terms page; automated reads allowed",
        origin="suggested",
        question_key="platforms",
        option=option,
        validation_run_id=None,
        validated_at=None,
    )


def _cite_everything(deduction: int = 4) -> Callable[[dict[str, Any]], SuggestionOutput]:
    """Instagram cites every piece of evidence the run showed the model; Bumble cites none."""

    def answer(payload: dict[str, Any]) -> SuggestionOutput:
        ids = [evidence["signal_id"] for evidence in payload["evidence"]]
        return SuggestionOutput(
            options=[
                SuggestedOptionWeight(
                    option="Instagram",
                    deduction=deduction,
                    rationale="Public by default.",
                    signal_ids=ids,
                    suggested_tags=["instagram"],
                ),
                SuggestedOptionWeight(option="Bumble", rationale="No evidence yet."),
            ]
        )

    return answer


async def test_a_suggestion_reads_its_new_sources_first_then_suggests_and_generates(
    intel_pool: AsyncConnectionPool,
) -> None:
    """spec §10: a weight suggestion checks its newly registered sources inside the run, then
    retrieves, suggests, and runs the ordinary generation over what it read."""
    await seed_quiz_vocabulary(intel_pool)
    done = await _done_run(intel_pool)
    tagged = await seed_signal(intel_pool, run_id=done, tags=("instagram",))
    retracted = await seed_signal(intel_pool, run_id=done, tags=("instagram",))
    evidence_store = PostgresEvidenceStore(intel_pool)
    outcome = await evidence_store.retract_signal(retracted, operator="ann", reason="wrong page")
    assert outcome == "retracted"
    queued = await PostgresQuestionStore(intel_pool).register_and_queue_suggestion(
        SUGGEST_REQUEST, [_chosen()], operator="Ann Operator"
    )
    fetcher = FakeFetcher({NEW_TERMS: make_page(POLICY, NEW_TERMS)})
    model = FakeQuestionModel(make_signal(tags=["instagram"]), suggest_with=_cite_everything())
    result = await run_once(intel_pool, make_deps(intel_pool, fetcher, model))
    assert result.status == "completed", result
    assert fetcher.fetched == [NEW_TERMS] and model.extract_calls == 1
    assert (result.outcome["sources_read"], result.outcome.get("sources_deferred", 0)) == (1, 0)
    (source_id,) = queued.registered
    ((read,),) = await _rows(
        intel_pool,
        "SELECT s.signal_id FROM intel_signals s JOIN intel_documents d"
        " ON d.document_id = s.document_id WHERE d.source_id = %s",
        source_id,
    )
    payload = json.loads(model.suggestion_users[0])
    # The chosen source's evidence first, then by tag; a retracted signal never.
    assert [e["signal_id"] for e in payload["evidence"]] == [str(read), str(tagged)]
    assert payload["question"]["cap"] == 8 and payload["question"]["type"] == "mutable"
    assert "Ann Operator" not in model.suggestion_users[0]
    ((proposal_id, status, target, release_no),) = await _rows(
        intel_pool,
        "SELECT proposal_id, status, target, against_release_no FROM intel_proposals"
        " WHERE kind = 'weight_suggestion' AND run_id = %s",
        queued.run_id,
    )
    assert (status, release_no, target["question_key"]) == ("delivered", 2, "platforms")
    instagram, bumble = target["options"]
    assert (instagram["deduction"], instagram["suggested_tags"]) == (4, ["instagram"])
    assert (bumble["option"], bumble["deduction"], bumble["signal_ids"]) == ("Bumble", None, [])
    links = await _rows(
        intel_pool,
        "SELECT signal_id FROM intel_proposal_signals WHERE proposal_id = %s",
        proposal_id,
    )
    assert {r[0] for r in links} == {read, tagged}
    assert model.propose_calls == 1  # the ordinary generation, over what the run read


async def test_a_newer_suggestion_supersedes_the_older_one_for_its_question_only(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    await seed_signal(intel_pool, run_id=await _done_run(intel_pool), tags=("instagram",))
    store = PostgresQuestionStore(intel_pool)
    model = FakeQuestionModel(suggest_with=_cite_everything())
    run_ids = []
    for question_key in ("platforms", "dating", "platforms"):
        queued = await store.register_and_queue_suggestion(
            {**SUGGEST_REQUEST, "question_key": question_key}, [], operator="ann"
        )
        result = await run_once(intel_pool, make_deps(intel_pool, FakeFetcher({}), model))
        assert result.status == "completed", result
        run_ids.append(queued.run_id)
    rows = await _rows(
        intel_pool,
        "SELECT run_id, status, supersede_reason FROM intel_proposals"
        " WHERE kind = 'weight_suggestion'",
    )
    assert {r[0]: (r[1], r[2]) for r in rows} == {
        run_ids[0]: ("superseded", "newer_proposal"),
        run_ids[1]: ("delivered", None),
        run_ids[2]: ("delivered", None),
    }
    assert model.propose_calls == 0  # nothing was read, so there is nothing to generate over


async def test_sources_past_the_suggestion_cap_are_read_in_their_own_run_first(
    intel_pool: AsyncConnectionPool,
) -> None:
    """spec §4.10: units past INTEL_MAX_CALLS_PER_SUGGESTION_RUN stay unconsumed and the poll says
    how many sources. Since 2026-10-08 the source left is queued as its own read and the
    suggestion waits for it, then answers from both pages."""
    await seed_quiz_vocabulary(intel_pool)
    queued = await PostgresQuestionStore(intel_pool).register_and_queue_suggestion(
        SUGGEST_REQUEST, [_chosen(), _chosen(NEW_SAFETY)], operator="ann"
    )
    fetcher = FakeFetcher({url: make_page(POLICY, url) for url in (NEW_TERMS, NEW_SAFETY)})
    model = FakeQuestionModel(make_signal(tags=["instagram"]), suggest_with=_cite_everything())
    deps = make_deps(intel_pool, fetcher, model, max_calls_per_suggestion_run=1)
    result = await run_once(intel_pool, deps)
    assert result.status == "waiting", result
    assert (result.outcome["sources_read"], result.outcome["sources_deferred"]) == (1, 1)
    assert fetcher.fetched == [NEW_TERMS]
    assert model.suggest_calls == 0 and model.propose_calls == 0  # nothing answered yet
    first, second = queued.registered
    assert await _open_runs(intel_pool, second) == [("source_check", "queued", "schedule")]
    assert await _open_runs(intel_pool, first) == []
    checked = await run_once(intel_pool, deps)  # the second page's own read, not the suggestion
    assert checked.status == "completed" and fetcher.fetched == [NEW_TERMS, NEW_SAFETY]
    resumed = await run_once(intel_pool, deps)
    assert resumed.status == "completed", resumed
    assert model.suggest_calls == 1
    assert resumed.outcome["sources_read"] == 1  # the first pass's counts are carried
    payload = json.loads(model.suggestion_users[0])
    assert len(payload["evidence"]) == 2  # one signal from each page


async def test_a_gate_refusal_while_reading_refuses_the_run_before_any_suggestion(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    await PostgresQuestionStore(intel_pool).register_and_queue_suggestion(
        SUGGEST_REQUEST, [_chosen()], operator="ann"
    )
    async with intel_pool.connection() as conn:
        await conn.execute(
            "UPDATE providers SET enabled = false WHERE provider_id = 'claude_intel'"
        )
    model = FakeQuestionModel(make_signal(tags=["instagram"]), suggest_with=_cite_everything())
    fetcher = FakeFetcher({NEW_TERMS: make_page(POLICY, NEW_TERMS)})
    result = await run_once(intel_pool, make_deps(intel_pool, fetcher, model))
    assert (result.status, result.error_code) == ("refused", "provider_disabled")
    assert result.outcome["refused_by"] == "gate" and result.outcome["sources_deferred"] == 1
    assert (model.extract_calls, model.suggest_calls, model.propose_calls) == (0, 0, 0)
    assert await _scalar(intel_pool, "SELECT count(*) FROM intel_proposals") == 0


async def test_a_suggestion_verdict_fails_the_run_and_generation_still_runs(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    await PostgresQuestionStore(intel_pool).register_and_queue_suggestion(
        SUGGEST_REQUEST, [_chosen()], operator="ann"
    )
    model = FakeQuestionModel(make_signal(tags=["instagram"]), suggestion_outcome="refusal")
    fetcher = FakeFetcher({NEW_TERMS: make_page(POLICY, NEW_TERMS)})
    result = await run_once(intel_pool, make_deps(intel_pool, fetcher, model))
    assert (result.status, result.error_code) == ("failed", "suggestion_refusal")
    written = "SELECT count(*) FROM intel_proposals WHERE kind = 'weight_suggestion'"
    assert await _scalar(intel_pool, written) == 0
    assert model.propose_calls == 1


async def test_a_suggestion_without_a_readable_vocabulary_fails_before_the_call(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool, release_no=3, document={"questions": [{"prompt": "?"}]})
    await PostgresQuestionStore(intel_pool).register_and_queue_suggestion(
        SUGGEST_REQUEST, [], operator="ann"
    )
    model = FakeQuestionModel()
    result = await run_once(intel_pool, make_deps(intel_pool, FakeFetcher({}), model))
    assert (result.status, result.error_code) == ("failed", "vocabulary_missing")
    assert model.suggest_calls == 0


async def test_a_draft_options_source_paused_before_its_first_read_is_still_read(
    intel_pool: AsyncConnectionPool,
) -> None:
    """Review Focus 1: linkedin is registered but mapped only in the draft, so the tick's pause
    pass pauses the new source before the run is claimed. The immediate read never looks at
    `enabled`, and the source stays paused until the draft publishes."""
    await seed_quiz_vocabulary(intel_pool)
    request = {**SUGGEST_REQUEST, "tags": {"Instagram": ["instagram"], "Bumble": ["linkedin"]}}
    queued = await PostgresQuestionStore(intel_pool).register_and_queue_suggestion(
        request, [_chosen(tags=("linkedin",), option="Bumble")], operator="ann"
    )
    (source_id,) = queued.registered
    await PostgresIntelStore(intel_pool).pause_unmapped_sources()  # the tick, before the claim
    fetcher = FakeFetcher({NEW_TERMS: make_page(POLICY, NEW_TERMS)})
    model = FakeQuestionModel(make_signal(tags=["linkedin"]), suggest_with=_cite_everything())
    result = await run_once(intel_pool, make_deps(intel_pool, fetcher, model))
    assert result.status == "completed" and fetcher.fetched == [NEW_TERMS]
    read = "SELECT count(*) FROM intel_documents WHERE source_id = %s"
    assert await _scalar(intel_pool, read, source_id) == 1
    ((enabled, reason),) = await _rows(
        intel_pool,
        "SELECT enabled, disabled_reason FROM intel_sources WHERE source_id = %s",
        source_id,
    )
    assert (enabled, reason) == (False, "unmapped")


async def test_a_retried_press_reads_the_named_sources_a_refused_one_left_unread(
    intel_pool: AsyncConnectionPool,
) -> None:
    """Final review M6. The first press registered three sources and was refused by the gate:
    the first one's check stopped before its page was read (deferred_provider_disabled) and the
    others were never checked. The second press reuses all three -- none is new -- yet the two
    with no evidence are read. The one an operator disabled meanwhile is not."""
    await seed_quiz_vocabulary(intel_pool)
    store = PostgresQuestionStore(intel_pool)
    chosen = [_chosen(), _chosen(NEW_SAFETY), _chosen(NEW_HELP)]
    first = await store.register_and_queue_suggestion(SUGGEST_REQUEST, chosen, operator="ann")
    terms, safety, help_page = first.registered
    async with intel_pool.connection() as conn:
        await conn.execute(
            "UPDATE providers SET enabled = false WHERE provider_id = 'claude_intel'"
        )
    pages = {url: make_page(POLICY, url) for url in (NEW_TERMS, NEW_SAFETY, NEW_HELP)}
    model = FakeQuestionModel(make_signal(tags=["instagram"]), suggest_with=_cite_everything())
    refused = await run_once(intel_pool, make_deps(intel_pool, FakeFetcher(pages), model))
    assert refused.status == "refused" and model.extract_calls == 0
    status = "SELECT last_run_status FROM intel_sources WHERE source_id = %s"
    assert await _scalar(intel_pool, status, terms) == "deferred_provider_disabled"
    async with intel_pool.connection() as conn:
        await conn.execute(
            "UPDATE providers SET enabled = true WHERE provider_id = 'claude_intel'"
        )
        await conn.execute(
            "UPDATE intel_sources SET enabled = false WHERE source_id = %s", (help_page,)
        )
    second = await store.register_and_queue_suggestion(SUGGEST_REQUEST, chosen, operator="ann")
    assert second.registered == () and set(second.reused) == {terms, safety, help_page}
    fetcher = FakeFetcher(pages)
    result = await run_once(intel_pool, make_deps(intel_pool, fetcher, model))
    assert result.status == "completed", result
    assert fetcher.fetched == [NEW_TERMS, NEW_SAFETY] and result.outcome["sources_read"] == 2
    assert await _scalar(intel_pool, status, terms) == "checked"
    assert await _scalar(intel_pool, status, help_page) is None


# ── reading side by side (spec 2026-10-03-intel-throughput §5) ───────────────────────────────

PAGES = [f"https://p.example/instagram-page-{n}" for n in range(6)]


async def _suggest_in_background(
    pool: AsyncConnectionPool, deps: Any, urls: list[str]
) -> asyncio.Task[RunResult]:
    await seed_quiz_vocabulary(pool)
    await PostgresQuestionStore(pool).register_and_queue_suggestion(
        SUGGEST_REQUEST, [_chosen(url) for url in urls], operator="ann"
    )
    return asyncio.create_task(run_once(pool, deps))


async def test_a_suggestion_reads_its_sources_several_at_once_within_the_bound(
    intel_pool: AsyncConnectionPool,
) -> None:
    fetcher = GatedFetcher({url: make_page(POLICY, url) for url in PAGES})
    model = FakeQuestionModel(make_signal(tags=["instagram"]), suggest_with=_cite_everything())
    deps = make_deps(intel_pool, fetcher, model, source_read_concurrency=4)
    running = await _suggest_in_background(intel_pool, deps, PAGES)
    await fetcher.until_waiting(4)
    await asyncio.sleep(0.3)
    assert fetcher.waiting == 4  # the fifth and sixth wait for a free lane
    fetcher.release.set()
    result = await asyncio.wait_for(running, 30)
    assert result.status == "completed", result
    assert fetcher.peak == 4 and sorted(fetcher.fetched) == sorted(PAGES)
    assert (result.outcome["sources_read"], result.outcome.get("sources_deferred", 0)) == (6, 0)
    assert model.extract_calls == 6 and model.suggest_calls == 1


async def test_the_suggestion_cap_is_exact_when_sources_are_read_at_once(
    intel_pool: AsyncConnectionPool,
) -> None:
    """Four lanes, slow extractions, a cap of three: three calls, side by side, never a fourth;
    the three sources left wait for their own check and are made due for it."""
    fetcher = FakeFetcher({url: make_page(POLICY, url) for url in PAGES})
    model = FakeQuestionModel(
        make_signal(tags=["instagram"]), suggest_with=_cite_everything(), extract_delay=0.3
    )
    deps = make_deps(
        intel_pool, fetcher, model, max_calls_per_suggestion_run=3, source_read_concurrency=4
    )
    running = await _suggest_in_background(intel_pool, deps, PAGES)
    result = await asyncio.wait_for(running, 30)
    assert result.status == "waiting", result
    assert model.extract_calls == 3 and model.extract_peak >= 2
    assert (result.outcome["sources_read"], result.outcome["sources_deferred"]) == (3, 3)
    reads = "SELECT count(*) FROM intel_runs WHERE kind = 'source_check' AND status = 'queued'"
    assert await _scalar(intel_pool, reads) == 3  # each left page is read in its own run
    assert model.suggest_calls == 0


async def test_one_sources_outage_never_cancels_the_reads_beside_it(
    intel_pool: AsyncConnectionPool,
) -> None:
    """The fetcher fails for one source while two others are mid-read: those two are read, the
    failed one is queued as its own read, and the suggestion waits for it."""
    urls = PAGES[:3]
    pages: dict[str, Any] = {url: make_page(POLICY, url) for url in urls}
    pages[urls[1]] = FetchFailure(code="fetcher_unreachable")
    fetcher = GatedFetcher(pages)
    model = FakeQuestionModel(make_signal(tags=["instagram"]), suggest_with=_cite_everything())
    deps = make_deps(intel_pool, fetcher, model, source_read_concurrency=4)
    running = await _suggest_in_background(intel_pool, deps, urls)
    await fetcher.until_waiting(3)
    fetcher.release.set()
    result = await asyncio.wait_for(running, 30)
    assert result.status == "waiting", result
    assert (result.outcome["sources_read"], result.outcome["sources_deferred"]) == (2, 1)
    assert result.outcome["stopped_fetcher_unreachable"] == 1
    assert model.extract_calls == 2 and model.suggest_calls == 0


async def test_validation_judges_candidates_at_once_and_keeps_their_order(
    intel_pool: AsyncConnectionPool,
) -> None:
    pages: dict[str, Any] = {url: make_page(POLICY, url) for url in PAGES[:4]}
    pages[PAGES[4]] = make_page("Loading...", PAGES[4])
    fetcher = GatedFetcher(pages)
    candidates = [_candidate("policy_page", url) for url in PAGES[:5]]
    await PostgresQuestionStore(intel_pool).queue_source_validation(
        {"candidates": candidates}, operator="ann"
    )
    deps = make_deps(intel_pool, fetcher, FakeQuestionModel(), source_read_concurrency=4)
    running = asyncio.create_task(run_once(intel_pool, deps))
    await fetcher.until_waiting(4)
    await asyncio.sleep(0.3)
    assert fetcher.waiting == 4
    fetcher.release.set()
    result = await asyncio.wait_for(running, 30)
    assert result.status == "completed" and fetcher.peak == 4
    assert [r["candidate"] for r in result.outcome["results"]] == candidates
    assert [r["reason"] for r in result.outcome["results"]] == [None, None, None, None, "too_short"]


# ── a saved search is read in its own run (spec 2026-10-03-intel-throughput §6) ───────────────

SAVED_SEARCH = "Instagram AI training news"


def _chosen_search(
    query: str = SAVED_SEARCH, *, tags: tuple[str, ...] = ("instagram",), option: str = "Instagram"
) -> NewSource:
    return NewSource(
        kind="search_query",
        source_url=None,
        query_text=query,
        tags=tags,
        check_every_hours=168,
        terms_note=None,
        origin="suggested",
        question_key="platforms",
        option=option,
        validation_run_id=None,
        validated_at=None,
    )


async def _open_runs(pool: AsyncConnectionPool, source_id: UUID) -> list[tuple[Any, ...]]:
    return await _rows(
        pool,
        "SELECT kind, status, requested_by FROM intel_runs"
        " WHERE source_id = %s AND status IN ('queued', 'running')",
        source_id,
    )


def _found_article() -> DiscoveryOutput:
    return DiscoveryOutput(candidates=[DiscoveryCandidate(url=ARTICLE, reason="r")])


async def test_a_suggestion_hands_its_saved_searches_to_their_own_runs(
    intel_pool: AsyncConnectionPool,
) -> None:
    """The page is read inside the run; the saved search is not read there at all. It is queued
    as its own discovery run, counted as deferred, and (since 2026-10-08) the suggestion waits
    for it and answers with what it found -- no second press needed."""
    await seed_quiz_vocabulary(intel_pool)
    store = PostgresQuestionStore(intel_pool)
    chosen = [_chosen(), _chosen_search()]
    queued = await store.register_and_queue_suggestion(SUGGEST_REQUEST, chosen, operator="ann")
    page_id, search_id = queued.registered
    fetcher = FakeFetcher(
        {NEW_TERMS: make_page(POLICY, NEW_TERMS), ARTICLE: make_page(POLICY, ARTICLE)}
    )
    model = FakeQuestionModel(
        make_signal(tags=["instagram"]),
        discovery=_found_article(),
        suggest_with=_cite_everything(),
    )
    deps = make_deps(intel_pool, fetcher, model, source_read_concurrency=4)
    result = await run_once(intel_pool, deps)
    assert result.status == "waiting", result
    assert model.discover_calls == 0 and fetcher.fetched == [NEW_TERMS]  # no search inside
    assert result.outcome["sources_read"] == 1
    assert result.outcome["sources_deferred"] == 1
    assert result.outcome["search_sources_deferred"] == 1
    assert model.suggest_calls == 0  # never written before the search is read
    assert await _open_runs(intel_pool, search_id) == [("discovery", "queued", "schedule")]
    assert await _open_runs(intel_pool, page_id) == []
    ((status, attempts, awaiting),) = await _rows(
        intel_pool,
        "SELECT status, attempts, awaiting_source_ids FROM intel_runs WHERE run_id = %s",
        queued.run_id,
    )
    assert (status, attempts, awaiting) == ("queued", 0, [search_id])  # no attempt used up
    # The search's own run, claimed by the next free slot (the waiting suggestion is not
    # claimable while it is open): one search, its page read.
    searched = await run_once(intel_pool, deps)
    assert searched.status == "completed" and model.discover_calls == 1
    ((found,),) = await _rows(
        intel_pool,
        "SELECT s.signal_id FROM intel_signals s JOIN intel_documents d"
        " ON d.document_id = s.document_id WHERE d.source_id = %s",
        search_id,
    )
    # Now the suggestion resumes: it reads nothing itself and answers with what the search found.
    resumed = await run_once(intel_pool, deps)
    assert resumed.status == "completed", resumed
    assert model.suggest_calls == 1 and model.discover_calls == 1 and model.extract_calls == 2
    payload = json.loads(model.suggestion_users[-1])
    assert str(found) in {e["signal_id"] for e in payload["evidence"]}


async def test_a_draft_options_saved_search_is_still_read_in_its_own_run(
    intel_pool: AsyncConnectionPool,
) -> None:
    """A draft option's search is paused as unmapped, so the scheduler would never queue it
    before the draft publishes. The suggestion queues its read anyway, as it used to read it
    inline, and the source stays paused."""
    await seed_quiz_vocabulary(intel_pool)
    request = {**SUGGEST_REQUEST, "tags": {"Instagram": ["instagram"], "Bumble": ["linkedin"]}}
    queued = await PostgresQuestionStore(intel_pool).register_and_queue_suggestion(
        request, [_chosen_search(tags=("linkedin",), option="Bumble")], operator="ann"
    )
    (search_id,) = queued.registered
    await PostgresIntelStore(intel_pool).pause_unmapped_sources()  # the tick, before the claim
    fetcher = FakeFetcher({ARTICLE: make_page(POLICY, ARTICLE)})
    model = FakeQuestionModel(
        make_signal(tags=["linkedin"]), discovery=_found_article(), suggest_with=_cite_everything()
    )
    deps = make_deps(intel_pool, fetcher, model)
    result = await run_once(intel_pool, deps)
    assert result.status == "waiting" and model.discover_calls == 0
    assert await _open_runs(intel_pool, search_id) == [("discovery", "queued", "schedule")]
    searched = await run_once(intel_pool, deps)
    assert searched.status == "completed" and model.discover_calls == 1
    ((enabled, reason),) = await _rows(
        intel_pool,
        "SELECT enabled, disabled_reason FROM intel_sources WHERE source_id = %s",
        search_id,
    )
    assert (enabled, reason) == (False, "unmapped")


async def test_a_saved_search_with_a_read_already_open_gets_no_second(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    queued = await PostgresQuestionStore(intel_pool).register_and_queue_suggestion(
        SUGGEST_REQUEST, [_chosen_search()], operator="ann"
    )
    (search_id,) = queued.registered
    already = await PostgresIntelStore(intel_pool).queue_source_check(search_id, operator="ann")
    model = FakeQuestionModel(suggest_with=_cite_everything())
    result = await run_once(intel_pool, make_deps(intel_pool, FakeFetcher({}), model))
    assert result.status == "waiting" and model.discover_calls == 0
    assert result.wait is not None and result.wait.awaiting == (search_id,)
    assert result.outcome["sources_deferred"] == 1
    assert result.outcome["search_sources_deferred"] == 1
    ((run_id,),) = await _rows(
        intel_pool,
        "SELECT run_id FROM intel_runs WHERE source_id = %s AND status IN ('queued', 'running')",
        search_id,
    )
    assert run_id == already


# ── a suggestion waits for its evidence (spec 2026-10-08-intel-suggestion-waits-for-evidence) ──


async def _run_at(pool: AsyncConnectionPool, deps: Any, when: datetime) -> RunResult | None:
    """run_once with the claim and the run's clock at ``when``; None when nothing is claimable."""
    store = PostgresIntelStore(pool)
    claimed = await store.claim_next(when, lease_seconds=900)
    if claimed is None:
        return None
    deps.clock = lambda: when
    result = await run(claimed, deps)
    if result.status == "waiting":
        assert result.wait is not None
        await store.wait_run(
            claimed.run_id,
            attempts=claimed.attempts,
            outcome=dict(result.outcome),
            awaiting=result.wait.awaiting,
            deadline=result.wait.deadline,
            not_before=result.wait.not_before,
        )
    else:
        await store.finish_run(
            claimed.run_id,
            status=result.status,
            outcome=dict(result.outcome),
            error_code=result.error_code,
        )
    return result


async def test_a_suggestion_with_no_evidence_at_all_answers_nothing(
    intel_pool: AsyncConnectionPool,
) -> None:
    """§2.5: an empty pool is never answered. No call, nothing written, and the question's
    previous suggestion stays delivered."""
    await seed_quiz_vocabulary(intel_pool)
    signal = await seed_signal(intel_pool, run_id=await _done_run(intel_pool), tags=("instagram",))
    store = PostgresQuestionStore(intel_pool)
    model = FakeQuestionModel(suggest_with=_cite_everything())
    deps = make_deps(intel_pool, FakeFetcher({}), model)
    await store.register_and_queue_suggestion(SUGGEST_REQUEST, [], operator="ann")
    first = await run_once(intel_pool, deps)
    assert first.status == "completed" and model.suggest_calls == 1
    await PostgresEvidenceStore(intel_pool).retract_signal(signal, operator="ann", reason="wrong")
    await store.register_and_queue_suggestion(SUGGEST_REQUEST, [], operator="ann")
    second = await run_once(intel_pool, deps)
    assert (second.status, second.error_code) == ("failed", "no_evidence_found")
    assert second.outcome["suggestion_no_evidence"] == 1
    assert second.outcome["suggestion_evidence"] == 0
    assert model.suggest_calls == 1  # never asked
    rows = await _rows(
        intel_pool, "SELECT status FROM intel_proposals WHERE kind = 'weight_suggestion'"
    )
    assert rows == [("delivered",)]


async def test_an_awaited_read_an_outage_left_unread_is_queued_again_then_answered(
    intel_pool: AsyncConnectionPool,
) -> None:
    """§2.4: the second page's read fails (the fetcher is down), so coming back the suggestion
    queues it again and waits, no sooner than the retry time; once it is read, it answers."""
    await seed_quiz_vocabulary(intel_pool)
    queued = await PostgresQuestionStore(intel_pool).register_and_queue_suggestion(
        SUGGEST_REQUEST, [_chosen(), _chosen(NEW_SAFETY)], operator="ann"
    )
    _, safety = queued.registered
    down: dict[str, Any] = {
        NEW_TERMS: make_page(POLICY, NEW_TERMS),
        NEW_SAFETY: FetchFailure(code="fetcher_unreachable"),
    }
    model = FakeQuestionModel(make_signal(tags=["instagram"]), suggest_with=_cite_everything())
    deps = make_deps(intel_pool, FakeFetcher(down), model)
    assert (await run_once(intel_pool, deps)).status == "waiting"
    failed = await run_once(intel_pool, deps)  # the page's own read, still down
    assert failed.status == "failed"
    # A fetcher-side failure says nothing about the page, so the source records no check at all:
    # it is still unread.
    unread = "SELECT last_checked_at IS NULL FROM intel_sources WHERE source_id = %s"
    assert await _scalar(intel_pool, unread, safety) is True
    back = await run_once(intel_pool, deps)  # the suggestion: queues the read again, waits
    assert back.status == "waiting" and back.wait is not None
    assert back.wait.not_before == NOW + timedelta(seconds=SUGGESTION_WAIT_RETRY_SECONDS)
    assert back.outcome["suggestion_reads_requeued"] == 1 and model.suggest_calls == 0
    up = make_deps(
        intel_pool,
        FakeFetcher({url: make_page(POLICY, url) for url in (NEW_TERMS, NEW_SAFETY)}),
        model,
    )
    read = await run_once(intel_pool, up)  # the read queued again; the page answers now
    assert read.status == "completed"
    assert await _run_at(intel_pool, up, NOW) is None  # not before its retry time
    later = NOW + timedelta(seconds=SUGGESTION_WAIT_RETRY_SECONDS + 1)
    answered = await _run_at(intel_pool, up, later)
    assert answered is not None and answered.status == "completed", answered
    assert model.suggest_calls == 1
    assert len(json.loads(model.suggestion_users[0])["evidence"]) == 2


async def test_past_its_deadline_a_suggestion_answers_from_what_it_holds(
    intel_pool: AsyncConnectionPool,
) -> None:
    """§2.4: a saved search that is never read does not hold the suggestion forever."""
    await seed_quiz_vocabulary(intel_pool)
    await seed_signal(intel_pool, run_id=await _done_run(intel_pool), tags=("instagram",))
    queued = await PostgresQuestionStore(intel_pool).register_and_queue_suggestion(
        SUGGEST_REQUEST, [_chosen_search()], operator="ann"
    )
    (search_id,) = queued.registered
    model = FakeQuestionModel(suggest_with=_cite_everything())
    deps = make_deps(intel_pool, FakeFetcher({}), model)
    assert (await run_once(intel_pool, deps)).status == "waiting"
    async with intel_pool.connection() as conn:  # its read is stuck: claimed, never finished
        await conn.execute(
            "UPDATE intel_runs SET status = 'running', lease_expires_at = %s WHERE source_id = %s",
            (NOW + timedelta(days=1), search_id),
        )
    assert await _run_at(intel_pool, deps, NOW + timedelta(minutes=10)) is None
    late = NOW + timedelta(seconds=SUGGESTION_WAIT_MAX_SECONDS + 1)
    answered = await _run_at(intel_pool, deps, late)
    assert answered is not None and answered.status == "completed", answered
    assert answered.outcome["suggestion_wait_timed_out"] == 1
    assert answered.outcome["suggestion_sources_unread"] == 1
    assert model.suggest_calls == 1


async def test_a_later_press_is_never_replaced_by_an_earlier_one_that_waited(
    intel_pool: AsyncConnectionPool,
) -> None:
    """§2.7: the first press waits for its saved search; a second press for the same question
    answers meanwhile; when the first finally answers, it is born superseded."""
    await seed_quiz_vocabulary(intel_pool)
    await seed_signal(intel_pool, run_id=await _done_run(intel_pool), tags=("instagram",))
    store = PostgresQuestionStore(intel_pool)
    model = FakeQuestionModel(discovery=_found_article(), suggest_with=_cite_everything())
    fetcher = FakeFetcher({ARTICLE: make_page(POLICY, ARTICLE)})
    deps = make_deps(intel_pool, fetcher, model)
    earlier = await store.register_and_queue_suggestion(
        SUGGEST_REQUEST, [_chosen_search()], operator="ann"
    )
    assert (await run_once(intel_pool, deps)).status == "waiting"
    search_run = await claim(intel_pool)  # the search's read, in flight
    later = await store.register_and_queue_suggestion(SUGGEST_REQUEST, [], operator="bob")
    assert (await run_once(intel_pool, deps)).status == "completed"  # the later press
    searched = await run(search_run, deps)
    await PostgresIntelStore(intel_pool).finish_run(
        search_run.run_id, status=searched.status, outcome=dict(searched.outcome)
    )
    resumed = await run_once(intel_pool, deps)
    assert resumed.status == "completed", resumed
    rows = await _rows(
        intel_pool,
        "SELECT run_id, status, supersede_reason FROM intel_proposals"
        " WHERE kind = 'weight_suggestion'",
    )
    assert {r[0]: (r[1], r[2]) for r in rows} == {
        later.run_id: ("delivered", None),
        earlier.run_id: ("superseded", "newer_proposal"),
    }


# ── sources-v2 (spec 2026-10-08-intel-source-proposal-v2) ─────────────────────────────────────


def _searches(option: str, count: int) -> ProposedOptionSources:
    return ProposedOptionSources(
        option=option,
        candidates=[
            ProposedSource(kind="search_query", query_text=f"{option} deepfake {n}", reason="r")
            for n in range(count)
        ]
        + [ProposedSource(kind="research", source_url=f"https://r.example/{option}", reason="r")],
    )


async def test_a_source_proposal_keeps_at_most_two_saved_searches_per_option(
    intel_pool: AsyncConnectionPool,
) -> None:
    """§2: a saved search is a multi-minute research call on every check; pages come first."""
    await seed_quiz_vocabulary(intel_pool)
    await PostgresQuestionStore(intel_pool).queue_source_proposal(QUESTION, operator="ann")
    sources = SourceProposalOutput(options=[_searches("Instagram", 4), _searches("Bumble", 1)])
    model = FakeQuestionModel(sources=sources)
    result = await run_once(intel_pool, make_deps(intel_pool, FakeFetcher({}), model))
    assert result.status == "completed", result
    instagram, bumble = result.outcome["options"]
    kinds = [c["kind"] for c in instagram["proposed"]]
    assert kinds == ["search_query", "search_query", "research"]
    assert [c["kind"] for c in bumble["proposed"]] == ["search_query", "research"]
    assert result.outcome["candidate_dropped_search_over_cap"] == 2
    assert result.outcome["prompt_version"] == "sources-v2"


async def test_a_source_proposal_with_nothing_for_any_answer_fails(
    intel_pool: AsyncConnectionPool,
) -> None:
    """§2: v1 'completed' a gender question with no search made and no source anywhere."""
    await seed_quiz_vocabulary(intel_pool)
    await PostgresQuestionStore(intel_pool).queue_source_proposal(QUESTION, operator="ann")
    model = FakeQuestionModel(sources=SourceProposalOutput())
    result = await run_once(intel_pool, make_deps(intel_pool, FakeFetcher({}), model))
    assert (result.status, result.error_code) == ("failed", "no_sources_found")
    assert result.outcome["source_proposal_empty"] == 1
    assert [o["proposed"] for o in result.outcome["options"]] == [[], []]


async def test_existing_sources_alone_still_complete_a_source_proposal(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    existing = await _registered(intel_pool, "https://p.example/instagram-terms", ("instagram",))
    await PostgresQuestionStore(intel_pool).queue_source_proposal(QUESTION, operator="ann")
    model = FakeQuestionModel(sources=SourceProposalOutput())
    result = await run_once(intel_pool, make_deps(intel_pool, FakeFetcher({}), model))
    assert result.status == "completed", result
    assert result.outcome["options"][0]["existing"] == [str(existing)]
