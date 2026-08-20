"""The protection score arithmetic (Task 11) — pure, no I/O, no database.

Every expected integer here is transcribed from the task brief's arithmetic
block verbatim; if a value here disagrees with that block, the block wins and
this file is wrong.
"""

from __future__ import annotations

from decimal import Decimal

from imageshield.score.engine import (
    Components,
    ConfirmedHit,
    ScoreState,
    ScoreWeights,
    ThreatPenalty,
    compute,
)
from tests.conftest import make_config


def _weights() -> ScoreWeights:
    return ScoreWeights.from_config(make_config())


def _full_marks_state() -> ScoreState:
    return ScoreState(
        enrolment_active=True,
        seed_count=5,
        seeds_fresh=True,
        has_overdue_scan=False,
        monitored_sources=2,
        confirmed_hits=(),
        awaiting_feedback_count=0,
        aged_open_recs=0,
        threats=(),
    )


def test_full_marks_is_100() -> None:
    c = compute(_full_marks_state(), _weights())
    assert (c.posture, c.coverage, c.exposure, c.threat) == (40, 25, 25, 10)
    assert c.total == 100


def test_empty_person_scores_only_defaults() -> None:
    state = ScoreState(
        enrolment_active=False,
        seed_count=0,
        seeds_fresh=False,
        has_overdue_scan=False,
        monitored_sources=0,
        confirmed_hits=(),
        awaiting_feedback_count=0,
        aged_open_recs=0,
        threats=(),
    )
    c = compute(state, _weights())
    # no enrolment, no seeds, no scans -> posture 0+0+5+5, coverage 0, exposure/threat full
    assert (c.posture, c.coverage, c.exposure, c.threat) == (10, 0, 25, 10)


def test_ncii_hit_costs_twelve_and_dead_url_restores_it() -> None:
    live = ConfirmedHit(severity="ncii_suspected", counts=True)
    dead = ConfirmedHit(severity="ncii_suspected", counts=False)
    base = _full_marks_state()
    assert compute(base.model_copy(update={"confirmed_hits": (live,)}), _weights()).exposure == 13
    assert compute(base.model_copy(update={"confirmed_hits": (dead,)}), _weights()).exposure == 25


def test_exposure_floors_at_zero() -> None:
    base = _full_marks_state()
    hits = tuple(ConfirmedHit(severity="ncii_suspected", counts=True) for _ in range(4))
    assert compute(base.model_copy(update={"confirmed_hits": hits}), _weights()).exposure == 0


def test_unassessed_and_none_severity_use_the_default_weight() -> None:
    base = _full_marks_state()
    unassessed = ConfirmedHit(severity="unassessed", counts=True)
    none_severity = ConfirmedHit(severity=None, counts=True)
    w = _weights()
    assert w.exposure_weight_default == 6
    assert (
        compute(base.model_copy(update={"confirmed_hits": (unassessed,)}), w).exposure
        == 25 - 6
    )
    assert (
        compute(base.model_copy(update={"confirmed_hits": (none_severity,)}), w).exposure
        == 25 - 6
    )


def test_stale_seeds_cap_at_half() -> None:
    base = _full_marks_state()
    state = base.model_copy(update={"seed_count": 5, "seeds_fresh": False})
    c = compute(state, _weights())
    # seed_pts would be round(20 * 5/5) = 20, but staleness caps it at 20 // 2 = 10.
    # posture = 10 (enrolment) + 10 (seeds) + 5 (feedback) + 5 (recs) = 30.
    assert c.posture == 30


def test_zero_seeds_never_hits_the_staleness_cap() -> None:
    base = _full_marks_state()
    state = base.model_copy(update={"seed_count": 0, "seeds_fresh": False})
    c = compute(state, _weights())
    # seed_count falsy -> staleness branch never runs, seed_pts stays round(0) = 0.
    assert c.posture == 10 + 0 + 5 + 5


def test_threat_decays_linearly() -> None:
    base = _full_marks_state()
    threat = ThreatPenalty(penalty=Decimal(4), age_days=5, decay_days=10, is_global=False)
    c = compute(base.model_copy(update={"threats": (threat,)}), _weights())
    # factor = 1 - 5/10 = 0.5, burn = 4 * 0.5 = 2, threat = 10 - 2 = 8.
    assert c.threat == 8


def test_global_threat_is_capped() -> None:
    base = _full_marks_state()
    threat = ThreatPenalty(penalty=Decimal(9), age_days=0, decay_days=10, is_global=True)
    c = compute(base.model_copy(update={"threats": (threat,)}), _weights())
    # factor at age 0 is 1.0, burn = 9, but global burn caps at threat_global_max_penalty (2).
    assert c.threat == 8


def test_targeted_and_global_threats_both_apply_and_stack() -> None:
    base = _full_marks_state()
    targeted = ThreatPenalty(penalty=Decimal(3), age_days=0, decay_days=10, is_global=False)
    glob = ThreatPenalty(penalty=Decimal(9), age_days=0, decay_days=10, is_global=True)
    c = compute(base.model_copy(update={"threats": (targeted, glob)}), _weights())
    # targeted burn = 3 (uncapped), global burn capped at 2 -> total burn 5 -> threat 5.
    assert c.threat == 5


def test_threat_floors_at_zero() -> None:
    base = _full_marks_state()
    threat = ThreatPenalty(penalty=Decimal(50), age_days=0, decay_days=10, is_global=False)
    c = compute(base.model_copy(update={"threats": (threat,)}), _weights())
    assert c.threat == 0


def test_aged_recs_bleed_posture() -> None:
    base = _full_marks_state()
    one_aged = compute(base.model_copy(update={"aged_open_recs": 1}), _weights())
    three_aged = compute(base.model_copy(update={"aged_open_recs": 3}), _weights())
    # recs points: max(0, 5 - 2*1) = 3; max(0, 5 - 2*3) = 0 (floored, not negative).
    assert one_aged.posture == 40 - 5 + 3
    assert three_aged.posture == 40 - 5 + 0


def test_awaiting_feedback_zeroes_the_feedback_subcomponent() -> None:
    base = _full_marks_state()
    c = compute(base.model_copy(update={"awaiting_feedback_count": 1}), _weights())
    assert c.posture == 40 - 5


def test_overdue_scan_zeroes_the_scan_subcomponent_but_not_providers() -> None:
    base = _full_marks_state()
    c = compute(base.model_copy(update={"has_overdue_scan": True}), _weights())
    assert c.coverage == 25 - 15


def test_monitored_sources_above_two_does_not_over_credit() -> None:
    base = _full_marks_state()
    c = compute(base.model_copy(update={"monitored_sources": 7}), _weights())
    assert c.coverage == 25


def test_components_as_dict_and_total_clamped() -> None:
    c = Components(posture=40, coverage=25, exposure=25, threat=10)
    assert c.as_dict() == {"posture": 40, "coverage": 25, "exposure": 25, "threat": 10}
    assert c.total == 100
    over = Components(posture=90, coverage=90, exposure=90, threat=90)
    assert over.total == 100
