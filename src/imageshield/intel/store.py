"""The intel registry and run queue (spec §3.2, §3.4, §3.8). The one writer of
intel_sources, intel_runs and intel_vocabulary, and of the run log's intel_run_events (migration
0046, spec 2026-10-03 §3.3: written through ``intel/run_log.py``'s ``RunLog``).

Every write that is an operator act writes its ``audit_log`` row in the same transaction
(``actor_type 'operator'``, ``metadata.operator``); a machine write (the vocabulary push)
uses ``actor_type 'service'``. Nothing here ever ``DELETE``s — ``intel_rw`` holds no
DELETE grant on any of these tables (migration 0039), so a bug that tried would fail at
the database rather than merely at review.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

import structlog
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from imageshield.intel.bounds import MAX_RUN_ATTEMPTS
from imageshield.intel.models import (
    Run,
    RunEvent,
    RunEvents,
    Source,
    SourcePause,
    SpendToday,
    Vocabulary,
)
from imageshield.intel.news_watch import (
    NEWS_WATCH_CHECK_EVERY_HOURS,
    NEWS_WATCH_CREATED_BY,
    watches,
)
from imageshield.intel.vocabulary import parse_vocabulary
from imageshield.providers.store import utc_spend_date
from imageshield.search.urlhash import NORMALISATION_VERSION, canonicalise, url_hash

log = structlog.get_logger("imageshield.intel")

CLAUDE_INTEL = "claude_intel"

# Public: intel/question_store.py reads the same rows.
SOURCE_COLUMNS = """source_id, kind, source_url, url_hash, query_text, tags, check_every_hours,
    next_check_at, enabled, terms_note, last_content_sha256, last_checked_at, last_run_status,
    consecutive_failures, disabled_reason, created_by, created_at, origin, proposed_for,
    validation_run_id, validated_at"""
RUN_COLUMNS = """run_id, kind, source_id, request, status, attempts, requested_by, outcome,
    error_code, created_at, completed_at, awaiting_source_ids, wait_deadline"""

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
        SELECT q.run_id FROM intel_runs q
         WHERE q.attempts < %(max_attempts)s
           AND (q.status = 'queued' OR (q.status = 'running' AND q.lease_expires_at <= %(now)s))
           -- A waiting suggestion (0050): not before its retry time, and only once none of the
           -- sources it awaits has a read open, or its deadline has passed.
           AND (q.not_before IS NULL OR q.not_before <= %(now)s)
           AND (q.awaiting_source_ids IS NULL OR q.wait_deadline <= %(now)s
                OR NOT EXISTS (SELECT 1 FROM intel_runs a
                                WHERE a.source_id = ANY(q.awaiting_source_ids)
                                  AND a.status IN ('queued', 'running')))
         ORDER BY q.created_at
         FOR UPDATE OF q SKIP LOCKED
         LIMIT 1)
    RETURNING {RUN_COLUMNS}
"""

# A weight suggestion gives its claim back and waits (spec 2026-10-08 §2): 'queued' again with
# its awaited sources, deadline and retry time, and the claim's attempt returned, so waiting
# never uses one up. Guarded on the claim exactly like finish_run.
_WAIT_SQL = """
    UPDATE intel_runs SET status = 'queued', attempts = attempts - 1, outcome = %(outcome)s,
           awaiting_source_ids = %(awaiting)s, wait_deadline = %(deadline)s,
           not_before = %(not_before)s, lease_expires_at = NULL
     WHERE run_id = %(run_id)s AND status = 'running' AND attempts = %(attempts)s
    RETURNING 1
"""

_EXPIRE_SQL = """
    UPDATE intel_runs SET status = 'failed', error_code = 'attempts_exhausted',
           completed_at = %(now)s
     WHERE status = 'running' AND lease_expires_at <= %(now)s AND attempts >= %(max_attempts)s
"""

