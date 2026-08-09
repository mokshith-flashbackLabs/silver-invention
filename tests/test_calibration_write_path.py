"""Banding inside record_infringements, against real Postgres.

The roll-up runs on every attestation write, so there is never a moment where
a stored infringement band disagrees with its own attestations.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from imageshield.calibration.models import (
    CalibrationConfig,
    NumericBand,
    PolicyEntry,
    ScoreDomain,
)
from imageshield.search.models import ProviderDescriptor
from imageshield.search.provider import ProviderMatch
from imageshield.types import ProviderId

HIVE = ProviderId("hive")
GOOGLE = ProviderId("google")

HIVE_DESC = ProviderDescriptor(
    provider_id=HIVE, score_kind="numeric", score_version="hive-web-search-v1"
)
GOOGLE_DESC = ProviderDescriptor(
    provider_id=GOOGLE, score_kind="categorical", score_version="google-web-detection-v1"
)


def hive_policy(*, calibrated: bool) -> dict[ProviderId, PolicyEntry]:
    return {
        HIVE: PolicyEntry(
            provider_id=HIVE,
            calibrated=calibrated,
            score_domain=ScoreDomain(min=Decimal("0.5"), max=Decimal("1.0")),
            config=CalibrationConfig(
                config_id=uuid4(),
                provider_id=HIVE,
                version="hive-cal-v1",
                score_kind="numeric",
                numeric_bands=(
                    NumericBand(band="drop", max=Decimal("0.72")),
                    NumericBand(band="review", min=Decimal("0.72"), max=Decimal("0.94")),
                    NumericBand(band="auto_confirm", min=Decimal("0.94")),
                ),
            ),
        )
    }


def hive_match(url: str, score: str) -> ProviderMatch:
    return ProviderMatch(
        image_url=f"{url}/img.jpg",
        page_urls=[url],
        provider_score=Decimal(score),
        provider_category=None,
        query_quality=None,
    )


def google_match(url: str, category: str) -> ProviderMatch:
    return ProviderMatch(
        image_url=f"{url}/img.jpg",
        page_urls=[url],
        provider_score=None,
        provider_category=category,
        query_quality=None,
    )


async def test_empty_policy_still_writes_review(search_fixture) -> None:
    """Nothing configured -> rule 1 -> review. This is the state the repo
    ships in, and it must hold without any calibration row existing."""
    store, run_id, user_ref = search_fixture
    await store.record_infringements(
        run_id, user_ref, HIVE_DESC, [hive_match("https://x.test/a", "0.99")], {}
    )
    rows = await store.list_infringements(user_ref, None)
    assert rows[0].band == "review"
    assert rows[0].attestations[0].band == "review"
    assert rows[0].attestations[0].calibration_version is None


async def test_uncalibrated_provider_cannot_auto_confirm(search_fixture) -> None:
    """CLAUDE.md §7.3, asserted against the real write path rather than only
    against the pure function."""
    store, run_id, user_ref = search_fixture
    await store.record_infringements(
        run_id, user_ref, HIVE_DESC, [hive_match("https://x.test/a", "0.99")],
        hive_policy(calibrated=False),
    )
    rows = await store.list_infringements(user_ref, None)
    assert rows[0].band == "review"
    assert rows[0].band_reason == "unanimous:review(n=1)"


async def test_calibrated_provider_bands_and_stamps_the_version(search_fixture) -> None:
    store, run_id, user_ref = search_fixture
    await store.record_infringements(
        run_id, user_ref, HIVE_DESC, [hive_match("https://x.test/a", "0.99")],
        hive_policy(calibrated=True),
    )
    rows = await store.list_infringements(user_ref, None)
    assert rows[0].band == "auto_confirm"
    assert rows[0].attestations[0].band == "auto_confirm"
    assert rows[0].attestations[0].calibration_version == "hive-cal-v1"


async def test_below_hive_floor_is_review_through_the_real_write_path(
    search_fixture,
) -> None:
    """0.4 is impossible for Hive. It must not be discarded as a low score."""
    store, run_id, user_ref = search_fixture
    await store.record_infringements(
        run_id, user_ref, HIVE_DESC, [hive_match("https://x.test/a", "0.40")],
        hive_policy(calibrated=True),
    )
    rows = await store.list_infringements(user_ref, None)
    assert rows[0].band == "review"


async def test_disagreement_across_providers_resolves_to_review(
    search_fixture, google_policy
) -> None:
    """One provider auto_confirms, the other drops. The stored infringement
    must be review, and band_reason must say why — a reviewer needs to
    understand at a glance which rule fired."""
    store, run_id, user_ref = search_fixture
    policy = {**hive_policy(calibrated=True), **google_policy("drop")}
    url = "https://x.test/shared"
    await store.record_infringements(
        run_id, user_ref, HIVE_DESC, [hive_match(url, "0.99")], policy
    )
    await store.record_infringements(
        run_id, user_ref, GOOGLE_DESC, [google_match(url, "page_match")], policy
    )
    rows = await store.list_infringements(user_ref, None)
    assert len(rows) == 1
    assert rows[0].band == "review"
    assert rows[0].band_reason == "disagreement:drop|auto_confirm->review"
    assert len(rows[0].attestations) == 2


async def test_two_providers_at_review_is_not_promoted(
    search_fixture, google_policy
) -> None:
    store, run_id, user_ref = search_fixture
    policy = {**hive_policy(calibrated=True), **google_policy("review")}
    url = "https://x.test/shared"
    await store.record_infringements(
        run_id, user_ref, HIVE_DESC, [hive_match(url, "0.80")], policy
    )
    await store.record_infringements(
        run_id, user_ref, GOOGLE_DESC, [google_match(url, "page_match")], policy
    )
    rows = await store.list_infringements(user_ref, None)
    assert rows[0].band == "review"
    assert rows[0].band_reason == "unanimous:review(n=2)"


async def test_roll_up_is_correct_after_each_write_not_only_at_run_end(
    search_fixture, google_policy
) -> None:
    """After the FIRST provider's write the stored band is already consistent
    with the attestations that exist at that moment. This is the property
    end-of-run roll-up would not have."""
    store, run_id, user_ref = search_fixture
    policy = {**hive_policy(calibrated=True), **google_policy("drop")}
    url = "https://x.test/shared"
    await store.record_infringements(
        run_id, user_ref, HIVE_DESC, [hive_match(url, "0.99")], policy
    )
    mid = await store.list_infringements(user_ref, None)
    assert mid[0].band == "auto_confirm"          # correct for one attestation
    await store.record_infringements(
        run_id, user_ref, GOOGLE_DESC, [google_match(url, "page_match")], policy
    )
    end = await store.list_infringements(user_ref, None)
    assert end[0].band == "review"                # correct for two
