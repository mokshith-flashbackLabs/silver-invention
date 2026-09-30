"""Proposal generation in the pipeline (spec §4.3, §10) -- real Postgres, fake model."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from imageshield.intel.evidence_store import PostgresEvidenceStore
from imageshield.intel.generation import GeneratedBatch, validate_proposals
from imageshield.intel.model import ModelCall, ModelUnavailable
from imageshield.intel.pipeline import run
from imageshield.intel.proposal_store import PostgresProposalStore
from imageshield.intel.schemas import (
    ProposalOutput,
    ProposedAttach,
    ProposedThreatEvent,
    ProposedWeightChange,
)
from imageshield.intel.store import PostgresIntelStore
from tests.intel_fakes import (
    NOW,
    POLICY,
    QUIZ_VOCABULARY,
    THREAT_SUGGESTED,
    FakeFetcher,
    FakeModel,
    claim,
    make_deps,
    make_page,
    make_signal,
    mapped_document,
    run_once,
    seed_proposal,
    seed_quiz_vocabulary,
    seed_signal,
    seed_threat_event,
    seed_threat_proposal,
    settle_runs,
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


def propose_threat(
    tags: tuple[str, ...] = ("instagram",), **extra: Any
) -> Callable[[dict[str, Any]], ProposalOutput]:
    """A fake model that proposes one threat event on ``tags`` citing every new signal."""

    def build(payload: dict[str, Any]) -> ProposalOutput:
        ids = [s["signal_id"] for s in payload["new_evidence"]]
        event = ProposedThreatEvent(
            **THREAT_SUGGESTED, tags=list(tags), rationale="A reported breach.", signal_ids=ids
        )
        return ProposalOutput(threat_events=[event], **extra)

    return build


async def _queue_regeneration(
    pool: AsyncConnectionPool, *, gap: UUID, tag: str, signal_ids: list[UUID]
) -> UUID:
    request = {"coverage_gap_id": str(gap), "tag": tag, "signal_ids": [str(s) for s in signal_ids]}
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO intel_runs (kind, request, requested_by)"
            " VALUES ('gap_regenerate', %s, 'schedule') RETURNING run_id",
            (Jsonb(request),),
        )
        row = await cur.fetchone()
    assert row is not None
    run_id: UUID = row[0]
    return run_id


async def test_a_run_proposes_a_threat_event_from_its_new_evidence(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    await PostgresIntelStore(intel_pool).queue_adhoc(URL, operator="a")
    model = _model(propose_with=propose_threat())
    result = await run_once(
        intel_pool, make_deps(intel_pool, FakeFetcher({URL: make_page(POLICY, URL)}), model)
    )
    assert result.status == "completed" and result.outcome["proposals_written"] == 1
    rows = await _rows(intel_pool, "SELECT kind, status, target, suggested FROM intel_proposals")
    assert rows == [("threat_event", "pending", {"tags": ["instagram"]}, THREAT_SUGGESTED)]
    payload = json.loads(model.proposal_users[0])
    assert payload["pending_events"] == [] and payload["live_events"] == []


async def test_a_threat_on_unmapped_tags_is_written_and_waits_for_a_mapping(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)  # linkedin: registered, not mapped
    await PostgresIntelStore(intel_pool).queue_adhoc(URL, operator="a")
    model = FakeModel(make_signal(tags=["linkedin"]), propose_with=propose_threat(("linkedin",)))
    await run_once(
        intel_pool, make_deps(intel_pool, FakeFetcher({URL: make_page(POLICY, URL)}), model)
    )
    (row,) = await PostgresProposalStore(intel_pool).list_proposals(
        statuses=None, kinds=["threat_event"], cursor=None, limit=5
    )
    assert (row["approvable"], row["why_not"]) == (False, "tags_unmapped")
    assert row["unmapped_tags"] == ["linkedin"]


async def test_new_evidence_attaches_to_a_pending_event_proposal(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    old = await seed_signal(intel_pool, tags=("instagram",))
    pending = await seed_threat_proposal(intel_pool, signal_ids=[old])
    await settle_runs(intel_pool)
    await PostgresIntelStore(intel_pool).queue_adhoc(URL, operator="a")

    def attach(payload: dict[str, Any]) -> ProposalOutput:
        assert [e["proposal_id"] for e in payload["pending_events"]] == [str(pending)]
        ids = [s["signal_id"] for s in payload["new_evidence"]]
        return ProposalOutput(attach=[ProposedAttach(proposal_id=str(pending), signal_ids=ids)])

    result = await run_once(
        intel_pool,
        make_deps(
            intel_pool, FakeFetcher({URL: make_page(POLICY, URL)}), _model(propose_with=attach)
        ),
    )
    assert result.outcome["proposals_attached"] == 1
    assert await _rows(
        intel_pool, f"SELECT count(*) FROM intel_proposal_signals WHERE proposal_id = '{pending}'"
    ) == [(2,)]


async def test_the_same_incident_read_again_adds_evidence_rather_than_a_second_proposal(
    intel_pool: AsyncConnectionPool,
) -> None:
    """Review Focus 4 through the pipeline. The second run re-reads the same page: a new
    intel_documents row (documents are unique per run) with the same canonical URL hash, so the
    same document for duplicate detection (spec note 2026-09-30). The same incident proposed
    again becomes new evidence for the first run's pending proposal, never a second one."""
    await seed_quiz_vocabulary(intel_pool)
    store = PostgresIntelStore(intel_pool)
    fetcher = FakeFetcher({URL: make_page(POLICY, URL)})
    await store.queue_adhoc(URL, operator="a")
    await run_once(
        intel_pool, make_deps(intel_pool, fetcher, _model(propose_with=propose_threat()))
    )
    await store.queue_adhoc(URL, operator="b")
    model = _model(propose_with=propose_threat())
    second = await run_once(intel_pool, make_deps(intel_pool, fetcher, model))
    assert len(json.loads(model.proposal_users[0])["pending_events"]) == 1
    assert second.outcome["proposal_converted_to_attach"] == 1
    assert second.outcome["proposals_attached"] == 1
    assert await _rows(intel_pool, "SELECT count(*) FROM intel_proposals") == [(1,)]
    assert await _rows(intel_pool, "SELECT count(*) FROM intel_proposal_signals") == [(2,)]


