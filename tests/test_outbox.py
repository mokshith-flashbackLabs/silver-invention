"""Transactional outbox helper tests (Task 3), DB-backed via the Task 1 harness.

Runs its own ``down --all`` + ``up`` as its arrange step (same coordination
convention as ``tests/test_schema_lint.py``), so this file doesn't fight
``tests/test_migrations.py``'s own down/up choreography for the shared
session-scoped throwaway database, and doesn't depend on execution order
either.

``imageshield.outbox.enqueue`` is async and runs real queries against real
Postgres over psycopg's async I/O, which cannot run on Windows' default
Proactor event loop; ``tests/conftest.py`` installs a selector-loop factory
via the ``pytest_asyncio_loop_factories`` hook (Windows only) so every async
test in the suite, this file included, gets a compatible loop with no
per-file setup here.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import psycopg
import pytest
from pydantic import ValidationError

from imageshield.outbox import (
    QUEUE_CONFIRM_HITS,
    QUEUE_IDENTITY_INDEX,
    QUEUES,
    OutboxPayload,
    enqueue,
    enqueue_sync,
)
from tests.db import run_migrate


@pytest.fixture
def migrated_db(throwaway_db: str) -> str:
    run_migrate(throwaway_db, "down", "--all")
    up_result = run_migrate(throwaway_db, "up")
    assert up_result.returncode == 0, up_result.stderr
    return throwaway_db


async def _outbox_row(
    dsn: str, outbox_id: int
) -> tuple[str, dict[str, object], object, int] | None:
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        cur = await conn.execute(
            "SELECT queue_name, payload, published_at, attempts "
            "FROM outbox WHERE outbox_id = %s",
            (outbox_id,),
        )
        row = await cur.fetchone()
        return row  # type: ignore[no-any-return]


async def test_enqueue_rolled_back_transaction_leaves_no_row(migrated_db: str) -> None:
    """The rolled-back-txn 'done when' case: enqueue inside a transaction
    that rolls back must leave the outbox with no trace of the row."""
    event_id = uuid4()

    async with await psycopg.AsyncConnection.connect(migrated_db) as conn:
        outbox_id = await enqueue(
            conn, QUEUE_IDENTITY_INDEX, OutboxPayload(event="enrolment.created", id=event_id)
        )
        assert outbox_id > 0
        await conn.rollback()

    row = await _outbox_row(migrated_db, outbox_id)
    assert row is None, "rolled-back enqueue must not leave an outbox row behind"


async def test_enqueue_committed_transaction_writes_exactly_one_row(migrated_db: str) -> None:
    """Committed enqueue: exactly one row, unpublished, zero attempts, and the
    payload round-trips through the real JSONB column."""
    event_id = uuid4()
    payload = OutboxPayload(event="enrolment.created", id=event_id)

    async with await psycopg.AsyncConnection.connect(migrated_db) as conn:
        outbox_id = await enqueue(conn, QUEUE_IDENTITY_INDEX, payload)
        await conn.commit()

    row = await _outbox_row(migrated_db, outbox_id)
    assert row is not None
    queue_name, stored_payload, published_at, attempts = row
    assert queue_name == QUEUE_IDENTITY_INDEX
    assert published_at is None
    assert attempts == 0
    assert stored_payload == {"event": "enrolment.created", "id": str(event_id)}
    # And it really round-trips back into the pydantic model, not just as a
    # dict that happens to look right.
    assert OutboxPayload.model_validate(stored_payload) == payload

    async with await psycopg.AsyncConnection.connect(migrated_db, autocommit=True) as conn:
        (count,) = await (
            await conn.execute("SELECT count(*) FROM outbox WHERE outbox_id = %s", (outbox_id,))
        ).fetchone()  # type: ignore[misc]
    assert count == 1


async def test_enqueue_unknown_queue_name_raises_before_insert(migrated_db: str) -> None:
    async with await psycopg.AsyncConnection.connect(migrated_db) as conn:
        with pytest.raises(ValueError, match="unknown queue_name"):
            await enqueue(
                conn, "not-a-real-queue", OutboxPayload(event="enrolment.created", id=uuid4())
            )
        await conn.rollback()

    async with await psycopg.AsyncConnection.connect(migrated_db, autocommit=True) as conn:
        (count,) = await (
            await conn.execute("SELECT count(*) FROM outbox")
        ).fetchone()  # type: ignore[misc]
    assert count == 0, "an unknown queue_name must raise before any INSERT runs"


def test_outbox_payload_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        OutboxPayload(event="enrolment.created", id=uuid4(), extra_field="not allowed")  # type: ignore[call-arg]


def test_outbox_payload_requires_event_and_id() -> None:
    with pytest.raises(ValidationError):
        OutboxPayload(event="enrolment.created")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        OutboxPayload(id=uuid4())  # type: ignore[call-arg]


def test_queues_contains_exactly_the_three_known_queues() -> None:
    assert frozenset({"identity:index", "search:runs", "confirm:hits"}) == QUEUES
    assert "confirm:hits" in QUEUES


def test_outbox_payload_id_is_uuid_type() -> None:
    payload = OutboxPayload(event="x", id=uuid4())
    assert isinstance(payload.id, UUID)


def test_enqueue_sync_writes_and_round_trips(migrated_db: str) -> None:
    """The sync twin, exercised the same way as the async path: committed
    write, one row, payload round-trips through the real JSONB column."""
    event_id = uuid4()
    payload = OutboxPayload(event="search.run_requested", id=event_id)

    with psycopg.connect(migrated_db) as conn:
        outbox_id = enqueue_sync(conn, "search:runs", payload)
        conn.commit()

    with psycopg.connect(migrated_db, autocommit=True) as conn:
        row = conn.execute(
            "SELECT queue_name, payload, published_at, attempts "
            "FROM outbox WHERE outbox_id = %s",
            (outbox_id,),
        ).fetchone()

    assert row is not None
    queue_name, stored_payload, published_at, attempts = row
    assert queue_name == "search:runs"
    assert published_at is None
    assert attempts == 0
    assert OutboxPayload.model_validate(stored_payload) == payload


def test_enqueue_sync_confirm_hits_round_trips(migrated_db: str) -> None:
    """The third queue (Task 4, protection-score design doc §7) round-trips
    exactly like the other two: same INSERT, same validation, same table."""
    event_id = uuid4()
    payload = OutboxPayload(event="confirm.hit_requested", id=event_id)

    with psycopg.connect(migrated_db) as conn:
        outbox_id = enqueue_sync(conn, QUEUE_CONFIRM_HITS, payload)
        conn.commit()

    with psycopg.connect(migrated_db, autocommit=True) as conn:
        row = conn.execute(
            "SELECT queue_name, payload, published_at, attempts "
            "FROM outbox WHERE outbox_id = %s",
            (outbox_id,),
        ).fetchone()

    assert row is not None
    queue_name, stored_payload, published_at, attempts = row
    assert queue_name == QUEUE_CONFIRM_HITS
    assert published_at is None
    assert attempts == 0
    assert OutboxPayload.model_validate(stored_payload) == payload
