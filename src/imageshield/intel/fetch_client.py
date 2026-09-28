"""The intel worker's ONLY way to read a third-party page: the no-DB fetcher's
``POST /v1/text`` (INVARIANTS #11 as amended, spec §4.2). This process holds the
database pool, so it never opens a connection to a URL a feed, an operator or a
model named -- the SSRF guard, the byte cap and the https-only rule live in the
isolated fetcher, and this module only relays its verdict.

Two failure families, kept apart because they mean different things to a source:

- an UPSTREAM verdict (``UPSTREAM_CODES``) is the fetcher's judgement about the
  page -- unreachable, not https, not text. It counts against a source's
  ``consecutive_failures``;
- a FETCHER-SIDE failure (``FETCHER_SIDE_CODES``) is ours -- the fetcher is down,
  crashed, or refused our token. It says nothing about the source, so the run
  stops and nothing is charged to the source.

``unsupported_type`` is deliberately overloaded by the fetcher (a non-text type,
a feed carrying a DTD, an unparseable feed or JSON body): none of the three needs
a different remedy here.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx
import structlog
from pydantic import BaseModel, ConfigDict

log = structlog.get_logger("imageshield.intel")

UPSTREAM_CODES = frozenset(
    {
        "not_https",
        "refused_private_address",
        "redirect_limit",
        "unsupported_type",
        "unfetchable",
        "too_large",
    }
)
FETCHER_SIDE_CODES = frozenset({"fetcher_unreachable", "fetcher_error"})


class TextFetch(BaseModel):
    """``/v1/text``'s 200 body. ``items`` is non-null only for a parsed feed:
    ``[{title, link, published}]``, ``published`` an ISO string, a raw string, or
    null."""

    model_config = ConfigDict(frozen=True)

    text: str
    content_type: str
    final_url: str
    truncated: bool
    items: list[dict[str, Any]] | None


class FetchFailure(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str


class TextFetcher(Protocol):
    async def fetch_text(self, url: str) -> TextFetch | FetchFailure: ...


def _error_code(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else None


class HttpTextFetcher:
    """The fetcher over HTTP. Never raises for a failed fetch: every failure is a
    :class:`FetchFailure`, so one bad page cannot crash a run."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        token: str,
        *,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout_seconds

    async def fetch_text(self, url: str) -> TextFetch | FetchFailure:
        try:
            response = await self._client.post(
                f"{self._base_url}/v1/text",
                json={"url": url},
                headers={"X-Fetcher-Token": self._token},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            log.warning("intel.fetcher_unreachable", error=type(exc).__name__)
            return FetchFailure(code="fetcher_unreachable")
        if response.status_code == 200:
            try:
                # pydantic's ValidationError and json's JSONDecodeError are both
                # ValueErrors: a malformed 200 is the fetcher's fault, not the page's.
                return TextFetch.model_validate(response.json())
            except ValueError:
                log.warning("intel.fetcher_error", status=200, reason="malformed_body")
                return FetchFailure(code="fetcher_error")
        code = _error_code(response)
        if code is None or code not in UPSTREAM_CODES:
            # A 401 (token), a 422 (our request) or a crash: ours, never the source's.
            log.warning("intel.fetcher_error", status=response.status_code, code=code)
            return FetchFailure(code="fetcher_error")
        return FetchFailure(code=code)
