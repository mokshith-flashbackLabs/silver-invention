"""protection_events: the credit list and the terminal retraction (spec §3.7, §4.7), and the
renewal's queue, evidence and write (§4.8).

Two modules write ``protection_events``, so "which code creates a credit" is answerable by file:
- ``decisions.py`` inserts one, from ``decided``, in the approval's own transaction, and is the
  ONLY inserter (tests/test_boundaries.py holds it to that). There is no hand-created credit:
  ``proposal_id`` is NOT NULL;
- this module retracts one, which is terminal.

Every operator write audits in the same transaction (``actor_type 'operator'``,
``metadata.operator``); the worker's renewal write audits as ``actor_type 'service'``. Statuses
on intel_proposals are SQL literals here, never parameters (tests/test_boundaries.py). Nothing
here removes a row: intel_rw holds no DELETE grant (0044).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

import structlog
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from imageshield.intel.bounds import PROTECTION_RENEWAL_WINDOW_DAYS

log = structlog.get_logger("imageshield.intel")

# The outcome keys a renewal_check run records (intel/pipeline.py writes them), and what the
# list read calls each. One place, so the writer and the reader cannot drift (spec §4.8).
RENEWAL_PROPOSED = "renewal_proposed"
RENEWAL_EVIDENCE_GONE = "renewal_evidence_gone"
RENEWAL_EVIDENCE_UNREACHABLE = "renewal_evidence_unreachable"
RENEWAL_NOT_DUE = "renewal_not_due"
_RENEWAL_RESULTS: dict[str, str] = {
    RENEWAL_PROPOSED: "proposed",
    RENEWAL_EVIDENCE_GONE: "evidence_gone",
    RENEWAL_EVIDENCE_UNREACHABLE: "evidence_unreachable",
    RENEWAL_NOT_DUE: "not_due",
}
# After one of these the worker never checks that credit again. An unreachable page or a
# failed run is retried instead (spec note 2026-09-30).
CONCLUSIVE_RENEWAL_RESULTS: tuple[str, ...] = (
    RENEWAL_PROPOSED,
    RENEWAL_EVIDENCE_GONE,
    RENEWAL_NOT_DUE,
)

_AUDIT_SQL = """
    INSERT INTO audit_log (actor_type, action, resource_id, metadata)
    VALUES (%(actor_type)s, %(action)s, %(resource_id)s, %(metadata)s)
"""

_EVENT_COLUMNS = """e.event_id, e.title, e.body, e.strength, e.tags, e.is_global, e.starts_at,
    e.review_by, e.status, e.proposal_id, e.renews_event_id, e.created_by, e.created_at,
    e.retracted_by, e.retracted_at, e.retract_reason"""

# state and renewal_due are read-time facts, never stored (spec §4.7). The latest renewal check
# and the latest renewal proposal ride along, so an operator sees why a credit is lapsing.
_LIST_SELECT = f"""
    SELECT {_EVENT_COLUMNS},
           CASE WHEN e.status = 'retracted' THEN 'retracted'
                WHEN e.starts_at > now() THEN 'scheduled'
                WHEN e.review_by <= now() THEN 'lapsed'
                ELSE 'live' END AS state,
           (e.status = 'active' AND e.starts_at <= now() AND e.review_by > now()
            AND e.review_by <= now() + make_interval(days => %(window)s)
            AND NOT EXISTS (SELECT 1 FROM protection_events r
                             WHERE r.renews_event_id = e.event_id)) AS renewal_due,
           run.run_id AS renewal_run_id, run.status AS renewal_run_status,
           run.outcome AS renewal_outcome, run.error_code AS renewal_error_code,
           run.completed_at AS renewal_completed_at,
           rp.proposal_id AS renewal_proposal_id, rp.status AS renewal_proposal_status
      FROM protection_events e
      LEFT JOIN LATERAL (
          SELECT r.run_id, r.status, r.outcome, r.error_code, r.completed_at
            FROM intel_runs r
           WHERE r.kind = 'renewal_check' AND r.request ->> 'event_id' = e.event_id::text
           ORDER BY r.created_at DESC, r.run_id DESC
           LIMIT 1) run ON true
      LEFT JOIN LATERAL (
          SELECT p.proposal_id, p.status
            FROM intel_proposals p
           WHERE p.kind = 'protection_event'
             AND p.target ->> 'renews_event_id' = e.event_id::text
           ORDER BY p.created_at DESC, p.proposal_id DESC
           LIMIT 1) rp ON true
