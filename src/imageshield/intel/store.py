"""The intel registry and run queue (spec §3.2, §3.4, §3.8). The one writer of
intel_sources, intel_runs and intel_vocabulary.

Every write that is an operator act writes its ``audit_log`` row in the same transaction
(``actor_type 'operator'``, ``metadata.operator``); a machine write (the vocabulary push)
uses ``actor_type 'service'``. Nothing here ever ``DELETE``s — ``intel_rw`` holds no
DELETE grant on any of these tables (migration 0039), so a bug that tried would fail at
the database rather than merely at review.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

import structlog
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from imageshield.intel.bounds import MAX_RUN_ATTEMPTS
from imageshield.intel.models import Run, Source, SpendToday, Vocabulary
from imageshield.providers.store import utc_spend_date
from imageshield.search.urlhash import NORMALISATION_VERSION, canonicalise, url_hash

log = structlog.get_logger("imageshield.intel")

CLAUDE_INTEL = "claude_intel"

_SOURCE_COLUMNS = """source_id, kind, source_url, url_hash, query_text, tags, check_every_hours,
    next_check_at, enabled, terms_note, last_content_sha256, last_checked_at, last_run_status,
    consecutive_failures, disabled_reason, created_by, created_at"""
_RUN_COLUMNS = """run_id, kind, source_id, request, status, attempts, requested_by, outcome,
    error_code, created_at, completed_at"""

_AUDIT_SQL = """
    INSERT INTO audit_log (actor_type, action, resource_id, metadata)
    VALUES (%(actor_type)s, %(action)s, %(resource_id)s, %(metadata)s)
"""

# One statement: advance the due sources and return them, so an overlapping task
# during a rolling deploy cannot create a second run for the same source.
_SCHEDULE_SQL = """
    WITH due AS (
        UPDATE intel_sources s
           SET next_check_at = %(now)s + make_interval(hours => s.check_every_hours),
               updated_at = now()
         WHERE s.enabled AND s.next_check_at <= %(now)s
           AND NOT EXISTS (SELECT 1 FROM intel_runs r WHERE r.source_id = s.source_id
                            AND r.status IN ('queued', 'running'))
        RETURNING s.source_id, s.kind
    )
    INSERT INTO intel_runs (kind, source_id, requested_by)
    SELECT CASE WHEN kind = 'search_query' THEN 'discovery' ELSE 'source_check' END,
           source_id, 'schedule'
      FROM due
    ON CONFLICT DO NOTHING
    RETURNING run_id
"""

_CLAIM_SQL = f"""
    UPDATE intel_runs SET status = 'running', attempts = attempts + 1,
           started_at = coalesce(started_at, %(now)s),
           lease_expires_at = %(now)s + make_interval(secs => %(lease)s)
     WHERE run_id = (
        SELECT run_id FROM intel_runs
         WHERE attempts < %(max_attempts)s
           AND (status = 'queued' OR (status = 'running' AND lease_expires_at <= %(now)s))
         ORDER BY created_at
         FOR UPDATE SKIP LOCKED
         LIMIT 1)
    RETURNING {_RUN_COLUMNS}
"""

_EXPIRE_SQL = """
    UPDATE intel_runs SET status = 'failed', error_code = 'attempts_exhausted',
           completed_at = %(now)s
     WHERE status = 'running' AND lease_expires_at <= %(now)s AND attempts >= %(max_attempts)s
"""

# ``<=`` on both halves of the pair, not ``<``: an equal pair is a legitimate re-push
# (e.g. the same release republished with a corrected document) and must overwrite
# rather than be silently dropped as "no-op". A strictly-older pair on either half of
# the pair fails the WHERE and the INSERT ... ON CONFLICT DO UPDATE ... WHERE applies
# no update, so ``RETURNING 1`` yields no row and the call reports "not applied".
_PUT_VOCAB_SQL = """
    INSERT INTO intel_vocabulary (id, release_no, map_version, scoring_version, quiz_version,
        document)
    VALUES (1, %(release_no)s, %(map_version)s, %(scoring_version)s, %(quiz_version)s, %(document)s)
    ON CONFLICT (id) DO UPDATE SET
        release_no = EXCLUDED.release_no, map_version = EXCLUDED.map_version,
        scoring_version = EXCLUDED.scoring_version, quiz_version = EXCLUDED.quiz_version,
        document = EXCLUDED.document, received_at = now()
     WHERE intel_vocabulary.release_no <= EXCLUDED.release_no
       AND intel_vocabulary.map_version <= EXCLUDED.map_version
    RETURNING 1
