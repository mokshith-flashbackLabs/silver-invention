"""Shared pydantic models for the HTTP surface.

Every inbound request body extends :class:`ServiceModel` —
``extra='forbid'`` makes an unknown field a 422, which is the runtime half of
the typed-identifier discipline (CLAUDE.md §10).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from imageshield.types import UserRef


class ServiceModel(BaseModel):
    """Base for all inbound request bodies. Unknown fields -> 422."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    db: Literal["ok", "degraded"]


class PingRequest(ServiceModel):
    message: str | None = None


class PingResponse(BaseModel):
    ok: bool
    message: str | None = None


class LivenessSessionCreateRequest(ServiceModel):
    user_ref: UserRef


class LivenessSessionCreateResponse(BaseModel):
    session_id: UUID
    provider_session_id: str
    region: str
    expires_at: datetime


class LivenessResultRequest(ServiceModel):
    """Both URL fields are semantically required; they are Optional here only
    so their absence maps to the contract's 400 (presigned_urls_missing)
    rather than pydantic's 422 — the step-3 spec pins the status code."""

    reference_put_url: str | None = None
    audit_put_urls: list[str] | None = None


class LivenessResultResponse(BaseModel):
    status: Literal["passed", "failed"]
    confidence: float | None
    # Always False in step 3 — indexing is step 4 (CLAUDE.md §8).
    enrolled: bool = False


class LivenessStatusResponse(BaseModel):
    status: Literal["created", "pending", "passed", "failed", "expired", "consumed"]
    confidence: float | None
    enrolled: bool = False
