"""Outbox relay tests (Task 4), DB-backed via the Task 1 harness, stub SQS
client — no LocalStack dependency in CI.

Runs its own ``down --all`` + ``up`` as its arrange step, same convention as
``tests/test_outbox.py``, so this file doesn't fight the shared session-scoped
throwaway database's other consumers.

The relay is sync (a separate process, a sync psycopg connection), so these
tests need no asyncio at all — unlike ``tests/test_outbox.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from imageshield.outbox import QUEUE_IDENTITY_INDEX, QUEUE_SEARCH_RUNS, OutboxPayload, enqueue_sync
from imageshield.relay import (
    _RECONNECT_BASE_SECONDS,
    BackoffTracker,
    _reconnect_and_poll_forever,
    build_sqs_client,
    poll_once,
)
from tests.conftest import VALID_ENV, make_config
from tests.db import run_migrate


@pytest.fixture
def migrated_db(throwaway_db: str) -> str:
    run_migrate(throwaway_db, "down", "--all")
    up_result = run_migrate(throwaway_db, "up")
    assert up_result.returncode == 0, up_result.stderr
    return throwaway_db


class _RecordingLogger:
    """Stand-in for structlog's BoundLogger: records calls instead of emitting."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def info(self, event: str, **kwargs: Any) -> None:
        self.events.append(("info", event, kwargs))

    def warning(self, event: str, **kwargs: Any) -> None:
        self.events.append(("warning", event, kwargs))

    def error(self, event: str, **kwargs: Any) -> None:
        self.events.append(("error", event, kwargs))

    def of_level(self, level: str, event: str) -> list[dict[str, Any]]:
        return [kwargs for lvl, evt, kwargs in self.events if lvl == level and evt == event]


class StubSqsClient:
    """Always-succeeds stub: records every send, never talks to a network."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def send_message(self, *, QueueUrl: str, MessageBody: str) -> dict[str, str]:
        self.sent.append((QueueUrl, json.loads(MessageBody)))
        return {"MessageId": "stub-message-id"}


class AlwaysFailingSqsClient:
    """Every send raises — simulates SQS being unreachable/erroring."""

    def __init__(self, message: str = "simulated send failure") -> None:
        self.message = message
        self.calls = 0

    def send_message(self, *, QueueUrl: str, MessageBody: str) -> dict[str, str]:
        self.calls += 1
        raise RuntimeError(self.message)


class SelectiveFailingSqsClient:
    """Fails only for payloads whose ``id`` is in ``fail_ids``; succeeds for
    everything else. Used to prove one bad row doesn't stall the batch."""

    def __init__(self, fail_ids: set[str]) -> None:
        self.fail_ids = fail_ids
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def send_message(self, *, QueueUrl: str, MessageBody: str) -> dict[str, str]:
        body = json.loads(MessageBody)
        if body["id"] in self.fail_ids:
            raise RuntimeError(f"simulated failure for id {body['id']}")
        self.sent.append((QueueUrl, body))
        return {"MessageId": "stub-message-id"}


class _SimulatedCrash(BaseException):
    """Deliberately NOT an ``Exception`` subclass: it must escape relay.py's
    ``except Exception`` handler exactly the way a real process crash
    (SIGKILL, an unhandled fatal error) would — used only to prove the
    per-row commit boundary in tests, never a real relay failure mode."""


class CrashAfterNSendsClient:
    """Succeeds for the first ``n`` sends, then "crashes" (raises a
    BaseException that poll_once does not catch) on the next one."""

    def __init__(self, n: int) -> None:
        self.n = n
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def send_message(self, *, QueueUrl: str, MessageBody: str) -> dict[str, str]:
        if len(self.sent) >= self.n:
            raise _SimulatedCrash("process died mid-publish")
        self.sent.append((QueueUrl, json.loads(MessageBody)))
        return {"MessageId": "stub-message-id"}


def _relay_config(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "sqs_identity_index_url": VALID_ENV["SQS_IDENTITY_INDEX_URL"],
        "sqs_search_runs_url": VALID_ENV["SQS_SEARCH_RUNS_URL"],
        "outbox_batch_size": 10,
        "outbox_max_attempts": 8,
    }
    values.update(overrides)
    return make_config(**values)


