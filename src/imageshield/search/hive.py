"""Hive **Web Search** adapter (reverse image search, ~25B indexed images).

Which Hive product a key hits is determined by the **Hive project the key
belongs to, not the URL** — every task-based Hive product shares
``POST {base}/api/v2/task/sync``. A key provisioned against Hive's
separately-named "Media Search" (movies/TV matching) returns
plausible-looking wrong results rather than an error, which is why a 200
whose body lacks the Web Search ``matches`` path is reported as
``status='error'``, never as an empty ``ok`` (see ``_parse_output``).

Request construction follows the shape proven live by the harness
(devtools/harness/server.py:274-302, 2026-08-06) and originally by the old
repo's weeklyInfringementScanner.js:1078-1160 — read, not ported. Its three
defects are deliberately absent here:

- rescaling ``similarity_score`` into a percentage (js:1129). Scores here
  stay raw ``Decimal`` in Hive's native 0.5-1.0 domain, where 0.5 is the
  floor (the lowest score Hive reports), not a midpoint.
- unbounded recursive retry on 429 (js:1148). Here: one bounded retry.
- unnormalised URL hashing — not this module's business at all.

The seed is passed as the ``url`` form field (a presigned GET minted by the
proxy) so no image bytes pass through this service.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

import httpx

from imageshield.search.provider import ProviderMatch, ProviderResult, ProviderStatus
from imageshield.types import ProviderId

_MAX_RATE_LIMIT_RETRIES = 1
_RATE_LIMIT_WAIT_CAP_SECONDS = 30.0
_RATE_LIMIT_DEFAULT_WAIT_SECONDS = 60.0  # capped to the line above

# INFERRED: the harness observed "a query quality signal" in Web Search
# responses (devtools/harness/README.md) but no saved payload pins the exact
# key. Candidates checked in order; raw_response preserves the truth either
# way, so a rename costs nothing historically.
_QUERY_QUALITY_KEYS = ("query_quality", "quality")

_NON_JSON_BODY_LIMIT = 2000


def _decode_body(response: httpx.Response) -> dict[str, Any]:
    """The verbatim raw_response for the provider_calls row. Non-JSON and
    non-object bodies are wrapped, never discarded."""
    try:
        payload = response.json()
    except ValueError:
        return {"non_json_body": response.text[:_NON_JSON_BODY_LIMIT]}
    if not isinstance(payload, dict):
        return {"non_object_body": payload}
    return payload


class HiveWebSearchProvider:
    id: ProviderId = ProviderId("hive")
    kind: Literal["image_search", "face_search", "classifier"] = "image_search"
    score_kind: Literal["numeric", "categorical"] = "numeric"
    score_version = "hive-web-search-v1"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._endpoint = f"{base_url.rstrip('/')}/api/v2/task/sync"
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._client = client if client is not None else httpx.AsyncClient()

    async def search(
        self, seed_url: str, max_results: int | None = None
    ) -> ProviderResult:
        started = time.monotonic()

        def _elapsed_ms() -> int:
            return int(1000 * (time.monotonic() - started))

        def _result(
            status: ProviderStatus,
            *,
            matches: list[ProviderMatch] | None = None,
            raw: dict[str, Any],
            http_status: int | None,
        ) -> ProviderResult:
            return ProviderResult(
                provider_id=self.id,
                status=status,
                matches=matches or [],
                raw_response=raw,
                http_status=http_status,
                latency_ms=_elapsed_ms(),
            )

        response: httpx.Response | None = None
        for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
            try:
                response = await self._client.post(
                    self._endpoint,
                    headers={"authorization": f"token {self._api_key}"},
                    data={"url": seed_url},
                    timeout=self._timeout,
                )
            except httpx.TimeoutException as exc:
                return _result("timeout", raw={"exception": str(exc)}, http_status=None)
            except httpx.HTTPError as exc:
                return _result("error", raw={"exception": str(exc)}, http_status=None)

            if response.status_code != 429:
                break
            if attempt < _MAX_RATE_LIMIT_RETRIES:
                await asyncio.sleep(_retry_after_seconds(response))

        assert response is not None
        raw = _decode_body(response)

        if response.status_code == 429:
            return _result("rate_limited", raw=raw, http_status=429)
        if response.status_code != 200:
            return _result("error", raw=raw, http_status=response.status_code)

        parsed = _parse_output(raw)
        if parsed is None:
            # Wrong-project tripwire: a 200 without the Web Search matches
            # path must not read as "nothing found".
            return _result("error", raw=raw, http_status=200)

        matches, query_quality = parsed
        if max_results is not None:
            matches = matches[:max_results]
        return _result(
            "ok",
            matches=[_to_match(entry, query_quality) for entry in matches
                     if _raw_score(entry) is not None],
            raw=raw,
            http_status=200,
        )


def _retry_after_seconds(response: httpx.Response) -> float:
    header = response.headers.get("retry-after")
    try:
        wait = float(header) if header is not None else _RATE_LIMIT_DEFAULT_WAIT_SECONDS
    except ValueError:
        wait = _RATE_LIMIT_DEFAULT_WAIT_SECONDS
    return min(max(wait, 0.0), _RATE_LIMIT_WAIT_CAP_SECONDS)


def _parse_output(
    raw: dict[str, Any],
) -> tuple[list[dict[str, Any]], str | None] | None:
    """Extract (matches, query_quality) from status[0].response.output, or
    None when the Web Search shape isn't there at all."""
    try:
        output = raw["status"][0]["response"]["output"]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(output, dict) or not isinstance(output.get("matches"), list):
        return None

    quality: str | None = None
    for key in _QUERY_QUALITY_KEYS:
        if output.get(key) is not None:
            quality = str(output[key])
            break
    entries = [entry for entry in output["matches"] if isinstance(entry, dict)]
    return entries, quality


def _raw_score(entry: dict[str, Any]) -> Decimal | None:
    # "score" per the harness (2026-08-06); "similarity_score" per the old
    # lambda (js:1117-1129). Reading an alternate key is not rescaling — the
    # value stays raw. A match with neither is skipped: the numeric CHECK
    # constraint cannot store a score-less numeric row, and raw_response
    # keeps the entry.
    value = entry.get("score", entry.get("similarity_score"))
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _to_match(entry: dict[str, Any], query_quality: str | None) -> ProviderMatch:
    backlinks = entry.get("backlinks")
    page_url: str | None = None
    if isinstance(backlinks, list) and backlinks and isinstance(backlinks[0], dict):
        first = backlinks[0].get("url")
        page_url = str(first) if first is not None else None
    return ProviderMatch(
        image_url=str(entry.get("url", "")),
        page_url=page_url,
        provider_score=_raw_score(entry),
        provider_category=None,
        query_quality=query_quality,
    )
