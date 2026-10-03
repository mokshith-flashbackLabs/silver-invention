"""The run log (spec 2026-10-03 §3): the recorder's caps and failure handling, the vocabulary's
words, migration 0046 (reversible, linted, and usable by the APP role rather than only the
superuser), and the store's two reads -- the filtered runs list and a run's events.

The model seam's streaming translation is tested in ``test_intel_model.py``, ``metered()``'s
rows in ``test_intel_metering.py``, the worker's ``run_started``/``run_finished`` in
``test_intel_worker.py`` and the routes in ``test_admin_intel_routes.py``.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, get_args
from uuid import UUID, uuid4

import psycopg
import pytest
import structlog
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from imageshield.db.connection import make_async_pool
from imageshield.http.models import IntelRunKind, IntelRunStatus
from imageshield.intel.model import ModelUnavailable
from imageshield.intel.models import Run
from imageshield.intel.run_log import (
    MAX_DETAIL_BYTES,
    MAX_EVENTS_PER_RUN,
    MAX_TEXT_CHARS,
    MAX_THINKING_CHARS,
    RUN_EVENT_KINDS,
    TRUNCATED_TEXT,
    RunLog,
    capped_detail,
    current_run_log,
    duration_words,
    model_call_failed_event,
    model_call_skipped_event,
    money_words,
    note_continuing,
    note_failed,
    note_skipped,
    run_finished_event,
    run_started_event,
    search_results_event,
)
from imageshield.intel.store import PostgresIntelStore
from tests.db import run_migrate
from tests.intel_fakes import MemoryRunEvents

MIGRATIONS = Path(__file__).parent.parent / "migrations"
_0046_INDEXES = ("intel_runs_kind_created_idx", "intel_runs_question_idx")


# ── the recorder (no database) ──────────────────────────────────────────────────────────────


async def test_the_last_row_is_truncated_and_nothing_follows() -> None:
    store = MemoryRunEvents()
    run_log = RunLog(uuid4(), store)
    seqs = [await run_log.append("search", f"Searched: q{n}", {"query": "q"}) for n in range(310)]
    assert seqs[: MAX_EVENTS_PER_RUN - 1] == list(range(1, MAX_EVENTS_PER_RUN))
    assert set(seqs[MAX_EVENTS_PER_RUN - 1 :]) == {None}
    events = store.events(run_log.run_id)
    assert len(events) == MAX_EVENTS_PER_RUN
    assert {e["kind"] for e in events[:-1]} == {"search"}
    assert events[-1] == {
        "seq": MAX_EVENTS_PER_RUN,
        "kind": "truncated",
        "text": TRUNCATED_TEXT,
        "detail": {},
    }
    # A row already written may still be finalised: an update adds no row.
    await run_log.update(1, "Searched: final")
    assert store.events(run_log.run_id)[0]["text"] == "Searched: final"
    assert len(store.events(run_log.run_id)) == MAX_EVENTS_PER_RUN


async def test_seq_continues_from_the_runs_last_row() -> None:
    """A reclaimed run appends after what the crashed attempt wrote; its cap counts both."""
    store = MemoryRunEvents()
    run_id = uuid4()
    first = RunLog(run_id, store)
    for _ in range(MAX_EVENTS_PER_RUN - 3):
        await first.append("search", "Searched: q")
    second = RunLog(run_id, store)
    assert await second.append("run_started", "Started: again") == MAX_EVENTS_PER_RUN - 2
    assert await second.append("search", "Searched: q") == MAX_EVENTS_PER_RUN - 1
    assert await second.append("search", "Searched: q") is None
    assert store.kinds(run_id)[-1] == "truncated"


async def test_text_is_capped_per_kind() -> None:
    store = MemoryRunEvents()
    run_log = RunLog(uuid4(), store)
    await run_log.append("search", "x" * 5000)
    seq = await run_log.append("thinking", "y" * 5000)
    assert seq is not None
    await run_log.update(seq, "z" * 9000)
    search, thinking = store.events(run_log.run_id)
    assert len(search["text"]) == MAX_TEXT_CHARS and search["text"].endswith("…")
    assert len(thinking["text"]) == MAX_THINKING_CHARS and thinking["text"].startswith("z")


def test_oversize_detail_drops_results_first_then_is_replaced() -> None:
    results = [{"title": "t" * 300, "url": f"https://r{n}.example/"} for n in range(40)]
    trimmed = capped_detail({"count": 40, "results": results, "error_code": None})
    assert trimmed == {"count": 40, "error_code": None}
    assert capped_detail({"query": "q" * (MAX_DETAIL_BYTES + 1)}) == {"oversize": True}
    small = {"cost_usd": Decimal("0.23"), "run": UUID(int=1)}
    assert capped_detail(small) == {"cost_usd": "0.23", "run": str(UUID(int=1))}


async def test_a_write_that_raises_is_a_warning_naming_only_the_run_and_the_kind() -> None:
    store = MemoryRunEvents(fail=True)
    run_log = RunLog(uuid4(), store)
    with structlog.testing.capture_logs() as logs:
        assert await run_log.append("search", "Searched: secret query", {"query": "q"}) is None
        await run_log.update(1, "thinking text")
    assert [e["event"] for e in logs] == ["intel.run_log_write_failed"] * 2
    assert logs[0] == {
        "event": "intel.run_log_write_failed",
        "log_level": "warning",
        "run_id": str(run_log.run_id),
        "kind": "search",
    }
    # The database comes back: the next write re-reads the run's last seq and carries on.
    store.fail = False
    assert await run_log.append("search", "Searched: q") == 1


async def test_with_no_log_bound_every_note_is_a_no_op() -> None:
    assert current_run_log.get() is None
    await note_skipped("breaker_open")
    await note_continuing(1)
    await note_failed(ModelUnavailable("timeout", "APITimeoutError"))


# ── the words (spec §3.2) ───────────────────────────────────────────────────────────────────


def test_durations_and_money_read_like_the_spec() -> None:
    assert duration_words(400) == "0.4s"
    assert duration_words(45_000) == "45s"
    assert duration_words(192_000) == "3m 12s"
    assert duration_words(3_840_000) == "1h 4m"
    assert money_words(Decimal("0.2345")) == "$0.23"
    assert money_words(Decimal("0.0042")) == "$0.0042"
    assert money_words(Decimal("0")) == "$0.00"


def _run(kind: str, request: dict[str, Any], attempts: int = 1) -> Run:
    return Run(
        run_id=uuid4(),
        kind=kind,
        source_id=None,
        request=request,
        status="running",
        attempts=attempts,
        requested_by="ann",
        outcome={},
        error_code=None,
        created_at=datetime.now(UTC),
        completed_at=None,
    )


def test_run_started_names_the_question_and_a_reclaimed_attempt() -> None:
    text, detail = run_started_event(_run("source_proposal", {"question_key": "platforms"}))
    assert text == "Started: find sources for 'platforms'"
    assert detail == {"run_kind": "source_proposal", "attempt": 1}
    text, detail = run_started_event(_run("weight_suggestion", {"question_key": "dating"}, 2))
    assert text == "Started: suggest points for 'dating' (attempt 2)"
    assert detail == {"run_kind": "weight_suggestion", "attempt": 2}
    text, _ = run_started_event(_run("source_validation", {"candidates": [{}, {}]}))
    assert text == "Started: check 2 candidate sources for a question"
    for kind in get_args(IntelRunKind):
        text, _ = run_started_event(_run(kind, {}))
        assert text.startswith("Started: ") and "{" not in text, kind


def test_run_finished_reads_per_status() -> None:
    text, detail = run_finished_event("completed", None, "0.2300", 220_000)
    assert text == "Finished in 3m 40s · $0.23"
    assert detail == {
        "status": "completed",
        "error_code": None,
        "cost_usd": "0.2300",
        "duration_ms": 220_000,
    }
    assert run_finished_event("failed", "error", "0", 5)[0] == "Failed: error"
    assert run_finished_event("refused", "budget_exceeded", "0", 5)[0] == (
        "Refused: budget_exceeded"
    )


def test_failures_and_skips_read_like_the_spec() -> None:
    denied = ModelUnavailable(
        "error",
        "PermissionDeniedError:403",
        message="not authorized to perform sts:GetWebIdentityToken",
        error_type="PermissionDeniedError",
        http_status=403,
    )
    text, detail = model_call_failed_event(denied)
    assert text == (
        "Claude call failed: permission denied (403) — not authorized to perform"
        " sts:GetWebIdentityToken"
    )
    assert detail == {
        "status": "error",
        "error_type": "PermissionDeniedError",
        "http_status": 403,
        "message": "not authorized to perform sts:GetWebIdentityToken",
    }
    text, detail = model_call_failed_event(ModelUnavailable("timeout", "APITimeoutError"))
    assert text == "Claude call failed: timed out"
    assert detail["error_type"] == "APITimeoutError" and detail["http_status"] is None
    assert model_call_skipped_event("provider_disabled") == (
        "Not sent: the provider is switched off",
        {"reason": "provider_disabled"},
    )
    assert model_call_skipped_event("breaker_open")[0] == "Not sent: the breaker is open"
    assert model_call_skipped_event("budget_exceeded")[0] == "Not sent: today's budget is spent"
    assert model_call_skipped_event("budget_unset")[0] == "Not sent: no daily budget is set"


def test_an_empty_result_list_and_an_unknown_error_shape_still_read() -> None:
    assert search_results_event([]) == (
        "No results",
        {"count": 0, "results": [], "error_code": None},
    )
    assert search_results_event(None)[1]["error_code"] == "unknown"


# ── migration 0046 ──────────────────────────────────────────────────────────────────────────


@pytest.fixture
def migrated_db(throwaway_db: str) -> str:
    assert run_migrate(throwaway_db, "down", "--all").returncode == 0
    up = run_migrate(throwaway_db, "up")
    assert up.returncode == 0, up.stderr
    return throwaway_db


def _steps_through(version: str) -> str:
    ups = sorted(p.name for p in MIGRATIONS.glob("*.up.sql"))
    return str(sum(1 for name in ups if name >= version))


def _check_values(conn: psycopg.Connection[Any], table: str, needle: str) -> set[str]:
    """The quoted values of the one CHECK on ``table`` whose definition mentions ``needle``."""
    rows = conn.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
        " WHERE conrelid = %s::regclass AND contype = 'c'",
        (table,),
    ).fetchall()
    (definition,) = [row[0] for row in rows if needle in row[0]]
    return set(re.findall(r"'([a-z_]+)'::text", definition))


def test_the_closed_vocabularies_match_the_database(migrated_db: str) -> None:
    """The event kinds are 0046's CHECK; the runs list's filter values are intel_runs' own
    kind and status CHECKs, so a run kind added later fails here rather than 422ing."""
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        assert _check_values(conn, "intel_run_events", "run_started") == set(RUN_EVENT_KINDS)
        assert _check_values(conn, "intel_runs", "adhoc_url") == set(get_args(IntelRunKind))
        assert _check_values(conn, "intel_runs", "refused") == set(get_args(IntelRunStatus))


def _present(conn: psycopg.Connection[Any]) -> set[str]:
    rows = conn.execute(
        "SELECT relname FROM pg_class WHERE relname = ANY(%s)",
        (["intel_run_events", *_0046_INDEXES],),
    ).fetchall()
    return {row[0] for row in rows}


def test_0046_is_reversible(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        assert _present(conn) == {"intel_run_events", *_0046_INDEXES}
        (run_id,) = conn.execute(  # type: ignore[misc]
            "INSERT INTO intel_runs (kind, request, requested_by)"
            " VALUES ('adhoc_url', '{}'::jsonb, 'ann') RETURNING run_id"
        ).fetchone()
        conn.execute(
            "INSERT INTO intel_run_events (run_id, seq, kind, text) VALUES (%s, 1, 'run_started',"
            " 'Started')",
            (run_id,),
        )
    down = run_migrate(migrated_db, "down", "--steps", _steps_through("0046_"))
    assert down.returncode == 0, down.stderr
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        assert _present(conn) == set()
        assert conn.execute("SELECT count(*) FROM intel_runs").fetchone() == (1,)
    up = run_migrate(migrated_db, "up")
    assert up.returncode == 0, up.stderr
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        assert _present(conn) == {"intel_run_events", *_0046_INDEXES}


def test_the_app_role_writes_and_reads_the_log_and_never_deletes(migrated_db: str) -> None:
    """Under SET ROLE: a superuser run hides a missing grant (the 0035 trap). intel_rw is the
    role app_services holds for the worker AND the admin routes (0039)."""
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        conn.execute("SET ROLE intel_rw")
        (run_id,) = conn.execute(  # type: ignore[misc]
            "INSERT INTO intel_runs (kind, request, requested_by)"
            " VALUES ('adhoc_url', '{}'::jsonb, 'ann') RETURNING run_id"
        ).fetchone()
        conn.execute(
            "INSERT INTO intel_run_events (run_id, seq, kind, text, detail)"
            " VALUES (%s, 1, 'thinking', '', %s)",
            (run_id, Jsonb({"chars": 0})),
        )
        conn.execute(
            "UPDATE intel_run_events SET text = 'Weighing it up', updated_at = now()"
            " WHERE run_id = %s AND seq = 1",
            (run_id,),
        )
        assert conn.execute(
            "SELECT kind, text FROM intel_run_events WHERE run_id = %s", (run_id,)
        ).fetchone() == ("thinking", "Weighing it up")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM intel_run_events WHERE run_id = %s", (run_id,))
        conn.execute("RESET ROLE")


def test_0046_checks_hold_the_shape(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        (run_id,) = conn.execute(  # type: ignore[misc]
            "INSERT INTO intel_runs (kind, request, requested_by)"
            " VALUES ('adhoc_url', '{}'::jsonb, 'ann') RETURNING run_id"
        ).fetchone()
        insert = "INSERT INTO intel_run_events (run_id, seq, kind, text) VALUES (%s, %s, %s, %s)"
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(insert, (run_id, 1, "raw_transcript", "x"))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(insert, (run_id, 0, "run_started", "x"))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(insert, (run_id, 1, "thinking", "x" * 4001))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(insert, (uuid4(), 1, "run_started", "x"))
        conn.execute(insert, (run_id, 1, "run_started", "x"))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(insert, (run_id, 1, "run_finished", "y"))


def _plan(conn: psycopg.Connection[Any], query: str, params: dict[str, Any]) -> str:
    conn.execute("SET enable_seqscan = off")
    cur = psycopg.ClientCursor(conn)
    cur.execute("EXPLAIN " + query, params)
    plan = "\n".join(row[0] for row in cur.fetchall())
    conn.execute("RESET enable_seqscan")
    return plan


def test_the_runs_filters_can_use_their_indexes(migrated_db: str) -> None:
    """The store's own WHERE clause states the question index's predicate, so the planner can
    prove the partial index applies."""
    from imageshield.intel.store import RUN_COLUMNS

    with psycopg.connect(migrated_db, autocommit=True) as conn:
        by_question = _plan(
            conn,
            f"SELECT {RUN_COLUMNS} FROM intel_runs WHERE request ? 'question_key'"
            " AND request ->> 'question_key' = %(question_key)s"
            " ORDER BY created_at DESC, run_id DESC LIMIT %(limit)s",
            {"question_key": "platforms", "limit": 1},
        )
        assert "intel_runs_question_idx" in by_question, by_question
        by_kind = _plan(
            conn,
            f"SELECT {RUN_COLUMNS} FROM intel_runs WHERE kind = ANY(%(kinds)s)"
            " ORDER BY created_at DESC, run_id DESC LIMIT %(limit)s",
            {"kinds": ["source_proposal"], "limit": 1},
        )
        assert "intel_runs_kind_created_idx" in by_kind, by_kind


# ── the store (real Postgres) ───────────────────────────────────────────────────────────────


@pytest.fixture
async def pool(migrated_db: str) -> AsyncIterator[AsyncConnectionPool]:
    p = make_async_pool(migrated_db, min_size=1, max_size=2)
    await p.open()
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture
async def app_role_pool(migrated_db: str) -> AsyncIterator[AsyncConnectionPool]:
    """Sessions AS ``intel_rw``: the grant the deployed worker and admin routes run under."""
    sep = "&" if "?" in migrated_db else "?"
    p = make_async_pool(f"{migrated_db}{sep}options=-c%20role%3Dintel_rw", min_size=1, max_size=2)
    await p.open()
    try:
        yield p
    finally:
        await p.close()


async def _seed_run(
    pool: AsyncConnectionPool,
    kind: str,
    *,
    status: str = "completed",
    question_key: str | None = None,
    at: datetime | None = None,
) -> UUID:
    request: dict[str, Any] = {"question_key": question_key} if question_key else {}
    if kind == "adhoc_url":
        request = {"url": "https://n.example/a"}
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO intel_runs (kind, request, status, requested_by, created_at)"
            " VALUES (%s, %s, %s, 'ann', %s) RETURNING run_id",
            (kind, Jsonb(request), status, at or datetime.now(UTC)),
        )
        row = await cur.fetchone()
    assert row is not None
    return UUID(str(row[0]))


async def test_the_recorder_and_the_read_work_as_the_app_role(
    pool: AsyncConnectionPool, app_role_pool: AsyncConnectionPool
) -> None:
    run_id = await _seed_run(pool, "source_proposal", status="running", question_key="platforms")
    store = PostgresIntelStore(app_role_pool)
    run_log = RunLog(run_id, store)
    assert await run_log.append("run_started", "Started: find sources", {"attempt": 1}) == 1
    seq = await run_log.append("thinking", "", {"chars": 0})
    assert seq == 2
    await run_log.update(seq, "Weighing the options", {"chars": 20})
    read = await store.run_events(run_id)
    assert read is not None
    assert (read.run_id, read.kind, read.status) == (run_id, "source_proposal", "running")
    assert [(e.seq, e.kind, e.text) for e in read.events] == [
        (1, "run_started", "Started: find sources"),
        (2, "thinking", "Weighing the options"),
    ]
    assert read.events[1].detail == {"chars": 20}
    assert read.events[1].updated_at >= read.events[1].at
    filtered = await store.list_runs(
        cursor=None, limit=5, kinds=["source_proposal"], question_key="platforms"
    )
    assert [r.run_id for r in filtered] == [run_id]


async def test_a_runs_events_read_back_in_order_and_an_unknown_run_is_none(
    pool: AsyncConnectionPool,
) -> None:
    store = PostgresIntelStore(pool)
    run_id = await _seed_run(pool, "adhoc_url")
    empty = await store.run_events(run_id)
    assert empty is not None and empty.events == ()
    reclaimed = RunLog(run_id, store)
    for n in range(3):
        await reclaimed.append("search", f"Searched: {n}", {"query": str(n)})
    again = RunLog(run_id, store)  # a reclaim: continues from max(seq) + 1
    assert await again.append("run_started", "Started: again (attempt 2)") == 4
    read = await store.run_events(run_id)
    assert read is not None and [e.seq for e in read.events] == [1, 2, 3, 4]
    assert await store.run_events(uuid4()) is None


async def test_the_runs_list_filters_combine_and_page(pool: AsyncConnectionPool) -> None:
    store = PostgresIntelStore(pool)
    base = datetime.now(UTC) - timedelta(hours=1)
    p_old = await _seed_run(pool, "source_proposal", question_key="platforms", at=base)
    p_new = await _seed_run(
        pool, "source_proposal", question_key="platforms", at=base + timedelta(minutes=2)
    )
    p_failed = await _seed_run(
        pool,
        "source_proposal",
        status="failed",
        question_key="platforms",
        at=base + timedelta(minutes=3),
    )
    w_dating = await _seed_run(
        pool, "weight_suggestion", question_key="dating", at=base + timedelta(minutes=4)
    )
    adhoc = await _seed_run(pool, "adhoc_url", status="queued", at=base + timedelta(minutes=5))

    async def ids(**filters: Any) -> list[UUID]:
        return [r.run_id for r in await store.list_runs(cursor=None, limit=50, **filters)]

    assert await ids() == [adhoc, w_dating, p_failed, p_new, p_old]
    assert await ids(kinds=["source_proposal"]) == [p_failed, p_new, p_old]
    assert await ids(kinds=["source_proposal", "adhoc_url"]) == [adhoc, p_failed, p_new, p_old]
    assert await ids(statuses=["failed", "queued"]) == [adhoc, p_failed]
    assert await ids(question_key="dating") == [w_dating]
    assert await ids(
        kinds=["source_proposal"], statuses=["completed"], question_key="platforms"
    ) == [
        p_new,
        p_old,
    ]
    assert await ids(question_key="nobody_asked") == []
    assert await ids(kinds=["weight_suggestion"], question_key="platforms") == []
    # Keyset paging under a filter: the cursor composes with it.
    first = await store.list_runs(cursor=None, limit=2, kinds=["source_proposal"])
    assert [r.run_id for r in first] == [p_failed, p_new]
    rest = await store.list_runs(
        cursor=(first[-1].created_at, first[-1].run_id), limit=2, kinds=["source_proposal"]
    )
    assert [r.run_id for r in rest] == [p_old]
