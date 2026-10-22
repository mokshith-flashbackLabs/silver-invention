"""The lesson_draft run, the lesson store and the lesson routes (spec
2026-10-15-intel-lessons-and-evidence §5). The run and the store run against Postgres; the routes
run over fake stores, as the other admin intel routes do."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from imageshield.http.app import create_app
from imageshield.intel.lesson_store import PostgresLessonStore
from imageshield.intel.proposal_models import DecisionRefused
from imageshield.intel.schemas import (
    ProposalLessonsOutput,
    ProposedEventLesson,
    ProposedLesson,
    ProposedLessonSection,
)
from tests.conftest import ADMIN_SERVICE_TOKEN, SERVICE_TOKEN, make_config
from tests.intel_fakes import (
    FakeFetcher,
    FakeModel,
    make_deps,
    run_once,
    seed_signal,
    seed_threat_event,
    seed_threat_proposal,
    settle_runs,
)

ADMIN = {"X-Service-Token": SERVICE_TOKEN, "X-Admin-Service-Token": ADMIN_SERVICE_TOKEN}


def _lesson() -> ProposedLesson:
    return ProposedLesson(
        title="Spot a fake sponsorship offer",
        intro="A fake brand deal leads to a sign-in page that steals the account.",
        sections=[
            ProposedLessonSection(kind="what_to_do", items=["Sign in only from the app itself."])
        ],
    )


async def _live_event(pool: AsyncConnectionPool) -> UUID:
    signals = [
        await seed_signal(pool, tags=("instagram",), publisher="a.example"),
        await seed_signal(pool, tags=("instagram",), publisher="b.example"),
    ]
    proposal = await seed_threat_proposal(
        pool, signal_ids=signals, status="applied", decided={"seeded": True}
    )
    event = await seed_threat_event(pool, proposal_id=proposal)
    # seed_signal queued an adhoc run per signal: finish them, so the next claim is the draft run.
    await settle_runs(pool)
    return event


def _answer_every_event(payload: dict[str, Any]) -> ProposalLessonsOutput:
    return ProposalLessonsOutput(
        event_lessons=[
            ProposedEventLesson(event=shown["key"], lesson=_lesson()) for shown in payload["events"]
        ]
    )


# ── the run ──────────────────────────────────────────────────────────────────


async def test_a_draft_run_writes_one_lesson_proposal_per_event(
    intel_pool: AsyncConnectionPool,
) -> None:
    event = await _live_event(intel_pool)
    model = FakeModel(lessons_with=_answer_every_event)
    await PostgresLessonStore(intel_pool).queue_draft(event_ids=(), operator="Op")
    result = await run_once(intel_pool, make_deps(intel_pool, FakeFetcher({}), model))
    assert result.status == "completed", result
    async with intel_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT kind, status, target FROM intel_proposals WHERE kind = 'lesson'"
        )
        rows = await cur.fetchall()
    assert rows == [("lesson", "pending", {"event_id": str(event)})]


async def test_an_event_with_a_pending_lesson_is_not_drafted_again(
    intel_pool: AsyncConnectionPool,
) -> None:
    await _live_event(intel_pool)
    model = FakeModel(lessons_with=_answer_every_event)
    store = PostgresLessonStore(intel_pool)
    await store.queue_draft(event_ids=(), operator="Op")
    await run_once(intel_pool, make_deps(intel_pool, FakeFetcher({}), model))
    await store.queue_draft(event_ids=(), operator="Op")
    result = await run_once(intel_pool, make_deps(intel_pool, FakeFetcher({}), model))
    assert result.outcome.get("lesson_draft_nothing_to_draft") == 1
    assert len(model.lesson_users) == 1


async def test_a_draft_repeating_a_live_lessons_title_is_dropped_and_shown_the_library(
    intel_pool: AsyncConnectionPool,
) -> None:
    """2026-10-22: the run shows the model every existing lesson, and code drops a draft whose
    title one already has: dev's two near-identical nudify events each got a lesson."""
    event = await _live_event(intel_pool)
    other = await _live_event(intel_pool)
    async with intel_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO intel_lessons (proposal_id, event_id, kind, topic, title, intro,"
            " sections, approved_by) SELECT proposal_id, NULL, 'general', 'fake_offers', %s,"
            " 'i', %s, 'op' FROM threat_events WHERE event_id = %s",
            (
                "Spot a fake sponsorship offer",
                Jsonb([{"kind": "what_to_do", "items": ["a"]}]),
                other,
            ),
        )
    model = FakeModel(lessons_with=_answer_every_event)
    await PostgresLessonStore(intel_pool).queue_draft(event_ids=(event,), operator="Op")
    result = await run_once(intel_pool, make_deps(intel_pool, FakeFetcher({}), model))
    assert result.outcome.get("lesson_dropped_repeat") == 1
    assert '"existing_lessons"' in model.lesson_users[0]
    assert "Spot a fake sponsorship offer" in model.lesson_users[0]
    async with intel_pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM intel_proposals WHERE kind = 'lesson'")
        row = await cur.fetchone()
    assert row is not None and row[0] == 0


