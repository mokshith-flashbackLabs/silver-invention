"""The question runs' database (spec §4.10, §4.6): queueing the three run kinds, registering chosen
sources, the suggestion's retrieval and write, and the reads behind the three polls.

Every operator write (a queued run, a registered source) writes its audit_log row in the same
transaction (actor_type 'operator', metadata.operator); the worker's own writes audit as
'service'. Nothing here DELETEs: intel_rw holds no DELETE grant (0039). A weight_suggestion is born
'delivered', and statuses are SQL LITERALS here, never parameters (tests/test_boundaries.py).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

import structlog
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from imageshield.intel.models import Run, Source
from imageshield.intel.proposal_models import ContextSignal, WriteResult
from imageshield.intel.proposal_store import _CONTEXT_COLUMNS, _CONTEXT_FROM, _context
from imageshield.intel.source_choice import (
    NewSource,
    Registered,
    Validation,
    proposal_identities,
    query_key,
    ready_keys,
)
from imageshield.intel.store import RUN_COLUMNS, SOURCE_COLUMNS
from imageshield.intel.suggestion import CATEGORY_CLASS, OptionSuggestion, SuggestionCandidates
from imageshield.search.urlhash import NORMALISATION_VERSION, url_hash

log = structlog.get_logger("imageshield.intel")

_AUDIT_SQL = """
    INSERT INTO audit_log (actor_type, action, resource_id, metadata)
    VALUES (%(actor_type)s, %(action)s, %(resource_id)s, %(metadata)s)
"""

_REGISTER_SQL = """
    INSERT INTO intel_sources (kind, source_url, url_hash, normalisation_version, query_text, tags,
        check_every_hours, next_check_at, terms_note, created_by, origin, proposed_for)
    VALUES (%(kind)s, %(url)s, %(hash)s, %(nv)s, %(query)s, %(tags)s, %(every)s,
            now() + make_interval(hours => %(every)s), %(terms)s, %(operator)s, %(origin)s,
            %(proposed_for)s)
    ON CONFLICT (url_hash) WHERE url_hash IS NOT NULL DO NOTHING
    RETURNING source_id
"""

_SUPERSEDE_SUGGESTION_SQL = """
    UPDATE intel_proposals SET status = 'superseded', supersede_reason = 'newer_proposal'
     WHERE kind = 'weight_suggestion' AND status = 'delivered'
       AND target->>'question_key' = %s
    RETURNING proposal_id
"""

_INSERT_SUGGESTION_SQL = """
    INSERT INTO intel_proposals (kind, status, target, suggested, rationale,
        against_scoring_version, against_release_no, run_id, model_id, prompt_version)
    VALUES ('weight_suggestion', 'delivered', %(target)s, '{}'::jsonb, '', %(asv)s, %(arn)s,
            %(run_id)s, %(model_id)s, %(prompt_version)s)
    RETURNING proposal_id
