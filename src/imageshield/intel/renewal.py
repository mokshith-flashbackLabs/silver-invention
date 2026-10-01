"""Protection renewal, pure (spec §4.8, INVARIANTS #49).

A credit near its review date is continued only by evidence checked again now. The worker fetches
the pages behind the credit's cited excerpts again (intel/pipeline.py) and hands them here; this
module decides, with no model and no I/O, which excerpts still appear verbatim and what the
renewal an operator must approve looks like.

- An excerpt is re-cited only if it is still a verbatim substring of the page fetched now, by the
  same verify_quote every extraction uses. One that is not is dropped, never carried forward.
- A signal with no surviving excerpt is not renewed: a signal with no verified excerpt does not
  exist (#49).
- The renewal's new signals copy the old ones' category, direction, tags, subjects, summary,
  model_id and prompt_version (that model wrote that text), under new documents with trust =
  listed: these are the pages an operator already approved on (§4.8).
- The proposal carries the credit's own scope and values forward, a global scope an operator
  chose included, and names what it renews. It still needs a fresh approval and a fresh location
  attestation (§4.5).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from imageshield.intel.evidence_store import DocumentRecord, SignalRecord
from imageshield.intel.proposal_models import ProtectionEventSuggested, ProtectionEventTarget
from imageshield.intel.publisher import publisher_domain
from imageshield.intel.text import content_sha256
from imageshield.intel.verify import VerifiedQuote, verify_quote
from imageshield.search.urlhash import canonicalise, url_hash

# A renewal is written by code, never by a model: these stand in the columns a generated proposal
# fills with the model that answered and its prompt (spec note 2026-09-30).
RENEWAL_WRITER = "code:renewal"
RENEWAL_VERSION = "renewal-v1"
# A page the fetcher could not reach says nothing about its text, so a renewal that verified
# nothing because of one is retried. Every other reason is the evidence's (spec note 2026-09-30).
TRANSIENT_PAGE_FAILURES = frozenset({"fetch_unfetchable"})

SignalCategory = Literal["policy", "incident", "tooling", "protection", "law", "research"]
SignalDirection = Literal["risk_up", "risk_down", "neutral"]


@dataclass(frozen=True)
class RenewalExcerpt:
    excerpt_id: UUID
    quote_text: str


@dataclass(frozen=True)
class RenewalSignal:
    """One active signal behind the credit, with the page it was read from."""

    signal_id: UUID
    category: SignalCategory
    direction: SignalDirection
    tags: tuple[str, ...]
    unregistered_subjects: tuple[str, ...]
    summary: str
    model_id: str
    prompt_version: str
    document_url: str  # canonical, as first requested
    document_url_hash: str  # the key the fetched-again pages are held under
    title: str  # masked when it was first recorded
    published_at: datetime | None
    excerpts: tuple[RenewalExcerpt, ...]


@dataclass(frozen=True)
class RenewalEvidence:
    """A credit due for renewal, and every active signal its approval rested on."""

    event_id: UUID
    title: str
    strength: int
    tags: tuple[str, ...]
    is_global: bool
    starts_at: datetime
    review_by: datetime
    signals: tuple[RenewalSignal, ...]

    @property
    def review_in_days(self) -> int:
        """The credit's own period in whole days. Rounded, never floored: starts_at +
        make_interval(days => n) can be an hour short of n days across a daylight-saving
        change in the session's time zone."""
        return round((self.review_by - self.starts_at) / timedelta(days=1))


@dataclass(frozen=True)
class RenewalPage:
    """A page fetched again: ``text`` is normalised, bounded and UNMASKED, what every quote is
    verified against, as in extraction."""

    requested_url: str
    final_url: str
    text: str
    truncated: bool


@dataclass(frozen=True)
class RenewedSignal:
    original: RenewalSignal
    page: RenewalPage
    quotes: tuple[VerifiedQuote, ...]


