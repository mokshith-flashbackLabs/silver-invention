"""Eval item and observation persistence. Real Postgres.

The consent and taxonomy constraints are asserted at the DB level in
test_migrations.py; these tests assert the store surfaces them as errors
rather than swallowing them, and that eval_rows() produces the LEFT JOIN
semantics the metrics module depends on.
"""

from __future__ import annotations

from decimal import Decimal

import psycopg
import pytest

from imageshield.types import ProviderId

HIVE = ProviderId("hive")


async def test_insert_rejects_a_blank_consent_basis(calibration_store) -> None:
    """No eval item without a traceable consent basis. Sourcing is consenting
    participants, public-domain, or synthetic only."""
    with pytest.raises(psycopg.errors.CheckViolation):
        await calibration_store.insert_eval_item(
            "v1", "s3://seed", "https://x.test/a",
            "true_match", "same_person", "   ", "tester",
        )


async def test_insert_rejects_derived_edit_labelled_false_match(
    calibration_store,
) -> None:
    """The inversion that would tune thresholds against the flagship case."""
    with pytest.raises(psycopg.errors.CheckViolation):
        await calibration_store.insert_eval_item(
            "v1", "s3://seed", "https://x.test/a",
            "false_match", "derived_edit", "team member, written consent", "tester",
        )


async def test_eval_rows_marks_an_unobserved_item_as_not_observed(
    calibration_store,
) -> None:
    """The LEFT JOIN that makes a miss countable. An item the provider never
    returned must come back with observed=False, not be absent."""
    found = await calibration_store.insert_eval_item(
        "v1", "s3://seed", "https://x.test/found",
        "true_match", "same_person", "team member, written consent", "tester",
    )
    await calibration_store.insert_eval_item(
        "v1", "s3://seed", "https://x.test/missed",
        "true_match", "novel_generation", "synthetic, public domain", "tester",
    )
    await calibration_store.upsert_eval_observation(
        found, HIVE, "numeric", Decimal("0.95"), None, None, "hive-web-search-v1"
    )
    rows = await calibration_store.eval_rows("v1", HIVE)
    assert len(rows) == 2
    by_observed = {r.observed for r in rows}
    assert by_observed == {True, False}
    missed = next(r for r in rows if not r.observed)
    assert missed.label == "true_match"
    assert missed.provider_score is None


async def test_reobserving_updates_rather_than_appends(calibration_store) -> None:
    item = await calibration_store.insert_eval_item(
        "v1", "s3://seed", "https://x.test/a",
        "true_match", "same_person", "team member, written consent", "tester",
    )
    await calibration_store.upsert_eval_observation(
        item, HIVE, "numeric", Decimal("0.80"), None, None, "hive-web-search-v1"
    )
    await calibration_store.upsert_eval_observation(
        item, HIVE, "numeric", Decimal("0.95"), None, None, "hive-web-search-v1"
    )
    rows = await calibration_store.eval_rows("v1", HIVE)
    assert len(rows) == 1
    assert rows[0].provider_score == Decimal("0.9500")


async def test_uncovered_seeds_reports_seeds_never_run(calibration_store) -> None:
    """The activate floor's coverage condition. A seed with no ok coverage row
    means its items' absences are not evidence of anything."""
    await calibration_store.insert_eval_item(
        "v1", "s3://seed-a", "https://x.test/a",
        "true_match", "same_person", "team member, written consent", "tester",
    )
    await calibration_store.insert_eval_item(
        "v1", "s3://seed-b", "https://x.test/b",
        "false_match", "lookalike", "team member, written consent", "tester",
    )
    await calibration_store.record_seed_coverage("v1", "s3://seed-a", HIVE, "ok", 12)
    assert await calibration_store.uncovered_seeds("v1", HIVE) == ("s3://seed-b",)


async def test_a_failed_seed_run_does_not_count_as_coverage(
    calibration_store,
) -> None:
    """status='timeout' means we did not learn anything about that seed. Its
    items' absences must not be read as misses."""
    await calibration_store.insert_eval_item(
        "v1", "s3://seed-a", "https://x.test/a",
        "true_match", "same_person", "team member, written consent", "tester",
    )
    await calibration_store.record_seed_coverage("v1", "s3://seed-a", HIVE, "timeout", 0)
    assert await calibration_store.uncovered_seeds("v1", HIVE) == ("s3://seed-a",)
