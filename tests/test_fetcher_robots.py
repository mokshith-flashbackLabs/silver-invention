"""robots.txt for source validation (spec §4.10, §4.2 amended 2026-09-30): the RFC 9309 matcher,
the per-origin cache, the fetcher route's ``respect_robots``, and the worker's client flag."""

from __future__ import annotations

import codecs
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from imageshield.fetcher.app import create_app
from imageshield.fetcher.config import FetcherConfig
from imageshield.fetcher.fetch import ROBOTS_MAX_BYTES
from imageshield.fetcher.robots import (
    PRODUCT_TOKEN,
    USER_AGENT,
    RobotsCache,
    RobotsRules,
    origin_of,
    parse_robots,
)
from imageshield.intel.fetch_client import FetchFailure, HttpTextFetcher, TextFetch
from tests.intel_fakes import FakeFetcher, make_page

TOKEN = "fetcher-token-for-tests-0003"
AUTH = {"X-Fetcher-Token": TOKEN}
PAGE = b"<html><body><p>Our terms say what we do with the photos you upload.</p></body></html>"
Route = Callable[[], httpx.Response]


def _robots(text: str) -> Route:
    return lambda: httpx.Response(
        200, content=text.encode(), headers={"content-type": "text/plain"}
    )


def _page() -> Route:
    return lambda: httpx.Response(200, content=PAGE, headers={"content-type": "text/html"})


def _status(code: int) -> Route:
    return lambda: httpx.Response(code)


def _redirect(location: str) -> Route:
    return lambda: httpx.Response(302, headers={"location": location})


def _client(
    routes: dict[str, Route], *, seen: list[str] | None = None, resolver: Any = None
) -> TestClient:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if seen is not None:
            seen.append(url)
        return routes.get(url, _status(404))()

    app = create_app(config=FetcherConfig(fetcher_token=TOKEN))
    app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.resolver = resolver or (lambda host: ("93.184.216.34",))
    return TestClient(app)


def _text(client: TestClient, url: str, **extra: Any) -> httpx.Response:
    return client.post("/v1/text", json={"url": url, **extra}, headers=AUTH)


# ── the matcher (pure) ────────────────────────────────────────────────────────


def test_the_longest_match_wins_not_the_first() -> None:
    """Review Focus 3: the stdlib's first-match rule would open /private/x here."""
    rules = parse_robots("User-agent: *\nAllow: /\nDisallow: /private\n")
    assert not rules.allows("/private/x")
    assert rules.allows("/public") and rules.allows("/")


def test_allow_wins_a_tie() -> None:
    assert parse_robots("User-agent: *\nDisallow: /a\nAllow: /a\n").allows("/a/b")


def test_wildcards_and_the_end_anchor() -> None:
    rules = parse_robots("User-agent: *\nDisallow: /*.pdf$\nDisallow: /*?\n")
    assert not rules.allows("/files/terms.pdf")
    assert rules.allows("/files/terms.pdf.html")
    assert not rules.allows("/search?q=photos")
    assert rules.allows("/search")


def test_our_group_beats_the_star_group_and_the_token_ignores_case() -> None:
    ours = "User-agent: *\nDisallow: /\n\nUser-agent: imageshield-fetcher\nAllow: /\n"
    assert parse_robots(ours).allows("/terms")
    other = "User-agent: *\nDisallow: /\n\nUser-agent: SomeBot\nAllow: /\n"
    assert not parse_robots(other).allows("/terms")


def test_every_group_naming_us_is_combined_and_agents_share_a_group() -> None:
    text = (
        f"User-agent: {PRODUCT_TOKEN}\nDisallow: /a\n\n"
        f"User-agent: OtherBot\nUser-agent: {PRODUCT_TOKEN}\nDisallow: /b\n"
    )
    rules = parse_robots(text)
    assert not rules.allows("/a/1") and not rules.allows("/b/1") and rules.allows("/c")


def test_no_file_no_rule_and_robots_itself_are_all_allowed() -> None:
    assert parse_robots(None).allows("/anything")
    assert parse_robots("User-agent: *\nDisallow:\n").allows("/anything")  # empty: no rule
    assert parse_robots("User-agent: *\nDisallow: /\n").allows("/robots.txt")
    assert parse_robots("Disallow: /\n").allows("/x")  # a rule before any group binds nobody


