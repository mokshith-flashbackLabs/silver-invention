"""Weight and tag suggestions for a quiz draft (spec §4.6, §4.10). Pure: no database, no model.

What the model sees. A suggestion run first reads the sources its request registered, then takes
active signals of the last SUGGESTION_CONTEXT_DAYS in four classes, in this order, deduplicated and
bounded at SUGGESTION_CONTEXT_MAX_SIGNALS:
1. signals from the request's chosen sources (those just read, and any it reused);
2. by tag: signals carrying one of the options' tags, or a registered tag whose slug or label the
   option names ("Bumble" finds `bumble` evidence before anybody maps it);
3. by subject: signals whose unregistered_subjects name an option;
4. by category: research, policy or incident signals whose summary names an option.

"Names" (``mentions``) is exact on normalised words, never fuzzy: the option's whole text, or any
of its words of at least four letters ("X (Twitter)" is named by "twitter"). An option shorter
than that must equal the text outright, so "No" and "X" never match a summary.

What is kept. Every option is checked against §4.5, and a value that fails is WITHHELD and
counted: a deduction outside 0..10 or above the cap, or with no surviving evidence, becomes null;
an unknown or retired tag is removed; a new_tag that is registered, malformed, or offered beside a
fitting tag is removed. Nothing is clamped and no number is invented, and the other options and
the suggestion stand. A suggestion whose every option is null is still delivered: "no evidence,
operator's call" is an answer, and a tag suggestion needs no evidence.

The read side (``suggestion_options``), for the poll and for GET /proposals/{id}: per option,
``corroborated`` is the one predicate (intel/corroboration.py) over that option's OWN cited
signals, and ``why_not`` says why not.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from imageshield.intel.bounds import (
    DEDUCTION_MAX,
    DEDUCTION_MIN,
    MAX_RATIONALE_CHARS,
    MAX_TAG_LABEL_CHARS,
)
from imageshield.intel.corroboration import uncorroborated
from imageshield.intel.models import Run
from imageshield.intel.pii import mask
from imageshield.intel.prompts import PromptSuggestionOption, PromptSuggestionQuestion
from imageshield.intel.proposal_models import ContextSignal, SuggestedTag
from imageshield.intel.schemas import ProposedTag, SuggestedOptionWeight, SuggestionOutput
from imageshield.intel.tags import TagRegistry, is_well_formed
from imageshield.intel.text import normalise
from imageshield.intel.vocabulary import ScoringVocabulary, normalise_subject

SuggestionWhyNot = Literal["no_evidence", "evidence_retracted", "uncorroborated"]
_MIN_TERM_CHARS = 4
# Retrieval's category class (spec §4.6). Public: question_store.py reads the class with it.
CATEGORY_CLASS: frozenset[str] = frozenset({"research", "policy", "incident"})


class SuggestionRunRequest(BaseModel):
    """A weight_suggestion run's stored request: the draft question as POST /weight-suggestions
    wrote it, plus the sources the same transaction registered (``new_source_ids``, read first)
    and every source the request named (``source_ids``, retrieval's first class)."""

    model_config = ConfigDict(frozen=True)

    question_key: str
    prompt: str
    type: str | None = None
    options: tuple[str, ...]
    cap: int | None = None
    tags: dict[str, tuple[str, ...]] | None = None
    new_source_ids: tuple[UUID, ...] = ()
    source_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True)
class OptionSuggestion:
    option: str
    deduction: int | None
    rationale: str
    signal_ids: tuple[UUID, ...]
    suggested_tags: tuple[str, ...]
    new_tag: SuggestedTag | None

    def as_json(self) -> dict[str, Any]:
        """One entry of the proposal's ``target.options`` (spec §3.6)."""
        return {
            "option": self.option,
            "deduction": self.deduction,
            "rationale": self.rationale,
            "signal_ids": [str(s) for s in self.signal_ids],
            "suggested_tags": list(self.suggested_tags),
            "new_tag": self.new_tag.model_dump() if self.new_tag is not None else None,
        }


@dataclass(frozen=True)
class SuggestionCandidates:
    """The four retrieval classes as the store reads them, newest first each."""

    from_sources: tuple[ContextSignal, ...] = ()
    by_tag: tuple[ContextSignal, ...] = ()
    with_subjects: tuple[ContextSignal, ...] = ()
    by_category: tuple[ContextSignal, ...] = ()


