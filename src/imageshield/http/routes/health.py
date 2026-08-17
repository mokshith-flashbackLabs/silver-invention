"""Health check — Postgres reachability only.

Unauthenticated (one of two routes allowed to be — see /readyz below).
Deliberately terse per the build spec: status, version, db ok/degraded — no
exception detail, no dependency map, nothing that helps an attacker profile
the deployment. Always 200, unlike Flashback's 503-on-degraded: the proxy
reads the body, and a degraded DB must not look like "service absent" to its
retry logic.

/readyz differs deliberately: it returns 503 when the db is unreachable or the
`svc` contract is broken. /health answers "is the process up"; /readyz answers
"may a deploy proceed" — and those are different questions with different
right answers, so the always-200 rule above does not apply to it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status

from imageshield.config import APP_VERSION
from imageshield.http.deps import DbCheck, get_db_check, get_db_pool
from imageshield.http.models import HealthResponse, ReadyResponse
from imageshield.http.svc_contract import check_svc_contract

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health(db_check: DbCheck = Depends(get_db_check)) -> HealthResponse:
    db: str
    try:
        await db_check()
        db = "ok"
    except Exception:  # health check must not propagate
        db = "degraded"
    return HealthResponse(
        status="ok" if db == "ok" else "degraded",
        version=APP_VERSION,
        db="ok" if db == "ok" else "degraded",
    )


@router.get("/readyz", response_model=ReadyResponse)
async def readyz(request: Request, response: Response) -> ReadyResponse:
    """Deploy gate: DB reachable AND the four svc views present and correct.

    503 rather than /health's always-200. /health tells the proxy whether we are
    answering; this tells a deploy whether it may proceed, and those are
    different questions with different right answers.

    ``get_db_pool`` is called directly here rather than via ``Depends``:
    FastAPI resolves ``Depends`` parameters before this function body starts,
    so a pool that is not yet wired (``app.state.db_pool`` absent — exactly the
    early-boot window this endpoint exists to signal) would raise there,
    outside the try/except below, and crash the probe instead of answering
    503. Calling it inline keeps that failure inside the block built to catch
    it — the same reason /health's own db_check() call above is inside its
    try, not injected pre-resolved.
    """
    try:
        db_pool = get_db_pool(request)
        async with db_pool.connection() as conn:
            problems = await check_svc_contract(conn)
    except Exception:  # readiness must not propagate
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadyResponse(
            status="not_ready", version=APP_VERSION, db="degraded", problems=[]
        )

    if problems:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadyResponse(
            status="not_ready", version=APP_VERSION, db="ok", problems=problems
        )
    return ReadyResponse(status="ready", version=APP_VERSION, db="ok", problems=[])
