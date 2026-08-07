"""Per-attestation banding. Pure functions, no database.

Permanent tests — never delete. Each one corresponds to a way this product
can hurt someone.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from imageshield.calibration.bands import band_for_attestation, validate_numeric_bands
from imageshield.calibration.models import (
    CalibrationConfig,
    NumericBand,
    PolicyEntry,
    ScoreDomain,
)
from imageshield.types import ProviderId

HIVE = ProviderId("hive")
GOOGLE = ProviderId("google")

# Hive Web Search: 0.5 is the FLOOR of the reported range, not a midpoint.
HIVE_DOMAIN = ScoreDomain(min=Decimal("0.5"), max=Decimal("1.0"))
GOOGLE_DOMAIN = ScoreDomain(
    categories=("full_match", "partial_match", "page_match")
)

HIVE_BANDS = (
    NumericBand(band="drop", max=Decimal("0.72")),
    NumericBand(band="review", min=Decimal("0.72"), max=Decimal("0.94")),
    NumericBand(band="auto_confirm", min=Decimal("0.94")),
)


def hive_config() -> CalibrationConfig:
    return CalibrationConfig(
        config_id=uuid4(),
        provider_id=HIVE,
        version="hive-cal-v1",
        score_kind="numeric",
        numeric_bands=HIVE_BANDS,
    )


def google_config() -> CalibrationConfig:
    return CalibrationConfig(
        config_id=uuid4(),
        provider_id=GOOGLE,
        version="google-cal-v1",
        score_kind="categorical",
        categorical_bands={
            "full_match": "auto_confirm",
            "partial_match": "review",
            "page_match": "review",
        },
    )


def hive_entry(*, calibrated: bool = True, config: bool = True) -> PolicyEntry:
    return PolicyEntry(
        provider_id=HIVE,
        calibrated=calibrated,
        score_domain=HIVE_DOMAIN,
        config=hive_config() if config else None,
    )


def google_entry(*, calibrated: bool = True) -> PolicyEntry:
    return PolicyEntry(
        provider_id=GOOGLE,
        calibrated=calibrated,
        score_domain=GOOGLE_DOMAIN,
        config=google_config(),
    )


def band_hive(score: str, **kwargs: bool) -> tuple[str, str]:
    d = band_for_attestation(hive_entry(**kwargs), "numeric", Decimal(score), None)
    return d.band, d.reason


# ── Rule 1 & 2: the gates that force review ──────────────────────────────

def test_no_policy_entry_is_review() -> None:
    d = band_for_attestation(None, "numeric", Decimal("0.99"), None)
    assert d.band == "review"
    assert d.reason == "no_active_config"
    assert d.calibration_version is None


def test_no_active_config_is_review() -> None:
    assert band_hive("0.99", config=False) == ("review", "no_active_config")


@pytest.mark.parametrize("score", ["0.50", "0.71", "0.80", "0.94", "1.00"])
def test_uncalibrated_provider_produces_review_for_every_input(score: str) -> None:
    """CLAUDE.md §7.3. The config says auto_confirm at 0.94+; calibrated=false
    overrides it. This blocks `drop` too — "review band only" is literal, and
    a real infringement landing in drop is invisible to the user forever."""
    band, reason = band_hive(score, calibrated=False)
    assert band == "review"
    assert reason == "provider_uncalibrated"


def test_uncalibrated_categorical_provider_is_also_review() -> None:
    d = band_for_attestation(
        google_entry(calibrated=False), "categorical", None, "full_match"
    )
    assert d.band == "review"
    assert d.reason == "provider_uncalibrated"


# ── Rule 3: shape mismatch never crashes ─────────────────────────────────

def test_score_kind_mismatch_is_review_not_an_exception() -> None:
    d = band_for_attestation(hive_entry(), "categorical", None, "full_match")
    assert d.band == "review"
    assert d.reason == "score_kind_mismatch"


def test_numeric_kind_with_null_score_is_review() -> None:
    d = band_for_attestation(hive_entry(), "numeric", None, None)
    assert d.band == "review"
    assert d.reason == "score_kind_mismatch"


# ── Rule 4: the score_domain fixture where it changes the outcome ────────

def test_below_hive_floor_is_review_not_drop() -> None:
    """THE fixture the step-7 done-when asks for.

    0.4 is impossible for Hive — its floor is 0.5. Read against an assumed
    0–1 scale it is merely a low score and bands to `drop`, silently
    discarded and never seen by anyone. Read against score_domain it is a
    malformed response (or a key on the wrong Hive project, which returns
    plausible-looking wrong results rather than an error) and a human looks.
    """
    assert band_hive("0.40") == ("review", "score_out_of_domain")


def test_above_hive_ceiling_is_review_not_auto_confirm() -> None:
    assert band_hive("1.30") == ("review", "score_out_of_domain")


def test_in_domain_low_score_still_drops() -> None:
    """0.60 IS in Hive's domain, so domain-awareness does not suppress a
    genuine low score — it only rejects impossible ones."""
    assert band_hive("0.60") == ("drop", "numeric:drop")


# ── Rule 5: native units, exact boundaries ───────────────────────────────

@pytest.mark.parametrize(
    ("score", "expected"),
    [
        ("0.50", "drop"),          # exactly the domain floor
        ("0.7199", "drop"),
        ("0.72", "review"),        # min inclusive
        ("0.9399", "review"),
        ("0.94", "auto_confirm"),  # min inclusive — the boundary that matters
        ("1.00", "auto_confirm"),  # top band's max is inclusive
    ],
)
def test_numeric_boundaries_are_min_inclusive_max_exclusive(
    score: str, expected: str
) -> None:
    assert band_hive(score)[0] == expected


def test_scores_are_never_rescaled() -> None:
    """A 0.72 boundary means 0.72 on Hive's native scale. If anything
    rescaled the domain onto 0–1, native 0.72 would map to 0.44 and land in
    `drop` instead of `review`."""
    assert band_hive("0.72")[0] == "review"


# ── Rules 6 & 7: categorical ─────────────────────────────────────────────

@pytest.mark.parametrize(
    ("category", "expected"),
    [
        ("full_match", "auto_confirm"),
        ("partial_match", "review"),
        ("page_match", "review"),
    ],
)
def test_categorical_bands_come_from_lookup(category: str, expected: str) -> None:
    d = band_for_attestation(google_entry(), "categorical", None, category)
    assert d.band == expected
    assert d.reason == f"categorical:{expected}"


def test_categorical_never_touches_provider_score() -> None:
    """Google Web Detection reports no number. provider_score stays NULL all
    the way through — inventing one would be normalising in the adapter."""
    d = band_for_attestation(google_entry(), "categorical", None, "full_match")
    assert d.band == "auto_confirm"
    # And a stray score alongside a categorical kind is ignored, not used.
    d2 = band_for_attestation(
        google_entry(), "categorical", Decimal("0.99"), "page_match"
    )
    assert d2.band == "review"


def test_unknown_category_is_review() -> None:
    d = band_for_attestation(google_entry(), "categorical", None, "some_new_kind")
    assert d.band == "review"
    assert d.reason == "unknown_category"


def test_calibration_version_is_stamped_on_a_real_decision() -> None:
    d = band_for_attestation(hive_entry(), "numeric", Decimal("0.99"), None)
    assert d.calibration_version == "hive-cal-v1"


# ── Band JSON validation, used by `propose` ──────────────────────────────

def test_valid_bands_have_no_problems() -> None:
    assert validate_numeric_bands(HIVE_BANDS, HIVE_DOMAIN) == []


def test_boundary_outside_domain_is_rejected() -> None:
    bad = (
        NumericBand(band="drop", max=Decimal("0.20")),
        NumericBand(band="review", min=Decimal("0.20")),
    )
    problems = validate_numeric_bands(bad, HIVE_DOMAIN)
    assert any("outside score_domain" in p for p in problems)


def test_gap_in_coverage_is_rejected() -> None:
    bad = (
        NumericBand(band="drop", max=Decimal("0.70")),
        NumericBand(band="auto_confirm", min=Decimal("0.80")),
    )
    problems = validate_numeric_bands(bad, HIVE_DOMAIN)
    assert any("gap" in p for p in problems)


def test_overlap_is_rejected() -> None:
    bad = (
        NumericBand(band="drop", max=Decimal("0.80")),
        NumericBand(band="auto_confirm", min=Decimal("0.70")),
    )
    problems = validate_numeric_bands(bad, HIVE_DOMAIN)
    assert any("overlap" in p for p in problems)
