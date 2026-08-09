"""Persistence for calibration configs and eval data — raw SQL, no ORM.

This module is the only place that turns the ``bands`` and ``score_domain``
JSONB columns into typed values. Parsing lives here rather than in
:mod:`models` so the pure modules stay free of anything that can fail on
malformed input.

A malformed row is **skipped, not raised**, at whichever granularity the
damage is contained to — three tiers, every one of them landing in
``review``, never an exception that escapes this module:

- bands unparseable       -> ``config=None``            -> rule 1 -> review
- score_domain unparseable -> falls back to ``ScoreDomain()`` (unbounded)
  -> Task 2's numeric-banding gate returns ``score_domain_unknown`` -> review
- the whole row unusable  -> the provider is skipped entirely ->
  ``policy.get(provider_id)`` is ``None`` -> rule 1 -> review

A hand-edited or hand-inserted row (bad JSONB shape, a value that fails to
parse as ``Decimal``) must not be able to fail a scan for every user, nor
even for just the one provider whose row is bad while the queue never stops
redelivering.

Each parsing step below therefore catches ``except Exception`` rather than
an enumerated set — the enumerated set was already reaching for "anything
parsing can throw" and naming it honestly is the fix, since ``Decimal("n/a")``
raises ``decimal.InvalidOperation`` (an ``ArithmeticError``) and indexing a
malformed shape (e.g. bands given as ``["drop"]`` instead of
``[{"band": "drop", ...}]``) raises ``AttributeError`` — neither is a
``ValueError``/``KeyError``/``TypeError``. This is still safe on Python 3.11:
``asyncio.CancelledError`` inherits from ``BaseException``, not
``Exception``, so a bare ``except Exception`` never swallows a task
cancellation.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

import structlog
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict

from imageshield.calibration.metrics import EvalRow
from imageshield.calibration.models import (
    Band,
    BandingPolicy,
    CalibrationConfig,
    NumericBand,
    PolicyEntry,
    ScoreDomain,
    ScoreKind,
)
from imageshield.types import ProviderId, parse_provider_id

log = structlog.get_logger("imageshield.calibration")

_VALID_BANDS: frozenset[str] = frozenset({"drop", "review", "auto_confirm"})


class ProviderMeta(BaseModel):
    """What ``sweep``/``propose`` need to know about a provider before they
    can interpret its raw values at all."""

    model_config = ConfigDict(frozen=True)

    score_kind: ScoreKind
    score_domain: ScoreDomain
    calibrated: bool


class StoredConfig(BaseModel):
    """A config row plus the provenance the activate floor needs. Distinct
    from CalibrationConfig, which is only what banding needs."""

    model_config = ConfigDict(frozen=True)

    config: CalibrationConfig
    eval_set_id: str | None
    active: bool

_LOAD_POLICY_SQL = """
    SELECT p.provider_id, p.calibrated, p.score_domain,
           c.config_id, c.version, c.score_kind, c.bands
    FROM providers p
    LEFT JOIN calibration_configs c
      ON c.provider_id = p.provider_id AND c.active
"""

_INSERT_EVAL_ITEM_SQL = """
    INSERT INTO eval_items (eval_set_id, seed_uri, candidate_url, label,
                            label_kind, consent_basis, labelled_by)
    VALUES (%(eval_set_id)s, %(seed_uri)s, %(candidate_url)s, %(label)s,
            %(label_kind)s, %(consent_basis)s, %(labelled_by)s)
    RETURNING item_id
"""

_EVAL_SEEDS_SQL = """
    SELECT DISTINCT seed_uri FROM eval_items
    WHERE eval_set_id = %(eval_set_id)s ORDER BY seed_uri
"""

_EVAL_ITEMS_FOR_SEED_SQL = """
    SELECT item_id, candidate_url FROM eval_items
    WHERE eval_set_id = %(eval_set_id)s AND seed_uri = %(seed_uri)s