async def test_a_hand_created_threat_is_never_drafted(intel_pool: AsyncConnectionPool) -> None:
    await seed_threat_event(intel_pool)  # no proposal, so no evidence
    model = FakeModel(lessons_with=_answer_every_event)
    await PostgresLessonStore(intel_pool).queue_draft(event_ids=(), operator="Op")
    result = await run_once(intel_pool, make_deps(intel_pool, FakeFetcher({}), model))
    assert result.outcome.get("lesson_draft_nothing_to_draft") == 1
    assert model.lesson_users == []


# ── the store ────────────────────────────────────────────────────────────────


async def _seed_lesson(pool: AsyncConnectionPool, event: UUID) -> UUID:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO intel_lessons (proposal_id, event_id, kind, title, intro, sections,"
            " approved_by) SELECT proposal_id, event_id, 'threat', 't', 'i', %s, 'op'"
            " FROM threat_events WHERE event_id = %s RETURNING lesson_id",
            (Jsonb([{"kind": "what_to_do", "items": ["a"]}]), event),
        )
        row = await cur.fetchone()
    assert row is not None
    lesson_id: UUID = row[0]
    return lesson_id


async def _live_count(pool: AsyncConnectionPool) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM svc.v_lessons")
        row = await cur.fetchone()
    assert row is not None
    count: int = row[0]
    return count


async def test_retiring_a_lesson_takes_it_off_the_view_and_the_live_list(
    intel_pool: AsyncConnectionPool,
) -> None:
    lesson = await _seed_lesson(intel_pool, await _live_event(intel_pool))
    store = PostgresLessonStore(intel_pool)
    assert await _live_count(intel_pool) == 1
    assert await store.retire(lesson, operator="Op", reason="out of date") == "retired"
    assert await store.retire(lesson, operator="Op", reason="again") == "already_retired"
    assert await store.retire(uuid4(), operator="Op", reason="x") == "not_found"
    assert await _live_count(intel_pool) == 0
    assert await store.list_lessons(live=True, limit=10, cursor=None) == []
    [retired] = await store.list_lessons(live=False, limit=10, cursor=None)
    assert retired["lesson_id"] == lesson and retired["retire_reason"] == "out of date"


async def test_an_ended_event_takes_its_lesson_off_the_view(
    intel_pool: AsyncConnectionPool,
) -> None:
    event = await _live_event(intel_pool)
    await _seed_lesson(intel_pool, event)
    assert await _live_count(intel_pool) == 1
    async with intel_pool.connection() as conn:
        await conn.execute(
            "UPDATE threat_events SET starts_at = now() - interval '2 days',"
            " expires_at = now() - interval '1 minute' WHERE event_id = %s",
            (event,),
        )
    assert await _live_count(intel_pool) == 0


# ── the routes ───────────────────────────────────────────────────────────────


class FakeLessonStore:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.queued: list[dict[str, Any]] = []
        self.retired: dict[UUID, Literal["retired", "already_retired", "not_found"]] = {}
        # 2026-10-22: an operator may hide a published quote.
        self.evidence: dict[UUID, dict[str, Any]] = {}
        self.excerpts: dict[UUID, dict[str, Any] | None] = {}
        self.hides: list[tuple[UUID, str, str]] = []

    async def queue_draft(self, *, event_ids: Any, operator: str) -> UUID:
        self.queued.append({"event_ids": list(event_ids), "operator": operator})
        return uuid4()

    async def list_lessons(self, *, live: bool, limit: int, cursor: Any) -> list[dict[str, Any]]:
        return [r for r in self.rows if (r["retired_at"] is None) == live][:limit]

    async def retire(
        self, lesson_id: UUID, *, operator: str, reason: str
    ) -> Literal["retired", "already_retired", "not_found"]:
        return self.retired.get(lesson_id, "not_found")

    async def published_evidence(self, owner_kind: str, owner_id: UUID) -> dict[str, Any] | None:
        return self.evidence.get(owner_id)

    async def hide_excerpt(
        self, excerpt_id: UUID, *, operator: str, reason: str
    ) -> dict[str, Any] | None:
        if excerpt_id not in self.excerpts:
            return None
        self.hides.append((excerpt_id, operator, reason))
        return {"excerpt_id": excerpt_id, "hidden": {"by": operator, "reason": reason}}

    async def unhide_excerpt(
        self, excerpt_id: UUID, *, operator: str, reason: str
    ) -> dict[str, Any] | None:
        if excerpt_id not in self.excerpts:
            return None
        return {"excerpt_id": excerpt_id, "hidden": None}


