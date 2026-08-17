"""The `svc` contract views, against real Postgres.

These four views are a **versioned contract with another repo**: the proxy's
``src/services/contract/readers.ts`` selects from them and its own
``v_covered_persons`` JOINs against ``v_person_enrolment_state``. Columns may be
added; none may be removed or retyped without a coordinated deploy. So the
column *names* are asserted explicitly here rather than inferred — a rename that
passes every other test in this suite still breaks their reader, and this file is
the only place that can catch it.

Direct SQL for the fixtures throughout. Threading a liveness session, an
enrolment, a provider run and an attribution through the real code paths would
make these tests about those paths; what is under test is a projection.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from imageshield.http.svc_contract import EXPECTED_VIEWS
from tests.db import run_migrate

VIEWS = (
    "v_person_enrolment_state",
    "v_person_report_summary",
    "v_person_hits",
    "v_person_liveness_attempts",
)

# Derived, not transcribed: src/imageshield/http/svc_contract.py is the single
# source of truth for the contract's shape, and /readyz refuses to come up when
# the database disagrees with it. A second literal list here would be a second
# copy of a cross-repo contract — CLAUDE.md §9. Order is still not part of the
# contract (their reader selects by name); presence and spelling are.
EXPECTED_COLUMNS: dict[str, set[str]] = {
    view: set(columns) for view, columns in EXPECTED_VIEWS.items()
}


@pytest.fixture
def migrated_db(throwaway_db: str) -> str:
    # The down leg is asserted too, not just run. A `down --all` that fails
    # leaves the previous test's rows in place and the next assertion counts
    # them, which presents as a mysterious off-by-N somewhere else entirely.
    down_result = run_migrate(throwaway_db, "down", "--all")
    assert down_result.returncode == 0, down_result.stderr
    up_result = run_migrate(throwaway_db, "up")
    assert up_result.returncode == 0, up_result.stderr
    return throwaway_db


def _rows(db_url: str, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with psycopg.connect(db_url, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        return list(conn.execute(sql, params).fetchall())


def _one(db_url: str, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any]:
    rows = _rows(db_url, sql, params)
    assert len(rows) == 1, f"expected exactly one row, got {len(rows)}"
    return rows[0]


def _subject(conn: psycopg.Connection[Any], *, adult: bool = True) -> UUID:
    user_ref = uuid4()
    conn.execute(
        "INSERT INTO subjects (user_ref, discovery_eligible, eligibility_reason)"
        " VALUES (%s, %s, %s)",
        (
            user_ref,
            adult,
            "adult" if adult else "minor_discovery_deferred",
        ),
    )
    return user_ref


def _enrolment(conn: psycopg.Connection[Any], user_ref: UUID) -> None:
    """A passed-and-consumed liveness session plus the enrolment on it.

    The session has to be 'consumed' for 0003's composite FK, and the three
    0010 consent columns are NOT NULL, so the shape is not optional.
    """
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


def _seed(
    conn: psycopg.Connection[Any], user_ref: UUID, *, face_id: UUID | None = None
) -> UUID:
    row = conn.execute(
        "INSERT INTO search_seeds (user_ref, seed_kind, source_object_ref,"
        " attributed_face_id) VALUES (%s, 'user_supplied', %s, %s) RETURNING seed_id",
        (user_ref, f"photo/{uuid4().hex}", face_id),
    ).fetchone()
    assert row is not None
    seed_id: UUID = row[0]
    return seed_id


def _run(
    conn: psycopg.Connection[Any],
    user_ref: UUID,
    seed_id: UUID,
    *,
    status: str = "completed",
    succeeded: tuple[str, ...] = ("hive",),
) -> UUID:
    row = conn.execute(
        "INSERT INTO search_runs (seed_id, user_ref, providers_attempted,"
        " providers_succeeded, threshold_config, status, seed_url, completed_at)"
        " VALUES (%s, %s, %s, %s, '{}'::jsonb, %s, 'https://s3.test/x', now())"
        " RETURNING run_id",
        (seed_id, user_ref, ["hive", "google"], list(succeeded), status),
    ).fetchone()
    assert row is not None
    run_id: UUID = row[0]
    return run_id


def _attributed_face(conn: psycopg.Connection[Any], user_ref: UUID) -> UUID:
    run = conn.execute(
        "INSERT INTO attribution_runs (photo_ref, requested_by, candidate_count,"
        " match_threshold, max_candidates, model_id, status)"
        " VALUES (%s, %s, 1, 92.00, 5, 'rek-v6', 'completed') RETURNING run_id",
        (f"photo/{uuid4().hex}", user_ref),
    ).fetchone()
    assert run is not None
    face = conn.execute(
        "INSERT INTO attributed_faces (run_id, face_index, bbox, detect_confidence,"
        " resolved_user_ref, match_score, model_id)"
        " VALUES (%s, 0, %s, 99.10, %s, 97.50, 'rek-v6') RETURNING face_id",
        (run[0], json.dumps({"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}), user_ref),
    ).fetchone()
    assert face is not None
    face_id: UUID = face[0]
    return face_id


def _infringement(
    conn: psycopg.Connection[Any],
    user_ref: UUID,
    run_id: UUID,
    *,
    domain: str = "example.test",
    status: str = "new",
    url_alive: bool = True,
    providers: tuple[str, ...] = ("hive",),
) -> UUID:
    url_hash = uuid4().hex + uuid4().hex
    conn.execute(
        "INSERT INTO content_urls (url_hash, url, source_domain, canonical_url)"
        " VALUES (%s, %s, %s, %s)",
        (url_hash, f"https://{domain}/p", domain, f"https://{domain}/p"),
    )
    row = conn.execute(
        "INSERT INTO infringements (user_ref, url_hash, page_url, image_url, status,"
        " url_alive, last_checked_at, band)"
        " VALUES (%s, %s, %s, 'https://example.test/i.jpg', %s, %s, now(), 'review')"
        " RETURNING infringement_id",
        (user_ref, url_hash, f"https://{domain}/p", status, url_alive),
    ).fetchone()
    assert row is not None
    infringement_id: UUID = row[0]
    for index, provider in enumerate(providers):
        conn.execute(
            "INSERT INTO attestations (infringement_id, provider_id, score_kind,"
            " provider_score, score_version, band, last_run_id)"
            " VALUES (%s, %s, 'numeric', %s, 'v1', 'review', %s)",
            (infringement_id, provider, 0.80 + index * 0.10, run_id),
        )
    return infringement_id


# ── the contract surface itself ──────────────────────────────────────────────


def test_the_four_views_exist_with_the_columns_the_proxy_reads(migrated_db: str) -> None:
    """Done-when: the four views exist and ``readers.ts`` reads them unchanged.

    Asserted against the PROXY'S column names, not ours. These views translate
    our vocabulary into theirs — infringements → hits, attestations → matches —
    and the translation is the contract.
    """
    for view, expected in EXPECTED_COLUMNS.items():
        actual = {
            row["column_name"]
            for row in _rows(
                migrated_db,
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = 'svc' AND table_name = %s",
                (view,),
            )
        }
        assert actual >= expected, f"svc.{view} is missing {expected - actual}"


def test_the_proxy_role_reads_the_views_and_nothing_else(migrated_db: str) -> None:
    """Done-when: ``imageshield_proxy_ro`` can SELECT the four views and nothing
    else; a direct ``SELECT * FROM public.enrolments`` fails with a permission
    error.

    Enforced by Postgres, not by application logic. A view's base-table reads are
    checked against the view owner, which is what lets the projection through
    while the tables stay closed. If this test ever has to be relaxed, the
    contract has stopped being a contract.
    """
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        conn.execute("SET ROLE imageshield_proxy_ro")
        for view in VIEWS:
            conn.execute(f"SELECT * FROM svc.{view}")  # must not raise
        for table in (
            "public.enrolments",
            "public.liveness_sessions",
            "public.infringements",
            "public.attestations",
            "public.attributed_faces",
            "public.subjects",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(f"SELECT * FROM {table}")
        conn.execute("RESET ROLE")


def test_the_proxy_role_cannot_write_through_a_view(migrated_db: str) -> None:
    """SELECT only. A simple view is auto-updatable in Postgres, so read-only is
    a property of the grant rather than of the view's shape."""
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        user_ref = _subject(conn)
        _enrolment(conn, user_ref)
        conn.execute("SET ROLE imageshield_proxy_ro")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE svc.v_person_enrolment_state SET status = 'deleted'")
        conn.execute("RESET ROLE")


