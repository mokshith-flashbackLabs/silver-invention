"""``score/tick.py``'s single pass, against a fake store — no database.

Mirrors ``tests/test_recheck_loop.py``'s split: the scheduler (signals, sleep,
process lifecycle) is untestable without a real event loop and a real clock,
so what is actually asserted is the pass body — ``run_once`` — which the
brief calls out as factored specifically for this reason.
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from imageshield.score.store import ScoreResult
from imageshield.score.tick import run_once
from imageshield.types import UserRef


class FakeScoreStore:
    """Records every call. ``recompute`` never raises and never inspects its
    arguments beyond recording them — the pass body's job is to call it for
    every subject, not to interpret the result."""

    def __init__(self, subjects: tuple[UserRef, ...], *, expired: int = 0) -> None:
        self._subjects = subjects
        self._expired = expired
        self.expire_calls: list[datetime] = []
        self.recompute_calls: list[tuple[UserRef, str]] = []

    async def recompute(
        self,
        user_ref: UserRef,
        *,
        cause_kind: str,
        cause_ref: str | None = None,
        now: datetime | None = None,
    ) -> ScoreResult | None:
        self.recompute_calls.append((user_ref, cause_kind))
        return None

    async def get_score(self, user_ref: UserRef) -> dict[str, object] | None:
        raise NotImplementedError

    async def list_events(
        self, user_ref: UserRef, *, limit: int = 50
    ) -> list[dict[str, object]]:
        raise NotImplementedError

    async def all_subject_refs(self) -> tuple[UserRef, ...]:
        return self._subjects

    async def expire_due_threat_events(self, *, now: datetime) -> int:
        self.expire_calls.append(now)
        return self._expired


async def test_run_once_expires_then_recomputes_every_subject() -> None:
    subjects = (UserRef(uuid4()), UserRef(uuid4()), UserRef(uuid4()))
    store = FakeScoreStore(subjects, expired=2)

    await run_once(store)

    assert len(store.expire_calls) == 1
    assert store.recompute_calls == [(s, "tick") for s in subjects]


async def test_run_once_expires_before_recomputing() -> None:
    """Order matters: a threat event expiring THIS tick must be expired
    before the recompute that is supposed to reflect it runs — otherwise the
    recompute reads a stale 'still active' event and the tick has to wait a
    full cycle to catch up."""
    calls: list[str] = []

    class OrderTrackingStore(FakeScoreStore):
        async def expire_due_threat_events(self, *, now: datetime) -> int:
            calls.append("expire")
            return await super().expire_due_threat_events(now=now)

        async def recompute(
            self,
            user_ref: UserRef,
            *,
            cause_kind: str,
            cause_ref: str | None = None,
            now: datetime | None = None,
        ) -> ScoreResult | None:
            calls.append("recompute")
            return await super().recompute(
                user_ref, cause_kind=cause_kind, cause_ref=cause_ref, now=now
            )

    store = OrderTrackingStore((UserRef(uuid4()),))
    await run_once(store)

    assert calls == ["expire", "recompute"]


async def test_run_once_with_no_subjects_still_expires() -> None:
    store = FakeScoreStore(())

    await run_once(store)

    assert len(store.expire_calls) == 1
    assert store.recompute_calls == []


async def test_one_subjects_recompute_failing_does_not_starve_the_rest() -> None:
    """A store timeout or data anomaly unique to one subject must not stop
    the sweep -- every subject after the failing one still gets recomputed
    this tick, same isolation shape as
    ``admin_threat_events.py::_recompute_each``."""
    subjects = (UserRef(uuid4()), UserRef(uuid4()), UserRef(uuid4()))
    failing = subjects[1]

    class FlakyStore(FakeScoreStore):
        async def recompute(
            self,
            user_ref: UserRef,
            *,
            cause_kind: str,
            cause_ref: str | None = None,
            now: datetime | None = None,
        ) -> ScoreResult | None:
            if user_ref == failing:
                raise RuntimeError("boom")
            return await super().recompute(
                user_ref, cause_kind=cause_kind, cause_ref=cause_ref, now=now
            )

    store = FlakyStore(subjects)

    await run_once(store)  # must not raise

    assert store.recompute_calls == [
        (subjects[0], "tick"),
        (subjects[2], "tick"),
    ]
