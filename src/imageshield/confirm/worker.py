"""The ``confirm:hits`` consumer: fetch, dedup, gated Rekognition bundle, triage.

Runs as its own process (``python -m imageshield.confirm.worker``), the same
shape as ``search/worker.py`` and ``recheck/worker.py`` — a separate
deployable so a slow or misbehaving third-party fetch never shares a process
with request handling. Like ``search/worker.py`` it may import boto3
(``pyproject.toml`` TID251 per-file ignore), here via
:mod:`imageshield.attribution.rekognition` and
:mod:`imageshield.confirm.moderation`, never directly.

The orchestration, in order (design doc §7 step 9, task-9 brief):

1. Parse the outbox payload; wrong event or malformed body is a poison pill.
   Re-read the authoritative :class:`~imageshield.confirm.models.ConfirmContext`
   from Postgres — the message on the wire carries only an id (CLAUDE.md §10).
   A context that is missing, or already past machine triage, is deleted with
   no further work: a human or an earlier delivery already decided it.
2. No ``image_url`` recorded on the hit — nothing to fetch. Unfetchable.
3. Fetch the image through the fetcher deployable (never Postgres, never S3 —
   ARCHITECTURE.md §3.7). A non-200 or a raised transport error is
   unfetchable, not a crash: the hit stays reviewable, url-only.
4. Hash and dedup BEFORE any AWS spend. A duplicate of an already
   human-decided hit for this same user short-circuits with no Rekognition
   call and no cost.
5. The provider gate (CLAUDE.md §7.6, INVARIANTS #37-42). A ``Skip`` records
   itself in ``provider_calls`` (guarded on ``run_id`` — see below) *and*
   marks the hit reviewable url-only via ``record_skipped``, so a broken
   budget or breaker never blocks a human from seeing the hit. The state
   stays ``unconfirmed``: the next search run's completion re-enqueues it.
6. The Rekognition bundle: detect faces, search the largest ``confirm_max_faces``
   for this one candidate (INVARIANTS #1a — the candidate list is exactly
   ``(ctx.user_ref,)``, never the whole collection), and assess moderation.
   A transient AWS failure here is crash-shaped: record the failed outcome
   and return ``False`` for redelivery.
7. Record the successful outcome. ``raw_response`` carries counts only — no
   URLs, no label text — because ``provider_calls`` is retained evidence and
   the labels already live on the infringement row (CLAUDE.md §7.2).
8. The CSAM tripwire. A quarantine hit gets no triage and no score effect;
   ``confirm.quarantined`` at error level is the ops alarm.
9. Severity classification and the machine triage record.

``run_id`` on the loaded context can be ``None`` — a provenance gap the write
path makes near-impossible, but not one this worker may assume away. Steps 5
and 7 guard on it: with no run to attach a ``provider_calls`` row to, log a
warning and skip that write rather than raising.
"""

from __future__ import annotations

import asyncio
import json
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

import httpx
import structlog
from psycopg_pool import AsyncConnectionPool
from pydantic import ValidationError

from imageshield.attribution.crop import UndecodableImage
from imageshield.attribution.models import (
    AttributedFace,
    AttributionUnavailable,
    BoundingBox,
    DetectedFace,
    FaceMatch,
)
from imageshield.attribution.rekognition import RekognitionFaceAttribution
from imageshield.attribution.resolve import resolve_face
from imageshield.config import Config, ConfigError, load_config
from imageshield.confirm.models import CONFIRM_REQUESTED_EVENT, REKOGNITION_CONFIRM_ID
from imageshield.confirm.moderation import (
    ConfirmUnavailable,
    ModerationSignal,
    RekognitionModeration,
)
from imageshield.confirm.phash import bit_population, dhash
from imageshield.confirm.store import ConfirmStore, PostgresConfirmStore
from imageshield.confirm.triage import classify, csam_quarantine, find_duplicate, is_explicit
from imageshield.db.connection import make_async_pool
from imageshield.http.logging import configure_logging
from imageshield.outbox import OutboxPayload
from imageshield.providers.gate import decide
from imageshield.providers.models import Dispatch, Skip
from imageshield.providers.store import ProviderControlStore, utc_spend_date
from imageshield.relay import _localstack_endpoint_url
from imageshield.search.provider import ProviderResult
from imageshield.search.worker import SqsConsumer, build_control_store