class RefusingDecisions:
    async def decide(self, proposal_id: UUID, **_: Any) -> Any:
        raise DecisionRefused(
            "lesson_invalid",
            "This lesson breaks a rule: see problems.",
            problems=({"field": "intro", "rule": "reassurance"},),
        )


def _client() -> tuple[TestClient, FakeLessonStore]:
    app = create_app(config=make_config())
    store = FakeLessonStore()
    app.state.lesson_store = store
    app.state.decision_store = RefusingDecisions()
    return TestClient(app), store


def test_lesson_drafts_queue_a_run_and_refuse_more_than_twenty_ids() -> None:
    client, store = _client()
    ok = client.post(
        "/v1/admin/intel/lesson-drafts", json={"event_ids": [], "operator": "Op"}, headers=ADMIN
    )
    assert ok.status_code == 202, ok.text
    assert UUID(ok.json()["run_id"]) and store.queued == [{"event_ids": [], "operator": "Op"}]
    too_many = [str(uuid4()) for _ in range(21)]
    refused = client.post(
        "/v1/admin/intel/lesson-drafts",
        json={"event_ids": too_many, "operator": "Op"},
        headers=ADMIN,
    )
    assert refused.status_code == 422


def test_the_library_lists_live_or_retired() -> None:
    client, store = _client()
    now = datetime.now(UTC)
    store.rows = [
        {"lesson_id": uuid4(), "approved_at": now, "retired_at": None, "title": "live"},
        {"lesson_id": uuid4(), "approved_at": now, "retired_at": now, "title": "gone"},
    ]
    live = client.get("/v1/admin/intel/lessons", headers=ADMIN)
    assert live.status_code == 200 and [r["title"] for r in live.json()["lessons"]] == ["live"]
    gone = client.get("/v1/admin/intel/lessons?status=retired", headers=ADMIN)
    assert [r["title"] for r in gone.json()["lessons"]] == ["gone"]


def test_retiring_an_unknown_lesson_is_404_and_a_retired_one_answers_retired() -> None:
    client, store = _client()
    body = {"reason": "out of date", "operator": "Op"}
    unknown = client.post(f"/v1/admin/intel/lessons/{uuid4()}/retire", json=body, headers=ADMIN)
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "lesson_not_found"
    known = uuid4()
    store.retired[known] = "already_retired"
    again = client.post(f"/v1/admin/intel/lessons/{known}/retire", json=body, headers=ADMIN)
    assert again.status_code == 200 and again.json()["status"] == "retired"


def test_a_lesson_refusal_names_its_problems() -> None:
    client, _ = _client()
    r = client.post(
        f"/v1/admin/intel/proposals/{uuid4()}/decision",
        json={"decision": "approved", "reason": "fine", "operator": "Op"},
        headers=ADMIN,
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "lesson_invalid"
    assert r.json()["error"]["problems"] == [{"field": "intro", "rule": "reassurance"}]


# -- 2026-10-22: hiding a published quote --


def test_the_evidence_read_answers_the_owners_quotes_or_evidence_not_found() -> None:
    client, store = _client()
    owner = uuid4()
    store.evidence = {owner: {"owner_kind": "event", "owner_id": str(owner), "excerpts": []}}
    ok = client.get(
        "/v1/admin/intel/evidence",
        params={"owner_kind": "event", "owner_id": str(owner)},
        headers=ADMIN,
    )
    assert ok.status_code == 200 and ok.json()["owner_id"] == str(owner)
    missing = client.get(
        "/v1/admin/intel/evidence",
        params={"owner_kind": "lesson", "owner_id": str(uuid4())},
        headers=ADMIN,
    )
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "evidence_not_found"
    wrong = client.get(
        "/v1/admin/intel/evidence",
        params={"owner_kind": "hit", "owner_id": str(owner)},
        headers=ADMIN,
    )
    assert wrong.status_code == 422


def test_hiding_and_unhiding_a_quote_name_the_operator_and_refuse_an_unknown_one() -> None:
    client, store = _client()
    excerpt = uuid4()
    store.excerpts = {excerpt: None}
    body = {"reason": "names a private person", "operator": "Op"}
    hidden = client.post(f"/v1/admin/intel/excerpts/{excerpt}/hide", json=body, headers=ADMIN)
    assert hidden.status_code == 200, hidden.text
    assert hidden.json()["hidden"] == {"by": "Op", "reason": "names a private person"}
    assert store.hides == [(excerpt, "Op", "names a private person")]
    shown = client.post(f"/v1/admin/intel/excerpts/{excerpt}/unhide", json=body, headers=ADMIN)
    assert shown.status_code == 200 and shown.json()["hidden"] is None
    unknown = client.post(f"/v1/admin/intel/excerpts/{uuid4()}/hide", json=body, headers=ADMIN)
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "excerpt_not_found"
    short = client.post(
        f"/v1/admin/intel/excerpts/{excerpt}/hide",
        json={"reason": "x", "operator": "Op"},
        headers=ADMIN,
    )
    assert short.status_code == 422
