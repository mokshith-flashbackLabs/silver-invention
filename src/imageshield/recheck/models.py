"""Domain types for the recheck loop."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from imageshield.recheck.policy import Verdict


@dataclass(frozen=True, slots=True)
class DueInfringement:
    """One row the recheck loop should probe.

    ``source_domain`` rides along from ``content_urls`` so the per-domain pacer
    and the allowlist need no second query per URL.
    """

    infringement_id: UUID
    page_url: str
    source_domain: str


@dataclass(frozen=True, slots=True)
class CheckResult:
    """What one probe concluded.

    ``refused`` carries the SSRF/allowlist reason when the request never
    happened. It is deliberately distinct from a ``unchanged`` verdict reached
    by, say, a timeout: both leave the row alone, but only one of them means we
    declined to look. Silence about a refusal would let a mis-recorded domain
    stop being checked forever with nothing in the logs.
    """

    infringement_id: UUID
    verdict: Verdict
    status_code: int | None = None
    refused: str | None = None
    redirects: int = 0