log = structlog.get_logger("imageshield.confirm.worker")

_WAIT_TIME_SECONDS = 10  # SQS long poll
_MAX_MESSAGES = 1

# A 64-bit dHash with <=4 or >=60 bits set is degenerate: a near-uniform image
# (a solid colour, a plain banner) produces one regardless of content, so a
# small Hamming distance between two such hashes is a property of the hash
# space, not evidence the two images are the same. See the dedup guard in
# handle_message step 4.
#
# Semantics-bearing: changing either bound changes triage/score semantics --
# bump SCORE_CONFIG_VERSION (config) in the same commit so historical journal
# rows stay interpretable.
_PHASH_DEGENERATE_LOW_BITS = 4
_PHASH_DEGENERATE_HIGH_BITS = 60

# Fetching bytes over the fetcher's HTTP API is a different failure mode from
# a Rekognition call: never crash-shaped. `None` (a non-200, or a body that
# was not an image) and a raised transport error are both the unfetchable
# path — record_unfetchable, delete the message.
Fetch = Callable[[str], Awaitable[bytes | None]]


class AttributionProvider(Protocol):
    """Structural: matches ``RekognitionFaceAttribution`` and a test fake
    alike. Never typed as the concrete class, so a fake needs no
    inheritance — just the two methods (INVARIANTS #1a's face-search ban
    only reaches the real adapter; this Protocol is unaware of AWS)."""

    async def detect_faces(self, image: bytes) -> tuple[DetectedFace, ...]: ...

    async def search_face(
        self,
        image: bytes,
        face: DetectedFace,
        *,
        collection_id: str,
        match_threshold: float,
        max_candidates: int,
    ) -> tuple[FaceMatch, ...]: ...


class ModerationProvider(Protocol):
    async def assess(self, image: bytes) -> ModerationSignal: ...


@dataclass(frozen=True, slots=True)
class ConfirmDeps:
    """Everything ``handle_message`` needs, gathered once per process.

    A plain frozen dataclass rather than a pydantic model: the fields are
    Protocols and a bare callable, and pydantic's ``arbitrary_types_allowed``
    isinstance-checks against them at construction — which either rejects a
    duck-typed test fake outright (a non-``@runtime_checkable`` Protocol) or,
    made runtime-checkable, only checks attribute names and not signatures,
    buying nothing a dataclass does not already give mypy statically.
    """

    store: ConfirmStore
    control: ProviderControlStore
    provider: AttributionProvider
    moderation: ModerationProvider
    fetch: Fetch
    face_match_threshold: float
    max_faces: int
    phash_hamming_max: int
    csam_age_low_threshold: int
    identity_collection: str
    attribution_max_candidates: int


def _elapsed_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


def build_sqs_consumer(config: Config) -> SqsConsumer:
    import boto3

    kwargs: dict[str, Any] = {"region_name": config.aws_region}
    endpoint_url = _localstack_endpoint_url(config.sqs_confirm_hits_url)
    if endpoint_url is not None:
        kwargs["endpoint_url"] = endpoint_url
    client: SqsConsumer = boto3.client("sqs", **kwargs)
    return client


def build_fetch(client: httpx.AsyncClient, *, base_url: str, token: str) -> Fetch:
    """The real fetch callable, over the fetcher deployable's HTTP API
    (``POST {base_url}/v1/fetch``, ``X-Fetcher-Token``). A non-200 becomes
    ``None`` rather than a raised error: the fetcher's own error envelope
    (``{"error": {"code", "message"}}``) is not this worker's concern beyond
    "did it work" — the detail lives in ``record_unfetchable`` instead of a
    parsed error body, since the fetcher's codes (``too_large``,
    ``refused_private_address``, ...) are operational detail for the fetcher,
    not something the review queue needs to distinguish.
    """

    async def fetch(url: str) -> bytes | None:
        response = await client.post(
            f"{base_url}/v1/fetch",
            json={"url": url},
            headers={"X-Fetcher-Token": token},
        )
        if response.status_code != 200:
            log.warning(
                "confirm.fetch_non_200", status_code=response.status_code
            )
            return None
        return response.content

    return fetch


