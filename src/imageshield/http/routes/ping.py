"""Placeholder authenticated routes — no feature code in Phase 1.

They exist so the auth chain and the ``extra='forbid'`` 422 behaviour are
provable end-to-end before any feature endpoint lands, and give the proxy an
authed connectivity check. The routers carry the auth dependencies at router
level so every route added to them is guarded structurally; Phase 5's CI gate
walks ``app.routes`` and asserts the dependency is present.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from imageshield.http.auth import require_admin_service_token, require_service_token
from imageshield.http.models import PingRequest, PingResponse

v1_router = APIRouter(prefix="/v1", dependencies=[Depends(require_service_token)])

admin_router = APIRouter(
    # /v1/admin, not /admin: one admin prefix for the whole surface, matching
    # the step-8 provider routes and PROXY_INTEGRATION.md's long-standing
    # `POST /v1/admin/backfill`. Two prefixes would mean step 9's route-auth CI
    # gate has to know about both, and a gate that covers one of two admin
    # surfaces is worse than no gate, because it reads as coverage.
    prefix="/v1/admin",
    dependencies=[Depends(require_service_token), Depends(require_admin_service_token)],
)


@v1_router.get("/ping", response_model=PingResponse)
async def ping() -> PingResponse:
    return PingResponse(ok=True)


@v1_router.post("/ping", response_model=PingResponse)
async def ping_echo(body: PingRequest) -> PingResponse:
    return PingResponse(ok=True, message=body.message)


@admin_router.get("/ping", response_model=PingResponse)
async def admin_ping() -> PingResponse:
    return PingResponse(ok=True)