"""

# Mirrors the attestations upsert: re-observation UPDATES, never appends.
_UPSERT_EVAL_OBSERVATION_SQL = """
    INSERT INTO eval_observations (item_id, provider_id, score_kind,
                                   provider_score, provider_category,
                                   query_quality, score_version)
    VALUES (%(item_id)s, %(provider_id)s, %(score_kind)s, %(provider_score)s,
            %(provider_category)s, %(query_quality)s, %(score_version)s)
    ON CONFLICT (item_id, provider_id) DO UPDATE
      SET score_kind = EXCLUDED.score_kind,
          provider_score = EXCLUDED.provider_score,
          provider_category = EXCLUDED.provider_category,
          query_quality = EXCLUDED.query_quality,
          score_version = EXCLUDED.score_version,
          observed_at = now()
"""

_RECORD_COVERAGE_SQL = """
    INSERT INTO eval_seed_coverage (eval_set_id, seed_uri, provider_id, status,
                                    candidates_returned)
    VALUES (%(eval_set_id)s, %(seed_uri)s, %(provider_id)s, %(status)s,
            %(candidates_returned)s)
    ON CONFLICT (eval_set_id, seed_uri, provider_id) DO UPDATE
      SET status = EXCLUDED.status,
          candidates_returned = EXCLUDED.candidates_returned,
          observed_at = now()
"""

# LEFT JOIN, deliberately: an item with no observation is a provider MISS and
# must reach the metrics module as observed=False rather than not arriving at
# all. Computing recall over only what a provider returned guarantees an
# excellent-looking number.
_EVAL_ROWS_SQL = """
    SELECT i.label, i.label_kind, (o.observation_id IS NOT NULL) AS observed,
           o.provider_score, o.provider_category
    FROM eval_items i
    LEFT JOIN eval_observations o
      ON o.item_id = i.item_id AND o.provider_id = %(provider_id)s
    WHERE i.eval_set_id = %(eval_set_id)s
    ORDER BY i.item_id
"""

# A seed with no ok coverage row was never successfully asked, so its items'
# absences are not evidence of anything.
_UNCOVERED_SEEDS_SQL = """
    SELECT DISTINCT i.seed_uri
    FROM eval_items i
    WHERE i.eval_set_id = %(eval_set_id)s
      AND NOT EXISTS (
        SELECT 1 FROM eval_seed_coverage c
        WHERE c.eval_set_id = i.eval_set_id
          AND c.seed_uri = i.seed_uri
          AND c.provider_id = %(provider_id)s
          AND c.status = 'ok')
    ORDER BY i.seed_uri
"""

_PROVIDER_META_SQL = """
    SELECT score_kind, score_domain, calibrated FROM providers
    WHERE provider_id = %(provider_id)s
"""

_INSERT_CONFIG_SQL = """
    INSERT INTO calibration_configs (provider_id, version, score_kind, bands,
                                     eval_set_id, eval_sample_size, measured)
    VALUES (%(provider_id)s, %(version)s, %(score_kind)s, %(bands)s,
            %(eval_set_id)s, %(eval_sample_size)s, %(measured)s)
    RETURNING config_id
"""

_GET_CONFIG_SQL = """
    SELECT config_id, provider_id, version, score_kind, bands, eval_set_id,
           active
    FROM calibration_configs WHERE config_id = %(config_id)s
