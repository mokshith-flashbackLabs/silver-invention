"""Persistence for seeds, runs, provider calls, and matches — raw SQL, no ORM.

Load-bearing choices:

- ``create_run`` writes the run row and its outbox row **in one
  transaction** (the step-2 pattern; ``imageshield.outbox.enqueue`` never
  commits). Discovery is never synchronous.
- ``claim_run`` is the idempotency gate under at-least-once delivery:
  queued → running exactly once, completed never re-executes, and a
  'running' claim older than ``_STALE_CLAIM_MINUTES`` is reclaimable (a
  worker died mid-run; SQS redelivered).
- ``record_matches`` writes ``band = 'review'`` unconditionally. No
  calibration exists yet (step 7), and an uncalibrated provider must not be
  able to tell someone their face was found without a human looking first.
- The url_hash is the step-5 interim raw hash (``search/urlhash.py``); the
  unique index ``(run_id, url_hash, provider_id)`` + ``ON CONFLICT DO
  NOTHING`` makes duplicate deliveries and repeated provider entries
  harmless.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from imageshield.outbox import QUEUE_SEARCH_RUNS, OutboxPayload, enqueue
from imageshield.search.models import (
    ClaimedRun,
    MatchRow,
    ProviderDescriptor,
    RunRow,
    SeedRow,
)
from imageshield.search.provider import ProviderMatch, ProviderResult
from imageshield.search.urlhash import interim_url_hash, source_domain
from imageshield.types import ProviderId, UserRef, parse_provider_id, parse_user_ref

RUN_REQUESTED_EVENT = "search.run_requested"

# A 'running' claim older than this is reclaimable: the worker that held it
# is presumed dead and SQS has redelivered the message. Must be shorter than
# the queue's visibility-timeout-times-maxReceive budget and comfortably
# longer than a real run (two provider calls, ≤ provider_timeout_seconds).
_STALE_CLAIM_MINUTES = 15

# Step 5 has no calibration; the run records WHY everything lands in
# 'review' so historical rows stay interpretable when step 7 changes this.
_THRESHOLD_CONFIG_V1 = {"band": "review", "reason": "uncalibrated_v1"}

_CREATE_SEED_SQL = """
    INSERT INTO search_seeds (user_ref, seed_kind, source_object_uri)
    VALUES (%(user_ref)s, %(seed_kind)s, %(source_object_uri)s)
    RETURNING seed_id
"""

_GET_SEED_SQL = """
    SELECT seed_id, user_ref, seed_kind, source_object_uri, status, created_at
    FROM search_seeds WHERE seed_id = %(seed_id)s
"""

_CREATE_RUN_SQL = """
    INSERT INTO search_runs (seed_id, user_ref, providers_attempted, threshold_config)
    VALUES (%(seed_id)s, %(user_ref)s, %(providers)s, %(threshold_config)s)
    RETURNING run_id
"""

_GET_RUN_SQL = """
    SELECT run_id, seed_id, user_ref, status, providers_attempted,
           providers_succeeded, matches_found, started_at, completed_at
    FROM search_runs WHERE run_id = %(run_id)s
"""

_CLAIM_RUN_SQL = f"""
    UPDATE search_runs r
    SET status = 'running', claimed_at = now()
    FROM search_seeds s
    WHERE r.run_id = %(run_id)s
      AND s.seed_id = r.seed_id
      AND (r.status = 'queued'
           OR (r.status = 'running'
               AND r.claimed_at < now() - interval '{_STALE_CLAIM_MINUTES} minutes'))
    RETURNING r.run_id, r.user_ref, s.source_object_uri, r.providers_attempted
"""

_ENABLED_PROVIDERS_SQL = "SELECT provider_id FROM providers WHERE enabled ORDER BY provider_id"

_RECORD_CALL_SQL = """
    INSERT INTO provider_calls (run_id, provider_id, status, http_status,
                                latency_ms, raw_response)
    VALUES (%(run_id)s, %(provider_id)s, %(status)s, %(http_status)s,
            %(latency_ms)s, %(raw_response)s)
