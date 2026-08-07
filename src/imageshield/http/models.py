"""Shared pydantic models for the HTTP surface.

Every inbound request body extends :class:`ServiceModel` —
``extra='forbid'`` makes an unknown field a 422, which is the runtime half of
the typed-identifier discipline (CLAUDE.md §10).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

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
    # True iff IndexFaces accepted the ReferenceImage and the enrolments row
    # was written (step 4). 'passed' + enrolled=False + reason tells the proxy
    # to start a FRESH liveness session.
    enrolled: bool = False
    reason: Literal["quality_rejected"] | None = None


class LivenessStatusResponse(BaseModel):
    status: Literal["created", "pending", "passed", "failed", "expired", "consumed"]
    confidence: float | None
    enrolled: bool = False


class SeedCreateRequest(ServiceModel):
    user_ref: UserRef
    seed_kind: Literal["enrolment", "user_supplied", "public_profile"]
    source_object_uri: str  # proxy's S3, http(s) presigned GET — never s3://

    @field_validator("source_object_uri")
    @classmethod
    def _http_only(cls, value: str) -> str:
        if not value.startswith(("https://", "http://")):
            raise ValueError(
                "must be an http(s) URL (presigned GET) — this service holds no S3 credentials"
            )
        return value


class SeedCreateResponse(BaseModel):
    seed_id: UUID


class SearchCreateRequest(ServiceModel):
    user_ref: UserRef
    seed_id: UUID
    providers: list[str] | None = None  # default: all enabled providers


class SearchCreateResponse(BaseModel):
    run_id: UUID


class SearchRunStatusResponse(BaseModel):
    status: Literal["queued", "running", "completed"]
    providers_attempted: list[str]
    # MUST stay distinguishable from providers_attempted: a silent provider
    # outage must never look identical to "nothing found".
    providers_succeeded: list[str]
    matches_found: int


class SearchMatchItem(BaseModel):
    match_id: UUID
    run_id: UUID
    provider_id: str
    image_url: str
    page_url: str | None
    score_kind: Literal["numeric", "categorical"]
    # Presentation-layer float; the DB keeps the exact NUMERIC and
    # raw_response keeps the verbatim provider value.
    provider_score: float | None
    provider_category: str | None
    query_quality: str | None
    band: str
    created_at: datetime


class SearchMatchesResponse(BaseModel):
    matches: list[SearchMatchItem]
