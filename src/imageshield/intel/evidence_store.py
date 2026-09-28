"""Evidence store: one transaction per unit, snapshots, source bookkeeping, signals
(spec §4.3-§4.4). ``record_unit`` is the one writer of ``intel_documents``,
``intel_signals``, ``intel_excerpts``, ``intel_snapshots`` and the source's
``last_content_sha256`` — together, in one transaction, or not at all. A reclaimed
run's second attempt at a document it already recorded is absorbed by
``ON CONFLICT DO NOTHING`` (the ``(run_id, url_hash)`` unique constraint) rather than
raising or double-writing signals: the caller sees ``None`` and moves on.

Every write here that is an operator act audits in the same transaction
(``actor_type 'operator'``); ``record_check``/``disable_source`` are the worker's own
bookkeeping and audit as ``actor_type 'service'``. Nothing here ``DELETE``s —
``intel_rw`` holds no DELETE grant on any of these tables (migration 0038).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID

import structlog
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from imageshield.intel.bounds import MAX_SOURCE_CONSECUTIVE_FAILURES
from imageshield.intel.verify import VerifiedQuote
from imageshield.search.urlhash import NORMALISATION_VERSION

log = structlog.get_logger("imageshield.intel")

_AUDIT_SQL = """
    INSERT INTO audit_log (actor_type, action, resource_id, metadata)
    VALUES (%(actor_type)s, %(action)s, %(resource_id)s, %(metadata)s)
