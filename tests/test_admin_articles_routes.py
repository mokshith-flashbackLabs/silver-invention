"""Admin article routes — behaviour over an in-memory fake store.

Repo convention: TestClient never runs the lifespan; the store is pre-wired on
``app.state``. The auth assertion is load-bearing — an article reaches every
user's feed, so both tokens are required at router level.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from imageshield.http.app import create_app
from tests.conftest import ADMIN_SERVICE_TOKEN, SERVICE_TOKEN, make_config

AUTH = {"X-Service-Token": SERVICE_TOKEN}
ADMIN = {**AUTH, "X-Admin-Service-Token": ADMIN_SERVICE_TOKEN}

_NOW = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)

_BODY: dict[str, Any] = {
    "title": "Older photos of you circulate too",
    "summary": "Why a photo from five years ago still matters.",
    "body": "Image search can only find copies of what we hold.",
    "images": [{"url": "https://cdn.example/hero.jpg", "alt": "A photo album"}],
    "sources": [{"name": "Example News", "url": "https://news.example/story"}],
    "operator": "alice",
}


class FakeArticleStore:
    def __init__(self) -> None:
        self.rows: dict[UUID, dict[str, Any]] = {}
        self.audit: list[tuple[str, str]] = []  # (action, operator)

    async def create_article(self, **kwargs: Any) -> UUID:
        article_id = uuid4()
        operator = kwargs.pop("operator")
        self.rows[article_id] = {
            "article_id": article_id,
            **kwargs,
            "status": "draft",
            "published_at": None,
            "created_by": operator,
            "updated_by": operator,
            "created_at": _NOW,
            "updated_at": _NOW,
        }
        self.audit.append(("article.created", operator))
        return article_id

    async def update_article(self, article_id: UUID, **kwargs: Any) -> bool:
        row = self.rows.get(article_id)
        if row is None:
            return False
        operator = kwargs.pop("operator")
        row.update(kwargs, updated_by=operator)
        self.audit.append(("article.updated", operator))
        return True

    async def publish_article(self, article_id: UUID, *, operator: str) -> str | None:
        row = self.rows.get(article_id)
        if row is None:
            return None
        if row["status"] != "published":
            row["status"] = "published"
            row["published_at"] = row["published_at"] or _NOW
            self.audit.append(("article.published", operator))
        return str(row["status"])

    async def archive_article(self, article_id: UUID, *, operator: str, reason: str) -> str | None:
        row = self.rows.get(article_id)
        if row is None:
            return None
        if row["status"] != "archived":
            row["status"] = "archived"
            self.audit.append(("article.archived", operator))
        return str(row["status"])

    async def get_article(self, article_id: UUID) -> dict[str, Any] | None:
        return self.rows.get(article_id)

    async def list_articles(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return list(self.rows.values())[:limit]


def make_client() -> tuple[TestClient, FakeArticleStore]:
    app = create_app(config=make_config())
    store = FakeArticleStore()
    app.state.article_store = store
    return TestClient(app), store


def test_every_article_route_needs_both_tokens() -> None:
    client, store = make_client()
    some_id = uuid4()
    calls = [
        ("GET", "/v1/admin/articles", None),
        ("POST", "/v1/admin/articles", _BODY),
        ("GET", f"/v1/admin/articles/{some_id}", None),
        ("PUT", f"/v1/admin/articles/{some_id}", _BODY),
        ("POST", f"/v1/admin/articles/{some_id}/publish", {"operator": "alice"}),
        ("POST", f"/v1/admin/articles/{some_id}/archive", {"operator": "alice", "reason": "old"}),
    ]
    for method, path, body in calls:
        assert client.request(method, path, json=body).status_code == 401, path
        assert client.request(method, path, json=body, headers=AUTH).status_code == 401, path
    assert store.rows == {}


def test_create_returns_201_and_a_draft_named_after_the_operator() -> None:
    client, store = make_client()

    response = client.post("/v1/admin/articles", json=_BODY, headers=ADMIN)

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "draft"
    row = store.rows[UUID(payload["article_id"])]
    assert row["created_by"] == "alice"
    assert row["images"] == _BODY["images"]
    assert store.audit == [("article.created", "alice")]


def test_an_http_picture_url_is_a_422_and_writes_nothing() -> None:
    client, store = make_client()
    body = dict(_BODY, images=[{"url": "http://cdn.example/hero.jpg", "alt": ""}])

    assert client.post("/v1/admin/articles", json=body, headers=ADMIN).status_code == 422
    body = dict(_BODY, sources=[{"name": "x", "url": "ftp://news.example/story"}])
    assert client.post("/v1/admin/articles", json=body, headers=ADMIN).status_code == 422
    assert store.rows == {}


def test_publish_archive_republish_and_the_no_op() -> None:
    client, store = make_client()
    article_id = client.post("/v1/admin/articles", json=_BODY, headers=ADMIN).json()["article_id"]

    published = client.post(
        f"/v1/admin/articles/{article_id}/publish", json={"operator": "bob"}, headers=ADMIN
    )
    assert published.status_code == 200
    assert published.json()["status"] == "published"
    first_published_at = store.rows[UUID(article_id)]["published_at"]

    # Publishing again changes nothing and audits nothing.
    again = client.post(
        f"/v1/admin/articles/{article_id}/publish", json={"operator": "bob"}, headers=ADMIN
    )
    assert again.json()["status"] == "published"

    archived = client.post(
        f"/v1/admin/articles/{article_id}/archive",
        json={"operator": "carol", "reason": "superseded"},
        headers=ADMIN,
    )
    assert archived.json()["status"] == "archived"

    client.post(
        f"/v1/admin/articles/{article_id}/publish", json={"operator": "dave"}, headers=ADMIN
    )
    assert store.rows[UUID(article_id)]["published_at"] == first_published_at
    assert store.audit == [
        ("article.created", "alice"),
        ("article.published", "bob"),
        ("article.archived", "carol"),
        ("article.published", "dave"),
    ]


def test_edit_updates_content_and_names_the_editor() -> None:
    client, _ = make_client()
    article_id = client.post("/v1/admin/articles", json=_BODY, headers=ADMIN).json()["article_id"]

    response = client.put(
        f"/v1/admin/articles/{article_id}",
        json=dict(_BODY, title="A better title", operator="erin"),
        headers=ADMIN,
    )

    assert response.status_code == 200
    assert response.json()["title"] == "A better title"
    assert response.json()["updated_by"] == "erin"
    assert response.json()["created_by"] == "alice"


def test_unknown_id_is_404_article_not_found_on_every_route() -> None:
    client, _ = make_client()
    missing = uuid4()
    responses = [
        client.get(f"/v1/admin/articles/{missing}", headers=ADMIN),
        client.put(f"/v1/admin/articles/{missing}", json=_BODY, headers=ADMIN),
        client.post(f"/v1/admin/articles/{missing}/publish", json={"operator": "a"}, headers=ADMIN),
        client.post(
            f"/v1/admin/articles/{missing}/archive",
            json={"operator": "a", "reason": "gone"},
            headers=ADMIN,
        ),
    ]
    for response in responses:
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "article_not_found"


def test_the_list_carries_every_status() -> None:
    client, _ = make_client()
    draft = client.post("/v1/admin/articles", json=_BODY, headers=ADMIN).json()["article_id"]
    live = client.post("/v1/admin/articles", json=_BODY, headers=ADMIN).json()["article_id"]
    client.post(f"/v1/admin/articles/{live}/publish", json={"operator": "a"}, headers=ADMIN)

    listed = client.get("/v1/admin/articles", headers=ADMIN).json()["articles"]

    assert {a["article_id"]: a["status"] for a in listed} == {draft: "draft", live: "published"}


def test_articles_never_mention_a_user_ref() -> None:
    """Spec §3: articles are not identity data. Enforced as a grep, like the
    other boundary gates in tests/test_boundaries.py."""
    root = Path(__file__).resolve().parents[1] / "src" / "imageshield"
    for path in (
        root / "articles" / "store.py",
        root / "http" / "routes" / "admin_articles.py",
    ):
        assert "user_ref" not in path.read_text(encoding="utf-8"), path