def _terms(option_key: str) -> set[str]:
    words = {word for word in option_key.split() if len(word) >= _MIN_TERM_CHARS}
    return words | ({option_key} if len(option_key) >= _MIN_TERM_CHARS else set())


def mentions(text: str, option: str) -> bool:
    option_key, text_key = normalise_subject(option), normalise_subject(text)
    if not option_key or not text_key:
        return False
    if text_key == option_key:
        return True
    padded = f" {text_key} "
    return any(f" {term} " in padded for term in _terms(option_key))


def slugs_named_by(options: Sequence[str], vocabulary: ScoringVocabulary | None) -> frozenset[str]:
    """Registered tags whose slug or label an option names: retrieval asks for them by tag."""
    if vocabulary is None:
        return frozenset()
    return frozenset(
        slug
        for slug, entry in vocabulary.tags.items()
        if any(mentions(entry.label, option) or mentions(slug, option) for option in options)
    )


def select_context(
    candidates: SuggestionCandidates, *, options: Sequence[str], limit: int
) -> list[ContextSignal]:
    chosen: dict[UUID, ContextSignal] = {}

    def take(signals: Iterable[ContextSignal]) -> None:
        for signal in signals:
            if len(chosen) >= limit:
                return
            if signal.status == "active":
                chosen.setdefault(signal.signal_id, signal)

    take(candidates.from_sources)
    take(candidates.by_tag)
    take(
        s
        for s in candidates.with_subjects
        if any(mentions(subject, o) for subject in s.unregistered_subjects for o in options)
    )
    take(
        s
        for s in candidates.by_category
        if s.category in CATEGORY_CLASS and any(mentions(s.summary, o) for o in options)
    )
    return list(chosen.values())


def _cited(
    raw_ids: Sequence[str], context: Mapping[UUID, ContextSignal], counts: Counter[str]
) -> tuple[UUID, ...]:
    cited: list[UUID] = []
    for raw in raw_ids:
        try:
            signal_id = UUID(raw)
        except ValueError:
            counts["suggestion_signal_dropped_unknown"] += 1
            continue
        signal = context.get(signal_id)
        if signal is None or signal.status != "active":
            counts["suggestion_signal_dropped_unknown"] += 1
        elif signal_id not in cited:
            cited.append(signal_id)
    return tuple(cited)


def _deduction(
    value: int | None, *, cap: int | None, has_evidence: bool, counts: Counter[str]
) -> int | None:
    if value is None:
        return None
    if not DEDUCTION_MIN <= value <= DEDUCTION_MAX or (cap is not None and value > cap):
        counts["suggestion_deduction_out_of_bounds"] += 1
        return None
    if not has_evidence:
        counts["suggestion_deduction_without_evidence"] += 1
        return None
    return value


def _text(raw: str, *, field_name: str, limit: int, counts: Counter[str]) -> str:
    masked, masks = mask(raw)
    if masks:
        counts[f"pii_masked_suggestion_{field_name}"] += 1
    text = normalise(masked)
    if len(text) > limit:
        counts[f"suggestion_{field_name}_too_long"] += 1
        return ""
    return text


def _tags(raw: Sequence[str], registry: TagRegistry, counts: Counter[str]) -> tuple[str, ...]:
    kept: list[str] = []
    for slug in raw:
        if slug in registry.retired:
            counts["suggestion_tag_dropped_retired"] += 1
        elif slug not in registry.active or not is_well_formed(slug):
            counts["suggestion_tag_dropped_unknown"] += 1
        elif slug not in kept:
            kept.append(slug)
    return tuple(kept)


def _new_tag(
    raw: ProposedTag | None, *, fits: bool, registered: frozenset[str], counts: Counter[str]
) -> SuggestedTag | None:
    if raw is None:
        return None
    if fits:
        counts["suggestion_new_tag_dropped_a_tag_fits"] += 1
        return None
    label = _text(raw.label, field_name="new_tag_label", limit=MAX_TAG_LABEL_CHARS, counts=counts)
    if not is_well_formed(raw.slug) or raw.slug in registered or not label:
        counts["suggestion_new_tag_dropped_invalid"] += 1
        return None
    return SuggestedTag(slug=raw.slug, label=label, kind=raw.kind)


