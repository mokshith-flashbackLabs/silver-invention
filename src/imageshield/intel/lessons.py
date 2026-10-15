"""Lessons (spec 2026-10-15-intel-lessons-and-evidence §3-§5): code, never the model, decides what
a lesson may say.

A lesson is checked for its bounds, for the score page's word rules (intel/user_copy.py) and for
every figure appearing in a quote of its own cited evidence. Its link must be the final URL of one
of those documents that came from a source an operator registered (``trust = 'listed'``): an
unvetted page can advertise the very apps an incident is about. At draft an unvetted link is
dropped and the lesson kept; anything else that fails drops the lesson, never the event it belongs
to, and is counted on the run's outcome.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from imageshield.intel.bounds import LESSON_TOPIC_PATTERN, MAX_RATIONALE_CHARS
from imageshield.intel.corroboration import uncorroborated
from imageshield.intel.pii import mask
from imageshield.intel.proposal_models import (
    ContextSignal,
    LessonDraft,
    LessonSuggested,
    LessonTarget,
    NewProposal,
)
from imageshield.intel.recency import Recency, evidence_stale
from imageshield.intel.schemas import ProposedGeneralLesson, ProposedLesson
from imageshield.intel.tags import is_well_formed, membership_problems
from imageshield.intel.text import normalise
from imageshield.intel.user_copy import copy_problems, unquoted_figures
from imageshield.intel.vocabulary import ScoringVocabulary
from imageshield.search.urlhash import url_hash


def _vetted_pages(cited: Sequence[ContextSignal]) -> set[str]:
    return {s.document_key for s in cited if s.trust == "listed" and s.document_key is not None}


def _texts(draft: LessonDraft) -> list[tuple[str, str]]:
    texts = [("title", draft.title), ("intro", draft.intro)]
    for i, section in enumerate(draft.sections):
        texts.extend((f"sections[{i}].items[{j}]", item) for j, item in enumerate(section.items))
    if draft.link is not None:
        texts.append(("link.label", draft.link.label))
    return texts


def lesson_problems(draft: LessonDraft, cited: Sequence[ContextSignal]) -> list[dict[str, str]]:
    """Every rule a lesson breaks, by field: the word rules, any figure no cited quote carries,
    and a link that is not one of the cited vetted pages. Empty: the lesson may be shown."""
    quotes = [quote for signal in cited for quote in signal.excerpts]
    problems: list[dict[str, str]] = []
    for name, text in _texts(draft):
        problems.extend({"field": name, "rule": rule} for rule in copy_problems(text))
        if unquoted_figures(text, quotes):
            problems.append({"field": name, "rule": "figure_not_quoted"})
    if draft.link is not None and url_hash(draft.link.url) not in _vetted_pages(cited):
        problems.append({"field": "link", "rule": "link_not_vetted"})
    return problems


def _masked(raw: str, counts: Counter[str]) -> str:
    masked, masks = mask(raw)
    if masks:
        counts["pii_masked_lesson"] += 1
    return normalise(masked)


def lesson_from(
    item: ProposedLesson | None, cited: Sequence[ContextSignal], counts: Counter[str]
) -> dict[str, Any] | None:
    """A drafted lesson as ``suggested.lesson`` stores it, or None (counted)."""
    if item is None:
        counts["lesson_missing"] += 1
        return None
    link: dict[str, str] | None = None
    raw_link = (item.link_url or "").strip()
    if raw_link:
        if (
            raw_link.startswith("https://")
            and url_hash(raw_link) in _vetted_pages(cited)
            and (item.link_label or "").strip()
        ):
            link = {"url": raw_link, "label": _masked(item.link_label or "", counts)}
        else:
            counts["lesson_link_not_vetted"] += 1
    try:
        draft = LessonDraft.model_validate(
            {
                "title": _masked(item.title, counts),
                "intro": _masked(item.intro, counts),
                "sections": [
                    {"kind": s.kind, "items": [_masked(i, counts) for i in s.items]}
                    for s in item.sections
                ],
                "link": link,
            }
        )
    except ValidationError:
        counts["lesson_dropped_out_of_bounds"] += 1
        return None
    if lesson_problems(draft, cited):
        counts["lesson_dropped_forbidden_words"] += 1
        return None
    return draft.model_dump(mode="json")


def _cited_ids(
    raw_ids: Sequence[str], context: Mapping[UUID, ContextSignal], counts: Counter[str]
) -> list[UUID] | None:
    cited: list[UUID] = []
    for raw in raw_ids:
        try:
            signal_id = UUID(raw)
        except ValueError:
            counts["proposal_dropped_unknown_signal"] += 1
            return None
        signal = context.get(signal_id)
        if signal is None or signal.status != "active":
            counts["proposal_dropped_unknown_signal"] += 1
            return None
        if signal_id not in cited:
            cited.append(signal_id)
    if not cited:
        counts["proposal_dropped_no_signals"] += 1
        return None
    return cited


def general_lesson(
    item: ProposedGeneralLesson,
    context: Mapping[UUID, ContextSignal],
    vocabulary: ScoringVocabulary,
    counts: Counter[str],
    *,
    taken_topics: Collection[str],
    recency: Recency,
) -> NewProposal | None:
    """A ``lesson`` proposal for a general lesson (spec §5), or None (counted). Its evidence must
    corroborate as a threat's does and pass the same recency rule (all of it dated and older than
    ``INTEL_THREAT_RECENCY_DAYS`` is ``evidence_stale``); its tags must be registered and live; and
    it must reach somebody: tags, or global."""
    if not re.fullmatch(LESSON_TOPIC_PATTERN, item.topic):
        counts["proposal_dropped_lesson_topic_malformed"] += 1
        return None
    if item.topic in taken_topics:
        counts["proposal_dropped_lesson_repeat"] += 1
        return None
    if any(not is_well_formed(t) for t in item.tags) or len(set(item.tags)) != len(item.tags):
        counts["proposal_dropped_tag_malformed"] += 1
        return None
    unknown, retired = membership_problems(item.tags, vocabulary.registry())
    if unknown:
        counts["proposal_dropped_unknown_tag"] += 1
        return None
    if retired:
        counts["proposal_dropped_tag_retired"] += 1
        return None
    if not item.tags and not item.is_global:
        counts["proposal_dropped_lesson_unscoped"] += 1
        return None
    cited_ids = _cited_ids(item.signal_ids, context, counts)
    if cited_ids is None:
        return None
    cited = [context[i] for i in cited_ids]
    if uncorroborated(cited):
        counts["proposal_dropped_uncorroborated"] += 1
        return None
    if evidence_stale(cited, recency):
        counts["proposal_dropped_evidence_stale"] += 1
        return None
    lesson = lesson_from(item.lesson, cited, counts)
    if lesson is None:
        return None
    rationale = _masked(item.rationale, counts)
    if not rationale or len(rationale) > MAX_RATIONALE_CHARS:
        counts["proposal_dropped_rationale"] += 1
        return None
    return NewProposal(
        kind="lesson",
        target=LessonTarget(topic=item.topic).model_dump(mode="json", exclude_none=True),
        suggested=LessonSuggested.model_validate(
            {"lesson": lesson, "tags": item.tags, "is_global": item.is_global}
        ).model_dump(mode="json"),
        rationale=rationale,
        signal_ids=tuple(cited_ids),
        document_keys=tuple(dict.fromkeys(s.document_key for s in cited if s.document_key)),
    )
