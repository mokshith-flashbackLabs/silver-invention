"""Persistence for seeds, runs, provider calls, and matches — raw SQL, no ORM.

Load-bearing choices:

- ``create_run`` writes the run row and its outbox row **in one
  transaction** (the step-2 pattern; ``imageshield.outbox.enqueue`` never
  commits). Discovery is never synchronous.
- ``claim_run`` is the idempotency gate under at-least-once delivery:
  queued → running exactly once, completed never re-executes, and a
  'running' claim older than ``_STALE_CLAIM_MINUTES`` is reclaimable (a
  worker died mid-run; SQS redelivered).
- ``record_infringements`` bands each attestation through the calibration
  policy snapshot and rolls the infringement up from its attestations (step
  7). With no active config, or an uncalibrated provider, every band is still
  ``review`` — the state the repo ships in.
- Writes are **upserts, never appends** (step 6). One infringement per
  ``(user_ref, url_hash)``, one attestation per ``(infringement, provider)``,
  and a rescan that finds the same unchanged URL updates counters instead of
  inserting. Row count grows with content, not with time.
- The url_hash is normalisation v1 (``search/urlhash.py``), computed over the
  **page** where a provider reports one — the page is what a user acts on.

Two counters that are easy to confuse: ``seen_count`` counts provider
observations of an infringement (two providers in one run bump it twice), so
it answers "how often has anything seen this". ``confirm_count`` is
per-provider and is the clean per-provider signal.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, NamedTuple, Protocol
from uuid import UUID

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from imageshield.calibration.bands import band_for_attestation, roll_up
from imageshield.calibration.models import BandingPolicy
from imageshield.outbox import QUEUE_SEARCH_RUNS, OutboxPayload, enqueue
from imageshield.search.models import (
    AttestationRow,
    ClaimedRun,
    InfringementRow,
    ProviderDescriptor,
    RunRow,
    SeedRow,
)
from imageshield.search.provider import ProviderMatch, ProviderResult
from imageshield.search.urlhash import (
    NORMALISATION_VERSION,
    canonicalise,
    source_domain,
    url_hash,
)
from imageshield.types import (
    ProviderId,
    UrlHash,
    UserRef,
    parse_provider_id,
    parse_user_ref,
)

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
    INSERT INTO content_urls (url_hash, url, source_domain, canonical_url,
                              normalisation_version)
    VALUES (%(url_hash)s, %(url)s, %(source_domain)s, %(canonical_url)s,
            %(normalisation_version)s)
    ON CONFLICT (url_hash) DO UPDATE SET last_seen_at = now()
"""

# Rescan semantics (step 6): found again -> UPDATE, never a second row. Not
# found -> touched by nothing, and a stale last_seen_at IS the signal; the
# recheck loop that sets url_alive = false is not in v1.
_UPSERT_INFRINGEMENT_SQL = """
    INSERT INTO infringements (user_ref, url_hash, page_url, image_url, keyed_on)
    VALUES (%(user_ref)s, %(url_hash)s, %(page_url)s, %(image_url)s, %(keyed_on)s)
    ON CONFLICT (user_ref, url_hash) DO UPDATE
      SET last_seen_at = now(),
          seen_count = infringements.seen_count + 1
    RETURNING infringement_id
"""

# provider_score is taken from the new observation: a provider's score for
# the same URL can move between runs, and the latest raw value is the one
# step 7 calibrates. It is still RAW — nothing here rescales.
_UPSERT_ATTESTATION_SQL = """
    INSERT INTO attestations (infringement_id, provider_id, score_kind,
                              provider_score, provider_category, query_quality,
                              score_version, last_run_id, band, calibration_version)
    VALUES (%(infringement_id)s, %(provider_id)s, %(score_kind)s,
            %(provider_score)s, %(provider_category)s, %(query_quality)s,
            %(score_version)s, %(run_id)s, %(band)s, %(calibration_version)s)
    ON CONFLICT (infringement_id, provider_id) DO UPDATE
      SET last_confirmed_at = now(),
          confirm_count = attestations.confirm_count + 1,
          provider_score = EXCLUDED.provider_score,
          provider_category = EXCLUDED.provider_category,
          query_quality = EXCLUDED.query_quality,
          score_version = EXCLUDED.score_version,
          last_run_id = EXCLUDED.last_run_id,
          band = EXCLUDED.band,
          calibration_version = EXCLUDED.calibration_version
"""

