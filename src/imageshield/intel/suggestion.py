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
from dataclasses import dataclass, replace
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from imageshield.intel.bounds import (
    DEDUCTION_MAX,
    DEDUCTION_MIN,
    MAX_FACTOR_LINE_CHARS,
    MAX_FACTOR_TITLE_CHARS,
    MAX_RATIONALE_CHARS,
    MAX_RECOMMENDATION_BODY_CHARS,
    MAX_RECOMMENDATION_LINK_CHARS,
    MAX_RECOMMENDATION_LINK_LABEL_CHARS,
    MAX_RECOMMENDATION_TITLE_CHARS,
    MAX_TAG_LABEL_CHARS,
)
from imageshield.intel.corroboration import uncorroborated
from imageshield.intel.models import Run
from imageshield.intel.pii import contains_pii, mask
from imageshield.intel.prompts import PromptSuggestionOption, PromptSuggestionQuestion
from imageshield.intel.proposal_models import ContextSignal, SuggestedTag
from imageshield.intel.schemas import (
    ProposedTag,
    SuggestedOptionWeight,
    SuggestedRecommendation,
    SuggestionOutput,
)
from imageshield.intel.source_choice import query_key
from imageshield.intel.tags import TagRegistry, is_well_formed
from imageshield.intel.text import normalise
from imageshield.intel.user_copy import copy_problems, unquoted_figures
from imageshield.intel.vocabulary import ScoringVocabulary, normalise_subject
from imageshield.search.urlhash import url_hash

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
class ResearchSearch:
    round: int
    query: str

    def as_json(self) -> dict[str, Any]:
        return {"round": self.round, "query": self.query}


@dataclass(frozen=True)
class OptionResearch:
    """What research did for one answer (spec 2026-10-10-intel-every-answer-researched §2.3).
    ``exhausted``: still unbacked after research ended. The backend reads the flag and never
    re-derives it."""

    rounds: int
    searches: tuple[ResearchSearch, ...]
    exhausted: bool

    def as_json(self) -> dict[str, Any]:
        return {
            "rounds": self.rounds,
            "searches": [s.as_json() for s in self.searches],
            "exhausted": self.exhausted,
        }


@dataclass(frozen=True)
class OptionSuggestion:
    option: str
    deduction: int | None
    rationale: str
    signal_ids: tuple[UUID, ...]
    suggested_tags: tuple[str, ...]
    new_tag: SuggestedTag | None
    # The model's research searches for this option (suggest-v2): an ask, never stored.
    research_searches: tuple[str, ...] = ()
    # What research did for it, attached before the write (spec 2026-10-10 §2.3).
    research: OptionResearch | None = None
    # suggest-v3 (spec 2026-10-11-intel-score-insights §2): the risk-factor words, "" when none.
    factor_title: str = ""
    factor_line: str = ""

    def as_json(self) -> dict[str, Any]:
        """One entry of the proposal's ``target.options`` (spec §3.6)."""
        out: dict[str, Any] = {
            "option": self.option,
            "deduction": self.deduction,
            "rationale": self.rationale,
            "signal_ids": [str(s) for s in self.signal_ids],
            "suggested_tags": list(self.suggested_tags),
            "new_tag": self.new_tag.model_dump() if self.new_tag is not None else None,
            "factor_title": self.factor_title,
            "factor_line": self.factor_line,
        }
        if self.research is not None:
            out["research"] = self.research.as_json()
        return out

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> OptionSuggestion:
        """A kept pass's entry back to an OptionSuggestion (spec 2026-10-10 §2.2.7)."""
        new_tag = raw.get("new_tag")
        return cls(
            option=str(raw["option"]),
            deduction=raw.get("deduction"),
            rationale=str(raw.get("rationale", "")),
            signal_ids=tuple(UUID(str(s)) for s in raw.get("signal_ids") or ()),
            suggested_tags=tuple(str(t) for t in raw.get("suggested_tags") or ()),
            new_tag=SuggestedTag(**new_tag) if isinstance(new_tag, dict) else None,
            factor_title=str(raw.get("factor_title", "")),
            factor_line=str(raw.get("factor_line", "")),
        )


