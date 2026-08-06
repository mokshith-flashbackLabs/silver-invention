"""RekognitionFaceIndex against a stubbed boto3 client.

Pins the four things the step-4 brief makes load-bearing:
- IndexFaces is called with QualityFilter='HIGH', MaxFaces=1,
  DetectionAttributes=[], ExternalImageId = user_ref, and the IN-MEMORY bytes;
- quality rejection (empty FaceRecords) maps to IndexRejected with reasons;
- AWS ClientError maps to FaceIndexUnavailable (route turns that into 503);
- ListFaces paginates and filters on the FaceIds we ask about.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError

from imageshield.enrolment.faceindex import RekognitionFaceIndex
from imageshield.enrolment.models import FaceIndexUnavailable, IndexedFace, IndexRejected


class StubClient:
    def __init__(self) -> None:
        self.index_kwargs: list[dict[str, Any]] = []
        self.delete_kwargs: list[dict[str, Any]] = []
        self.list_kwargs: list[dict[str, Any]] = []
        self.index_response: Any = {
            "FaceRecords": [{"Face": {"FaceId": "face-1", "Confidence": 99.9}}],
            "FaceModelVersion": "7.0",
        }
        self.list_responses: list[dict[str, Any]] = [{"Faces": []}]

    def index_faces(self, **kwargs: Any) -> dict[str, Any]:
        self.index_kwargs.append(kwargs)
        if isinstance(self.index_response, Exception):
            raise self.index_response
        return self.index_response

    def delete_faces(self, **kwargs: Any) -> dict[str, Any]:
        self.delete_kwargs.append(kwargs)
        return {}

    def list_faces(self, **kwargs: Any) -> dict[str, Any]:
        self.list_kwargs.append(kwargs)
        return self.list_responses.pop(0)


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "IndexFaces")


async def test_index_face_sends_the_exact_contract() -> None:
    stub = StubClient()
    index = RekognitionFaceIndex(region="us-east-1", client=stub)

    result = await index.index_face(
        collection_id="identity-v1",
        external_image_id="11111111-1111-1111-1111-111111111111",
        image_bytes=b"jpeg-bytes",
    )

    assert isinstance(result, IndexedFace)
    assert result.face_id == "face-1"
    assert result.quality_score == 99.9
    assert result.model_id == "rekognition:7.0"
    (kwargs,) = stub.index_kwargs
    assert kwargs == {
        "CollectionId": "identity-v1",
        "ExternalImageId": "11111111-1111-1111-1111-111111111111",
        "QualityFilter": "HIGH",
        "MaxFaces": 1,
        "DetectionAttributes": [],
        "Image": {"Bytes": b"jpeg-bytes"},
    }


async def test_quality_rejection_maps_to_reasons() -> None:
    stub = StubClient()
    stub.index_response = {
        "FaceRecords": [],
        "UnindexedFaces": [{"Reasons": ["LOW_SHARPNESS", "LOW_BRIGHTNESS"]}],
        "FaceModelVersion": "7.0",
    }
    index = RekognitionFaceIndex(region="us-east-1", client=stub)

    result = await index.index_face(
        collection_id="identity-v1", external_image_id="u", image_bytes=b"x"
    )

    assert result == IndexRejected(reasons=("LOW_SHARPNESS", "LOW_BRIGHTNESS"))


async def test_no_face_detected_is_a_rejection_not_a_crash() -> None:
    stub = StubClient()
    stub.index_response = {"FaceRecords": [], "UnindexedFaces": [], "FaceModelVersion": "7.0"}
    index = RekognitionFaceIndex(region="us-east-1", client=stub)

    result = await index.index_face(
        collection_id="identity-v1", external_image_id="u", image_bytes=b"x"
    )

    assert result == IndexRejected(reasons=("NO_FACE_DETECTED",))


async def test_client_error_maps_to_unavailable() -> None:
    stub = StubClient()
    stub.index_response = _client_error("ThrottlingException")
    index = RekognitionFaceIndex(region="us-east-1", client=stub)

    with pytest.raises(FaceIndexUnavailable):
        await index.index_face(
            collection_id="identity-v1", external_image_id="u", image_bytes=b"x"
        )


async def test_list_face_ids_paginates_and_returns_survivors() -> None:
    stub = StubClient()
    stub.list_responses = [
        {"Faces": [{"FaceId": "face-1"}], "NextToken": "t1"},
        {"Faces": [{"FaceId": "face-2"}]},
    ]
    index = RekognitionFaceIndex(region="us-east-1", client=stub)

    survivors = await index.list_face_ids("identity-v1", ("face-1", "face-2", "face-3"))

    assert survivors == ("face-1", "face-2")
    assert stub.list_kwargs[0]["FaceIds"] == ["face-1", "face-2", "face-3"]
    assert stub.list_kwargs[1]["NextToken"] == "t1"


async def test_collection_gone_means_nothing_searchable() -> None:
    stub = StubClient()

    def raise_not_found(**kwargs: Any) -> dict[str, Any]:
        raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "ListFaces")

    stub.list_faces = raise_not_found  # type: ignore[method-assign]
    index = RekognitionFaceIndex(region="us-east-1", client=stub)

    assert await index.list_face_ids("identity-v1", ("face-1",)) == ()
