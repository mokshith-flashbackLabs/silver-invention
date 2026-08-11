"""Persistence for ``liveness_sessions`` — raw SQL, no ORM (CLAUDE.md §2).

:class:`LivenessStore` is the Protocol the routes depend on;
:class:`PostgresLivenessStore` is the real implementation over the app's
async pool. Route tests use an in-memory fake of the Protocol
(tests/test_liveness_routes.py); the real SQL is proven against Postgres in
tests/test_liveness_store.py.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from imageshield.enrolment.models import QUALITY_REJECTED_REASON, EnrolmentRow, NewEnrolment
from imageshield.enrolment.store import to_enrolment_row
from imageshield.liveness.models import CreateRejection, LivenessSessionRow
from imageshield.subjects.models import Eligibility
from imageshield.subjects.store import upsert_subject
from imageshield.types import SessionId, UserRef

_COLUMNS = (
    "session_id, user_ref, provider_session_id, status, confidence, failure_reason,"
    " attempt_number, reference_image_uri, audit_image_uris, created_at, completed_at,"
    " expires_at, consumed_at, result_idempotency_key"
)

_SELECT_SQL = f"SELECT {_COLUMNS} FROM liveness_sessions WHERE session_id = %(session_id)s"

# One round-trip for both create-time guards plus the all-time attempt count.
# The 24h window is the rate-limit's clock; attempt_number counts all-time.
_CHECK_SQL = """
    SELECT
      EXISTS(
        SELECT 1 FROM liveness_sessions
        WHERE user_ref = %(user_ref)s AND status = 'passed' AND consumed_at IS NULL
      ) AS passed_unconsumed,
      (SELECT count(*) FROM liveness_sessions
        WHERE user_ref = %(user_ref)s
          AND created_at > now() - interval '24 hours') AS attempts_24h,
      (SELECT count(*) FROM liveness_sessions
        WHERE user_ref = %(user_ref)s) AS attempts_all_time
"""

_INSERT_SQL = f"""
    INSERT INTO liveness_sessions
      (user_ref, provider_session_id, status, attempt_number, expires_at)
    VALUES
      (%(user_ref)s, %(provider_session_id)s, 'created', %(attempt_number)s,
       now() + make_interval(secs => %(ttl_seconds)s))
    RETURNING {_COLUMNS}
"""

# completed_at IS NULL: a key must never be rewritten after the outcome is
# recorded — the stored key is what distinguishes an idempotent retry (200
# replay) from a genuine second use of the session (410).
_CLAIM_SQL = """
    UPDATE liveness_sessions
    SET result_idempotency_key = %(idempotency_key)s
    WHERE session_id = %(session_id)s AND completed_at IS NULL
"""

_FINALIZE_SQL = f"""
    UPDATE liveness_sessions
    SET status = %(status)s::liveness_status,
        confidence = %(confidence)s,
        failure_reason = %(failure_reason)s,
        reference_image_uri = %(reference_image_uri)s,
        audit_image_uris = %(audit_image_uris)s,
        completed_at = now()
    WHERE session_id = %(session_id)s
    RETURNING {_COLUMNS}
"""

_MARK_EXPIRED_SQL = f"""
    UPDATE liveness_sessions
    SET status = 'expired'
    WHERE session_id = %(session_id)s AND completed_at IS NULL
    RETURNING {_COLUMNS}
"""

# Consumption: UPDATE must precede the enrolment INSERT in the same
# transaction — migration 0003's composite FK requires the session's CURRENT
# status to be 'consumed' at insert time. completed_at IS NULL guards against
# a concurrent finalizer: the second writer sees zero rows and backs off.
_CONSUME_SQL = f"""
    UPDATE liveness_sessions
    SET status = 'consumed',
        confidence = %(confidence)s,
        failure_reason = %(failure_reason)s,
        reference_image_uri = %(reference_image_uri)s,
        audit_image_uris = %(audit_image_uris)s,
        completed_at = now(),
        consumed_at = now()
    WHERE session_id = %(session_id)s AND completed_at IS NULL
    RETURNING {_COLUMNS}
"""

_ENROLMENT_COLUMNS = (
    "enrolment_id, session_id, user_ref, collection_id, external_face_id,"
    " quality_score, model_id, source_object_uri, status, created_at, deleted_at,"
    " consent_ref, consent_document_sha256, consent_signed_at"
)

_INSERT_ENROLMENT_SQL = f"""
    INSERT INTO enrolments
      (session_id, session_status, user_ref, collection_id, external_face_id,
       quality_score, model_id, source_object_uri,
       consent_ref, consent_document_sha256, consent_signed_at)
    VALUES
      (%(session_id)s, 'consumed', %(user_ref)s, %(collection_id)s,
       %(external_face_id)s, %(quality_score)s, %(model_id)s,
       %(source_object_uri)s, %(consent_ref)s, %(consent_document_sha256)s,
       %(consent_signed_at)s)
    RETURNING {_ENROLMENT_COLUMNS}
