"""AI-proposed quiz questions (spec 2026-10-13-intel-quiz-questions-design.md).

Pure generation rules first, then the write, research, decision, acknowledgement and reconcile
against real Postgres, then the pipeline's ``questions_open`` and the configuration."""

from __future__ import annotations

import contextlib
import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from imageshield.config import ConfigError
from imageshield.intel.config import load_intel_config
from imageshield.intel.decisions import PostgresDecisionStore
from imageshield.intel.generation import GeneratedBatch, validate_proposals
from imageshield.intel.limits import QuestionLimits
from imageshield.intel.prompts import PROPOSE_PROMPT_VERSION, proposal_request
from imageshield.intel.proposal_models import (
    ContextSignal,
    Decided,
    DecisionRefused,
    NewProposal,
    OpenQuestion,
    QuestionContext,
    WriteResult,
)
from imageshield.intel.proposal_store import PostgresProposalStore
from imageshield.intel.question_store import PostgresQuestionStore
from imageshield.intel.recency import Recency
from imageshield.intel.reconcile import PostgresReconciler
from imageshield.intel.schemas import (
    ProposalOutput,
    ProposedQuestionOption,
    ProposedQuizQuestion,
    ProposedTag,
)
from imageshield.intel.store import PostgresIntelStore
from imageshield.intel.user_copy import question_copy_problems
from tests.intel_fakes import (
    NOW,
    POLICY,
    TEST_QUESTION_LIMITS,
    FakeFetcher,
    FakeModel,
    make_deps,
    make_page,
    make_signal,
    quiz_document,
    run_once,
    scoring,
    seed_proposal,
    seed_quiz_vocabulary,
    seed_signal,
)
from tests.test_intel_config import _ALL_ENV_NAMES, _env

V = scoring()
REPO = Path(__file__).resolve().parents[1]
LIMITS = QuestionLimits(
    min_signals=3, min_publishers=2, window_days=90, max_options=4, max_pending=3, dismissed_days=90
)
OPEN = QuestionContext(open=(), dismissed=())


def _sig(
    *, publisher: str = "a.example", days_old: int = 1, status: str = "active"
) -> ContextSignal:
    return ContextSignal(
        signal_id=uuid4(),
        category="incident",
        direction="risk_up",
        tags=(),
        unregistered_subjects=(),
        summary="s",
        trust="web",
        publisher_domain=publisher,
        status=status,
        created_at=NOW - timedelta(days=days_old),
    )


def _three() -> list[ContextSignal]:
    return [_sig(publisher="a.example"), _sig(publisher="b.example"), _sig(publisher="a.example")]


def _question(signals: list[ContextSignal], **kw: Any) -> ProposedQuizQuestion:
    fields: dict[str, Any] = {
        "question_key": "public_profiles",
        "subject": "public profiles",
        "prompt": "Are the accounts where you post photos of yourself public?",
        "multi": False,
        "options": [
            ProposedQuestionOption(option="All of them", tags=["instagram"]),
            ProposedQuestionOption(option="Some of them"),
            ProposedQuestionOption(option="None of them"),
        ],
        "rationale": "Several reports tie public profiles to scraped photos.",
        "signal_ids": [str(s.signal_id) for s in signals],
    }
    fields.update(kw)
    return ProposedQuizQuestion.model_validate(fields)


def _validate(
    questions: list[ProposedQuizQuestion],
    signals: list[ContextSignal],
    *,
    context: QuestionContext | None = OPEN,
    limits: QuestionLimits = LIMITS,
    events_only: bool = False,
) -> tuple[GeneratedBatch, Counter[str]]:
    counts: Counter[str] = Counter()
    batch = validate_proposals(
        ProposalOutput(quiz_questions=questions),
        context={s.signal_id: s for s in signals},
        gap_pool=[],
        vocabulary=V,
        now=NOW,
        counts=counts,
        recency=Recency(NOW, 90),
        events_only=events_only,
        questions=context,
        question_limits=limits,
    )
    return batch, counts


# ── generation (§3) ───────────────────────────────────────────────────────────


def test_the_prompt_is_propose_v13_and_an_answer_without_questions_still_parses() -> None:
    assert PROPOSE_PROMPT_VERSION == "propose-v13"
    assert ProposalOutput.model_validate({}).quiz_questions == []


def test_a_well_formed_question_is_written_with_its_target_and_suggested() -> None:
    signals = _three()
    batch, counts = _validate([_question(signals)], signals)
    (question,) = batch.quiz_questions
    assert question.kind == "quiz_question"
    assert question.target == {"question_key": "public_profiles", "subject": "public profiles"}
    assert question.suggested["prompt"].startswith("Are the accounts")
    assert [o["option"] for o in question.suggested["options"]] == [
        "All of them",
        "Some of them",
        "None of them",
    ]
    assert question.suggested["options"][0]["tags"] == ["instagram"]
    assert question.signal_ids == tuple(s.signal_id for s in signals)
    assert batch.proposals == [question]
    assert not counts


def test_an_answer_with_digits_passes_the_question_word_rule() -> None:
    signals = _three()
    options = [ProposedQuestionOption(option="18-24"), ProposedQuestionOption(option="25 or over")]
    batch, _ = _validate([_question(signals, options=options)], signals)
    assert len(batch.quiz_questions) == 1
    assert question_copy_problems("Nine in ten") == []
    assert question_copy_problems("Is it safe?") == ["reassurance"]


