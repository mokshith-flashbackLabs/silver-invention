"""The crop and the Rekognition adapter, over a recording fake client.

Real Pillow, fake boto3. The crop arithmetic is the part with a wrong answer
that looks right — an off-by-a-margin crop still returns a face, just somebody
else's — so it is exercised against real image bytes rather than a mock.
"""

from __future__ import annotations

import io
from typing import Any
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from PIL import Image

from imageshield.attribution.crop import CropTooSmall, UndecodableImage, crop_to_face
from imageshield.attribution.models import (
    AttributionUnavailable,
    BoundingBox,
    DetectedFace,
)
from imageshield.attribution.rekognition import RekognitionFaceAttribution


def _photo(width: int = 800, height: int = 600, colour: str = "white") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, format="JPEG")
    return buffer.getvalue()


def _size(image: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(image)) as opened:
        return opened.size


# ── the crop ─────────────────────────────────────────────────────────────────


def test_the_crop_includes_a_margin_around_the_box() -> None:
    """A crop tight to the detected box loses the jaw, hairline and ear context
    that face embeddings lean on."""
    photo = _photo(800, 600)
    # 200x180px face in the middle.
    bbox = BoundingBox(x=0.25, y=0.3, w=0.25, h=0.3)

    cropped = crop_to_face(photo, bbox)

    width, height = _size(cropped)
    # 25% of each dimension on both sides -> 1.5x the box.
    assert width == pytest.approx(200 * 1.5, abs=2)
    assert height == pytest.approx(180 * 1.5, abs=2)


def test_a_box_at_the_frame_edge_is_clamped_not_padded() -> None:
    """Rekognition projects boxes past the image boundary for faces at the
    edge. Unclamped, the margin arithmetic goes negative and Pillow silently
    pads with black, moving the face off-centre in its own crop."""
    photo = _photo(800, 600)
    bbox = BoundingBox(x=0.0, y=0.0, w=0.2, h=0.2)

    cropped = crop_to_face(photo, bbox)

    width, height = _size(cropped)
    # Left/top clamp at 0, so only the right/bottom margin is added.
    assert width == pytest.approx(800 * 0.25, abs=2)
    assert height == pytest.approx(600 * 0.25, abs=2)


def test_a_box_overflowing_the_far_edge_is_clamped_too() -> None:
    photo = _photo(800, 600)
    bbox = BoundingBox(x=0.9, y=0.9, w=0.2, h=0.2)  # extends past 1.0

    cropped = crop_to_face(photo, bbox)

    width, height = _size(cropped)
    assert width <= 800 and height <= 600
    assert width > 0 and height > 0


def test_a_face_too_small_to_search_is_refused() -> None:
    """Searching a 12px face returns noise, and noise above the threshold
    attributes the WRONG person — the one outcome worth avoiding here."""
    photo = _photo(800, 600)
    bbox = BoundingBox(x=0.5, y=0.5, w=0.01, h=0.01)  # 8x6px

    with pytest.raises(CropTooSmall):
        crop_to_face(photo, bbox)


def test_bytes_that_are_not_an_image_are_rejected_clearly() -> None:
    with pytest.raises(UndecodableImage):
        crop_to_face(b"this is not a jpeg", BoundingBox(x=0.1, y=0.1, w=0.5, h=0.5))


# ── the adapter ──────────────────────────────────────────────────────────────


class FakeRekognition:
    """Records every call. Exposes no S3 anything."""

    def __init__(self, **responses: Any) -> None:
        self._responses = responses
        self.detect_calls: list[dict[str, Any]] = []
        self.search_calls: list[dict[str, Any]] = []

    def detect_faces(self, **kwargs: Any) -> Any:
        self.detect_calls.append(kwargs)
        result = self._responses.get("detect", {"FaceDetails": []})
        if isinstance(result, Exception):
            raise result
        return result

    def search_faces_by_image(self, **kwargs: Any) -> Any:
        self.search_calls.append(kwargs)
        result = self._responses.get("search", {"FaceMatches": []})
        if isinstance(result, Exception):
            raise result
        return result


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "op")


async def test_detect_faces_maps_boxes_and_indexes_in_order() -> None:
    fake = FakeRekognition(
        detect={
            "FaceDetails": [
                {
                    "BoundingBox": {"Left": 0.1, "Top": 0.2, "Width": 0.3, "Height": 0.4},
                    "Confidence": 99.5,
                },
                {
                    "BoundingBox": {"Left": 0.5, "Top": 0.1, "Width": 0.2, "Height": 0.3},
                    "Confidence": 88.0,
                },
            ]
        }
    )
    adapter = RekognitionFaceAttribution(region="us-east-1", client=fake)

    faces = await adapter.detect_faces(_photo())

    assert [f.face_index for f in faces] == [0, 1]
    assert faces[0].bbox == BoundingBox(x=0.1, y=0.2, w=0.3, h=0.4)
    assert faces[0].detect_confidence == 99.5
    assert faces[1].detect_confidence == 88.0


