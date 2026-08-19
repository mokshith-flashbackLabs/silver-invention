"""Transactional outbox helper (CLAUDE.md §2, §10).

The outbox pattern used across this repo: every producer writes its domain
row and its outbox row in **one transaction**, on the connection it already
has open. A separate relay process (Task 4, ``src/imageshield/relay.py``)
polls unpublished rows and publishes them to SQS. :func:`enqueue` /
:func:`enqueue_sync` are the ONLY legal enqueue path — neither opens a
connection, commits, or touches SQS; the caller's transaction boundary
decides whether the outbox row survives alongside the domain write it
accompanies. The ruff ``TID251`` banned-api rule (``pyproject.toml``) blocks
importing ``boto3`` anywhere outside the relay module, so a direct
``SendMessage`` from a request handler can't slip in and defeat the pattern.

Messages carry IDs, never payloads (CLAUDE.md §10): :class:`OutboxPayload`
enforces exactly two fields, ``event`` and ``id``. Workers re-read
authoritative state from Postgres rather than trusting anything richer
smuggled onto the queue message; the stored row wins.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict

# The three known queues (CLAUDE.md §2). They map to
# Config.sqs_identity_index_url / Config.sqs_search_runs_url /
# Config.sqs_confirm_hits_url respectively — that mapping is consumed by the
# relay (Task 4); this module only owns the canonical spelling so producers
# and the relay can't drift apart on it.
QUEUE_IDENTITY_INDEX = "identity:index"
QUEUE_SEARCH_RUNS = "search:runs"
# The confirm pipeline (protection-score design doc §7, migration 0021):
# review-band infringements meeting per-provider "most similar" criteria
# enqueue here for Rekognition-based triage ahead of human review.
QUEUE_CONFIRM_HITS = "confirm:hits"
QUEUES: frozenset[str] = frozenset({QUEUE_IDENTITY_INDEX, QUEUE_SEARCH_RUNS, QUEUE_CONFIRM_HITS})

_INSERT_SQL = """
    INSERT INTO outbox (queue_name, payload)
    VALUES (%(queue_name)s, %(payload)s)
    RETURNING outbox_id
"""


class OutboxPayload(BaseModel):
    """Messages carry IDs, never payloads (CLAUDE.md §10).

    Exactly two fields, nothing else: a worker reads ``id`` back off this
    payload and re-reads the authoritative row from Postgres rather than
    trusting anything the queue message happens to carry.
    """

    model_config = ConfigDict(extra="forbid")

    event: str
    id: UUID


def _validate_queue_name(queue_name: str) -> None:
    if queue_name not in QUEUES:
        raise ValueError(
            f"unknown queue_name {queue_name!r}; must be one of {sorted(QUEUES)}"
        )


async def enqueue(
    conn: psycopg.AsyncConnection[Any], queue_name: str, payload: OutboxPayload
) -> int:
    """Insert an outbox row on the caller's connection, inside the caller's
    open transaction.

    Does not commit, does not open a connection, does not touch SQS. Raises
    ``ValueError`` for an unrecognised ``queue_name`` before executing any
    SQL. Returns the new row's ``outbox_id``.
    """
    _validate_queue_name(queue_name)
    async with conn.cursor() as cur:
        await cur.execute(
            _INSERT_SQL,
            {"queue_name": queue_name, "payload": Jsonb(payload.model_dump(mode="json"))},
        )
        row = await cur.fetchone()
    assert row is not None, "INSERT ... RETURNING must yield exactly one row"
    return int(row[0])


def enqueue_sync(
    conn: psycopg.Connection[Any], queue_name: str, payload: OutboxPayload
) -> int:
    """Sync twin of :func:`enqueue`, for non-async callers (e.g. scripts)."""
    _validate_queue_name(queue_name)
    with conn.cursor() as cur:
        cur.execute(
            _INSERT_SQL,
            {"queue_name": queue_name, "payload": Jsonb(payload.model_dump(mode="json"))},
        )
        row = cur.fetchone()
    assert row is not None, "INSERT ... RETURNING must yield exactly one row"
    return int(row[0])