def _insert_row(
    conn: psycopg.Connection[Any], queue_name: str, payload: dict[str, Any], *, attempts: int = 0
) -> int:
    """Raw INSERT (bypassing enqueue_sync's queue-name validation) so tests
    can also exercise an unknown queue_name and pre-set an attempts count."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO outbox (queue_name, payload, attempts) "
            "VALUES (%(queue_name)s, %(payload)s, %(attempts)s) RETURNING outbox_id",
            {"queue_name": queue_name, "payload": Jsonb(payload), "attempts": attempts},
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


def _row_state(
    conn: psycopg.Connection[Any], outbox_id: int
) -> tuple[Any, int, str | None]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT published_at, attempts, last_error FROM outbox WHERE outbox_id = %s",
            (outbox_id,),
        )
        row = cur.fetchone()
    assert row is not None
    return row  # type: ignore[return-value]


def test_happy_path_publishes_once_then_stops(migrated_db: str) -> None:
    config = _relay_config()
    payload = OutboxPayload(event="enrolment.created", id=uuid4())

    with psycopg.connect(migrated_db) as conn:
        outbox_id = enqueue_sync(conn, QUEUE_IDENTITY_INDEX, payload)
        conn.commit()

        client = StubSqsClient()
        backoff = BackoffTracker()
        logger = _RecordingLogger()

        stats = poll_once(conn, config, client, backoff, logger=logger)
        assert stats.published == 1
        assert stats.failed == 0
        assert len(client.sent) == 1
        sent_url, sent_body = client.sent[0]
        assert sent_url == config.sqs_identity_index_url
        assert sent_body == {"event": "enrolment.created", "id": str(payload.id)}

        published_at, attempts, last_error = _row_state(conn, outbox_id)
        assert published_at is not None
        assert attempts == 0
        assert last_error is None

        # Re-running the poll must publish nothing further.
        stats_again = poll_once(conn, config, client, backoff, logger=logger)
        assert stats_again.published == 0
        assert stats_again.failed == 0
        assert len(client.sent) == 1


def test_failing_client_records_attempts_then_later_poll_retries(migrated_db: str) -> None:
    config = _relay_config(outbox_max_attempts=8)
    payload = OutboxPayload(event="search.run_requested", id=uuid4())

    with psycopg.connect(migrated_db) as conn:
        outbox_id = enqueue_sync(conn, QUEUE_SEARCH_RUNS, payload)
        conn.commit()

        failing_client = AlwaysFailingSqsClient("boom: sqs down")
        stats = poll_once(conn, config, failing_client, BackoffTracker(), logger=_RecordingLogger())
        assert stats.published == 0
        assert stats.failed == 1
        assert failing_client.calls == 1

        published_at, attempts, last_error = _row_state(conn, outbox_id)
        assert published_at is None
        assert attempts == 1
        assert last_error is not None
        assert "boom: sqs down" in last_error

        # A later poll with a now-working client and a fresh BackoffTracker
        # (equivalent to the documented restart-resets-backoff tradeoff — see
        # relay.py's module docstring) retries and this time publishes.
        working_client = StubSqsClient()
        stats2 = poll_once(
            conn, config, working_client, BackoffTracker(), logger=_RecordingLogger()
        )
        assert stats2.published == 1
        assert stats2.failed == 0
        assert len(working_client.sent) == 1

        published_at2, attempts2, last_error2 = _row_state(conn, outbox_id)
        assert published_at2 is not None
        assert attempts2 == 1, "the successful publish must not bump attempts further"
        assert last_error2 is None, (
            "a row that failed once then published must not carry a stale last_error"
        )


def test_backoff_blocks_immediate_retry_with_same_tracker(migrated_db: str) -> None:
    """Same BackoffTracker instance reused across polls: the row must be
    skipped (not resent) on the very next poll, since backoff hasn't elapsed."""
    config = _relay_config()
    payload = OutboxPayload(event="enrolment.created", id=uuid4())

    with psycopg.connect(migrated_db) as conn:
        outbox_id = enqueue_sync(conn, QUEUE_IDENTITY_INDEX, payload)
        conn.commit()

        failing_client = AlwaysFailingSqsClient()
        backoff = BackoffTracker()
        poll_once(conn, config, failing_client, backoff, logger=_RecordingLogger())
        assert failing_client.calls == 1

        # Same tracker, same (still failing, though it won't even be called)
        # client: the row must be held back by backoff, not retried yet.
        stats = poll_once(conn, config, failing_client, backoff, logger=_RecordingLogger())
        assert stats.published == 0
        assert stats.failed == 0
        assert stats.skipped_backoff == 1
        assert failing_client.calls == 1, "backoff must prevent a same-cycle resend"

        _, attempts, _ = _row_state(conn, outbox_id)
        assert attempts == 1


