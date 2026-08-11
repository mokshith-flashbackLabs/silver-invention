"""Egress guards for a fetcher that points at hostile domains.

The recheck worker sends requests to URLs supplied by third-party search
providers, describing pages hosted by people with an interest in this system
misbehaving. INVARIANTS #11's posture for the crop fetcher applies here for the
same reasons, and the two guards are deliberately independent:

1. **Domain allowlist**, sourced from ``content_urls.source_domain`` — we only
   ever probe hosts we already recorded a URL for. A page_url mutated in the
   database, or a redirect to somewhere new, has no entry and is refused.
2. **SSRF guard AFTER DNS resolution, never before.** Checking the *hostname*
   proves nothing: ``evil.example`` can resolve to ``169.254.169.254``. Every
   address the name resolves to must be globally routable, or the request does
   not happen.

Both are applied to **every redirect hop**, not just the first. A guard applied
only to the original URL is not a guard: `https://allowed.example/x` returning
`302 -> http://169.254.169.254/latest/meta-data/` would sail straight through.
That is why the client follows redirects by hand rather than letting httpx do
it.

**Known limitation, stated rather than hidden:** resolving here and then
letting the HTTP client resolve again leaves a DNS-rebinding window. Closing it
properly means pinning the checked address into the connection, which httpx
does not expose cleanly. The allowlist is the second line for exactly this
reason, and the deployment posture is the third — this worker runs on its own
egress path with no VPC access to any internal service, so a successful rebind
reaches nothing worth reaching (INVARIANTS #11).
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable
from urllib.parse import urlsplit

# Why a request was refused. These are recorded, never silent: a refusal is not
# the same as a check that found the page alive, and treating it as one would
# quietly stop rechecking a whole domain.
RefusalReason = str


def host_of(url: str) -> str | None:
    """The hostname, lowercased, or ``None`` if the URL has no usable host."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"}:
        return None
    host = parts.hostname
    return host.lower() if host else None


def refusal_reason(
    url: str, allowed_domains: Iterable[str], resolver: Resolver | None = None
) -> RefusalReason | None:
    """``None`` if this URL may be fetched; otherwise why it may not.

    Order matters only for cost: the allowlist is a set membership test and the
    resolution is a network round trip, so the cheap absolute check runs first.
    Both must pass.
    """
    host = host_of(url)
    if host is None:
        return "not_an_http_url"
    if host not in set(allowed_domains):
        # Never seen this domain in content_urls. We have no business probing it
        # — it is not a host any provider told us about.
        return "domain_not_allowlisted"

    resolve = resolver if resolver is not None else _resolve
    try:
        addresses = resolve(host)
    except OSError:
        return "dns_failure"
    if not addresses:
        return "dns_empty"
    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            return "unparseable_address"
        # is_global is False for private, loopback, link-local (169.254/16 and
        # the IPv6 equivalent), multicast, reserved and unspecified ranges.
        # Every address must pass: a name resolving to one public and one
        # private address is a rebinding attempt, not a lucky configuration.
        if not parsed.is_global:
            return "private_address"
    return None


def _resolve(host: str) -> list[str]:
    """Every address this name resolves to, v4 and v6.

    ``getaddrinfo``'s sockaddr is a 2-tuple for v4 and a 4-tuple for v6; the
    address is element 0 of both, and is always a string. The cast is for mypy,
    which types the sockaddr union widely.
    """
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [str(info[4][0]) for info in infos]


class Resolver:
    """Structural type for :func:`_resolve` — a callable taking a host and
    returning addresses. Injected in tests so a name can be made to resolve to
    ``169.254.169.254`` without touching real DNS."""

    def __call__(self, host: str) -> list[str]:  # pragma: no cover - protocol
        raise NotImplementedError
