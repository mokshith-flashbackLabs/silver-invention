"""The recheck store's SQL, against real Postgres.

The two timestamps are the thing under test. ``last_checked_at`` means "we
learned something" and is exposed to the proxy; ``last_attempted_at`` means "we
tried" and exists so a permanently unreachable host cannot pin the front of
every batch forever (migration 0013).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import psycopg
import pytest

from imageshield.db.connection import make_async_pool
from imageshield.recheck.store import PostgresRecheckStore
from imageshield.types import UserRef
from tests.db import ensure_subject, run_migrate


@pytest.fixture
def migrated_db(throwaway_db: str) -> str:
    run_migrate(throwaway_db, "down", "--all")
    up_result = run_migrate(throwaway_db, "up")
    assert up_result.returncode == 0, up_result.stderr
    return throwaway_db


@pytest.fixture
async def store(migrated_db: str) -> AsyncIterator[PostgresRecheckStore]:
    pool = make_async_pool(migrated_db, min_size=1, max_size=2)
    await pool.open()
    try:
        yield PostgresRecheckStore(pool)
    finally:
        await pool.close()


async def _infringement(
    store: PostgresRecheckStore,
    db_url: str,
    *,
    domain: str = "example.test",
    checked_days_ago: int | None = None,
    attempted_days_ago: int | None = None,
    alive: bool = True,
) -> UUID:
    owner = UserRef(uuid4())
    await ensure_subject(store._pool, owner)
    url_hash = uuid4().hex + uuid4().hex
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO content_urls (url_hash, url, source_domain)"
            " VALUES (%s, %s, %s)",
            (url_hash, f"https://{domain}/p", domain),
        )
        row = conn.execute(
            "INSERT INTO infringements"
            " (user_ref, url_hash, page_url, url_alive, last_checked_at,"
            "  last_attempted_at)"
            " VALUES (%s, %s, %s, %s,"
            "  CASE WHEN %s::int IS NULL THEN NULL"
            "       ELSE now() - make_interval(days => %s::int) END,"
            "  CASE WHEN %s::int IS NULL THEN NULL"
            "       ELSE now() - make_interval(days => %s::int) END)"
            " RETURNING infringement_id",
            (
                owner,
                url_hash,
                f"https://{domain}/p",
                alive,
                checked_days_ago,
                checked_days_ago,
                attempted_days_ago,
                attempted_days_ago,
            ),
        ).fetchone()
    assert row is not None
    infringement_id: UUID = row[0]
    return infringement_id


def _row(db_url: str, infringement_id: UUID) -> tuple[bool, object, object]:
    with psycopg.connect(db_url, autocommit=True) as conn:
        row = conn.execute(
            "SELECT url_alive, last_checked_at, last_attempted_at FROM infringements"
            " WHERE infringement_id = %s",
            (infringement_id,),
        ).fetchone()
    assert row is not None
    return row


# ── which rows are due ───────────────────────────────────────────────────────


async def test_a_never_checked_live_row_is_due(
    store: PostgresRecheckStore, migrated_db: str
) -> None:
    infringement_id = await _infringement(store, migrated_db)
    batch = await store.due_batch(interval_days=7, limit=10)
    assert [item.infringement_id for item in batch] == [infringement_id]
    assert batch[0].source_domain == "example.test"


async def test_a_recently_checked_row_is_not_due(
    store: PostgresRecheckStore, migrated_db: str
) -> None:
    await _infringement(store, migrated_db, checked_days_ago=1)
    assert await store.due_batch(interval_days=7, limit=10) == ()


async def test_a_dead_url_is_never_probed_again(
    store: PostgresRecheckStore, migrated_db: str
) -> None:
    """Resurrection is real but rare, and the row already told the user the
    truth at the time. Re-probing every dead URL forever is not worth it."""
    await _infringement(store, migrated_db, alive=False)
    assert await store.due_batch(interval_days=7, limit=10) == ()


async def test_a_failing_row_does_not_starve_the_queue(
    store: PostgresRecheckStore, migrated_db: str
) -> None:
    """The reason migration 0013 exists.

    A host that never resolves keeps its NULL ``last_checked_at`` forever.
    Ordered by that alone it would sit at the front of every batch, and once
    there are more such rows than the batch size, nothing else is EVER checked
    — a loop that looks healthy and has silently stopped working.

    Ordering by ``last_attempted_at`` puts the just-failed row behind the one
    nobody has tried yet.
    """
    just_failed = await _infringement(
        store, migrated_db, domain="broken.test", attempted_days_ago=0
    )
    never_tried = await _infringement(store, migrated_db, domain="fresh.test")

    batch = await store.due_batch(interval_days=7, limit=10)

    assert [item.infringement_id for item in batch] == [never_tried, just_failed]


async def test_the_batch_size_is_respected(
    store: PostgresRecheckStore, migrated_db: str
) -> None:
    for index in range(5):
        await _infringement(store, migrated_db, domain=f"d{index}.test")
    assert len(await store.due_batch(interval_days=7, limit=2)) == 2


# ── the allowlist comes from our own data ────────────────────────────────────


async def test_allowed_domains_is_every_domain_in_content_urls(
    store: PostgresRecheckStore, migrated_db: str
) -> None:
    await _infringement(store, migrated_db, domain="a.test")
    await _infringement(store, migrated_db, domain="b.test")
    assert await store.allowed_domains() == frozenset({"a.test", "b.test"})


# ── recording a verdict ──────────────────────────────────────────────────────


async def test_dead_sets_url_alive_false_and_both_clocks(
    store: PostgresRecheckStore, migrated_db: str
) -> None:
    infringement_id = await _infringement(store, migrated_db)

    await store.record_verdict(infringement_id, "dead")

    alive, checked, attempted = _row(migrated_db, infringement_id)
    assert alive is False
    assert checked is not None
    assert attempted is not None


async def test_alive_moves_both_clocks_and_leaves_url_alive_true(
    store: PostgresRecheckStore, migrated_db: str
) -> None:
    infringement_id = await _infringement(store, migrated_db)

    await store.record_verdict(infringement_id, "alive")

    alive, checked, attempted = _row(migrated_db, infringement_id)
    assert alive is True
    assert checked is not None
    assert attempted is not None


async def test_unchanged_moves_only_the_attempt_clock(
    store: PostgresRecheckStore, migrated_db: str
) -> None:
    """A probe that timed out, hit a 5xx, or was refused learned NOTHING.

    Stamping ``last_checked_at`` would make the row read as freshly verified —
    and it is the field the proxy uses to decide whether it may tell a user
    "this came down".
    """
    infringement_id = await _infringement(store, migrated_db)

    await store.record_verdict(infringement_id, "unchanged")

    alive, checked, attempted = _row(migrated_db, infringement_id)
    assert alive is True
    assert checked is None, "an unreachable host must not look freshly verified"
    assert attempted is not None


async def test_a_verdict_never_deletes_the_row(
    store: PostgresRecheckStore, migrated_db: str
) -> None:
    """A dead URL is still evidence, and the user has already been told about
    it. Erasing it would delete the record of what happened to them."""
    infringement_id = await _infringement(store, migrated_db)

    await store.record_verdict(infringement_id, "dead")

    with psycopg.connect(migrated_db, autocommit=True) as conn:
        count = conn.execute(
            "SELECT count(*) FROM infringements WHERE infringement_id = %s",
            (infringement_id,),
        ).fetchone()
    assert count == (1,)
