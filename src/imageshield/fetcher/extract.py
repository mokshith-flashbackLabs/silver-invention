"""Hostile-input parsing stays in the isolated fetcher (INVARIANTS #11). Stdlib
only, the og_image.py precedent: no bs4, no lxml, no feedparser.

**XXE and entity-expansion refusal scans the WHOLE decoded text, not a
prefix.** A ``<!DOCTYPE`` or ``<!ENTITY`` declaration is refused wherever it
sits in the document — a hostile feed can pad its body past any fixed offset
before the declaration that matters, so a prefix scan is a check an attacker
can simply out-run. ``xml.etree.ElementTree`` has no external-entity fetch of
its own (unlike ``lxml``), but a local entity declaration is still enough for
a billion-laughs expansion, so the refusal happens before ``fromstring`` ever
sees the text, whatever the input's length.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict

_BLOCK = {
    "p",
    "div",
    "br",
    "li",
    "tr",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "section",
    "article",
    "header",
    "footer",
    "blockquote",
    "pre",
    "table",
    "ul",
    "ol",
}
_SKIP = {"script", "style", "template", "noscript", "svg"}

# Refused wherever they appear in the document (see the module docstring) —
# never sliced to a prefix. A doctype with no entity is refused too: an
# internal DTD can still redefine general entities other than the five
# predefined ones, so "no ENTITY keyword" is not by itself a safe document.
_DISALLOWED_XML_MARKERS = ("<!DOCTYPE", "<!ENTITY")


class UnsupportedDocument(Exception):
    pass


class FeedItem(BaseModel):
    model_config = ConfigDict(frozen=True)
    title: str
    link: str
    published: str | None


class ExtractedText(BaseModel):
    """``published_at`` is the page's OWN statement of when it was published, read from its HTML
    metadata (``published_metadata``), as ISO-8601, or None. Never the fetch time, and never
    anything for a feed, JSON or plain text (a feed's items carry their own dates)."""

    model_config = ConfigDict(frozen=True)
    text: str
    items: list[FeedItem] | None
    published_at: str | None = None


class _Visible(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self._skip += 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP and self._skip:
            self._skip -= 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


# ── the page's own publication date (2026-10-04, spec 2026-10-04-intel-evidence-quality §2) ───
#
# Read from metadata the publisher wrote, in this order, the first value that parses winning:
# ``article:published_time`` (Open Graph), JSON-LD ``datePublished`` (top level, a top-level list,
# or inside ``@graph``; an article-typed node before any other), ``itemprop="datePublished"``, and
# the ``name=`` tags below in their listed order. Bytes already in memory, stdlib only, nothing
# kept: the same rules as the text extraction around it.

# ``<meta name=...>`` keys that carry a publication date, in the order they are tried.
_NAMED_DATE_META: tuple[str, ...] = (
    "date",
    "pubdate",
    "publish-date",
    "parsely-pub-date",
    "dc.date",
    "dcterms.date",
)
# JSON-LD @types whose datePublished is the page's own, tried before any other node's.
_ARTICLE_TYPES = frozenset(
    {
        "article",
        "newsarticle",
        "blogposting",
        "report",
        "scholarlyarticle",
        "techarticle",
        "reportagenewsarticle",
        "analysisnewsarticle",
        "opinionnewsarticle",
        "backgroundnewsarticle",
        "liveblogposting",
        "socialmediaposting",
    }
)
_DATE_FORMATS: tuple[str, ...] = ("%Y%m%d", "%Y/%m/%d", "%B %d, %Y", "%b %d, %Y", "%d %B %Y")


class _PublishedMeta(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.article_time: list[str] = []
        self.itemprop: list[str] = []
        self.named: dict[str, list[str]] = {}
        self.json_ld: list[str] = []
        self._ld: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): (value or "").strip() for key, value in attrs}
        itemprop = values.get("itemprop", "").lower() == "datepublished"
        if tag == "meta":
            content = values.get("content", "")
            if not content:
                return
            name = values.get("name", "").lower()
            if "article:published_time" in (values.get("property", "").lower(), name):
                self.article_time.append(content)
            if itemprop:
                self.itemprop.append(content)
            if name in _NAMED_DATE_META:
                self.named.setdefault(name, []).append(content)
        elif itemprop:  # <time itemprop="datePublished" datetime="...">
            value = values.get("datetime") or values.get("content")
            if value:
                self.itemprop.append(value)
        if tag == "script" and values.get("type", "").split(";")[0] == "application/ld+json":
            self._ld = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._ld is not None:
            self.json_ld.append("".join(self._ld))
            self._ld = None

    def handle_data(self, data: str) -> None:
        if self._ld is not None:
            self._ld.append(data)


def iso_date(value: str) -> str | None:
    """A publication date as ISO-8601 with a timezone (a naive value is UTC), or None when it
    does not parse. ISO first, then RFC 2822, then a few written forms."""
    raw = value.strip()
    if not raw:
        return None
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError):
            for pattern in _DATE_FORMATS:
                try:
                    parsed = datetime.strptime(raw, pattern)
                    break
                except ValueError:
                    continue
    if parsed is None:
        return None
    return (parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)).isoformat()


