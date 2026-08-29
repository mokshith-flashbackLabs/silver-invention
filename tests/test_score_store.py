"""``PostgresScoreStore`` against real Postgres (Task 12).

Same convention as ``tests/test_confirm_store.py`` and ``tests/test_svc_views
.py``: direct SQL for fixtures, so these tests are about the store's
transaction shape rather than about another store's write path. Where the
brief specifically calls for the REAL write path (user feedback), the real
``PostgresSearchStore.record_feedback`` is used instead of a raw UPDATE — the
whole point of that test is proving the store composes correctly with
another module's write, not re-deriving what that write does.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from imageshield.db.connection import make_async_pool
from imageshield.score.engine import ScoreWeights
from imageshield.score.store import PostgresScoreStore
from imageshield.search.store import PostgresSearchStore
from imageshield.types import UserRef
from tests.conftest import make_config
from tests.db import ensure_subject, run_migrate


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
def store(pool: AsyncConnectionPool) -> PostgresScoreStore:
    cfg = make_config()
    return PostgresScoreStore(
        pool, weights=ScoreWeights.from_config(cfg), config_version=cfg.score_config_version
    )


def _user() -> UserRef:
    return UserRef(uuid4())


def _enrolment(conn: psycopg.Connection[Any], user_ref: UserRef) -> None:
    """A passed-and-consumed liveness session plus its enrolment — the same
    shape as ``tests/test_svc_views.py``'s ``_enrolment``, needed here to
    exercise ``ScoreState.enrolment_active``."""
    session_id = conn.execute(
        "INSERT INTO liveness_sessions"
        " (user_ref, provider_session_id, status, expires_at, consumed_at)"
        " VALUES (%s, %s, 'consumed', now() + interval '10 minutes', now())"
        " RETURNING session_id",
        (user_ref, uuid4().hex),
    ).fetchone()
    assert session_id is not None
    conn.execute(
        "INSERT INTO enrolments (session_id, user_ref, collection_id, external_face_id,"
        " model_id, source_object_uri, consent_ref, consent_document_sha256,"
        " consent_signed_at)"
        " VALUES (%s, %s, 'identity-v1', %s, 'rek-v6', 's3://proxy/ref.jpg', %s, %s, now())",
        (session_id[0], user_ref, uuid4().hex, uuid4(), "a" * 64),
    )


def _seed(conn: psycopg.Connection[Any], user_ref: UserRef) -> UUID:
    row = conn.execute(
        "INSERT INTO search_seeds (user_ref, seed_kind, source_object_ref)"
        " VALUES (%s, 'user_supplied', %s) RETURNING seed_id",
        (user_ref, f"photo/{uuid4().hex}"),
    ).fetchone()
    assert row is not None
    seed_id: UUID = row[0]
    return seed_id


def _infringement(
    conn: psycopg.Connection[Any],
    user_ref: UserRef,
    *,
    confirm_state: str = "confirmed",
    severity: str | None = "ncii_suspected",
    confirm_decided_by: str | None = "test-reviewer",
    url_alive: bool = True,
    status: str = "new",
) -> UUID:
    """A confirmed hit needs no seed, no run and no attestation — the exposure
    component's state load reads only ``infringements`` and
    ``infringement_feedback``. The CHECK on ``infringements_confirmed_needs_
    human`` (INVARIANTS #19/#47) is why ``confirm_decided_by``/``_at`` are
    required together whenever ``confirm_state = 'confirmed'``.
    """
    url_hash = uuid4().hex + uuid4().hex
    url = f"https://example.test/{uuid4().hex}"
    decided_at = "now()" if confirm_decided_by else "NULL"
    conn.execute(
        "INSERT INTO content_urls (url_hash, url, source_domain, canonical_url)"
        " VALUES (%s, %s, 'example.test', %s)",
        (url_hash, url, url),
    )
    row = conn.execute(
        "INSERT INTO infringements (user_ref, url_hash, page_url, image_url, confirm_state,"
        f" severity, confirm_decided_by, url_alive, status, confirm_decided_at)"
        f" VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, {decided_at})"
        " RETURNING infringement_id",
        (
            user_ref,
            url_hash,
            url,
            f"{url}.jpg",
            confirm_state,
            severity,
            confirm_decided_by,
            url_alive,
            status,
        ),
    ).fetchone()
    assert row is not None
    infringement_id: UUID = row[0]
    return infringement_id


def _row(migrated_db: str, sql: str, params: tuple[Any, ...]) -> dict[str, Any]:
    with psycopg.connect(migrated_db, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        row = conn.execute(sql, params).fetchone()
    assert row is not None
    return row


def _rows(migrated_db: str, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    with psycopg.connect(migrated_db, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        return list(conn.execute(sql, params).fetchall())


# ── recompute: the journal ───────────────────────────────────────────────


async def test_journal_sums_to_the_materialized_score(
    migrated_db: str, store: PostgresScoreStore, pool: AsyncConnectionPool
) -> None:
    user_ref = _user()
    await ensure_subject(pool, user_ref)
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        _enrolment(conn, user_ref)
        _seed(conn, user_ref)

    result = await store.recompute(user_ref, cause_kind="test")
    assert result is not None
    assert result.changed is True

    events = _rows(
        migrated_db,
        "SELECT delta, score_after FROM score_events WHERE user_ref = %s"
        " ORDER BY score_event_id",
        (user_ref,),
    )
    assert events, "recompute on a non-trivial state must journal something"
    assert sum(row["delta"] for row in events) == result.score
    assert events[-1]["score_after"] == result.score

    stored = _row(
        migrated_db, "SELECT score FROM protection_scores WHERE user_ref = %s", (user_ref,)
    )
    assert stored["score"] == result.score


async def test_recompute_twice_writes_nothing_new(
    migrated_db: str, store: PostgresScoreStore, pool: AsyncConnectionPool
) -> None:
    user_ref = _user()
    await ensure_subject(pool, user_ref)

    first = await store.recompute(user_ref, cause_kind="test")
    assert first is not None
    assert first.changed is True

    events_before = _rows(
        migrated_db, "SELECT score_event_id FROM score_events WHERE user_ref = %s", (user_ref,)
    )
    recs_before = _rows(
        migrated_db, "SELECT rec_id FROM recommendations WHERE user_ref = %s", (user_ref,)
    )
    score_row_before = _row(
        migrated_db,
        "SELECT score, computed_at FROM protection_scores WHERE user_ref = %s",
        (user_ref,),
    )

    second = await store.recompute(user_ref, cause_kind="test")
    assert second is not None
    assert second.changed is False
    assert second.score == first.score
    assert second.components == first.components

    events_after = _rows(
        migrated_db, "SELECT score_event_id FROM score_events WHERE user_ref = %s", (user_ref,)
    )
    recs_after = _rows(
        migrated_db, "SELECT rec_id FROM recommendations WHERE user_ref = %s", (user_ref,)
    )
    score_row_after = _row(
        migrated_db,
        "SELECT score, computed_at FROM protection_scores WHERE user_ref = %s",
        (user_ref,),
    )
    assert events_after == events_before
    assert recs_after == recs_before
    assert score_row_after == score_row_before  # computed_at untouched too


async def test_concurrent_first_recomputes_journal_the_diff_exactly_once(
    migrated_db: str, store: PostgresScoreStore, pool: AsyncConnectionPool
) -> None:
    """Invariant #44 regression: a subject that has never been scored has no
    ``protection_scores`` row, and locking a row that does not exist locks
    nothing -- so two concurrent recomputes could both read baseline zero and
    both journal the same diff, doubling the score in ``score_events`` while
    the materialized row (an idempotent upsert) looked correct. The step-2
    zero-row seed turns the ``FOR UPDATE`` into a real serialization point
    even on the very first recompute, which is the case that previously had
    no row for it to lock.
    """
    user_ref = _user()
    await ensure_subject(pool, user_ref)

    first, second = await asyncio.gather(
        store.recompute(user_ref, cause_kind="test"),
        store.recompute(user_ref, cause_kind="test"),
    )

    # Whichever transaction ran second is serialized behind the first and
    # sees the state it already wrote reflected in its own read -- no change,
    # nothing journaled. Exactly one of the two must have written anything.
    changed = [r for r in (first, second) if r is not None and r.changed]
    assert len(changed) == 1

    events = _rows(
        migrated_db,
        "SELECT delta FROM score_events WHERE user_ref = %s",
        (user_ref,),
    )
    stored = _row(
        migrated_db, "SELECT score FROM protection_scores WHERE user_ref = %s", (user_ref,)
    )
    assert sum(row["delta"] for row in events) == stored["score"]


async def test_config_version_stamped_on_every_journal_row(
    migrated_db: str, store: PostgresScoreStore, pool: AsyncConnectionPool
) -> None:
    user_ref = _user()
    await ensure_subject(pool, user_ref)

    result = await store.recompute(user_ref, cause_kind="test")
    assert result is not None

    cfg = make_config()
    events = _rows(
        migrated_db,
        "SELECT config_version FROM score_events WHERE user_ref = %s",
        (user_ref,),
    )
    assert events
    assert all(row["config_version"] == cfg.score_config_version for row in events)

    scored = _row(
        migrated_db,
        "SELECT config_version FROM protection_scores WHERE user_ref = %s",
        (user_ref,),
    )
    assert scored["config_version"] == cfg.score_config_version


# ── exposure: never lowered by the user's own reaction ───────────────────


async def test_user_feedback_never_lowers_the_score(
    migrated_db: str, store: PostgresScoreStore, pool: AsyncConnectionPool
) -> None:
    user_ref = _user()
    await ensure_subject(pool, user_ref)
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        not_me_id = _infringement(conn, user_ref, severity="ncii_suspected")
        authorised_id = _infringement(conn, user_ref, severity="ncii_suspected")

    baseline = await store.recompute(user_ref, cause_kind="test")
    assert baseline is not None
    score_x = baseline.score

    search_store = PostgresSearchStore(pool)

    await search_store.record_feedback(not_me_id, user_ref, "not_me")
    after_not_me = await store.recompute(user_ref, cause_kind="test")
    assert after_not_me is not None
    assert after_not_me.score >= score_x

    await search_store.record_feedback(authorised_id, user_ref, "authorised")
    after_authorised = await store.recompute(user_ref, cause_kind="test")
    assert after_authorised is not None
    assert after_authorised.score >= after_not_me.score


async def test_dead_url_restores_exposure(
    migrated_db: str, store: PostgresScoreStore, pool: AsyncConnectionPool
) -> None:
    user_ref = _user()
    await ensure_subject(pool, user_ref)
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        infringement_id = _infringement(conn, user_ref, severity="ncii_suspected")

    before = await store.recompute(user_ref, cause_kind="test")
    assert before is not None

    with psycopg.connect(migrated_db, autocommit=True) as conn:
        conn.execute(
            "UPDATE infringements SET url_alive = false WHERE infringement_id = %s",
            (infringement_id,),
        )

    after = await store.recompute(user_ref, cause_kind="test")
    assert after is not None
    assert after.components.exposure > before.components.exposure
    assert after.score >= before.score


async def test_resolved_restores_exposure_like_a_dead_url(
    migrated_db: str, store: PostgresScoreStore, pool: AsyncConnectionPool
) -> None:
    """migration 0028's signal gets the identical treatment as a dead URL and
    as ``authorised``/``dismissed_not_me``: a hit the user has resolved
    themselves stops costing exposure points, through the same ``counts``
    boolean in ``score/store.py``'s ``_CONFIRMED_HITS_SQL``.

    This is INVARIANTS #44 ("no feedback signal ever lowers the score")
    applied to the new signal: charging exposure for a hit the person already
    told us they dealt with is exactly a score-lowering effect of that
    signal, just arriving through a stale read on the next recompute instead
    of an explicit write. The design spec's own "nothing here touches a
    score" (§2) is about the ONE user-facing score in the backend repo
    (P13, bands, escrow) -- this repo's own control-room exposure component
    is a second, independent score this repo owns, and INVARIANTS #44 binds
    it regardless of who reads it.
    """
    user_ref = _user()
    await ensure_subject(pool, user_ref)
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        infringement_id = _infringement(conn, user_ref, severity="ncii_suspected")

    before = await store.recompute(user_ref, cause_kind="test")
    assert before is not None

    search_store = PostgresSearchStore(pool)
    await search_store.record_feedback(infringement_id, user_ref, "resolved")

    after = await store.recompute(user_ref, cause_kind="test")
    assert after is not None
    assert after.components.exposure > before.components.exposure
    assert after.score >= before.score


# ── unknown subject ────────────────────────────────────────────────────────


async def test_unknown_subject_returns_none_and_writes_nothing(
    migrated_db: str, store: PostgresScoreStore
) -> None:
    user_ref = _user()

    result = await store.recompute(user_ref, cause_kind="test")

    assert result is None
    assert _rows(
        migrated_db, "SELECT 1 FROM protection_scores WHERE user_ref = %s", (user_ref,)
    ) == []
    assert _rows(
        migrated_db, "SELECT 1 FROM score_events WHERE user_ref = %s", (user_ref,)
    ) == []
    assert _rows(
        migrated_db, "SELECT 1 FROM recommendations WHERE user_ref = %s", (user_ref,)
    ) == []


# ── recommendation lifecycle ──────────────────────────────────────────────


async def test_recommendation_lifecycle(
    migrated_db: str, store: PostgresScoreStore, pool: AsyncConnectionPool
) -> None:
    user_ref = _user()
    await ensure_subject(pool, user_ref)

    result = await store.recompute(user_ref, cause_kind="test")
    assert result is not None

    by_kind = {
        row["kind"]: row["status"]
        for row in _rows(
            migrated_db, "SELECT kind, status FROM recommendations WHERE user_ref = %s",
            (user_ref,),
        )
    }
    assert by_kind["add_seed_photos"] == "open"
    assert by_kind["complete_enrolment"] == "open"

    # Dismiss complete_enrolment — enrolment stays inactive, so the catalog
    # would desire it again on every future recompute; a dismissal must block
    # that forever, not just once.
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        conn.execute(
            "UPDATE recommendations SET status = 'dismissed'"
            " WHERE user_ref = %s AND kind = 'complete_enrolment'",
            (user_ref,),
        )
        for _ in range(5):
            _seed(conn, user_ref)

    await store.recompute(user_ref, cause_kind="test")

    after = _rows(
        migrated_db, "SELECT kind, status FROM recommendations WHERE user_ref = %s",
        (user_ref,),
    )
    statuses_by_kind: dict[str, list[str]] = {}
    for row in after:
        statuses_by_kind.setdefault(row["kind"], []).append(row["status"])

    assert "completed" in statuses_by_kind["add_seed_photos"]
    # Still exactly the one dismissed row -- never re-inserted as 'open'.
    assert statuses_by_kind["complete_enrolment"] == ["dismissed"]


async def test_a_subject_decision_is_not_awaiting_their_feedback(
    migrated_db: str, store: PostgresScoreStore, pool: AsyncConnectionPool
) -> None:
    """INVARIANTS #45, subject-decision lane (spec 2026-08-21).

    A subject who answers "yes, this is my photo" writes `confirm_state =
    'confirmed'` with `confirm_decided_by = 'subject'` and NO
    `infringement_feedback` row -- the decision lane and the feedback lane are
    deliberately separate. Without this, `awaiting_feedback_count` counts the
    hit they just answered: they lose `SCORE_POSTURE_FEEDBACK` for answering
    (a #45 violation on top of the intended Exposure move) and the app asks
    them to respond to a hit they have already responded to.

    The Exposure drop is expected and correct -- only the Posture penalty is
    the bug -- so this asserts on the component, not the total.
    """
    subject_user = _user()
    operator_user = _user()
    await ensure_subject(pool, subject_user)
    await ensure_subject(pool, operator_user)
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        _infringement(conn, subject_user, confirm_decided_by="subject")
        _infringement(conn, operator_user, confirm_decided_by="test-reviewer")

    decided_by_subject = await store.recompute(subject_user, cause_kind="test")
    decided_by_operator = await store.recompute(operator_user, cause_kind="test")
    assert decided_by_subject is not None and decided_by_operator is not None

    # The operator-decided hit legitimately awaits the user's feedback; the
    # subject-decided one does not -- they already said it.
    assert decided_by_subject.components.posture > decided_by_operator.components.posture
