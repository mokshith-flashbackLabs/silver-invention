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

**There are two viewers and two ceilings (2026-09-14).** ``operator_target`` /
``operator_renders_last_24h`` / ``record_operator_render`` are the reviewer's
half of the same surface, added so a false-positive rate can be measured by a
human who can actually see the hit (INVARIANTS #19 as amended). They render
through the identical code path — ``target``'s image address and bbox, the
fetcher's same crop call, the same whole-frame blur with the face sharpened
only on reveal. There is no operator render mode, and #23 is unchanged
because of it.

What differs is only authorisation and accounting: an operator is authorised
by the admin token rather than by ownership, so ``operator_target`` carries no
``user_ref`` predicate; and the two ceilings are counted separately, so an
operator reviewing a hit never spends the subject's own daily allowance.
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

# `actor_type = 'subject'` (2026-09-14): an OPERATOR render writes
# subject_ref = the hit's owner too, so without this predicate a reviewer
# working through somebody's hits would spend that person's own daily
# allowance and lock them out of their report. 0024's partial index still
# serves the query -- the extra predicate is a filter on the rows it returns,
# not a different access path.
_COUNT_RENDERS_SQL = """
    SELECT count(*) FROM audit_log
    WHERE action = 'preview.rendered'
      AND actor_type = 'subject'
      AND subject_ref = %(user_ref)s
      AND occurred_at > now() - interval '24 hours'
"""

# The operator half. No `user_ref` predicate: an operator is authorised by the
# admin service token, not by owning the hit -- ownership is the SUBJECT's
# gate and copying it here would mean no operator could review anything.
# `quarantined` is excluded and `duplicate` is not: a quarantined hit is
# CSAM-suspected and is rendered to nobody ever (escalation is the manual
# legal process in docs/OPERATIONS.md), whereas a duplicate is an ordinary hit
# collapsed onto another and is exactly the kind of thing a reviewer measuring
# the matcher needs to look at.
#
# `restricted` is deliberately NOT selected. It is the SUBJECT's refusal --
# "we established this is your abuse imagery, so we will not show it to you
# and will not ask" -- and it is the one hit a false-positive review most
# needs to check, because auto-confirm is the one place no human ever looked.
_OPERATOR_TARGET_SQL = """
    SELECT coalesce(i.preview_image_url, i.image_url),
           rt.triage -> 'best_face_bbox',
           i.user_ref
    FROM infringements i
    LEFT JOIN review_tasks rt ON rt.infringement_id = i.infringement_id
    WHERE i.infringement_id = %(infringement_id)s
      AND i.confirm_state <> 'quarantined'
"""

# Per-OPERATOR, off 0033's audit_operator_preview_renders_idx. The operator's
# name is in the metadata rather than in a column because `subject_ref` is
# already spoken for -- it names whose hit was rendered, which is the filter
# that has to keep working for both viewers.
_COUNT_OPERATOR_RENDERS_SQL = """
    SELECT count(*) FROM audit_log
    WHERE action = 'preview.rendered'
      AND actor_type = 'operator'
      AND metadata ->> 'operator' = %(operator)s
      AND occurred_at > now() - interval '24 hours'
"""

# ONE action name for both viewers, distinguished by actor_type. "Every render
# of this hit, by anybody" then stays a single filter on
# (action, resource_id) -- which is what an audit of a specific person's
# imagery has to be able to ask. subject_ref is the hit's OWNER even here, so
# that filter works by person too; the operator's name rides in the metadata.
_RECORD_OPERATOR_RENDER_SQL = """
    INSERT INTO audit_log (actor_type, action, subject_ref, resource_id, metadata)
    VALUES ('operator', %(action)s, %(user_ref)s, %(infringement_id)s, %(metadata)s)
"""

_RECORD_RENDER_SQL = """
    INSERT INTO audit_log (actor_type, action, subject_ref, resource_id, metadata)
    VALUES ('subject', %(action)s, %(user_ref)s, %(infringement_id)s, %(metadata)s)
"""


def _bbox(raw: object) -> dict[str, float] | None:
    """The triage JSONB's ``best_face_bbox`` as four floats, or ``None``.

    A defensive shape check rather than a blind cast: the JSONB is
    machine-written, but a malformed bbox must degrade to "no preview", never
    to a fetcher call with garbage coordinates. Shared by the subject and
    operator paths so the two cannot disagree about what is renderable.
    """
    if not isinstance(raw, dict):
        return None
    try:
        return {key: float(raw[key]) for key in ("x", "y", "w", "h")}
    except (KeyError, TypeError, ValueError):
        return None


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


class OperatorPreviewTarget(BaseModel):
    """What one hit offers the OPERATOR preview. Identical to
    :class:`PreviewTarget` minus ``restricted`` (which is the subject's
    refusal, not a reviewer's) and plus ``user_ref`` — the audit row names
    whose hit was rendered, so the query has to return it."""

    model_config = ConfigDict(frozen=True)

    image_url: str | None
    bbox: dict[str, float] | None
    user_ref: UserRef


class PreviewStore(Protocol):
    async def target(
        self, infringement_id: UUID, user_ref: UserRef
    ) -> PreviewTarget | None: ...

    async def renders_last_24h(self, user_ref: UserRef) -> int: ...

    async def record_render(
        self, user_ref: UserRef, infringement_id: UUID, *, reveal: bool
    ) -> None: ...

    async def operator_target(
        self, infringement_id: UUID
    ) -> OperatorPreviewTarget | None: ...

    async def operator_renders_last_24h(self, operator: str) -> int: ...

    async def record_operator_render(
        self,
        operator: str,
        user_ref: UserRef,
        infringement_id: UUID,
        *,
        reveal: bool,
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
        return PreviewTarget(
            image_url=image_url, bbox=_bbox(raw_bbox), restricted=bool(restricted)
        )

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

    async def operator_target(
        self, infringement_id: UUID
    ) -> OperatorPreviewTarget | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                _OPERATOR_TARGET_SQL, {"infringement_id": infringement_id}
            )
            row = await cur.fetchone()
        if row is None:
            return None
        image_url, raw_bbox, user_ref = row
        return OperatorPreviewTarget(
            image_url=image_url, bbox=_bbox(raw_bbox), user_ref=UserRef(user_ref)
        )

    async def operator_renders_last_24h(self, operator: str) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                _COUNT_OPERATOR_RENDERS_SQL, {"operator": operator}
            )
            row = await cur.fetchone()
        assert row is not None
        count: int = row[0]
        return count

    async def record_operator_render(
        self,
        operator: str,
        user_ref: UserRef,
        infringement_id: UUID,
        *,
        reveal: bool,
    ) -> None:
        metadata: dict[str, Any] = {"reveal": reveal, "operator": operator}
        async with self._pool.connection() as conn:
            await conn.execute(
                _RECORD_OPERATOR_RENDER_SQL,
                {
                    "action": PREVIEW_RENDERED_ACTION,
                    "user_ref": user_ref,
                    "infringement_id": infringement_id,
                    "metadata": Jsonb(metadata),
                },
            )


__all__ = [
    "PREVIEW_RENDERED_ACTION",
    "OperatorPreviewTarget",
    "PostgresPreviewStore",
    "PreviewStore",
    "PreviewTarget",
]
