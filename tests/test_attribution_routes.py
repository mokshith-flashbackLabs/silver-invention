"""``POST /v1/attribute`` end to end over fakes.

Route + service together, with the provider, fetcher and store faked — the
repo convention (``tests/conftest.py``): TestClient never runs the lifespan,
and the real SQL is proven in ``tests/test_attribution_store.py``.
"""

from __future__ import annotations

import io
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from imageshield.attribution.models import (
    AttributionOutcome,
    AttributionUnavailable,
    BoundingBox,
    DetectedFace,
    FaceMatch,
    RegisteredSeed,
)
from imageshield.http.app import create_app
from imageshield.liveness.models import UploadError
from imageshield.score.store import ScoreResult
from imageshield.types import UserRef
from tests.conftest import SERVICE_TOKEN, make_config

AUTH = {"X-Service-Token": SERVICE_TOKEN}
PRESIGNED = "https://proxy-s3.example/photo.jpg?X-Amz-Signature=abc"
BOX = BoundingBox(x=0.1, y=0.2, w=0.3, h=0.4)


def _encoded(fmt: str) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), (10, 120, 60)).save(buffer, format=fmt)
    return buffer.getvalue()


class FakeFetcher:
    def __init__(self, error: Exception | None = None, payload: bytes | None = None) -> None:
        self.urls: list[str] = []
        self._error = error
        # A real (tiny) JPEG: the service now normalises bytes for Rekognition
        # before detect_faces, so an undecodable placeholder would divert every
        # test down the unavailable path.
        self._payload = payload if payload is not None else _encoded("JPEG")

    async def fetch(self, presigned_get_url: str) -> bytes:
        self.urls.append(presigned_get_url)
        if self._error is not None:
            raise self._error
        return self._payload


