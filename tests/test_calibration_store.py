"""load_active_policy against real Postgres, with hand-inserted malformed
JSONB the pydantic models cannot represent from Python (which is exactly why
these states escaped: the happy-path tests can only ever construct rows the
models accept).

Each case pins one branch of the three-tier fallback documented in
``calibration/store.py``'s module docstring: bands unparseable -> config is
None for that provider; score_domain unparseable -> that provider falls back
to an unbounded ScoreDomain while every other provider is unaffected. None of
these may raise out of ``load_active_policy``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import psycopg
import pytest

from imageshield.calibration.bands import band_for_attestation
from imageshield.calibration.models import ScoreDomain
from imageshield.calibration.store import PostgresCalibrationStore
from imageshield.db.connection import make_async_pool
from imageshield.types import ProviderId
from tests.db import run_migrate

HIVE = ProviderId("hive")
GOOGLE = ProviderId("google")
STUB = ProviderId("stub")  # migration 0019: seeded DISABLED, but still a row
REKOGNITION_CONFIRM = ProviderId("rekognition_confirm")  # migration 0021: classifier row


@pytest.fixture
def migrated_db(throwaway_db: str) -> str:
    run_migrate(throwaway_db, "down", "--all")
    up_result = run_migrate(throwaway_db, "up")
    assert up_result.returncode == 0, up_result.stderr
    return throwaway_db


def _exec(db_url: str, sql: str, params: tuple[Any, ...] = ()) -> None:
    """Direct psycopg, not the store: these rows are shaped to be rejected by
    the pydantic models, so they can only be inserted as raw SQL."""
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute(sql, params)


async def _load_policy(db_url: str) -> Any:
    pool = make_async_pool(db_url, min_size=1, max_size=2)
    await pool.open()
    try:
        return await PostgresCalibrationStore(pool).load_active_policy()
    finally:
        await pool.close()


async def test_band_with_unparseable_decimal_falls_back_to_no_config(
    migrated_db: str,
) -> None:
    """Decimal("n/a") -> decimal.InvalidOperation (an ArithmeticError, not a
    ValueError) -- the pre-fix ``except (ValueError, KeyError, TypeError)``
    let this escape ``load_active_policy`` entirely."""
    _exec(
        migrated_db,
        "INSERT INTO calibration_configs (provider_id, version, score_kind, bands, active)"
        " VALUES ('hive', 'bad-decimal-v1', 'numeric', %s::jsonb, true)",
        ('[{"band":"drop","max":"n/a"}]',),
    )
    policy = await _load_policy(migrated_db)

    assert policy[HIVE].config is None
    decision = band_for_attestation(policy[HIVE], "numeric", Decimal("0.99"), None)
    assert decision.band == "review"


async def test_bands_as_bare_strings_falls_back_to_no_config(migrated_db: str) -> None:
    """["drop"] -> entry.get("band") called on a str -> AttributeError, which
    the pre-fix except clause also did not catch."""
    _exec(
        migrated_db,
        "INSERT INTO calibration_configs (provider_id, version, score_kind, bands, active)"
        " VALUES ('hive', 'bad-shape-v1', 'numeric', %s::jsonb, true)",
        ('["drop"]',),
    )
    policy = await _load_policy(migrated_db)

    assert policy[HIVE].config is None
    decision = band_for_attestation(policy[HIVE], "numeric", Decimal("0.99"), None)
    assert decision.band == "review"


async def test_bands_as_bare_ints_falls_back_to_no_config(migrated_db: str) -> None:
    """[1] -> entry.get("band") called on an int -> AttributeError."""
    _exec(
        migrated_db,
        "INSERT INTO calibration_configs (provider_id, version, score_kind, bands, active)"
        " VALUES ('hive', 'bad-shape-v2', 'numeric', %s::jsonb, true)",
        ("[1]",),
    )
    policy = await _load_policy(migrated_db)

    assert policy[HIVE].config is None


async def test_malformed_score_domain_min_does_not_fail_other_providers(
    migrated_db: str,
) -> None:
    """A non-numeric score_domain.min on hive -> decimal.InvalidOperation.
    Before the fix this was completely unguarded and failed the WHOLE policy
    load -- google, which has nothing wrong with it, must still come back."""
    _exec(
        migrated_db,
        "UPDATE providers SET score_domain = %s::jsonb, calibrated = true"
        " WHERE provider_id = 'hive'",
        ('{"min":"abc","max":"1.0"}',),
    )
    _exec(
        migrated_db,
        "INSERT INTO calibration_configs (provider_id, version, score_kind, bands, active)"
        " VALUES ('hive', 'hive-cal-v1', 'numeric', %s::jsonb, true)",
        ('[{"band":"auto_confirm","min":0.5}]',),
    )
    policy = await _load_policy(migrated_db)

    assert set(policy) == {HIVE, GOOGLE, STUB, REKOGNITION_CONFIRM}
    assert policy[HIVE].score_domain == ScoreDomain()  # unbounded fallback
    assert policy[GOOGLE].score_domain.categories is not None  # untouched
    assert policy[HIVE].config is not None  # bands parsed fine; only domain was bad

    # Contained, not just absent: with calibrated=true and a real config, the
    # ONLY thing standing between this provider and auto_confirm is the
    # domain fallback. Numeric banding's own gate (bands.py rule 3c) reads
    # the unbounded ScoreDomain() as score_domain_unknown -> review.
    decision = band_for_attestation(policy[HIVE], "numeric", Decimal("0.99"), None)
    assert decision.band == "review"
    assert decision.reason == "score_domain_unknown"


async def test_malformed_score_domain_categories_does_not_fail_other_providers(
    migrated_db: str,
) -> None:
    """{"categories": 5} -> tuple(5) -> TypeError. Same containment
    requirement as the min case, on the other provider."""
    _exec(
        migrated_db,
        "UPDATE providers SET score_domain = %s::jsonb WHERE provider_id = 'google'",
        ('{"categories": 5}',),
    )
    policy = await _load_policy(migrated_db)

    assert set(policy) == {HIVE, GOOGLE, STUB, REKOGNITION_CONFIRM}
    assert policy[GOOGLE].score_domain == ScoreDomain()  # unbounded fallback
    assert policy[HIVE].score_domain.min == Decimal("0.5")  # untouched
