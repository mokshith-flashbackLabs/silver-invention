"""Publication dates and the recency of evidence (spec 2026-10-04-intel-evidence-quality §2, §3).

A document's ``intel_documents.published_at`` is when the PUBLISHER says it was published, never
when we fetched it. It comes, first source winning, from:

1. a feed item's own date (a feed entry keeps the date its feed gave it);
2. the page's HTML metadata, parsed by the fetcher (``fetcher/extract.py``);
3. a web search result's ``page_age``, for a page a saved search found (relative values such as
   "3 days ago" are read against the moment the search answered);
4. the extraction model's ``published_date``, accepted only as ``YYYY-MM-DD`` whose year appears
   in the document text -- a date the text states, never one the model supplies.

Metadata beats the model, and every value must be plausible (``plausible``): not before
``PUBLISHED_MIN_YEAR``, not more than ``PUBLISHED_FUTURE_SLACK_DAYS`` in the future. Anything
else leaves the document undated, which is a fact the reads report (``evidence_dates``), not a
failure.

``evidence_stale`` is the one recency predicate: a threat_event whose active evidence is ALL
dated and ALL older than ``INTEL_THREAT_RECENCY_DAYS`` is not a current incident. Undated
evidence never makes a proposal stale.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from imageshield.fetcher.extract import iso_date
from imageshield.intel.bounds import PUBLISHED_FUTURE_SLACK_DAYS, PUBLISHED_MIN_YEAR
from imageshield.intel.proposal_models import ContextSignal

_RELATIVE = re.compile(
    r"^(?P<n>\d+|an?|one)\s+(?P<unit>second|minute|hour|day|week|month|year)s?\s+ago$"
)
_UNIT = {
    "second": timedelta(seconds=1),
    "minute": timedelta(minutes=1),
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(weeks=1),
    "month": timedelta(days=30),
    "year": timedelta(days=365),
}
_STATED = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class Recency:
    """``INTEL_THREAT_RECENCY_DAYS`` at a moment: evidence published before ``cutoff`` is old."""

    now: datetime
    days: int

    @property
    def cutoff(self) -> datetime:
        return self.now - timedelta(days=self.days)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def plausible(value: datetime | None, now: datetime) -> datetime | None:
    """``value`` if somebody could have published on it, else None."""
    if value is None:
        return None
    value = _aware(value)
    if value.year < PUBLISHED_MIN_YEAR:
        return None
    if value > _aware(now) + timedelta(days=PUBLISHED_FUTURE_SLACK_DAYS):
        return None
    return value


def parse_published(value: str | None) -> datetime | None:
    """An absolute date in any form the fetcher reads (ISO-8601, RFC 2822, a written date)."""
    if not value:
        return None
    parsed = iso_date(value)
    return datetime.fromisoformat(parsed) if parsed is not None else None


def parse_page_age(value: str | None, retrieved_at: datetime) -> datetime | None:
    """A web search result's ``page_age``: an absolute date, or a relative one ("3 days ago",
    "yesterday") read against ``retrieved_at``, the moment the search answered. None when it
    cannot be read."""
    if not value:
        return None
    text = " ".join(value.strip().lower().split())
    if text in ("today", "just now"):
        return _aware(retrieved_at)
    if text == "yesterday":
        return _aware(retrieved_at) - timedelta(days=1)
    match = _RELATIVE.match(text)
    if match is not None:
        raw = match.group("n")
        count = int(raw) if raw.isdigit() else 1
        return _aware(retrieved_at) - count * _UNIT[match.group("unit")]
    return parse_published(value)


def stated_date(raw: str | None, text: str) -> datetime | None:
    """The extraction model's ``published_date``: accepted only as ``YYYY-MM-DD`` whose year
    appears in the document text it read, so a date the model invented is dropped."""
    if raw is None or not _STATED.match(raw.strip()):
        return None
    try:
        day = date.fromisoformat(raw.strip())
    except ValueError:
        return None
    if str(day.year) not in text:
        return None
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def choose_published(
    *,
    feed: datetime | None,
    metadata: datetime | None,
    page_age: datetime | None,
    stated: datetime | None,
    now: datetime,
) -> tuple[datetime | None, str]:
    """The document's publication date and where it came from: the first plausible value in the
    module's order, or ``(None, "unknown")``. The fetch time is never a candidate."""
    for source, value in (
        ("feed", feed),
        ("metadata", metadata),
        ("search", page_age),
        ("text", stated),
    ):
        chosen = plausible(value, now)
        if chosen is not None:
            return chosen, source
    return None, "unknown"


def evidence_stale(active: Sequence[ContextSignal], recency: Recency) -> bool:
    """True when every active signal is dated AND older than the window. Undated evidence never
    makes a proposal stale, and no evidence at all is ``evidence_retracted``, not stale."""
    signals = [s for s in active if s.status == "active"]
    return bool(signals) and all(
        s.published_at is not None and _aware(s.published_at) < recency.cutoff for s in signals
    )


def evidence_dates(signals: Sequence[ContextSignal]) -> dict[str, Any]:
    """``{newest, oldest, undated}`` over the active evidence, one entry per DOCUMENT (a page
    with three signals is one dated or undated document): ``newest``/``oldest`` as ISO dates
    (UTC) or null, ``undated`` the count of documents with no publication date."""
    by_document: dict[object, datetime | None] = {}
    for signal in signals:
        if signal.status != "active":
            continue
        key: object = signal.document_key or signal.signal_id
        if by_document.get(key) is None:
            by_document[key] = signal.published_at
    dated = [_aware(d).astimezone(UTC).date() for d in by_document.values() if d is not None]
    return {
        "newest": max(dated).isoformat() if dated else None,
        "oldest": min(dated).isoformat() if dated else None,
        "undated": sum(1 for d in by_document.values() if d is None),
    }
