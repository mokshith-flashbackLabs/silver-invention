"""The crop fetcher's FastAPI app (ARCHITECTURE.md §3.7) — a standalone
deployable, separate from ``imageshield.http.app``, with no path to Postgres.

Two routes: ``POST /v1/fetch`` (raw bytes) and ``POST /v1/crop`` (a face crop,
blurred by default). Both are token-gated on ``X-Fetcher-Token``; ``/health``
is not, matching the main app's rule that a load-balancer probe cannot carry a
secret.

The error envelope here is deliberately smaller than the main app's
(``imageshield.http.errors``): ``{"error": {"code", "message"}}``, no
``retryable``/``request_id``. This process has no request-logging middleware
to bind a request id, and its only caller (the confirm worker) needs a code to
branch on, nothing more — matching the shape with a heavier contract this
process does not participate in would be coupling for its own sake.

Docs are disabled, same reasoning as the main app: no public ingress, no human
caller, no reason to publish an OpenAPI surface.
"""

from __future__ import annotations

import hmac
import io
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import APIRouter, Depends, FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse
from PIL import Image, ImageFilter
from pydantic import BaseModel, ConfigDict

from imageshield.attribution.crop import CropTooSmall, UndecodableImage, crop_to_face
from imageshield.attribution.models import BoundingBox
from imageshield.fetcher.config import FetcherConfig, load_fetcher_config
from imageshield.fetcher.fetch import FetchRefused, fetch_image
from imageshield.recheck.ssrf import Resolver

log = structlog.get_logger("imageshield.fetcher")

# The margin crop from attribution.crop.crop_to_face is never shown un-blurred
# by default (ARCHITECTURE.md §2.4: a reviewer sees a face crop, not a full
# image, and it stays blurred until they choose to look). Radius and quality
# are properties of what "usably blurred but still a JPEG worth sending" means
# — not operational knobs — so they are constants here, not config, the same
# reasoning attribution/crop.py gives its own margin fraction.
_BLUR_RADIUS = 12
_BLUR_JPEG_QUALITY = 80


class FetcherError(Exception):
    """Carries an HTTP status alongside the minimal ``{code, message}`` this
    app's error handler renders. Deliberately not
    ``imageshield.http.errors.ServiceError`` — see the module docstring."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(FetcherError)
    async def _fetcher_error_handler(request: Request, exc: FetcherError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )


# Every FetchRefused code maps to one status. 'redirect_limit' groups with the
# 400s rather than with 'unfetchable': it is this process declining to keep
# following a chain (a policy refusal, like the ssrf guard), not a transport
# failure or a bad status FROM the upstream server — which is what
# 'unfetchable' (502) means below.
_FETCH_REFUSED_STATUS: dict[str, int] = {
    "refused_private_address": 400,
    "not_an_image": 400,
    "too_large": 413,
    "redirect_limit": 400,
    "unfetchable": 502,
}


def _fetch_refused_to_error(exc: FetchRefused) -> FetcherError:
    status_code = _FETCH_REFUSED_STATUS.get(exc.code, 502)
    return FetcherError(status_code, exc.code, exc.detail)


class _RequestModel(BaseModel):
    """Local to the fetcher, deliberately: mirrors
    ``imageshield.http.models.ServiceModel``'s ``extra='forbid'`` contract
    (CLAUDE.md §10) without importing the main app's HTTP package, which this
    standalone deployable has no reason to depend on.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class FetchRequest(_RequestModel):
    url: str


class _BBoxRequest(_RequestModel):
    x: float
    y: float
    w: float
    h: float


class CropRequest(_RequestModel):
    url: str
    bbox: _BBoxRequest
    blur: bool = True


def get_fetcher_config(request: Request) -> FetcherConfig:
    config: FetcherConfig = request.app.state.config
    return config


def get_http_client(request: Request) -> httpx.AsyncClient:
    client: httpx.AsyncClient = request.app.state.http_client
    return client


def get_resolver(request: Request) -> Resolver | None:
    # None is a legitimate value here — it means "use recheck.ssrf's real
    # getaddrinfo default" (see address_refusal). Tests override it with a
    # fake; production leaves it unset.
    resolver: Resolver | None = getattr(request.app.state, "resolver", None)
    return resolver


