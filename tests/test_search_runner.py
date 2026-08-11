"""``execute_run`` over fake adapters, a fake store and a fake control plane.

Two specs in miniature:

- **step 5** — one provider timing out still completes the run with the other's
  results, and ``providers_succeeded`` reflects only what worked.
- **step 8** — the dispatch guard chain runs BEFORE any adapter is touched, so a
  disabled / breaker-open / budget-exhausted provider is never called at all.
  Every assertion about that is an assertion about ``RecordingProvider.calls``,
  not about cost inspected after the fact.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from imageshield.search.runner import execute_run
from tests.providers_fakes import (
    CADENCE,
    GOOGLE,
    HIVE,
    FakeControlStore,
    FakeSeedStore,
    RecordingProvider,
    fetch_failed_result,
    make_claim,
    make_seed,
    ok_result,
    runtime,
    timeout_result,
)

BOTH = {HIVE: runtime(HIVE), GOOGLE: runtime(GOOGLE)}


class _Any:
    """Matches any value in an equality assertion. Used where the exact prose of
    a reason string is not the thing under test."""

    def __eq__(self, other: object) -> bool:
        return True

    def __hash__(self) -> int:
        return 0

    def __repr__(self) -> str:
        return "<any>"


ANY_REASON = _Any()


async def test_a_subject_who_became_ineligible_is_refused_at_dispatch() -> None:
    """The route's 403 only covers the instant of the request.

    `POST /v1/search` is 202-and-enqueue-only, and `subjects.discovery_eligible`
    is deliberately mutable — a DOB correction at re-enrolment writes it. A queued
    backlog, or a dead worker whose claim goes stale for _STALE_CLAIM_MINUTES,
    puts minutes between the check and the dispatch. Without re-reading the flag
    on the claim, a run enqueued while the subject was eligible would dispatch
    against a minor, write infringements, and complete — which is the exact
    outcome INVARIANTS #8b exists to make impossible.

    The run is marked 'refused', NOT 'completed': a completed run with zero
    results says "we looked and found nothing" about a search that never ran.
    """
    hive = RecordingProvider(HIVE, result=ok_result(HIVE, ["https://x/a"]))
    store, control = FakeSeedStore(), FakeControlStore(BOTH)
    claim = make_claim((HIVE, GOOGLE), store.seed, discovery_eligible=False)

    outcome = await execute_run(claim, {HIVE: hive}, store, {}, control, CADENCE)

    assert hive.calls == []  # no provider client invoked
    assert outcome.providers_succeeded == ()
    assert outcome.matches_recorded == 0
    assert store.refusals == [(claim.run_id, claim.user_ref, ANY_REASON)]
    # Nothing that could reassure a reader, and nothing consumed:
    assert store.completed == []  # not completed
    assert store.matches == []  # no infringements
    assert store.cadence_updates == []  # cadence untouched
    assert control.outcomes == []  # no spend
    assert control.skips == []  # not even a per-provider skip row
    assert control.half_open_claims == []  # no breaker state consumed


async def test_one_provider_timing_out_still_completes_with_the_others_results() -> None:
    hive = RecordingProvider(HIVE, result=ok_result(HIVE, ["https://x/a", "https://x/b"]))
    google = RecordingProvider(GOOGLE, result=timeout_result(GOOGLE))
    store, control = FakeSeedStore(), FakeControlStore(BOTH)
    claim = make_claim((HIVE, GOOGLE), store.seed)

    outcome = await execute_run(
        claim, {HIVE: hive, GOOGLE: google}, store, {}, control, CADENCE
    )

    assert outcome.providers_succeeded == (HIVE,)
    assert outcome.matches_recorded == 2
    assert hive.calls == [claim.seed_url]
    assert google.calls == [claim.seed_url]
    # Both calls recorded — a timeout is a provider_calls row, never silence.
    assert {r.status for _, r, _ in control.outcomes} == {"ok", "timeout"}
    assert [p.provider_id for _, p, _ in store.matches] == [HIVE]
    assert store.completed == [(claim.run_id, (HIVE,))]


async def test_adapter_raising_becomes_error_result_not_a_failed_run() -> None:
    hive = RecordingProvider(HIVE, error=RuntimeError("connection pool exploded"))
    google = RecordingProvider(GOOGLE, result=ok_result(GOOGLE, ["https://x/c"]))
    store, control = FakeSeedStore(), FakeControlStore(BOTH)
    claim = make_claim((HIVE, GOOGLE), store.seed)

    outcome = await execute_run(
        claim, {HIVE: hive, GOOGLE: google}, store, {}, control, CADENCE
    )

    assert outcome.providers_succeeded == (GOOGLE,)
    [hive_result] = [r for _, r, _ in control.outcomes if r.provider_id == HIVE]
    assert hive_result.status == "error"
    assert "connection pool exploded" in str(hive_result.raw_response)
    assert store.completed == [(claim.run_id, (GOOGLE,))]


async def test_attempted_provider_without_adapter_is_visible_error() -> None:
    google = RecordingProvider(GOOGLE, result=ok_result(GOOGLE, []))
    store, control = FakeSeedStore(), FakeControlStore(BOTH)
    claim = make_claim((HIVE, GOOGLE), store.seed)

    outcome = await execute_run(claim, {GOOGLE: google}, store, {}, control, CADENCE)

    assert outcome.providers_succeeded == (GOOGLE,)
    [hive_result] = [r for _, r, _ in control.outcomes if r.provider_id == HIVE]
    assert hive_result.status == "error"  # visible, never silently skipped


# ── Step 8: the guard chain runs before dispatch ──────────────────────────


async def test_disabled_provider_is_never_called_and_the_run_still_completes() -> None:
    hive = RecordingProvider(HIVE, result=ok_result(HIVE, ["https://x/a"]))
    google = RecordingProvider(GOOGLE, result=ok_result(GOOGLE, ["https://x/b"]))
    store = FakeSeedStore()
    control = FakeControlStore({HIVE: runtime(HIVE), GOOGLE: runtime(GOOGLE, enabled=False)})
    claim = make_claim((HIVE, GOOGLE), store.seed)

    outcome = await execute_run(
        claim, {HIVE: hive, GOOGLE: google}, store, {}, control, CADENCE
    )

    assert google.calls == []  # the whole point: no API call
    assert hive.calls == [claim.seed_url]
    assert outcome.providers_succeeded == (HIVE,)
    assert outcome.providers_skipped == (GOOGLE,)
    assert [(pid, reason) for _, pid, reason, _ in control.skips] == [
        (GOOGLE, "provider_disabled")
    ]
    # Attempted-but-not-succeeded: partial coverage stays visible.
    assert claim.providers_attempted == (HIVE, GOOGLE)
    assert store.completed == [(claim.run_id, (HIVE,))]


async def test_budget_exceeded_makes_no_api_call() -> None:
    """Step-8 done-when, asserted on invocations rather than on cost after the
    fact: exceeding daily_budget_usd makes NO API call."""
    hive = RecordingProvider(HIVE, result=ok_result(HIVE, ["https://x/a"]))
    store = FakeSeedStore()
    control = FakeControlStore(
        {HIVE: runtime(HIVE, cost="1.00", daily_budget="10.00")},
        spend={HIVE: "10.00"},
    )
    claim = make_claim((HIVE,), store.seed)

    outcome = await execute_run(claim, {HIVE: hive}, store, {}, control, CADENCE)

    assert hive.calls == []
    assert control.outcomes == []  # no call row from record_outcome: nothing ran
    assert [(pid, reason) for _, pid, reason, _ in control.skips] == [
        (HIVE, "budget_exceeded")
    ]
    assert outcome.providers_succeeded == ()
    assert outcome.providers_skipped == (HIVE,)


async def test_open_breaker_within_cooldown_is_never_called() -> None:
    hive = RecordingProvider(HIVE, result=ok_result(HIVE, ["https://x/a"]))
    store = FakeSeedStore()
    control = FakeControlStore(
        {
            HIVE: runtime(
                HIVE,
                breaker_state="open",
                breaker_opened_at=datetime.now(UTC),
                failures=5,
                cooldown=300,
            )
        },
        half_open_grants=set(),  # the DB claim refuses: still cooling down
    )
    claim = make_claim((HIVE,), store.seed)

    await execute_run(claim, {HIVE: hive}, store, {}, control, CADENCE)

    assert hive.calls == []
    assert control.half_open_claims == [HIVE]  # it did try to claim a probe
    assert [reason for _, _, reason, _ in control.skips] == ["breaker_open"]


async def test_half_open_probe_is_dispatched_when_the_claim_is_won() -> None:
    hive = RecordingProvider(HIVE, result=ok_result(HIVE, ["https://x/a"]))
    store = FakeSeedStore()
    control = FakeControlStore(
        {
            HIVE: runtime(
                HIVE,
                breaker_state="open",
                breaker_opened_at=datetime.now(UTC) - timedelta(seconds=600),
                failures=5,
                cooldown=300,
            )
        },
        half_open_grants={HIVE},
    )
    claim = make_claim((HIVE,), store.seed)

    outcome = await execute_run(claim, {HIVE: hive}, store, {}, control, CADENCE)

    assert hive.calls == [claim.seed_url]  # exactly one probe
    assert outcome.providers_succeeded == (HIVE,)
    assert control.skips == []


# ── Step 8: cadence ──────────────────────────────────────────────────────


async def test_a_non_empty_scan_promotes_to_priority_and_resets_the_counter() -> None:
    seed = make_seed(scan_tier="relaxed", consecutive_empty_scans=9)
    store = FakeSeedStore(seed)
    hive = RecordingProvider(HIVE, result=ok_result(HIVE, ["https://x/a"]))
    control = FakeControlStore({HIVE: runtime(HIVE)})

    await execute_run(
        make_claim((HIVE,), seed), {HIVE: hive}, store, {}, control, CADENCE
    )

    [(seed_id, update)] = store.cadence_updates
    assert seed_id == seed.seed_id
    assert update.scan_tier == "priority"
    assert update.consecutive_empty_scans == 0


async def test_an_empty_scan_increments_and_demotes_at_the_threshold() -> None:
    seed = make_seed(scan_tier="standard", consecutive_empty_scans=7)
    store = FakeSeedStore(seed)
    hive = RecordingProvider(HIVE, result=ok_result(HIVE, []))
    control = FakeControlStore({HIVE: runtime(HIVE)})

    await execute_run(
        make_claim((HIVE,), seed), {HIVE: hive}, store, {}, control, CADENCE
    )

    [(_, update)] = store.cadence_updates
    assert update.consecutive_empty_scans == 8
    assert update.scan_tier == "relaxed"


async def test_a_run_where_nothing_succeeded_does_not_change_the_tier() -> None:
    """A run where every provider was skipped or timed out produced no evidence.
    Counting it as an empty scan would relax a user's cadence *because* our own
    provider integration was down."""
    seed = make_seed(scan_tier="standard", consecutive_empty_scans=7)
    store = FakeSeedStore(seed)
    hive = RecordingProvider(HIVE, result=timeout_result(HIVE))
    control = FakeControlStore({HIVE: runtime(HIVE)})

    await execute_run(
        make_claim((HIVE,), seed), {HIVE: hive}, store, {}, control, CADENCE
    )

    assert store.cadence_updates == []


async def test_an_expired_seed_url_completes_the_run_and_leaves_cadence_alone() -> None:
    """Done-when: a run whose seed_url 403s completes with an empty
    providers_succeeded and NO cadence change.

    This is the failure mode 0011 makes recoverable rather than permanent. If
    the URL expires between enqueue and dispatch the providers fail normally;
    the proxy re-enqueues with a freshly minted one. There is deliberately no
    refresh path on this side -- adding one would need the S3 credentials this
    service does not hold.

    The cadence half is the part that matters for the user. A run where nothing
    succeeded is not evidence of an empty scan, so `should_retier` filters it
    out: otherwise our own expired credential would quietly demote someone from
    weekly to fortnightly monitoring, taking the cost saving from exactly the
    wrong place.
    """
    hive = RecordingProvider(HIVE, result=fetch_failed_result(HIVE))
    google = RecordingProvider(GOOGLE, result=fetch_failed_result(GOOGLE))
    store, control = FakeSeedStore(), FakeControlStore(BOTH)
    claim = make_claim(
        (HIVE, GOOGLE),
        store.seed,
        seed_url="https://proxy-s3.example/seed.jpg?X-Amz-Signature=expired",
    )

    outcome = await execute_run(
        claim, {HIVE: hive, GOOGLE: google}, store, {}, control, CADENCE
    )

    # Both were genuinely attempted — this is a fetch failure, not a skip.
    assert hive.calls == [claim.seed_url]
    assert google.calls == [claim.seed_url]
    # The run COMPLETES (it ran), with nothing succeeded and nothing found.
    assert store.completed == [(claim.run_id, ())]
    assert outcome.providers_succeeded == ()
    assert outcome.matches_recorded == 0
    assert store.refusals == []  # 'refused' is for ineligibility, not failure
    # And the seed's cadence is untouched.
    assert store.cadence_updates == []
    # Both failures are recorded rows, never silence (CLAUDE.md §7.5).
    assert {r.status for _, r, _ in control.outcomes} == {"error"}
