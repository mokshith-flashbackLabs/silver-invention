"""Articles — operator-authored content for the app feed (spec 2026-08-27).

The one writer of ``articles``. Every write is one transaction: the row
change plus one ``audit_log`` row naming the operator, the same shape as
``threats/store.py``. Reads are plain projections.

Not identity data: nothing here takes or returns a ``user_ref``. Pictures are
pasted URLs (INVARIANTS #9 -- no bytes, anywhere); the proxy reads published
rows through ``svc.v_articles`` (migration 0026), never this table.
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

import structlog
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

log = structlog.get_logger("imageshield.articles")

ARTICLE_CREATED_ACTION = "article.created"
ARTICLE_UPDATED_ACTION = "article.updated"
ARTICLE_PUBLISHED_ACTION = "article.published"
ARTICLE_ARCHIVED_ACTION = "article.archived"

_INSERT_SQL = """
    INSERT INTO articles (title, summary, body, images, sources, created_by, updated_by)
    VALUES (%(title)s, %(summary)s, %(body)s, %(images)s, %(sources)s,
            %(operator)s, %(operator)s)
    RETURNING article_id
"""

_UPDATE_SQL = """
    UPDATE articles
       SET title = %(title)s, summary = %(summary)s, body = %(body)s,
           images = %(images)s, sources = %(sources)s,
           updated_by = %(operator)s, updated_at = now()
     WHERE article_id = %(article_id)s
    RETURNING article_id
"""

# COALESCE keeps the original date on a re-publish from archived; a first
# publish stamps now(). The status filter makes an already-published row a
# no-op rather than a second audit entry for nothing.
_PUBLISH_SQL = """
    UPDATE articles
       SET status = 'published',
           published_at = COALESCE(published_at, now()),
           updated_by = %(operator)s, updated_at = now()
     WHERE article_id = %(article_id)s AND status <> 'published'
    RETURNING article_id
"""

_ARCHIVE_SQL = """
    UPDATE articles
       SET status = 'archived', updated_by = %(operator)s, updated_at = now()
     WHERE article_id = %(article_id)s AND status <> 'archived'
    RETURNING article_id
"""

_STATUS_SQL = "SELECT status FROM articles WHERE article_id = %(article_id)s"

_GET_SQL = """
    SELECT article_id, title, summary, body, images, sources, status, published_at,
           created_by, updated_by, created_at, updated_at
    FROM articles
    WHERE article_id = %(article_id)s
"""

_LIST_SQL = """
    SELECT article_id, title, summary, body, images, sources, status, published_at,
           created_by, updated_by, created_at, updated_at
    FROM articles
    ORDER BY updated_at DESC
    LIMIT %(limit)s
"""

_AUDIT_SQL = """
    INSERT INTO audit_log (actor_type, action, resource_id, metadata)
    VALUES ('operator', %(action)s, %(article_id)s, %(metadata)s)
