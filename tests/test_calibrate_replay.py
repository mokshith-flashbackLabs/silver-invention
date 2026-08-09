"""`calibrate replay` — read-only, verified by checksum.

This command is the difference between "we tightened the threshold" and "we
tightened the threshold and 340 users will lose an alert they have already
seen." If it can write, it is not that.
"""

from __future__ import annotations

from devtools.calibrate.__main__ import plan_reband

from imageshield.types import ProviderId

HIVE = ProviderId("hive")


async def test_replay_writes_nothing(calibration_store, banded_infringements) -> None:
    """Row counts and a checksum over every mutable banding column, before
    and after. Both must be identical."""
    before_counts = await banded_infringements.counts()
    before_sum = await calibration_store.band_checksum()

    plan = await plan_reband(calibration_store, HIVE, banded_infringements.entry())
    assert plan.attestations_changed > 0   # the plan is not vacuous

    assert await banded_infringements.counts() == before_counts
    assert await calibration_store.band_checksum() == before_sum


async def test_replay_reports_the_delta_by_direction(
    calibration_store, banded_infringements
) -> None:
    plan = await plan_reband(calibration_store, HIVE, banded_infringements.entry())
    assert plan.attestations_changed >= 1
    assert plan.infringements_changed >= 1
    assert set(plan.by_direction) <= {
        "review->auto_confirm", "review->drop", "auto_confirm->review",
        "drop->review", "auto_confirm->drop", "drop->auto_confirm",
    }


async def test_replay_counts_distinct_users_affected(
    calibration_store, banded_infringements
) -> None:
    """The number a human actually needs: how many people's reports change."""
    plan = await plan_reband(calibration_store, HIVE, banded_infringements.entry())
    assert plan.users_affected >= 1