def test_the_user_agent_header_carries_the_product_token() -> None:
    assert USER_AGENT.split("/")[0] == PRODUCT_TOKEN


def test_the_cache_expires_after_a_day_and_evicts_the_oldest_origin() -> None:
    now = [0.0]
    cache = RobotsCache(clock=lambda: now[0], max_origins=2)
    cache.put("https://a.example", RobotsRules())
    now[0] = 24 * 60 * 60 - 1
    assert cache.get("https://a.example") is not None
    now[0] = 24 * 60 * 60 + 1
    assert cache.get("https://a.example") is None
    for origin in ("https://a.example", "https://b.example", "https://c.example"):
        cache.put(origin, RobotsRules())
    assert cache.get("https://a.example") is None and cache.get("https://c.example") is not None


# ── the fetcher route ─────────────────────────────────────────────────────────


def test_a_disallowed_path_is_refused_before_the_page_is_fetched() -> None:
    seen: list[str] = []
    client = _client(
        {
            "https://p.example/robots.txt": _robots("User-agent: *\nDisallow: /private\n"),
            "https://p.example/private/a": _page(),
        },
        seen=seen,
    )
    r = _text(client, "https://p.example/private/a", respect_robots=True)
    assert r.status_code == 403 and r.json()["error"]["code"] == "robots_disallowed"
    assert seen == ["https://p.example/robots.txt"]


def test_an_allowed_path_is_fetched_and_robots_is_read_once_per_origin() -> None:
    seen: list[str] = []
    client = _client(
        {
            "https://p.example/robots.txt": _robots("User-agent: *\nDisallow: /private\n"),
            "https://p.example/terms": _page(),
            "https://p.example/privacy": _page(),
        },
        seen=seen,
    )
    assert _text(client, "https://p.example/terms", respect_robots=True).status_code == 200
    assert _text(client, "https://p.example/privacy", respect_robots=True).status_code == 200
    assert seen.count("https://p.example/robots.txt") == 1


def test_a_missing_robots_file_allows_everything() -> None:
    client = _client(
        {"https://p.example/robots.txt": _status(404), "https://p.example/terms": _page()}
    )
    assert _text(client, "https://p.example/terms", respect_robots=True).status_code == 200


def test_a_5xx_robots_file_is_unreachable_and_never_cached() -> None:
    """Review Focus 3: RFC 9309 treats an unreachable robots.txt as a complete disallow. A
    transient 503 must not block the origin for a day, so it is not cached."""
    seen: list[str] = []
    routes = {"https://p.example/robots.txt": _status(503), "https://p.example/terms": _page()}
    client = _client(routes, seen=seen)
    r = _text(client, "https://p.example/terms", respect_robots=True)
    assert r.status_code == 502 and r.json()["error"]["code"] == "robots_unreachable"
    routes["https://p.example/robots.txt"] = _robots("User-agent: *\nAllow: /\n")
    assert _text(client, "https://p.example/terms", respect_robots=True).status_code == 200
    assert seen.count("https://p.example/robots.txt") == 2


def test_robots_is_read_only_when_asked() -> None:
    seen: list[str] = []
    client = _client({"https://p.example/private/a": _page()}, seen=seen)
    assert _text(client, "https://p.example/private/a").status_code == 200
    assert seen == ["https://p.example/private/a"]


def test_a_redirect_to_another_origin_is_checked_there_too() -> None:
    client = _client(
        {
            "https://a.example/robots.txt": _robots("User-agent: *\nAllow: /\n"),
            "https://a.example/x": _redirect("https://b.example/y"),
            "https://b.example/robots.txt": _robots("User-agent: *\nDisallow: /y\n"),
            "https://b.example/y": _page(),
        }
    )
    r = _text(client, "https://a.example/x", respect_robots=True)
    assert r.status_code == 403 and r.json()["error"]["code"] == "robots_disallowed"