"""

# The four retrieval classes (spec §4.6). intel/suggestion.py orders, filters and bounds them.
_CANDIDATE_CLASSES = (
    "d.source_id = ANY(%(sources)s::uuid[])",
    "s.tags && %(tags)s::text[]",
    "cardinality(s.unregistered_subjects) > 0",
    "s.category = ANY(%(categories)s::text[])",
)


class QuestionStore(Protocol):
    async def queue_source_proposal(self, request: dict[str, Any], *, operator: str) -> UUID: ...
    async def queue_source_validation(self, request: dict[str, Any], *, operator: str) -> UUID: ...
    async def get_run(self, run_id: UUID) -> Run | None: ...
    async def sources_by_ids(self, source_ids: Sequence[UUID]) -> list[Source]: ...
    async def sources_with_tags(self, tags: Sequence[str]) -> list[Source]: ...
    async def known_hits(self, url_hashes: Sequence[str]) -> frozenset[str]: ...
    async def validations(self, run_ids: Sequence[UUID]) -> dict[UUID, Validation]: ...
    async def proposed_identities(
        self, question_key: str, *, since: datetime
    ) -> frozenset[str]: ...
    async def register_and_queue_suggestion(
        self, request: dict[str, Any], sources: Sequence[NewSource], *, operator: str
    ) -> Registered: ...
    async def mark_sources_due(self, source_ids: Sequence[UUID], *, now: datetime) -> None: ...
    async def suggestion_candidates(
        self, *, source_ids: Sequence[UUID], tags: Sequence[str], since: datetime, limit: int
    ) -> SuggestionCandidates: ...
    async def suggestion_written(self, run_id: UUID) -> bool: ...
    async def write_suggestion(
        self,
        run_id: UUID,
        *,
        question_key: str,
        options: Sequence[OptionSuggestion],
        against_scoring_version: str,
        against_release_no: int,
        model_id: str,
        prompt_version: str,
    ) -> WriteResult | None: ...


class PostgresQuestionStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def _queue(
        self, kind: str, request: dict[str, Any], *, operator: str, metadata: dict[str, Any]
    ) -> UUID:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "INSERT INTO intel_runs (kind, request, requested_by) VALUES (%s, %s, %s)"
                " RETURNING run_id",
                (kind, Jsonb(request), operator),
            )
            row = await cur.fetchone()
            assert row is not None
            run_id: UUID = row[0]
            await conn.execute(
                _AUDIT_SQL,
                {
                    "actor_type": "operator",
                    "action": f"intel.{kind}_queued",
                    "resource_id": run_id,
                    "metadata": Jsonb({"operator": operator, **metadata}),
                },
            )
        return run_id

    async def queue_source_proposal(self, request: dict[str, Any], *, operator: str) -> UUID:
        return await self._queue(
            "source_proposal",
            request,
            operator=operator,
            metadata={"question_key": request.get("question_key")},
        )

    async def queue_source_validation(self, request: dict[str, Any], *, operator: str) -> UUID:
        return await self._queue(
            "source_validation",
            request,
            operator=operator,
            metadata={"candidates": len(request.get("candidates", []))},
        )

    async def get_run(self, run_id: UUID) -> Run | None:
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(f"SELECT {RUN_COLUMNS} FROM intel_runs WHERE run_id = %s", (run_id,))
            row = await cur.fetchone()
        return Run.model_validate(row) if row is not None else None

    async def sources_by_ids(self, source_ids: Sequence[UUID]) -> list[Source]:
        if not source_ids:
            return []
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"SELECT {SOURCE_COLUMNS} FROM intel_sources WHERE source_id = ANY(%s::uuid[])",
                (list(source_ids),),
            )
            rows = await cur.fetchall()
        return [Source.model_validate(row) for row in rows]

    async def sources_with_tags(self, tags: Sequence[str]) -> list[Source]:
        """Registry sources whose tags intersect ``tags``, enabled ones first (spec §4.10)."""
        if not tags:
            return []
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"SELECT {SOURCE_COLUMNS} FROM intel_sources WHERE tags && %s::text[]"
                " ORDER BY enabled DESC, created_at, source_id",
                (list(tags),),
            )
            rows = await cur.fetchall()
        return [Source.model_validate(row) for row in rows]

    async def known_hits(self, url_hashes: Sequence[str]) -> frozenset[str]:
        """Which of ``url_hashes`` are known hit locations (spec §6.1). intel_rw reads only this
        column of content_urls."""
        if not url_hashes:
            return frozenset()
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT url_hash FROM content_urls WHERE url_hash = ANY(%s::text[])",
                (list(url_hashes),),
            )
            return frozenset(r[0] for r in await cur.fetchall())

    async def validations(self, run_ids: Sequence[UUID]) -> dict[UUID, Validation]:
        """Completed source_validation runs only: a queued run, or another kind's id, validates
        nothing, so it reads as ``unknown_run``."""
        if not run_ids:
            return {}
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT run_id, completed_at, outcome FROM intel_runs"
                " WHERE run_id = ANY(%s::uuid[]) AND kind = 'source_validation'"
                " AND status = 'completed' AND completed_at IS NOT NULL",
                (list(run_ids),),
            )
            rows = await cur.fetchall()
        return {r[0]: Validation(completed_at=r[1], ready=ready_keys(r[2])) for r in rows}

    async def proposed_identities(self, question_key: str, *, since: datetime) -> frozenset[str]:
        """Every candidate this question's completed stage-1 runs proposed since ``since``: how
        stage 4 knows a chosen source came from stage 1, with no field in its body."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT outcome FROM intel_runs WHERE kind = 'source_proposal'"
                " AND status = 'completed' AND request->>'question_key' = %s"
                " AND completed_at >= %s",
                (question_key, since),
            )
            rows = await cur.fetchall()
        found: set[str] = set()
        for (outcome,) in rows:
            found |= proposal_identities(outcome)
        return frozenset(found)

    async def register_and_queue_suggestion(
        self, request: dict[str, Any], sources: Sequence[NewSource], *, operator: str
    ) -> Registered:
        """spec §4.10 stage 4, in ONE transaction: register each chosen source, or reuse the row
        with the same url_hash or normalised query (never rewriting it), then queue the
        weight_suggestion run naming the sources it must read first (``new_source_ids``) and
        every source the request named (``source_ids``). A new source's first scheduled check
        is a full interval away, because the run reads it now. The advisory lock serialises two
        presses registering one query: url_hash has a unique index, query_text has none."""
        registered: list[UUID] = []
        reused: list[UUID] = []
        named: list[UUID] = []
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('intel_source_registration', 0))"
            )
            cur = await conn.execute(
                "SELECT source_id, query_text FROM intel_sources WHERE kind = 'search_query'"
            )
            queries: dict[str, UUID] = {query_key(q): sid for sid, q in await cur.fetchall()}
            for source in sources:
                source_id, created = await self._register(conn, source, queries, operator)
                if created:
                    registered.append(source_id)
                elif source_id not in registered and source_id not in reused:
                    reused.append(source_id)
                if source_id not in named:
                    named.append(source_id)
            stored = {
                **request,
                "new_source_ids": [str(i) for i in registered],
                "source_ids": [str(i) for i in named],
            }
            cur = await conn.execute(
                "INSERT INTO intel_runs (kind, request, requested_by)"
                " VALUES ('weight_suggestion', %s, %s) RETURNING run_id",
                (Jsonb(stored), operator),
            )
            row = await cur.fetchone()
            assert row is not None
            run_id: UUID = row[0]
            await conn.execute(
                _AUDIT_SQL,
                {
                    "actor_type": "operator",
                    "action": "intel.weight_suggestion_queued",
                    "resource_id": run_id,
                    "metadata": Jsonb(
                        {
                            "operator": operator,
                            "question_key": request.get("question_key"),
                            "registered": [str(i) for i in registered],
                            "reused": [str(i) for i in reused],
                        }
                    ),
                },
            )
        return Registered(run_id=run_id, registered=tuple(registered), reused=tuple(reused))

    async def _register(
        self,
        conn: AsyncConnection[tuple[Any, ...]],
        source: NewSource,
        queries: dict[str, UUID],
        operator: str,
    ) -> tuple[UUID, bool]:
        """(source_id, created): reuse by identity first, insert otherwise."""
        if source.query_text is not None:
            existing = queries.get(query_key(source.query_text))
            if existing is not None:
                return existing, False
        else:
            found = await self._by_url_hash(conn, source.source_url or "")
            if found is not None:
                return found, False
        cur = await conn.execute(
            _REGISTER_SQL,
            {
                "kind": source.kind,
                "url": source.source_url,
                "hash": url_hash(source.source_url) if source.source_url is not None else None,
                "nv": NORMALISATION_VERSION if source.source_url is not None else None,
                "query": source.query_text,
                "tags": list(source.tags),
                "every": source.check_every_hours,
                "terms": source.terms_note,
                "operator": operator,
                "origin": source.origin,
                "proposed_for": Jsonb(
                    {"question_key": source.question_key, "option": source.option}
                ),
            },
        )
        row = await cur.fetchone()
        if row is None:  # POST /sources registered the same URL a moment ago
            found = await self._by_url_hash(conn, source.source_url or "")
            assert found is not None
            return found, False
        source_id: UUID = row[0]
        if source.query_text is not None:
            queries[query_key(source.query_text)] = source_id
        await conn.execute(
            _AUDIT_SQL,
            {
                "actor_type": "operator",
                "action": "intel.source_created",
                "resource_id": source_id,
                "metadata": Jsonb(
                    {
                        "operator": operator,
                        "kind": source.kind,
                        "origin": source.origin,
                        "via": "weight_suggestion",
                    }
                ),
            },
        )
        return source_id, True

    @staticmethod
    async def _by_url_hash(conn: AsyncConnection[tuple[Any, ...]], url: str) -> UUID | None:
        cur = await conn.execute(
            "SELECT source_id FROM intel_sources WHERE url_hash = %s", (url_hash(url),)
        )
        row = await cur.fetchone()
        return row[0] if row is not None else None

    async def mark_sources_due(self, source_ids: Sequence[UUID], *, now: datetime) -> None:
        """A source the suggestion run could not finish reading: its first scheduled check comes
        on the next tick instead of a full interval later (spec §4.10). Only ever earlier."""
        if not source_ids:
            return
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE intel_sources SET next_check_at = %s, updated_at = now()"
                " WHERE source_id = ANY(%s::uuid[]) AND next_check_at > %s",
                (now, list(source_ids), now),
            )

    async def suggestion_candidates(
        self, *, source_ids: Sequence[UUID], tags: Sequence[str], since: datetime, limit: int
    ) -> SuggestionCandidates:
        """Each class: active signals since ``since``, newest first, at most ``limit``."""
        params = {
            "since": since,
            "limit": limit,
            "sources": list(source_ids),
            "tags": list(tags),
            "categories": sorted(CATEGORY_CLASS),
        }
        found: list[tuple[ContextSignal, ...]] = []
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            for clause in _CANDIDATE_CLASSES:
                await cur.execute(
                    f"SELECT {_CONTEXT_COLUMNS} FROM {_CONTEXT_FROM}"
                    f" WHERE s.status = 'active' AND s.created_at >= %(since)s AND {clause}"
                    " ORDER BY s.created_at DESC, s.signal_id DESC LIMIT %(limit)s",
                    params,
                )
                found.append(tuple(_context(row) for row in await cur.fetchall()))
        from_sources, by_tag, with_subjects, by_category = found
        return SuggestionCandidates(from_sources, by_tag, with_subjects, by_category)

    async def suggestion_written(self, run_id: UUID) -> bool:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT 1 FROM intel_proposals WHERE kind = 'weight_suggestion' AND run_id = %s",
                (run_id,),
            )
            return await cur.fetchone() is not None

    async def write_suggestion(
        self,
        run_id: UUID,
        *,
        question_key: str,
        options: Sequence[OptionSuggestion],
        against_scoring_version: str,
        against_release_no: int,
        model_id: str,
        prompt_version: str,
    ) -> WriteResult | None:
        """spec §3.6: born 'delivered', superseding the older delivered suggestion for the same
        question_key, in ONE transaction with its signal links and its audit row. The links are
        every option's cited signals, and there may be none: "no evidence, operator's call" is an
        answer (§3.6, note of 2026-09-30). None when this run already wrote one: a reclaimed run
        never writes twice. The advisory lock orders two suggestions for one question."""
        signal_ids: list[UUID] = []
        for option in options:
            signal_ids += [s for s in option.signal_ids if s not in signal_ids]
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended('intel_weight_suggestion:' || %s::text, 0))",
                (question_key,),
            )
            cur = await conn.execute(
                "SELECT 1 FROM intel_proposals WHERE kind = 'weight_suggestion' AND run_id = %s",
                (run_id,),
            )
            if await cur.fetchone() is not None:
                return None
            cur = await conn.execute(_SUPERSEDE_SUGGESTION_SQL, (question_key,))
            superseded = [r[0] for r in await cur.fetchall()]
            target = {"question_key": question_key, "options": [o.as_json() for o in options]}
            cur = await conn.execute(
                _INSERT_SUGGESTION_SQL,
                {
                    "target": Jsonb(target),
                    "asv": against_scoring_version,
                    "arn": against_release_no,
                    "run_id": run_id,
                    "model_id": model_id,
                    "prompt_version": prompt_version,
                },
            )
            row = await cur.fetchone()
            assert row is not None
            proposal_id: UUID = row[0]
            if signal_ids:
                await conn.execute(
                    "INSERT INTO intel_proposal_signals (proposal_id, signal_id)"
                    " SELECT %s, unnest(%s::uuid[])",
                    (proposal_id, signal_ids),
                )
            await conn.execute(
                _AUDIT_SQL,
                {
                    "actor_type": "service",
                    "action": "intel.weight_suggestion_written",
                    "resource_id": proposal_id,
                    "metadata": Jsonb(
                        {
                            "run_id": str(run_id),
                            "question_key": question_key,
                            "superseded": [str(i) for i in superseded],
                        }
                    ),
                },
            )
        return WriteResult((proposal_id,), tuple(superseded))
