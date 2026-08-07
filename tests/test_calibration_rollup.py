"""Attestation bands -> infringement band. Pure, no database.

Permanent — never delete. Rules 2 and 3 of the roll-up are deliberately
asymmetric and it would be easy to "fix" that asymmetry into a bug.
"""

from __future__ import annotations

import pytest

from imageshield.calibration.bands import roll_up
from imageshield.calibration.models import Band


def test_single_attestation_keeps_its_band() -> None:
    assert roll_up(["auto_confirm"]) == ("auto_confirm", "unanimous:auto_confirm(n=1)")


def test_drop_and_auto_confirm_disagreement_yields_review() -> None:
    """The done-when case. One provider says discard it, another says alarm
    the user unreviewed. That is not a reason to average them — it is the
    clearest possible signal that a human should look."""
    band, reason = roll_up(["drop", "auto_confirm"])
    assert band == "review"
    assert reason == "disagreement:drop|auto_confirm->review"


def test_band_reason_records_the_disagreement_in_both_orders() -> None:
    assert roll_up(["auto_confirm", "drop"])[1] == "disagreement:drop|auto_confirm->review"


def test_two_providers_at_review_is_not_a_promotion() -> None:
    """Concurrence between two image-search providers indexing overlapping
    corpora is not two independent observations."""
    band, reason = roll_up(["review", "review"])
    assert band == "review"
    assert reason == "unanimous:review(n=2)"


def test_unanimous_auto_confirm_stays_auto_confirm() -> None:
    """Agreement adds nothing, but it also takes nothing away: each of these
    alone would already have been auto_confirm."""
    assert roll_up(["auto_confirm", "auto_confirm"])[0] == "auto_confirm"


def test_unanimous_drop_stays_drop() -> None:
    assert roll_up(["drop", "drop", "drop"])[0] == "drop"


@pytest.mark.parametrize(
    "bands",
    [
        ["review", "auto_confirm"],
        ["drop", "review"],
        ["drop", "review", "auto_confirm"],
        ["auto_confirm", "auto_confirm", "drop"],
    ],
)
def test_any_spread_at_all_resolves_to_review(bands: list[Band]) -> None:
    assert roll_up(bands)[0] == "review"


def test_empty_is_review_rather_than_an_exception() -> None:
    assert roll_up([]) == ("review", "no_attestations")


def test_roll_up_never_returns_a_band_no_provider_gave() -> None:
    """A guard against anyone introducing averaging here later: the output is
    always either a band that was in the input, or review."""
    for bands in (["drop"], ["drop", "drop"], ["auto_confirm", "review"]):
        result, _ = roll_up(bands)
        assert result in set(bands) | {"review"}


def test_unrecognised_band_value_is_review_not_a_crash() -> None:
    """C3. These values are read back out of the database. A value that
    isn't one of the three known bands used to raise KeyError inside
    BAND_ORDER[b] via min()/max(); it must resolve to review instead, in the
    safe direction, with the offending value named in the reason."""
    band, reason = roll_up(["drop", "bogus"])  # type: ignore[list-item]
    assert band == "review"
    assert reason == "unknown_band:bogus"


def test_bare_string_passed_instead_of_a_list_is_review_not_a_crash() -> None:
    """C3, the specific case named in review: a caller who forgot to wrap a
    single band in a list passes a bare str. Sequence[Band] silently accepts
    it at the type level and Python iterates it character by character
    ('d', 'r', 'o', 'p', ...), none of which is a real band. That must
    resolve to review, not raise partway through min()/max()."""
    band, reason = roll_up("drop")  # type: ignore[arg-type]
    assert band == "review"
    assert reason == "unknown_band:d"