class FakeProvider:
    """Records what it was asked, and never filters. The candidate filter lives
    in resolve.py on purpose — a provider that filtered would make the rule a
    property of whichever adapter is wired in."""

    model_id = "rekognition:7.0"

    def __init__(
        self,
        faces: tuple[DetectedFace, ...] = (),
        matches: dict[int, tuple[FaceMatch, ...]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._faces = faces
        self._matches = matches or {}
        self._error = error
        self.searches: list[tuple[int, str, float, int]] = []
        self.detect_bytes: list[bytes] = []

    async def detect_faces(self, image: bytes) -> tuple[DetectedFace, ...]:
        self.detect_bytes.append(image)
        if self._error is not None:
            raise self._error
        return self._faces

    async def search_face(
        self,
        image: bytes,
        face: DetectedFace,
        *,
        collection_id: str,
        match_threshold: float,
        max_candidates: int,
    ) -> tuple[FaceMatch, ...]:
        self.searches.append(
            (face.face_index, collection_id, match_threshold, max_candidates)
        )
        return self._matches.get(face.face_index, ())


class FakeStore:
    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []
        self.failed: list[dict[str, Any]] = []

    async def record_run(self, **kwargs: Any) -> AttributionOutcome:
        self.runs.append(kwargs)
        # planned_seeds, not seed_owners: since 2026-08-31 the caller decides
        # WHAT each subject is searched with (the photo, or a crop of their own
        # face) and hands the store a plan rather than a list of people.
        seeds = tuple(
            RegisteredSeed(
                user_ref=plan.user_ref,
                seed_id=uuid4(),
                crop_object_ref=plan.crop_object_ref,
            )
            for plan in kwargs["planned_seeds"]
        )
        return AttributionOutcome(run_id=uuid4(), faces=kwargs["faces"], seeds=seeds)

    async def record_failed_run(self, **kwargs: Any) -> UUID:
        self.failed.append(kwargs)
        return uuid4()


class FakeScoreStore:
    """Records every ``recompute`` call, keyed on ``app.state`` so it needs no
    change to ``make_client``'s callers: fetch it back via
    ``client.app.state.score_store``. ``raise_error`` proves the
    swallow-and-log wrapper never changes the route's response."""

    def __init__(self, *, raise_error: bool = False) -> None:
        self._raise_error = raise_error
        self.calls: list[tuple[UserRef, str]] = []
        # Kept alongside `calls` rather than replacing it, same reasoning as
        # test_search_routes.py's fake: existing 2-tuple assertions stay put.
        self.calls_with_ref: list[tuple[UserRef, str, str | None]] = []

    async def recompute(
        self,
        user_ref: UserRef,
        *,
        cause_kind: str,
        cause_ref: str | None = None,
        now: Any = None,
    ) -> ScoreResult | None:
        if self._raise_error:
            raise RuntimeError("score store unavailable")
        self.calls.append((user_ref, cause_kind))
        self.calls_with_ref.append((user_ref, cause_kind, cause_ref))
        return None

    async def get_score(self, user_ref: UserRef) -> dict[str, Any] | None:
        raise NotImplementedError

    async def list_events(
        self, user_ref: UserRef, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def all_subject_refs(self) -> tuple[UserRef, ...]:
        raise NotImplementedError

    async def expire_due_threat_events(self, *, now: Any) -> int:
        raise NotImplementedError


class FakeUploader:
    """Records every presigned PUT. Reachable as
    ``client.app.state.object_uploader``, the same trick FakeScoreStore uses so
    ``make_client``'s existing 4-tuple callers need no change.

    ``fail_for`` names crop_refs whose PUT raises, which is how the
    no-seed-on-upload-failure rule gets exercised without a network.
    """

    def __init__(self, fail_for: frozenset[str] = frozenset()) -> None:
        self.puts: list[tuple[str, bytes, str]] = []
        self._fail_for = fail_for

    async def put(self, url: str, data: bytes, *, content_type: str) -> None:
        self.puts.append((url, data, content_type))
        if any(ref in url for ref in self._fail_for):
            raise UploadError("presigned PUT returned HTTP 403")


def make_client(
    faces: tuple[DetectedFace, ...] = (),
    matches: dict[int, tuple[FaceMatch, ...]] | None = None,
    *,
    provider_error: Exception | None = None,
    fetch_error: Exception | None = None,
    fetch_payload: bytes | None = None,
    raising_score_store: bool = False,
    upload_fails_for: frozenset[str] = frozenset(),
    **config_overrides: Any,
) -> tuple[TestClient, FakeProvider, FakeStore, FakeFetcher]:
    app = create_app(config=make_config(**config_overrides))
    provider = FakeProvider(faces, matches, provider_error)
    store = FakeStore()
    fetcher = FakeFetcher(fetch_error, fetch_payload)
    app.state.attribution_provider = provider
    app.state.attribution_store = store
    app.state.photo_fetcher = fetcher
    app.state.score_store = FakeScoreStore(raise_error=raising_score_store)
    app.state.object_uploader = FakeUploader(upload_fails_for)
    return TestClient(app), provider, store, fetcher


def _face(index: int) -> DetectedFace:
    return DetectedFace(face_index=index, bbox=BOX, detect_confidence=99.5)


def _body(candidates: list[UUID], **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "photo_ref": "photos/2026/08/abc.jpg",
        "requested_by": str(candidates[0]),
        "candidate_refs": [str(c) for c in candidates],
        "presigned_get_url": PRESIGNED,
    }
    body.update(overrides)
    return body


def _post(client: TestClient, body: dict[str, Any]) -> Any:
    return client.post("/v1/attribute", json=body, headers=AUTH)


# ── the seed rule ────────────────────────────────────────────────────────────


def test_one_enrolled_face_among_two_strangers_registers_one_seed() -> None:
    owner = uuid4()
    client, _p, _s, _f = make_client(
        faces=(_face(0), _face(1), _face(2)),
        matches={1: (FaceMatch(external_image_id=str(owner), similarity=95.0),)},
    )

    response = _post(client, _body([owner]))

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["seeds_registered"]) == 1
    assert payload["seeds_registered"][0]["user_ref"] == str(owner)
    # All three faces come back, strangers included, each with its bbox.
    assert [f["face_index"] for f in payload["faces"]] == [0, 1, 2]
    assert [f["resolved_user_ref"] for f in payload["faces"]] == [None, str(owner), None]
    assert all(f["bbox"] == {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4} for f in payload["faces"])
    # score.recompute fires exactly once, with the seed-registered cause.
    assert client.app.state.score_store.calls == [(UserRef(owner), "seed_registered")]
    # INVARIANTS #44: the cause is readable -- the attribution run that
    # registered the seed, the natural id this route has in scope.
    assert client.app.state.score_store.calls_with_ref == [
        (UserRef(owner), "seed_registered", payload["run_id"])
    ]


def test_two_household_members_register_two_seeds() -> None:
    alice, bob = uuid4(), uuid4()
    client, _p, _s, _f = make_client(
        faces=(_face(0), _face(1)),
        matches={
            0: (FaceMatch(external_image_id=str(alice), similarity=95.0),),
            1: (FaceMatch(external_image_id=str(bob), similarity=96.0),),
        },
    )

    payload = _post(client, _body([alice, bob])).json()

    assert {s["user_ref"] for s in payload["seeds_registered"]} == {str(alice), str(bob)}
    # Once per DISTINCT registered seed user_ref.
    assert set(client.app.state.score_store.calls) == {
        (UserRef(alice), "seed_registered"),
        (UserRef(bob), "seed_registered"),
    }
    assert len(client.app.state.score_store.calls) == 2
    # Same attribution run for both -- one outcome, two registered seeds.
    assert set(client.app.state.score_store.calls_with_ref) == {
        (UserRef(alice), "seed_registered", payload["run_id"]),
        (UserRef(bob), "seed_registered", payload["run_id"]),
    }


def test_no_enrolled_faces_registers_no_seeds_and_recomputes_nothing() -> None:
    owner, stranger = uuid4(), uuid4()
    client, _p, _s, _f = make_client(
        faces=(_face(0),),
        matches={0: (FaceMatch(external_image_id=str(stranger), similarity=99.9),)},
    )

    response = _post(client, _body([owner]))

    assert response.status_code == 200
    assert client.app.state.score_store.calls == []


def test_response_is_unchanged_when_score_recompute_raises() -> None:
    """The swallow-and-log wrapper: a broken score store must never turn a
    successful attribution into a failed request."""
    owner = uuid4()
    client, _p, _s, _f = make_client(
        faces=(_face(0),),
        matches={0: (FaceMatch(external_image_id=str(owner), similarity=95.0),)},
        raising_score_store=True,
    )

    response = _post(client, _body([owner]))

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["seeds_registered"]) == 1


def test_no_enrolled_faces_returns_200_with_zero_seeds() -> None:
    """Not an error. Most faces in most photos belong to people who are not
    enrolled, and that is the outcome the rule intends."""
    owner, stranger = uuid4(), uuid4()
    client, _p, _s, _f = make_client(
        faces=(_face(0),),
        matches={0: (FaceMatch(external_image_id=str(stranger), similarity=99.9),)},
    )

    response = _post(client, _body([owner]))

    assert response.status_code == 200
    payload = response.json()
    assert payload["seeds_registered"] == []
    assert payload["faces"][0]["resolved_user_ref"] is None
    assert payload["faces"][0]["match_score"] is None


def test_no_faces_at_all_returns_200_with_an_empty_list() -> None:
    owner = uuid4()
    client, provider, store, _f = make_client(faces=())

    response = _post(client, _body([owner]))

    assert response.status_code == 200
    assert response.json()["faces"] == []
    assert response.json()["seeds_registered"] == []
    assert provider.searches == []  # nothing to search for
    assert len(store.runs) == 1  # the run is still recorded, and completed


def test_a_non_candidate_outranking_the_owner_is_discarded_at_the_route() -> None:
    """The same planted-non-candidate assertion as the unit test, through the
    whole stack — proving nothing downstream re-admits the discarded match."""
    owner, stranger = uuid4(), uuid4()
    client, _p, _s, _f = make_client(
        faces=(_face(0),),
        matches={
            0: (
                FaceMatch(external_image_id=str(stranger), similarity=99.9),
                FaceMatch(external_image_id=str(owner), similarity=90.1),
            )
        },
    )

    payload = _post(client, _body([owner])).json()

    assert payload["faces"][0]["resolved_user_ref"] == str(owner)
    assert payload["faces"][0]["match_score"] == 90.1
    assert [s["user_ref"] for s in payload["seeds_registered"]] == [str(owner)]


# ── config, not the request body ─────────────────────────────────────────────


def test_the_threshold_and_max_candidates_come_from_config() -> None:
    """INVARIANTS #1b. Taking them from the body would let a caller bypass the
    ATTRIBUTION_MAX_CANDIDATES >= 2 floor that boot refuses below."""
    owner = uuid4()
    client, provider, store, _f = make_client(
        faces=(_face(0),),
        attribution_match_threshold=88.5,
        attribution_max_candidates=7,
    )

    _post(client, _body([owner]))

    assert provider.searches == [(0, "identity-v1", 88.5, 7)]
    # ...and both are recorded on the run, so a later retune cannot make this
    # attribution uninterpretable.
    assert store.runs[0]["match_threshold"] == 88.5
    assert store.runs[0]["max_candidates"] == 7


def test_body_supplied_threshold_is_rejected_as_an_unknown_field() -> None:
    owner = uuid4()
    client, _p, store, _f = make_client(faces=(_face(0),))

    response = _post(client, _body([owner], match_threshold=1.0))

    assert response.status_code == 422
    assert store.runs == []


# ── request validation ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url", ["http://proxy-s3.example/p.jpg", "s3://bucket/key.jpg", "photos/abc.jpg"]
)
def test_a_non_https_presigned_url_is_422(url: str) -> None:
    owner = uuid4()
    client, _p, store, fetcher = make_client(faces=(_face(0),))

    response = _post(client, _body([owner], presigned_get_url=url))

    assert response.status_code == 422
    assert fetcher.urls == []  # never fetched
    assert store.runs == []


