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
    rekognition_collection_id: str

    liveness_min_confidence: float
    face_match_threshold: float
    min_enrolment_age: int
    liveness_session_ttl_seconds: int
    liveness_max_attempts_24h: int

    hive_api_key: str
    hive_base_url: str

    sqs_identity_index_url: str
    sqs_search_runs_url: str

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

    @field_validator("hive_api_key")
    @classmethod
    def _secret_not_placeholder(cls, value: str) -> str:
        if not value:
            raise ValueError("must not be empty")
        if value.strip().lower() in SENTINEL_VALUES:
            raise ValueError("is still set to a placeholder")
        return value

    @field_validator("aws_region")
    @classmethod
    def _region(cls, value: str) -> str:
        if not _AWS_REGION_RE.match(value):
            raise ValueError("must be an AWS region like ap-south-1")
        return value

    @field_validator("rekognition_collection_id")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("hive_base_url", "sqs_identity_index_url", "sqs_search_runs_url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        AnyHttpUrl(value)
        return value

    @field_validator("liveness_min_confidence", "face_match_threshold")
    @classmethod
    def _confidence(cls, value: float) -> float:
        if not 0 <= value <= 100:
            raise ValueError("must be between 0 and 100")
        return value

    @field_validator("min_enrolment_age")
    @classmethod
    def _age(cls, value: int) -> int:
        if not 0 <= value <= 130:
            raise ValueError("must be between 0 and 130")
        return value

    @field_validator("liveness_session_ttl_seconds", "liveness_max_attempts_24h")
    @classmethod
    def _positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("must be a positive integer")
        return value

    @model_validator(mode="after")
    def _tokens_distinct(self) -> Config:
        if self.service_token == self.admin_service_token:
            raise ValueError(
                "SERVICE_TOKEN and ADMIN_SERVICE_TOKEN must differ — refusing to start"
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
