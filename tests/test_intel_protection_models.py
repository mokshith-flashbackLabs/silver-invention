"""Protection-event proposal shapes (spec §3.6, §4.5, §4.7, notes 2026-09-30). Pure: no database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from imageshield.intel.proposal_models import (
    LiveProtection,
    ProtectionEventDecided,
    ProtectionEventSuggested,
    ProtectionEventTarget,
    ProtectionEventValues,
    RenewalRequest,
)

GOOD: dict[str, Any] = {
    "title": "Instagram lets people keep their photos out of AI training",
    "strength": 2,
    "review_in_days": 180,
    "tags": ["instagram"],
    "is_global": False,
    "applies_regardless_of_location": True,
}


def test_a_complete_protection_decision_parses_and_dumps_as_stored() -> None:
    assert ProtectionEventDecided.model_validate(GOOD).model_dump(mode="json") == GOOD


def test_a_global_decision_carries_no_tags() -> None:
    everyone = {**GOOD, "tags": [], "is_global": True}
    assert ProtectionEventDecided.model_validate(everyone).model_dump(mode="json") == everyone


@pytest.mark.parametrize(
    "bad",
    [
        {"strength": 0},
        {"strength": 6},
        {"strength": 2.5},
        {"strength": True},
        {"strength": "2"},
        {"review_in_days": 29},
        {"review_in_days": 367},
        {"title": ""},
        {"title": "   "},
        {"title": "x" * 201},
        {"tags": []},  # tag-scoped with no tags reaches nobody
        {"is_global": True},  # both scopes at once
        {"tags": ["Instagram"]},
        {"tags": ["x", "x"]},
        {"is_global": "yes"},
        {"applies_regardless_of_location": False},
        {"body": "a protection carries no body"},
        {"renews_event_id": str(uuid4())},  # what a renewal continues lives in its target
    ],
)
def test_a_protection_decision_outside_the_bounds_is_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ProtectionEventDecided.model_validate({**GOOD, **bad})


def test_the_location_attestation_is_required() -> None:
    fields = {k: v for k, v in GOOD.items() if k != "applies_regardless_of_location"}
    with pytest.raises(ValidationError):
        ProtectionEventDecided.model_validate(fields)


def test_suggested_is_the_title_strength_and_review_period_only() -> None:
    fields = {"title": GOOD["title"], "strength": 2, "review_in_days": 180}
    assert ProtectionEventSuggested.model_validate(fields).model_dump() == fields
    with pytest.raises(ValidationError):
        ProtectionEventSuggested.model_validate({**fields, "tags": ["instagram"]})


def test_a_target_is_tags_or_global_and_may_name_what_it_renews() -> None:
    renewed = uuid4()
    assert ProtectionEventTarget.model_validate({"tags": ["x"], "is_global": False}).tags == ("x",)
    target = ProtectionEventTarget.model_validate(
        {"tags": [], "is_global": True, "renews_event_id": str(renewed)}
    )
    assert target.is_global and target.renews_event_id == renewed
    for bad in (
        {"tags": [], "is_global": False},
        {"tags": ["x"], "is_global": True},
        {"tags": ["X"]},
    ):
        with pytest.raises(ValidationError):
            ProtectionEventTarget.model_validate(bad)


def test_values_are_any_subset_of_the_editable_keys_and_nothing_else() -> None:
    assert ProtectionEventValues.model_validate({"strength": 4}).model_dump(exclude_unset=True) == {
        "strength": 4
    }
    assert ProtectionEventValues.model_validate({}).model_dump(exclude_unset=True) == {}
    for bad in (
        {"applies_regardless_of_location": True},  # a body field, never a value
        {"renews_event_id": str(uuid4())},
        {"severity": 3},
    ):
        with pytest.raises(ValidationError):
            ProtectionEventValues.model_validate(bad)


def test_a_live_protection_names_its_direction_on_the_detail_read() -> None:
    starts = datetime.now(UTC)
    live = LiveProtection(
        uuid4(),
        "Opt-out",
        2,
        ("instagram",),
        False,
        starts,
        starts + timedelta(days=90),
        uuid4(),
        None,
        (),
    )
    related = live.related()
    assert related["direction"] == "protection" and related["kind"] == "protection"
    assert related["strength"] == 2 and related["tags"] == ["instagram"]
    assert set(related) == {
        "event_id",
        "direction",
        "kind",
        "title",
        "strength",
        "tags",
        "is_global",
        "starts_at",
        "review_by",
        "proposal_id",
    }


def test_a_renewal_request_parses_from_its_jsonb() -> None:
    event = uuid4()
    assert RenewalRequest.model_validate({"event_id": str(event)}).event_id == event
    with pytest.raises(ValidationError):
        RenewalRequest.model_validate({})