@pytest.mark.parametrize(
    ("change", "count"),
    [
        ({"question_key": "Public-Profiles"}, "proposal_dropped_question_key_malformed"),
        ({"question_key": "platforms"}, "proposal_dropped_question_key_taken"),
        ({"prompt": "Is your photo public?"}, "proposal_dropped_question_forbidden_words"),
        ({"prompt": "x" * 301}, "proposal_dropped_question_too_long"),
        ({"subject": "   "}, "proposal_dropped_empty_subject"),
        (
            {"options": [ProposedQuestionOption(option="Only one")]},
            "proposal_dropped_question_option_count",
        ),
        (
            {"options": [ProposedQuestionOption(option=f"Answer {i}") for i in range(5)]},
            "proposal_dropped_question_option_count",
        ),
        (
            {
                "options": [
                    ProposedQuestionOption(option="Yes"),
                    ProposedQuestionOption(option="yes"),
                ]
            },
            "proposal_dropped_question_options_repeat",
        ),
        (
            {
                "options": [
                    ProposedQuestionOption(option="Yes, it was found"),
                    ProposedQuestionOption(option="No"),
                ]
            },
            "proposal_dropped_option_forbidden_words",
        ),
        (
            {
                "options": [
                    ProposedQuestionOption(option="Yes", tags=["nope"]),
                    ProposedQuestionOption(option="No"),
                ]
            },
            "proposal_dropped_unknown_tag",
        ),
        (
            {
                "options": [
                    ProposedQuestionOption(option="Yes", tags=["myspace"]),
                    ProposedQuestionOption(option="No"),
                ]
            },
            "proposal_dropped_tag_retired",
        ),
        (
            {
                "options": [
                    ProposedQuestionOption(
                        option="Yes",
                        new_tag=ProposedTag(slug="instagram", label="I", kind="platform"),
                    ),
                    ProposedQuestionOption(option="No"),
                ]
            },
            "proposal_dropped_new_tag_invalid",
        ),
    ],
)
def test_each_rule_drops_the_whole_question(change: dict[str, Any], count: str) -> None:
    signals = _three()
    batch, counts = _validate([_question(signals, **change)], signals)
    assert batch.quiz_questions == []
    assert counts[count] == 1


def test_a_new_tag_that_is_unregistered_is_kept_on_its_answer() -> None:
    signals = _three()
    options = [
        ProposedQuestionOption(
            option="Yes",
            new_tag=ProposedTag(slug="reverse_search", label="Reverse search", kind="practice"),
        ),
        ProposedQuestionOption(option="No"),
    ]
    batch, _ = _validate([_question(signals, options=options)], signals)
    assert batch.quiz_questions[0].suggested["options"][0]["new_tag"] == {
        "slug": "reverse_search",
        "label": "Reverse search",
        "kind": "practice",
    }


@pytest.mark.parametrize(
    "signals",
    [
        [_sig(publisher="a.example"), _sig(publisher="b.example")],  # too few
        [_sig(publisher="a.example") for _ in range(3)],  # one publisher
        [
            _sig(publisher="a.example"),
            _sig(publisher="b.example"),
            _sig(publisher="c.example", days_old=91),
        ],  # one outside the window
    ],
)
def test_the_evidence_bar_is_recounted_over_the_cited_signals(signals: list[ContextSignal]) -> None:
    batch, counts = _validate([_question(signals)], signals)
    assert batch.quiz_questions == []
    assert counts["proposal_dropped_question_below_threshold"] == 1


def test_a_retracted_cited_signal_drops_the_question() -> None:
    signals = [*_three(), _sig(status="retracted")]
    batch, counts = _validate([_question(signals)], signals)
    assert batch.quiz_questions == [] and counts["proposal_dropped_unknown_signal"] == 1


def _open(status: str, key: str = "other_key", subject: str = "other subject") -> OpenQuestion:
    return OpenQuestion(uuid4(), status, key, subject, "A question?")


@pytest.mark.parametrize("status", ["pending", "approved", "rejected"])
def test_an_open_or_dismissed_key_or_subject_is_never_proposed_again(status: str) -> None:
    signals = _three()
    for repeat in (_open(status, key="public_profiles"), _open(status, subject="Public Profiles")):
        context = (
            QuestionContext(open=(), dismissed=(repeat,))
            if status == "rejected"
            else QuestionContext(open=(repeat,), dismissed=())
        )
        batch, counts = _validate([_question(signals)], signals, context=context)
        assert batch.quiz_questions == []
        assert (
            counts["proposal_dropped_question_key_taken"]
            + counts["proposal_dropped_question_subject_taken"]
            == 1
        )


def test_two_questions_in_one_batch_cannot_share_a_key_or_a_subject() -> None:
    signals = _three()
    first = _question(signals)
    same_key = _question(signals, subject="another subject")
    same_subject = _question(signals, question_key="another_key")
    batch, counts = _validate([first, same_key, same_subject], signals)
    assert len(batch.quiz_questions) == 1
    assert counts["proposal_dropped_question_key_taken"] == 1
    assert counts["proposal_dropped_question_subject_taken"] == 1