# The holder's heartbeat (spec 2026-10-03-intel-throughput §2). Guarded on the claim that is
# renewing: a run another claimer has since taken (attempts moved on) or that expire_exhausted
# ended (status moved on) is not this holder's to extend. An expired lease nobody took yet IS
# still this holder's, so it is extended rather than lost.
_RENEW_LEASE_SQL = """
    UPDATE intel_runs SET lease_expires_at = %(now)s + make_interval(secs => %(lease)s)
     WHERE run_id = %(run_id)s AND status = 'running' AND attempts = %(attempts)s
    RETURNING 1
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

# An operator's own disable clears 'unmapped', so the pause pass never re-enables what an
# operator turned off (spec §4.10). Every field is coalesced, so a PATCH may set a terms note but
# never clear one (optional since 0047, 2026-10-03): null leaves the stored note as it is.
_PATCH_SOURCE_SQL = f"""
    UPDATE intel_sources SET
        enabled = coalesce(%(enabled)s::boolean, enabled),
        disabled_reason = CASE
            WHEN %(enabled)s::boolean IS TRUE THEN NULL
            WHEN %(enabled)s::boolean IS FALSE AND disabled_reason = 'unmapped' THEN NULL
            ELSE disabled_reason END,
        consecutive_failures = CASE WHEN %(enabled)s::boolean IS TRUE THEN 0
                                    ELSE consecutive_failures END,
        check_every_hours = coalesce(%(check_every_hours)s::int, check_every_hours),
        tags = coalesce(%(tags)s::text[], tags),
        terms_note = coalesce(%(terms_note)s::text, terms_note),
        query_text = coalesce(%(query_text)s::text, query_text),
        updated_at = now()
     WHERE source_id = %(source_id)s
    RETURNING {SOURCE_COLUMNS}
"""

# The daily news watch (spec 2026-10-09-intel-news-watch-design §2.2): one saved search per
# mapped platform tag. 0052's partial unique index makes this idempotent, and a watch an operator
# disabled or edited is never re-created or reset, because its row still exists.
_ENSURE_NEWS_WATCHES_SQL = """
    INSERT INTO intel_sources (kind, query_text, tags, check_every_hours, created_by, origin)
    SELECT 'search_query', w.query_text, ARRAY[w.tag], %(every)s, %(by)s, 'news_watch'
      FROM unnest(%(tags)s::text[], %(queries)s::text[]) AS w(tag, query_text)
    ON CONFLICT ((tags[1])) WHERE origin = 'news_watch' DO NOTHING
    RETURNING source_id, tags[1] AS tag
"""

# spec §4.9, §4.10: a source whose NON-EMPTY tags are all unmapped in the live vocabulary pauses;
# one paused that way resumes when any of its tags is mapped again, or when its tags are cleared
# (an untagged source never pauses). A source disabled for any other reason, or by an operator
# (which leaves disabled_reason NULL), is never touched.
_PAUSE_UNMAPPED_SQL = """
    UPDATE intel_sources SET enabled = false, disabled_reason = 'unmapped', updated_at = now()
     WHERE enabled AND cardinality(tags) > 0 AND NOT (tags && %(mapped)s::text[])
    RETURNING source_id
