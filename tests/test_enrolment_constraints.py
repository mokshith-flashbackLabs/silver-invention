"""Migration 0003: the DB itself refuses an enrolment for any session that is
not 'consumed' — a failed (or merely created/passed-unconsumed) session cannot
produce an enrolment even if application code is buggy (step-4 done-when:
"assert at the DB level, not just in application code").
"""

from __future__ import annotations

from uuid import UUID, uuid4

import psycopg
import pytest

from tests.db import run_migrate

INSERT_ENROLMENT = """
    INSERT INTO enrolments
      (session_id, user_ref, collection_id, external_face_id, model_id,
       source_object_uri)
    VALUES (%s, %s, 'identity-v1', %s, 'rekognition:7.0',
            'https://proxy-s3.example/ref.jpg')
"""


@pytest.fixture
def migrated_db(throwaway_db: str) -> str:
    run_migrate(throwaway_db, "down", "--all")
    up_result = run_migrate(throwaway_db, "up")
    assert up_result.returncode == 0, up_result.stderr
    return throwaway_db


def _insert_session(conn: psycopg.Connection[tuple[object, ...]], status: str) -> tuple[UUID, UUID]:
    session_id, user_ref = uuid4(), uuid4()
    conn.execute(
        "INSERT INTO liveness_sessions"
        " (session_id, user_ref, provider_session_id, status, expires_at,"
        "  completed_at, consumed_at)"
        " VALUES (%s, %s, %s, %s::liveness_status, now() + interval '10 minutes',"
        "  CASE WHEN %s IN ('passed','failed','consumed') THEN now() END,"
        "  CASE WHEN %s = 'consumed' THEN now() END)",
        (session_id, user_ref, f"prov-{uuid4()}", status, status, status),
    )
    return session_id, user_ref


def test_failed_session_cannot_enrol(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        session_id, user_ref = _insert_session(conn, "failed")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(INSERT_ENROLMENT, (session_id, user_ref, f"face-{uuid4()}"))


def test_passed_but_unconsumed_session_cannot_enrol(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        session_id, user_ref = _insert_session(conn, "passed")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(INSERT_ENROLMENT, (session_id, user_ref, f"face-{uuid4()}"))


def test_consumed_session_can_enrol_exactly_once(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        session_id, user_ref = _insert_session(conn, "consumed")
        conn.execute(INSERT_ENROLMENT, (session_id, user_ref, f"face-{uuid4()}"))
        # UNIQUE FK on session_id: the single-use enforcement (CLAUDE.md §4 #2).
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(INSERT_ENROLMENT, (session_id, user_ref, f"face-{uuid4()}"))


def test_session_status_column_only_accepts_consumed(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        session_id, user_ref = _insert_session(conn, "failed")
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO enrolments"
                " (session_id, session_status, user_ref, collection_id,"
                "  external_face_id, model_id, source_object_uri)"
                " VALUES (%s, 'failed', %s, 'identity-v1', %s, 'rekognition:7.0',"
                "  'https://proxy-s3.example/ref.jpg')",
                (session_id, user_ref, f"face-{uuid4()}"),
            )


def test_consumed_session_status_is_pinned_while_enrolment_exists(
    migrated_db: str,
) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        session_id, user_ref = _insert_session(conn, "consumed")
        conn.execute(INSERT_ENROLMENT, (session_id, user_ref, f"face-{uuid4()}"))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "UPDATE liveness_sessions SET status = 'passed' WHERE session_id = %s",
                (session_id,),
            )
