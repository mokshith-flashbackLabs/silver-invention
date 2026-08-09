"""`calibrate activate` — the floor, recomputed fresh.

Six refusal conditions, each asserted independently. The floor is in code so
loosening it is a code change with a review and a git blame.
"""

from __future__ import annotations

import argparse
from uuid import uuid4

import pytest
from devtools.calibrate.__main__ import activate_config, build_parser, check_floor

from imageshield.types import ProviderId

HIVE = ProviderId("hive")
MIN_ITEMS = 200


async def run_activate_with_store(
    store, args: argparse.Namespace, min_items: int = MIN_ITEMS
) -> int:
    """Mirrors `run_activate` against the fixture's store rather than building
    its own pool.

    ``min_items`` is a parameter here for the same reason it is NOT a CLI flag
    in `run_activate`: the real command sources the floor from
    ``Config.calibration_min_eval_items`` and offers no way to lower it from
    the command line. A `--min-items` flag used to exist and could carry a
    20-item eval set to `auto_confirm` on a trusted provider, which defeated
    the whole point of the floor living in code. If this helper ever starts
    reading the floor off `args` again, that flag has come back.
    """
    if not args.confirm:
        return 1
    try:
        await activate_config(
            store, args.config, activated_by=args.by, min_items=min_items
        )
    except ValueError:
        return 1
    return 0


def test_activate_has_no_flag_that_lowers_the_floor() -> None:
    """Permanent. The floor is the defence against a small or hard-negative-free
    eval set, and a command-line override turns "a code change with a review and
    a git blame" into a keystroke."""
    config_id = str(uuid4())
    base = ["activate", "--config", config_id, "--by", "tester", "--confirm"]

    args = build_parser().parse_args(base)
    assert not hasattr(args, "min_items")

    # argparse exits non-zero on an unrecognised option, which is the
    # behaviour that matters: there is no way to hand this command a lower
    # floor.
    with pytest.raises(SystemExit):
        build_parser().parse_args([*base, "--min-items", "1"])


async def test_a_sound_config_passes_the_floor(sound_eval_set) -> None:
    store, config_id = sound_eval_set
    stored = await store.get_config(config_id)
    result = await check_floor(store, stored, MIN_ITEMS)
    assert result.ok, result.problems


async def test_refuses_when_auto_confirm_precision_is_below_target(
    weak_precision_eval_set,
) -> None:
    store, config_id = weak_precision_eval_set
    result = await check_floor(store, await store.get_config(config_id), MIN_ITEMS)
    assert not result.ok
    assert any("precision" in p for p in result.problems)


async def test_refuses_when_drop_npv_is_below_target(weak_npv_eval_set) -> None:
    store, config_id = weak_npv_eval_set
    result = await check_floor(store, await store.get_config(config_id), MIN_ITEMS)
    assert not result.ok
    assert any("NPV" in p for p in result.problems)


async def test_refuses_below_the_minimum_sample_size(small_eval_set) -> None:
    store, config_id = small_eval_set
    result = await check_floor(store, await store.get_config(config_id), MIN_ITEMS)
    assert not result.ok
    assert any("sample size" in p for p in result.problems)


async def test_refuses_a_set_with_zero_lookalikes(no_lookalike_eval_set) -> None:
    """The condition that closes the real failure. A sweep over items with no
    hard negatives yields precision 1.0 trivially, because random negatives
    are easy to reject. The arithmetic passes; the measurement is meaningless.
    This refusal is unconditional and no sample size compensates for it."""
    store, config_id = no_lookalike_eval_set
    result = await check_floor(store, await store.get_config(config_id), MIN_ITEMS)
    assert not result.ok
    assert any("lookalike" in p for p in result.problems)


async def test_refuses_a_config_with_no_eval_set_id(orphan_config) -> None:
    store, config_id = orphan_config
    result = await check_floor(store, await store.get_config(config_id), MIN_ITEMS)
    assert not result.ok
    assert any("eval_set_id" in p for p in result.problems)


async def test_refuses_when_a_seed_was_never_successfully_observed(
    uncovered_eval_set,
) -> None:
    store, config_id = uncovered_eval_set
    result = await check_floor(store, await store.get_config(config_id), MIN_ITEMS)
    assert not result.ok
    assert any("coverage" in p for p in result.problems)


