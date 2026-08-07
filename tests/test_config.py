from __future__ import annotations

from pathlib import Path

import pytest

from imageshield.config import Config, ConfigError, load_config
from tests.conftest import SERVICE_TOKEN, VALID_ENV, make_config


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    """Pin the environment to exactly VALID_ENV and chdir away from the repo
    so a developer's .env.local cannot refill deleted keys."""
    monkeypatch.chdir(tmp_path)
    for key in [*VALID_ENV, "ENVIRONMENT", "SERVICE_TOKEN_AUTH_DISABLED"]:
        monkeypatch.delenv(key, raising=False)
    for key, value in VALID_ENV.items():
        monkeypatch.setenv(key, value)
    return monkeypatch


def test_valid_env_loads(clean_env: pytest.MonkeyPatch) -> None:
    cfg = load_config()
    assert cfg.rekognition_collection_id == "identity-v1"
    assert cfg.environment == "production"
    assert cfg.auth_disabled is False


@pytest.mark.parametrize("missing_key", sorted(VALID_ENV))
def test_missing_key_is_fatal_and_named(
    clean_env: pytest.MonkeyPatch, missing_key: str
) -> None:
    clean_env.delenv(missing_key)
    with pytest.raises(ConfigError) as excinfo:
        load_config()
    assert missing_key in str(excinfo.value)


def test_secret_values_never_echoed(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("LIVENESS_MIN_CONFIDENCE", "not-a-number")
    with pytest.raises(ConfigError) as excinfo:
        load_config()
    message = str(excinfo.value)
    assert SERVICE_TOKEN not in message
    assert VALID_ENV["HIVE_API_KEY"] not in message


def test_equal_tokens_refuse_to_start(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("ADMIN_SERVICE_TOKEN", VALID_ENV["SERVICE_TOKEN"])
    with pytest.raises(ConfigError, match="must differ"):
        load_config()


@pytest.mark.parametrize(
    ("key", "bad_value"),
    [
        ("LIVENESS_MIN_CONFIDENCE", "abc"),
        ("LIVENESS_MIN_CONFIDENCE", "101"),
        ("FACE_MATCH_THRESHOLD", "-1"),
        ("MIN_ENROLMENT_AGE", "-2"),
        ("LIVENESS_SESSION_TTL_SECONDS", "0"),
        ("LIVENESS_MAX_ATTEMPTS_24H", "0"),
        ("DATABASE_URL", "mysql://nope"),
        ("AWS_REGION", "not-a-region"),
        ("HIVE_BASE_URL", "not a url"),
        ("HIVE_API_KEY", "changeme"),
        ("SERVICE_TOKEN", "short"),
    ],
)
def test_malformed_values_are_fatal(
    clean_env: pytest.MonkeyPatch, key: str, bad_value: str
) -> None:
    clean_env.setenv(key, bad_value)
    with pytest.raises(ConfigError) as excinfo:
        load_config()
    assert key in str(excinfo.value)


def test_auth_bypass_requires_development() -> None:
    requested_in_prod = make_config(
        environment="production", service_token_auth_disabled=True
    )
    assert requested_in_prod.auth_disabled is False

    requested_in_dev = make_config(
        environment="development", service_token_auth_disabled=True
    )
    assert requested_in_dev.auth_disabled is True

    not_requested = make_config(environment="development")
    assert not_requested.auth_disabled is False


def test_config_is_frozen(config: Config) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        config.service_token = "mutated-token-value-000"  # type: ignore[misc]


def test_outbox_field_defaults(config: Config) -> None:
    assert config.outbox_poll_interval_seconds == 1.0
    assert config.outbox_batch_size == 50
    assert config.outbox_max_attempts == 8


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("outbox_poll_interval_seconds", 0.0),
        ("outbox_poll_interval_seconds", -1.0),
        ("outbox_batch_size", 0),
        ("outbox_batch_size", -5),
        ("outbox_max_attempts", 0),
        ("outbox_max_attempts", -1),
    ],
)
def test_outbox_fields_reject_non_positive_values(field: str, bad_value: float) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        make_config(**{field: bad_value})


def test_liveness_cost_defaults_to_devtools_measured_figure(config: Config) -> None:
    """$0.015 per completed check, measured 2026-08-05 (devtools/harness
    README). Lives in config because it feeds the step-8 budget logic."""
    assert config.liveness_cost_per_check_usd == 0.015


def test_liveness_cost_env_override(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("LIVENESS_COST_PER_CHECK_USD", "0.02")
    assert load_config().liveness_cost_per_check_usd == 0.02


@pytest.mark.parametrize("bad_value", [0.0, -0.01])
def test_liveness_cost_rejects_non_positive_values(bad_value: float) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        make_config(liveness_cost_per_check_usd=bad_value)


def test_google_vision_api_key_sentinel_rejected() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        make_config(google_vision_api_key="changeme")


def test_google_vision_and_provider_timeout_defaults() -> None:
    cfg = make_config()
    assert cfg.google_vision_endpoint.startswith("https://vision.googleapis.com/")
    assert cfg.provider_timeout_seconds == 120.0


def test_provider_timeout_must_be_positive() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        make_config(provider_timeout_seconds=0)
