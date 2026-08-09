"""Execute one claimed search run against its providers.

One provider failing never fails the run (CLAUDE.md §7.6): every adapter
outcome — matches, timeout, rate limit, or an unexpected exception — becomes
a ``provider_calls`` row, and the run always completes with
``providers_succeeded`` reflecting only what actually worked. A silent
provider outage must never look identical to "nothing found"
(INVARIANTS-adjacent: search_runs.providers_succeeded exists for exactly
this).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import structlog
from pydantic import BaseModel, ConfigDict

from imageshield.calibration.models import BandingPolicy
from imageshield.search.models import ClaimedRun, ProviderDescriptor
from imageshield.search.provider import ProviderResult, SearchProvider
from imageshield.search.store import SearchStore
from imageshield.types import ProviderId

log = structlog.get_logger("imageshield.search")


class RunOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    providers_succeeded: tuple[ProviderId, ...]
    matches_recorded: int


async def _call_provider(
    provider: SearchProvider, provider_id: ProviderId, seed_url: str
) -> ProviderResult:
    """Adapters already convert their own HTTP failures into results; this
    wrapper is the belt for anything unexpected, so one provider blowing up
    can never take the gather() — and with it the whole run — down."""
    try:
        return await provider.search(seed_url)
    except Exception as exc:  # broad on purpose: isolate failures per provider
        log.error("search.provider_crashed", provider_id=provider_id, error=str(exc))
        return ProviderResult(
            provider_id=provider_id,
            status="error",
            matches=[],
            raw_response={"exception": str(exc)},
            http_status=None,
            latency_ms=0,
        )


def _missing_adapter_result(provider_id: ProviderId) -> ProviderResult:
    return ProviderResult(
        provider_id=provider_id,
        status="error",
        matches=[],
        raw_response={"error": "no adapter registered for this provider"},
        http_status=None,
        latency_ms=0,
    )


async def execute_run(
    claim: ClaimedRun,
    providers: Mapping[ProviderId, SearchProvider],
    store: SearchStore,
    policy: BandingPolicy,
) -> RunOutcome:
    tasks = [
        _call_provider(providers[pid], pid, claim.seed_url)
        if pid in providers
        else asyncio.sleep(0, result=_missing_adapter_result(pid))
        for pid in claim.providers_attempted
    ]
    results: list[ProviderResult] = list(await asyncio.gather(*tasks))

    succeeded: list[ProviderId] = []
    matches_recorded = 0
    for result in results:
        await store.record_provider_call(claim.run_id, result)
        if result.status != "ok":
            log.warning(
                "search.provider_failed",
                run_id=str(claim.run_id),
                provider_id=result.provider_id,
                status=result.status,
                http_status=result.http_status,
            )
            continue
        succeeded.append(result.provider_id)
        adapter = providers[result.provider_id]
        matches_recorded += await store.record_infringements(
            claim.run_id,
            claim.user_ref,
            ProviderDescriptor(
                provider_id=result.provider_id,
                score_kind=adapter.score_kind,
                score_version=adapter.score_version,
            ),
            result.matches,
            policy,
        )

    await store.complete_run(claim.run_id, succeeded)
    log.info(
        "search.run_completed",
        run_id=str(claim.run_id),
        providers_attempted=list(claim.providers_attempted),
        providers_succeeded=[str(p) for p in succeeded],
        matches_recorded=matches_recorded,
    )
    return RunOutcome(
        providers_succeeded=tuple(succeeded), matches_recorded=matches_recorded
    )
