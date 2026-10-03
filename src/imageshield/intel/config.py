"""Environment configuration for the intel worker ONLY (spec §4.1).

Separate from ``imageshield.config`` for the fetcher's reason
(``fetcher/config.py``): only this container loads it, so model settings never
spread into the API, relay, search or confirm containers, and those never
crash-loop on a key they do not use. The database DSN resolves exactly as
``Config``'s does (``DATABASE_URL``, else the five ``DB_*`` parts) so the two
classes stay unable to disagree about DSN composition.
"""

from __future__ import annotations

from typing import Literal

from pydantic import ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from imageshield.config import SENTINEL_VALUES, ConfigError
from imageshield.db.dsn import compose_database_url
from imageshield.env import load_dotenv_local


class IntelConfig(BaseSettings):
    model_config = SettingsConfigDict(frozen=True, extra="ignore", case_sensitive=False)

    environment: Literal["development", "test", "production"] = "production"

    database_url: str = ""
    db_host: str | None = None
    db_port: int | None = None
    db_name: str | None = None
    db_user: str | None = None
    db_password: str | None = None
    db_sslmode: str = "require"
    db_pool_max_size: int = 2

    # Required, no default: an absent key is a boot failure, never a silent off.
    intel_enabled: bool
    intel_model_provider: Literal["stub", "claude"]
    intel_anthropic_region: str
    anthropic_aws_workspace_id: str
    intel_extraction_model: str
    # Proposal generation (step 2, spec §4.3). Config, not a literal -- the same
    # build-gate reason as the extraction model -- and priced at construction.
    intel_proposal_model: str
    # Config, not a literal: the phone-shaped build gate flags this string in src/.
    intel_web_search_tool_type: str
    intel_max_web_searches_per_run: int = 5
    intel_blocked_domains: list[str] = []
    intel_max_calls_per_run: int = 20
    intel_max_document_chars: int = 200_000
    # Source proposal (step 5, spec §4.10): stage 1's web-search budget. Required with no default,
    # like every key the spec gives none (§4.1).
    intel_max_source_proposal_searches: int
    # A weight suggestion's first read of the sources it registered is legitimately larger than a
    # weekly check (§4.10), so it has its own call cap.
    intel_max_calls_per_suggestion_run: int = 60
    # Throughput (spec 2026-10-03-intel-throughput §8). Required with no default, like every key
    # its spec gives none: each environment says how much it runs at once.
    # - INTEL_RUN_CONCURRENCY: runs one worker process executes at the same time.
    # - INTEL_SOURCE_READ_CONCURRENCY: sources (or pages) one run reads at the same time.
    # - INTEL_SEARCH_READ_EFFORT: Claude's effort on the web-search calls that READ or CHECK (a
    #   saved search's read, stage 3's validation search) and on no other call.
    # Every call still passes the provider gate (budget, breaker, kill switch) before it is sent,
    # but concurrent calls each pass it before any of them is recorded, so the daily cap can be
    # overshot by at most the calls in flight: RUN x SOURCE_READ for one worker, times the
    # number of services-worker tasks. No reservation is made; the bound is the design.
    # DB_POOL_MAX_SIZE must cover RUN x (SOURCE_READ + 1) + 1 (one connection per read in flight
    # and per run's lease heartbeat, plus the loop's own); the worker warns at boot when it does
    # not, and test_ecs_task_defs holds every deployed task to it.
    intel_run_concurrency: int
    intel_source_read_concurrency: int
    intel_search_read_effort: Literal["low", "medium", "high", "xhigh", "max"]

    provider_config_cache_seconds: float = 10.0
    provider_failure_threshold: int = 5
    breaker_cooldown_seconds: int = 300
    breaker_cooldown_max_seconds: int = 3600

    fetcher_base_url: str
    fetcher_token: str
    intel_poll_seconds: float = 30.0
    intel_lease_seconds: int = 900

    # Dev's intel-worker container runs under ENVIRONMENT=production (the same
    # posture the relay/search-worker containers already use) with
    # INTEL_ENABLED=false, so LOG_LEVEL still needs a home on this class even
    # though nothing above names it — the production gate below refuses
    # 'debug' the same way Config's does.
    log_level: Literal["debug", "info", "warning", "error"] = "info"

    @field_validator("fetcher_token")
    @classmethod
    def _token(cls, value: str) -> str:
        if len(value) < 16 or value.strip().lower() in SENTINEL_VALUES:
            raise ValueError("must be at least 16 characters and not a placeholder")
        return value

    @field_validator(
        "intel_max_web_searches_per_run",
        "intel_max_calls_per_run",
        "intel_max_document_chars",
        "intel_max_source_proposal_searches",
        "intel_max_calls_per_suggestion_run",
        "intel_run_concurrency",
        "intel_source_read_concurrency",
        "intel_lease_seconds",
        "db_pool_max_size",
        "provider_failure_threshold",
        "breaker_cooldown_seconds",
        "breaker_cooldown_max_seconds",
    )
    @classmethod
    def _positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("must be a positive integer")
        return value

    @field_validator("provider_config_cache_seconds", "intel_poll_seconds")
    @classmethod
    def _positive_float(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("must be a positive number")
        return value

    def pool_size_needed(self) -> int:
        """The connections the worker can want at once: one per source read in flight and one per
        run's lease heartbeat, for every run in flight, plus the loop's own (housekeeping and
        claims). Statements are short and hold no connection across a model call, so this is a
        ceiling rather than a typical load."""
        return self.intel_run_concurrency * (self.intel_source_read_concurrency + 1) + 1

    @model_validator(mode="after")
    def _resolve_database_url(self) -> IntelConfig:
        if self.database_url:
            return self
        parts = (self.db_host, self.db_port, self.db_name, self.db_user, self.db_password)
        if any(p in (None, "") for p in parts):
            raise ValueError(
                "DATABASE_URL is required (or all of DB_HOST, DB_PORT, DB_NAME, DB_USER,"
                " DB_PASSWORD, to compose one)"
            )
        assert self.db_host and self.db_port and self.db_name and self.db_user and self.db_password
        object.__setattr__(
            self,
            "database_url",
            compose_database_url(
                host=self.db_host,
                port=self.db_port,
                name=self.db_name,
                user=self.db_user,
                password=self.db_password,
                sslmode=self.db_sslmode,
            ),
        )
        return self

    @model_validator(mode="after")
    def _breaker_cooldown_ordered(self) -> IntelConfig:
        if self.breaker_cooldown_max_seconds < self.breaker_cooldown_seconds:
            raise ValueError("BREAKER_COOLDOWN_MAX_SECONDS must be >= BREAKER_COOLDOWN_SECONDS")
        return self

    @model_validator(mode="after")
    def _no_debug_logging_in_production(self) -> IntelConfig:
        if self.environment == "production" and self.log_level == "debug":
            raise ValueError("LOG_LEVEL must not be 'debug' when ENVIRONMENT=production")
        return self

    @model_validator(mode="after")
    def _development_uses_the_stub(self) -> IntelConfig:
        if self.environment == "development" and self.intel_model_provider != "stub":
            raise ValueError(
                "INTEL_MODEL_PROVIDER must be 'stub' when ENVIRONMENT=development"
                " — the dev model spends real money"
            )
        return self

    @model_validator(mode="after")
    def _production_never_uses_the_stub(self) -> IntelConfig:
        if self.environment == "production" and self.intel_model_provider == "stub":
            raise ValueError(
                "INTEL_MODEL_PROVIDER must not be 'stub' when ENVIRONMENT=production"
                " — the stub reads nothing, silently"
            )
        return self


def load_intel_config() -> IntelConfig:
    """Read and validate configuration from the environment.

    Raises :class:`ConfigError` with one line per offending key. Messages name
    the key and the rule only — never the received value — the same contract
    as ``imageshield.config.load_config``.
    """
    load_dotenv_local()
    try:
        return IntelConfig()  # fields come from the environment
    except ValidationError as exc:
        issues: list[str] = []
        for err in exc.errors(include_url=False, include_input=False):
            loc = ".".join(str(part) for part in err["loc"]) or "(config)"
            issues.append(f"{loc.upper()}: {err['msg']}")
        raise ConfigError(
            "Invalid configuration:\n" + "\n".join(f"  - {issue}" for issue in issues)
        ) from None
