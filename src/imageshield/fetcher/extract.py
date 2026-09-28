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
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
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
    model_config = ConfigDict(frozen=True)
    text: str
    items: list[FeedItem] | None


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
        return ExtractedText(text="".join(parser.parts), items=None)
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
