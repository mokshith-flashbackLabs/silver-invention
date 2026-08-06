"""Rekognition Face Liveness provider.

The only production module besides the relay allowed to import boto3
(pyproject.toml TID251 per-file-ignores) — Rekognition is a service we own
the relationship with (CLAUDE.md §3). There is **no S3 client here and none
anywhere in src/**: image bytes are PUT through proxy-minted presigned URLs
(:mod:`imageshield.liveness.uploader`) and discarded.

Established devtools facts, not re-derived (devtools/harness, 2026-08-05):
region us-east-1 supports Face Liveness; challenge types are
FaceMovementAndLightChallenge / FaceMovementChallenge (chosen by the client
SDK, not by us); ``GetFaceLivenessSessionResults`` returns ``Status``
(CREATED | IN_PROGRESS | SUCCEEDED | FAILED | EXPIRED), ``Confidence``,
``ReferenceImage.Bytes`` and ``AuditImages[].Bytes``; cost ≈ $0.015 per
completed check (now ``Config.liveness_cost_per_check_usd``).

No search-by-face call may ever appear here or anywhere in this path —
identity is the ``user_ref`` in the request (INVARIANTS.md #1), and the
step-9 CI gate greps this tree for the Rekognition API name to enforce it.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol, cast

# TID251 per-file ignore (pyproject.toml): Rekognition only; SQS still goes
# through the outbox, and no S3 client exists anywhere in src/.
import boto3
from botocore.exceptions import ClientError

from imageshield.liveness.models import ProviderResult, ProviderSessionNotFound, ProviderStatus

_STATUS_MAP: dict[str, ProviderStatus] = {
    "CREATED": "created",
    "IN_PROGRESS": "in_progress",
    "SUCCEEDED": "succeeded",
    "FAILED": "failed",
    "EXPIRED": "expired",
}


class LivenessProvider(Protocol):
    async def create_session(self) -> str: ...

    async def get_result(self, provider_session_id: str) -> ProviderResult: ...


class RekognitionLivenessProvider:
    """boto3-backed implementation. Calls are blocking, so they run in a
    worker thread via ``asyncio.to_thread`` — the pattern uvicorn expects for
    sync SDKs inside async handlers."""

    def __init__(self, *, region: str, client: Any | None = None) -> None:
        self._client = client if client is not None else boto3.client(
            "rekognition", region_name=region
        )

    async def create_session(self) -> str:
        response = await asyncio.to_thread(self._client.create_face_liveness_session)
        return str(response["SessionId"])

    async def get_result(self, provider_session_id: str) -> ProviderResult:
        try:
            response = await asyncio.to_thread(
                self._client.get_face_liveness_session_results,
                SessionId=provider_session_id,
            )
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code == "SessionNotFoundException":
                raise ProviderSessionNotFound(provider_session_id) from exc
            raise

        raw_status = str(response["Status"])
        status = _STATUS_MAP.get(raw_status)
        if status is None:
            raise RuntimeError(f"unrecognised Face Liveness status {raw_status!r}")

        confidence = response.get("Confidence")
        reference = cast(
            "bytes | None", (response.get("ReferenceImage") or {}).get("Bytes")
        )
        audit_images = tuple(
            cast(bytes, image["Bytes"])
            for image in response.get("AuditImages") or []
            if image.get("Bytes")
        )
        return ProviderResult(
            status=status,
            confidence=float(confidence) if confidence is not None else None,
            reference_image=reference,
            audit_images=audit_images,
        )
