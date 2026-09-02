from __future__ import annotations

import io

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from imageshield.fetcher.app import create_app
from imageshield.fetcher.config import FetcherConfig

TOKEN = "fetcher-token-for-tests-0003"
AUTH = {"X-Fetcher-Token": TOKEN}


def _config() -> FetcherConfig:
    return FetcherConfig(fetcher_token=TOKEN)


def _png(size: tuple[int, int] = (64, 64)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (120, 30, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


def _client(handler, *, resolver=None) -> TestClient:
    app = create_app(config=_config())
    app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.resolver = resolver or (lambda host: ("93.184.216.34",))  # a global address
    return TestClient(app)


def _image_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=_png(), headers={"content-type": "image/png"})


def test_fetch_requires_the_token() -> None:
    client = _client(_image_handler)
    assert client.post("/v1/fetch", json={"url": "https://x.example/a.png"}).status_code == 401


def test_fetch_returns_the_image_bytes() -> None:
    client = _client(_image_handler)
    response = client.post("/v1/fetch", json={"url": "https://x.example/a.png"}, headers=AUTH)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == _png()


def test_fetch_refuses_private_addresses() -> None:
    client = _client(_image_handler, resolver=lambda host: ("169.254.169.254",))
    response = client.post("/v1/fetch", json={"url": "https://x.example/a.png"}, headers=AUTH)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "refused_private_address"


def test_fetch_refuses_non_image_content_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})

    client = _client(handler)
    response = client.post("/v1/fetch", json={"url": "https://x.example/a"}, headers=AUTH)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "not_an_image"


def test_fetch_caps_the_body_size() -> None:
    big = b"\xff" * (10 * 1024 * 1024 + 1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=big, headers={"content-type": "image/jpeg"})

    client = _client(handler)
    response = client.post("/v1/fetch", json={"url": "https://x.example/big"}, headers=AUTH)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "too_large"


def test_fetch_rechecks_every_redirect_hop() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "x.example":
            return httpx.Response(302, headers={"location": "https://evil.internal/a.png"})
        return httpx.Response(200, content=_png(), headers={"content-type": "image/png"})

    def resolver(host: str) -> tuple[str, ...]:
        return ("10.0.0.5",) if host == "evil.internal" else ("93.184.216.34",)

    client = _client(handler, resolver=resolver)
    response = client.post("/v1/fetch", json={"url": "https://x.example/a.png"}, headers=AUTH)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "refused_private_address"


def _textured_png(size: tuple[int, int] = (256, 256)) -> bytes:
    """See tests/test_fetcher_render.py::_texture — a flat fill cannot show a
    blur, so the crop-route tests need texture too."""
    cells = (max(2, size[0] // 8), max(2, size[1] // 8))
    base = Image.new("L", cells)
    base.putdata([255 if (x + y) % 2 == 0 else 0 for y in range(cells[1]) for x in range(cells[0])])
    buffer = io.BytesIO()
    base.resize(size, Image.NEAREST).convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


def _textured_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=_textured_png(), headers={"content-type": "image/png"})


def _grey_variance(data: bytes) -> float:
    pixels = list(Image.open(io.BytesIO(data)).convert("L").getdata())
    mean = sum(pixels) / len(pixels)
    return sum((value - mean) ** 2 for value in pixels) / len(pixels)


CROP_BODY = {
    "url": "https://x.example/a.png",
    "bbox": {"x": 0.4, "y": 0.4, "w": 0.2, "h": 0.2},
}


def test_crop_returns_the_whole_frame_blurred_by_default() -> None:
    """Changed 2026-09-02: this asserted ``size[0] < 64  # cropped, not the
    whole frame``. The whole frame IS the contract now -- the subject needs to
    recognise the photo to answer honestly, and the blur is what makes showing
    it safe (spec §0.1)."""
    client = _client(_textured_handler)
    response = client.post("/v1/crop", json=CROP_BODY, headers=AUTH)

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    rendered = Image.open(io.BytesIO(response.content))
    assert rendered.size == (256, 256)  # the whole frame, not a crop


def test_crop_with_blur_false_sharpens_only_the_face() -> None:
    """``blur=false`` is 'sharpen the face', NOT 'return it unblurred' -- the
    two responses must differ (a reveal button that changes nothing is a lie),
    and the revealed one must still be mostly blurred (spec §0.4)."""
    client = _client(_textured_handler)
    blurred = client.post("/v1/crop", json=CROP_BODY, headers=AUTH)
    revealed = client.post("/v1/crop", json={**CROP_BODY, "blur": False}, headers=AUTH)

    assert blurred.status_code == revealed.status_code == 200
    assert blurred.headers["content-type"] == revealed.headers["content-type"] == "image/jpeg"
    assert blurred.content != revealed.content

    assert _grey_variance(revealed.content) < _grey_variance(_textured_png()) / 3


def test_crop_no_longer_refuses_a_tiny_face() -> None:
    """``crop_too_small`` was raised by ``crop_to_face``, which this route no
    longer calls. A tiny face now yields a blurred frame (spec §1a)."""
    client = _client(_textured_handler)
    body = {**CROP_BODY, "bbox": {"x": 0.5, "y": 0.5, "w": 0.001, "h": 0.001}}
    response = client.post("/v1/crop", json=body, headers=AUTH)

    assert response.status_code == 200


def test_crop_still_refuses_undecodable_bytes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"nope", headers={"content-type": "image/png"})

    client = _client(handler)
    response = client.post("/v1/crop", json=CROP_BODY, headers=AUTH)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "not_an_image"


def test_health_needs_no_token() -> None:
    assert _client(_image_handler).get("/health").status_code == 200
