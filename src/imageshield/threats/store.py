"""Threat events — operator CRUD, the domain/global matcher, and the audit
trail (design doc §6, migration 0022, Task 14 brief).

A threat event is a console-authored fact ("this domain just had a leak",
"this platform had an incident") that penalises the ``threat`` component of
every matched person's protection score — see ``score/store.py``'s
``_THREATS_SQL``, which reads only ``status = 'active'`` rows here.

That single detail is what makes retraction reversible without a manual
compensating entry: ``retract_event`` never touches ``score_events`` or
``protection_scores`` directly. It flips ``threat_events.status`` to
``'retracted'`` and returns the matched ``user_ref``s so the caller (the
admin route) can run ``ScoreStore.recompute`` for each — the engine reads
``status = 'active'`` on its next pass, the threat penalty vanishes from the
computed components, and the diff-and-journal machinery in
``score/store.py`` writes the opposite delta on its own. The reversal is a
property of *where the engine reads its state*, not a second code path that
has to agree with the first.

``create_event`` is one transaction: insert the event row, materialise
matches (domain-based, plus a second global insert when ``is_global``), and
write one audit row. All three succeed together or none do — a threat event
that exists with no matches, or matches with no audit trail, is exactly the
kind of half-applied state ``providers/store.py``'s ``_write_with_audit``
exists to prevent, and this module follows the same shape by hand because
here the "write" is itself two statements (domain match + global match) that
must share one audit row summarising both.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

import structlog
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from imageshield.types import UserRef, parse_user_ref

log = structlog.get_logger("imageshield.threats")

THREAT_CREATED_ACTION = "threat_event.created"
THREAT_RETRACTED_ACTION = "threat_event.retracted"

_INSERT_EVENT_SQL = """
    INSERT INTO threat_events
        (kind, title, body, severity, domains, is_global, penalty,
         expires_at, decay_days, status, created_by)
    VALUES
        (%(kind)s, %(title)s, %(body)s, %(severity)s, %(domains)s, %(is_global)s,
         %(penalty)s, %(expires_at)s, %(decay_days)s, 'active', %(operator)s)
    RETURNING event_id
"""

# DISTINCT ON (i.user_ref): one match row per person even when several of
# their infringements sit on the same threat domain, or on more than one of
# the event's domains. url_alive only — a dead URL is not a live relevance
# signal to penalise someone over. Deliberately no confirm_state filter: a
# threat event is about domain relevance, not the adjudication verdict on any
# one hit.
_MATCH_DOMAINS_SQL = """
    INSERT INTO threat_event_matches (event_id, user_ref, matched_via, penalty_applied)
    SELECT DISTINCT ON (i.user_ref) %(event_id)s, i.user_ref, c.source_domain, %(penalty)s
    FROM infringements i
    JOIN content_urls c ON c.url_hash = i.url_hash
    WHERE c.source_domain = ANY(%(domains)s) AND i.url_alive
    ORDER BY i.user_ref, c.source_domain
    ON CONFLICT DO NOTHING
    RETURNING user_ref
"""

# Runs AFTER the domain match, when present. ON CONFLICT DO NOTHING means a
# person already matched by domain keeps their domain attribution rather than
# being overwritten with 'global' — this insert only reaches people the
# domain pass did not.
_MATCH_GLOBAL_SQL = """
    INSERT INTO threat_event_matches (event_id, user_ref, matched_via, penalty_applied)
    SELECT %(event_id)s, user_ref, 'global', %(penalty)s FROM subjects
    ON CONFLICT DO NOTHING
    RETURNING user_ref
"""

_AUDIT_SQL = """
    INSERT INTO audit_log (actor_type, action, resource_id, metadata)
    VALUES ('operator', %(action)s, %(event_id)s, %(metadata)s)
"""

_RETRACT_SQL = """
    UPDATE threat_events SET status = 'retracted', updated_at = now()
    WHERE event_id = %(event_id)s AND status = 'active'
    RETURNING event_id
"""

_MATCHED_REFS_SQL = """
    SELECT user_ref FROM threat_event_matches WHERE event_id = %(event_id)s
"""

_LIST_EVENTS_SQL = """
    SELECT event_id, kind, title, body, severity, domains, is_global, penalty,
           starts_at, expires_at, decay_days, status, created_by, created_at, updated_at
    FROM threat_events
    ORDER BY created_at DESC
    LIMIT %(limit)s
