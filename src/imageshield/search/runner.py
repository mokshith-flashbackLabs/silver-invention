"""Execute one claimed search run against its providers.

One provider failing never fails the run (CLAUDE.md §7.6): every adapter
outcome — matches, timeout, rate limit, or an unexpected exception — becomes
a ``provider_calls`` row, and the run always completes with
``providers_succeeded`` reflecting only what actually worked. A silent
provider outage must never look identical to "nothing found"
(INVARIANTS-adjacent: search_runs.providers_succeeded exists for exactly
this).

Step 8 puts the dispatch guard chain in front of every call. For each provider
in ``providers_attempted``, :func:`imageshield.providers.gate.decide` runs
ENABLED -> BREAKER -> BUDGET *before* the adapter is touched, and a refusal at
any of those becomes a ``provider_calls`` row with no API call and no cost. Three
consequences worth stating:

- **The guard runs here, not at request time.** ``POST /v1/search`` is 202 and
  enqueue-only, so a provider disabled between enqueue and dispatch would
  otherwise still be called. Running the chain at dispatch is what makes the kill
  switch take effect within ``PROVIDER_CONFIG_CACHE_SECONDS`` on runs that are
  already in flight.
- **A skipped provider stays in ``providers_attempted``** and is absent from
  ``providers_succeeded``, exactly like a timeout. Partial coverage has to be
  visible (CLAUDE.md §7.5).
- **The run still completes** with whatever the other providers returned.

After the run, the seed's scan tier is recomputed (``search/cadence.py``) — but
only when at least one provider actually succeeded. A run where everything was
skipped or timed out produced no evidence, and demoting a user's cadence because
our own integration was down would take the cost saving out of exactly the wrong
place.

``confirm`` (design doc §7) is the per-provider "most similar" criteria that
decides whether a hit this run wrote gets enqueued onto ``confirm:hits`` for
Rekognition-based triage ahead of human review. It is built once in
``search/worker.py:run_forever`` from config and threaded through every call
here unchanged; ``None`` disables the enqueue for that run (e.g. a caller that
does not want the confirm pipeline touched at all). The enqueue itself lives in
``store.complete_run`` so it commits in the same transaction as completion and
the cadence update.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime

import structlog
from pydantic import BaseModel, ConfigDict

from imageshield.calibration.models import BandingPolicy
from imageshield.confirm.models import ConfirmCriteria
from imageshield.providers.gate import decide
from imageshield.providers.models import Dispatch, Skip
from imageshield.providers.store import ProviderControlStore, utc_spend_date
from imageshield.search.cadence import CadenceInput, CadencePolicy, should_retier
from imageshield.search.models import ClaimedRun, ProviderDescriptor
from imageshield.search.provider import ProviderResult, SearchProvider
from imageshield.search.store import SearchStore
from imageshield.types import ProviderId

log = structlog.get_logger("imageshield.search")


class RunOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    providers_succeeded: tuple[ProviderId, ...]
    providers_skipped: tuple[ProviderId, ...]
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
            error_detail=f"adapter raised {type(exc).__name__}",
        )


def _missing_adapter_result(provider_id: ProviderId) -> ProviderResult:
    return ProviderResult(
        provider_id=provider_id,
        status="error",
        matches=[],
        raw_response={"error": "no adapter registered for this provider"},
        http_status=None,
        latency_ms=0,
        error_detail="no adapter registered for this provider",
    )


async def execute_run(
    claim: ClaimedRun,
    providers: Mapping[ProviderId, SearchProvider],
    store: SearchStore,
    policy: BandingPolicy,
    control: ProviderControlStore,
    cadence: CadencePolicy,
    *,
    confirm: ConfirmCriteria | None,
    now: datetime | None = None,
) -> RunOutcome:
    moment = now if now is not None else datetime.now(UTC)

    # Guard chain step 1 again, at dispatch. The route already refused ineligible
    # subjects before creating a run, but the flag is mutable and this run may
    # have sat queued (or had its claim go stale) for minutes since. Re-reading
    # it here is what makes "discovery never runs for an ineligible subject" true
    # of the whole pipeline rather than only of the request instant.
    #
    # Before any provider is touched, and before any spend or breaker state can
    # be consumed — the ordering rule from INVARIANTS #37 applies here exactly as
    # it does in the route.
    if not claim.discovery_eligible:
        await store.refuse_run(
            claim.run_id,
            claim.user_ref,
            reason="subject became ineligible between enqueue and dispatch",
        )
        log.warning(
            "search.run_refused",
            run_id=str(claim.run_id),
            outcome="discovery_not_available",
        )
        return RunOutcome(
            providers_succeeded=(), providers_skipped=(), matches_recorded=0
        )

    runtimes = await control.runtimes()

    # The guard chain, sequentially per provider. Sequential on purpose: the
    # budget read is one indexed row and the half-open claim is one conditional
    # UPDATE, so this is two cheap round trips per provider, and running them
    # concurrently would let two providers race for the same half-open probe
    # slot in a way the DB claim would then have to arbitrate anyway.
    decisions = [
        await decide(
            pid, runtime=runtimes.get(pid), store=control, now=moment
        )
        for pid in claim.providers_attempted
    ]

    dispatches = [d for d in decisions if isinstance(d, Dispatch)]
    skips = [d for d in decisions if isinstance(d, Skip)]

    for skip in skips:
        await control.record_skip(
            claim.run_id, skip.provider_id, skip.reason, skip.detail
        )

    tasks = [
        _call_provider(providers[d.provider_id], d.provider_id, claim.seed_url)
        if d.provider_id in providers
        else asyncio.sleep(0, result=_missing_adapter_result(d.provider_id))
        for d in dispatches
    ]
    results: list[ProviderResult] = list(await asyncio.gather(*tasks))
    cost_by_provider = {d.provider_id: d.cost_usd for d in dispatches}
    probe_by_provider = {d.provider_id: d.probe for d in dispatches}
    spend_date = utc_spend_date(moment)

    succeeded: list[ProviderId] = []
    matches_recorded = 0
    for result in results:
        # Call row + spend upsert + breaker transition, one transaction.
        await control.record_outcome(
            claim.run_id,
            result,
            cost_usd=cost_by_provider.get(result.provider_id),
            spend_date=spend_date,
            probe=probe_by_provider.get(result.provider_id, False),
        )
        if result.status != "ok":
            log.warning(
                "search.provider_failed",
                run_id=str(claim.run_id),
                provider_id=result.provider_id,
                status=result.status,
                http_status=result.http_status,
                attempts=result.attempts,
                error_detail=result.error_detail,
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

    # Completion and the cadence update commit together (see the store method):
    # a crash between them used to lose the tier change permanently, because the
    # completed run is no longer claimable and nothing would retry it.
    retier = (
        CadenceInput(
            found_matches=matches_recorded > 0, now=moment, policy=cadence
        )
        if should_retier(len(succeeded))
        else None
    )
    if retier is None:
        log.warning(
            "search.cadence_unchanged",
            run_id=str(claim.run_id),
            reason="no provider succeeded — this run is not evidence of an empty scan",
        )
    update = await store.complete_run(
        claim.run_id, claim.seed_id, succeeded, retier=retier, confirm=confirm
    )
    if update is not None:
        log.info(
            "search.cadence_updated",
            run_id=str(claim.run_id),
            seed_id=str(claim.seed_id),
            scan_tier=update.scan_tier,
            consecutive_empty_scans=update.consecutive_empty_scans,
            next_scan_after=update.next_scan_after.isoformat(),
        )

    log.info(
        "search.run_completed",
        run_id=str(claim.run_id),
        providers_attempted=list(claim.providers_attempted),
        providers_succeeded=[str(p) for p in succeeded],
        providers_skipped=[str(s.provider_id) for s in skips],
        matches_recorded=matches_recorded,
        cost_usd=str(
            sum(
                (cost_by_provider[r.provider_id] or 0) for r in results
                if r.provider_id in cost_by_provider
            )
        ),
    )
    return RunOutcome(
        providers_succeeded=tuple(succeeded),
        providers_skipped=tuple(s.provider_id for s in skips),
        matches_recorded=matches_recorded,
    )
