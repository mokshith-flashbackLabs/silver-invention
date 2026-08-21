"""PostgresPreviewStore against real Postgres (fixture convention of
tests/test_confirm_store.py: own down --all + up arrange step, direct SQL for
fixtures).

The load-bearing behaviours: the ownership/visibility ``None`` is one
indistinguishable answer for absent / not-yours / quarantined / duplicate
(the feedback endpoint's 404-oracle discipline), and the render audit trail
both records and rate-limits (INVARIANTS #31/#32)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from imageshield.db.connection import make_async_pool
from imageshield.preview.store import PostgresPreviewStore
from imageshield.types import UserRef
from tests.db import run_migrate

BBOX = {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}


@pytest.fixture
def migrated_db(throwaway_db: str) -> str:
    run_migrate(throwaway_db, "down", "--all")
    up_result = run_migrate(throwaway_db, "up")
    assert up_result.returncode == 0, up_result.stderr
    return throwaway_db


@pytest.fixture
async def store(migrated_db: str) -> AsyncIterator[PostgresPreviewStore]:
    pool = make_async_pool(migrated_db, min_size=1, max_size=2)
    await pool.open()
    try:
        yield PostgresPreviewStore(pool)
    finally:
        await pool.close()


def _user() -> UserRef:
    return UserRef(uuid4())


def _infringement(
    conn: psycopg.Connection[Any],
    user_ref: UserRef,
    *,
    confirm_state: str = "machine_triaged",
    image_url: str | None = "set",
) -> UUID:
    url_hash = uuid4().hex + uuid4().hex
    url = f"https://example.test/{uuid4().hex}"
    row = conn.execute(
        "INSERT INTO content_urls (url_hash, url, source_domain, canonical_url)"
        " VALUES (%s, %s, 'example.test', %s)",
        (url_hash, url, url),
    )
    row = conn.execute(
        "INSERT INTO infringements (user_ref, url_hash, page_url, image_url, confirm_state)"
        " VALUES (%s, %s, %s, %s, %s) RETURNING infringement_id",
        (
            user_ref,
            url_hash,
            url,
            f"{url}.jpg" if image_url == "set" else image_url,
            confirm_state,
        ),
    ).fetchone()
    assert row is not None
    infringement_id: UUID = row[0]
    return infringement_id


def _task(
    conn: psycopg.Connection[Any],
    infringement_id: UUID,
    user_ref: UserRef,
    *,
    triage: dict[str, Any],
) -> None:
    conn.execute(
        "INSERT INTO review_tasks (infringement_id, user_ref, severity, triage)"
        " VALUES (%s, %s, 'benign_copy', %s)",
        (infringement_id, user_ref, Jsonb(triage)),
    )


# ── target ────────────────────────────────────────────────────────────────


async def test_owner_gets_image_url_and_bbox(
    migrated_db: str, store: PostgresPreviewStore
) -> None:
    user_ref = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        infringement_id = _infringement(conn, user_ref)
        _task(conn, infringement_id, user_ref, triage={"best_face_bbox": BBOX})

    target = await store.target(infringement_id, user_ref)

    assert target is not None
    assert target.image_url is not None and target.image_url.endswith(".jpg")
    assert target.bbox == BBOX


async def test_wrong_user_ref_is_none(
    migrated_db: str, store: PostgresPreviewStore
) -> None:
    user_ref = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        infringement_id = _infringement(conn, user_ref)
        _task(conn, infringement_id, user_ref, triage={"best_face_bbox": BBOX})

    assert await store.target(infringement_id, _user()) is None


@pytest.mark.parametrize("state", ["quarantined", "duplicate"])
async def test_invisible_states_are_none(
    migrated_db: str, store: PostgresPreviewStore, state: str
) -> None:
    """To the subject these rows do not exist — same answer as absent, so the
    response can never confirm that a quarantined hit is out there."""
    user_ref = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        if state == "duplicate":
            original = _infringement(conn, user_ref)
            infringement_id = _infringement(conn, user_ref, confirm_state="unconfirmed")
            conn.execute(
                "UPDATE infringements SET confirm_state = 'duplicate', duplicate_of = %s"
                " WHERE infringement_id = %s",
                (original, infringement_id),
            )
        else:
            infringement_id = _infringement(conn, user_ref, confirm_state=state)

    assert await store.target(infringement_id, user_ref) is None


async def test_untriaged_hit_has_no_bbox_but_is_not_none(
    migrated_db: str, store: PostgresPreviewStore
) -> None:
    """No review_tasks row yet = 'being checked': the route answers
    preview_unavailable, not the ownership 404."""
    user_ref = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        infringement_id = _infringement(conn, user_ref, confirm_state="unconfirmed")

    target = await store.target(infringement_id, user_ref)

    assert target is not None
    assert target.bbox is None


async def test_malformed_bbox_degrades_to_none(
    migrated_db: str, store: PostgresPreviewStore
) -> None:
    """A malformed triage bbox must become 'no preview', never a fetcher call
    with garbage coordinates."""
    user_ref = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        infringement_id = _infringement(conn, user_ref)
        _task(
            conn,
            infringement_id,
            user_ref,
            triage={"best_face_bbox": {"x": 0.1, "y": "not a number"}},
        )

    target = await store.target(infringement_id, user_ref)

    assert target is not None
    assert target.bbox is None


# ── render audit + ceiling ────────────────────────────────────────────────


async def test_record_render_writes_the_audit_row_and_counts(
    migrated_db: str, store: PostgresPreviewStore
) -> None:
    user_ref = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        infringement_id = _infringement(conn, user_ref)

    await store.record_render(user_ref, infringement_id, reveal=True)

    with psycopg.connect(migrated_db, autocommit=True) as conn:
        row = conn.execute(
            "SELECT actor_type, action, subject_ref, resource_id, metadata"
            " FROM audit_log WHERE action = 'preview.rendered'",
        ).fetchone()
    assert row is not None
    assert row[0] == "subject"
    assert row[2] == user_ref
    assert row[3] == infringement_id
    assert row[4] == {"reveal": True}
    assert await store.renders_last_24h(user_ref) == 1


async def test_ceiling_count_scopes_to_user_and_window(
    migrated_db: str, store: PostgresPreviewStore
) -> None:
    user_ref = _user()
    other = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        infringement_id = _infringement(conn, user_ref)
        # Another user's render and a stale render (25h old) must not count.
        conn.execute(
            "INSERT INTO audit_log (actor_type, action, subject_ref, resource_id, metadata)"
            " VALUES ('subject', 'preview.rendered', %s, %s, '{}'::jsonb)",
            (other, infringement_id),
        )
        conn.execute(
            "INSERT INTO audit_log"
            " (actor_type, action, subject_ref, resource_id, metadata, occurred_at)"
            " VALUES ('subject', 'preview.rendered', %s, %s, '{}'::jsonb,"
            " now() - interval '25 hours')",
            (user_ref, infringement_id),
        )

    await store.record_render(user_ref, infringement_id, reveal=False)

    assert await store.renders_last_24h(user_ref) == 1
