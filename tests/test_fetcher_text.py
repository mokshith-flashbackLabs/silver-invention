"""``POST /v1/text`` — text, feeds and JSON, for likeness intel (spec §4.2).

The one egress path for intel: the worker never fetches a third-party URL
itself. Same SSRF guard and hand-rolled redirect walk as ``/v1/fetch`` and
``/v1/page``, plus two properties neither of those needs: ``https`` only on
EVERY hop (this fetch's output can end up quoted as evidence, not merely
rendered for one response), and a whole-document scan for ``<!DOCTYPE``/
``<!ENTITY`` before any XML parser sees a feed body -- a hostile feed can pad
itself past any fixed offset, so the refusal cannot be limited to a prefix.
"""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from imageshield.fetcher.app import create_app
from imageshield.fetcher.config import FetcherConfig

TOKEN = "fetcher-token-for-tests-0003"
AUTH = {"X-Fetcher-Token": TOKEN}


def _client(handler, *, resolver=None, **cfg: object) -> TestClient:
    app = create_app(config=FetcherConfig(fetcher_token=TOKEN, **cfg))  # type: ignore[arg-type]
    app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.resolver = resolver or (lambda host: ("93.184.216.34",))
    return TestClient(app)


def _ok(body: bytes, ctype: str):
    return lambda request: httpx.Response(200, content=body, headers={"content-type": ctype})


def test_html_becomes_visible_text() -> None:
    html = (
        b"<html><head><title>T</title><style>p{}</style><script>x()</script></head>"
        b"<body><h1>Terms</h1><p>We may use your photos.</p></body></html>"
    )
    r = _client(_ok(html, "text/html; charset=utf-8")).post(
        "/v1/text", json={"url": "https://p.example/terms"}, headers=AUTH
    )
    assert r.status_code == 200
    body = r.json()
    assert "We may use your photos." in body["text"] and "x()" not in body["text"]
    assert body["truncated"] is False and body["items"] is None
    assert body["final_url"] == "https://p.example/terms"


def test_declared_charset_is_honoured_and_a_lying_one_decodes_with_replacement() -> None:
    latin = "Café terms".encode("latin-1")
    ok = _client(_ok(latin, "text/plain; charset=iso-8859-1")).post(
        "/v1/text", json={"url": "https://p.example/a"}, headers=AUTH
    )
    assert "Café" in ok.json()["text"]
    lying = _client(_ok("Café".encode("cp1252"), "text/plain; charset=utf-8")).post(
        "/v1/text", json={"url": "https://p.example/b"}, headers=AUTH
    )
    assert lying.status_code == 200 and "�" in lying.json()["text"]


def test_rss_items_are_listed() -> None:
    rss = (
        b"<?xml version='1.0'?><rss><channel><item><title>Breach at X</title>"
        b"<link>https://n.example/1</link><pubDate>Mon, 21 Sep 2026 10:00:00 GMT</pubDate>"
        b"</item></channel></rss>"
    )
    r = _client(_ok(rss, "application/rss+xml")).post(
        "/v1/text", json={"url": "https://n.example/feed"}, headers=AUTH
    )
    items = r.json()["items"]
    assert items == [
        {
            "title": "Breach at X",
            "link": "https://n.example/1",
            "published": "2026-09-21T10:00:00+00:00",
        }
    ]


def test_a_feed_with_a_doctype_is_refused_before_parsing() -> None:
    evil = b"<?xml version='1.0'?><!DOCTYPE r [<!ENTITY a 'x'>]><rss/>"
    r = _client(_ok(evil, "application/xml")).post(
        "/v1/text", json={"url": "https://n.example/feed"}, headers=AUTH
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "unsupported_type"


def test_an_entity_declaration_past_the_first_2048_chars_is_still_refused() -> None:
    """The refusal scans the WHOLE decoded body. A prefix-only scan (the first
    2048 chars) is a check a hostile feed can simply pad its way past -- this
    comment alone is over 2048 bytes before the DOCTYPE/ENTITY ever appears."""
    padding = "<!--" + ("x" * 2200) + "-->"
    evil = f"<?xml version='1.0'?><rss>{padding}<!DOCTYPE r [<!ENTITY a 'x'>]></rss>".encode()
    assert len(evil) > 2048
    r = _client(_ok(evil, "application/xml")).post(
        "/v1/text", json={"url": "https://n.example/feed"}, headers=AUTH
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "unsupported_type"


def test_unsupported_type_and_http_are_refused() -> None:
    c = _client(_ok(b"%PDF", "application/pdf"))
    assert (
        c.post("/v1/text", json={"url": "https://p.example/x.pdf"}, headers=AUTH).json()["error"][
            "code"
        ]
        == "unsupported_type"
    )
    assert (
        c.post("/v1/text", json={"url": "http://p.example/x"}, headers=AUTH).json()["error"]["code"]
        == "not_https"
    )


def test_private_address_on_a_redirect_hop_is_refused() -> None:
    hops: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://169.254.169.254/"})

    def resolver(host: str) -> tuple[str, ...]:
        return ("169.254.169.254",) if host == "169.254.169.254" else ("93.184.216.34",)

    r = _client(handler, resolver=resolver).post(
        "/v1/text", json={"url": "https://p.example/a"}, headers=AUTH
    )
    assert r.json()["error"]["code"] == "refused_private_address" and len(hops) == 1


def test_a_redirect_to_http_is_refused() -> None:
    """https is required on EVERY hop, not just the URL the caller supplied --
    a public, https-first hop must not be able to bounce this fetch onto plain
    http. The second hop is refused before it is ever requested, so exactly
    one request reaches the handler."""
    hops: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://p.example/insecure"})

    r = _client(handler).post("/v1/text", json={"url": "https://p.example/a"}, headers=AUTH)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "not_https"
    assert len(hops) == 1


def test_truncation_is_reported() -> None:
    r = _client(_ok(b"a" * 5000, "text/plain"), intel_text_max_bytes=1000).post(
        "/v1/text", json={"url": "https://p.example/a"}, headers=AUTH
    )
    assert r.json()["truncated"] is True and len(r.json()["text"]) == 1000


def test_page_route_is_unchanged() -> None:
    r = _client(_ok(b"<html></html>", "text/html")).post(
        "/v1/page", json={"url": "https://p.example/a"}, headers=AUTH
    )
    assert r.status_code == 200 and "html" in r.json()
