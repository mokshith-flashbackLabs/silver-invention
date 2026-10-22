"""Lessons in the propose step (spec 2026-10-15-intel-lessons-and-evidence §4, §5)."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from uuid import uuid4

from imageshield.intel.generation import GeneratedBatch, validate_proposals
from imageshield.intel.prompts import PROPOSE_PROMPT_VERSION, with_proposed_events
from imageshield.intel.proposal_models import ContextSignal, ExistingLesson
from imageshield.intel.recency import Recency
from imageshield.intel.schemas import (
    ProposalEventsOutput,
    ProposalOutput,
    ProposedEventLesson,
    ProposedGeneralLesson,
    ProposedLesson,
    ProposedLessonSection,
    ProposedThreatEvent,
)
from imageshield.search.urlhash import url_hash
from tests.intel_fakes import NOW, scoring

V = scoring()


def _signal(publisher: str, quote: str) -> ContextSignal:
    url = f"https://{publisher}/a"
    return ContextSignal(
        signal_id=uuid4(),
        category="incident",
        direction="risk_up",
        tags=("instagram",),
        unregistered_subjects=(),
        summary="s",
        trust="listed",
        publisher_domain=publisher,
        status="active",
        created_at=NOW - timedelta(days=1),
        document_key=url_hash(url),
        excerpts=(quote,),
        document_url=url,
    )


# Two quotes that do not echo each other: since 2026-10-22 a lesson needs two INDEPENDENT sources.
SIGNALS = [
    _signal("a.example", "Scammers send a fake sign-in link to channel owners."),
    _signal("b.example", "Brand deals offered by strangers led creators to lose their accounts."),
]
LESSON = ProposedLesson(
    title="Spot a fake sponsorship offer",
    intro="A fake brand deal leads to a sign-in page that steals the account.",
    sections=[
        ProposedLessonSection(kind="warning_signs", items=["A deal you never applied for."]),
        ProposedLessonSection(kind="what_to_do", items=["Sign in only from the app itself."]),
    ],
)


def _threat() -> ProposedThreatEvent:
    return ProposedThreatEvent(
        kind="platform_incident",
        title="Fake sponsorship offers",
        body="b",
        severity=3,
        expires_in_days=30,
        tags=["instagram"],
        rationale="r",
        signal_ids=[str(s.signal_id) for s in SIGNALS],
    )


def _general() -> ProposedGeneralLesson:
    return ProposedGeneralLesson(
        topic="fake_sponsorships",
        tags=["instagram"],
        lesson=LESSON,
        rationale="r",
        signal_ids=[str(s.signal_id) for s in SIGNALS],
    )


def _validate(
    output: ProposalOutput,
    *,
    events_only: bool = False,
    existing: tuple[ExistingLesson, ...] = (),
) -> tuple[GeneratedBatch, Counter[str]]:
    counts: Counter[str] = Counter()
    batch = validate_proposals(
        output,
        context={s.signal_id: s for s in SIGNALS},
        gap_pool=[],
        vocabulary=V,
        now=NOW,
        counts=counts,
        recency=Recency(NOW, 90),
        events_only=events_only,
        existing_lessons=existing,
    )
    return batch, counts


def test_the_prompt_version_is_v15() -> None:
    assert PROPOSE_PROMPT_VERSION == "propose-v15"


def test_an_event_lesson_rides_on_its_threat_by_key() -> None:
    batch, counts = _validate(
        ProposalOutput(
            threat_events=[_threat()],
            event_lessons=[ProposedEventLesson(event="T1", lesson=LESSON)],
        )
    )
    [threat] = batch.threat_events
    assert threat.suggested["lesson"]["title"] == "Spot a fake sponsorship offer"
    assert counts["lesson_missing"] == 0


def test_a_threat_with_no_lesson_is_kept_and_counted() -> None:
    batch, counts = _validate(ProposalOutput(threat_events=[_threat()]))
    assert batch.threat_events[0].suggested["lesson"] is None
    assert counts["lesson_missing"] == 1


def test_a_lesson_for_an_unknown_key_is_counted() -> None:
    _, counts = _validate(
        ProposalOutput(event_lessons=[ProposedEventLesson(event="T9", lesson=LESSON)])
    )
    assert counts["lesson_event_unknown"] == 1


def test_general_lessons_are_written_and_dropped_on_an_events_only_pass() -> None:
    batch, _ = _validate(ProposalOutput(lessons=[_general()]))
    assert [p.kind for p in batch.lessons] == ["lesson"]
    batch, counts = _validate(ProposalOutput(lessons=[_general()]), events_only=True)
    assert batch.lessons == [] and counts["proposal_dropped_not_an_event"] == 1


def test_an_open_topic_and_two_lessons_on_one_topic_keep_none_then_the_first() -> None:
    batch, counts = _validate(
        ProposalOutput(lessons=[_general()]),
        existing=(ExistingLesson(topic="fake_sponsorships", title="Another title"),),
    )
    assert batch.lessons == [] and counts["proposal_dropped_lesson_repeat"] == 1
    batch, counts = _validate(ProposalOutput(lessons=[_general(), _general()]))
    assert len(batch.lessons) == 1 and counts["proposal_dropped_lesson_repeat"] == 1


def test_with_proposed_events_keys_every_event() -> None:
    user = with_proposed_events(
        '{"new_evidence": []}', ProposalEventsOutput(threat_events=[_threat()])
    )
    assert '"key": "T1"' in user and "Fake sponsorship offers" in user


# -- 2026-10-22: a lesson that repeats an existing one --


def test_a_general_lesson_with_an_existing_title_under_a_new_topic_is_dropped() -> None:
    batch, counts = _validate(
        ProposalOutput(lessons=[_general()]),
        existing=(ExistingLesson(topic=None, title="Spot a FAKE sponsorship offer."),),
    )
    assert batch.lessons == [] and counts["proposal_dropped_lesson_repeat"] == 1


def test_an_event_lesson_repeating_an_existing_title_is_dropped_and_the_event_kept() -> None:
    """Dev, 2026-10-21: an event lesson and a library lesson read "Fake recovery services that
    target scam victims", word for word."""
    batch, counts = _validate(
        ProposalOutput(
            threat_events=[_threat()],
            event_lessons=[ProposedEventLesson(event="T1", lesson=LESSON)],
        ),
        existing=(ExistingLesson(topic="x", title="Spot a fake sponsorship offer"),),
    )
    [threat] = batch.threat_events
    assert threat.suggested["lesson"] is None
    assert counts["lesson_dropped_repeat"] == 1


def test_two_lessons_with_one_title_in_a_run_keep_the_first() -> None:
    general = _general()
    batch, counts = _validate(
        ProposalOutput(
            threat_events=[_threat()],
            event_lessons=[ProposedEventLesson(event="T1", lesson=LESSON)],
            lessons=[general],
        )
    )
    assert batch.threat_events[0].suggested["lesson"] is not None
    assert batch.lessons == [] and counts["proposal_dropped_lesson_repeat"] == 1
