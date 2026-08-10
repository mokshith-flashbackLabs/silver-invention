"""Subject eligibility: the pure mapping, the store, and the migration.

The mapping tests look trivial and are the most important ones here. A minor
must map to ineligible *unconditionally* — there is no configuration value that
turns that branch off, because the v2 change has to cost a code review alongside
the config change, never config alone.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import psycopg
import pytest

from imageshield.db.connection import make_async_pool
from imageshield.subjects.eligibility import MINOR_DISCOVERY_SUPPORTED, eligibility_for
from imageshield.subjects.store import PostgresSubjectStore
from imageshield.types import UserRef
from tests.db import run_migrate


def test_an_adult_is_eligible_and_a_minor_is_not() -> None:
    adult = eligibility_for(True)
    assert (adult.discovery_eligible, adult.eligibility_reason) == (True, "adult")

    minor = eligibility_for(False)
    assert (minor.discovery_eligible, minor.eligibility_reason) == (
        False,
        "minor_discovery_deferred",
    )


def test_minor_discovery_ships_off() -> None:
    """v1 ships with this False and it is a module constant, not a setting.

    Flipping it needs CSAM screening on fetched candidates and a
    mandatory-reporting path to exist first — the same reasoning as the
    calibration floor living in code (CLAUDE.md §7.3).
    """
    assert MINOR_DISCOVERY_SUPPORTED is False


# ── store + schema ───────────────────────────────────────────────────────


@pytest.fixture
def migrated_db(throwaway_db: str) -> str:
    run_migrate(throwaway_db, "down", "--all")
    up = run_migrate(throwaway_db, "up")
    assert up.returncode == 0, up.stderr
    return throwaway_db


@pytest.fixture
async def store(migrated_db: str) -> AsyncIterator[PostgresSubjectStore]:
    pool = make_async_pool(migrated_db, min_size=1, max_size=2)
    await pool.open()
    try:
        yield PostgresSubjectStore(pool)
    finally:
        await pool.close()


async def test_upsert_then_read_roundtrips_both_directions(
    store: PostgresSubjectStore,
) -> None:
    user_ref = UserRef(uuid4())
    assert await store.get_subject(user_ref) is None

    await store.upsert_subject(user_ref, eligibility_for(True))
    adult = await store.get_subject(user_ref)
    assert adult is not None
    assert (adult.discovery_eligible, adult.eligibility_reason) == (True, "adult")

    # A DOB correction: the flag must be updatable, not write-once.
    await store.upsert_subject(user_ref, eligibility_for(False))
    minor = await store.get_subject(user_ref)
    assert minor is not None
    assert minor.discovery_eligible is False
    assert minor.eligibility_reason == "minor_discovery_deferred"
    assert minor.updated_at > adult.updated_at


async def test_a_re_assertion_of_the_same_flag_does_not_bump_updated_at(
    store: PostgresSubjectStore,
) -> None:
    """`updated_at` has to mean "the assertion changed", not "the user enrolled
    again" — otherwise every re-enrolment reads as an eligibility change."""
    user_ref = UserRef(uuid4())
    await store.upsert_subject(user_ref, eligibility_for(True))
    first = await store.get_subject(user_ref)

    await store.upsert_subject(user_ref, eligibility_for(True))
    second = await store.get_subject(user_ref)

    assert first is not None and second is not None
    assert first.updated_at == second.updated_at


async def test_the_database_refuses_a_row_whose_reason_contradicts_its_flag(
    migrated_db: str,
) -> None:
    """A row claiming 'adult' AND ineligible is the exact corruption that would
    let a minor be scanned while the reason column reads reassuringly. The CHECK
    is what stops it, not every writer remembering."""
    with (
        psycopg.connect(migrated_db, autocommit=True) as conn,
        pytest.raises(psycopg.errors.CheckViolation),
    ):
        conn.execute(
            "INSERT INTO subjects (user_ref, discovery_eligible, eligibility_reason)"
            " VALUES (%s, true, 'minor_discovery_deferred')",
            (uuid4(),),
        )


async def test_the_database_refuses_an_unknown_eligibility_reason(
    migrated_db: str,
) -> None:
    with (
        psycopg.connect(migrated_db, autocommit=True) as conn,
        pytest.raises(psycopg.errors.CheckViolation),
    ):
        conn.execute(
            "INSERT INTO subjects (user_ref, discovery_eligible, eligibility_reason)"
            " VALUES (%s, false, 'parental_request')",
            (uuid4(),),
        )


async def test_a_refusal_writes_exactly_one_audit_row_and_nothing_else(
    store: PostgresSubjectStore, migrated_db: str
) -> None:
    user_ref = UserRef(uuid4())

    await store.record_discovery_refusal(
        user_ref, outcome="discovery_not_available", metadata={"min_discovery_age": 18}
    )

    with psycopg.connect(migrated_db, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT actor_type, action, subject_ref, metadata FROM audit_log"
        ).fetchall()
        # Nothing else, in particular no search_runs row: a run with zero
        # results reads as "we looked and found nothing".
        assert conn.execute("SELECT count(*) FROM search_runs").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM provider_calls").fetchone() == (0,)
    assert len(rows) == 1
    actor_type, action, subject_ref, metadata = rows[0]
    assert (actor_type, action, subject_ref) == ("service", "discovery.refused", user_ref)
    assert metadata["outcome"] == "discovery_not_available"
    assert metadata["min_discovery_age"] == 18
