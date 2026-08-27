"""``PostgresArticleStore`` and ``svc.v_articles`` against real Postgres.

What is under test is the transaction shape (one audit row per write, none
for a no-op), the status CHECKs the database enforces, and the projection the
proxy reads — published rows only, readable by ``imageshield_proxy_ro`` and
by nobody through the base table.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from imageshield.articles.store import (
    ARTICLE_ARCHIVED_ACTION,
    ARTICLE_CREATED_ACTION,
    ARTICLE_PUBLISHED_ACTION,
    ARTICLE_UPDATED_ACTION,
    PostgresArticleStore,
)
from imageshield.db.connection import make_async_pool
from tests.db import run_migrate


@pytest.fixture
def migrated_db(throwaway_db: str) -> str:
    down_result = run_migrate(throwaway_db, "down", "--all")
    assert down_result.returncode == 0, down_result.stderr
    up_result = run_migrate(throwaway_db, "up")
    assert up_result.returncode == 0, up_result.stderr
    return throwaway_db


@pytest.fixture
async def pool(migrated_db: str) -> AsyncIterator[AsyncConnectionPool]:
    p = make_async_pool(migrated_db, min_size=1, max_size=2)
    await p.open()
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture
def store(pool: AsyncConnectionPool) -> PostgresArticleStore:
    return PostgresArticleStore(pool)


def _rows(db_url: str, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(db_url, autocommit=True) as conn:
        return list(conn.execute(sql, params).fetchall())


_IMAGES = [{"url": "https://cdn.example/a.jpg", "alt": "album"}]
_SOURCES = [{"name": "Example News", "url": "https://news.example/story"}]


async def _create(store: PostgresArticleStore, *, operator: str = "alice") -> UUID:
    return await store.create_article(
        title="Older photos of you circulate too",
        summary="blurb",
        body="text",
        images=_IMAGES,
        sources=_SOURCES,
        operator=operator,
    )


async def test_every_write_lands_exactly_one_audit_row_naming_the_operator(
    store: PostgresArticleStore, migrated_db: str
) -> None:
    article_id = await _create(store, operator="alice")
    assert await store.update_article(
        article_id, title="T2", summary="", body="", images=[], sources=[], operator="bob"
    )
    assert await store.publish_article(article_id, operator="carol") == "published"
    assert await store.publish_article(article_id, operator="carol") == "published"  # no-op
    assert await store.archive_article(article_id, operator="dave", reason="superseded") == (
        "archived"
    )

    audit = _rows(
        migrated_db,
        "SELECT action, metadata->>'operator', actor_type FROM audit_log"
        " WHERE resource_id = %s ORDER BY audit_id",
        (article_id,),
    )
    assert audit == [
        (ARTICLE_CREATED_ACTION, "alice", "operator"),
        (ARTICLE_UPDATED_ACTION, "bob", "operator"),
        (ARTICLE_PUBLISHED_ACTION, "carol", "operator"),
        (ARTICLE_ARCHIVED_ACTION, "dave", "operator"),
    ]
    assert _rows(
        migrated_db,
        "SELECT metadata->>'reason' FROM audit_log WHERE action = %s",
        (ARTICLE_ARCHIVED_ACTION,),
    ) == [("superseded",)]


async def test_the_view_shows_published_rows_only(
    store: PostgresArticleStore, migrated_db: str
) -> None:
    article_id = await _create(store)
    assert _rows(migrated_db, "SELECT count(*) FROM svc.v_articles") == [(0,)]

    await store.publish_article(article_id, operator="alice")
    rows = _rows(
        migrated_db,
        "SELECT article_id, title, images, sources FROM svc.v_articles",
    )
    assert rows == [(article_id, "Older photos of you circulate too", _IMAGES, _SOURCES)]

    await store.archive_article(article_id, operator="alice", reason="superseded")
    assert _rows(migrated_db, "SELECT count(*) FROM svc.v_articles") == [(0,)]


async def test_a_republish_keeps_the_original_published_at(
    store: PostgresArticleStore, migrated_db: str
) -> None:
    article_id = await _create(store)
    await store.publish_article(article_id, operator="alice")
    (first,) = _rows(
        migrated_db, "SELECT published_at FROM articles WHERE article_id = %s", (article_id,)
    )[0]
    await store.archive_article(article_id, operator="alice", reason="pause")
    assert await store.publish_article(article_id, operator="alice") == "published"
    (again,) = _rows(
        migrated_db, "SELECT published_at FROM articles WHERE article_id = %s", (article_id,)
    )[0]
    assert again == first


async def test_unknown_ids_return_none_and_write_no_audit_row(
    store: PostgresArticleStore, migrated_db: str
) -> None:
    missing = uuid4()
    assert await store.publish_article(missing, operator="a") is None
    assert await store.archive_article(missing, operator="a", reason="gone") is None
    assert (
        await store.update_article(
            missing, title="T", summary="", body="", images=[], sources=[], operator="a"
        )
        is False
    )
    assert await store.get_article(missing) is None
    assert _rows(
        migrated_db, "SELECT count(*) FROM audit_log WHERE resource_id = %s", (missing,)
    ) == [(0,)]


def test_the_proxy_role_reads_the_view_and_not_the_table(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        conn.execute("SET ROLE imageshield_proxy_ro")
        conn.execute("SELECT * FROM svc.v_articles")  # must not raise
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM public.articles")
        conn.execute("RESET ROLE")


def test_the_database_refuses_a_published_row_with_no_date_and_a_dated_draft(
    migrated_db: str,
) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO articles (title, status, created_by, updated_by)"
                " VALUES ('t', 'published', 'a', 'a')"
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO articles (title, status, published_at, created_by, updated_by)"
                " VALUES ('t', 'draft', now(), 'a', 'a')"
            )


def test_the_database_refuses_non_array_pictures(migrated_db: str) -> None:
    with (
        psycopg.connect(migrated_db, autocommit=True) as conn,
        pytest.raises(psycopg.errors.CheckViolation),
    ):
        conn.execute(
            "INSERT INTO articles (title, images, created_by, updated_by)"
            " VALUES ('t', '{\"url\": \"https://x.example/a.jpg\"}'::jsonb, 'a', 'a')"
        )
