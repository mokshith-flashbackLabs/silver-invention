"""Admin review-queue routes — behaviour over in-memory fakes (Task 15).

Same convention as ``tests/test_admin_threat_routes.py``: ``TestClient``
never runs the lifespan, ``ReviewStore`` is a pre-wired fake on
``app.state``, and both tokens are required at router level.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
        decisions: tuple[dict[str, Any], ...] = (),
        hits: tuple[dict[str, Any], ...] = (),
    ) -> None:
        self._task = task
        self._depths = depths if depths is not None else {}
        self._outcome = outcome
        self._decisions = decisions
        self._hits = hits
        self.decide_calls: list[dict[str, Any]] = []
        self.limits: list[tuple[str, int]] = []
        self.stats_calls: list[datetime] = []

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

    async def subject_decisions(self, *, limit: int) -> tuple[dict[str, Any], ...]:
        self.limits.append(("decisions", limit))
        return self._decisions

    async def open_hits(self, *, limit: int) -> tuple[dict[str, Any], ...]:
        self.limits.append(("hits", limit))
        return self._hits

    async def verdict_stats(self, *, since: datetime) -> dict[str, Any]:
        self.stats_calls.append(since)
        return {
            "since": since,
            "by_severity": [
                {
                    "machine_severity": "explicit_unmatched",
                    "total": 3,
                    "true_positive": 1,
                    "false_positive": 1,
                    "unsure": 1,
                    "false_positive_rate": 0.5,
                },
                {
                    "machine_severity": "benign_copy",
                    "total": 1,
                    "true_positive": 0,
                    "false_positive": 0,
                    "unsure": 1,
                    "false_positive_rate": None,
                },
            ],
            "subject_agreement": {
                "compared": 0,
                "agreed": 0,
                "disagreed": 0,
                "agreement_rate": None,
            },
            "by_operator": [
                {
                    "operator": "alice",
                    "total": 4,
                    "true_positive": 1,
                    "false_positive": 1,
                    "unsure": 2,
                },
            ],
        }


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
    decisions: tuple[dict[str, Any], ...] = (),
    hits: tuple[dict[str, Any], ...] = (),
) -> tuple[TestClient, FakeReviewStore]:
    app = create_app(config=make_config())
    review = FakeReviewStore(
        task=task, depths=depths, outcome=outcome, decisions=decisions, hits=hits
    )
    app.state.review_store = review
    return TestClient(app), review


def _decision_body(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"decision": "confirmed", "operator": "alice"}
    base.update(overrides)
    return base


def test_every_route_needs_both_tokens() -> None:
    client, review = make_client(task=_task())

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


def test_next_returns_200_with_the_task_json() -> None:
    task = _task()
    client, _review = make_client(task=task)

    response = client.get("/v1/admin/review/next", headers=ADMIN)

    assert response.status_code == 200
    body = response.json()
    assert body["task_id"] == str(task["task_id"])
    assert body["infringement_id"] == str(task["infringement_id"])
    assert body["severity"] == "ncii_suspected"
    assert body["triage"] == {"best_face_bbox": {"left": 0.1}}
    assert body["source_domain"] == "example.test"


def test_next_returns_204_when_the_queue_is_empty() -> None:
    client, _review = make_client(task=None)

    response = client.get("/v1/admin/review/next", headers=ADMIN)

    assert response.status_code == 204
    assert response.content == b""


def test_queue_returns_the_depths_dict() -> None:
    depths = {"ncii_suspected": 3, "benign_copy": 1}
    client, _review = make_client(depths=depths)

    response = client.get("/v1/admin/review/queue", headers=ADMIN)

    assert response.status_code == 200
    assert response.json() == depths


def test_decide_confirmed_returns_the_outcome() -> None:
    infringement_id = uuid4()
    user_ref = UserRef(uuid4())
    outcome = DecisionOutcome(
        infringement_id=infringement_id,
        user_ref=user_ref,
        decision="confirmed",
        severity="ncii_suspected",
    )
    client, review = make_client(outcome=outcome)

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
    assert review.decide_calls[0]["operator"] == "alice"
    assert review.decide_calls[0]["severity"] == "ncii_suspected"


def test_decide_rejected_returns_the_outcome() -> None:
    outcome = DecisionOutcome(
        infringement_id=uuid4(), user_ref=UserRef(uuid4()), decision="rejected", severity=None
    )
    client, _review = make_client(outcome=outcome)

    response = client.post(
        f"/v1/admin/review/{uuid4()}/decision",
        json=_decision_body(decision="rejected"),
        headers=ADMIN,
    )

    assert response.status_code == 200
    assert response.json()["decision"] == "rejected"


def test_decide_uncertain_returns_the_outcome() -> None:
    outcome = DecisionOutcome(
        infringement_id=uuid4(), user_ref=UserRef(uuid4()), decision="uncertain", severity=None
    )
    client, _review = make_client(outcome=outcome)

    response = client.post(
        f"/v1/admin/review/{uuid4()}/decision",
        json=_decision_body(decision="uncertain"),
        headers=ADMIN,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "uncertain"
    assert body["severity"] is None


def test_decide_404s_on_an_unknown_task() -> None:
    client, review = make_client(outcome=None)

    response = client.post(
        f"/v1/admin/review/{uuid4()}/decision", json=_decision_body(), headers=ADMIN
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "review_task_not_found"
    assert len(review.decide_calls) == 1


def test_decide_422s_on_a_bogus_severity() -> None:
    client, review = make_client()

    response = client.post(
        f"/v1/admin/review/{uuid4()}/decision",
        json=_decision_body(severity="not_a_real_severity"),
        headers=ADMIN,
    )

    assert response.status_code == 422
    assert review.decide_calls == []


def test_decide_422s_on_a_bogus_decision() -> None:
    client, review = make_client()

    response = client.post(
        f"/v1/admin/review/{uuid4()}/decision",
        json=_decision_body(decision="maybe"),
        headers=ADMIN,
    )

    assert response.status_code == 422
    assert review.decide_calls == []


def test_decide_422s_on_a_blank_operator() -> None:
    client, review = make_client()

    response = client.post(
        f"/v1/admin/review/{uuid4()}/decision",
        json=_decision_body(operator=""),
        headers=ADMIN,
    )

    assert response.status_code == 422
    assert review.decide_calls == []


# -- the observer feed (spec 2026-08-21 s6) ----------------------------------


def test_subject_decisions_feed_returns_the_store_rows() -> None:
    row = {
        "occurred_at": "2026-08-21T10:00:00+00:00",
        "user_ref": str(uuid4()),
        "infringement_id": str(uuid4()),
        "decision": "confirmed",
        "severity": "ncii_suspected",
        "source_domain": "salon.example",
    }
    client, review = make_client(decisions=(row,))

    response = client.get("/v1/admin/review/subject-decisions", headers=ADMIN)

    assert response.status_code == 200
    assert response.json() == {"decisions": [row]}
    assert review.limits == [("decisions", 50)]


def test_subject_decisions_limit_is_clamped() -> None:
    client, _review = make_client()

    assert (
        client.get(
            "/v1/admin/review/subject-decisions", params={"limit": 501}, headers=ADMIN
        ).status_code
        == 422
    )


def test_open_hits_shows_that_a_person_has_a_hit() -> None:
    """Owner requirement 2026-08-21: the control room always sees THAT a
    person has a hit -- metadata only, never pixels."""
    row = {
        "user_ref": str(uuid4()),
        "infringement_id": str(uuid4()),
        "confirm_state": "machine_triaged",
        "severity": "benign_copy",
        "source_domain": "salon.example",
        "first_seen_at": "2026-08-20T12:48:21+00:00",
    }
    client, review = make_client(hits=(row,))

    response = client.get(
        "/v1/admin/review/open-hits", params={"limit": 10}, headers=ADMIN
    )

    assert response.status_code == 200
    assert response.json() == {"hits": [row]}
    assert review.limits == [("hits", 10)]


def test_the_new_feeds_need_both_tokens() -> None:
    client, _review = make_client()

    assert client.get("/v1/admin/review/subject-decisions").status_code == 401
    assert client.get("/v1/admin/review/open-hits", headers=AUTH).status_code == 401


# ── GET /v1/admin/review/stats (2026-09-14) ──────────────────────────────


def test_stats_defaults_to_a_thirty_day_window() -> None:
    """The default is computed per request, not at import: a module constant
    would freeze the window on a long-lived process and the numbers would
    quietly stop moving."""
    client, review = make_client()

    before = datetime.now(UTC)
    response = client.get("/v1/admin/review/stats", headers=ADMIN)
    after = datetime.now(UTC)

    assert response.status_code == 200
    (since,) = review.stats_calls
    assert before - timedelta(days=30) <= since <= after - timedelta(days=30)


def test_stats_serialises_a_null_rate_as_null() -> None:
    """The one field this endpoint must never fabricate. 0.0 would read as
    "no false positives" out of a window where nobody decided anything."""
    client, review = make_client()

    body = client.get(
        "/v1/admin/review/stats",
        params={"since": "2026-09-01T00:00:00Z"},
        headers=ADMIN,
    ).json()

    assert review.stats_calls == [datetime(2026, 9, 1, tzinfo=UTC)]
    measured, unmeasured = body["by_severity"]
    assert measured["false_positive_rate"] == 0.5
    assert unmeasured["false_positive_rate"] is None
    assert body["subject_agreement"]["agreement_rate"] is None
    assert body["by_operator"][0]["operator"] == "alice"


def test_stats_needs_both_tokens() -> None:
    client, review = make_client()

    assert client.get("/v1/admin/review/stats").status_code == 401
    assert client.get("/v1/admin/review/stats", headers=AUTH).status_code == 401
    assert review.stats_calls == []
