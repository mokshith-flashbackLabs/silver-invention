"""INTERIM url hashing — step 6 replaces this module.

Step 5 hashes the **raw** URL. Real normalisation (lowercase host, strip
www., drop protocol/fragment/tracking params, then hash) is step 6's whole
job, and dedup across providers is meaningless until it lands — the same
image found by Hive and Google usually differs in query params. This module
is the single call site to swap; nothing else in the codebase computes a
url_hash.

The old repo hashed unnormalised URLs *permanently* (one of the three
weeklyInfringementScanner defects) — the difference here is that this is a
declared placeholder with one owner, not a design.
"""

from __future__ import annotations

import hashlib
from urllib.parse import urlsplit

from imageshield.types import UrlHash, parse_url_hash


def interim_url_hash(url: str) -> UrlHash:
    return parse_url_hash(hashlib.sha256(url.encode("utf-8")).hexdigest())


def source_domain(url: str) -> str:
    try:
        return urlsplit(url).hostname or "unknown"
    except ValueError:
        return "unknown"