def test_an_empty_candidate_list_is_422() -> None:
    """Zero candidates cannot attribute anything, so the call is a no-op that
    would still cost a DetectFaces and N searches."""
    client, _p, _s, fetcher = make_client(faces=(_face(0),))

    response = _post(
        client,
        {
            "photo_ref": "photos/abc.jpg",
            "requested_by": str(uuid4()),
            "candidate_refs": [],
            "presigned_get_url": PRESIGNED,
        },
    )

    assert response.status_code == 422
    assert fetcher.urls == []


def test_a_blank_photo_ref_is_422() -> None:
    owner = uuid4()
    client, _p, _s, _f = make_client(faces=(_face(0),))
    assert _post(client, _body([owner], photo_ref="   ")).status_code == 422


def test_attribute_requires_a_service_token() -> None:
    client, _p, _s, _f = make_client()
    response = client.post("/v1/attribute", json=_body([uuid4()]))
    assert response.status_code == 401


# ── failure is recorded, not silently empty ──────────────────────────────────


def test_a_provider_failure_is_503_and_records_a_failed_run() -> None:
    """'We could not look' must stay distinguishable from 'we looked and
    matched nobody' — a completed run with zero faces would read as the
    second."""
    owner = uuid4()
    client, _p, store, _f = make_client(
        provider_error=AttributionUnavailable("DetectFaces failed with Throttling")
    )

    response = _post(client, _body([owner]))

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "attribution_unavailable"
    assert response.json()["error"]["retryable"] is True
    assert store.runs == []
    assert len(store.failed) == 1
    assert "Throttling" in store.failed[0]["error_detail"]


