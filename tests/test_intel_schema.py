"""0039 — the intel schema (spec §3). Privileges are asserted under SET ROLE, the
only place a role's real grants show (test_articles_store precedent)."""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from tests.db import run_migrate


def _steps_through(version: str) -> str:
    """How many ``down --steps N`` roll back ``version`` and everything after it.

    Counted from the migration files, never hardcoded: a literal ``1`` silently
    retargets the next migration the day one is added on top (0040 did exactly
    that to this test).
    """
    ups = sorted(p.name for p in (Path(__file__).parent.parent / "migrations").glob("*.up.sql"))
    return str(sum(1 for name in ups if name >= version))


@pytest.fixture
def migrated_db(throwaway_db: str) -> str:
    assert run_migrate(throwaway_db, "down", "--all").returncode == 0
    up = run_migrate(throwaway_db, "up")
    assert up.returncode == 0, up.stderr
    return throwaway_db


def test_providers_kind_is_text_with_llm_allowed(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        (typ,) = conn.execute(  # type: ignore[misc]
            "SELECT data_type FROM information_schema.columns"
            " WHERE table_name = 'providers' AND column_name = 'kind'"
        ).fetchone()
        assert typ == "text"
        row = conn.execute(
            "SELECT kind, enabled, calibrated FROM providers WHERE provider_id = 'claude_intel'"
        ).fetchone()
        assert row == ("llm", False, False)
        (budget,) = conn.execute(  # type: ignore[misc]
            "SELECT daily_budget_usd FROM providers WHERE provider_id = 'claude_intel'"
        ).fetchone()
        assert budget == Decimal("50.00")  # 0040: the owner's daily ceiling, provider still off
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "UPDATE providers SET calibrated = true WHERE provider_id = 'claude_intel'"
            )


