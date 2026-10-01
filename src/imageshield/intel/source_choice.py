"""Choosing sources for one quiz question (spec §4.10). Pure: no database, no fetcher, no model.

Stage 1, proposing. The model names candidates per option, and code decides which survive: https
only, canonical (search/urlhash.py), deduplicated within the option and against the option's
existing registry sources, and never a person-shaped query. The run then drops known hit locations
(a database read) and caps each option at MAX_PROPOSED_SOURCES_PER_OPTION. An option's "existing"
sources are the registry sources whose tags intersect the option's tags.

Identity. A source is one canonical URL (its url_hash) or one search query (normalised and
case-folded), whatever kind it was proposed as: the registry reuses a row by that. A validation
result is keyed by kind AND identity, because the text floor depends on the kind.

Stage 3, validating. Code alone decides: a known hit location, the fetch (https on every hop,
robots.txt honoured), and then the text floor for the kind -- or, for a feed, at least one item.
A search_query's one web search only supplies pages for the same checks. Every reason is a
lowercase token (BLOCKED_REASONS), and the transient ones mean "check again later".

A candidate is model-written at stage 1, so its reason is masked (§6.3) and bounded. Nothing here
is registered: the operator chooses at stage 2, and stage 4 registers only what stage 3 passed.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from imageshield.intel.bounds import (
    MAX_CANDIDATE_REASON_CHARS,
    MAX_QUERY_TEXT_CHARS,
    MIN_POLICY_TEXT_CHARS,
    MIN_SOURCE_TEXT_CHARS,
    VALIDATION_TTL_HOURS,
)
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


# ── stage 3: validation (spec §4.10) ─────────────────────────────────────────

# Verdicts about the source itself.
SOURCE_VERDICTS: frozenset[str] = frozenset(
    {
        "known_hit_location",
        "not_https",
        "unreachable",
        "unsupported_type",
        "robots_disallowed",
        "robots_unreachable",
        "too_short",
        "no_items",
        "query_names_a_person",
        "no_results",
    }
)
# Says nothing about the source: check again later. The last four are the provider gate's own.
TRANSIENT_REASONS: frozenset[str] = frozenset(
    {
        "search_unavailable",
        "fetcher_unavailable",
        "run_call_cap",
        "budget_exceeded",
        "breaker_open",
        "provider_disabled",
        "budget_unset",
    }
)
BLOCKED_REASONS: frozenset[str] = SOURCE_VERDICTS | TRANSIENT_REASONS

# The fetcher client's failure codes, as a validation reason.
FETCH_REASONS: dict[str, str] = {
    "robots_disallowed": "robots_disallowed",
    "robots_unreachable": "robots_unreachable",
    "not_https": "not_https",
    "unsupported_type": "unsupported_type",
    "refused_private_address": "unreachable",
    "redirect_limit": "unreachable",
    "unfetchable": "unreachable",
    "too_large": "unreachable",
    "fetcher_unreachable": "fetcher_unavailable",
    "fetcher_error": "fetcher_unavailable",
}


class ValidationCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    option: str
    kind: str
    source_url: str | None = None
    query_text: str | None = None

    def as_json(self) -> dict[str, Any]:
        """The candidate exactly as submitted: what a result echoes."""
        return {
            "option": self.option,
            "kind": self.kind,
            "source_url": self.source_url,
            "query_text": self.query_text,
        }


class ValidationRequest(BaseModel):
    """A source_validation run's stored request, as POST /source-validations writes it."""

    model_config = ConfigDict(frozen=True)

    candidates: tuple[ValidationCandidate, ...]


def url_verdict(kind: str, text: str, items: Sequence[object] | None) -> str | None:
    """After a successful fetch; ``text`` is already normalised. A feed must list at least one
    item (its text is a listing the fetcher builds, so no text floor applies to it); a policy
    page must reach MIN_POLICY_TEXT_CHARS; anything else, a search result included,
    MIN_SOURCE_TEXT_CHARS. None means ready."""
    if kind == "feed":
        return None if items else "no_items"
    floor = MIN_POLICY_TEXT_CHARS if kind == "policy_page" else MIN_SOURCE_TEXT_CHARS
    return None if len(text) >= floor else "too_short"


