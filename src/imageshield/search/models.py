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
ScoreKind = Literal["numeric", "categorical"]
# Which URL the infringement's url_hash was computed over: the page a
# provider reported, or the image itself when it reported no page.
KeyedOn = Literal["page_url", "image_url"]

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
    """The three facts record_infringements needs about the provider that
    produced a batch of matches — carried by the adapter, not looked up per
    row."""

    model_config = ConfigDict(frozen=True)

    provider_id: ProviderId
    score_kind: ScoreKind
    score_version: str


class AttestationRow(BaseModel):
    """One provider's observation of an infringement. Rescans update this row
    rather than appending, so the counters are the history."""

    model_config = ConfigDict(frozen=True)

    provider_id: str
    score_kind: ScoreKind
    provider_score: Decimal | None  # RAW and provider-native. Never rescaled.
    provider_category: str | None
    query_quality: str | None
    score_version: str
    first_confirmed_at: datetime
    last_confirmed_at: datetime
    confirm_count: int
    band: str
    calibration_version: str | None


class InfringementRow(BaseModel):
    """The thing a user acts on: one page, with every provider that attested
    to it. Never shared across users — see the UNIQUE (user_ref, url_hash)."""

    model_config = ConfigDict(frozen=True)

    infringement_id: UUID
    page_url: str
    image_url: str | None
    keyed_on: KeyedOn
    first_seen_at: datetime
    last_seen_at: datetime
    seen_count: int
    band: str
    status: str
    band_reason: str | None
    attestations: tuple[AttestationRow, ...]
