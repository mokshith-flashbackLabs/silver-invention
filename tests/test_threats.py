"""``PostgresThreatStore`` against real Postgres (Task 14).

Same convention as ``tests/test_score_store.py``: direct SQL for fixtures, so
these tests are about the store's transaction shape (the matcher, the audit
trail, retraction) rather than re-deriving another module's write path.

The reversal test at the bottom is the one the brief calls out by name:
"event retraction restores exactly what it took." It reuses the enrolment /
seed seeding helpers from ``tests/test_score_store.py`` (Task 12) rather than
re-deriving them, since the whole point of that test is proving
``ThreatStore`` composes correctly with ``ScoreStore.recompute`` — not
re-testing enrolment or seeding.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from imageshield.db.connection import make_async_pool
from imageshield.score.engine import ScoreWeights
from imageshield.score.store import PostgresScoreStore
from imageshield.threats.store import (
    THREAT_CREATED_ACTION,
    THREAT_RETRACTED_ACTION,
    PostgresThreatStore,
)
from imageshield.types import UserRef
from tests.conftest import make_config
from tests.db import ensure_subject, run_migrate
from tests.test_score_store import _enrolment, _seed  # Task 12 seeding idioms


@pytest.fixture
def migrated_db(throwaway_db: str) -> str:
    down_result = run_migrate(throwaway_db, "down", "--all")
    assert down_result.returncode == 0, down_result.stderr
    up_result = run_migrate(throwaway_db, "up")
    assert up_result.returncode == 0, up_result.stderr
    return throwaway_db


@pytest.fixture
async def pool(migrated_db: str) -> AsyncIterator[AsyncConnectionPool]:
    p = make_async_pool(migrated_db, min_size=1, max_size=2)
    await p.open()
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture
def store(pool: AsyncConnectionPool) -> PostgresThreatStore:
    return PostgresThreatStore(pool)


@pytest.fixture
def score_store(pool: AsyncConnectionPool) -> PostgresScoreStore:
    cfg = make_config()
    return PostgresScoreStore(
        pool, weights=ScoreWeights.from_config(cfg), config_version=cfg.score_config_version
    )


def _user() -> UserRef:
    return UserRef(uuid4())


def _infringement_on_domain(
    conn: psycopg.Connection[Any], user_ref: UserRef, domain: str, *, url_alive: bool = True
) -> UUID:
    """A minimal infringement on a given ``source_domain`` — the matcher does
    not care about ``confirm_state``, only ``url_alive`` and the domain."""
    url_hash = uuid4().hex + uuid4().hex
    url = f"https://{domain}/{uuid4().hex}"
    conn.execute(
        "INSERT INTO content_urls (url_hash, url, source_domain, canonical_url)"
        " VALUES (%s, %s, %s, %s)",
        (url_hash, url, domain, url),
    )
    row = conn.execute(
        "INSERT INTO infringements (user_ref, url_hash, page_url, image_url, url_alive)"
        " VALUES (%s, %s, %s, %s, %s) RETURNING infringement_id",
        (user_ref, url_hash, url, f"{url}.jpg", url_alive),
    ).fetchone()
    assert row is not None
    infringement_id: UUID = row[0]
    return infringement_id


def _rows(migrated_db: str, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    with psycopg.connect(migrated_db, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        return list(conn.execute(sql, params).fetchall())


def _row(migrated_db: str, sql: str, params: tuple[Any, ...]) -> dict[str, Any]:
    with psycopg.connect(migrated_db, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        row = conn.execute(sql, params).fetchone()
    assert row is not None
    return row


_EXPIRES_SOON = datetime.now(UTC) + timedelta(days=7)


async def _create(
    store: PostgresThreatStore,
    *,
    domains: tuple[str, ...] = (),
    is_global: bool = False,
    penalty: Decimal = Decimal("5.00"),
    operator: str = "ops-team",
    title: str = "test threat",
) -> tuple[UUID, tuple[UserRef, ...]]:
    return await store.create_event(
        kind="leak",
        title=title,
        body="",
        severity=3,
        domains=domains,
        is_global=is_global,
        penalty=penalty,
        expires_at=_EXPIRES_SOON,
        decay_days=30,
        operator=operator,
    )


# ── domain matcher ────────────────────────────────────────────────────────


async def test_domain_match_hits_only_users_with_live_hits_on_the_domain(
    migrated_db: str, store: PostgresThreatStore, pool: AsyncConnectionPool
) -> None:
    matching_user = _user()
    other_domain_user = _user()
    await ensure_subject(pool, matching_user)
    await ensure_subject(pool, other_domain_user)
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        _infringement_on_domain(conn, matching_user, "evil.example")
        _infringement_on_domain(conn, other_domain_user, "unrelated.example")

    event_id, matched = await _create(store, domains=("evil.example",))

    assert set(matched) == {matching_user}
    match_row = _row(
        migrated_db,
        "SELECT matched_via, penalty_applied FROM threat_event_matches"
        " WHERE event_id = %s AND user_ref = %s",
        (event_id, matching_user),
    )
    assert match_row["matched_via"] == "evil.example"
    assert match_row["penalty_applied"] == Decimal("5.00")


async def test_a_dead_url_on_a_matching_domain_is_not_matched(
    migrated_db: str, store: PostgresThreatStore, pool: AsyncConnectionPool
) -> None:
    user_ref = _user()
    await ensure_subject(pool, user_ref)
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        _infringement_on_domain(conn, user_ref, "evil.example", url_alive=False)

    _event_id, matched = await _create(store, domains=("evil.example",))

    assert matched == ()


async def test_global_event_matches_every_subject(
    migrated_db: str, store: PostgresThreatStore, pool: AsyncConnectionPool
) -> None:
    users = [_user() for _ in range(3)]
    for u in users:
        await ensure_subject(pool, u)

    _event_id, matched = await _create(store, is_global=True, domains=())

    assert set(matched) == set(users)


async def test_global_event_does_not_double_count_a_domain_matched_user(
    migrated_db: str, store: PostgresThreatStore, pool: AsyncConnectionPool
) -> None:
    """A person matched via domain keeps their domain attribution — the
    global insert must not overwrite it, and the returned set must not
    contain them twice."""
    domain_user = _user()
    global_only_user = _user()
    await ensure_subject(pool, domain_user)
    await ensure_subject(pool, global_only_user)
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        _infringement_on_domain(conn, domain_user, "evil.example")

    event_id, matched = await _create(store, domains=("evil.example",), is_global=True)

    assert set(matched) == {domain_user, global_only_user}
    rows = _rows(
        migrated_db,
        "SELECT user_ref, matched_via FROM threat_event_matches WHERE event_id = %s",
        (event_id,),
    )
    by_user = {row["user_ref"]: row["matched_via"] for row in rows}
    assert by_user[domain_user] == "evil.example"
    assert by_user[global_only_user] == "global"


# ── audit trail ───────────────────────────────────────────────────────────


async def test_create_writes_one_audit_row_with_the_operator(
    migrated_db: str, store: PostgresThreatStore, pool: AsyncConnectionPool
) -> None:
    user_ref = _user()
    await ensure_subject(pool, user_ref)

    event_id, _matched = await _create(store, is_global=True, operator="alice")

    rows = _rows(
        migrated_db,
        "SELECT action, actor_type, resource_id, metadata FROM audit_log"
        " WHERE resource_id = %s",
        (event_id,),
    )
    assert len(rows) == 1
    assert rows[0]["action"] == THREAT_CREATED_ACTION
    assert rows[0]["actor_type"] == "operator"
    assert rows[0]["metadata"]["operator"] == "alice"


async def test_retract_writes_an_audit_row_with_the_operator_and_reason(
    migrated_db: str, store: PostgresThreatStore, pool: AsyncConnectionPool
) -> None:
    user_ref = _user()
    await ensure_subject(pool, user_ref)
    event_id, _matched = await _create(store, is_global=True)

    await store.retract_event(event_id, operator="bob", reason="false alarm, resolved")

    rows = _rows(
        migrated_db,
        "SELECT action, metadata FROM audit_log WHERE resource_id = %s AND action = %s",
        (event_id, THREAT_RETRACTED_ACTION),
    )
    assert len(rows) == 1
    assert rows[0]["metadata"]["operator"] == "bob"
    assert rows[0]["metadata"]["reason"] == "false alarm, resolved"


# ── retraction ────────────────────────────────────────────────────────────


async def test_retract_flips_status_once_and_a_second_retract_returns_none(
    migrated_db: str, store: PostgresThreatStore, pool: AsyncConnectionPool
) -> None:
    user_ref = _user()
    await ensure_subject(pool, user_ref)
    event_id, matched = await _create(store, is_global=True)

    first = await store.retract_event(event_id, operator="bob", reason="resolved")
    assert first is not None
    assert set(first) == set(matched)

    status_row = _row(
        migrated_db, "SELECT status FROM threat_events WHERE event_id = %s", (event_id,)
    )
    assert status_row["status"] == "retracted"

    second = await store.retract_event(event_id, operator="bob", reason="resolved again")
    assert second is None

    # Exactly one retraction audit row, not two.
    retract_rows = _rows(
        migrated_db,
        "SELECT 1 AS hit FROM audit_log WHERE resource_id = %s AND action = %s",
        (event_id, THREAT_RETRACTED_ACTION),
    )
    assert len(retract_rows) == 1


async def test_retract_of_an_unknown_event_returns_none(
    store: PostgresThreatStore,
) -> None:
    assert await store.retract_event(uuid4(), operator="bob", reason="n/a") is None


# ── list ──────────────────────────────────────────────────────────────────


async def test_list_events_returns_newest_first(
    migrated_db: str, store: PostgresThreatStore, pool: AsyncConnectionPool
) -> None:
    user_ref = _user()
    await ensure_subject(pool, user_ref)
    first_id, _ = await _create(store, is_global=True, title="first")
    second_id, _ = await _create(store, is_global=True, title="second")

    events = await store.list_events()

    ids = [e["event_id"] for e in events]
    assert ids.index(second_id) < ids.index(first_id)


# ── THE reversal test ────────────────────────────────────────────────────


async def test_event_retraction_restores_exactly_the_pre_event_score(
    migrated_db: str,
    store: PostgresThreatStore,
    score_store: PostgresScoreStore,
    pool: AsyncConnectionPool,
) -> None:
    """Build a well-set-up user (enrolled, fresh seed — the Task 12 seeding
    idioms), recompute a baseline, hit them with a matched threat event and
    recompute again (score must drop), then retract and recompute a third
    time. The engine reads only ``status = 'active'`` threats
    (``score/store.py``'s ``_THREATS_SQL``), so retraction must reproduce the
    baseline EXACTLY — not approximately — with no manual compensating
    journal entry anywhere in this module.
    """
    user_ref = _user()
    await ensure_subject(pool, user_ref)
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        _enrolment(conn, user_ref)
        _seed(conn, user_ref)
        _infringement_on_domain(conn, user_ref, "evil.example")

    baseline = await score_store.recompute(user_ref, cause_kind="test")
    assert baseline is not None

    event_id, matched = await _create(
        store, domains=("evil.example",), penalty=Decimal("9.00")
    )
    assert user_ref in matched

    after_event = await score_store.recompute(user_ref, cause_kind="threat_event")
    assert after_event is not None
    assert after_event.score < baseline.score

    reversed_ = await store.retract_event(event_id, operator="ops", reason="retracted")
    assert reversed_ is not None
    assert user_ref in reversed_

    after_retract = await score_store.recompute(user_ref, cause_kind="threat_retracted")
    assert after_retract is not None
    assert after_retract.score == baseline.score
    assert after_retract.components == baseline.components
