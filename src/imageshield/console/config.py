"""Environment-driven configuration for the control-room console.

Same idioms as ``imageshield.fetcher.config`` deliberately, and for the same
reason that module states: an operator debugging a boot failure in any of
this repo's processes should read the same shape of message — the
token-length rule, sentinel-value rejection, and the ``ValidationError`` ->
one-line-per-key formatting in :func:`load_console_config`.

**There is no ``database_url`` field here, and none may be added.** The
console is a standalone deployable (a separate box from the services API and
the fetcher) that talks to both of them over HTTP only — CLAUDE.md's
boundary rule applies here too: no database access of any kind. Nothing in
this module imports ``imageshield.config.Config`` — only the two small,
DB-free helpers it exports (``SENTINEL_VALUES``, ``ConfigError``), the same
thing ``fetcher/config.py`` does.
"""

from __future__ import annotations

from pydantic import ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from imageshield.config import SENTINEL_VALUES, ConfigError
from imageshield.env import load_dotenv_local


class ConsoleConfig(BaseSettings):
    model_config = SettingsConfigDict(frozen=True, extra="ignore", case_sensitive=False)

    # "name:token,name:token" -- parsed by console.auth.parse_operators.
    # Kept as a raw string here (rather than parsed eagerly into a dict)
    # so this class stays a plain settings object; load_console_config below
    # still validates it fully at boot, via the same parser auth.py uses at
    # request time, so a malformed roster fails at startup, not on the first
    # login attempt.
    console_operators: str

    # The services admin API is the console's ONLY upstream. No fetcher
    # fields: staff never see hit imagery (spec 2026-08-21 §0.2), so this
    # process deliberately cannot reach the pixels path at all.
    services_base_url: str
    service_token: str
    admin_service_token: str

    @field_validator("service_token", "admin_service_token")
    @classmethod
    def _token(cls, value: str) -> str:
        if len(value) < 16:
            raise ValueError("must be at least 16 characters")
        if value.strip().lower() in SENTINEL_VALUES:
            raise ValueError("is still set to a placeholder")
        return value

    @field_validator("console_operators")
    @classmethod
    def _operators_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must name at least one operator (name:token,...)")
        return value

    @field_validator("services_base_url")
    @classmethod
    def _base_url(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value


def load_console_config() -> ConsoleConfig:
    """Read and validate configuration from the environment.

    Raises :class:`ConfigError` with one line per offending key. Messages
    name the key and the rule only -- never the received value -- the same
    contract as ``imageshield.fetcher.config.load_fetcher_config``.
    """
    load_dotenv_local()
    try:
        config = ConsoleConfig()  # fields come from the environment
    except ValidationError as exc:
        issues: list[str] = []
        for err in exc.errors(include_url=False, include_input=False):
            loc = ".".join(str(part) for part in err["loc"]) or "(config)"
            issues.append(f"{loc.upper()}: {err['msg']}")
        raise ConfigError(
            "Invalid configuration:\n" + "\n".join(f"  - {issue}" for issue in issues)
        ) from None

    # Deferred import: console.auth imports ConsoleConfig from this module,
    # so importing it at module scope here would be circular. By the time
    # this function runs both modules are already fully loaded.
    from imageshield.console.auth import parse_operators

    try:
        parse_operators(config.console_operators)
    except ValueError as exc:
        raise ConfigError(f"Invalid configuration:\n  - CONSOLE_OPERATORS: {exc}") from None
    return config
