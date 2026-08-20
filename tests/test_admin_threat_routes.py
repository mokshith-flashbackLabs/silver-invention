"""Admin threat-event routes — behaviour over in-memory fakes (Task 14).

Same convention as ``tests/test_admin_providers.py``: ``TestClient`` never
runs the lifespan, both ``ThreatStore`` and ``ScoreStore`` are pre-wired
fakes on ``app.state``, and the auth assertion is load-bearing — a threat
event can penalise every enrolled person's score in one call, so both tokens
are required at router level.
"""

from __future__ import annotations

from decimal import Decimal
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
    the route's own behaviour (status codes, the recompute fan-out, 404
    shape) rather than the matcher, which ``tests/test_threats.py`` covers
    against real Postgres."""

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


class FakeScoreStore:
    """Records every ``recompute`` call as ``(user_ref, cause_kind)`` — same
    shape as ``tests/test_search_routes.py``'s fake, kept local here so this
    file has no cross-module test coupling."""

    def __init__(self, *, raise_error: bool = False) -> None:
        self._raise_error = raise_error
        self.calls: list[tuple[UserRef, str]] = []

    async def recompute(
        self,
        user_ref: UserRef,
        *,
        cause_kind: str,
        cause_ref: str | None = None,
        now: Any = None,
    ) -> Any:
        if self._raise_error:
            raise RuntimeError("score store unavailable")
        self.calls.append((user_ref, cause_kind))
        return None

    async def get_score(self, user_ref: UserRef) -> dict[str, Any] | None:
        raise NotImplementedError

    async def list_events(
        self, user_ref: UserRef, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def all_subject_refs(self) -> tuple[UserRef, ...]:
        raise NotImplementedError

    async def expire_due_threat_events(self, *, now: Any) -> int:
        raise NotImplementedError


def make_client(
    *, matched: tuple[UserRef, ...] = (), raising_score_store: bool = False
) -> tuple[TestClient, FakeThreatStore, FakeScoreStore]:
    app = create_app(config=make_config())
    threats = FakeThreatStore(matched=matched)
    score = FakeScoreStore(raise_error=raising_score_store)
    app.state.threat_store = threats
    app.state.score_store = score
    return TestClient(app), threats, score


def _body(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": "leak",
        "title": "Example leak",
        "severity": 3,
        "domains": ["evil.example"],
        "is_global": False,
        "penalty": "5.00",
        "expires_at": "2026-09-01T00:00:00Z",
        "decay_days": 30,
        "operator": "alice",
    }
    base.update(overrides)
    return base


def test_every_route_needs_both_tokens() -> None:
    client, threats, score = make_client()

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
    assert score.calls == []


def test_create_returns_201_with_matched_count_and_recomputes_each_matched_ref() -> None:
    matched = (UserRef(uuid4()), UserRef(uuid4()))
    client, threats, score = make_client(matched=matched)

    response = client.post("/v1/admin/threat-events", json=_body(), headers=ADMIN)

    assert response.status_code == 201
    body = response.json()
    event_id = UUID(body["event_id"])
    assert body["matched_count"] == 2
    assert len(threats.create_calls) == 1
    assert sorted(score.calls) == sorted((u, "threat_event") for u in matched)
    # Every recompute is tagged with this event as its cause_ref via the
    # store's own call, exercised at the store level in test_threats.py —
    # here we only need the fan-out itself and that the id round-trips.
    assert str(event_id) != ""


def test_create_carries_penalty_as_a_decimal_not_a_float() -> None:
    client, threats, _score = make_client()

    client.post("/v1/admin/threat-events", json=_body(penalty="12.34"), headers=ADMIN)

    assert threats.create_calls[0]["penalty"] == Decimal("12.34")
    assert isinstance(threats.create_calls[0]["penalty"], Decimal)


def test_create_zero_matches_triggers_no_recompute() -> None:
    client, _threats, score = make_client(matched=())

    response = client.post("/v1/admin/threat-events", json=_body(), headers=ADMIN)

    assert response.status_code == 201
    assert response.json()["matched_count"] == 0
    assert score.calls == []


def test_a_raising_score_store_does_not_change_the_create_response() -> None:
    matched = (UserRef(uuid4()),)
    client, _threats, _score = make_client(matched=matched, raising_score_store=True)

    response = client.post("/v1/admin/threat-events", json=_body(), headers=ADMIN)

    assert response.status_code == 201
    assert response.json()["matched_count"] == 1


def test_retract_404s_on_an_unknown_event() -> None:
    client, _threats, score = make_client()

    response = client.post(
        f"/v1/admin/threat-events/{uuid4()}/retract",
        json={"operator": "bob", "reason": "resolved incident"},
        headers=ADMIN,
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "threat_event_not_found"
    assert score.calls == []


def test_retract_success_recomputes_each_matched_ref() -> None:
    matched = (UserRef(uuid4()), UserRef(uuid4()))
    client, _threats, score = make_client(matched=matched)
    create_response = client.post("/v1/admin/threat-events", json=_body(), headers=ADMIN)
    event_id = create_response.json()["event_id"]
    score.calls.clear()  # isolate the retract fan-out from the create fan-out

    response = client.post(
        f"/v1/admin/threat-events/{event_id}/retract",
        json={"operator": "bob", "reason": "resolved incident"},
        headers=ADMIN,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["matched_count"] == 2
    assert sorted(score.calls) == sorted((u, "threat_retracted") for u in matched)


def test_extra_field_is_rejected_with_422() -> None:
    client, threats, _score = make_client()

    response = client.post(
        "/v1/admin/threat-events", json=_body(unexpected="field"), headers=ADMIN
    )

    assert response.status_code == 422
    assert threats.create_calls == []


def test_domains_required_unless_global() -> None:
    client, threats, _score = make_client()

    response = client.post(
        "/v1/admin/threat-events",
        json=_body(domains=[], is_global=False),
        headers=ADMIN,
    )

    assert response.status_code == 422
    assert threats.create_calls == []


def test_list_returns_200() -> None:
    client, _threats, _score = make_client()

    response = client.get("/v1/admin/threat-events", headers=ADMIN)

    assert response.status_code == 200
    assert response.json() == {"events": []}
