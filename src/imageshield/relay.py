"""Outbox relay: the consumer side of the transactional outbox (Task 4).

Runs as a **separate process** (``python -m imageshield.relay``) — never
imported by the HTTP app, which is why the ``TID251`` boto3 ban
(``pyproject.toml``) carries a per-file ignore for exactly this module. The
HTTP app and every other producer write outbox rows on their own connection
(:mod:`imageshield.outbox`); this module is the only place that reads them
back out and calls ``SendMessage``.

Poll loop, once per cycle, up to ``outbox_batch_size`` rows:

1. ``SELECT ... LIMIT 1 FOR UPDATE SKIP LOCKED`` the single next eligible row
   — ``SKIP LOCKED`` means two relay processes running concurrently never
   double-send the same row.
2. **Publish first, mark second, commit per row** — call ``SendMessage``, and
   only once that returns does the row get ``published_at = now()``; that
   single row's transaction is committed immediately, before moving on. A
   crash between send and mark leaves that one row unpublished, so the next
   poll retries it — but every row already committed before the crash stays
   published. The blast radius of a mid-batch crash is exactly one row, never
   the whole batch. Duplicates are the expected outcome of at-least-once
   delivery; every consumer downstream must be idempotent (CLAUDE.md §10) —
   this module does not try to prevent duplicates, only to never lose a row.
3. Each row's ``SELECT ... FOR UPDATE`` lock is held only for that one row's
   ``SendMessage`` round-trip, not for the whole batch's worth of network
   calls — a slow or stalled provider blocks at most one row's lock, not up
   to ``outbox_batch_size`` of them sitting open against Postgres at once. A
   row whose send fails does not stall the rest of the batch: the failure is
   caught, recorded (``attempts += 1``, ``last_error``), committed, and the
   loop moves to the next row.
4. A row that fails is not retried immediately. Backoff is tracked as
   ``outbox_id -> earliest-retry monotonic time`` **in process memory only**
   (``base * 2 ** attempts``, capped at 5 minutes). The DDL
   (``migrations/0001_initial_schema.up.sql``) has no ``next_attempt_at``
   column — that is a deliberate, verbatim spec choice, not an oversight —
   so **a relay restart resets every row's backoff to zero**. That is an
   accepted tradeoff: worst case after a restart is a burst of eager retries
   against rows that were already going to be retried anyway, not lost
   messages or lost dead-letter state (dead-letter status is durable, in the
   `attempts` column itself, not in memory — see below).
5. Rows reach `outbox_max_attempts` are dead letters. The poll query already
   excludes them (`attempts < outbox_max_attempts`), so they stop being
   touched entirely. The transition into dead-letter state is logged once,
   at error level, at the exact moment `attempts` reaches the ceiling —
   because after that moment the row is never selected again, so this branch
   can only run for a given row a single time. Ops finds dead rows via
   ``published_at IS NULL AND attempts >= outbox_max_attempts``; the log line
   is a bell, that query is the durable source of truth.

``run_forever`` reconnects on DB connectivity loss. A ``psycopg.OperationalError``
(connection blip, Postgres restart) is caught at the reconnect loop around
``poll_once``/the connection itself — never left to unwind the process — and
retried with exponential backoff (base 1s, capped at 30s), reset to the base
after every successful poll. This process is the only outbox→SQS path, so a
transient DB hiccup must not be fatal. This does **not** replace a process
supervisor: anything other than ``psycopg.OperationalError`` (an unhandled
bug, an out-of-memory kill, ...) still crashes the process, and a supervisor
(systemd unit, ECS service restart policy) is expected to bring it back up.
"""

from __future__ import annotations

import json
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import psycopg
import structlog

from imageshield.config import Config, ConfigError, load_config
from imageshield.http.logging import configure_logging
from imageshield.outbox import QUEUE_IDENTITY_INDEX, QUEUE_SEARCH_RUNS

_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_CAP_SECONDS = 300.0  # ~5 minutes

_RECONNECT_BASE_SECONDS = 1.0
_RECONNECT_CAP_SECONDS = 30.0

_QUEUE_NAME_TO_CONFIG_FIELD = {
    QUEUE_IDENTITY_INDEX: "sqs_identity_index_url",
    QUEUE_SEARCH_RUNS: "sqs_search_runs_url",
}

_SELECT_NEXT_SQL = """
    SELECT outbox_id, queue_name, payload
    FROM outbox
    WHERE published_at IS NULL
      AND attempts < %(max_attempts)s
      AND NOT (outbox_id = ANY(%(skip_ids)s::bigint[]))
    ORDER BY outbox_id
    LIMIT 1
    FOR UPDATE SKIP LOCKED
"""