"""

_UPSERT_URL_SQL = """
    INSERT INTO content_urls (url_hash, url, source_domain)
    VALUES (%(url_hash)s, %(url)s, %(source_domain)s)
    ON CONFLICT (url_hash) DO UPDATE SET last_seen_at = now()
"""

_INSERT_MATCH_SQL = """
    INSERT INTO search_matches (run_id, url_hash, user_ref, provider_id,
                                image_url, page_url, provider_score, score_version,
                                band, score_kind, provider_category, query_quality)
    VALUES (%(run_id)s, %(url_hash)s, %(user_ref)s, %(provider_id)s,
            %(image_url)s, %(page_url)s, %(provider_score)s, %(score_version)s,
            'review', %(score_kind)s, %(provider_category)s, %(query_quality)s)
    ON CONFLICT (run_id, url_hash, provider_id) DO NOTHING
"""

_COMPLETE_RUN_SQL = """
    UPDATE search_runs
    SET status = 'completed',
        providers_succeeded = %(providers_succeeded)s,
        matches_found = (SELECT count(*) FROM search_matches
                         WHERE run_id = %(run_id)s),
        completed_at = now()
    WHERE run_id = %(run_id)s
"""

_LIST_MATCHES_SQL = """
    SELECT match_id, run_id, provider_id, image_url, page_url, score_kind,
           provider_score, provider_category, query_quality, band, created_at
    FROM search_matches
    WHERE user_ref = %(user_ref)s
      AND (%(since)s::timestamptz IS NULL OR created_at >= %(since)s)
    ORDER BY created_at DESC, match_id
