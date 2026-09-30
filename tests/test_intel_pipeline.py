"""The intel pipeline end to end (task 10, spec §4.3 and §10): the real Postgres
stores, a fake fetcher and a fake model -- never the network.

Most assertions here are about what did NOT happen: no fetch of a known hit
location, no second model call for a consumed unit, no document row for a refused
page, no phone number in a stored column. The fakes and the ``intel_pool`` fixture
are shared with the worker tests (``tests/intel_fakes.py``, ``conftest.py``).
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from psycopg_pool import AsyncConnectionPool

from imageshield.intel.evidence_store import PostgresEvidenceStore
from imageshield.intel.fetch_client import FetchFailure, HttpTextFetcher, TextFetch
from imageshield.intel.model import ModelUnavailable
from imageshield.intel.pii import contains_pii
from imageshield.intel.pipeline import changed_hunks, run
from imageshield.intel.schemas import (
    DiscoveryCandidate,
    DiscoveryOutput,
    ExtractedSignal,
    ExtractionOutput,
)
from imageshield.intel.store import PostgresIntelStore
from imageshield.search.urlhash import url_hash
from tests.intel_fakes import (
    NOW,
    POLICY,
    QUOTE,
    FakeFetcher,
    FakeModel,
    claim,
    make_deps,
    make_page,
    make_signal,
    run_once,
)

TERMS = "https://p.example/terms"
ARTICLE = "https://n.example/a"
FEED = "https://n.example/feed"
ABUSE = "https://abuse.example/x"
PHONE_QUOTE = "Call +44 20 7946 0958 to opt out of AI training on your photos."


async def _source(
    pool: AsyncConnectionPool,
    *,
    kind: str = "policy_page",
    url: str | None = TERMS,
    query: str | None = None,
    queue: bool = True,
) -> UUID:
    store = PostgresIntelStore(pool)
    source = await store.create_source(
        kind=kind,
        source_url=url,
        query_text=query,
        tags=("instagram",),
        check_every_hours=24,
        terms_note="automated access permitted",
        operator="a",
    )
    if queue:
        await store.queue_source_check(source.source_id, operator="a")
    return source.source_id


async def _queue(pool: AsyncConnectionPool, source_id: UUID) -> None:
    assert await PostgresIntelStore(pool).queue_source_check(source_id, operator="a") is not None


async def _known_hit(pool: AsyncConnectionPool, url: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO content_urls (url_hash, url, source_domain) VALUES (%s, %s, %s)",
            (url_hash(url), url, "abuse.example"),
        )


async def _scalar(pool: AsyncConnectionPool, query: str, *params: Any) -> Any:
    async with pool.connection() as conn:
        cur = await conn.execute(query, params)
        row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _documents(pool: AsyncConnectionPool) -> int:
    return int(await _scalar(pool, "SELECT count(*) FROM intel_documents"))


async def _signals(pool: AsyncConnectionPool) -> int:
    return int(await _scalar(pool, "SELECT count(*) FROM intel_signals"))


def _feed_items(links: list[str], *, days_old: int = 1) -> list[dict[str, Any]]:
    published = (NOW - timedelta(days=days_old)).isoformat()
    return [
        {"title": f"t{i}", "link": link, "published": published} for i, link in enumerate(links)
    ]


# ── the fetcher client (no database) ─────────────────────────────────────────


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_the_http_fetcher_relays_text_and_sends_its_token() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["token"] = request.headers["X-Fetcher-Token"]
        seen["body"] = json.loads(request.content)
        body = {
            "text": "t",
            "content_type": "text/plain",
            "final_url": ARTICLE,
            "truncated": False,
            "items": None,
        }
        return httpx.Response(200, json=body)

    async with _client(handler) as client:
        got = await HttpTextFetcher(client, "http://fetcher.local/", "tok").fetch_text(ARTICLE)
    assert isinstance(got, TextFetch) and got.final_url == ARTICLE
    assert seen == {"path": "/v1/text", "token": "tok", "body": {"url": ARTICLE}}


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (400, {"error": {"code": "not_https", "message": "m"}}, "not_https"),
        (502, {"error": {"code": "unfetchable", "message": "m"}}, "unfetchable"),
        (400, {"error": {"code": "unsupported_type", "message": "m"}}, "unsupported_type"),
        # Ours, never the page's: a bad token, a crash, a malformed 200.
        (401, {"error": {"code": "unauthorised", "message": "m"}}, "fetcher_error"),
        (500, "boom", "fetcher_error"),
        (200, {"text": "only"}, "fetcher_error"),
    ],
)
async def test_the_http_fetcher_separates_upstream_verdicts_from_its_own_failures(
    status: int, body: Any, code: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    async with _client(handler) as client:
        got = await HttpTextFetcher(client, "http://fetcher.local", "tok").fetch_text(ARTICLE)
    assert got == FetchFailure(code=code)


async def test_an_unreachable_fetcher_is_a_failure_not_a_crash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    async with _client(handler) as client:
        got = await HttpTextFetcher(client, "http://fetcher.local", "tok").fetch_text(ARTICLE)
    assert got == FetchFailure(code="fetcher_unreachable")


# ── the policy diff (pure) ───────────────────────────────────────────────────


def test_the_policy_diff_is_changed_sentences_plus_context() -> None:
    before = " ".join(f"Clause {i} stays the same." for i in range(50))
    after = before.replace("Clause 25 stays the same.", "Clause 25 now allows AI training.")
    hunk = changed_hunks(before, after, max_chars=10_000)
    assert "Clause 25 now allows AI training." in hunk
    assert "Clause 23 stays the same." in hunk and "Clause 27 stays the same." in hunk
    assert "Clause 10 stays" not in hunk and "Clause 40 stays" not in hunk
    # A pure deletion still sends where the text went.
    removed = before.replace(" Clause 25 stays the same.", "")
    hunk = changed_hunks(before, removed, max_chars=10_000)
    assert "Clause 24 stays the same." in hunk and "Clause 26 stays the same." in hunk
    assert "Clause 10 stays" not in hunk
    assert len(changed_hunks(before, after, max_chars=20)) == 20


# ── source_check: policy pages ───────────────────────────────────────────────


async def test_policy_page_first_check_extracts_verifies_and_masks(
    intel_pool: AsyncConnectionPool,
) -> None:
    source_id = await _source(intel_pool)
    fetcher = FakeFetcher({TERMS: make_page(POLICY, TERMS)})
    result = await run(
        await claim(intel_pool), make_deps(intel_pool, fetcher, FakeModel(make_signal()))
    )
    assert result.status == "completed" and result.error_code is None
    assert result.outcome["signals_kept"] == 1 and result.outcome["documents_recorded"] == 1
    assert result.outcome["tag_dropped_unknown_tag"] == 1
    # model_calls: extraction + the run's one generation call (step 2)
    assert result.outcome["pii_masked_summary"] == 1 and result.outcome["model_calls"] == 2
    evidence = PostgresEvidenceStore(intel_pool)
    (signal,) = await evidence.list_signals(cursor=None, limit=5)
    assert signal["tags"] == ["instagram"] and "press@example.com" not in signal["summary"]
    detail = await evidence.get_signal(signal["signal_id"])
    assert detail is not None
    (excerpt,) = detail["excerpts"]
    assert POLICY[excerpt["char_start"] : excerpt["char_end"]] == excerpt["quote_text"] == QUOTE
    assert detail["document"]["trust"] == "listed" and detail["document"]["source_id"] == source_id
    assert await evidence.snapshot_for(source_id) is not None


async def test_an_unchanged_page_makes_no_model_call(intel_pool: AsyncConnectionPool) -> None:
    source_id = await _source(intel_pool, queue=False)
    fetcher = FakeFetcher({TERMS: make_page(POLICY, TERMS)})
    model = FakeModel(make_signal())
    results = []
    for _ in range(2):
        await _queue(intel_pool, source_id)
        results.append(await run_once(intel_pool, make_deps(intel_pool, fetcher, model)))
    assert model.extract_calls == 1
    assert results[1].outcome["unchanged"] == 1 and results[1].outcome["model_calls"] == 0


async def test_a_changed_policy_page_sends_only_the_changed_sentences(
    intel_pool: AsyncConnectionPool,
) -> None:
    before = " ".join(f"Clause {i} of these terms stays exactly as it was." for i in range(40))
    changed = "Clause 30 now lets us train AI models on your public photos by default."
    after = before.replace("Clause 30 of these terms stays exactly as it was.", changed)
    source_id = await _source(intel_pool, queue=False)
    model = FakeModel(ExtractionOutput(signals=[]))
    await _queue(intel_pool, source_id)
    await run_once(
        intel_pool, make_deps(intel_pool, FakeFetcher({TERMS: make_page(before, TERMS)}), model)
    )
    model.extraction = make_signal(changed)
    await _queue(intel_pool, source_id)
    result = await run_once(
        intel_pool, make_deps(intel_pool, FakeFetcher({TERMS: make_page(after, TERMS)}), model)
    )
    sent = json.loads(model.users[1])["document"]
    assert changed in sent and "Clause 5 of these terms" not in sent
    # The model read a diff; the quote was verified against the whole fetched text.
    assert result.outcome["policy_diff"] == 1 and result.outcome["signals_kept"] == 1


async def test_a_short_policy_page_disables_itself_without_a_model_call(
    intel_pool: AsyncConnectionPool,
) -> None:
    source_id = await _source(intel_pool)
    model = FakeModel(make_signal())
    fetcher = FakeFetcher({TERMS: make_page("Loading... enable JavaScript.", TERMS)})
    result = await run(await claim(intel_pool), make_deps(intel_pool, fetcher, model))
    assert result.outcome["disabled_too_short"] == 1 and model.extract_calls == 0
    source = await PostgresIntelStore(intel_pool).get_source(source_id)
    assert source is not None and not source.enabled and source.disabled_reason == "too_short"
    audits = await _scalar(
        intel_pool,
        "SELECT count(*) FROM audit_log"
        " WHERE action = 'intel.source_disabled' AND resource_id = %s",
        source_id,
    )
    assert audits == 1 and await _documents(intel_pool) == 0


async def test_a_source_fetch_failure_counts_against_it_but_a_fetcher_outage_does_not(
    intel_pool: AsyncConnectionPool,
) -> None:
    source_id = await _source(intel_pool, queue=False)
    store = PostgresIntelStore(intel_pool)
    await _queue(intel_pool, source_id)
    down = FakeFetcher({TERMS: FetchFailure(code="unfetchable")})
    result = await run_once(intel_pool, make_deps(intel_pool, down, FakeModel()))
    assert result.status == "completed" and result.outcome["fetch_unfetchable"] == 1
    source = await store.get_source(source_id)
    assert source is not None and source.consecutive_failures == 1
    await _queue(intel_pool, source_id)
    ours = FakeFetcher({TERMS: FetchFailure(code="fetcher_unreachable")})
    result = await run_once(intel_pool, make_deps(intel_pool, ours, FakeModel()))
    assert result.status == "failed" and result.error_code == "fetcher_unreachable"
    source = await store.get_source(source_id)
    assert source is not None and source.consecutive_failures == 1


# ── verification ─────────────────────────────────────────────────────────────


async def test_a_paraphrase_is_dropped_and_the_unit_still_consumed(
    intel_pool: AsyncConnectionPool,
) -> None:
    await PostgresIntelStore(intel_pool).queue_adhoc(ARTICLE, operator="a")
    fetcher = FakeFetcher({ARTICLE: make_page(POLICY, ARTICLE)})
    model = FakeModel(make_signal("Instagram trains AI on your photos by default now"))
    result = await run(await claim(intel_pool), make_deps(intel_pool, fetcher, model))
    assert result.status == "completed" and result.outcome["quote_dropped_not_a_substring"] == 1
    assert result.outcome["signal_dropped_no_quote"] == 1 and result.outcome["signals_kept"] == 0
    assert await _documents(intel_pool) == 1 and await _signals(intel_pool) == 0


async def test_a_lying_charset_is_dropped_not_crashing(intel_pool: AsyncConnectionPool) -> None:
    sentence = "Photos from the café terrace may be used to train our models."
    garbled = POLICY + " " + sentence.replace("é", "�")
    assert "�" in garbled and sentence not in garbled
    await PostgresIntelStore(intel_pool).queue_adhoc(ARTICLE, operator="a")
    fetcher = FakeFetcher({ARTICLE: make_page(garbled, ARTICLE)})
    model = FakeModel(make_signal(sentence))  # the model "fixed" the character
    result = await run(await claim(intel_pool), make_deps(intel_pool, fetcher, model))
    assert result.status == "completed" and result.outcome["quote_dropped_not_a_substring"] == 1
    assert result.outcome["signals_kept"] == 0


async def test_a_quote_under_twenty_characters_is_dropped(intel_pool: AsyncConnectionPool) -> None:
    await PostgresIntelStore(intel_pool).queue_adhoc(ARTICLE, operator="a")
    fetcher = FakeFetcher({ARTICLE: make_page(POLICY, ARTICLE)})
    model = FakeModel(make_signal("Instagram Terms."))
    result = await run(await claim(intel_pool), make_deps(intel_pool, fetcher, model))
    assert result.outcome["quote_dropped_too_short"] == 1 and result.outcome["signals_kept"] == 0


async def test_a_phone_bearing_quote_never_reaches_a_stored_column(
    intel_pool: AsyncConnectionPool,
) -> None:
    await PostgresIntelStore(intel_pool).queue_adhoc(ARTICLE, operator="a")
    fetcher = FakeFetcher({ARTICLE: make_page(POLICY + " " + PHONE_QUOTE, ARTICLE)})
    extraction = ExtractionOutput(
        signals=[
            ExtractedSignal(
                category="policy",
                direction="risk_up",
                tags=["instagram"],
                unregistered_subjects=["helpline +44 20 7946 0958"],
                summary="Opt out by phoning +44 20 7946 0958.",
                quotes=[PHONE_QUOTE, QUOTE],
            )
        ]
    )
    model = FakeModel(extraction)
    result = await run(await claim(intel_pool), make_deps(intel_pool, fetcher, model))
    assert result.outcome["quote_dropped_pii_in_excerpt"] == 1
    assert result.outcome["signals_kept"] == 1 and result.outcome["pii_masked_summary"] == 1
    assert "7946" not in model.users[0]  # the model read masked text
    async with intel_pool.connection() as conn:
        calls = await (
            await conn.execute("SELECT raw_response::text, error_detail FROM provider_calls")
        ).fetchall()
        stored = await (
            await conn.execute(
                "SELECT s.summary, array_to_string(s.unregistered_subjects, ' '), e.quote_text"
                " FROM intel_signals s JOIN intel_excerpts e USING (signal_id)"
            )
        ).fetchall()
    assert calls and stored
    for raw, detail in calls:
        assert not contains_pii(raw) and not contains_pii(detail or "")
    for row in stored:
        assert not any(contains_pii(value) for value in row)


async def test_model_tags_are_deduplicated_and_a_masked_summary_stays_in_bounds(
    intel_pool: AsyncConnectionPool,
) -> None:
    await PostgresIntelStore(intel_pool).queue_adhoc(ARTICLE, operator="a")
    summary = "a@b.io " * 70  # 490 characters; masking lengthens it past the 500 CHECK
    fetcher = FakeFetcher({ARTICLE: make_page(POLICY, ARTICLE)})
    model = FakeModel(make_signal(tags=["instagram", "instagram", "myspace"], summary=summary))
    result = await run(await claim(intel_pool), make_deps(intel_pool, fetcher, model))
    assert result.status == "completed" and result.outcome["signals_kept"] == 1
    assert result.outcome["tag_dropped_duplicate"] == 1
    assert result.outcome["tag_dropped_retired_tag"] == 1
    assert result.outcome["summary_truncated"] == 1
    (signal,) = await PostgresEvidenceStore(intel_pool).list_signals(cursor=None, limit=5)
    assert signal["tags"] == ["instagram"] and len(signal["summary"]) <= 500


async def test_a_long_document_is_read_only_to_the_char_limit(
    intel_pool: AsyncConnectionPool,
) -> None:
    await PostgresIntelStore(intel_pool).queue_adhoc(ARTICLE, operator="a")
    text = QUOTE + " " + "More filler text follows here. " * 200
    fetcher = FakeFetcher({ARTICLE: make_page(text, ARTICLE)})
    model = FakeModel(make_signal())
    deps = make_deps(intel_pool, fetcher, model, max_document_chars=100)
    result = await run(await claim(intel_pool), deps)
    assert result.outcome["document_truncated"] == 1 and result.outcome["signals_kept"] == 1
    assert len(json.loads(model.users[0])["document"]) == 100
    assert await _scalar(intel_pool, "SELECT truncated FROM intel_documents") is True


# ── known hit locations (§6.1) ───────────────────────────────────────────────


async def test_a_pasted_url_redirecting_onto_a_known_hit_is_refused(
    intel_pool: AsyncConnectionPool,
) -> None:
    await _known_hit(intel_pool, ABUSE)
    await PostgresIntelStore(intel_pool).queue_adhoc(ARTICLE, operator="a")
    fetcher = FakeFetcher({ARTICLE: make_page(POLICY, ARTICLE, final=ABUSE)})
    model = FakeModel(make_signal())
    result = await run(await claim(intel_pool), make_deps(intel_pool, fetcher, model))
    assert result.outcome["known_hit_location"] == 1 and model.extract_calls == 0
    assert await _documents(intel_pool) == 0


async def test_a_listed_source_redirecting_onto_a_known_hit_is_refused_on_final_url(
    intel_pool: AsyncConnectionPool,
) -> None:
    await _known_hit(intel_pool, ABUSE)
    source_id = await _source(intel_pool)
    fetcher = FakeFetcher({TERMS: make_page(POLICY, TERMS, final=ABUSE)})
    model = FakeModel(make_signal())
    result = await run(await claim(intel_pool), make_deps(intel_pool, fetcher, model))
    assert result.outcome["known_hit_location"] == 1 and model.extract_calls == 0
    assert await _documents(intel_pool) == 0
    assert await PostgresEvidenceStore(intel_pool).snapshot_for(source_id) is None
    source = await PostgresIntelStore(intel_pool).get_source(source_id)
    assert source is not None and source.last_run_status == "known_hit_location"


async def test_a_listed_source_that_is_a_known_hit_is_never_fetched(
    intel_pool: AsyncConnectionPool,
) -> None:
    await _known_hit(intel_pool, TERMS)
    await _source(intel_pool)
    fetcher = FakeFetcher({TERMS: make_page(POLICY, TERMS)})
    result = await run(await claim(intel_pool), make_deps(intel_pool, fetcher, FakeModel()))
    assert result.outcome["known_hit_location"] == 1 and fetcher.fetched == []


async def test_a_known_hit_feed_item_is_never_fetched_or_read(
    intel_pool: AsyncConnectionPool,
) -> None:
    await _known_hit(intel_pool, ABUSE)
    await _source(intel_pool, kind="feed", url=FEED)
    pages: dict[str, Any] = {
        FEED: make_page("feed", FEED, items=_feed_items([ABUSE, ARTICLE])),
        ABUSE: make_page(POLICY, ABUSE),
        ARTICLE: make_page(POLICY, ARTICLE),
    }
    fetcher, model = FakeFetcher(pages), FakeModel(make_signal())
    result = await run(await claim(intel_pool), make_deps(intel_pool, fetcher, model))
    assert result.outcome["known_hit_location"] == 1 and fetcher.fetched == [FEED, ARTICLE]
    assert model.extract_calls == 1 and await _documents(intel_pool) == 1


async def test_a_known_hit_discovery_result_is_never_fetched_or_read(
    intel_pool: AsyncConnectionPool,
) -> None:
    await _known_hit(intel_pool, ABUSE)
    await _source(intel_pool, kind="search_query", url=None, query="platform AI training")
    discovery = DiscoveryOutput(
        candidates=[
            DiscoveryCandidate(url=ABUSE, reason="r"),
            DiscoveryCandidate(url=ARTICLE, reason="r"),
        ]
    )
    fetcher = FakeFetcher({ABUSE: make_page(POLICY, ABUSE), ARTICLE: make_page(POLICY, ARTICLE)})
    model = FakeModel(make_signal(), discovery=discovery)
    result = await run(await claim(intel_pool), make_deps(intel_pool, fetcher, model))
    assert result.outcome["known_hit_location"] == 1 and fetcher.fetched == [ARTICLE]
    assert model.extract_calls == 1 and await _documents(intel_pool) == 1


# ── consumption: verdicts consume, transients do not ─────────────────────────


@pytest.mark.parametrize("outcome", ["refusal", "max_tokens", "unparseable"])
async def test_neutral_model_outcomes_consume_the_unit_and_leave_the_breaker_alone(
    intel_pool: AsyncConnectionPool, outcome: str
) -> None:
    source_id = await _source(intel_pool, queue=False)
    fetcher = FakeFetcher({TERMS: make_page(POLICY, TERMS)})
    model = FakeModel(outcome=outcome)
    await _queue(intel_pool, source_id)
    first = await run_once(intel_pool, make_deps(intel_pool, fetcher, model))
    assert first.status == "completed" and first.outcome[f"model_{outcome}"] == 1
    await _queue(intel_pool, source_id)
    second = await run_once(intel_pool, make_deps(intel_pool, fetcher, model))
    assert second.outcome["unchanged"] == 1 and model.extract_calls == 1  # consumed
    assert await _documents(intel_pool) == 1 and await _signals(intel_pool) == 0
    assert await _scalar(intel_pool, "SELECT string_agg(status, ',') FROM provider_calls") == "ok"
    breaker = await _scalar(
        intel_pool,
        "SELECT breaker_state || ':' || breaker_consecutive_failures FROM providers"
        " WHERE provider_id = 'claude_intel'",
    )
    assert breaker == "closed:0"


@pytest.mark.parametrize(
    ("reason", "sql"),
    [
        ("provider_disabled", "UPDATE providers SET enabled = false"),
        ("budget_unset", "UPDATE providers SET daily_budget_usd = NULL"),
    ],
)
async def test_a_gate_refusal_leaves_the_unit_unconsumed(
    intel_pool: AsyncConnectionPool, reason: str, sql: str
) -> None:
    source_id = await _source(intel_pool, queue=False)
    fetcher = FakeFetcher({TERMS: make_page(POLICY, TERMS)})
    model = FakeModel(make_signal())
    async with intel_pool.connection() as conn:
        await conn.execute(sql + " WHERE provider_id = 'claude_intel'")
    await _queue(intel_pool, source_id)
    refused = await run_once(intel_pool, make_deps(intel_pool, fetcher, model))
    assert refused.status == "refused" and refused.error_code == reason
    assert refused.outcome["refused_by"] == "gate" and model.extract_calls == 0
    assert await _documents(intel_pool) == 0
    assert await PostgresEvidenceStore(intel_pool).snapshot_for(source_id) is None
    source = await PostgresIntelStore(intel_pool).get_source(source_id)
    assert source is not None and source.last_run_status == f"deferred_{reason}"
    async with intel_pool.connection() as conn:
        await conn.execute(
            "UPDATE providers SET enabled = true, daily_budget_usd = 50"
            " WHERE provider_id = 'claude_intel'"
        )
    await _queue(intel_pool, source_id)
    retried = await run_once(intel_pool, make_deps(intel_pool, fetcher, model))
    assert retried.status == "completed" and retried.outcome["signals_kept"] == 1


async def test_a_model_timeout_leaves_the_unit_unconsumed(intel_pool: AsyncConnectionPool) -> None:
    source_id = await _source(intel_pool, queue=False)
    fetcher = FakeFetcher({TERMS: make_page(POLICY, TERMS)})
    down = FakeModel(unavailable=ModelUnavailable("timeout", "APITimeoutError"))
    await _queue(intel_pool, source_id)
    failed = await run_once(intel_pool, make_deps(intel_pool, fetcher, down))
    assert failed.status == "failed" and failed.error_code == "timeout"
    assert await _documents(intel_pool) == 0
    assert await _scalar(intel_pool, "SELECT status FROM provider_calls") == "timeout"
    await _queue(intel_pool, source_id)
    retried = await run_once(intel_pool, make_deps(intel_pool, fetcher, FakeModel(make_signal())))
    assert retried.status == "completed" and retried.outcome["signals_kept"] == 1


async def test_a_reclaimed_run_is_never_billed_twice(intel_pool: AsyncConnectionPool) -> None:
    await PostgresIntelStore(intel_pool).queue_adhoc(ARTICLE, operator="a")
    fetcher = FakeFetcher({ARTICLE: make_page(POLICY, ARTICLE)})
    model = FakeModel(make_signal())
    claimed = await claim(intel_pool)
    await run(claimed, make_deps(intel_pool, fetcher, model))
    again = await run(claimed, make_deps(intel_pool, fetcher, model))  # the lease expired
    assert model.extract_calls == 1 and again.outcome["already_recorded"] == 1
    assert await _documents(intel_pool) == 1 and await _signals(intel_pool) == 1


# ── feeds ────────────────────────────────────────────────────────────────────


async def test_a_first_feed_check_reads_the_ten_newest_and_counts_the_rest(
    intel_pool: AsyncConnectionPool,
) -> None:
    await _source(intel_pool, kind="feed", url=FEED)
    items = [
        {
            "title": f"t{i}",
            "link": f"https://n.example/{i}",
            "published": (NOW - timedelta(days=i % 60)).isoformat(),
        }
        for i in range(500)
    ]
    pages: dict[str, Any] = {FEED: make_page("feed", FEED, items=items)}
    pages.update(
        {f"https://n.example/{i}": make_page(POLICY, f"https://n.example/{i}") for i in range(500)}
    )
    fetcher, model = FakeFetcher(pages), FakeModel(ExtractionOutput(signals=[]))
    result = await run(await claim(intel_pool), make_deps(intel_pool, fetcher, model))
    fresh = sum(1 for i in range(500) if i % 60 <= 30)
    assert model.extract_calls == 10 and result.outcome["documents_recorded"] == 10
    assert result.outcome["too_old"] == 500 - fresh
    assert result.outcome["feed_backlog"] == fresh - 10
    newest = [f"https://n.example/{i}" for i in (*range(0, 500, 60), 1)]
    assert fetcher.fetched == [FEED, *newest]


async def test_feed_dates_that_are_naive_or_odd_never_crash_the_run(
    intel_pool: AsyncConnectionPool,
) -> None:
    await _source(intel_pool, kind="feed", url=FEED)
    naive_recent = (NOW - timedelta(days=1)).replace(tzinfo=None).isoformat()
    naive_old = (NOW - timedelta(days=40)).replace(tzinfo=None).isoformat()
    items: list[dict[str, Any]] = [
        {"title": "a", "link": "https://n.example/1", "published": naive_recent},
        {"title": "b", "link": "https://n.example/2", "published": naive_old},
        {"title": "c", "link": "https://n.example/3", "published": 123},
        {"title": "d", "link": "https://n.example/4", "published": "last Tuesday"},
        {"title": "e", "link": "http://n.example/5", "published": None},
        {"title": "f", "published": None},
    ]
    pages: dict[str, Any] = {FEED: make_page("feed", FEED, items=items)}
    pages.update(
        {f"https://n.example/{i}": make_page(POLICY, f"https://n.example/{i}") for i in range(5)}
    )
    model = FakeModel(ExtractionOutput(signals=[]))
    result = await run(await claim(intel_pool), make_deps(intel_pool, FakeFetcher(pages), model))
    assert result.status == "completed" and model.extract_calls == 3
    assert result.outcome["too_old"] == 1 and result.outcome["not_https"] == 1
    assert result.outcome["feed_item_no_link"] == 1


async def test_a_feed_backlog_is_read_on_later_runs_while_the_feed_is_unchanged(
    intel_pool: AsyncConnectionPool,
) -> None:
    source_id = await _source(intel_pool, kind="feed", url=FEED, queue=False)
    links = [f"https://n.example/{i}" for i in range(25)]
    pages: dict[str, Any] = {FEED: make_page("feed", FEED, items=_feed_items(links))}
    pages.update({link: make_page(POLICY, link) for link in links})
    fetcher, model = FakeFetcher(pages), FakeModel(ExtractionOutput(signals=[]))
    calls = []
    for _ in range(4):
        await _queue(intel_pool, source_id)
        await run_once(intel_pool, make_deps(intel_pool, fetcher, model))
        calls.append(model.extract_calls)
    assert calls == [10, 20, 25, 25]
    read = [url for url in fetcher.fetched if url != FEED]
    assert sorted(read) == sorted(links)  # every item once, none twice


async def test_a_redirecting_feed_item_is_not_read_twice(intel_pool: AsyncConnectionPool) -> None:
    source_id = await _source(intel_pool, kind="feed", url=FEED, queue=False)
    short = "https://n.example/short"
    pages: dict[str, Any] = {
        FEED: make_page("feed", FEED, items=_feed_items([short])),
        short: make_page(POLICY, short, final=ARTICLE),
    }
    fetcher, model = FakeFetcher(pages), FakeModel(ExtractionOutput(signals=[]))
    for _ in range(2):
        await _queue(intel_pool, source_id)
        await run_once(intel_pool, make_deps(intel_pool, fetcher, model))
    assert model.extract_calls == 1 and fetcher.fetched.count(short) == 1


# ── discovery ────────────────────────────────────────────────────────────────


async def test_discovery_fetches_candidates_with_web_trust(intel_pool: AsyncConnectionPool) -> None:
    await _source(intel_pool, kind="search_query", url=None, query="platform privacy policy change")
    discovery = DiscoveryOutput(
        candidates=[
            DiscoveryCandidate(url=ARTICLE, reason="r"),
            DiscoveryCandidate(url="http://n.example/b", reason="r"),
        ]
    )
    fetcher = FakeFetcher({ARTICLE: make_page(POLICY, ARTICLE)})
    model = FakeModel(make_signal(), discovery=discovery)
    result = await run(await claim(intel_pool), make_deps(intel_pool, fetcher, model))
    assert result.outcome["not_https"] == 1 and fetcher.fetched == [ARTICLE]
    assert result.outcome["model_calls"] == 3 and result.outcome["signals_kept"] == 1
    assert await _scalar(intel_pool, "SELECT trust FROM intel_documents") == "web"


async def test_discovery_skips_a_candidate_read_in_the_last_thirty_days(
    intel_pool: AsyncConnectionPool,
) -> None:
    other = "https://n.example/b"
    await PostgresIntelStore(intel_pool).queue_adhoc(ARTICLE, operator="a")
    fetcher = FakeFetcher({ARTICLE: make_page(POLICY, ARTICLE), other: make_page(POLICY, other)})
    discovery = DiscoveryOutput(
        candidates=[
            DiscoveryCandidate(url=ARTICLE, reason="r"),
            DiscoveryCandidate(url=other, reason="r"),
        ]
    )
    model = FakeModel(make_signal(), discovery=discovery)
    await run_once(intel_pool, make_deps(intel_pool, fetcher, model))
    await _source(intel_pool, kind="search_query", url=None, query="platform AI training")
    result = await run_once(intel_pool, make_deps(intel_pool, fetcher, model))
    assert result.outcome["recently_read"] == 1 and fetcher.fetched == [ARTICLE, other]


async def test_the_call_cap_stops_taking_units_and_leaves_the_rest(
    intel_pool: AsyncConnectionPool,
) -> None:
    await _source(intel_pool, kind="search_query", url=None, query="platform AI training")
    urls = [f"https://n.example/{i}" for i in range(3)]
    discovery = DiscoveryOutput(candidates=[DiscoveryCandidate(url=u, reason="r") for u in urls])
    fetcher = FakeFetcher({u: make_page(POLICY, u) for u in urls})
    model = FakeModel(make_signal(), discovery=discovery)
    deps = make_deps(intel_pool, fetcher, model, max_calls_per_run=2)
    result = await run(await claim(intel_pool), deps)
    # generation is one call beyond the reading cap (spec note, 2026-09-30)
    assert result.status == "completed" and result.outcome["model_calls"] == 3
    assert result.outcome["call_cap_deferred"] == 2 and result.outcome["stopped_call_cap"] == 1
    assert model.extract_calls == 1 and fetcher.fetched == urls[:1]