@dataclass(frozen=True)
class GeneralRecommendation:
    """A question's drafted general recommendation (spec 2026-10-11-intel-score-insights §2), as
    stored in ``target.recommendation``."""

    title: str
    body: str
    link_url: str | None
    link_label: str | None
    signal_ids: tuple[UUID, ...]

    def as_json(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "body": self.body,
            "link_url": self.link_url,
            "link_label": self.link_label,
            "signal_ids": [str(s) for s in self.signal_ids],
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any] | None) -> GeneralRecommendation | None:
        if not isinstance(raw, Mapping):
            return None
        return cls(
            title=str(raw["title"]),
            body=str(raw["body"]),
            link_url=raw.get("link_url"),
            link_label=raw.get("link_label"),
            signal_ids=tuple(UUID(str(s)) for s in raw.get("signal_ids") or ()),
        )


@dataclass(frozen=True)
class SuggestionCandidates:
    """The four retrieval classes as the store reads them, newest first each."""

    from_sources: tuple[ContextSignal, ...] = ()
    by_tag: tuple[ContextSignal, ...] = ()
    with_subjects: tuple[ContextSignal, ...] = ()
    by_category: tuple[ContextSignal, ...] = ()
    # The question's research searches' evidence (spec 2026-10-10 §2.2; final review C1,
    # 2026-10-11): its own class, taken FIRST, so research evidence reaches the model however much
    # else there is, and never displaces the operator's sources by sharing their class.
    from_research: tuple[ContextSignal, ...] = ()


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

    take(candidates.from_research)
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


def _words(raw: str, counts: Counter[str]) -> str:
    masked, masks = mask(raw)
    if masks:
        counts["pii_masked_suggestion_words"] += 1
    return normalise(masked)


def _quotes(cited: Iterable[ContextSignal]) -> list[str]:
    return [quote for signal in cited for quote in signal.excerpts]


def _factor_pair(
    chosen: SuggestedOptionWeight,
    *,
    deduction: int | None,
    cited: Sequence[ContextSignal],
    counts: Counter[str],
) -> tuple[str, str]:
    """The risk-factor words (spec 2026-10-11-intel-score-insights §4), kept or dropped as a pair.
    The option itself is always kept."""
    if not deduction:
        if chosen.factor_title or chosen.factor_line:
            counts["factor_line_no_deduction"] += 1
        return "", ""
    if not chosen.factor_title and not chosen.factor_line:
        return "", ""  # nothing drafted: an answer without words, not a dropped pair
    title, line = _words(chosen.factor_title, counts), _words(chosen.factor_line, counts)
    if not title or not line:
        counts["factor_line_missing"] += 1
        return "", ""
    if len(title) > MAX_FACTOR_TITLE_CHARS or len(line) > MAX_FACTOR_LINE_CHARS:
        counts["factor_line_too_long"] += 1
        return "", ""
    if copy_problems(title) or copy_problems(line):
        counts["factor_line_forbidden_words"] += 1
        return "", ""
    if unquoted_figures(f"{title} {line}", _quotes(cited)):
        counts["factor_line_figure_not_quoted"] += 1
        return "", ""
    return title, line


