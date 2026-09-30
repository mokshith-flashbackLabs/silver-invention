"""robots.txt for likeness intel's source validation (spec §4.10; §4.2 amended 2026-09-30).

RFC 9309, the parts a validator needs:
- **Groups.** One or more ``user-agent`` lines, then rules. Every group naming our product token
  (case-insensitive) is obeyed, combined; failing that, the ``*`` group; failing that, nothing.
- **Precedence.** The longest matching rule wins, and ``allow`` wins a tie. ``*`` matches any run
  of characters and a trailing ``$`` anchors the end. The stdlib parser takes the FIRST matching
  rule and has no wildcards: it lets ``Allow: /`` above ``Disallow: /private`` open ``/private/x``
  and never matches ``Disallow: /*.pdf$``. That is why this module exists.
- **Availability** (``fetch.fetch_robots``): a 2xx gives the rules; a 4xx other than 429, or too
  many redirects, means "unavailable", so everything is allowed; a 5xx, a 429, a timeout or a
  transport error means "unreachable", so nothing is, and nothing is cached.
- ``/robots.txt`` itself is always allowed. Percent-encoding is compared as written.

A definite answer is cached per origin for ``ROBOTS_CACHE_SECONDS`` (24 hours), bounded at
``ROBOTS_CACHE_MAX_ORIGINS``, in this process only: the fetcher holds no database. Robots is a
floor, not a permission, and the operator's ``terms_note`` is still required (§3.2).
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from imageshield.fetcher.fetch import FetchRefused, fetch_robots
from imageshield.recheck.ssrf import Resolver

PRODUCT_TOKEN = "ImageShield-Fetcher"
USER_AGENT = f"{PRODUCT_TOKEN}/1.0"
ROBOTS_CACHE_SECONDS = 24 * 60 * 60
ROBOTS_CACHE_MAX_ORIGINS = 1024


def _matches(pattern: str, path: str) -> bool:
    """RFC 9309 §2.2.3. Each ``*``-separated segment is found leftmost, and an anchored last
    segment is checked against the end. No regular expression, so a hostile pattern cannot make
    matching slow."""
    anchored = pattern.endswith("$")
    parts = (pattern[:-1] if anchored else pattern).split("*")
    if not path.startswith(parts[0]):
        return False
    position = len(parts[0])
    if len(parts) == 1:
        return position == len(path) if anchored else True
    for middle in parts[1:-1]:
        found = path.find(middle, position)
        if found < 0:
            return False
        position = found + len(middle)
    last = parts[-1]
    if anchored:
        return len(path) - len(last) >= position and path.endswith(last)
    return path.find(last, position) >= 0


@dataclass(frozen=True)
class RobotsRules:
    """The rules one robots.txt gives our product token, as ``(allow, pattern)`` pairs."""

    rules: tuple[tuple[bool, str], ...] = ()

    def allows(self, path: str) -> bool:
        if path == "/robots.txt":
            return True
        best: tuple[int, bool] | None = None
        for allow, pattern in self.rules:
            if _matches(pattern, path):
                candidate = (len(pattern.encode()), allow)  # longer wins; allow wins a tie
                if best is None or candidate > best:
                    best = candidate
        return True if best is None else best[1]


def parse_robots(text: str | None, product_token: str = PRODUCT_TOKEN) -> RobotsRules:
    """``None`` is an unavailable robots.txt: no rules, so everything is allowed."""
    if text is None:
        return RobotsRules()
    groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
    agents: list[str] = []
    rules: list[tuple[bool, str]] = []
    reading_agents = False
    for raw in text.splitlines():
        field, separator, value = raw.split("#", 1)[0].partition(":")
        if not separator:
            continue
        field, value = field.strip().lower(), value.strip()
        if field == "user-agent":
            if not reading_agents and (agents or rules):
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
            reading_agents = True
        elif field in ("allow", "disallow"):
            reading_agents = False
            # An empty value is no rule, and a rule before any group binds nobody.
            if agents and value:
                rules.append((field == "allow", value))
    if agents or rules:
        groups.append((agents, rules))
    token = product_token.lower()
    wanted = token if any(token in group_agents for group_agents, _ in groups) else "*"
    return RobotsRules(
        tuple(
            rule
            for group_agents, group_rules in groups
            if wanted in group_agents
            for rule in group_rules
        )
    )


def origin_of(url: str) -> str:
    """``https://host[:port]``, lowercased, the default port dropped: what one robots.txt covers."""
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError as exc:
        raise FetchRefused("unfetchable", "malformed port") from exc
    host = (parts.hostname or "").lower()
    if ":" in host:
        # An IPv6 literal: urlsplit strips the brackets, and the URL is invalid without them.
        host = f"[{host}]"
    return f"https://{host}" + (f":{port}" if port not in (None, 443) else "")


def path_of(url: str) -> str:
    """What a rule is matched against: the path (``/`` when empty) and the query."""
    parts = urlsplit(url)
    path = parts.path or "/"
    return f"{path}?{parts.query}" if parts.query else path


class RobotsCache:
    """Definite answers per origin, for ``ttl_seconds``, the least recently used evicted first."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl_seconds: float = ROBOTS_CACHE_SECONDS,
        max_origins: int = ROBOTS_CACHE_MAX_ORIGINS,
    ) -> None:
        self._clock = clock
        self._ttl = ttl_seconds
        self._max = max_origins
        self._entries: OrderedDict[str, tuple[float, RobotsRules]] = OrderedDict()

    def get(self, origin: str) -> RobotsRules | None:
        entry = self._entries.get(origin)
        if entry is None:
            return None
        stored_at, rules = entry
        if self._clock() - stored_at >= self._ttl:
            del self._entries[origin]
            return None
        self._entries.move_to_end(origin)
        return rules

    def put(self, origin: str, rules: RobotsRules) -> None:
        self._entries[origin] = (self._clock(), rules)
        self._entries.move_to_end(origin)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)


async def robots_allows(
    client: httpx.AsyncClient,
    url: str,
    *,
    cache: RobotsCache,
    timeout_seconds: float,
    max_redirects: int,
    resolver: Resolver | None = None,
) -> bool:
    """Whether our product token may fetch ``url``. Raises ``FetchRefused``: ``robots_unreachable``
    (nothing cached), or ``refused_private_address`` exactly as the page itself would be."""
    origin = origin_of(url)
    rules = cache.get(origin)
    if rules is None:
        robots = await fetch_robots(
            client,
            origin,
            timeout_seconds=timeout_seconds,
            max_redirects=max_redirects,
            resolver=resolver,
        )
        rules = parse_robots(robots.text)
        cache.put(origin, rules)
    return rules.allows(path_of(url))