async def test_a_gap_regenerate_run_proposes_events_from_the_signals_it_names(
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
    await _queue_regeneration(intel_pool, gap=gap, tag="linkedin", signal_ids=signals)
    change = ProposedWeightChange(**INSTAGRAM_UP, signal_ids=[str(signals[0])])
    model = FakeModel(propose_with=propose_threat(("linkedin",), weight_changes=[change]))
    result = await run_once(intel_pool, make_deps(intel_pool, FakeFetcher({}), model))
    assert result.status == "completed", result
    assert result.outcome["proposals_written"] == 1
    assert result.outcome["proposal_dropped_not_an_event"] == 1
    assert model.extract_calls == 0 and model.propose_calls == 1
    assert "Propose only threat_events and attach" in model.proposal_systems[0]
    payload = json.loads(model.proposal_users[0])
    assert {s["signal_id"] for s in payload["new_evidence"]} == {str(s) for s in signals}
    (row,) = await PostgresProposalStore(intel_pool).list_proposals(
        statuses=None, kinds=["threat_event"], cursor=None, limit=5
    )
    assert set(row["signal_ids"]) == set(signals) and row["approvable"] is True


async def test_a_regeneration_whose_evidence_was_retracted_makes_no_call(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(
        intel_pool, map_version=2, document=mapped_document("LinkedIn", "linkedin")
    )
    sid = await seed_signal(intel_pool, subjects=("LinkedIn",))
    await PostgresEvidenceStore(intel_pool).retract_signal(sid, operator="a", reason="wrong")
    await settle_runs(intel_pool)
    await _queue_regeneration(intel_pool, gap=uuid4(), tag="linkedin", signal_ids=[sid])
    model = FakeModel()
    result = await run_once(intel_pool, make_deps(intel_pool, FakeFetcher({}), model))
    assert result.status == "completed"
    assert result.outcome["gap_regenerate_no_active_signals"] == 1
    assert model.propose_calls == 0


async def test_a_gate_refusal_leaves_a_regeneration_unwritten_for_the_retry(
    intel_pool: AsyncConnectionPool,
) -> None:
    """Review Focus 1, the run half: a refused regeneration writes no proposals mark, which is
    what lets the gap pass queue it again (Task 8)."""
    await seed_quiz_vocabulary(
        intel_pool, map_version=2, document=mapped_document("LinkedIn", "linkedin")
    )
    sid = await seed_signal(intel_pool, subjects=("LinkedIn",))
    await settle_runs(intel_pool)
    await _queue_regeneration(intel_pool, gap=uuid4(), tag="linkedin", signal_ids=[sid])
    async with intel_pool.connection() as conn:
        await conn.execute(
            "UPDATE providers SET enabled = false WHERE provider_id = 'claude_intel'"
        )
    model = FakeModel(propose_with=propose_threat(("linkedin",)))
    result = await run_once(intel_pool, make_deps(intel_pool, FakeFetcher({}), model))
    assert result.status == "refused" and result.outcome["refused_by"] == "gate"
    assert model.propose_calls == 0
    assert await _rows(
        intel_pool,
        "SELECT proposals_written_at IS NULL FROM intel_runs WHERE kind = 'gap_regenerate'",
    ) == [(True,)]


async def test_an_unreadable_regeneration_request_fails_the_run(
    intel_pool: AsyncConnectionPool,
) -> None:
    async with intel_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO intel_runs (kind, request, requested_by)"
            " VALUES ('gap_regenerate', '{\"tag\": \"x\"}', 'schedule')"
        )
    result = await run_once(intel_pool, make_deps(intel_pool, FakeFetcher({}), FakeModel()))
    assert (result.status, result.error_code) == ("failed", "request_unreadable")


# ── beyond the brief ───────────────────────────────────────────────────────────
# The two requirements carried from Task 4 -- a duplicate is found only if every signal handed to
# validation carries its document's key, and only if the pending proposals arrive newest first --
# and the wiring the cases above leave unpinned.


def cite_related_and_new(
    tags: tuple[str, ...] = ("instagram",),
) -> Callable[[dict[str, Any]], ProposalOutput]:
    """A fake model that proposes one threat event on ``tags`` citing the related evidence it was
    shown AND the run's new evidence, so the proposal's pages include a page an earlier run read."""

    def build(payload: dict[str, Any]) -> ProposalOutput:
        ids = [s["signal_id"] for s in payload["related_evidence"]]
        ids += [s["signal_id"] for s in payload["new_evidence"]]
        event = ProposedThreatEvent(
            **THREAT_SUGGESTED, tags=list(tags), rationale="A reported breach.", signal_ids=ids
        )
        return ProposalOutput(threat_events=[event])

    return build


def _spy_on_validation(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The keyword arguments of every validate_proposals call the pipeline makes. The real
    function still runs."""
    calls: list[dict[str, Any]] = []

    def spy(output: ProposalOutput, **kwargs: Any) -> GeneratedBatch:
        calls.append(kwargs)
        return validate_proposals(output, **kwargs)

    monkeypatch.setattr("imageshield.intel.pipeline.validate_proposals", spy)
    return calls


async def _document_keys(pool: AsyncConnectionPool) -> dict[UUID, str]:
    """signal_id -> the canonical URL hash of its document, straight from the database."""
    rows = await _rows(
        pool,
        "SELECT s.signal_id, d.url_hash FROM intel_signals s"
        " JOIN intel_documents d ON d.document_id = s.document_id",
    )
    return {row[0]: row[1] for row in rows}


class _DismissesWhileAnswering(FakeModel):
    """A model whose answer arrives after an operator has rejected ``target``: the decision lands
    between the pipeline's read of the pending proposals and its write."""

    def __init__(self, pool: AsyncConnectionPool, target: UUID, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._pool = pool
        self._target = target

    async def propose(self, system: str, user: str) -> ModelCall[ProposalOutput]:
        answer = await super().propose(system, user)
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE intel_proposals SET status = 'rejected', decided_by = 'ann',"
                " decided_at = now(), decision_reason = 'not this' WHERE proposal_id = %s",
                (self._target,),
            )
        return answer


async def test_every_signal_handed_to_validation_carries_its_documents_key(
    intel_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Carried from Task 4. Duplicate detection compares documents by canonical URL hash, and a
    signal without one matches nothing, so a duplicate would quietly become a second pending
    proposal. The run's own signal, a related one and a gap-pool candidate are all checked
    against the database's own hash."""
    await seed_quiz_vocabulary(intel_pool)
    related = await seed_signal(intel_pool, tags=("instagram",))
    candidate = await seed_signal(intel_pool, subjects=("Bluesky",), category="incident")
    await settle_runs(intel_pool)
    await PostgresIntelStore(intel_pool).queue_adhoc(URL, operator="a")
    calls = _spy_on_validation(monkeypatch)
    fetcher = FakeFetcher({URL: make_page(POLICY, URL)})
    await run_once(intel_pool, make_deps(intel_pool, fetcher, _model()))
    (call,) = calls
    keys = await _document_keys(intel_pool)
    context, gap_pool = call["context"], call["gap_pool"]
    assert len(context) == 2 and related in context  # the run's own signal, and the related one
    assert [s.signal_id for s in gap_pool] == [candidate]
    for signal in [*context.values(), *gap_pool]:
        assert signal.document_key is not None
        assert signal.document_key == keys[signal.signal_id]


async def test_a_regenerations_signals_carry_their_documents_key_too(
    intel_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed_quiz_vocabulary(
        intel_pool, map_version=2, document=mapped_document("LinkedIn", "linkedin")
    )
    signals = [
        await seed_signal(intel_pool, subjects=("LinkedIn",), publisher=p)
        for p in ("a.example", "b.example")
    ]
    related = await seed_signal(intel_pool, tags=("linkedin",), publisher="c.example")
    await settle_runs(intel_pool)
    await _queue_regeneration(intel_pool, gap=uuid4(), tag="linkedin", signal_ids=signals)
    calls = _spy_on_validation(monkeypatch)
    await run_once(intel_pool, make_deps(intel_pool, FakeFetcher({}), FakeModel()))
    (call,) = calls
    keys = await _document_keys(intel_pool)
    assert set(call["context"]) == {*signals, related}
    assert call["events_only"] is True and call["new_signal_ids"] == frozenset(signals)
    for signal in [*call["context"].values(), *call["gap_pool"]]:
        assert signal.document_key is not None
        assert signal.document_key == keys[signal.signal_id]


async def test_a_duplicate_is_found_through_the_page_of_a_related_signal(
    intel_pool: AsyncConnectionPool,
) -> None:
    """Carried from Task 4, the related half. The second run reads a DIFFERENT page and cites the
    first run's signal (related evidence) beside its own. The proposal's pages then include the
    pending proposal's, so it is that proposal again -- but only if the related signal arrived
    with its document's key."""
    await seed_quiz_vocabulary(intel_pool)
    other = "https://q.example/news"
    store = PostgresIntelStore(intel_pool)
    fetcher = FakeFetcher({URL: make_page(POLICY, URL), other: make_page(POLICY, other)})
    await store.queue_adhoc(URL, operator="a")
    await run_once(
        intel_pool, make_deps(intel_pool, fetcher, _model(propose_with=propose_threat()))
    )
    await store.queue_adhoc(other, operator="b")
    model = _model(propose_with=cite_related_and_new())
    second = await run_once(intel_pool, make_deps(intel_pool, fetcher, model))
    assert len(json.loads(model.proposal_users[0])["related_evidence"]) == 1
    assert second.outcome["proposal_converted_to_attach"] == 1
    assert second.outcome["proposals_attached"] == 1 and second.outcome["proposals_written"] == 0
    assert await _rows(intel_pool, "SELECT count(*) FROM intel_proposals") == [(1,)]
    assert await _rows(intel_pool, "SELECT count(*) FROM intel_proposal_signals") == [(2,)]


async def test_the_newest_of_two_pending_twins_takes_the_new_evidence(
    intel_pool: AsyncConnectionPool,
) -> None:
    """Carried from Task 4. duplicate_of takes the first match in the order it is given, so the
    pipeline must hand it the store's newest-first order. Two pending proposals on one tag set
    both rest on a page a third run reads again; the newer one is the one that grows."""
    await seed_quiz_vocabulary(intel_pool)
    store = PostgresIntelStore(intel_pool)
    fetcher = FakeFetcher({URL: make_page(POLICY, URL)})
    for operator in ("a", "b"):  # two earlier runs read the page; neither proposes anything
        await store.queue_adhoc(URL, operator=operator)
        await run_once(intel_pool, make_deps(intel_pool, fetcher, _model()))
    earlier = [
        row[0]
        for row in await _rows(
            intel_pool,
            "SELECT s.signal_id FROM intel_signals s JOIN intel_documents d USING (document_id)"
            " JOIN intel_runs r USING (run_id) ORDER BY r.created_at",
        )
    ]
    older = await seed_threat_proposal(
        intel_pool, signal_ids=[earlier[0]], created_at=NOW - timedelta(hours=2)
    )
    newer = await seed_threat_proposal(
        intel_pool, signal_ids=[earlier[1]], created_at=NOW - timedelta(hours=1)
    )
    await store.queue_adhoc(URL, operator="c")
    model = _model(propose_with=propose_threat())
    result = await run_once(intel_pool, make_deps(intel_pool, fetcher, model))
    shown = json.loads(model.proposal_users[0])["pending_events"]
    assert [e["proposal_id"] for e in shown] == [str(newer), str(older)]
    assert result.outcome["proposal_converted_to_attach"] == 1
    assert await _rows(
        intel_pool, f"SELECT count(*) FROM intel_proposal_signals WHERE proposal_id = '{newer}'"
    ) == [(2,)]
    assert await _rows(
        intel_pool, f"SELECT count(*) FROM intel_proposal_signals WHERE proposal_id = '{older}'"
    ) == [(1,)]


async def test_an_attachment_to_a_proposal_decided_meanwhile_is_dropped_and_counted(
    intel_pool: AsyncConnectionPool,
) -> None:
    """The write re-checks under lock that the target is still pending (Task 5). The run counts
    what that drops, so a dismissed proposal's lost evidence shows on the run, not nowhere."""
    await seed_quiz_vocabulary(intel_pool)
    old = await seed_signal(intel_pool, tags=("instagram",))
    pending = await seed_threat_proposal(intel_pool, signal_ids=[old])
    await settle_runs(intel_pool)
    await PostgresIntelStore(intel_pool).queue_adhoc(URL, operator="a")

    def attach(payload: dict[str, Any]) -> ProposalOutput:
        ids = [s["signal_id"] for s in payload["new_evidence"]]
        return ProposalOutput(attach=[ProposedAttach(proposal_id=str(pending), signal_ids=ids)])

    model = _DismissesWhileAnswering(
        intel_pool, pending, extraction=make_signal(tags=["instagram"]), propose_with=attach
    )
    result = await run_once(
        intel_pool, make_deps(intel_pool, FakeFetcher({URL: make_page(POLICY, URL)}), model)
    )
    assert result.status == "completed"
    assert result.outcome["attach_dropped_not_pending"] == 1
    assert "proposals_attached" not in result.outcome
    assert await _rows(
        intel_pool, f"SELECT count(*) FROM intel_proposal_signals WHERE proposal_id = '{pending}'"
    ) == [(1,)]


async def test_an_attachment_may_add_only_the_runs_own_evidence(
    intel_pool: AsyncConnectionPool,
) -> None:
    """A related signal is active and was shown to the model, but an earlier run wrote it: it is
    not new evidence, so an attachment naming it is dropped whole."""
    await seed_quiz_vocabulary(intel_pool)
    old = await seed_signal(intel_pool, tags=("instagram",))
    pending = await seed_threat_proposal(intel_pool, signal_ids=[old])
    stray = await seed_signal(intel_pool, tags=("instagram",), publisher="b.example")
    await settle_runs(intel_pool)
    await PostgresIntelStore(intel_pool).queue_adhoc(URL, operator="a")

    def attach_stray(payload: dict[str, Any]) -> ProposalOutput:
        assert str(stray) in [s["signal_id"] for s in payload["related_evidence"]]
        attach = ProposedAttach(proposal_id=str(pending), signal_ids=[str(stray)])
        return ProposalOutput(attach=[attach])

    result = await run_once(
        intel_pool,
        make_deps(
            intel_pool,
            FakeFetcher({URL: make_page(POLICY, URL)}),
            _model(propose_with=attach_stray),
        ),
    )
    assert result.outcome["attach_dropped_signal_not_new"] == 1
    assert "proposals_attached" not in result.outcome
    assert await _rows(
        intel_pool, f"SELECT count(*) FROM intel_proposal_signals WHERE proposal_id = '{pending}'"
    ) == [(1,)]


async def test_a_reading_run_is_shown_the_live_events_on_its_tags_and_no_more(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    live = await seed_threat_event(intel_pool, tags=("instagram",), title="Seeded incident")
    await seed_threat_event(intel_pool, tags=("linkedin",))  # another tag: not shown
    await PostgresIntelStore(intel_pool).queue_adhoc(URL, operator="a")
    model = _model()
    await run_once(
        intel_pool, make_deps(intel_pool, FakeFetcher({URL: make_page(POLICY, URL)}), model)
    )
    payload = json.loads(model.proposal_users[0])
    assert [(e["event_id"], e["title"]) for e in payload["live_events"]] == [
        (str(live), "Seeded incident")
    ]
    # events-only is a regeneration's instruction, never a reading run's
    assert "Propose only threat_events" not in model.proposal_systems[0]


async def test_a_regeneration_is_shown_the_events_on_its_tag_though_its_signals_carry_none(
    intel_pool: AsyncConnectionPool,
) -> None:
    """A gap's signals name the subject as UNREGISTERED, so none of them carries a tag: it is the
    request's newly mapped tag that finds the pending proposals and live events on it."""
    await seed_quiz_vocabulary(
        intel_pool, map_version=2, document=mapped_document("LinkedIn", "linkedin")
    )
    signals = [
        await seed_signal(intel_pool, subjects=("LinkedIn",), publisher=p)
        for p in ("a.example", "b.example")
    ]
    cited = await seed_signal(intel_pool, tags=("linkedin",), publisher="c.example")
    pending = await seed_threat_proposal(intel_pool, signal_ids=[cited], tags=("linkedin",))
    live = await seed_threat_event(intel_pool, tags=("linkedin",))
    await seed_threat_event(intel_pool, tags=("instagram",))  # another tag: not shown
    await settle_runs(intel_pool)
    await _queue_regeneration(intel_pool, gap=uuid4(), tag="linkedin", signal_ids=signals)
    fetcher, model = FakeFetcher({}), FakeModel()
    result = await run_once(intel_pool, make_deps(intel_pool, fetcher, model))
    assert result.status == "completed"
    payload = json.loads(model.proposal_users[0])
    assert [e["proposal_id"] for e in payload["pending_events"]] == [str(pending)]
    assert [e["event_id"] for e in payload["live_events"]] == [str(live)]
    assert fetcher.fetched == []  # a regeneration reads nothing


async def test_a_regeneration_that_repeats_a_pending_event_attaches_its_signals_to_it(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(
        intel_pool, map_version=2, document=mapped_document("LinkedIn", "linkedin")
    )
    signals = [
        await seed_signal(intel_pool, subjects=("LinkedIn",), publisher=p)
        for p in ("a.example", "b.example")
    ]
    cited = await seed_signal(intel_pool, tags=("linkedin",), publisher="c.example")
    pending = await seed_threat_proposal(intel_pool, signal_ids=[cited], tags=("linkedin",))
    await settle_runs(intel_pool)
    await _queue_regeneration(intel_pool, gap=uuid4(), tag="linkedin", signal_ids=signals)
    model = FakeModel(propose_with=cite_related_and_new(("linkedin",)))
    result = await run_once(intel_pool, make_deps(intel_pool, FakeFetcher({}), model))
    assert result.outcome["proposal_converted_to_attach"] == 1
    assert result.outcome["proposals_attached"] == 1 and result.outcome["proposals_written"] == 0
    assert await _rows(intel_pool, "SELECT count(*) FROM intel_proposals") == [(1,)]
    assert await _rows(
        intel_pool, f"SELECT count(*) FROM intel_proposal_signals WHERE proposal_id = '{pending}'"
    ) == [(3,)]


async def test_a_reclaimed_regeneration_does_not_generate_twice(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(
        intel_pool, map_version=2, document=mapped_document("LinkedIn", "linkedin")
    )
    sid = await seed_signal(intel_pool, subjects=("LinkedIn",))
    await settle_runs(intel_pool)
    await _queue_regeneration(intel_pool, gap=uuid4(), tag="linkedin", signal_ids=[sid])
    model = FakeModel(propose_with=propose_threat(("linkedin",)))
    deps = make_deps(intel_pool, FakeFetcher({}), model)
    claimed = await claim(intel_pool)
    await run(claimed, deps)
    again = await run(claimed, deps)  # a worker died before finish_run; the run is re-executed
    assert again.outcome["proposals_already_written"] == 1
    assert model.propose_calls == 1
    assert await _rows(intel_pool, "SELECT count(*) FROM intel_proposals") == [(1,)]


async def test_an_unavailable_model_fails_a_regeneration_and_leaves_it_unwritten(
    intel_pool: AsyncConnectionPool,
) -> None:
    """The retry (Task 8) queues a regeneration that ended refused OR failed without writing."""
    await seed_quiz_vocabulary(
        intel_pool, map_version=2, document=mapped_document("LinkedIn", "linkedin")
    )
    sid = await seed_signal(intel_pool, subjects=("LinkedIn",))
    await settle_runs(intel_pool)
    await _queue_regeneration(intel_pool, gap=uuid4(), tag="linkedin", signal_ids=[sid])
    model = FakeModel(propose_unavailable=ModelUnavailable("timeout", "APITimeoutError"))
    result = await run_once(intel_pool, make_deps(intel_pool, FakeFetcher({}), model))
    assert (result.status, result.error_code) == ("failed", "timeout")
    assert await _rows(
        intel_pool,
        "SELECT proposals_written_at IS NULL FROM intel_runs WHERE kind = 'gap_regenerate'",
    ) == [(True,)]