"""

_PATCH_SOURCE_SQL = f"""
    UPDATE intel_sources SET
        enabled = coalesce(%(enabled)s::boolean, enabled),
        disabled_reason = CASE WHEN %(enabled)s::boolean IS TRUE THEN NULL ELSE disabled_reason END,
        consecutive_failures = CASE WHEN %(enabled)s::boolean IS TRUE THEN 0
                                    ELSE consecutive_failures END,
        check_every_hours = coalesce(%(check_every_hours)s::int, check_every_hours),
        tags = coalesce(%(tags)s::text[], tags),
        terms_note = coalesce(%(terms_note)s::text, terms_note),
        query_text = coalesce(%(query_text)s::text, query_text),
        updated_at = now()
     WHERE source_id = %(source_id)s
    RETURNING {_SOURCE_COLUMNS}
"""


class IntelStore(Protocol):
    async def create_source(
        self,
        *,
        kind: str,
        source_url: str | None,
        query_text: str | None,
        tags: tuple[str, ...],
        check_every_hours: int,
        terms_note: str,
        operator: str,
    ) -> Source: ...
    async def list_sources(
        self, *, cursor: tuple[datetime, UUID] | None, limit: int
    ) -> list[Source]: ...
    async def get_source(self, source_id: UUID) -> Source | None: ...
    async def patch_source(
        self,
        source_id: UUID,
        *,
        operator: str,
        enabled: bool | None = None,
        check_every_hours: int | None = None,
        tags: tuple[str, ...] | None = None,
        terms_note: str | None = None,
        query_text: str | None = None,
    ) -> Source | None: ...
    async def queue_source_check(self, source_id: UUID, *, operator: str) -> UUID | None: ...
    async def queue_adhoc(self, url: str, *, operator: str) -> UUID: ...
    async def schedule_due(self, now: datetime) -> list[UUID]: ...
    async def claim_next(self, now: datetime, *, lease_seconds: int) -> Run | None: ...
    async def expire_exhausted(self, now: datetime) -> int: ...
    async def finish_run(
        self, run_id: UUID, *, status: str, outcome: dict[str, Any], error_code: str | None = None
    ) -> None: ...
    async def set_run_vocabulary(
        self, run_id: UUID, *, release_no: int, map_version: int
    ) -> None: ...
    async def list_runs(self, *, cursor: tuple[datetime, UUID] | None, limit: int) -> list[Run]: ...
    async def is_known_hit(self, url_hash_value: str) -> bool: ...
    async def put_vocabulary(
        self,
        *,
        release_no: int,
        map_version: int,
        scoring_version: str,
        quiz_version: str,
        document: dict[str, Any],
    ) -> bool: ...
    async def load_vocabulary(self) -> Vocabulary | None: ...
    async def spend_today(self, now: datetime) -> SpendToday: ...


class PostgresIntelStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def create_source(
        self,
        *,
        kind: str,
        source_url: str | None,
        query_text: str | None,
        tags: tuple[str, ...],
        check_every_hours: int,
        terms_note: str,
        operator: str,
    ) -> Source:
        canonical = canonicalise(source_url) if source_url is not None else None
        async with self._pool.connection() as conn, conn.transaction():
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"""INSERT INTO intel_sources (kind, source_url, url_hash, normalisation_version,
                        query_text, tags, check_every_hours, terms_note, created_by)
                    VALUES (%(kind)s, %(url)s, %(hash)s, %(nv)s, %(query)s, %(tags)s, %(every)s,
                            %(terms)s, %(operator)s)
                    RETURNING {_SOURCE_COLUMNS}""",
                {
                    "kind": kind,
                    "url": canonical,
                    "hash": url_hash(canonical) if canonical is not None else None,
                    "nv": NORMALISATION_VERSION if canonical is not None else None,
                    "query": query_text,
                    "tags": list(tags),
                    "every": check_every_hours,
                    "terms": terms_note,
                    "operator": operator,
                },
            )
            row = await cur.fetchone()
            assert row is not None
            await conn.execute(
                _AUDIT_SQL,
                {
                    "actor_type": "operator",
                    "action": "intel.source_created",
                    "resource_id": row["source_id"],
                    "metadata": Jsonb({"operator": operator, "kind": kind}),
                },
            )
        return Source.model_validate({**row, "tags": tuple(row["tags"])})

    async def list_sources(
        self, *, cursor: tuple[datetime, UUID] | None, limit: int
    ) -> list[Source]:
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            if cursor is None:
                await cur.execute(
                    f"SELECT {_SOURCE_COLUMNS} FROM intel_sources"
                    " ORDER BY created_at DESC, source_id DESC LIMIT %s",
                    (limit,),
                )
            else:
                await cur.execute(
                    f"SELECT {_SOURCE_COLUMNS} FROM intel_sources"
                    " WHERE (created_at, source_id) < (%s, %s)"
                    " ORDER BY created_at DESC, source_id DESC LIMIT %s",
                    (cursor[0], cursor[1], limit),
                )
            rows = await cur.fetchall()
        return [Source.model_validate(row) for row in rows]

    async def get_source(self, source_id: UUID) -> Source | None:
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"SELECT {_SOURCE_COLUMNS} FROM intel_sources WHERE source_id = %s", (source_id,)
            )
            row = await cur.fetchone()
        return Source.model_validate(row) if row is not None else None

    async def patch_source(
        self,
        source_id: UUID,
        *,
        operator: str,
        enabled: bool | None = None,
        check_every_hours: int | None = None,
        tags: tuple[str, ...] | None = None,
        terms_note: str | None = None,
        query_text: str | None = None,
    ) -> Source | None:
        async with self._pool.connection() as conn, conn.transaction():
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                _PATCH_SOURCE_SQL,
                {
                    "source_id": source_id,
                    "enabled": enabled,
                    "check_every_hours": check_every_hours,
                    "tags": list(tags) if tags is not None else None,
                    "terms_note": terms_note,
                    "query_text": query_text,
                },
            )
            row = await cur.fetchone()
            if row is None:
                return None
            await conn.execute(
                _AUDIT_SQL,
                {
                    "actor_type": "operator",
                    "action": "intel.source_updated",
                    "resource_id": source_id,
                    "metadata": Jsonb({"operator": operator}),
                },
            )
        return Source.model_validate(row)

    async def queue_source_check(self, source_id: UUID, *, operator: str) -> UUID | None:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT kind FROM intel_sources WHERE source_id = %s FOR UPDATE", (source_id,)
            )
            source = await cur.fetchone()
            if source is None:
                return None
            cur = await conn.execute(
                "SELECT run_id FROM intel_runs WHERE source_id = %s"
                " AND status IN ('queued','running')",
                (source_id,),
            )
            existing = await cur.fetchone()
            if existing is not None:
                return existing[0]  # type: ignore[no-any-return]
            kind = "discovery" if source[0] == "search_query" else "source_check"
            cur = await conn.execute(
                "INSERT INTO intel_runs (kind, source_id, requested_by) VALUES (%s, %s, %s)"
                " RETURNING run_id",
                (kind, source_id, operator),
            )
            run = await cur.fetchone()
            assert run is not None
            await conn.execute(
                _AUDIT_SQL,
                {
                    "actor_type": "operator",
                    "action": "intel.check_queued",
                    "resource_id": run[0],
                    "metadata": Jsonb({"operator": operator}),
                },
            )
            return run[0]  # type: ignore[no-any-return]

    async def queue_adhoc(self, url: str, *, operator: str) -> UUID:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "INSERT INTO intel_runs (kind, request, requested_by) VALUES ('adhoc_url', %s, %s)"
                " RETURNING run_id",
                (Jsonb({"url": url}), operator),
            )
            run = await cur.fetchone()
            assert run is not None
            await conn.execute(
                _AUDIT_SQL,
                {
                    "actor_type": "operator",
                    "action": "intel.document_queued",
                    "resource_id": run[0],
                    "metadata": Jsonb({"operator": operator}),
                },
            )
            return run[0]  # type: ignore[no-any-return]

    async def schedule_due(self, now: datetime) -> list[UUID]:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(_SCHEDULE_SQL, {"now": now})
            return [r[0] for r in await cur.fetchall()]

    async def claim_next(self, now: datetime, *, lease_seconds: int) -> Run | None:
        async with self._pool.connection() as conn, conn.transaction():
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                _CLAIM_SQL, {"now": now, "lease": lease_seconds, "max_attempts": MAX_RUN_ATTEMPTS}
            )
            row = await cur.fetchone()
        return Run.model_validate(row) if row is not None else None

    async def expire_exhausted(self, now: datetime) -> int:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(_EXPIRE_SQL, {"now": now, "max_attempts": MAX_RUN_ATTEMPTS})
            count = cur.rowcount
        if count:
            log.error("intel.run_attempts_exhausted", count=count)
        return count

    async def finish_run(
        self, run_id: UUID, *, status: str, outcome: dict[str, Any], error_code: str | None = None
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE intel_runs SET status = %s, outcome = %s, error_code = %s,"
                " completed_at = now(), lease_expires_at = NULL WHERE run_id = %s",
                (status, Jsonb(outcome), error_code, run_id),
            )

    async def set_run_vocabulary(self, run_id: UUID, *, release_no: int, map_version: int) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE intel_runs SET vocabulary_release_no = %s, vocabulary_map_version = %s"
                " WHERE run_id = %s",
                (release_no, map_version, run_id),
            )

    async def list_runs(self, *, cursor: tuple[datetime, UUID] | None, limit: int) -> list[Run]:
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            if cursor is None:
                await cur.execute(
                    f"SELECT {_RUN_COLUMNS} FROM intel_runs"
                    " ORDER BY created_at DESC, run_id DESC LIMIT %s",
                    (limit,),
                )
            else:
                await cur.execute(
                    f"SELECT {_RUN_COLUMNS} FROM intel_runs"
                    " WHERE (created_at, run_id) < (%s, %s)"
                    " ORDER BY created_at DESC, run_id DESC LIMIT %s",
                    (cursor[0], cursor[1], limit),
                )
            rows = await cur.fetchall()
        return [Run.model_validate(row) for row in rows]

    async def put_vocabulary(
        self,
        *,
        release_no: int,
        map_version: int,
        scoring_version: str,
        quiz_version: str,
        document: dict[str, Any],
    ) -> bool:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                _PUT_VOCAB_SQL,
                {
                    "release_no": release_no,
                    "map_version": map_version,
                    "scoring_version": scoring_version,
                    "quiz_version": quiz_version,
                    "document": Jsonb(document),
                },
            )
            applied = await cur.fetchone() is not None
            if applied:
                await conn.execute(
                    _AUDIT_SQL,
                    {
                        "actor_type": "service",
                        "action": "intel.vocabulary_received",
                        "resource_id": None,
                        "metadata": Jsonb({"release_no": release_no, "map_version": map_version}),
                    },
                )
        return applied

    async def load_vocabulary(self) -> Vocabulary | None:
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                "SELECT release_no, map_version, scoring_version, quiz_version, document"
                " FROM intel_vocabulary WHERE id = 1"
            )
            row = await cur.fetchone()
        return Vocabulary.model_validate(row) if row is not None else None

    async def is_known_hit(self, url_hash_value: str) -> bool:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT 1 FROM content_urls WHERE url_hash = %s", (url_hash_value,)
            )
            return await cur.fetchone() is not None

    async def spend_today(self, now: datetime) -> SpendToday:
        spend_date = utc_spend_date(now)
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT p.daily_budget_usd, s.call_count, s.cost_usd FROM providers p"
                " LEFT JOIN provider_spend s ON s.provider_id = p.provider_id AND s.spend_date = %s"
                " WHERE p.provider_id = %s",
                (spend_date, CLAUDE_INTEL),
            )
            row = await cur.fetchone()
        budget, calls, cost = row if row is not None else (None, None, None)
        return SpendToday(
            spend_date=spend_date,
            call_count=calls or 0,
            spent_today_usd=cost or Decimal("0"),
            daily_budget_usd=budget,
        )
