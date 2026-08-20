"""The control-room console's FastAPI app -- a standalone deployable,
separate from ``imageshield.http.app`` and from ``imageshield.fetcher.app``,
with no path to Postgres of any kind. Server-rendered Jinja2 over the
services admin API and the fetcher; every write it makes flows through one
of those two HTTP clients.

``GET /health`` is tokenless -- the ECS health-check target, same rule as
every other deployable in this repo: a load-balancer probe cannot carry a
secret. Every other route requires HTTP Basic auth against
``CONSOLE_OPERATORS`` (``console/auth.py``); the operator name it returns
flows into every write (``decide``, ``create_event``, ``retract_event``) so
the audit trail names a person, not "whoever holds the token".

Docs are disabled, same reasoning as the other two deployables: no public
ingress, no reason to publish an OpenAPI surface for an internal tool.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import structlog
from fastapi import APIRouter, Depends, FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from imageshield.console.auth import require_operator
from imageshield.console.client import ConsoleUpstreamError, FetcherClient, ServicesClient
from imageshield.console.config import ConsoleConfig, load_console_config

log = structlog.get_logger("imageshield.console")

_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def get_services_client(request: Request) -> ServicesClient:
    client: ServicesClient = request.app.state.services_client
    return client


def get_fetcher_client(request: Request) -> FetcherClient:
    client: FetcherClient = request.app.state.fetcher_client
    return client


health_router = APIRouter()


@health_router.get("/health")
async def health() -> dict[str, str]:
    """Always 200, no token, no upstream call -- the ECS health-check target."""
    return {"status": "ok"}


router = APIRouter(dependencies=[Depends(require_operator)])


@router.get("/")
async def dashboard(
    request: Request,
    operator: str = Depends(require_operator),
    services_client: ServicesClient = Depends(get_services_client),
) -> HTMLResponse:
    health_data = await services_client.provider_health()
    queue = await services_client.review_queue()
    return _templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "operator": operator,
            "providers": health_data.get("providers", []),
            "queue": queue,
        },
    )


@router.get("/review")
async def review_get(
    request: Request,
    operator: str = Depends(require_operator),
    services_client: ServicesClient = Depends(get_services_client),
) -> HTMLResponse:
    task = await services_client.review_next()
    bbox: dict[str, Any] | None = None
    if task is not None:
        triage = task.get("triage") or {}
        bbox = triage.get("best_face_bbox")
    return _templates.TemplateResponse(
        request,
        "review.html",
        {"operator": operator, "task": task, "bbox": bbox},
    )


@router.post("/review/{task_id}")
async def review_decide(
    task_id: UUID,
    decision: str = Form(...),
    severity: str = Form(""),
    operator: str = Depends(require_operator),
    services_client: ServicesClient = Depends(get_services_client),
) -> RedirectResponse:
    await services_client.decide(
        task_id, decision=decision, operator=operator, severity=severity or None
    )
    return RedirectResponse(url="/review", status_code=303)


@router.get("/crop")
async def crop(
    url: str = Query(...),
    x: float = Query(...),
    y: float = Query(...),
    w: float = Query(...),
    h: float = Query(...),
    blur: bool = Query(True),
    fetcher_client: FetcherClient = Depends(get_fetcher_client),
) -> Response:
    """The ONLY pixels path this console has -- live-rendered on every call,
    never stored (module docstring). Blurred by default; the reveal link in
    ``review.html`` re-requests the same crop with ``blur=0``."""
    content, media_type = await fetcher_client.crop(
        url=url, bbox={"x": x, "y": y, "w": w, "h": h}, blur=blur
    )
    return Response(content=content, media_type=media_type)


@router.get("/events")
async def events_get(
    request: Request,
    operator: str = Depends(require_operator),
    services_client: ServicesClient = Depends(get_services_client),
) -> HTMLResponse:
    events = await services_client.list_events()
    return _templates.TemplateResponse(
        request, "events.html", {"operator": operator, "events": events}
    )


@router.post("/events")
async def events_create(
    kind: str = Form(...),
    title: str = Form(...),
    body: str = Form(""),
    severity: int = Form(...),
    domains: str = Form(""),
    is_global: bool = Form(False),
    penalty: str = Form(...),
    expires_at: str = Form(...),
    decay_days: int = Form(...),
    operator: str = Depends(require_operator),
    services_client: ServicesClient = Depends(get_services_client),
) -> RedirectResponse:
    domain_tuple = tuple(d.strip() for d in domains.split(",") if d.strip())
    payload: dict[str, Any] = {
        "kind": kind,
        "title": title,
        "body": body,
        "severity": severity,
        "domains": list(domain_tuple),
        "is_global": is_global,
        "penalty": penalty,
        "expires_at": expires_at,
        "decay_days": decay_days,
        "operator": operator,
    }
    await services_client.create_event(payload)
    return RedirectResponse(url="/events", status_code=303)


@router.post("/events/{event_id}/retract")
async def events_retract(
    event_id: UUID,
    reason: str = Form(...),
    operator: str = Depends(require_operator),
    services_client: ServicesClient = Depends(get_services_client),
) -> RedirectResponse:
    await services_client.retract_event(event_id, operator=operator, reason=reason)
    return RedirectResponse(url="/events", status_code=303)


@router.get("/scores")
async def scores_get(
    request: Request,
    user_ref: str | None = Query(None),
    operator: str = Depends(require_operator),
    services_client: ServicesClient = Depends(get_services_client),
) -> HTMLResponse:
    searched = bool(user_ref)
    score_data: dict[str, Any] | None = None
    events: list[dict[str, Any]] = []
    if searched and user_ref is not None:
        result = await services_client.score(user_ref)
        if result is not None:
            score_data = result.get("score")
            events = result.get("events", [])
    return _templates.TemplateResponse(
        request,
        "score.html",
        {
            "operator": operator,
            "user_ref": user_ref,
            "searched": searched,
            "score": score_data,
            "events": events,
        },
    )


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ConsoleUpstreamError)
    async def _upstream_error_handler(
        request: Request, exc: ConsoleUpstreamError
    ) -> JSONResponse:
        # An upstream failure here is an operator-facing incident, not a
        # secret -- but the detail is upstream response text, never a token
        # (the clients never put a token in a body they'd echo back).
        return JSONResponse(
            status_code=502,
            content={"error": {"code": "upstream_error", "message": exc.detail}},
        )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    cfg: ConsoleConfig = app.state.config
    # getattr guards let a caller (tests, a local harness) pre-wire fakes
    # before startup without being overwritten -- same convention as
    # imageshield.http.app and imageshield.fetcher.app. One httpx.AsyncClient
    # per upstream, since the two live at different base URLs with different
    # tokens.
    if getattr(app.state, "services_client", None) is None:
        app.state._services_http_client = httpx.AsyncClient(timeout=30.0)
        app.state.services_client = ServicesClient(
            app.state._services_http_client,
            base_url=cfg.services_base_url,
            service_token=cfg.service_token,
            admin_service_token=cfg.admin_service_token,
        )
    if getattr(app.state, "fetcher_client", None) is None:
        app.state._fetcher_http_client = httpx.AsyncClient(timeout=30.0)
        app.state.fetcher_client = FetcherClient(
            app.state._fetcher_http_client,
            base_url=cfg.fetcher_base_url,
            token=cfg.fetcher_token,
        )
    log.info("console.started")
    try:
        yield
    finally:
        for attr in ("_services_http_client", "_fetcher_http_client"):
            client = getattr(app.state, attr, None)
            if client is not None:
                await client.aclose()


def create_app(config: ConsoleConfig | None = None) -> FastAPI:
    cfg = config if config is not None else load_console_config()
    app = FastAPI(
        title="ImageShield Console",
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
