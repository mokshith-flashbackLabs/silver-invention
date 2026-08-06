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

from psycopg_pool import AsyncConnectionPool

from imageshield.liveness.models import CreateRejection, LivenessSessionRow
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
