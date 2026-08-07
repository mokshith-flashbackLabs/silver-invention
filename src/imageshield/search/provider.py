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

ProviderStatus = Literal["ok", "error", "rate_limited", "timeout", "budget_exceeded"]
# 'budget_exceeded' is unused until step 8 (cost tracking); it exists now so
# the status vocabulary doesn't change shape when budgets arrive.


class ProviderMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_url: str
    page_url: str | None            # backlinks[0].url for Hive when present
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


class SearchProvider(Protocol):
    id: ProviderId
    kind: Literal["image_search", "face_search", "classifier"]
    score_kind: Literal["numeric", "categorical"]
    score_version: str

    async def search(
        self, seed_url: str, max_results: int | None = None
    ) -> ProviderResult: ...