def test_a_webp_photo_reaches_rekognition_as_jpeg() -> None:
    """The proxy's presigned photo can be WebP/HEIC; Rekognition takes
    JPEG/PNG only (spec 2026-08-21 §2)."""
    owner = uuid4()
    client, provider, _store, _fetcher = make_client(
        (_face(0),),
        {0: (FaceMatch(external_image_id=str(owner), similarity=97.0),)},
        fetch_payload=_encoded("WEBP"),
    )

    response = _post(client, _body([owner]))

    assert response.status_code == 200
    with Image.open(io.BytesIO(provider.detect_bytes[0])) as opened:
        assert opened.format == "JPEG"


def test_undecodable_photo_bytes_are_503_and_record_a_failed_run() -> None:
    owner = uuid4()
    client, provider, store, _fetcher = make_client(fetch_payload=b"not an image")

    response = _post(client, _body([owner]))

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "attribution_unavailable"
    assert provider.detect_bytes == []  # never reaches Rekognition
    assert len(store.failed) == 1
    assert "undecodable" in store.failed[0]["error_detail"]


def test_the_photo_is_fetched_through_the_presigned_url_only() -> None:
    """No S3 client anywhere; the bytes arrive through the proxy's URL and are
    discarded. Asserted on the fetcher because it is the one component allowed
    to hold them."""
    owner = uuid4()
    client, _p, _s, fetcher = make_client(faces=())

    _post(client, _body([owner]))

    assert fetcher.urls == [PRESIGNED]


def test_the_photo_ref_not_the_url_is_what_reaches_the_store() -> None:
    """The presigned URL expires; the seed must carry the durable ref
    (migration 0011). A URL landing in source_object_ref is the bug 02 fixed."""
    owner = uuid4()
    client, _p, store, _f = make_client(
        faces=(_face(0),),
        matches={0: (FaceMatch(external_image_id=str(owner), similarity=95.0),)},
    )

    _post(client, _body([owner]))

    assert store.runs[0]["photo_ref"] == "photos/2026/08/abc.jpg"
    assert PRESIGNED not in str(store.runs[0])