"""

_RESUME_MAPPED_SQL = """
    UPDATE intel_sources SET enabled = true, disabled_reason = NULL, updated_at = now()
     WHERE NOT enabled AND disabled_reason = 'unmapped'
       AND (cardinality(tags) = 0 OR tags && %(mapped)s::text[])
    RETURNING source_id
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
        terms_note: str | None,
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
    async def renew_lease(
        self, run_id: UUID, *, attempts: int, now: datetime, lease_seconds: int
    ) -> bool: ...
    async def expire_exhausted(self, now: datetime) -> int: ...
    async def finish_run(
        self,
        run_id: UUID,
        *,
        status: str,
        outcome: dict[str, Any],
        error_code: str | None = None,
        attempts: int | None = None,
    ) -> bool: ...
    async def wait_run(
        self,
        run_id: UUID,
        *,
        attempts: int,
        outcome: dict[str, Any],
        awaiting: Sequence[UUID],
        deadline: datetime,
        not_before: datetime | None,
    ) -> bool: ...
    async def set_run_vocabulary(
        self, run_id: UUID, *, release_no: int, map_version: int
    ) -> None: ...
    async def list_runs(
        self,
        *,
        cursor: tuple[datetime, UUID] | None,
        limit: int,
        kinds: Sequence[str] | None = None,
        statuses: Sequence[str] | None = None,
        question_key: str | None = None,
    ) -> list[Run]: ...
    async def run_events(self, run_id: UUID) -> RunEvents | None: ...
    async def last_run_event_seq(self, run_id: UUID) -> int: ...
    async def insert_run_event(
        self, run_id: UUID, *, seq: int, kind: str, text: str, detail: Mapping[str, Any]
    ) -> None: ...
    async def update_run_event(
        self, run_id: UUID, *, seq: int, text: str, detail: Mapping[str, Any]
    ) -> None: ...
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
    async def pause_unmapped_sources(self) -> SourcePause: ...
    async def ensure_news_watches(self) -> tuple[UUID, ...]: ...
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
        terms_note: str | None,
        operator: str,
    ) -> Source:
        """A source registered on the Sources screen: never validated, so its 0047 evidence
        (``validation_run_id``, ``validated_at``) stays null, and its terms note may be null."""
        canonical = canonicalise(source_url) if source_url is not None else None
        async with self._pool.connection() as conn, conn.transaction():
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"""INSERT INTO intel_sources (kind, source_url, url_hash, normalisation_version,
                        query_text, tags, check_every_hours, terms_note, created_by)
                    VALUES (%(kind)s, %(url)s, %(hash)s, %(nv)s, %(query)s, %(tags)s, %(every)s,
                            %(terms)s, %(operator)s)
                    RETURNING {SOURCE_COLUMNS}""",
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
                    f"SELECT {SOURCE_COLUMNS} FROM intel_sources"
                    " ORDER BY created_at DESC, source_id DESC LIMIT %s",
                    (limit,),
                )
            else:
                await cur.execute(
                    f"SELECT {SOURCE_COLUMNS} FROM intel_sources"
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
                f"SELECT {SOURCE_COLUMNS} FROM intel_sources WHERE source_id = %s", (source_id,)
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

    async def renew_lease(
        self, run_id: UUID, *, attempts: int, now: datetime, lease_seconds: int
    ) -> bool:
        """Extend a running run's lease to ``now + lease_seconds``, only for the claim that
        holds it (``attempts`` is that claim's). False: the run is no longer this holder's."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                _RENEW_LEASE_SQL,
                {"run_id": run_id, "attempts": attempts, "now": now, "lease": lease_seconds},
            )
            return await cur.fetchone() is not None

    async def expire_exhausted(self, now: datetime) -> int:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(_EXPIRE_SQL, {"now": now, "max_attempts": MAX_RUN_ATTEMPTS})
            count = cur.rowcount
        if count:
            log.error("intel.run_attempts_exhausted", count=count)
        return count

    async def wait_run(
        self,
        run_id: UUID,
        *,
        attempts: int,
        outcome: dict[str, Any],
        awaiting: Sequence[UUID],
        deadline: datetime,
        not_before: datetime | None,
    ) -> bool:
        """A weight suggestion gives its claim back until the reads it queued are done (spec
        2026-10-08-intel-suggestion-waits-for-evidence §2): 'queued' again, the attempt returned,
        guarded on the claim like ``finish_run``. Returns whether the row was written."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                _WAIT_SQL,
                {
                    "outcome": Jsonb(outcome),
                    "awaiting": list(awaiting),
                    "deadline": deadline,
                    "not_before": not_before,
                    "run_id": run_id,
                    "attempts": attempts,
                },
            )
            return await cur.fetchone() is not None

    async def finish_run(
        self,
        run_id: UUID,
        *,
        status: str,
        outcome: dict[str, Any],
        error_code: str | None = None,
        attempts: int | None = None,
    ) -> bool:
        """Write a run's result. With ``attempts`` (the worker always passes its claim's), only
        while that claim still holds the run, so a holder whose lease was taken over never writes
        over the newer holder's result. Returns whether the row was written."""
        guard = ""
        params: dict[str, Any] = {
            "status": status,
            "outcome": Jsonb(outcome),
            "error_code": error_code,
            "run_id": run_id,
        }
        if attempts is not None:
            guard = " AND status = 'running' AND attempts = %(attempts)s"
            params["attempts"] = attempts
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE intel_runs SET status = %(status)s, outcome = %(outcome)s,"
                " error_code = %(error_code)s, completed_at = now(), lease_expires_at = NULL"
                f" WHERE run_id = %(run_id)s{guard}",
                params,
            )
            return cur.rowcount > 0

    async def set_run_vocabulary(self, run_id: UUID, *, release_no: int, map_version: int) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE intel_runs SET vocabulary_release_no = %s, vocabulary_map_version = %s"
                " WHERE run_id = %s",
                (release_no, map_version, run_id),
            )

    async def list_runs(
        self,
        *,
        cursor: tuple[datetime, UUID] | None,
        limit: int,
        kinds: Sequence[str] | None = None,
        statuses: Sequence[str] | None = None,
        question_key: str | None = None,
    ) -> list[Run]:
        """Newest first, keyset-paged on ``(created_at, run_id)``. The filters combine with AND;
        an empty or absent one filters nothing (spec 2026-10-03 §3.6)."""
        where: list[str] = []
        params: dict[str, Any] = {"limit": limit}
        if kinds:
            where.append("kind = ANY(%(kinds)s)")
            params["kinds"] = list(kinds)
        if statuses:
            where.append("status = ANY(%(statuses)s)")
            params["statuses"] = list(statuses)
        if question_key is not None:
            # `request ? 'question_key'` is intel_runs_question_idx's own predicate (0046), stated
            # so the planner can prove the partial index applies.
            where.append(
                "request ? 'question_key' AND request ->> 'question_key' = %(question_key)s"
            )
            params["question_key"] = question_key
        if cursor is not None:
            where.append("(created_at, run_id) < (%(cursor_at)s, %(cursor_id)s)")
            params["cursor_at"], params["cursor_id"] = cursor
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"SELECT {RUN_COLUMNS} FROM intel_runs{clause}"
                " ORDER BY created_at DESC, run_id DESC LIMIT %(limit)s",
                params,
            )
            rows = await cur.fetchall()
        return [Run.model_validate(row) for row in rows]

    # -- the run log (migration 0046, intel/run_log.py) --------------------------------------

    async def run_events(self, run_id: UUID) -> RunEvents | None:
        """The run's kind and status, THEN its rows: ``run_finished`` is written before the run's
        terminal status, so a read that sees a terminal status always sees the complete log."""
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                "SELECT run_id, kind, status FROM intel_runs WHERE run_id = %s", (run_id,)
            )
            run = await cur.fetchone()
            if run is None:
                return None
            await cur.execute(
                "SELECT seq, kind, text, detail, at, updated_at FROM intel_run_events"
                " WHERE run_id = %s ORDER BY seq",
                (run_id,),
            )
            rows = await cur.fetchall()
        return RunEvents(
            run_id=run["run_id"],
            kind=run["kind"],
            status=run["status"],
            events=tuple(RunEvent.model_validate(row) for row in rows),
        )

    async def last_run_event_seq(self, run_id: UUID) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT coalesce(max(seq), 0) FROM intel_run_events WHERE run_id = %s", (run_id,)
            )
            row = await cur.fetchone()
        return int(row[0]) if row is not None else 0

    async def insert_run_event(
        self, run_id: UUID, *, seq: int, kind: str, text: str, detail: Mapping[str, Any]
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO intel_run_events (run_id, seq, kind, text, detail)"
                " VALUES (%s, %s, %s, %s, %s)",
                (run_id, seq, kind, text, Jsonb(dict(detail))),
            )

    async def update_run_event(
        self, run_id: UUID, *, seq: int, text: str, detail: Mapping[str, Any]
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE intel_run_events SET text = %s, detail = %s, updated_at = now()"
                " WHERE run_id = %s AND seq = %s",
                (text, Jsonb(dict(detail)), run_id, seq),
            )

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

    async def ensure_news_watches(self) -> tuple[UUID, ...]:
        """One daily news watch for every platform tag the live quiz maps (spec
        2026-10-09-intel-news-watch-design §2.2), created by code on a worker tick. Returns the
        watches created now. With no vocabulary, or one that cannot be read, nothing is created."""
        async with self._pool.connection() as conn, conn.transaction():
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                "SELECT release_no, map_version, scoring_version, quiz_version, document"
                " FROM intel_vocabulary WHERE id = 1"
            )
            row = await cur.fetchone()
            vocabulary = (
                parse_vocabulary(Vocabulary.model_validate(row)) if row is not None else None
            )
            if vocabulary is None:
                return ()
            wanted = watches(vocabulary)
            if not wanted:
                return ()
            created = await conn.execute(
                _ENSURE_NEWS_WATCHES_SQL,
                {
                    "every": NEWS_WATCH_CHECK_EVERY_HOURS,
                    "by": NEWS_WATCH_CREATED_BY,
                    "tags": [w.tag for w in wanted],
                    "queries": [w.query_text for w in wanted],
                },
            )
            rows = await created.fetchall()
            if rows:
                await conn.execute(
                    _AUDIT_SQL,
                    {
                        "actor_type": "service",
                        "action": "intel.news_watch_created",
                        "resource_id": None,
                        "metadata": Jsonb(
                            {
                                "release_no": vocabulary.release_no,
                                "map_version": vocabulary.map_version,
                                "sources": [str(r[0]) for r in rows],
                                "tags": [r[1] for r in rows],
                            }
                        ),
                    },
                )
        if rows:
            log.info("intel.news_watch_created", tags=[r[1] for r in rows])
        return tuple(r[0] for r in rows)

    async def pause_unmapped_sources(self) -> SourcePause:
        """spec §4.9's source row, STATE-BASED: run on every worker tick before scheduling, it
        compares every source's tags with what the live quiz maps NOW, so a source registered or
        re-tagged since the last push follows the quiz too. With no vocabulary, or one that cannot
        be read, nothing moves: what is mapped is unknown then."""
        async with self._pool.connection() as conn, conn.transaction():
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                "SELECT release_no, map_version, scoring_version, quiz_version, document"
                " FROM intel_vocabulary WHERE id = 1"
            )
            row = await cur.fetchone()
            vocabulary = (
                parse_vocabulary(Vocabulary.model_validate(row)) if row is not None else None
            )
            if vocabulary is None:
                return SourcePause()
            mapped = sorted(vocabulary.mapped_tags)
            moved = await conn.execute(_PAUSE_UNMAPPED_SQL, {"mapped": mapped})
            paused = tuple(r[0] for r in await moved.fetchall())
            moved = await conn.execute(_RESUME_MAPPED_SQL, {"mapped": mapped})
            resumed = tuple(r[0] for r in await moved.fetchall())
            if paused or resumed:
                await conn.execute(
                    _AUDIT_SQL,
                    {
                        "actor_type": "service",
                        "action": "intel.sources_followed_quiz",
                        "resource_id": None,
                        "metadata": Jsonb(
                            {
                                "release_no": vocabulary.release_no,
                                "map_version": vocabulary.map_version,
                                "paused": [str(i) for i in paused],
                                "resumed": [str(i) for i in resumed],
                            }
                        ),
                    },
                )
        if paused or resumed:
            log.info("intel.sources_followed_quiz", paused=len(paused), resumed=len(resumed))
        return SourcePause(paused=paused, resumed=resumed)