"""


class ArticleStore(Protocol):
    async def create_article(
        self,
        *,
        title: str,
        summary: str,
        body: str,
        images: list[dict[str, str]],
        sources: list[dict[str, str]],
        operator: str,
    ) -> UUID: ...

    async def update_article(
        self,
        article_id: UUID,
        *,
        title: str,
        summary: str,
        body: str,
        images: list[dict[str, str]],
        sources: list[dict[str, str]],
        operator: str,
    ) -> bool: ...

    async def publish_article(self, article_id: UUID, *, operator: str) -> str | None: ...

    async def archive_article(
        self, article_id: UUID, *, operator: str, reason: str
    ) -> str | None: ...

    async def get_article(self, article_id: UUID) -> dict[str, Any] | None: ...

    async def list_articles(self, *, limit: int = 50) -> list[dict[str, Any]]: ...


def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "article_id": row[0],
        "title": row[1],
        "summary": row[2],
        "body": row[3],
        "images": list(row[4]),
        "sources": list(row[5]),
        "status": row[6],
        "published_at": row[7],
        "created_by": row[8],
        "updated_by": row[9],
        "created_at": row[10],
        "updated_at": row[11],
    }


class PostgresArticleStore:
    """The one writer of ``articles``."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def create_article(
        self,
        *,
        title: str,
        summary: str,
        body: str,
        images: list[dict[str, str]],
        sources: list[dict[str, str]],
        operator: str,
    ) -> UUID:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                _INSERT_SQL,
                {
                    "title": title,
                    "summary": summary,
                    "body": body,
                    "images": Jsonb(images),
                    "sources": Jsonb(sources),
                    "operator": operator,
                },
            )
            row = await cur.fetchone()
            assert row is not None
            article_id: UUID = row[0]
            await conn.execute(
                _AUDIT_SQL,
                {
                    "action": ARTICLE_CREATED_ACTION,
                    "article_id": article_id,
                    "metadata": Jsonb({"operator": operator, "title": title}),
                },
            )
        log.info("article.created", article_id=str(article_id), operator=operator)
        return article_id

    async def update_article(
        self,
        article_id: UUID,
        *,
        title: str,
        summary: str,
        body: str,
        images: list[dict[str, str]],
        sources: list[dict[str, str]],
        operator: str,
    ) -> bool:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                _UPDATE_SQL,
                {
                    "article_id": article_id,
                    "title": title,
                    "summary": summary,
                    "body": body,
                    "images": Jsonb(images),
                    "sources": Jsonb(sources),
                    "operator": operator,
                },
            )
            if await cur.fetchone() is None:
                return False
            await conn.execute(
                _AUDIT_SQL,
                {
                    "action": ARTICLE_UPDATED_ACTION,
                    "article_id": article_id,
                    "metadata": Jsonb({"operator": operator, "title": title}),
                },
            )
        log.info("article.updated", article_id=str(article_id), operator=operator)
        return True

    async def publish_article(self, article_id: UUID, *, operator: str) -> str | None:
        return await self._transition(
            article_id,
            _PUBLISH_SQL,
            action=ARTICLE_PUBLISHED_ACTION,
            metadata={"operator": operator},
            operator=operator,
        )

    async def archive_article(self, article_id: UUID, *, operator: str, reason: str) -> str | None:
        return await self._transition(
            article_id,
            _ARCHIVE_SQL,
            action=ARTICLE_ARCHIVED_ACTION,
            metadata={"operator": operator, "reason": reason},
            operator=operator,
        )

    async def _transition(
        self,
        article_id: UUID,
        transition_sql: str,
        *,
        action: str,
        metadata: dict[str, Any],
        operator: str,
    ) -> str | None:
        """Apply a status change and audit it, or report the unchanged status.

        Returns the article's status after the call, or ``None`` for an
        unknown id. A no-op (already in the target state) writes no audit
        row: an audit entry for a write that did not happen is the same
        half-applied state ``threats/store.py`` refuses to leave behind.
        """
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                transition_sql, {"article_id": article_id, "operator": operator}
            )
            changed = await cur.fetchone() is not None
            if changed:
                await conn.execute(
                    _AUDIT_SQL,
                    {"action": action, "article_id": article_id, "metadata": Jsonb(metadata)},
                )
            cur = await conn.execute(_STATUS_SQL, {"article_id": article_id})
            row = await cur.fetchone()
        if row is None:
            return None
        status = str(row[0])
        log.info(action, article_id=str(article_id), operator=operator, changed=changed)
        return status

    async def get_article(self, article_id: UUID) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_GET_SQL, {"article_id": article_id})
            row = await cur.fetchone()
        return None if row is None else _row_to_dict(row)

    async def list_articles(self, *, limit: int = 50) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_LIST_SQL, {"limit": limit})
            rows = await cur.fetchall()
        return [_row_to_dict(row) for row in rows]