def render_validation(run: Run) -> dict[str, Any]:
    """GET /source-validations/{run_id}: an object, never a bare list. ``results`` answers the
    submitted candidates one for one and in order, and is null until the run completes;
    ``honoured_until`` is when stage 4 stops accepting them (§4.10)."""
    results = run.outcome.get("results")
    completed = run.status == "completed" and run.completed_at is not None
    return {
        "run_id": run.run_id,
        "status": run.status,
        "error_code": run.error_code,
        "results": results if isinstance(results, list) else None,
        "honoured_until": (
            run.completed_at + timedelta(hours=VALIDATION_TTL_HOURS)
            if completed and run.completed_at is not None
            else None
        ),
    }


# ── stage 4: registration (spec §4.10) ───────────────────────────────────────

# Why a chosen source is not validated: the closed set of `source_not_validated` entry reasons.
NOT_VALIDATED_REASONS: frozenset[str] = frozenset({"unknown_run", "expired", "not_ready"})


@dataclass(frozen=True)
class Validation:
    """A completed source_validation run, as stage 4 checks it: when it completed, and the
    (kind, identity) of every candidate it found ready."""

    completed_at: datetime
    ready: frozenset[CandidateKey]


def ready_keys(outcome: Mapping[str, Any]) -> frozenset[CandidateKey]:
    keys: set[CandidateKey] = set()
    results = outcome.get("results")
    for result in results if isinstance(results, list) else []:
        if not isinstance(result, dict) or result.get("status") != "ready":
            continue
        candidate = result.get("candidate")
        if isinstance(candidate, dict) and isinstance(candidate.get("kind"), str):
            keys.add(
                candidate_key(
                    candidate["kind"], candidate.get("source_url"), candidate.get("query_text")
                )
            )
    return frozenset(keys)


def proposal_identities(outcome: Mapping[str, Any]) -> frozenset[str]:
    """Every candidate a source-proposal run proposed, for any option, by identity: a chosen
    source among them registers with origin 'suggested'."""
    found: set[str] = set()
    options = outcome.get("options")
    for option in options if isinstance(options, list) else []:
        proposed = option.get("proposed") if isinstance(option, dict) else None
        for candidate in proposed if isinstance(proposed, list) else []:
            if isinstance(candidate, dict) and isinstance(candidate.get("kind"), str):
                found.add(
                    identity(
                        candidate["kind"], candidate.get("source_url"), candidate.get("query_text")
                    )
                )
    return frozenset(found)


class _Chosen(Protocol):
    @property
    def kind(self) -> str: ...
    @property
    def source_url(self) -> str | None: ...
    @property
    def query_text(self) -> str | None: ...
    @property
    def validation_run_id(self) -> UUID: ...


def validation_problems(
    sources: Sequence[_Chosen], validations: Mapping[UUID, Validation], *, now: datetime
) -> list[dict[str, Any]]:
    """spec §4.10 stage 4: each chosen source must be ready, as its kind, in its validation run,
    and that run must have completed within VALIDATION_TTL_HOURS. The failing entries, as
    ``{index, reason}`` in ascending index. They name nothing the request sent: the envelope is
    what the backend logs (http/errors.py)."""
    horizon = now - timedelta(hours=VALIDATION_TTL_HOURS)
    problems: list[dict[str, Any]] = []
    for index, source in enumerate(sources):
        validation = validations.get(source.validation_run_id)
        key = candidate_key(source.kind, source.source_url, source.query_text)
        if validation is None:
            reason = "unknown_run"
        elif validation.completed_at < horizon:
            reason = "expired"
        elif key not in validation.ready:
            reason = "not_ready"
        else:
            continue
        problems.append({"index": index, "reason": reason})
    return problems


@dataclass(frozen=True)
class NewSource:
    """One chosen source, ready to register, or to reuse the registry row with its identity.
    ``source_url`` is canonical and ``query_text`` normalised."""

    kind: str
    source_url: str | None
    query_text: str | None
    tags: tuple[str, ...]
    check_every_hours: int
    terms_note: str
    origin: str
    question_key: str
    option: str

    @property
    def identity(self) -> str:
        return identity(self.kind, self.source_url, self.query_text)


def merge_by_identity(sources: Sequence[NewSource]) -> list[NewSource]:
    """One source per identity, in first-seen order. A source chosen for two options registers
    once and carries both options' tags; its other fields are the first choice's."""
    merged: dict[str, NewSource] = {}
    for source in sources:
        first = merged.get(source.identity)
        if first is None:
            merged[source.identity] = source
        else:
            extra = tuple(t for t in source.tags if t not in first.tags)
            merged[source.identity] = replace(first, tags=first.tags + extra)
    return list(merged.values())


@dataclass(frozen=True)
class Registered:
    run_id: UUID
    registered: tuple[UUID, ...]
    reused: tuple[UUID, ...]
