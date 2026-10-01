"""The question runs' database (spec §4.10, §4.6): queueing the three run kinds, registering chosen
sources, the suggestion's retrieval and write, and the reads behind the three polls.

Every operator write (a queued run, a registered source) writes its audit_log row in the same
transaction (actor_type 'operator', metadata.operator); the worker's own writes audit as
'service'. Nothing here DELETEs: intel_rw holds no DELETE grant (0039). A weight_suggestion is born
'delivered', and statuses are SQL LITERALS here, never parameters (tests/test_boundaries.py).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol
from uuid import UUID

import structlog
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from imageshield.intel.models import Run, Source
from imageshield.intel.store import RUN_COLUMNS, SOURCE_COLUMNS

log = structlog.get_logger("imageshield.intel")

_AUDIT_SQL = """
    INSERT INTO audit_log (actor_type, action, resource_id, metadata)
    VALUES (%(actor_type)s, %(action)s, %(resource_id)s, %(metadata)s)
"""


class QuestionStore(Protocol):
    async def queue_source_proposal(self, request: dict[str, Any], *, operator: str) -> UUID: ...
    async def queue_source_validation(self, request: dict[str, Any], *, operator: str) -> UUID: ...
    async def get_run(self, run_id: UUID) -> Run | None: ...
    async def sources_by_ids(self, source_ids: Sequence[UUID]) -> list[Source]: ...
    async def sources_with_tags(self, tags: Sequence[str]) -> list[Source]: ...
    async def known_hits(self, url_hashes: Sequence[str]) -> frozenset[str]: ...


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