def _ld_nodes(value: Any) -> Iterator[dict[str, Any]]:
    """The nodes a page's JSON-LD describes itself with: the top-level object(s) and anything in
    their ``@graph``. Nested properties (an author, a publisher) are never walked."""
    if isinstance(value, list):
        for item in value:
            yield from _ld_nodes(item)
    elif isinstance(value, dict):
        yield value
        graph = value.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                yield from _ld_nodes(item)


def _is_article(node: dict[str, Any]) -> bool:
    kinds = node.get("@type")
    names = [kinds] if isinstance(kinds, str) else kinds if isinstance(kinds, list) else []
    return any(isinstance(k, str) and k.lower() in _ARTICLE_TYPES for k in names)


def _json_ld_dates(scripts: list[str]) -> list[str]:
    articles: list[str] = []
    others: list[str] = []
    for script in scripts:
        try:
            document = json.loads(script)
        except (ValueError, RecursionError):
            continue
        for node in _ld_nodes(document):
            published = node.get("datePublished")
            if isinstance(published, str) and published.strip():
                (articles if _is_article(node) else others).append(published)
    return articles + others


def published_metadata(html: str) -> str | None:
    """The page's own publication date from its HTML metadata, as ISO-8601, or None."""
    meta = _PublishedMeta()
    try:
        meta.feed(html)
        meta.close()
    except (AssertionError, ValueError):  # HTMLParser on badly broken markup: no date, no failure
        return None
    candidates = [
        *meta.article_time,
        *_json_ld_dates(meta.json_ld),
        *meta.itemprop,
        *(value for name in _NAMED_DATE_META for value in meta.named.get(name, [])),
    ]
    for candidate in candidates:
        parsed = iso_date(candidate)
        if parsed is not None:
            return parsed
    return None


def _charset(content_type: str) -> str:
    for part in content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.lower() == "charset" and value:
            return value.strip("\"' ")
    return "utf-8"


def _decode(content_type: str, raw: bytes) -> str:
    try:
        return raw.decode(_charset(content_type), errors="replace")
    except LookupError:  # an unknown charset name falls back, never raises
        return raw.decode("utf-8", errors="replace")


def _published(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError):
        return value.strip() or None


def _feed_items(text: str) -> list[FeedItem]:
    upper = text.upper()
    if any(marker in upper for marker in _DISALLOWED_XML_MARKERS):
        raise UnsupportedDocument("DTD refused")
    root = ElementTree.fromstring(text)
    items: list[FeedItem] = []
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        fields = {child.tag.rsplit("}", 1)[-1]: child for child in node}
        link_node = fields.get("link")
        link = (
            (link_node.get("href") or link_node.text or "").strip() if link_node is not None else ""
        )
        title = (fields["title"].text or "").strip() if "title" in fields else ""
        # NOT `a or b or c`: an ``Element`` with no children is falsy (a
        # ``DeprecationWarning`` today, ``TypeError`` in a future stdlib), so
        # a <pubDate/> with no sub-elements would silently fall through to
        # <published> instead of being read.
        date_node = fields.get("pubDate")
        if date_node is None:
            date_node = fields.get("published")
        if date_node is None:
            date_node = fields.get("updated")
        items.append(
            FeedItem(
                title=title,
                link=link,
                published=_published(date_node.text if date_node is not None else None),
            )
        )
    return items


def to_text(content_type: str, raw: bytes) -> ExtractedText:
    text = _decode(content_type, raw)
    base = content_type.split(";")[0].strip().lower()
    if base in ("text/html", "application/xhtml+xml"):
        parser = _Visible()
        parser.feed(text)
        return ExtractedText(
            text="".join(parser.parts), items=None, published_at=published_metadata(text)
        )
    if base in ("application/rss+xml", "application/atom+xml", "application/xml", "text/xml"):
        try:
            items = _feed_items(text)
        except ElementTree.ParseError as exc:
            raise UnsupportedDocument("unparseable xml") from exc
        joined = "\n".join(f"{i.title} {i.link}" for i in items)
        return ExtractedText(text=joined, items=items)
    if base == "application/json":
        try:
            return ExtractedText(text=json.dumps(json.loads(text), indent=1), items=None)
        except json.JSONDecodeError as exc:
            raise UnsupportedDocument("unparseable json") from exc
    return ExtractedText(text=text, items=None)