def test_the_open_question_cap_bounds_a_batch() -> None:
    signals = _three()
    context = QuestionContext(
        open=(_open("pending"), _open("approved", key="k2", subject="s2")), dismissed=()
    )
    batch, counts = _validate(
        [_question(signals), _question(signals, question_key="second", subject="second")],
        signals,
        context=context,
    )
    assert len(batch.quiz_questions) == 1
    assert counts["proposal_dropped_question_cap"] == 1


@pytest.mark.parametrize("closed", ["no_context", "events_only"])
def test_a_closed_pass_drops_every_question(closed: str) -> None:
    signals = _three()
    batch, counts = _validate(
        [_question(signals)],
        signals,
        context=None if closed == "no_context" else OPEN,
        events_only=closed == "events_only",
    )
    assert batch.quiz_questions == []
    assert counts["proposal_dropped_questions_closed"] == 1


def test_the_request_carries_questions_open_and_the_open_questions() -> None:
    pending = {
        "proposal_id": str(uuid4()),
        "status": "pending",
        "question_key": "k",
        "subject": "s",
        "prompt": "p",
    }
    system, user = proposal_request(
        [],
        [],
        quiz=[],
        registry_tags=[],
        mapped_tags=[],
        today="2026-10-13",
        threat_recency_days=90,
        questions_open=True,
        max_question_options=8,
        pending_questions=[pending],  # type: ignore[list-item]
    )
    payload = json.loads(user)
    assert payload["questions_open"] is True and payload["max_question_options"] == 8
    assert payload["pending_questions"] == [pending] and payload["dismissed_questions"] == []
    assert "quiz_questions" in system
    _, user = proposal_request(
        [],
        [],
        quiz=[],
        registry_tags=[],
        mapped_tags=[],
        today="2026-10-13",
        threat_recency_days=90,
        questions_open=True,
        events_only=True,
    )
    assert json.loads(user)["questions_open"] is False


# ── the write and its research (§3.3, §4) ─────────────────────────────────────


def _new_proposal(
    signal_ids: list[UUID], key: str = "public_profiles", subject: str = "public profiles"
) -> NewProposal:
    return NewProposal(
        kind="quiz_question",
        target={"question_key": key, "subject": subject},
        suggested={
            "prompt": "Are the accounts where you post photos of yourself public?",
            "multi": False,
            "options": [
                {"option": "All of them", "tags": ["instagram"], "new_tag": None},
                {"option": "None of them", "tags": [], "new_tag": None},
            ],
        },
        rationale="Reports tie public profiles to scraped photos.",
        signal_ids=tuple(signal_ids),
    )


async def _write(
    pool: AsyncConnectionPool,
    proposals: list[NewProposal],
    limits: QuestionLimits = TEST_QUESTION_LIMITS,
) -> WriteResult:
    run_id = await PostgresIntelStore(pool).queue_adhoc("https://p.example/x", operator="a")
    result = await PostgresProposalStore(pool, threat_recency_days=90).write_generated(
        run_id,
        proposals,
        against_scoring_version="s2",
        against_release_no=2,
        model_id="claude-opus-5-5",
        prompt_version="propose-v12",
        question_limits=limits,
    )
    assert result is not None
    return result


async def _one(pool: AsyncConnectionPool, query: str, *params: Any) -> Any:
    async with pool.connection() as conn:
        cur = await conn.execute(query, params)
        row = await cur.fetchone()
    assert row is not None
    return row[0]


