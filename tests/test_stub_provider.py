"""`SEARCH_PROVIDER=stub` — the switch, and the thing it is wired to.

This existed as a config field with a docstring calling it "the only thing
standing between a test run and billable Hive traffic", and nothing outside
`config.py` read it. `build_providers()` constructed the real Hive and Google
adapters unconditionally, migration 0004 seeds both `providers.enabled = true`,
and `daily_budget_usd` is NULL for both so the budget guard permits the spend.
A `POST /v1/search` in development with the worker running billed real money
against real keys.

A safety switch wired to nothing is worse than no switch: it produces false
confidence, and the person relying on it is the one who wrote the docstring.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from imageshield.calibration.bands import band_for_attestation
from imageshield.search.google import GoogleWebDetectionProvider
from imageshield.search.hive import HiveWebSearchProvider
from imageshield.search.provider import SearchProvider
from imageshield.search.stub import StubSearchProvider
from imageshield.search.worker import build_providers
from imageshield.types import ProviderId
from tests.conftest import make_config

STUB = ProviderId("stub")
HIVE = ProviderId("hive")
GOOGLE = ProviderId("google")


# ── the switch ───────────────────────────────────────────────────────────────


def test_stub_builds_the_stub_and_no_live_adapter() -> None:
    """Done-when: no Hive or Google adapter is CONSTRUCTED. Not disabled, not
    skipped by a guard — absent, so there is no object holding a real API key
    that any code path could reach.
    """
    providers = build_providers(make_config(search_provider="stub"))

    assert set(providers) == {STUB}
    assert isinstance(providers[STUB], StubSearchProvider)
    for adapter in providers.values():
        assert not isinstance(adapter, HiveWebSearchProvider | GoogleWebDetectionProvider)


def test_a_live_setting_builds_the_real_adapters_and_not_the_stub() -> None:
    """The other edge: the stub must not silently displace real coverage
    anywhere it was not asked for. `hive` and `google` both mean "the real
    stack" — which of the pair actually runs is `providers.enabled`'s job (the
    hot-reloadable kill switch, §7.6), not this boot-time knob's.
    """
    for setting in ("hive", "google"):
        providers = build_providers(
            make_config(environment="production", search_provider=setting)
        )
        assert set(providers) == {HIVE, GOOGLE}, setting
        assert isinstance(providers[HIVE], HiveWebSearchProvider)
        assert isinstance(providers[GOOGLE], GoogleWebDetectionProvider)
        assert STUB not in providers


def test_the_stub_satisfies_the_provider_protocol() -> None:
    """Structural, not nominal: the runner reads `score_kind` and
    `score_version` off whatever is in the mapping, so an adapter missing one
    fails at dispatch rather than at import."""
    adapter: SearchProvider = StubSearchProvider()
    assert adapter.id == STUB
    assert adapter.kind in ("image_search", "face_search", "classifier")
    assert adapter.score_kind in ("numeric", "categorical")
    assert adapter.score_version


# ── what the stub does ───────────────────────────────────────────────────────


async def test_the_stub_returns_zero_matches_and_makes_no_http_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Done-when: no socket is opened. Asserted by breaking httpx underneath it
    rather than by inspecting the stub's source — a future edit that adds a
    "harmless" call fails here instead of on a bill.
    """

    async def _boom(*args: Any, **kwargs: Any) -> httpx.Response:
        raise AssertionError("the stub provider made an HTTP request")

    monkeypatch.setattr(httpx.AsyncClient, "send", _boom)
    monkeypatch.setattr(httpx.AsyncClient, "request", _boom)

    result = await StubSearchProvider().search(
        "https://proxy-s3.example/seed.jpg?X-Amz-Signature=stub"
    )

    assert result.provider_id == STUB
    assert result.status == "ok"
    assert result.matches == []
    assert result.http_status is None
    assert result.attempts == 1


async def test_the_stub_is_deterministic() -> None:
    """"Returns nothing" must not be "returns nothing this time". A stub that
    occasionally produced a match would put a fabricated finding on a real
    user_ref's report."""
    stub = StubSearchProvider()
    for seed in ("https://a.test/1.jpg", "https://b.test/2.jpg", "https://a.test/1.jpg"):
        result = await stub.search(seed)
        assert result.matches == []
        assert result.status == "ok"


async def test_the_stub_says_in_its_payload_that_it_is_a_stub() -> None:
    """`raw_response` is stored verbatim on every provider_calls row and is what
    anyone recalibrating over history reads. A stub row that looks like a real
    empty result is a row that will be counted as evidence of an empty scan."""
    result = await StubSearchProvider().search("https://a.test/1.jpg")
    assert result.raw_response.get("stub") is True
    assert result.error_detail is not None
    assert "stub" in result.error_detail.lower()


# ── honesty: a stub result cannot reach a user as a real finding ─────────────


def test_the_stub_never_borrows_a_real_provider_id() -> None:
    """The tempting shortcut is registering the stub under `hive`, so the run
    completes with `providers_succeeded = ['hive']`. That writes attestations
    claiming a provider we never called and a score_version we never ran, and it
    is the one thing this adapter must never do."""
    stub = StubSearchProvider()
    assert stub.id not in (HIVE, GOOGLE)
    assert stub.score_version not in (
        HiveWebSearchProvider.score_version,
        GoogleWebDetectionProvider.score_version,
    )


def test_a_stub_attestation_could_only_ever_reach_review() -> None:
    """CLAUDE.md §7.3. The stub has no `providers` row and no calibration
    config, so it has no policy entry — and rule 1 of the banding logic sends a
    provider with no active config to `review`, never `auto_confirm` and never
    `drop`. Belt on top of "it returns nothing": if a future edit ever gave the
    stub a match, a human would still stand between it and a user.
    """
    decision = band_for_attestation(None, StubSearchProvider().score_kind, None, None)
    assert decision.band == "review"
