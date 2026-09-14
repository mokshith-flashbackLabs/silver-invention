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

``restricted`` (2026-09-14) travels back as a flag rather than as a ``None``:
the refusal is the route's, not this query's. See the comment on
``_TARGET_SQL``.
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
# coalesce, not image_url (0030): when the provider keyed this hit on a page it
# supplied no image address at all -- Google's `pagesWithMatchingImages` entries
# carry only `url` and `pageTitle` -- so the fetchable image is the og:image the
# confirm pipeline resolved from the page. Reading image_url alone is what left
# 11 of 12 real hits with no preview on 2026-09-07: the column was non-null, so
# nothing looked broken, but it held a page address the fetcher refuses.
# preview_image_url is NULL whenever image_url was directly fetchable, so the
# ordinary case is unchanged.
#
# `restricted` (2026-09-14) is the subject-refusal flag, NOT a renderability
# flag, and the difference is the whole reason it is a column rather than a
# `None`. A confirmed `ncii_suspected` finding is one the machine (or an
# operator) marked infringing without asking the subject; owner decision D4
# says they are shown nothing and asked nothing about it, so the ROUTE refuses
# it. `target()` still answers truthfully that a picture exists, because
# `svc.v_person_hits.preview_available` mirrors this query's renderability
# logic row by row (0031, tests/test_svc_preview_available.py) and that column
# means "a picture exists" -- collapsing a restricted hit to `None` here would
# silently change what the proxy's column means.
#
# The predicate is state+severity rather than the `auto:nsfw` marker: every
# auto-confirmed row is confirmed + ncii_suspected, so it catches all of them
# either way, AND it also catches an OPERATOR-confirmed `ncii_suspected` hit,
# which the backend presents the same restricted way. One predicate on both
# sides of the boundary is what stops the two drifting apart.
_TARGET_SQL = """
    SELECT coalesce(i.preview_image_url, i.image_url), rt.triage -> 'best_face_bbox',
           (i.confirm_state = 'confirmed' AND i.severity = 'ncii_suspected') AS restricted
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
    about the image, never pixels (INVARIANTS #9).

    ``restricted`` is the one field that is not about rendering: a confirmed
    ``ncii_suspected`` finding is never shown to its subject (2026-09-14,
    owner decision D4), and the route refuses it before it charges the render
    ceiling or writes the audit row. It defaults to ``False`` so a
    construction that predates the flag still means "an ordinary hit".
    """

    model_config = ConfigDict(frozen=True)

    image_url: str | None
    bbox: dict[str, float] | None
    restricted: bool = False


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
        image_url, raw_bbox, restricted = row
        bbox: dict[str, float] | None = None
        if isinstance(raw_bbox, dict):
            # Defensive shape check rather than a blind cast: the triage JSONB
            # is machine-written, but a malformed bbox must degrade to
            # "no preview", never to a fetcher call with garbage coordinates.
            try:
                bbox = {key: float(raw_bbox[key]) for key in ("x", "y", "w", "h")}
            except (KeyError, TypeError, ValueError):
                bbox = None
        return PreviewTarget(image_url=image_url, bbox=bbox, restricted=bool(restricted))

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
