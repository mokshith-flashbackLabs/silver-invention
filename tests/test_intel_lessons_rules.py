"""A lesson's shape and rules (spec 2026-10-15-intel-lessons-and-evidence §3, §5)."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from imageshield.intel.lessons import general_lesson, lesson_from, lesson_problems
from imageshield.intel.proposal_models import ContextSignal, LessonDraft
from imageshield.intel.recency import Recency
from imageshield.intel.schemas import (
    ProposedGeneralLesson,
    ProposedLesson,
    ProposedLessonSection,
)
from imageshield.search.urlhash import url_hash
from tests.intel_fakes import NOW, scoring

V = scoring()
VETTED = "https://help.example.com/remove-ai-video"
WEB = "https://blog.example.net/story"


def _signal(url: str, trust: str, quote: str, publisher: str = "example.com") -> ContextSignal:
    return ContextSignal(
        signal_id=uuid4(),
        category="protection",
        direction="risk_down",
        tags=("instagram",),
        unregistered_subjects=(),
        summary="s",
        trust=trust,  # type: ignore[arg-type]
        publisher_domain=publisher,
        status="active",
        created_at=NOW - timedelta(days=1),
        document_key=url_hash(url),
        excerpts=(quote,),
        document_url=url,
    )


CITED = [
    _signal(VETTED, "listed", "Requests are reviewed within 48 hours by the privacy team."),
    _signal(WEB, "web", "Scammers send a link to a fake sign-in page.", publisher="example.net"),
]


def _lesson(**over: object) -> ProposedLesson:
    fields: dict[str, object] = {
        "title": "Ask for an AI video of you to be removed",
        "intro": (
            "A platform can take down a realistic AI video of your face after a privacy request."
        ),
        "sections": [
            ProposedLessonSection(
                kind="how_to_use", items=["Open the privacy complaint form.", "Name the video."]
            )
        ],
        "link_url": VETTED,
        "link_label": "Privacy complaint form",
    }
    fields.update(over)
    return ProposedLesson.model_validate(fields)


def test_a_clean_lesson_is_kept_with_its_vetted_link() -> None:
    counts: Counter[str] = Counter()
    kept = lesson_from(_lesson(), CITED, counts)
    assert kept is not None
    assert kept["link"] == {"url": VETTED, "label": "Privacy complaint form"}
    assert counts == Counter()


def test_a_link_to_an_unvetted_page_is_dropped_and_the_lesson_kept() -> None:
    counts: Counter[str] = Counter()
    kept = lesson_from(_lesson(link_url=WEB), CITED, counts)
    assert kept is not None and kept["link"] is None
    assert counts["lesson_link_not_vetted"] == 1


def test_a_figure_no_cited_quote_carries_drops_the_lesson() -> None:
    counts: Counter[str] = Counter()
    assert (
        lesson_from(_lesson(intro="Requests are reviewed within 24 hours."), CITED, counts) is None
    )
    assert counts["lesson_dropped_forbidden_words"] == 1


def test_a_quoted_figure_is_allowed() -> None:
    kept = lesson_from(_lesson(intro="Requests are reviewed within 48 hours."), CITED, Counter())
    assert kept is not None


@pytest.mark.parametrize(
    "intro",
    [
        "You are safe once it is removed.",
        "If your photo was found, ask for removal.",
        "Visit www.example.com now.",
        "Nine in ten creators get these offers.",
    ],
)
def test_reassurance_a_finding_or_an_address_drops_the_lesson(intro: str) -> None:
    counts: Counter[str] = Counter()
    assert lesson_from(_lesson(intro=intro), CITED, counts) is None


def test_out_of_bounds_drops_the_lesson_never_truncates() -> None:
    counts: Counter[str] = Counter()
    assert lesson_from(_lesson(title="x" * 61), CITED, counts) is None
    assert counts["lesson_dropped_out_of_bounds"] == 1


def test_a_missing_lesson_is_counted() -> None:
    counts: Counter[str] = Counter()
    assert lesson_from(None, CITED, counts) is None
    assert counts["lesson_missing"] == 1


def test_a_section_kind_may_appear_once() -> None:
    with pytest.raises(ValidationError):
        LessonDraft.model_validate(
            {
                "title": "t",
                "intro": "i",
                "sections": [
                    {"kind": "what_to_do", "items": ["a"]},
                    {"kind": "what_to_do", "items": ["b"]},
                ],
            }
        )


def test_lesson_problems_names_the_field_and_the_rule() -> None:
    draft = LessonDraft.model_validate(
        {
            "title": "Stay safe",
            "intro": "Requests take 9 days.",
            "sections": [{"kind": "what_to_do", "items": ["Report it."]}],
            "link": {"url": WEB, "label": "Read"},
        }
    )
    problems = lesson_problems(draft, CITED)
    assert {"field": "title", "rule": "reassurance"} in problems
    assert {"field": "intro", "rule": "figure_not_quoted"} in problems
    assert {"field": "link", "rule": "link_not_vetted"} in problems


def test_a_general_lesson_becomes_a_lesson_proposal() -> None:
    counts: Counter[str] = Counter()
    context = {s.signal_id: s for s in CITED}
    proposal = general_lesson(
        ProposedGeneralLesson(
            topic="remove_ai_videos",
            tags=["instagram"],
            is_global=False,
            lesson=_lesson(),
            rationale="Two publishers describe the removal route.",
            signal_ids=[str(s.signal_id) for s in CITED],
        ),
        context,
        V,
        counts,
        taken_topics=set(),
        recency=Recency(NOW, 90),
    )
    assert proposal is not None and proposal.kind == "lesson"
    assert proposal.target == {"topic": "remove_ai_videos"}
    assert proposal.suggested["tags"] == ["instagram"]
    assert proposal.suggested["is_global"] is False


def test_a_repeated_topic_and_an_unscoped_lesson_are_dropped() -> None:
    counts: Counter[str] = Counter()
    context = {s.signal_id: s for s in CITED}
    item = ProposedGeneralLesson(
        topic="remove_ai_videos",
        lesson=_lesson(),
        rationale="r",
        signal_ids=[str(s.signal_id) for s in CITED],
    )
    fresh = Recency(NOW, 90)
    assert (
        general_lesson(item, context, V, counts, taken_topics={"remove_ai_videos"}, recency=fresh)
        is None
    )
    assert counts["proposal_dropped_lesson_repeat"] == 1
    assert general_lesson(item, context, V, counts, taken_topics=set(), recency=fresh) is None
    assert counts["proposal_dropped_lesson_unscoped"] == 1


def test_a_general_lesson_on_stale_evidence_is_dropped() -> None:
    counts: Counter[str] = Counter()
    old = [replace(s, published_at=NOW - timedelta(days=400)) for s in CITED]
    item = ProposedGeneralLesson(
        topic="remove_ai_videos",
        tags=["instagram"],
        lesson=_lesson(),
        rationale="r",
        signal_ids=[str(s.signal_id) for s in old],
    )
    assert (
        general_lesson(
            item,
            {s.signal_id: s for s in old},
            V,
            counts,
            taken_topics=set(),
            recency=Recency(NOW, 90),
        )
        is None
    )
    assert counts["proposal_dropped_evidence_stale"] == 1
