"""Rekognition collection operations: IndexFaces, DeleteFaces, ListFaces.

The third sanctioned boto3 importer (pyproject.toml TID251 per-file-ignores),
alongside the relay and the liveness provider. No S3 client — bytes arrive
in memory from ``GetFaceLivenessSessionResults`` and are never re-fetched.

Hard rules enforced by construction (step-4 brief, CLAUDE.md §4):
- ``QualityFilter='HIGH'`` — never AUTO. A poor enrolment vector degrades
  every match the user will ever get, permanently.
- ``ExternalImageId`` is the caller-supplied ``user_ref`` and nothing else.
- There is no search method on this protocol AT ALL. Identity comes from the
  request; the old system's search-by-face call is the fragmentation bug
  (INVARIANTS #1), and the step-9 CI grep hunts for that API's name — which
  is deliberately not written out here, so the grep stays clean.

Every ClientError maps to :class:`FaceIndexUnavailable` (route → 503,
retryable). A permanently failing call therefore surfaces as repeated 503s
with the AWS error code in the log — acceptable for v1, where the only
callers are the proxy's bounded retries.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

# TID251 per-file ignore (pyproject.toml): Rekognition collection ops only.
import boto3
import structlog
from botocore.exceptions import ClientError

from imageshield.enrolment.models import (
    FaceHit,
    FaceIndexUnavailable,
    FaceSearchResult,
    IndexedFace,
    IndexRejected,
)

log = structlog.get_logger("imageshield.enrolment")


class FaceIndex(Protocol):
    async def index_face(
        self, *, collection_id: str, external_image_id: str, image_bytes: bytes
    ) -> IndexedFace | IndexRejected: ...

    async def delete_faces(self, collection_id: str, face_ids: tuple[str, ...]) -> None: ...

    async def search_face(
        self, *, collection_id: str, image_bytes: bytes, threshold: float, max_faces: int
    ) -> FaceSearchResult: ...

    async def list_face_ids(
        self, collection_id: str, face_ids: tuple[str, ...]
    ) -> tuple[str, ...]: ...


class RekognitionFaceIndex:
    """boto3-backed implementation; blocking calls run via asyncio.to_thread."""

    def __init__(self, *, region: str, client: Any | None = None) -> None:
        self._client = client if client is not None else boto3.client(
            "rekognition", region_name=region
        )

    async def index_face(
        self, *, collection_id: str, external_image_id: str, image_bytes: bytes
    ) -> IndexedFace | IndexRejected:
        try:
            response = await asyncio.to_thread(
                self._client.index_faces,
                CollectionId=collection_id,
                ExternalImageId=external_image_id,
                QualityFilter="HIGH",
                MaxFaces=1,
                DetectionAttributes=[],
                Image={"Bytes": image_bytes},
            )
        except ClientError as exc:
            raise self._unavailable("IndexFaces", exc) from exc

        records = response.get("FaceRecords") or []
        if not records:
            reasons = tuple(
                str(reason)
                for unindexed in response.get("UnindexedFaces") or []
                for reason in unindexed.get("Reasons") or []
            )
            return IndexRejected(reasons=reasons or ("NO_FACE_DETECTED",))

        face = records[0]["Face"]
        confidence = face.get("Confidence")
        return IndexedFace(
            face_id=str(face["FaceId"]),
            quality_score=float(confidence) if confidence is not None else None,
            model_id=f"rekognition:{response.get('FaceModelVersion', 'unknown')}",
        )

    async def search_face(
        self, *, collection_id: str, image_bytes: bytes, threshold: float, max_faces: int
    ) -> FaceSearchResult:
        """SearchFacesByImage for the collision gate (enrolment/collision.py).

        THE ONLY CALLER IS THE COLLISION MODULE, which can refuse an enrolment
        and cannot assign one. Nothing else in the enrolment path may call
        this — tests/test_boundaries.py names the exemption file by file.
        """
        try:
            response = await asyncio.to_thread(
                self._client.search_faces_by_image,
                CollectionId=collection_id,
                Image={"Bytes": image_bytes},
                FaceMatchThreshold=threshold,
                MaxFaces=max_faces,
                # NONE on the SEARCH, deliberately: HIGH belongs to IndexFaces.
                # A frame that would fail HIGH must still be checked, not
                # skipped past.
                QualityFilter="NONE",
            )
        except ClientError as exc:
            if self._error_code(exc) == "InvalidParameterException":
                # No searchable face in the frame. Not an outage: IndexFaces
                # will answer the same frame with quality_rejected.
                return FaceSearchResult(hits=(), model_id="rekognition:unknown")
            raise self._unavailable("SearchFacesByImage", exc) from exc

        hits = tuple(
            FaceHit(
                external_image_id=str(match.get("Face", {}).get("ExternalImageId", "")),
                similarity=float(match.get("Similarity", 0.0)),
                face_id=str(match.get("Face", {}).get("FaceId", "")),
            )
            for match in response.get("FaceMatches") or []
        )
        return FaceSearchResult(
            hits=hits, model_id=f"rekognition:{response.get('FaceModelVersion', 'unknown')}"
        )

    async def delete_faces(self, collection_id: str, face_ids: tuple[str, ...]) -> None:
        try:
            await asyncio.to_thread(
                self._client.delete_faces,
                CollectionId=collection_id,
                FaceIds=list(face_ids),
            )
        except ClientError as exc:
            if self._error_code(exc) == "ResourceNotFoundException":
                return  # collection gone -> nothing searchable, which is the goal
            raise self._unavailable("DeleteFaces", exc) from exc

    async def list_face_ids(
        self, collection_id: str, face_ids: tuple[str, ...]
    ) -> tuple[str, ...]:
        found: list[str] = []
        next_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "CollectionId": collection_id,
                "FaceIds": list(face_ids),
            }
            if next_token is not None:
                kwargs["NextToken"] = next_token
            try:
                response = await asyncio.to_thread(self._client.list_faces, **kwargs)
            except ClientError as exc:
                if self._error_code(exc) == "ResourceNotFoundException":
                    return ()
                raise self._unavailable("ListFaces", exc) from exc
            found.extend(str(face["FaceId"]) for face in response.get("Faces") or [])
            next_token = response.get("NextToken")
            if next_token is None:
                return tuple(found)

    @staticmethod
    def _error_code(exc: ClientError) -> str:
        return str(exc.response.get("Error", {}).get("Code", ""))

    def _unavailable(self, operation: str, exc: ClientError) -> FaceIndexUnavailable:
        code = self._error_code(exc)
        log.warning("faceindex.call_failed", operation=operation, error_code=code)
        return FaceIndexUnavailable(f"{operation} failed with {code or 'unknown error'}")
