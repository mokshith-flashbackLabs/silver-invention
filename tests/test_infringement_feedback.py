"""User feedback on a hit — the store, against real Postgres.

Store-level rather than through ``TestClient``, following the repo convention
(``tests/conftest.py``): route behaviour is proven over an in-memory fake in
``tests/test_search_routes.py``, and everything that needs a real database is
proven here. That split matters more than usual for this feature, because two
of the done-when items are assertions about tables this code never names, and a
fake can prove nothing about those.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from imageshield.db.connection import make_async_pool
from imageshield.search.store import PostgresSearchStore
from imageshield.types import UserRef
from tests.db import ensure_subject, run_migrate


@pytest.fixture
def migrated_db(throwaway_db: str) -> str:
    run_migrate(throwaway_db, "down", "--all")
    up_result = run_migrate(throwaway_db, "up")
    assert up_result.returncode == 0, up_result.stderr
    return throwaway_db


@pytest.fixture
async def store(migrated_db: str) -> AsyncIterator[PostgresSearchStore]:
    pool = make_async_pool(migrated_db, min_size=1, max_size=2)
    await pool.open()
    try:
        yield PostgresSearchStore(pool)
    finally:
        await pool.close()


async def _an_infringement(
    store: PostgresSearchStore, db_url: str
) -> tuple[UUID, UserRef]:
    """One content_url + one infringement, written directly.

    Direct SQL rather than through a provider run: this is about a hit that
    already exists, and threading a whole run through here would make the test
    about the run.
    """
    owner = UserRef(uuid4())
    await ensure_subject(store._pool, owner)
    url_hash = uuid4().hex + uuid4().hex  # 64 hex chars
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO content_urls (url_hash, url, source_domain)"
            " VALUES (%s, 'https://example.test/p', 'example.test')",
            (url_hash,),
        )
        row = conn.execute(
            "INSERT INTO infringements (user_ref, url_hash, page_url)"
            " VALUES (%s, %s, 'https://example.test/p') RETURNING infringement_id",
            (owner, url_hash),
        ).fetchone()
    assert row is not None
    infringement_id: UUID = row[0]
    return infringement_id, owner


def _checksum(db_url: str, table: str) -> Any:
    """A digest of an entire table's contents, order-independent.

    ``sum(hashtext(t::text))`` over every row: any insert, delete, or
    single-column edit anywhere in the table moves it. More complete than
    enumerating the columns a test happens to think of — which is the point,
    since the risk being guarded against is a future writer touching something
    nobody listed.
    """
    with psycopg.connect(db_url, autocommit=True) as conn:
        row = conn.execute(
            f"SELECT count(*), coalesce(sum(hashtext(t::text)), 0) FROM {table} t"
        ).fetchone()
    return row


# ── "not yours" and "not there" are one answer ───────────────────────────────


async def test_a_foreign_infringement_and_a_missing_one_both_return_none(
    store: PostgresSearchStore, migrated_db: str
) -> None:
    """Done-when, store half: the two conditions are indistinguishable at the
    source. The route cannot leak a difference it is never told.

    ``user_ref`` is in the WHERE clause rather than checked afterwards, so
    there is no branch that could drift apart later. A caller able to tell
    "not yours" from "not there" could walk the id space and learn that a given
    infringement exists — which for this data discloses somebody's abuse.
    """
    infringement_id, _owner = await _an_infringement(store, migrated_db)
    intruder = UserRef(uuid4())

    not_yours = await store.record_feedback(infringement_id, intruder, "confirmed")
    not_there = await store.record_feedback(uuid4(), intruder, "confirmed")

    assert not_yours is None
    assert not_there is None


async def test_a_refused_feedback_writes_nothing(
    store: PostgresSearchStore, migrated_db: str
) -> None:
    infringement_id, _owner = await _an_infringement(store, migrated_db)

    await store.record_feedback(infringement_id, UserRef(uuid4()), "not_me")

    assert _checksum(migrated_db, "infringement_feedback")[0] == 0
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        status = conn.execute(
            "SELECT status FROM infringements WHERE infringement_id = %s",
            (infringement_id,),
        ).fetchone()
    assert status == ("new",)


# ── the signal → status mapping ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("signal", "expected"),
    [
        ("not_me", "dismissed_not_me"),
        ("confirmed", "acknowledged"),
        ("uncertain", "new"),  # unchanged, but still recorded
    ],
)
async def test_each_signal_sets_the_specified_status(
    store: PostgresSearchStore, migrated_db: str, signal: str, expected: str
) -> None:
    infringement_id, owner = await _an_infringement(store, migrated_db)

    returned = await store.record_feedback(infringement_id, owner, signal)

    assert returned == expected
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        stored = conn.execute(
            "SELECT status FROM infringements WHERE infringement_id = %s",
            (infringement_id,),
        ).fetchone()
        recorded = conn.execute(
            "SELECT signal FROM infringement_feedback WHERE infringement_id = %s",
            (infringement_id,),
        ).fetchall()
    assert stored == (expected,)
    # 'uncertain' leaves the status alone but is STILL written — the user
    # looked and could not tell, which is a real answer a reviewer needs.
    assert [row[0] for row in recorded] == [signal]


# ── not_me is recorded and NOT acted on ──────────────────────────────────────


async def test_not_me_provably_touches_nothing_but_feedback_and_status(
    store: PostgresSearchStore, migrated_db: str
) -> None:
    """Done-when: `not_me` writes a feedback row, sets status, and provably does
    NOT touch enrolments, attestations, or any band — asserted with row
    checksums before and after.

    This is the invariant with a human cost behind it. Users reject TRUE
    positives under distress, and it is common. If a rejection retrained the
    identity index or suppressed the domain, the users most affected by real
    abuse would systematically degrade their own protection — invisibly, and
    concentrated on exactly the people this product exists for.

    Whole-table checksums rather than named columns, so a future writer that
    starts adjusting something nobody thought to assert on still trips this.
    """
    infringement_id, owner = await _an_infringement(store, migrated_db)
    watched = ("enrolments", "attestations", "content_urls", "subjects", "search_runs")
    before = {table: _checksum(migrated_db, table) for table in watched}
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        band_before = conn.execute(
            "SELECT band, band_reason, url_alive, last_checked_at FROM infringements"
            " WHERE infringement_id = %s",
            (infringement_id,),
        ).fetchone()

    assert await store.record_feedback(infringement_id, owner, "not_me") == (
        "dismissed_not_me"
    )

    after = {table: _checksum(migrated_db, table) for table in watched}
    assert after == before, "not_me reached a table it must never touch"
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        band_after = conn.execute(
            "SELECT band, band_reason, url_alive, last_checked_at FROM infringements"
            " WHERE infringement_id = %s",
            (infringement_id,),
        ).fetchone()
        feedback = conn.execute(
            "SELECT signal FROM infringement_feedback WHERE infringement_id = %s",
            (infringement_id,),
        ).fetchall()
    # The band is untouched: rejection does not feed banding.
    assert band_after == band_before
    # And the signal IS kept — this is "do not act on it", not "discard it".
    assert [row[0] for row in feedback] == ["not_me"]


async def test_a_contradictory_second_feedback_appends_and_leaves_the_first(
    store: PostgresSearchStore, migrated_db: str
) -> None:
    """Done-when: a second, contradictory feedback writes a second row; the
    first is not modified.

    Someone dismissing a hit and then coming back to confirm it is the case
    this table exists for. An UPSERT would erase the part a reviewer needs.
    """
    infringement_id, owner = await _an_infringement(store, migrated_db)

    first = await store.record_feedback(infringement_id, owner, "not_me")
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        original = conn.execute(
            "SELECT feedback_id, signal, created_at FROM infringement_feedback"
        ).fetchone()

    second = await store.record_feedback(infringement_id, owner, "confirmed")

    assert first == "dismissed_not_me"
    assert second == "acknowledged"
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT feedback_id, signal, created_at FROM infringement_feedback"
            " ORDER BY created_at"
        ).fetchall()
        status = conn.execute(
            "SELECT status FROM infringements WHERE infringement_id = %s",
            (infringement_id,),
        ).fetchone()
    assert len(rows) == 2
    assert rows[0] == original, "the first row was modified"
    assert [row[1] for row in rows] == ["not_me", "confirmed"]
    assert status == ("acknowledged",)  # the latest word wins on the status


async def test_feedback_survives_being_given_twice_with_the_same_signal(
    store: PostgresSearchStore, migrated_db: str
) -> None:
    """Append-only means append-only. A double-tap in the UI writes two rows and
    is not an error — there is no UNIQUE to violate, by design."""
    infringement_id, owner = await _an_infringement(store, migrated_db)

    await store.record_feedback(infringement_id, owner, "not_me")
    await store.record_feedback(infringement_id, owner, "not_me")

    assert _checksum(migrated_db, "infringement_feedback")[0] == 2
