"""Articles — operator CRUD for the app's feed (spec 2026-08-27 §6).

Same posture as ``admin_threat_events.py``: both tokens at router level, so a
route added here is guarded structurally. Nothing here touches a score, a
subject or a hit — an article is operator content published to every user
with no per-person state — so there is no recompute loop and no ``user_ref``
anywhere in this file.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Query

from imageshield.articles.store import ArticleStore
from imageshield.http.auth import require_admin_service_token, require_service_token
from imageshield.http.deps import get_article_store
from imageshield.http.errors import ServiceError
from imageshield.http.models import (
    ArticleArchiveRequest,
    ArticleCreateResponse,
    ArticleItem,
    ArticlePublishRequest,
    ArticlesResponse,
    ArticleStatusResponse,
    ArticleUpsertRequest,
)

log = structlog.get_logger("imageshield.articles")

router = APIRouter(
    prefix="/v1/admin/articles",
    dependencies=[Depends(require_service_token), Depends(require_admin_service_token)],
)


def _not_found() -> ServiceError:
    return ServiceError(404, "article_not_found", "No article with this id.", retryable=False)


def _item(row: dict[str, Any]) -> ArticleItem:
    return ArticleItem(
        article_id=row["article_id"],
        title=row["title"],
        summary=row["summary"],
        body=row["body"],
        images=list(row["images"]),
        sources=list(row["sources"]),
        status=row["status"],
        published_at=row["published_at"],
        created_by=row["created_by"],
        updated_by=row["updated_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _content(body: ArticleUpsertRequest) -> dict[str, Any]:
    return {
        "title": body.title,
        "summary": body.summary,
        "body": body.body,
        "images": [image.model_dump() for image in body.images],
        "sources": [source.model_dump() for source in body.sources],
        "operator": body.operator,
    }


@router.get("")
async def list_articles(
    limit: int = Query(50, ge=1, le=200),
    store: ArticleStore = Depends(get_article_store),
) -> ArticlesResponse:
    rows = await store.list_articles(limit=limit)
    return ArticlesResponse(articles=[_item(row) for row in rows])


@router.post("", status_code=201)
async def create_article(
    body: ArticleUpsertRequest,
    store: ArticleStore = Depends(get_article_store),
) -> ArticleCreateResponse:
    article_id = await store.create_article(**_content(body))
    log.info("article.created_via_admin", article_id=str(article_id), operator=body.operator)
    return ArticleCreateResponse(article_id=article_id)


@router.get("/{article_id}")
async def get_article(
    article_id: UUID,
    store: ArticleStore = Depends(get_article_store),
) -> ArticleItem:
    row = await store.get_article(article_id)
    if row is None:
        raise _not_found()
    return _item(row)


@router.put("/{article_id}")
async def update_article(
    article_id: UUID,
    body: ArticleUpsertRequest,
    store: ArticleStore = Depends(get_article_store),
) -> ArticleItem:
    if not await store.update_article(article_id, **_content(body)):
        raise _not_found()
    row = await store.get_article(article_id)
    if row is None:  # nothing deletes an article; this keeps the type honest
        raise _not_found()
    return _item(row)


@router.post("/{article_id}/publish")
async def publish_article(
    article_id: UUID,
    body: ArticlePublishRequest,
    store: ArticleStore = Depends(get_article_store),
) -> ArticleStatusResponse:
    status = await store.publish_article(article_id, operator=body.operator)
    if status is None:
        raise _not_found()
    return ArticleStatusResponse(article_id=article_id, status=status)


@router.post("/{article_id}/archive")
async def archive_article(
    article_id: UUID,
    body: ArticleArchiveRequest,
    store: ArticleStore = Depends(get_article_store),
) -> ArticleStatusResponse:
    status = await store.archive_article(article_id, operator=body.operator, reason=body.reason)
    if status is None:
        raise _not_found()
    return ArticleStatusResponse(article_id=article_id, status=status)