def validate_recommendation(
    raw: SuggestedRecommendation | None,
    *,
    context: Mapping[UUID, ContextSignal],
    counts: Counter[str],
) -> GeneralRecommendation | None:
    """A question's drafted general recommendation (spec 2026-10-11-intel-score-insights §4), or
    None. The link follows the threat-action rule: kept only when https and a cited page."""
    if raw is None:
        return None
    signal_ids = _cited(raw.signal_ids, context, counts)
    if not signal_ids:
        counts["recommendation_dropped_no_evidence"] += 1
        return None
    title, body = _words(raw.title, counts), _words(raw.body, counts)
    if (
        not title
        or not body
        or len(title) > MAX_RECOMMENDATION_TITLE_CHARS
        or len(body) > MAX_RECOMMENDATION_BODY_CHARS
    ):
        counts["recommendation_dropped_out_of_bounds"] += 1
        return None
    if copy_problems(title) or copy_problems(body):
        counts["recommendation_dropped_forbidden_words"] += 1
        return None
    cited = [context[i] for i in signal_ids]
    if unquoted_figures(f"{title} {body}", _quotes(cited)):
        counts["recommendation_dropped_figure_not_quoted"] += 1
        return None
    link: str | None = None
    label: str | None = None
    raw_link = (raw.link_url or "").strip()
    if raw_link:
        pages = {s.document_key for s in cited if s.document_key is not None}
        if (
            raw_link.startswith("https://")
            and len(raw_link) <= MAX_RECOMMENDATION_LINK_CHARS
            and url_hash(raw_link) in pages
        ):
            link = raw_link
            # The label is shown beside the link, so it is held to the same rules (final review I3,
            # 2026-10-12). A bad label is dropped and counted; the link stays.
            text = _words(raw.link_label or "", counts)
            if text and (
                len(text) > MAX_RECOMMENDATION_LINK_LABEL_CHARS
                or copy_problems(text)
                or unquoted_figures(text, _quotes(cited))
            ):
                counts["recommendation_link_label_dropped"] += 1
                text = ""
            label = text or None
        else:
            counts["recommendation_link_not_cited"] += 1
    return GeneralRecommendation(title, body, link, label, signal_ids)


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
        deduction = _deduction(
            chosen.deduction, cap=cap, has_evidence=bool(signal_ids), counts=counts
        )
        factor_title, factor_line = _factor_pair(
            chosen, deduction=deduction, cited=[context[i] for i in signal_ids], counts=counts
        )
        suggestions.append(
            OptionSuggestion(
                option=option,
                deduction=deduction,
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
                research_searches=tuple(chosen.research_searches),
                factor_title=factor_title,
                factor_line=factor_line,
            )
        )
    return suggestions


def is_backed(option: OptionSuggestion, context: Mapping[UUID, ContextSignal]) -> bool:
    """Backed (spec 2026-10-10-intel-every-answer-researched §2): a number, and its cited
    evidence passes the one corroboration predicate -- the judgement ``suggestion_options``
    reports as ``corroborated``."""
    if option.deduction is None or not option.signal_ids:
        return False
    active = [
        context[i] for i in option.signal_ids if i in context and context[i].status == "active"
    ]
    return bool(active) and not uncorroborated(active)


def plan_research(
    options: Sequence[OptionSuggestion],
    *,
    searched: Mapping[str, Sequence[Mapping[str, Any]]],
    per_answer: int,
    counts: Counter[str],
) -> dict[str, list[str]]:
    """Each unbacked answer's next searches (spec 2026-10-10 §2.2.3): the model's own, at most
    ``per_answer``, never one that names a person, never one this run already made."""
    seen = {query_key(str(s["query"])) for entries in searched.values() for s in entries}
    planned: dict[str, list[str]] = {}
    for option in options:
        kept: list[str] = []
        for raw in option.research_searches:
            query = normalise(raw)
            if not query:
                continue
            if contains_pii(query):
                counts["research_search_names_a_person"] += 1
                continue
            key = query_key(query)
            if key in seen:
                counts["research_search_repeated"] += 1
                continue
            if len(kept) >= per_answer:
                counts["research_search_over_cap"] += 1
                continue
            seen.add(key)
            kept.append(query)
        if kept:
            planned[option.option] = kept
    return planned


def keep_backed(
    options: Sequence[OptionSuggestion],
    *,
    backed: Mapping[str, bool],
    kept: Sequence[Mapping[str, Any]] | None,
    kept_backed: Mapping[str, bool],
    counts: Counter[str],
) -> tuple[list[OptionSuggestion], dict[str, bool]]:
    """An answer an earlier pass backed stays backed (final review C1, 2026-10-11): a later pass
    that leaves it unbacked -- its evidence crowded out of the context by research -- gets the
    earlier entry back, and it is never researched again."""
    merged = list(options)
    now = dict(backed)
    if kept is None:
        return merged, now
    earlier = {str(o.get("option")): o for o in kept}
    for index, option in enumerate(merged):
        entry = earlier.get(option.option)
        if not now.get(option.option) and kept_backed.get(option.option) and entry is not None:
            merged[index] = OptionSuggestion.from_json(entry)
            now[option.option] = True
            counts["research_kept_backed_answer"] += 1
    return merged, now