# Every provider's band for this infringement — including ones written
# earlier in the same run by a different provider — so the roll-up sees the
# whole picture and not just what this call wrote.
_ATTESTATION_BANDS_SQL = """
    SELECT band FROM attestations WHERE infringement_id = %(infringement_id)s
"""

_SET_INFRINGEMENT_BAND_SQL = """
    UPDATE infringements SET band = %(band)s, band_reason = %(band_reason)s
    WHERE infringement_id = %(infringement_id)s
"""

_COMPLETE_RUN_SQL = """
    UPDATE search_runs
    SET status = 'completed',
        providers_succeeded = %(providers_succeeded)s,
        matches_found = (SELECT count(*) FROM attestations
                         WHERE last_run_id = %(run_id)s),
        completed_at = now()
    WHERE run_id = %(run_id)s
"""

# An infringement with no attestation cannot exist (the write path always
# creates both), so the inner JOIN is not hiding rows.
_LIST_INFRINGEMENTS_SQL = """
    SELECT i.infringement_id, i.page_url, i.image_url, i.keyed_on,
           i.first_seen_at, i.last_seen_at, i.seen_count, i.band, i.status,
           i.band_reason,
           a.provider_id, a.score_kind, a.provider_score, a.provider_category,
           a.query_quality, a.score_version, a.first_confirmed_at,
           a.last_confirmed_at, a.confirm_count, a.band, a.calibration_version
    FROM infringements i
    JOIN attestations a ON a.infringement_id = i.infringement_id
    WHERE i.user_ref = %(user_ref)s
      AND (%(since)s::timestamptz IS NULL OR i.last_seen_at >= %(since)s)
    ORDER BY i.last_seen_at DESC, i.infringement_id, a.provider_id
"""


class InfringementKey(NamedTuple):
    url_hash: UrlHash
    key_url: str
    keyed_on: str  # 'page_url' | 'image_url'
    match: ProviderMatch


