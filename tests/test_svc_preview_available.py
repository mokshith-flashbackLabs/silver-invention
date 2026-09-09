"""`svc.v_person_hits.preview_available` must agree with the render path.

Two expressions of one fact is the drift this repo keeps finding, so this is
the test that ties them: 0031's view column and
``preview.store.PostgresPreviewStore.target`` both answer "can this hit show a
picture?", and the proxy trusts the first while the subject experiences the
second. If they disagree, a card promises an image and the preview 404s --
which is exactly what happened on 2026-09-08 when the proxy was guessing from
`severity` instead.

The JSONB-null case has its own test on purpose. `triage -> 'best_face_bbox'`
returns a JSONB null for a key present-but-null, and JSONB null is not SQL
NULL, so `IS NOT NULL` is TRUE for a bbox that is literally `null`. Four real
hits were in that state, and the wrong check reported 7 renderable previews
where there were 3.
"""

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

pytestmark = pytest.mark.asyncio

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


def _hit(
    conn: psycopg.Connection[Any],
    user_ref: UserRef,
    *,
    image_url: str | None,
    preview_image_url: str | None,
    triage: dict[str, Any] | None,
    confirm_state: str = "machine_triaged",
) -> UUID:
    url_hash = uuid4().hex + uuid4().hex
    url = f"https://example.test/{uuid4().hex}"
    conn.execute(
        "INSERT INTO content_urls (url_hash, url, source_domain, canonical_url)"
        " VALUES (%s, %s, 'example.test', %s)",
        (url_hash, url, url),
    )
    row = conn.execute(
        "INSERT INTO infringements"
        " (user_ref, url_hash, page_url, image_url, preview_image_url, confirm_state)"
        " VALUES (%s, %s, %s, %s, %s, %s) RETURNING infringement_id",
        (user_ref, url_hash, url, image_url, preview_image_url, confirm_state),
    ).fetchone()
    assert row is not None
    infringement_id: UUID = row[0]
    if triage is not None:
        conn.execute(
            "INSERT INTO review_tasks (infringement_id, user_ref, severity, triage)"
            " VALUES (%s, %s, 'unassessed', %s)",
            (infringement_id, user_ref, Jsonb(triage)),
        )
    return infringement_id


def _view_says(conn: psycopg.Connection[Any], infringement_id: UUID) -> bool:
    row = conn.execute(
        "SELECT preview_available FROM svc.v_person_hits WHERE hit_id = %s",
        (infringement_id,),
    ).fetchone()
    assert row is not None, "the hit must be visible in the view"
    return bool(row[0])


_CASES: list[tuple[str, str | None, str | None, dict[str, Any] | None, bool]] = [
    # name, image_url, preview_image_url, triage, expected
    ("provider image + real bbox", "https://cdn.test/a.jpg", None, {"best_face_bbox": BBOX}, True),
    ("resolved preview + real bbox", None, "https://cdn.test/b.jpg", {"best_face_bbox": BBOX}, True),
    ("both urls present", "https://cdn.test/a.jpg", "https://cdn.test/b.jpg", {"best_face_bbox": BBOX}, True),
    ("no url at all", None, None, {"best_face_bbox": BBOX}, False),
    ("bbox is JSONB null", "https://cdn.test/a.jpg", None, {"best_face_bbox": None}, False),
    ("bbox key absent", "https://cdn.test/a.jpg", None, {"unfetchable": "nope"}, False),
    ("no review task at all", "https://cdn.test/a.jpg", None, None, False),
]


@pytest.mark.parametrize("name,image_url,preview_url,triage,expected", _CASES)
async def test_view_column_matches_the_render_path(
    migrated_db: str,
    store: PostgresPreviewStore,
    name: str,
    image_url: str | None,
    preview_url: str | None,
    triage: dict[str, Any] | None,
    expected: bool,
) -> None:
    user_ref = UserRef(uuid4())
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        hit_id = _hit(
            conn,
            user_ref,
            image_url=image_url,
            preview_image_url=preview_url,
            triage=triage,
        )
        from_view = _view_says(conn, hit_id)

    target = await store.target(hit_id, user_ref)

    renderable = target is not None and target.image_url is not None and target.bbox is not None

    assert from_view is expected, f"{name}: view said {from_view}, expected {expected}"
    assert renderable is expected, f"{name}: render path said {renderable}, expected {expected}"
    assert from_view is renderable, (
        f"{name}: the view and the render path DISAGREE "
        f"(view={from_view}, render={renderable}) — a card would promise an "
        f"image the preview cannot serve"
    )