_MARK_PUBLISHED_SQL = """
    UPDATE outbox SET published_at = now(), last_error = NULL
    WHERE outbox_id = %(outbox_id)s
"""

_RECORD_FAILURE_SQL = """
    UPDATE outbox
    SET attempts = attempts + 1, last_error = %(error)s
    WHERE outbox_id = %(outbox_id)s
    RETURNING attempts
"""


class SqsClient(Protocol):
    """The one SQS operation this module needs, typed by hand.

    boto3 ships no bundled type stubs (no ``boto3-stubs`` dependency in this
    repo), so a hand-written ``Protocol`` is what keeps ``mypy --strict``
    happy *and* keeps the client injectable — tests pass a plain stub object
    that satisfies this shape, no moto/LocalStack required.
    """

    def send_message(self, *, QueueUrl: str, MessageBody: str) -> object: ...


def _localstack_endpoint_url(queue_url: str) -> str | None:
    """Derive ``endpoint_url`` for the boto3 client when a queue URL points at
    localhost/LocalStack; ``None`` for a real AWS endpoint (region routing is
    enough there)."""
    parsed = urlsplit(queue_url)
    host = parsed.hostname or ""
    if host in {"localhost", "127.0.0.1"} or "localstack" in host:
        return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    return None


def build_sqs_client(config: Config) -> SqsClient:
    """Default SQS client factory. The only function in this module allowed
    to import boto3 at call time; injected callers (tests) never need it."""
    import boto3

    kwargs: dict[str, Any] = {"region_name": config.aws_region}
    endpoint_url = _localstack_endpoint_url(config.sqs_identity_index_url)
    if endpoint_url is not None:
        kwargs["endpoint_url"] = endpoint_url
    client: SqsClient = boto3.client("sqs", **kwargs)
    return client


@dataclass
class BackoffTracker:
    """In-memory ``outbox_id -> earliest-retry time`` map. See the module
    docstring for why this is memory-only rather than a DB column."""

    _next_attempt_at: dict[int, float] = field(default_factory=dict)

    def blocked(self, outbox_id: int, *, now: float) -> bool:
        deadline = self._next_attempt_at.get(outbox_id)
        return deadline is not None and now < deadline

    def record_failure(self, outbox_id: int, attempts: int, *, now: float) -> None:
        delay = min(_BACKOFF_BASE_SECONDS * (2**attempts), _BACKOFF_CAP_SECONDS)
        self._next_attempt_at[outbox_id] = now + delay

    def clear(self, outbox_id: int) -> None:
        self._next_attempt_at.pop(outbox_id, None)


@dataclass(frozen=True)
class PollStats:
    published: int
    failed: int
    skipped_backoff: int


def _queue_url(config: Config, queue_name: str) -> str | None:
    field_name = _QUEUE_NAME_TO_CONFIG_FIELD.get(queue_name)
    if field_name is None:
        return None
    value = getattr(config, field_name)
    return value if isinstance(value, str) else None


def poll_once(
    conn: psycopg.Connection[Any],
    config: Config,
    client: SqsClient,
    backoff: BackoffTracker,
    *,
    logger: structlog.stdlib.BoundLogger | Any = None,
) -> PollStats:
    """Run one poll/publish cycle, up to ``outbox_batch_size`` rows.

    Each row gets its own transaction: fetch (``FOR UPDATE SKIP LOCKED``),
    publish, mark, **commit** — before the next row is even selected. This
    bounds both the blast radius of a mid-batch crash (one row, not the whole
    batch — see module docstring point 2) and how long any single row's lock
    is held (one ``SendMessage`` round-trip, not up to ``outbox_batch_size``
    of them).

    Safe to call repeatedly (e.g. from :func:`run_forever`, or directly from
    tests) — each call is a complete, self-contained cycle.
    """
    log = logger if logger is not None else structlog.get_logger("imageshield.relay")
    published = 0
    failed = 0
    skipped_backoff = 0
    # Rows seen-and-deferred (backoff) within *this* call, so the scan moves
    # on to the next distinct row instead of re-selecting the same blocked
    # one every iteration — bounded by outbox_batch_size either way.
    skip_ids: list[int] = []

    for _ in range(config.outbox_batch_size):
        now = time.monotonic()

        with conn.cursor() as cur:
            cur.execute(
                _SELECT_NEXT_SQL,
                {"max_attempts": config.outbox_max_attempts, "skip_ids": skip_ids},
            )
            row = cur.fetchone()

        if row is None:
            conn.rollback()  # nothing was written; ends the (empty) transaction
            break

        outbox_id, queue_name, payload = row

        if backoff.blocked(outbox_id, now=now):
            conn.rollback()  # release this row's lock; nothing was written
            skip_ids.append(outbox_id)
            skipped_backoff += 1
            continue

        try:
            queue_url = _queue_url(config, queue_name)
            if queue_url is None:
                raise ValueError(f"unknown queue_name {queue_name!r}")
            client.send_message(QueueUrl=queue_url, MessageBody=json.dumps(payload))
        except Exception as exc:  # broad on purpose: one bad row must never stall the batch
            with conn.cursor() as cur:
                cur.execute(_RECORD_FAILURE_SQL, {"outbox_id": outbox_id, "error": str(exc)})
                failure_row = cur.fetchone()
            conn.commit()
            assert failure_row is not None
            attempts = failure_row[0]
            backoff.record_failure(outbox_id, attempts, now=now)
            failed += 1
            log.warning(
                "outbox.publish_failed",
                outbox_id=outbox_id,
                queue_name=queue_name,
                attempts=attempts,
                error=str(exc),
            )
            if attempts >= config.outbox_max_attempts:
                # First (and only) time this row can ever hit this branch:
                # the poll query excludes attempts >= max on every future
                # cycle, so this row will never be selected again. Evict its
                # backoff entry now — record_failure() just added one above,
                # and with the row never selected again nothing will ever
                # call clear() for it otherwise, leaking one dict entry per
                # dead-lettered row for the life of the process.
                log.error(
                    "outbox.dead_letter",
                    outbox_id=outbox_id,
                    queue_name=queue_name,
                    attempts=attempts,
                )
                backoff.clear(outbox_id)
        else:
            with conn.cursor() as cur:
                cur.execute(_MARK_PUBLISHED_SQL, {"outbox_id": outbox_id})
            conn.commit()
            backoff.clear(outbox_id)
            published += 1

    return PollStats(published=published, failed=failed, skipped_backoff=skipped_backoff)