def build_deps(
    config: Config,
    pool: AsyncConnectionPool,
    http_client: httpx.AsyncClient,
) -> ConfirmDeps:
    return ConfirmDeps(
        store=PostgresConfirmStore(pool),
        control=build_control_store(config, pool),
        provider=RekognitionFaceAttribution(region=config.aws_region),
        moderation=RekognitionModeration(region=config.aws_region),
        fetch=build_fetch(
            http_client,
            base_url=config.fetcher_base_url,
            token=config.fetcher_token,
        ),
        face_match_threshold=config.confirm_face_match_threshold,
        max_faces=config.confirm_max_faces,
        phash_hamming_max=config.confirm_phash_hamming_max,
        csam_age_low_threshold=config.csam_age_low_threshold,
        identity_collection=config.identity_collection,
        attribution_max_candidates=config.attribution_max_candidates,
    )


def _face_area(face: DetectedFace) -> float:
    return face.bbox.w * face.bbox.h


async def _record_outcome_guarded(
    deps: ConfirmDeps,
    run_id: UUID | None,
    infringement_id: UUID,
    result: ProviderResult,
    *,
    cost_usd: Any,
    spend_date: Any,
    probe: bool,
) -> None:
    """The ``run_id is None`` guard shared by steps 5 and 7: a hit with no
    representative attestation has nothing to attach a ``provider_calls`` row
    to. Logged as a warning rather than silently skipped, and never raised —
    a provenance gap must not block the triage the row still needs.
    """
    if run_id is None:
        log.warning(
            "confirm.no_run_id_for_provider_call",
            infringement_id=str(infringement_id),
            status=result.status,
        )
        return
    await deps.control.record_outcome(
        run_id, result, cost_usd=cost_usd, spend_date=spend_date, probe=probe
    )


