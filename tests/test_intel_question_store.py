"""The question store against real Postgres (spec §4.10): registration and its reuse rules, and the
validation and proposal reads stage 4 depends on."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from imageshield.intel.question_store import PostgresQuestionStore
from imageshield.intel.source_choice import NewSource, candidate_key, identity
from imageshield.intel.store import PostgresIntelStore
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
) -> NewSource:
    return NewSource(
        kind=kind,
        source_url=url,
        query_text=query,
        tags=tags,
        check_every_hours=168,
        terms_note="public terms page; automated reads allowed",
        origin=origin,
        question_key="platforms",
        option=option,
    )


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