def _reconnect_and_poll_forever(
    config: Config,
    client: SqsClient,
    backoff: BackoffTracker,
    log: Any,
    *,
    stop_requested: Callable[[], bool],
    connect: Callable[[], psycopg.Connection[Any]],
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """The reconnect-with-backoff outer loop, factored out of
    :func:`run_forever` so it's testable without a real Postgres outage or a
    real clock: inject ``connect``/``sleep``/``stop_requested``.

    A ``psycopg.OperationalError`` raised either by ``connect()`` itself or
    by a ``poll_once`` call on the connection it opened (a connection blip,
    Postgres restarting) is caught here — logged at error level, then
    retried after an exponentially growing sleep (capped at
    ``_RECONNECT_CAP_SECONDS``). The backoff resets to
    ``_RECONNECT_BASE_SECONDS`` after every successful poll, so a brief
    outage doesn't leave the relay sleeping 30s between polls forever
    afterwards. Anything other than ``OperationalError`` propagates and is
    fatal — see the module docstring on the supervisor expectation.
    """
    reconnect_delay = _RECONNECT_BASE_SECONDS
    while not stop_requested():
        try:
            with connect() as conn:
                while not stop_requested():
                    stats = poll_once(conn, config, client, backoff, logger=log)
                    reconnect_delay = _RECONNECT_BASE_SECONDS
                    if stats.published or stats.failed:
                        log.info(
                            "relay.poll_completed",
                            published=stats.published,
                            failed=stats.failed,
                            skipped_backoff=stats.skipped_backoff,
                        )
                    sleep(config.outbox_poll_interval_seconds)
        except psycopg.OperationalError as exc:
            log.error(
                "relay.db_connection_error",
                error=str(exc),
                retry_in_seconds=reconnect_delay,
            )
            sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, _RECONNECT_CAP_SECONDS)


def run_forever(config: Config, *, client: SqsClient | None = None) -> None:
    """The real relay loop: connect, poll, sleep, repeat, until asked to
    stop — reconnecting with backoff on DB connectivity loss rather than
    dying (see the module docstring and :func:`_reconnect_and_poll_forever`).
    """
    log = structlog.get_logger("imageshield.relay")
    sqs_client = client if client is not None else build_sqs_client(config)
    backoff = BackoffTracker()

    stop_requested = False

    def _handle_stop(signum: int, _frame: object) -> None:
        nonlocal stop_requested
        log.info("relay.stop_requested", signal=signum)
        stop_requested = True

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    log.info(
        "relay.started",
        poll_interval_seconds=config.outbox_poll_interval_seconds,
        batch_size=config.outbox_batch_size,
        max_attempts=config.outbox_max_attempts,
    )
    _reconnect_and_poll_forever(
        config,
        sqs_client,
        backoff,
        log,
        stop_requested=lambda: stop_requested,
        connect=lambda: psycopg.connect(config.database_url),
    )
    log.info("relay.stopped")


def main() -> int:
    configure_logging()
    try:
        config = load_config()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    run_forever(config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
