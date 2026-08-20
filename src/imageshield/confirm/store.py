"""Persistence for the confirm pipeline — raw SQL, no ORM (CLAUDE.md §2).

Every write method is **one transaction**. The guarded transitions
(``record_duplicate``, ``record_quarantine``, ``record_triage``,
``record_unfetchable``) share one shape: an ``UPDATE ... WHERE
infringement_id = %s AND confirm_state IN ('unconfirmed', 'machine_triaged')``
that only fires on a hit still open to a machine decision. A rowcount of zero
means the hit was already decided by a human (or already duplicate/quarantined)
and the method returns silently — idempotent under SQS at-least-once delivery,
and a human decision can never be clobbered by a re-delivered message.

``record_skipped`` is the one exception, added by a plan amendment: it never
touches ``infringements.confirm_state`` at all. It exists for the case where
the breaker or budget blocks Rekognition (CLAUDE.md §7.6, INVARIANTS #37-41) —
the hit must stay reviewable URL-only rather than wait on a broken dependency,
and leaving ``confirm_state='unconfirmed'`` is what lets the next run's
completion re-enqueue it for a real triage instead of silently dropping it.

The ``review_tasks`` upsert (shared by ``record_triage``, ``record_unfetchable``
and ``record_skipped``) guards its ``DO UPDATE`` with ``WHERE
review_tasks.status = 'pending'``: a decided task is never reopened by a
re-run. ``record_quarantine``'s upsert carries the same guard even though the
outer ``infringements`` guard already makes a decided row unreachable here —
belt and braces, since the two guards are checked independently.

``load_context``'s representative-attestation pick mirrors migration 0016's
``representative`` CTE exactly (band rank, ``provider_score DESC NULLS LAST``,
``provider_id``) so the ``run_id`` handed to the confirm worker is the same
attestation the proxy's ``v_person_hits`` view would show a user.
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from imageshield.confirm.models import ConfirmContext
from imageshield.types import UserRef, parse_user_ref

# actor_type 'service': the caller is the confirm worker acting on a hit, not
# an operator at a console (mirrors subjects/store.py's DISCOVERY_REFUSED_ACTION).
CONFIRM_QUARANTINED_ACTION = "confirm.quarantined"

_LOAD_CONTEXT_SQL = """
    SELECT i.infringement_id, i.user_ref, i.confirm_state, i.image_url, i.page_url,
           rep.last_run_id
    FROM infringements i
    LEFT JOIN LATERAL (
        SELECT a.last_run_id
        FROM attestations a
        WHERE a.infringement_id = i.infringement_id
        ORDER BY
            CASE a.band WHEN 'auto_confirm' THEN 0 WHEN 'review' THEN 1 ELSE 2 END,
            a.provider_score DESC NULLS LAST,
            a.provider_id
        LIMIT 1
    ) rep ON true
    WHERE i.infringement_id = %(infringement_id)s
"""

# The partial index from 0021 (infringements_decided_phash_idx) is built for
# exactly this predicate. Only THIS user's rows, and only a HUMAN decision —
# 'machine_triaged' is deliberately excluded so nothing inherits a phash match
# from a hit nobody has looked at yet.
_DECIDED_PHASHES_SQL = """
    SELECT infringement_id, phash FROM infringements
    WHERE user_ref = %(user_ref)s
      AND phash IS NOT NULL
      AND confirm_state IN ('confirmed', 'rejected')
    ORDER BY infringement_id
"""

_RECORD_DUPLICATE_SQL = """
    UPDATE infringements
    SET confirm_state = 'duplicate', duplicate_of = %(duplicate_of)s, phash = %(phash)s
    WHERE infringement_id = %(infringement_id)s
      AND confirm_state IN ('unconfirmed', 'machine_triaged')
"""

# A duplicate needs no human review — the hit it duplicates already carries
# (or will carry) the decision.
_DELETE_PENDING_REVIEW_TASK_SQL = """
    DELETE FROM review_tasks
    WHERE infringement_id = %(infringement_id)s AND status = 'pending'
