"""A suggestion's research state survives its waits (spec 2026-10-10-intel-every-answer-researched
§2.2, §3)."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from imageshield.intel.pipeline import ResearchWait
from imageshield.intel.question_store import PostgresQuestionStore
from imageshield.intel.run_log import run_research_event
from imageshield.intel.store import PostgresIntelStore
from tests.intel_fakes import NOW, claim, seed_quiz_vocabulary
from tests.question_fakes import QUESTION

REQUEST = {**QUESTION, "type": "mutable", "cap": 8}


async def test_wait_run_keeps_the_research_state(intel_pool: AsyncConnectionPool) -> None:
    await seed_quiz_vocabulary(intel_pool)
    questions = PostgresQuestionStore(intel_pool)
    queued = await questions.register_and_queue_suggestion(REQUEST, [], operator="ann")
    claimed = await claim(intel_pool)
    assert claimed is not None and claimed.run_id == queued.run_id
    source_ids = (uuid4(), uuid4())
    kept = {"model_id": "m", "options": [], "backed": {}, "searches": {"Bumble": []}}
    assert await PostgresIntelStore(intel_pool).wait_run(
        claimed.run_id,
        attempts=claimed.attempts,
        outcome={"research_rounds": 1},
        awaiting=list(source_ids),
        deadline=NOW + timedelta(minutes=45),
        not_before=None,
        research=ResearchWait(round=1, source_ids=source_ids, kept=kept, answers=1),
    )
    run = await questions.get_run(claimed.run_id)
    assert run is not None
    assert (run.research_round, run.research_source_ids, run.research_pass) == (
        1,
        list(source_ids),
        kept,
    )


async def test_a_wait_without_research_leaves_the_state_alone(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    questions = PostgresQuestionStore(intel_pool)
    await questions.register_and_queue_suggestion(REQUEST, [], operator="ann")
    claimed = await claim(intel_pool)
    assert claimed is not None
    assert await PostgresIntelStore(intel_pool).wait_run(
        claimed.run_id,
        attempts=claimed.attempts,
        outcome={},
        awaiting=[uuid4()],
        deadline=NOW + timedelta(minutes=45),
        not_before=None,
    )
    run = await questions.get_run(claimed.run_id)
    assert run is not None
    assert (run.research_round, run.research_source_ids, run.research_pass) == (0, None, None)


@pytest.mark.parametrize(
    ("answers", "round_no", "text"),
    [
        (1, 1, "Searching again for 1 answer (round 1 of 4)"),
        (2, 3, "Searching again for 2 answers (round 3 of 4)"),
    ],
)
def test_the_research_event_words(answers: int, round_no: int, text: str) -> None:
    assert run_research_event(answers, round_no, 4) == (
        text,
        {"answers": answers, "round": round_no, "max_rounds": 4},
    )