"""


class ThreatStore(Protocol):
    async def create_event(
        self,
        *,
        kind: str,
        title: str,
        body: str,
        severity: int,
        domains: tuple[str, ...],
        is_global: bool,
        penalty: Decimal,
        expires_at: datetime,
        decay_days: int,
        operator: str,
    ) -> tuple[UUID, tuple[UserRef, ...]]: ...

    async def retract_event(
        self, event_id: UUID, *, operator: str, reason: str
    ) -> tuple[UserRef, ...] | None: ...

    async def list_events(self, *, limit: int = 50) -> list[dict[str, Any]]: ...


class PostgresThreatStore:
    """The one writer of ``threat_events`` / ``threat_event_matches``."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def create_event(
        self,
        *,
        kind: str,
        title: str,
        body: str,
        severity: int,
        domains: tuple[str, ...],
        is_global: bool,
        penalty: Decimal,
        expires_at: datetime,
        decay_days: int,
        operator: str,
    ) -> tuple[UUID, tuple[UserRef, ...]]:
        matched: set[UserRef] = set()
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                _INSERT_EVENT_SQL,
                {
                    "kind": kind,
                    "title": title,
                    "body": body,
                    "severity": severity,
                    "domains": list(domains),
                    "is_global": is_global,
                    "penalty": penalty,
                    "expires_at": expires_at,
                    "decay_days": decay_days,
                    "operator": operator,
                },
            )
            row = await cur.fetchone()
            assert row is not None
            event_id: UUID = row[0]

            cur = await conn.execute(
                _MATCH_DOMAINS_SQL,
                {"event_id": event_id, "domains": list(domains), "penalty": penalty},
            )
            matched.update(parse_user_ref(r[0]) for r in await cur.fetchall())

            if is_global:
                cur = await conn.execute(
                    _MATCH_GLOBAL_SQL, {"event_id": event_id, "penalty": penalty}
                )
                matched.update(parse_user_ref(r[0]) for r in await cur.fetchall())

            await conn.execute(
                _AUDIT_SQL,
                {
                    "action": THREAT_CREATED_ACTION,
                    "event_id": event_id,
                    "metadata": Jsonb(
                        {"operator": operator, "title": title, "matched": len(matched)}
                    ),
                },
            )
        log.info(
            "threat_event.created",
            event_id=str(event_id),
            operator=operator,
            matched_count=len(matched),
        )
        return event_id, tuple(matched)

    async def retract_event(
        self, event_id: UUID, *, operator: str, reason: str
    ) -> tuple[UserRef, ...] | None:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(_RETRACT_SQL, {"event_id": event_id})
            if await cur.fetchone() is None:
                # Unknown event, or already not active (expired/retracted).
                # Either way there is nothing to reverse and nothing to audit
                # — an audit row for a write that did not happen is the same
                # half-applied state providers/store.py's _write_with_audit
                # refuses to leave behind.
                return None

            cur = await conn.execute(_MATCHED_REFS_SQL, {"event_id": event_id})
            matched = tuple(parse_user_ref(r[0]) for r in await cur.fetchall())

            await conn.execute(
                _AUDIT_SQL,
                {
                    "action": THREAT_RETRACTED_ACTION,
                    "event_id": event_id,
                    "metadata": Jsonb(
                        {"operator": operator, "reason": reason, "matched": len(matched)}
                    ),
                },
            )
        log.info(
            "threat_event.retracted",
            event_id=str(event_id),
            operator=operator,
            matched_count=len(matched),
        )
        return matched

    async def list_events(self, *, limit: int = 50) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_LIST_EVENTS_SQL, {"limit": limit})
            rows = await cur.fetchall()
        return [
            {
                "event_id": row[0],
                "kind": row[1],
                "title": row[2],
                "body": row[3],
                "severity": row[4],
                "domains": list(row[5]),
                "is_global": row[6],
                "penalty": row[7],
                "starts_at": row[8],
                "expires_at": row[9],
                "decay_days": row[10],
                "status": row[11],
                "created_by": row[12],
                "created_at": row[13],
                "updated_at": row[14],
            }
            for row in rows
        ]


__all__ = [
    "THREAT_CREATED_ACTION",
    "THREAT_RETRACTED_ACTION",
    "PostgresThreatStore",
    "ThreatStore",
]
