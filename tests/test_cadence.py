"""Adaptive scan cadence — the tier state machine, pure.

This is the only piece of step 8 that reduces cost rather than capping it, and
the direction of every transition is a safety decision: demoting too eagerly
means a user is scanned less often than they believe.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from imageshield.search.cadence import (
    CadencePolicy,
    interval_days,
    next_tier,
    should_retier,
    update_for,
)

POLICY = CadencePolicy(
    standard_days=7,
    relaxed_days=14,
    dormant_days=30,
    new_tier_weeks=4,
    relaxed_after_empty=8,
    dormant_after_empty=20,
    priority_release_after_empty=13,
)
OLD_SEED_DAYS = 365


def _empty(current: str, empties: int, age_days: int = OLD_SEED_DAYS):  # type: ignore[no-untyped-def]
    return next_tier(
        current=current,  # type: ignore[arg-type]
        consecutive_empty_scans=empties,
        found_matches=False,
        seed_age_days=age_days,
        policy=POLICY,
    )


def test_eight_consecutive_empty_scans_demote_to_relaxed_and_seven_do_not() -> None:
    assert _empty("standard", 6) == ("standard", 7)
    assert _empty("standard", 7) == ("relaxed", 8)


def test_twenty_consecutive_empty_scans_demote_to_dormant() -> None:
    assert _empty("relaxed", 18) == ("relaxed", 19)
    assert _empty("relaxed", 19) == ("dormant", 20)


def test_any_non_empty_scan_promotes_to_priority_and_resets_the_counter() -> None:
    """Promotion skips every intermediate tier. Someone just found in a deepfake
    needs the most frequent cadence available, not a gradual climb back."""
    for tier in ("new", "standard", "relaxed", "dormant", "priority"):
        assert next_tier(
            current=tier,  # type: ignore[arg-type]
            consecutive_empty_scans=19,
            found_matches=True,
            seed_age_days=OLD_SEED_DAYS,
            policy=POLICY,
        ) == ("priority", 0)


def test_priority_is_held_until_the_release_threshold_then_returns_to_standard() -> None:
    assert _empty("priority", 11) == ("priority", 12)
    assert _empty("priority", 12) == ("standard", 13)
    # Never straight to relaxed or dormant: a seed with a hit in living memory
    # re-earns its demotion from the top.
    assert _empty("priority", 25)[0] == "standard"


def test_a_new_seed_is_held_at_the_new_tier_for_its_first_weeks() -> None:
    """Even past the relaxed threshold. The first month after enrolment is when
    a user is most likely to be checking and when the corpus has had least time
    to turn something up."""
    assert _empty("new", 10, age_days=20) == ("new", 11)
    # 4 weeks = 28 days; at 28 the hold is over and the counter applies.
    assert _empty("new", 10, age_days=28) == ("relaxed", 11)


def test_a_new_seed_past_its_hold_with_a_low_counter_becomes_standard() -> None:
    assert _empty("new", 1, age_days=30) == ("standard", 2)


@pytest.mark.parametrize(
    ("tier", "days"),
    [
        ("new", 7),
        ("standard", 7),
        ("priority", 7),  # same interval as standard; different transitions
        ("relaxed", 14),
        ("dormant", 30),
    ],
)
def test_intervals(tier: str, days: int) -> None:
    assert interval_days(tier, POLICY) == days  # type: ignore[arg-type]


def test_should_retier_requires_at_least_one_successful_provider() -> None:
    """A run where everything was skipped or timed out produced no evidence
    either way. Treating it as an empty scan would relax a user's cadence
    because OUR integration was broken."""
    assert should_retier(0) is False
    assert should_retier(1) is True


def test_the_tier_interval_still_lands_on_tier_next_scan_after() -> None:
    """The adaptive interval did not go away -- it moved.

    Owner's decision 2026-09-09: dispatch is every Sunday, tiers ignored. The
    tier is still computed and its interval still recorded, on
    `tier_next_scan_after`, so the saving forgone stays measurable (§7.8 puts
    it at 4-10x). This test is the one that used to assert `next_scan_after`
    carried these numbers; it now asserts the counterfactual column does.
    """
    now = datetime(2026, 8, 9, tzinfo=UTC)

    demoted = update_for(
        current="standard",
        consecutive_empty_scans=7,
        found_matches=False,
        seed_age_days=OLD_SEED_DAYS,
        now=now,
        policy=POLICY,
    )
    assert demoted.scan_tier == "relaxed"
    assert (demoted.tier_next_scan_after - now).days == 14  # relaxed's interval

    promoted = update_for(
        current="dormant",
        consecutive_empty_scans=30,
        found_matches=True,
        seed_age_days=OLD_SEED_DAYS,
        now=now,
        policy=POLICY,
    )
    assert promoted.scan_tier == "priority"
    assert (promoted.tier_next_scan_after - now).days == 7


def test_next_scan_after_is_the_coming_sunday_whatever_the_tier() -> None:
    """What ACTUALLY dispatches. The proxy's search sweep enqueues any seed
    whose next_scan_after has passed, so this column is the schedule -- and
    INVARIANTS #42 means it must not carry a date nothing acts on."""
    # 2026-08-09 is a Sunday; a run completing that day is next due the 16th.
    sunday = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
    on_sunday = update_for(
        current="standard", consecutive_empty_scans=0, found_matches=True,
        seed_age_days=OLD_SEED_DAYS, now=sunday, policy=POLICY,
    )
    assert on_sunday.next_scan_after == datetime(2026, 8, 16, 0, 0, tzinfo=UTC)

    # A Wednesday run is due the coming Sunday, NOT seven days later. This is
    # the bug: a Wednesday upload previously set the 16th (a Wednesday), so
    # "every Sunday" never held for anyone who joined mid-week.
    wednesday = datetime(2026, 8, 12, 9, 30, tzinfo=UTC)
    midweek = update_for(
        current="standard", consecutive_empty_scans=0, found_matches=True,
        seed_age_days=OLD_SEED_DAYS, now=wednesday, policy=POLICY,
    )
    assert midweek.next_scan_after == datetime(2026, 8, 16, 0, 0, tzinfo=UTC)


def test_a_dormant_seed_is_still_due_next_sunday() -> None:
    """The whole point of ignoring tiers: a seed the adaptive policy would have
    parked for a month is still scanned this coming Sunday."""
    wednesday = datetime(2026, 8, 12, 9, 30, tzinfo=UTC)
    parked = update_for(
        current="dormant", consecutive_empty_scans=40, found_matches=False,
        seed_age_days=OLD_SEED_DAYS, now=wednesday, policy=POLICY,
    )
    assert parked.scan_tier == "dormant"
    assert parked.next_scan_after == datetime(2026, 8, 16, 0, 0, tzinfo=UTC)
    # ...while the column that records what tiering wanted keeps saying 30 days.
    assert (parked.tier_next_scan_after - wednesday).days == 30
