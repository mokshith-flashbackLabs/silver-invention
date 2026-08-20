"""The moderation client: DetectModerationLabels + DetectFaces(AGE_RANGE).

The sixth sanctioned boto3 importer (``pyproject.toml`` TID251 per-file
ignores). Rekognition only, never S3 — the confirm worker (Task 9) re-fetches
image bytes through ``confirm/fetch.py`` and hands them here; nothing in this
module reads or writes an object store.

Two independent calls, both blocking, both run via ``asyncio.to_thread`` the
same way ``attribution/rekognition.py`` does. ``DetectModerationLabels``
answers "is this explicit"; ``DetectFaces`` with ``AGE_RANGE`` answers "how
old does the youngest face in it look" — the second call is the CSAM
tripwire's other half (``triage.csam_quarantine``), and it must never be
skipped just because the first call came back clean. To be precise about what
that tripwire actually requires: ``csam_quarantine`` fires on explicit content
AND a young-looking face together, never on age alone — a clean moderation
call with no explicit label does not quarantine regardless of the age
estimate. What must never be skipped is *this call*, because an explicit
result paired with a face this call never assessed would silently drop the
age half of that AND.
"""

from __future__ import annotations

import asyncio
from typing import Any

# TID251 per-file ignore (pyproject.toml): Rekognition only, never S3.
import boto3
import structlog
from botocore.exceptions import ClientError
from pydantic import BaseModel, ConfigDict

log = structlog.get_logger("imageshield.confirm")


class ConfirmUnavailable(RuntimeError):
    """A moderation-provider call failed. The worker treats this like any
    other unfetchable hit — retried with backoff, never a silent pass."""


class ModerationLabel(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    parent_name: str
    confidence: float


class ModerationSignal(BaseModel):
    model_config = ConfigDict(frozen=True)

    labels: tuple[ModerationLabel, ...]
    min_age_low: float | None


class RekognitionModeration:
    """boto3-backed; blocking calls run via ``asyncio.to_thread``."""

    def __init__(self, *, region: str, client: Any | None = None) -> None:
        self._client = (
            client if client is not None else boto3.client("rekognition", region_name=region)
        )

    async def assess(self, image: bytes) -> ModerationSignal:
        try:
            # Semantics-bearing: changing MinConfidence changes triage/score
            # semantics -- bump SCORE_CONFIG_VERSION (config) in the same
            # commit so historical journal rows stay interpretable.
            moderation_response = await asyncio.to_thread(
                self._client.detect_moderation_labels,
                Image={"Bytes": image},
                MinConfidence=60,
            )
        except ClientError as exc:
            raise self._unavailable("DetectModerationLabels", exc) from exc

        try:
            faces_response = await asyncio.to_thread(
                self._client.detect_faces,
                Image={"Bytes": image},
                Attributes=["AGE_RANGE"],
            )
        except ClientError as exc:
            raise self._unavailable("DetectFaces", exc) from exc

        labels = tuple(
            ModerationLabel(
                name=str(label.get("Name", "")),
                parent_name=str(label.get("ParentName", "")),
                confidence=float(label.get("Confidence", 0.0)),
            )
            for label in moderation_response.get("ModerationLabels") or []
        )

        ages_low: list[float] = []
        for detail in faces_response.get("FaceDetails") or []:
            age_range = detail.get("AgeRange")
            if age_range is None:
                continue
            low = age_range.get("Low")
            if low is None:
                continue
            ages_low.append(float(low))

        return ModerationSignal(
            labels=labels,
            min_age_low=min(ages_low) if ages_low else None,
        )

    @staticmethod
    def _error_code(exc: ClientError) -> str:
        return str(exc.response.get("Error", {}).get("Code", ""))

    def _unavailable(self, operation: str, exc: ClientError) -> ConfirmUnavailable:
        code = self._error_code(exc)
        log.warning("confirm.moderation_call_failed", operation=operation, error_code=code)
        return ConfirmUnavailable(f"{operation} failed with {code or 'unknown error'}")
