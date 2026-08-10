"""The provider-agnostic search interface (CLAUDE.md §7).

Adapters translate a provider's response into :class:`ProviderMatch` rows and
**stop**. They do not normalise, band, threshold, rescale, or compare —
Provider A's 0.92 and Provider B's 0.92 are different quantities with
different distributions, and calibration is a separate, versioned,
config-driven step (step 7). An adapter that normalises makes recalibration
impossible without a redeploy.

``raw_response`` is stored verbatim on every ``provider_calls`` row,
including failures. It is the only way to recompute bands over history when
a provider retunes.

Two score shapes exist and neither is converted into the other:

- ``numeric`` — the provider reports a similarity number (Hive Web Search,
  0.5-1.0). Stored raw in ``provider_score``.
- ``categorical`` — the provider reports membership in a category and no
  number (Google Web Detection: full_match / partial_match / page_match).
  ``provider_score`` stays NULL; inventing a number here would be
  normalising inside the adapter.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from imageshield.types import ProviderId

ProviderStatus = Literal[
    "ok",
    "error",
    "rate_limited",
    "timeout",
    # The three step-8 skip statuses. No adapter ever produces them: they are
    # written by the dispatch guard (imageshield/providers/gate.py) for a
    # provider that was NOT called, and they are recorded rather than silent
    # because a skipped provider must be distinguishable from one that ran and
    # found nothing (CLAUDE.md §7.5).
    "budget_exceeded",
    "breaker_open",
    "provider_disabled",
]


class ProviderMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_url: str
    # EVERY page carrying the matched image (Hive's backlinks[]), order
    # preserved, duplicates collapsed. One infringement per entry: the page
    # is what a user acts on, so one match on three pages is three places to
    # act. Empty means the provider reported no host page, and the store
    # falls back to keying on image_url.
    page_urls: list[str]
    provider_score: Decimal | None  # numeric providers only — RAW, never rescaled
    provider_category: str | None   # categorical providers only
    query_quality: str | None


class ProviderResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: ProviderId
    status: ProviderStatus
    matches: list[ProviderMatch]
    raw_response: dict[str, Any]    # VERBATIM, always, even on error
    http_status: int | None
    latency_ms: int
    # How many HTTP attempts this result cost, counting the first. >1 means the
    # provider rate-limited us and the bounded retry (providers/ratelimit.py)
    # ran. Recorded on provider_calls.attempt so "we are being throttled" is
    # visible before it becomes "we are being throttled and gave up".
    attempts: int = 1
    # Short, stable failure summary for provider_calls.error_detail. The verbatim
    # body stays in raw_response, which the retention job nulls after
    # RAW_RESPONSE_RETENTION_DAYS; this outlives it.
    error_detail: str | None = None


class SearchProvider(Protocol):
    id: ProviderId
    kind: Literal["image_search", "face_search", "classifier"]
    score_kind: Literal["numeric", "categorical"]
    score_version: str

    async def search(
        self, seed_url: str, max_results: int | None = None
    ) -> ProviderResult: ...
