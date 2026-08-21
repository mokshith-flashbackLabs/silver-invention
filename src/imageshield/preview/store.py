"""Persistence for the subject preview surface — raw SQL, no ORM (CLAUDE.md §2).

``target`` answers one question with one row: may THIS user_ref see a crop for
THIS hit, and from where. Absent, not-theirs, quarantined and duplicate are one
indistinguishable ``None`` — the same 404-oracle discipline as the feedback
endpoint (a caller who can tell "not yours" from "not there" can walk the id
space and learn that a given infringement exists).

The render ceiling (INVARIANTS #32) counts ``preview.rendered`` audit rows in a
rolling 24h window, against migration 0024's partial index. The count and the
insert are deliberately NOT atomic: the ceiling is an abuse brake, not an exact
quota — two racing requests overshooting by one is acceptable, a lock on
``audit_log`` is not.

The audit row is written BEFORE the crop is rendered (INVARIANTS #31): a render
that then fails upstream still shows an attempt, and still counts against the
ceiling.
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict

from imageshield.types import UserRef

PREVIEW_RENDERED_ACTION = "preview.rendered"

# LEFT JOIN: a hit that never triaged has no review_tasks row — that is the
# "preview not available yet" case, not a missing hit. 0021's UNIQUE
# (infringement_id) guarantees at most one task row.
_TARGET_SQL = """
    SELECT i.image_url, rt.triage -> 'best_face_bbox'
    FROM infringements i
    LEFT JOIN review_tasks rt ON rt.infringement_id = i.infringement_id
    WHERE i.infringement_id = %(infringement_id)s
      AND i.user_ref = %(user_ref)s
      AND i.confirm_state NOT IN ('quarantined', 'duplicate')
"""

_COUNT_RENDERS_SQL = """
    SELECT count(*) FROM audit_log
    WHERE action = 'preview.rendered'
      AND subject_ref = %(user_ref)s
      AND occurred_at > now() - interval '24 hours'
"""

_RECORD_RENDER_SQL = """
    INSERT INTO audit_log (actor_type, action, subject_ref, resource_id, metadata)
    VALUES ('subject', %(action)s, %(user_ref)s, %(infringement_id)s, %(metadata)s)
"""


class PreviewTarget(BaseModel):
    """What one hit offers the crop renderer. ``bbox`` is the hit image's best
    face box from machine triage (``review_tasks.triage``) — normalised floats
    about the image, never pixels (INVARIANTS #9)."""

    model_config = ConfigDict(frozen=True)

    image_url: str | None
    bbox: dict[str, float] | None


class PreviewStore(Protocol):
    async def target(
        self, infringement_id: UUID, user_ref: UserRef
    ) -> PreviewTarget | None: ...

    async def renders_last_24h(self, user_ref: UserRef) -> int: ...

    async def record_render(
        self, user_ref: UserRef, infringement_id: UUID, *, reveal: bool
    ) -> None: ...


class PostgresPreviewStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def target(
        self, infringement_id: UUID, user_ref: UserRef
    ) -> PreviewTarget | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                _TARGET_SQL,
                {"infringement_id": infringement_id, "user_ref": user_ref},
            )
            row = await cur.fetchone()
        if row is None:
            return None
        image_url, raw_bbox = row
        bbox: dict[str, float] | None = None
        if isinstance(raw_bbox, dict):
            # Defensive shape check rather than a blind cast: the triage JSONB
            # is machine-written, but a malformed bbox must degrade to
            # "no preview", never to a fetcher call with garbage coordinates.
            try:
                bbox = {key: float(raw_bbox[key]) for key in ("x", "y", "w", "h")}
            except (KeyError, TypeError, ValueError):
                bbox = None
        return PreviewTarget(image_url=image_url, bbox=bbox)

    async def renders_last_24h(self, user_ref: UserRef) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_COUNT_RENDERS_SQL, {"user_ref": user_ref})
            row = await cur.fetchone()
        assert row is not None
        count: int = row[0]
        return count

    async def record_render(
        self, user_ref: UserRef, infringement_id: UUID, *, reveal: bool
    ) -> None:
        metadata: dict[str, Any] = {"reveal": reveal}
        async with self._pool.connection() as conn:
            await conn.execute(
                _RECORD_RENDER_SQL,
                {
                    "action": PREVIEW_RENDERED_ACTION,
                    "user_ref": user_ref,
                    "infringement_id": infringement_id,
                    "metadata": Jsonb(metadata),
                },
            )


__all__ = [
    "PREVIEW_RENDERED_ACTION",
    "PostgresPreviewStore",
    "PreviewStore",
    "PreviewTarget",
]
