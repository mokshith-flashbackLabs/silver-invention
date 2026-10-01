"""``/v1/admin/intel/protection-events*`` -- shape and error mapping, over a fake store (spec
§4.7). The store itself is tested against Postgres in test_intel_protection_store.py."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from imageshield.http.app import create_app
from imageshield.intel.protection_store import ProtectionRetraction
from tests.conftest import ADMIN_SERVICE_TOKEN, SERVICE_TOKEN, make_config

ADMIN = {"X-Service-Token": SERVICE_TOKEN, "X-Admin-Service-Token": ADMIN_SERVICE_TOKEN}
URL = "/v1/admin/intel/protection-events"


class FakeProtectionStore:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.retractions: dict[UUID, ProtectionRetraction] = {}
        self.retract_calls: list[dict[str, Any]] = []

    async def list_events(self, *, statuses: Any, cursor: Any, limit: int) -> list[dict[str, Any]]:
        self.calls.append({"statuses": statuses, "cursor": cursor, "limit": limit})
        return self.rows[:limit]

    async def retract(
        self, event_id: UUID, *, operator: str, reason: str
    ) -> ProtectionRetraction | None:
        self.retract_calls.append({"event_id": event_id, "operator": operator, "reason": reason})
        return self.retractions.get(event_id)


def _client() -> tuple[TestClient, FakeProtectionStore]:
    app = create_app(config=make_config())
    store = FakeProtectionStore()
    app.state.protection_store = store
    return TestClient(app), store


def _event(**kw: Any) -> dict[str, Any]:
    return {"event_id": uuid4(), "created_at": datetime.now(UTC), "title": "t", **kw}


def test_the_list_passes_its_filter_and_pages() -> None:
    client, store = _client()
    store.rows = [_event(), _event()]
    r = client.get(f"{URL}?status=active&status=retracted&limit=2", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert store.calls[0]["statuses"] == ["active", "retracted"]
    assert len(r.json()["events"]) == 2 and r.json()["next_cursor"] is not None


def test_the_list_without_a_filter_passes_none() -> None:
    client, store = _client()
    r = client.get(URL, headers=ADMIN)
    assert r.status_code == 200 and r.json() == {"events": [], "next_cursor": None}
    assert store.calls[0]["statuses"] is None


def test_an_unknown_status_or_a_malformed_cursor_is_422() -> None:
    client, _ = _client()
    bad_status = client.get(f"{URL}?status=lapsed", headers=ADMIN)
    assert bad_status.status_code == 422
    assert bad_status.json()["error"]["code"] == "validation_error"
    bad_cursor = client.get(f"{URL}?cursor=nope", headers=ADMIN)
    assert bad_cursor.status_code == 422
    assert bad_cursor.json()["error"]["code"] == "invalid_cursor"


def test_a_retraction_answers_what_it_retracted() -> None:
    client, store = _client()
    event_id, renewal, pending = uuid4(), uuid4(), uuid4()
    store.retractions[event_id] = ProtectionRetraction(event_id, (renewal,), (pending,))
    r = client.post(
        f"{URL}/{event_id}/retract", headers=ADMIN, json={"operator": "ann", "reason": "withdrawn"}
    )
    assert r.status_code == 200, r.text
    assert r.json() == {
        "event_id": str(event_id),
        "status": "retracted",
        "also_retracted": [str(renewal)],
        "renewal_proposals_rejected": [str(pending)],
    }
    assert store.retract_calls == [{"event_id": event_id, "operator": "ann", "reason": "withdrawn"}]


def test_a_repeat_retraction_answers_200_with_nothing_more_retracted() -> None:
    client, store = _client()
    event_id = uuid4()
    store.retractions[event_id] = ProtectionRetraction(event_id, (), (), already_retracted=True)
    r = client.post(
        f"{URL}/{event_id}/retract", headers=ADMIN, json={"operator": "ann", "reason": "again"}
    )
    assert r.status_code == 200
    assert r.json()["also_retracted"] == [] and r.json()["renewal_proposals_rejected"] == []


def test_an_unknown_credit_is_404_protection_event_not_found() -> None:
    client, _ = _client()
    r = client.post(
        f"{URL}/{uuid4()}/retract", headers=ADMIN, json={"operator": "ann", "reason": "withdrawn"}
    )
    assert r.status_code == 404 and r.json()["error"]["code"] == "protection_event_not_found"


@pytest.mark.parametrize(
    "body",
    [
        {"reason": "withdrawn"},  # no operator
        {"operator": "ann", "reason": "no"},  # under 3 characters
        {"operator": "ann", "reason": "withdrawn", "extra": 1},
    ],
)
def test_a_malformed_retraction_is_422(body: dict[str, Any]) -> None:
    client, store = _client()
    r = client.post(f"{URL}/{uuid4()}/retract", headers=ADMIN, json=body)
    assert r.status_code == 422 and store.retract_calls == []


def test_both_tokens_are_required() -> None:
    client, _ = _client()
    r = client.get(URL, headers={"X-Service-Token": SERVICE_TOKEN})
    assert r.status_code == 401