def test_a_robots_host_on_a_private_address_is_refused_like_the_page() -> None:
    client = _client({}, resolver=lambda host: ("10.0.0.5",))
    r = _text(client, "https://p.example/terms", respect_robots=True)
    assert r.status_code == 400 and r.json()["error"]["code"] == "refused_private_address"


# ── the worker's client and fake ──────────────────────────────────────────────


async def test_the_intel_client_asks_for_robots_only_when_told() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        body = {
            "text": "t",
            "content_type": "text/plain",
            "final_url": "https://p.example/a",
            "truncated": False,
            "items": None,
        }
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = HttpTextFetcher(client, "http://fetcher.local", "tok")
        assert isinstance(await fetcher.fetch_text("https://p.example/a"), TextFetch)
        await fetcher.fetch_text("https://p.example/a", respect_robots=True)
    assert bodies == [
        {"url": "https://p.example/a"},
        {"url": "https://p.example/a", "respect_robots": True},
    ]


@pytest.mark.parametrize(
    ("status", "code"), [(403, "robots_disallowed"), (502, "robots_unreachable")]
)
async def test_the_intel_client_relays_the_robots_verdicts(status: int, code: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": code, "message": "m"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        got = await HttpTextFetcher(client, "http://fetcher.local", "tok").fetch_text(
            "https://p.example/a", respect_robots=True
        )
    assert got == FetchFailure(code=code)


async def test_the_fake_fetcher_answers_robots_like_the_fetcher() -> None:
    url = "https://p.example/a"
    fake = FakeFetcher({url: make_page("text", url)}, robots_disallowed={url})
    assert await fake.fetch_text(url, respect_robots=True) == FetchFailure(code="robots_disallowed")
    assert isinstance(await fake.fetch_text(url), TextFetch)
    assert fake.robots_checked == [url]


# ── beyond the plan's list: two real-world inputs, and the rest of spec §4.2's availability rules ─


def test_a_byte_order_mark_does_not_hide_the_first_line() -> None:
    """Windows editors save UTF-8 with a BOM. Left on the first line it turns ``User-agent`` into
    an unknown field, the group loses its agent, and a blanket ``Disallow: /`` binds nobody."""
    blanket = codecs.BOM_UTF8 + b"User-agent: *\nDisallow: /\n"
    client = _client(
        {
            "https://p.example/robots.txt": lambda: httpx.Response(
                200, content=blanket, headers={"content-type": "text/plain"}
            ),
            "https://p.example/terms": _page(),
        }
    )
    r = _text(client, "https://p.example/terms", respect_robots=True)
    assert r.status_code == 403 and r.json()["error"]["code"] == "robots_disallowed"


def test_an_ipv6_literal_origin_keeps_its_brackets() -> None:
    """Without them the robots URL is invalid and the route answered 500 (httpx.InvalidURL is not
    an HTTPError), where the same URL fetched with no robots check works."""
    assert origin_of("https://[2001:db8::1]:8443/terms") == "https://[2001:db8::1]:8443"
    seen: list[str] = []
    client = _client(
        {
            "https://[2001:db8::1]/robots.txt": _robots("User-agent: *\nAllow: /\n"),
            "https://[2001:db8::1]/terms": _page(),
        },
        seen=seen,
    )
    assert _text(client, "https://[2001:db8::1]/terms", respect_robots=True).status_code == 200
    assert seen[0] == "https://[2001:db8::1]/robots.txt"


@pytest.mark.parametrize(
    ("robots_status", "verdict", "code"),
    [
        (401, 200, None),  # every 4xx but 429 is "unavailable": nothing is restricted
        (403, 200, None),
        (410, 200, None),
        (429, 502, "robots_unreachable"),  # a 429 is "unreachable", like a 5xx
        (500, 502, "robots_unreachable"),
    ],
)
def test_the_robots_status_decides_between_unavailable_and_unreachable(
    robots_status: int, verdict: int, code: str | None
) -> None:
    client = _client(
        {
            "https://p.example/robots.txt": _status(robots_status),
            "https://p.example/terms": _page(),
        }
    )
    r = _text(client, "https://p.example/terms", respect_robots=True)
    assert r.status_code == verdict
    if code is not None:
        assert r.json()["error"]["code"] == code


