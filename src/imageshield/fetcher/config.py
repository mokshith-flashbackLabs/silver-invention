"""Environment-driven configuration for the crop fetcher deployable.

**There is no ``database_url`` field here, and none may be added.** That is
the entire point of this class being separate from ``imageshield.config``
rather than a subset of it: ARCHITECTURE.md §3.7 says the fetcher has no VPC
access to any internal service, and the cheapest way to make that true even
under a future misconfiguration is for the field to not exist. Nothing in
this module imports ``imageshield.config.Config`` — only the two small,
DB-free helpers it exports (``SENTINEL_VALUES``, ``ConfigError``), which this
mirrors rather than depends on.

Same idioms as ``imageshield.config`` deliberately: the token-length rule,
sentinel-value rejection, and the ``ValidationError`` → one-line-per-key
formatting in :func:`load_fetcher_config` — so an operator debugging a boot
failure in either process reads the same shape of message.
"""

from __future__ import annotations

from pydantic import ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from imageshield.config import SENTINEL_VALUES, ConfigError
from imageshield.env import load_dotenv_local


class FetcherConfig(BaseSettings):
    model_config = SettingsConfigDict(frozen=True, extra="ignore", case_sensitive=False)

    # Compared via hmac.compare_digest against X-Fetcher-Token (fetcher/app.py)
    # — same rule as SERVICE_TOKEN/ADMIN_SERVICE_TOKEN: at least 16 characters,
    # and never one of the placeholder values nobody meant to ship.
    fetcher_token: str

    # A hostile server can return an arbitrarily long body; this is the cap
    # `fetch_image` enforces WHILE streaming, not after the fact.
    fetch_max_bytes: int = 10 * 1024 * 1024
    fetch_timeout_seconds: float = 10.0
    # Matches the recheck loop's posture (INVARIANTS #11): a redirect cap low
    # enough that a chain cannot be used to walk somewhere.
    fetch_max_redirects: int = 2

    @field_validator("fetcher_token")
    @classmethod
    def _token(cls, value: str) -> str:
        if len(value) < 16:
            raise ValueError("must be at least 16 characters")
        if value.strip().lower() in SENTINEL_VALUES:
            raise ValueError("is still set to a placeholder")
        return value

    @field_validator("fetch_max_bytes", "fetch_max_redirects")
    @classmethod
    def _positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("must be a positive integer")
        return value

    @field_validator("fetch_timeout_seconds")
    @classmethod
    def _positive_float(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("must be a positive number")
        return value


def load_fetcher_config() -> FetcherConfig:
    """Read and validate configuration from the environment.

    Raises :class:`ConfigError` with one line per offending key. Messages name
    the key and the rule only — never the received value — the same contract
    as ``imageshield.config.load_config``.
    """
    load_dotenv_local()
    try:
        return FetcherConfig()  # fields come from the environment
    except ValidationError as exc:
        issues: list[str] = []
        for err in exc.errors(include_url=False, include_input=False):
            loc = ".".join(str(part) for part in err["loc"]) or "(config)"
            issues.append(f"{loc.upper()}: {err['msg']}")
        raise ConfigError(
            "Invalid configuration:\n" + "\n".join(f"  - {issue}" for issue in issues)
        ) from None
