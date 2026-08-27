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
        providers: list[dict[str, Any]] | None = None,
    ) -> None:
        self._task = task
        self._open_hits = open_hits if open_hits is not None else []
        self._decisions = decisions if decisions is not None else []
        self._providers = providers if providers is not None else []
        self.decide_calls: list[dict[str, Any]] = []
        self.create_event_calls: list[dict[str, Any]] = []
        self.retract_calls: list[dict[str, Any]] = []
        self.provider_writes: list[dict[str, Any]] = []
        self.articles: dict[UUID, dict[str, Any]] = {}
        self.article_calls: list[dict[str, Any]] = []

    async def provider_health(self) -> dict[str, Any]:
        return {
            "as_of": "2026-08-27T10:00:00+00:00",
            "window_hours": 24,
            "providers": self._providers,
        }

    async def disable_provider(self, provider_id: str, *, reason: str, operator: str) -> None:
        self.provider_writes.append(
            {
                "action": "disable",
                "provider_id": provider_id,
                "reason": reason,
                "operator": operator,
            }
        )

    async def enable_provider(self, provider_id: str, *, reason: str, operator: str) -> None:
        self.provider_writes.append(
            {
                "action": "enable",
                "provider_id": provider_id,
                "reason": reason,
                "operator": operator,
            }
        )

    async def reset_breaker(self, provider_id: str, *, reason: str, operator: str) -> None:
        self.provider_writes.append(
            {
                "action": "breaker/reset",
                "provider_id": provider_id,
                "reason": reason,
                "operator": operator,
            }
        )

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

    async def list_articles(self) -> list[dict[str, Any]]:
        return list(self.articles.values())

    async def get_article(self, article_id: UUID) -> dict[str, Any] | None:
        return self.articles.get(article_id)

    async def create_article(self, payload: dict[str, Any]) -> dict[str, Any]:
        article_id = uuid4()
        self.articles[article_id] = {
            "article_id": str(article_id),
            **payload,
            "status": "draft",
            "published_at": None,
            "created_by": payload["operator"],
            "updated_by": payload["operator"],
            "created_at": "2026-08-27T10:00:00+00:00",
            "updated_at": "2026-08-27T10:00:00+00:00",
        }
        self.article_calls.append({"action": "create", **payload})
        return {"article_id": str(article_id), "status": "draft"}

    async def update_article(self, article_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        self.article_calls.append({"action": "update", "article_id": article_id, **payload})
        return self.articles.get(article_id, {})

    async def publish_article(self, article_id: UUID, *, operator: str) -> None:
        self.article_calls.append(
            {"action": "publish", "article_id": article_id, "operator": operator}
        )

    async def archive_article(self, article_id: UUID, *, operator: str, reason: str) -> None:
        self.article_calls.append(
            {"action": "archive", "article_id": article_id, "operator": operator, "reason": reason}
        )


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


# ── provider ops on the dashboard ─────────────────────────────────────────

_QUIET_PROVIDER: dict[str, Any] = {
    "provider_id": "hive",
    "enabled": True,
    "breaker_state": "closed",
    "breaker_reason": None,
    "call_count": 0,
    "cost_usd": "0.00",
    "daily_budget_usd": "10.00",
    "monthly_budget_usd": None,
    "month_to_date_cost_usd": "14.00",
    "budget_headroom_usd": "10.00",
    # None, not 0.0: no calls in the window is a different fact from a 0% rate.
    "success_rate": None,
    "window_call_count": 0,
    "successful_calls_24h": 0,
    "latency_p50_ms": None,
    "latency_p99_ms": None,
    "alarms": [{"kind": "no_successful_calls_24h", "detail": "0 successful calls in 24h"}],
}


def test_dashboard_leads_with_the_alarms_and_renders_a_null_rate_as_a_dash() -> None:
    response = _client(services=FakeServicesClient(providers=[_QUIET_PROVIDER])).get(
        "/", auth=ALICE
    )

    assert response.status_code == 200
    body = response.content.decode()
    assert "no_successful_calls_24h" in body
    assert "—" in body
    # Not a bare "0%": base.html's CSS carries `width: 100%`.
    assert "0%</td>" not in body
    assert "24h window" in body


def test_dashboard_offers_reset_only_while_the_breaker_is_not_closed() -> None:
    closed = _client(services=FakeServicesClient(providers=[_QUIET_PROVIDER])).get(
        "/", auth=ALICE
    )
    assert b"/providers/hive/breaker-reset" not in closed.content
    assert b"/providers/hive/disable" in closed.content

    opened = dict(_QUIET_PROVIDER, breaker_state="open", breaker_reason="timeout")
    response = _client(services=FakeServicesClient(providers=[opened])).get("/", auth=ALICE)
    assert b"/providers/hive/breaker-reset" in response.content


def test_provider_disable_posts_through_with_the_operator_and_redirects_home() -> None:
    fake = FakeServicesClient()
    response = _client(services=fake).post(
        "/providers/hive/disable",
        data={"reason": "returning garbage", "csrf_token": _csrf("alice")},
        auth=ALICE,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert fake.provider_writes == [
        {"action": "disable", "provider_id": "hive", "reason": "returning garbage",
         "operator": "alice"}
    ]


def test_provider_enable_and_breaker_reset_post_through() -> None:
    fake = FakeServicesClient()
    client = _client(services=fake)
    client.post(
        "/providers/hive/enable",
        data={"reason": "vendor confirmed fixed", "csrf_token": _csrf("bob")},
        auth=BOB,
        follow_redirects=False,
    )
    client.post(
        "/providers/hive/breaker-reset",
        data={"reason": "verified by hand", "csrf_token": _csrf("bob")},
        auth=BOB,
        follow_redirects=False,
    )

    assert [w["action"] for w in fake.provider_writes] == ["enable", "breaker/reset"]
    assert {w["operator"] for w in fake.provider_writes} == {"bob"}


def test_provider_disable_without_csrf_is_403_and_writes_nothing() -> None:
    fake = FakeServicesClient()
    response = _client(services=fake).post(
        "/providers/hive/disable",
        data={"reason": "returning garbage"},
        auth=ALICE,
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert fake.provider_writes == []


# ── articles ──────────────────────────────────────────────────────────────


def test_articles_list_and_new_form_render() -> None:
    client = _client(services=FakeServicesClient())
    assert client.get("/articles", auth=ALICE).status_code == 200
    new = client.get("/articles/new", auth=ALICE)
    assert new.status_code == 200
    assert b'name="images"' in new.content


def test_articles_create_parses_the_line_encoded_pictures_and_sources() -> None:
    fake = FakeServicesClient()
    response = _client(services=fake).post(
        "/articles",
        data={
            "title": "Older photos of you circulate too",
            "summary": "blurb",
            "body": "text",
            "images": "https://cdn.example/a.jpg | album\n\nhttps://cdn.example/b.jpg",
            "sources": "Example News | https://news.example/story",
            "csrf_token": _csrf("alice"),
        },
        auth=ALICE,
        follow_redirects=False,
    )

    assert response.status_code == 303
    created = fake.article_calls[0]
    assert response.headers["location"] == f"/articles/{next(iter(fake.articles))}"
    assert created["operator"] == "alice"
    assert created["images"] == [
        {"url": "https://cdn.example/a.jpg", "alt": "album"},
        {"url": "https://cdn.example/b.jpg", "alt": ""},
    ]
    assert created["sources"] == [{"name": "Example News", "url": "https://news.example/story"}]


def test_article_edit_page_links_pictures_and_never_renders_an_img() -> None:
    fake = FakeServicesClient()
    client = _client(services=fake)
    client.post(
        "/articles",
        data={
            "title": "T",
            "images": "https://cdn.example/a.jpg | album",
            "sources": "",
            "csrf_token": _csrf("alice"),
        },
        auth=ALICE,
        follow_redirects=False,
    )
    article_id = next(iter(fake.articles))

    response = client.get(f"/articles/{article_id}", auth=ALICE)

    assert response.status_code == 200
    assert b'href="https://cdn.example/a.jpg"' in response.content
    assert b"<img" not in response.content
    # Prefilled textarea round-trips the line encoding.
    assert b"https://cdn.example/a.jpg | album" in response.content


def test_article_publish_and_archive_post_through_with_the_operator() -> None:
    fake = FakeServicesClient()
    client = _client(services=fake)
    client.post(
        "/articles", data={"title": "T", "csrf_token": _csrf("alice")}, auth=ALICE,
        follow_redirects=False,
    )
    article_id = next(iter(fake.articles))

    published = client.post(
        f"/articles/{article_id}/publish", data={"csrf_token": _csrf("bob")}, auth=BOB,
        follow_redirects=False,
    )
    archived = client.post(
        f"/articles/{article_id}/archive",
        data={"reason": "superseded", "csrf_token": _csrf("bob")},
        auth=BOB,
        follow_redirects=False,
    )

    assert published.status_code == 303 and archived.status_code == 303
    assert published.headers["location"] == f"/articles/{article_id}"
    assert [c["action"] for c in fake.article_calls] == ["create", "publish", "archive"]
    assert fake.article_calls[1] == {
        "action": "publish",
        "article_id": article_id,
        "operator": "bob",
    }
    assert fake.article_calls[2]["reason"] == "superseded"


def test_article_writes_without_csrf_are_403_and_reach_nothing() -> None:
    fake = FakeServicesClient()
    client = _client(services=fake)
    assert client.post("/articles", data={"title": "T"}, auth=ALICE).status_code == 403
    missing = uuid4()
    assert client.post(f"/articles/{missing}/publish", data={}, auth=ALICE).status_code == 403
    assert fake.article_calls == []


def test_unknown_article_is_a_404_envelope() -> None:
    response = _client(services=FakeServicesClient()).get(f"/articles/{uuid4()}", auth=ALICE)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "article_not_found"


def test_articles_pages_require_credentials() -> None:
    client = _client()
    assert client.get("/articles").status_code == 401
    assert client.get("/articles/new").status_code == 401