def validate_suggestion(
    output: SuggestionOutput,
    *,
    options: Sequence[str],
    cap: int | None,
    context: Mapping[UUID, ContextSignal],
    vocabulary: ScoringVocabulary,
    counts: Counter[str],
) -> list[OptionSuggestion]:
    """One entry per requested option, in the request's order (§4.5, §4.6). ``context`` is the
    evidence the model was shown: an id outside it is dropped, never trusted."""
    by_option: dict[str, SuggestedOptionWeight] = {}
    for item in output.options:
        if item.option not in options:
            counts["suggestion_option_unknown"] += 1
        elif item.option in by_option:
            counts["suggestion_option_duplicate"] += 1
        else:
            by_option[item.option] = item
    registry = vocabulary.registry()
    registered = registry.active | registry.retired
    suggestions: list[OptionSuggestion] = []
    for option in options:
        chosen = by_option.get(option)
        if chosen is None:
            counts["suggestion_option_missing"] += 1
            suggestions.append(OptionSuggestion(option, None, "", (), (), None))
            continue
        signal_ids = _cited(chosen.signal_ids, context, counts)
        tags = _tags(chosen.suggested_tags, registry, counts)
        suggestions.append(
            OptionSuggestion(
                option=option,
                deduction=_deduction(
                    chosen.deduction, cap=cap, has_evidence=bool(signal_ids), counts=counts
                ),
                rationale=_text(
                    chosen.rationale,
                    field_name="rationale",
                    limit=MAX_RATIONALE_CHARS,
                    counts=counts,
                ),
                signal_ids=signal_ids,
                suggested_tags=tags,
                new_tag=_new_tag(
                    chosen.new_tag, fits=bool(tags), registered=registered, counts=counts
                ),
            )
        )
    return suggestions


def _uuid(value: object) -> UUID | None:
    try:
        return UUID(str(value))
    except ValueError:
        return None


def suggestion_options(
    target: Mapping[str, Any], linked: Sequence[ContextSignal]
) -> list[dict[str, Any]]:
    """The per-option read of a stored weight_suggestion: its ``target.options`` plus
    ``corroborated`` and ``why_not``, each judged from that option's own cited signals only."""
    by_id = {s.signal_id: s for s in linked}
    rows: list[dict[str, Any]] = []
    for raw in target.get("options") or []:
        if not isinstance(raw, dict):
            continue
        cited = [i for value in raw.get("signal_ids") or [] if (i := _uuid(value)) is not None]
        active = [by_id[i] for i in cited if i in by_id and by_id[i].status == "active"]
        why: SuggestionWhyNot | None
        if not cited:
            why = "no_evidence"
        elif not active:
            why = "evidence_retracted"
        elif uncorroborated(active):
            why = "uncorroborated"
        else:
            why = None
        rows.append(
            {
                "option": raw.get("option"),
                "deduction": raw.get("deduction"),
                "rationale": raw.get("rationale", ""),
                "signal_ids": [str(i) for i in cited],
                "suggested_tags": raw.get("suggested_tags") or [],
                "new_tag": raw.get("new_tag"),
                "corroborated": why is None,
                "why_not": why,
            }
        )
    return rows


def prompt_suggestion_question(
    request: SuggestionRunRequest,
    tags_by_option: Mapping[str, Sequence[str]],
    vocabulary: ScoringVocabulary | None,
) -> PromptSuggestionQuestion:
    return PromptSuggestionQuestion(
        key=request.question_key,
        prompt=request.prompt,
        type=request.type,
        cap=request.cap,
        options=[
            PromptSuggestionOption(
                option=option,
                tags=list(tags_by_option.get(option, ())),
                live_deduction=(
                    vocabulary.deduction(request.question_key, option)
                    if vocabulary is not None
                    else None
                ),
            )
            for option in request.options
        ],
    )


def render_suggestion(run: Run, found: tuple[UUID, list[dict[str, Any]]] | None) -> dict[str, Any]:
    """GET /weight-suggestions/{run_id} (spec §4.6, §4.10). ``proposal_id`` and ``options`` are
    null until the run wrote a suggestion (``found``, from the store); ``sources_deferred``
    counts the sources left for their first scheduled check."""
    deferred = run.outcome.get("sources_deferred", 0)
    return {
        "run_id": run.run_id,
        "status": run.status,
        "proposal_id": found[0] if found is not None else None,
        "error_code": run.error_code,
        "options": found[1] if found is not None else None,
        "sources_deferred": deferred if isinstance(deferred, int) else 0,
    }