def test_tags_well_formed(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        ok = conn.execute(
            "SELECT intel_tags_well_formed(ARRAY['x','dating_apps']::text[])"
        ).fetchone()
        assert ok == (True,)
        for bad in ("ARRAY['X']", "ARRAY['a','a']", "ARRAY['1a']", "ARRAY[NULL]::text[]"):
            result = conn.execute(f"SELECT intel_tags_well_formed({bad}::text[])").fetchone()
            assert result == (False,)
        assert conn.execute("SELECT intel_tags_well_formed('{}'::text[])").fetchone() == (True,)


def test_intel_rw_writes_but_never_deletes(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        conn.execute("SET ROLE intel_rw")
        (source_id,) = conn.execute(  # type: ignore[misc]
            "INSERT INTO intel_sources (kind, source_url, url_hash, normalisation_version,"
            " check_every_hours, terms_note, created_by)"
            " VALUES ('policy_page', 'https://p.example/terms', repeat('a', 64), 'v1', 24,"
            " 'automated access permitted per robots and terms', 'alice') RETURNING source_id"
        ).fetchone()
        (run_id,) = conn.execute(  # type: ignore[misc]
            "INSERT INTO intel_runs (kind, source_id, requested_by)"
            " VALUES ('source_check', %s, 'schedule')"
            " RETURNING run_id",
            (source_id,),
        ).fetchone()
        conn.execute("INSERT INTO audit_log (actor_type, action) VALUES ('service', 'intel.test')")
        conn.execute("SELECT url_hash FROM content_urls LIMIT 1")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT url FROM content_urls LIMIT 1")  # column grant is url_hash only
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM intel_runs WHERE run_id = %s", (run_id,))
        conn.execute("RESET ROLE")


def test_intel_rw_grant_surface_covers_every_intel_table_and_metering(migrated_db: str) -> None:
    """Controller ruling: the grant test must exercise, under SET ROLE, every new
    intel table's granted operations (SELECT/INSERT/UPDATE), `providers` UPDATE,
    and the provider_calls/provider_spend writes the metering path needs -- not
    only the four operations `test_intel_rw_writes_but_never_deletes` happens to
    touch. And it must assert no DELETE on any intel table, not only intel_runs.

    Builds one dependency chain (source -> run -> document -> signal -> excerpt,
    plus a standalone proposal linked to the signal, plus the singleton
    vocabulary row) so every table has a real row to SELECT/UPDATE/refuse-DELETE
    against, then checks providers/provider_calls/provider_spend separately.
    """
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        conn.execute("SET ROLE intel_rw")

        (source_id,) = conn.execute(  # type: ignore[misc]
            "INSERT INTO intel_sources (kind, source_url, url_hash, normalisation_version,"
            " check_every_hours, terms_note, created_by)"
            " VALUES ('policy_page', 'https://q.example/terms', repeat('b', 64), 'v1', 24,"
            " 'automated access permitted per robots and terms', 'alice') RETURNING source_id"
        ).fetchone()
        conn.execute(
            "INSERT INTO intel_snapshots (source_id, content_sha256, snapshot_text, content_type,"
            " truncated) VALUES (%s, repeat('c', 64), 'terms text', 'text/html', false)",
            (source_id,),
        )
        (run_id,) = conn.execute(  # type: ignore[misc]
            "INSERT INTO intel_runs (kind, source_id, requested_by)"
            " VALUES ('source_check', %s, 'schedule')"
            " RETURNING run_id",
            (source_id,),
        ).fetchone()
        (document_id,) = conn.execute(  # type: ignore[misc]
            "INSERT INTO intel_documents (run_id, source_id, document_url, final_url, url_hash,"
            " document_url_hash, normalisation_version, publisher_domain, trust, content_sha256,"
            " truncated) VALUES (%s, %s, 'https://q.example/terms', 'https://q.example/terms',"
            " repeat('d', 64), repeat('d', 64), 'v1', 'q.example', 'listed', repeat('e', 64),"
            " false) RETURNING document_id",
            (run_id, source_id),
        ).fetchone()
        (signal_id,) = conn.execute(  # type: ignore[misc]
            "INSERT INTO intel_signals (document_id, category, direction, summary, model_id,"
            " prompt_version) VALUES (%s, 'policy', 'risk_up', 'A policy change worth noting.',"
            " 'claude-sonnet-5', 'p1') RETURNING signal_id",
            (document_id,),
        ).fetchone()
        (excerpt_id,) = conn.execute(  # type: ignore[misc]
            "INSERT INTO intel_excerpts (signal_id, quote_text, char_start, char_end, quote_sha256)"
            " VALUES (%s, 'this is a quoted excerpt of the source text', 0, 20, repeat('f', 64))"
            " RETURNING excerpt_id",
            (signal_id,),
        ).fetchone()
        proposal_id = uuid4()
        conn.execute(
            "INSERT INTO intel_proposals (proposal_id, kind, status, target, suggested, rationale,"
            " model_id, prompt_version) VALUES (%s, 'coverage_gap', 'pending', '{}', '{}', 'r',"
            " 'claude-sonnet-5', 'p1')",
            (proposal_id,),
        )
        conn.execute(
            "INSERT INTO intel_proposal_signals (proposal_id, signal_id) VALUES (%s, %s)",
            (proposal_id, signal_id),
        )
        conn.execute(
            "INSERT INTO intel_vocabulary (id, release_no, map_version, scoring_version,"
            " quiz_version, document) VALUES (1, 1, 1, 'v1', 'v1', '{}')"
        )

        # SELECT + UPDATE on every one of the nine tables.
        assert conn.execute(
            "SELECT source_id FROM intel_sources WHERE source_id = %s", (source_id,)
        ).fetchone() == (source_id,)
        conn.execute(
            "UPDATE intel_sources SET updated_at = now() WHERE source_id = %s", (source_id,)
        )

        assert conn.execute(
            "SELECT source_id FROM intel_snapshots WHERE source_id = %s", (source_id,)
        ).fetchone() == (source_id,)
        conn.execute(
            "UPDATE intel_snapshots SET fetched_at = now() WHERE source_id = %s", (source_id,)
        )

        assert conn.execute(
            "SELECT run_id FROM intel_runs WHERE run_id = %s", (run_id,)
        ).fetchone() == (run_id,)
        conn.execute("UPDATE intel_runs SET attempts = attempts + 1 WHERE run_id = %s", (run_id,))

        assert conn.execute(
            "SELECT document_id FROM intel_documents WHERE document_id = %s", (document_id,)
        ).fetchone() == (document_id,)
        conn.execute(
            "UPDATE intel_documents SET title = 'Terms of service' WHERE document_id = %s",
            (document_id,),
        )

        assert conn.execute(
            "SELECT signal_id FROM intel_signals WHERE signal_id = %s", (signal_id,)
        ).fetchone() == (signal_id,)
        conn.execute(
            "UPDATE intel_signals SET summary = 'An updated summary.' WHERE signal_id = %s",
            (signal_id,),
        )

        assert conn.execute(
            "SELECT excerpt_id FROM intel_excerpts WHERE excerpt_id = %s", (excerpt_id,)
        ).fetchone() == (excerpt_id,)
        conn.execute(
            "UPDATE intel_excerpts SET quote_sha256 = repeat('9', 64) WHERE excerpt_id = %s",
            (excerpt_id,),
        )

        assert conn.execute(
            "SELECT proposal_id FROM intel_proposals WHERE proposal_id = %s", (proposal_id,)
        ).fetchone() == (proposal_id,)
        conn.execute(
            "UPDATE intel_proposals SET rationale = 'updated rationale' WHERE proposal_id = %s",
            (proposal_id,),
        )

        assert conn.execute(
            "SELECT proposal_id, signal_id FROM intel_proposal_signals"
            " WHERE proposal_id = %s AND signal_id = %s",
            (proposal_id, signal_id),
        ).fetchone() == (proposal_id, signal_id)
        conn.execute(
            "UPDATE intel_proposal_signals SET proposal_id = proposal_id"
            " WHERE proposal_id = %s AND signal_id = %s",
            (proposal_id, signal_id),
        )

        assert conn.execute("SELECT id FROM intel_vocabulary WHERE id = 1").fetchone() == (1,)
        conn.execute("UPDATE intel_vocabulary SET received_at = now() WHERE id = 1")

        # providers UPDATE -- the metering path enables/disables and re-prices
        # claude_intel without a migration.
        conn.execute(
            "UPDATE providers SET daily_budget_usd = 5.00 WHERE provider_id = 'claude_intel'"
        )
        assert conn.execute(
            "SELECT daily_budget_usd FROM providers WHERE provider_id = 'claude_intel'"
        ).fetchone() == (Decimal("5.00"),)

        # provider_calls / provider_spend writes -- the metering path itself.
        conn.execute(
            "INSERT INTO provider_calls (intel_run_id, provider_id, status, raw_response)"
            " VALUES (%s, 'claude_intel', 'ok', '{}'::jsonb)",
            (run_id,),
        )
        conn.execute(
            "INSERT INTO provider_spend (provider_id, spend_date, call_count, cost_usd)"
            " VALUES ('claude_intel', current_date, 1, 0.25)"
            " ON CONFLICT (provider_id, spend_date)"
            " DO UPDATE SET call_count = provider_spend.call_count + 1,"
            " cost_usd = provider_spend.cost_usd + 0.25"
        )

        # No DELETE anywhere on the nine intel tables -- 0015's rule, checked
        # per table rather than only on intel_runs.
        no_delete_cases = [
            (
                "intel_proposal_signals",
                "proposal_id = %s AND signal_id = %s",
                (proposal_id, signal_id),
            ),
            ("intel_proposals", "proposal_id = %s", (proposal_id,)),
            ("intel_excerpts", "excerpt_id = %s", (excerpt_id,)),
            ("intel_signals", "signal_id = %s", (signal_id,)),
            ("intel_documents", "document_id = %s", (document_id,)),
            ("intel_runs", "run_id = %s", (run_id,)),
            ("intel_snapshots", "source_id = %s", (source_id,)),
            ("intel_sources", "source_id = %s", (source_id,)),
            ("intel_vocabulary", "id = %s", (1,)),
        ]
        for table, where, params in no_delete_cases:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(f"DELETE FROM {table} WHERE {where}", params)

        conn.execute("RESET ROLE")


def test_source_shape_checks(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):  # policy_page without a url
            conn.execute(
                "INSERT INTO intel_sources (kind, check_every_hours, terms_note, created_by)"
                " VALUES ('policy_page', 24, 'automated access permitted', 'alice')"
            )
        with pytest.raises(psycopg.errors.CheckViolation):  # search_query with a url
            conn.execute(
                "INSERT INTO intel_sources (kind, source_url, url_hash, normalisation_version,"
                " query_text, check_every_hours, terms_note, created_by)"
                " VALUES ('search_query', 'https://a.b/',"
                " repeat('b', 64), 'v1', 'q', 24, 'automated access permitted', 'alice')"
            )


def test_one_open_run_per_source(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        (source_id,) = conn.execute(  # type: ignore[misc]
            "INSERT INTO intel_sources (kind, query_text, check_every_hours, terms_note,"
            " created_by) VALUES ('search_query', 'platform privacy change', 24,"
            " 'automated access permitted', 'a')"
            " RETURNING source_id"
        ).fetchone()
        conn.execute(
            "INSERT INTO intel_runs (kind, source_id, requested_by) VALUES ('discovery', %s, 'a')",
            (source_id,),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO intel_runs (kind, source_id, requested_by)"
                " VALUES ('discovery', %s, 'a')",
                (source_id,),
            )


def test_down_succeeds_after_claude_intel_was_metered(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        (run_id,) = conn.execute(  # type: ignore[misc]
            "INSERT INTO intel_runs (kind, requested_by, request)"
            " VALUES ('adhoc_url', 'a', '{}'::jsonb)"
            " RETURNING run_id"
        ).fetchone()
        conn.execute(
            "INSERT INTO provider_calls (intel_run_id, provider_id, status, raw_response)"
            " VALUES (%s, 'claude_intel', 'ok', '{}'::jsonb)",
            (run_id,),
        )
        conn.execute(
            "INSERT INTO provider_spend (provider_id, spend_date, call_count, cost_usd)"
            " VALUES ('claude_intel', current_date, 1, 0.01)"
        )
    down = run_migrate(migrated_db, "down", "--steps", _steps_through("0039_"))
    assert down.returncode == 0, down.stderr
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        assert conn.execute("SELECT 1 FROM pg_type WHERE typname = 'provider_kind'").fetchone() == (
            1,
        )
        assert (
            conn.execute("SELECT 1 FROM providers WHERE provider_id = 'claude_intel'").fetchone()
            is None
        )
    assert run_migrate(migrated_db, "up").returncode == 0


def test_proposal_shape_checks(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):  # approved with no name
            conn.execute(
                "INSERT INTO intel_proposals (kind, status, target, suggested, rationale,"
                " model_id, prompt_version, decided) VALUES ('threat_event', 'approved', '{}',"
                " '{}', 'r', 'm', 'p', '{}')"
            )
        # suggestion must be delivered/superseded
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO intel_proposals (kind, status, target, suggested, rationale,"
                " model_id, prompt_version)"
                " VALUES ('weight_suggestion', 'pending', '{}', '{}', 'r', 'm', 'p')"
            )
        conn.execute(
            "INSERT INTO intel_proposals (proposal_id, kind, status, target, suggested,"
            " rationale, model_id, prompt_version)"
            " VALUES (%s, 'coverage_gap', 'pending', '{}', '{}', 'r', 'm', 'p')",
            (uuid4(),),
        )


def test_0041_prices_the_proposal_models_worst_case(migrated_db: str) -> None:
    """Step 2 calls Opus 5.5; the step-0 worst case across both models is 0.45."""
    query = "SELECT cost_per_call_usd FROM providers WHERE provider_id = 'claude_intel'"
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        assert conn.execute(query).fetchone() == (Decimal("0.45"),)
    down = run_migrate(migrated_db, "down", "--steps", _steps_through("0041_"))
    assert down.returncode == 0, down.stderr
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        assert conn.execute(query).fetchone() == (Decimal("0.25"),)
    assert run_migrate(migrated_db, "up").returncode == 0


_OLD_RELEVANCE = "CHECKis_globalORcardinalitydomains>0"
_NEW_RELEVANCE = "CHECKis_globalORcardinalitydomains>0ORcardinalitytags>0"

_THREAT = (
    "INSERT INTO threat_events (kind, title, severity, tags, domains, is_global,"
    " expires_at, decay_days, status, created_by, proposal_id)"
    " VALUES ('leak', 't', 3, %s::text[], %s::text[], %s, now() + interval '7 days', 7,"
    " %s, 'op', %s) RETURNING event_id"
)

_THREAT_PROPOSAL = (
    "INSERT INTO intel_proposals (kind, status, target, suggested, rationale, model_id,"
    " prompt_version) VALUES ('threat_event', 'pending', '{\"tags\": [\"x\"]}', '{}', 'r',"
    " 'm', 'p') RETURNING proposal_id"
)


def _relevance_checks(conn: psycopg.Connection[Any]) -> dict[str, bool]:
    """Every CHECK on threat_events: normalised definition -> convalidated. Whitespace,
    parentheses and a NOT VALID suffix are removed, exactly as 0042's DO blocks compare."""
    rows = conn.execute(
        "SELECT pg_get_constraintdef(oid), convalidated FROM pg_constraint"
        " WHERE conrelid = 'threat_events'::regclass AND contype = 'c'"
    ).fetchall()
    return {re.sub(r"[\s()]", "", d.removesuffix(" NOT VALID")): v for d, v in rows}


def test_0042_threat_events_carry_tags_and_an_optional_proposal(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        (event_id,) = conn.execute(  # type: ignore[misc]
            _THREAT, (["linkedin"], [], False, "active", None)
        ).fetchone()
        assert conn.execute(
            "SELECT tags, proposal_id FROM threat_events WHERE event_id = %s", (event_id,)
        ).fetchone() == (["linkedin"], None)
        for tags in (["LinkedIn"], ["x", "x"]):  # the shape every intel tags column checks
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(_THREAT, (tags, [], False, "active", None))
        with pytest.raises(psycopg.errors.CheckViolation):  # matches nothing at all
            conn.execute(_THREAT, ([], [], False, "active", None))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(_THREAT, (["x"], [], False, "active", uuid4()))
        checks = _relevance_checks(conn)
        assert _OLD_RELEVANCE not in checks and checks[_NEW_RELEVANCE] is True


def test_0042_one_event_per_proposal(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        (pid,) = conn.execute(_THREAT_PROPOSAL).fetchone()  # type: ignore[misc]
        conn.execute(_THREAT, (["x"], [], False, "active", pid))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(_THREAT, (["x"], [], False, "active", pid))


def test_0042_intel_rw_reads_and_inserts_threat_events_and_nothing_more(migrated_db: str) -> None:
    """Approving a threat proposal inserts the event in the decision's own transaction (spec
    §3.7). Asserted under SET ROLE: a superuser run hides a missing grant (the 0035 trap)."""
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        (pid,) = conn.execute(_THREAT_PROPOSAL).fetchone()  # type: ignore[misc]
        conn.execute("SET ROLE intel_rw")
        conn.execute(_THREAT, (["x"], [], False, "active", pid))
        assert conn.execute(
            "SELECT count(*) FROM threat_events WHERE proposal_id = %s", (pid,)
        ).fetchone() == (1,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE threat_events SET title = 'x'")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM threat_events")
        conn.execute("RESET ROLE")


def test_0042_down_refuses_a_live_tag_only_threat_then_restores_the_old_check_not_valid(
    migrated_db: str,
) -> None:
    """spec §3.7's down leg, then Review Focus 2: an up over the row that down grandfathered."""
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        (tag_only,) = conn.execute(  # type: ignore[misc]
            _THREAT, (["linkedin"], [], False, "active", None)
        ).fetchone()
        # Scoped by a domain as well: the old CHECK holds it, so it never blocks the down.
        conn.execute(_THREAT, (["linkedin"], ["evil.example"], False, "active", None))
    steps = _steps_through("0042_")
    refused = run_migrate(migrated_db, "down", "--steps", steps)
    assert refused.returncode != 0 and "retract" in refused.stderr
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        assert conn.execute(  # nothing was reverted
            "SELECT 1 FROM pg_views WHERE viewname = 'v_active_scoped_events'"
        ).fetchone() == (1,)
        conn.execute(
            "UPDATE threat_events SET status = 'retracted' WHERE event_id = %s", (tag_only,)
        )
    down = run_migrate(migrated_db, "down", "--steps", steps)
    assert down.returncode == 0, down.stderr
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        assert _relevance_checks(conn)[_OLD_RELEVANCE] is False  # restored NOT VALID
        assert conn.execute(
            "SELECT status FROM threat_events WHERE event_id = %s", (tag_only,)
        ).fetchone() == ("retracted",)
        assert (
            conn.execute(
                "SELECT 1 FROM pg_views WHERE viewname = 'v_active_scoped_events'"
            ).fetchone()
            is None
        )
        with pytest.raises(psycopg.errors.CheckViolation):  # new rows are held to it
            conn.execute(
                "INSERT INTO threat_events (kind, title, severity, expires_at, decay_days,"
                " created_by) VALUES ('leak', 't', 3, now() + interval '1 day', 1, 'op')"
            )
    up = run_migrate(migrated_db, "up")
    assert up.returncode == 0, up.stderr
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        checks = _relevance_checks(conn)
        assert _OLD_RELEVANCE not in checks
        assert checks[_NEW_RELEVANCE] is False  # the grandfathered row keeps it unvalidated


def test_0042_down_all_survives_a_retracted_tag_only_threat(migrated_db: str) -> None:
    """A retracted tag-only event must not break a FULL down. NOT VALID skips existing rows,
    not a later UPDATE of one: 0037's down runs ``UPDATE threat_events SET penalty = 0.01
    WHERE penalty IS NULL``, Postgres re-checks the CHECK 0042's down restores, and every event
    written since 0037 has no penalty. Without the UPDATE in 0042's down this failed the NEXT
    database test's fixture, far from its cause."""
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        conn.execute(_THREAT, (["linkedin"], [], False, "retracted", None))
    down = run_migrate(migrated_db, "down", "--all")
    assert down.returncode == 0, down.stderr
    assert run_migrate(migrated_db, "up").returncode == 0

    # The same row one generation later: 0042's own down grandfathered it, a re-up kept it
    # (unvalidated), and the full down must still get past 0037.
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        conn.execute(_THREAT, (["linkedin"], [], False, "retracted", None))
    steps = _steps_through("0042_")
    assert run_migrate(migrated_db, "down", "--steps", steps).returncode == 0
    assert run_migrate(migrated_db, "up").returncode == 0
    down = run_migrate(migrated_db, "down", "--all")
    assert down.returncode == 0, down.stderr
    assert run_migrate(migrated_db, "up").returncode == 0


_PROTECTION_PROPOSAL = (
    "INSERT INTO intel_proposals (kind, status, target, suggested, rationale, model_id,"
    " prompt_version) VALUES ('protection_event', 'pending',"
    " '{\"tags\": [\"x\"], \"is_global\": false}', '{}', 'r', 'm', 'p') RETURNING proposal_id"
)

_PROTECTION = (
    "INSERT INTO protection_events (title, strength, tags, is_global, starts_at, review_by,"
    " status, proposal_id, renews_event_id, created_by, retracted_by, retracted_at,"
    " retract_reason)"
    " VALUES ('p', %(strength)s, %(tags)s::text[], %(is_global)s, now() + %(starts)s::interval,"
    " now() + %(ends)s::interval, %(status)s, %(proposal_id)s, %(renews)s, 'op',"
    " %(retracted_by)s, %(retracted_at)s, %(retract_reason)s) RETURNING event_id"
)


def _protection(conn: psycopg.Connection[Any], **overrides: Any) -> UUID:
    """One protection_events row, on a fresh proposal unless ``proposal_id`` is given."""
    params: dict[str, Any] = {
        "strength": 3,
        "tags": ["x"],
        "is_global": False,
        "starts": "0 days",
        "ends": "90 days",
        "status": "active",
        "renews": None,
        "retracted_by": None,
        "retracted_at": None,
        "retract_reason": None,
        **overrides,
    }
    if "proposal_id" not in overrides:
        proposal = conn.execute(_PROTECTION_PROPOSAL).fetchone()
        assert proposal is not None
        params["proposal_id"] = proposal[0]
    row = conn.execute(_PROTECTION, params).fetchone()
    assert row is not None
    event_id: UUID = row[0]
    return event_id


def test_0044_protection_events_hold_their_shape(migrated_db: str) -> None:
    """spec §3.7: strength 1-5, exactly one of tags or global, a review within 366 days of the
    start, a retraction that names who and why, and a proposal behind every credit."""
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        _protection(conn)  # a tag-scoped credit
        _protection(conn, tags=[], is_global=True)  # a global one
        for bad in (
            {"strength": 0},
            {"strength": 6},
            {"tags": [], "is_global": False},  # reaches nobody
            {"tags": ["x"], "is_global": True},  # both scopes at once
            {"tags": ["X"]},  # a malformed slug
            {"tags": ["x", "x"]},  # a repeated slug
            {"ends": "0 days"},  # review_by == starts_at
            {"ends": "367 days"},  # past a year and a day
            {"status": "lapsed"},  # active or retracted only
            {"status": "retracted"},  # retracted with no name on it
            {"retracted_by": "op"},  # a name on an active credit
        ):
            with pytest.raises(psycopg.errors.CheckViolation):
                _protection(conn, **bad)
        with pytest.raises(psycopg.errors.NotNullViolation):  # no hand-created credit
            _protection(conn, proposal_id=None)
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _protection(conn, proposal_id=uuid4())


def test_0044_one_credit_per_proposal_and_one_renewal_per_credit(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        first = _protection(conn)
        row = conn.execute(
            "SELECT proposal_id FROM protection_events WHERE event_id = %s", (first,)
        ).fetchone()
        assert row is not None
        with pytest.raises(psycopg.errors.UniqueViolation):
            _protection(conn, proposal_id=row[0])
        _protection(conn, renews=first, starts="90 days", ends="180 days")
        with pytest.raises(psycopg.errors.UniqueViolation):
            _protection(conn, renews=first, starts="90 days", ends="180 days")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _protection(conn, renews=uuid4())


def test_0044_intel_rw_creates_locks_and_retracts_credits_and_never_deletes(
    migrated_db: str,
) -> None:
    """Approving inserts a credit and locks the one a renewal continues; retracting updates
    one; all as intel_rw (spec §3.7, §4.7). Asserted under SET ROLE: a superuser run hides a
    missing grant (the 0035 trap)."""
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        conn.execute("SET ROLE intel_rw")
        event_id = _protection(conn)
        conn.execute(
            "SELECT event_id FROM protection_events WHERE event_id = %s FOR UPDATE", (event_id,)
        )
        conn.execute(
            "UPDATE protection_events SET status = 'retracted', retracted_by = 'op',"
            " retracted_at = now(), retract_reason = 'withdrawn' WHERE event_id = %s",
            (event_id,),
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM protection_events")
        conn.execute("RESET ROLE")


def test_0044_one_open_renewal_check_per_credit(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        event = str(uuid4())
        insert = (
            "INSERT INTO intel_runs (kind, request, requested_by, status)"
            " VALUES ('renewal_check', jsonb_build_object('event_id', %s::text), 'schedule', %s)"
        )
        conn.execute(insert, (event, "queued"))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(insert, (event, "queued"))
        conn.execute(
            "UPDATE intel_runs SET status = 'completed' WHERE request ->> 'event_id' = %s",
            (event,),
        )
        conn.execute(insert, (event, "queued"))  # a finished check does not block the next


def _threat_half(path: Path) -> str:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    start = text.index("SELECT event_id,")
    end = text.index("AND cardinality(tags) > 0", start) + len("AND cardinality(tags) > 0")
    return text[start:end]


def test_0044_keeps_the_threat_half_byte_identical_to_0042() -> None:
    """spec §3.7 (step 4): the view gains a protection half; its threat half, and the one the
    down restores, are 0042's exactly."""
    migrations = Path(__file__).parent.parent / "migrations"
    threat_half = _threat_half(migrations / "0042_intel_scoped_threats.up.sql")
    for name in (
        "0044_intel_protection_events.up.sql",
        "0044_intel_protection_events.down.sql",
    ):
        assert _threat_half(migrations / name) == threat_half, name


def test_0044_down_restores_the_threat_only_view_with_its_grant(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        _protection(conn)
        conn.execute(
            "INSERT INTO intel_runs (kind, request, requested_by)"
            " VALUES ('renewal_check', jsonb_build_object('event_id', %s::text), 'schedule')",
            (str(uuid4()),),
        )
    down = run_migrate(migrated_db, "down", "--steps", _steps_through("0044_"))
    assert down.returncode == 0, down.stderr
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        assert conn.execute("SELECT to_regclass('public.protection_events')").fetchone() == (
            None,
        )
        assert conn.execute(
            "SELECT to_regclass('public.intel_runs_one_open_renewal')"
        ).fetchone() == (None,)
        definition = conn.execute(
            "SELECT definition FROM pg_views"
            " WHERE schemaname = 'svc' AND viewname = 'v_active_scoped_events'"
        ).fetchone()
        assert definition is not None and "protection" not in definition[0]
        assert conn.execute(
            "SELECT has_table_privilege('imageshield_proxy_ro', 'svc.v_active_scoped_events',"
            " 'SELECT')"
        ).fetchone() == (True,)
        assert conn.execute(
            "SELECT status, error_code FROM intel_runs WHERE kind = 'renewal_check'"
        ).fetchone() == ("failed", "migration_down")
    up = run_migrate(migrated_db, "up")
    assert up.returncode == 0, up.stderr
