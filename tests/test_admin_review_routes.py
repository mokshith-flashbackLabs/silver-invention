"""Admin review-queue routes — behaviour over in-memory fakes (Task 15).

Same convention as ``tests/test_admin_threat_routes.py``: ``TestClient``
never runs the lifespan, both ``ReviewStore`` and ``ScoreStore`` are
pre-wired fakes on ``app.state``, and both tokens are required at router
level.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from imageshield.http.app import create_app
from imageshield.review.store import DecisionOutcome
from imageshield.types import UserRef
from tests.conftest import ADMIN_SERVICE_TOKEN, SERVICE_TOKEN, make_config

AUTH = {"X-Service-Token": SERVICE_TOKEN}
ADMIN = {**AUTH, "X-Admin-Service-Token": ADMIN_SERVICE_TOKEN}


class FakeReviewStore:
    def __init__(
        self,
        *,
        task: dict[str, Any] | None = None,
        depths: dict[str, int] | None = None,
        outcome: DecisionOutcome | Exception | None = None,
    ) -> None:
        self._task = task
        self._depths = depths if depths is not None else {}
        self._outcome = outcome
        self.decide_calls: list[dict[str, Any]] = []

    async def next_task(self) -> dict[str, Any] | None:
        return self._task

    async def queue_depth(self) -> dict[str, int]:
        return self._depths

    async def decide(
        self, task_id: UUID, *, decision: str, operator: str, severity: str | None
    ) -> DecisionOutcome | None:
        self.decide_calls.append(
            {"task_id": task_id, "decision": decision, "operator": operator,
             "severity": severity}
        )
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


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
    ) -> Any:
        if self._raise_error:
            raise RuntimeError("score store unavailable")
        self.calls.append((user_ref, cause_kind, cause_ref))
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


def _task(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "task_id": uuid4(),
        "infringement_id": uuid4(),
        "user_ref": UserRef(uuid4()),
        "severity": "ncii_suspected",
        "triage": {"best_face_bbox": {"left": 0.1}},
        "image_url": "https://example.test/a.jpg",
        "page_url": "https://example.test/a",
        "face_match_score": 91.25,
        "source_domain": "example.test",
    }
    base.update(overrides)
    return base


def make_client(
    *,
    task: dict[str, Any] | None = None,
    depths: dict[str, int] | None = None,
    outcome: DecisionOutcome | Exception | None = None,
    raising_score_store: bool = False,
) -> tuple[TestClient, FakeReviewStore, FakeScoreStore]:
    app = create_app(config=make_config())
    review = FakeReviewStore(task=task, depths=depths, outcome=outcome)
    score = FakeScoreStore(raise_error=raising_score_store)
    app.state.review_store = review
    app.state.score_store = score
    return TestClient(app), review, score


def _decision_body(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"decision": "confirmed", "operator": "alice"}
    base.update(overrides)
    return base


def test_every_route_needs_both_tokens() -> None:
    client, review, score = make_client(task=_task())

    assert client.get("/v1/admin/review/next").status_code == 401
    assert client.get("/v1/admin/review/next", headers=AUTH).status_code == 401
    assert client.get("/v1/admin/review/queue").status_code == 401
    assert client.get("/v1/admin/review/queue", headers=AUTH).status_code == 401

    decision_path = f"/v1/admin/review/{uuid4()}/decision"
    assert client.post(decision_path, json=_decision_body()).status_code == 401
    assert (
        client.post(decision_path, json=_decision_body(), headers=AUTH).status_code == 401
    )

    assert review.decide_calls == []
    assert score.calls == []


def test_next_returns_200_with_the_task_json() -> None:
    task = _task()
    client, _review, _score = make_client(task=task)

    response = client.get("/v1/admin/review/next", headers=ADMIN)

    assert response.status_code == 200
    body = response.json()
    assert body["task_id"] == str(task["task_id"])
    assert body["infringement_id"] == str(task["infringement_id"])
    assert body["severity"] == "ncii_suspected"
    assert body["triage"] == {"best_face_bbox": {"left": 0.1}}
    assert body["source_domain"] == "example.test"


def test_next_returns_204_when_the_queue_is_empty() -> None:
    client, _review, _score = make_client(task=None)

    response = client.get("/v1/admin/review/next", headers=ADMIN)

    assert response.status_code == 204
    assert response.content == b""


def test_queue_returns_the_depths_dict() -> None:
    depths = {"ncii_suspected": 3, "benign_copy": 1}
    client, _review, _score = make_client(depths=depths)

    response = client.get("/v1/admin/review/queue", headers=ADMIN)

    assert response.status_code == 200
    assert response.json() == depths


def test_decide_confirmed_triggers_recompute_with_review_decision_cause() -> None:
    infringement_id = uuid4()
    user_ref = UserRef(uuid4())
    outcome = DecisionOutcome(
        infringement_id=infringement_id,
        user_ref=user_ref,
        decision="confirmed",
        severity="ncii_suspected",
    )
    client, review, score = make_client(outcome=outcome)

    response = client.post(
        f"/v1/admin/review/{uuid4()}/decision",
        json=_decision_body(decision="confirmed", severity="ncii_suspected"),
        headers=ADMIN,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["infringement_id"] == str(infringement_id)
    assert body["decision"] == "confirmed"
    assert body["severity"] == "ncii_suspected"
    assert score.calls == [(user_ref, "review_decision", str(infringement_id))]
    assert review.decide_calls[0]["operator"] == "alice"
    assert review.decide_calls[0]["severity"] == "ncii_suspected"


def test_decide_rejected_also_triggers_recompute() -> None:
    outcome = DecisionOutcome(
        infringement_id=uuid4(), user_ref=UserRef(uuid4()), decision="rejected", severity=None
    )
    client, _review, score = make_client(outcome=outcome)

    response = client.post(
        f"/v1/admin/review/{uuid4()}/decision",
        json=_decision_body(decision="rejected"),
        headers=ADMIN,
    )

    assert response.status_code == 200
    assert len(score.calls) == 1
    assert score.calls[0][1] == "review_decision"


def test_decide_uncertain_triggers_no_recompute() -> None:
    outcome = DecisionOutcome(
        infringement_id=uuid4(), user_ref=UserRef(uuid4()), decision="uncertain", severity=None
    )
    client, _review, score = make_client(outcome=outcome)

    response = client.post(
        f"/v1/admin/review/{uuid4()}/decision",
        json=_decision_body(decision="uncertain"),
        headers=ADMIN,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "uncertain"
    assert body["severity"] is None
    assert score.calls == []


def test_decide_404s_on_an_unknown_task() -> None:
    client, review, score = make_client(outcome=None)

    response = client.post(
        f"/v1/admin/review/{uuid4()}/decision", json=_decision_body(), headers=ADMIN
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "review_task_not_found"
    assert score.calls == []
    assert len(review.decide_calls) == 1


def test_decide_422s_on_a_bogus_severity() -> None:
    client, review, _score = make_client()

    response = client.post(
        f"/v1/admin/review/{uuid4()}/decision",
        json=_decision_body(severity="not_a_real_severity"),
        headers=ADMIN,
    )

    assert response.status_code == 422
    assert review.decide_calls == []


def test_decide_422s_on_a_bogus_decision() -> None:
    client, review, _score = make_client()

    response = client.post(
        f"/v1/admin/review/{uuid4()}/decision",
        json=_decision_body(decision="maybe"),
        headers=ADMIN,
    )

    assert response.status_code == 422
    assert review.decide_calls == []


def test_decide_422s_on_a_blank_operator() -> None:
    client, review, _score = make_client()

    response = client.post(
        f"/v1/admin/review/{uuid4()}/decision",
        json=_decision_body(operator=""),
        headers=ADMIN,
    )

    assert response.status_code == 422
    assert review.decide_calls == []


def test_a_raising_score_store_does_not_change_the_decide_response() -> None:
    outcome = DecisionOutcome(
        infringement_id=uuid4(), user_ref=UserRef(uuid4()), decision="confirmed", severity=None
    )
    client, _review, _score = make_client(outcome=outcome, raising_score_store=True)

    response = client.post(
        f"/v1/admin/review/{uuid4()}/decision",
        json=_decision_body(decision="confirmed"),
        headers=ADMIN,
    )

    assert response.status_code == 200
    assert response.json()["decision"] == "confirmed"
