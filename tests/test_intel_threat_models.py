"""Threat-event proposal shapes (spec §3.6, §4.5, note 2026-09-30). Pure: no database."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from imageshield.intel.proposal_models import (
    DecisionRefused,
    GapRegenerateRequest,
    ThreatEventDecided,
    ThreatEventSuggested,
    ThreatEventTarget,
    ThreatEventValues,
)

GOOD: dict[str, Any] = {
    "kind": "leak",
    "title": "Instagram breach exposes private photos",
    "body": "Photos some Instagram users kept private were exposed in a breach.",
    "severity": 3,
    "expires_in_days": 30,
    "tags": ["instagram"],
}


def test_a_complete_threat_decision_parses_and_dumps_as_stored() -> None:
    assert ThreatEventDecided.model_validate(GOOD).model_dump(mode="json") == GOOD


def test_a_threat_written_before_bodies_reads_as_an_empty_trimmed_body() -> None:
    """spec 2026-10-06-intel-event-body: a stored suggestion with no body still parses."""
    without = {k: v for k, v in GOOD.items() if k != "body"}
    assert ThreatEventDecided.model_validate(without).body == ""
    assert ThreatEventDecided.model_validate({**GOOD, "body": "  padded  "}).body == "padded"


@pytest.mark.parametrize(
    "bad",
    [
        {"severity": 0},
        {"severity": 6},
        {"severity": 2.5},
        {"severity": True},
        {"severity": "3"},
        {"expires_in_days": 0},
        {"expires_in_days": 91},
        {"kind": "tsunami"},
        {"title": ""},
        {"title": "   "},
        {"title": "x" * 201},
        {"tags": []},
        {"tags": ["Instagram"]},
        {"tags": ["x", "x"]},
        {"body": "x" * 401},  # editable since 2026-10-06, up to 400 characters
    ],
)
def test_a_threat_decision_outside_the_bounds_is_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ThreatEventDecided.model_validate({**GOOD, **bad})


@pytest.mark.parametrize(
    "edge",
    [
        {"severity": 1},
        {"severity": 5},
        {"expires_in_days": 1},
        {"expires_in_days": 90},
        {"title": "x" * 200},
        {"kind": "deepfake_wave"},
        {"kind": "platform_incident"},
        {"kind": "other"},
        {"tags": ["a", "b_2", "x" * 40]},
    ],
)
def test_a_threat_decision_on_a_bound_is_accepted(edge: dict[str, Any]) -> None:
    """The refusals above pin the outside of each bound; this pins the inside edge, so a
    ``gt`` written for a ``ge`` (a severity of 1 refused) cannot pass."""
    expected = {**GOOD, **edge}
    assert ThreatEventDecided.model_validate(expected).model_dump(mode="json") == expected


def test_suggested_is_the_decided_keys_without_tags() -> None:
    fields = {k: v for k, v in GOOD.items() if k != "tags"}
    assert ThreatEventSuggested.model_validate(fields).model_dump() == fields
    with pytest.raises(ValidationError):  # tags belong to target and decided
        ThreatEventSuggested.model_validate(GOOD)


def test_values_are_any_subset_of_the_decided_keys_and_nothing_else() -> None:
    assert ThreatEventValues.model_validate({"severity": 5}).model_dump(exclude_unset=True) == {
        "severity": 5
    }
    assert ThreatEventValues.model_validate({"body": "b"}).model_dump(exclude_unset=True) == {
        "body": "b"
    }
    assert ThreatEventValues.model_validate({}).model_dump(exclude_unset=True) == {}
    with pytest.raises(ValidationError):
        ThreatEventValues.model_validate({"delta": 1})


def test_a_threat_target_is_one_or_more_distinct_slugs() -> None:
    assert ThreatEventTarget.model_validate({"tags": ["x"]}).tags == ("x",)
    for bad in ([], ["X"], ["x", "x"]):
        with pytest.raises(ValidationError):
            ThreatEventTarget.model_validate({"tags": bad})


def test_a_gap_regenerate_request_parses_from_its_jsonb() -> None:
    gap, signal = uuid4(), uuid4()
    request = GapRegenerateRequest.model_validate(
        {"coverage_gap_id": str(gap), "tag": "linkedin", "signal_ids": [str(signal)]}
    )
    assert (request.coverage_gap_id, request.tag, request.signal_ids) == (
        gap,
        "linkedin",
        (signal,),
    )


def test_a_tag_refusal_carries_its_slugs() -> None:
    refused = DecisionRefused("unknown_tag", "a tag is not registered", slugs=("tiktok",))
    assert (refused.code, refused.slugs) == ("unknown_tag", ("tiktok",))
    assert DecisionRefused("proposal_not_found", "no").slugs == ()