async def test_a_written_question_queues_exactly_one_research_run(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    sid = await seed_signal(intel_pool, tags=("instagram",))
    result = await _write(intel_pool, [_new_proposal([sid])])
    (pid,) = result.written
    (run_id,) = result.research_runs
    target = await _one(
        intel_pool, "SELECT target FROM intel_proposals WHERE proposal_id = %s", pid
    )
    assert target["research_run_id"] == str(run_id)
    kind, requested_by, request = await _one(
        intel_pool,
        "SELECT ARRAY[kind, requested_by, request::text] FROM intel_runs WHERE run_id = %s",
        run_id,
    )
    request = json.loads(request)
    assert (kind, requested_by) == ("weight_suggestion", "system:intel")
    assert request["for_question"] == str(pid) and request["signal_ids"] == [str(sid)]
    assert request["options"] == ["All of them", "None of them"] and request["type"] == "mutable"
    assert request["tags"] == {"All of them": ["instagram"], "None of them": []}
    assert (
        await _one(
            intel_pool,
            "SELECT count(*) FROM audit_log WHERE action = 'intel.question_research_queued'",
        )
        == 1
    )


async def test_the_write_rechecks_key_subject_and_cap_under_its_lock(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    sid = await seed_signal(intel_pool, tags=("instagram",))
    await _write(intel_pool, [_new_proposal([sid])])
    again = await _write(
        intel_pool,
        [
            _new_proposal([sid]),  # same key and subject
            _new_proposal([sid], key="second", subject="public profiles"),  # same subject
            _new_proposal([sid], key="third", subject="third"),
        ],
        limits=QuestionLimits(1, 1, 90, 8, 2, 90),
    )
    assert again.dropped_questions == 2 and len(again.written) == 1
    capped = await _write(
        intel_pool,
        [_new_proposal([sid], key="fourth", subject="fourth")],
        limits=QuestionLimits(1, 1, 90, 8, 2, 90),
    )
    assert capped.dropped_questions == 1 and capped.written == ()


async def test_a_question_without_limits_is_never_written_unchecked(
    intel_pool: AsyncConnectionPool,
) -> None:
    sid = await seed_signal(intel_pool, tags=("instagram",))
    run_id = await PostgresIntelStore(intel_pool).queue_adhoc("https://p.example/y", operator="a")
    result = await PostgresProposalStore(intel_pool, threat_recency_days=90).write_generated(
        run_id,
        [_new_proposal([sid])],
        against_scoring_version="s2",
        against_release_no=2,
        model_id="m",
        prompt_version="propose-v12",
    )
    assert result is not None and result.written == () and result.dropped_questions == 1


async def test_the_research_runs_evidence_carries_the_questions_signals(
    intel_pool: AsyncConnectionPool,
) -> None:
    old = await seed_signal(intel_pool, created_at=datetime.now(UTC) - timedelta(days=400))
    found = await PostgresQuestionStore(intel_pool).suggestion_candidates(
        source_ids=[],
        tags=[],
        since=datetime.now(UTC) - timedelta(days=30),
        limit=10,
        signal_ids=[old],
    )
    assert [s.signal_id for s in found.from_research] == [old]


# ── research state on the reads, and the decision (§4, §5) ────────────────────


async def _question_with_research(
    pool: AsyncConnectionPool,
    *,
    run_status: str = "completed",
    numbers: Mapping[str, int | None] | None = None,
    key: str = "public_profiles",
) -> UUID:
    await seed_quiz_vocabulary(pool)
    sid = await seed_signal(pool, tags=("instagram",))
    result = await _write(pool, [_new_proposal([sid], key=key, subject=key)])
    (pid,) = result.written
    (run_id,) = result.research_runs
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE intel_runs SET status = %s WHERE run_id = %s", (run_status, run_id)
        )
    if numbers is not None:
        await seed_proposal(
            pool,
            signal_ids=[sid],
            kind="weight_suggestion",
            status="delivered",
            target={
                "question_key": key,
                "options": [
                    {"option": o, "deduction": d, "signal_ids": [str(sid)]}
                    for o, d in numbers.items()
                ],
            },
        )
    return pid


async def _decide(
    pool: AsyncConnectionPool,
    pid: UUID,
    decision: Literal["approved", "rejected"] = "approved",
    values: dict[str, Any] | None = None,
) -> Decided:
    return await PostgresDecisionStore(pool, threat_recency_days=90).decide(
        pid,
        decision=decision,
        values=values,
        reason="checked it",
        operator="ann",
    )


async def _refused(
    pool: AsyncConnectionPool,
    pid: UUID,
    values: dict[str, Any] | None = None,
    decision: Literal["approved", "rejected"] = "approved",
) -> str:
    with pytest.raises(DecisionRefused) as caught:
        await _decide(pool, pid, decision, values)
    return caught.value.code


async def test_research_pending_while_the_run_is_open(intel_pool: AsyncConnectionPool) -> None:
    pid = await _question_with_research(intel_pool, run_status="queued")
    row = await PostgresProposalStore(intel_pool, threat_recency_days=90).get_proposal(pid)
    assert row is not None
    assert (row["approvable"], row["why_not"]) == (False, "research_pending")
    assert (
        row["research"]["status"] == "queued" and row["research"]["suggestion_proposal_id"] is None
    )
    with pytest.raises(DecisionRefused) as caught:
        await _decide(intel_pool, pid)
    assert caught.value.code == "proposal_not_decidable"
    assert "still running" in caught.value.message


async def test_an_approval_with_no_values_takes_the_researched_numbers(
    intel_pool: AsyncConnectionPool,
) -> None:
    pid = await _question_with_research(intel_pool, numbers={"All of them": 5, "None of them": 0})
    listed = await PostgresProposalStore(intel_pool, threat_recency_days=90).list_proposals(
        statuses=None, kinds=["quiz_question"], cursor=None, limit=10
    )
    assert listed[0]["approvable"] is True and listed[0]["research"]["status"] == "completed"
    decided = await _decide(intel_pool, pid)
    assert decided.status == "approved" and decided.kind == "quiz_question"
    assert decided.decided is not None
    assert decided.decided["options"] == [
        {"option": "All of them", "deduction": 5, "tags": ["instagram"]},
        {"option": "None of them", "deduction": 0, "tags": []},
    ]
    assert decided.decided["suggestion_proposal_id"] == str(
        listed[0]["research"]["suggestion_proposal_id"]
    )
    assert decided.decided["multi"] is False and decided.decided["cap"] is None


async def test_an_unbacked_answer_needs_the_operators_number(
    intel_pool: AsyncConnectionPool,
) -> None:
    pid = await _question_with_research(
        intel_pool, numbers={"All of them": 5, "None of them": None}
    )
    assert await _refused(intel_pool, pid) == "question_incomplete"
    decided = await _decide(
        intel_pool,
        pid,
        values={"options": [{}, {"deduction": 1, "option": "No accounts are public"}]},
    )
    assert decided.decided is not None
    assert decided.decided["options"][1] == {
        "option": "No accounts are public",
        "deduction": 1,
        "tags": [],
    }


async def test_a_failed_research_leaves_every_number_to_the_operator(
    intel_pool: AsyncConnectionPool,
) -> None:
    pid = await _question_with_research(intel_pool, run_status="failed")
    assert await _refused(intel_pool, pid) == "question_incomplete"
    decided = await _decide(
        intel_pool, pid, values={"options": [{"deduction": 4}, {"deduction": 0}]}
    )
    assert decided.decided is not None and decided.decided["suggestion_proposal_id"] is None


@pytest.mark.parametrize(
    ("values", "code"),
    [
        ({"multi": True}, "question_incomplete"),
        ({"options": [{}]}, "values_out_of_bounds"),
        ({"prompt": "Is it safe to post?"}, "values_out_of_bounds"),
        ({"options": [{"deduction": 11}, {}]}, "values_out_of_bounds"),
        ({"cap": 5}, "values_out_of_bounds"),
        ({"options": [{"tags": ["nope"]}, {}]}, "unknown_tag"),
        ({"options": [{"tags": ["myspace"]}, {}]}, "tag_retired"),
    ],
)
async def test_an_approval_that_breaks_a_rule_is_refused(
    intel_pool: AsyncConnectionPool, values: dict[str, Any], code: str
) -> None:
    pid = await _question_with_research(intel_pool, numbers={"All of them": 5, "None of them": 0})
    assert await _refused(intel_pool, pid, values) == code
    assert (
        await _one(intel_pool, "SELECT status FROM intel_proposals WHERE proposal_id = %s", pid)
        == "pending"
    )


async def test_a_multi_select_with_a_cap_is_approved(intel_pool: AsyncConnectionPool) -> None:
    pid = await _question_with_research(intel_pool, numbers={"All of them": 5, "None of them": 0})
    decided = await _decide(intel_pool, pid, values={"multi": True, "cap": 6})
    assert decided.decided is not None
    assert (decided.decided["multi"], decided.decided["cap"]) == (True, 6)


def _live(
    document: dict[str, Any], key: str, deductions: dict[str, int] | None = None
) -> dict[str, Any]:
    return {
        **document,
        "questions": [
            *document["questions"],
            {
                "key": key,
                "prompt": "Q?",
                "type": "mutable",
                "options": ["All of them", "None of them"],
                "deductions": deductions or {"All of them": 5, "None of them": 0},
                "cap": None,
            },
        ],
    }


async def test_a_key_the_live_quiz_took_is_refused(intel_pool: AsyncConnectionPool) -> None:
    pid = await _question_with_research(
        intel_pool, numbers={"All of them": 5, "None of them": 0}, key="taken_later"
    )
    await seed_quiz_vocabulary(
        intel_pool, release_no=3, document=_live(quiz_document(), "taken_later")
    )
    assert await _refused(intel_pool, pid) == "question_key_taken"


async def test_a_withdrawal_works_until_this_question_is_live(
    intel_pool: AsyncConnectionPool,
) -> None:
    """Final review 2026-10-13: "live" is THIS question -- its answers and their points -- so a key
    the quiz editor took by hand leaves the approved question withdrawable, never stuck."""
    numbers = {"All of them": 5, "None of them": 0}
    pid = await _question_with_research(intel_pool, numbers=numbers)
    await _decide(intel_pool, pid)
    assert (await _decide(intel_pool, pid, "rejected")).status == "rejected"
    live = await _question_with_research(intel_pool, numbers=numbers, key="second_key")
    hand = await _question_with_research(intel_pool, numbers=numbers, key="hand_added")
    await _decide(intel_pool, live)
    await _decide(intel_pool, hand)
    document = _live(
        _live(quiz_document(), "second_key"), "hand_added", {"All of them": 9, "None of them": 0}
    )
    await seed_quiz_vocabulary(intel_pool, release_no=3, document=document)
    assert await _refused(intel_pool, live, decision="rejected") == "proposal_not_pending"
    assert (await _decide(intel_pool, hand, "rejected")).status == "rejected"


async def test_the_acknowledgement_moves_approved_questions_with_weight_changes(
    intel_pool: AsyncConnectionPool,
) -> None:
    pid = await _question_with_research(intel_pool, numbers={"All of them": 5, "None of them": 0})
    await _decide(intel_pool, pid)
    sid = await seed_signal(intel_pool, tags=("instagram",))
    weight = await seed_proposal(
        intel_pool, signal_ids=[sid], status="approved", decided={"delta": 1, "body": ""}
    )
    store = PostgresDecisionStore(intel_pool, threat_recency_days=90)
    first = await store.mark_applied(
        scoring_version="likeness-score-v9", proposal_ids=[pid, weight]
    )
    assert set(first.applied) == {pid, weight}
    first = await store.mark_applied(scoring_version="likeness-score-v9", proposal_ids=[pid])
    assert first.already_applied == (pid,)
    assert (
        await _one(
            intel_pool, "SELECT applied_ref FROM intel_proposals WHERE proposal_id = %s", pid
        )
        == "likeness-score-v9"
    )


async def test_the_reconcile_supersedes_a_pending_question_whose_key_went_live(
    intel_pool: AsyncConnectionPool,
) -> None:
    numbers = {"All of them": 5, "None of them": 0}
    pending = await _question_with_research(intel_pool, numbers=numbers, key="went_live")
    approved = await _question_with_research(intel_pool, numbers=numbers, key="also_live")
    await _decide(intel_pool, approved)
    await PostgresReconciler(intel_pool).reconcile()
    await seed_quiz_vocabulary(
        intel_pool, release_no=3, document=_live(_live(quiz_document(), "went_live"), "also_live")
    )
    await PostgresReconciler(intel_pool).reconcile()
    status = (
        "SELECT status || ':' || coalesce(supersede_reason, '') FROM intel_proposals"
        " WHERE proposal_id = %s"
    )
    assert await _one(intel_pool, status, pending) == "superseded:resolved_by_quiz"
    assert await _one(intel_pool, status, approved) == "approved:"


async def test_the_kind_check_admits_quiz_question_and_needs_decided_when_approved(
    intel_pool: AsyncConnectionPool,
) -> None:
    import psycopg

    sid = await seed_signal(intel_pool)
    with pytest.raises(psycopg.errors.CheckViolation):
        await seed_proposal(
            intel_pool,
            signal_ids=[sid],
            kind="quiz_question",
            status="approved",
            target={"question_key": "k1", "subject": "s"},
        )
    await seed_proposal(
        intel_pool,
        signal_ids=[sid],
        kind="quiz_question",
        target={"question_key": "k2", "subject": "s"},
    )


# ── the pipeline's questions_open (§3.2) ──────────────────────────────────────

URL = "https://p.example/terms"


def _ask(payload: dict[str, Any]) -> ProposalOutput:
    ids = [s["signal_id"] for s in payload["new_evidence"]]
    return ProposalOutput(
        quiz_questions=[
            ProposedQuizQuestion(
                question_key="public_profiles",
                subject="public profiles",
                prompt="Are the accounts where you post photos of yourself public?",
                options=[
                    ProposedQuestionOption(option="Yes", tags=["instagram"]),
                    ProposedQuestionOption(option="No"),
                ],
                rationale="reports",
                signal_ids=ids,
            )
        ]
    )


async def test_an_adhoc_run_proposes_a_question_and_queues_its_research(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    await PostgresIntelStore(intel_pool).queue_adhoc(URL, operator="a")
    model = FakeModel(make_signal(tags=["instagram"]), propose_with=_ask)
    result = await run_once(
        intel_pool, make_deps(intel_pool, FakeFetcher({URL: make_page(POLICY, URL)}), model)
    )
    assert result.status == "completed"
    assert result.outcome["question_research_queued"] == 1
    payload = json.loads(model.proposal_users[0])
    assert payload["questions_open"] is True and payload["max_question_options"] == 8
    assert (
        await _one(intel_pool, "SELECT count(*) FROM intel_proposals WHERE kind = 'quiz_question'")
        == 1
    )


async def test_max_pending_zero_closes_question_proposals(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    await PostgresIntelStore(intel_pool).queue_adhoc(URL, operator="a")
    model = FakeModel(make_signal(tags=["instagram"]), propose_with=_ask)
    deps = make_deps(
        intel_pool,
        FakeFetcher({URL: make_page(POLICY, URL)}),
        model,
        question_limits=QuestionLimits(1, 1, 90, 8, 0, 90),
    )
    result = await run_once(intel_pool, deps)
    assert json.loads(model.proposal_users[0])["questions_open"] is False
    assert result.outcome["proposal_dropped_questions_closed"] == 1


async def test_a_research_searchs_discovery_never_opens_questions(
    intel_pool: AsyncConnectionPool,
) -> None:
    from imageshield.intel.pipeline import _Ctx, _discovery

    await seed_quiz_vocabulary(intel_pool)
    async with intel_pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO intel_sources (kind, query_text, tags, check_every_hours, next_check_at,"
            " enabled, disabled_reason, created_by, origin, proposed_for) VALUES ('search_query',"
            " 'q', '{}', 168, now(), false, 'research_once', 'system:research', 'research', %s)"
            " RETURNING source_id",
            (Jsonb({"question_key": "k", "option": "o"}),),
        )
        row = await cur.fetchone()
        assert row is not None
        cur = await conn.execute(
            "INSERT INTO intel_runs (kind, source_id, requested_by) VALUES ('discovery', %s, 'x')"
            " RETURNING run_id",
            (row[0],),
        )
        run_row = await cur.fetchone()
        assert run_row is not None
    run = await PostgresQuestionStore(intel_pool).get_run(run_row[0])
    assert run is not None
    ctx = _Ctx(
        run=run,
        deps=make_deps(intel_pool, FakeFetcher({}), FakeModel()),
        vocabulary=None,
        registry=None,
        questions_open=True,
    )
    with contextlib.suppress(Exception):  # only the flag matters here
        await _discovery(ctx)
    assert ctx.questions_open is False


# ── configuration (§7) ────────────────────────────────────────────────────────

QUESTION_KEYS = {
    "INTEL_QUESTION_MIN_SIGNALS": "3",
    "INTEL_QUESTION_MIN_PUBLISHERS": "2",
    "INTEL_QUESTION_WINDOW_DAYS": "90",
    "INTEL_QUESTION_MAX_OPTIONS": "8",
    "INTEL_QUESTION_MAX_PENDING": "3",
    "INTEL_QUESTION_DISMISSED_DAYS": "90",
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    monkeypatch.chdir(tmp_path)
    for key in _ALL_ENV_NAMES:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


@pytest.mark.parametrize("key", sorted(QUESTION_KEYS))
def test_every_question_key_is_required(env: pytest.MonkeyPatch, key: str) -> None:
    _env(env)
    env.delenv(key)
    with pytest.raises(ConfigError, match=key):
        load_intel_config()


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("INTEL_QUESTION_MIN_SIGNALS", "0"),
        ("INTEL_QUESTION_MIN_PUBLISHERS", "0"),
        ("INTEL_QUESTION_WINDOW_DAYS", "0"),
        ("INTEL_QUESTION_MAX_OPTIONS", "1"),
        ("INTEL_QUESTION_MAX_PENDING", "-1"),
        ("INTEL_QUESTION_DISMISSED_DAYS", "-1"),
    ],
)
def test_a_question_key_out_of_bounds_refuses_boot(
    env: pytest.MonkeyPatch, key: str, bad: str
) -> None:
    _env(env, **{key: bad})
    with pytest.raises(ConfigError, match=key):
        load_intel_config()


def test_question_limits_carry_the_configured_values(env: pytest.MonkeyPatch) -> None:
    _env(env, INTEL_QUESTION_MAX_PENDING="0")
    assert load_intel_config().question_limits() == QuestionLimits(3, 2, 90, 8, 0, 90)


@pytest.mark.parametrize(
    "path",
    ["infra/ecs/imageshield-dev-services-worker.json", "infra/ecs/prod/services-worker.json"],
)
def test_both_task_definitions_set_every_question_key(path: str) -> None:
    doc = json.loads((REPO / path).read_text())
    (worker,) = [c for c in doc["containerDefinitions"] if c["name"] == "intel-worker"]
    values = {e["name"]: e["value"] for e in worker["environment"]}
    assert {k: values.get(k) for k in QUESTION_KEYS} == QUESTION_KEYS


def test_env_example_names_every_question_key() -> None:
    text = (REPO / ".env.example").read_text()
    for key, value in QUESTION_KEYS.items():
        assert f"\n{key}={value}\n" in text, key


# ── final review fixes (2026-10-13) ───────────────────────────────────────────


def test_a_key_carrying_personal_data_is_dropped() -> None:
    signals = _three()
    batch, counts = _validate([_question(signals, question_key="q_4155552671")], signals)
    assert batch.quiz_questions == []
    assert counts["proposal_dropped_question_key_pii"] == 1


ECHOED = (
    "Public profiles are scraped far more often than private ones by the tools that collect"
    " photos of people for training image models."
)


def test_echoes_of_one_source_are_not_corroboration_for_a_question() -> None:
    """The bar counts publishers; the decision collapses echoed excerpts. A question resting on one
    text quoted by two outlets is never written, so no research run is spent on it."""
    echoed = [replace(s, excerpts=(ECHOED,)) for s in _three()]
    batch, counts = _validate([_question(echoed)], echoed)
    assert batch.quiz_questions == []
    assert counts["proposal_dropped_question_uncorroborated"] == 1


async def test_the_write_never_writes_a_key_the_live_quiz_has(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    sid = await seed_signal(intel_pool, tags=("instagram",))
    result = await _write(intel_pool, [_new_proposal([sid], key="dating", subject="dating apps")])
    assert result.written == () and result.dropped_questions == 1


async def test_a_suggestion_older_than_the_question_never_supplies_its_points(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    sid = await seed_signal(intel_pool, tags=("instagram",))
    await seed_proposal(
        intel_pool,
        signal_ids=[sid],
        kind="weight_suggestion",
        status="delivered",
        target={
            "question_key": "old_key",
            "options": [
                {"option": "All of them", "deduction": 5, "signal_ids": [str(sid)]},
                {"option": "None of them", "deduction": 0, "signal_ids": [str(sid)]},
            ],
        },
        created_at=datetime.now(UTC) - timedelta(days=1),
    )
    result = await _write(intel_pool, [_new_proposal([sid], key="old_key", subject="old key")])
    (pid,) = result.written
    (run_id,) = result.research_runs
    async with intel_pool.connection() as conn:
        await conn.execute(
            "UPDATE intel_runs SET status = 'completed' WHERE run_id = %s", (run_id,)
        )
    assert await _refused(intel_pool, pid) == "question_incomplete"


async def test_an_answer_whose_evidence_was_retracted_gets_no_default_points(
    intel_pool: AsyncConnectionPool,
) -> None:
    from imageshield.intel.evidence_store import PostgresEvidenceStore

    pid = await _question_with_research(intel_pool, numbers=None)
    retracted = await seed_signal(intel_pool, tags=("instagram",))
    kept = await seed_signal(intel_pool, tags=("instagram",))
    await seed_proposal(
        intel_pool,
        signal_ids=[retracted, kept],
        kind="weight_suggestion",
        status="delivered",
        target={
            "question_key": "public_profiles",
            "options": [
                {"option": "All of them", "deduction": 5, "signal_ids": [str(retracted)]},
                {"option": "None of them", "deduction": 0, "signal_ids": [str(kept)]},
            ],
        },
    )
    await PostgresEvidenceStore(intel_pool).retract_signal(retracted, operator="a", reason="wrong")
    assert await _refused(intel_pool, pid) == "question_incomplete"
    decided = await _decide(intel_pool, pid, values={"options": [{"deduction": 3}, {}]})
    assert decided.decided is not None
    assert [o["deduction"] for o in decided.decided["options"]] == [3, 0]


async def test_text_an_operator_types_is_stored_normalised(
    intel_pool: AsyncConnectionPool,
) -> None:
    pid = await _question_with_research(intel_pool, numbers={"All of them": 5, "None of them": 0})
    decided = await _decide(
        intel_pool,
        pid,
        values={
            "prompt": "Are  the accounts" + chr(160) + "you post on public?",
            "options": [{}, {"option": " None "}],
        },
    )
    assert decided.decided is not None
    assert decided.decided["prompt"] == "Are the accounts you post on public?"
    assert decided.decided["options"][1]["option"] == "None"


async def test_a_weight_suggestion_run_never_opens_questions(
    intel_pool: AsyncConnectionPool,
) -> None:
    """The loop guard: a Suggest points run, a question's own research among them, generates over
    what it read with questions closed, and drops any question the model returns anyway."""
    from tests.question_fakes import FakeQuestionModel
    from tests.test_intel_question_runs import NEW_TERMS, SUGGEST_REQUEST, _chosen, _cite_everything

    await seed_quiz_vocabulary(intel_pool)
    await PostgresQuestionStore(intel_pool).register_and_queue_suggestion(
        SUGGEST_REQUEST, [_chosen()], operator="Ann Operator"
    )
    model = FakeQuestionModel(
        make_signal(tags=["instagram"]), suggest_with=_cite_everything(), propose_with=_ask
    )
    fetcher = FakeFetcher({NEW_TERMS: make_page(POLICY, NEW_TERMS)})
    result = await run_once(intel_pool, make_deps(intel_pool, fetcher, model))
    assert result.status == "completed", result
    assert model.propose_calls == 1
    assert json.loads(model.proposal_users[0])["questions_open"] is False
    assert result.outcome["proposal_dropped_questions_closed"] == 1
    count = "SELECT count(*) FROM intel_proposals WHERE kind = 'quiz_question'"
    assert await _one(intel_pool, count) == 0


async def test_a_gap_regeneration_never_opens_questions(intel_pool: AsyncConnectionPool) -> None:
    from tests.test_intel_question_runs import _done_run

    await seed_quiz_vocabulary(intel_pool)
    done = await _done_run(intel_pool)
    sid = await seed_signal(intel_pool, run_id=done, subjects=("Telegram",))
    request = {"coverage_gap_id": str(uuid4()), "tag": "instagram", "signal_ids": [str(sid)]}
    async with intel_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO intel_runs (kind, request, requested_by)"
            " VALUES ('gap_regenerate', %s, 'x')",
            (Jsonb(request),),
        )
    model = FakeModel(propose_with=_ask)
    result = await run_once(intel_pool, make_deps(intel_pool, FakeFetcher({}), model))
    assert model.propose_calls == 1
    assert json.loads(model.proposal_users[0])["questions_open"] is False
    assert result.outcome["proposal_dropped_questions_closed"] == 1


async def test_at_the_open_question_cap_a_run_proposes_none(
    intel_pool: AsyncConnectionPool,
) -> None:
    from tests.test_intel_question_runs import _done_run

    await seed_quiz_vocabulary(intel_pool)
    sid = await seed_signal(intel_pool, run_id=await _done_run(intel_pool), tags=("instagram",))
    for n in range(3):
        await seed_proposal(
            intel_pool,
            signal_ids=[sid],
            kind="quiz_question",
            target={"question_key": f"open_{n}", "subject": f"open {n}"},
            suggested={"prompt": "Q?", "multi": False, "options": []},
        )
    await PostgresIntelStore(intel_pool).queue_adhoc(URL, operator="a")
    model = FakeModel(make_signal(tags=["instagram"]), propose_with=_ask)
    result = await run_once(
        intel_pool, make_deps(intel_pool, FakeFetcher({URL: make_page(POLICY, URL)}), model)
    )
    assert json.loads(model.proposal_users[0])["questions_open"] is False
    assert result.outcome["questions_closed_at_cap"] == 1
    assert result.outcome["proposal_dropped_questions_closed"] == 1


async def test_an_ordinary_searchs_discovery_opens_questions(
    intel_pool: AsyncConnectionPool,
) -> None:
    from imageshield.intel.pipeline import _Ctx, _discovery

    await seed_quiz_vocabulary(intel_pool)
    async with intel_pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO intel_sources (kind, query_text, tags, check_every_hours, next_check_at,"
            " enabled, created_by, origin) VALUES ('search_query', 'q', '{}', 168, now(), true,"
            " 'ann', 'operator') RETURNING source_id"
        )
        row = await cur.fetchone()
        assert row is not None
        cur = await conn.execute(
            "INSERT INTO intel_runs (kind, source_id, requested_by) VALUES ('discovery', %s, 'x')"
            " RETURNING run_id",
            (row[0],),
        )
        run_row = await cur.fetchone()
        assert run_row is not None
    run = await PostgresQuestionStore(intel_pool).get_run(run_row[0])
    assert run is not None
    ctx = _Ctx(
        run=run,
        deps=make_deps(intel_pool, FakeFetcher({}), FakeModel()),
        vocabulary=None,
        registry=None,
    )
    with contextlib.suppress(Exception):  # only the flag matters here
        await _discovery(ctx)
    assert ctx.questions_open is True