def test_dead_row_never_selected(migrated_db: str) -> None:
    config = _relay_config(outbox_max_attempts=3)

    with psycopg.connect(migrated_db) as conn:
        dead_id = _insert_row(
            conn, QUEUE_IDENTITY_INDEX, {"event": "x", "id": str(uuid4())}, attempts=3
        )

        client = StubSqsClient()
        stats = poll_once(conn, config, client, BackoffTracker(), logger=_RecordingLogger())
        assert stats.published == 0
        assert stats.failed == 0
        assert client.sent == []

        published_at, attempts, _ = _row_state(conn, dead_id)
        assert published_at is None
        assert attempts == 3


def test_dead_letter_transition_logged_once(migrated_db: str) -> None:
    """attempts reaching outbox_max_attempts must log an error exactly once,
    at the moment of transition — not on every subsequent poll (it can't be,
    since the row stops being selected once dead)."""
    config = _relay_config(outbox_max_attempts=1)
    payload = OutboxPayload(event="enrolment.created", id=uuid4())

    with psycopg.connect(migrated_db) as conn:
        outbox_id = enqueue_sync(conn, QUEUE_IDENTITY_INDEX, payload)
        conn.commit()

        logger = _RecordingLogger()
        stats = poll_once(
            conn, config, AlwaysFailingSqsClient(), BackoffTracker(), logger=logger
        )
        assert stats.failed == 1

        dead_letter_events = logger.of_level("error", "outbox.dead_letter")
        assert len(dead_letter_events) == 1
        assert dead_letter_events[0]["outbox_id"] == outbox_id
        assert dead_letter_events[0]["queue_name"] == QUEUE_IDENTITY_INDEX

        # The row is now dead (attempts == outbox_max_attempts == 1); a
        # further poll must not select it again, hence cannot re-log it.
        logger2 = _RecordingLogger()
        poll_once(conn, config, AlwaysFailingSqsClient(), BackoffTracker(), logger=logger2)
        assert logger2.of_level("error", "outbox.dead_letter") == []


def test_mixed_batch_one_failure_does_not_block_others(migrated_db: str) -> None:
    config = _relay_config()
    ok_payload = OutboxPayload(event="enrolment.created", id=uuid4())
    fail_payload = OutboxPayload(event="enrolment.created", id=uuid4())

    with psycopg.connect(migrated_db) as conn:
        ok_id = enqueue_sync(conn, QUEUE_IDENTITY_INDEX, ok_payload)
        fail_id = enqueue_sync(conn, QUEUE_IDENTITY_INDEX, fail_payload)
        conn.commit()

        client = SelectiveFailingSqsClient(fail_ids={str(fail_payload.id)})
        stats = poll_once(conn, config, client, BackoffTracker(), logger=_RecordingLogger())

        assert stats.published == 1
        assert stats.failed == 1

        ok_published_at, ok_attempts, _ = _row_state(conn, ok_id)
        assert ok_published_at is not None
        assert ok_attempts == 0

        fail_published_at, fail_attempts, fail_error = _row_state(conn, fail_id)
        assert fail_published_at is None
        assert fail_attempts == 1
        assert fail_error is not None


