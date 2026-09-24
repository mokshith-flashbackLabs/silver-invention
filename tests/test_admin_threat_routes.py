"""Admin threat-event routes — behaviour over in-memory fakes (Task 14).

Same convention as ``tests/test_admin_providers.py``: ``TestClient`` never
runs the lifespan, ``ThreatStore`` is a pre-wired fake on ``app.state``, and
the auth assertion is load-bearing — a threat event can name hundreds of
``user_ref``s in one call, so both tokens are required at router level.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from imageshield.http.app import create_app
from imageshield.types import UserRef
from tests.conftest import ADMIN_SERVICE_TOKEN, SERVICE_TOKEN, make_config

AUTH = {"X-Service-Token": SERVICE_TOKEN}
ADMIN = {**AUTH, "X-Admin-Service-Token": ADMIN_SERVICE_TOKEN}


class FakeThreatStore:
    """Records every create/retract call. ``matched`` is what ``create_event``
    hands back for every call — enough for these route tests, which are about
    the route's own behaviour (status codes, ``matched_count``, 404 shape)
    rather than the matcher, which ``tests/test_threats.py`` covers against
    real Postgres."""

    def __init__(self, *, matched: tuple[UserRef, ...] = ()) -> None:
        self._matched = matched
        self.create_calls: list[dict[str, Any]] = []
        self.retract_calls: list[tuple[UUID, str, str]] = []
        self._active: dict[UUID, tuple[UserRef, ...]] = {}

    async def create_event(self, **kwargs: Any) -> tuple[UUID, tuple[UserRef, ...]]:
        self.create_calls.append(kwargs)
        event_id = uuid4()
        self._active[event_id] = self._matched
        return event_id, self._matched

    async def retract_event(
        self, event_id: UUID, *, operator: str, reason: str
    ) -> tuple[UserRef, ...] | None:
        self.retract_calls.append((event_id, operator, reason))
        return self._active.pop(event_id, None)

    async def list_events(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return []


def make_client(*, matched: tuple[UserRef, ...] = ()) -> tuple[TestClient, FakeThreatStore]:
    app = create_app(config=make_config())
    threats = FakeThreatStore(matched=matched)
    app.state.threat_store = threats
    return TestClient(app), threats


def _body(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": "leak",
        "title": "Example leak",
        "severity": 3,
        "domains": ["evil.example"],
        "is_global": False,
        "expires_at": "2026-09-01T00:00:00Z",
        "decay_days": 30,
        "operator": "alice",
    }
    base.update(overrides)
    return base


def test_every_route_needs_both_tokens() -> None:
    client, threats = make_client()

    create_body = _body()
    assert client.post("/v1/admin/threat-events", json=create_body).status_code == 401
    assert (
        client.post("/v1/admin/threat-events", json=create_body, headers=AUTH).status_code
        == 401
    )

    retract_path = f"/v1/admin/threat-events/{uuid4()}/retract"
    retract_body = {"operator": "bob", "reason": "resolved incident"}
    assert client.post(retract_path, json=retract_body).status_code == 401
    assert client.post(retract_path, json=retract_body, headers=AUTH).status_code == 401

    assert client.get("/v1/admin/threat-events").status_code == 401
    assert client.get("/v1/admin/threat-events", headers=AUTH).status_code == 401

    assert threats.create_calls == []
    assert threats.retract_calls == []


def test_create_returns_201_with_matched_count() -> None:
    matched = (UserRef(uuid4()), UserRef(uuid4()))
    client, threats = make_client(matched=matched)

    response = client.post("/v1/admin/threat-events", json=_body(), headers=ADMIN)

    assert response.status_code == 201
    body = response.json()
    event_id = UUID(body["event_id"])
    assert body["matched_count"] == 2
    assert len(threats.create_calls) == 1
    assert str(event_id) != ""


def test_create_needs_no_penalty() -> None:
    client, threats = make_client()

    response = client.post("/v1/admin/threat-events", json=_body(), headers=ADMIN)

    assert response.status_code == 201
    assert "penalty" not in threats.create_calls[0]


def test_create_ignores_a_sent_penalty() -> None:
    # An older backend still sends one for a release; new services ignore it.
    client, threats = make_client()

    response = client.post("/v1/admin/threat-events", json=_body(penalty="5.00"), headers=ADMIN)

    assert response.status_code == 201
    assert "penalty" not in threats.create_calls[0]


def test_create_zero_matches_returns_zero_matched_count() -> None:
    client, _threats = make_client(matched=())

    response = client.post("/v1/admin/threat-events", json=_body(), headers=ADMIN)

    assert response.status_code == 201
    assert response.json()["matched_count"] == 0


def test_retract_404s_on_an_unknown_event() -> None:
    client, _threats = make_client()

    response = client.post(
        f"/v1/admin/threat-events/{uuid4()}/retract",
        json={"operator": "bob", "reason": "resolved incident"},
        headers=ADMIN,
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "threat_event_not_found"


def test_retract_success_returns_matched_count() -> None:
    matched = (UserRef(uuid4()), UserRef(uuid4()))
    client, _threats = make_client(matched=matched)
    create_response = client.post("/v1/admin/threat-events", json=_body(), headers=ADMIN)
    event_id = create_response.json()["event_id"]

    response = client.post(
        f"/v1/admin/threat-events/{event_id}/retract",
        json={"operator": "bob", "reason": "resolved incident"},
        headers=ADMIN,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["matched_count"] == 2


def test_extra_field_is_rejected_with_422() -> None:
    client, threats = make_client()

    response = client.post(
        "/v1/admin/threat-events", json=_body(unexpected="field"), headers=ADMIN
    )

    assert response.status_code == 422
    assert threats.create_calls == []


def test_domains_required_unless_global() -> None:
    client, threats = make_client()

    response = client.post(
        "/v1/admin/threat-events",
        json=_body(domains=[], is_global=False),
        headers=ADMIN,
    )

    assert response.status_code == 422
    assert threats.create_calls == []


def test_list_returns_200() -> None:
    client, _threats = make_client()

    response = client.get("/v1/admin/threat-events", headers=ADMIN)

    assert response.status_code == 200
    assert response.json() == {"events": []}
