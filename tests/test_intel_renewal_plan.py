"""Protection renewal, the pure half (spec §4.8, INVARIANTS #49): which excerpts still verify,
and the renewal proposal and records built from them. No database, no model, no fetch."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from imageshield.intel.renewal import (
    RENEWAL_VERSION,
    RENEWAL_WRITER,
    RenewalEvidence,
    RenewalExcerpt,
    RenewalPage,
    RenewalSignal,
    plan_renewal,
    renewal_rationale,
    renewal_suggested,
    renewal_target,
    renewal_units,
)
from imageshield.search.urlhash import url_hash

QUOTE_A = "Members can now opt their photos out of AI model training at any time."
QUOTE_B = "The opt-out applies to every account, whatever country the member lives in."
URL_A = "https://newsroom.example.com/opt-out"
URL_B = "https://help.example.com/privacy"
T0 = datetime(2026, 9, 30, tzinfo=UTC)


def _signal(url: str, *quotes: str) -> RenewalSignal:
    return RenewalSignal(
        signal_id=uuid4(),
        category="protection",
        direction="risk_down",
        tags=("instagram",),
        unregistered_subjects=(),
        summary="An opt-out shipped.",
        model_id="claude-sonnet-5",
        prompt_version="extract-v1",
        document_url=url,
        document_url_hash=url_hash(url),
        title="Newsroom",
        published_at=None,
        excerpts=tuple(RenewalExcerpt(uuid4(), q) for q in quotes),
    )


def _evidence(
    *signals: RenewalSignal, tags: tuple[str, ...] = ("instagram",), is_global: bool = False
) -> RenewalEvidence:
    return RenewalEvidence(
        event_id=uuid4(),
        title="Instagram opt-out",
        strength=2,
        tags=tags,
        is_global=is_global,
        starts_at=T0 - timedelta(days=160),
        review_by=T0 + timedelta(days=20),
        signals=signals,
        body="You can keep your photos out of AI training in your settings.",
    )


def _page(url: str, text: str, final: str | None = None) -> RenewalPage:
    return RenewalPage(requested_url=url, final_url=final or url, text=text, truncated=False)


def test_an_excerpt_still_on_its_page_is_recited_at_its_new_offsets() -> None:
    signal = _signal(URL_A, QUOTE_A)
    text = "Moved paragraph. " + QUOTE_A
    plan = plan_renewal(_evidence(signal), {signal.document_url_hash: _page(URL_A, text)})
    (renewed,) = plan.signals
    (quote,) = renewed.quotes
    assert (quote.text, quote.char_start) == (QUOTE_A, len("Moved paragraph. "))
    assert (plan.excerpts_checked, plan.excerpts_verified, plan.unreachable) == (1, 1, False)


def test_a_changed_page_drops_the_excerpt_and_the_signal_with_it() -> None:
    signal = _signal(URL_A, QUOTE_A)
    gone = _page(URL_A, "The opt-out was withdrawn in March.")
    plan = plan_renewal(_evidence(signal), {signal.document_url_hash: gone})
    assert plan.signals == () and dict(plan.dropped) == {"not_a_substring": 1}
    assert plan.unreachable is False


def test_only_the_excerpts_that_still_verify_are_recited() -> None:
    kept, lost = _signal(URL_A, QUOTE_A, QUOTE_B), _signal(URL_B, QUOTE_B)
    pages = {
        kept.document_url_hash: _page(URL_A, QUOTE_A),
        lost.document_url_hash: _page(URL_B, "This page now says something else entirely."),
    }
    plan = plan_renewal(_evidence(kept, lost), pages)
    (renewed,) = plan.signals
    assert renewed.original is kept and [q.text for q in renewed.quotes] == [QUOTE_A]
    assert (plan.excerpts_checked, plan.excerpts_verified) == (3, 1)
    assert dict(plan.dropped) == {"not_a_substring": 2}


def test_an_unreachable_page_is_told_apart_from_one_that_cannot_be_read() -> None:
    """Review Focus 2, the rule: only a page the fetcher could not reach is retried."""
    signal = _signal(URL_A, QUOTE_A)
    unreachable = plan_renewal(_evidence(signal), {signal.document_url_hash: "fetch_unfetchable"})
    assert unreachable.signals == () and unreachable.unreachable is True
    for reason in ("fetch_unsupported_type", "known_hit_location", "not_https"):
        plan = plan_renewal(_evidence(signal), {signal.document_url_hash: reason})
        assert plan.unreachable is False and dict(plan.dropped) == {reason: 1}


def test_the_units_are_new_listed_documents_carrying_the_old_signals_text() -> None:
    web = _signal(URL_A, QUOTE_A)
    final = "https://newsroom.example.com/opt-out-moved"
    plan = plan_renewal(_evidence(web), {web.document_url_hash: _page(URL_A, QUOTE_A, final)})
    run_id = uuid4()
    ((document, signals),) = renewal_units(run_id, plan)
    assert document.run_id == run_id and document.trust == "listed"
    assert document.source_id is None and document.final_url == final
    assert document.url_hash == url_hash(final) and document.document_url_hash == url_hash(URL_A)
    assert document.publisher_domain == "example.com" and document.title == "Newsroom"
    (signal,) = signals
    assert (signal.category, signal.direction, signal.summary, signal.model_id) == (
        "protection",
        "risk_down",
        "An opt-out shipped.",
        "claude-sonnet-5",
    )
    assert [q.text for q in signal.quotes] == [QUOTE_A]


def test_two_cited_urls_now_on_one_page_make_one_document() -> None:
    a, b = _signal(URL_A, QUOTE_A), _signal(URL_B, QUOTE_B)
    page = _page(URL_A, QUOTE_A + " " + QUOTE_B)
    plan = plan_renewal(_evidence(a, b), {a.document_url_hash: page, b.document_url_hash: page})
    ((_, signals),) = renewal_units(uuid4(), plan)
    assert len(signals) == 2


def test_the_proposal_carries_the_credits_own_scope_values_and_what_it_renews() -> None:
    """Review Focus 5, the shape: a global scope is carried forward."""
    evidence = _evidence(_signal(URL_A, QUOTE_A))
    assert renewal_target(evidence) == {
        "tags": ["instagram"],
        "is_global": False,
        "renews_event_id": str(evidence.event_id),
    }
    # The credit's "what it means for you" is carried forward unchanged (spec
    # 2026-10-06-intel-event-body).
    assert renewal_suggested(evidence) == {
        "title": "Instagram opt-out",
        "body": "You can keep your photos out of AI training in your settings.",
        "strength": 2,
        "review_in_days": 180,
    }
    everyone = _evidence(_signal(URL_A, QUOTE_A), tags=(), is_global=True)
    assert renewal_target(everyone)["is_global"] is True and renewal_target(everyone)["tags"] == []
    assert (RENEWAL_WRITER, RENEWAL_VERSION) == ("code:renewal", "renewal-v1")


def test_the_rationale_counts_what_was_checked_and_says_code_wrote_it() -> None:
    signal = _signal(URL_A, QUOTE_A, QUOTE_B)
    plan = plan_renewal(_evidence(signal), {signal.document_url_hash: _page(URL_A, QUOTE_A)})
    text = renewal_rationale(plan)
    assert "1 of 2" in text and "not by a model" in text


def test_the_review_period_survives_a_daylight_saving_hour() -> None:
    evidence = _evidence(_signal(URL_A, QUOTE_A))
    assert (
        replace(evidence, review_by=evidence.review_by - timedelta(hours=1)).review_in_days == 180
    )
