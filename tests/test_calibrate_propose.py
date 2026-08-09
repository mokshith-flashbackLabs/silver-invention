"""`calibrate propose` writes an INACTIVE config, or refuses."""

from __future__ import annotations

from decimal import Decimal

import pytest
from devtools.calibrate.__main__ import build_bands_json, propose_config

from imageshield.types import ProviderId

HIVE = ProviderId("hive")


async def seed_items(store, n_pos: int, n_look: int) -> None:
    """A genuinely separable eval set — mostly high-confidence true matches
    and mostly low-confidence lookalikes, plus a handful of each on the
    harder side of the split (``n_pos``/``n_look`` must be >= 5).

    That residual overlap matters: a perfectly clean two-cluster set (every
    true_match at one score, every false_match at a strictly lower one)
    makes the recommended auto_confirm and drop boundaries land on the exact
    same candidate threshold, because that single split point is
    simultaneously the first threshold precision clears 0.99 at and the last
    threshold NPV clears 0.99 at. The touching-boundary guard in
    ``sweep_numeric`` correctly refuses that as "no review band" rather than
    recommending it, so a fixture meant to exercise a *successful* propose
    needs a set with real separation between the two boundaries.
    """
    for i in range(n_pos):
        score = "0.80" if i < 5 else "0.97"
        item = await store.insert_eval_item(
            "v1", "s3://seed", f"https://x.test/p{i}",
            "true_match", "same_person", "team member, written consent", "tester",
        )
        await store.upsert_eval_observation(
            item, HIVE, "numeric", Decimal(score), None, None, "hive-web-search-v1"
        )
    for i in range(n_look):
        score = "0.90" if i < 5 else "0.55"
        item = await store.insert_eval_item(
            "v1", "s3://seed", f"https://x.test/l{i}",
            "false_match", "lookalike", "team member, written consent", "tester",
        )
        await store.upsert_eval_observation(
            item, HIVE, "numeric", Decimal(score), None, None, "hive-web-search-v1"
        )
    await store.record_seed_coverage("v1", "s3://seed", HIVE, "ok", n_pos + n_look)


async def test_propose_writes_an_inactive_row(calibration_store) -> None:
    """Nothing propose does can change what users see. Activation is a
    separate, gated command."""
    await seed_items(calibration_store, 100, 100)
    config_id = await propose_config(
        calibration_store, HIVE, "v1", "hive-cal-v2", bands_json=None
    )
    stored = await calibration_store.get_config(config_id)
    assert stored is not None
    assert stored.active is False
    assert stored.eval_set_id == "v1"


async def test_propose_records_the_effective_sample_size(calibration_store) -> None:
    """Excluding uncertain — the number the activate floor tests."""
    await seed_items(calibration_store, 100, 100)
    await calibration_store.insert_eval_item(
        "v1", "s3://seed", "https://x.test/u0",
        "uncertain", "lookalike", "team member, written consent", "tester",
    )
    config_id = await propose_config(
        calibration_store, HIVE, "v1", "hive-cal-v3", bands_json=None
    )
    async with calibration_store._pool.connection() as conn:
        cur = await conn.execute(
            "SELECT eval_sample_size FROM calibration_configs WHERE config_id = %s",
            (config_id,),
        )
        row = await cur.fetchone()
    assert row[0] == 200


async def test_propose_rejects_bands_outside_the_score_domain(
    calibration_store,
) -> None:
    """A 0.2 boundary is meaningless for Hive, whose floor is 0.5 — and would
    silently send in-domain scores to no_band_covers_score at runtime."""
    await seed_items(calibration_store, 100, 100)
    bad = build_bands_json(Decimal("0.20"), Decimal("0.30"))
    with pytest.raises(ValueError, match="outside score_domain"):
        await propose_config(
            calibration_store, HIVE, "v1", "hive-cal-v4", bands_json=bad
        )


async def test_propose_refuses_when_the_sweep_recommends_nothing(
    calibration_store,
) -> None:
    """Identical scores on opposite labels — no boundary separates them. The
    correct outcome is a refusal, not a config nobody can justify."""
    for label, kind in (("true_match", "same_person"), ("false_match", "lookalike")):
        item = await calibration_store.insert_eval_item(
            "v1", "s3://seed", f"https://x.test/{kind}",
            label, kind, "team member, written consent", "tester",
        )
        await calibration_store.upsert_eval_observation(
            item, HIVE, "numeric", Decimal("0.95"), None, None, "hive-web-search-v1"
        )
    await calibration_store.record_seed_coverage("v1", "s3://seed", HIVE, "ok", 2)
    with pytest.raises(ValueError, match="no recommendation"):
        await propose_config(
            calibration_store, HIVE, "v1", "hive-cal-v5", bands_json=None
        )