"""

_SIGNAL_COLUMNS = """signal_id, document_id, category, direction, tags, unregistered_subjects,
    summary, model_id, prompt_version, status, retracted_by, retracted_at, retract_reason,
    created_at"""

_DOCUMENT_COLUMNS = """document_id, run_id, source_id, document_url, final_url, url_hash,
    document_url_hash, publisher_domain, trust, content_sha256, truncated, title, published_at,
    fetched_at"""


@dataclass(frozen=True)
class DocumentRecord:
    """One fetched unit (spec §4.3), pre-insert.

    ``document_url_hash`` is the normalised hash of the *requested* URL, distinct
    from ``url_hash`` (the *final* URL, after any redirect) only when a redirect
    happened. It defaults to ``None`` — meaning "no redirect, use ``url_hash``" —
    and ``record_unit`` resolves that default at insert time, so a caller that
    never followed a redirect need not pass it at all.
    """

    run_id: UUID
    document_url: str
    final_url: str
    url_hash: str
    publisher_domain: str
    trust: Literal["listed", "web"]
    content_sha256: str
    truncated: bool
    title: str
    published_at: datetime | None
    source_id: UUID | None = None
    document_url_hash: str | None = None


@dataclass(frozen=True)
class SignalRecord:
    """One extracted signal (spec §4.4), pre-insert, with its supporting quotes.
    ``quotes`` are already-verified (``imageshield.intel.verify.verify_quote``) —
    this module trusts them and writes each as its own ``intel_excerpts`` row."""

    category: Literal["policy", "incident", "tooling", "protection", "law", "research"]
    direction: Literal["risk_up", "risk_down", "neutral"]
    tags: tuple[str, ...]
    unregistered_subjects: tuple[str, ...]
    summary: str
    model_id: str
    prompt_version: str
    quotes: tuple[VerifiedQuote, ...]


@dataclass(frozen=True)
class SnapshotRecord:
    """The latest normalised, PII-masked text of a ``policy_page`` source
    (INVARIANTS #9 amended) — one row per source, replaced on every re-fetch."""

    source_id: UUID
    content_sha256: str
    snapshot_text: str
    content_type: str
    truncated: bool


class EvidenceStore(Protocol):
    async def record_unit(
        self,
        document: DocumentRecord,
        signals: Sequence[SignalRecord],
        *,
        snapshot: SnapshotRecord | None,
        source_hash: tuple[UUID, str] | None,
    ) -> UUID | None: ...
    async def snapshot_for(self, source_id: UUID) -> SnapshotRecord | None: ...
    async def seen_url_hashes(self, source_id: UUID, hashes: Sequence[str]) -> set[str]: ...
    async def recently_fetched(self, hashes: Sequence[str], *, days: int) -> set[str]: ...
    async def record_check(self, source_id: UUID, *, ok: bool, status: str) -> None: ...
    async def disable_source(
        self, source_id: UUID, *, reason: Literal["too_short", "unreachable"]
    ) -> None: ...
    async def list_signals(
        self, *, cursor: tuple[datetime, UUID] | None, limit: int
    ) -> list[dict[str, Any]]: ...
    async def get_signal(self, signal_id: UUID) -> dict[str, Any] | None: ...
    async def retract_signal(
        self, signal_id: UUID, *, operator: str, reason: str
    ) -> Literal["retracted", "not_found", "not_active"]: ...


class PostgresEvidenceStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def record_unit(
        self,
        document: DocumentRecord,
        signals: Sequence[SignalRecord],
        *,
        snapshot: SnapshotRecord | None,
        source_hash: tuple[UUID, str] | None,
    ) -> UUID | None:
        async with self._pool.connection() as conn, conn.transaction():
            params = {
                **dataclasses.asdict(document),
                "document_url_hash": document.document_url_hash or document.url_hash,
                "nv": NORMALISATION_VERSION,
            }
            cur = await conn.execute(
                """INSERT INTO intel_documents (run_id, source_id, document_url, final_url,
                       url_hash, document_url_hash, normalisation_version, publisher_domain,
                       trust, content_sha256, truncated, title, published_at)
                   VALUES (%(run_id)s, %(source_id)s, %(document_url)s, %(final_url)s,
                           %(url_hash)s, %(document_url_hash)s, %(nv)s, %(publisher_domain)s,
                           %(trust)s, %(content_sha256)s, %(truncated)s, %(title)s,
                           %(published_at)s)
                   ON CONFLICT (run_id, url_hash) DO NOTHING
                   RETURNING document_id""",
                params,
            )
            row = await cur.fetchone()
            if row is None:
                return None  # a reclaimed run already recorded this unit
            document_id: UUID = row[0]

            for signal in signals:
                cur = await conn.execute(
                    """INSERT INTO intel_signals (document_id, category, direction, tags,
                           unregistered_subjects, summary, model_id, prompt_version)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING signal_id""",
                    (
                        document_id,
                        signal.category,
                        signal.direction,
                        list(signal.tags),
                        list(signal.unregistered_subjects),
                        signal.summary,
                        signal.model_id,
                        signal.prompt_version,
                    ),
                )
                signal_row = await cur.fetchone()
                assert signal_row is not None
                signal_id = signal_row[0]
                for quote in signal.quotes:
                    await conn.execute(
                        "INSERT INTO intel_excerpts (signal_id, quote_text, char_start,"
                        " char_end, quote_sha256) VALUES (%s, %s, %s, %s, %s)",
                        (signal_id, quote.text, quote.char_start, quote.char_end, quote.sha256),
                    )

            if snapshot is not None:
                await conn.execute(
                    """INSERT INTO intel_snapshots (source_id, content_sha256, snapshot_text,
                           content_type, truncated)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (source_id) DO UPDATE SET
                           content_sha256 = EXCLUDED.content_sha256,
                           snapshot_text = EXCLUDED.snapshot_text,
                           content_type = EXCLUDED.content_type,
                           truncated = EXCLUDED.truncated,
                           fetched_at = now()""",
                    (
                        snapshot.source_id,
                        snapshot.content_sha256,
                        snapshot.snapshot_text,
                        snapshot.content_type,
                        snapshot.truncated,
                    ),
                )

            if source_hash is not None:
                await conn.execute(
                    "UPDATE intel_sources SET last_content_sha256 = %s, updated_at = now()"
                    " WHERE source_id = %s",
                    (source_hash[1], source_hash[0]),
                )
        return document_id

    async def snapshot_for(self, source_id: UUID) -> SnapshotRecord | None:
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                "SELECT source_id, content_sha256, snapshot_text, content_type, truncated"
                " FROM intel_snapshots WHERE source_id = %s",
                (source_id,),
            )
            row = await cur.fetchone()
        return SnapshotRecord(**row) if row is not None else None

    async def seen_url_hashes(self, source_id: UUID, hashes: Sequence[str]) -> set[str]:
        hash_list = list(hashes)
        if not hash_list:
            return set()
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT url_hash FROM intel_documents WHERE source_id = %s AND url_hash = ANY(%s)",
                (source_id, hash_list),
            )
            rows = await cur.fetchall()
        return {r[0] for r in rows}

    async def recently_fetched(self, hashes: Sequence[str], *, days: int) -> set[str]:
        """Which of ``hashes`` was fetched within the last ``days`` days, matching
        EITHER ``url_hash`` (the final URL) or ``document_url_hash`` (the requested
        URL). A caller — task 10's fetch loop, before every fetch — may hash either
        side of a redirect; matching only one column would let a redirecting URL
        get re-fetched (and re-billed) every run despite already being on file
        under its other hash. Signature: ``recently_fetched(hashes: Sequence[str],
        *, days: int) -> set[str]``.
        """
        hash_list = list(hashes)
        if not hash_list:
            return set()
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT DISTINCT h FROM (
                       SELECT url_hash AS h FROM intel_documents
                        WHERE url_hash = ANY(%(hashes)s)
                          AND fetched_at > now() - make_interval(days => %(days)s)
                       UNION
                       SELECT document_url_hash AS h FROM intel_documents
                        WHERE document_url_hash = ANY(%(hashes)s)
                          AND fetched_at > now() - make_interval(days => %(days)s)
                   ) matched""",
                {"hashes": hash_list, "days": days},
            )
            rows = await cur.fetchall()
        return {r[0] for r in rows}

    async def record_check(self, source_id: UUID, *, ok: bool, status: str) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT consecutive_failures, enabled FROM intel_sources"
                " WHERE source_id = %s FOR UPDATE",
                (source_id,),
            )
            row = await cur.fetchone()
            if row is None:
                return
            old_failures, was_enabled = row
            new_failures = 0 if ok else old_failures + 1
            just_disabled = (
                (not ok) and was_enabled and new_failures >= MAX_SOURCE_CONSECUTIVE_FAILURES
            )
            await conn.execute(
                """UPDATE intel_sources SET
                       last_checked_at = now(),
                       last_run_status = %(status)s,
                       consecutive_failures = %(failures)s,
                       enabled = CASE WHEN %(just_disabled)s THEN false ELSE enabled END,
                       disabled_reason = CASE WHEN %(just_disabled)s THEN 'unreachable'
                                             ELSE disabled_reason END,
                       updated_at = now()
                    WHERE source_id = %(source_id)s""",
                {
                    "status": status,
                    "failures": new_failures,
                    "just_disabled": just_disabled,
                    "source_id": source_id,
                },
            )
            if just_disabled:
                log.warning(
                    "intel.source_disabled",
                    source_id=str(source_id),
                    reason="unreachable",
                    consecutive_failures=new_failures,
                )
                await conn.execute(
                    _AUDIT_SQL,
                    {
                        "actor_type": "service",
                        "action": "intel.source_disabled",
                        "resource_id": source_id,
                        "metadata": Jsonb(
                            {"reason": "unreachable", "consecutive_failures": new_failures}
                        ),
                    },
                )

    async def disable_source(
        self, source_id: UUID, *, reason: Literal["too_short", "unreachable"]
    ) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute(
                "UPDATE intel_sources SET enabled = false, disabled_reason = %s, updated_at = now()"
                " WHERE source_id = %s",
                (reason, source_id),
            )
            log.warning("intel.source_disabled", source_id=str(source_id), reason=reason)
            await conn.execute(
                _AUDIT_SQL,
                {
                    "actor_type": "service",
                    "action": "intel.source_disabled",
                    "resource_id": source_id,
                    "metadata": Jsonb({"reason": reason}),
                },
            )

    async def list_signals(
        self, *, cursor: tuple[datetime, UUID] | None, limit: int
    ) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            if cursor is None:
                await cur.execute(
                    f"SELECT {_SIGNAL_COLUMNS} FROM intel_signals"
                    " ORDER BY created_at DESC, signal_id DESC LIMIT %s",
                    (limit,),
                )
            else:
                await cur.execute(
                    f"SELECT {_SIGNAL_COLUMNS} FROM intel_signals"
                    " WHERE (created_at, signal_id) < (%s, %s)"
                    " ORDER BY created_at DESC, signal_id DESC LIMIT %s",
                    (cursor[0], cursor[1], limit),
                )
            rows = await cur.fetchall()
        return [
            {
                **row,
                "tags": list(row["tags"]),
                "unregistered_subjects": list(row["unregistered_subjects"]),
            }
            for row in rows
        ]

    async def get_signal(self, signal_id: UUID) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"SELECT {_SIGNAL_COLUMNS} FROM intel_signals WHERE signal_id = %s", (signal_id,)
            )
            signal = await cur.fetchone()
            if signal is None:
                return None
            await cur.execute(
                "SELECT excerpt_id, quote_text, char_start, char_end, quote_sha256"
                " FROM intel_excerpts WHERE signal_id = %s ORDER BY char_start",
                (signal_id,),
            )
            excerpts = await cur.fetchall()
            await cur.execute(
                f"SELECT {_DOCUMENT_COLUMNS} FROM intel_documents WHERE document_id = %s",
                (signal["document_id"],),
            )
            document = await cur.fetchone()
        return {
            **signal,
            "tags": list(signal["tags"]),
            "unregistered_subjects": list(signal["unregistered_subjects"]),
            "excerpts": excerpts,
            "document": document,
        }

    async def retract_signal(
        self, signal_id: UUID, *, operator: str, reason: str
    ) -> Literal["retracted", "not_found", "not_active"]:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "UPDATE intel_signals SET status = 'retracted', retracted_by = %s,"
                " retracted_at = now(), retract_reason = %s"
                " WHERE signal_id = %s AND status = 'active' RETURNING 1",
                (operator, reason, signal_id),
            )
            updated = await cur.fetchone() is not None
            if updated:
                await conn.execute(
                    _AUDIT_SQL,
                    {
                        "actor_type": "operator",
                        "action": "intel.signal_retracted",
                        "resource_id": signal_id,
                        "metadata": Jsonb({"operator": operator, "reason": reason}),
                    },
                )
                return "retracted"
            cur = await conn.execute(
                "SELECT 1 FROM intel_signals WHERE signal_id = %s", (signal_id,)
            )
            exists = await cur.fetchone() is not None
        return "not_active" if exists else "not_found"