def fan_out(matches: Sequence[ProviderMatch]) -> list[InfringementKey]:
    """One key per page the image was found on; image_url as the fallback
    when the provider reported no page at all.

    Keying on the page rather than the image is the whole dedup decision: the
    page is what a user acts on — a takedown notice, a lawyer, a report — so
    one match with three backlinks becomes three infringements.

    Collapsed on url_hash **within the batch**, so the same page returned
    twice in one run (different image URLs, or the same page with different
    tracking params) is one write rather than a self-inflicted double count.
    First occurrence wins: providers return relevance order, and raw_response
    keeps everything regardless.

    Public because ``calibrate observe`` must map provider responses to
    candidate URLs through exactly this code. An eval measurement made
    against a reimplementation measures the reimplementation.
    """
    seen: dict[UrlHash, InfringementKey] = {}
    for match in matches:
        targets = (
            [(page, "page_url") for page in match.page_urls]
            if match.page_urls
            else [(match.image_url, "image_url")]
        )
        for key_url, keyed_on in targets:
            digest = url_hash(key_url)
            if digest not in seen:
                seen[digest] = InfringementKey(digest, key_url, keyed_on, match)
    return list(seen.values())


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

    async def record_infringements(
        self,
        run_id: UUID,
        user_ref: UserRef,
        provider: ProviderDescriptor,
        matches: Sequence[ProviderMatch],
        policy: BandingPolicy,
    ) -> int: ...

    async def complete_run(
        self, run_id: UUID, providers_succeeded: Sequence[ProviderId]
    ) -> None: ...

    async def list_infringements(
        self, user_ref: UserRef, since: datetime | None
    ) -> tuple[InfringementRow, ...]: ...


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

    async def record_infringements(
        self,
        run_id: UUID,
        user_ref: UserRef,
        provider: ProviderDescriptor,
        matches: Sequence[ProviderMatch],
        policy: BandingPolicy,
    ) -> int:
        """Upsert one infringement per page found and one attestation per
        (infringement, provider). Returns the number of infringements touched
        — inserted or updated, since a rescan legitimately touches without
        inserting."""
        keys = fan_out(matches)
        async with self._pool.connection() as conn, conn.transaction():
            for key in keys:
                await conn.execute(
                    _UPSERT_URL_SQL,
                    {
                        "url_hash": key.url_hash,
                        "url": key.key_url,
                        "source_domain": source_domain(key.key_url),
                        "canonical_url": canonicalise(key.key_url),
                        "normalisation_version": NORMALISATION_VERSION,
                    },
                )
                cur = await conn.execute(
                    _UPSERT_INFRINGEMENT_SQL,
                    {
                        "user_ref": user_ref,
                        "url_hash": key.url_hash,
                        "page_url": key.key_url,
                        "image_url": key.match.image_url,
                        "keyed_on": key.keyed_on,
                    },
                )
                row = await cur.fetchone()
                assert row is not None
                infringement_id: UUID = row[0]
                decision = band_for_attestation(
                    policy.get(provider.provider_id),
                    provider.score_kind,
                    key.match.provider_score,
                    key.match.provider_category,
                )
                await conn.execute(
                    _UPSERT_ATTESTATION_SQL,
                    {
                        "infringement_id": infringement_id,
                        "provider_id": provider.provider_id,
                        "score_kind": provider.score_kind,
                        "provider_score": key.match.provider_score,
                        "provider_category": key.match.provider_category,
                        "query_quality": key.match.query_quality,
                        "score_version": provider.score_version,
                        "run_id": run_id,
                        "band": decision.band,
                        "calibration_version": decision.calibration_version,
                    },
                )
                # Roll up here rather than at end of run: otherwise, between
                # provider A's write and provider B's, the stored band
                # disagrees with the attestations backing it and a reader can
                # observe that window.
                bands_cur = await conn.execute(
                    _ATTESTATION_BANDS_SQL, {"infringement_id": infringement_id}
                )
                rolled, reason = roll_up([r[0] for r in await bands_cur.fetchall()])
                await conn.execute(
                    _SET_INFRINGEMENT_BAND_SQL,
                    {
                        "infringement_id": infringement_id,
                        "band": rolled,
                        "band_reason": reason,
                    },
                )
        return len(keys)

    async def complete_run(
        self, run_id: UUID, providers_succeeded: Sequence[ProviderId]
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                _COMPLETE_RUN_SQL,
                {"run_id": run_id, "providers_succeeded": list(providers_succeeded)},
            )

    async def list_infringements(
        self, user_ref: UserRef, since: datetime | None
    ) -> tuple[InfringementRow, ...]:
        """One row per infringement with its attestations nested. ``since``
        filters on ``last_seen_at``, so a rescan that re-confirms an old
        infringement brings it back into a recent window — which is the
        point: it is still there."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                _LIST_INFRINGEMENTS_SQL, {"user_ref": user_ref, "since": since}
            )
            rows = await cur.fetchall()
        return _group_infringements(rows)


def _group_infringements(rows: Sequence[tuple[Any, ...]]) -> tuple[InfringementRow, ...]:
    """Collapse the joined (infringement x attestation) rows, preserving the
    query's ordering.

    Unpacked by name in ``_LIST_INFRINGEMENTS_SQL``'s column order rather than
    indexed by position, so a future column added to that SELECT can't
    silently shift an existing field onto the wrong name.
    """
    heads: dict[UUID, tuple[Any, ...]] = {}
    attestations: dict[UUID, list[AttestationRow]] = {}
    for row in rows:
        (
            infringement_id,
            page_url,
            image_url,
            keyed_on,
            first_seen_at,
            last_seen_at,
            seen_count,
            band,
            status,
            band_reason,
            att_provider_id,
            att_score_kind,
            att_provider_score,
            att_provider_category,
            att_query_quality,
            att_score_version,
            att_first_confirmed_at,
            att_last_confirmed_at,
            att_confirm_count,
            att_band,
            att_calibration_version,
        ) = row
        if infringement_id not in heads:
            heads[infringement_id] = (
                infringement_id,
                page_url,
                image_url,
                keyed_on,
                first_seen_at,
                last_seen_at,
                seen_count,
                band,
                status,
                band_reason,
            )
            attestations[infringement_id] = []
        attestations[infringement_id].append(
            AttestationRow(
                provider_id=att_provider_id,
                score_kind=att_score_kind,
                provider_score=att_provider_score,
                provider_category=att_provider_category,
                query_quality=att_query_quality,
                score_version=att_score_version,
                first_confirmed_at=att_first_confirmed_at,
                last_confirmed_at=att_last_confirmed_at,
                confirm_count=att_confirm_count,
                band=att_band,
                calibration_version=att_calibration_version,
            )
        )
    return tuple(
        InfringementRow(
            infringement_id=head[0],
            page_url=head[1],
            image_url=head[2],
            keyed_on=head[3],
            first_seen_at=head[4],
            last_seen_at=head[5],
            seen_count=head[6],
            band=head[7],
            status=head[8],
            band_reason=head[9],
            attestations=tuple(attestations[key]),
        )
        for key, head in heads.items()
    )