"""


class SearchStore(Protocol):
    async def create_seed(
        self, user_ref: UserRef, seed_kind: str, source_object_uri: str
    ) -> UUID: ...

    async def get_seed(self, seed_id: UUID) -> SeedRow | None: ...

    async def create_run(
        self, user_ref: UserRef, seed_id: UUID, providers_attempted: Sequence[ProviderId]
    ) -> UUID: ...

    async def get_run(self, run_id: UUID) -> RunRow | None: ...

    async def claim_run(self, run_id: UUID) -> ClaimedRun | None: ...

    async def enabled_provider_ids(self) -> tuple[ProviderId, ...]: ...

    async def record_provider_call(self, run_id: UUID, result: ProviderResult) -> None: ...

    async def record_matches(
        self,
        run_id: UUID,
        user_ref: UserRef,
        provider: ProviderDescriptor,
        matches: Sequence[ProviderMatch],
    ) -> int: ...

    async def complete_run(
        self, run_id: UUID, providers_succeeded: Sequence[ProviderId]
    ) -> None: ...

    async def list_matches(
        self, user_ref: UserRef, since: datetime | None
    ) -> tuple[MatchRow, ...]: ...


class PostgresSearchStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def create_seed(
        self, user_ref: UserRef, seed_kind: str, source_object_uri: str
    ) -> UUID:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                _CREATE_SEED_SQL,
                {
                    "user_ref": user_ref,
                    "seed_kind": seed_kind,
                    "source_object_uri": source_object_uri,
                },
            )
            row = await cur.fetchone()
        assert row is not None
        seed_id: UUID = row[0]
        return seed_id

    async def get_seed(self, seed_id: UUID) -> SeedRow | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_GET_SEED_SQL, {"seed_id": seed_id})
            row = await cur.fetchone()
        if row is None:
            return None
        return SeedRow(
            seed_id=row[0],
            user_ref=parse_user_ref(row[1]),
            seed_kind=row[2],
            source_object_uri=row[3],
            status=row[4],
            created_at=row[5],
        )

    async def create_run(
        self, user_ref: UserRef, seed_id: UUID, providers_attempted: Sequence[ProviderId]
    ) -> UUID:
        """Run row + outbox row, one transaction (never synchronous — the
        step-2 outbox is the only dispatch path)."""
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                _CREATE_RUN_SQL,
                {
                    "seed_id": seed_id,
                    "user_ref": user_ref,
                    "providers": list(providers_attempted),
                    "threshold_config": Jsonb(_THRESHOLD_CONFIG_V1),
                },
            )
            row = await cur.fetchone()
            assert row is not None
            run_id: UUID = row[0]
            await enqueue(
                conn,
                QUEUE_SEARCH_RUNS,
                OutboxPayload(event=RUN_REQUESTED_EVENT, id=run_id),
            )
        return run_id

    async def get_run(self, run_id: UUID) -> RunRow | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_GET_RUN_SQL, {"run_id": run_id})
            row = await cur.fetchone()
        if row is None:
            return None
        return RunRow(
            run_id=row[0],
            seed_id=row[1],
            user_ref=parse_user_ref(row[2]),
            status=row[3],
            providers_attempted=tuple(row[4]),
            providers_succeeded=tuple(row[5]),
            matches_found=row[6],
            started_at=row[7],
            completed_at=row[8],
        )

    async def claim_run(self, run_id: UUID) -> ClaimedRun | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_CLAIM_RUN_SQL, {"run_id": run_id})
            row = await cur.fetchone()
        if row is None:
            return None
        return ClaimedRun(
            run_id=row[0],
            user_ref=parse_user_ref(row[1]),
            seed_url=row[2],
            providers_attempted=tuple(parse_provider_id(p) for p in row[3]),
        )

    async def enabled_provider_ids(self) -> tuple[ProviderId, ...]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_ENABLED_PROVIDERS_SQL)
            rows = await cur.fetchall()
        return tuple(parse_provider_id(row[0]) for row in rows)

    async def record_provider_call(self, run_id: UUID, result: ProviderResult) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                _RECORD_CALL_SQL,
                {
                    "run_id": run_id,
                    "provider_id": result.provider_id,
                    "status": result.status,
                    "http_status": result.http_status,
                    "latency_ms": result.latency_ms,
                    "raw_response": Jsonb(result.raw_response),
                },
            )

    async def record_matches(
        self,
        run_id: UUID,
        user_ref: UserRef,
        provider: ProviderDescriptor,
        matches: Sequence[ProviderMatch],
    ) -> int:
        inserted = 0
        async with self._pool.connection() as conn, conn.transaction():
            for match in matches:
                url_hash = interim_url_hash(match.image_url)
                await conn.execute(
                    _UPSERT_URL_SQL,
                    {
                        "url_hash": url_hash,
                        "url": match.image_url,
                        "source_domain": source_domain(match.image_url),
                    },
                )
                cur = await conn.execute(
                    _INSERT_MATCH_SQL,
                    {
                        "run_id": run_id,
                        "url_hash": url_hash,
                        "user_ref": user_ref,
                        "provider_id": provider.provider_id,
                        "image_url": match.image_url,
                        "page_url": match.page_url,
                        "provider_score": match.provider_score,
                        "score_version": provider.score_version,
                        "score_kind": provider.score_kind,
                        "provider_category": match.provider_category,
                        "query_quality": match.query_quality,
                    },
                )
                inserted += cur.rowcount
        return inserted

    async def complete_run(
        self, run_id: UUID, providers_succeeded: Sequence[ProviderId]
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                _COMPLETE_RUN_SQL,
                {"run_id": run_id, "providers_succeeded": list(providers_succeeded)},
            )

    async def list_matches(
        self, user_ref: UserRef, since: datetime | None
    ) -> tuple[MatchRow, ...]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                _LIST_MATCHES_SQL, {"user_ref": user_ref, "since": since}
            )
            rows = await cur.fetchall()
        return tuple(_to_match_row(row) for row in rows)


def _to_match_row(row: tuple[Any, ...]) -> MatchRow:
    return MatchRow(
        match_id=row[0],
        run_id=row[1],
        provider_id=row[2],
        image_url=row[3],
        page_url=row[4],
        score_kind=row[5],
        provider_score=row[6],
        provider_category=row[7],
        query_quality=row[8],
        band=row[9],
        created_at=row[10],
    )
