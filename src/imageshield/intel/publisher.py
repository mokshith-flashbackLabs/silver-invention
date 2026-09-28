"""#50's publisher identity: the registrable domain (eTLD+1, ICANN section only)
of the fetcher's FINAL url. The bundled list is used; nothing is fetched at
runtime (the intel worker makes no third-party request, spec §2)."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from publicsuffixlist import PublicSuffixList

_PSL = PublicSuffixList(only_icann=True)


def publisher_domain(final_url: str) -> str:
    host = (urlsplit(final_url).hostname or "").rstrip(".").lower()
    if not host:
        return "unknown"
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    return _PSL.privatesuffix(host) or host
