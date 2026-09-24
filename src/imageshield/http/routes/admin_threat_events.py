"""Threat events — operator CRUD (Task 14).

Same posture as ``admin_providers.py``: both tokens required at router level,
so a route added to this file is guarded structurally. A write can name
**many** subjects at once — a domain leak or a global platform incident can
match hundreds of ``user_ref``s in one call — and ``matched_count`` on the
response tells the operator how many, without anything here recomputing a
score for any of them.
"""

from __future__ import annotations

from uuid import UUID

import structlog
from fastapi import APIRouter, Depends

from imageshield.http.auth import require_admin_service_token, require_service_token
from imageshield.http.deps import get_threat_store
from imageshield.http.errors import ServiceError
from imageshield.http.models import (
    ThreatEventCreateRequest,
    ThreatEventCreateResponse,
    ThreatEventItem,
    ThreatEventRetractRequest,
    ThreatEventRetractResponse,
    ThreatEventsResponse,
)
from imageshield.threats.store import ThreatStore

log = structlog.get_logger("imageshield.threats")

router = APIRouter(
    prefix="/v1/admin/threat-events",
    dependencies=[Depends(require_service_token), Depends(require_admin_service_token)],
)


@router.post("", status_code=201)
async def create_threat_event(
    body: ThreatEventCreateRequest,
    store: ThreatStore = Depends(get_threat_store),
) -> ThreatEventCreateResponse:
    event_id, matched = await store.create_event(
        kind=body.kind,
        title=body.title,
        body=body.body,
        severity=body.severity,
        domains=body.domains,
        is_global=body.is_global,
        expires_at=body.expires_at,
        decay_days=body.decay_days,
        operator=body.operator,
    )
    log.info(
        "threat_event.created_via_admin",
        event_id=str(event_id),
        operator=body.operator,
        matched_count=len(matched),
    )
    return ThreatEventCreateResponse(event_id=event_id, matched_count=len(matched))


@router.post("/{event_id}/retract")
async def retract_threat_event(
    event_id: UUID,
    body: ThreatEventRetractRequest,
    store: ThreatStore = Depends(get_threat_store),
) -> ThreatEventRetractResponse:
    matched = await store.retract_event(event_id, operator=body.operator, reason=body.reason)
    if matched is None:
        raise ServiceError(
            404,
            "threat_event_not_found",
            "No active threat event with this id.",
            retryable=False,
        )
    log.info(
        "threat_event.retracted_via_admin",
        event_id=str(event_id),
        operator=body.operator,
        matched_count=len(matched),
    )
    return ThreatEventRetractResponse(event_id=event_id, matched_count=len(matched))


@router.get("")
async def list_threat_events(
    store: ThreatStore = Depends(get_threat_store),
) -> ThreatEventsResponse:
    rows = await store.list_events()
    return ThreatEventsResponse(
        events=[
            ThreatEventItem(
                event_id=row["event_id"],
                kind=row["kind"],
                title=row["title"],
                body=row["body"],
                severity=row["severity"],
                domains=list(row["domains"]),
                is_global=row["is_global"],
                starts_at=row["starts_at"],
                expires_at=row["expires_at"],
                decay_days=row["decay_days"],
                status=row["status"],
                created_by=row["created_by"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]
    )
