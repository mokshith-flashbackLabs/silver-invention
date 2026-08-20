"""Admin score/journal read route — behaviour over an in-memory fake (Task 15).

Same convention as ``tests/test_admin_threat_routes.py`` and
``tests/test_admin_review_routes.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from imageshield.http.app import create_app
from imageshield.types import UserRef
from tests.conftest import ADMIN_SERVICE_TOKEN, SERVICE_TOKEN, make_config

AUTH = {"X-Service-Token": SERVICE_TOKEN}
ADMIN = {**AUTH, "X-Admin-Service-Token": ADMIN_SERVICE_TOKEN}


class FakeScoreStore:
    def __init__(
        self,
        *,
        score: dict[str, Any] | None = None,
        events: list[dict[str, Any]] | None = None,
    ) -> None:
        self._score = score
        self._events = events if events is not None else []
        self.list_events_calls: list[tuple[UserRef, int]] = []

    async def recompute(self, user_ref: UserRef, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def get_score(self, user_ref: UserRef) -> dict[str, Any] | None:
        return self._score

    async def list_events(
        self, user_ref: UserRef, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        self.list_events_calls.append((user_ref, limit))
        return self._events

    async def all_subject_refs(self) -> tuple[UserRef, ...]:
        raise NotImplementedError

    async def expire_due_threat_events(self, *, now: Any) -> int:
        raise NotImplementedError


def _score_row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "score": 72,
        "components": {"posture": 20, "coverage": 20, "exposure": 12, "threat": 20},
        "config_version": "score-v1",
        "computed_at": datetime(2026, 8, 20, tzinfo=UTC),
    }
    base.update(overrides)
    return base


def _event_row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "score_event_id": 1,
        "delta": -5,
        "component": "exposure",
        "cause_kind": "review_decision",
        "cause_ref": str(uuid4()),
        "config_version": "score-v1",
        "score_after": 72,
        "created_at": datetime(2026, 8, 20, tzinfo=UTC),
    }
    base.update(overrides)
    return base


def make_client(
    *, score: dict[str, Any] | None = None, events: list[dict[str, Any]] | None = None
) -> tuple[TestClient, FakeScoreStore]:
    app = create_app(config=make_config())
    store = FakeScoreStore(score=score, events=events)
    app.state.score_store = store
    return TestClient(app), store


def test_route_needs_both_tokens() -> None:
    client, _store = make_client(score=_score_row())
    user_ref = uuid4()

    assert client.get(f"/v1/admin/scores/{user_ref}").status_code == 401
    assert client.get(f"/v1/admin/scores/{user_ref}", headers=AUTH).status_code == 401


def test_happy_path_returns_score_and_events() -> None:
    events = [_event_row()]
    client, store = make_client(score=_score_row(), events=events)
    user_ref = uuid4()

    response = client.get(f"/v1/admin/scores/{user_ref}", headers=ADMIN)

    assert response.status_code == 200
    body = response.json()
    assert body["score"]["score"] == 72
    assert body["score"]["components"] == {
        "posture": 20, "coverage": 20, "exposure": 12, "threat": 20
    }
    assert body["score"]["config_version"] == "score-v1"
    assert len(body["events"]) == 1
    assert body["events"][0]["cause_kind"] == "review_decision"
    assert store.list_events_calls == [(UserRef(user_ref), 50)]


def test_404_when_no_score_row() -> None:
    client, _store = make_client(score=None)
    user_ref = uuid4()

    response = client.get(f"/v1/admin/scores/{user_ref}", headers=ADMIN)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "score_not_found"
