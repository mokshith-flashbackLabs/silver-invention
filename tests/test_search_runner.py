"""execute_run over fake adapters and a fake store — the spec's done-when in
miniature: one provider timing out still completes the run with the other's
results, and providers_succeeded reflects only the one that worked."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID, uuid4

from imageshield.search.models import ClaimedRun, ProviderDescriptor
from imageshield.search.provider import ProviderMatch, ProviderResult
from imageshield.search.runner import execute_run
from imageshield.types import ProviderId, UserRef

HIVE = ProviderId("hive")
GOOGLE = ProviderId("google")


def _match(url: str) -> ProviderMatch:
    return ProviderMatch(
        image_url=url,
        page_url=None,
        provider_score=Decimal("0.9"),
        provider_category=None,
        query_quality=None,
    )


class FakeProvider:
    kind: Literal["image_search", "face_search", "classifier"] = "image_search"
    score_kind: Literal["numeric", "categorical"] = "numeric"
    score_version = "fake-v1"

    def __init__(
        self,
        provider_id: ProviderId,
        result: ProviderResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.id = provider_id
        self._result = result
        self._error = error
        self.calls: list[str] = []

    async def search(
        self, seed_url: str, max_results: int | None = None
    ) -> ProviderResult:
        self.calls.append(seed_url)
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


class FakeStore:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, ProviderResult]] = []
        self.matches: list[tuple[UUID, ProviderDescriptor, Sequence[ProviderMatch]]] = []
        self.completed: list[tuple[UUID, tuple[ProviderId, ...]]] = []

    async def record_provider_call(self, run_id: UUID, result: ProviderResult) -> None:
        self.calls.append((run_id, result))

    async def record_matches(
        self,
        run_id: UUID,
        user_ref: UserRef,
        provider: ProviderDescriptor,
        matches: Sequence[ProviderMatch],
    ) -> int:
        self.matches.append((run_id, provider, matches))
        return len(matches)

    async def complete_run(
        self, run_id: UUID, providers_succeeded: Sequence[ProviderId]
    ) -> None:
        self.completed.append((run_id, tuple(providers_succeeded)))

    # unused SearchStore surface, present so the fake satisfies the Protocol
    async def create_seed(self, *a: Any, **k: Any) -> UUID:
        raise NotImplementedError

    async def get_seed(self, *a: Any, **k: Any) -> Any:
        raise NotImplementedError

    async def create_run(self, *a: Any, **k: Any) -> UUID:
        raise NotImplementedError

    async def get_run(self, *a: Any, **k: Any) -> Any:
        raise NotImplementedError

    async def claim_run(self, *a: Any, **k: Any) -> Any:
        raise NotImplementedError

    async def enabled_provider_ids(self) -> tuple[ProviderId, ...]:
        raise NotImplementedError

    async def list_matches(self, *a: Any, **k: Any) -> Any:
        raise NotImplementedError


def _claim(providers: tuple[ProviderId, ...]) -> ClaimedRun:
    return ClaimedRun(
        run_id=uuid4(),
        user_ref=UserRef(uuid4()),
        seed_url="https://s3/seed.jpg",
        providers_attempted=providers,
    )


def _ok(provider_id: ProviderId, urls: list[str]) -> ProviderResult:
    return ProviderResult(
        provider_id=provider_id,
        status="ok",
        matches=[_match(u) for u in urls],
        raw_response={"matches": urls},
        http_status=200,
        latency_ms=5,
    )


def _timeout(provider_id: ProviderId) -> ProviderResult:
    return ProviderResult(
        provider_id=provider_id,
        status="timeout",
        matches=[],
        raw_response={"exception": "read timeout"},
        http_status=None,
        latency_ms=5000,
    )


async def test_one_provider_timing_out_still_completes_with_the_others_results() -> None:
    hive = FakeProvider(HIVE, result=_ok(HIVE, ["https://x/a.jpg", "https://x/b.jpg"]))
    google = FakeProvider(GOOGLE, result=_timeout(GOOGLE))
    store = FakeStore()
    claim = _claim((HIVE, GOOGLE))

    outcome = await execute_run(claim, {HIVE: hive, GOOGLE: google}, store)

    assert outcome.providers_succeeded == (HIVE,)
    assert outcome.matches_recorded == 2
    assert hive.calls == ["https://s3/seed.jpg"]
    assert google.calls == ["https://s3/seed.jpg"]
    # both calls recorded — a timeout is a provider_calls row, never silence
    assert {r.status for _, r in store.calls} == {"ok", "timeout"}
    # matches recorded only for the ok provider
    assert [p.provider_id for _, p, _ in store.matches] == [HIVE]
    assert store.completed == [(claim.run_id, (HIVE,))]


async def test_adapter_raising_becomes_error_result_not_a_failed_run() -> None:
    hive = FakeProvider(HIVE, error=RuntimeError("connection pool exploded"))
    google = FakeProvider(GOOGLE, result=_ok(GOOGLE, ["https://x/c.jpg"]))
    store = FakeStore()
    claim = _claim((HIVE, GOOGLE))

    outcome = await execute_run(claim, {HIVE: hive, GOOGLE: google}, store)

    assert outcome.providers_succeeded == (GOOGLE,)
    hive_calls = [r for _, r in store.calls if r.provider_id == HIVE]
    assert hive_calls[0].status == "error"
    assert "connection pool exploded" in str(hive_calls[0].raw_response)
    assert store.completed == [(claim.run_id, (GOOGLE,))]


async def test_attempted_provider_without_adapter_is_visible_error() -> None:
    google = FakeProvider(GOOGLE, result=_ok(GOOGLE, []))
    store = FakeStore()
    claim = _claim((HIVE, GOOGLE))

    outcome = await execute_run(claim, {GOOGLE: google}, store)

    assert outcome.providers_succeeded == (GOOGLE,)
    hive_calls = [r for _, r in store.calls if r.provider_id == HIVE]
    assert len(hive_calls) == 1
    assert hive_calls[0].status == "error"  # visible, never silently skipped
