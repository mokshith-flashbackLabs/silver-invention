"""Every search limit is configuration (spec 2026-10-10-intel-every-answer-researched §5.1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from imageshield.config import ConfigError
from imageshield.intel import bounds, model
from imageshield.intel.config import load_intel_config
from imageshield.intel.limits import SearchLimits
from tests.test_intel_config import _ALL_ENV_NAMES, _env

LIMIT_KEYS = (
    "INTEL_MAX_SOURCE_PROPOSAL_SEARCHES",
    "INTEL_SOURCE_PROPOSAL_MAX_OUTPUT",
    "INTEL_MAX_PROPOSED_SOURCES_PER_ANSWER",
    "INTEL_MAX_PROPOSED_SEARCHES_PER_ANSWER",
    "INTEL_MAX_WEB_SEARCHES_PER_RUN",
    "INTEL_SEARCH_READ_MAX_OUTPUT",
    "INTEL_DISCOVERY_MIN_RESULT_PAGES",
    "INTEL_DISCOVERY_MAX_RESULT_PAGES",
    "INTEL_VALIDATION_SEARCHES",
    "INTEL_VALIDATION_RESULT_PAGES",
    "INTEL_SOURCE_PROPOSAL_COVERAGE_ASKS",
    "INTEL_RESEARCH_SEARCHES_PER_ANSWER",
    "INTEL_SUGGESTION_RESEARCH_ROUNDS",
)
ZERO_ALLOWED = {"INTEL_SOURCE_PROPOSAL_COVERAGE_ASKS", "INTEL_SUGGESTION_RESEARCH_ROUNDS"}
REPO = Path(__file__).resolve().parents[1]
SPEC_VALUES = {
    "INTEL_MAX_SOURCE_PROPOSAL_SEARCHES": "40",
    "INTEL_SOURCE_PROPOSAL_MAX_OUTPUT": "24000",
    "INTEL_MAX_PROPOSED_SOURCES_PER_ANSWER": "8",
    "INTEL_MAX_PROPOSED_SEARCHES_PER_ANSWER": "3",
    "INTEL_MAX_WEB_SEARCHES_PER_RUN": "10",
    "INTEL_SEARCH_READ_MAX_OUTPUT": "16000",
    "INTEL_DISCOVERY_MIN_RESULT_PAGES": "5",
    "INTEL_DISCOVERY_MAX_RESULT_PAGES": "12",
    "INTEL_VALIDATION_SEARCHES": "3",
    "INTEL_VALIDATION_RESULT_PAGES": "8",
    "INTEL_SOURCE_PROPOSAL_COVERAGE_ASKS": "2",
    "INTEL_RESEARCH_SEARCHES_PER_ANSWER": "3",
    "INTEL_SUGGESTION_RESEARCH_ROUNDS": "4",
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    monkeypatch.chdir(tmp_path)
    for key in _ALL_ENV_NAMES:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


@pytest.mark.parametrize("key", LIMIT_KEYS)
def test_every_limit_is_required(env: pytest.MonkeyPatch, key: str) -> None:
    _env(env)
    env.delenv(key)
    with pytest.raises(ConfigError, match=key):
        load_intel_config()


@pytest.mark.parametrize("key", LIMIT_KEYS)
def test_a_negative_limit_refuses_boot(env: pytest.MonkeyPatch, key: str) -> None:
    _env(env, **{key: "-1"})
    with pytest.raises(ConfigError, match=key):
        load_intel_config()


@pytest.mark.parametrize("key", sorted(set(LIMIT_KEYS) - ZERO_ALLOWED))
def test_zero_refuses_boot_where_at_least_one_is_required(
    env: pytest.MonkeyPatch, key: str
) -> None:
    _env(env, **{key: "0"})
    with pytest.raises(ConfigError, match=key):
        load_intel_config()


@pytest.mark.parametrize("key", sorted(ZERO_ALLOWED))
def test_zero_turns_coverage_and_research_off(env: pytest.MonkeyPatch, key: str) -> None:
    _env(env, **{key: "0"})
    load_intel_config()


def test_a_discovery_minimum_above_its_maximum_refuses_boot(env: pytest.MonkeyPatch) -> None:
    _env(env, INTEL_DISCOVERY_MIN_RESULT_PAGES="9", INTEL_DISCOVERY_MAX_RESULT_PAGES="8")
    with pytest.raises(ConfigError, match="INTEL_DISCOVERY_MIN_RESULT_PAGES"):
        load_intel_config()


def test_search_limits_carries_the_configured_values(env: pytest.MonkeyPatch) -> None:
    _env(env)
    assert load_intel_config().search_limits() == SearchLimits(
        proposed_sources_per_answer=8,
        proposed_searches_per_answer=3,
        discovery_min_result_pages=5,
        discovery_max_result_pages=12,
        validation_searches=3,
        validation_result_pages=8,
        source_proposal_coverage_asks=2,
        suggestion_research_rounds=4,
        research_searches_per_answer=3,
    )


def test_the_moved_constants_are_gone_from_code() -> None:
    for name in (
        "DISCOVERY_MIN_RESULT_PAGES",
        "DISCOVERY_MAX_RESULT_PAGES",
        "MAX_PROPOSED_SOURCES_PER_OPTION",
        "MAX_PROPOSED_SEARCHES_PER_OPTION",
        "MAX_SEARCH_RESULTS_CHECKED",
    ):
        assert not hasattr(bounds, name), name
    for name in ("_SOURCE_PROPOSAL_MAX_TOKENS", "_VALIDATION_SEARCHES"):
        assert not hasattr(model, name), name


@pytest.mark.parametrize(
    "path",
    ["infra/ecs/imageshield-dev-services-worker.json", "infra/ecs/prod/services-worker.json"],
)
def test_both_task_definitions_set_every_limit_to_the_spec_value(path: str) -> None:
    doc = json.loads((REPO / path).read_text())
    (worker,) = [c for c in doc["containerDefinitions"] if c["name"] == "intel-worker"]
    env = {e["name"]: e["value"] for e in worker["environment"]}
    assert {k: env.get(k) for k in LIMIT_KEYS} == SPEC_VALUES


def test_env_example_names_every_limit() -> None:
    text = (REPO / ".env.example").read_text()
    for key in LIMIT_KEYS:
        assert f"\n{key}=" in text, key
