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
from imageshield.fetcher.fetch import ROBOTS_MAX_BYTES, FetchRefused, fetch_text
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
    against the rules already read, so it costs no second request, and before it is requested."""
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
    assert "https://p.example/terms?lang=en" not in seen


# ── every redirect hop is checked BEFORE it is requested (fix task 2b) ─────────────────────────
#
# The task-2 route checked the requested URL first and the landing URL after the walk, so a
# disallowed landing page had already been requested (its text was only thrown away) and a
# middle hop of a chain was never checked at all. The fetcher's guarded GET now takes an opt-in
# hook that is awaited with each redirect hop before that hop is requested. Each test below asserts
# on the fake transport's request log: what was NOT requested is the point.

ALLOW_ALL = "User-agent: *\nAllow: /\n"
DISALLOW_ALL = "User-agent: *\nDisallow: /\n"

A_ROBOTS = "https://a.example/robots.txt"
B_ROBOTS = "https://b.example/robots.txt"
C_ROBOTS = "https://c.example/robots.txt"


def _chain(*, b_robots: str = ALLOW_ALL, c_robots: str = ALLOW_ALL) -> dict[str, Route]:
    """a.example/x -> b.example/mid -> c.example/final: two redirects, which is what the default
    cap allows, across three origins that each have a robots.txt of their own."""
    return {
        A_ROBOTS: _robots(ALLOW_ALL),
        "https://a.example/x": _redirect("https://b.example/mid"),
        B_ROBOTS: _robots(b_robots),
        "https://b.example/mid": _redirect("https://c.example/final"),
        C_ROBOTS: _robots(c_robots),
        "https://c.example/final": _page(),
    }


def test_a_redirect_into_a_disallowed_path_never_requests_the_landing_url() -> None:
    """The landing origin's robots.txt is read, because that is how the refusal is known. The
    landing page itself is not."""
    seen: list[str] = []
    client = _client(
        {
            A_ROBOTS: _robots(ALLOW_ALL),
            "https://a.example/x": _redirect("https://b.example/y"),
            B_ROBOTS: _robots("User-agent: *\nDisallow: /y\n"),
            "https://b.example/y": _page(),
        },
        seen=seen,
    )
    r = _text(client, "https://a.example/x", respect_robots=True)
    assert r.status_code == 403 and r.json()["error"]["code"] == "robots_disallowed"
    assert seen == [A_ROBOTS, "https://a.example/x", B_ROBOTS]


def test_a_middle_hop_that_disallows_us_stops_the_chain_before_it_is_requested() -> None:
    """a -> b -> c. Looking only where the walk landed (c) never consulted b's origin or b's
    path. Now b's rules stop the walk at b: neither b's page nor anything at c is requested."""
    seen: list[str] = []
    client = _client(_chain(b_robots="User-agent: *\nDisallow: /mid\n"), seen=seen)
    r = _text(client, "https://a.example/x", respect_robots=True)
    assert r.status_code == 403 and r.json()["error"]["code"] == "robots_disallowed"
    assert seen == [A_ROBOTS, "https://a.example/x", B_ROBOTS]


def test_a_final_hop_that_disallows_us_is_never_requested_either() -> None:
    """The allowed middle hop is fetched; the disallowed final one is not."""
    seen: list[str] = []
    client = _client(_chain(c_robots="User-agent: *\nDisallow: /final\n"), seen=seen)
    r = _text(client, "https://a.example/x", respect_robots=True)
    assert r.status_code == 403 and r.json()["error"]["code"] == "robots_disallowed"
    assert seen == [A_ROBOTS, "https://a.example/x", B_ROBOTS, "https://b.example/mid", C_ROBOTS]


def test_an_allowed_redirect_chain_still_returns_the_text_with_each_hop_checked_first() -> None:
    seen: list[str] = []
    client = _client(_chain(), seen=seen)
    r = _text(client, "https://a.example/x", respect_robots=True)
    assert r.status_code == 200
    body = r.json()
    assert body["final_url"] == "https://c.example/final" and "Our terms" in body["text"]
    assert seen == [
        A_ROBOTS,
        "https://a.example/x",
        B_ROBOTS,
        "https://b.example/mid",
        C_ROBOTS,
        "https://c.example/final",
    ]


def test_without_respect_robots_a_redirect_chain_is_walked_exactly_as_before() -> None:
    """Every robots.txt here refuses us, so reading any of them would change the answer."""
    routes = _chain(b_robots=DISALLOW_ALL, c_robots=DISALLOW_ALL)
    routes[A_ROBOTS] = _robots(DISALLOW_ALL)
    for extra in ({}, {"respect_robots": False}):
        seen: list[str] = []
        client = _client(routes, seen=seen)
        r = _text(client, "https://a.example/x", **extra)
        assert r.status_code == 200 and r.json()["final_url"] == "https://c.example/final"
        assert seen == ["https://a.example/x", "https://b.example/mid", "https://c.example/final"]


def test_fetch_and_page_walk_redirects_exactly_as_before_and_never_read_robots() -> None:
    """``/v1/fetch`` and ``/v1/page`` are the confirm pipeline's hit-image and page paths. They pass
    no hop hook, so a chain is walked as it always was and no robots.txt is read for them, though
    every robots.txt here would refuse us."""
    image = b"not-really-a-png"
    routes: dict[str, Route] = {
        A_ROBOTS: _robots(DISALLOW_ALL),
        B_ROBOTS: _robots(DISALLOW_ALL),
        C_ROBOTS: _robots(DISALLOW_ALL),
        "https://a.example/img": _redirect("https://b.example/img"),
        "https://b.example/img": _redirect("https://c.example/img.png"),
        "https://c.example/img.png": lambda: httpx.Response(
            200, content=image, headers={"content-type": "image/png"}
        ),
        "https://a.example/p": _redirect("https://b.example/p"),
        "https://b.example/p": _redirect("https://c.example/p.html"),
        "https://c.example/p.html": _page(),
    }
    seen: list[str] = []
    client = _client(routes, seen=seen)
    fetched = client.post("/v1/fetch", json={"url": "https://a.example/img"}, headers=AUTH)
    assert fetched.status_code == 200 and fetched.content == image
    page = client.post("/v1/page", json={"url": "https://a.example/p"}, headers=AUTH)
    assert page.status_code == 200 and page.json()["html"] == PAGE.decode()
    assert seen == [
        "https://a.example/img",
        "https://b.example/img",
        "https://c.example/img.png",
        "https://a.example/p",
        "https://b.example/p",
        "https://c.example/p.html",
    ]


def test_a_hop_whose_robots_file_is_unreachable_is_not_requested_and_not_cached() -> None:
    """The rule the requested origin gets, applied to a hop: an unreachable robots.txt is a
    complete disallow, and a transient 503 must not block that origin for a day."""
    seen: list[str] = []
    routes = _chain()
    routes[B_ROBOTS] = _status(503)
    client = _client(routes, seen=seen)
    r = _text(client, "https://a.example/x", respect_robots=True)
    assert r.status_code == 502 and r.json()["error"]["code"] == "robots_unreachable"
    assert seen == [A_ROBOTS, "https://a.example/x", B_ROBOTS]
    routes[B_ROBOTS] = _robots(ALLOW_ALL)
    assert _text(client, "https://a.example/x", respect_robots=True).status_code == 200
    assert seen.count(B_ROBOTS) == 2 and seen.count(A_ROBOTS) == 1


def test_a_redirect_to_plain_http_is_refused_before_any_robots_read_for_it() -> None:
    """The hop's own https rule comes first, so the robots hook only ever sees a hop that already
    passed it. Reading https://b.example/robots.txt on behalf of an http:// hop would answer a
    question nobody asked, about a URL the walk is about to refuse anyway."""
    seen: list[str] = []
    client = _client(
        {
            A_ROBOTS: _robots(ALLOW_ALL),
            "https://a.example/x": _redirect("http://b.example/y"),
            B_ROBOTS: _robots(DISALLOW_ALL),
            "http://b.example/y": _page(),
        },
        seen=seen,
    )
    r = _text(client, "https://a.example/x", respect_robots=True)
    assert r.status_code == 400 and r.json()["error"]["code"] == "not_https"
    assert seen == [A_ROBOTS, "https://a.example/x"]


def test_a_redirect_to_a_private_address_is_refused_and_nothing_is_requested_from_it() -> None:
    """Neither the hop nor its robots.txt is requested. Either order of the address check and the
    hook gives this answer (the robots read is address-checked too), so it pins the outcome."""

    def resolver(host: str) -> tuple[str, ...]:
        return ("10.0.0.5",) if host == "internal.example" else ("93.184.216.34",)

    seen: list[str] = []
    client = _client(
        {
            A_ROBOTS: _robots(ALLOW_ALL),
            "https://a.example/x": _redirect("https://internal.example/y"),
            "https://internal.example/robots.txt": _robots(ALLOW_ALL),
            "https://internal.example/y": _page(),
        },
        seen=seen,
        resolver=resolver,
    )
    r = _text(client, "https://a.example/x", respect_robots=True)
    assert r.status_code == 400 and r.json()["error"]["code"] == "refused_private_address"
    assert seen == [A_ROBOTS, "https://a.example/x"]


def test_a_relative_redirect_is_checked_as_the_absolute_url_it_resolves_to() -> None:
    seen: list[str] = []
    client = _client(
        {
            A_ROBOTS: _robots("User-agent: *\nDisallow: /moved\n"),
            "https://a.example/x": _redirect("/moved"),
            "https://a.example/moved": _page(),
        },
        seen=seen,
    )
    r = _text(client, "https://a.example/x", respect_robots=True)
    assert r.status_code == 403 and r.json()["error"]["code"] == "robots_disallowed"
    assert seen == [A_ROBOTS, "https://a.example/x"]


def test_a_chain_past_the_redirect_cap_checks_only_the_hops_it_requests() -> None:
    """Three redirects is one past the default cap of two: the fourth URL is never requested, so
    its robots.txt is never read either."""
    seen: list[str] = []
    client = _client(
        {
            A_ROBOTS: _robots(ALLOW_ALL),
            "https://a.example/x": _redirect("https://b.example/mid"),
            B_ROBOTS: _robots(ALLOW_ALL),
            "https://b.example/mid": _redirect("https://c.example/z"),
            C_ROBOTS: _robots(ALLOW_ALL),
            "https://c.example/z": _redirect("https://d.example/final"),
            "https://d.example/robots.txt": _robots(ALLOW_ALL),
            "https://d.example/final": _page(),
        },
        seen=seen,
    )
    r = _text(client, "https://a.example/x", respect_robots=True)
    assert r.status_code == 400 and r.json()["error"]["code"] == "redirect_limit"
    assert seen == [
        A_ROBOTS,
        "https://a.example/x",
        B_ROBOTS,
        "https://b.example/mid",
        C_ROBOTS,
        "https://c.example/z",
    ]


# The hook itself, with no robots in it: the contract ``fetch_text`` and ``_get_guarded`` document.


async def _walk(handler: Callable[[httpx.Request], httpx.Response], hook: Any) -> Any:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await fetch_text(
            client,
            "https://a.example/x",
            max_bytes=10_000,
            timeout_seconds=5.0,
            max_redirects=2,
            resolver=lambda host: ("93.184.216.34",),
            before_redirect=hook,
        )


async def test_the_hop_hook_is_awaited_for_redirect_hops_only_and_before_each_request() -> None:
    log: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(f"GET {request.url}")
        if str(request.url) == "https://a.example/x":
            return httpx.Response(302, headers={"location": "/y"})  # relative
        if str(request.url) == "https://a.example/y":
            return httpx.Response(302, headers={"location": "https://b.example/z"})
        return _page()()

    async def hook(hop_url: str) -> None:
        log.append(f"HOOK {hop_url}")

    fetched = await _walk(handler, hook)
    assert fetched.final_url == "https://b.example/z"
    assert log == [
        "GET https://a.example/x",  # the URL the caller supplied: no hook
        "HOOK https://a.example/y",  # the relative Location, already absolute
        "GET https://a.example/y",
        "HOOK https://b.example/z",
        "GET https://b.example/z",
    ]


async def test_a_refusing_hop_hook_stops_the_walk_and_its_refusal_leaves_unchanged() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://b.example/y"})

    async def hook(hop_url: str) -> None:
        raise FetchRefused("robots_disallowed", f"refused {hop_url}")

    with pytest.raises(FetchRefused) as refused:
        await _walk(handler, hook)
    assert refused.value.code == "robots_disallowed"
    assert refused.value.detail == "refused https://b.example/y"
    assert requested == ["https://a.example/x"]  # the refused hop was never contacted