"""

_LOCK_PENDING_RENEWALS_SQL = """
    SELECT proposal_id FROM intel_proposals
     WHERE kind = 'protection_event' AND status = 'pending'
       AND target ->> 'renews_event_id' = %(event)s
       FOR UPDATE
"""

_RETRACT_SQL = """
    UPDATE protection_events
       SET status = 'retracted', retracted_by = %(operator)s, retracted_at = now(),
           retract_reason = %(reason)s
     WHERE event_id = %(event_id)s AND status = 'active'
    RETURNING event_id
"""

# A renewal approved but not started credits nobody yet, and would start crediting at this
# credit's review date. It goes with it. A started renewal is its own live credit.
_RETRACT_UNSTARTED_RENEWAL_SQL = """
    UPDATE protection_events
       SET status = 'retracted', retracted_by = %(operator)s, retracted_at = now(),
           retract_reason = %(reason)s
     WHERE renews_event_id = %(event_id)s AND status = 'active' AND starts_at > now()
    RETURNING event_id
"""

# Runs AFTER the credit's row is locked, so it must never WAIT on a proposal. The rows locked up
# front are this transaction's own and are never skipped; SKIP LOCKED passes over only a renewal
# proposal committed since then that another transaction holds right now. An approval of it then
# finds the credit retracted and is refused (proposal_not_pending), and it stays pending. Waiting
# on it instead would be a proposal lock taken after a protection_events lock -- a deadlock with
# that approval, which holds its proposal and waits on this credit.
_REJECT_PENDING_RENEWALS_SQL = """
    UPDATE intel_proposals
       SET status = 'rejected', decided_by = %(operator)s, decided_at = now(),
           decision_reason = %(reason)s
     WHERE status = 'pending' AND proposal_id IN (
           SELECT proposal_id FROM intel_proposals
            WHERE kind = 'protection_event' AND status = 'pending'
              AND target ->> 'renews_event_id' = %(event)s
              FOR UPDATE SKIP LOCKED)
    RETURNING proposal_id
"""


def renewal_result(outcome: Mapping[str, Any]) -> str | None:
    """What a renewal check's outcome says, for the list read; None while it is undecided."""
    for key, result in _RENEWAL_RESULTS.items():
        if key in outcome:
            return result
    return None


@dataclass(frozen=True)
class ProtectionRetraction:
    """What one retraction did. ``already_retracted`` is a repeat: nothing was written."""

    event_id: UUID
    also_retracted: tuple[UUID, ...]
    renewal_proposals_rejected: tuple[UUID, ...]
    already_retracted: bool = False