def attach_research(
    options: Sequence[OptionSuggestion],
    *,
    backed: Mapping[str, bool],
    searched: Mapping[str, Sequence[Mapping[str, Any]]],
    researching: bool,
) -> list[OptionSuggestion]:
    """Each option with its research record (spec 2026-10-10 §2.3). ``researching``: research
    rounds were configured, so an answer still unbacked is ``exhausted``; with research off,
    none is."""
    out: list[OptionSuggestion] = []
    for option in options:
        entries = tuple(
            ResearchSearch(int(s["round"]), str(s["query"]))
            for s in searched.get(option.option, ())
        )
        out.append(
            replace(
                option,
                research=OptionResearch(
                    rounds=len({s.round for s in entries}),
                    searches=entries,
                    # Only after a search ran for it (final review I2, 2026-10-11): an
                    # answer nothing was searched for is not "researched, nothing found".
                    exhausted=researching
                    and bool(entries)
                    and not backed.get(option.option, False),
                ),
            )
        )
    return out


def _uuid(value: object) -> UUID | None:
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _why_not(
    raw_ids: Iterable[object], by_id: Mapping[UUID, ContextSignal]
) -> tuple[list[UUID], SuggestionWhyNot | None]:
    cited = [i for value in raw_ids if (i := _uuid(value)) is not None]
    active = [by_id[i] for i in cited if i in by_id and by_id[i].status == "active"]
    if not cited:
        return cited, "no_evidence"
    if not active:
        return cited, "evidence_retracted"
    if uncorroborated(active):
        return cited, "uncorroborated"
    return cited, None


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
        cited, why = _why_not(raw.get("signal_ids") or [], by_id)
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
                # What research did for this answer (spec 2026-10-10 §2.3); absent before it.
                "research": raw.get("research"),
                # The risk-factor words (spec 2026-10-11-intel-score-insights §5); "" before them.
                "factor_title": raw.get("factor_title", ""),
                "factor_line": raw.get("factor_line", ""),
            }
        )
    return rows


def suggestion_recommendation(
    target: Mapping[str, Any], linked: Sequence[ContextSignal]
) -> dict[str, Any] | None:
    """The read of a stored suggestion's general recommendation (spec
    2026-10-11-intel-score-insights §5), judged on its own cited signals with the options' rule, or
    None when it has none."""
    raw = target.get("recommendation")
    if not isinstance(raw, dict):
        return None
    cited, why = _why_not(raw.get("signal_ids") or [], {s.signal_id: s for s in linked})
    return {
        "title": raw.get("title"),
        "body": raw.get("body"),
        "link_url": raw.get("link_url"),
        "link_label": raw.get("link_label"),
        "signal_ids": [str(i) for i in cited],
        "corroborated": why is None,
        "why_not": why,
    }


def prompt_suggestion_question(
    request: SuggestionRunRequest,
    tags_by_option: Mapping[str, Sequence[str]],
    vocabulary: ScoringVocabulary | None,
    *,
    searched: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
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
                searched=[str(s["query"]) for s in (searched or {}).get(option, ())],
            )
            for option in request.options
        ],
    )


def render_suggestion(
    run: Run, found: tuple[UUID, list[dict[str, Any]], dict[str, Any] | None] | None
) -> dict[str, Any]:
    """GET /weight-suggestions/{run_id} (spec §4.6, §4.10). ``proposal_id`` and ``options`` are
    null until the run wrote a suggestion (``found``, from the store); ``sources_deferred``
    counts the sources this run could not read itself.

    *Amended 2026-10-08 (spec 2026-10-08-intel-suggestion-waits-for-evidence §4):* while the run
    waits for those reads, ``awaiting_sources`` says how many and ``wait_deadline`` until when at
    most; both are 0 / null once it has resumed or ended."""
    deferred = run.outcome.get("sources_deferred", 0)
    waiting = run.status == "queued" and run.awaiting_source_ids is not None
    return {
        "run_id": run.run_id,
        "status": run.status,
        "proposal_id": found[0] if found is not None else None,
        "error_code": run.error_code,
        "options": found[1] if found is not None else None,
        # The drafted general recommendation (spec 2026-10-11-intel-score-insights §5), or None.
        "recommendation": found[2] if found is not None else None,
        "sources_deferred": deferred if isinstance(deferred, int) else 0,
        "research_round": run.research_round,
        "awaiting_sources": len(run.awaiting_source_ids or []) if waiting else 0,
        "wait_deadline": run.wait_deadline if waiting else None,
    }
