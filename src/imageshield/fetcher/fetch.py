"""Fetch a URL's bytes, refusing anything that is not a public resource of the
one content type the caller asked for. The only module in this repo that
touches hostile bytes.

**Three public fetchers, one guard.** ``fetch_image`` takes ``image/*``,
``fetch_page`` takes ``text/html`` and ``fetch_text`` takes ``TEXT_TYPES``; all
three are thin wrappers over ``_get_guarded``, which owns the redirect walk,
the SSRF check on every hop and the streaming byte cap. ``fetch_page`` was
added 2026-09-07 because Google's ``pagesWithMatchingImages`` entries carry no
image URL, so a page-keyed hit had nothing fetchable and the subject saw no
picture (11 of 12 real hits). It widens the hostile-input surface deliberately
and under the same guard — the alternative was leaving those hits
unreviewable. ``fetch_text`` was added for likeness intel (spec §4.2): the
worker never fetches a third-party URL itself, so this is the only egress path
for a page's terms, a news feed or a JSON API response, and it is the one
caller that also refuses a plain-``http`` hop (``https_only=True``) — the
other two render or relay bytes for one request, while this one's output can
end up quoted as evidence. ``fetch_robots`` (2026-09-30) is a fourth thin wrapper over
the same guard, for source validation's robots.txt check (spec §4.10,
``fetcher/robots.py``): https on every hop, any content type, and the upstream's
status kept on ``FetchRefused.status`` so a 4xx can be told from a 5xx.

Mirrors ``recheck/client.py``'s hand-rolled redirect walk (same
``_REDIRECT_STATUSES``, same "guard, then request, on every hop" shape) for
the same reason: letting the HTTP client follow redirects internally applies
the guard to the first URL only, and ``https://allowed.example/x`` -> ``302
http://169.254.169.254/`` would then be fetched. Unlike the recheck loop,
there is no domain allowlist here — this fetcher is hostile-URL universal, not
scoped to a corpus of known domains — so ``recheck.ssrf.address_refusal`` (the
DNS + global-address half, extracted for exactly this reuse) is the whole
guard.

Streaming, not ``client.get()``: a hostile server can return an arbitrarily
long body, so the byte cap has to apply WHILE reading, not after. The
connection is closed the moment the running total exceeds ``max_bytes``,
before the rest of the body is ever pulled off the wire.

**Known limitation, stated rather than hidden (same DNS-rebinding window
``recheck/ssrf.py`` documents):** ``address_refusal`` resolves the host and
checks every address it returns, but the underlying ``httpx`` connection
resolves the name again to actually connect -- a DNS answer that changes
between those two resolutions can slip a private address past the check.
There is no allowlist layer here to fall back on -- by design, since this
fetcher is hostile-URL universal rather than scoped to a known corpus like
the recheck loop. The mitigations are the same two the recheck loop leans on
for its own third line: ``address_refusal`` still closes off the wide-open
case (a name that resolves to a private/link-local/loopback address at all),
and this deployable's no-VPC egress posture (INVARIANTS #11) means a
successful rebind still reaches nothing worth reaching from here.

``FetchedImage`` holds bytes **in memory only, for the lifetime of one
request**. Nothing here writes to disk, a column, or a log (INVARIANTS #9) —
the caller (``fetcher/app.py``) streams it straight into the HTTP response (or
through ``fetcher.render.render_preview``, which blurs the whole frame) and it
is discarded when that response is sent.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict

from imageshield.recheck.ssrf import Resolver, address_refusal

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class FetchRefused(Exception):
    """Why a fetch did not happen.

    ``code`` is the stable machine string ``fetcher/app.py`` maps to an HTTP
    status; ``detail`` is free text for logs, never echoed to a caller as-is.
    ``status`` is the upstream's HTTP status when it answered with a non-2xx, so a
    robots.txt read can tell a 4xx from a 5xx; it is None for every other refusal.
    """

    def __init__(self, code: str, detail: str, *, status: int | None = None) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.status = status


class FetchedImage(BaseModel):
    """In-memory only. See the module docstring — this is never persisted."""

    model_config = ConfigDict(frozen=True)

    content_type: str
    body: bytes


class FetchedPage(BaseModel):
    """A page's HTML, in memory for one request, so its ``og:image`` can be
    read. Never persisted and never logged — INVARIANTS #9 covers a document
    exactly as it covers image bytes.
    """

    model_config = ConfigDict(frozen=True)

    content_type: str
    html: str


async def _get_guarded(
    client: httpx.AsyncClient,
    url: str,
    *,
    accept_prefixes: tuple[str, ...],
    reject_code: str,
    max_bytes: int,
    truncate_over_cap: bool = False,
    timeout_seconds: float,
    max_redirects: int,
    resolver: Resolver | None,
    https_only: bool = False,
) -> tuple[str, bytes, str]:
    """The guarded GET both public fetchers are built on.

    ONE implementation on purpose. ``fetch_image``, ``fetch_page`` and
    ``fetch_text`` differ only in which content type they accept, how big a
    body they tolerate and (``fetch_text`` only) whether every hop must stay
    on ``https``; everything that makes this safe — the hand-rolled redirect
    walk with ``address_refusal`` re-run on every hop, the content-type check
    before the body is read, the cap applied WHILE streaming — is identical,
    and a second copy of it is how one of the three ends up missing a hop
    check later.

    Returns ``(content_type, body, final_url)`` — ``final_url`` is ``current``
    at the hop that actually answered, so a caller can tell a subject or an
    operator which URL the bytes really came from after any redirects.
    """
    current = url
    for _hop in range(max_redirects + 1):
        if https_only and urlsplit(current).scheme != "https":
            # Checked on EVERY hop, not just the URL the caller supplied: a
            # https origin can still redirect to a plain-http location, and an
            # intel fetch must never follow that — same reasoning as the SSRF
            # check re-running per hop, just for a different failure mode.
            raise FetchRefused("not_https", f"{current} is not https")

        # Run off the event loop: a real getaddrinfo() is a blocking syscall,
        # and this loop can run once per redirect hop.
        refusal = await asyncio.to_thread(address_refusal, current, resolver)
        if refusal is not None:
            # Every ssrf refusal reason (not just 'private_address') collapses
            # onto one FetchRefused code — the caller's contract is "this
            # target is not one we may fetch", and the specific reason is
            # detail for logs, not a distinction the HTTP response makes.
            raise FetchRefused("refused_private_address", refusal)

        try:
            request = client.build_request("GET", current, timeout=timeout_seconds)
            response = await client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise FetchRefused("unfetchable", str(exc)) from exc

        try:
            if response.status_code in _REDIRECT_STATUSES:
                location = response.headers.get("location")
                if not location:
                    raise FetchRefused("unfetchable", "redirect with no location header")
                current = str(httpx.URL(current).join(location))
                continue

            if not 200 <= response.status_code < 300:
                raise FetchRefused(
                    "unfetchable",
                    f"upstream returned {response.status_code}",
                    status=response.status_code,
                )

            # Checked BEFORE the body is read: no point paying for bytes we
            # are about to refuse, and a hostile response can make the body
            # arbitrarily expensive to pull off the wire.
            content_type = response.headers.get("content-type", "")
            if not content_type.startswith(accept_prefixes):
                raise FetchRefused(reject_code, content_type or "(missing)")

            body = bytearray()
            try:
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        if truncate_over_cap:
                            # A PAGE only needs its <head>: the og:image sits
                            # near the top, so the first max_bytes is the
                            # answer and the rest is waste we stop paying for.
                            # An IMAGE gets no such affordance -- half a JPEG
                            # is not an image, and returning one would push the
                            # failure into the decoder instead of surfacing it
                            # here. Found in production: a 256KB refusal 413'd
                            # YouTube, LinkedIn, Facebook, Instagram and
                            # nearstore on the first real run (2026-09-07),
                            # because modern platform HTML is megabytes.
                            del body[max_bytes:]
                            break
                        raise FetchRefused("too_large", f"exceeded {max_bytes} bytes")
            except httpx.HTTPError as exc:
                raise FetchRefused("unfetchable", str(exc)) from exc

            return content_type, bytes(body), current
        finally:
            await response.aclose()

    raise FetchRefused("redirect_limit", f"exceeded {max_redirects} redirects")


async def fetch_image(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int,
    timeout_seconds: float,
    max_redirects: int,
    resolver: Resolver | None = None,
) -> FetchedImage:
    """GET ``url`` and refuse anything that is not an image. Raises
    :class:`FetchRefused` for every way this can legitimately not work; nothing
    else should escape.
    """
    content_type, body, _final_url = await _get_guarded(
        client,
        url,
        accept_prefixes=("image/",),
        reject_code="not_an_image",
        max_bytes=max_bytes,
        timeout_seconds=timeout_seconds,
        max_redirects=max_redirects,
        resolver=resolver,
    )
    return FetchedImage(content_type=content_type, body=body)


async def fetch_page(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int,
    timeout_seconds: float,
    max_redirects: int,
    resolver: Resolver | None = None,
) -> FetchedPage:
    """GET ``url`` as HTML, so ``confirm.og_image.page_preview_url`` can read the
    preview image the page publishes for itself.

    Exists because Google's ``pagesWithMatchingImages`` entries carry no image
    URL, which left a page-keyed hit with nothing fetchable and the subject with
    no picture to answer "is this you?" against.

    Decoded with ``errors="replace"``: the only consumer reads ASCII URLs out of
    ``<meta>`` tags, and a mis-encoded or hostile page must yield a useless
    string rather than an exception. The charset in ``content-type`` is
    deliberately not honoured — trusting it would let a page choose our decoder.
    """
    content_type, body, _final_url = await _get_guarded(
        client,
        url,
        accept_prefixes=("text/html",),
        reject_code="not_a_page",
        truncate_over_cap=True,
        max_bytes=max_bytes,
        timeout_seconds=timeout_seconds,
        max_redirects=max_redirects,
        resolver=resolver,
    )
    return FetchedPage(content_type=content_type, html=body.decode("utf-8", errors="replace"))


TEXT_TYPES: tuple[str, ...] = (
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "application/rss+xml",
    "application/atom+xml",
    "application/xml",
    "text/xml",
    "application/json",
)


class FetchedText(BaseModel):
    """Bytes of a public text document, in memory for one request. Never
    persisted and never logged by the fetcher (INVARIANTS #9)."""

    model_config = ConfigDict(frozen=True)

    content_type: str
    final_url: str
    raw: bytes
    truncated: bool


async def fetch_text(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int,
    timeout_seconds: float,
    max_redirects: int,
    resolver: Resolver | None = None,
) -> FetchedText:
    """GET ``url`` as one of ``TEXT_TYPES`` for likeness intel (spec §4.2).

    ``https_only=True``: every hop must stay on ``https``, not merely the URL
    the caller supplied — a redirect to plain ``http`` is refused exactly like
    a redirect to a private address, because this fetcher's callers use the
    result as evidence, not as a rendering convenience.

    Reading ``max_bytes + 1`` is what makes ``truncated`` honest: one byte past
    the cap proves more existed than what is returned.
    """
    content_type, body, final_url = await _get_guarded(
        client,
        url,
        accept_prefixes=TEXT_TYPES,
        reject_code="unsupported_type",
        truncate_over_cap=True,
        max_bytes=max_bytes + 1,
        timeout_seconds=timeout_seconds,
        max_redirects=max_redirects,
        resolver=resolver,
        https_only=True,
    )
    truncated = len(body) > max_bytes
    return FetchedText(
        content_type=content_type,
        final_url=final_url,
        raw=body[:max_bytes],
        truncated=truncated,
    )


# RFC 9309 asks a crawler to parse at least 500 KiB of a robots.txt; past this the head is used.
ROBOTS_MAX_BYTES = 512 * 1024


class RobotsFile(BaseModel):
    """A robots.txt, in memory for one request. ``text`` is None when there is no usable file (a
    4xx other than 429, or a redirect chain past the cap): RFC 9309 then allows everything."""

    model_config = ConfigDict(frozen=True)

    text: str | None


async def fetch_robots(
    client: httpx.AsyncClient,
    origin: str,
    *,
    timeout_seconds: float,
    max_redirects: int,
    resolver: Resolver | None = None,
) -> RobotsFile:
    """GET ``{origin}/robots.txt`` for source validation (spec §4.10), under the same guard as
    every fetch here: the SSRF check and https on every hop, a byte cap applied while reading.

    Any content type is accepted: a robots.txt served as ``text/html`` still parses, to nothing if
    it is a page. A 5xx, a 429, a timeout or a transport error is "unreachable", which RFC 9309
    reads as a complete disallow: it raises ``robots_unreachable``. A private address raises
    ``refused_private_address`` unchanged, because the page shares the host."""
    try:
        _content_type, body, _final_url = await _get_guarded(
            client,
            f"{origin}/robots.txt",
            accept_prefixes=("",),
            reject_code="unsupported_type",
            truncate_over_cap=True,
            max_bytes=ROBOTS_MAX_BYTES,
            timeout_seconds=timeout_seconds,
            max_redirects=max_redirects,
            resolver=resolver,
            https_only=True,
        )
    except FetchRefused as exc:
        if exc.code == "refused_private_address":
            raise
        unavailable = exc.status is not None and 400 <= exc.status < 500 and exc.status != 429
        if exc.code == "redirect_limit" or unavailable:
            return RobotsFile(text=None)
        raise FetchRefused("robots_unreachable", exc.detail) from exc
    # utf-8-sig: a byte-order mark (Windows editors write one) is not part of the first line.
    # Left in, ``User-agent`` would read as an unknown field and a blanket ``Disallow: /`` would
    # bind nobody.
    return RobotsFile(text=body.decode("utf-8-sig", errors="replace"))