async def handle_message(
    body: str,
    deps: ConfirmDeps,
    *,
    logger: structlog.stdlib.BoundLogger | Any = None,
) -> bool:
    """Process one queue message. Returns True when it must be deleted
    (including poison pills and already-decided hits) and False only for a
    crash-shaped failure that should be redelivered."""
    worker_log = logger if logger is not None else log

    # ── 1. Parse + re-read authoritative state ──────────────────────────
    try:
        payload = OutboxPayload.model_validate(json.loads(body))
    except (ValueError, ValidationError) as exc:
        worker_log.error("confirm.malformed_message", error=str(exc))
        return True

    if payload.event != CONFIRM_REQUESTED_EVENT:
        worker_log.error("confirm.unknown_event", payload_event=payload.event)
        return True

    try:
        ctx = await deps.store.load_context(payload.id)
        if ctx is None or ctx.confirm_state not in ("unconfirmed", "machine_triaged"):
            worker_log.info(
                "confirm.already_decided",
                infringement_id=str(payload.id),
                confirm_state=None if ctx is None else ctx.confirm_state,
            )
            return True

        # ── 2. No image_url on the hit ───────────────────────────────────
        if ctx.image_url is None:
            await deps.store.record_unfetchable(
                ctx.infringement_id, detail="no image_url recorded"
            )
            return True

        # ── 3. Fetch through the fetcher deployable ──────────────────────
        try:
            image_bytes = await deps.fetch(ctx.image_url)
        except Exception as exc:
            worker_log.warning("confirm.fetch_failed", error=str(exc))
            await deps.store.record_unfetchable(
                ctx.infringement_id, detail=f"fetch failed: {exc}"
            )
            return True
        if image_bytes is None:
            await deps.store.record_unfetchable(
                ctx.infringement_id, detail="fetcher returned no image"
            )
            return True

        # ── 4. pHash + dedup, BEFORE any AWS spend ────────────────────────
        try:
            new_phash = dhash(image_bytes)
        except UndecodableImage:
            await deps.store.record_unfetchable(ctx.infringement_id, detail="undecodable")
            return True

        # A near-0 or near-all-1s population is a low-texture image (a solid
        # background, a plain banner) rather than a real perceptual match:
        # every such image collides with every other one at a tiny Hamming
        # distance regardless of content. Skipping dedup entirely for these
        # is deliberate over "just widen the threshold" -- a collision against
        # an already-REJECTED hit would machine-hide a real new hit, which is
        # the worse edge (CLAUDE.md §7.3). Recorded on the triage row so a
        # reviewer can see why dedup was bypassed.
        phash_degenerate = (
            bit_population(new_phash) <= _PHASH_DEGENERATE_LOW_BITS
            or bit_population(new_phash) >= _PHASH_DEGENERATE_HIGH_BITS
        )
        if not phash_degenerate:
            decided = await deps.store.decided_phashes(ctx.user_ref)
            duplicate_of = find_duplicate(new_phash, decided, deps.phash_hamming_max)
            if duplicate_of is not None:
                await deps.store.record_duplicate(
                    ctx.infringement_id, duplicate_of=duplicate_of, phash=new_phash
                )
                return True

        # ── 5. The provider gate ───────────────────────────────────────────
        now = datetime.now(UTC)
        runtimes = await deps.control.runtimes()
        decision = await decide(
            REKOGNITION_CONFIRM_ID,
            runtime=runtimes.get(REKOGNITION_CONFIRM_ID),
            store=deps.control,
            now=now,
        )
        if isinstance(decision, Skip):
            if ctx.run_id is not None:
                await deps.control.record_skip(
                    ctx.run_id, REKOGNITION_CONFIRM_ID, decision.reason, decision.detail
                )
            else:
                worker_log.warning(
                    "confirm.no_run_id_for_skip",
                    infringement_id=str(ctx.infringement_id),
                    reason=decision.reason,
                )
            await deps.store.record_skipped(
                ctx.infringement_id, reason=decision.reason, detail=decision.detail
            )
            return True

        # decision is now known to be Dispatch (Skip returned above).
        dispatch: Dispatch = decision

        # ── 6. The Rekognition bundle ──────────────────────────────────────
        started_at = time.monotonic()
        try:
            faces = await deps.provider.detect_faces(image_bytes)
            ranked_faces = sorted(faces, key=_face_area, reverse=True)[: deps.max_faces]
            attributed: list[AttributedFace] = []
            for face in ranked_faces:
                matches = await deps.provider.search_face(
                    image_bytes,
                    face,
                    collection_id=deps.identity_collection,
                    match_threshold=deps.face_match_threshold,
                    max_candidates=deps.attribution_max_candidates,
                )
                attributed.append(resolve_face(face, matches, (ctx.user_ref,)))
            moderation = await deps.moderation.assess(image_bytes)
        except (AttributionUnavailable, ConfirmUnavailable) as exc:
            failed = ProviderResult(
                provider_id=REKOGNITION_CONFIRM_ID,
                status="error",
                matches=[],
                raw_response={},
                http_status=None,
                latency_ms=_elapsed_ms(started_at),
                attempts=1,
                error_detail=str(exc),
            )
            await _record_outcome_guarded(
                deps,
                ctx.run_id,
                ctx.infringement_id,
                failed,
                cost_usd=dispatch.cost_usd,
                spend_date=utc_spend_date(now),
                probe=dispatch.probe,
            )
            return False

        best_score: float | None = None
        best_bbox: BoundingBox | None = None
        for face_result in attributed:
            if face_result.match_score is not None and (
                best_score is None or face_result.match_score > best_score
            ):
                best_score = face_result.match_score
                best_bbox = face_result.bbox

        # ── 7. Record the successful outcome ────────────────────────────────
        ok_result = ProviderResult(
            provider_id=REKOGNITION_CONFIRM_ID,
            status="ok",
            matches=[],
            raw_response={
                "faces_searched": len(ranked_faces),
                "face_match_score": best_score,
                "moderation_label_count": len(moderation.labels),
                "min_age_low": moderation.min_age_low,
            },
            http_status=None,
            latency_ms=_elapsed_ms(started_at),
            attempts=1,
        )
        await _record_outcome_guarded(
            deps,
            ctx.run_id,
            ctx.infringement_id,
            ok_result,
            cost_usd=dispatch.cost_usd,
            spend_date=utc_spend_date(now),
            probe=dispatch.probe,
        )

        # ── 8. CSAM tripwire ─────────────────────────────────────────────────
        explicit = is_explicit(moderation.labels)
        moderation_label_dicts = [label.model_dump() for label in moderation.labels]
        if csam_quarantine(
            explicit=explicit,
            min_age_low=moderation.min_age_low,
            age_low_threshold=deps.csam_age_low_threshold,
        ):
            await deps.store.record_quarantine(
                ctx.infringement_id,
                phash=new_phash,
                moderation_labels=moderation_label_dicts,
                min_age_low=moderation.min_age_low,
            )
            worker_log.error(
                "confirm.quarantined", infringement_id=str(ctx.infringement_id)
            )
            return True

        # ── 9. Severity classification + machine triage ──────────────────────
        severity = classify(
            explicit=explicit,
            face_match_score=best_score,
            face_match_threshold=deps.face_match_threshold,
        )
        await deps.store.record_triage(
            ctx.infringement_id,
            severity=severity,
            phash=new_phash,
            face_match_score=best_score,
            moderation_labels=moderation_label_dicts,
            triage={
                "image_url": ctx.image_url,
                "best_face_bbox": best_bbox.as_dict() if best_bbox is not None else None,
                "face_match_score": best_score,
                "moderation_labels": [label.name for label in moderation.labels],
                "phash_degenerate": phash_degenerate,
            },
        )
        return True
    except Exception as exc:  # broad on purpose: the outer net, same shape as
        # search/worker.py's handle_message. Every specific outcome above
        # (unfetchable, duplicate, skipped, quarantined, triaged) already
        # returns before reaching here; this only catches what none of them
        # anticipated -- a store method raising, an unexpected exception from
        # a dependency -- so the process survives to redeliver rather than
        # crash-looping on hostile or merely unlucky input.
        worker_log.error(
            "confirm.handle_message_failed",
            infringement_id=str(payload.id),
            error=str(exc),
        )
        return False


