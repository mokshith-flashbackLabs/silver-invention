"""The quiz-change reconcile (spec §4.9, §10): the pure plan, then its transaction."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from psycopg_pool import AsyncConnectionPool

from imageshield.intel.proposal_store import PostgresProposalStore
from imageshield.intel.reconcile import PendingWeightChange, PostgresReconciler, plan_reconcile
from imageshield.intel.worker import tick
from tests.intel_fakes import (
    QUIZ_VOCABULARY,
    FakeFetcher,
    FakeModel,
    make_deps,
    quiz_document,
    renamed_document,
    scoring,
    seed_proposal,
    seed_quiz_vocabulary,
    seed_signal,
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
    detail = await PostgresProposalStore(intel_pool).get_proposal(pid)
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
