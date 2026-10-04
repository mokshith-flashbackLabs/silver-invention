"""The quiz-change reconcile (spec §4.9, §10): the pure plan, then its transaction."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from psycopg_pool import AsyncConnectionPool

from imageshield.intel.bounds import GAP_REGENERATE_MAX_RUNS, PROPOSAL_CONTEXT_MAX_SIGNALS
from imageshield.intel.proposal_models import ContextSignal, GapPass
from imageshield.intel.proposal_store import PostgresProposalStore
from imageshield.intel.reconcile import (
    FinishedRegeneration,
    PendingGap,
    PendingWeightChange,
    PostgresReconciler,
    plan_gap_resolution,
    plan_gap_retries,
    plan_reconcile,
)
from imageshield.intel.schemas import ProposalOutput, ProposedThreatEvent
from imageshield.intel.worker import tick
from tests.intel_fakes import (
    NOW,
    QUIZ_VOCABULARY,
    THREAT_SUGGESTED,
    FakeFetcher,
    FakeModel,
    make_deps,
    mapped_document,
    quiz_document,
    renamed_document,
    scoring,
    seed_proposal,
    seed_quiz_vocabulary,
    seed_signal,
    settle_runs,
)

T0 = datetime(2026, 9, 30, tzinfo=UTC)


def _p(
    option: str = "Instagram", current: int = 3, against: int = 2, minutes: int = 0
) -> PendingWeightChange:
    return PendingWeightChange(
        uuid4(), "platforms", option, current, against, T0 + timedelta(minutes=minutes)
    )


def _renamed(*entries: tuple[int, str, str]) -> list[dict[str, Any]]:
    return [
        {"release_no": r, "question_key": "platforms", "old_option": o, "new_option": n}
        for r, o, n in entries
    ]


def test_a_rename_retargets_when_the_deduction_still_matches() -> None:
    p = _p()
    plan = plan_reconcile(
        [p],
        scoring(renamed_document("Instagram", "Instagram (Meta)", release_no=3), release_no=3),
        reconciled_release_no=2,
    )
    assert plan.retarget == ((p.proposal_id, "Instagram", "Instagram (Meta)"),)
    assert plan.supersede == ()


def test_a_rename_whose_deduction_moved_supersedes() -> None:
    doc = renamed_document("Instagram", "Instagram (Meta)", release_no=3)
    doc["questions"][0]["deductions"]["Instagram (Meta)"] = 4
    p = _p()
    plan = plan_reconcile([p], scoring(doc, release_no=3), reconciled_release_no=2)
    assert plan.retarget == () and plan.supersede == ((p.proposal_id, "cell_changed"),)


def test_a_chain_across_two_releases_lands_on_the_final_text() -> None:
    doc = copy.deepcopy(QUIZ_VOCABULARY)
    platforms = doc["questions"][0]
    platforms["options"] = ["Insta", *platforms["options"][1:]]
    platforms["deductions"] = {
        "Insta": 3,
        **{k: v for k, v in platforms["deductions"].items() if k != "Instagram"},
    }
    doc["renamed"] = _renamed((5, "Instagram", "IG"), (6, "IG", "Insta"))
    p = _p(against=4)
    plan = plan_reconcile([p], scoring(doc, release_no=6), reconciled_release_no=None)
    assert plan.retarget == ((p.proposal_id, "Instagram", "Insta"),)


def test_a_swap_in_one_release_moves_a_to_b() -> None:
    """Review Focus 4."""
    doc = copy.deepcopy(QUIZ_VOCABULARY)
    doc["questions"][0]["deductions"].update({"Instagram": 2, "LinkedIn": 3})
    doc["renamed"] = _renamed((7, "Instagram", "LinkedIn"), (7, "LinkedIn", "Instagram"))
    on_instagram = _p(option="Instagram", current=3, against=6)
    plan = plan_reconcile([on_instagram], scoring(doc, release_no=7), reconciled_release_no=6)
    assert plan.retarget == ((on_instagram.proposal_id, "Instagram", "LinkedIn"),)


def test_removed_option_removed_question_and_not_mutable_supersede() -> None:
    doc = copy.deepcopy(QUIZ_VOCABULARY)
    doc["questions"][0]["options"].remove("Threads")
    del doc["questions"][0]["deductions"]["Threads"]
    doc["questions"][1]["type"] = "escrowed"
    removed_option = _p(option="Threads", current=1)
    not_mutable = PendingWeightChange(uuid4(), "dating", "Yes", 9, 2, T0)
    removed_question = PendingWeightChange(uuid4(), "gone", "x", 1, 2, T0)
    plan = plan_reconcile(
        [removed_option, not_mutable, removed_question],
        scoring(doc, release_no=3),
        reconciled_release_no=2,
    )
    assert {pid for pid, reason in plan.supersede if reason == "cell_changed"} == {
        removed_option.proposal_id,
        not_mutable.proposal_id,
        removed_question.proposal_id,
    }


def test_renames_already_reconciled_or_older_than_the_proposal_are_not_reapplied() -> None:
    doc = quiz_document(renamed=_renamed((6, "Instagram", "Gone")))
    reconciled = _p()
    later = _p(against=7)
    plan = plan_reconcile([reconciled], scoring(doc, release_no=7), reconciled_release_no=6)
    assert plan.empty
    plan = plan_reconcile([later], scoring(doc, release_no=7), reconciled_release_no=None)
    assert plan.empty


def test_two_pendings_landing_on_one_cell_keep_the_newest() -> None:
    doc = renamed_document("Instagram", "Instagram (Meta)", release_no=3)
    older = _p(option="Instagram", minutes=0)
    newer = _p(option="Instagram (Meta)", against=3, minutes=5)
    plan = plan_reconcile([older, newer], scoring(doc, release_no=3), reconciled_release_no=2)
    assert plan.supersede == ((older.proposal_id, "newer_proposal"),)
    assert plan.retarget == ()


MAPPED = scoring(mapped_document("LinkedIn", "linkedin"), map_version=2)


def _gap(
    subject: str = "LinkedIn", slug: str | None = None, *signals: UUID, minutes: int = 0
) -> PendingGap:
    return PendingGap(uuid4(), subject, slug, tuple(signals), T0 + timedelta(minutes=minutes))


def _subject_signal(*subjects: str, status: str = "active") -> ContextSignal:
    return ContextSignal(
        signal_id=uuid4(),
        category="incident",
        direction="risk_up",
        tags=(),
        unregistered_subjects=subjects,
        summary="s",
        trust="listed",
        publisher_domain="p.example",
        status=status,
        created_at=T0,
    )


def test_a_gap_resolves_to_its_suggested_tag_once_that_tag_is_mapped() -> None:
    gap = _gap("Professional networks", "linkedin")
    (resolution,) = plan_gap_resolution([gap], [], MAPPED)
    assert (resolution.proposal_id, resolution.tag) == (gap.proposal_id, "linkedin")


def test_a_gap_resolves_when_its_subject_is_a_mapped_tags_slug_or_label() -> None:
    assert [r.tag for r in plan_gap_resolution([_gap("LinkedIn")], [], MAPPED)] == ["linkedin"]
    assert plan_gap_resolution([_gap("Linked In")], [], MAPPED) == ()
    assert plan_gap_resolution([_gap("LinkedIn")], [], scoring()) == ()  # registered, unmapped


def test_the_regeneration_input_is_the_gaps_evidence_then_matching_subjects_bounded() -> None:
    own = uuid4()
    match = _subject_signal("LinkedIn")
    gone = _subject_signal("linkedin", status="retracted")
    other = _subject_signal("Bumble")
    (resolution,) = plan_gap_resolution([_gap("LinkedIn", None, own)], [match, gone, other], MAPPED)
    assert resolution.signal_ids == (own, match.signal_id)
    many = [_subject_signal("LinkedIn") for _ in range(PROPOSAL_CONTEXT_MAX_SIGNALS + 5)]
    (bounded,) = plan_gap_resolution([_gap("LinkedIn", None, own)], many, MAPPED)
    assert len(bounded.signal_ids) == PROPOSAL_CONTEXT_MAX_SIGNALS
    assert bounded.signal_ids[0] == own


def test_a_failed_regeneration_is_retried_after_six_hours_up_to_five_runs() -> None:
    def finished(hours_ago: float, runs: int) -> FinishedRegeneration:
        return FinishedRegeneration(
            uuid4(), uuid4(), {"tag": "linkedin"}, T0 - timedelta(hours=hours_ago), runs
        )

    due, early, spent = finished(7, 1), finished(5, 1), finished(7, GAP_REGENERATE_MAX_RUNS)
    assert plan_gap_retries([due, early, spent], now=T0) == (due,)


async def _scalar(pool: AsyncConnectionPool, query: str, *params: Any) -> Any:
    async with pool.connection() as conn:
        cur = await conn.execute(query, params)
        row = await cur.fetchone()
    assert row is not None
    return row[0]


async def test_the_reconcile_applies_once_per_pair_and_records_it(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    sid = await seed_signal(intel_pool, tags=("instagram",))
    pid = await seed_proposal(intel_pool, signal_ids=[sid])
    reconciler = PostgresReconciler(intel_pool)
    first = await reconciler.reconcile()
    assert first is not None and (first.retargeted, first.superseded) == (0, 0)
    await seed_quiz_vocabulary(
        intel_pool,
        release_no=3,
        document=renamed_document("Instagram", "Instagram (Meta)", release_no=3),
    )
    result = await reconciler.reconcile()
    assert result is not None and result.retargeted == 1
    assert (
        await _scalar(
            intel_pool, "SELECT target->>'option' FROM intel_proposals WHERE proposal_id = %s", pid
        )
        == "Instagram (Meta)"
    )
    assert await _scalar(
        intel_pool,
        "SELECT ARRAY[reconciled_release_no, reconciled_map_version] FROM intel_vocabulary",
    ) == [3, 1]
    assert (
        await _scalar(
            intel_pool,
            "SELECT count(*) FROM audit_log WHERE action = 'intel.vocabulary_reconciled'",
        )
        == 1
    )
    assert await reconciler.reconcile() is None  # same pair: nothing changes


async def test_an_approved_change_is_never_rewritten_and_reads_stale(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    sid = await seed_signal(intel_pool, tags=("instagram",))
    pid = await seed_proposal(intel_pool, signal_ids=[sid], status="approved", decided={"delta": 1})
    await seed_quiz_vocabulary(
        intel_pool,
        release_no=3,
        document=renamed_document("Instagram", "Instagram (Meta)", release_no=3),
    )
    await PostgresReconciler(intel_pool).reconcile()
    assert (
        await _scalar(
            intel_pool, "SELECT target->>'option' FROM intel_proposals WHERE proposal_id = %s", pid
        )
        == "Instagram"
    )
    detail = await PostgresProposalStore(intel_pool, threat_recency_days=90).get_proposal(pid)
    assert detail is not None
    assert detail["stale"] is True and detail["why_stale"] == "option_renamed"


async def test_a_removed_option_supersedes_its_pending_change(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    sid = await seed_signal(intel_pool)
    pid = await seed_proposal(intel_pool, signal_ids=[sid])
    doc = copy.deepcopy(QUIZ_VOCABULARY)
    doc["questions"][0]["options"].remove("Instagram")
    del doc["questions"][0]["deductions"]["Instagram"]
    await seed_quiz_vocabulary(intel_pool, release_no=3, document=doc)
    await PostgresReconciler(intel_pool).reconcile()
    assert await _scalar(
        intel_pool,
        "SELECT ARRAY[status, supersede_reason] FROM intel_proposals WHERE proposal_id = %s",
        pid,
    ) == ["superseded", "cell_changed"]


async def test_tick_reconciles_before_it_claims(intel_pool: AsyncConnectionPool) -> None:
    await seed_quiz_vocabulary(intel_pool, release_no=3)
    deps = make_deps(intel_pool, FakeFetcher({}), FakeModel())
    assert await tick(deps, lease_seconds=900) is False
    assert await _scalar(
        intel_pool,
        "SELECT ARRAY[reconciled_release_no, reconciled_map_version] FROM intel_vocabulary",
    ) == [3, 1]


async def _gap_state(pool: AsyncConnectionPool, gap: UUID) -> list[Any]:
    return await _scalar(  # type: ignore[no-any-return]
        pool,
        "SELECT ARRAY[status, supersede_reason, target->>'regenerated_by_run_id']"
        " FROM intel_proposals WHERE proposal_id = %s",
        gap,
    )


async def test_mapping_a_tag_resolves_its_gap_and_queues_one_regeneration_with_it(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)  # linkedin registered, not mapped
    signals = [
        await seed_signal(intel_pool, subjects=("LinkedIn",), publisher=p)
        for p in ("a.example", "b.example", "c.example")
    ]
    gap = await seed_proposal(
        intel_pool, signal_ids=signals, kind="coverage_gap", target={"subject": "LinkedIn"}
    )
    reconciler = PostgresReconciler(intel_pool)
    assert await reconciler.resolve_gaps(NOW) == GapPass(0, 0)  # not mapped yet
    await seed_quiz_vocabulary(
        intel_pool, map_version=2, document=mapped_document("LinkedIn", "linkedin")
    )
    assert await reconciler.resolve_gaps(NOW) == GapPass(1, 0)
    status, reason, run_ref = await _gap_state(intel_pool, gap)
    assert (status, reason) == ("superseded", "resolved_by_quiz")
    request = await _scalar(
        intel_pool,
        "SELECT request FROM intel_runs WHERE kind = 'gap_regenerate' AND status = 'queued'"
        " AND run_id::text = %s",
        run_ref,
    )
    assert request["coverage_gap_id"] == str(gap) and request["tag"] == "linkedin"
    assert set(request["signal_ids"]) == {str(s) for s in signals}
    assert await reconciler.resolve_gaps(NOW) == GapPass(0, 0)  # idempotent
    assert (
        await _scalar(intel_pool, "SELECT count(*) FROM intel_runs WHERE kind = 'gap_regenerate'")
        == 1
    )
    assert (
        await _scalar(
            intel_pool,
            "SELECT count(*) FROM audit_log WHERE action = 'intel.coverage_gaps_resolved'",
        )
        == 1
    )


async def test_a_gap_whose_tag_was_mapped_before_the_pass_ran_is_still_resolved(
    intel_pool: AsyncConnectionPool,
) -> None:
    """The step-2 spec note: pairs reconciled before step 3 shipped are already recorded, so the
    resolution reads the state NOW, never the change since the last reconcile."""
    await seed_quiz_vocabulary(
        intel_pool, map_version=2, document=mapped_document("LinkedIn", "linkedin")
    )
    reconciler = PostgresReconciler(intel_pool)
    await reconciler.reconcile()  # the pair is recorded before the gap exists
    sid = await seed_signal(intel_pool, subjects=("LinkedIn",))
    gap = await seed_proposal(
        intel_pool, signal_ids=[sid], kind="coverage_gap", target={"subject": "LinkedIn"}
    )
    assert await reconciler.reconcile() is None
    assert (await reconciler.resolve_gaps(NOW)).resolved == 1
    assert (await _gap_state(intel_pool, gap))[0] == "superseded"


async def test_a_refused_regeneration_is_queued_again_after_six_hours_up_to_five_runs(
    intel_pool: AsyncConnectionPool,
) -> None:
    """Review Focus 1: the evidence behind a closed gap is never lost to a gate refusal."""
    await seed_quiz_vocabulary(
        intel_pool, map_version=2, document=mapped_document("LinkedIn", "linkedin")
    )
    sid = await seed_signal(intel_pool, subjects=("LinkedIn",))
    gap = await seed_proposal(
        intel_pool, signal_ids=[sid], kind="coverage_gap", target={"subject": "LinkedIn"}
    )
    reconciler = PostgresReconciler(intel_pool)
    await reconciler.resolve_gaps(NOW)

    async def refuse_latest(hours_ago: int) -> str:
        run_ref: str = (await _gap_state(intel_pool, gap))[2]
        async with intel_pool.connection() as conn:
            await conn.execute(
                "UPDATE intel_runs SET status = 'refused', completed_at = %s"
                " WHERE run_id::text = %s",
                (NOW - timedelta(hours=hours_ago), run_ref),
            )
        return run_ref

    first = await refuse_latest(5)
    assert (await reconciler.resolve_gaps(NOW)).retried == 0  # not six hours yet
    await refuse_latest(7)
    assert (await reconciler.resolve_gaps(NOW)).retried == 1
    assert (await _gap_state(intel_pool, gap))[2] != first  # the gap follows the newest run
    for _ in range(GAP_REGENERATE_MAX_RUNS - 2):
        await refuse_latest(7)
        assert (await reconciler.resolve_gaps(NOW)).retried == 1
    await refuse_latest(7)
    assert (await reconciler.resolve_gaps(NOW)).retried == 0  # five runs: it stops
    assert (
        await _scalar(intel_pool, "SELECT count(*) FROM intel_runs WHERE kind = 'gap_regenerate'")
        == GAP_REGENERATE_MAX_RUNS
    )


async def test_tick_resolves_the_gap_then_runs_its_regeneration(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(
        intel_pool, map_version=2, document=mapped_document("LinkedIn", "linkedin")
    )
    signals = [
        await seed_signal(intel_pool, subjects=("LinkedIn",), publisher=p)
        for p in ("a.example", "b.example")
    ]
    gap = await seed_proposal(
        intel_pool, signal_ids=signals, kind="coverage_gap", target={"subject": "LinkedIn"}
    )
    await settle_runs(intel_pool)

    def propose(payload: dict[str, Any]) -> ProposalOutput:
        ids = [s["signal_id"] for s in payload["new_evidence"]]
        event = ProposedThreatEvent(
            **THREAT_SUGGESTED, tags=["linkedin"], rationale="r", signal_ids=ids
        )
        return ProposalOutput(threat_events=[event])

    deps = make_deps(intel_pool, FakeFetcher({}), FakeModel(propose_with=propose))
    assert await tick(deps, lease_seconds=900) is True
    (threat,) = await PostgresProposalStore(intel_pool, threat_recency_days=90).list_proposals(
        statuses=["pending"], kinds=["threat_event"], cursor=None, limit=5
    )
    assert set(threat["signal_ids"]) == set(signals) and threat["approvable"] is True
    assert (await _gap_state(intel_pool, gap))[:2] == ["superseded", "resolved_by_quiz"]
