"""handle_message: the idempotency contract under at-least-once delivery.

Delete-on-handled (including unclaimable duplicates), keep-on-crash so SQS
redelivers and the store's stale-claim window lets the retry reclaim."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

from imageshield.search.models import ClaimedRun
from imageshield.search.worker import handle_message
from imageshield.types import ProviderId
from tests.providers_fakes import CADENCE, FakeControlStore, make_seed, runtime

HIVE = ProviderId("hive")


class WorkerFakeStore:
    """claim_run yields the preloaded claim once; everything downstream of
    the claim is recorded so tests can assert execution happened (or not)."""

    def __init__(self, claim: ClaimedRun | None) -> None:
        self._claim = claim
        self.claim_requests: list[UUID] = []
        self.completed: list[UUID] = []
        self.cadence_updates: list[UUID] = []
        self.refusals: list[UUID] = []
        self.fail_execution = False
        self.seed = make_seed()

    async def claim_run(self, run_id: UUID) -> ClaimedRun | None:
        self.claim_requests.append(run_id)
        claim, self._claim = self._claim, None
        return claim

    async def get_seed(self, seed_id: UUID) -> Any:
        return self.seed

    async def refuse_run(
        self, run_id: UUID, user_ref: Any, *, reason: str
    ) -> None:
        self.refusals.append(run_id)

    async def record_infringements(self, *a: Any, **k: Any) -> int:
        return 0

    async def complete_run(
        self,
        run_id: UUID,
        seed_id: UUID,
        providers_succeeded: Sequence[ProviderId],
        *,
        retier: Any,
    ) -> Any:
        if self.fail_execution:
            raise RuntimeError("db went away")
        self.completed.append(run_id)
        if retier is not None:
            self.cadence_updates.append(seed_id)
        return None

    async def create_seed(self, *a: Any, **k: Any) -> UUID:
        raise NotImplementedError

    async def create_run(self, *a: Any, **k: Any) -> UUID:
        raise NotImplementedError

    async def get_run(self, *a: Any, **k: Any) -> Any:
        raise NotImplementedError

    async def enabled_provider_ids(self) -> tuple[ProviderId, ...]:
        raise NotImplementedError

    async def list_infringements(self, *a: Any, **k: Any) -> Any:
        raise NotImplementedError


def _claim(run_id: UUID, seed: Any) -> ClaimedRun:
    return ClaimedRun(
        run_id=run_id,
        seed_id=seed.seed_id,
        user_ref=seed.user_ref,
        seed_url=seed.source_object_uri,
        providers_attempted=(HIVE,),
        # No default on the model, deliberately: a caller must state it, so a
        # future claim path cannot forget to read the flag and silently dispatch.
        discovery_eligible=True,
    )


class FakeCalibrationStore:
    """Empty policy: rule 1 fires and every band is 'review' — this module
    doesn't exercise banding, only the claim/execute/delete contract."""

    async def load_active_policy(self) -> dict[Any, Any]:
        return {}


def control() -> FakeControlStore:
    """A fresh control plane per call. This module is not about the guard chain
    (tests/test_provider_gate.py is), so the provider is simply healthy."""
    return FakeControlStore({HIVE: runtime(HIVE)})


def _body(run_id: UUID, event: str = "search.run_requested") -> str:
    return json.dumps({"event": event, "id": str(run_id)})


async def test_valid_message_claims_executes_and_reports_handled() -> None:
    run_id = uuid4()
    store = WorkerFakeStore(None)
    store._claim = _claim(run_id, store.seed)

    handled = await handle_message(
        _body(run_id), store, {}, FakeCalibrationStore(), control(), CADENCE
    )

    assert handled is True
    assert store.claim_requests == [run_id]
    assert store.completed == [run_id]  # executed (no adapters -> error calls, still completes)


async def test_unclaimable_run_is_handled_without_execution() -> None:
    """Duplicate delivery of an already-completed run: ack and move on."""
    run_id = uuid4()
    store = WorkerFakeStore(claim=None)

    handled = await handle_message(
        _body(run_id), store, {}, FakeCalibrationStore(), control(), CADENCE
    )

    assert handled is True
    assert store.completed == []


async def test_unknown_event_and_malformed_body_are_poison_pills() -> None:
    store = WorkerFakeStore(claim=None)
    calibration_store = FakeCalibrationStore()
    assert (
        await handle_message(
            _body(uuid4(), event="something.else"),
            store,
            {},
            calibration_store,
            control(),
            CADENCE,
        )
        is True
    )
    assert (
        await handle_message(
            "not json at all", store, {}, calibration_store, control(), CADENCE
        )
        is True
    )
    assert (
        await handle_message(
            '{"event": "search.run_requested"}',
            store,
            {},
            calibration_store,
            control(),
            CADENCE,
        )
        is True
    )
    assert store.claim_requests == []  # never even attempted a claim


async def test_execution_failure_keeps_message_for_redelivery() -> None:
    run_id = uuid4()
    store = WorkerFakeStore(None)
    store._claim = _claim(run_id, store.seed)
    store.fail_execution = True

    handled = await handle_message(
        _body(run_id), store, {}, FakeCalibrationStore(), control(), CADENCE
    )

    assert handled is False  # not deleted -> SQS visibility timeout redelivers
    assert store.completed == []
