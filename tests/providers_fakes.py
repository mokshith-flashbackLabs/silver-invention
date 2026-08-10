"""Shared in-memory fakes for the step-8 provider control plane.

Here rather than duplicated per test file because several done-when items are
about what was **not** called — "no provider client is invoked", "makes NO API
call" — and a fake that records invocations is only convincing if every test
asserts against the same one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID, uuid4

from imageshield.providers.models import DailySpend, ProviderRuntime, SkipReason
from imageshield.search.cadence import (
    CadenceInput,
    CadencePolicy,
    CadenceUpdate,
    update_for,
)
from imageshield.search.models import ClaimedRun, ProviderDescriptor, SeedRow
from imageshield.search.provider import ProviderMatch, ProviderResult
from imageshield.types import ProviderId, UserRef

HIVE = ProviderId("hive")
GOOGLE = ProviderId("google")

CADENCE = CadencePolicy(
    standard_days=7,
    relaxed_days=14,
    dormant_days=30,
    new_tier_weeks=4,
    relaxed_after_empty=8,
    dormant_after_empty=20,
    priority_release_after_empty=13,
)


def runtime(
    provider_id: ProviderId = HIVE,
    *,
    enabled: bool = True,
    cost: str | None = "0.0035",
    daily_budget: str | None = None,
    monthly_budget: str | None = None,
    breaker_state: Literal["closed", "open", "half_open"] = "closed",
    breaker_opened_at: datetime | None = None,
    failures: int = 0,
    cooldown: int | None = None,
) -> ProviderRuntime:
    return ProviderRuntime(
        provider_id=provider_id,
        enabled=enabled,
        cost_per_call_usd=Decimal(cost) if cost is not None else None,
        daily_budget_usd=Decimal(daily_budget) if daily_budget is not None else None,
        monthly_budget_usd=(
            Decimal(monthly_budget) if monthly_budget is not None else None
        ),
        rate_limit_per_min=None,
        breaker_state=breaker_state,
        breaker_opened_at=breaker_opened_at,
        breaker_reason="timeout" if breaker_state != "closed" else None,
        breaker_consecutive_failures=failures,
        breaker_cooldown_seconds=cooldown,
    )


class FakeControlStore:
    """Records every guard read and every write, and calls nothing."""

    def __init__(
        self,
        runtimes_by_id: Mapping[ProviderId, ProviderRuntime] | None = None,
        *,
        spend: Mapping[ProviderId, str] | None = None,
        half_open_grants: set[ProviderId] | None = None,
    ) -> None:
        self._runtimes = dict(runtimes_by_id or {HIVE: runtime(HIVE)})
        self._spend = {k: Decimal(v) for k, v in (spend or {}).items()}
        self._half_open_grants = half_open_grants or set()
        self.outcomes: list[tuple[UUID, ProviderResult, Decimal | None]] = []
        self.skips: list[tuple[UUID, ProviderId, SkipReason, str]] = []
        self.probes: list[tuple[ProviderId, bool]] = []
        self.half_open_claims: list[ProviderId] = []
        self.enabled_writes: list[tuple[ProviderId, bool, str]] = []
        self.breaker_resets: list[ProviderId] = []

    async def runtimes(self) -> Mapping[ProviderId, ProviderRuntime]:
        return dict(self._runtimes)

    async def daily_spend(
        self, provider_id: ProviderId, spend_date: date
    ) -> DailySpend | None:
        if provider_id not in self._spend:
            return None
        return DailySpend(
            provider_id=provider_id,
            spend_date=spend_date,
            call_count=1,
            cost_usd=self._spend[provider_id],
        )

    async def claim_half_open_probe(self, provider_id: ProviderId) -> bool:
        self.half_open_claims.append(provider_id)
        return provider_id in self._half_open_grants

    async def record_outcome(
        self,
        run_id: UUID,
        result: ProviderResult,
        *,
        cost_usd: Decimal | None,
        spend_date: date,
        probe: bool = False,
    ) -> None:
        self.outcomes.append((run_id, result, cost_usd))
        self.probes.append((result.provider_id, probe))

    async def record_skip(
        self, run_id: UUID, provider_id: ProviderId, reason: SkipReason, detail: str
    ) -> None:
        self.skips.append((run_id, provider_id, reason, detail))

    async def set_enabled(
        self, provider_id: ProviderId, enabled: bool, *, actor: str, reason: str
    ) -> bool:
        if provider_id not in self._runtimes:
            return False
        self.enabled_writes.append((provider_id, enabled, reason))
        return True

    async def reset_breaker(
        self, provider_id: ProviderId, *, actor: str, reason: str
    ) -> bool:
        if provider_id not in self._runtimes:
            return False
        self.breaker_resets.append(provider_id)
        return True


class RecordingProvider:
    """A provider adapter that records every invocation.

    Several done-when items are assertions about invocation, not about cost
    written after the fact: "assert with a mocked client that records
    invocations, not by inspecting cost after the fact".
    """

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


def ok_result(
    provider_id: ProviderId, urls: Sequence[str], *, attempts: int = 1
) -> ProviderResult:
    return ProviderResult(
        provider_id=provider_id,
        status="ok",
        matches=[
            ProviderMatch(
                image_url=url,
                page_urls=[],
                provider_score=Decimal("0.9"),
                provider_category=None,
                query_quality=None,
            )
            for url in urls
        ],
        raw_response={"matches": list(urls)},
        http_status=200,
        latency_ms=5,
        attempts=attempts,
    )


def timeout_result(provider_id: ProviderId) -> ProviderResult:
    return ProviderResult(
        provider_id=provider_id,
        status="timeout",
        matches=[],
        raw_response={"exception": "read timeout"},
        http_status=None,
        latency_ms=5000,
        error_detail="request timed out",
    )


class FakeSeedStore:
    """The slice of SearchStore ``execute_run`` touches, plus cadence."""

    def __init__(self, seed: SeedRow | None = None) -> None:
        self.seed = seed if seed is not None else make_seed()
        self.matches: list[tuple[UUID, ProviderDescriptor, Sequence[ProviderMatch]]] = []
        self.completed: list[tuple[UUID, tuple[ProviderId, ...]]] = []
        self.cadence_updates: list[tuple[UUID, CadenceUpdate]] = []
        self.refusals: list[tuple[UUID, UserRef, str]] = []

    async def get_seed(self, seed_id: UUID) -> SeedRow | None:
        return self.seed if seed_id == self.seed.seed_id else None

    async def refuse_run(
        self, run_id: UUID, user_ref: UserRef, *, reason: str
    ) -> None:
        self.refusals.append((run_id, user_ref, reason))

    async def record_infringements(
        self,
        run_id: UUID,
        user_ref: UserRef,
        provider: ProviderDescriptor,
        matches: Sequence[ProviderMatch],
        policy: Any,
    ) -> int:
        self.matches.append((run_id, provider, matches))
        return len(matches)

    async def complete_run(
        self,
        run_id: UUID,
        seed_id: UUID,
        providers_succeeded: Sequence[ProviderId],
        *,
        retier: CadenceInput | None,
    ) -> CadenceUpdate | None:
        """Mirrors the real store: completion and re-tiering are ONE call, so a
        test cannot accidentally assert a cadence write that the production path
        could not have made independently of the completion."""
        self.completed.append((run_id, tuple(providers_succeeded)))
        if retier is None:
            return None
        update = update_for(
            current=self.seed.scan_tier,
            consecutive_empty_scans=self.seed.consecutive_empty_scans,
            found_matches=retier.found_matches,
            seed_age_days=(retier.now - self.seed.created_at).days,
            now=retier.now,
            policy=retier.policy,
        )
        self.cadence_updates.append((seed_id, update))
        return update

    # Unused SearchStore surface, present so the fake satisfies the Protocol.
    async def create_seed(self, *a: Any, **k: Any) -> UUID:
        raise NotImplementedError

    async def create_run(self, *a: Any, **k: Any) -> UUID:
        raise NotImplementedError

    async def get_run(self, *a: Any, **k: Any) -> Any:
        raise NotImplementedError

    async def claim_run(self, *a: Any, **k: Any) -> Any:
        raise NotImplementedError

    async def enabled_provider_ids(self) -> tuple[ProviderId, ...]:
        raise NotImplementedError

    async def list_infringements(self, *a: Any, **k: Any) -> Any:
        raise NotImplementedError


def make_seed(**overrides: Any) -> SeedRow:
    defaults: dict[str, Any] = {
        "seed_id": uuid4(),
        "user_ref": UserRef(uuid4()),
        "seed_kind": "user_supplied",
        "source_object_uri": "https://s3/seed.jpg",
        "status": "active",
        "created_at": datetime.now(UTC),
    }
    return SeedRow(**{**defaults, **overrides})


def make_claim(
    providers: tuple[ProviderId, ...],
    seed: SeedRow | None = None,
    *,
    discovery_eligible: bool = True,
) -> ClaimedRun:
    resolved = seed if seed is not None else make_seed()
    return ClaimedRun(
        run_id=uuid4(),
        seed_id=resolved.seed_id,
        user_ref=resolved.user_ref,
        seed_url=resolved.source_object_uri,
        providers_attempted=providers,
        discovery_eligible=discovery_eligible,
    )
