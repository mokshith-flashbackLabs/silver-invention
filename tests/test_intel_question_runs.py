"""The question runs against real Postgres (spec §4.10, §4.6): a fake fetcher and a fake model,
never the network. This task adds the source proposal; tasks 6 and 9 add validation and the
weight suggestion."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from imageshield.intel.evidence_store import PostgresEvidenceStore
from imageshield.intel.fetch_client import FetchFailure
from imageshield.intel.pipeline import RunResult
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
    """A suggestion with no evidence at all is still delivered (Review Focus 5)."""
    await seed_quiz_vocabulary(intel_pool)
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
    assert await _scalar(intel_pool, "SELECT count(*) FROM intel_proposal_signals") == 0
    assert model.propose_calls == 0  # nothing was read, so there is nothing to generate over


async def test_sources_past_the_suggestion_cap_are_deferred_and_made_due(
    intel_pool: AsyncConnectionPool,
) -> None:
    """spec §4.10: units past INTEL_MAX_CALLS_PER_SUGGESTION_RUN stay unconsumed, the poll says
    how many sources, and the suggestion proceeds with what was read."""
    await seed_quiz_vocabulary(intel_pool)
    queued = await PostgresQuestionStore(intel_pool).register_and_queue_suggestion(
        SUGGEST_REQUEST, [_chosen(), _chosen(NEW_SAFETY)], operator="ann"
    )
    fetcher = FakeFetcher({url: make_page(POLICY, url) for url in (NEW_TERMS, NEW_SAFETY)})
    model = FakeQuestionModel(make_signal(tags=["instagram"]), suggest_with=_cite_everything())
    deps = make_deps(intel_pool, fetcher, model, max_calls_per_suggestion_run=1)
    result = await run_once(intel_pool, deps)
    assert result.status == "completed", result
    assert (result.outcome["sources_read"], result.outcome["sources_deferred"]) == (1, 1)
    assert fetcher.fetched == [NEW_TERMS]
    assert model.suggest_calls == 1 and model.propose_calls == 1  # outside the reading cap
    first, second = queued.registered
    due = "SELECT next_check_at <= %s FROM intel_sources WHERE source_id = %s"
    assert await _scalar(intel_pool, due, NOW, second) is True  # its first check comes next tick
    assert await _scalar(intel_pool, due, NOW, first) is False


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
