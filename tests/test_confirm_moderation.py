"""The Rekognition moderation client, over a recording fake client.

Fake boto3 only — no real network, no real image bytes needed (unlike
attribution's crop tests, nothing here decodes an image). Shape mirrors
``tests/test_attribution_rekognition.py``'s ``FakeRekognition``.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError

from imageshield.confirm.moderation import (
    ConfirmUnavailable,
    ModerationSignal,
    RekognitionModeration,
)


class FakeRekognition:
    """Records every call. Exposes no S3 anything."""

    def __init__(self, **responses: Any) -> None:
        self._responses = responses
        self.moderation_calls: list[dict[str, Any]] = []
        self.faces_calls: list[dict[str, Any]] = []

    def detect_moderation_labels(self, **kwargs: Any) -> Any:
        self.moderation_calls.append(kwargs)
        result = self._responses.get("moderation", {"ModerationLabels": []})
        if isinstance(result, Exception):
            raise result
        return result

    def detect_faces(self, **kwargs: Any) -> Any:
        self.faces_calls.append(kwargs)
        result = self._responses.get("faces", {"FaceDetails": []})
        if isinstance(result, Exception):
            raise result
        return result


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "op")


async def test_assess_maps_labels_and_calls_with_min_confidence() -> None:
    fake = FakeRekognition(
        moderation={
            "ModerationLabels": [
                {"Name": "Exposed Genitalia", "ParentName": "Explicit Nudity", "Confidence": 91.2},
                {"Name": "Explicit Nudity", "ParentName": "", "Confidence": 95.0},
            ]
        },
        faces={"FaceDetails": [{"AgeRange": {"Low": 25, "High": 35}}]},
    )
    client = RekognitionModeration(region="us-east-1", client=fake)

    signal = await client.assess(b"some image bytes")

    assert isinstance(signal, ModerationSignal)
    assert len(signal.labels) == 2
    assert signal.labels[0].name == "Exposed Genitalia"
    assert signal.labels[0].parent_name == "Explicit Nudity"
    assert signal.labels[0].confidence == 91.2
    assert signal.min_age_low == 25.0
    (moderation_call,) = fake.moderation_calls
    assert moderation_call["MinConfidence"] == 60
    (faces_call,) = fake.faces_calls
    assert faces_call["Attributes"] == ["AGE_RANGE"]


async def test_assess_takes_the_minimum_age_across_faces() -> None:
    fake = FakeRekognition(
        faces={
            "FaceDetails": [
                {"AgeRange": {"Low": 30, "High": 40}},
                {"AgeRange": {"Low": 14, "High": 20}},
                {"AgeRange": {"Low": 22, "High": 28}},
            ]
        }
    )
    client = RekognitionModeration(region="us-east-1", client=fake)

    signal = await client.assess(b"some image bytes")

    assert signal.min_age_low == 14.0


async def test_assess_age_is_none_when_no_faces() -> None:
    fake = FakeRekognition(faces={"FaceDetails": []})
    client = RekognitionModeration(region="us-east-1", client=fake)

    signal = await client.assess(b"some image bytes")

    assert signal.min_age_low is None
    assert signal.labels == ()


async def test_assess_age_is_none_when_age_range_absent() -> None:
    fake = FakeRekognition(faces={"FaceDetails": [{}]})
    client = RekognitionModeration(region="us-east-1", client=fake)

    signal = await client.assess(b"some image bytes")

    assert signal.min_age_low is None


@pytest.mark.parametrize("code", ["ThrottlingException", "InternalServerError"])
async def test_moderation_failure_raises_unavailable(code: str) -> None:
    fake = FakeRekognition(moderation=_client_error(code))
    client = RekognitionModeration(region="us-east-1", client=fake)

    with pytest.raises(ConfirmUnavailable):
        await client.assess(b"some image bytes")


@pytest.mark.parametrize("code", ["ThrottlingException", "InternalServerError"])
async def test_faces_failure_raises_unavailable(code: str) -> None:
    fake = FakeRekognition(faces=_client_error(code))
    client = RekognitionModeration(region="us-east-1", client=fake)

    with pytest.raises(ConfirmUnavailable):
        await client.assess(b"some image bytes")