async def test_the_floor_ignores_the_measured_column(tampered_measured) -> None:
    """If the check trusted `measured`, editing a number in a JSONB column
    would defeat it. The data is in eval_observations; derive it there."""
    store, config_id = tampered_measured    # measured says precision 1.0; data says 0.5
    result = await check_floor(store, await store.get_config(config_id), MIN_ITEMS)
    assert not result.ok


async def test_a_review_only_config_skips_the_floor_entirely(
    review_only_config,
) -> None:
    """It alarms nobody, so there is nothing to gate."""
    store, config_id = review_only_config
    result = await check_floor(store, await store.get_config(config_id), MIN_ITEMS)
    assert result.ok


async def test_a_config_without_a_drop_band_is_not_held_to_drop_npv(
    no_drop_band_config,
) -> None:
    """Conditions 1 and 2 are each skipped when the config does not declare
    that band."""
    store, config_id = no_drop_band_config
    result = await check_floor(store, await store.get_config(config_id), MIN_ITEMS)
    assert not any("NPV" in p for p in result.problems)


# ── Activation itself ────────────────────────────────────────────────────

async def test_activate_requires_confirm(sound_eval_set) -> None:
    store, config_id = sound_eval_set
    args = build_parser().parse_args(
        ["activate", "--config", str(config_id), "--by", "tester"]
    )
    assert args.confirm is False
    # run_activate must return non-zero and write nothing without --confirm.
    before = await store.band_checksum()
    assert await run_activate_with_store(store, args) != 0
    assert await store.band_checksum() == before


async def test_activate_records_activated_by_and_flips_active(
    sound_eval_set, banded_infringements
) -> None:
    store, config_id = sound_eval_set
    await activate_config(store, config_id, activated_by="tester", min_items=MIN_ITEMS)
    stored = await store.get_config(config_id)
    assert stored.active is True
    async with store._pool.connection() as conn:
        cur = await conn.execute(
            "SELECT activated_by, activated_at FROM calibration_configs "
            "WHERE config_id = %s",
            (config_id,),
        )
        row = await cur.fetchone()
    assert row[0] == "tester"
    assert row[1] is not None


async def test_activate_stamps_calibration_version_on_every_rebanded_row(
    sound_eval_set, banded_infringements
) -> None:
    store, config_id = sound_eval_set
    await activate_config(store, config_id, activated_by="tester", min_items=MIN_ITEMS)
    async with store._pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM attestations "
            "WHERE provider_id = 'hive' AND calibration_version IS NULL"
        )
        row = await cur.fetchone()
    assert row[0] == 0


async def test_activate_never_touches_providers_calibrated(
    sound_eval_set, banded_infringements
) -> None:
    """The second key is a separate command. Sound config and may-alarm-people
    are different claims — the first is arithmetic, the second is judgement
    about whether the eval set resembles the real world, and no code can
    check that."""
    store, config_id = sound_eval_set
    before = (await store.provider_meta(HIVE)).calibrated
    await activate_config(store, config_id, activated_by="tester", min_items=MIN_ITEMS)
    assert (await store.provider_meta(HIVE)).calibrated == before


async def test_activating_a_second_config_deactivates_the_first(
    two_sound_configs,
) -> None:
    store, first, second = two_sound_configs
    await activate_config(store, first, activated_by="tester", min_items=MIN_ITEMS)
    await activate_config(store, second, activated_by="tester", min_items=MIN_ITEMS)
    assert (await store.get_config(first)).active is False
    assert (await store.get_config(second)).active is True


async def test_bands_stay_review_while_the_provider_is_untrusted(
    sound_eval_set, banded_infringements
) -> None:
    """Activation alone changes nothing a user sees: rule 2 still forces
    review until `trust` runs. Two keys, both required."""
    store, config_id = sound_eval_set
    await activate_config(store, config_id, activated_by="tester", min_items=MIN_ITEMS)
    async with store._pool.connection() as conn:
        cur = await conn.execute("SELECT DISTINCT band FROM attestations")
        bands = {r[0] for r in await cur.fetchall()}
    assert bands == {"review"}
