"""The page-preview resolver.

Google's ``pagesWithMatchingImages`` entries carry ``url`` and ``pageTitle`` and
nothing else — no image URL at all. Keying an infringement on such a page left
``image_url`` holding a PAGE address, which ``fetcher.fetch_image`` correctly
refuses (``not_an_image``, HTTP 400), so no triage ran, no ``best_face_bbox``
was written, and the subject's ask-card had no picture. 11 of 12 real hits on
2026-09-07 were in that state.

These tests cover the pure half of the fix: given a page's HTML, what image did
the page itself publish as its preview? No network here — the fetching and its
SSRF guard belong to the caller.
"""

from __future__ import annotations

from imageshield.confirm.og_image import page_preview_url

_BASE = "https://www.example.com/creator/posts/12345"


def test_reads_the_og_image_a_page_publishes() -> None:
    html = """
    <html><head>
      <meta property="og:title" content="A post">
      <meta property="og:image" content="https://cdn.example.com/p/12345.jpg">
    </head><body>irrelevant</body></html>
    """
    assert page_preview_url(html, _BASE) == "https://cdn.example.com/p/12345.jpg"


def test_falls_back_to_twitter_image_when_there_is_no_og_image() -> None:
    html = """
    <html><head>
      <meta name="twitter:image" content="https://cdn.example.com/t/12345.jpg">
    </head></html>
    """
    assert page_preview_url(html, _BASE) == "https://cdn.example.com/t/12345.jpg"


def test_prefers_og_image_over_twitter_image() -> None:
    html = """
    <html><head>
      <meta name="twitter:image" content="https://cdn.example.com/t/1.jpg">
      <meta property="og:image" content="https://cdn.example.com/o/1.jpg">
    </head></html>
    """
    assert page_preview_url(html, _BASE) == "https://cdn.example.com/o/1.jpg"


def test_resolves_a_relative_image_against_the_page_url() -> None:
    html = '<meta property="og:image" content="/static/preview.jpg">'
    assert page_preview_url(html, _BASE) == "https://www.example.com/static/preview.jpg"


def test_resolves_a_protocol_relative_image_to_https() -> None:
    html = '<meta property="og:image" content="//cdn.example.com/p.jpg">'
    assert page_preview_url(html, _BASE) == "https://cdn.example.com/p.jpg"


def test_none_when_the_page_publishes_no_preview() -> None:
    html = "<html><head><title>nothing here</title></head><body>text</body></html>"
    assert page_preview_url(html, _BASE) is None


def test_none_rather_than_raising_on_malformed_markup() -> None:
    # A hostile or truncated page must not take the confirm worker down; the
    # only acceptable failure here is "no preview found".
    assert page_preview_url("<meta property=og:image content=", _BASE) is None
    assert page_preview_url("", _BASE) is None


def test_refuses_a_non_http_scheme() -> None:
    # `javascript:` and `data:` are not fetchable images, and a data: URI would
    # smuggle bytes past the fetcher's SSRF guard entirely (INVARIANTS #9).
    for hostile in (
        '<meta property="og:image" content="javascript:alert(1)">',
        '<meta property="og:image" content="data:image/png;base64,AAAA">',
        '<meta property="og:image" content="file:///etc/passwd">',
    ):
        assert page_preview_url(hostile, _BASE) is None


def test_ignores_an_empty_or_whitespace_content_attribute() -> None:
    assert page_preview_url('<meta property="og:image" content="">', _BASE) is None
    assert page_preview_url('<meta property="og:image" content="   ">', _BASE) is None


def test_case_insensitive_on_the_property_name() -> None:
    html = '<META PROPERTY="OG:IMAGE" CONTENT="https://cdn.example.com/p.jpg">'
    assert page_preview_url(html, _BASE) == "https://cdn.example.com/p.jpg"
