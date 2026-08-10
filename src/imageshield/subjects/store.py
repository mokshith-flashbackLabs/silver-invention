"""Persistence for ``subjects`` — raw SQL, no ORM (CLAUDE.md §2).

Two entry points, deliberately shaped differently:

- :func:`upsert_subject` takes a **connection**, not a pool, and never
  commits — the same contract as :func:`imageshield.outbox.enqueue`. Enrolment
  has to write the subject row and the enrolment row in one transaction, so
  this has to compose into somebody else's.
- :class:`PostgresSubjectStore` is the pool-backed store the HTTP surface
  depends on, for the reads and the refusal audit row.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from imageshield.subjects.models import Eligibility, SubjectRow
from imageshield.types import UserRef, parse_user_ref

# ON CONFLICT DO UPDATE, not DO NOTHING: a subject re-enrolling after the proxy
# corrects a DOB must have the correction applied. `updated_at` is what tells
# an auditor the flag was re-asserted rather than merely inherited.
#
# The WHERE clause suppresses no-op writes so `updated_at` means "the assertion
# changed", not "the user enrolled again" — otherwise every re-enrolment looks
# like an eligibility change in the audit trail.
_UPSERT_SQL = """
    INSERT INTO subjects (user_ref, discovery_eligible, eligibility_reason)
    VALUES (%(user_ref)s, %(discovery_eligible)s, %(eligibility_reason)s)
    ON CONFLICT (user_ref) DO UPDATE
      SET discovery_eligible = EXCLUDED.discovery_eligible,
          eligibility_reason = EXCLUDED.eligibility_reason,
          updated_at = now()
      WHERE subjects.discovery_eligible IS DISTINCT FROM EXCLUDED.discovery_eligible
"""

_SELECT_SQL = """
    SELECT user_ref, discovery_eligible, eligibility_reason, created_at, updated_at
    FROM subjects WHERE user_ref = %(user_ref)s
"""

# actor_type 'service': the caller is the proxy acting for a user, not an
# operator at a console. 'operator' is reserved for the calibration CLI, which
# is the only other audit_log writer.
_AUDIT_SQL = """
    INSERT INTO audit_log (actor_type, action, subject_ref, resource_id, metadata)
    VALUES ('service', %(action)s, %(subject_ref)s, %(resource_id)s, %(metadata)s)
"""

DISCOVERY_REFUSED_ACTION = "discovery.refused"


async def upsert_subject(
    conn: AsyncConnection[Any], user_ref: UserRef, eligibility: Eligibility
) -> None:
    """Write the subject row on an existing connection. Does NOT commit.

    Callers inside a transaction get the atomicity the step-8 brief requires:
    the subject row and the enrolment row commit together or neither does.
    """
    await conn.execute(
        _UPSERT_SQL,
        {
            "user_ref": user_ref,
            "discovery_eligible": eligibility.discovery_eligible,
            "eligibility_reason": eligibility.eligibility_reason,
        },
    )


class SubjectStore(Protocol):
    async def get_subject(self, user_ref: UserRef) -> SubjectRow | None: ...

    async def upsert_subject(
        self, user_ref: UserRef, eligibility: Eligibility
    ) -> None: ...

    async def record_discovery_refusal(
        self, user_ref: UserRef, *, outcome: str, metadata: Mapping[str, Any]
    ) -> None: ...


class PostgresSubjectStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def get_subject(self, user_ref: UserRef) -> SubjectRow | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_SELECT_SQL, {"user_ref": user_ref})
            row = await cur.fetchone()
        if row is None:
            return None
        return SubjectRow(
            user_ref=parse_user_ref(row[0]),
            discovery_eligible=row[1],
            eligibility_reason=row[2],
            created_at=row[3],
            updated_at=row[4],
        )

    async def upsert_subject(
        self, user_ref: UserRef, eligibility: Eligibility
    ) -> None:
        async with self._pool.connection() as conn:
            await upsert_subject(conn, user_ref, eligibility)

    async def record_discovery_refusal(
        self, user_ref: UserRef, *, outcome: str, metadata: Mapping[str, Any]
    ) -> None:
        """The ONLY row a refused discovery request writes.

        A refusal must be indistinguishable in the data from a request that was
        never made, except here. In particular no ``search_runs`` row: a run
        with zero results reads as "we looked and found nothing", which for an
        ineligible subject is a false reassurance about a safety product.
        """
        async with self._pool.connection() as conn:
            await conn.execute(
                _AUDIT_SQL,
                {
                    "action": DISCOVERY_REFUSED_ACTION,
                    "subject_ref": user_ref,
                    "resource_id": None,
                    "metadata": Jsonb({"outcome": outcome, **dict(metadata)}),
                },
            )
