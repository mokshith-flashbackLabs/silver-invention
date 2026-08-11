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

from imageshield.search.cadence import ScanTier
from imageshield.types import ProviderId, UserRef

# 'refused' is a dispatch-time eligibility refusal (step 8): the subject was
# eligible when the run was created and had stopped being by the time a worker
# claimed it. Distinct from 'completed' on purpose — a completed run with zero
# results reads as "we looked and found nothing", which about a search that never
# ran is a false reassurance (INVARIANTS #8b).
RunStatus = Literal["queued", "running", "completed", "refused"]
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
    # An OPAQUE DURABLE reference to the proxy's S3 object — an object key, not
    # a URL. It used to hold a presigned GET, which is a credential with at most
    # 7 days of life (SigV4's cap): the seed worked in week one and 403'd forever
    # after, presenting as "the provider is failing" rather than "our URLs
    # expired". The expiring half now lives on the run (``ClaimedRun.seed_url``),
    # minted fresh by the proxy per search.
    source_object_ref: str
    status: str
    created_at: datetime
    # Step 8 cadence state. Defaulted to the migration's own defaults so a
    # caller constructing a SeedRow in a test doesn't have to know about
    # cadence to talk about a seed.
    scan_tier: ScanTier = "standard"
    next_scan_after: datetime | None = None
    consecutive_empty_scans: int = 0


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
    # Carried on the run so `GET /v1/search/runs/{run_id}` can state the seed's
    # real monitoring cadence. Tiering must never be silent: someone on
    # 'dormant' who believes they are scanned weekly is being misled about a
    # safety product.
    scan_tier: ScanTier = "standard"
    next_scan_after: datetime | None = None


class ClaimedRun(BaseModel):
    """What the worker needs to execute a run — re-read from Postgres on
    claim; the stored row wins over anything the queue message carried."""

    model_config = ConfigDict(frozen=True)

    run_id: UUID
    seed_id: UUID
    user_ref: UserRef
    # Read from ``search_runs``, NEVER from the seed. The proxy mints it at
    # enqueue with a ≥15-minute TTL; if it expires before dispatch the providers
    # fail normally, the run completes with an empty ``providers_succeeded``,
    # and cadence is unchanged (``should_retier``). The proxy re-enqueues with a
    # fresh one — there is no refresh path here, and adding one would require S3
    # credentials this service deliberately does not hold.
    seed_url: str
    providers_attempted: tuple[ProviderId, ...]
    # Re-read at claim time, not trusted from run creation. The route checked it
    # before creating the run, but `subjects.discovery_eligible` is mutable (a
    # DOB correction at re-enrolment writes it) and a queued backlog or a stale
    # claim can put minutes between the two. False here refuses the run rather
    # than dispatching against a subject who may no longer be searched.
    discovery_eligible: bool


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
    # Written only by the recheck worker, and only a 404/410 sets url_alive
    # false. A timeout, a 5xx or a 403 leaves both alone: telling a victim
    # their problem is fixed because a site was briefly unreachable is the
    # wrong error to make, and the asymmetry runs the same direction as
    # everywhere else here.
    url_alive: bool = True
    last_checked_at: datetime | None = None
    attestations: tuple[AttestationRow, ...]
