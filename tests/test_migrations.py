"""Migration 0001 tests, run against a real, disposable Postgres database.

Every up/down call goes through ``tests.db.run_migrate``, which shells out to
the real ``scripts/migrate.py`` CLI — the artifact under test, not a
reimplementation of its transaction/checksum logic. Each test starts with an
explicit ``down --all`` (a no-op on an already-empty database) so tests don't
depend on execution order, even though they share one throwaway database for
the session.
"""

from __future__ import annotations

import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from imageshield.preview.store import (
    _COUNT_RENDERS_SQL,
    _RECORD_RENDER_SQL,
    PREVIEW_RENDERED_ACTION,
)
from imageshield.review.store import _SUBJECT_DECISIONS_SQL
from tests.db import run_migrate  # re-exported
from tests.db import second_throwaway_db as second_throwaway_db

CORE_TABLES = {
    "liveness_sessions",
    "enrolments",
    "providers",
    "search_seeds",
    "search_runs",
    "content_urls",
    "provider_calls",
    "infringements",
    "attestations",
    "outbox",
    "audit_log",
}
CUSTOM_TYPES = {"liveness_status", "provider_kind"}


def _table_names(conn: psycopg.Connection[tuple[str]]) -> set[str]:
    rows = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()
    return {row[0] for row in rows}


def _type_names(conn: psycopg.Connection[tuple[str]]) -> set[str]:
    rows = conn.execute(
        "SELECT typname FROM pg_type WHERE typname = ANY(%s)",
        (list(CUSTOM_TYPES),),
    ).fetchall()
    return {row[0] for row in rows}


def _role_exists(conn: psycopg.Connection[tuple[str]]) -> bool:
    row = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'imageshield_app'").fetchone()
    return row is not None


def _external_dependant_dbs(conn: psycopg.Connection[tuple[str]], own: set[str]) -> set[str]:
    """Databases OTHER than the test's own that hold grants on the
    cluster-global ``imageshield_app`` role.

    The role can only be dropped once no database on the cluster grants it
    anything (the down migration documents this). On a dev machine the
    persistent compose database (``imageshield``, used by the devtools
    harness) typically has 0001 applied, so the role legitimately survives a
    throwaway database's ``down --all``. Tests that assert the role is gone
    must therefore branch on this, or they fail on any machine where the
    harness has ever run.
    """
    rows = conn.execute(
        "SELECT DISTINCT d.datname"
        " FROM pg_shdepend s"
        " JOIN pg_database d ON d.oid = s.dbid"
        " JOIN pg_roles r ON r.oid = s.refobjid"
        " WHERE r.rolname = 'imageshield_app'"
    ).fetchall()
    return {row[0] for row in rows} - own


def _db_name(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).path.lstrip("/")


