"""The ``search:runs`` consumer: SQS → claim → execute → delete.

Runs as a **separate process** (``python -m imageshield.search.worker``) —
the consumer counterpart of the relay, for the queue that has existed in
config since step 1. Like the relay it may import boto3 (pyproject TID251
per-file-ignore) — for an SQS client only, never S3.

Idempotency under at-least-once delivery (CLAUDE.md §10):

- Messages carry IDs, never payloads. The body is the outbox payload
  ``{"event": "search.run_requested", "id": <run_id>}``; the worker re-reads
  authoritative state from Postgres via ``claim_run`` — the stored row wins.
- ``claim_run`` succeeds exactly once per run (queued → running). A
  duplicate delivery finds the run claimed/completed, gets ``None``, and the
  message is deleted without re-executing — no double provider spend.
- A crash mid-run leaves the message undeleted; SQS redelivers after the
  visibility timeout, and the store's stale-claim window (15 min) lets the
  retry reclaim the orphaned 'running' row.
- Unparseable bodies and unknown events are poison pills: logged at error
  level and deleted, because redelivering them forever helps nobody.

boto3's client is sync; provider calls and the store are async on one shared
pool — receive/delete hop through ``asyncio.to_thread`` so a long poll never
blocks an in-flight provider call on another message.
"""

from __future__ import annotations

import asyncio
import json
import signal
import sys
from typing import Any, Protocol

import structlog
from pydantic import ValidationError

from imageshield.calibration.store import CalibrationStore, PostgresCalibrationStore
from imageshield.config import Config, ConfigError, load_config
from imageshield.db.connection import make_async_pool
from imageshield.http.logging import configure_logging
from imageshield.outbox import OutboxPayload
from imageshield.relay import _localstack_endpoint_url
from imageshield.search.google import GoogleWebDetectionProvider
from imageshield.search.hive import HiveWebSearchProvider
from imageshield.search.provider import SearchProvider
from imageshield.search.runner import execute_run
from imageshield.search.store import RUN_REQUESTED_EVENT, PostgresSearchStore, SearchStore
from imageshield.types import ProviderId

_WAIT_TIME_SECONDS = 10  # SQS long poll
_MAX_MESSAGES = 1


class SqsConsumer(Protocol):
    """The two SQS operations this module needs, typed by hand (boto3 ships
    no bundled stubs — same approach as relay.SqsClient)."""

    def receive_message(
        self, *, QueueUrl: str, MaxNumberOfMessages: int, WaitTimeSeconds: int
    ) -> dict[str, Any]: ...

    def delete_message(self, *, QueueUrl: str, ReceiptHandle: str) -> object: ...


def build_sqs_consumer(config: Config) -> SqsConsumer:
    import boto3

    kwargs: dict[str, Any] = {"region_name": config.aws_region}
    endpoint_url = _localstack_endpoint_url(config.sqs_search_runs_url)
    if endpoint_url is not None:
        kwargs["endpoint_url"] = endpoint_url
    client: SqsConsumer = boto3.client("sqs", **kwargs)
    return client


def build_providers(config: Config) -> dict[ProviderId, SearchProvider]:
    return {
        ProviderId("hive"): HiveWebSearchProvider(
            base_url=config.hive_base_url,
            api_key=config.hive_api_key,
            timeout_seconds=config.provider_timeout_seconds,
        ),
        ProviderId("google"): GoogleWebDetectionProvider(
            endpoint=config.google_vision_endpoint,
            api_key=config.google_vision_api_key,
            timeout_seconds=config.provider_timeout_seconds,
        ),
    }


async def handle_message(
    body: str,
    store: SearchStore,
    providers: dict[ProviderId, SearchProvider],
    calibration_store: CalibrationStore,
    *,
    logger: structlog.stdlib.BoundLogger | Any = None,
) -> bool:
    """Process one queue message. Returns True when the message is handled
    and must be deleted — including poison pills and duplicate deliveries —
    and False when it should stay for redelivery (execution crashed)."""
    log = logger if logger is not None else structlog.get_logger("imageshield.search.worker")

    try:
        payload = OutboxPayload.model_validate(json.loads(body))
    except (ValueError, ValidationError) as exc:
        log.error("worker.malformed_message", error=str(exc))
        return True  # poison pill: redelivering forever helps nobody

    if payload.event != RUN_REQUESTED_EVENT:
        # kwarg name: `event` is structlog's own positional slot
        log.error("worker.unknown_event", payload_event=payload.event)
        return True

    claim = await store.claim_run(payload.id)
    if claim is None:
        # Already completed, claimed recently by another worker, or unknown:
        # the normal duplicate-delivery outcome. The stored row won.
        log.info("worker.run_not_claimable", run_id=str(payload.id))
        return True

    try:
        # Snapshotted once per claimed run, not once per process: a config
        # activated between two runs must apply to the next run immediately,
        # and a config activated mid-run must not split that run's results
        # across two rulesets.
        policy = await calibration_store.load_active_policy()
        await execute_run(claim, providers, store, policy)
    except Exception as exc:  # broad on purpose: crash = leave for redelivery
        log.error("worker.run_execution_failed", run_id=str(payload.id), error=str(exc))
        return False
    return True


async def run_forever(config: Config, *, consumer: SqsConsumer | None = None) -> None:
    log = structlog.get_logger("imageshield.search.worker")
    sqs = consumer if consumer is not None else build_sqs_consumer(config)
    providers = build_providers(config)
    queue_url = config.sqs_search_runs_url

    pool = make_async_pool(
        config.database_url,
        min_size=config.db_pool_min_size,
        max_size=config.db_pool_max_size,
    )
    await pool.open()
    store = PostgresSearchStore(pool)
    calibration_store = PostgresCalibrationStore(pool)

    stop_requested = False

    def _handle_stop(signum: int, _frame: object) -> None:
        nonlocal stop_requested
        log.info("worker.stop_requested", signal=signum)
        stop_requested = True

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    log.info("worker.started", queue_url=queue_url)
    try:
        while not stop_requested:
            response = await asyncio.to_thread(
                sqs.receive_message,
                QueueUrl=queue_url,
                MaxNumberOfMessages=_MAX_MESSAGES,
                WaitTimeSeconds=_WAIT_TIME_SECONDS,
            )
            for message in response.get("Messages", []):
                handled = await handle_message(
                    message.get("Body", ""),
                    store,
                    providers,
                    calibration_store,
                    logger=log,
                )
                if handled:
                    await asyncio.to_thread(
                        sqs.delete_message,
                        QueueUrl=queue_url,
                        ReceiptHandle=message["ReceiptHandle"],
                    )
    finally:
        await pool.close()
    log.info("worker.stopped")


def main() -> int:
    configure_logging()
    try:
        config = load_config()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if sys.platform == "win32":
        # psycopg's async pool cannot run on Windows' default Proactor loop —
        # same constraint (and same fix) as imageshield/__main__.py. Local
        # dev only; the deployed container is Linux.
        import selectors

        with asyncio.Runner(
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())
        ) as runner:
            runner.run(run_forever(config))
        return 0

    asyncio.run(run_forever(config))
    return 0


if __name__ == "__main__":
    sys.exit(main())