"""

# RETURNING user_ref rather than a separate SELECT: record_quarantine,
# record_triage and record_unfetchable all need it for the review_tasks
# insert, and reading it off the same guarded UPDATE keeps the check and the
# read atomic with no separate round trip.
_RECORD_QUARANTINE_INFRINGEMENT_SQL = """
    UPDATE infringements
    SET confirm_state = 'quarantined', phash = %(phash)s,
        moderation_labels = %(moderation_labels)s
    WHERE infringement_id = %(infringement_id)s
      AND confirm_state IN ('unconfirmed', 'machine_triaged')
    RETURNING user_ref
"""

_UPSERT_QUARANTINE_REVIEW_TASK_SQL = """
    INSERT INTO review_tasks (infringement_id, user_ref, severity, triage, status)
    VALUES (%(infringement_id)s, %(user_ref)s, 'ncii_suspected', %(triage)s, 'quarantined')
    ON CONFLICT (infringement_id) DO UPDATE
      SET severity = EXCLUDED.severity, triage = EXCLUDED.triage, status = EXCLUDED.status
    WHERE review_tasks.status = 'pending'
"""

_QUARANTINE_AUDIT_SQL = """
    INSERT INTO audit_log (actor_type, action, subject_ref, resource_id, metadata)
    VALUES ('service', %(action)s, %(subject_ref)s, %(resource_id)s, %(metadata)s)
"""

_RECORD_TRIAGE_INFRINGEMENT_SQL = """
    UPDATE infringements
    SET confirm_state = 'machine_triaged', severity = %(severity)s, phash = %(phash)s,
        face_match_score = %(face_match_score)s, moderation_labels = %(moderation_labels)s
    WHERE infringement_id = %(infringement_id)s
      AND confirm_state IN ('unconfirmed', 'machine_triaged')
    RETURNING user_ref
"""

_RECORD_UNFETCHABLE_INFRINGEMENT_SQL = """
    UPDATE infringements
    SET confirm_state = 'machine_triaged', severity = 'unassessed'
    WHERE infringement_id = %(infringement_id)s
      AND confirm_state IN ('unconfirmed', 'machine_triaged')
    RETURNING user_ref
"""

# Shared by record_triage, record_unfetchable and record_skipped. The WHERE
# guard is the load-bearing part: a task a human has already decided is never
# reopened by a machine re-run.
_UPSERT_REVIEW_TASK_SQL = """
    INSERT INTO review_tasks (infringement_id, user_ref, severity, triage)
    VALUES (%(infringement_id)s, %(user_ref)s, %(severity)s, %(triage)s)
    ON CONFLICT (infringement_id) DO UPDATE
      SET severity = EXCLUDED.severity, triage = EXCLUDED.triage
    WHERE review_tasks.status = 'pending'
"""

# record_skipped never touches confirm_state, so there is no guarded UPDATE to
# RETURNING off of — just enough of a read to know the hit exists (and to get
# its user_ref for the review_tasks FK) without asserting anything about where
# it is in the confirm lifecycle.
_SELECT_INFRINGEMENT_USER_SQL = """
    SELECT user_ref FROM infringements WHERE infringement_id = %(infringement_id)s