def test_up_from_empty_applies_0001_cleanly(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")

    result = run_migrate(throwaway_db, "up")

    assert result.returncode == 0, result.stderr
    assert "applied 0001_initial_schema.up.sql" in result.stdout

    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        tables = _table_names(conn)
        types = _type_names(conn)
        role_exists = _role_exists(conn)

    assert tables >= CORE_TABLES
    assert types == CUSTOM_TYPES
    assert role_exists is True


def test_down_all_after_up_leaves_no_phase2_objects(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    up_result = run_migrate(throwaway_db, "up")
    assert up_result.returncode == 0, up_result.stderr

    down_result = run_migrate(throwaway_db, "down", "--all")

    assert down_result.returncode == 0, down_result.stderr

    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        tables = _table_names(conn)
        types = _type_names(conn)
        role_exists = _role_exists(conn)
        external = _external_dependant_dbs(conn, {_db_name(throwaway_db)})

    assert tables == {"schema_migrations"}
    assert types == set()
    if external:
        # Another database on this cluster (e.g. the compose dev database the
        # harness migrated) still grants the role: the down migration's
        # documented behaviour is to leave it in place.
        assert role_exists is True
    else:
        assert role_exists is False


def test_up_down_up_round_trip(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")

    first_up = run_migrate(throwaway_db, "up")
    down = run_migrate(throwaway_db, "down", "--all")
    second_up = run_migrate(throwaway_db, "up")

    assert first_up.returncode == 0, first_up.stderr
    assert down.returncode == 0, down.stderr
    assert second_up.returncode == 0, second_up.stderr

    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        tables = _table_names(conn)
    assert tables >= CORE_TABLES


def test_imageshield_app_role_is_insert_only_on_audit_log(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    up_result = run_migrate(throwaway_db, "up")
    assert up_result.returncode == 0, up_result.stderr

    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        conn.execute("SET ROLE imageshield_app")

        # INSERT is the one grant this role has; the role is never granted
        # SELECT either, so verification below happens after RESET ROLE.
        conn.execute("INSERT INTO audit_log (actor_type, action) VALUES ('system', 'test')")

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE audit_log SET action = 'changed'")

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM audit_log")

        conn.execute("RESET ROLE")
        (count,) = conn.execute("SELECT count(*) FROM audit_log").fetchone()  # type: ignore[misc]
        assert count == 1


def test_0004_score_shape_check_constraint(throwaway_db: str) -> None:
    """A match row must carry a numeric score OR a category — never neither.

    Migration 0004 relaxes ``provider_score`` to nullable (Google Web
    Detection returns ``score: null`` and the adapter must not invent a
    number) and the CHECK constraint is what keeps the relaxation from
    admitting score-less numeric rows.
    """
    run_migrate(throwaway_db, "down", "--all")
    up = run_migrate(throwaway_db, "up")
    assert up.returncode == 0, up.stderr
    # search_matches is dropped by 0006; its constraint is asserted at the
    # migration state where the table still exists. The same shape rule lives
    # on `attestations` now — see test_0005_uniques_enforce_the_dedup_key.
    back = run_migrate(throwaway_db, "down", "--steps", _steps_back_to("0004_"))
    assert back.returncode == 0, back.stderr

    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO search_seeds (seed_id, user_ref, seed_kind, source_object_uri)"
            " VALUES ('00000000-0000-0000-0000-000000000001',"
            "  '00000000-0000-0000-0000-0000000000aa', 'user_supplied', 'https://x/img.jpg')"
        )
        conn.execute(
            "INSERT INTO search_runs (run_id, seed_id, user_ref, providers_attempted,"
            " threshold_config) VALUES ('00000000-0000-0000-0000-000000000002',"
            " '00000000-0000-0000-0000-000000000001',"
            " '00000000-0000-0000-0000-0000000000aa', '{hive}', '{}')"
        )
        conn.execute(
            "INSERT INTO content_urls (url_hash, url, source_domain)"
            f" VALUES ('{'a' * 64}', 'https://x/img.jpg', 'x')"
        )

        common = (
            "INSERT INTO search_matches (run_id, url_hash, user_ref, provider_id,"
            " image_url, score_version, band, score_kind, provider_score, provider_category)"
            " VALUES ('00000000-0000-0000-0000-000000000002', %s,"
            " '00000000-0000-0000-0000-0000000000aa', %s, 'https://x/img.jpg', 'v1',"
            " 'review', %s, %s, %s)"
        )
        # numeric with a score: fine
        conn.execute(common, ("a" * 64, "hive", "numeric", "0.8712", None))
        # categorical with a category and NULL score: fine
        conn.execute(common, ("a" * 64, "google", "categorical", None, "full_match"))
        # neither score nor category: rejected by the CHECK
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(common, ("a" * 64, "hive", "numeric", None, None))


def test_0004_providers_seeded_with_score_domain(throwaway_db: str) -> None:
    """Step 7 reads the score domain from here — Hive's floor is 0.5, and
    banding 0.5-1.0 as though it were 0-1 reads weak matches as moderate."""
    run_migrate(throwaway_db, "down", "--all")
    up = run_migrate(throwaway_db, "up")
    assert up.returncode == 0, up.stderr

    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        rows = dict(
            conn.execute(
                "SELECT provider_id, score_domain FROM providers"
                " WHERE provider_id IN ('hive', 'google')"
            ).fetchall()
        )
        (run_status,) = conn.execute(
            "SELECT column_default FROM information_schema.columns"
            " WHERE table_name = 'search_runs' AND column_name = 'status'"
        ).fetchone()  # type: ignore[misc]

    assert rows["hive"] == {"min": 0.5, "max": 1.0}
    assert rows["google"] == {"categories": ["full_match", "partial_match", "page_match"]}
    assert run_status == "'queued'::text"


def test_down_all_on_one_db_does_not_break_role_for_sibling_db(
    throwaway_db: str, second_throwaway_db: str
) -> None:
    """The cluster-global ``imageshield_app`` role must survive tearing down
    ONE of two databases on the same server that both have 0001 applied —
    mirrors two concurrent pytest sessions against one compose Postgres, or
    a second app database sharing the cluster. Before the down-migration fix
    this failed with "role ... cannot be dropped because some objects
    depend on it" (the role is still granted on db B's audit_log) and left
    db A's own teardown broken.
    """
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(second_throwaway_db, "down", "--all")

    up_a = run_migrate(throwaway_db, "up")
    up_b = run_migrate(second_throwaway_db, "up")
    assert up_a.returncode == 0, up_a.stderr
    assert up_b.returncode == 0, up_b.stderr

    down_a = run_migrate(throwaway_db, "down", "--all")
    assert down_a.returncode == 0, down_a.stderr

    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        assert _table_names(conn) == {"schema_migrations"}

    # db B is untouched: same assertions as
    # test_imageshield_app_role_is_insert_only_on_audit_log, run against db B
    # *after* db A's teardown, proving the role and its grants there are
    # unaffected by whatever db A's down migration did to the role.
    with psycopg.connect(second_throwaway_db, autocommit=True) as conn:
        conn.execute("SET ROLE imageshield_app")
        conn.execute("INSERT INTO audit_log (actor_type, action) VALUES ('system', 'test')")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE audit_log SET action = 'changed'")
        conn.execute("RESET ROLE")
        (count,) = conn.execute("SELECT count(*) FROM audit_log").fetchone()  # type: ignore[misc]
        assert count == 1

    # db B was the last of the TEST databases depending on the role: tearing
    # it down too must succeed, and — unless some other database on the
    # cluster (e.g. the compose dev database) still grants the role — must
    # actually remove it from the cluster.
    down_b = run_migrate(second_throwaway_db, "down", "--all")
    assert down_b.returncode == 0, down_b.stderr

    own = {_db_name(throwaway_db), _db_name(second_throwaway_db)}
    with psycopg.connect(second_throwaway_db, autocommit=True) as conn:
        external = _external_dependant_dbs(conn, own)
        role_exists = _role_exists(conn)
    if external:
        assert role_exists is True
    else:
        assert role_exists is False


def _steps_back_to(version: str) -> str:
    """How many ``down --steps N`` to land with ``version`` still applied.

    Computed from the migrations directory, never hardcoded. Hand-counted
    numbers broke on three consecutive migrations, and the failure mode the
    original comment feared — "a silently-wrong count would revert the wrong
    migration and pass" — is precisely what computing it prevents. A literal
    only fails loudly when some *other* assertion in the same test happens to
    notice.
    """
    from tests.db import ROOT

    # Compare the 4-digit prefix, not the whole filename: "0004_foo.up.sql" is
    # lexicographically greater than "0004", so a substring comparison would
    # count the target migration itself and revert one step too many.
    later = [
        path
        for path in (ROOT / "migrations").glob("*.up.sql")
        if path.name[:4] > version[:4]
    ]
    return str(len(later))


def test_0005_creates_infringements_and_attestations(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    result = run_migrate(throwaway_db, "up")
    assert result.returncode == 0, result.stderr

    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        assert {"infringements", "attestations"} <= _table_names(conn)

        cols = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'content_urls'"
            ).fetchall()
        }
        assert {"normalisation_version", "canonical_url"} <= cols

        # The retention job (step 6) must be able to null the payload while
        # keeping the metadata row; 0001 declared this column NOT NULL.
        nullable = conn.execute(
            "SELECT is_nullable FROM information_schema.columns"
            " WHERE table_name = 'provider_calls' AND column_name = 'raw_response'"
        ).fetchone()
        assert nullable == ("YES",)


def test_0005_uniques_enforce_the_dedup_key(throwaway_db: str) -> None:
    """The two constraints step 6 exists to establish: one infringement per
    (user, url) — never collapsed across users — and one attestation per
    (infringement, provider)."""
    run_migrate(throwaway_db, "down", "--all")
    up = run_migrate(throwaway_db, "up")
    assert up.returncode == 0, up.stderr

    user_a = "00000000-0000-0000-0000-0000000000aa"
    user_b = "00000000-0000-0000-0000-0000000000bb"
    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO content_urls (url_hash, url, source_domain, canonical_url)"
            f" VALUES ('{'a' * 64}', 'https://x/p', 'x', 'https://x/p')"
        )
        insert = (
            "INSERT INTO infringements (user_ref, url_hash, page_url)"
            f" VALUES (%s, '{'a' * 64}', 'https://x/p') RETURNING infringement_id"
        )
        (inf_a,) = conn.execute(insert, (user_a,)).fetchone()  # type: ignore[misc]

        # Same URL, DIFFERENT user -> a second infringement. Cross-user is
        # never dedup; collapsing here would leak one person's matches.
        conn.execute(insert, (user_b,))
        (count,) = conn.execute("SELECT count(*) FROM infringements").fetchone()  # type: ignore[misc]
        assert count == 2

        # Same URL, SAME user -> rejected. A rescan must UPDATE, not INSERT.
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(insert, (user_a,))

    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        attest = (
            "INSERT INTO attestations (infringement_id, provider_id, score_kind,"
            " provider_score, score_version) VALUES (%s, 'hive', 'numeric', 0.87, 'v1')"
        )
        conn.execute(attest, (inf_a,))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(attest, (inf_a,))

    # Score shape carries over from 0004: neither a score nor a category is
    # not a storable attestation.
    with (
        psycopg.connect(throwaway_db, autocommit=True) as conn,
        pytest.raises(psycopg.errors.CheckViolation),
    ):
        conn.execute(
            "INSERT INTO attestations (infringement_id, provider_id, score_kind,"
            " score_version) VALUES (%s, 'google', 'numeric', 'v1')",
            (inf_a,),
        )


def test_0005_migrates_existing_search_matches_rows(throwaway_db: str) -> None:
    """Step-5 rows become one infringement per (user, url) with one
    attestation per provider, and pre-existing content_urls rows are labelled
    v0-interim — they were hashed by the raw-URL placeholder, and calling
    them v1 would be exactly the silent split the version column prevents."""
    run_migrate(throwaway_db, "down", "--all")
    up = run_migrate(throwaway_db, "up")
    assert up.returncode == 0, up.stderr
    back = run_migrate(throwaway_db, "down", "--steps", _steps_back_to("0004_"))
    assert back.returncode == 0, back.stderr

    user = "00000000-0000-0000-0000-0000000000aa"
    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO search_seeds (seed_id, user_ref, seed_kind, source_object_uri)"
            " VALUES ('00000000-0000-0000-0000-000000000001',"
            f"  '{user}', 'user_supplied', 'https://x/img.jpg')"
        )
        conn.execute(
            "INSERT INTO search_runs (run_id, seed_id, user_ref, providers_attempted,"
            " threshold_config) VALUES ('00000000-0000-0000-0000-000000000002',"
            " '00000000-0000-0000-0000-000000000001',"
            f" '{user}', '{{hive,google}}', '{{}}')"
        )
        conn.execute(
            "INSERT INTO content_urls (url_hash, url, source_domain)"
            f" VALUES ('{'a' * 64}', 'https://x/page', 'x')"
        )
        common = (
            "INSERT INTO search_matches (run_id, url_hash, user_ref, provider_id,"
            " image_url, page_url, score_version, band, score_kind, provider_score,"
            " provider_category) VALUES ('00000000-0000-0000-0000-000000000002',"
            f" '{'a' * 64}', '{user}', %s, 'https://x/img.jpg', 'https://x/page',"
            " 'v1', 'review', %s, %s, %s)"
        )
        conn.execute(common, ("hive", "numeric", "0.8712", None))
        conn.execute(common, ("google", "categorical", None, "full_match"))

    forward = run_migrate(throwaway_db, "up")
    assert forward.returncode == 0, forward.stderr

    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        infringements = conn.execute(
            "SELECT infringement_id, page_url, image_url, keyed_on, seen_count"
            " FROM infringements"
        ).fetchall()
        assert len(infringements) == 1  # two providers, ONE infringement
        assert infringements[0][1] == "https://x/page"
        assert infringements[0][2] == "https://x/img.jpg"
        assert infringements[0][3] == "page_url"
        assert infringements[0][4] == 2

        attestations = conn.execute(
            "SELECT provider_id, score_kind, provider_score, provider_category,"
            " confirm_count FROM attestations ORDER BY provider_id"
        ).fetchall()
        assert attestations == [
            ("google", "categorical", None, "full_match", 1),
            ("hive", "numeric", Decimal("0.8712"), None, 1),
        ]

        version = conn.execute(
            "SELECT normalisation_version, canonical_url FROM content_urls"
        ).fetchall()
        assert version == [("v0-interim", "https://x/page")]


