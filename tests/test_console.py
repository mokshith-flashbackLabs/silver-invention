"""Tests for the control-room console (Task 17; spec 2026-08-21 §6).

The console is a standalone FastAPI app with no database access of its own
-- every read and write flows through one injected ``ServicesClient``. Since
spec 2026-08-21 §0.2 there is NO fetcher client and NO pixels path: staff
never see hit imagery. Tests pre-wire fakes on ``app.state`` before
constructing ``TestClient``, the same convention
``imageshield.console.app._lifespan``'s getattr guards exist for.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from imageshield.console.app import create_app
from imageshield.console.auth import _csrf_token_for_date, make_csrf_token, parse_operators
from imageshield.console.config import ConsoleConfig

ALICE = ("alice", "token-a")
BOB = ("bob", "token-b")

_TASK_ID = uuid4()

_TASK: dict[str, Any] = {
    "task_id": str(_TASK_ID),
    "infringement_id": str(uuid4()),
    "user_ref": str(uuid4()),
    "severity": "ncii_suspected",
    "triage": {
        "best_face_bbox": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        "face_match_score": 0.91,
    },
    "image_url": "https://img.example/a.jpg",
    "page_url": "https://porn-site.example/p/1",
    "face_match_score": 0.91,
    "source_domain": "porn-site.example",
}


class FakeServicesClient:
    def __init__(
        self,
        task: dict[str, Any] | None = _TASK,
        *,
        open_hits: list[dict[str, Any]] | None = None,
        decisions: list[dict[str, Any]] | None = None,
    ) -> None:
        self._task = task
        self._open_hits = open_hits if open_hits is not None else []
        self._decisions = decisions if decisions is not None else []
        self.decide_calls: list[dict[str, Any]] = []
        self.create_event_calls: list[dict[str, Any]] = []
        self.retract_calls: list[dict[str, Any]] = []

    async def provider_health(self) -> dict[str, Any]:
        return {"providers": []}

    async def review_next(self) -> dict[str, Any] | None:
        return self._task

    async def review_queue(self) -> dict[str, Any]:
        return {"ncii_suspected": 1}

    async def decide(
        self, task_id: UUID, *, decision: str, operator: str, severity: str | None
    ) -> None:
        self.decide_calls.append(
            {"task_id": task_id, "decision": decision, "operator": operator, "severity": severity}
        )

    async def list_events(self) -> list[dict[str, Any]]:
        return []

    async def create_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.create_event_calls.append(payload)
        return {"event_id": str(uuid4()), "matched_count": 0}

    async def retract_event(self, event_id: UUID, *, operator: str, reason: str) -> None:
        self.retract_calls.append({"event_id": event_id, "operator": operator, "reason": reason})

    async def score(self, user_ref: str) -> dict[str, Any] | None:
        return None

    async def subject_decisions(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return self._decisions

    async def open_hits(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return self._open_hits


def _config() -> ConsoleConfig:
    return ConsoleConfig(
        console_operators="alice:token-a,bob:token-b",
        services_base_url="http://localhost:8081",
        service_token="service-token-for-tests-0001",
        admin_service_token="admin-token-for-tests-0002",
    )


def _csrf(operator: str) -> str:
    return make_csrf_token(_config(), operator)


def _client(services: FakeServicesClient | None = None) -> TestClient:
    app = create_app(config=_config())
    app.state.services_client = services if services is not None else FakeServicesClient()
    return TestClient(app)


# ── auth ─────────────────────────────────────────────────────────────────


def test_health_needs_no_credentials() -> None:
    assert _client().get("/health").status_code == 200


def test_no_credentials_is_401_with_www_authenticate() -> None:
    response = _client().get("/review")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Basic realm="imageshield-console"'


def test_wrong_password_is_401() -> None:
    response = _client().get("/review", auth=("alice", "wrong-password"))
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Basic realm="imageshield-console"'


def test_unknown_operator_name_is_401() -> None:
    response = _client().get("/review", auth=("mallory", "token-a"))
    assert response.status_code == 401


def test_correct_credentials_reach_the_route() -> None:
    response = _client().get("/review", auth=ALICE)
    assert response.status_code == 200
    response = _client().get("/review", auth=BOB)
    assert response.status_code == 200


def test_parse_operators_rejects_a_duplicate_name() -> None:
    with pytest.raises(ValueError):
        parse_operators("alice:token-a,alice:token-b")


def test_parse_operators_rejects_an_empty_token() -> None:
    with pytest.raises(ValueError):
        parse_operators("alice:")


def test_parse_operators_rejects_an_empty_roster() -> None:
    with pytest.raises(ValueError):
        parse_operators("")


def test_parse_operators_happy_path() -> None:
    assert parse_operators("alice:token-a,bob:token-b") == {
        "alice": "token-a",
        "bob": "token-b",
    }


# ── review ───────────────────────────────────────────────────────────────


def test_review_renders_the_fake_tasks_domain_and_severity() -> None:
    response = _client(services=FakeServicesClient()).get("/review", auth=ALICE)
    assert response.status_code == 200
    assert "porn-site.example" in response.text
    assert "ncii_suspected" in response.text


def test_review_renders_an_empty_queue() -> None:
    response = _client(services=FakeServicesClient(task=None)).get("/review", auth=ALICE)
    assert response.status_code == 200
    assert "Queue is empty" in response.text


def test_post_decision_calls_decide_with_the_logged_in_operator_and_redirects() -> None:
    fake = FakeServicesClient()
    client = _client(services=fake)

    response = client.post(
        f"/review/{_TASK_ID}",
        data={"decision": "confirmed", "severity": "", "csrf_token": _csrf("alice")},
        auth=ALICE,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/review"
    assert len(fake.decide_calls) == 1
    call = fake.decide_calls[0]
    assert call["task_id"] == _TASK_ID
    assert call["decision"] == "confirmed"
    assert call["operator"] == "alice"
    assert call["severity"] is None


def test_post_uncertain_decision_carries_no_severity() -> None:
    fake = FakeServicesClient()
    client = _client(services=fake)

    response = client.post(
        f"/review/{_TASK_ID}",
        data={"decision": "uncertain", "csrf_token": _csrf("bob")},
        auth=BOB,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert fake.decide_calls[0]["decision"] == "uncertain"
    assert fake.decide_calls[0]["operator"] == "bob"
    assert fake.decide_calls[0]["severity"] is None


# ── no pixels, anywhere (spec 2026-08-21 §0.2) ───────────────────────────


def test_the_crop_route_is_gone() -> None:
    """Staff never see hit imagery: the console's only pixels path was
    removed with the 2026-08-21 decision."""
    response = _client().get(
        "/crop",
        params={"url": "https://img.example/a.jpg", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        auth=ALICE,
    )
    assert response.status_code == 404


def test_review_page_renders_no_images_at_all() -> None:
    response = _client().get("/review", auth=ALICE)

    assert response.status_code == 200
    assert b"<img" not in response.content


# ── the observer page ─────────────────────────────────────────────────────


def test_decisions_page_shows_open_hits_and_decided_answers() -> None:
    """Owner requirement 2026-08-21: the control room always sees THAT a
    person has a hit — and what its subject decided — metadata only."""
    open_hit = {
        "user_ref": str(uuid4()),
        "infringement_id": str(uuid4()),
        "confirm_state": "machine_triaged",
        "severity": "benign_copy",
        "source_domain": "open-hit.example",
        "first_seen_at": "2026-08-20T12:48:21+00:00",
    }
    decided = {
        "occurred_at": "2026-08-21T09:00:00+00:00",
        "user_ref": str(uuid4()),
        "infringement_id": str(uuid4()),
        "decision": "confirmed",
        "severity": "ncii_suspected",
        "source_domain": "decided-hit.example",
    }
    client = _client(FakeServicesClient(open_hits=[open_hit], decisions=[decided]))

    response = client.get("/decisions", auth=ALICE)

    assert response.status_code == 200
    assert "open-hit.example" in response.text
    assert "decided-hit.example" in response.text
    # Explicit-severity confirmations are flagged as campaign candidates.
    assert 'class="flag"' in response.text
    assert b"<img" not in response.content


def test_decisions_page_requires_credentials() -> None:
    assert _client().get("/decisions").status_code == 401


# ── csrf ─────────────────────────────────────────────────────────────────


def test_post_without_csrf_token_is_403() -> None:
    fake = FakeServicesClient()
    client = _client(services=fake)

    response = client.post(
        f"/review/{_TASK_ID}",
        data={"decision": "confirmed", "severity": ""},
        auth=ALICE,
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert response.json() == {
        "error": {"code": "csrf_rejected", "message": "invalid or missing csrf token"}
    }
    assert fake.decide_calls == []


def test_post_with_wrong_csrf_token_is_403() -> None:
    fake = FakeServicesClient()
    client = _client(services=fake)

    response = client.post(
        f"/review/{_TASK_ID}",
        data={"decision": "confirmed", "severity": "", "csrf_token": "not-the-right-token"},
        auth=ALICE,
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert fake.decide_calls == []


def test_post_with_another_operators_csrf_token_is_403() -> None:
    """Bound to the operator name: alice cannot replay bob's token."""
    fake = FakeServicesClient()
    client = _client(services=fake)

    response = client.post(
        f"/review/{_TASK_ID}",
        data={"decision": "confirmed", "severity": "", "csrf_token": _csrf("bob")},
        auth=ALICE,
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert fake.decide_calls == []


def test_post_with_yesterdays_csrf_token_is_accepted() -> None:
    """The daily-rotation grace window: a form open across UTC midnight
    still submits successfully."""
    fake = FakeServicesClient()
    client = _client(services=fake)
    yesterday = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)
    token = _csrf_token_for_date(_config(), "alice", yesterday)

    response = client.post(
        f"/review/{_TASK_ID}",
        data={"decision": "confirmed", "severity": "", "csrf_token": token},
        auth=ALICE,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert len(fake.decide_calls) == 1


def test_post_with_a_two_day_old_csrf_token_is_403() -> None:
    """Outside the one-day grace window: rejected, unlike yesterday's."""
    fake = FakeServicesClient()
    client = _client(services=fake)
    two_days_ago = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=2)
    token = _csrf_token_for_date(_config(), "alice", two_days_ago)

    response = client.post(
        f"/review/{_TASK_ID}",
        data={"decision": "confirmed", "severity": "", "csrf_token": token},
        auth=ALICE,
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert fake.decide_calls == []


# ── events ───────────────────────────────────────────────────────────────


def test_events_create_posts_through_and_redirects() -> None:
    fake = FakeServicesClient()
    client = _client(services=fake)

    response = client.post(
        "/events",
        data={
            "kind": "leak",
            "title": "A site went dark",
            "body": "",
            "severity": "3",
            "domains": "a.example, b.example",
            "penalty": "5.00",
            "expires_at": "2026-09-01T00:00",
            "decay_days": "30",
            "csrf_token": _csrf("alice"),
        },
        auth=ALICE,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/events"
    assert len(fake.create_event_calls) == 1
    payload = fake.create_event_calls[0]
    assert payload["operator"] == "alice"
    assert payload["domains"] == ["a.example", "b.example"]
    assert payload["kind"] == "leak"
    assert payload["is_global"] is False


def test_events_list_renders() -> None:
    response = _client(services=FakeServicesClient()).get("/events", auth=ALICE)
    assert response.status_code == 200


def test_events_retract_posts_through_and_redirects() -> None:
    fake = FakeServicesClient()
    client = _client(services=fake)
    event_id = uuid4()

    response = client.post(
        f"/events/{event_id}/retract",
        data={"reason": "false alarm", "csrf_token": _csrf("alice")},
        auth=ALICE,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/events"
    assert fake.retract_calls[0]["event_id"] == event_id
    assert fake.retract_calls[0]["operator"] == "alice"
    assert fake.retract_calls[0]["reason"] == "false alarm"


# ── scores ───────────────────────────────────────────────────────────────


def test_scores_lookup_with_no_query_renders_the_form() -> None:
    response = _client().get("/scores", auth=ALICE)
    assert response.status_code == 200


def test_scores_lookup_not_found() -> None:
    response = _client().get("/scores", params={"user_ref": str(uuid4())}, auth=ALICE)
    assert response.status_code == 200
    assert "No protection score" in response.text


# ── operator name never leaks a token ───────────────────────────────────


def test_operator_name_but_not_token_appears_in_the_page() -> None:
    response = _client().get("/", auth=ALICE)
    assert response.status_code == 200
    assert "alice" in response.text
    assert "token-a" not in response.text
