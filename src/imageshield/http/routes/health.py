"""Health check — Postgres reachability only.

Unauthenticated (the one route allowed to be). Deliberately terse per the
build spec: status, version, db ok/degraded — no exception detail, no
dependency map, nothing that helps an attacker profile the deployment.
Always 200, unlike Flashback's 503-on-degraded: the proxy reads the body,
and a degraded DB must not look like "service absent" to its retry logic.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from imageshield.config import APP_VERSION
from imageshield.http.deps import DbCheck, get_db_check
from imageshield.http.models import HealthResponse

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
