"""Liveness session lifecycle endpoints (CLAUDE.md §8 step 3, step-3 brief).

Hard rules enforced here:

- Identity is the ``user_ref`` in the request. No search-by-face call, no
  "does this face already exist" check — that is the old system's
  fragmentation bug (INVARIANTS.md #1) and it must not reappear.
- ``LIVENESS_MIN_CONFIDENCE`` comes from config; no inline threshold
  literals (INVARIANTS.md #1b).
- No S3 client. The ReferenceImage and AuditImages are PUT through
  proxy-minted presigned URLs and the bytes are discarded (CLAUDE.md §3.3).
- Session create is NOT idempotent: it creates a provider session and burns
  an attempt against the 24h cap, so an ``Idempotency-Key`` on it is rejected
  with 400 — a caller must never assume that retrying it is safe.
- The result call REQUIRES ``Idempotency-Key``: a same-key retry replays the
  stored outcome; a different key against a completed session is a genuine
  replay and gets 410 (sessions are single-use).

The stored ``reference_image_uri``/``audit_image_uris`` are the presigned
URLs with the query string stripped: the query is the signature — a
credential — and must not be persisted or logged.

``enrolled`` is always False here. Indexing is step 4.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Header

from imageshield.config import Config
from imageshield.http.auth import require_service_token
from imageshield.http.deps import (
    get_config,
    get_liveness_provider,
    get_liveness_store,
    get_object_uploader,
)
from imageshield.http.errors import ServiceError
from imageshield.http.models import (
    LivenessResultRequest,
    LivenessResultResponse,
    LivenessSessionCreateRequest,
    LivenessSessionCreateResponse,
    LivenessStatusResponse,
)
from imageshield.liveness.models import (
    CreateRejection,
    LivenessSessionRow,
    ProviderSessionNotFound,
    UploadError,
)
from imageshield.liveness.provider import LivenessProvider
from imageshield.liveness.store import LivenessStore
from imageshield.liveness.uploader import ObjectUploader
from imageshield.types import SessionId

log = structlog.get_logger("imageshield.liveness")

router = APIRouter(prefix="/v1/liveness", dependencies=[Depends(require_service_token)])

_IMAGE_CONTENT_TYPE = "image/jpeg"  # Face Liveness reference/audit frames are JPEG.


def _rejection_error(rejection: CreateRejection) -> ServiceError:
    if rejection is CreateRejection.PASSED_UNCONSUMED:
        return ServiceError(
            409,
            "liveness_already_passed",
            "A passed-but-unconsumed liveness session already exists for this user.",
            retryable=False,
        )
    return ServiceError(
        429,
        "liveness_attempts_exceeded",
        "Too many liveness attempts in the trailing 24 hours.",
        retryable=False,
    )


def _strip_query(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _require_presigned(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ServiceError(
            400,
            "presigned_urls_invalid",
            "Presigned URLs must be absolute http(s) URLs.",
            retryable=False,
        )


def _result_response(row: LivenessSessionRow) -> LivenessResultResponse:
    if row.status not in ("passed", "failed"):
        raise ServiceError(
            502,
            "liveness_result_inconsistent",
            f"Session finalised with unexpected status {row.status!r}.",
            retryable=True,
        )
    return LivenessResultResponse(status=row.status, confidence=row.confidence)


def _expired(row: LivenessSessionRow, now: datetime) -> bool:
    return row.completed_at is None and now >= row.expires_at


@router.post("/sessions", status_code=201, response_model=LivenessSessionCreateResponse)
async def create_liveness_session(
    body: LivenessSessionCreateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    cfg: Config = Depends(get_config),
    store: LivenessStore = Depends(get_liveness_store),
    provider: LivenessProvider = Depends(get_liveness_provider),
) -> LivenessSessionCreateResponse:
    if idempotency_key is not None:
        raise ServiceError(
            400,
            "idempotency_key_not_allowed",
            "Session creation is not idempotent: it creates a provider session and"
            " burns a rate-limit attempt. Do not retry it blind.",
            retryable=False,
        )

    # Fast-fail BEFORE the provider call so a rejected create burns nothing.
    rejection = await store.check_create_allowed(
        body.user_ref, max_attempts_24h=cfg.liveness_max_attempts_24h
    )
    if rejection is not None:
        raise _rejection_error(rejection)

    provider_session_id = await provider.create_session()

    # Re-checked atomically with the INSERT; a concurrent create can slip in
    # between the pre-check and here. The abandoned provider session simply
    # expires — cost accrues per completed check, not per created session.
    created = await store.create_session(
        user_ref=body.user_ref,
        provider_session_id=provider_session_id,
        ttl_seconds=cfg.liveness_session_ttl_seconds,
        max_attempts_24h=cfg.liveness_max_attempts_24h,
    )
    if isinstance(created, CreateRejection):
        raise _rejection_error(created)

    log.info(
        "liveness.session_created",
        session_id=str(created.session_id),
        attempt_number=created.attempt_number,
    )
    return LivenessSessionCreateResponse(
        session_id=created.session_id,
        provider_session_id=created.provider_session_id,
        region=cfg.aws_region,
        expires_at=created.expires_at,
    )


@router.post("/{session_id}/result", response_model=LivenessResultResponse)
async def post_liveness_result(
    session_id: UUID,
    body: LivenessResultRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    cfg: Config = Depends(get_config),
    store: LivenessStore = Depends(get_liveness_store),
    provider: LivenessProvider = Depends(get_liveness_provider),
    uploader: ObjectUploader = Depends(get_object_uploader),
) -> LivenessResultResponse:
    if idempotency_key is None:
        raise ServiceError(
            400,
            "idempotency_key_missing",
            "Idempotency-Key is required on the result call.",
            retryable=False,
        )
    if body.reference_put_url is None or body.audit_put_urls is None:
        raise ServiceError(
            400,
            "presigned_urls_missing",
            "reference_put_url and audit_put_urls are required.",
            retryable=False,
        )
    _require_presigned(body.reference_put_url)
    for url in body.audit_put_urls:
        _require_presigned(url)

    sid = SessionId(session_id)
    row = await store.get_session(sid)
    if row is None:
        raise ServiceError(404, "session_not_found", "Unknown liveness session.", retryable=False)
    if row.consumed_at is not None:
        raise ServiceError(
            410,
            "liveness_consumed",
            "This liveness session has already been consumed.",
            retryable=False,
        )
    if row.completed_at is not None:
        if row.result_idempotency_key == idempotency_key:
            return _result_response(row)  # idempotent replay of the same request
        raise ServiceError(
            410,
            "liveness_consumed",
            "This liveness session already has a recorded result.",
            retryable=False,
        )
    if _expired(row, datetime.now(UTC)):
        await store.mark_expired(sid)
        raise ServiceError(
            410,
            "liveness_expired",
            "This liveness session has expired.",
            retryable=False,
        )

    await store.claim_result(sid, idempotency_key)

    try:
        result = await provider.get_result(row.provider_session_id)
    except ProviderSessionNotFound:
        await store.mark_expired(sid)
        raise ServiceError(
            410,
            "liveness_expired",
            "The provider no longer knows this session.",
            retryable=False,
        ) from None

    if result.status in ("created", "in_progress"):
        raise ServiceError(
            409,
            "liveness_result_not_ready",
            "The liveness challenge has not completed yet.",
            retryable=True,
        )
    if result.status == "expired":
        await store.mark_expired(sid)
        raise ServiceError(
            410,
            "liveness_expired",
            "The liveness session expired at the provider.",
            retryable=False,
        )

    if result.status == "failed":
        final = await store.finalize_result(
            sid,
            status="failed",
            confidence=result.confidence,
            failure_reason="provider_reported_failure",
            reference_image_uri=None,
            audit_image_uris=None,
        )
        return _finish(final, cfg)

    # result.status == "succeeded": the one threshold, from config (no literals).
    if result.confidence is None or result.confidence < cfg.liveness_min_confidence:
        final = await store.finalize_result(
            sid,
            status="failed",
            confidence=result.confidence,
            failure_reason="confidence_below_threshold",
            reference_image_uri=None,
            audit_image_uris=None,
        )
        return _finish(final, cfg)

    if result.reference_image is None:
        # Rekognition SUCCEEDED without a ReferenceImage would break the
        # liveness→enrolment binding (step 4 indexes exactly these bytes) —
        # treat as a provider fault, leave the session retryable.
        raise ServiceError(
            502,
            "provider_reference_missing",
            "The provider returned no ReferenceImage for a succeeded session.",
            retryable=True,
        )

    # Persist the ReferenceImage — do not merely read it. These bytes exist
    # only in this response; step 4 enrols from this stored object, which is
    # what binds "a live human was present" to "this face got indexed".
    stored_audit_uris: list[str] = []
    try:
        await uploader.put(
            body.reference_put_url, result.reference_image, content_type=_IMAGE_CONTENT_TYPE
        )
        for image, url in zip(result.audit_images, body.audit_put_urls, strict=False):
            await uploader.put(url, image, content_type=_IMAGE_CONTENT_TYPE)
            stored_audit_uris.append(_strip_query(url))
    except UploadError:
        # Not finalised: the proxy retries with the same Idempotency-Key and
        # the retry re-processes from the top (provider reads are repeatable).
        raise ServiceError(
            502,
            "presigned_put_failed",
            "Persisting images through the presigned URLs failed.",
            retryable=True,
        ) from None

    final = await store.finalize_result(
        sid,
        status="passed",
        confidence=result.confidence,
        failure_reason=None,
        reference_image_uri=_strip_query(body.reference_put_url),
        audit_image_uris=tuple(stored_audit_uris),
    )
    return _finish(final, cfg)


def _finish(row: LivenessSessionRow, cfg: Config) -> LivenessResultResponse:
    log.info(
        "liveness.check_completed",
        session_id=str(row.session_id),
        status=row.status,
        confidence=row.confidence,
        failure_reason=row.failure_reason,
        cost_usd=cfg.liveness_cost_per_check_usd,
    )
    return _result_response(row)


@router.get("/{session_id}", response_model=LivenessStatusResponse)
async def get_liveness_session(
    session_id: UUID,
    store: LivenessStore = Depends(get_liveness_store),
) -> LivenessStatusResponse:
    row = await store.get_session(SessionId(session_id))
    if row is None:
        raise ServiceError(404, "session_not_found", "Unknown liveness session.", retryable=False)
    return LivenessStatusResponse(status=_effective_status(row), confidence=row.confidence)


def _effective_status(
    row: LivenessSessionRow,
) -> str:
    if row.consumed_at is not None:
        return "consumed"
    if _expired(row, datetime.now(UTC)):
        return "expired"
    return row.status
