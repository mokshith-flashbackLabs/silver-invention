"""Row models for the search domain (seeds, runs, matches).

Frozen pydantic models mirroring the table shapes — the runtime half of the
typed-identifier discipline for everything the store reads back out of
Postgres (CLAUDE.md §10).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from imageshield.types import ProviderId, UserRef

RunStatus = Literal["queued", "running", "completed"]

SEED_KINDS = ("enrolment", "user_supplied", "public_profile")


class SeedRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    seed_id: UUID
    user_ref: UserRef
    seed_kind: str
    source_object_uri: str
    status: str
    created_at: datetime


class RunRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: UUID
    seed_id: UUID
    user_ref: UserRef
    status: RunStatus
    providers_attempted: tuple[str, ...]
    providers_succeeded: tuple[str, ...]
    matches_found: int
    started_at: datetime
    completed_at: datetime | None


class ClaimedRun(BaseModel):
    """What the worker needs to execute a run — re-read from Postgres on
    claim; the stored row wins over anything the queue message carried."""

    model_config = ConfigDict(frozen=True)

    run_id: UUID
    user_ref: UserRef
    seed_url: str
    providers_attempted: tuple[ProviderId, ...]


class ProviderDescriptor(BaseModel):
    """The three facts record_matches needs about the provider that produced
    a batch of matches — carried by the adapter, not looked up per row."""

    model_config = ConfigDict(frozen=True)

    provider_id: ProviderId
    score_kind: Literal["numeric", "categorical"]
    score_version: str


class MatchRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    match_id: UUID
    run_id: UUID
    provider_id: str
    image_url: str
    page_url: str | None
    score_kind: str
    provider_score: Decimal | None
    provider_category: str | None
    query_quality: str | None
    band: str
    created_at: datetime
