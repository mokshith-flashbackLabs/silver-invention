"""`calibrate propose` writes an INACTIVE config, or refuses."""

from __future__ import annotations

from decimal import Decimal

import pytest
from devtools.calibrate.__main__ import build_bands_json, load_sweep, propose_config

from imageshield.calibration.report import render_categorical_sweep
from imageshield.types import ProviderId

HIVE = ProviderId("hive")
GOOGLE = ProviderId("google")


async def seed_categorical_items(
    store, items: list[tuple[str, str, str]]
) -> None:
    """``items`` is a list of ``(label, label_kind, provider_category)``, all
    against one seed, one coverage row written 'ok' for the batch."""
    for i, (label, label_kind, category) in enumerate(items):
        item = await store.insert_eval_item(
            "v1", "s3://seed", f"https://x.test/c{i}",
            label, label_kind, "team member, written consent", "tester",
        )
        await store.upsert_eval_observation(
            item, GOOGLE, "categorical", None, category, None,
            "google-web-detection-v1",
        )
    await store.record_seed_coverage("v1", "s3://seed", GOOGLE, "ok", len(items))


async def _config_count(store, provider_id: ProviderId) -> int:
    async with store._pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM calibration_configs WHERE provider_id = %s",
            (provider_id,),
        )
        row = await cur.fetchone()
    return int(row[0])


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


async def test_propose_rejects_numeric_bands_for_a_categorical_provider(
    calibration_store,
) -> None:
    """C1 repro #1: a numeric bands array handed to a categorical provider
    (google) must not be stored under score_kind='categorical'. Such a row
    is unreadable afterwards — get_config raises, load_active_policy logs
    malformed_active_config and falls back to review — but a reader coping
    with garbage does not make writing the garbage acceptable."""
    await seed_categorical_items(
        calibration_store,
        [
            ("true_match", "same_person", "full_match"),
            ("false_match", "lookalike", "partial_match"),
        ],
    )
    bad = build_bands_json(Decimal("0.20"), Decimal("0.30"))  # numeric shape
    with pytest.raises(ValueError):
        await propose_config(
            calibration_store, GOOGLE, "v1", "google-cal-bad1", bands_json=bad
        )
    assert await _config_count(calibration_store, GOOGLE) == 0


async def test_propose_rejects_an_unknown_band_value_and_unknown_category(
    calibration_store,
) -> None:
    """C1 repro #2: a band value parse_bands itself rejects ('banana'), and
    a category outside the provider's vocabulary ('nonexistent_cat') — both
    defects, neither should ever reach a written row."""
    await seed_categorical_items(
        calibration_store,
        [
            ("true_match", "same_person", "full_match"),
            ("false_match", "lookalike", "partial_match"),
        ],
    )
    bad = {"full_match": "banana", "nonexistent_cat": "drop"}
    with pytest.raises(ValueError):
        await propose_config(
            calibration_store, GOOGLE, "v1", "google-cal-bad2", bands_json=bad
        )
    assert await _config_count(calibration_store, GOOGLE) == 0


async def test_propose_refuses_categorical_when_every_category_stays_review(
    calibration_store,
) -> None:
    """One true_match and one false_match sharing a category: precision 0.5
    (not enough to promote), and dropping the category would discard the
    only true match in it (NPV 0/1, not enough to drop). Nothing beyond the
    no-op default is recommended, so propose must refuse rather than write a
    config nobody can justify."""
    await seed_categorical_items(
        calibration_store,
        [
            ("true_match", "same_person", "full_match"),
            ("false_match", "lookalike", "full_match"),
        ],
    )
    with pytest.raises(ValueError, match="no recommendation"):
        await propose_config(
            calibration_store, GOOGLE, "v1", "google-cal-bad3", bands_json=None
        )
    assert await _config_count(calibration_store, GOOGLE) == 0


async def test_propose_succeeds_categorical_when_only_drop_is_supported(
    calibration_store,
) -> None:
    """No category reaches auto_confirm here, but page_match is five
    false_match items and zero true matches — dropping it costs nothing
    (NPV 1.0), so it IS a data-supported recommendation. propose must not
    refuse merely because auto_confirm is unreachable (that is the numeric
    branch's condition, not this one), and the rendered report must not
    claim the provider stays uncalibrated when a drop band is in fact being
    proposed — the report and the recommendation must never disagree."""
    items = (
        [("true_match", "same_person", "full_match")]
        + [("false_match", "lookalike", "full_match")]
        + [("false_match", "lookalike", "page_match")] * 5
    )
    await seed_categorical_items(calibration_store, items)

    config_id = await propose_config(
        calibration_store, GOOGLE, "v1", "google-cal-v6", bands_json=None
    )
    stored = await calibration_store.get_config(config_id)
    assert stored is not None
    assert stored.config.categorical_bands["page_match"] == "drop"
    assert "auto_confirm" not in stored.config.categorical_bands.values()

    _meta, _rows, uncovered, sweep = await load_sweep(calibration_store, GOOGLE, "v1")
    text = render_categorical_sweep(sweep, GOOGLE, "v1", uncovered)
    assert "stays uncalibrated" not in text
    assert "page_match=drop" in text
