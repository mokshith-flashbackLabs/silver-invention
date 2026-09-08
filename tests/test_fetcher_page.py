"""``POST /v1/page`` — read a page's HTML so its ``og:image`` can be resolved.

Why this exists: Google's ``pagesWithMatchingImages`` entries carry no image
URL, so a page-keyed infringement had nothing fetchable and 11 of 12 real hits
on 2026-09-07 reached the subject with no picture to answer "is this you?"
against.

This widens the hostile-input surface on the one deployable built to contain
it, so it carries the SAME guard as ``/v1/fetch`` and nothing weaker: the
hand-rolled redirect walk with ``address_refusal`` on every hop, a byte cap
applied while streaming, and a content-type gate. The gate is the only
difference — ``text/html`` here, ``image/*`` there — and the cap is much
smaller, because a document is not a photograph.
"""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from imageshield.fetcher.app import create_app
from imageshield.fetcher.config import FetcherConfig

TOKEN = "fetcher-token-for-tests-0003"
AUTH = {"X-Fetcher-Token": TOKEN}

_PAGE = b'<html><head><meta property="og:image" content="https://cdn.x/p.jpg"></head></html>'


def _config(**overrides: object) -> FetcherConfig:
    return FetcherConfig(fetcher_token=TOKEN, **overrides)  # type: ignore[arg-type]


def _client(handler, *, resolver=None, config=None) -> TestClient:
    app = create_app(config=config or _config())
    app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.resolver = resolver or (lambda host: ("93.184.216.34",))
    return TestClient(app)


def _page_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=_PAGE, headers={"content-type": "text/html; charset=utf-8"})


def test_page_requires_the_token() -> None:
    client = _client(_page_handler)
    assert client.post("/v1/page", json={"url": "https://x.example/a"}).status_code == 401


def test_page_returns_the_html() -> None:
    client = _client(_page_handler)
    response = client.post("/v1/page", json={"url": "https://x.example/a"}, headers=AUTH)
    assert response.status_code == 200
    assert response.json()["html"] == _PAGE.decode()


def test_page_refuses_private_addresses() -> None:
    # The guard that matters most: a page URL is attacker-influenceable, so the
    # SSRF check must run here exactly as it does for an image.
    client = _client(_page_handler, resolver=lambda host: ("169.254.169.254",))
    response = client.post("/v1/page", json={"url": "https://x.example/a"}, headers=AUTH)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "refused_private_address"


def test_page_refuses_a_non_html_content_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})

    client = _client(handler)
    response = client.post("/v1/page", json={"url": "https://x.example/a"}, headers=AUTH)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "not_a_page"


def test_page_truncates_at_the_cap_instead_of_refusing() -> None:
    """A page is not a photograph. We want the <head>'s meta tags, so reading
    the first N bytes and stopping is the CORRECT answer -- whereas a truncated
    image is useless, which is why /v1/fetch still refuses.

    Found in production, not in review: a 256KB cap refused YouTube, LinkedIn,
    Facebook, Instagram and nearstore with 413 on the first real run (2026-09-07).
    Modern platform HTML is megabytes; the og:image is near the top of it."""
    head = b'<html><head><meta property="og:image" content="https://cdn.x/p.jpg">'
    body = head + b"<!--" + b"x" * 5000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "text/html"})

    client = _client(handler, config=_config(page_max_bytes=1000))
    response = client.post("/v1/page", json={"url": "https://x.example/a"}, headers=AUTH)
    assert response.status_code == 200
    html = response.json()["html"]
    assert len(html.encode()) <= 1000
    # the part that matters survived, so the resolver can still do its job
    assert "https://cdn.x/p.jpg" in html


def test_an_image_over_the_cap_is_still_refused() -> None:
    """Truncation is a PAGE affordance only. Half a JPEG is not an image, and
    silently returning one would push the failure into the decoder."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 5000, headers={"content-type": "image/png"})

    client = _client(handler, config=_config(fetch_max_bytes=1000))
    response = client.post("/v1/fetch", json={"url": "https://x.example/a.png"}, headers=AUTH)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "too_large"


def test_page_reruns_the_guard_on_every_redirect_hop() -> None:
    # The whole reason the redirect walk is hand-rolled: a public first hop
    # must not be able to bounce us onto a link-local address.
    hops: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(str(request.url))
        if len(hops) == 1:
            return httpx.Response(302, headers={"location": "http://169.254.169.254/"})
        return _page_handler(request)

    def resolver(host: str) -> tuple[str, ...]:
        return ("169.254.169.254",) if host == "169.254.169.254" else ("93.184.216.34",)

    client = _client(handler, resolver=resolver)
    response = client.post("/v1/page", json={"url": "https://x.example/a"}, headers=AUTH)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "refused_private_address"
    assert len(hops) == 1  # never followed to the link-local address


def test_page_refuses_an_upstream_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, headers={"content-type": "text/html"})

    client = _client(handler)
    response = client.post("/v1/page", json={"url": "https://x.example/a"}, headers=AUTH)
    assert response.json()["error"]["code"] == "unfetchable"
