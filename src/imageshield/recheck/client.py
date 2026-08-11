"""The prober: HEAD only, guarded on every hop.

**HEAD, never GET.** We need liveness, not bytes. A GET would pull the
infringing image or page into this process, which is the one thing this repo
does not do (CLAUDE.md §1) — and it would do it for material we have already
classified as likely abuse. There is no code path here that can issue a GET;
the transport protocol exposes a single ``head`` method, so a future edit that
wanted one would have to change the interface.

Redirects are followed **by hand**, at most ``max_redirects`` times, with the
allowlist and SSRF guards re-applied to every hop. Letting the HTTP client
follow them internally would apply the guards to the first URL only, and
`https://allowed.example/x` → `302 http://169.254.169.254/` would then be
fetched — the classic bypass. See ``recheck/ssrf.py``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol
from urllib.parse import urljoin

import structlog

from imageshield.recheck.models import CheckResult, DueInfringement
from imageshield.recheck.policy import verdict_for_status
from imageshield.recheck.ssrf import Resolver, refusal_reason

log = structlog.get_logger("imageshield.recheck")

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class HeadResponse(Protocol):
    @property
    def status_code(self) -> int: ...

    @property
    def headers(self) -> dict[str, str]: ...


class HeadTransport(Protocol):
    """One method, on purpose. There is no ``get`` to call by accident."""

    async def head(self, url: str, *, timeout_seconds: float) -> HeadResponse: ...


class TransportError(Exception):
    """A timeout, a DNS failure at connect time, a reset connection.

    Not evidence of removal — the caller maps it to ``unchanged``.
    """


class UrlChecker:
    def __init__(
        self,
        transport: HeadTransport,
        *,
        timeout_seconds: float,
        max_redirects: int,
        resolver: Resolver | None = None,
    ) -> None:
        self._transport = transport
        self._timeout = timeout_seconds
        self._max_redirects = max_redirects
        self._resolver = resolver

    async def check(
        self, item: DueInfringement, allowed_domains: Iterable[str]
    ) -> CheckResult:
        allowed = set(allowed_domains)
        url = item.page_url
        for hop in range(self._max_redirects + 1):
            refusal = refusal_reason(url, allowed, self._resolver)
            if refusal is not None:
                log.warning(
                    "recheck.refused",
                    infringement_id=str(item.infringement_id),
                    reason=refusal,
                    hop=hop,
                )
                return CheckResult(
                    infringement_id=item.infringement_id,
                    verdict="unchanged",
                    refused=refusal,
                    redirects=hop,
                )
            try:
                response = await self._transport.head(url, timeout_seconds=self._timeout)
            except TransportError as exc:
                # Could not reach the host. That is a statement about us, not
                # about whether the page is still there.
                log.info(
                    "recheck.unreachable",
                    infringement_id=str(item.infringement_id),
                    detail=str(exc),
                )
                return CheckResult(
                    infringement_id=item.infringement_id,
                    verdict="unchanged",
                    redirects=hop,
                )

            if response.status_code not in _REDIRECT_STATUSES:
                return CheckResult(
                    infringement_id=item.infringement_id,
                    verdict=verdict_for_status(response.status_code),
                    status_code=response.status_code,
                    redirects=hop,
                )
            location = response.headers.get("location") or response.headers.get("Location")
            if not location:
                # A redirect with nowhere to go tells us nothing.
                return CheckResult(
                    infringement_id=item.infringement_id,
                    verdict="unchanged",
                    status_code=response.status_code,
                    redirects=hop,
                )
            url = urljoin(url, location)

        # Out of hops. A redirect chain longer than the cap is not evidence of
        # removal either — leave the row alone.
        log.info(
            "recheck.redirect_limit",
            infringement_id=str(item.infringement_id),
            limit=self._max_redirects,
        )
        return CheckResult(
            infringement_id=item.infringement_id,
            verdict="unchanged",
            redirects=self._max_redirects,
        )
