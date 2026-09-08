"""What image does a page publish as its own preview?

Google's ``pagesWithMatchingImages`` entries carry ``url`` and ``pageTitle`` and
nothing else, so a page-keyed infringement has no provider-supplied image URL.
Every platform that matters here — YouTube, LinkedIn, Instagram, Facebook —
does publish one anyway, as the ``og:image`` its own share cards use. Reading
that is how a page match gets a picture for the subject's ask-card.

**Pure, and deliberately so.** This function takes HTML that somebody else
fetched and returns a URL that somebody else must fetch. It performs no I/O, so
it cannot be the thing that reaches a private address: both requests stay with
the caller, behind ``fetcher.fetch_image``'s SSRF guard.

``html.parser`` from the standard library, not bs4 or lxml: CLAUDE.md §3 rule 6
says ask before adding a dependency, and a tolerant parser for a handful of
``<meta>`` tags is exactly what the stdlib already does. It also never executes
anything, which matters for markup from a hostile page.
"""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

# In preference order. `og:image` is the Open Graph standard every platform in
# the 2026-09-07 sample publishes; `twitter:image` is the common fallback.
# Nothing else is consulted — a `<link rel=image_src>` or a guessed `<img>` is
# not a statement by the page about what it depicts, and this image is shown to
# a person being asked "is this you?".
_PREFERRED = ("og:image", "twitter:image")

# The page said "this is my preview" — but the value is attacker-influenceable,
# so only fetchable web schemes survive. `data:` is refused specifically: it
# would carry bytes inline and bypass the fetcher's guard entirely
# (INVARIANTS #9), and `javascript:`/`file:` are not images at all.
_FETCHABLE_SCHEMES = frozenset({"http", "https"})


class _MetaCollector(HTMLParser):
    """Collects the content of the meta tags we care about, first value wins."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta":
            return
        pairs = {name: (value or "") for name, value in attrs}
        # Open Graph uses `property`, Twitter uses `name`; pages in the wild
        # use either for both, so accept whichever carries a key we want.
        key = (pairs.get("property") or pairs.get("name") or "").strip().lower()
        if key in _PREFERRED and key not in self.found:
            self.found[key] = pairs.get("content", "")


def page_preview_url(html: str, base_url: str) -> str | None:
    """The page's own preview image, absolute, or ``None``.

    ``None`` is a normal answer — a page with no Open Graph tags simply has no
    preview to offer, and the caller records the hit as unfetchable rather than
    guessing. Malformed markup is also ``None``: a truncated or hostile page
    must not take the confirm worker down.
    """
    collector = _MetaCollector()
    try:
        collector.feed(html)
        collector.close()
    except Exception:
        # HTMLParser is tolerant, but "no preview" is the only failure mode this
        # function is allowed to have, so anything it does raise lands here.
        return None

    for key in _PREFERRED:
        raw = collector.found.get(key, "").strip()
        if not raw:
            continue
        absolute = urljoin(base_url, raw)
        if urlsplit(absolute).scheme.lower() in _FETCHABLE_SCHEMES:
            return absolute
    return None
