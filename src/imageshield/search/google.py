"""Google Cloud Vision **Web Detection** adapter — categorical, no numbers.

Full and partial matches carry no similarity score (``score: null``,
observed live 2026-08-06 — devtools/harness/README.md), so this provider's
score shape is **categorical**: ``full_match`` / ``partial_match`` /
``page_match``, and ``provider_score`` is None on every match, always.
Synthesising a number here would be normalising inside the adapter, which
makes recalibration a redeploy (CLAUDE.md §7.2).

``webEntities`` (and ``bestGuessLabels``) are never read: they resolve via
knowledge-graph lookup and name famous people only — non-public figures get
generic content labels. Google deliberately does not identify faces, so an
entity match is not evidence about our user and must never influence a band.
The strings appear in this module only inside ``raw_response``, verbatim.

Request shape proven live by the harness (devtools/harness/server.py:308-382).
The seed is passed as ``imageUri`` (a presigned GET minted by the proxy) so
no image bytes pass through this service; Google fetches the URL itself, so
it must be publicly reachable.
"""

from __future__ import annotations

import time
from typing import Any, Literal

import httpx

from imageshield.providers.ratelimit import RetryPolicy, send_with_retry
from imageshield.search.provider import ProviderMatch, ProviderResult, ProviderStatus
from imageshield.types import ProviderId

_DEFAULT_MAX_RESULTS = 50  # the harness default; Vision's own cap per feature

# webDetection section -> provider_category. visuallySimilarImages is
# deliberately absent: "similar" is not a match attestation.
_SECTION_CATEGORIES = (
    ("fullMatchingImages", "full_match"),
    ("partialMatchingImages", "partial_match"),
    ("pagesWithMatchingImages", "page_match"),
)

_NON_JSON_BODY_LIMIT = 2000


class GoogleWebDetectionProvider:
    id: ProviderId = ProviderId("google")
    kind: Literal["image_search", "face_search", "classifier"] = "image_search"
    score_kind: Literal["numeric", "categorical"] = "categorical"
    score_version = "google-web-detection-v1"

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        timeout_seconds: float,
        retry_policy: RetryPolicy,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._retry_policy = retry_policy
        self._client = client if client is not None else httpx.AsyncClient()

    async def search(
        self, seed_url: str, max_results: int | None = None
    ) -> ProviderResult:
        started = time.monotonic()

        def _result(
            status: ProviderStatus,
            *,
            matches: list[ProviderMatch] | None = None,
            raw: dict[str, Any],
            http_status: int | None,
            attempts: int = 1,
            error_detail: str | None = None,
        ) -> ProviderResult:
            return ProviderResult(
                provider_id=self.id,
                status=status,
                matches=matches or [],
                raw_response=raw,
                http_status=http_status,
                latency_ms=int(1000 * (time.monotonic() - started)),
                attempts=attempts,
                error_detail=error_detail,
            )

        body = {
            "requests": [
                {
                    "image": {"source": {"imageUri": seed_url}},
                    "features": [
                        {
                            "type": "WEB_DETECTION",
                            "maxResults": max_results or _DEFAULT_MAX_RESULTS,
                        }
                    ],
                }
            ]
        }
        async def _send() -> httpx.Response:
            return await self._client.post(
                self._endpoint,
                params={"key": self._api_key},
                json=body,
                timeout=self._timeout,
            )

        try:
            response, attempts = await send_with_retry(_send, self._retry_policy)
        except httpx.TimeoutException as exc:
            return _result(
                "timeout",
                raw={"exception": str(exc)},
                http_status=None,
                error_detail="request timed out",
            )
        except httpx.HTTPError as exc:
            return _result(
                "error",
                raw={"exception": str(exc)},
                http_status=None,
                error_detail=f"transport error: {type(exc).__name__}",
            )

        raw = _decode_body(response)
        if response.status_code == 429:
            return _result(
                "rate_limited",
                raw=raw,
                http_status=429,
                attempts=attempts,
                error_detail=f"rate limited after {attempts} attempt(s)",
            )
        if response.status_code != 200:
            return _result(
                "error",
                raw=raw,
                http_status=response.status_code,
                attempts=attempts,
                error_detail=f"http {response.status_code}",
            )

        responses = raw.get("responses")
        first: dict[str, Any] = (
            responses[0]
            if isinstance(responses, list) and responses and isinstance(responses[0], dict)
            else {}
        )
        if "error" in first:
            # Vision reports per-image failures (unfetchable URL, bad image)
            # inside a 200 — that is a failed call, not "nothing found".
            return _result(
                "error",
                raw=raw,
                http_status=200,
                attempts=attempts,
                error_detail="200 carrying a per-image error",
            )

        web_detection = first.get("webDetection")
        matches = _to_matches(web_detection if isinstance(web_detection, dict) else {})
        if max_results is not None:
            matches = matches[:max_results]
        return _result(
            "ok", matches=matches, raw=raw, http_status=200, attempts=attempts
        )


def _decode_body(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {"non_json_body": response.text[:_NON_JSON_BODY_LIMIT]}
    if not isinstance(payload, dict):
        return {"non_object_body": payload}
    return payload


def _to_matches(web_detection: dict[str, Any]) -> list[ProviderMatch]:
    matches: list[ProviderMatch] = []
    for section, category in _SECTION_CATEGORIES:
        entries = web_detection.get(section)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("url"):
                continue
            url = str(entry["url"])
            matches.append(
                ProviderMatch(
                    image_url=url,
                    # a page_match entry IS a page; full/partial are images
                    # whose host page Google does not report, so they carry no
                    # page at all and the store keys them on the image URL.
                    page_urls=[url] if category == "page_match" else [],
                    provider_score=None,  # NULL. Always. Never synthesised.
                    provider_category=category,
                    query_quality=None,
                )
            )
    return matches
