"""Proposal generation in the pipeline (spec §4.3, §10) -- real Postgres, fake model."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from psycopg_pool import AsyncConnectionPool

from imageshield.intel.model import ModelUnavailable
from imageshield.intel.pipeline import run
from imageshield.intel.schemas import ProposalOutput, ProposedWeightChange
from imageshield.intel.store import PostgresIntelStore
from tests.intel_fakes import (
    POLICY,
    QUIZ_VOCABULARY,
    FakeFetcher,
    FakeModel,
    claim,
    make_deps,
    make_page,
    make_signal,
    run_once,
    seed_quiz_vocabulary,
)

URL = "https://p.example/terms"


def cite_new(**change: Any) -> Callable[[dict[str, Any]], ProposalOutput]:
    """A fake model that proposes ``change`` citing every signal the run just wrote."""

    def build(payload: dict[str, Any]) -> ProposalOutput:
        ids = [s["signal_id"] for s in payload["new_evidence"]]
        return ProposalOutput(weight_changes=[ProposedWeightChange(**change, signal_ids=ids)])

    return build


INSTAGRAM_UP = {
    "question_key": "platforms",
    "option": "Instagram",
    "current": 3,
    "delta": 1,
    "rationale": "Public photos now train AI models by default.",
}


async def _rows(pool: AsyncConnectionPool, query: str) -> list[tuple[Any, ...]]:
    async with pool.connection() as conn:
        cur = await conn.execute(query)
        return list(await cur.fetchall())


def _model(**kw: Any) -> FakeModel:
    return FakeModel(make_signal(tags=["instagram"]), **kw)


async def test_a_run_that_wrote_signals_proposes_a_weight_change(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    await PostgresIntelStore(intel_pool).queue_adhoc(URL, operator="a")
    model = _model(propose_with=cite_new(**INSTAGRAM_UP))
    result = await run_once(
        intel_pool,
        make_deps(intel_pool, FakeFetcher({URL: make_page(POLICY, URL)}), model),
    )
    assert result.status == "completed" and result.outcome["proposals_written"] == 1
    assert result.outcome["model_calls"] == 2 and model.propose_calls == 1
    rows = await _rows(
        intel_pool,
        "SELECT p.kind, p.status, r.vocabulary_release_no, r.vocabulary_map_version"
        " FROM intel_proposals p JOIN intel_runs r USING (run_id)",
    )
    assert rows == [("weight_change", "pending", 2, 1)]
    # spec §10: every signal can name the vocabulary pair it was produced against
    signal_pairs = await _rows(
        intel_pool,
        "SELECT r.vocabulary_release_no, r.vocabulary_map_version FROM intel_signals s"
        " JOIN intel_documents d USING (document_id) JOIN intel_runs r ON r.run_id = d.run_id",
    )
    assert signal_pairs == [(2, 1)]
    assert await _rows(intel_pool, "SELECT status FROM provider_calls ORDER BY created_at") == [
        ("ok",),
        ("ok",),
    ]


async def test_a_brand_new_mutable_question_is_proposable_with_no_code_change(
    intel_pool: AsyncConnectionPool,
) -> None:
    doc = copy.deepcopy(QUIZ_VOCABULARY)
    doc["questions"].append(
        {
            "key": "dating_apps",
            "prompt": "Which dating apps?",
            "type": "mutable",
            "options": ["Bumble"],
            "deductions": {"Bumble": 3},
            "cap": None,
        }
    )
    await seed_quiz_vocabulary(intel_pool, release_no=3, document=doc)
    await PostgresIntelStore(intel_pool).queue_adhoc(URL, operator="a")
    change = {**INSTAGRAM_UP, "question_key": "dating_apps", "option": "Bumble"}
    result = await run_once(
        intel_pool,
        make_deps(
            intel_pool,
            FakeFetcher({URL: make_page(POLICY, URL)}),
            _model(propose_with=cite_new(**change)),
        ),
    )
    assert result.outcome["proposals_written"] == 1


async def test_a_reclaimed_run_does_not_generate_twice(intel_pool: AsyncConnectionPool) -> None:
    await seed_quiz_vocabulary(intel_pool)
    await PostgresIntelStore(intel_pool).queue_adhoc(URL, operator="a")
    model = _model(propose_with=cite_new(**INSTAGRAM_UP))
    deps = make_deps(intel_pool, FakeFetcher({URL: make_page(POLICY, URL)}), model)
    claimed = await claim(intel_pool)
    await run(claimed, deps)
    again = await run(claimed, deps)  # a worker died before finish_run; the run is re-executed
    assert again.outcome["already_recorded"] == 1
    assert again.outcome["proposals_already_written"] == 1
    assert model.propose_calls == 1 and model.extract_calls == 1
    assert await _rows(intel_pool, "SELECT count(*) FROM intel_proposals") == [(1,)]


async def test_a_truncated_proposal_call_is_consumed_not_retried(
    intel_pool: AsyncConnectionPool,
) -> None:
    """Review Focus 2."""
    await seed_quiz_vocabulary(intel_pool)
    await PostgresIntelStore(intel_pool).queue_adhoc(URL, operator="a")
    model = _model(proposal_outcome="max_tokens")
    deps = make_deps(intel_pool, FakeFetcher({URL: make_page(POLICY, URL)}), model)
    claimed = await claim(intel_pool)
    first = await run(claimed, deps)
    assert first.status == "completed" and first.outcome["proposal_model_max_tokens"] == 1
    assert await _rows(intel_pool, "SELECT count(*) FROM intel_proposals") == [(0,)]
    assert await _rows(intel_pool, "SELECT proposals_written_at IS NOT NULL FROM intel_runs") == [
        (True,)
    ]
    await run(claimed, deps)
    assert model.propose_calls == 1


async def test_an_unavailable_model_at_generation_fails_the_run_and_keeps_the_signals(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    await PostgresIntelStore(intel_pool).queue_adhoc(URL, operator="a")
    model = _model(propose_unavailable=ModelUnavailable("timeout", "APITimeoutError"))
    result = await run_once(
        intel_pool, make_deps(intel_pool, FakeFetcher({URL: make_page(POLICY, URL)}), model)
    )
    assert result.status == "failed" and result.error_code == "timeout"
    assert result.outcome["proposals_deferred_timeout"] == 1
    assert result.outcome["signals_kept"] == 1
    assert await _rows(intel_pool, "SELECT proposals_written_at IS NULL FROM intel_runs") == [
        (True,)
    ]


async def test_no_vocabulary_skips_generation(intel_pool: AsyncConnectionPool) -> None:
    async with intel_pool.connection() as conn:
        await conn.execute("DELETE FROM intel_vocabulary")  # superuser test DB
    await PostgresIntelStore(intel_pool).queue_adhoc(URL, operator="a")
    model = _model()
    result = await run_once(
        intel_pool, make_deps(intel_pool, FakeFetcher({URL: make_page(POLICY, URL)}), model)
    )
    assert result.outcome["proposals_skipped_vocabulary_missing"] == 1
    assert model.propose_calls == 0


async def test_generation_is_one_call_beyond_the_reading_cap(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    await PostgresIntelStore(intel_pool).queue_adhoc(URL, operator="a")
    model = _model(propose_with=cite_new(**INSTAGRAM_UP))
    deps = make_deps(
        intel_pool, FakeFetcher({URL: make_page(POLICY, URL)}), model, max_calls_per_run=1
    )
    result = await run_once(intel_pool, deps)
    assert result.outcome["model_calls"] == 2 and result.outcome["proposals_written"] == 1
