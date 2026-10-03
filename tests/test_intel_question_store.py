"""The question store against real Postgres (spec §4.10): registration and its reuse rules, and the
validation and proposal reads stage 4 depends on."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from imageshield.intel.evidence_store import PostgresEvidenceStore
from imageshield.intel.question_store import PostgresQuestionStore
from imageshield.intel.source_choice import NewSource, candidate_key, identity
from imageshield.intel.store import PostgresIntelStore
from imageshield.intel.suggestion import OptionSuggestion
from tests.intel_fakes import seed_signal
from tests.question_fakes import QUESTION

REQUEST = {**QUESTION, "type": "mutable", "cap": 8}


def _new(
    *,
    kind: str = "policy_page",
    url: str | None = "https://p.example/terms",
    query: str | None = None,
    option: str = "Instagram",
    origin: str = "operator",
    tags: tuple[str, ...] = ("instagram",),
    note: str | None = "public terms page; automated reads allowed",
    validation: tuple[UUID, datetime] | None = None,
) -> NewSource:
    return NewSource(
        kind=kind,
        source_url=url,
        query_text=query,
        tags=tags,
        check_every_hours=168,
        terms_note=note,
        origin=origin,
        question_key="platforms",
        option=option,
        validation_run_id=validation[0] if validation is not None else None,
        validated_at=validation[1] if validation is not None else None,
    )


async def _validation_run(pool: AsyncConnectionPool) -> tuple[UUID, datetime]:
    """A completed source_validation run: its id and completion time, the evidence stage 4
    records on a source it registers (0047)."""
    ((run_id, completed_at),) = await _rows(
        pool,
        "INSERT INTO intel_runs (kind, status, requested_by, completed_at)"
        " VALUES ('source_validation', 'completed', 'ann', now() - interval '1 hour')"
        " RETURNING run_id, completed_at",
    )
    return run_id, completed_at


async def _rows(pool: AsyncConnectionPool, query: str, *params: Any) -> list[tuple[Any, ...]]:
    async with pool.connection() as conn:
        cur = await conn.execute(query, params or None)
        return list(await cur.fetchall())


async def test_registration_writes_the_sources_and_the_run_in_one_go(
    intel_pool: AsyncConnectionPool,
) -> None:
    store = PostgresQuestionStore(intel_pool)
    before = datetime.now(UTC)
    query = _new(
        kind="search_query", url=None, query="Bumble privacy news", option="Bumble", tags=()
    )
    registered = await store.register_and_queue_suggestion(
        REQUEST, [_new(origin="suggested"), query], operator="ann"
    )
    assert len(registered.registered) == 2 and registered.reused == ()
    policy, search = await _rows(
        intel_pool,
        "SELECT kind, origin, proposed_for, tags, check_every_hours, next_check_at, created_by"
        " FROM intel_sources ORDER BY kind",
    )
    assert policy[:5] == (
        "policy_page",
        "suggested",
        {"question_key": "platforms", "option": "Instagram"},
        ["instagram"],
        168,
    )
    # The run reads it at once; its first scheduled check is a full interval later.
    assert policy[5] >= before + timedelta(hours=167) and policy[6] == "ann"
    assert search[:4] == (
        "search_query",
        "operator",
        {"question_key": "platforms", "option": "Bumble"},
        [],
    )
    ((request,),) = await _rows(
        intel_pool, "SELECT request FROM intel_runs WHERE run_id = %s", registered.run_id
    )
    assert request["new_source_ids"] == [str(i) for i in registered.registered]
    assert request["source_ids"] == [str(i) for i in registered.registered]
    assert (request["type"], request["tags"]) == ("mutable", {"Instagram": ["instagram"]})
    actions = sorted(
        r[0]
        for r in await _rows(
            intel_pool, "SELECT action FROM audit_log WHERE actor_type = 'operator'"
        )
    )
    assert actions == [
        "intel.source_created",
        "intel.source_created",
        "intel.weight_suggestion_queued",
    ]


async def test_a_source_already_registered_is_reused_not_duplicated(
    intel_pool: AsyncConnectionPool,
) -> None:
    """spec §10: a source already in the registry is reused, not duplicated, when chosen again --
    and the reused row is not rewritten."""
    registry = PostgresIntelStore(intel_pool)
    by_url = await registry.create_source(
        kind="news",
        source_url="https://p.example/terms?utm_source=x",
        query_text=None,
        tags=("linkedin",),
        check_every_hours=24,
        terms_note="automated access permitted",
        operator="alice",
    )
    by_query = await registry.create_source(
        kind="search_query",
        source_url=None,
        query_text="Bumble  Privacy News",
        tags=(),
        check_every_hours=24,
        terms_note="automated access permitted",
        operator="alice",
    )
    query = _new(
        kind="search_query", url=None, query="bumble privacy news", option="Bumble", tags=()
    )
    registered = await PostgresQuestionStore(intel_pool).register_and_queue_suggestion(
        REQUEST, [_new(), query], operator="ann"
    )
    assert registered.registered == ()
    assert set(registered.reused) == {by_url.source_id, by_query.source_id}
    assert await _rows(intel_pool, "SELECT count(*) FROM intel_sources") == [(2,)]
    unchanged = await registry.get_source(by_url.source_id)
    assert unchanged is not None
    assert (unchanged.kind, unchanged.tags, unchanged.origin) == ("news", ("linkedin",), "operator")
    ((request,),) = await _rows(
        intel_pool, "SELECT request FROM intel_runs WHERE kind = 'weight_suggestion'"
    )
    assert request["new_source_ids"] == [] and len(request["source_ids"]) == 2


async def test_a_registered_source_records_its_validation_and_needs_no_terms_note(
    intel_pool: AsyncConnectionPool,
) -> None:
    """0047 (2026-10-03): the terms note is optional, and a source stage 4 registers records the
    validation run that found it ready and when that run completed, as the evidence instead."""
    validation = await _validation_run(intel_pool)
    query = _new(
        kind="search_query",
        url=None,
        query="Bumble privacy news",
        option="Bumble",
        tags=(),
        note="a",  # one character is a note
        validation=validation,
    )
    registered = await PostgresQuestionStore(intel_pool).register_and_queue_suggestion(
        REQUEST, [_new(note=None, validation=validation), query], operator="ann"
    )
    assert len(registered.registered) == 2
    sources = {
        s.kind: s
        for s in await PostgresQuestionStore(intel_pool).sources_by_ids(registered.registered)
    }
    policy, search = sources["policy_page"], sources["search_query"]
    assert (policy.terms_note, search.terms_note) == (None, "a")
    for source in (policy, search):
        assert (source.validation_run_id, source.validated_at) == validation


async def test_a_reused_source_keeps_its_own_note_and_evidence(
    intel_pool: AsyncConnectionPool,
) -> None:
    """A source created on the Sources screen carries no evidence (never validated). Chosen again
    at stage 4 it is reused, and neither its note nor its null evidence is rewritten."""
    registry = PostgresIntelStore(intel_pool)
    existing = await registry.create_source(
        kind="policy_page",
        source_url="https://p.example/terms",
        query_text=None,
        tags=("instagram",),
        check_every_hours=24,
        terms_note="the operator's own note",
        operator="alice",
    )
    assert (existing.validation_run_id, existing.validated_at) == (None, None)
    validation = await _validation_run(intel_pool)
    registered = await PostgresQuestionStore(intel_pool).register_and_queue_suggestion(
        REQUEST, [_new(note=None, validation=validation)], operator="ann"
    )
    assert registered.reused == (existing.source_id,) and registered.registered == ()
    after = await registry.get_source(existing.source_id)
    assert after is not None
    assert (after.terms_note, after.validation_run_id, after.validated_at) == (
        "the operator's own note",
        None,
        None,
    )


async def test_the_same_source_twice_in_one_request_registers_once(
    intel_pool: AsyncConnectionPool,
) -> None:
    """The route merges duplicates (merge_by_identity); the store stays safe without it."""
    registered = await PostgresQuestionStore(intel_pool).register_and_queue_suggestion(
        REQUEST, [_new(option="Instagram"), _new(option="Bumble")], operator="ann"
    )
    assert len(registered.registered) == 1 and registered.reused == ()
    assert await _rows(intel_pool, "SELECT count(*) FROM intel_sources") == [(1,)]


async def test_validations_read_only_completed_validation_runs(
    intel_pool: AsyncConnectionPool,
) -> None:
    def result(url: str, status: str) -> dict[str, Any]:
        candidate = {
            "option": "Instagram",
            "kind": "policy_page",
            "source_url": url,
            "query_text": None,
        }
        return {"candidate": candidate, "status": status, "reason": None}

    outcome = Jsonb(
        {
            "results": [
                result("https://p.example/terms?utm_source=x", "ready"),
                result("https://p.example/app", "blocked"),
            ]
        }
    )
    insert = (
        "INSERT INTO intel_runs (kind, status, requested_by, outcome, completed_at)"
        " VALUES (%s, %s, 'ann', %s, now()) RETURNING run_id"
    )
    ids = []
    async with intel_pool.connection() as conn:
        for kind, status in (
            ("source_validation", "completed"),
            ("source_validation", "queued"),
            ("source_proposal", "completed"),
        ):
            cur = await conn.execute(insert, (kind, status, outcome))
            row = await cur.fetchone()
            assert row is not None
            ids.append(row[0])
    found = await PostgresQuestionStore(intel_pool).validations(ids)
    assert set(found) == {ids[0]}
    # Keyed by kind and identity, so the echoed URL's tracking parameter does not matter.
    assert found[ids[0]].ready == frozenset(
        {candidate_key("policy_page", "https://p.example/terms", None)}
    )


async def test_proposed_identities_read_the_questions_stage_one_runs_since_the_cutoff(
    intel_pool: AsyncConnectionPool,
) -> None:
    insert = (
        "INSERT INTO intel_runs (kind, status, requested_by, request, outcome, completed_at)"
        " VALUES ('source_proposal', 'completed', 'ann', %s, %s,"
        " now() - make_interval(hours => %s))"
    )
    async with intel_pool.connection() as conn:
        for question_key, hours_ago, url in (
            ("platforms", 1, "https://p.example/recent"),
            ("dating", 1, "https://p.example/other-question"),
            ("platforms", 30, "https://p.example/too-old"),
        ):
            candidate = {
                "kind": "policy_page",
                "source_url": url,
                "query_text": None,
                "reason": "r",
            }
            outcome = {
                "options": [{"option": "o", "tags": [], "existing": [], "proposed": [candidate]}]
            }
            await conn.execute(
                insert, (Jsonb({"question_key": question_key}), Jsonb(outcome), hours_ago)
            )
    found = await PostgresQuestionStore(intel_pool).proposed_identities(
        "platforms", since=datetime.now(UTC) - timedelta(hours=24)
    )
    assert found == frozenset({identity("policy_page", "https://p.example/recent", None)})


# ── the weight suggestion ────────────────────────────────────────────────────

META: dict[str, Any] = {
    "against_scoring_version": "s2",
    "against_release_no": 2,
    "model_id": "claude-opus-5-5",
    "prompt_version": "suggest-v1",
}


async def _suggestion_run(pool: AsyncConnectionPool) -> UUID:
    ((run_id,),) = await _rows(
        pool,
        "INSERT INTO intel_runs (kind, status, requested_by)"
        " VALUES ('weight_suggestion', 'running', 'ann') RETURNING run_id",
    )
    return run_id


async def test_a_suggestion_is_born_delivered_and_supersedes_its_questions_older_one(
    intel_pool: AsyncConnectionPool,
) -> None:
    store = PostgresQuestionStore(intel_pool)
    sid = await seed_signal(intel_pool, tags=("instagram",))
    cited = OptionSuggestion("Instagram", 3, "r", (sid,), ("instagram",), None)
    empty = OptionSuggestion("Instagram", None, "", (), (), None)
    first, other, second = [await _suggestion_run(intel_pool) for _ in range(3)]
    one = await store.write_suggestion(first, question_key="platforms", options=[cited], **META)
    two = await store.write_suggestion(other, question_key="dating", options=[empty], **META)
    three = await store.write_suggestion(second, question_key="platforms", options=[empty], **META)
    assert one is not None and two is not None and three is not None
    assert three.superseded == one.written and two.superseded == ()
    statuses = dict(await _rows(intel_pool, "SELECT proposal_id, status FROM intel_proposals"))
    assert statuses == {
        one.written[0]: "superseded",
        two.written[0]: "delivered",
        three.written[0]: "delivered",
    }
    # "No evidence, operator's call" is an answer: a suggestion may link no signal.
    links = await _rows(intel_pool, "SELECT proposal_id, signal_id FROM intel_proposal_signals")
    assert links == [(one.written[0], sid)]
    # A reclaimed run never writes twice.
    again = await store.write_suggestion(second, question_key="platforms", options=[empty], **META)
    assert again is None
    assert await store.suggestion_written(second)
    assert not await store.suggestion_written(await _suggestion_run(intel_pool))
    audited = "SELECT count(*) FROM audit_log WHERE action = 'intel.weight_suggestion_written'"
    assert await _rows(intel_pool, audited) == [(3,)]


async def test_suggestion_candidates_read_the_four_classes_active_and_inside_the_window(
    intel_pool: AsyncConnectionPool,
) -> None:
    chosen = await PostgresIntelStore(intel_pool).create_source(
        kind="policy_page",
        source_url="https://p.example/chosen",
        query_text=None,
        tags=(),
        check_every_hours=24,
        terms_note="automated access permitted",
        operator="alice",
    )
    from_source = await seed_signal(intel_pool, category="law")
    async with intel_pool.connection() as conn:
        await conn.execute(
            "UPDATE intel_documents SET source_id = %s WHERE document_id ="
            " (SELECT document_id FROM intel_signals WHERE signal_id = %s)",
            (chosen.source_id, from_source),
        )
    tagged = await seed_signal(intel_pool, tags=("instagram",), category="law")
    subject = await seed_signal(intel_pool, subjects=("Bumble",), category="law")
    research = await seed_signal(intel_pool, category="research")
    now = datetime.now(UTC)
    await seed_signal(intel_pool, tags=("instagram",), created_at=now - timedelta(days=400))
    gone = await seed_signal(intel_pool, tags=("instagram",))
    await PostgresEvidenceStore(intel_pool).retract_signal(gone, operator="ann", reason="wrong")
    found = await PostgresQuestionStore(intel_pool).suggestion_candidates(
        source_ids=[chosen.source_id],
        tags=["instagram"],
        since=now - timedelta(days=365),
        limit=50,
    )
    assert [s.signal_id for s in found.from_sources] == [from_source]
    assert [s.signal_id for s in found.by_tag] == [tagged]
    assert [s.signal_id for s in found.with_subjects] == [subject]
    assert [s.signal_id for s in found.by_category] == [research]


async def test_marking_a_source_due_brings_its_first_check_forward_never_back(
    intel_pool: AsyncConnectionPool,
) -> None:
    store = PostgresQuestionStore(intel_pool)
    registered = await store.register_and_queue_suggestion(REQUEST, [_new()], operator="ann")
    (source_id,) = registered.registered
    now = datetime.now(UTC)
    await store.mark_sources_due([source_id], now=now)
    await store.mark_sources_due([source_id], now=now + timedelta(hours=1))
    query = "SELECT next_check_at FROM intel_sources WHERE source_id = %s"
    assert await _rows(intel_pool, query, source_id) == [(now,)]


async def test_a_suggestion_reads_back_per_option_by_run_and_by_proposal(
    intel_pool: AsyncConnectionPool,
) -> None:
    store = PostgresQuestionStore(intel_pool)
    listed = await seed_signal(intel_pool, tags=("instagram",))
    web = await seed_signal(intel_pool, trust="web", publisher="a.example")
    run_id = await _suggestion_run(intel_pool)
    written = await store.write_suggestion(
        run_id,
        question_key="platforms",
        options=[
            OptionSuggestion("Instagram", 3, "r", (listed,), ("instagram",), None),
            OptionSuggestion("Bumble", None, "", (web,), (), None),
            OptionSuggestion("Hinge", None, "", (), (), None),
        ],
        **META,
    )
    assert written is not None
    (proposal_id,) = written.written
    found = await store.suggestion_of_run(run_id)
    assert found is not None and found[0] == proposal_id
    assert [(o["option"], o["deduction"], o["corroborated"], o["why_not"]) for o in found[1]] == [
        ("Instagram", 3, True, None),
        ("Bumble", None, False, "uncorroborated"),
        ("Hinge", None, False, "no_evidence"),
    ]
    assert await store.options_of_suggestion(proposal_id) == found[1]
    assert await store.suggestion_of_run(await _suggestion_run(intel_pool)) is None
    # The per-option read is computed now, not stored: a later retraction shows at once.
    await PostgresEvidenceStore(intel_pool).retract_signal(listed, operator="ann", reason="wrong")
    instagram = (await store.options_of_suggestion(proposal_id))[0]
    assert (instagram["corroborated"], instagram["why_not"]) == (False, "evidence_retracted")
