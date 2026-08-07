"""The provider interface models are the boundary every adapter writes into —
``extra='forbid'`` and the closed status enum are what keep a provider's
private response vocabulary from leaking past the adapter."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from imageshield.search.provider import ProviderMatch, ProviderResult
from imageshield.types import ProviderId


def test_provider_match_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ProviderMatch(
            image_url="https://x/a.jpg",
            page_url=None,
            provider_score=Decimal("0.9"),
            provider_category=None,
            query_quality=None,
            normalised_score=0.5,  # type: ignore[call-arg]
        )


def test_provider_result_status_is_closed_enum() -> None:
    with pytest.raises(ValidationError):
        ProviderResult(
            provider_id=ProviderId("hive"),
            status="partial",  # type: ignore[arg-type]
            matches=[],
            raw_response={},
            http_status=200,
            latency_ms=1,
        )


def test_provider_score_stays_decimal_raw() -> None:
    match = ProviderMatch(
        image_url="https://x/a.jpg",
        page_url=None,
        provider_score=Decimal("0.5001"),
        provider_category=None,
        query_quality=None,
    )
    assert match.provider_score == Decimal("0.5001")
