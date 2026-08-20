"""Shared pydantic models for the HTTP surface.

Every inbound request body extends :class:`ServiceModel` —
``extra='forbid'`` makes an unknown field a 422, which is the runtime half of
the typed-identifier discipline (CLAUDE.md §10).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from imageshield.enrolment.models import SENTINEL_CONSENT_REF
from imageshield.search.feedback import FeedbackSignal
from imageshield.types import UserRef


class ServiceModel(BaseModel):
    """Base for all inbound request bodies. Unknown fields -> 422."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    db: Literal["ok", "degraded"]


class ReadyResponse(BaseModel):
    """Readiness. Unlike HealthResponse this is allowed to be a 503 — it gates
    a deploy, and a deploy must not succeed into a broken svc contract."""

    status: str
    version: str
    db: str
    problems: list[str] = []


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
    """All three fields are semantically required; they are Optional here only
    so their absence maps to the contract's 400 (presigned_urls_missing /
    subject_eligibility_required) rather than pydantic's 422 — the step-3 and
    step-8 specs pin the status codes."""

    reference_put_url: str | None = None
    audit_put_urls: list[str] | None = None
    # Computed by the proxy as `age >= MIN_DISCOVERY_AGE` against a DOB this
    # service never sees. NO DEFAULT, and the absence is a 400: defaulting true
    # scans a minor, defaulting false silently breaks adult monitoring. Both
    # fail quietly, which is exactly why the field is mandatory rather than
    # inferred (step-8 brief).
    subject_is_adult: bool | None = None
    # Consent evidence, supplied by the proxy. All three are required and none
    # is defaulted or inferred: this service holds a user_ref and a face
    # vector, and cannot determine who is required to sign — the proxy has the
    # persons table, the guardianship graph, and the DocuSeal webhook. Absent
    # -> 400 consent_required, same shape as the two fields above.
    consent_ref: UUID | None = None
    consent_document_sha256: str | None = None
    consent_signed_at: datetime | None = None

    @field_validator("consent_ref")
    @classmethod
    def _not_the_sentinel(cls, value: UUID | None) -> UUID | None:
        if value == SENTINEL_CONSENT_REF:
            raise ValueError(
                "consent_ref is the reserved pre-consent sentinel; it is a migration"
                " artifact and must never be issued by the proxy"
            )
        return value

    @field_validator("consent_document_sha256")
    @classmethod
    def _not_blank(cls, value: str | None) -> str | None:
        # NOT NULL alone permits '' — the same defect 0007's consent_basis
        # CHECK exists to close. A blank hash proves nothing about *what* was
        # agreed to, which is the only reason the column exists.
        if value is not None and not value.strip():
            raise ValueError("consent_document_sha256 must not be blank")
        return value

    @field_validator("consent_signed_at")
    @classmethod
    def _not_in_the_future(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        # A naive timestamp is read as UTC rather than rejected: ISO 8601
        # without an offset is common, and guessing local time here would be
        # the actual error.
        moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        if moment > datetime.now(UTC):
            raise ValueError("consent_signed_at is in the future")
        return value


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
    # So the proxy can reconcile its own consent record against what an
    # enrolment was actually bound to. None until an enrolment exists — a
    # session that never enrolled has no consent to report.
    consent_ref: UUID | None = None


class SubjectResponse(BaseModel):
    """What the proxy can ask about a subject. Deliberately two fields: this is
    the eligibility answer, not a user record — we have no user model."""

    discovery_eligible: bool
    eligibility_reason: Literal["adult", "minor_discovery_deferred"]


class FloorsResponse(BaseModel):
    """The floors both repos carry, published so neither has to guess.

    Today `ATTRIBUTION_MAX_CANDIDATES` on the proxy side *documents* our floor
    and enforces nothing, and `MIN_DISCOVERY_AGE` is carried independently in
    both repos — so if v2 moves it, `subject_is_adult` means something different
    on each side of the boundary and nothing detects that. The proxy asserts
    against this at boot and refuses to start on a mismatch, which turns a
    silent divergence into a failed deploy.

    Every field is read from :class:`Config` at request time, never from a
    separate constant — a constant here is a second copy that lies the moment
    somebody edits one and not the other, which is the failure this endpoint
    exists to prevent.
    """

    min_discovery_age: int
    min_enrolment_age: int
    attribution_max_candidates: int
    # A decimal crosses as a STRING, matching GET /v1/admin/providers/health.
    # A float round-trip is exactly the drift the NUMERIC columns exist to
    # avoid, and the proxy compares this for equality.
    attribution_match_threshold: str


class SeedCreateRequest(ServiceModel):
    user_ref: UserRef
    seed_kind: Literal["enrolment", "user_supplied", "public_profile"]
    # An OPAQUE DURABLE reference to the proxy's object — an object key, not a
    # URL. The validator below is INVERTED from what it was: it used to *require*
    # https://, which is how a presigned GET came to be stored on a row that
    # outlives it by years.
    source_object_ref: str

    @field_validator("source_object_ref")
    @classmethod
    def _not_a_url(cls, value: str) -> str:
        """Refuse anything that looks like a fetchable, expiring URL.

        A presigned URL arriving here is the exact defect this column was
        renamed to fix — it works for a week and then 403s forever, and the
        failure surfaces as a provider outage. Silently accepting one would
        reintroduce the bug with a new column name, so it fails loudly.
        """
        if not value.strip():
            raise ValueError("source_object_ref must not be blank")
        if urlsplit(value).scheme.lower() in {"http", "https"}:
            raise ValueError(
                "source_object_ref must be an opaque durable reference, not a URL."
                " A presigned URL expires (SigV4 caps at 7 days) and this column"
                " does not; pass the object key and supply seed_url per search run."
            )
        if "x-amz-signature" in value.lower():
            raise ValueError(
                "source_object_ref carries a presigned signature — that is a"
                " credential, not an identifier"
            )
        return value


class SeedCreateResponse(BaseModel):
    seed_id: UUID


class SearchCreateRequest(ServiceModel):
    user_ref: UserRef
    seed_id: UUID
    providers: list[str] | None = None  # default: all enabled providers
    # A freshly-minted presigned GET, per run, ≥15-minute TTL. Optional here
    # only so its absence maps to 400 seed_url_required rather than pydantic's
    # 422 — there is no default and no fallback to the seed. Falling back is
    # precisely the bug: the seed holds a durable ref that no provider can
    # fetch, so substituting it would dispatch a search that cannot work and
    # report it as a provider failure.
    seed_url: str | None = None

    @field_validator("seed_url")
    @classmethod
    def _https_only(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = urlsplit(value)
        # https only: a third-party provider fetches this over the public
        # internet, and the object behind it is a photograph of the user's face.
        if parts.scheme.lower() != "https" or not parts.netloc:
            raise ValueError("seed_url must be an absolute https:// URL")
        return value


class SearchCreateResponse(BaseModel):
    run_id: UUID


class SearchRunStatusResponse(BaseModel):
    # 'refused' means the subject stopped being eligible for discovery between
    # this run being enqueued and a worker claiming it. It must stay distinct
    # from 'completed': the proxy has to be able to tell a user "we did not
    # search" rather than "we searched and found nothing" (INVARIANTS #8b).
    status: Literal["queued", "running", "completed", "refused"]
    providers_attempted: list[str]
    # MUST stay distinguishable from providers_attempted: a silent provider
    # outage must never look identical to "nothing found". Step 8 adds three
    # more ways to be attempted-but-not-succeeded — kill switch, open breaker,
    # exhausted budget — which is another reason the pair has to stay separate.
    providers_succeeded: list[str]
    matches_found: int
    # Tiering must never be silent (step 8). The proxy needs both of these to
    # tell a user their real monitoring cadence: someone on 'dormant' who
    # believes they are scanned weekly is being misled about a safety product.
    scan_tier: Literal["new", "standard", "relaxed", "dormant", "priority"]
    # None until the first completed scan sets it.
    next_scan_after: datetime | None


class AttestationItem(BaseModel):
    provider_id: str
    score_kind: Literal["numeric", "categorical"]
    # Presentation-layer float; the DB keeps the exact NUMERIC and
    # raw_response keeps the verbatim provider value.
    provider_score: float | None
    provider_category: str | None
    query_quality: str | None
    score_version: str
    first_confirmed_at: datetime
    last_confirmed_at: datetime
    confirm_count: int
    band: str
    calibration_version: str | None


class InfringementItem(BaseModel):
    infringement_id: UUID
    page_url: str
    # NO image_url. It was here and was REMOVED, deliberately: this is a
    # user-facing list read, and it was handing any caller a direct link to the
    # infringing image. Rendering a report list needs the domain, the dates, the
    # band and the status — not a way to load the picture. Showing the image at
    # all is a crop, blurred by default, behind its own gated call, and the crop
    # fetcher is not built. The column survives on `infringements`; it is
    # evidence. It just does not travel on this response.
    keyed_on: Literal["page_url", "image_url"]
    first_seen_at: datetime
    last_seen_at: datetime
    seen_count: int
    band: str
    status: str
    band_reason: str | None
    # Set false only by the recheck loop, and only on a 404/410. A dead URL is
    # the one unambiguously good thing this product can tell someone in v1, and
    # the proxy needs both fields to say it honestly: `url_alive=false` with a
    # recent `last_checked_at` is "this came down", while a stale
    # `last_checked_at` means we have not looked lately and should not claim.
    url_alive: bool
    last_checked_at: datetime | None
    # Agreement signal, not a hit count: three independent providers agreeing
    # is meaningfully different from one (CLAUDE.md §7.4).
    provider_count: int
    attestations: list[AttestationItem]


class InfringementsResponse(BaseModel):
    infringements: list[InfringementItem]


class FeedbackRequest(ServiceModel):
    user_ref: UserRef
    # Closed vocabulary, taken from ``search/feedback.py`` rather than restated:
    # the signal set and the signal → status mapping are one fact, and two
    # copies of it drift. 'uncertain' is a real answer and is kept distinct
    # (someone who looked and could not tell has told us something);
    # 'authorised' is "this is me, and it is authorised" and terminates the hit.
    signal: FeedbackSignal


class AttributeRequest(ServiceModel):
    photo_ref: str  # the proxy's photo_id. Opaque to us — never dereferenced.
    requested_by: UserRef
    # Enrolled people who MAY be in this photo. The search cannot be restricted
    # to them (the API takes no candidate set), so this is the list every match
    # is filtered against afterwards — see attribution/resolve.py.
    candidate_refs: list[UserRef]
    presigned_get_url: str

    @field_validator("photo_ref")
    @classmethod
    def _ref_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("photo_ref must not be blank")
        return value

    @field_validator("presigned_get_url")
    @classmethod
    def _https_only(cls, value: str) -> str:
        parts = urlsplit(value)
        # https only. The object behind it is a photograph of someone's face,
        # and it crosses the public internet to reach Rekognition.
        if parts.scheme.lower() != "https" or not parts.netloc:
            raise ValueError("presigned_get_url must be an absolute https:// URL")
        return value

    @field_validator("candidate_refs")
    @classmethod
    def _at_least_one_candidate(cls, value: list[UserRef]) -> list[UserRef]:
        # Zero candidates cannot attribute anything, so the whole call is a
        # no-op that still costs a DetectFaces and N searches. Refuse it rather
        # than bill for a result that is empty by construction.
        if not value:
            raise ValueError("candidate_refs must name at least one enrolled person")
        return value


class AttributedFaceItem(BaseModel):
    face_index: int
    bbox: dict[str, float]
    # "This region is a face". NOT the same quantity as match_score, and kept
    # in a separate field so a confident detection of a stranger can never read
    # as a confident identification.
    detect_confidence: float
    # None is the COMMON case: most faces in most photos belong to people who
    # are not enrolled. Not an error, and not something to render as a failure.
    resolved_user_ref: UUID | None
    match_score: float | None


class RegisteredSeedItem(BaseModel):
    user_ref: UUID
    seed_id: UUID


class AttributeResponse(BaseModel):
    run_id: UUID
    faces: list[AttributedFaceItem]
    seeds_registered: list[RegisteredSeedItem]


class FeedbackResponse(BaseModel):
    """The infringement's status after the write — unchanged for 'uncertain'."""

    status: str


class ProviderReasonRequest(ServiceModel):
    """A reason is mandatory on every admin write.

    Not paperwork: ``providers.enabled = false`` with no recorded reason is the
    state where nobody remembers whether the provider is off because of a
    billing surprise, a vendor breach, or a test somebody forgot to undo — and
    the difference decides whether turning it back on is safe.
    """

    reason: str = Field(min_length=3, max_length=500)


class ProviderDisableRequest(ProviderReasonRequest):
    """Same shape as its parent, named separately because the step-8 contract
    pins ``{ reason }`` on ``disable`` specifically."""


class ProviderAdminResponse(BaseModel):
    provider_id: str
    enabled: bool | None = None
    breaker_state: str | None = None


class ProviderHealthItem(BaseModel):
    provider_id: str
    enabled: bool
    breaker_state: str
    breaker_reason: str | None
    call_count: int
    # Money crosses this boundary as a decimal STRING, never a JSON number: a
    # float round-trip is exactly the drift the Decimal columns exist to avoid.
    cost_usd: str
    daily_budget_usd: str | None
    monthly_budget_usd: str | None
    month_to_date_cost_usd: str
    budget_headroom_usd: str | None
    # None when the window held no calls — not the same fact as a 0.0 rate.
    success_rate: float | None
    window_call_count: int
    successful_calls_24h: int
    latency_p50_ms: int | None
    latency_p99_ms: int | None
    alarms: list[dict[str, str]]


class ProviderHealthResponse(BaseModel):
    as_of: datetime
    window_hours: int
    providers: list[ProviderHealthItem]


class ThreatEventCreateRequest(ServiceModel):
    """Console input for a new threat event. ``penalty`` crosses as a decimal
    STRING (pydantic coerces to ``Decimal``), same convention as every other
    money-shaped value at this boundary — a float round-trip is exactly the
    drift ``NUMERIC(5,2)`` exists to avoid.
    """

    kind: Literal["leak", "deepfake_wave", "platform_incident", "other"]
    title: str = Field(min_length=1)
    body: str = ""
    severity: int = Field(ge=1, le=5)
    domains: tuple[str, ...] = ()
    is_global: bool = False
    penalty: Decimal = Field(gt=0)
    expires_at: datetime
    decay_days: int = Field(gt=0)
    operator: str = Field(min_length=1)

    @model_validator(mode="after")
    def _domains_or_global(self) -> ThreatEventCreateRequest:
        # Mirrors 0022's own CHECK (is_global OR cardinality(domains) > 0) so
        # the obviously-wrong request fails as a 422 here rather than as an
        # opaque database constraint violation.
        if not self.is_global and not self.domains:
            raise ValueError(
                "domains must name at least one domain unless is_global is true"
            )
        return self


class ThreatEventCreateResponse(BaseModel):
    event_id: UUID
    matched_count: int


class ThreatEventRetractRequest(ServiceModel):
    operator: str = Field(min_length=1)
    reason: str = Field(min_length=3, max_length=500)


class ThreatEventRetractResponse(BaseModel):
    event_id: UUID
    matched_count: int
    status: Literal["retracted"] = "retracted"


class ThreatEventItem(BaseModel):
    event_id: UUID
    kind: str
    title: str
    body: str
    severity: int
    domains: list[str]
    is_global: bool
    # Decimal-as-string, same convention as penalty above and as
    # ProviderHealthItem's cost fields.
    penalty: str
    starts_at: datetime
    expires_at: datetime
    decay_days: int
    status: str
    created_by: str
    created_at: datetime
    updated_at: datetime


class ThreatEventsResponse(BaseModel):
    events: list[ThreatEventItem]
