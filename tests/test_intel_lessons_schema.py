"""Migration 0055 (spec 2026-10-15-intel-lessons-and-evidence §8): the lesson and evidence tables,
the widened CHECKs and the three svc views, read as the proxy reads them."""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest

from tests.db import run_migrate

VIEWS = {
    "v_evidence": [
        "owner_kind",
        "owner_id",
        "document_id",
        "publisher_domain",
        "document_title",
        "published_at",
        "excerpt_id",
        "quote",
        "link_url",
        "independent_sources",
        "approved_at",
    ],
    "v_lessons": [
        "lesson_id",
        "event_id",
        "kind",
        "topic",
        "title",
        "intro",
        "sections",
        "link_url",
        "link_label",
        "tags",
        "is_global",
        "approved_at",
    ],
    "v_intel_weekly_totals": [
        "week_start",
        "documents_read",
        "publishers",
        "risks_found",
        "proposed",
        "approved",
        "rejected",
    ],
}


def test_views_have_exactly_the_contract_columns(intel_db: str) -> None:
    with psycopg.connect(intel_db) as conn:
        for view, columns in VIEWS.items():
            rows = conn.execute(
                "SELECT a.attname FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid"
                " JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = 'svc' AND c.relname = %s AND a.attnum > 0"
                " AND NOT a.attisdropped ORDER BY a.attnum",
                (view,),
            ).fetchall()
            assert [r[0] for r in rows] == columns, view


def test_views_are_granted_to_the_proxy_and_carry_no_user_ref(intel_db: str) -> None:
    with psycopg.connect(intel_db) as conn:
        for view, columns in VIEWS.items():
            granted = conn.execute(
                "SELECT has_table_privilege('imageshield_proxy_ro', %s, 'SELECT')",
                (f"svc.{view}",),
            ).fetchone()
            assert granted == (True,), view
            assert "user_ref" not in columns and "person_ref" not in columns


def test_kind_and_run_kind_checks_admit_lessons(intel_db: str) -> None:
    with psycopg.connect(intel_db) as conn:
        conn.execute(
            "INSERT INTO intel_runs (kind, request, requested_by)"
            " VALUES ('lesson_draft', '{}', 'op')"
        )
        conn.execute(
            "INSERT INTO intel_proposals (kind, status, target, suggested, rationale, model_id,"
            " prompt_version) VALUES ('lesson', 'pending', '{\"topic\": \"spot_sextortion\"}',"
            " '{}', 'r', 'm', 'propose-v14')"
        )


def test_a_general_lesson_needs_a_topic_and_no_event(intel_db: str) -> None:
    with psycopg.connect(intel_db) as conn:
        proposal = conn.execute(
            "INSERT INTO intel_proposals (kind, status, target, suggested, rationale, model_id,"
            " prompt_version) VALUES ('lesson', 'pending', '{}', '{}', 'r', 'm', 'v')"
            " RETURNING proposal_id"
        ).fetchone()
        assert proposal is not None
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO intel_lessons (proposal_id, event_id, kind, topic, title, intro,"
                " sections, approved_by)"
                " VALUES (%s, %s, 'general', 'x_topic', 't', 'i', '[]', 'op')",
                (proposal[0], uuid4()),
            )


def test_0055_down_and_up_again(intel_db: str) -> None:
    # Two steps: 0056 (hidden quotes) sits on top of it since 2026-10-22.
    down = run_migrate(intel_db, "down", "--steps", "2")
    assert down.returncode == 0, down.stderr
    up = run_migrate(intel_db, "up")
    assert up.returncode == 0, up.stderr


def test_0056_down_publishes_hidden_quotes_again_and_up_restores_the_columns(intel_db: str) -> None:
    """2026-10-22: 0056 adds the hide state and leaves hidden quotes out of svc.v_evidence; its
    down drops the state and the view shows every quote, as 0055 made it."""
    columns = (
        "SELECT count(*) FROM information_schema.columns WHERE table_name = 'intel_excerpts'"
        " AND column_name IN ('hidden_at', 'hidden_by', 'hide_reason')"
    )
    with psycopg.connect(intel_db, autocommit=True) as conn:
        assert conn.execute(columns).fetchone() == (3,)
        definition = conn.execute("SELECT pg_get_viewdef('svc.v_evidence')").fetchone()
        assert definition is not None and "hidden_at IS NULL" in definition[0]
    down = run_migrate(intel_db, "down", "--steps", "1")
    assert down.returncode == 0, down.stderr
    with psycopg.connect(intel_db, autocommit=True) as conn:
        assert conn.execute(columns).fetchone() == (0,)
    up = run_migrate(intel_db, "up")
    assert up.returncode == 0, up.stderr
    with psycopg.connect(intel_db, autocommit=True) as conn:
        assert conn.execute(columns).fetchone() == (3,)
