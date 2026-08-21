"""``GET /v1/infringements/{id}/preview`` over fakes (repo convention:
TestClient never runs the lifespan; the store's SQL is proven in
tests/test_preview_store.py, the client's HTTP in tests/test_preview_client.py).

The two load-bearing route behaviours: the ownership 404 is byte-identical for
absent and not-yours (the oracle), and every render writes the audit row BEFORE
the crop is attempted (INVARIANTS #31) with the ceiling enforced off the same
trail (#32)."""

from __future__ import annotations

from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from imageshield.http.app import create_app
from imageshield.preview.client import CropUnavailable
from imageshield.preview.store import PreviewTarget
from imageshield.types import UserRef
from tests.conftest import SERVICE_TOKEN, make_config

AUTH = {"X-Service-Token": SERVICE_TOKEN}
BBOX = {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}
IMAGE_URL = "https://cdn.example/hit.webp"


class FakePreviewStore:
    def __init__(
        self,
        target: PreviewTarget | None,
        *,
        renders: int = 0,
    ) -> None:
        self._target = target
        self._renders = renders
        self.target_calls: list[tuple[UUID, UserRef]] = []
        self.recorded: list[tuple[UserRef, UUID, bool]] = []

    async def target(
        self, infringement_id: UUID, user_ref: UserRef
    ) -> PreviewTarget | None:
        self.target_calls.append((infringement_id, user_ref))
        return self._target

    async def renders_last_24h(self, user_ref: UserRef) -> int:
        return self._renders

    async def record_render(
        self, user_ref: UserRef, infringement_id: UUID, *, reveal: bool
    ) -> None:
        self.recorded.append((user_ref, infringement_id, reveal))


class FakeCropClient:
    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.calls: list[dict[str, object]] = []

    async def crop(self, *, url: str, bbox: dict[str, float], blur: bool) -> bytes:
        self.calls.append({"url": url, "bbox": bbox, "blur": blur})
        if self._error is not None:
            raise self._error
        return b"jpeg-bytes"


def make_client(
    target: PreviewTarget | None,
    *,
    renders: int = 0,
    crop_error: Exception | None = None,
) -> tuple[TestClient, FakePreviewStore, FakeCropClient]:
    app = create_app(config=make_config())
    store = FakePreviewStore(target, renders=renders)
    crop = FakeCropClient(error=crop_error)
    app.state.preview_store = store
    app.state.crop_client = crop
    return TestClient(app), store, crop


def _get(client: TestClient, infringement_id: UUID, user_ref: UUID, **params: object):
    return client.get(
        f"/v1/infringements/{infringement_id}/preview",
        params={"user_ref": str(user_ref), **params},
        headers=AUTH,
    )


def test_absent_and_not_yours_are_byte_identical_404s() -> None:
    client, _store, _crop = make_client(None)

    first = _get(client, uuid4(), uuid4())
    second = _get(client, uuid4(), uuid4())

    assert first.status_code == 404
    assert first.json()["error"]["code"] == "infringement_not_found"
    # Byte-identical bodies modulo request_id: no oracle.
    a, b = first.json(), second.json()
    a["error"].pop("request_id", None)
    b["error"].pop("request_id", None)
    assert a == b


def test_no_bbox_is_preview_unavailable() -> None:
    client, _store, crop = make_client(PreviewTarget(image_url=IMAGE_URL, bbox=None))

    response = _get(client, uuid4(), uuid4())

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "preview_unavailable"
    assert crop.calls == []


def test_happy_path_streams_blurred_jpeg_with_no_store() -> None:
    client, store, crop = make_client(PreviewTarget(image_url=IMAGE_URL, bbox=BBOX))
    infringement_id, user_ref = uuid4(), uuid4()

    response = _get(client, infringement_id, user_ref)

    assert response.status_code == 200
    assert response.content == b"jpeg-bytes"
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "no-store, private"
    assert crop.calls == [{"url": IMAGE_URL, "bbox": BBOX, "blur": True}]
    assert store.recorded == [(user_ref, infringement_id, False)]


def test_reveal_passes_blur_false_and_is_audited() -> None:
    client, store, crop = make_client(PreviewTarget(image_url=IMAGE_URL, bbox=BBOX))
    infringement_id, user_ref = uuid4(), uuid4()

    response = _get(client, infringement_id, user_ref, reveal="true")

    assert response.status_code == 200
    assert crop.calls[0]["blur"] is False
    assert store.recorded == [(user_ref, infringement_id, True)]


def test_ceiling_answers_429_before_rendering() -> None:
    client, store, crop = make_client(
        PreviewTarget(image_url=IMAGE_URL, bbox=BBOX), renders=200
    )

    response = _get(client, uuid4(), uuid4())

    assert response.status_code == 429
    body = response.json()["error"]
    assert body["code"] == "preview_rate_limited"
    assert body["retryable"] is True
    assert crop.calls == []
    assert store.recorded == []  # refused renders are not attempts


def test_unrenderable_crop_is_preview_unavailable_but_still_audited() -> None:
    client, store, _crop = make_client(
        PreviewTarget(image_url=IMAGE_URL, bbox=BBOX),
        crop_error=CropUnavailable("too small", unrenderable=True),
    )

    response = _get(client, uuid4(), uuid4())

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "preview_unavailable"
    assert len(store.recorded) == 1  # audit happened BEFORE the render (#31)


def test_upstream_failure_is_502_retryable() -> None:
    client, store, _crop = make_client(
        PreviewTarget(image_url=IMAGE_URL, bbox=BBOX),
        crop_error=CropUnavailable("fetcher 502", unrenderable=False),
    )

    response = _get(client, uuid4(), uuid4())

    assert response.status_code == 502
    body = response.json()["error"]
    assert body["code"] == "preview_unavailable_upstream"
    assert body["retryable"] is True
    assert len(store.recorded) == 1


def test_missing_token_is_401() -> None:
    client, _store, _crop = make_client(PreviewTarget(image_url=IMAGE_URL, bbox=BBOX))

    response = client.get(
        f"/v1/infringements/{uuid4()}/preview", params={"user_ref": str(uuid4())}
    )

    assert response.status_code == 401
