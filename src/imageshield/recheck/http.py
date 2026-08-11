"""The real HEAD transport, over httpx.

Thin on purpose. Everything interesting — the guards, the redirect walk, the
status mapping — lives in ``client.py``/``ssrf.py``/``policy.py`` and is tested
without a socket. This adapts httpx to the one-method ``HeadTransport``
protocol and translates its exceptions.

``follow_redirects=False`` is load-bearing, not a default. The caller walks
redirects by hand so the allowlist and SSRF guards apply to every hop; letting
httpx follow them would check only the first URL.
"""

from __future__ import annotations

import httpx

from imageshield.recheck.client import HeadResponse, TransportError


class HttpxHeadTransport:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def head(self, url: str, *, timeout_seconds: float) -> HeadResponse:
        try:
            response = await self._client.head(
                url, timeout=timeout_seconds, follow_redirects=False
            )
        except httpx.HTTPError as exc:
            # Timeout, connection reset, DNS failure at connect time. None of
            # them are evidence that the page was removed.
            raise TransportError(str(exc)) from exc
        return _Response(response)


class _Response:
    """Adapts httpx.Response to the HeadResponse protocol. No body is read —
    a HEAD has none, and nothing here would look at it if it did."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    @property
    def status_code(self) -> int:
        return self._response.status_code

    @property
    def headers(self) -> dict[str, str]:
        return dict(self._response.headers)


def make_client(*, max_redirects: int) -> httpx.AsyncClient:
    """An httpx client for probing hostile hosts.

    No cookies persist across requests, redirects are never followed
    automatically, and the connection pool is small — this loop is weekly and
    paced per domain, so throughput is not the constraint.
    """
    return httpx.AsyncClient(
        follow_redirects=False,
        max_redirects=max_redirects,
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=2),
        headers={"User-Agent": "ImageShield-Recheck/1.0"},
    )