def test_enrolment_state_carries_no_vector_and_no_face_id(migrated_db: str) -> None:
    """INVARIANTS #14. This view is where "just one more column" is easiest, and
    ``external_face_id`` is the one that would matter."""
    columns = {
        row["column_name"]
        for row in _rows(
            migrated_db,
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'svc' AND table_name = 'v_person_enrolment_state'",
        )
    }
    assert not columns & {"external_face_id", "collection_id", "quality_score"}


def test_person_hits_never_exposes_an_image_url(migrated_db: str) -> None:
    """``image_url`` stays on ``infringements`` as evidence and does not travel on
    a user-facing read — the same conclusion 0005 and the ``GET
    /v1/search/infringements`` change reached. Widening this view would undo it
    silently, since nothing else would fail."""
    columns = {
        row["column_name"]
        for row in _rows(
            migrated_db,
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'svc' AND table_name = 'v_person_hits'",
        )
    }
    assert not [c for c in columns if c.endswith("image_url") or c == "thumbnail_url"]


# ── v_person_report_summary ──────────────────────────────────────────────────


def test_a_person_with_no_infringements_appears_with_zeroes(migrated_db: str) -> None:
    """Done-when. A missing row and a row of zeroes render very differently on a
    home screen, and the obvious aggregate (GROUP BY over ``infringements``)
    produces the missing one."""
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        user_ref = _subject(conn)

    row = _one(
        migrated_db,
        "SELECT * FROM svc.v_person_report_summary WHERE person_ref = %s",
        (user_ref,),
    )
    assert row["active_reports"] == 0
    assert row["unresolved_matches"] == 0
    assert row["live_exposure_count"] == 0
    assert row["monitored_sources"] == 0
    assert row["last_run_at"] is None
    assert row["first_scan_completed_at"] is None


