"""The one face search allowed in the enrolment path can REFUSE, never ASSIGN."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError

from imageshield.enrolment.collision import collision_check
from imageshield.enrolment.faceindex import RekognitionFaceIndex
from imageshield.enrolment.models import FaceHit, FaceIndexUnavailable, FaceSearchResult
from imageshield.types import UserRef
from tests.conftest import make_config


class FakeSearcher:
    def __init__(self, result: FaceSearchResult | Exception) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def search_face(
        self, *, collection_id: str, image_bytes: bytes, threshold: float, max_faces: int
    ) -> FaceSearchResult:
        self.calls.append(
            {
                "collection_id": collection_id,
                "image_bytes": image_bytes,
                "threshold": threshold,
                "max_faces": max_faces,
            }
        )
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def hit(ref: Any, similarity: float) -> FaceHit:
    return FaceHit(external_image_id=str(ref), similarity=similarity, face_id=f"f-{similarity}")


def result(*hits: FaceHit) -> FaceSearchResult:
    return FaceSearchResult(hits=tuple(hits), model_id="rekognition:7.0")


async def test_a_different_user_ref_above_threshold_is_a_collision() -> None:
    me, them = UserRef(uuid4()), UserRef(uuid4())
    searcher = FakeSearcher(result(hit(them, 98.4)))
    cfg = make_config(enrolment_collision_threshold=97.0)

    collision = await collision_check(searcher, cfg, me, b"frame")

    assert collision is not None
    assert collision.matched_user_ref == them
    assert collision.similarity == 98.4
    assert collision.model_id == "rekognition:7.0"
    assert collision.threshold_used == 97.0


async def test_my_own_face_is_not_a_collision() -> None:
    """Re-enrolment (new phone, fresh scan) must keep working."""
    me = UserRef(uuid4())
    searcher = FakeSearcher(result(hit(me, 99.9)))

    assert await collision_check(searcher, make_config(), me, b"frame") is None


async def test_the_best_foreign_hit_wins_and_own_hits_are_skipped() -> None:
    me, a, b = UserRef(uuid4()), UserRef(uuid4()), UserRef(uuid4())
    searcher = FakeSearcher(result(hit(me, 99.9), hit(a, 97.2), hit(b, 98.8)))

    collision = await collision_check(searcher, make_config(), me, b"frame")

    assert collision is not None and collision.matched_user_ref == b


async def test_a_non_uuid_external_image_id_is_discarded() -> None:
    """We set every ExternalImageId ourselves (INVARIANTS #6); anything else is
    something we did not put there — ignore it, do not refuse a person over it."""
    me = UserRef(uuid4())
    searcher = FakeSearcher(result(hit("not-a-uuid", 99.0)))

    assert await collision_check(searcher, make_config(), me, b"frame") is None


async def test_no_hits_is_no_collision() -> None:
    assert (
        await collision_check(FakeSearcher(result()), make_config(), UserRef(uuid4()), b"x") is None
    )


async def test_the_search_is_parameterised_from_config_and_the_collision_key() -> None:
    """One threshold per purpose (#1b): the gate reads ITS key, not
    face_match_threshold, and asks the identity collection for one page."""
    searcher = FakeSearcher(result())
    cfg = make_config(
        enrolment_collision_threshold=97.0,
        face_match_threshold=95.0,
        enrolment_collision_max_faces=5,
    )

    await collision_check(searcher, cfg, UserRef(uuid4()), b"frame")

    assert searcher.calls == [
        {
            "collection_id": cfg.identity_collection,
            "image_bytes": b"frame",
            "threshold": 97.0,
            "max_faces": 5,
        }
    ]


async def test_a_search_failure_propagates_so_the_route_fails_closed() -> None:
    searcher = FakeSearcher(FaceIndexUnavailable("SearchFacesByImage failed"))
    with pytest.raises(FaceIndexUnavailable):
        await collision_check(searcher, make_config(), UserRef(uuid4()), b"frame")


# ── RekognitionFaceIndex.search_face — the wrapper that carries the call ────


class FakeRekognitionSearch:
    def __init__(self, response: dict[str, Any] | Exception) -> None:
        self.response = response
        self.kwargs: list[dict[str, Any]] = []

    def search_faces_by_image(self, **kwargs: Any) -> Any:
        self.kwargs.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "SearchFacesByImage")


async def test_search_face_maps_the_rekognition_response() -> None:
    fake = FakeRekognitionSearch(
        {
            "FaceMatches": [
                {"Similarity": 98.4, "Face": {"FaceId": "fid-1", "ExternalImageId": "abc"}},
            ],
            "FaceModelVersion": "7.0",
        }
    )
    index = RekognitionFaceIndex(region="us-east-1", client=fake)

    found = await index.search_face(
        collection_id="identity-v1", image_bytes=b"frame", threshold=97.0, max_faces=5
    )

    assert found == FaceSearchResult(
        hits=(FaceHit(external_image_id="abc", similarity=98.4, face_id="fid-1"),),
        model_id="rekognition:7.0",
    )
    assert fake.kwargs == [
        {
            "CollectionId": "identity-v1",
            "Image": {"Bytes": b"frame"},
            "FaceMatchThreshold": 97.0,
            "MaxFaces": 5,
            # NONE on the SEARCH, deliberately: HIGH belongs to IndexFaces. A
            # frame that would fail HIGH must still be checked, not skipped past.
            "QualityFilter": "NONE",
        }
    ]


async def test_search_face_with_no_face_in_frame_is_an_empty_result_not_an_error() -> None:
    """Rekognition raises InvalidParameterException when it finds no face to
    search with. That is not an outage — IndexFaces will reject the same frame
    with a clean quality_rejected — so it must not fail the call closed."""
    index = RekognitionFaceIndex(
        region="us-east-1", client=FakeRekognitionSearch(_client_error("InvalidParameterException"))
    )

    found = await index.search_face(
        collection_id="identity-v1", image_bytes=b"frame", threshold=97.0, max_faces=5
    )

    assert found.hits == ()


async def test_search_face_provider_failure_is_face_index_unavailable() -> None:
    index = RekognitionFaceIndex(
        region="us-east-1", client=FakeRekognitionSearch(_client_error("ThrottlingException"))
    )
    with pytest.raises(FaceIndexUnavailable):
        await index.search_face(
            collection_id="identity-v1", image_bytes=b"frame", threshold=97.0, max_faces=5
        )
