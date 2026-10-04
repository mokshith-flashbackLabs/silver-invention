"""0048 -- an approval supersedes the pending proposals it overlaps, ``covered_by_decision``
(spec 2026-10-04-intel-evidence-quality §5). Privileges asserted under SET ROLE intel_rw, the
only place a role's real grants show (test_intel_schema precedent)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg.types.json import Jsonb

from tests.db import run_migrate

_PROPOSAL = (
    "INSERT INTO intel_proposals (kind, status, supersede_reason, target, suggested, rationale,"
    " model_id, prompt_version) VALUES ('threat_event', %s, %s, %s, %s, 'why',"
    " 'claude-opus-5-5', 'propose-v4') RETURNING proposal_id"
)


def _steps_through(version: str) -> str:
    ups = sorted(p.name for p in (Path(__file__).parent.parent / "migrations").glob("*.up.sql"))
    return str(sum(1 for name in ups if name >= version))


@pytest.fixture
def migrated_db(throwaway_db: str) -> str:
    assert run_migrate(throwaway_db, "down", "--all").returncode == 0
    up = run_migrate(throwaway_db, "up")
    assert up.returncode == 0, up.stderr
    return throwaway_db


def _insert(conn: psycopg.Connection[Any], status: str, reason: str | None) -> UUID:
    row = conn.execute(
        _PROPOSAL,
        (
            status,
            reason,
            Jsonb({"tags": ["instagram"]}),
            Jsonb({"kind": "leak", "title": "t", "severity": 2, "expires_in_days": 30}),
        ),
    ).fetchone()
    assert row is not None
    proposal_id: UUID = row[0]
    return proposal_id


def _reason(conn: psycopg.Connection[Any], proposal_id: UUID) -> str | None:
    row = conn.execute(
        "SELECT supersede_reason FROM intel_proposals WHERE proposal_id = %s", (proposal_id,)
    ).fetchone()
    assert row is not None
    reason: str | None = row[0]
    return reason


def test_0048_covered_by_decision_is_a_reason_and_the_pairing_still_holds(
    migrated_db: str,
) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        conn.execute("SET ROLE intel_rw")
        covered = _insert(conn, "superseded", "covered_by_decision")
        assert _reason(conn, covered) == "covered_by_decision"
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert(conn, "superseded", "because")
        with pytest.raises(psycopg.errors.CheckViolation):  # a reason only on superseded
            _insert(conn, "pending", "covered_by_decision")
        pending = _insert(conn, "pending", None)
        conn.execute(
            "UPDATE intel_proposals SET status = 'superseded',"
            " supersede_reason = 'covered_by_decision' WHERE proposal_id = %s",
            (pending,),
        )
        assert _reason(conn, pending) == "covered_by_decision"
        conn.execute("RESET ROLE")


def test_0048_down_relabels_covered_rows_and_up_accepts_the_reason_again(
    migrated_db: str,
) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        covered = _insert(conn, "superseded", "covered_by_decision")
        older = _insert(conn, "superseded", "resolved_by_quiz")
    down = run_migrate(migrated_db, "down", "--steps", _steps_through("0048_"))
    assert down.returncode == 0, down.stderr
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        assert _reason(conn, covered) == "newer_proposal"
        assert _reason(conn, older) == "resolved_by_quiz"
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert(conn, "superseded", "covered_by_decision")
    up = run_migrate(migrated_db, "up")
    assert up.returncode == 0, up.stderr
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        assert _reason(conn, _insert(conn, "superseded", "covered_by_decision")) == (
            "covered_by_decision"
        )