"""


def parse_score_domain(raw: dict[str, Any] | None) -> ScoreDomain:
    if not raw:
        return ScoreDomain()
    categories = raw.get("categories")
    return ScoreDomain(
        min=Decimal(str(raw["min"])) if raw.get("min") is not None else None,
        max=Decimal(str(raw["max"])) if raw.get("max") is not None else None,
        categories=tuple(categories) if categories else None,
    )


def parse_bands(
    score_kind: ScoreKind, raw: Any
) -> tuple[tuple[NumericBand, ...], dict[str, Band]]:
    """Raises ValueError on anything unrecognised; the caller logs and skips."""
    if score_kind == "numeric":
        if not isinstance(raw, list):
            raise ValueError("numeric bands must be a JSON array")
        parsed: list[NumericBand] = []
        for entry in raw:
            band = entry.get("band")
            if band not in _VALID_BANDS:
                raise ValueError(f"unknown band {band!r}")
            parsed.append(
                NumericBand(
                    band=band,
                    min=Decimal(str(entry["min"])) if entry.get("min") is not None else None,
                    max=Decimal(str(entry["max"])) if entry.get("max") is not None else None,
                )
            )
        return tuple(parsed), {}
    if not isinstance(raw, dict):
        raise ValueError("categorical bands must be a JSON object")
    for value in raw.values():
        if value not in _VALID_BANDS:
            raise ValueError(f"unknown band {value!r}")
    return (), dict(raw)


async def load_active_policy(conn: AsyncConnection[Any]) -> BandingPolicy:
    """Snapshot every provider's calibrated flag, score domain, and active
    config. Taken once per run so every attestation in that run is banded by
    one consistent set of rules — a config activated mid-run cannot split a
    run's results across two rulesets.

    See the module docstring for the three-tier fallback: bands, score
    domain, and (as a last resort) the whole row are each guarded
    separately, so damage from one malformed value is contained to the
    smallest scope that value actually affects.
    """
    cur = await conn.execute(_LOAD_POLICY_SQL)
    rows = await cur.fetchall()
    policy: dict[ProviderId, PolicyEntry] = {}
    for (
        provider_id,
        calibrated,
        score_domain,
        config_id,
        version,
        score_kind,
        bands,
    ) in rows:
        try:
            pid = parse_provider_id(provider_id)
        except Exception as exc:
            log.error(
                "calibration.malformed_provider_row",
                provider_id=provider_id,
                error=str(exc),
            )
            continue  # can't even name the provider; nothing to key it under

        try:
            domain = parse_score_domain(score_domain)
        except Exception as exc:
            log.error(
                "calibration.malformed_score_domain",
                provider_id=provider_id,
                error=str(exc),
            )
            # Unbounded ScoreDomain: numeric banding's own gate (bands.py,
            # rule 3c) rejects a domain missing either bound and returns
            # score_domain_unknown -> review. Contained to this provider.
            domain = ScoreDomain()

        config: CalibrationConfig | None = None
        if config_id is not None:
            try:
                numeric, categorical = parse_bands(score_kind, bands)
                config = CalibrationConfig(
                    config_id=config_id,
                    provider_id=pid,
                    version=version,
                    score_kind=score_kind,
                    numeric_bands=numeric,
                    categorical_bands=categorical,
                )
            except Exception as exc:
                log.error(
                    "calibration.malformed_active_config",
                    provider_id=provider_id,
                    version=version,
                    error=str(exc),
                )
                # config stays None -> band_for_attestation's rule 1 -> review

        try:
            policy[pid] = PolicyEntry(
                provider_id=pid,
                calibrated=calibrated,
                score_domain=domain,
                config=config,
            )
        except Exception as exc:
            log.error(
                "calibration.malformed_provider_row",
                provider_id=provider_id,
                error=str(exc),
            )
            # Whole row unusable: provider absent from the policy entirely.
            # policy.get(provider_id) is None downstream -> rule 1 -> review.
    return policy


class CalibrationStore(Protocol):
    """What the search worker needs from calibration persistence — typed by
    hand so a fake in tests is checked against the same shape as the real
    thing, matching ``search.store.SearchStore``'s pattern."""

    async def load_active_policy(self) -> BandingPolicy: ...


class PostgresCalibrationStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def load_active_policy(self) -> BandingPolicy:
        async with self._pool.connection() as conn:
            return await load_active_policy(conn)

    async def insert_eval_item(
        self,
        eval_set_id: str,
        seed_uri: str,
        candidate_url: str,
        label: str,
        label_kind: str,
        consent_basis: str,
        labelled_by: str,
    ) -> UUID:
        """CheckViolation propagates. A rejected item is a labelling error the
        operator must see, not something to log and continue past."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                _INSERT_EVAL_ITEM_SQL,
                {
                    "eval_set_id": eval_set_id,
                    "seed_uri": seed_uri,
                    "candidate_url": candidate_url,
                    "label": label,
                    "label_kind": label_kind,
                    "consent_basis": consent_basis,
                    "labelled_by": labelled_by,
                },
            )
            row = await cur.fetchone()
        assert row is not None
        item_id: UUID = row[0]
        return item_id

    async def eval_seeds(self, eval_set_id: str) -> tuple[str, ...]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_EVAL_SEEDS_SQL, {"eval_set_id": eval_set_id})
            return tuple(r[0] for r in await cur.fetchall())

    async def eval_items_for_seed(
        self, eval_set_id: str, seed_uri: str
    ) -> tuple[tuple[UUID, str], ...]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                _EVAL_ITEMS_FOR_SEED_SQL,
                {"eval_set_id": eval_set_id, "seed_uri": seed_uri},
            )
            return tuple((r[0], r[1]) for r in await cur.fetchall())

    async def upsert_eval_observation(
        self,
        item_id: UUID,
        provider_id: ProviderId,
        score_kind: str,
        provider_score: Decimal | None,
        provider_category: str | None,
        query_quality: str | None,
        score_version: str,
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                _UPSERT_EVAL_OBSERVATION_SQL,
                {
                    "item_id": item_id,
                    "provider_id": provider_id,
                    "score_kind": score_kind,
                    "provider_score": provider_score,
                    "provider_category": provider_category,
                    "query_quality": query_quality,
                    "score_version": score_version,
                },
            )

    async def record_seed_coverage(
        self,
        eval_set_id: str,
        seed_uri: str,
        provider_id: ProviderId,
        status: str,
        candidates_returned: int,
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                _RECORD_COVERAGE_SQL,
                {
                    "eval_set_id": eval_set_id,
                    "seed_uri": seed_uri,
                    "provider_id": provider_id,
                    "status": status,
                    "candidates_returned": candidates_returned,
                },
            )

    async def eval_rows(
        self, eval_set_id: str, provider_id: ProviderId
    ) -> tuple[EvalRow, ...]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                _EVAL_ROWS_SQL,
                {"eval_set_id": eval_set_id, "provider_id": provider_id},
            )
            rows = await cur.fetchall()
        return tuple(
            EvalRow(
                label=r[0],
                label_kind=r[1],
                observed=r[2],
                provider_score=r[3],
                provider_category=r[4],
            )
            for r in rows
        )

    async def uncovered_seeds(
        self, eval_set_id: str, provider_id: ProviderId
    ) -> tuple[str, ...]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                _UNCOVERED_SEEDS_SQL,
                {"eval_set_id": eval_set_id, "provider_id": provider_id},
            )
            return tuple(r[0] for r in await cur.fetchall())

    async def provider_meta(self, provider_id: ProviderId) -> ProviderMeta | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_PROVIDER_META_SQL, {"provider_id": provider_id})
            row = await cur.fetchone()
        if row is None:
            return None
        return ProviderMeta(
            score_kind=row[0],
            score_domain=parse_score_domain(row[1]),
            calibrated=row[2],
        )

    async def insert_config(
        self,
        provider_id: ProviderId,
        version: str,
        score_kind: ScoreKind,
        bands: Any,
        eval_set_id: str | None,
        eval_sample_size: int | None,
        measured: dict[str, Any] | None,
    ) -> UUID:
        """Always INACTIVE. Activation is a separate, gated command."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                _INSERT_CONFIG_SQL,
                {
                    "provider_id": provider_id,
                    "version": version,
                    "score_kind": score_kind,
                    "bands": Jsonb(bands),
                    "eval_set_id": eval_set_id,
                    "eval_sample_size": eval_sample_size,
                    "measured": Jsonb(measured) if measured is not None else None,
                },
            )
            row = await cur.fetchone()
        assert row is not None
        config_id: UUID = row[0]
        return config_id

    async def get_config(self, config_id: UUID) -> StoredConfig | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_GET_CONFIG_SQL, {"config_id": config_id})
            row = await cur.fetchone()
        if row is None:
            return None
        numeric, categorical = parse_bands(row[3], row[4])
        return StoredConfig(
            config=CalibrationConfig(
                config_id=row[0],
                provider_id=parse_provider_id(row[1]),
                version=row[2],
                score_kind=row[3],
                numeric_bands=numeric,
                categorical_bands=categorical,
            ),
            eval_set_id=row[5],
            active=row[6],
        )
