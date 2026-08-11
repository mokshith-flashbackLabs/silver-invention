"""Persistence for the recheck loop — raw SQL, no ORM (CLAUDE.md §2).

Two timestamps with two different meanings, and the distinction is the whole
reason migration 0013 exists:

- ``last_checked_at`` — the last time we **learned something**. Only a definite
  verdict writes it. It is exposed on ``GET /v1/search/infringements`` and the
  proxy uses it to decide whether it may tell a user "this came down".
- ``last_attempted_at`` — the last time we **tried**. Every probe writes it,
  including the ones that failed. This is what the due-queue orders by, so a
  permanently unreachable host cannot pin the front of every batch and starve
  the rest.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from imageshield.recheck.models import DueInfringement
from imageshield.recheck.policy import Verdict

# Due = still live, and not definitively checked within the interval. Ordered by
# ATTEMPT time so a failing row goes to the back rather than blocking the queue.
#
# Joined to content_urls for source_domain: the pacer needs it per row and the
# allowlist is built from the same column, so one query answers both.
#
# url_alive = true in the WHERE, so a dead URL is never probed again.
_DUE_SQL = """
    SELECT i.infringement_id, i.page_url, c.source_domain
    FROM infringements i
    JOIN content_urls c ON c.url_hash = i.url_hash
    WHERE i.url_alive = true
      AND (i.last_checked_at IS NULL
           OR i.last_checked_at < now() - make_interval(days => %(interval_days)s))
    ORDER BY i.last_attempted_at ASC NULLS FIRST, i.infringement_id
    LIMIT %(limit)s
"""

# Every domain we have ever recorded a URL for. Sourced from our own data rather
# than a hand-maintained list, so it cannot drift out of agreement with what the
# providers actually returned.
_ALLOWED_DOMAINS_SQL = "SELECT DISTINCT source_domain FROM content_urls"

# We tried and learned nothing (timeout, 5xx, refused by the egress guards).
# ONLY the attempt clock moves — url_alive and last_checked_at are untouched.
_ATTEMPTED_SQL = """
    UPDATE infringements SET last_attempted_at = now()
    WHERE infringement_id = %(infringement_id)s
"""

# Alive: both clocks. url_alive is already true (it is in the due query's
# WHERE), so writing it again would be a no-op that muddies the intent.
_MARK_ALIVE_SQL = """
    UPDATE infringements
    SET last_checked_at = now(), last_attempted_at = now()
    WHERE infringement_id = %(infringement_id)s
"""

# Dead — a 404 or a 410, and nothing else. NEVER a DELETE: a dead URL is still
# evidence, and the user has already been told about it. "No longer online" is
# the good news; erasing the row would delete the record of what happened.
_MARK_DEAD_SQL = """
    UPDATE infringements
    SET url_alive = false, last_checked_at = now(), last_attempted_at = now()
    WHERE infringement_id = %(infringement_id)s
"""

_SQL_BY_VERDICT: dict[str, str] = {
    "alive": _MARK_ALIVE_SQL,
    "dead": _MARK_DEAD_SQL,
    "unchanged": _ATTEMPTED_SQL,
}


class RecheckStore(Protocol):
    async def due_batch(
        self, *, interval_days: int, limit: int
    ) -> tuple[DueInfringement, ...]: ...

    async def allowed_domains(self) -> frozenset[str]: ...

    async def record_verdict(self, infringement_id: UUID, verdict: Verdict) -> None: ...


class PostgresRecheckStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def due_batch(
        self, *, interval_days: int, limit: int
    ) -> tuple[DueInfringement, ...]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                _DUE_SQL, {"interval_days": interval_days, "limit": limit}
            )
            rows = await cur.fetchall()
        return tuple(
            DueInfringement(
                infringement_id=row[0], page_url=row[1], source_domain=row[2]
            )
            for row in rows
        )

    async def allowed_domains(self) -> frozenset[str]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_ALLOWED_DOMAINS_SQL)
            rows = await cur.fetchall()
        return frozenset(row[0] for row in rows if row[0])

    async def record_verdict(self, infringement_id: UUID, verdict: Verdict) -> None:
        """One statement per verdict. ``unchanged`` moves the attempt clock and
        nothing else — see the module docstring for why that is not the same as
        doing nothing."""
        async with self._pool.connection() as conn:
            await conn.execute(
                _SQL_BY_VERDICT[verdict], {"infringement_id": infringement_id}
            )