@dataclass(frozen=True)
class RenewalPlan:
    signals: tuple[RenewedSignal, ...]
    excerpts_checked: int
    excerpts_verified: int
    dropped: Mapping[str, int]
    unreachable: bool


def plan_renewal(evidence: RenewalEvidence, pages: Mapping[str, RenewalPage | str]) -> RenewalPlan:
    """``pages`` maps each signal's ``document_url_hash`` to the page fetched again, or to why
    it could not be read (``fetch_<code>``, ``known_hit_location``, ``not_https``)."""
    renewed: list[RenewedSignal] = []
    dropped: Counter[str] = Counter()
    checked = 0
    unreachable = False
    for signal in evidence.signals:
        page = pages.get(signal.document_url_hash, "not_fetched")
        if isinstance(page, str):
            checked += len(signal.excerpts)
            dropped[page] += len(signal.excerpts)
            unreachable = unreachable or page in TRANSIENT_PAGE_FAILURES
            continue
        quotes: dict[tuple[int, int], VerifiedQuote] = {}
        for excerpt in signal.excerpts:
            checked += 1
            result = verify_quote(page.text, excerpt.quote_text)
            if isinstance(result, VerifiedQuote):
                quotes.setdefault((result.char_start, result.char_end), result)
            else:
                dropped[result] += 1
        if quotes:
            ordered = tuple(sorted(quotes.values(), key=lambda q: q.char_start))
            renewed.append(RenewedSignal(signal, page, ordered))
    return RenewalPlan(
        signals=tuple(renewed),
        excerpts_checked=checked,
        excerpts_verified=sum(len(r.quotes) for r in renewed),
        dropped=dict(dropped),
        unreachable=unreachable,
    )


def renewal_units(
    run_id: UUID, plan: RenewalPlan
) -> list[tuple[DocumentRecord, list[SignalRecord]]]:
    """One new document per page fetched again, keyed by its final URL (a run's documents are
    unique on it), carrying the renewed signals read from it."""
    units: dict[str, tuple[DocumentRecord, list[SignalRecord]]] = {}
    for renewed in plan.signals:
        page, original = renewed.page, renewed.original
        final_hash = url_hash(page.final_url)
        if final_hash not in units:
            units[final_hash] = (
                DocumentRecord(
                    run_id=run_id,
                    document_url=canonicalise(page.requested_url),
                    final_url=page.final_url,
                    url_hash=final_hash,
                    publisher_domain=publisher_domain(page.final_url),
                    trust="listed",
                    content_sha256=content_sha256(page.text),
                    truncated=page.truncated,
                    title=original.title,
                    published_at=original.published_at,
                    source_id=None,
                    document_url_hash=url_hash(page.requested_url),
                ),
                [],
            )
        units[final_hash][1].append(
            SignalRecord(
                category=original.category,
                direction=original.direction,
                tags=original.tags,
                unregistered_subjects=original.unregistered_subjects,
                summary=original.summary,
                model_id=original.model_id,
                prompt_version=original.prompt_version,
                quotes=renewed.quotes,
            )
        )
    return list(units.values())


def renewal_target(evidence: RenewalEvidence) -> dict[str, Any]:
    return ProtectionEventTarget(
        tags=evidence.tags, is_global=evidence.is_global, renews_event_id=evidence.event_id
    ).model_dump(mode="json")


def renewal_suggested(evidence: RenewalEvidence) -> dict[str, Any]:
    return ProtectionEventSuggested(
        title=evidence.title,
        strength=evidence.strength,
        review_in_days=evidence.review_in_days,
    ).model_dump(mode="json")


def renewal_rationale(plan: RenewalPlan) -> str:
    return (
        "A renewal written by code, not by a model: "
        f"{plan.excerpts_verified} of {plan.excerpts_checked} cited excerpts were fetched again"
        " and still appear word for word on their pages. The protection lapses at its review"
        " date unless this renewal is approved."
    )
