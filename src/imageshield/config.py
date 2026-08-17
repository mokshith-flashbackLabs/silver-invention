"""Environment-driven configuration.

Every tunable knob is read from environment variables — there are no config
files and no environment branches (build spec Phase 1 §3). Validation happens
once, when :func:`load_config` runs at process boot; a missing or malformed
key is fatal and the process exits non-zero with a message naming the key.

Follows the Flashback agent service pattern (AgentMeeMaw src/flashback/config.py:
``ConfigError``, sentinel-value rejection, ``.env.local`` autoload) but uses
pydantic-settings instead of hand-rolled ``_required()`` lookups, per the build
spec. Tests construct :class:`Config` directly with keyword overrides.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import AnyHttpUrl, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from imageshield.env import load_dotenv_local

APP_VERSION = "0.1.0"

# Placeholder values that must never pass for a secret (Flashback config.py:27).
SENTINEL_VALUES = {"changeme", "change-me", "replace-me", "test"}

_AWS_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
_POSTGRES_URL_RE = re.compile(r"^postgres(ql)?://.+")

# Hard ceiling on PROVIDER_CONFIG_CACHE_SECONDS. In code, not config: the
# whole value of the cap is that raising it costs a code change and a review.
_PROVIDER_CACHE_TTL_CAP_SECONDS = 30.0


class ConfigError(RuntimeError):
    """Raised when required environment configuration is missing or malformed."""


class Config(BaseSettings):
    model_config = SettingsConfigDict(frozen=True, extra="ignore", case_sensitive=False)

    environment: Literal["development", "test", "production"] = "production"
    http_host: str = "0.0.0.0"
    http_port: int = 8000
    db_pool_min_size: int = 1
    db_pool_max_size: int = 4

    database_url: str
    service_token: str
    admin_service_token: str

    aws_region: str

    # Renamed from REKOGNITION_COLLECTION_ID to match the deploy contract
    # (DEPLOY-DEV-HANDOFF §5). Hard rename, no alias: the old name is not in any
    # deployed environment, and two names for one value is the drift CLAUDE.md §9
    # warns about.
    identity_collection: str

    # The region the identity collection lives in. A Rekognition collection is
    # regional: enrol into one region, search another, and the second is empty.
    # Nothing errors — it reads as "no matches", which is the one wrong answer
    # this product must never give silently. Asserted equal to aws_region below.
    rekognition_region: str

    # ── Declared, validated, and NOT READ in v1 ───────────────────────────
    # Both exist so the deployed env block matches DEPLOY-DEV-HANDOFF §5
    # literally; a variable the operator sets that silently vanishes is worse
    # than one that is documented as inert.
    #
    # discovered_collection: `discovered-v1` and clustering are "specified, do
    # not build yet" (CLAUDE.md §6).
    discovered_collection: str
    # enrolment_collision_threshold: THERE IS NO COLLISION CHECK, deliberately.
    # Wiring this to one means a similarity score influencing enrolment, which is
    # invariant #1 ("identity never comes from a similarity score") and the
    # fragmentation bug the old system shipped. Read #1 and #1a before giving
    # this a reader.
    enrolment_collision_threshold: float

    # Threshold for provider face-search matching. DISTINCT from
    # face_match_threshold (enrolment) and attribution_match_threshold
    # (attribution) — one threshold per purpose, invariant #1b. Refused at
    # exactly 80: that is Rekognition's default, i.e. the value nobody chose.
    search_match_threshold: float

    # Concurrent in-flight attribution searches.
    attribution_max_inflight: int

    # 'stub' dispatches no provider call at all. In development this is the only
    # thing standing between a test run and billable Hive traffic — the dev key
    # is real, Hive has no sandbox, and its cost_per_call_usd is NULL so the
    # step-8 budget guard fails closed and caps nothing. Asserted below.
    search_provider: Literal["stub", "hive", "google"] = "stub"

    # Dev-only guard on collection size, so a runaway test cannot enrol
    # thousands of faces into a shared dev collection.
    dev_face_ceiling: int

    log_level: Literal["debug", "info", "warning", "error"] = "info"

    liveness_min_confidence: float
    face_match_threshold: float
    # Two ages, not one, and the split is load-bearing (step 8).
    #
    # MIN_ENROLMENT_AGE is who may enrol — consent, guardianship and household
    # seats all work for minors. MIN_DISCOVERY_AGE is who may be SEARCHED. They
    # were one number until step 8, which is why minors were blocked from
    # enrolling at all. Neither carries a default: an age floor that ships with
    # one is an age floor somebody can forget to set. The v1 values live in
    # .env.example and INVARIANTS #8, deliberately not repeated here — a number
    # quoted in a comment beside a required env var is a number that can drift.
    #
    # Discovery finds images resembling the seed and nudify sites alter real
    # photos, so for an enrolled minor a *successful* result is CSAM inside
    # this pipeline. CSAM screening and reporting are deferred until the
    # partner corpus connects; until they exist the correct behaviour is that
    # nothing looks.
    #
    # MIN_DISCOVERY_AGE drops in v2, and lowering it here does NOT enable
    # minor discovery on its own: the proxy sends a boolean and
    # ``imageshield.subjects.eligibility`` maps minor -> ineligible
    # unconditionally. That is deliberate — the change must cost a code review
    # alongside the config change, never config alone.
    min_enrolment_age: int
    min_discovery_age: int
    liveness_session_ttl_seconds: int
    liveness_max_attempts_24h: int
    # Measured, not guessed: ≈$0.015 per completed Face Liveness check
    # (devtools/harness README, verified 2026-08-05 against real Rekognition).
    # Config rather than a devtools note because it feeds the step-8 budget
    # logic. Defaulted: it is a billing fact, not a secret or an environment
    # difference.
    liveness_cost_per_check_usd: float = 0.015

    hive_api_key: str
    hive_base_url: str

    # Google Cloud Vision Web Detection (the second step-5 search provider).
    # The endpoint is a stable public constant, defaulted; the key is a secret
    # and required.
    google_vision_api_key: str
    google_vision_endpoint: str = "https://vision.googleapis.com/v1/images:annotate"

    # One HTTP timeout for provider search calls. Hive's synchronous
    # /api/v2/task/sync can run long (the harness used 120s and the old
    # lambda 60s); a provider hitting this surfaces as status='timeout' on
    # its provider_calls row — it never fails the run.
    provider_timeout_seconds: float = 120.0

    # How long provider_calls.raw_response is kept before the retention job
    # nulls it (the metadata row stays). Recalibrating over history needs
    # recent payloads, not all of them, and the JSONB is the unbounded part
    # of an otherwise run-bounded table.
    raw_response_retention_days: int = 90

    # Floor on eval set size before a calibration config may activate a
    # non-review band. 200 is a policy choice, not a statistical derivation:
    # it is the point below which a precision figure is too weak to justify
    # alarming a person without a human looking. Config rather than a
    # constant so tightening it is an ops change, but note that LOOSENING it
    # still cannot bypass the zero-lookalike refusal, which is unconditional.
    calibration_min_eval_items: int = 200

    # ── Step 8: circuit breaker ───────────────────────────────────────────
    # Consecutive failures that open a provider's breaker. Failure means
    # timeout, 5xx, connection error, or a malformed response. It does NOT
    # mean 429 (that is rate limiting, retried within bounds) and it does not
    # mean a 200 with zero matches — conflating "no matches" with failure
    # would open the breaker on the single most ordinary result this system
    # produces.
    provider_failure_threshold: int = 5
    # How long an open breaker waits before allowing ONE half-open probe.
    breaker_cooldown_seconds: int = 300
    # The doubling cap. Without it a provider down for a weekend ends up with
    # a cooldown longer than the outage, so recovery is never noticed.
    breaker_cooldown_max_seconds: int = 3600

    # ── Step 8: rate limiting ─────────────────────────────────────────────
    # Bounded retries on 429, honouring `retry-after`, with jitter. The old
    # lambda recursed on 429 with no attempt counter
    # (weeklyInfringementScanner.js:1148), so a persistently limited provider
    # recursed until the Lambda timed out. This is the counter.
    provider_max_retries: int = 3
    # Jitter as a fraction of the computed wait, so N workers retrying the
    # same rate-limited provider don't re-collide in lockstep.
    provider_retry_jitter_fraction: float = 0.25
    # Ceiling on any single 429 back-off, so a provider answering
    # `retry-after: 86400` cannot pin a worker for a day.
    provider_retry_max_wait_seconds: float = 30.0

    # ── Step 8: kill switches ─────────────────────────────────────────────
    # Provider rows (enabled, budgets, breaker state) are re-read at least
    # this often. Capped at 30s in the validator: a kill switch has to take
    # effect in seconds during an incident, and a cache is the one thing that
    # can silently make "disabled" mean "disabled in a while".
    provider_config_cache_seconds: float = 30.0

    # ── Step 8: adaptive cadence ──────────────────────────────────────────
    # The 4-10x lever. A user with no hits in six months does not need weekly
    # scans. Every number here is a cost/safety trade-off, so all of them are
    # config — cadence is a safety decision, not a growth lever (CLAUDE.md §1).
    scan_interval_standard_days: int = 7
    scan_interval_relaxed_days: int = 14
    scan_interval_dormant_days: int = 30
    # 'new' holds weekly for this many weeks after enrolment, then becomes
    # 'standard'. Same interval; the tier exists so the first month is never
    # relaxed by an empty-scan counter that has barely started.
    scan_new_tier_weeks: int = 4
    scan_relaxed_after_empty: int = 8
    scan_dormant_after_empty: int = 20
    # 'priority' is never demoted by the empty counter while a recent hit
    # stands. Adjudication is out of scope (CLAUDE.md §6), so v1 approximates
    # "any confirmed infringement in 90 days" as 13 consecutive empty weekly
    # scans (~91 days) since the last non-empty one. Replace with a query
    # against confirmed infringements when the review queue lands.
    scan_priority_release_after_empty: int = 13

    # ── Step 8: observability thresholds ──────────────────────────────────
    provider_spend_alarm_fraction: float = 0.80
    provider_success_rate_alarm: float = 0.90
    provider_alarm_window_hours: int = 1

    sqs_identity_index_url: str
    sqs_search_runs_url: str

    # Outbox relay (Task 4, src/imageshield/relay.py). Defaulted rather than
    # required — the HTTP process never reads these, only the relay does, and
    # they are safe to run with sensible defaults out of the box.
    outbox_poll_interval_seconds: float = 1.0
    outbox_batch_size: int = 50
    outbox_max_attempts: int = 8

    # ── Attribution (src/imageshield/attribution/) ────────────────────────
    # "Is this face that person" — a DIFFERENT quantity from
    # LIVENESS_MIN_CONFIDENCE ("is this a live human") and from
    # detect_confidence ("is this region a face"). One threshold per purpose,
    # from config, never a shared literal (INVARIANTS #1b).
    attribution_match_threshold: float = 92.0
    # MaxFaces on the search. At least two, enforced below and at boot.
    #
    # A single-candidate search silently loses coverage as the collection
    # grows: a stranger outranking the household member returns ONLY the
    # stranger, that match is discarded by the candidate filter, and the photo
    # never becomes a seed for its own owner. Nothing errors; the seed simply
    # never appears. The floor is the only thing standing between that and a
    # monitoring product that quietly finds less the more people enrol.
    attribution_max_candidates: int = 5

    # ── The recheck loop (src/imageshield/recheck/) ───────────────────────
    # How stale a definite check may get before the URL is probed again.
    recheck_interval_days: int = 7
    # Rows per pass. The pass is sequential and per-domain paced, so this is
    # also roughly "how long one pass takes" — keep it a size a single cycle
    # can actually finish.
    recheck_batch_size: int = 500
    recheck_poll_interval_seconds: float = 300.0
    # Matches the crop fetcher's posture (INVARIANTS #11): hard timeout, and a
    # redirect cap low enough that a chain cannot be used to walk somewhere.
    recheck_timeout_seconds: float = 5.0
    recheck_max_redirects: int = 2
    # Minimum gap between two probes of the SAME domain. Probing one site's 400
    # URLs in a burst gets the worker blocked and, more to the point, looks like
    # an attack from the far end — it is the traffic shape a scanner makes.
    # Nothing is imposed across different domains.
    recheck_per_domain_interval_seconds: float = 2.0

    # Requested via SERVICE_TOKEN_AUTH_DISABLED=1; only takes effect in
    # development — see :attr:`auth_disabled`.
    service_token_auth_disabled: bool = False

    @field_validator("database_url")
    @classmethod
    def _postgres_url(cls, value: str) -> str:
        if not _POSTGRES_URL_RE.match(value):
            raise ValueError("must be a postgres:// connection URL")
        return value

    @field_validator("service_token", "admin_service_token")
    @classmethod
    def _token(cls, value: str) -> str:
        if len(value) < 16:
            raise ValueError("must be at least 16 characters")
        if value.strip().lower() in SENTINEL_VALUES:
            raise ValueError("is still set to a placeholder")
        return value

    @field_validator("hive_api_key", "google_vision_api_key")
    @classmethod
    def _secret_not_placeholder(cls, value: str) -> str:
        if not value:
            raise ValueError("must not be empty")
        if value.strip().lower() in SENTINEL_VALUES:
            raise ValueError("is still set to a placeholder")
        return value

    @field_validator("aws_region", "rekognition_region")
    @classmethod
    def _region(cls, value: str) -> str:
        if not _AWS_REGION_RE.match(value):
            raise ValueError("must be an AWS region like ap-south-1")
        return value

    @field_validator("identity_collection", "discovered_collection")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator(
        "hive_base_url",
        "google_vision_endpoint",
        "sqs_identity_index_url",
        "sqs_search_runs_url",
    )
    @classmethod
    def _http_url(cls, value: str) -> str:
        AnyHttpUrl(value)
        return value

    @field_validator(
        "liveness_min_confidence",
        "face_match_threshold",
        "enrolment_collision_threshold",
        "search_match_threshold",
    )
    @classmethod
    def _confidence(cls, value: float) -> float:
        if not 0 <= value <= 100:
            raise ValueError("must be between 0 and 100")
        return value

    @field_validator("min_enrolment_age", "min_discovery_age")
    @classmethod
    def _age(cls, value: int) -> int:
        if not 0 <= value <= 130:
            raise ValueError("must be between 0 and 130")
        return value

    @field_validator(
        "liveness_session_ttl_seconds",
        "liveness_max_attempts_24h",
        "outbox_batch_size",
        "outbox_max_attempts",
        "raw_response_retention_days",
        "calibration_min_eval_items",
        "provider_failure_threshold",
        "breaker_cooldown_seconds",
        "breaker_cooldown_max_seconds",
        "provider_max_retries",
        "scan_interval_standard_days",
        "scan_interval_relaxed_days",
        "scan_interval_dormant_days",
        "scan_new_tier_weeks",
        "scan_relaxed_after_empty",
        "scan_dormant_after_empty",
        "scan_priority_release_after_empty",
        "provider_alarm_window_hours",
        "recheck_interval_days",
        "recheck_batch_size",
        "attribution_max_inflight",
        "dev_face_ceiling",
    )
    @classmethod
    def _positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("must be a positive integer")
        return value

    @field_validator(
        "outbox_poll_interval_seconds",
        "liveness_cost_per_check_usd",
        "provider_timeout_seconds",
        "provider_retry_max_wait_seconds",
        "provider_config_cache_seconds",
        "recheck_poll_interval_seconds",
        "recheck_timeout_seconds",
        "attribution_match_threshold",
    )
    @classmethod
    def _positive_float(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("must be a positive number")
        return value

    @field_validator("provider_retry_jitter_fraction")
    @classmethod
    def _fraction(cls, value: float) -> float:
        if not 0 <= value <= 1:
            raise ValueError("must be a fraction between 0 and 1")
        return value

    @field_validator("provider_spend_alarm_fraction", "provider_success_rate_alarm")
    @classmethod
    def _alarm_fraction(cls, value: float) -> float:
        if not 0 < value <= 1:
            raise ValueError("must be a fraction greater than 0 and at most 1")
        return value

    @field_validator("provider_config_cache_seconds")
    @classmethod
    def _cache_ttl_capped(cls, value: float) -> float:
        # "Never cache the enabled flag beyond 30s" (step-8 brief). A kill
        # switch that takes a minute to bite is not a kill switch, and the
        # only way to be sure of that is to refuse to boot with a longer TTL.
        if value > _PROVIDER_CACHE_TTL_CAP_SECONDS:
            raise ValueError(
                f"must not exceed {_PROVIDER_CACHE_TTL_CAP_SECONDS:g}s — a provider"
                " kill switch has to take effect within seconds"
            )
        return value

    @model_validator(mode="after")
    def _tokens_distinct(self) -> Config:
        if self.service_token == self.admin_service_token:
            raise ValueError(
                "SERVICE_TOKEN and ADMIN_SERVICE_TOKEN must differ — refusing to start"
            )
        return self

    @model_validator(mode="after")
    def _attribution_max_candidates_floor(self) -> Config:
        """At least two, refused at BOOT rather than at the call site.

        The failure a single candidate produces is silent: a stranger who
        outranks the household member is the only result, the candidate filter
        discards it, and the photo never becomes a seed for its own owner.
        Nothing raises, nothing is logged as wrong, and the coverage loss grows
        with the collection. A knob whose bad value is invisible has to be
        refused where it is set.
        """
        if self.attribution_max_candidates < 2:
            raise ValueError(
                "ATTRIBUTION_MAX_CANDIDATES must be >= 2 — a single-candidate"
                " search silently drops the real match whenever any stranger in"
                " the collection outranks it"
            )
        return self

    @model_validator(mode="after")
    def _ages_ordered(self) -> Config:
        # A discovery age BELOW the enrolment age would mean the search gate is
        # looser than the gate that let the person in — i.e. it gates nobody.
        # Equal is legitimate: it is the pre-step-8 world, where one number did
        # both jobs.
        if self.min_discovery_age < self.min_enrolment_age:
            raise ValueError(
                "MIN_DISCOVERY_AGE must be >= MIN_ENROLMENT_AGE — a discovery age"
                " below the enrolment age gates nobody"
            )
        return self

    @model_validator(mode="after")
    def _breaker_cooldown_ordered(self) -> Config:
        if self.breaker_cooldown_max_seconds < self.breaker_cooldown_seconds:
            raise ValueError(
                "BREAKER_COOLDOWN_MAX_SECONDS must be >= BREAKER_COOLDOWN_SECONDS"
            )
        return self

    @model_validator(mode="after")
    def _scan_thresholds_ordered(self) -> Config:
        # relaxed before dormant, and priority released no sooner than a seed
        # would have been relaxed — otherwise a hit makes a seed's cadence
        # WORSE than never having had one, which is backwards.
        if self.scan_dormant_after_empty <= self.scan_relaxed_after_empty:
            raise ValueError(
                "SCAN_DORMANT_AFTER_EMPTY must be > SCAN_RELAXED_AFTER_EMPTY"
            )
        if self.scan_priority_release_after_empty < self.scan_relaxed_after_empty:
            raise ValueError(
                "SCAN_PRIORITY_RELEASE_AFTER_EMPTY must be >= SCAN_RELAXED_AFTER_EMPTY"
                " — a seed with a recent hit must never be demoted sooner than one"
                " that never had one"
            )
        return self

    @model_validator(mode="after")
    def _rekognition_region_matches_deployment(self) -> Config:
        """D7. Refused at boot because the failure is silent everywhere else.

        A collection is regional. Enrol into ap-south-1, search us-east-1, and
        the search succeeds against an empty collection: no error, no alarm,
        just "no matches in monitored sources" forever. CLAUDE.md §1 calls a
        false negative a broken promise, and this is the cheapest way to make one.
        """
        if self.rekognition_region != self.aws_region:
            raise ValueError(
                "REKOGNITION_REGION must equal AWS_REGION — a collection is"
                " regional, and searching the wrong region returns an empty"
                " result that is indistinguishable from 'no matches'"
            )
        return self

    @model_validator(mode="after")
    def _no_debug_logging_in_production(self) -> Config:
        """Debug logs in this service carry user_ref, bounding boxes and
        provider payloads. The redaction processor covers known keys; debug
        level widens what reaches the log in the first place."""
        if self.environment == "production" and self.log_level == "debug":
            raise ValueError(
                "LOG_LEVEL must not be 'debug' when ENVIRONMENT=production"
            )
        return self

    @model_validator(mode="after")
    def _development_uses_the_stub_provider(self) -> Config:
        """The dev Hive credential is a REAL key — Hive has no sandbox.

        Hive Web Search is contract-priced and `hive.cost_per_call_usd` is NULL,
        so a budget set against it fails closed and caps nothing (§7.6). That
        makes config the only cheap place to stop a dev run spending real money,
        and an env edit alone must not be enough to do it.
        """
        if self.environment == "development" and self.search_provider != "stub":
            raise ValueError(
                "SEARCH_PROVIDER must be 'stub' when ENVIRONMENT=development —"
                " the dev provider keys are real and Hive has no sandbox"
            )
        return self

    @model_validator(mode="after")
    def _search_threshold_is_deliberate(self) -> Config:
        """80 is Rekognition's FaceMatchThreshold default.

        The handoff says "pin it — not 80" because the default is the value you
        get when nobody made a decision, and for the threshold that decides
        whether someone is told their face is in porn, an accidental value is
        not acceptable. Any other number is fine; this only refuses the one that
        means "unset".
        """
        if self.search_match_threshold == 80.0:
            raise ValueError(
                "SEARCH_MATCH_THRESHOLD must not be exactly 80 — that is"
                " Rekognition's default, i.e. an unchosen value; pin a measured one"
            )
        return self

    @property
    def auth_disabled(self) -> bool:
        """True only when the bypass was requested AND environment is development."""
        return self.service_token_auth_disabled and self.environment == "development"


def load_config() -> Config:
    """Read and validate configuration from the environment.

    Raises :class:`ConfigError` with one line per offending key. Messages name
    the key and the rule only — never the received value, since several keys
    are secrets and this text goes to stderr.
    """
    load_dotenv_local()
    try:
        return Config()  # fields come from the environment
    except ValidationError as exc:
        issues: list[str] = []
        for err in exc.errors(include_url=False, include_input=False):
            loc = ".".join(str(part) for part in err["loc"]) or "(config)"
            issues.append(f"{loc.upper()}: {err['msg']}")
        raise ConfigError(
            "Invalid configuration:\n" + "\n".join(f"  - {issue}" for issue in issues)
        ) from None
