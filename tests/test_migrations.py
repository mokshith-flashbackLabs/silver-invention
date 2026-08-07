"""Migration 0001 tests, run against a real, disposable Postgres database.

Every up/down call goes through ``tests.db.run_migrate``, which shells out to
the real ``scripts/migrate.py`` CLI — the artifact under test, not a
reimplementation of its transaction/checksum logic. Each test starts with an
explicit ``down --all`` (a no-op on an already-empty database) so tests don't
depend on execution order, even though they share one throwaway database for
the session.
"""

from __future__ import annotations

from decimal import Decimal

import psycopg
import pytest

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
    back = run_migrate(throwaway_db, "down", "--steps", _steps_back_to_0004())
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


def _steps_back_to_0004() -> str:
    """How many `down --steps N` to land just after 0004.

    Computed from the migrations directory rather than hardcoded, so adding
    0007 doesn't silently turn this into "revert 0006 and stop".
    """
    from tests.db import ROOT

    later = [p for p in (ROOT / "migrations").glob("*.up.sql") if p.name >= "0005"]
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
    back = run_migrate(throwaway_db, "down", "--steps", _steps_back_to_0004())
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
        assert CALIBRATION_TABLES <= _table_names(conn)


def test_0007_down_removes_them_and_the_added_columns(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    run_migrate(throwaway_db, "down", "--steps", "1")
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
            with pytest.raises(psycopg.errors.CheckViolation):
                with conn.transaction():
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
            with pytest.raises(psycopg.errors.CheckViolation):
                with conn.transaction():
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
        with pytest.raises(psycopg.errors.UniqueViolation):
            with conn.transaction():
                conn.execute(stmt, ("v2",))
        # Inactive rows are unconstrained — many may coexist.
        with conn.transaction():
            conn.execute(
                "INSERT INTO calibration_configs (provider_id, version, score_kind, bands)"
                " VALUES ('hive', 'v3', 'numeric', '[]'::jsonb),"
                "        ('hive', 'v4', 'numeric', '[]'::jsonb)"
            )