async def test_search_crops_before_searching() -> None:
    """The whole reason Pillow is a dependency: SearchFacesByImage searches the
    LARGEST face in whatever it is given, so the image it is given must contain
    only the face we mean."""
    owner = uuid4()
    fake = FakeRekognition(
        search={
            "FaceMatches": [
                {"Face": {"ExternalImageId": str(owner)}, "Similarity": 96.5}
            ],
            "FaceModelVersion": "7.0",
        }
    )
    adapter = RekognitionFaceAttribution(region="us-east-1", client=fake)
    photo = _photo(800, 600)
    face = DetectedFace(
        face_index=1,
        bbox=BoundingBox(x=0.25, y=0.3, w=0.25, h=0.3),
        detect_confidence=99.0,
    )

    matches = await adapter.search_face(
        photo, face, collection_id="identity-v1", match_threshold=92.0, max_candidates=5
    )

    assert matches[0].external_image_id == str(owner)
    assert matches[0].similarity == 96.5
    (call,) = fake.search_calls
    assert call["CollectionId"] == "identity-v1"
    assert call["FaceMatchThreshold"] == 92.0
    assert call["MaxFaces"] == 5
    # The bytes sent are the CROP, not the photo.
    assert call["Image"]["Bytes"] != photo
    assert _size(call["Image"]["Bytes"]) != (800, 600)
    # And the model version is remembered for the run's provenance.
    assert adapter.model_id == "rekognition:7.0"


async def test_the_adapter_never_filters_candidates() -> None:
    """The discard rule lives in resolve.py so it is one pure function with one
    set of tests, not a property of whichever adapter is wired in. The adapter
    returns everything the collection said."""
    a, b = uuid4(), uuid4()
    fake = FakeRekognition(
        search={
            "FaceMatches": [
                {"Face": {"ExternalImageId": str(a)}, "Similarity": 99.0},
                {"Face": {"ExternalImageId": str(b)}, "Similarity": 93.0},
            ]
        }
    )
    adapter = RekognitionFaceAttribution(region="us-east-1", client=fake)
    face = DetectedFace(
        face_index=0,
        bbox=BoundingBox(x=0.25, y=0.3, w=0.25, h=0.3),
        detect_confidence=99.0,
    )

    matches = await adapter.search_face(
        _photo(), face, collection_id="identity-v1", match_threshold=92.0, max_candidates=5
    )

    assert {m.external_image_id for m in matches} == {str(a), str(b)}


async def test_a_face_too_small_to_crop_is_unattributed_not_an_error() -> None:
    fake = FakeRekognition()
    adapter = RekognitionFaceAttribution(region="us-east-1", client=fake)
    tiny = DetectedFace(
        face_index=0,
        bbox=BoundingBox(x=0.5, y=0.5, w=0.005, h=0.005),
        detect_confidence=99.0,
    )

    matches = await adapter.search_face(
        _photo(), tiny, collection_id="identity-v1", match_threshold=92.0, max_candidates=5
    )

    assert matches == ()
    assert fake.search_calls == []  # never asked


async def test_no_face_in_the_crop_is_unattributed_not_an_error() -> None:
    """Rekognition raises InvalidParameterException when a crop contains no
    detectable face. Common and benign once a crop is tight."""
    fake = FakeRekognition(search=_client_error("InvalidParameterException"))
    adapter = RekognitionFaceAttribution(region="us-east-1", client=fake)
    face = DetectedFace(
        face_index=0,
        bbox=BoundingBox(x=0.25, y=0.3, w=0.25, h=0.3),
        detect_confidence=99.0,
    )

    matches = await adapter.search_face(
        _photo(), face, collection_id="identity-v1", match_threshold=92.0, max_candidates=5
    )

    assert matches == ()


@pytest.mark.parametrize("code", ["ThrottlingException", "InternalServerError"])
async def test_a_real_provider_failure_raises_unavailable(code: str) -> None:
    fake = FakeRekognition(detect=_client_error(code))
    adapter = RekognitionFaceAttribution(region="us-east-1", client=fake)

    with pytest.raises(AttributionUnavailable):
        await adapter.detect_faces(_photo())


async def test_an_undecodable_photo_is_unavailable_not_a_silent_empty() -> None:
    fake = FakeRekognition()
    adapter = RekognitionFaceAttribution(region="us-east-1", client=fake)
    face = DetectedFace(
        face_index=0,
        bbox=BoundingBox(x=0.25, y=0.3, w=0.25, h=0.3),
        detect_confidence=99.0,
    )

    with pytest.raises(AttributionUnavailable):
        await adapter.search_face(
            b"not an image",
            face,
            collection_id="identity-v1",
            match_threshold=92.0,
            max_candidates=5,
        )
