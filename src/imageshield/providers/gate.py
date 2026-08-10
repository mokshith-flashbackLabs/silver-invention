"""The dispatch guard chain — every pre-dispatch check, in one place.

::

    POST /v1/search
      1. ELIGIBILITY   subjects.discovery_eligible
                         no row  -> 409 subject_unknown
                         false   -> 403 discovery_not_available
                         Create NO search_run. Call NO provider. One audit row.
      2. ENABLED       providers.enabled = false -> skip that provider
      3. BREAKER       breaker_state = 'open'    -> skip, record in provider_calls
      4. BUDGET        spend + cost > daily      -> skip, status='budget_exceeded'
      5. DISPATCH

**The order is the design.** The cheapest and most absolute checks come first,
so an eligibility refusal can never consume budget or trip a breaker — it stops
before either exists.

One refinement the printed order does not show: the breaker step is split. Its
*read* (is this provider breaker-eligible at all) happens at position 3, but the
durable half-open **claim** happens last, immediately before dispatch. Claiming
at position 3 would let a BUDGET refusal at position 4 return a skip having
already burned the single probe, leaving the breaker in ``half_open`` with no
call made. The same ordering rule that keeps eligibility ahead of budget applies
inside the chain: a cheaper refusal must never consume a scarcer resource.

Step 1 refuses the whole request and lives in the route
(``http/routes/search.py``), because it has to happen before a ``search_runs``
row exists. Steps 2-4 are per-provider, never fail the run, and live here —
they run at *dispatch* time in the worker, which is also what makes the kill
switch bite on a run that was enqueued before the switch was flipped.

A skipped provider stays in ``search_runs.providers_attempted`` and is absent
from ``providers_succeeded``, exactly like a timeout. That asymmetry is not
sloppiness: partial coverage has to be visible, because a silent provider outage
would otherwise look identical to "no infringements found" (CLAUDE.md §7.5).

**Known bound on the budget check.** It is check-then-act: two runs dispatching
the same provider concurrently can both read the same ``provider_spend`` row and
both pass, so a daily budget can be overshot by at most
``(concurrent dispatches) x cost_per_call_usd`` — one call each, not a runaway,
because the upsert is transactional and the next read sees it. Closing the window
entirely would mean holding a row lock across a provider HTTP call that can run
for ``PROVIDER_TIMEOUT_SECONDS``, serialising every run in the fleet behind the
slowest provider. Cents of overshoot is the better trade; it is recorded here
rather than discovered later.
"""

from __future__ import annotations

from datetime import datetime

from imageshield.providers.budget import verdict as budget_verdict
from imageshield.providers.models import (
    DailySpend,
    Decision,
    Dispatch,
    ProviderRuntime,
    Skip,
)
from imageshield.providers.store import ProviderControlStore, spend_or_zero, utc_spend_date
from imageshield.types import ProviderId


async def decide(
    provider_id: ProviderId,
    *,
    runtime: ProviderRuntime | None,
    store: ProviderControlStore,
    now: datetime,
) -> Decision:
    """Run steps 2-4 for one provider. Never raises; never calls the provider.

    An unknown ``provider_id`` (a row deleted between run creation and dispatch)
    is treated as disabled rather than as an error: the run must still complete
    with the other provider's results.
    """
    if runtime is None:
        return Skip(
            provider_id=provider_id,
            reason="provider_disabled",
            detail="no provider row — treated as disabled so the run still completes",
        )

    # ── 2. ENABLED ───────────────────────────────────────────────────────
    if not runtime.enabled:
        return Skip(
            provider_id=provider_id,
            reason="provider_disabled",
            detail="kill switch: providers.enabled = false",
        )

    # ── 3. BREAKER (read-only part) ──────────────────────────────────────
    # Split deliberately in two. The *decision* that this provider is breaker-
    # eligible happens here; the durable half-open CLAIM happens after the
    # budget check, at the bottom of this function.
    #
    # Claiming here — the obvious reading of the printed chain order — is a bug:
    # the claim writes breaker_state='half_open', and a BUDGET refusal on the
    # next line would then return a Skip having already consumed the single
    # probe. Nothing releases it, so the breaker sits in 'half_open' until the
    # cooldown-plus-grace reclaim fires (and, before that reclaim existed, until
    # a human ran the admin reset). The guard chain's ordering rule is that a
    # cheaper, more absolute refusal must never consume a scarcer resource — a
    # probe is exactly such a resource.
    needs_probe = runtime.breaker_state == "open"
    if runtime.breaker_state == "half_open":
        # Someone else's probe is in flight. Skipping is the whole point of
        # half-open: one call, not one call per worker. A probe abandoned by a
        # dead worker is recovered by the reclaim inside claim_half_open_probe,
        # not by second-guessing it here.
        return Skip(
            provider_id=provider_id,
            reason="breaker_open",
            detail="half-open probe already in flight",
        )

    # ── 4. BUDGET ────────────────────────────────────────────────────────
    # One indexed row, read BEFORE dispatch because checking after the call means
    # the money is already spent.
    spend: DailySpend | None = await store.daily_spend(
        provider_id, utc_spend_date(now)
    )
    money = budget_verdict(
        cost_per_call_usd=runtime.cost_per_call_usd,
        daily_budget_usd=runtime.daily_budget_usd,
        spent_today_usd=spend_or_zero(spend),
    )
    if not money.allowed:
        # A budget-exceeded provider never fails the run. It is recorded in
        # providers_attempted but not in providers_succeeded, exactly like a
        # timeout. Note this returns BEFORE any probe was claimed.
        return Skip(
            provider_id=provider_id, reason="budget_exceeded", detail=money.detail
        )

    # ── 3b. BREAKER (the durable claim) ──────────────────────────────────
    # Last thing before dispatch, so the probe is consumed only when the call is
    # actually going to happen. Exactly one caller wins; everyone else skips.
    # The claim is a conditional UPDATE in Postgres, not a check here, because
    # "allow ONE probe" is meaningless across N workers otherwise.
    probe = False
    if needs_probe:
        probe = await store.claim_half_open_probe(provider_id)
        if not probe:
            return Skip(
                provider_id=provider_id,
                reason="breaker_open",
                detail=runtime.breaker_reason or "breaker open",
            )

    # ── 5. DISPATCH ──────────────────────────────────────────────────────
    # The cost travels with the decision so the recording path cannot charge a
    # different figure from the one the guard checked.
    return Dispatch(
        provider_id=provider_id, cost_usd=runtime.cost_per_call_usd, probe=probe
    )
