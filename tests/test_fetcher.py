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


def test_crop_returns_blurred_jpeg_by_default() -> None:
    client = _client(_image_handler)
    body = {"url": "https://x.example/a.png", "bbox": {"x": 0.25, "y": 0.25, "w": 0.5, "h": 0.5}}
    response = client.post("/v1/crop", json=body, headers=AUTH)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    blurred = Image.open(io.BytesIO(response.content))
    assert blurred.size[0] < 64  # cropped, not the whole frame


def test_health_needs_no_token() -> None:
    assert _client(_image_handler).get("/health").status_code == 200