def require_fetcher_token(
    x_fetcher_token: str | None = Header(default=None, alias="X-Fetcher-Token"),
    cfg: FetcherConfig = Depends(get_fetcher_config),
) -> None:
    """Validate ``X-Fetcher-Token``. Purely a guard — handlers never see it.
    No bypass: unlike the main app's SERVICE_TOKEN_AUTH_DISABLED escape hatch,
    this deployable has exactly two callers (the confirm worker on /v1/fetch,
    the services API's subject preview endpoint on /v1/crop — spec 2026-08-21)
    and no local harness that needs one.
    """
    if x_fetcher_token is None or not _constant_time_equal(x_fetcher_token, cfg.fetcher_token):
        raise FetcherError(401, "unauthorised", "invalid fetcher token")


def _constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


health_router = APIRouter()


@health_router.get("/health")
async def health() -> dict[str, str]:
    """Always 200, no dependency check — there is no database to be
    unreachable from, and this route carries no token (a probe cannot carry
    a secret, same rule as the main app's ``/health``)."""
    return {"status": "ok"}


router = APIRouter(prefix="/v1", dependencies=[Depends(require_fetcher_token)])


@router.post("/fetch")
async def fetch(
    body: FetchRequest,
    client: httpx.AsyncClient = Depends(get_http_client),
    resolver: Resolver | None = Depends(get_resolver),
    cfg: FetcherConfig = Depends(get_fetcher_config),
) -> Response:
    try:
        fetched = await fetch_image(
            client,
            body.url,
            max_bytes=cfg.fetch_max_bytes,
            timeout_seconds=cfg.fetch_timeout_seconds,
            max_redirects=cfg.fetch_max_redirects,
            resolver=resolver,
        )
    except FetchRefused as exc:
        raise _fetch_refused_to_error(exc) from exc
    return Response(content=fetched.body, media_type=fetched.content_type)


@router.post("/crop")
async def crop(
    body: CropRequest,
    client: httpx.AsyncClient = Depends(get_http_client),
    resolver: Resolver | None = Depends(get_resolver),
    cfg: FetcherConfig = Depends(get_fetcher_config),
) -> Response:
    try:
        fetched = await fetch_image(
            client,
            body.url,
            max_bytes=cfg.fetch_max_bytes,
            timeout_seconds=cfg.fetch_timeout_seconds,
            max_redirects=cfg.fetch_max_redirects,
            resolver=resolver,
        )
    except FetchRefused as exc:
        raise _fetch_refused_to_error(exc) from exc

    bbox = BoundingBox(x=body.bbox.x, y=body.bbox.y, w=body.bbox.w, h=body.bbox.h)
    try:
        cropped = crop_to_face(fetched.body, bbox)
    except CropTooSmall as exc:
        raise FetcherError(400, "crop_too_small", str(exc)) from exc
    except UndecodableImage as exc:
        # The upstream content-type claimed image/*; the bytes disagreed.
        # Same user-facing meaning as the not_an_image refusal above — what we
        # fetched is not usable as an image — so it reuses that code rather
        # than minting a second one for a distinction only this service sees.
        raise FetcherError(400, "not_an_image", str(exc)) from exc

    if body.blur:
        with Image.open(io.BytesIO(cropped)) as opened:
            blurred = opened.convert("RGB").filter(ImageFilter.GaussianBlur(_BLUR_RADIUS))
            buffer = io.BytesIO()
            blurred.save(buffer, format="JPEG", quality=_BLUR_JPEG_QUALITY)
            cropped = buffer.getvalue()

    return Response(content=cropped, media_type="image/jpeg")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # getattr guards let a caller (tests, a local harness) pre-wire fakes
    # before startup without being overwritten — same convention as the main
    # app's lifespan (imageshield.http.app).
    if getattr(app.state, "http_client", None) is None:
        app.state.http_client = httpx.AsyncClient(
            follow_redirects=False,
            limits=httpx.Limits(max_connections=8),
            headers={"User-Agent": "ImageShield-Fetcher/1.0"},
        )
    if getattr(app.state, "resolver", None) is None:
        app.state.resolver = None
    log.info("fetcher.started")
    try:
        yield
    finally:
        client = getattr(app.state, "http_client", None)
        if client is not None:
            await client.aclose()


def create_app(config: FetcherConfig | None = None) -> FastAPI:
    cfg = config if config is not None else load_fetcher_config()
    app = FastAPI(
        title="ImageShield Fetcher",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan,
    )
    app.state.config = cfg
    _install_error_handlers(app)
    app.include_router(health_router)
    app.include_router(router)
    return app
