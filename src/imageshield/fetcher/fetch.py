"""Fetch a URL's bytes, refusing anything that is not a public, image-typed
resource. The only function in this repo that touches hostile bytes.

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

``FetchedImage`` holds bytes **in memory only, for the lifetime of one
request**. Nothing here writes to disk, a column, or a log (INVARIANTS #9) —
the caller (``fetcher/app.py``) streams it straight into the HTTP response (or
through ``attribution.crop.crop_to_face``) and it is discarded when that
response is sent.
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


async def fetch_image(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int,
    timeout_seconds: float,
    max_redirects: int,
    resolver: Resolver | None = None,
) -> FetchedImage:
    """GET ``url``, following redirects by hand so the SSRF guard re-runs on
    every hop. Raises :class:`FetchRefused` for every way this can legitimately
    not work; nothing else should escape this function.
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
                raise FetchRefused(
                    "unfetchable", f"upstream returned {response.status_code}"
                )

            # Checked BEFORE the body is read: no point paying for bytes we
            # are about to refuse, and a hostile response can make the body
            # arbitrarily expensive to pull off the wire.
            content_type = response.headers.get("content-type", "")
            if not content_type.startswith("image/"):
                raise FetchRefused("not_an_image", content_type or "(missing)")

            body = bytearray()
            try:
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise FetchRefused("too_large", f"exceeded {max_bytes} bytes")
            except httpx.HTTPError as exc:
                raise FetchRefused("unfetchable", str(exc)) from exc

            return FetchedImage(content_type=content_type, body=bytes(body))
        finally:
            await response.aclose()

    raise FetchRefused("redirect_limit", f"exceeded {max_redirects} redirects")
