"""IntelConfig: the intel worker's own environment class (task 5, spec §4.1).

Isolated from any developer ``.env.local`` in the cwd the same way
``tests/test_config.py``'s ``clean_env`` fixture is: chdir to a fresh
``tmp_path`` and delete every key first, so a real ``.env.local`` sitting
above the repo root cannot silently refill a key a test deleted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from imageshield.config import ConfigError
from imageshield.intel.config import IntelConfig, load_intel_config

BASE = {
    "DATABASE_URL": "postgresql://u:p@localhost:15433/imageshield",
    "INTEL_ENABLED": "true",
    "INTEL_MODEL_PROVIDER": "claude",
    "INTEL_ANTHROPIC_REGION": "ap-south-1",
    "ANTHROPIC_AWS_WORKSPACE_ID": "wrkspc_test",
    "INTEL_EXTRACTION_MODEL": "claude-sonnet-5",
    "INTEL_PROPOSAL_MODEL": "claude-opus-5-5",
    "INTEL_WEB_SEARCH_TOOL_TYPE": "web_search_20260209",
    "FETCHER_BASE_URL": "http://localhost:8083",
    "FETCHER_TOKEN": "fetcher-token-for-tests-0003",
}

_ALL_ENV_NAMES = {name.upper() for name in IntelConfig.model_fields} | {
    "DB_HOST",
    "DB_PORT",
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
}


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    monkeypatch.chdir(tmp_path)
    for key in _ALL_ENV_NAMES:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def _env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    for key, value in {**BASE, **overrides}.items():
        monkeypatch.setenv(key, value)


def test_loads_with_defaults(clean_env: pytest.MonkeyPatch) -> None:
    _env(clean_env)
    cfg = load_intel_config()
    assert cfg.intel_poll_seconds == 30 and cfg.intel_max_calls_per_run == 20
    assert cfg.environment == "production"


def test_intel_enabled_is_required(clean_env: pytest.MonkeyPatch) -> None:
    _env(clean_env)
    clean_env.delenv("INTEL_ENABLED")
    with pytest.raises(ConfigError, match="INTEL_ENABLED"):
        load_intel_config()


def test_development_refuses_a_live_model(clean_env: pytest.MonkeyPatch) -> None:
    _env(clean_env, ENVIRONMENT="development")
    with pytest.raises(ConfigError, match="stub"):
        load_intel_config()


def test_production_refuses_the_stub(clean_env: pytest.MonkeyPatch) -> None:
    _env(clean_env, INTEL_MODEL_PROVIDER="stub")
    with pytest.raises(ConfigError, match="stub"):
        load_intel_config()


def test_database_url_composes_from_parts(clean_env: pytest.MonkeyPatch) -> None:
    _env(clean_env)
    clean_env.delenv("DATABASE_URL")
    for key, value in {
        "DB_HOST": "h",
        "DB_PORT": "5432",
        "DB_NAME": "n",
        "DB_USER": "u",
        "DB_PASSWORD": "pw-for-tests",
    }.items():
        clean_env.setenv(key, value)
    assert load_intel_config().database_url.startswith("postgresql://")


def test_it_has_no_field_the_shared_config_would_need() -> None:
    # The fetcher precedent: a separate class, so model settings never spread.
    assert "service_token" not in IntelConfig.model_fields


def test_log_level_debug_is_refused_in_production(clean_env: pytest.MonkeyPatch) -> None:
    _env(clean_env, LOG_LEVEL="debug")
    with pytest.raises(ConfigError, match="LOG_LEVEL"):
        load_intel_config()


def test_intel_proposal_model_is_required(clean_env: pytest.MonkeyPatch) -> None:
    _env(clean_env)
    clean_env.delenv("INTEL_PROPOSAL_MODEL")
    with pytest.raises(ConfigError, match="INTEL_PROPOSAL_MODEL"):
        load_intel_config()
