"""``/v1/admin/intel/proposals*`` -- shape and error mapping, over fakes (spec 4.7). The
decision logic itself is tested against Postgres in test_intel_decisions.py."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from imageshield.http.app import create_app
from imageshield.intel.proposal_models import AppliedResult, Decided, DecisionRefused
from tests.conftest import ADMIN_SERVICE_TOKEN, SERVICE_TOKEN, make_config

ADMIN = {"X-Service-Token": SERVICE_TOKEN, "X-Admin-Service-Token": ADMIN_SERVICE_TOKEN}


def _row(**kw: Any) -> dict[str, Any]:
    return {
        "proposal_id": uuid4(),
        "kind": "weight_change",
        "status": "pending",
        "created_at": datetime.now(UTC),
        "target": {},
        "signal_ids": [],
        **kw,
    }


class FakeProposalStore:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []

    async def list_proposals(
        self, *, statuses: Any, kinds: Any, cursor: Any, limit: int
    ) -> list[dict[str, Any]]:
        self.calls.append({"statuses": statuses, "kinds": kinds, "cursor": cursor, "limit": limit})
        return self.rows[:limit]

    async def get_proposal(self, proposal_id: UUID) -> dict[str, Any] | None:
        return next((r for r in self.rows if r["proposal_id"] == proposal_id), None)


class FakeDecisionStore:
    def __init__(self) -> None:
        self.refuse: str | None = None
        self.slugs: tuple[str, ...] = ()
        self.result: Decided | None = None
        self.decisions: list[dict[str, Any]] = []

    async def decide(self, proposal_id: UUID, **kw: Any) -> Decided:
        self.decisions.append({"proposal_id": proposal_id, **kw})
        if self.refuse is not None:
            raise DecisionRefused(self.refuse, "refused", slugs=self.slugs)  # type: ignore[arg-type]
        if self.result is not None:
            return self.result
        return Decided(
            proposal_id, "weight_change", kw["decision"], None, kw["values"] or {"delta": 1}
        )

    async def mark_applied(
        self, *, scoring_version: str, proposal_ids: tuple[UUID, ...]
    ) -> AppliedResult:
        return AppliedResult(proposal_ids[:1], (), proposal_ids[1:])


def _client() -> tuple[TestClient, FakeProposalStore, FakeDecisionStore]:
    app = create_app(config=make_config())
    proposals, decisions = FakeProposalStore(), FakeDecisionStore()
    app.state.proposal_store = proposals
    app.state.decision_store = decisions
    return TestClient(app), proposals, decisions


def _decision(**kw: Any) -> dict[str, Any]:
    return {"decision": "approved", "reason": "checked it", "operator": "ann", **kw}


def test_the_list_passes_repeated_filters_and_pages() -> None:
    client, proposals, _ = _client()
    proposals.rows = [_row(), _row()]
    r = client.get(
        "/v1/admin/intel/proposals?status=pending&status=approved&kind=weight_change&limit=2",
        headers=ADMIN,
    )
    assert r.status_code == 200, r.text
    assert proposals.calls[0]["statuses"] == ["pending", "approved"]
    assert proposals.calls[0]["kinds"] == ["weight_change"]
    assert len(r.json()["proposals"]) == 2 and r.json()["next_cursor"] is not None


def test_the_list_without_filters_passes_none() -> None:
    client, proposals, _ = _client()
    r = client.get("/v1/admin/intel/proposals", headers=ADMIN)
    assert r.status_code == 200 and r.json() == {"proposals": [], "next_cursor": None}
    assert proposals.calls[0]["statuses"] is None and proposals.calls[0]["kinds"] is None


@pytest.mark.parametrize("query", ["kind=threat", "status=open"])
def test_an_unknown_enum_value_is_422(query: str) -> None:
    client, _, _ = _client()
    r = client.get(f"/v1/admin/intel/proposals?{query}", headers=ADMIN)
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


def test_a_malformed_cursor_is_422_invalid_cursor() -> None:
    client, _, _ = _client()
    r = client.get("/v1/admin/intel/proposals?cursor=nope", headers=ADMIN)
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_cursor"


def test_the_detail_is_404_proposal_not_found_for_an_unknown_id() -> None:
    client, proposals, _ = _client()
    row = _row()
    proposals.rows = [row]
    assert (
        client.get(f"/v1/admin/intel/proposals/{row['proposal_id']}", headers=ADMIN).status_code
        == 200
    )
    r = client.get(f"/v1/admin/intel/proposals/{uuid4()}", headers=ADMIN)
    assert r.status_code == 404 and r.json()["error"]["code"] == "proposal_not_found"


def test_a_decision_answers_the_spec_body() -> None:
    client, _, decisions = _client()
    pid = uuid4()
    r = client.post(
        f"/v1/admin/intel/proposals/{pid}/decision",
        headers=ADMIN,
        json=_decision(values={"delta": -1}),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {
        "proposal_id": str(pid),
        "kind": "weight_change",
        "status": "approved",
        "applied_ref": None,
        "decided": {"delta": -1},
        "superseded": [],
    }
    assert decisions.decisions[0]["operator"] == "ann"


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("proposal_not_found", 404),
        ("proposal_not_pending", 409),
        ("proposal_not_decidable", 409),
        ("proposal_evidence_retracted", 409),
        ("proposal_evidence_stale", 409),
        ("proposal_uncorroborated", 409),
        ("proposal_tags_unmapped", 409),
        ("proposal_cell_awaiting_publish", 409),
        ("values_out_of_bounds", 422),
        ("unknown_tag", 422),
        ("tag_retired", 422),
    ],
)
def test_every_refusal_maps_to_its_status_by_name(code: str, status: int) -> None:
    client, _, decisions = _client()
    decisions.refuse = code
    r = client.post(
        f"/v1/admin/intel/proposals/{uuid4()}/decision", headers=ADMIN, json=_decision()
    )
    assert r.status_code == status and r.json()["error"]["code"] == code


def test_a_tag_refusal_names_its_slugs() -> None:
    client, _, decisions = _client()
    decisions.refuse, decisions.slugs = "unknown_tag", ("tiktok",)
    r = client.post(
        f"/v1/admin/intel/proposals/{uuid4()}/decision",
        headers=ADMIN,
        json=_decision(values={"tags": ["tiktok"]}),
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "unknown_tag"
    assert r.json()["error"]["slugs"] == ["tiktok"]


def test_a_threat_approval_answers_applied_with_the_event_id() -> None:
    client, _, decisions = _client()
    pid, event_id = uuid4(), uuid4()
    decided = {"kind": "leak", "title": "t", "severity": 3, "expires_in_days": 30, "tags": ["x"]}
    decisions.result = Decided(pid, "threat_event", "applied", str(event_id), decided)
    r = client.post(f"/v1/admin/intel/proposals/{pid}/decision", headers=ADMIN, json=_decision())
    assert r.status_code == 200, r.text
    assert r.json() == {
        "proposal_id": str(pid),
        "kind": "threat_event",
        "status": "applied",
        "applied_ref": str(event_id),
        "decided": decided,
        "superseded": [],
    }


@pytest.mark.parametrize(
    "body",
    [
        _decision(decision="rejected", values={"delta": 1}),  # values on a rejection
        {"decision": "approved", "reason": "checked it"},  # no operator
        _decision(decision="maybe"),
        _decision(reason="ok"),  # under 3 characters
    ],
)
def test_a_malformed_decision_body_is_422_validation_error(body: dict[str, Any]) -> None:
    client, _, decisions = _client()
    r = client.post(f"/v1/admin/intel/proposals/{uuid4()}/decision", headers=ADMIN, json=body)
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    assert decisions.decisions == []


def test_applied_takes_no_operator_and_answers_three_lists() -> None:
    client, _, _ = _client()
    ids = [str(uuid4()), str(uuid4())]
    r = client.post(
        "/v1/admin/intel/proposals/applied",
        headers=ADMIN,
        json={"scoring_version": "likeness-health-v13", "proposal_ids": ids},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"applied": ids[:1], "already_applied": [], "not_applied": ids[1:]}
    stray = client.post(
        "/v1/admin/intel/proposals/applied",
        headers=ADMIN,
        json={"scoring_version": "s", "proposal_ids": ids, "operator": "x"},
    )
    assert stray.status_code == 422
    empty = client.post(
        "/v1/admin/intel/proposals/applied",
        headers=ADMIN,
        json={"scoring_version": "s", "proposal_ids": []},
    )
    assert empty.status_code == 422


def test_the_location_attestation_reaches_the_decision() -> None:
    client, _, decisions = _client()
    r = client.post(
        f"/v1/admin/intel/proposals/{uuid4()}/decision",
        headers=ADMIN,
        json=_decision(applies_regardless_of_location=True),
    )
    assert r.status_code == 200, r.text
    assert decisions.decisions[0]["applies_regardless_of_location"] is True
    client.post(f"/v1/admin/intel/proposals/{uuid4()}/decision", headers=ADMIN, json=_decision())
    assert decisions.decisions[1]["applies_regardless_of_location"] is None


def test_the_location_attestation_must_be_a_json_boolean() -> None:
    client, _, decisions = _client()
    r = client.post(
        f"/v1/admin/intel/proposals/{uuid4()}/decision",
        headers=ADMIN,
        json=_decision(applies_regardless_of_location="true"),
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    assert decisions.decisions == []


def test_a_protection_approval_answers_applied_with_the_event_id() -> None:
    client, _, decisions = _client()
    pid, event_id = uuid4(), uuid4()
    decided = {
        "title": "t",
        "strength": 2,
        "review_in_days": 180,
        "tags": ["x"],
        "is_global": False,
        "applies_regardless_of_location": True,
    }
    decisions.result = Decided(pid, "protection_event", "applied", str(event_id), decided)
    r = client.post(
        f"/v1/admin/intel/proposals/{pid}/decision",
        headers=ADMIN,
        json=_decision(applies_regardless_of_location=True),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {
        "proposal_id": str(pid),
        "kind": "protection_event",
        "status": "applied",
        "applied_ref": str(event_id),
        "decided": decided,
        "superseded": [],
    }


def test_an_approval_names_the_proposals_it_covered() -> None:
    """2026-10-04: the pending proposals an approval superseded covered_by_decision."""
    client, _, decisions = _client()
    pid, covered = uuid4(), uuid4()
    decisions.result = Decided(pid, "weight_change", "approved", None, {"delta": 1}, (covered,))
    r = client.post(f"/v1/admin/intel/proposals/{pid}/decision", headers=ADMIN, json=_decision())
    assert r.status_code == 200, r.text
    assert r.json()["superseded"] == [str(covered)]