def test_a_robots_timeout_is_unreachable() -> None:
    def timed_out() -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    client = _client(
        {"https://p.example/robots.txt": timed_out, "https://p.example/terms": _page()}
    )
    r = _text(client, "https://p.example/terms", respect_robots=True)
    assert r.status_code == 502 and r.json()["error"]["code"] == "robots_unreachable"


def test_a_robots_redirect_chain_past_the_cap_means_no_rules() -> None:
    """The default cap is two redirects; a third is 'unavailable', so everything is allowed."""
    client = _client(
        {
            "https://p.example/robots.txt": _redirect("https://p.example/r1"),
            "https://p.example/r1": _redirect("https://p.example/r2"),
            "https://p.example/r2": _redirect("https://p.example/r3"),
            "https://p.example/terms": _page(),
        }
    )
    assert _text(client, "https://p.example/terms", respect_robots=True).status_code == 200


def test_a_robots_file_reached_through_a_redirect_is_obeyed() -> None:
    client = _client(
        {
            "https://p.example/robots.txt": _redirect("https://www.p.example/robots.txt"),
            "https://www.p.example/robots.txt": _robots("User-agent: *\nDisallow: /terms\n"),
            "https://p.example/terms": _page(),
        }
    )
    r = _text(client, "https://p.example/terms", respect_robots=True)
    assert r.status_code == 403 and r.json()["error"]["code"] == "robots_disallowed"


def test_a_robots_redirect_to_plain_http_is_unreachable_not_allowed() -> None:
    """https is required on every hop, robots.txt's included; the refusal fails closed."""
    client = _client(
        {
            "https://p.example/robots.txt": _redirect("http://p.example/robots.txt"),
            "https://p.example/terms": _page(),
        }
    )
    r = _text(client, "https://p.example/terms", respect_robots=True)
    assert r.status_code == 502 and r.json()["error"]["code"] == "robots_unreachable"


def test_a_robots_file_past_the_cap_is_read_from_its_head() -> None:
    """Truncated, not refused: a rule inside the first 512 KiB binds, one past it does not."""
    filler = "# " + "x" * 1022 + "\n"
    text = "User-agent: *\nDisallow: /early\n" + filler * 600 + "Disallow: /late\n"
    assert len(text.encode()) > ROBOTS_MAX_BYTES
    client = _client(
        {
            "https://p.example/robots.txt": _robots(text),
            "https://p.example/early": _page(),
            "https://p.example/late": _page(),
        }
    )
    early = _text(client, "https://p.example/early", respect_robots=True)
    assert early.status_code == 403 and early.json()["error"]["code"] == "robots_disallowed"
    assert _text(client, "https://p.example/late", respect_robots=True).status_code == 200


def test_a_redirect_to_another_origin_that_allows_us_is_returned() -> None:
    client = _client(
        {
            "https://a.example/robots.txt": _robots("User-agent: *\nAllow: /\n"),
            "https://a.example/x": _redirect("https://b.example/y"),
            "https://b.example/robots.txt": _robots("User-agent: *\nDisallow: /other\n"),
            "https://b.example/y": _page(),
        }
    )
    r = _text(client, "https://a.example/x", respect_robots=True)
    assert r.status_code == 200 and r.json()["final_url"] == "https://b.example/y"


def test_a_same_origin_redirect_into_a_disallowed_path_is_refused() -> None:
    """``/terms`` -> ``/terms?lang=en`` is an ordinary locale redirect and ``Disallow: /*?`` an
    ordinary rule. Comparing origins alone let the page through; the landing URL is checked too,
    against the rules already read, so it costs no second request."""
    seen: list[str] = []
    client = _client(
        {
            "https://p.example/robots.txt": _robots("User-agent: *\nDisallow: /*?\n"),
            "https://p.example/terms": _redirect("https://p.example/terms?lang=en"),
            "https://p.example/terms?lang=en": _page(),
        },
        seen=seen,
    )
    r = _text(client, "https://p.example/terms", respect_robots=True)
    assert r.status_code == 403 and r.json()["error"]["code"] == "robots_disallowed"
    assert seen.count("https://p.example/robots.txt") == 1