async def run_forever(config: Config, *, consumer: SqsConsumer | None = None) -> None:
    worker_log = structlog.get_logger("imageshield.confirm.worker")
    sqs = consumer if consumer is not None else build_sqs_consumer(config)
    queue_url = config.sqs_confirm_hits_url

    pool = make_async_pool(
        config.database_url,
        min_size=config.db_pool_min_size,
        max_size=config.db_pool_max_size,
    )
    await pool.open()
    http_client = httpx.AsyncClient(timeout=config.provider_timeout_seconds)
    deps = build_deps(config, pool, http_client)

    stop_requested = False

    def _handle_stop(signum: int, _frame: object) -> None:
        nonlocal stop_requested
        worker_log.info("confirm.stop_requested", signal=signum)
        stop_requested = True

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    worker_log.info("confirm.worker_started", queue_url=queue_url)
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
                    message.get("Body", ""), deps, logger=worker_log
                )
                if handled:
                    await asyncio.to_thread(
                        sqs.delete_message,
                        QueueUrl=queue_url,
                        ReceiptHandle=message["ReceiptHandle"],
                    )
    finally:
        await http_client.aclose()
        await pool.close()
    worker_log.info("confirm.worker_stopped")


def main() -> int:
    configure_logging()
    try:
        config = load_config()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if sys.platform == "win32":
        # psycopg's async pool cannot run on Windows' default Proactor loop —
        # same constraint (and same fix) as search/worker.py.
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
