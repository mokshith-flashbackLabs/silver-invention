"""The services API's client for the fetcher's ``POST /v1/crop``.

The bbox travels services → fetcher and no further — the app receives a crop
URL, never crop data or a bbox it applies itself (INVARIANTS #13). The token is
attached per request and never logged or echoed.

Fetcher refusals split into exactly two caller-visible kinds:

- ``unrenderable`` — the fetcher looked and the image cannot yield a crop
  (``crop_too_small``, ``not_an_image``). Retrying will not help; the route
  maps it to the same ``preview_unavailable`` a missing bbox gets.
- everything else — SSRF refusals, timeouts, size caps, 5xx: upstream trouble,
  retryable, mapped to ``preview_unavailable_upstream``.
"""

from __future__ import annotations

import json

import httpx

# Fetcher error codes that mean "this image cannot yield a crop" rather than
# "the fetch went wrong" (fetcher/app.py: crop_too_small, not_an_image).
_UNRENDERABLE_CODES = frozenset({"crop_too_small", "not_an_image"})


class CropUnavailable(Exception):
    def __init__(self, detail: str, *, unrenderable: bool) -> None:
        super().__init__(detail)
        self.unrenderable = unrenderable


class FetcherCropClient:
    def __init__(self, client: httpx.AsyncClient, *, base_url: str, token: str) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._token = token

    async def crop(self, *, url: str, bbox: dict[str, float], blur: bool) -> bytes:
        try:
            response = await self._client.post(
                f"{self._base_url}/v1/crop",
                json={"url": url, "bbox": bbox, "blur": blur},
                headers={"X-Fetcher-Token": self._token},
            )
        except httpx.HTTPError as exc:
            raise CropUnavailable(
                f"fetcher unreachable: {type(exc).__name__}", unrenderable=False
            ) from exc
        if response.status_code == 200:
            return response.content
        raise CropUnavailable(
            f"fetcher answered {response.status_code}",
            unrenderable=_error_code(response) in _UNRENDERABLE_CODES,
        )


def _error_code(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError):
        return None
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else None


__all__ = ["CropUnavailable", "FetcherCropClient"]