def test_live_exposure_excludes_dismissed_authorised_and_dead(migrated_db: str) -> None:
    """Done-when: ``live_exposure_count`` excludes both ``dismissed_not_me`` and
    ``authorised``.

    This count is why the whole task exists. In the legacy system every
    unresolved match cost 18 points, so marking a hit "this is abuse of me" left
    it unresolved and permanently depressed the score, while *dismissing* one
    improved it. Reporting abuse must never make the number worse.
    """
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        user_ref = _subject(conn)
        seed = _seed(conn, user_ref)
        run = _run(conn, user_ref, seed)
        _infringement(conn, user_ref, run, domain="a.test", status="new")
        _infringement(conn, user_ref, run, domain="b.test", status="acknowledged")
        _infringement(conn, user_ref, run, domain="c.test", status="dismissed_not_me")
        _infringement(conn, user_ref, run, domain="d.test", status="authorised")
        _infringement(conn, user_ref, run, domain="e.test", url_alive=False)

    row = _one(
        migrated_db,
        "SELECT * FROM svc.v_person_report_summary WHERE person_ref = %s",
        (user_ref,),
    )
    # new + acknowledged, both still up. Not the two terminal user positions,
    # and not the dead URL.
    assert row["live_exposure_count"] == 2
    assert row["active_reports"] == 2  # 'new' x2 (a.test and the dead e.test)
    assert row["unresolved_matches"] == 3


def test_counts_do_not_multiply_when_a_person_has_several_runs(
    migrated_db: str,
) -> None:
    """The join trap. ``infringements LEFT JOIN search_runs ON user_ref`` gives
    every count a factor of the other table's cardinality, and it looks right
    until a user has more than one of each."""
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        user_ref = _subject(conn)
        seed = _seed(conn, user_ref)
        first = _run(conn, user_ref, seed)
        _run(conn, user_ref, seed)
        _run(conn, user_ref, seed)
        _infringement(conn, user_ref, first, domain="a.test")
        _infringement(conn, user_ref, first, domain="b.test")

    row = _one(
        migrated_db,
        "SELECT * FROM svc.v_person_report_summary WHERE person_ref = %s",
        (user_ref,),
    )
    assert row["live_exposure_count"] == 2
    assert row["unresolved_matches"] == 2


def test_monitored_sources_counts_providers_that_returned_and_are_enabled(
    migrated_db: str,
) -> None:
    """"We monitor 2 sources" while one has an open breaker is a false claim
    (CLAUDE.md §7.5). Configured is not the same as returned, and returned once
    is not the same as still enabled."""
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        user_ref = _subject(conn)
        seed = _seed(conn, user_ref)
        _run(conn, user_ref, seed, succeeded=("hive", "google"))
        both = _one(
            migrated_db,
            "SELECT * FROM svc.v_person_report_summary WHERE person_ref = %s",
            (user_ref,),
        )
        assert both["monitored_sources"] == 2

        conn.execute("UPDATE providers SET enabled = false WHERE provider_id = 'google'")

    after_kill = _one(
        migrated_db,
        "SELECT * FROM svc.v_person_report_summary WHERE person_ref = %s",
        (user_ref,),
    )
    assert after_kill["monitored_sources"] == 1