def test_crash_mid_batch_leaves_earlier_row_published_and_later_row_untouched(
    migrated_db: str,
) -> None:
    """Per-row commit boundary, proven directly: row1 publishes and commits;
    the process then "dies" (a BaseException escapes poll_once, simulating a
    crash) while row2's SendMessage is in flight, before row2's SELECT ...
    FOR UPDATE transaction ever writes or commits anything. Row1 must survive
    the crash; row2 must be completely untouched — not even attempts bumped.
    """
    config = _relay_config()
    payload1 = OutboxPayload(event="enrolment.created", id=uuid4())
    payload2 = OutboxPayload(event="enrolment.created", id=uuid4())

    with psycopg.connect(migrated_db) as conn:
        id1 = enqueue_sync(conn, QUEUE_IDENTITY_INDEX, payload1)
        id2 = enqueue_sync(conn, QUEUE_IDENTITY_INDEX, payload2)
        conn.commit()

        client = CrashAfterNSendsClient(n=1)
        with pytest.raises(_SimulatedCrash):
            poll_once(conn, config, client, BackoffTracker(), logger=_RecordingLogger())
        assert len(client.sent) == 1, "row1's send must have happened before the crash"

        # Simulate the abrupt process death for real: drop the connection
        # without an explicit commit/rollback. Postgres itself rolls back
        # row2's still-open FOR UPDATE transaction on disconnect — exactly
        # what happens when a real process is killed mid-transaction.
        conn.close()

    with psycopg.connect(migrated_db) as verify_conn:
        published_at1, attempts1, error1 = _row_state(verify_conn, id1)
        published_at2, attempts2, error2 = _row_state(verify_conn, id2)

    assert published_at1 is not None, "row1's commit must survive the crash"
    assert attempts1 == 0
    assert error1 is None

    assert published_at2 is None, "row2 must never have been marked"
    assert attempts2 == 0, "row2 must not even be recorded as a failure"
    assert error2 is None


def test_unknown_queue_name_recorded_as_failure_no_crash(migrated_db: str) -> None:
    config = _relay_config()

    with psycopg.connect(migrated_db) as conn:
        outbox_id = _insert_row(
            conn, "not-a-real-queue", {"event": "x", "id": str(uuid4())}
        )

        client = StubSqsClient()
        stats = poll_once(conn, config, client, BackoffTracker(), logger=_RecordingLogger())

        assert stats.published == 0
        assert stats.failed == 1
        assert client.sent == [], "an unknown queue name must never reach SendMessage"

        published_at, attempts, last_error = _row_state(conn, outbox_id)
        assert published_at is None
        assert attempts == 1
        assert last_error is not None
        assert "unknown queue_name" in last_error


def test_reconnect_loop_survives_one_operational_error_then_resumes(
    migrated_db: str,
) -> None:
    """A `connect()` that raises `psycopg.OperationalError` once (simulating
    a connection blip/Postgres restart) and then succeeds must not kill the
    loop: it logs the error, backs off, reconnects, and resumes polling —
    proven here by an enqueued row actually getting published after the
    simulated blip."""
    config = _relay_config()
    payload = OutboxPayload(event="enrolment.created", id=uuid4())

    with psycopg.connect(migrated_db) as setup_conn:
        outbox_id = enqueue_sync(setup_conn, QUEUE_IDENTITY_INDEX, payload)
        setup_conn.commit()

    connect_attempts = 0
    opened_conns: list[psycopg.Connection[Any]] = []

    def fake_connect() -> psycopg.Connection[Any]:
        nonlocal connect_attempts
        connect_attempts += 1
        if connect_attempts == 1:
            raise psycopg.OperationalError("simulated connection blip")
        conn = psycopg.connect(migrated_db)
        opened_conns.append(conn)
        return conn

    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def stop_requested() -> bool:
        # Stop once the backoff sleep (1st call) and one post-poll sleep
        # (2nd call) have both happened -- i.e. after exactly one successful
        # poll cycle following the reconnect.
        return len(sleeps) >= 2

    client = StubSqsClient()
    logger = _RecordingLogger()

    try:
        _reconnect_and_poll_forever(
            config,
            client,
            BackoffTracker(),
            logger,
            stop_requested=stop_requested,
            connect=fake_connect,
            sleep=fake_sleep,
        )
    finally:
        for conn in opened_conns:
            conn.close()

    assert connect_attempts == 2, "first connect must fail, second must be tried"
    assert len(client.sent) == 1, "the row must be published after the reconnect"

    error_events = logger.of_level("error", "relay.db_connection_error")
    assert len(error_events) == 1
    assert "simulated connection blip" in error_events[0]["error"]
    assert error_events[0]["retry_in_seconds"] == _RECONNECT_BASE_SECONDS

    assert sleeps[0] == _RECONNECT_BASE_SECONDS, "backoff sleep uses the base delay"
    assert sleeps[1] == config.outbox_poll_interval_seconds, "poll loop sleep is unaffected"

    with psycopg.connect(migrated_db) as verify_conn:
        published_at, attempts, _ = _row_state(verify_conn, outbox_id)
    assert published_at is not None
    assert attempts == 0