"""


class ConfirmStore(Protocol):
    async def load_context(self, infringement_id: UUID) -> ConfirmContext | None: ...

    async def decided_phashes(self, user_ref: UserRef) -> tuple[tuple[UUID, int], ...]: ...

    async def record_duplicate(
        self, infringement_id: UUID, *, duplicate_of: UUID, phash: int
    ) -> None: ...

    async def record_quarantine(
        self,
        infringement_id: UUID,
        *,
        phash: int | None,
        moderation_labels: list[dict[str, Any]],
        min_age_low: float | None,
    ) -> None: ...

    async def record_triage(
        self,
        infringement_id: UUID,
        *,
        severity: str,
        phash: int | None,
        face_match_score: float | None,
        moderation_labels: list[dict[str, Any]] | None,
        triage: dict[str, Any],
    ) -> None: ...

    async def record_unfetchable(self, infringement_id: UUID, *, detail: str) -> None: ...

    async def record_skipped(
        self, infringement_id: UUID, *, reason: str, detail: str
    ) -> None: ...


class PostgresConfirmStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def load_context(self, infringement_id: UUID) -> ConfirmContext | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_LOAD_CONTEXT_SQL, {"infringement_id": infringement_id})
            row = await cur.fetchone()
        if row is None:
            return None
        return ConfirmContext(
            infringement_id=row[0],
            user_ref=parse_user_ref(row[1]),
            confirm_state=row[2],
            image_url=row[3],
            page_url=row[4],
            run_id=row[5],
        )

    async def decided_phashes(self, user_ref: UserRef) -> tuple[tuple[UUID, int], ...]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_DECIDED_PHASHES_SQL, {"user_ref": user_ref})
            rows = await cur.fetchall()
        return tuple((row[0], row[1]) for row in rows)

    async def record_duplicate(
        self, infringement_id: UUID, *, duplicate_of: UUID, phash: int
    ) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                _RECORD_DUPLICATE_SQL,
                {
                    "infringement_id": infringement_id,
                    "duplicate_of": duplicate_of,
                    "phash": phash,
                },
            )
            if cur.rowcount == 0:
                return
            await conn.execute(
                _DELETE_PENDING_REVIEW_TASK_SQL, {"infringement_id": infringement_id}
            )

    async def record_quarantine(
        self,
        infringement_id: UUID,
        *,
        phash: int | None,
        moderation_labels: list[dict[str, Any]],
        min_age_low: float | None,
    ) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                _RECORD_QUARANTINE_INFRINGEMENT_SQL,
                {
                    "infringement_id": infringement_id,
                    "phash": phash,
                    "moderation_labels": Jsonb(moderation_labels),
                },
            )
            row = await cur.fetchone()
            if row is None:
                return
            user_ref = row[0]
            triage = Jsonb({"quarantine": True, "min_age_low": min_age_low})
            await conn.execute(
                _UPSERT_QUARANTINE_REVIEW_TASK_SQL,
                {
                    "infringement_id": infringement_id,
                    "user_ref": user_ref,
                    "triage": triage,
                },
            )
            await conn.execute(
                _QUARANTINE_AUDIT_SQL,
                {
                    "action": CONFIRM_QUARANTINED_ACTION,
                    "subject_ref": user_ref,
                    "resource_id": infringement_id,
                    "metadata": Jsonb({"min_age_low": min_age_low}),
                },
            )

    async def record_triage(
        self,
        infringement_id: UUID,
        *,
        severity: str,
        phash: int | None,
        face_match_score: float | None,
        moderation_labels: list[dict[str, Any]] | None,
        triage: dict[str, Any],
    ) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                _RECORD_TRIAGE_INFRINGEMENT_SQL,
                {
                    "infringement_id": infringement_id,
                    "severity": severity,
                    "phash": phash,
                    "face_match_score": face_match_score,
                    "moderation_labels": (
                        Jsonb(moderation_labels) if moderation_labels is not None else None
                    ),
                },
            )
            row = await cur.fetchone()
            if row is None:
                return
            user_ref = row[0]
            await conn.execute(
                _UPSERT_REVIEW_TASK_SQL,
                {
                    "infringement_id": infringement_id,
                    "user_ref": user_ref,
                    "severity": severity,
                    "triage": Jsonb(triage),
                },
            )

    async def record_unfetchable(self, infringement_id: UUID, *, detail: str) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                _RECORD_UNFETCHABLE_INFRINGEMENT_SQL, {"infringement_id": infringement_id}
            )
            row = await cur.fetchone()
            if row is None:
                return
            user_ref = row[0]
            await conn.execute(
                _UPSERT_REVIEW_TASK_SQL,
                {
                    "infringement_id": infringement_id,
                    "user_ref": user_ref,
                    "severity": "unassessed",
                    "triage": Jsonb({"unfetchable": detail}),
                },
            )

    async def record_skipped(
        self, infringement_id: UUID, *, reason: str, detail: str
    ) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                _SELECT_INFRINGEMENT_USER_SQL, {"infringement_id": infringement_id}
            )
            row = await cur.fetchone()
            if row is None:
                return
            user_ref = row[0]
            await conn.execute(
                _UPSERT_REVIEW_TASK_SQL,
                {
                    "infringement_id": infringement_id,
                    "user_ref": user_ref,
                    "severity": "unassessed",
                    "triage": Jsonb({"skipped": reason, "detail": detail}),
                },
            )
