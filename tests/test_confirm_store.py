"""PostgresConfirmStore against real Postgres (same convention as
tests/test_search_store.py: own down --all + up arrange step; direct SQL for
fixtures, as in tests/test_svc_views.py, so these tests are about the store's
transactions rather than about search_store's write path)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from imageshield.confirm.store import CONFIRM_QUARANTINED_ACTION, PostgresConfirmStore
from imageshield.db.connection import make_async_pool
from imageshield.types import UserRef
from tests.db import run_migrate


@pytest.fixture
def migrated_db(throwaway_db: str) -> str:
    run_migrate(throwaway_db, "down", "--all")
    up_result = run_migrate(throwaway_db, "up")
    assert up_result.returncode == 0, up_result.stderr
    return throwaway_db


@pytest.fixture
async def store(migrated_db: str) -> AsyncIterator[PostgresConfirmStore]:
    pool = make_async_pool(migrated_db, min_size=1, max_size=2)
    await pool.open()
    try:
        yield PostgresConfirmStore(pool)
    finally:
        await pool.close()


def _user() -> UserRef:
    return UserRef(uuid4())


def _subject(conn: psycopg.Connection[Any], user_ref: UserRef) -> None:
    conn.execute(
        "INSERT INTO subjects (user_ref, discovery_eligible, eligibility_reason)"
        " VALUES (%s, true, 'adult')",
        (user_ref,),
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


def _run(conn: psycopg.Connection[Any], user_ref: UserRef, seed_id: UUID) -> UUID:
    row = conn.execute(
        "INSERT INTO search_runs (seed_id, user_ref, providers_attempted, threshold_config,"
        " seed_url, status, completed_at)"
        " VALUES (%s, %s, %s, '{}'::jsonb, 'https://s3.test/x', 'completed', now())"
        " RETURNING run_id",
        (seed_id, user_ref, ["hive"]),
    ).fetchone()
    assert row is not None
    run_id: UUID = row[0]
    return run_id


def _infringement(
    conn: psycopg.Connection[Any],
    user_ref: UserRef,
    run_id: UUID,
    *,
    confirm_state: str = "unconfirmed",
    phash: int | None = None,
    severity: str | None = None,
    confirm_decided_by: str | None = None,
    provider_score: str = "0.80",
) -> UUID:
    url_hash = uuid4().hex + uuid4().hex
    url = f"https://example.test/{uuid4().hex}"
    conn.execute(
        "INSERT INTO content_urls (url_hash, url, source_domain, canonical_url)"
        " VALUES (%s, %s, 'example.test', %s)",
        (url_hash, url, url),
    )
    decided_at = "now()" if confirm_decided_by else "NULL"
    row = conn.execute(
        "INSERT INTO infringements (user_ref, url_hash, page_url, image_url, confirm_state,"
        f" phash, severity, confirm_decided_by, confirm_decided_at)"
        f" VALUES (%s, %s, %s, %s, %s, %s, %s, %s, {decided_at})"
        " RETURNING infringement_id",
        (
            user_ref,
            url_hash,
            url,
            f"{url}.jpg",
            confirm_state,
            phash,
            severity,
            confirm_decided_by,
        ),
    ).fetchone()
    assert row is not None
    infringement_id: UUID = row[0]
    conn.execute(
        "INSERT INTO attestations (infringement_id, provider_id, score_kind, provider_score,"
        " score_version, band, last_run_id)"
        " VALUES (%s, 'hive', 'numeric', %s, 'v1', 'review', %s)",
        (infringement_id, provider_score, run_id),
    )
    return infringement_id


def _pending_task(
    conn: psycopg.Connection[Any],
    infringement_id: UUID,
    user_ref: UserRef,
    *,
    severity: str = "unassessed",
) -> None:
    conn.execute(
        "INSERT INTO review_tasks (infringement_id, user_ref, severity, triage)"
        " VALUES (%s, %s, %s, '{}'::jsonb)",
        (infringement_id, user_ref, severity),
    )


def _decided_task(
    conn: psycopg.Connection[Any],
    infringement_id: UUID,
    user_ref: UserRef,
    *,
    severity: str = "ncii_suspected",
) -> None:
    conn.execute(
        "INSERT INTO review_tasks (infringement_id, user_ref, severity, triage, status,"
        " decision, decided_by, decided_at)"
        " VALUES (%s, %s, %s, '{}'::jsonb, 'decided', 'confirmed', 'reviewer', now())",
        (infringement_id, user_ref, severity),
    )


def _row(migrated_db: str, sql: str, params: tuple[Any, ...]) -> dict[str, Any]:
    with psycopg.connect(migrated_db, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        row = conn.execute(sql, params).fetchone()
    assert row is not None
    return row


def _rows(migrated_db: str, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    with psycopg.connect(migrated_db, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        return list(conn.execute(sql, params).fetchall())


# ── load_context ──────────────────────────────────────────────────────────


async def test_context_loads_with_the_representative_run_id(
    migrated_db: str, store: PostgresConfirmStore
) -> None:
    user_ref = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        _subject(conn, user_ref)
        seed = _seed(conn, user_ref)
        run_id = _run(conn, user_ref, seed)
        infringement_id = _infringement(conn, user_ref, run_id)

    context = await store.load_context(infringement_id)
    assert context is not None
    assert context.infringement_id == infringement_id
    assert context.user_ref == user_ref
    assert context.confirm_state == "unconfirmed"
    assert context.run_id == run_id
    assert context.page_url is not None
    assert context.image_url is not None


async def test_unknown_infringement_id_returns_none(
    migrated_db: str, store: PostgresConfirmStore
) -> None:
    assert await store.load_context(uuid4()) is None


# ── record_duplicate ─────────────────────────────────────────────────────


async def test_record_duplicate_transitions_state_and_removes_pending_task(
    migrated_db: str, store: PostgresConfirmStore
) -> None:
    user_ref = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        _subject(conn, user_ref)
        seed = _seed(conn, user_ref)
        run_id = _run(conn, user_ref, seed)
        original = _infringement(conn, user_ref, run_id)
        duplicate = _infringement(conn, user_ref, run_id)
        _pending_task(conn, duplicate, user_ref)

    await store.record_duplicate(duplicate, duplicate_of=original, phash=12345)

    row = _row(
        migrated_db,
        "SELECT confirm_state, duplicate_of, phash FROM infringements WHERE infringement_id = %s",
        (duplicate,),
    )
    assert row["confirm_state"] == "duplicate"
    assert row["duplicate_of"] == original
    assert row["phash"] == 12345
    assert (
        _rows(
            migrated_db,
            "SELECT 1 FROM review_tasks WHERE infringement_id = %s",
            (duplicate,),
        )
        == []
    )


async def test_record_duplicate_refuses_on_an_already_decided_row(
    migrated_db: str, store: PostgresConfirmStore
) -> None:
    user_ref = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        _subject(conn, user_ref)
        seed = _seed(conn, user_ref)
        run_id = _run(conn, user_ref, seed)
        original = _infringement(conn, user_ref, run_id)
        decided = _infringement(
            conn, user_ref, run_id, confirm_state="confirmed", confirm_decided_by="reviewer"
        )
        _decided_task(conn, decided, user_ref)

    await store.record_duplicate(decided, duplicate_of=original, phash=999)

    row = _row(
        migrated_db,
        "SELECT confirm_state, duplicate_of, phash FROM infringements WHERE infringement_id = %s",
        (decided,),
    )
    assert row["confirm_state"] == "confirmed"
    assert row["duplicate_of"] is None
    assert row["phash"] is None
    # rowcount 0 -> method returns without touching -- the decided task is
    # still there, untouched.
    task = _row(
        migrated_db,
        "SELECT status FROM review_tasks WHERE infringement_id = %s",
        (decided,),
    )
    assert task["status"] == "decided"


# ── record_triage ─────────────────────────────────────────────────────────


async def test_record_triage_upserts_a_pending_task(
    migrated_db: str, store: PostgresConfirmStore
) -> None:
    user_ref = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        _subject(conn, user_ref)
        seed = _seed(conn, user_ref)
        run_id = _run(conn, user_ref, seed)
        infringement_id = _infringement(conn, user_ref, run_id)

    await store.record_triage(
        infringement_id,
        severity="benign_copy",
        phash=42,
        face_match_score=91.25,
        moderation_labels=[{"name": "Explicit Nudity", "confidence": 10.0}],
        triage={"face_match_score": 91.25},
    )

    infr = _row(
        migrated_db,
        "SELECT confirm_state, severity, phash, face_match_score, moderation_labels"
        " FROM infringements WHERE infringement_id = %s",
        (infringement_id,),
    )
    assert infr["confirm_state"] == "machine_triaged"
    assert infr["severity"] == "benign_copy"
    assert infr["phash"] == 42
    assert float(infr["face_match_score"]) == 91.25
    assert infr["moderation_labels"] == [{"name": "Explicit Nudity", "confidence": 10.0}]

    task = _row(
        migrated_db,
        "SELECT user_ref, severity, triage, status FROM review_tasks"
        " WHERE infringement_id = %s",
        (infringement_id,),
    )
    assert task["user_ref"] == user_ref
    assert task["severity"] == "benign_copy"
    assert task["triage"] == {"face_match_score": 91.25}
    assert task["status"] == "pending"

    # Re-run with a different severity: the pending task IS reopened/updated.
    await store.record_triage(
        infringement_id,
        severity="ncii_suspected",
        phash=42,
        face_match_score=97.0,
        moderation_labels=None,
        triage={"face_match_score": 97.0},
    )
    task_after = _row(
        migrated_db,
        "SELECT severity, triage FROM review_tasks WHERE infringement_id = %s",
        (infringement_id,),
    )
    assert task_after["severity"] == "ncii_suspected"
    assert task_after["triage"] == {"face_match_score": 97.0}


async def test_record_triage_does_not_reopen_a_decided_task(
    migrated_db: str, store: PostgresConfirmStore
) -> None:
    """Defensive belt-and-braces: a review_tasks row already 'decided' is never
    overwritten by the ON CONFLICT upsert, even though in practice a decided
    task's infringement would already have moved to confirm_state
    'confirmed'/'rejected' (which the outer guard would separately refuse)."""
    user_ref = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        _subject(conn, user_ref)
        seed = _seed(conn, user_ref)
        run_id = _run(conn, user_ref, seed)
        infringement_id = _infringement(
            conn, user_ref, run_id, confirm_state="machine_triaged"
        )
        _decided_task(conn, infringement_id, user_ref, severity="ncii_suspected")

    await store.record_triage(
        infringement_id,
        severity="benign_copy",
        phash=7,
        face_match_score=50.0,
        moderation_labels=None,
        triage={"new": "value"},
    )

    # The infringement row IS updated -- the guard only checks confirm_state,
    # which was 'machine_triaged' and stayed eligible.
    infr = _row(
        migrated_db,
        "SELECT severity FROM infringements WHERE infringement_id = %s",
        (infringement_id,),
    )
    assert infr["severity"] == "benign_copy"

    # The decided review_tasks row is untouched.
    task = _row(
        migrated_db,
        "SELECT severity, status, triage FROM review_tasks WHERE infringement_id = %s",
        (infringement_id,),
    )
    assert task["severity"] == "ncii_suspected"
    assert task["status"] == "decided"
    assert task["triage"] == {}


async def test_record_triage_refuses_on_an_already_decided_infringement(
    migrated_db: str, store: PostgresConfirmStore
) -> None:
    user_ref = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        _subject(conn, user_ref)
        seed = _seed(conn, user_ref)
        run_id = _run(conn, user_ref, seed)
        infringement_id = _infringement(
            conn, user_ref, run_id, confirm_state="rejected"
        )

    await store.record_triage(
        infringement_id,
        severity="ncii_suspected",
        phash=1,
        face_match_score=99.0,
        moderation_labels=None,
        triage={"x": 1},
    )

    infr = _row(
        migrated_db,
        "SELECT confirm_state, severity FROM infringements WHERE infringement_id = %s",
        (infringement_id,),
    )
    assert infr["confirm_state"] == "rejected"
    assert infr["severity"] is None
    assert (
        _rows(
            migrated_db,
            "SELECT 1 FROM review_tasks WHERE infringement_id = %s",
            (infringement_id,),
        )
        == []
    )


# ── record_quarantine ────────────────────────────────────────────────────


async def test_record_quarantine_writes_the_audit_row_and_review_task(
    migrated_db: str, store: PostgresConfirmStore
) -> None:
    user_ref = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        _subject(conn, user_ref)
        seed = _seed(conn, user_ref)
        run_id = _run(conn, user_ref, seed)
        infringement_id = _infringement(conn, user_ref, run_id)

    await store.record_quarantine(
        infringement_id,
        phash=555,
        moderation_labels=[{"name": "Explicit Nudity", "confidence": 96.0}],
        min_age_low=11.5,
    )

    infr = _row(
        migrated_db,
        "SELECT confirm_state, phash, moderation_labels FROM infringements"
        " WHERE infringement_id = %s",
        (infringement_id,),
    )
    assert infr["confirm_state"] == "quarantined"
    assert infr["phash"] == 555
    assert infr["moderation_labels"] == [{"name": "Explicit Nudity", "confidence": 96.0}]

    task = _row(
        migrated_db,
        "SELECT severity, status, triage FROM review_tasks WHERE infringement_id = %s",
        (infringement_id,),
    )
    assert task["severity"] == "ncii_suspected"
    assert task["status"] == "quarantined"
    assert task["triage"] == {"quarantine": True, "min_age_low": 11.5}

    audit = _row(
        migrated_db,
        "SELECT action, subject_ref, resource_id, metadata FROM audit_log"
        " WHERE resource_id = %s",
        (infringement_id,),
    )
    assert audit["action"] == CONFIRM_QUARANTINED_ACTION == "confirm.quarantined"
    assert audit["subject_ref"] == user_ref
    assert audit["metadata"] == {"min_age_low": 11.5}


# ── record_unfetchable ───────────────────────────────────────────────────


async def test_record_unfetchable_lands_unassessed_severity(
    migrated_db: str, store: PostgresConfirmStore
) -> None:
    user_ref = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        _subject(conn, user_ref)
        seed = _seed(conn, user_ref)
        run_id = _run(conn, user_ref, seed)
        infringement_id = _infringement(conn, user_ref, run_id)

    await store.record_unfetchable(infringement_id, detail="timed out fetching image_url")

    infr = _row(
        migrated_db,
        "SELECT confirm_state, severity, phash FROM infringements WHERE infringement_id = %s",
        (infringement_id,),
    )
    assert infr["confirm_state"] == "machine_triaged"
    assert infr["severity"] == "unassessed"
    assert infr["phash"] is None

    task = _row(
        migrated_db,
        "SELECT severity, triage, status FROM review_tasks WHERE infringement_id = %s",
        (infringement_id,),
    )
    assert task["severity"] == "unassessed"
    assert task["triage"] == {"unfetchable": "timed out fetching image_url"}
    assert task["status"] == "pending"


# ── record_skipped ───────────────────────────────────────────────────────


async def test_record_skipped_leaves_confirm_state_unconfirmed(
    migrated_db: str, store: PostgresConfirmStore
) -> None:
    user_ref = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        _subject(conn, user_ref)
        seed = _seed(conn, user_ref)
        run_id = _run(conn, user_ref, seed)
        infringement_id = _infringement(conn, user_ref, run_id)

    await store.record_skipped(
        infringement_id, reason="breaker_open", detail="rekognition_confirm breaker open"
    )

    infr = _row(
        migrated_db,
        "SELECT confirm_state, severity FROM infringements WHERE infringement_id = %s",
        (infringement_id,),
    )
    assert infr["confirm_state"] == "unconfirmed"
    assert infr["severity"] is None

    task = _row(
        migrated_db,
        "SELECT severity, triage, status FROM review_tasks WHERE infringement_id = %s",
        (infringement_id,),
    )
    assert task["severity"] == "unassessed"
    assert task["triage"] == {
        "skipped": "breaker_open",
        "detail": "rekognition_confirm breaker open",
    }
    assert task["status"] == "pending"


async def test_record_skipped_on_unknown_id_is_a_silent_noop(
    migrated_db: str, store: PostgresConfirmStore
) -> None:
    await store.record_skipped(uuid4(), reason="breaker_open", detail="n/a")


# ── decided_phashes ──────────────────────────────────────────────────────


async def test_decided_phashes_is_isolated_per_user(
    migrated_db: str, store: PostgresConfirmStore
) -> None:
    user_a = _user()
    user_b = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        _subject(conn, user_a)
        _subject(conn, user_b)
        seed_a = _seed(conn, user_a)
        seed_b = _seed(conn, user_b)
        run_a = _run(conn, user_a, seed_a)
        run_b = _run(conn, user_b, seed_b)
        infr_a = _infringement(
            conn,
            user_a,
            run_a,
            confirm_state="confirmed",
            confirm_decided_by="reviewer",
            phash=100,
        )
        _infringement(
            conn,
            user_b,
            run_b,
            confirm_state="confirmed",
            confirm_decided_by="reviewer",
            phash=200,
        )

    result = await store.decided_phashes(user_a)
    assert result == ((infr_a, 100),)


async def test_decided_phashes_excludes_machine_triaged_rows(
    migrated_db: str, store: PostgresConfirmStore
) -> None:
    """A hit with a phash but no human decision must not be a dedup source --
    nothing can inherit from an undecided hit."""
    user_ref = _user()
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        _subject(conn, user_ref)
        seed = _seed(conn, user_ref)
        run_id = _run(conn, user_ref, seed)
        confirmed = _infringement(
            conn,
            user_ref,
            run_id,
            confirm_state="confirmed",
            confirm_decided_by="reviewer",
            phash=300,
        )
        rejected = _infringement(
            conn, user_ref, run_id, confirm_state="rejected", phash=301
        )
        _infringement(
            conn, user_ref, run_id, confirm_state="machine_triaged", phash=302
        )
        _infringement(conn, user_ref, run_id, confirm_state="unconfirmed", phash=None)

    result = dict(await store.decided_phashes(user_ref))
    assert result == {confirmed: 300, rejected: 301}
