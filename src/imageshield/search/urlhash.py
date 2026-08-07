"""URL normalisation v1 — the dedup key (CLAUDE.md §8 step 6).

``url_hash = sha256(canonical_url)``. Changing any rule here invalidates
every stored hash, so the rules are versioned: ``NORMALISATION_VERSION`` is
stored on every ``content_urls`` row, and a future v2 must bump it rather
than silently splitting the dedup into two populations that never match.

v1 rules, in order:

 1. lowercase the scheme and host
 2. host to punycode (IDN normalisation)
 3. strip default ports (:80 on http, :443 on https)
 4. strip the fragment entirely
 5. resolve dot segments (/a/./b/../c -> /a/c)
 6. PRESERVE path case — paths are case-sensitive, hosts are not
 7. strip tracking params (utm_*, fbclid, gclid, msclkid, mc_eid, ref,
    ref_src, source, igshid, _ga, yclid)
 8. sort remaining query params by key
 9. normalise percent-encoding: uppercase hex, decode unreserved chars
10. strip a single trailing slash EXCEPT on a bare-root path

The scheme is deliberately **kept**: http and https are different origins and
collapsing them would merge two URLs a takedown notice treats separately.

Deliberate edge behaviour: an empty path becomes ``/`` (so ``example.com``
and ``example.com/`` collapse); userinfo (``user:pass@``) is dropped; and a
string that cannot be parsed as a URL at all canonicalises to itself
verbatim. The store must never crash on a garbage provider URL — such a URL
simply dedups exact-match only.

This module replaces the step-5 interim hash of the **raw** URL. The old repo
hashed unnormalised URLs permanently (one of the three
weeklyInfringementScanner defects); the interim version here was a declared
placeholder with one call site, which is what made this swap a rewrite of one
file instead of an archaeology project.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit

from imageshield.types import UrlHash, parse_url_hash

NORMALISATION_VERSION = "v1"

_TRACKING_EXACT = frozenset(
    {
        "fbclid",
        "gclid",
        "msclkid",
        "mc_eid",
        "ref",
        "ref_src",
        "source",
        "igshid",
        "_ga",
        "yclid",
    }
)
_TRACKING_PREFIX = "utm_"
_DEFAULT_PORTS = {"http": 80, "https": 443}
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
_PCT_RE = re.compile(r"%([0-9A-Fa-f]{2})")


def url_hash(url: str) -> UrlHash:
    return parse_url_hash(hashlib.sha256(canonicalise(url).encode("utf-8")).hexdigest())


def canonicalise(url: str) -> str:
    raw = url.strip()
    try:
        parts = urlsplit(raw)
        port = parts.port  # raises ValueError on a malformed port
    except ValueError:
        return raw
    if not parts.scheme or not parts.hostname:
        return raw

    scheme = parts.scheme.lower()
    host = _idna(parts.hostname)  # urlsplit already lowercased it
    netloc = host if port is None or port == _DEFAULT_PORTS.get(scheme) else f"{host}:{port}"
    path = _strip_trailing_slash(_resolve_dot_segments(_normalise_pct(parts.path)) or "/")
    query = _normalise_query(parts.query)
    return f"{scheme}://{netloc}{path}" + (f"?{query}" if query else "")


def source_domain(url: str) -> str:
    try:
        host = urlsplit(canonicalise(url)).hostname
    except ValueError:
        return "unknown"
    return host or "unknown"


def _idna(host: str) -> str:
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def _normalise_pct(value: str) -> str:
    """Uppercase percent-escape hex; decode escapes of unreserved characters.

    Reserved characters are never decoded — ``%2F`` must not become a path
    separator, which would make two different URLs hash alike.
    """

    def _one(match: re.Match[str]) -> str:
        char = chr(int(match.group(1), 16))
        return char if char in _UNRESERVED else f"%{match.group(1).upper()}"

    return _PCT_RE.sub(_one, value)


def _resolve_dot_segments(path: str) -> str:
    if not path:
        return ""
    segments = path.split("/")
    out: list[str] = []
    for seg in segments:
        if seg == ".":
            continue
        if seg == "..":
            if len(out) > 1:
                out.pop()
            continue
        out.append(seg)
    # A trailing "." or ".." names a directory, so the result keeps its
    # trailing slash (rule 10 usually strips it again; this is what makes
    # /a/b/.. resolve to /a rather than /ab).
    if segments[-1] in (".", "..") and (not out or out[-1] != ""):
        out.append("")
    return "/".join(out)


def _strip_trailing_slash(path: str) -> str:
    return path[:-1] if path.endswith("/") and len(path) > 1 else path


def _normalise_query(query: str) -> str:
    if not query:
        return ""
    kept: list[str] = []
    for param in query.split("&"):
        if not param:
            continue
        key = param.split("=", 1)[0].lower()
        if key.startswith(_TRACKING_PREFIX) or key in _TRACKING_EXACT:
            continue
        kept.append(_normalise_pct(param))
    # Sort by key, then by the whole param, so repeated keys are ordered
    # deterministically rather than by provider whim.
    kept.sort(key=lambda p: (p.split("=", 1)[0], p))
    return "&".join(kept)