def test_a_refused_run_is_not_reported_as_a_scan(migrated_db: str) -> None:
    """Invariant #43's reasoning applied to the summary: a run refused at
    dispatch has a ``completed_at`` and looked at nothing, so surfacing it as
    ``last_run_at`` would tell the user we scanned when we did not."""
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        user_ref = _subject(conn)
        seed = _seed(conn, user_ref)
        _run(conn, user_ref, seed, status="refused", succeeded=())

    row = _one(
        migrated_db,
        "SELECT * FROM svc.v_person_report_summary WHERE person_ref = %s",
        (user_ref,),
    )
    assert row["last_run_at"] is None
    assert row["first_scan_completed_at"] is None
    assert row["monitored_sources"] == 0


# ── v_person_hits ────────────────────────────────────────────────────────────


def test_one_row_per_hit_however_many_providers_attested(migrated_db: str) -> None:
    """CLAUDE.md §7.4: the same URL found by three providers is one hit with
    three attestations, and ``provider_count`` is the agreement signal."""
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        user_ref = _subject(conn)
        seed = _seed(conn, user_ref)
        run = _run(conn, user_ref, seed)
        _infringement(conn, user_ref, run, providers=("hive", "google"))

    row = _one(
        migrated_db, "SELECT * FROM svc.v_person_hits WHERE person_ref = %s", (user_ref,)
    )
    assert row["provider_count"] == 2
    # A single representative attestation's raw score, never a blend of the two
    # — provider A's 0.90 and provider B's 0.90 are different quantities
    # (INVARIANTS #15c).
    assert row["score"] == Decimal("0.9000")


def test_hits_carry_provenance_to_the_seed_and_the_attributed_face(
    migrated_db: str,
) -> None:
    """``source_photo_id`` and ``face_bbox`` come through attestation → run →
    seed → attributed face. The proxy draws the box; without the bbox it has to
    render the whole photo, which is the thing we do not do."""
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        user_ref = _subject(conn)
        face_id = _attributed_face(conn, user_ref)
        seed = _seed(conn, user_ref, face_id=face_id)
        run = _run(conn, user_ref, seed)
        _infringement(conn, user_ref, run, domain="host.test")
        expected_ref = conn.execute(
            "SELECT source_object_ref FROM search_seeds WHERE seed_id = %s", (seed,)
        ).fetchone()

    row = _one(
        migrated_db, "SELECT * FROM svc.v_person_hits WHERE person_ref = %s", (user_ref,)
    )
    assert expected_ref is not None
    assert row["source_photo_id"] == expected_ref[0]
    assert row["face_bbox"] == {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}
    assert row["source_domain"] == "host.test"
    assert row["host_page_url"] == "https://host.test/p"


def test_the_permanently_null_columns_are_present_and_typed(migrated_db: str) -> None:
    """``report_id``, ``title`` and ``resolution_note`` have no source here. They
    are returned as typed NULLs rather than omitted: a missing column breaks
    their reader, a NULL does not. Documented as permanently null so nobody
    builds UI expecting them."""
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        user_ref = _subject(conn)
        seed = _seed(conn, user_ref)
        run = _run(conn, user_ref, seed)
        _infringement(conn, user_ref, run)

    row = _one(
        migrated_db, "SELECT * FROM svc.v_person_hits WHERE person_ref = %s", (user_ref,)
    )
    assert row["report_id"] is None
    assert row["title"] is None
    assert row["resolution_note"] is None
    types = {
        r["column_name"]: r["data_type"]
        for r in _rows(
            migrated_db,
            "SELECT column_name, data_type FROM information_schema.columns"
            " WHERE table_schema = 'svc' AND table_name = 'v_person_hits'",
        )
    }
    assert types["report_id"] == "uuid"
    assert types["title"] == "text"
    assert types["resolution_note"] == "text"


