"""Fetch a URL's bytes, refusing anything that is not a public resource of the
one content type the caller asked for. The only module in this repo that
touches hostile bytes.

**Two public fetchers, one guard.** ``fetch_image`` takes ``image/*`` and
``fetch_page`` takes ``text/html``; both are thin wrappers over
``_get_guarded``, which owns the redirect walk, the SSRF check on every hop and
the streaming byte cap. ``fetch_page`` was added 2026-09-07 because Google's
``pagesWithMatchingImages`` entries carry no image URL, so a page-keyed hit had
nothing fetchable and the subject saw no picture (11 of 12 real hits). It
widens the hostile-input surface deliberately and under the same guard — the
alternative was leaving those hits unreviewable.

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

import httpx
from pydantic import BaseModel, ConfigDict

from imageshield.recheck.ssrf import Resolver, address_refusal

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class FetchRefused(Exception):
    """Why a fetch did not happen.

    ``code`` is the stable machine string ``fetcher/app.py`` maps to an HTTP
    status; ``detail`` is free text for logs, never echoed to a caller as-is.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


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
    accept_prefix: str,
    reject_code: str,
    max_bytes: int,
    timeout_seconds: float,
    max_redirects: int,
    resolver: Resolver | None,
) -> tuple[str, bytes]:
    """The guarded GET both public fetchers are built on.

    ONE implementation on purpose. ``fetch_image`` and ``fetch_page`` differ
    only in which content type they accept and how big a body they tolerate;
    everything that makes this safe — the hand-rolled redirect walk with
    ``address_refusal`` re-run on every hop, the content-type check before the
    body is read, the cap applied WHILE streaming — is identical, and a second
    copy of it is how one of the two ends up missing a hop check later.
    """
    current = url
    for _hop in range(max_redirects + 1):
        refusal = address_refusal(current, resolver)
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
                raise FetchRefused("unfetchable", f"upstream returned {response.status_code}")

            # Checked BEFORE the body is read: no point paying for bytes we
            # are about to refuse, and a hostile response can make the body
            # arbitrarily expensive to pull off the wire.
            content_type = response.headers.get("content-type", "")
            if not content_type.startswith(accept_prefix):
                raise FetchRefused(reject_code, content_type or "(missing)")

            body = bytearray()
            try:
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise FetchRefused("too_large", f"exceeded {max_bytes} bytes")
            except httpx.HTTPError as exc:
                raise FetchRefused("unfetchable", str(exc)) from exc

            return content_type, bytes(body)
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
    content_type, body = await _get_guarded(
        client,
        url,
        accept_prefix="image/",
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
    content_type, body = await _get_guarded(
        client,
        url,
        accept_prefix="text/html",
        reject_code="not_a_page",
        max_bytes=max_bytes,
        timeout_seconds=timeout_seconds,
        max_redirects=max_redirects,
        resolver=resolver,
    )
    return FetchedPage(content_type=content_type, html=body.decode("utf-8", errors="replace"))
