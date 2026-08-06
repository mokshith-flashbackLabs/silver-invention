"""Presigned-URL uploads into the proxy's S3.

This service holds no S3 credentials and no S3 client (CLAUDE.md §3.3): a
presigned PUT is plain HTTPS, so httpx is all that's needed. Bytes are
streamed out and discarded — nothing is written to disk or the database.

Error messages never include the URL: the presigned query string carries the
signature, which is a credential and must not reach a log line.
"""

from __future__ import annotations

from typing import Protocol

import httpx

from imageshield.liveness.models import UploadError


class ObjectUploader(Protocol):
    async def put(self, url: str, data: bytes, *, content_type: str) -> None: ...


class HttpxObjectUploader:
    def __init__(self, *, timeout_seconds: float = 30.0) -> None:
        self._timeout = timeout_seconds

    async def put(self, url: str, data: bytes, *, content_type: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.put(
                    url, content=data, headers={"Content-Type": content_type}
                )
        except httpx.HTTPError as exc:
            raise UploadError(f"presigned PUT failed: {type(exc).__name__}") from exc
        if response.status_code // 100 != 2:
            raise UploadError(f"presigned PUT returned HTTP {response.status_code}")
