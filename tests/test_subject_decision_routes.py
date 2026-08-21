"""``POST /v1/infringements/{id}/decision`` over fakes (repo convention:
TestClient never runs the lifespan; the transaction is proven against real
Postgres in tests/test_review.py).

The route's own duties: the 404 oracle, the 409 on conflict, the closed
two-value decision vocabulary (``extra='forbid'``), and the swallow-and-log
score recompute that fires exactly once — on 'decided', never on replay."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from imageshield.http.app import create_app
from imageshield.review.store import SubjectDecisionOutcome
from imageshield.types import UserRef
from tests.conftest import SERVICE_TOKEN, make_config

AUTH = {"X-Service-Token": SERVICE_TOKEN}


class FakeReviewStore:
    def __init__(self, outcome: SubjectDecisionOutcome | None) -> None:
        self._outcome = outcome
        self.calls: list[tuple[UUID, UserRef, str]] = []

    async def subject_decide(
        self, infringement_id: UUID, *, user_ref: UserRef, decision: str
    ) -> SubjectDecisionOutcome | None:
        self.calls.append((infringement_id, user_ref, decision))
        return self._outcome

    async def next_task(self) -> dict[str, Any] | None:
        raise NotImplementedError

    async def queue_depth(self) -> dict[str, int]:
        raise NotImplementedError

    async def decide(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


class FakeScoreStore:
    def __init__(self, *, raise_error: bool = False) -> None:
        self._raise_error = raise_error
        self.calls: list[tuple[UserRef, str, str | None]] = []

    async def recompute(
        self,
        user_ref: UserRef,
        *,
        cause_kind: str,
        cause_ref: str | None = None,
        now: Any = None,
    ) -> None:
        if self._raise_error:
            raise RuntimeError("score store unavailable")
        self.calls.append((user_ref, cause_kind, cause_ref))


def make_client(
    outcome: SubjectDecisionOutcome | None,
    *,
    raising_score_store: bool = False,
) -> tuple[TestClient, FakeReviewStore, FakeScoreStore]:
    app = create_app(config=make_config())
    store = FakeReviewStore(outcome)
    score_store = FakeScoreStore(raise_error=raising_score_store)
    app.state.review_store = store
    app.state.score_store = score_store
    return TestClient(app), store, score_store


def _outcome(kind: str, decision: str = "confirmed") -> SubjectDecisionOutcome:
    return SubjectDecisionOutcome(
        infringement_id=uuid4(),
        decision=decision,
        severity="benign_copy",
        outcome=kind,
    )


def _post(client: TestClient, infringement_id: UUID, body: dict[str, Any]):
    return client.post(
        f"/v1/infringements/{infringement_id}/decision", json=body, headers=AUTH
    )


def test_a_decided_answer_returns_200_and_recomputes_the_score() -> None:
    client, store, score_store = make_client(_outcome("decided"))
    infringement_id, user_ref = uuid4(), uuid4()

    response = _post(
        client, infringement_id, {"user_ref": str(user_ref), "decision": "confirmed"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "confirmed"
    assert body["severity"] == "benign_copy"
    assert body["idempotent_replay"] is False
    assert store.calls == [(infringement_id, user_ref, "confirmed")]
    assert score_store.calls == [(user_ref, "subject_decision", str(infringement_id))]


def test_a_replay_is_200_and_recomputes_nothing() -> None:
    client, _store, score_store = make_client(_outcome("replay"))

    response = _post(
        client, uuid4(), {"user_ref": str(uuid4()), "decision": "confirmed"}
    )

    assert response.status_code == 200
    assert response.json()["idempotent_replay"] is True
    assert score_store.calls == []


def test_a_conflict_is_409_and_not_retryable() -> None:
    client, _store, score_store = make_client(_outcome("conflict", decision="rejected"))

    response = _post(
        client, uuid4(), {"user_ref": str(uuid4()), "decision": "confirmed"}
    )

    assert response.status_code == 409
    body = response.json()["error"]
    assert body["code"] == "decision_conflict"
    assert body["retryable"] is False
    assert score_store.calls == []


def test_absent_and_not_yours_are_the_same_404() -> None:
    client, _store, _score = make_client(None)

    response = _post(
        client, uuid4(), {"user_ref": str(uuid4()), "decision": "rejected"}
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "infringement_not_found"


def test_uncertain_is_not_a_decision() -> None:
    client, store, _score = make_client(_outcome("decided"))

    response = _post(
        client, uuid4(), {"user_ref": str(uuid4()), "decision": "uncertain"}
    )

    assert response.status_code == 422
    assert store.calls == []


def test_extra_fields_are_rejected() -> None:
    client, store, _score = make_client(_outcome("decided"))

    response = _post(
        client,
        uuid4(),
        {"user_ref": str(uuid4()), "decision": "confirmed", "severity": "benign_copy"},
    )

    assert response.status_code == 422
    assert store.calls == []


def test_a_raising_score_store_never_changes_the_response() -> None:
    client, _store, _score = make_client(_outcome("decided"), raising_score_store=True)

    response = _post(
        client, uuid4(), {"user_ref": str(uuid4()), "decision": "confirmed"}
    )

    assert response.status_code == 200