def test_match_lifecycle_reports_open_and_url_dead(migrated_db: str) -> None:
    """Done-when: ``match_lifecycle`` returns ``open`` and ``url_dead``
    correctly; ``takedown_requested`` and ``removed`` are unreachable in v1
    because takedown is not built. The column carries all four values so their
    reader needs no change later."""
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        user_ref = _subject(conn)
        seed = _seed(conn, user_ref)
        run = _run(conn, user_ref, seed)
        _infringement(conn, user_ref, run, domain="live.test", url_alive=True)
        _infringement(conn, user_ref, run, domain="dead.test", url_alive=False)

    lifecycles = {
        row["source_domain"]: row["match_lifecycle"]
        for row in _rows(
            migrated_db,
            "SELECT * FROM svc.v_person_hits WHERE person_ref = %s",
            (user_ref,),
        )
    }
    assert lifecycles == {"live.test": "open", "dead.test": "url_dead"}


def test_match_action_is_the_latest_feedback_not_the_first(migrated_db: str) -> None:
    """``infringement_feedback`` is append-only — a user changing their mind
    writes a second row — so "what did they say" is the newest row, not the only
    one."""
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        user_ref = _subject(conn)
        seed = _seed(conn, user_ref)
        run = _run(conn, user_ref, seed)
        infringement = _infringement(conn, user_ref, run)
        conn.execute(
            "INSERT INTO infringement_feedback (infringement_id, user_ref, signal,"
            " created_at) VALUES (%s, %s, 'not_me', now() - interval '1 hour')",
            (infringement, user_ref),
        )
        conn.execute(
            "INSERT INTO infringement_feedback (infringement_id, user_ref, signal)"
            " VALUES (%s, %s, 'confirmed')",
            (infringement, user_ref),
        )

    row = _one(
        migrated_db, "SELECT * FROM svc.v_person_hits WHERE person_ref = %s", (user_ref,)
    )
    assert row["match_action"] == "confirmed"


def test_resolved_at_is_set_for_a_terminal_position_and_for_a_dead_url(
    migrated_db: str,
) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        user_ref = _subject(conn)
        seed = _seed(conn, user_ref)
        run = _run(conn, user_ref, seed)
        open_hit = _infringement(conn, user_ref, run, domain="open.test")
        authorised = _infringement(
            conn, user_ref, run, domain="mine.test", status="authorised"
        )
        conn.execute(
            "INSERT INTO infringement_feedback (infringement_id, user_ref, signal)"
            " VALUES (%s, %s, 'authorised')",
            (authorised, user_ref),
        )
        _infringement(conn, user_ref, run, domain="gone.test", url_alive=False)

    resolved = {
        row["source_domain"]: row["resolved_at"]
        for row in _rows(
            migrated_db,
            "SELECT * FROM svc.v_person_hits WHERE person_ref = %s",
            (user_ref,),
        )
    }
    assert resolved["open.test"] is None
    assert resolved["mine.test"] is not None
    assert resolved["gone.test"] is not None
    assert open_hit is not None


# ── v_person_liveness_attempts ───────────────────────────────────────────────


def test_liveness_attempts_window_matches_what_the_rate_limit_enforces(
    migrated_db: str,
) -> None:
    """The window is not a free choice: it must be the same predicate
    ``liveness/store.py`` enforces ``LIVENESS_MAX_ATTEMPTS_24H`` against, or the
    proxy's pre-check and our refusal disagree and the client sees a 429 it was
    told it would not get."""
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        user_ref = _subject(conn)
        for age in ("2 hours", "23 hours", "25 hours"):
            conn.execute(
                "INSERT INTO liveness_sessions (user_ref, provider_session_id, status,"
                " expires_at, created_at)"
                " VALUES (%s, %s, 'failed', now(), now() - %s::interval)",
                (user_ref, uuid4().hex, age),
            )

    row = _one(
        migrated_db,
        "SELECT * FROM svc.v_person_liveness_attempts WHERE person_ref = %s",
        (user_ref,),
    )
    assert row["attempts_24h"] == 2  # the 25-hour-old attempt has aged out
    assert row["last_attempt_at"] is not None


# ── the status vocabulary is now a constraint ────────────────────────────────


def test_an_unknown_infringement_status_is_refused_by_the_database(
    migrated_db: str,
) -> None:
    """``hit_status`` is a published contract column now, so the vocabulary stops
    being a convention. 0005 left it unconstrained."""
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        user_ref = _subject(conn)
        seed = _seed(conn, user_ref)
        run = _run(conn, user_ref, seed)
        infringement = _infringement(conn, user_ref, run)
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "UPDATE infringements SET status = 'resolved_probably'"
                " WHERE infringement_id = %s",
                (infringement,),
            )