def _event_row(row: dict[str, Any]) -> dict[str, Any]:
    renewal: dict[str, Any] | None = None
    if row["renewal_run_id"] is not None or row["renewal_proposal_id"] is not None:
        renewal = {
            "run_id": row["renewal_run_id"],
            "run_status": row["renewal_run_status"],
            "result": renewal_result(row["renewal_outcome"] or {}),
            "error_code": row["renewal_error_code"],
            "completed_at": row["renewal_completed_at"],
            "proposal_id": row["renewal_proposal_id"],
            "proposal_status": row["renewal_proposal_status"],
        }
    return {
        "event_id": row["event_id"],
        "title": row["title"],
        "body": row["body"],
        "strength": row["strength"],
        "tags": list(row["tags"]),
        "is_global": row["is_global"],
        "starts_at": row["starts_at"],
        "review_by": row["review_by"],
        "status": row["status"],
        "state": row["state"],
        "renewal_due": row["renewal_due"],
        "renewal": renewal,
        "proposal_id": row["proposal_id"],
        "renews_event_id": row["renews_event_id"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "retracted_by": row["retracted_by"],
        "retracted_at": row["retracted_at"],
        "retract_reason": row["retract_reason"],
    }


class ProtectionStore(Protocol):
    async def list_events(
        self,
        *,
        statuses: Sequence[str] | None,
        cursor: tuple[datetime, UUID] | None,
        limit: int,
    ) -> list[dict[str, Any]]: ...
    async def retract(
        self, event_id: UUID, *, operator: str, reason: str
    ) -> ProtectionRetraction | None: ...


class PostgresProtectionStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def list_events(
        self,
        *,
        statuses: Sequence[str] | None,
        cursor: tuple[datetime, UUID] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Keyset-paged on ``(created_at, event_id)``, newest first (spec §4.7)."""
        clauses = ["true"]
        params: dict[str, Any] = {"window": PROTECTION_RENEWAL_WINDOW_DAYS, "limit": limit}
        if statuses:
            clauses.append("e.status = ANY(%(statuses)s::text[])")
            params["statuses"] = list(statuses)
        if cursor is not None:
            clauses.append("(e.created_at, e.event_id) < (%(at)s, %(id)s)")
            params.update(at=cursor[0], id=cursor[1])
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"{_LIST_SELECT} WHERE {' AND '.join(clauses)}"
                " ORDER BY e.created_at DESC, e.event_id DESC LIMIT %(limit)s",
                params,
            )
            rows = await cur.fetchall()
        return [_event_row(row) for row in rows]

    async def retract(
        self, event_id: UUID, *, operator: str, reason: str
    ) -> ProtectionRetraction | None:
        """Terminal, in one transaction with its audit row (spec §4.7 as amended 2026-09-30).
        It also retracts the credit's renewal that has not started and rejects its pending
        renewal proposal, in the operator's name. None for an id that is no protection credit;
        a repeat on a retracted one writes nothing and says so."""
        event = str(event_id)
        async with self._pool.connection() as conn, conn.transaction():
            # The pending renewal proposals first: an approval locks its proposal before the
            # credit it renews (intel/decisions.py), so the same order here cannot deadlock.
            await conn.execute(_LOCK_PENDING_RENEWALS_SQL, {"event": event})
            cur = await conn.execute(
                _RETRACT_SQL, {"event_id": event_id, "operator": operator, "reason": reason}
            )
            if await cur.fetchone() is None:
                cur = await conn.execute(
                    "SELECT 1 FROM protection_events WHERE event_id = %s AND status = 'retracted'",
                    (event_id,),
                )
                if await cur.fetchone() is None:
                    return None
                return ProtectionRetraction(event_id, (), (), already_retracted=True)
            cur = await conn.execute(
                _RETRACT_UNSTARTED_RENEWAL_SQL,
                {
                    "event_id": event_id,
                    "operator": operator,
                    "reason": f"Retracted with the protection it continues: {reason}",
                },
            )
            also = tuple(r[0] for r in await cur.fetchall())
            cur = await conn.execute(
                _REJECT_PENDING_RENEWALS_SQL,
                {
                    "event": event,
                    "operator": operator,
                    "reason": f"The protection this renews was retracted: {reason}",
                },
            )
            rejected = tuple(r[0] for r in await cur.fetchall())
            await conn.execute(
                _AUDIT_SQL,
                {
                    "actor_type": "operator",
                    "action": "intel.protection_retracted",
                    "resource_id": event_id,
                    "metadata": Jsonb(
                        {
                            "operator": operator,
                            "reason": reason,
                            "also_retracted": [str(i) for i in also],
                            "renewal_proposals_rejected": [str(i) for i in rejected],
                        }
                    ),
                },
            )
        return ProtectionRetraction(event_id, also, rejected)
