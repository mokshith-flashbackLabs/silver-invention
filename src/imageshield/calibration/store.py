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

import structlog
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

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

_LOAD_POLICY_SQL = """
    SELECT p.provider_id, p.calibrated, p.score_domain,
           c.config_id, c.version, c.score_kind, c.bands
    FROM providers p
    LEFT JOIN calibration_configs c
      ON c.provider_id = p.provider_id AND c.active
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
