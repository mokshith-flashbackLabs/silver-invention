"""Google Web Detection adapter tests, over httpx.MockTransport.

The two rules that must never regress: provider_score is None on every
match (no synthesised numbers for a categorical provider), and webEntities
never produce match rows (they name famous people via knowledge-graph
lookup — not evidence about our user)."""

from __future__ import annotations

import json
from typing import Any

import httpx

from imageshield.search.google import GoogleWebDetectionProvider

ENDPOINT = "https://vision.googleapis.com/v1/images:annotate"


def _body(web_detection: dict[str, Any]) -> dict[str, Any]:
    return {"responses": [{"webDetection": web_detection}]}


def _adapter(handler: Any) -> GoogleWebDetectionProvider:
    return GoogleWebDetectionProvider(
        endpoint=ENDPOINT,
        api_key="g-test",
        timeout_seconds=5.0,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


FIXTURE = _body(
    {
        "fullMatchingImages": [{"url": "https://a/full.jpg"}],
        "partialMatchingImages": [{"url": "https://a/partial.jpg", "score": None}],
        "pagesWithMatchingImages": [{"url": "https://a/page.html", "pageTitle": "t"}],
        "visuallySimilarImages": [{"url": "https://a/similar.jpg"}],
        "webEntities": [
            {"entityId": "/m/x", "score": 12.9, "description": "Famous Person"}
        ],
        "bestGuessLabels": [{"label": "person"}],
    }
)


async def test_categories_mapped_and_scores_always_null() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.read())
        return httpx.Response(200, json=FIXTURE)

    result = await _adapter(handler).search("https://seed.example/s.jpg")

    assert seen["url"] == f"{ENDPOINT}?key=g-test"
    request_image = seen["body"]["requests"][0]["image"]
    assert request_image == {"source": {"imageUri": "https://seed.example/s.jpg"}}
    assert seen["body"]["requests"][0]["features"][0]["type"] == "WEB_DETECTION"

    assert result.status == "ok"
    by_category = {m.provider_category: m for m in result.matches}
    assert set(by_category) == {"full_match", "partial_match", "page_match"}
    assert all(m.provider_score is None for m in result.matches)  # NULL. Always.
    assert by_category["full_match"].image_url == "https://a/full.jpg"
    assert by_category["full_match"].page_urls == []  # Google reports no host page
    assert by_category["page_match"].image_url == "https://a/page.html"
    assert by_category["page_match"].page_urls == ["https://a/page.html"]  # it IS a page


async def test_web_entities_never_become_matches() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_body(
                {"webEntities": [{"description": "Famous Person", "score": 22.1}]}
            ),
        )

    result = await _adapter(handler).search("https://seed/s.jpg")
    assert result.status == "ok"
    assert result.matches == []
    # verbatim raw keeps them for the record
    assert "Famous Person" in json.dumps(result.raw_response)


async def test_per_image_error_inside_200_is_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"responses": [{"error": {"code": 7, "message": "cannot fetch url"}}]},
        )

    result = await _adapter(handler).search("https://seed/s.jpg")
    assert result.status == "error"
    assert result.http_status == 200
    assert result.raw_response["responses"][0]["error"]["message"] == "cannot fetch url"


async def test_empty_web_detection_is_ok_zero_matches() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_body({}))

    result = await _adapter(handler).search("https://seed/s.jpg")
    assert result.status == "ok"
    assert result.matches == []


async def test_max_results_flows_into_request_and_slices() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.read())
        return httpx.Response(
            200,
            json=_body(
                {"fullMatchingImages": [{"url": f"https://a/{n}.jpg"} for n in range(4)]}
            ),
        )

    result = await _adapter(handler).search("https://seed/s.jpg", max_results=2)
    assert seen["body"]["requests"][0]["features"][0]["maxResults"] == 2
    assert len(result.matches) == 2


async def test_timeout_and_http_error() -> None:
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    result = await _adapter(timeout_handler).search("https://seed/s.jpg")
    assert result.status == "timeout"

    def error_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "key invalid"}})

    result = await _adapter(error_handler).search("https://seed/s.jpg")
    assert result.status == "error"
    assert result.http_status == 403
    assert result.raw_response == {"error": {"message": "key invalid"}}
