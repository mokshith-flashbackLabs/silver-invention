"""Hive Web Search adapter tests, over httpx.MockTransport.

The three old-repo defects are pinned absent: scores stay raw Decimal in
0.5-1.0 (never rescaled to percent — weeklyInfringementScanner.js:1129), the 429 retry is
bounded (js:1148 recursed without a depth counter), and hashing is not this
module's business at all.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx

from imageshield.providers.ratelimit import RetryPolicy
from imageshield.search.hive import HiveWebSearchProvider

BASE = "https://api.thehive.ai"


def _ok_body(matches: list[dict[str, Any]], quality: str | None = "good") -> dict[str, Any]:
    output: dict[str, Any] = {"matches": matches}
    if quality is not None:
        output["query_quality"] = quality
    return {"status": [{"response": {"output": output}}]}


# Two retries rather than the config default of three: enough to prove the
# bound without making every 429 test wait on four round trips. jitter_fraction
# 0 keeps the (zero) waits deterministic.
RETRY = RetryPolicy(max_retries=2, max_wait_seconds=1.0, jitter_fraction=0.0)


def _adapter(handler: Any, retry: RetryPolicy = RETRY) -> HiveWebSearchProvider:
    return HiveWebSearchProvider(
        base_url=BASE,
        api_key="k-test",
        timeout_seconds=5.0,
        retry_policy=retry,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_search_parses_matches_raw_scores_and_backlinks() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        seen["body"] = request.read().decode()
        return httpx.Response(
            200,
            json=_ok_body(
                [
                    {
                        "url": "https://img.example/a.jpg",
                        "score": 0.8712,
                        "backlinks": [
                            {"url": "https://page.example/post/1"},
                            {"url": "https://page.example/post/2"},
                            {"url": "https://page.example/post/1"},  # duplicate
                            {"noturl": True},  # malformed entry
                            {"url": "https://mirror.example/copy"},
                        ],
                    },
                    {"url": "https://img.example/b.jpg", "score": 0.5001, "backlinks": []},
                ]
            ),
        )

    result = await _adapter(handler).search("https://seed.example/s.jpg")

    assert seen["auth"] == "token k-test"
    assert seen["url"] == f"{BASE}/api/v2/task/sync"
    assert "https%3A%2F%2Fseed.example%2Fs.jpg" in seen["body"]

    assert result.status == "ok"
    assert result.http_status == 200
    first, second = result.matches
    assert first.provider_score == Decimal("0.8712")  # RAW — no rescale, ever
    # EVERY backlink: one match with three distinct pages is three places to
    # act, so the store makes three infringements out of it. Order preserved,
    # duplicates collapsed, malformed entries skipped.
    assert first.page_urls == [
        "https://page.example/post/1",
        "https://page.example/post/2",
        "https://mirror.example/copy",
    ]
    assert first.provider_category is None
    assert first.query_quality == "good"
    assert second.provider_score == Decimal("0.5001")
    assert second.page_urls == []
    # raw_response is the verbatim body, sufficient to recompute everything
    assert result.raw_response["status"][0]["response"]["output"]["matches"][0]["score"] == 0.8712


async def test_similarity_score_fallback_key_is_read_raw() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_ok_body([{"url": "https://i/x.jpg", "similarity_score": 0.66}])
        )

    result = await _adapter(handler).search("https://seed/s.jpg")
    assert result.matches[0].provider_score == Decimal("0.66")


async def test_scoreless_match_is_skipped_but_kept_in_raw_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_ok_body(
                [{"url": "https://i/no-score.jpg"}, {"url": "https://i/ok.jpg", "score": 0.7}]
            ),
        )

    result = await _adapter(handler).search("https://seed/s.jpg")
    assert [m.image_url for m in result.matches] == ["https://i/ok.jpg"]
    assert len(result.raw_response["status"][0]["response"]["output"]["matches"]) == 2


async def test_max_results_slices() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_ok_body([{"url": f"https://i/{n}.jpg", "score": 0.9} for n in range(5)]),
        )

    result = await _adapter(handler).search("https://seed/s.jpg", max_results=2)
    assert len(result.matches) == 2


async def test_429_retries_once_then_succeeds() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"why": "slow down"})
        return httpx.Response(200, json=_ok_body([{"url": "https://i/a.jpg", "score": 0.8}]))

    result = await _adapter(handler).search("https://seed/s.jpg")
    assert len(calls) == 2
    assert result.status == "ok"
    assert len(result.matches) == 1
    assert result.attempts == 2  # recorded on provider_calls.attempt


async def test_persistent_429_is_rate_limited_and_bounded() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "0"}, json={"why": "still no"})

    result = await _adapter(handler).search("https://seed/s.jpg")
    # 1 initial + PROVIDER_MAX_RETRIES retries, then stop. Bounded, unlike the
    # old lambda's unbounded recursion (weeklyInfringementScanner.js:1148).
    assert len(calls) == 1 + RETRY.max_retries
    assert result.status == "rate_limited"
    assert result.attempts == 1 + RETRY.max_retries
    assert result.http_status == 429
    assert result.matches == []
    assert result.raw_response == {"why": "still no"}


async def test_timeout_is_a_result_not_an_exception() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom")

    result = await _adapter(handler).search("https://seed/s.jpg")
    assert result.status == "timeout"
    assert result.http_status is None
    assert "boom" in json.dumps(result.raw_response)


async def test_http_error_preserves_body_verbatim() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "internal"})

    result = await _adapter(handler).search("https://seed/s.jpg")
    assert result.status == "error"
    assert result.http_status == 500
    assert result.raw_response == {"message": "internal"}


async def test_200_without_matches_path_is_error_not_empty_ok() -> None:
    """The wrong-Hive-project tripwire: a key provisioned against Media
    Search returns a plausible 200 with a different shape. That must never
    read as 'no matches found'."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": [{"response": {"output": {"classes": []}}}]})

    result = await _adapter(handler).search("https://seed/s.jpg")
    assert result.status == "error"
    assert result.matches == []


async def test_empty_matches_list_is_ok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_body([]))

    result = await _adapter(handler).search("https://seed/s.jpg")
    assert result.status == "ok"
    assert result.matches == []
