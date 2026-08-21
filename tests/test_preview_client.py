"""FetcherCropClient over httpx.MockTransport — no network.

The split that matters: 'this image cannot yield a crop' (unrenderable, do not
retry) versus 'the fetch went wrong' (upstream, retryable). The route maps the
first to preview_unavailable and the second to preview_unavailable_upstream.
"""

from __future__ import annotations

import json

import httpx
import pytest

from imageshield.preview.client import CropUnavailable, FetcherCropClient

BBOX = {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}


def _client(handler: httpx.MockTransport) -> FetcherCropClient:
    return FetcherCropClient(
        httpx.AsyncClient(transport=handler),
        base_url="http://fetcher.test:8083",
        token="fetcher-token-value",
    )


async def test_crop_posts_the_contract_and_returns_bytes() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["token"] = request.headers.get("X-Fetcher-Token")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"jpeg-bytes")

    client = _client(httpx.MockTransport(handler))
    content = await client.crop(url="https://cdn.example/a.webp", bbox=BBOX, blur=True)

    assert content == b"jpeg-bytes"
    assert seen["url"] == "http://fetcher.test:8083/v1/crop"
    assert seen["token"] == "fetcher-token-value"
    assert seen["body"] == {"url": "https://cdn.example/a.webp", "bbox": BBOX, "blur": True}


@pytest.mark.parametrize("code", ["crop_too_small", "not_an_image"])
async def test_unrenderable_codes_are_flagged(code: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": code, "message": "no"}})

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(CropUnavailable) as excinfo:
        await client.crop(url="https://cdn.example/a.jpg", bbox=BBOX, blur=True)
    assert excinfo.value.unrenderable is True


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"error": {"code": "refused_private_address"}}),
        httpx.Response(502, text="bad gateway"),
        httpx.Response(500, text="not json at all"),
    ],
)
async def test_other_failures_are_upstream(response: httpx.Response) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(CropUnavailable) as excinfo:
        await client.crop(url="https://cdn.example/a.jpg", bbox=BBOX, blur=False)
    assert excinfo.value.unrenderable is False


async def test_transport_errors_are_upstream() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(CropUnavailable) as excinfo:
        await client.crop(url="https://cdn.example/a.jpg", bbox=BBOX, blur=True)
    assert excinfo.value.unrenderable is False