def test_0006_drops_search_matches(throwaway_db: str) -> None:
    """The superseded table is gone and nothing references it. 0005 migrated
    its rows; keeping it would leave two competing sources of truth."""
    run_migrate(throwaway_db, "down", "--all")
    result = run_migrate(throwaway_db, "up")
    assert result.returncode == 0, result.stderr

    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        assert "search_matches" not in _table_names(conn)


CALIBRATION_TABLES = {
    "calibration_configs",
    "eval_items",
    "eval_observations",
    "eval_seed_coverage",
}


def test_0007_creates_calibration_tables(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with psycopg.connect(throwaway_db) as conn:
        assert _table_names(conn) >= CALIBRATION_TABLES


def test_0007_down_removes_them_and_the_added_columns(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    # 0007 itself must be REVERTED here (the calibration tables have to be
    # gone), so the target is the migration before it.
    run_migrate(throwaway_db, "down", "--steps", _steps_back_to("0006_"))
    with psycopg.connect(throwaway_db) as conn:
        assert CALIBRATION_TABLES & _table_names(conn) == set()
        cols = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'attestations'"
            ).fetchall()
        }
        assert "band" not in cols
        assert "calibration_version" not in cols


def test_0007_eval_item_without_consent_basis_is_rejected(throwaway_db: str) -> None:
    """Invariant: no eval item without a traceable consent basis. NOT NULL
    alone lets '' through, so the '\\S' CHECK is what actually enforces it.

    Includes chr(11) (vertical tab) and chr(12) (form feed): an earlier
    version of this constraint enumerated a trim charset (" \\t\\n\\r") that
    fixed tab/newline but still admitted these two — a test that pinned only
    the cases already known would not have caught that gap. chr(160)
    (non-breaking space) is deliberately NOT included here: Postgres's regex
    engine in this database's collation treats it as non-whitespace, so
    '\\S' does not reject it either. That is a known, documented boundary —
    see test_0007_consent_basis_regex_accepts_nbsp below and
    task-1-report.md — not something this test should paper over by silently
    expecting a rejection that doesn't happen.
    """
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with psycopg.connect(throwaway_db) as conn:
        for bad in ("", "   ", "\t\n", chr(11), chr(12)):
            with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
                conn.execute(
                    "INSERT INTO eval_items (eval_set_id, seed_uri, candidate_url,"
                    " label, label_kind, consent_basis, labelled_by)"
                    " VALUES ('v1', 's3://seed', 'https://x.test/a',"
                    " 'true_match', 'same_person', %s, 'tester')",
                    (bad,),
                )


def test_0007_consent_basis_regex_accepts_nbsp(throwaway_db: str) -> None:
    """Documents a known, deliberate gap rather than hiding it: a
    consent_basis of a single non-breaking space (chr(160)) is NOT rejected.
    Postgres's '\\S' (any non-whitespace character) does not treat U+00A0 as
    whitespace in this database's collation, so this insert succeeds. If a
    future migration tightens the check to close this gap, this test should
    start failing and be updated deliberately — it must not be deleted
    silently.
    """
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with psycopg.connect(throwaway_db) as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO eval_items (eval_set_id, seed_uri, candidate_url,"
                " label, label_kind, consent_basis, labelled_by)"
                " VALUES ('v1', 's3://seed', 'https://x.test/nbsp',"
                " 'true_match', 'same_person', %s, 'tester')",
                (chr(160),),
            )
        (stored,) = conn.execute(
            "SELECT consent_basis FROM eval_items WHERE candidate_url = 'https://x.test/nbsp'"
        ).fetchone()  # type: ignore[misc]
        assert stored == chr(160)


@pytest.mark.parametrize(
    ("label_kind", "label", "allowed"),
    [
        ("same_person", "true_match", True),
        ("same_person", "false_match", False),
        ("derived_edit", "true_match", True),
        ("derived_edit", "false_match", False),   # the inversion that must be impossible
        ("derived_edit", "uncertain", True),
        ("novel_generation", "true_match", True),
        ("novel_generation", "false_match", False),
        ("lookalike", "false_match", True),
        ("lookalike", "true_match", False),
        ("lookalike", "uncertain", True),
        ("unrelated", "false_match", True),
        ("unrelated", "true_match", False),
    ],
)
def test_0007_label_kind_and_label_must_agree(
    throwaway_db: str, label_kind: str, label: str, allowed: bool
) -> None:
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with psycopg.connect(throwaway_db) as conn:
        stmt = (
            "INSERT INTO eval_items (eval_set_id, seed_uri, candidate_url,"
            " label, label_kind, consent_basis, labelled_by)"
            " VALUES ('v1', 's3://seed', %s, %s, %s, 'team member, written consent', 'tester')"
        )
        url = f"https://x.test/{label_kind}-{label}"
        if allowed:
            with conn.transaction():
                conn.execute(stmt, (url, label, label_kind))
        else:
            with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
                conn.execute(stmt, (url, label, label_kind))


