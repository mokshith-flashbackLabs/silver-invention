"""Choosing sources for one quiz question (spec §4.10). Pure: no database, no fetcher, no model.

Stage 1, proposing. The model names candidates per option, and code decides which survive: https
only, canonical (search/urlhash.py), deduplicated within the option and against the option's
existing registry sources, and never a person-shaped query. The run then drops known hit locations
(a database read) and caps each option at MAX_PROPOSED_SOURCES_PER_OPTION. An option's "existing"
sources are the registry sources whose tags intersect the option's tags.

Identity. A source is one canonical URL (its url_hash) or one search query (normalised and
case-folded), whatever kind it was proposed as: the registry reuses a row by that. A validation
result is keyed by kind AND identity, because the text floor depends on the kind.

A candidate is model-written, so its reason is masked (§6.3) and bounded. Nothing here is
registered: the operator chooses at stage 2, and code checks at stage 3.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from imageshield.intel.bounds import MAX_CANDIDATE_REASON_CHARS, MAX_QUERY_TEXT_CHARS
from imageshield.intel.models import Run, Source
from imageshield.intel.pii import contains_pii, mask
from imageshield.intel.prompts import PromptSourceOption, PromptSourceQuestion
from imageshield.intel.schemas import ProposedSource, SourceProposalOutput
from imageshield.intel.text import normalise
from imageshield.intel.vocabulary import ScoringVocabulary
from imageshield.search.urlhash import canonicalise, url_hash

CandidateKey = tuple[str, str]


def is_https(url: str) -> bool:
    try:
        return urlsplit(url.strip()).scheme.lower() == "https"
    except ValueError:
        return False


def query_key(query_text: str) -> str:
    return normalise(query_text).casefold()


def identity(kind: str, source_url: str | None, query_text: str | None) -> str:
    """The registry's reuse key: a canonical URL's hash, or ``q:`` and the normalised query."""
    if kind == "search_query":
        return "q:" + query_key(query_text or "")
    return str(url_hash(source_url or ""))


def candidate_key(kind: str, source_url: str | None, query_text: str | None) -> CandidateKey:
    return (kind, identity(kind, source_url, query_text))


def source_identity(source: Source) -> str:
    return identity(source.kind, source.source_url, source.query_text)


@dataclass(frozen=True)
class Candidate:
    kind: str
    source_url: str | None
    query_text: str | None
    reason: str

    @property
    def identity(self) -> str:
        return identity(self.kind, self.source_url, self.query_text)

    def as_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source_url": self.source_url,
            "query_text": self.query_text,
            "reason": self.reason,
        }


class QuestionRequest(BaseModel):
    """A source_proposal run's stored request, as POST /source-proposals writes it."""

    model_config = ConfigDict(frozen=True)

    question_key: str
    prompt: str
    options: tuple[str, ...]
    tags: dict[str, tuple[str, ...]] | None = None


def option_tags(
    option: str,
    *,
    question_key: str,
    request_tags: Mapping[str, Sequence[str]] | None,
    vocabulary: ScoringVocabulary | None,
) -> tuple[str, ...]:
    """spec §4.6: when the request carries tags, even ``{}``, they replace the vocabulary's map
    for this run, because the draft is authoritative for the draft. The cached map is used only
    when the field is absent."""
    if request_tags is not None:
        return tuple(request_tags.get(option, ()))
    if vocabulary is None:
        return ()
    return vocabulary.option_tags.get((question_key, option), ())


def prompt_source_question(
    request: QuestionRequest, tags_by_option: Mapping[str, Sequence[str]]
) -> PromptSourceQuestion:
    return PromptSourceQuestion(
        key=request.question_key,
        prompt=request.prompt,
        options=[
            PromptSourceOption(option=option, tags=list(tags_by_option.get(option, ())))
            for option in request.options
        ],
    )


def _reason(raw: str, counts: Counter[str]) -> str:
    masked, masks = mask(raw)
    if masks:
        counts["pii_masked_candidate_reason"] += 1
    return normalise(masked)[:MAX_CANDIDATE_REASON_CHARS]


def _clean_one(raw: ProposedSource, counts: Counter[str]) -> Candidate | None:
    if raw.kind == "search_query":
        query = normalise(raw.query_text or "")
        if not query or raw.source_url:
            counts["candidate_dropped_malformed"] += 1
            return None
        if len(query) > MAX_QUERY_TEXT_CHARS:
            counts["candidate_dropped_query_too_long"] += 1
            return None
        if contains_pii(query):
            counts["candidate_dropped_query_names_a_person"] += 1
            return None
        return Candidate(raw.kind, None, query, _reason(raw.reason, counts))
    url = (raw.source_url or "").strip()
    if not url or raw.query_text:
        counts["candidate_dropped_malformed"] += 1
        return None
    if not is_https(url):
        counts["candidate_dropped_not_https"] += 1
        return None
    return Candidate(raw.kind, canonicalise(url), None, _reason(raw.reason, counts))


def clean_candidates(
    output: SourceProposalOutput,
    *,
    options: Sequence[str],
    existing: Mapping[str, frozenset[str]],
    counts: Counter[str],
) -> dict[str, list[Candidate]]:
    """Per requested option, in the model's order: the candidates that survive shape, https, the
    PII check and deduplication, within the option and against ``existing`` (the identities of
    the option's registry sources). An option the request did not name is dropped whole."""
    cleaned: dict[str, list[Candidate]] = {option: [] for option in options}
    seen: dict[str, set[str]] = {option: set(existing.get(option, ())) for option in options}
    for group in output.options:
        if group.option not in cleaned:
            counts["candidate_dropped_unknown_option"] += len(group.candidates)
            continue
        for raw in group.candidates:
            candidate = _clean_one(raw, counts)
            if candidate is None:
                continue
            if candidate.identity in seen[group.option]:
                counts["candidate_dropped_duplicate"] += 1
                continue
            seen[group.option].add(candidate.identity)
            cleaned[group.option].append(candidate)
    return cleaned


def _uuid(value: object) -> UUID | None:
    try:
        return UUID(str(value))
    except ValueError:
        return None


def existing_source_ids(run: Run) -> list[UUID]:
    """Every source id a source-proposal run listed as existing, in order, once each."""
    ids: list[UUID] = []
    options = run.outcome.get("options")
    for option in options if isinstance(options, list) else []:
        values = option.get("existing") if isinstance(option, dict) else None
        for raw in values or []:
            parsed = _uuid(raw)
            if parsed is not None and parsed not in ids:
                ids.append(parsed)
    return ids


def render_source_proposal(run: Run, sources: Mapping[UUID, Source]) -> dict[str, Any]:
    """GET /source-proposals/{run_id} (spec §4.10). ``options`` is null until the run finished.
    ``existing`` is re-read from the registry, so it shows each source as it is now."""
    raw = run.outcome.get("options")
    options: list[dict[str, Any]] | None = None
    if isinstance(raw, list):
        options = []
        for option in raw:
            if not isinstance(option, dict):
                continue
            existing = [
                sources[source_id]
                for value in option.get("existing") or []
                if (source_id := _uuid(value)) is not None and source_id in sources
            ]
            options.append(
                {
                    "option": option.get("option"),
                    "tags": option.get("tags") or [],
                    "existing": existing,
                    "proposed": option.get("proposed") or [],
                }
            )
    return {
        "run_id": run.run_id,
        "status": run.status,
        "error_code": run.error_code,
        "options": options,
    }
