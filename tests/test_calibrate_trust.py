"""`calibrate trust` — the only writer of providers.calibrated."""

from __future__ import annotations

import pytest
from devtools.calibrate.__main__ import trust_provider

from imageshield.types import ProviderId

HIVE = ProviderId("hive")


async def test_trust_flips_calibrated_and_audits(
    sound_eval_set, banded_infringements
) -> None:
    store, _config_id = sound_eval_set
    await trust_provider(
        store, HIVE, trusted=True, actor="tester",
        reason="eval set v1 reviewed; 61 lookalikes sourced from consenting team",
    )
    assert (await store.provider_meta(HIVE)).calibrated is True
    async with store._pool.connection() as conn:
        cur = await conn.execute(
            "SELECT actor_type, action, metadata FROM audit_log "
            "WHERE action = 'calibration.trusted'"
        )
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0][2]["reason"].startswith("eval set v1 reviewed")
    assert rows[0][2]["actor"] == "tester"


async def test_trust_requires_a_reason(sound_eval_set) -> None:
    store, _ = sound_eval_set
    with pytest.raises(ValueError, match="reason"):
        await trust_provider(store, HIVE, trusted=True, actor="tester", reason="  ")


async def test_bands_move_off_review_only_after_both_keys(
    sound_eval_set, banded_infringements
) -> None:
    """The whole two-key design in one assertion."""
    from devtools.calibrate.__main__ import activate_config

    store, config_id = sound_eval_set
    await activate_config(store, config_id, activated_by="tester", min_items=200)
    await trust_provider(
        store, HIVE, trusted=True, actor="tester", reason="eval set reviewed"
    )
    async with store._pool.connection() as conn:
        cur = await conn.execute("SELECT DISTINCT band FROM attestations")
        bands = {r[0] for r in await cur.fetchall()}
    assert bands != {"review"}


async def test_revoke_sets_everything_back_to_review(
    sound_eval_set, banded_infringements
) -> None:
    """A safety flag you cannot withdraw is not one."""
    from devtools.calibrate.__main__ import activate_config

    store, config_id = sound_eval_set
    await activate_config(store, config_id, activated_by="tester", min_items=200)
    await trust_provider(store, HIVE, trusted=True, actor="tester", reason="ok")
    await trust_provider(
        store, HIVE, trusted=False, actor="tester", reason="eval set was not diverse"
    )
    assert (await store.provider_meta(HIVE)).calibrated is False
    async with store._pool.connection() as conn:
        cur = await conn.execute("SELECT DISTINCT band FROM attestations")
        assert {r[0] for r in await cur.fetchall()} == {"review"}
