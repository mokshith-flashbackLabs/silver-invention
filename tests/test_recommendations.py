"""The recommendation rule table (Task 11) — pure, no I/O, no database.

Each rule is tested firing and clearing independently, plus the fixed
output order and the event-sourced recommendation's extra fields. The sync
against what is actually open in the database (complete/expire/dismiss) is
Task 12's job in the store and is not exercised here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from imageshield.recommendations.catalog import EventNeedingScan, RecSpec, desired
from imageshield.score.engine import ScoreState, ScoreWeights
from tests.conftest import make_config


def _weights() -> ScoreWeights:
    return ScoreWeights.from_config(make_config())


def _fully_set_up_state(**overrides: object) -> ScoreState:
    base = dict(
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
    base.update(overrides)
    return ScoreState(**base)  # type: ignore[arg-type]


def test_fully_set_up_state_desires_nothing() -> None:
    assert desired(_fully_set_up_state(), (), _weights()) == ()


def test_incomplete_enrolment_fires_complete_enrolment() -> None:
    state = _fully_set_up_state(enrolment_active=False)
    specs = desired(state, (), _weights())
    assert specs == (RecSpec(kind="complete_enrolment", params={}),)


def test_complete_enrolment_clears_once_enrolment_active() -> None:
    state = _fully_set_up_state(enrolment_active=True)
    specs = desired(state, (), _weights())
    assert all(s.kind != "complete_enrolment" for s in specs)


def test_seed_count_below_target_fires_add_seed_photos() -> None:
    w = _weights()
    state = _fully_set_up_state(seed_count=2)
    specs = desired(state, (), w)
    assert RecSpec(kind="add_seed_photos", params={"target": w.seed_target, "have": 2}) in specs


def test_seed_count_at_target_clears_add_seed_photos() -> None:
    w = _weights()
    state = _fully_set_up_state(seed_count=w.seed_target)
    specs = desired(state, (), w)
    assert all(s.kind != "add_seed_photos" for s in specs)


def test_stale_seeds_fire_refresh_seeds() -> None:
    w = _weights()
    state = _fully_set_up_state(seed_count=5, seeds_fresh=False)
    specs = desired(state, (), w)
    assert RecSpec(kind="refresh_seeds", params={"fresh_days": w.seed_fresh_days}) in specs


def test_fresh_seeds_clear_refresh_seeds() -> None:
    state = _fully_set_up_state(seed_count=5, seeds_fresh=True)
    specs = desired(state, (), _weights())
    assert all(s.kind != "refresh_seeds" for s in specs)


def test_zero_seeds_does_not_fire_refresh_seeds() -> None:
    # seed_count == 0 is covered by add_seed_photos, not refresh_seeds — an
    # empty portfolio isn't "stale", it's "not started".
    state = _fully_set_up_state(seed_count=0, seeds_fresh=False)
    specs = desired(state, (), _weights())
    assert all(s.kind != "refresh_seeds" for s in specs)


def test_awaiting_feedback_fires_respond_to_hits_with_count() -> None:
    state = _fully_set_up_state(awaiting_feedback_count=3)
    specs = desired(state, (), _weights())
    assert RecSpec(kind="respond_to_hits", params={"count": 3}) in specs


def test_no_awaiting_feedback_clears_respond_to_hits() -> None:
    state = _fully_set_up_state(awaiting_feedback_count=0)
    specs = desired(state, (), _weights())
    assert all(s.kind != "respond_to_hits" for s in specs)


def test_event_needing_scan_fires_run_priority_scan_with_source_and_expiry() -> None:
    event_id = uuid4()
    expires_at = datetime(2026, 9, 1, tzinfo=UTC)
    event = EventNeedingScan(event_id=event_id, expires_at=expires_at)
    specs = desired(_fully_set_up_state(), (event,), _weights())
    assert specs == (
        RecSpec(
            kind="run_priority_scan",
            params={"event_id": str(event_id)},
            source_event_id=event_id,
            expires_at=expires_at,
        ),
    )


def test_no_events_needing_scan_clears_run_priority_scan() -> None:
    specs = desired(_fully_set_up_state(), (), _weights())
    assert all(s.kind != "run_priority_scan" for s in specs)


def test_multiple_events_each_produce_their_own_recommendation_in_order() -> None:
    e1 = EventNeedingScan(event_id=uuid4(), expires_at=datetime(2026, 9, 1, tzinfo=UTC))
    e2 = EventNeedingScan(event_id=uuid4(), expires_at=datetime(2026, 9, 2, tzinfo=UTC))
    specs = desired(_fully_set_up_state(), (e1, e2), _weights())
    assert [s.source_event_id for s in specs] == [e1.event_id, e2.event_id]


def test_output_order_matches_the_fixed_rule_order() -> None:
    w = _weights()
    event = EventNeedingScan(event_id=uuid4(), expires_at=datetime(2026, 9, 1, tzinfo=UTC))
    state = _fully_set_up_state(
        enrolment_active=False,
        seed_count=0,
        seeds_fresh=False,
        awaiting_feedback_count=2,
    )
    specs = desired(state, (event,), w)
    assert [s.kind for s in specs] == [
        "complete_enrolment",
        "add_seed_photos",
        "respond_to_hits",
        "run_priority_scan",
    ]
