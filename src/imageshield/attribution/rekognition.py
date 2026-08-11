"""The Rekognition adapter: DetectFaces, then a search per cropped face.

The fifth sanctioned boto3 importer (``pyproject.toml`` TID251 per-file
ignores). **No S3 client** — the photo arrives as bytes read through a
proxy-minted presigned GET and is discarded, the same shape as the enrolment
path.

This module is one of exactly two places in ``src/`` permitted to call face
search (INVARIANTS #1a); the other is nothing. What makes it permitted rather
than the fragmentation bug: every candidate is a ``user_ref`` the caller named,
no ``user_ref`` is created or reassigned anywhere reachable from a result, and a
non-match is a first-class success. The candidate filter itself lives in
``resolve.py``, deliberately NOT here — the discard rule must be one pure
function with one set of tests, not a property of whichever adapter is wired in.

Why the crop, restated because it is the non-obvious part: ``SearchFacesByImage``
"first detects the largest face in the image", so calling it three times on a
three-face photo searches the same face three times. Face N is isolated by
cropping to its bounding box (``crop.py``) before the search.
"""

from __future__ import annotations

import asyncio
from typing import Any

# TID251 per-file ignore (pyproject.toml): Rekognition only, never S3.
import boto3
import structlog
from botocore.exceptions import ClientError

from imageshield.attribution.crop import CropTooSmall, UndecodableImage, crop_to_face
from imageshield.attribution.models import (
    AttributionUnavailable,
    BoundingBox,
    DetectedFace,
    FaceMatch,
)

log = structlog.get_logger("imageshield.attribution")


class RekognitionFaceAttribution:
    """boto3-backed; blocking calls run via ``asyncio.to_thread``."""

    def __init__(
        self, *, region: str, client: Any | None = None, model_id: str | None = None
    ) -> None:
        self._client = client if client is not None else boto3.client(
            "rekognition", region_name=region
        )
        # Populated from the first response that reports it. Every score this
        # adapter produces is recorded against it (INVARIANTS #4): a similarity
        # from one face model means nothing against one from another.
        self._model_id = model_id or "rekognition:unknown"

    @property
    def model_id(self) -> str:
        return self._model_id

    async def detect_faces(self, image: bytes) -> tuple[DetectedFace, ...]:
        try:
            response = await asyncio.to_thread(
                self._client.detect_faces,
                Image={"Bytes": image},
                Attributes=["DEFAULT"],
            )
        except ClientError as exc:
            raise self._unavailable("DetectFaces", exc) from exc

        faces: list[DetectedFace] = []
        for index, detail in enumerate(response.get("FaceDetails") or []):
            box = detail.get("BoundingBox") or {}
            faces.append(
                DetectedFace(
                    face_index=index,
                    bbox=BoundingBox(
                        x=float(box.get("Left", 0.0)),
                        y=float(box.get("Top", 0.0)),
                        w=float(box.get("Width", 0.0)),
                        h=float(box.get("Height", 0.0)),
                    ),
                    detect_confidence=float(detail.get("Confidence", 0.0)),
                )
            )
        return tuple(faces)

    async def search_face(
        self,
        image: bytes,
        face: DetectedFace,
        *,
        collection_id: str,
        match_threshold: float,
        max_candidates: int,
    ) -> tuple[FaceMatch, ...]:
        try:
            cropped = await asyncio.to_thread(crop_to_face, image, face.bbox)
        except CropTooSmall:
            # A face too small to search is an UNATTRIBUTED face, not a failure.
            # Searching a 12px face returns noise, and noise above the threshold
            # attributes the wrong person — the one outcome worth avoiding here.
            log.info(
                "attribution.face_too_small",
                face_index=face.face_index,
                detect_confidence=face.detect_confidence,
            )
            return ()
        except UndecodableImage as exc:
            # The whole photo is unreadable, so no face in it can be searched.
            raise AttributionUnavailable(f"photo could not be decoded: {exc}") from exc

        try:
            response = await asyncio.to_thread(
                self._client.search_faces_by_image,
                CollectionId=collection_id,
                Image={"Bytes": cropped},
                FaceMatchThreshold=match_threshold,
                MaxFaces=max_candidates,
                # HIGH would discard exactly the awkward-angle faces a real
                # social photo is full of, and a discarded face is a lost seed.
                # This is not the enrolment path -- INVARIANTS #5's HIGH filter
                # is about the vector we STORE, and nothing here stores one.
                QualityFilter="AUTO",
            )
        except ClientError as exc:
            if self._error_code(exc) == "InvalidParameterException":
                # Rekognition raises this when the crop contains no detectable
                # face — common and benign once a crop is tight. Unattributed.
                log.info("attribution.no_face_in_crop", face_index=face.face_index)
                return ()
            raise self._unavailable("SearchFacesByImage", exc) from exc

        self._remember_model(response.get("FaceModelVersion"))
        return tuple(
            FaceMatch(
                external_image_id=str(match["Face"].get("ExternalImageId", "")),
                similarity=float(match.get("Similarity", 0.0)),
            )
            for match in response.get("FaceMatches") or []
        )

    def _remember_model(self, version: object) -> None:
        if version is not None:
            self._model_id = f"rekognition:{version}"

    @staticmethod
    def _error_code(exc: ClientError) -> str:
        return str(exc.response.get("Error", {}).get("Code", ""))

    def _unavailable(self, operation: str, exc: ClientError) -> AttributionUnavailable:
        code = self._error_code(exc)
        log.warning("attribution.call_failed", operation=operation, error_code=code)
        return AttributionUnavailable(f"{operation} failed with {code or 'unknown error'}")
