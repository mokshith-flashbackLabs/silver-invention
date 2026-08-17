from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

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
    assert cfg.identity_collection == "identity-v1"
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
        ("REKOGNITION_REGION", "not-a-region"),
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


def test_raw_response_retention_defaults_to_90_days(config: Config) -> None:
    """Long enough to recalibrate over recent history, short enough that the
    JSONB does not become the largest thing in the database."""
    assert config.raw_response_retention_days == 90


def test_raw_response_retention_env_override(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("RAW_RESPONSE_RETENTION_DAYS", "30")
    assert load_config().raw_response_retention_days == 30


@pytest.mark.parametrize("bad_value", [0, -1])
def test_raw_response_retention_rejects_non_positive_values(bad_value: int) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        make_config(raw_response_retention_days=bad_value)


def test_calibration_min_eval_items_defaults_to_200() -> None:
    assert make_config().calibration_min_eval_items == 200


def test_calibration_min_eval_items_must_be_positive() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        make_config(calibration_min_eval_items=0)


# ── Step 8: two ages, not one ─────────────────────────────────────────────


def test_both_ages_are_required_from_the_environment(
    clean_env: pytest.MonkeyPatch,
) -> None:
    """Neither has a default. An age floor that ships with one is an age floor
    somebody can forget to set."""
    clean_env.delenv("MIN_DISCOVERY_AGE", raising=False)
    with pytest.raises(ConfigError) as exc:
        load_config()
    assert "MIN_DISCOVERY_AGE" in str(exc.value)


def test_the_v1_split_is_13_to_enrol_and_18_to_be_searched(
    clean_env: pytest.MonkeyPatch,
) -> None:
    clean_env.setenv("MIN_ENROLMENT_AGE", "13")
    clean_env.setenv("MIN_DISCOVERY_AGE", "18")
    cfg = load_config()
    assert (cfg.min_enrolment_age, cfg.min_discovery_age) == (13, 18)


def test_a_discovery_age_below_the_enrolment_age_refuses_to_boot() -> None:
    """It would gate nobody: everyone who got through enrolment is already past
    it. Equal is legitimate — that is the pre-step-8 world, 18/18."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        make_config(min_enrolment_age=18, min_discovery_age=13)
    assert make_config(min_enrolment_age=18, min_discovery_age=18).min_discovery_age == 18


# ── Step 8: the provider control knobs ────────────────────────────────────


def test_step_8_defaults() -> None:
    cfg = make_config()
    assert cfg.provider_failure_threshold == 5
    assert cfg.breaker_cooldown_seconds == 300
    assert cfg.breaker_cooldown_max_seconds == 3600
    assert cfg.provider_max_retries == 3
    assert cfg.provider_config_cache_seconds == 30.0
    assert cfg.scan_relaxed_after_empty == 8
    assert cfg.scan_dormant_after_empty == 20
    assert cfg.provider_spend_alarm_fraction == 0.80


def test_the_provider_cache_ttl_is_capped_at_30_seconds() -> None:
    """A kill switch that takes a minute to bite is not a kill switch, and the
    only way to be sure is to refuse to boot with a longer TTL. The cap lives in
    code, so raising it costs a review."""
    from pydantic import ValidationError

    assert make_config(provider_config_cache_seconds=30.0)
    assert make_config(provider_config_cache_seconds=5.0)
    with pytest.raises(ValidationError):
        make_config(provider_config_cache_seconds=31.0)


def test_a_cooldown_cap_below_the_base_cooldown_refuses_to_boot() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        make_config(breaker_cooldown_seconds=600, breaker_cooldown_max_seconds=300)


def test_cadence_thresholds_must_be_ordered() -> None:
    """A seed with a recent hit must never be demoted sooner than one that never
    had one — otherwise a hit makes a user's cadence WORSE."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        make_config(scan_relaxed_after_empty=20, scan_dormant_after_empty=8)
    with pytest.raises(ValidationError):
        make_config(scan_relaxed_after_empty=8, scan_priority_release_after_empty=4)


def test_the_retry_jitter_fraction_must_be_a_fraction() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        make_config(provider_retry_jitter_fraction=1.5)
    with pytest.raises(ValidationError):
        make_config(provider_retry_jitter_fraction=-0.1)


# -- Attribution knobs ------------------------------------------------------


def test_a_single_candidate_search_refuses_to_boot() -> None:
    """Done-when: max_candidates < 2 is rejected by the config schema at boot.

    Refused HERE rather than at the call site because the failure a single
    candidate produces is silent. A stranger who outranks the household member
    is the only result returned, the candidate filter discards it, and the
    photo never becomes a seed for its own owner. Nothing raises, nothing is
    logged as wrong, and the coverage loss grows with the collection. A knob
    whose bad value is invisible has to be refused where it is set.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        make_config(attribution_max_candidates=1)
    with pytest.raises(ValidationError):
        make_config(attribution_max_candidates=0)
    assert make_config(attribution_max_candidates=2).attribution_max_candidates == 2


def test_the_attribution_threshold_is_its_own_knob() -> None:
    """INVARIANTS #1b: one threshold per purpose. "Is this face that person" is
    a different question from "is this a live human", and sharing a number
    between them is how the old system's 90/95/99 spread started."""
    cfg = make_config(attribution_match_threshold=88.5, liveness_min_confidence=95.0)
    assert cfg.attribution_match_threshold == 88.5
    assert cfg.liveness_min_confidence == 95.0


# ── Dev deploy handoff: rename + four boot assertions ─────────────────────


def test_rekognition_region_must_equal_aws_region() -> None:
    """D7. A regional collection reached from the wrong region is empty, and an
    empty collection is indistinguishable from 'no matches'."""
    with pytest.raises(ValidationError, match="REKOGNITION_REGION"):
        make_config(aws_region="ap-south-1", rekognition_region="us-east-1")


def test_matching_regions_are_accepted() -> None:
    cfg = make_config(aws_region="ap-south-1", rekognition_region="ap-south-1")
    assert cfg.rekognition_region == "ap-south-1"


def test_debug_logging_is_refused_in_production() -> None:
    with pytest.raises(ValidationError, match="LOG_LEVEL"):
        make_config(environment="production", log_level="debug")


def test_debug_logging_is_allowed_in_development() -> None:
    cfg = make_config(environment="development", log_level="debug", search_provider="stub")
    assert cfg.log_level == "debug"


def test_development_refuses_a_live_provider() -> None:
    """The dev Hive key is real and Hive has no sandbox; its price is NULL so the
    budget guard fails closed and caps nothing."""
    with pytest.raises(ValidationError, match="SEARCH_PROVIDER"):
        make_config(environment="development", search_provider="hive")


def test_production_allows_a_live_provider() -> None:
    cfg = make_config(environment="production", search_provider="hive")
    assert cfg.search_provider == "hive"


def test_production_refuses_the_stub_provider() -> None:
    """The other direction, and it is the dangerous one.

    `search_provider` defaults to 'stub', so production inherits it from an
    unset variable rather than from a decision. The stub returns zero matches
    and makes no call, so a production deploy carrying it reports "no matches in
    monitored sources" for every user, forever, with nothing failing: no error,
    no alarm, no provider_calls row that looks wrong. CLAUDE.md §1 — a false
    negative is a broken promise, and this is the cheapest way to make one for
    the entire population at once.
    """
    with pytest.raises(ValidationError, match="SEARCH_PROVIDER"):
        make_config(environment="production", search_provider="stub")


def test_search_match_threshold_refuses_the_rekognition_default() -> None:
    """80 is the value you get when nobody chose one."""
    with pytest.raises(ValidationError, match="SEARCH_MATCH_THRESHOLD"):
        make_config(search_match_threshold=80.0)


def test_search_match_threshold_accepts_a_deliberate_value() -> None:
    assert make_config(search_match_threshold=88.5).search_match_threshold == 88.5


def test_identity_collection_replaces_the_old_name() -> None:
    assert make_config().identity_collection == "identity-v1"
    assert not hasattr(make_config(), "rekognition_collection_id")


def test_unread_fields_are_still_validated() -> None:
    """DISCOVERED_COLLECTION and ENROLMENT_COLLISION_THRESHOLD have no reader in
    v1, but a blank or out-of-range value must still refuse to boot."""
    with pytest.raises(ValidationError):
        make_config(discovered_collection="   ")
    with pytest.raises(ValidationError):
        make_config(enrolment_collision_threshold=101.0)