def test_build_sqs_client_derives_localstack_endpoint() -> None:
    config = _relay_config(
        sqs_identity_index_url="http://localhost:14566/000000000000/imageshield-identity-index",
    )
    client = build_sqs_client(config)
    # boto3 client objects expose their resolved endpoint via meta.endpoint_url.
    assert client.meta.endpoint_url == "http://localhost:14566"  # type: ignore[attr-defined]


def test_build_sqs_client_real_aws_url_has_no_endpoint_override() -> None:
    config = _relay_config(
        sqs_identity_index_url="https://sqs.ap-south-1.amazonaws.com/123456789012/queue",
        sqs_search_runs_url="https://sqs.ap-south-1.amazonaws.com/123456789012/other",
    )
    client = build_sqs_client(config)
    assert "amazonaws.com" in client.meta.endpoint_url  # type: ignore[attr-defined]


# ── Boot contract (mirrors tests/test_boot.py's pattern for `python -m imageshield`) ──

_PASSTHROUGH = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP", "PATHEXT", "COMSPEC")


def test_relay_entry_point_exits_nonzero_on_missing_config(tmp_path: Path) -> None:
    env = {key: os.environ[key] for key in _PASSTHROUGH if key in os.environ}
    env.update(dict(VALID_ENV))
    del env["DATABASE_URL"]
    result = subprocess.run(
        [sys.executable, "-m", "imageshield.relay"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        timeout=60,
    )
    assert result.returncode == 1
    assert "DATABASE_URL" in result.stderr
    assert "Invalid configuration" in result.stderr


# ── Optional LocalStack integration (env-gated, skipped by default; CI never
# sets this) ─────────────────────────────────────────────────────────────────


@pytest.mark.skipif(
    os.environ.get("RELAY_LOCALSTACK_INTEGRATION") != "1",
    reason="opt-in: set RELAY_LOCALSTACK_INTEGRATION=1 to run against a real "
    "local LocalStack (docker-compose.local.yml)",
)
def test_real_localstack_end_to_end_publish(migrated_db: str) -> None:
    config = _relay_config(
        sqs_identity_index_url="http://localhost:14566/000000000000/imageshield-identity-index",
        sqs_search_runs_url="http://localhost:14566/000000000000/imageshield-search-runs",
    )
    client = build_sqs_client(config)
    try:
        client.send_message(  # type: ignore[call-arg]
            QueueUrl=config.sqs_identity_index_url, MessageBody=json.dumps({"probe": True})
        )
    except Exception as exc:  # broad on purpose: any reachability failure means skip
        pytest.skip(f"LocalStack unreachable: {exc}")

    payload = OutboxPayload(event="enrolment.created", id=uuid4())
    with psycopg.connect(migrated_db) as conn:
        enqueue_sync(conn, QUEUE_IDENTITY_INDEX, payload)
        conn.commit()

        stats = poll_once(conn, config, client, BackoffTracker(), logger=_RecordingLogger())
        assert stats.published == 1

    received = client.receive_message(  # type: ignore[call-arg]
        QueueUrl=config.sqs_identity_index_url, MaxNumberOfMessages=10, WaitTimeSeconds=2
    )
    messages = received.get("Messages", [])
    assert any(
        json.loads(m["Body"])["id"] == str(payload.id) for m in messages
    ), "published message must actually be receivable from the real queue"