def test_0007_only_one_active_config_per_provider(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with psycopg.connect(throwaway_db) as conn:
        stmt = (
            "INSERT INTO calibration_configs (provider_id, version, score_kind, bands, active)"
            " VALUES ('hive', %s, 'numeric', '[]'::jsonb, true)"
        )
        with conn.transaction():
            conn.execute(stmt, ("v1",))
        with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
            conn.execute(stmt, ("v2",))
        # Inactive rows are unconstrained — many may coexist.
        with conn.transaction():
            conn.execute(
                "INSERT INTO calibration_configs (provider_id, version, score_kind, bands)"
                " VALUES ('hive', 'v3', 'numeric', '[]'::jsonb),"
                "        ('hive', 'v4', 'numeric', '[]'::jsonb)"
            )


# ── 0008: subjects and the seed FK ────────────────────────────────────────


def _columns(conn: psycopg.Connection[tuple[str]], table: str) -> set[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
        (table,),
    ).fetchall()
    return {row[0] for row in rows}


def _constraints(conn: psycopg.Connection[tuple[str]], table: str) -> set[str]:
    rows = conn.execute(
        "SELECT conname FROM pg_constraint WHERE conrelid = %s::regclass", (table,)
    ).fetchall()
    return {row[0] for row in rows}


def test_0008_creates_subjects_and_the_seed_fk(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with psycopg.connect(throwaway_db) as conn:
        assert "subjects" in _table_names(conn)
        assert "search_seeds_subject_fk" in _constraints(conn, "search_seeds")


def test_0008_backfills_existing_enrolments_and_seeds_as_adult(throwaway_db: str) -> None:
    """Everything enrolled before step 8 predates minor support: MIN_ENROLMENT_AGE
    was 18 and enrolment refused anyone younger, so every existing subject is an
    adult by construction. That is a statement about the old gate, not an
    assumption about the population.

    Exercised by rolling forward, back to 0007, inserting pre-step-8 rows, then
    running 0008 — the real upgrade path rather than a simulation of it.
    """
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    run_migrate(throwaway_db, "down", "--steps", _steps_back_to("0007_"))

    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        session = conn.execute(
            "INSERT INTO liveness_sessions (user_ref, provider_session_id, status,"
            " attempt_number, expires_at, consumed_at, completed_at)"
            " VALUES (gen_random_uuid(), 'ps-1', 'consumed', 1,"
            "         now() + interval '10 minutes', now(), now())"
            " RETURNING session_id, user_ref"
        ).fetchone()
        assert session is not None
        session_id, enrolled_ref = session
        conn.execute(
            "INSERT INTO enrolments (session_id, session_status, user_ref, collection_id,"
            " external_face_id, model_id, source_object_uri)"
            " VALUES (%s, 'consumed', %s, 'identity-v1', 'face-1', 'rekognition:7.0',"
            "         'https://s3/ref.jpg')",
            (session_id, enrolled_ref),
        )
        seeded = conn.execute(
            "INSERT INTO search_seeds (user_ref, seed_kind, source_object_uri)"
            " VALUES (gen_random_uuid(), 'user_supplied', 'https://s3/seed.jpg')"
            " RETURNING user_ref"
        ).fetchone()
        assert seeded is not None

    up = run_migrate(throwaway_db, "up")
    assert up.returncode == 0, up.stderr

    with psycopg.connect(throwaway_db) as conn:
        rows = conn.execute(
            "SELECT user_ref, discovery_eligible, eligibility_reason FROM subjects"
        ).fetchall()
    assert {(row[1], row[2]) for row in rows} == {(True, "adult")}
    assert {row[0] for row in rows} == {enrolled_ref, seeded[0]}


def test_0008_down_drops_the_table_and_the_constraint(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    run_migrate(throwaway_db, "down", "--steps", _steps_back_to("0007_"))
    with psycopg.connect(throwaway_db) as conn:
        assert "subjects" not in _table_names(conn)
        assert "search_seeds_subject_fk" not in _constraints(conn, "search_seeds")


# ── 0009: cost, breakers, cadence ─────────────────────────────────────────

PROVIDER_STEP_8_COLUMNS = {
    "cost_per_call_usd",
    "monthly_budget_usd",
    "rate_limit_per_min",
    "breaker_state",
    "breaker_opened_at",
    "breaker_reason",
    "breaker_consecutive_failures",
    "breaker_cooldown_seconds",
}
SEED_STEP_8_COLUMNS = {"scan_tier", "next_scan_after", "consecutive_empty_scans"}


def test_0009_adds_the_cost_and_cadence_columns(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with psycopg.connect(throwaway_db) as conn:
        assert "provider_spend" in _table_names(conn)
        assert _columns(conn, "providers") >= PROVIDER_STEP_8_COLUMNS
        assert _columns(conn, "search_seeds") >= SEED_STEP_8_COLUMNS
        # daily_budget_usd belongs to 0001; 0009 deliberately does not re-add it.
        assert "daily_budget_usd" in _columns(conn, "providers")


def test_0009_writes_the_real_google_cost_and_leaves_hive_null(throwaway_db: str) -> None:
    """Google Cloud Vision Web Detection is published list price. Hive Web Search
    is contract-priced and no measured figure exists anywhere in this repo, so
    the column stays NULL rather than holding a plausible-looking guess — a
    budget enforced against an unsourced number is worse than no budget, because
    the error only surfaces on an invoice."""
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with psycopg.connect(throwaway_db) as conn:
        costs = dict(
            conn.execute("SELECT provider_id, cost_per_call_usd FROM providers").fetchall()
        )
    assert costs["google"] == Decimal("0.003500")
    assert costs["hive"] is None


def test_0009_rejects_an_unknown_breaker_state(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with (
        psycopg.connect(throwaway_db) as conn,
        pytest.raises(psycopg.errors.CheckViolation),
        conn.transaction(),
    ):
        conn.execute("UPDATE providers SET breaker_state = 'tripped'")


def test_0009_rejects_an_unknown_scan_tier(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with psycopg.connect(throwaway_db) as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO subjects (user_ref, discovery_eligible, eligibility_reason)"
                " VALUES ('11111111-1111-1111-1111-111111111111', true, 'adult')"
            )
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute(
                "INSERT INTO search_seeds (user_ref, seed_kind, source_object_ref, scan_tier)"
                " VALUES ('11111111-1111-1111-1111-111111111111', 'user_supplied',"
                "         'https://s3/x.jpg', 'hourly')"
            )


def test_0009_rejects_an_unknown_provider_call_status(throwaway_db: str) -> None:
    """providers_succeeded is derived from this column, so a typo'd status
    silently becomes a not-ok row — which reads downstream as a provider
    outage, i.e. as coverage we did not have."""
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with (
        psycopg.connect(throwaway_db) as conn,
        pytest.raises(psycopg.errors.CheckViolation),
        conn.transaction(),
    ):
        conn.execute(
            "INSERT INTO provider_calls (provider_id, status, raw_response)"
            " VALUES ('hive', 'sort_of_ok', '{}'::jsonb)"
        )


def test_0009_down_removes_them_and_keeps_daily_budget(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    run_migrate(throwaway_db, "down", "--steps", _steps_back_to("0008_"))
    with psycopg.connect(throwaway_db) as conn:
        assert "provider_spend" not in _table_names(conn)
        assert _columns(conn, "providers") & PROVIDER_STEP_8_COLUMNS == set()
        assert _columns(conn, "search_seeds") & SEED_STEP_8_COLUMNS == set()
        # 0001's column survives: dropping it here would make this migration
        # destructive to something it never created.
        assert "daily_budget_usd" in _columns(conn, "providers")


# ── 0010: the consent reference on enrolments ─────────────────────────────

# Reserved: the proxy must never issue it. It exists only so NOT NULL could be
# applied to rows written before consent was required.
SENTINEL_CONSENT_REF = "00000000-0000-0000-0000-000000000000"

CONSENT_COLUMNS = {"consent_ref", "consent_document_sha256", "consent_signed_at"}

_INSERT_CONSUMED_SESSION = (
    "INSERT INTO liveness_sessions (user_ref, provider_session_id, status,"
    " attempt_number, expires_at, consumed_at, completed_at)"
    " VALUES (gen_random_uuid(), gen_random_uuid()::text, 'consumed', 1,"
    "         now() + interval '10 minutes', now(), now())"
    " RETURNING session_id, user_ref"
)

_INSERT_ENROLMENT_WITH_CONSENT = (
    "INSERT INTO enrolments (session_id, session_status, user_ref, collection_id,"
    " external_face_id, model_id, source_object_uri,"
    " consent_ref, consent_document_sha256, consent_signed_at)"
    " VALUES (%s, 'consumed', %s, 'identity-v1', %s, 'rekognition:7.0',"
    "         'https://proxy-s3.example/ref.jpg', %s, 'sha256:abc', now())"
)


def _consumed_session(conn: psycopg.Connection[tuple[Any, ...]]) -> tuple[Any, Any]:
    row = conn.execute(_INSERT_CONSUMED_SESSION).fetchone()
    assert row is not None
    return row[0], row[1]


def test_0010_adds_the_consent_columns_as_not_null(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with psycopg.connect(throwaway_db) as conn:
        assert _columns(conn, "enrolments") >= CONSENT_COLUMNS
        nullability = conn.execute(
            "SELECT is_nullable FROM information_schema.columns"
            " WHERE table_name = 'enrolments' AND column_name = ANY(%s)",
            (sorted(CONSENT_COLUMNS),),
        ).fetchall()
    # NOT NULL is the whole point: INVARIANTS #2's second half was previously
    # enforced nowhere, so a consent-free enrolment was writable.
    assert {row[0] for row in nullability} == {"NO"}


def test_0010_rejects_an_enrolment_with_no_consent_ref(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        session_id, user_ref = _consumed_session(conn)
        with pytest.raises(psycopg.errors.NotNullViolation):
            conn.execute(
                "INSERT INTO enrolments (session_id, session_status, user_ref,"
                " collection_id, external_face_id, model_id, source_object_uri)"
                " VALUES (%s, 'consumed', %s, 'identity-v1', 'face-1',"
                "         'rekognition:7.0', 'https://proxy-s3.example/ref.jpg')",
                (session_id, user_ref),
            )


def test_0010_rejects_a_fresh_enrolment_carrying_the_sentinel(throwaway_db: str) -> None:
    """Done-when: the sentinel is refused by the DATABASE, not by application
    code — so this asserts it with raw SQL, with no application code in the
    path at all. The sentinel is a migration artifact; it is not a valid state
    going forward, and an app-layer check alone would not survive a new writer.
    """
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        session_id, user_ref = _consumed_session(conn)
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                _INSERT_ENROLMENT_WITH_CONSENT,
                (session_id, user_ref, "face-1", SENTINEL_CONSENT_REF),
            )


def test_0010_accepts_a_real_consent_ref(throwaway_db: str) -> None:
    """The CHECK must reject only the sentinel — a normal enrolment still writes."""
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        session_id, user_ref = _consumed_session(conn)
        conn.execute(
            _INSERT_ENROLMENT_WITH_CONSENT,
            (session_id, user_ref, "face-1", "11111111-2222-3333-4444-555555555555"),
        )
        stored = conn.execute(
            "SELECT consent_ref::text, consent_document_sha256 FROM enrolments"
        ).fetchone()
    assert stored == ("11111111-2222-3333-4444-555555555555", "sha256:abc")


def test_0010_backfills_pre_consent_rows_with_the_sentinel(throwaway_db: str) -> None:
    """The real upgrade path: roll back to 0009, write an enrolment from before
    consent was required, roll forward. The row must survive — dropping it
    would destroy a biometric enrolment (CLAUDE.md §5, never DELETE) — carrying
    the sentinel and its own created_at as consent_signed_at."""
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    run_migrate(throwaway_db, "down", "--steps", _steps_back_to("0009_"))

    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        session_id, user_ref = _consumed_session(conn)
        conn.execute(
            "INSERT INTO enrolments (session_id, session_status, user_ref,"
            " collection_id, external_face_id, model_id, source_object_uri)"
            " VALUES (%s, 'consumed', %s, 'identity-v1', 'face-1',"
            "         'rekognition:7.0', 'https://proxy-s3.example/ref.jpg')",
            (session_id, user_ref),
        )

    up = run_migrate(throwaway_db, "up")
    assert up.returncode == 0, up.stderr

    with psycopg.connect(throwaway_db) as conn:
        row = conn.execute(
            "SELECT consent_ref::text, consent_document_sha256,"
            " consent_signed_at = created_at FROM enrolments"
        ).fetchone()
    assert row == (SENTINEL_CONSENT_REF, "PRE_CONSENT_TEST_DATA", True)


def test_0010_down_removes_the_consent_columns(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    run_migrate(throwaway_db, "down", "--steps", _steps_back_to("0009_"))
    with psycopg.connect(throwaway_db) as conn:
        assert _columns(conn, "enrolments") & CONSENT_COLUMNS == set()
        assert "enrolments_consent_not_sentinel" not in _constraints(conn, "enrolments")


# ── 0011: durable seed ref vs per-run presigned URL ───────────────────────


def _column_comment(
    conn: psycopg.Connection[tuple[Any, ...]], table: str, column: str
) -> str | None:
    row = conn.execute(
        "SELECT col_description(%s::regclass, ordinal_position)"
        " FROM information_schema.columns"
        " WHERE table_name = %s AND column_name = %s",
        (table, table, column),
    ).fetchone()
    return None if row is None else row[0]


def test_0011_renames_the_seed_column_and_adds_the_run_url(throwaway_db: str) -> None:
    """The expiring thing moves onto the expiring object.

    A presigned URL is a short-lived credential (SigV4 caps at 7 days), not a
    durable identifier. Stored on the seed it works in week 1 and 403s forever
    after — surfacing as "the provider is failing", not "our URLs expired".
    """
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with psycopg.connect(throwaway_db) as conn:
        seed_columns = _columns(conn, "search_seeds")
        assert "source_object_ref" in seed_columns
        assert "source_object_uri" not in seed_columns
        assert "seed_url" in _columns(conn, "search_runs")
        nullable = conn.execute(
            "SELECT is_nullable FROM information_schema.columns"
            " WHERE table_name = 'search_runs' AND column_name = 'seed_url'"
        ).fetchone()
        assert nullable == ("NO",)
        # enrolments.source_object_uri is a DIFFERENT column and must not move:
        # it is the ReferenceImage pointer, and INVARIANTS #9 names it.
        assert "source_object_uri" in _columns(conn, "enrolments")


def test_0011_documents_that_the_ref_is_never_a_presigned_url(throwaway_db: str) -> None:
    """The comment is the durable warning. Someone will look at a TEXT column
    holding an object key and reach for a URL; the column has to say no."""
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with psycopg.connect(throwaway_db) as conn:
        comment = _column_comment(conn, "search_seeds", "source_object_ref")
    assert comment is not None
    assert "presigned" in comment.lower()


def test_0011_backfills_existing_runs_with_an_empty_seed_url(throwaway_db: str) -> None:
    """The real upgrade path. Existing runs cannot be repaired — a presigned URL
    cannot be turned back into an object key — so they get '' and the proxy
    re-enqueues. The rows survive; nothing is silently rewritten to look valid.
    """
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    run_migrate(throwaway_db, "down", "--steps", _steps_back_to("0010_"))

    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        user_ref = conn.execute(
            "INSERT INTO subjects (user_ref, discovery_eligible, eligibility_reason)"
            " VALUES (gen_random_uuid(), true, 'adult') RETURNING user_ref"
        ).fetchone()
        assert user_ref is not None
        seed = conn.execute(
            "INSERT INTO search_seeds (user_ref, seed_kind, source_object_uri)"
            " VALUES (%s, 'user_supplied',"
            "         'https://s3/seed.jpg?X-Amz-Signature=deadbeef')"
            " RETURNING seed_id",
            (user_ref[0],),
        ).fetchone()
        assert seed is not None
        conn.execute(
            "INSERT INTO search_runs (seed_id, user_ref, providers_attempted,"
            " threshold_config) VALUES (%s, %s, '{hive}', '{}')",
            (seed[0], user_ref[0]),
        )

    up = run_migrate(throwaway_db, "up")
    assert up.returncode == 0, up.stderr

    with psycopg.connect(throwaway_db) as conn:
        assert conn.execute("SELECT seed_url FROM search_runs").fetchone() == ("",)
        # The dead presigned URL is preserved verbatim under the new name, not
        # salvaged and not deleted: the count of these is what gets reported so
        # the proxy knows which seeds to re-register.
        assert conn.execute(
            "SELECT source_object_ref FROM search_seeds"
        ).fetchone() == ("https://s3/seed.jpg?X-Amz-Signature=deadbeef",)


def test_0011_down_restores_the_seed_column_and_drops_the_run_url(
    throwaway_db: str,
) -> None:
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    run_migrate(throwaway_db, "down", "--steps", _steps_back_to("0010_"))
    with psycopg.connect(throwaway_db) as conn:
        assert "source_object_uri" in _columns(conn, "search_seeds")
        assert "source_object_ref" not in _columns(conn, "search_seeds")
        assert "seed_url" not in _columns(conn, "search_runs")


# -- 0012: user feedback on a hit ------------------------------------------


def test_0012_creates_the_feedback_table(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with psycopg.connect(throwaway_db) as conn:
        assert "infringement_feedback" in _table_names(conn)
        assert _columns(conn, "infringement_feedback") == {
            "feedback_id",
            "infringement_id",
            "user_ref",
            "signal",
            "created_at",
        }


def test_0012_rejects_an_unknown_signal(throwaway_db: str) -> None:
    """The vocabulary is fixed at the database. A typo'd signal that inserted
    cleanly would be counted by a reviewer as neither agreement nor
    disagreement, silently."""
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        infringement_id, user_ref = _seed_infringement(conn)
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO infringement_feedback (infringement_id, user_ref, signal)"
                " VALUES (%s, %s, 'definitely_not_me')",
                (infringement_id, user_ref),
            )


def test_0012_feedback_is_append_only_in_shape(throwaway_db: str) -> None:
    """Two contradictory signals coexist. Nothing in the schema forces one row
    per (infringement, user) -- a user changing their mind is the history, and
    a UNIQUE here would destroy the part a reviewer needs."""
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        infringement_id, user_ref = _seed_infringement(conn)
        for signal in ("not_me", "confirmed"):
            conn.execute(
                "INSERT INTO infringement_feedback (infringement_id, user_ref, signal)"
                " VALUES (%s, %s, %s)",
                (infringement_id, user_ref, signal),
            )
        rows = conn.execute(
            "SELECT signal FROM infringement_feedback WHERE infringement_id = %s",
            (infringement_id,),
        ).fetchall()
    assert {row[0] for row in rows} == {"not_me", "confirmed"}


def test_0012_down_drops_the_table(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    run_migrate(throwaway_db, "down", "--steps", _steps_back_to("0011_"))
    with psycopg.connect(throwaway_db) as conn:
        assert "infringement_feedback" not in _table_names(conn)


def _seed_infringement(conn: psycopg.Connection[tuple[Any, ...]]) -> tuple[Any, Any]:
    """One content_url + one infringement, the minimum a feedback row needs."""
    user_ref = conn.execute(
        "INSERT INTO subjects (user_ref, discovery_eligible, eligibility_reason)"
        " VALUES (gen_random_uuid(), true, 'adult') RETURNING user_ref"
    ).fetchone()
    assert user_ref is not None
    conn.execute(
        "INSERT INTO content_urls (url_hash, url, source_domain)"
        " VALUES ('h' || repeat('a', 63), 'https://example.test/p', 'example.test')"
        " ON CONFLICT DO NOTHING"
    )
    row = conn.execute(
        "INSERT INTO infringements (user_ref, url_hash, page_url)"
        " VALUES (%s, 'h' || repeat('a', 63), 'https://example.test/p')"
        " RETURNING infringement_id",
        (user_ref[0],),
    ).fetchone()
    assert row is not None
    return row[0], user_ref[0]


# -- 0013: the recheck loop's attempt clock --------------------------------


def test_0013_adds_last_attempted_at_and_the_due_index(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with psycopg.connect(throwaway_db) as conn:
        assert "last_attempted_at" in _columns(conn, "infringements")
        # last_checked_at survives untouched: the two mean different things.
        assert "last_checked_at" in _columns(conn, "infringements")
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'infringements'"
            ).fetchall()
        }
    assert "infringements_recheck_due_idx" in indexes


def test_0013_leaves_existing_rows_unattempted(throwaway_db: str) -> None:
    """NULL, not now(). Backfilling a timestamp would tell the loop every
    pre-existing infringement had already been tried, and the first pass would
    skip the entire corpus."""
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    run_migrate(throwaway_db, "down", "--steps", _steps_back_to("0012_"))

    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        _seed_infringement(conn)

    up = run_migrate(throwaway_db, "up")
    assert up.returncode == 0, up.stderr

    with psycopg.connect(throwaway_db) as conn:
        row = conn.execute(
            "SELECT last_attempted_at, url_alive FROM infringements"
        ).fetchone()
    assert row == (None, True)


def test_0013_down_removes_the_column_and_the_index(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    run_migrate(throwaway_db, "down", "--steps", _steps_back_to("0012_"))
    with psycopg.connect(throwaway_db) as conn:
        assert "last_attempted_at" not in _columns(conn, "infringements")
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'infringements'"
            ).fetchall()
        }
    assert "infringements_recheck_due_idx" not in indexes


# -- 0014: attribution ------------------------------------------------------


def test_0014_creates_the_attribution_tables(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with psycopg.connect(throwaway_db) as conn:
        tables = _table_names(conn)
        assert {"attribution_runs", "attributed_faces"} <= tables
        assert "attributed_face_id" in _columns(conn, "search_seeds")


def test_0014_rejects_a_score_without_a_person_and_a_person_without_a_score(
    throwaway_db: str,
) -> None:
    """The CHECK pairs them. A match score with no person, or a person with no
    score, is a bug that should fail at the insert rather than become an
    unreadable row later — they are DIFFERENT quantities from
    detect_confidence and each other."""
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        run_id = _attribution_run(conn)
        for resolved, score in (("gen_random_uuid()", "NULL"), ("NULL", "99.1")):
            with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
                conn.execute(
                    "INSERT INTO attributed_faces (run_id, face_index, bbox,"
                    " detect_confidence, resolved_user_ref, match_score, model_id)"
                    f" VALUES (%s, 0, '{{}}'::jsonb, 99.9, {resolved}, {score},"
                    "         'rekognition:7.0')",
                    (run_id,),
                )


def test_0014_allows_an_unattributed_face(throwaway_db: str) -> None:
    """NULL/NULL is the COMMON case — most faces in most photos belong to
    people who are not enrolled, and the bbox is stored for every one of them."""
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        run_id = _attribution_run(conn)
        conn.execute(
            "INSERT INTO attributed_faces (run_id, face_index, bbox,"
            " detect_confidence, model_id)"
            " VALUES (%s, 0, '{\"x\":0.1,\"y\":0.2,\"w\":0.3,\"h\":0.4}'::jsonb,"
            "         99.9, 'rekognition:7.0')",
            (run_id,),
        )
        row = conn.execute(
            "SELECT resolved_user_ref, match_score, bbox FROM attributed_faces"
        ).fetchone()
    assert row is not None
    assert row[0] is None and row[1] is None
    assert row[2] == {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}


def test_0014_one_row_per_face_index_per_run(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        run_id = _attribution_run(conn)
        stmt = (
            "INSERT INTO attributed_faces (run_id, face_index, bbox,"
            " detect_confidence, model_id)"
            " VALUES (%s, 0, '{}'::jsonb, 99.9, 'rekognition:7.0')"
        )
        conn.execute(stmt, (run_id,))
        with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
            conn.execute(stmt, (run_id,))


def test_0014_down_removes_everything_and_keeps_the_seeds(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    run_migrate(throwaway_db, "down", "--steps", _steps_back_to("0013_"))
    with psycopg.connect(throwaway_db) as conn:
        assert {"attribution_runs", "attributed_faces"} & _table_names(conn) == set()
        assert "search_seeds" in _table_names(conn)
        assert "attributed_face_id" not in _columns(conn, "search_seeds")


def _attribution_run(conn: psycopg.Connection[tuple[Any, ...]]) -> Any:
    row = conn.execute(
        "INSERT INTO attribution_runs (photo_ref, requested_by, candidate_count,"
        " match_threshold, max_candidates, model_id)"
        " VALUES ('photo-1', gen_random_uuid(), 2, 92.0, 5, 'rekognition:7.0')"
        " RETURNING run_id"
    ).fetchone()
    assert row is not None
    return row[0]


# -- 0015: per-module database roles ----------------------------------------

MODULE_ROLES = {"identity_rw", "search_rw", "calibration_rw", "audit_w"}


def _roles(conn: psycopg.Connection[tuple[Any, ...]]) -> set[str]:
    rows = conn.execute(
        "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)", (sorted(MODULE_ROLES),)
    ).fetchall()
    return {row[0] for row in rows}


def test_0015_creates_every_module_role(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with psycopg.connect(throwaway_db) as conn:
        assert _roles(conn) == MODULE_ROLES


def test_0015_audit_w_can_insert_but_never_update_or_delete(throwaway_db: str) -> None:
    """The step-9 done-when: `UPDATE audit_log` fails under the application
    role. An audit log an application can edit is not an audit log."""
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        conn.execute("SET ROLE audit_w")
        conn.execute("INSERT INTO audit_log (actor_type, action) VALUES ('system', 't')")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE audit_log SET action = 'changed'")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM audit_log")
        conn.execute("RESET ROLE")


def test_0015_identity_cannot_touch_search_tables(throwaway_db: str) -> None:
    """Least privilege is only real if it refuses something. The identity role
    holds the biometric path; discovery data is not its business."""
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        conn.execute("SET ROLE identity_rw")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM infringements")
        conn.execute("RESET ROLE")


def test_0015_search_cannot_read_the_enrolment_table(throwaway_db: str) -> None:
    """The one that matters most. `enrolments` binds a face vector to a person;
    the discovery path has no reason to read it, and the role says so."""
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        conn.execute("SET ROLE search_rw")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT external_face_id FROM enrolments")
        # ...but the tables the out-of-band tasks added ARE its business.
        conn.execute("SELECT count(*) FROM infringement_feedback")
        conn.execute("SELECT count(*) FROM attributed_faces")
        conn.execute("RESET ROLE")


def test_0015_no_role_can_edit_the_migration_ledger(throwaway_db: str) -> None:
    """A role that could edit schema_migrations could make an applied migration
    look unapplied — and the next deploy would re-run it."""
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        for role in sorted(MODULE_ROLES):
            conn.execute(f"SET ROLE {role}")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("DELETE FROM schema_migrations")
            conn.execute("RESET ROLE")


# ── 0019: the stub provider, registered and disabled ──────────────────────

_STUB_PROVIDER_COLUMNS = (
    "kind, enabled, calibrated, score_version, cost_per_call_usd,"
    " score_kind, score_domain"
)


def _stub_provider_row(conn: psycopg.Connection[tuple[Any, ...]]) -> tuple[Any, ...] | None:
    return conn.execute(
        f"SELECT {_STUB_PROVIDER_COLUMNS} FROM providers WHERE provider_id = 'stub'"
    ).fetchone()


def test_0019_seeds_the_stub_provider_disabled_and_uncalibrated(throwaway_db: str) -> None:
    """SEARCH_PROVIDER=stub builds StubSearchProvider (src/imageshield/search/stub.py),
    but before this migration ``providers`` held only hive and google — a dev run
    dispatched against the enabled ids (hive, google), found no matching adapter
    for either under that setting, and every provider_calls row read
    'no adapter registered for this provider'.

    enabled=false is the safety-critical value here, not a placeholder: 0016's
    ``svc.v_person_report_summary.monitored_sources`` counts providers that
    succeeded AND are enabled, so an enabled stub would claim a source that was
    never actually searched (CLAUDE.md §7.5). calibrated=false keeps it on the
    §7.3 review-only path even though the adapter never produces a score to band
    in the first place.
    """
    run_migrate(throwaway_db, "down", "--all")
    up = run_migrate(throwaway_db, "up")
    assert up.returncode == 0, up.stderr

    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        row = _stub_provider_row(conn)

    assert row is not None
    (kind, enabled, calibrated, score_version, cost_per_call_usd, score_kind, score_domain) = row
    # image_search, not face_search: matches StubSearchProvider.kind exactly
    # (CLAUDE.md §7.1) — claiming face_search would assert coverage the stub
    # does not provide.
    assert kind == "image_search"
    assert enabled is False
    assert calibrated is False
    # Identifies the row as the stub, never a real provider's version string,
    # and matches StubSearchProvider.score_version so adapter and row cannot
    # drift apart on what actually produced it.
    assert score_version == "stub-no-op-v1"
    assert score_kind == "numeric"
    # No measured domain: the stub never reports a score to have one, and a
    # fabricated range would be exactly the adapter-level fabrication
    # CLAUDE.md §7.2 forbids, applied to the config row instead. NULL also
    # doubles as a second guard — bands.py rule 3c ("score_domain_unknown")
    # still refuses anything above `review` if `calibrated` were ever
    # hand-flipped to true.
    assert score_domain is None
    assert cost_per_call_usd == Decimal("0")


def test_0019_stub_cost_is_zero_not_null(throwaway_db: str) -> None:
    """A NULL cost_per_call_usd fails the budget guard CLOSED (invariant #38):
    a provider that cannot be priced is skipped rather than dispatched. The stub
    makes no network call at all (search/stub.py), so 0 — not 'unknown' — is the
    honest figure; a NULL here would make the one free provider refusable by its
    own budget check the moment anyone set a cap."""
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        row = conn.execute(
            "SELECT cost_per_call_usd FROM providers WHERE provider_id = 'stub'"
        ).fetchone()
    assert row is not None
    assert row[0] is not None
    assert row[0] == Decimal("0")


def test_0021_seeded_providers_are_exactly_hive_google_stub_and_rekognition_confirm(
    throwaway_db: str,
) -> None:
    # Was "...exactly hive, google, and stub" until 0021 added a fourth row;
    # renamed along with the assertion rather than left to describe a set the
    # migrated schema no longer produces.
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        ids = {row[0] for row in conn.execute("SELECT provider_id FROM providers").fetchall()}
    assert ids == {"hive", "google", "stub", "rekognition_confirm"}


def test_0019_down_deletes_only_the_stub_row(throwaway_db: str) -> None:
    """The reversal must not touch 0004's hive/google rows."""
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    down = run_migrate(throwaway_db, "down", "--steps", _steps_back_to("0018_"))
    assert down.returncode == 0, down.stderr
    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        ids = {row[0] for row in conn.execute("SELECT provider_id FROM providers").fetchall()}
    assert ids == {"hive", "google"}


def test_0019_up_down_up_round_trip_restores_the_identical_stub_row(
    throwaway_db: str,
) -> None:
    run_migrate(throwaway_db, "down", "--all")
    first_up = run_migrate(throwaway_db, "up")
    assert first_up.returncode == 0, first_up.stderr
    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        before = _stub_provider_row(conn)
    assert before is not None

    down = run_migrate(throwaway_db, "down", "--all")
    assert down.returncode == 0, down.stderr
    second_up = run_migrate(throwaway_db, "up")
    assert second_up.returncode == 0, second_up.stderr

    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        after = _stub_provider_row(conn)
    assert after == before


# ── 0021: confirm & review schema ─────────────────────────────────────────


def test_0021_confirm_columns_and_review_tasks(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    up = run_migrate(throwaway_db, "up")
    assert up.returncode == 0, up.stderr
    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        cols = {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'infringements'"
            ).fetchall()
        }
        assert {
            "confirm_state", "severity", "confirm_decided_by", "confirm_decided_at",
            "duplicate_of", "phash", "face_match_score", "moderation_labels",
        } <= cols
        # confirmed without a human is a constraint violation (INVARIANTS #19 by schema)
        conn.execute(
            "INSERT INTO subjects (user_ref, discovery_eligible, eligibility_reason)"
            " VALUES ('00000000-0000-0000-0000-000000000001', true, 'adult')"
        )
        conn.execute(
            "INSERT INTO content_urls (url_hash, url, source_domain) VALUES"
            " (repeat('a', 64), 'https://x.example/a', 'x.example')"
        )
        conn.execute(
            "INSERT INTO infringements (user_ref, url_hash, page_url) VALUES"
            " ('00000000-0000-0000-0000-000000000001', repeat('a', 64), 'https://x.example/a')"
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "UPDATE infringements SET confirm_state = 'confirmed'"
            )


def test_0021_seeds_the_rekognition_confirm_provider(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    assert run_migrate(throwaway_db, "up").returncode == 0
    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        row = conn.execute(
            "SELECT kind, enabled, calibrated, cost_per_call_usd FROM providers"
            " WHERE provider_id = 'rekognition_confirm'"
        ).fetchone()
    assert row is not None
    assert row[0] == "classifier"
    assert row[1] is True
    assert row[2] is False
    assert row[3] == Decimal("0.005")


# ── 0022: protection score, recommendations, threat events ─────────────────


def test_0022_score_tables_and_role(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    assert run_migrate(throwaway_db, "up").returncode == 0
    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema = 'public'"
            ).fetchall()
        }
        assert {
            "protection_scores", "score_events", "recommendations",
            "threat_events", "threat_event_matches",
        } <= tables
        assert conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = 'score_rw'"
        ).fetchone() is not None


def test_0022_score_events_is_insert_only_for_score_rw(throwaway_db: str) -> None:
    """An editable journal is not a journal — same shape as the audit_log test."""
    run_migrate(throwaway_db, "down", "--all")
    assert run_migrate(throwaway_db, "up").returncode == 0
    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO subjects (user_ref, discovery_eligible, eligibility_reason)"
            " VALUES ('00000000-0000-0000-0000-000000000002', true, 'adult')"
        )
        conn.execute("SET ROLE score_rw")
        conn.execute(
            "INSERT INTO score_events"
            " (user_ref, delta, component, cause_kind, config_version, score_after)"
            " VALUES ('00000000-0000-0000-0000-000000000002', 5, 'posture',"
            "         'initialised', 'score-v1', 5)"
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE score_events SET delta = 100")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM score_events")
        conn.execute("RESET ROLE")



def test_0025_audit_w_can_read_audit_log_but_still_cannot_edit_it(throwaway_db: str) -> None:
    """The two reads the 2026-08-21 push added, exercised under the application
    role rather than the owner.

    Both queries passed in CI while being impossible in dev, because the suite
    connects as the database owner and the owner reads everything. So the part
    of this test that carries the weight is ``SET ROLE``, not the SQL: without
    it, it would go green against a missing grant exactly as the rest of the
    suite did. The queries are imported from the modules that ship them — a
    copy retyped here would drift from the code this test exists to protect.
    """
    run_migrate(throwaway_db, "down", "--all")
    assert run_migrate(throwaway_db, "up").returncode == 0
    user_ref = uuid4()
    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        conn.execute("SET ROLE audit_w")

        # The endpoint's own order: audit the render (INVARIANTS #31), then
        # count renders in the window (#32). INSERT was always granted; the
        # count is what 0025 adds.
        conn.execute(
            _RECORD_RENDER_SQL,
            {
                "action": PREVIEW_RENDERED_ACTION,
                "user_ref": user_ref,
                "infringement_id": uuid4(),
                "metadata": Jsonb({"reveal": False}),
            },
        )
        row = conn.execute(_COUNT_RENDERS_SQL, {"user_ref": user_ref}).fetchone()
        assert row is not None and row[0] == 1

        # The console's observer feed, same grant, different action filter.
        conn.execute(_SUBJECT_DECISIONS_SQL, {"limit": 10})

        # The read must not have brought write access with it. 0015's
        # append-only property is what makes this an audit log at all, and
        # 0025's whole claim is that SELECT does not touch it.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE audit_log SET action = 'changed'")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM audit_log")
        conn.execute("RESET ROLE")


def test_0025_down_returns_audit_log_to_write_only(throwaway_db: str) -> None:
    """Reversibility, and the failure mode this migration fixes: with 0025
    reverted the ceiling query is a permission error again, which is what dev
    was doing on every preview call."""
    run_migrate(throwaway_db, "down", "--all")
    assert run_migrate(throwaway_db, "up").returncode == 0
    assert run_migrate(throwaway_db, "down", "--steps", "1").returncode == 0
    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        conn.execute("SET ROLE audit_w")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(_COUNT_RENDERS_SQL, {"user_ref": uuid4()})
        conn.execute("RESET ROLE")

# ── scripts/migrate.py's own DATABASE_URL-from-parts fallback ─────────────
#
# The migration ECS task never goes through imageshield.config.Config (see
# scripts/migrate.py's _database_url() docstring) — it has its OWN, smaller
# copy of the "compose from DB_* parts" fallback, sharing only
# imageshield.db.dsn.compose_database_url with Config. These two tests are
# unlike tests.db.run_migrate, which always sets DATABASE_URL directly and so
# never exercises this path. Both failure cases exit via SystemExit inside
# _database_url() itself, before scripts/migrate.py ever calls
# psycopg.connect(...), so neither needs a reachable database.

_MIGRATE_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate.py"
_PASSTHROUGH_ENV_KEYS = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP", "PATHEXT", "COMSPEC")


def _run_migrate_script(
    env_overrides: dict[str, str], tmp_path: Path
) -> subprocess.CompletedProcess[str]:
    env = {key: os.environ[key] for key in _PASSTHROUGH_ENV_KEYS if key in os.environ}
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(_MIGRATE_SCRIPT), "up", "--dry-run"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        timeout=60,
    )


def test_migrate_script_refuses_with_neither_database_url_nor_parts(tmp_path: Path) -> None:
    result = _run_migrate_script({}, tmp_path)
    assert result.returncode != 0
    assert "DATABASE_URL" in result.stderr


def test_migrate_script_refuses_with_partial_parts_naming_the_gap(tmp_path: Path) -> None:
    result = _run_migrate_script(
        {"DB_HOST": "db.internal", "DB_PORT": "5432", "DB_NAME": "imageshield", "DB_USER": "app"},
        tmp_path,
    )
    assert result.returncode != 0
    assert "DB_PASSWORD" in result.stderr