"""

# The proxy reconciles its own consent records against what we actually bound
# an enrolment to. Read off enrolments, not liveness_sessions: consent belongs
# to the enrolment, and a session that never enrolled has none.
_SELECT_CONSENT_REF_SQL = """
    SELECT consent_ref FROM enrolments WHERE session_id = %(session_id)s
"""

_NOTIFY_SQL = "SELECT pg_notify('enrolment_complete', %(session_id)s::text)"


class LivenessStore(Protocol):
    async def check_create_allowed(
        self, user_ref: UserRef, *, max_attempts_24h: int
    ) -> CreateRejection | None: ...

    async def create_session(
        self,
        *,
        user_ref: UserRef,
        provider_session_id: str,
        ttl_seconds: int,
        max_attempts_24h: int,
    ) -> LivenessSessionRow | CreateRejection: ...

    async def get_session(self, session_id: SessionId) -> LivenessSessionRow | None: ...

    async def claim_result(self, session_id: SessionId, idempotency_key: str) -> None: ...

    async def finalize_result(
        self,
        session_id: SessionId,
        *,
        status: str,
        confidence: float | None,
        failure_reason: str | None,
        reference_image_uri: str | None,
        audit_image_uris: tuple[str, ...] | None,
    ) -> LivenessSessionRow: ...

    async def mark_expired(self, session_id: SessionId) -> LivenessSessionRow: ...

    async def finalize_enrolled(
        self,
        session_id: SessionId,
        *,
        confidence: float | None,
        reference_image_uri: str,
        audit_image_uris: tuple[str, ...],
        enrolment: NewEnrolment,
        eligibility: Eligibility,
    ) -> tuple[LivenessSessionRow, EnrolmentRow] | None: ...

    async def finalize_quality_rejected(
        self,
        session_id: SessionId,
        *,
        confidence: float | None,
        reference_image_uri: str,
        audit_image_uris: tuple[str, ...],
    ) -> LivenessSessionRow | None: ...

    async def get_enrolment_consent_ref(self, session_id: SessionId) -> UUID | None: ...


def _to_row(record: tuple[Any, ...]) -> LivenessSessionRow:
    (
        session_id,
        user_ref,
        provider_session_id,
        status,
        confidence,
        failure_reason,
        attempt_number,
        reference_image_uri,
        audit_image_uris,
        created_at,
        completed_at,
        expires_at,
        consumed_at,
        result_idempotency_key,
    ) = record
    return LivenessSessionRow(
        session_id=session_id,
        user_ref=user_ref,
        provider_session_id=provider_session_id,
        status=str(status),
        confidence=float(confidence) if isinstance(confidence, Decimal) else confidence,
        failure_reason=failure_reason,
        attempt_number=attempt_number,
        reference_image_uri=reference_image_uri,
        audit_image_uris=tuple(audit_image_uris) if audit_image_uris is not None else None,
        created_at=created_at,
        completed_at=completed_at,
        expires_at=expires_at,
        consumed_at=consumed_at,
        result_idempotency_key=result_idempotency_key,
    )


class PostgresLivenessStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def check_create_allowed(
        self, user_ref: UserRef, *, max_attempts_24h: int
    ) -> CreateRejection | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_CHECK_SQL, {"user_ref": user_ref})
            record = await cur.fetchone()
        assert record is not None
        return self._rejection(record, max_attempts_24h)

    @staticmethod
    def _rejection(record: tuple[Any, ...], max_attempts_24h: int) -> CreateRejection | None:
        passed_unconsumed, attempts_24h, _ = record
        if passed_unconsumed:
            return CreateRejection.PASSED_UNCONSUMED
        if attempts_24h >= max_attempts_24h:
            return CreateRejection.ATTEMPTS_EXCEEDED
        return None

    async def create_session(
        self,
        *,
        user_ref: UserRef,
        provider_session_id: str,
        ttl_seconds: int,
        max_attempts_24h: int,
    ) -> LivenessSessionRow | CreateRejection:
        async with self._pool.connection() as conn, conn.transaction():
            # Serialise creates per user_ref so two concurrent requests can't
            # both pass the guards. Advisory, transaction-scoped, DB-only —
            # the provider call already happened, so nothing network-bound
            # runs while this lock is held.
            await conn.execute(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended('liveness_create:' || %(user_ref)s::text, 0))",
                {"user_ref": user_ref},
            )
            cur = await conn.execute(_CHECK_SQL, {"user_ref": user_ref})
            record = await cur.fetchone()
            assert record is not None
            rejection = self._rejection(record, max_attempts_24h)
            if rejection is not None:
                return rejection
            attempts_all_time = int(record[2])
            cur = await conn.execute(
                _INSERT_SQL,
                {
                    "user_ref": user_ref,
                    "provider_session_id": provider_session_id,
                    "attempt_number": attempts_all_time + 1,
                    "ttl_seconds": ttl_seconds,
                },
            )
            inserted = await cur.fetchone()
        assert inserted is not None
        return _to_row(inserted)

    async def get_session(self, session_id: SessionId) -> LivenessSessionRow | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_SELECT_SQL, {"session_id": session_id})
            record = await cur.fetchone()
        return _to_row(record) if record is not None else None

    async def claim_result(self, session_id: SessionId, idempotency_key: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                _CLAIM_SQL, {"session_id": session_id, "idempotency_key": idempotency_key}
            )

    async def finalize_result(
        self,
        session_id: SessionId,
        *,
        status: str,
        confidence: float | None,
        failure_reason: str | None,
        reference_image_uri: str | None,
        audit_image_uris: tuple[str, ...] | None,
    ) -> LivenessSessionRow:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                _FINALIZE_SQL,
                {
                    "session_id": session_id,
                    "status": status,
                    "confidence": confidence,
                    "failure_reason": failure_reason,
                    "reference_image_uri": reference_image_uri,
                    "audit_image_uris": (
                        list(audit_image_uris) if audit_image_uris is not None else None
                    ),
                },
            )
            record = await cur.fetchone()
        if record is None:
            raise LookupError(f"liveness session {session_id} does not exist")
        return _to_row(record)

    async def mark_expired(self, session_id: SessionId) -> LivenessSessionRow:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_MARK_EXPIRED_SQL, {"session_id": session_id})
            record = await cur.fetchone()
        if record is not None:
            return _to_row(record)
        # Already completed (the guard skipped the UPDATE) — return as-is.
        existing = await self.get_session(session_id)
        if existing is None:
            raise LookupError(f"liveness session {session_id} does not exist")
        return existing

    async def finalize_enrolled(
        self,
        session_id: SessionId,
        *,
        confidence: float | None,
        reference_image_uri: str,
        audit_image_uris: tuple[str, ...],
        enrolment: NewEnrolment,
        eligibility: Eligibility,
    ) -> tuple[LivenessSessionRow, EnrolmentRow] | None:
        """Session consumption + subject row + enrolment row + NOTIFY, all in
        ONE transaction (step 8).

        The subject upsert sits deliberately BEFORE the enrolments insert:
        enrolment is what creates the subject, and migration 0008 notes the FK
        from ``enrolments.user_ref`` to ``subjects`` as follow-up. Writing in
        this order is what makes adding that constraint a one-line migration
        rather than a reordering exercise.

        Neither row can exist without the other. Killing the process between
        them leaves nothing behind: no enrolment claiming a subject that has no
        eligibility flag, and no subject row for someone who never enrolled.
        """
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                _CONSUME_SQL,
                {
                    "session_id": session_id,
                    "confidence": confidence,
                    "failure_reason": None,
                    "reference_image_uri": reference_image_uri,
                    "audit_image_uris": list(audit_image_uris),
                },
            )
            session_record = await cur.fetchone()
            if session_record is None:
                return None  # concurrent finalizer won; caller compensates
            await upsert_subject(conn, enrolment.user_ref, eligibility)
            cur = await conn.execute(
                _INSERT_ENROLMENT_SQL,
                {
                    "session_id": session_id,
                    "user_ref": enrolment.user_ref,
                    "collection_id": enrolment.collection_id,
                    "external_face_id": enrolment.external_face_id,
                    "quality_score": enrolment.quality_score,
                    "model_id": enrolment.model_id,
                    "source_object_uri": enrolment.source_object_uri,
                    "consent_ref": enrolment.consent_ref,
                    "consent_document_sha256": enrolment.consent_document_sha256,
                    "consent_signed_at": enrolment.consent_signed_at,
                },
            )
            enrolment_record = await cur.fetchone()
            assert enrolment_record is not None
            await conn.execute(_NOTIFY_SQL, {"session_id": session_id})
        return _to_row(session_record), to_enrolment_row(enrolment_record)

    async def finalize_quality_rejected(
        self,
        session_id: SessionId,
        *,
        confidence: float | None,
        reference_image_uri: str,
        audit_image_uris: tuple[str, ...],
    ) -> LivenessSessionRow | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                _CONSUME_SQL,
                {
                    "session_id": session_id,
                    "confidence": confidence,
                    "failure_reason": QUALITY_REJECTED_REASON,
                    "reference_image_uri": reference_image_uri,
                    "audit_image_uris": list(audit_image_uris),
                },
            )
            record = await cur.fetchone()
        return _to_row(record) if record is not None else None

    async def get_enrolment_consent_ref(self, session_id: SessionId) -> UUID | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_SELECT_CONSENT_REF_SQL, {"session_id": session_id})
            record = await cur.fetchone()
        consent_ref: UUID | None = record[0] if record is not None else None
        return consent_ref
