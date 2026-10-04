"""Publication dates and recency (spec 2026-10-04-intel-evidence-quality §2, §3). Pure."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from imageshield.intel.fetch_client import TextFetch
from imageshield.intel.model import web_results
from imageshield.intel.proposal_models import ContextSignal
from imageshield.intel.recency import (
    Recency,
    choose_published,
    evidence_dates,
    evidence_stale,
    parse_page_age,
    plausible,
    stated_date,
)

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
RECENCY = Recency(NOW, 90)


def _signal(
    published: datetime | None,
    *,
    document: str | None = None,
    status: str = "active",
) -> ContextSignal:
    return ContextSignal(
        signal_id=uuid4(),
        category="incident",
        direction="risk_up",
        tags=("instagram",),
        unregistered_subjects=(),
        summary="s",
        trust="web",
        publisher_domain="news.example",
        status=status,
        created_at=NOW,
        document_key=document or uuid4().hex,
        published_at=published,
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("3 days ago", NOW - timedelta(days=3)),
        ("1 week ago", NOW - timedelta(weeks=1)),
        ("an hour ago", NOW - timedelta(hours=1)),
        ("2 months ago", NOW - timedelta(days=60)),
        ("yesterday", NOW - timedelta(days=1)),
        ("April 30, 2025", datetime(2025, 4, 30, tzinfo=UTC)),
        ("2024-07-02T10:00:00", datetime(2024, 7, 2, 10, tzinfo=UTC)),
    ],
)
def test_a_page_age_is_read_absolute_or_against_the_moment_the_search_answered(
    raw: str, expected: datetime
) -> None:
    assert parse_page_age(raw, NOW) == expected


@pytest.mark.parametrize("raw", [None, "", "recently", "a while back"])
def test_an_unreadable_page_age_is_none(raw: str | None) -> None:
    assert parse_page_age(raw, NOW) is None


def test_a_stated_date_needs_the_iso_form_and_its_year_in_the_text() -> None:
    text = "Published 2 July 2024. YouTube now lets people request removal."
    assert stated_date("2024-07-02", text) == datetime(2024, 7, 2, tzinfo=UTC)
    assert stated_date("2023-07-02", text) is None  # the year is nowhere in the text
    assert stated_date("July 2, 2024", text) is None
    assert stated_date("2024-13-40", text) is None
    assert stated_date(None, text) is None


def test_an_implausible_date_is_dropped() -> None:
    assert plausible(datetime(1970, 1, 1, tzinfo=UTC), NOW) is None
    assert plausible(NOW + timedelta(days=30), NOW) is None
    assert plausible(NOW + timedelta(days=1), NOW) == NOW + timedelta(days=1)  # a zone ahead
    assert plausible(datetime(2026, 1, 1), NOW) == datetime(2026, 1, 1, tzinfo=UTC)  # naive: UTC


def test_metadata_beats_the_model_and_the_feed_beats_metadata() -> None:
    feed, meta = datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC)
    search, stated = datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 6, 1, tzinfo=UTC)
    assert choose_published(
        feed=None, metadata=meta, page_age=search, stated=stated, now=NOW
    ) == (meta, "metadata")
    assert choose_published(
        feed=feed, metadata=meta, page_age=search, stated=stated, now=NOW
    ) == (feed, "feed")
    assert choose_published(
        feed=None, metadata=None, page_age=search, stated=stated, now=NOW
    ) == (search, "search")
    assert choose_published(
        feed=None, metadata=None, page_age=None, stated=stated, now=NOW
    ) == (stated, "text")
    # An implausible metadata date falls through rather than winning.
    assert choose_published(
        feed=None, metadata=NOW + timedelta(days=400), page_age=None, stated=stated, now=NOW
    ) == (stated, "text")


def test_the_fetch_time_is_never_a_publication_date() -> None:
    assert choose_published(
        feed=None, metadata=None, page_age=None, stated=None, now=NOW
    ) == (None, "unknown")


def test_stale_only_when_every_active_signal_is_dated_and_old() -> None:
    old, older = NOW - timedelta(days=200), NOW - timedelta(days=400)
    assert evidence_stale([_signal(old), _signal(older)], RECENCY)
    assert not evidence_stale([_signal(old), _signal(NOW - timedelta(days=5))], RECENCY)
    assert not evidence_stale([_signal(old), _signal(None)], RECENCY)  # undated is never stale
    assert not evidence_stale([_signal(None)], RECENCY)
    assert not evidence_stale([], RECENCY)  # no evidence is evidence_retracted, not stale
    # A retracted recent signal does not rescue old active evidence.
    assert evidence_stale([_signal(old), _signal(NOW, status="retracted")], RECENCY)
    assert not evidence_stale([_signal(NOW - timedelta(days=89))], RECENCY)


def test_evidence_dates_count_documents_not_signals() -> None:
    a = _signal(datetime(2026, 9, 12, 8, tzinfo=UTC), document="a")
    a2 = _signal(datetime(2026, 9, 12, 8, tzinfo=UTC), document="a")
    b = _signal(datetime(2026, 10, 2, tzinfo=UTC), document="b")
    c, c2 = _signal(None, document="c"), _signal(None, document="c")
    d = _signal(None, document="d")
    gone = _signal(datetime(2020, 1, 1, tzinfo=UTC), document="e", status="retracted")
    assert evidence_dates([a, a2, b, c, c2, d, gone]) == {
        "newest": "2026-10-02",
        "oldest": "2026-09-12",
        "undated": 2,
    }
    assert evidence_dates([]) == {"newest": None, "oldest": None, "undated": 0}


def test_the_fetch_client_reads_the_page_date_leniently() -> None:
    base = {
        "text": "t",
        "content_type": "text/html",
        "final_url": "https://n.example/a",
        "truncated": False,
        "items": None,
    }
    assert TextFetch.model_validate(base).published_at is None  # an older fetcher
    dated = TextFetch.model_validate({**base, "published_at": "2026-09-12T09:30:00-04:00"})
    assert dated.published_at == datetime(2026, 9, 12, 13, 30, tzinfo=UTC)
    naive = TextFetch.model_validate({**base, "published_at": "2026-09-12T09:30:00"})
    assert naive.published_at == datetime(2026, 9, 12, 9, 30, tzinfo=UTC)
    assert TextFetch.model_validate({**base, "published_at": "soon"}).published_at is None


def test_web_results_carry_each_results_page_age_and_skip_an_error_block() -> None:
    content = [
        {"type": "server_tool_use", "id": "x", "name": "web_search", "input": {"query": "q"}},
        {
            "type": "web_search_tool_result",
            "content": [
                {"type": "web_search_result", "url": "https://n.example/a",
                 "page_age": "2 days ago"},
                {"type": "web_search_result", "url": "https://n.example/b", "page_age": None},
            ],
        },
        {"type": "web_search_tool_result", "content": {"error_code": "max_uses_exceeded"}},
    ]
    found = web_results(content)
    assert [(r.url, r.page_age) for r in found] == [
        ("https://n.example/a", "2 days ago"),
        ("https://n.example/b", None),
    ]


def test_extract_v2_asks_only_for_a_date_the_text_states() -> None:
    from imageshield.intel.prompts import EXTRACT_PROMPT_VERSION, extraction_request
    from imageshield.intel.schemas import ExtractionOutput

    assert EXTRACT_PROMPT_VERSION == "extract-v2"
    system, _ = extraction_request("text", source_kind="news", tag_hints=(), registry_tags=())
    flat = " ".join(system.split())
    assert "published_date" in flat and "YYYY-MM-DD" in flat and "never guess" in flat
    assert "published_date" in ExtractionOutput.model_json_schema()["properties"]
    # A malformed value is a string the parse keeps, never a response it cannot read.
    assert ExtractionOutput.model_validate({"signals": [], "published_date": "soon"})
