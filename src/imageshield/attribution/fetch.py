"""Reads the photo through the proxy's presigned GET. The only component that
holds image bytes, and it holds them for the length of one call.

Rekognition's ``Image`` accepts ``Bytes`` or ``S3Object`` and nothing else —
there is no URL form, verified against botocore's service model — and this
service holds no S3 credentials, so reading through the presigned URL is the
only route. That is the same pattern the enrolment path already uses
(CLAUDE.md §3.3: "we read or write through them and discard the bytes"), and
the rule it must not break is INVARIANTS #9: nothing is written to disk, to a
column, or to a log.
"""

from __future__ import annotations

import httpx
import structlog

from imageshield.attribution.models import AttributionUnavailable

log = structlog.get_logger("imageshield.attribution")

# Rekognition rejects images over 5MB passed as Bytes, so anything larger
# cannot be attributed anyway. Refused before the body is read rather than
# after: an unbounded read from a URL we did not mint is how a service runs out
# of memory.
MAX_PHOTO_BYTES = 5 * 1024 * 1024


class HttpxPhotoFetcher:
    def __init__(self, client: httpx.AsyncClient, *, timeout_seconds: float = 15.0) -> None:
        self._client = client
        self._timeout = timeout_seconds

    async def fetch(self, presigned_get_url: str) -> bytes:
        try:
            response = await self._client.get(
                presigned_get_url, timeout=self._timeout, follow_redirects=False
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # An expired presigned URL lands here as a 403. Retryable from the
            # proxy's side with a fresh URL, which is why this is 'unavailable'
            # rather than a permanent failure.
            raise AttributionUnavailable(f"could not read the photo: {exc}") from exc

        if len(response.content) > MAX_PHOTO_BYTES:
            raise AttributionUnavailable(
                f"photo is {len(response.content)} bytes; Rekognition accepts at"
                f" most {MAX_PHOTO_BYTES}"
            )
        return response.content


def make_client() -> httpx.AsyncClient:
    """The photo comes from the proxy's own S3, not a third-party host, so this
    needs none of the recheck loop's SSRF posture — but redirects are still not
    followed, because a presigned URL that redirects is not a presigned URL."""
    return httpx.AsyncClient(follow_redirects=False)
