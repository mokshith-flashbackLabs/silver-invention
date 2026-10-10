# Every Answer Is Researched — Implementation Plan (services)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Suggest points searches for every answer of a quiz question, searches again for any answer it could not back (up to a configured number of rounds), records per answer what was searched, and every search limit becomes configuration.

**Architecture:** One frozen `SearchLimits` value built from `IntelConfig` rides on `PipelineDeps`; the model reads its own search/token limits from `IntelConfig`. Stage 1 (`_source_proposal`) loops coverage asks for uncovered answers. Stage 4 (`_suggest`) judges each answer with the existing corroboration predicate; unbacked answers get research searches (asked for in the same suggestion call), registered as read-once disabled saved searches, read by their own discovery runs, and the suggestion run waits on them with the existing wait machinery, keeping its round state in three new `intel_runs` columns.

**Tech Stack:** Python 3.11+, FastAPI, psycopg 3 raw SQL, pydantic 2, pytest + pytest-postgresql, ruff, mypy strict.

**Spec:** `docs/superpowers/specs/2026-10-10-intel-every-answer-researched-design.md` (and its backend twin, `image_backend/docs/superpowers/specs/2026-10-10-published-citations-confirmed-and-researched-design.md`, which this repo does not implement).

## Global Constraints

- **No hardcoding** (owner, 2026-10-10): every limit in spec §5.1 is a required `IntelConfig` key with **no code default**, set in `.env.example`, `infra/ecs/imageshield-dev-services-worker.json` and `infra/ecs/prod/services-worker.json` with the §5.1 values. Safety limits in `intel/bounds.py` (quotes, attempts, PII, corroboration, `SUGGESTION_WAIT_*`) stay code constants.
- **No steering** (owner, 2026-10-09): no prompt, code path or test fixture text names a real question, answer, platform or example case. Rules are general.
- §5.1 values: `INTEL_MAX_SOURCE_PROPOSAL_SEARCHES=40`, `INTEL_SOURCE_PROPOSAL_MAX_TOKENS=24000`, `INTEL_MAX_PROPOSED_SOURCES_PER_ANSWER=8`, `INTEL_MAX_PROPOSED_SEARCHES_PER_ANSWER=3`, `INTEL_MAX_WEB_SEARCHES_PER_RUN=10`, `INTEL_SEARCH_READ_MAX_TOKENS=16000`, `INTEL_DISCOVERY_MIN_RESULT_PAGES=5`, `INTEL_DISCOVERY_MAX_RESULT_PAGES=12`, `INTEL_VALIDATION_SEARCHES=3`, `INTEL_VALIDATION_RESULT_PAGES=8`, `INTEL_SOURCE_PROPOSAL_COVERAGE_ASKS=2`, `INTEL_RESEARCH_SEARCHES_PER_ANSWER=3`, `INTEL_SUGGESTION_RESEARCH_ROUNDS=4`.
- Validation: coverage asks and research rounds ≥ 0 (0 turns the feature off); every other key ≥ 1; discovery min ≤ max (boot refuses otherwise).
- "Backed" = a deduction, cited evidence, and `corroboration.uncorroborated(active cited signals)` is False. One predicate, no second definition.
- A research search is a `search_query` source, `origin='research'`, `enabled=false`, `disabled_reason='research_once'`, `proposed_for={question_key, option}`, `created_by='system:research'`. (Spec §2.2.4 said `next_check_at='infinity'`; `queue_source_reads` overwrites `next_check_at`, so disabled is the mechanism. Task 6 amends the spec.)
- A suggestion is still written **once** per run; a newer press still wins.
- Commits: one per task, message trailer `Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>` (never a Claude trailer). Ruff-format only files you create (`ruff format <new files>`); `ruff check` and `mypy` the files you touch. Run the targeted tests named in each task, not the full suite (owner). One DB pytest session at a time.

## Review Focus

1. **A reclaimed or retried run mid-research** (lease expired after a round was registered but before `wait_run`): expected — no duplicate research sources (query dedup under the registration lock) and no second billing of the suggestion it already wrote. Pinned in Task 5 (`test_a_rerun_of_a_research_round_registers_nothing_twice`).
2. **All research searches dedup to sources that already exist and were read earlier** (no open read): expected — the run is claimable at once and answers from what exists, never loops. Pinned in Task 5 (`test_research_searches_that_reuse_read_sources_resume_at_once`).
3. **Research rounds set to 0 in config**: expected — behaviour identical to today, no `research` searches requested in the payload, `exhausted` false. Pinned in Task 4 (`test_rounds_zero_asks_for_no_searches`) and Task 5.
4. **A question whose every answer is already backed on the first pass**: expected — no research round, `research.rounds == 0` everywhere, one suggestion call. Pinned in Task 5.
5. **The kept pass written after a later-round failure carries the earlier round's numbers, not nulls**: pinned in Task 5 (`test_a_refused_second_round_writes_the_first_rounds_pass`).

---

### Task 1: Every search limit is configuration

**Files:**
- Create: `src/imageshield/intel/limits.py`
- Modify: `src/imageshield/intel/config.py` (fields ~l.48–57, `_positive` validator list ~l.103–118, new validators, `search_limits()`)
- Modify: `src/imageshield/intel/model.py` (delete `_SOURCE_PROPOSAL_MAX_TOKENS`, `_VALIDATION_SEARCHES` ~l.88–95; `discover`, `propose_sources`, `search_once`, `choose_results` ~l.472–515)
- Modify: `src/imageshield/intel/bounds.py` (delete `DISCOVERY_MIN_RESULT_PAGES`, `DISCOVERY_MAX_RESULT_PAGES`, `MAX_PROPOSED_SOURCES_PER_OPTION`, `MAX_PROPOSED_SEARCHES_PER_OPTION`, `MAX_SEARCH_RESULTS_CHECKED` and their comments)
- Modify: `src/imageshield/intel/pipeline.py` (`PipelineDeps` gains `limits`; imports ~l.65–66; `_discover` target ~l.677–680)
- Modify: `src/imageshield/intel/question_runs.py` (imports l.33–42; `_source_proposal` caps l.132, l.168, l.172; `_validate_search` l.300)
- Modify: `src/imageshield/intel/prompts.py` (`SOURCE_PROPOSAL_PROMPT_VERSION`, `_SOURCE_PROPOSAL_SYSTEM`, `source_proposal_request`)
- Modify: `src/imageshield/intel/source_choice.py` (module docstring line 6 names the deleted constant)
- Modify: `src/imageshield/intel/worker.py` (`PipelineDeps(...)` ~l.396)
- Modify: `tests/intel_fakes.py` (`make_deps`, new `TEST_LIMITS`)
- Modify: `tests/test_intel_config.py` (`BASE`, new tests), `tests/test_intel_pipeline.py:923` comment
- Modify: `.env.example`, `infra/ecs/imageshield-dev-services-worker.json`, `infra/ecs/prod/services-worker.json` (intel-worker container env)
- Test: `tests/test_intel_config.py`, `tests/test_intel_limits_config.py` (new), `tests/test_ecs_task_defs.py` (existing required-field tests cover the task defs)

**Interfaces:**
- Produces: `imageshield.intel.limits.SearchLimits` (frozen dataclass, fields below); `IntelConfig.search_limits() -> SearchLimits`; `PipelineDeps.limits: SearchLimits`; `tests.intel_fakes.TEST_LIMITS`; `make_deps(..., limits: SearchLimits = TEST_LIMITS)`; `source_proposal_request(question, *, registry_tags, per_option, searches_per_option, coverage_ask=False)` (the `coverage_ask` flag is wired in Task 3; Task 1 adds the parameter with the default).

- [ ] **Step 1: Write the failing config tests**

Add to `tests/test_intel_config.py` `BASE` (keep it alphabetical-ish beside the existing intel keys):

```python
    "INTEL_MAX_WEB_SEARCHES_PER_RUN": "10",
    "INTEL_SOURCE_PROPOSAL_MAX_TOKENS": "24000",
    "INTEL_SEARCH_READ_MAX_TOKENS": "16000",
    "INTEL_MAX_PROPOSED_SOURCES_PER_ANSWER": "8",
    "INTEL_MAX_PROPOSED_SEARCHES_PER_ANSWER": "3",
    "INTEL_DISCOVERY_MIN_RESULT_PAGES": "5",
    "INTEL_DISCOVERY_MAX_RESULT_PAGES": "12",
    "INTEL_VALIDATION_SEARCHES": "3",
    "INTEL_VALIDATION_RESULT_PAGES": "8",
    "INTEL_SOURCE_PROPOSAL_COVERAGE_ASKS": "2",
    "INTEL_SUGGESTION_RESEARCH_ROUNDS": "4",
    "INTEL_RESEARCH_SEARCHES_PER_ANSWER": "3",
```

Create `tests/test_intel_limits_config.py`:

```python
"""Every search limit is configuration (spec 2026-10-10-intel-every-answer-researched §5.1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from imageshield.config import ConfigError
from imageshield.intel import bounds, model
from imageshield.intel.config import load_intel_config
from imageshield.intel.limits import SearchLimits
from tests.test_intel_config import BASE, clean_env, _env  # noqa: F401  (fixture re-export)

LIMIT_KEYS = (
    "INTEL_MAX_SOURCE_PROPOSAL_SEARCHES",
    "INTEL_SOURCE_PROPOSAL_MAX_TOKENS",
    "INTEL_MAX_PROPOSED_SOURCES_PER_ANSWER",
    "INTEL_MAX_PROPOSED_SEARCHES_PER_ANSWER",
    "INTEL_MAX_WEB_SEARCHES_PER_RUN",
    "INTEL_SEARCH_READ_MAX_TOKENS",
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


@pytest.mark.parametrize("key", LIMIT_KEYS)
def test_every_limit_is_required(clean_env: pytest.MonkeyPatch, key: str) -> None:
    _env(clean_env)
    clean_env.delenv(key)
    with pytest.raises(ConfigError, match=key):
        load_intel_config()


@pytest.mark.parametrize("key", LIMIT_KEYS)
def test_a_negative_limit_refuses_boot(clean_env: pytest.MonkeyPatch, key: str) -> None:
    _env(clean_env, **{key: "-1"})
    with pytest.raises(ConfigError, match=key):
        load_intel_config()


@pytest.mark.parametrize("key", sorted(set(LIMIT_KEYS) - ZERO_ALLOWED))
def test_zero_refuses_boot_where_at_least_one_is_required(
    clean_env: pytest.MonkeyPatch, key: str
) -> None:
    _env(clean_env, **{key: "0"})
    with pytest.raises(ConfigError, match=key):
        load_intel_config()


@pytest.mark.parametrize("key", sorted(ZERO_ALLOWED))
def test_zero_turns_coverage_and_research_off(clean_env: pytest.MonkeyPatch, key: str) -> None:
    _env(clean_env, **{key: "0"})
    load_intel_config()


def test_a_discovery_minimum_above_its_maximum_refuses_boot(
    clean_env: pytest.MonkeyPatch,
) -> None:
    _env(clean_env, INTEL_DISCOVERY_MIN_RESULT_PAGES="9", INTEL_DISCOVERY_MAX_RESULT_PAGES="8")
    with pytest.raises(ConfigError, match="INTEL_DISCOVERY_MIN_RESULT_PAGES"):
        load_intel_config()


def test_search_limits_carries_the_configured_values(clean_env: pytest.MonkeyPatch) -> None:
    _env(clean_env)
    assert load_intel_config().search_limits() == SearchLimits(
        proposed_sources_per_answer=8,
        proposed_searches_per_answer=3,
        discovery_min_result_pages=5,
        discovery_max_result_pages=12,
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
    "path", ["infra/ecs/imageshield-dev-services-worker.json", "infra/ecs/prod/services-worker.json"]
)
def test_both_task_definitions_set_every_limit_to_the_spec_value(path: str) -> None:
    doc = json.loads((REPO / path).read_text())
    (worker,) = [c for c in doc["containerDefinitions"] if c["name"] == "intel-worker"]
    env = {e["name"]: e["value"] for e in worker["environment"]}
    assert {k: env.get(k) for k in LIMIT_KEYS} == {
        "INTEL_MAX_SOURCE_PROPOSAL_SEARCHES": "40",
        "INTEL_SOURCE_PROPOSAL_MAX_TOKENS": "24000",
        "INTEL_MAX_PROPOSED_SOURCES_PER_ANSWER": "8",
        "INTEL_MAX_PROPOSED_SEARCHES_PER_ANSWER": "3",
        "INTEL_MAX_WEB_SEARCHES_PER_RUN": "10",
        "INTEL_SEARCH_READ_MAX_TOKENS": "16000",
        "INTEL_DISCOVERY_MIN_RESULT_PAGES": "5",
        "INTEL_DISCOVERY_MAX_RESULT_PAGES": "12",
        "INTEL_VALIDATION_SEARCHES": "3",
        "INTEL_VALIDATION_RESULT_PAGES": "8",
        "INTEL_SOURCE_PROPOSAL_COVERAGE_ASKS": "2",
        "INTEL_RESEARCH_SEARCHES_PER_ANSWER": "3",
        "INTEL_SUGGESTION_RESEARCH_ROUNDS": "4",
    }


def test_env_example_names_every_limit() -> None:
    text = (REPO / ".env.example").read_text()
    for key in LIMIT_KEYS:
        assert f"\n{key}=" in text, key
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_intel_limits_config.py tests/test_intel_config.py -v`
Expected: FAIL — `ModuleNotFoundError: imageshield.intel.limits`, and the BASE keys rejected or ignored.

- [ ] **Step 3: Create `src/imageshield/intel/limits.py`**

```python
"""Search volume and research limits (spec 2026-10-10-intel-every-answer-researched §5.1).

Every value is a required IntelConfig key with no code default (owner, 2026-10-10: "do not
hardcode anything"); this is the one value the run code reads them through. Safety limits stay
code constants in ``intel/bounds.py``."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SearchLimits:
    # Stage 1 (source proposal): candidates kept per answer, and of those, saved searches.
    proposed_sources_per_answer: int
    proposed_searches_per_answer: int
    # A saved-search read keeps at least / at most this many of its searches' result pages.
    discovery_min_result_pages: int
    discovery_max_result_pages: int
    # Stage 3: result pages fetched before a validation search counts as empty.
    validation_result_pages: int
    # Stage 1: extra asks for answers no source is aimed at (0: off).
    source_proposal_coverage_asks: int
    # Stage 4: research rounds per suggestion run (0: off), and searches per unbacked answer
    # per round.
    suggestion_research_rounds: int
    research_searches_per_answer: int
```

- [ ] **Step 4: Add the keys to `IntelConfig`**

In `src/imageshield/intel/config.py`, replace `intel_max_web_searches_per_run: int = 5` with `intel_max_web_searches_per_run: int` and add, after `intel_max_source_proposal_searches: int`:

```python
    # Search volume (spec 2026-10-10-intel-every-answer-researched §5.1). Required with no
    # default, like every key that spec gives none: the values live in the task definitions.
    intel_source_proposal_max_tokens: int
    intel_search_read_max_tokens: int
    intel_max_proposed_sources_per_answer: int
    intel_max_proposed_searches_per_answer: int
    intel_discovery_min_result_pages: int
    intel_discovery_max_result_pages: int
    intel_validation_searches: int
    intel_validation_result_pages: int
    # 0 turns the feature off.
    intel_source_proposal_coverage_asks: int
    intel_suggestion_research_rounds: int
    intel_research_searches_per_answer: int
```

Add these names to the `_positive` validator's field list: `intel_source_proposal_max_tokens`, `intel_search_read_max_tokens`, `intel_max_proposed_sources_per_answer`, `intel_max_proposed_searches_per_answer`, `intel_discovery_min_result_pages`, `intel_discovery_max_result_pages`, `intel_validation_searches`, `intel_validation_result_pages`, `intel_research_searches_per_answer`. Then add:

```python
    @field_validator("intel_source_proposal_coverage_asks", "intel_suggestion_research_rounds")
    @classmethod
    def _non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("must be zero or a positive integer")
        return value

    @model_validator(mode="after")
    def _discovery_pages_ordered(self) -> IntelConfig:
        if self.intel_discovery_min_result_pages > self.intel_discovery_max_result_pages:
            raise ValueError(
                "INTEL_DISCOVERY_MIN_RESULT_PAGES must not exceed INTEL_DISCOVERY_MAX_RESULT_PAGES"
            )
        return self

    def search_limits(self) -> SearchLimits:
        return SearchLimits(
            proposed_sources_per_answer=self.intel_max_proposed_sources_per_answer,
            proposed_searches_per_answer=self.intel_max_proposed_searches_per_answer,
            discovery_min_result_pages=self.intel_discovery_min_result_pages,
            discovery_max_result_pages=self.intel_discovery_max_result_pages,
            validation_result_pages=self.intel_validation_result_pages,
            source_proposal_coverage_asks=self.intel_source_proposal_coverage_asks,
            suggestion_research_rounds=self.intel_suggestion_research_rounds,
            research_searches_per_answer=self.intel_research_searches_per_answer,
        )
```

Import `model_validator` beside `field_validator` from pydantic if it is not already imported, and `from imageshield.intel.limits import SearchLimits`. Check how `load_intel_config` turns a pydantic `ValidationError` into `ConfigError` naming the key: the model-validator message must contain `INTEL_DISCOVERY_MIN_RESULT_PAGES` so the test's `match` holds (it does, through the message text).

- [ ] **Step 5: The model reads its limits from config**

In `src/imageshield/intel/model.py` delete the `_SOURCE_PROPOSAL_MAX_TOKENS` and `_VALIDATION_SEARCHES` constants (keep their explanatory comment, moved onto the methods below, and keep `_MAX_TOKENS` for the calls that make no search). Change the four methods:

```python
    async def discover(self, system: str, user: str) -> ModelCall[DiscoveryOutput]:
        return await self._search(
            DiscoveryOutput,
            system=system,
            user=user,
            max_uses=self._config.intel_max_web_searches_per_run,
            max_tokens=self._config.intel_search_read_max_tokens,
            effort=self._config.intel_search_read_effort,
        )

    async def propose_sources(self, system: str, user: str) -> ModelCall[SourceProposalOutput]:
        # Stage 1 answers for every option at once after up to INTEL_MAX_SOURCE_PROPOSAL_SEARCHES
        # searches, and adaptive thinking spends from the same budget (spec
        # 2026-10-08-intel-source-proposal-v2 §1): INTEL_SOURCE_PROPOSAL_MAX_TOKENS.
        return await self._search(
            SourceProposalOutput,
            system=system,
            user=user,
            max_uses=self._config.intel_max_source_proposal_searches,
            max_tokens=self._config.intel_source_proposal_max_tokens,
        )

    async def search_once(self, system: str, user: str) -> ModelCall[DiscoveryOutput]:
        return await self._search(
            DiscoveryOutput,
            system=system,
            user=user,
            max_uses=self._config.intel_validation_searches,
            max_tokens=self._config.intel_search_read_max_tokens,
            effort=self._config.intel_search_read_effort,
        )
```

and in `choose_results` pass `max_tokens=self._config.intel_search_read_max_tokens` to `_send`. Confirm `_search` and `_send` both take `max_tokens` (they do: `max_tokens: int = _MAX_TOKENS`).

- [ ] **Step 6: `PipelineDeps.limits` and its readers**

In `pipeline.py`: add `limits: SearchLimits` as the last field of `PipelineDeps` (with a docstring line: "``limits`` is IntelConfig.search_limits(), no default here for the same reason"), import `SearchLimits`, remove `DISCOVERY_MAX_RESULT_PAGES` / `DISCOVERY_MIN_RESULT_PAGES` from the bounds import, and in `_discover`:

```python
        target = min(
            ctx.deps.limits.discovery_max_result_pages,
            max(ctx.deps.limits.discovery_min_result_pages, len(call.output.candidates)),
        )
```

In `question_runs.py`: drop `MAX_PROPOSED_SEARCHES_PER_OPTION`, `MAX_PROPOSED_SOURCES_PER_OPTION`, `MAX_SEARCH_RESULTS_CHECKED` from the import; in `_source_proposal` read `limits = ctx.deps.limits` once and use `limits.proposed_sources_per_answer` (the `per_option=` argument and the `len(proposed[option]) >=` check) and `limits.proposed_searches_per_answer` (the `searches >=` check); pass `searches_per_option=limits.proposed_searches_per_answer` to `source_proposal_request`. In `_validate_search`: `for url in urls[: ctx.deps.limits.validation_result_pages]:`.

In `worker.py`'s `PipelineDeps(...)`: `limits=config.search_limits(),`.

In `source_choice.py`'s docstring, replace "MAX_PROPOSED_SOURCES_PER_OPTION" with "INTEL_MAX_PROPOSED_SOURCES_PER_ANSWER".

- [ ] **Step 7: The source-proposal prompt states the search cap and names no example (`sources-v3`)**

In `prompts.py` set `SOURCE_PROPOSAL_PROMPT_VERSION = "sources-v3"`. In `_SOURCE_PROPOSAL_SYSTEM`:
- delete the parenthetical `(for example, who deepfake abuse targets by gender and by age)` so the sentence reads "Every question about the person has such evidence. Search for it.";
- replace `- Mostly pages: at most two search_query candidates per option.` with `- Mostly pages: at most max_search_queries_per_option search_query candidates per option.`

Change the builder:

```python
def source_proposal_request(
    question: PromptSourceQuestion,
    *,
    registry_tags: Sequence[RegistryTag],
    per_option: int,
    searches_per_option: int,
    coverage_ask: bool = False,
) -> tuple[str, str]:
    """The question, its options with their tags, and the tag registry. Never a person.
    ``coverage_ask`` (spec 2026-10-10 §2.1) marks a follow-up ask for options an earlier search
    found nothing aimed at; Task 3 gives it its words."""
    payload: dict[str, Any] = {
        "question": question,
        "max_candidates_per_option": per_option,
        "max_search_queries_per_option": searches_per_option,
        "tag_registry": list(registry_tags),
    }
    if coverage_ask:
        payload["coverage_ask"] = True
    return _SOURCE_PROPOSAL_SYSTEM, json.dumps(payload, ensure_ascii=False)
```

(import `Any` from typing if it is not imported in prompts.py.)

- [ ] **Step 8: Test fakes, env example, task definitions**

`tests/intel_fakes.py`: add

```python
# The limits the tests written before 2026-10-10 assume (the old code constants), with coverage
# asks and research rounds off: a test that wants either passes its own SearchLimits.
TEST_LIMITS = SearchLimits(
    proposed_sources_per_answer=5,
    proposed_searches_per_answer=2,
    discovery_min_result_pages=5,
    discovery_max_result_pages=8,
    validation_result_pages=5,
    source_proposal_coverage_asks=0,
    suggestion_research_rounds=0,
    research_searches_per_answer=2,
)
```

and give `make_deps` a keyword `limits: SearchLimits = TEST_LIMITS` passed through as `limits=limits`. Update the `tests/test_intel_pipeline.py:923` comment to `# TEST_LIMITS.discovery_min_result_pages`.

`.env.example`: change `INTEL_MAX_SOURCE_PROPOSAL_SEARCHES=25` to `40`, and add under it:

```
# Search volume (required, no default; spec
# docs/superpowers/specs/2026-10-10-intel-every-answer-researched-design.md §5.1).
INTEL_SOURCE_PROPOSAL_MAX_TOKENS=24000
INTEL_MAX_PROPOSED_SOURCES_PER_ANSWER=8
INTEL_MAX_PROPOSED_SEARCHES_PER_ANSWER=3
INTEL_MAX_WEB_SEARCHES_PER_RUN=10
INTEL_SEARCH_READ_MAX_TOKENS=16000
INTEL_DISCOVERY_MIN_RESULT_PAGES=5
INTEL_DISCOVERY_MAX_RESULT_PAGES=12
INTEL_VALIDATION_SEARCHES=3
INTEL_VALIDATION_RESULT_PAGES=8
# Every answer is researched (0 turns each off).
INTEL_SOURCE_PROPOSAL_COVERAGE_ASKS=2
INTEL_SUGGESTION_RESEARCH_ROUNDS=4
INTEL_RESEARCH_SEARCHES_PER_ANSWER=3
```

Both task definitions, `intel-worker` container `environment`: set `INTEL_MAX_SOURCE_PROPOSAL_SEARCHES` to `"40"` and add the other twelve keys with the same values (edit the JSON with a script that preserves formatting and LF line endings — `open(..., newline="\n")`).

- [ ] **Step 9: Run the tests**

Run: `.venv/Scripts/python -m pytest tests/test_intel_limits_config.py tests/test_intel_config.py tests/test_intel_model.py tests/test_ecs_task_defs.py -v`
Expected: PASS. Then the run-code tests that touch the changed caps: `.venv/Scripts/python -m pytest tests/test_intel_question_runs.py tests/test_intel_pipeline.py -q` — PASS (TEST_LIMITS keeps the old numbers).

- [ ] **Step 10: Lint, type-check, commit**

```bash
.venv/Scripts/ruff format src/imageshield/intel/limits.py tests/test_intel_limits_config.py
.venv/Scripts/ruff check src/imageshield/intel tests/test_intel_limits_config.py tests/intel_fakes.py
.venv/Scripts/mypy src/imageshield/intel
git add -A src/imageshield/intel tests .env.example infra/ecs
git commit -m "feat(intel): every search limit is configuration, and each is raised

Spec 2026-10-10-intel-every-answer-researched §5.1 (owner: 'also increase search
limits', 'do not hardcode anything'). Thirteen required keys with no code default;
the moved constants are gone from bounds.py and model.py; sources-v3 states the
saved-search cap from the payload and names no example.

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

### Task 2: Schema 0053 and the run's research state

**Files:**
- Create: `migrations/0053_intel_research_rounds.up.sql`, `migrations/0053_intel_research_rounds.down.sql`
- Modify: `src/imageshield/intel/models.py` (`Run`)
- Modify: `src/imageshield/intel/store.py` (`RUN_COLUMNS`, `_WAIT_SQL`, `IntelStore.wait_run` protocol + `PostgresIntelStore.wait_run`)
- Modify: `src/imageshield/intel/pipeline.py` (`RunWait`, new `ResearchWait`)
- Modify: `src/imageshield/intel/worker.py` (`_wait`)
- Modify: `src/imageshield/intel/run_log.py` (`run_research_event`)
- Test: `tests/test_migrations.py` (or the repo's migration up/down test module — find it with `grep -rln "0050_intel_suggestion_waits" tests`), `tests/test_intel_research_state.py` (new)

**Interfaces:**
- Produces: `Run.research_round: int = 0`, `Run.research_source_ids: list[UUID] | None = None`, `Run.research_pass: dict[str, Any] | None = None`; `pipeline.ResearchWait(round: int, source_ids: tuple[UUID, ...], kept: dict[str, Any], answers: int)`; `RunWait.research: ResearchWait | None = None`; `IntelStore.wait_run(..., research: ResearchWait | None = None)`; `run_log.run_research_event(answers: int, round_no: int, max_rounds: int) -> tuple[str, dict[str, Any]]`.

- [ ] **Step 1: Write the migration**

`migrations/0053_intel_research_rounds.up.sql`:

```sql
-- Every answer is researched (spec 2026-10-10-intel-every-answer-researched §2.2, §3). A research
-- search is a saved search a weight suggestion registers for an answer it could not back: read
-- once by its own discovery run, never scheduled (enabled = false, disabled_reason
-- 'research_once'), proposed_for its question and answer.
ALTER TABLE intel_sources DROP CONSTRAINT intel_sources_origin_valid;
ALTER TABLE intel_sources ADD CONSTRAINT intel_sources_origin_valid
  CHECK (origin IN ('suggested', 'operator', 'news_watch', 'research'));
ALTER TABLE intel_sources ADD CONSTRAINT intel_sources_research_shape
  CHECK (origin <> 'research' OR (kind = 'search_query' AND proposed_for IS NOT NULL));
ALTER TABLE intel_sources DROP CONSTRAINT intel_sources_disabled_reason_valid;
ALTER TABLE intel_sources ADD CONSTRAINT intel_sources_disabled_reason_valid
  CHECK (disabled_reason IN ('too_short', 'unreachable', 'unmapped', 'research_once'));

-- A weight suggestion's research state across its waits: the round in progress, the research
-- searches it registered (retrieval reads them beside request.source_ids), and the pass that
-- started the current round (written if a later round cannot finish).
ALTER TABLE intel_runs
  ADD COLUMN research_round SMALLINT NOT NULL DEFAULT 0,
  ADD COLUMN research_source_ids UUID[],
  ADD COLUMN research_pass JSONB,
  ADD CONSTRAINT intel_runs_research_only_suggestions
    CHECK (kind = 'weight_suggestion'
           OR (research_round = 0 AND research_source_ids IS NULL AND research_pass IS NULL)),
  ADD CONSTRAINT intel_runs_research_round_non_negative CHECK (research_round >= 0);

-- The run log's one new step: "Searching again for 2 answers (round 1 of 4)".
ALTER TABLE intel_run_events DROP CONSTRAINT intel_run_events_kind_valid;
ALTER TABLE intel_run_events ADD CONSTRAINT intel_run_events_kind_valid
  CHECK (kind IN ('run_started', 'model_call_started', 'thinking', 'search',
                  'search_results', 'writing', 'continuing', 'model_call_finished',
                  'model_call_failed', 'model_call_skipped', 'run_finished',
                  'truncated', 'waiting', 'research_round'));
```

`migrations/0053_intel_research_rounds.down.sql`:

```sql
-- Reverses 0053. Refuses while any research source exists: deleting one would orphan the
-- documents and signals its read produced.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM intel_sources WHERE origin = 'research') THEN
    RAISE EXCEPTION 'research sources exist: disable and keep them, or delete their evidence first';
  END IF;
END
$$;
DELETE FROM intel_run_events WHERE kind = 'research_round';
ALTER TABLE intel_run_events DROP CONSTRAINT intel_run_events_kind_valid;
ALTER TABLE intel_run_events ADD CONSTRAINT intel_run_events_kind_valid
  CHECK (kind IN ('run_started', 'model_call_started', 'thinking', 'search',
                  'search_results', 'writing', 'continuing', 'model_call_finished',
                  'model_call_failed', 'model_call_skipped', 'run_finished',
                  'truncated', 'waiting'));
ALTER TABLE intel_runs
  DROP CONSTRAINT intel_runs_research_round_non_negative,
  DROP CONSTRAINT intel_runs_research_only_suggestions,
  DROP COLUMN research_pass,
  DROP COLUMN research_source_ids,
  DROP COLUMN research_round;
ALTER TABLE intel_sources DROP CONSTRAINT intel_sources_disabled_reason_valid;
ALTER TABLE intel_sources ADD CONSTRAINT intel_sources_disabled_reason_valid
  CHECK (disabled_reason IN ('too_short', 'unreachable', 'unmapped'));
ALTER TABLE intel_sources DROP CONSTRAINT intel_sources_research_shape;
ALTER TABLE intel_sources DROP CONSTRAINT intel_sources_origin_valid;
ALTER TABLE intel_sources ADD CONSTRAINT intel_sources_origin_valid
  CHECK (origin IN ('suggested', 'operator', 'news_watch'));
```

- [ ] **Step 2: Write the failing state test**

Create `tests/test_intel_research_state.py`:

```python
"""A suggestion's research state survives its waits (spec 2026-10-10 §2.2, §3)."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from imageshield.intel.pipeline import ResearchWait
from imageshield.intel.question_store import PostgresQuestionStore
from imageshield.intel.run_log import run_research_event
from imageshield.intel.store import PostgresIntelStore
from tests.intel_fakes import NOW, claim, seed_quiz_vocabulary
from tests.question_fakes import QUESTION


async def test_wait_run_keeps_the_research_state(intel_pool: AsyncConnectionPool) -> None:
    await seed_quiz_vocabulary(intel_pool)
    store = PostgresIntelStore(intel_pool)
    queued = await PostgresQuestionStore(intel_pool).register_and_queue_suggestion(
        {**QUESTION, "type": "mutable", "cap": 8}, [], operator="ann"
    )
    claimed = await claim(intel_pool)
    assert claimed is not None and claimed.run_id == queued.run_id
    source_ids = (uuid4(), uuid4())
    kept = {"model_id": "m", "options": [], "backed": {}, "searches": {"Bumble": []}}
    assert await store.wait_run(
        claimed.run_id,
        attempts=claimed.attempts,
        outcome={"research_rounds": 1},
        awaiting=list(source_ids),
        deadline=NOW + timedelta(minutes=45),
        not_before=None,
        research=ResearchWait(round=1, source_ids=source_ids, kept=kept, answers=1),
    )
    (run,) = [r for r in await store.list_runs(cursor=None, limit=5) if r.run_id == claimed.run_id]
    assert (run.research_round, run.research_source_ids, run.research_pass) == (
        1,
        list(source_ids),
        kept,
    )


async def test_a_wait_without_research_leaves_the_state_alone(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    store = PostgresIntelStore(intel_pool)
    await PostgresQuestionStore(intel_pool).register_and_queue_suggestion(
        {**QUESTION, "type": "mutable", "cap": 8}, [], operator="ann"
    )
    claimed = await claim(intel_pool)
    assert claimed is not None
    await store.wait_run(
        claimed.run_id,
        attempts=claimed.attempts,
        outcome={},
        awaiting=[uuid4()],
        deadline=NOW + timedelta(minutes=45),
        not_before=None,
    )
    (run,) = [r for r in await store.list_runs(cursor=None, limit=5) if r.run_id == claimed.run_id]
    assert (run.research_round, run.research_source_ids, run.research_pass) == (0, None, None)


@pytest.mark.parametrize(
    ("answers", "round_no", "text"),
    [
        (1, 1, "Searching again for 1 answer (round 1 of 4)"),
        (2, 3, "Searching again for 2 answers (round 3 of 4)"),
    ],
)
def test_the_research_event_words(answers: int, round_no: int, text: str) -> None:
    assert run_research_event(answers, round_no, 4) == (
        text,
        {"answers": answers, "round": round_no, "max_rounds": 4},
    )
```

(If `list_runs` returns a page object rather than a list, read its `runs`; if a get-one-run helper exists on `PostgresQuestionStore` — `get_run(run_id)` does — use `await PostgresQuestionStore(intel_pool).get_run(claimed.run_id)` instead of filtering a page.)

- [ ] **Step 3: Run it to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_intel_research_state.py -v`
Expected: FAIL — `ImportError: cannot import name 'ResearchWait'`.

- [ ] **Step 4: Implement**

`models.py` — `Run` gains, after `wait_deadline`:

```python
    # A weight suggestion's research state across its waits (migration 0053, spec 2026-10-10
    # §2.2): the round in progress, the research searches it registered, and the pass that
    # started the current round. 0 / null on every other run.
    research_round: int = 0
    research_source_ids: list[UUID] | None = None
    research_pass: dict[str, Any] | None = None
```

`store.py` — `RUN_COLUMNS` appends `, research_round, research_source_ids, research_pass`. `_WAIT_SQL` becomes:

```python
_WAIT_SQL = """
    UPDATE intel_runs SET status = 'queued', attempts = attempts - 1, outcome = %(outcome)s,
           awaiting_source_ids = %(awaiting)s, wait_deadline = %(deadline)s,
           not_before = %(not_before)s, lease_expires_at = NULL,
           research_round = coalesce(%(research_round)s, research_round),
           research_source_ids = coalesce(%(research_source_ids)s::uuid[], research_source_ids),
           research_pass = coalesce(%(research_pass)s, research_pass)
     WHERE run_id = %(run_id)s AND status = 'running' AND attempts = %(attempts)s
    RETURNING 1
"""
```

and both `wait_run` signatures (protocol and Postgres) gain `research: ResearchWait | None = None`, passing:

```python
                    "research_round": research.round if research is not None else None,
                    "research_source_ids": (
                        list(research.source_ids) if research is not None else None
                    ),
                    "research_pass": Jsonb(research.kept) if research is not None else None,
```

Import `ResearchWait` under `if TYPE_CHECKING:` in store.py if importing pipeline at module level would cycle (pipeline imports store); the parameter annotation is then a string via `from __future__ import annotations`, which store.py already has.

`pipeline.py` — beside `RunWait`:

```python
@dataclass(frozen=True)
class ResearchWait:
    """A research round's state to keep while the run waits (spec 2026-10-10 §2.2, migration
    0053): the round now in progress, every research search registered so far, the pass that
    started this round (written if a later round cannot finish), and how many answers it
    searches again (for the run log)."""

    round: int
    source_ids: tuple[UUID, ...]
    kept: dict[str, Any]
    answers: int
```

and `RunWait` gains `research: ResearchWait | None = None` (after `not_before`).

`run_log.py`:

```python
def run_research_event(answers: int, round_no: int, max_rounds: int) -> tuple[str, dict[str, Any]]:
    """``Searching again for 2 answers (round 1 of 4)`` (spec 2026-10-10 §2.3)."""
    text = (
        f"Searching again for {_plural(answers, 'answer', 'answers')}"
        f" (round {round_no} of {max_rounds})"
    )
    return text, {"answers": answers, "round": round_no, "max_rounds": max_rounds}
```

`worker.py` `_wait` — before the `waiting` append:

```python
    if wait.research is not None:
        await run_log.append(
            "research_round",
            *run_research_event(
                wait.research.answers,
                wait.research.round,
                deps.limits.suggestion_research_rounds,
            ),
        )
```

and pass `research=wait.research` to `deps.store.wait_run(...)`. Import `run_research_event`.

- [ ] **Step 5: Migration up/down test**

Find the module that applies migrations up and down in order (`grep -rln "0050_intel_suggestion_waits" tests`) and add the same assertions it makes for 0050: 0053 up then down then up succeeds on an empty database; with a `research` source present the down raises. If the suite applies every migration's down/up generically, add only the refusal test:

```python
async def test_0053_down_refuses_while_research_sources_exist(intel_pool) -> None:
    async with intel_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO intel_sources (kind, query_text, tags, check_every_hours, enabled,"
            " disabled_reason, created_by, origin, proposed_for)"
            " VALUES ('search_query', 'q', '{}', 168, false, 'research_once', 'system:research',"
            " 'research', '{\"question_key\": \"platforms\", \"option\": \"Bumble\"}')"
        )
    down = (REPO / "migrations/0053_intel_research_rounds.down.sql").read_text()
    with pytest.raises(Exception, match="research sources exist"):
        async with intel_pool.connection() as conn:
            await conn.execute(down)
```

- [ ] **Step 6: Run the tests**

Run: `.venv/Scripts/python -m pytest tests/test_intel_research_state.py <the migration test module> tests/test_intel_worker.py -v`
Expected: PASS.

- [ ] **Step 7: Lint, type-check, commit**

```bash
.venv/Scripts/ruff format tests/test_intel_research_state.py
.venv/Scripts/ruff check src/imageshield/intel tests/test_intel_research_state.py
.venv/Scripts/mypy src/imageshield/intel
git add migrations/0053_intel_research_rounds.*.sql src/imageshield/intel tests
git commit -m "feat(intel): migration 0053 — research sources and a suggestion's research state

Spec 2026-10-10 §3: origin 'research' + disabled_reason 'research_once' on
intel_sources; research_round / research_source_ids / research_pass on intel_runs,
written by wait_run; a 'research_round' run event.

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

### Task 3: Stage 1 asks again for uncovered answers

**Files:**
- Modify: `src/imageshield/intel/question_runs.py` (`_source_proposal`)
- Modify: `src/imageshield/intel/prompts.py` (`_SOURCE_PROPOSAL_SYSTEM` coverage paragraph)
- Test: `tests/test_intel_question_runs.py`

**Interfaces:**
- Consumes: `SearchLimits.source_proposal_coverage_asks`, `source_proposal_request(..., coverage_ask=True)` (Task 1).
- Produces: source-proposal outcome `options[].uncovered: bool`, counts `source_proposal_coverage_asks`, `source_proposal_uncovered`, `source_proposal_coverage_<outcome>`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_intel_question_runs.py` (imports: `from dataclasses import replace`, `from tests.intel_fakes import TEST_LIMITS`):

```python
def _proposal_for(*options: str) -> SourceProposalOutput:
    return SourceProposalOutput(
        options=[
            ProposedOptionSources(
                option=o,
                candidates=[
                    ProposedSource(
                        kind="research",
                        source_url=f"https://r.example/{o.lower()}",
                        reason="findings for this answer",
                    )
                ],
            )
            for o in options
        ]
    )


class _ScriptedSources(FakeQuestionModel):
    """Answers each propose_sources call with the next scripted output."""

    def __init__(self, *outputs: SourceProposalOutput, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._outputs = list(outputs)

    async def propose_sources(self, system: str, user: str):  # type: ignore[override]
        self.sources = self._outputs.pop(0) if self._outputs else SourceProposalOutput()
        return await super().propose_sources(system, user)


async def test_an_uncovered_answer_gets_a_coverage_ask_naming_only_it(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    await PostgresQuestionStore(intel_pool).queue_source_proposal(QUESTION, operator="ann")
    model = _ScriptedSources(_proposal_for("Instagram"), _proposal_for("Bumble"))
    limits = replace(TEST_LIMITS, source_proposal_coverage_asks=1)
    result = await run_once(intel_pool, make_deps(intel_pool, FakeFetcher({}), model, limits=limits))
    assert result.status == "completed", result
    assert model.source_proposal_calls == 2
    second = json.loads(model.source_proposal_users[1])
    assert second["coverage_ask"] is True
    assert [o["option"] for o in second["question"]["options"]] == ["Bumble"]
    instagram, bumble = result.outcome["options"]
    assert bumble["proposed"] and not bumble["uncovered"] and not instagram["uncovered"]
    assert result.outcome["source_proposal_coverage_asks"] == 1


async def test_a_still_uncovered_answer_is_reported_and_the_run_completes(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    await PostgresQuestionStore(intel_pool).queue_source_proposal(QUESTION, operator="ann")
    model = _ScriptedSources(_proposal_for("Instagram"))  # every later ask finds nothing
    limits = replace(TEST_LIMITS, source_proposal_coverage_asks=2)
    result = await run_once(intel_pool, make_deps(intel_pool, FakeFetcher({}), model, limits=limits))
    assert result.status == "completed"
    assert model.source_proposal_calls == 3
    _, bumble = result.outcome["options"]
    assert bumble["uncovered"] is True
    assert result.outcome["source_proposal_uncovered"] == 1


async def test_with_coverage_asks_off_no_second_call_is_made(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    await PostgresQuestionStore(intel_pool).queue_source_proposal(QUESTION, operator="ann")
    model = _ScriptedSources(_proposal_for("Instagram"))
    result = await run_once(intel_pool, make_deps(intel_pool, FakeFetcher({}), model))
    assert model.source_proposal_calls == 1 and result.outcome["options"][1]["uncovered"] is True


async def test_a_failed_coverage_ask_keeps_the_first_calls_sources(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    await PostgresQuestionStore(intel_pool).queue_source_proposal(QUESTION, operator="ann")

    class _SecondCallRefused(_ScriptedSources):
        async def propose_sources(self, system: str, user: str):  # type: ignore[override]
            if self.source_proposal_calls == 1:
                self.sources_outcome = "refusal"
            return await super().propose_sources(system, user)

    model = _SecondCallRefused(_proposal_for("Instagram"), _proposal_for("Bumble"))
    limits = replace(TEST_LIMITS, source_proposal_coverage_asks=1)
    result = await run_once(intel_pool, make_deps(intel_pool, FakeFetcher({}), model, limits=limits))
    assert result.status == "completed"
    instagram, bumble = result.outcome["options"]
    assert instagram["proposed"] and bumble["uncovered"] is True
    assert result.outcome["source_proposal_coverage_refusal"] == 1
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_intel_question_runs.py -k "coverage or uncovered" -v`
Expected: FAIL — one call made, no `uncovered` key.

- [ ] **Step 3: Implement**

Refactor `_source_proposal` so the "clean, drop known hits, apply caps" body becomes a helper used by the first call and every coverage ask:

```python
async def _keep_candidates(
    ctx: _Ctx,
    output: SourceProposalOutput,
    *,
    options: Sequence[str],
    existing: Mapping[str, Sequence[Source]],
    proposed: dict[str, list[Candidate]],
) -> None:
    """One proposal call's answer through the same cleaning, known-hit drop and per-answer caps
    (spec §4.10 stage 1), appended to ``proposed``."""
    limits = ctx.deps.limits
    cleaned = clean_candidates(
        output,
        options=options,
        existing={o: frozenset(source_identity(s) for s in existing[o]) for o in options},
        counts=ctx.counts,
    )
    hits = await ctx.deps.questions.known_hits(
        sorted(
            {
                str(url_hash(c.source_url))
                for candidates in cleaned.values()
                for c in candidates
                if c.source_url is not None
            }
        )
    )
    for option, candidates in cleaned.items():
        searches = sum(1 for c in proposed[option] if c.kind == "search_query")
        for candidate in candidates:
            if candidate.source_url is not None and url_hash(candidate.source_url) in hits:
                ctx.counts["candidate_dropped_known_hit_location"] += 1
            elif len(proposed[option]) >= limits.proposed_sources_per_answer:
                ctx.counts["candidate_dropped_over_cap"] += 1
            elif (
                candidate.kind == "search_query"
                and searches >= limits.proposed_searches_per_answer
            ):
                ctx.counts["candidate_dropped_search_over_cap"] += 1
            else:
                searches += candidate.kind == "search_query"
                proposed[option].append(candidate)
```

(`clean_candidates` deduplicates against `existing`; a coverage ask's candidates are also deduplicated against what `proposed` already holds for the option: pass `existing={o: frozenset(source_identity(s) for s in existing[o]) | frozenset(c.identity for c in proposed[o]) ...}` if `Candidate` exposes an identity — check `source_choice.Candidate` and use its identity property or `identity(c.kind, c.source_url, c.query_text)`.)

After the first call's `_keep_candidates`, and only when the first call produced output (status still "completed"), loop:

```python
            for _ in range(ctx.deps.limits.source_proposal_coverage_asks):
                uncovered = [o for o in request.options if not proposed[o] and not existing[o]]
                if not uncovered:
                    break
                ctx.counts["source_proposal_coverage_asks"] += 1
                ask = request.model_copy(update={"options": tuple(uncovered)})
                system, user = source_proposal_request(
                    prompt_source_question(ask, tags_by_option),
                    registry_tags=(
                        prompt_registry(vocabulary, set(all_tags)) if vocabulary is not None else []
                    ),
                    per_option=ctx.deps.limits.proposed_sources_per_answer,
                    searches_per_option=ctx.deps.limits.proposed_searches_per_answer,
                    coverage_ask=True,
                )
                try:
                    more = await _call_model(ctx, lambda: ctx.deps.model.propose_sources(system, user))
                except (_Stop, _CallCap) as stopped:
                    reason = stopped.reason if isinstance(stopped, _Stop) else "run_call_cap"
                    ctx.counts[f"source_proposal_coverage_{reason}"] += 1
                    break
                if more.output is None:
                    ctx.counts[f"source_proposal_coverage_{more.outcome}"] += 1
                    break
                await _keep_candidates(
                    ctx, more.output, options=uncovered, existing=existing, proposed=proposed
                )
```

(`QuestionRequest.options` must be a tuple or list for `model_copy`; match its declared type. `prompt_source_question(ask, tags_by_option)` only lists `ask.options`.) Bind `system, user` per iteration through default arguments in the lambda (`lambda s=system, u=user: ctx.deps.model.propose_sources(s, u)`) so ruff's B023 is satisfied.

The outcome's per-option dict gains `"uncovered": not proposed[option] and not existing[option]`, and once after the loop: `ctx.counts["source_proposal_uncovered"] += sum(1 for o in request.options if not proposed[o] and not existing[o])` (only when non-zero, so the counter is absent otherwise — Counter keeps zero keys if you `+= 0`; guard with `if n:`).

In `prompts.py`, add to `_SOURCE_PROPOSAL_SYSTEM` (before "Each candidate is:"):

```
If coverage_ask is true, an earlier search found no source aimed at these options. Search for
each one specifically and, when nothing speaks to it directly, for the closest evidence, saying in
each reason how it applies to that option.
```

- [ ] **Step 4: Run the tests**

Run: `.venv/Scripts/python -m pytest tests/test_intel_question_runs.py -k "source_proposal or coverage or uncovered" -v`
Expected: PASS (new and existing stage-1 tests).

- [ ] **Step 5: Commit**

```bash
.venv/Scripts/ruff check src/imageshield/intel tests/test_intel_question_runs.py
.venv/Scripts/mypy src/imageshield/intel
git add src/imageshield/intel/question_runs.py src/imageshield/intel/prompts.py tests/test_intel_question_runs.py
git commit -m "feat(intel): stage 1 asks again for answers no source is aimed at

Spec 2026-10-10 §2.1: up to INTEL_SOURCE_PROPOSAL_COVERAGE_ASKS follow-up proposal
calls naming only the uncovered answers; a still-uncovered answer is reported
(options[].uncovered) and the run completes.

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

### Task 4: The suggestion asks for research searches and records research per answer

**Files:**
- Modify: `src/imageshield/intel/schemas.py` (`SuggestedOptionWeight.research_searches`)
- Modify: `src/imageshield/intel/prompts.py` (`SUGGEST_PROMPT_VERSION = "suggest-v2"`, `_SUGGEST_SYSTEM`, `PromptSuggestionOption.searched`, `suggestion_request` payload)
- Modify: `src/imageshield/intel/suggestion.py` (`OptionSuggestion` research fields + `from_json`, `validate_suggestion` carries searches, new `is_backed`, `plan_research`, `attach_research`, `prompt_suggestion_question(..., searched=...)`, `suggestion_options` passes `research`, `render_suggestion` adds `research_round`)
- Test: `tests/test_intel_suggestion.py` (the pure-module tests; find with `grep -rln "validate_suggestion" tests`), `tests/test_intel_prompts.py` (or wherever prompt fixed-text tests live: `grep -rln "SUGGEST_PROMPT_VERSION\|_SUGGEST_SYSTEM" tests`)

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `SuggestedOptionWeight.research_searches: list[str] = Field(default_factory=list)`
  - `OptionResearch(rounds: int, searches: tuple[ResearchSearch, ...], exhausted: bool)` and `ResearchSearch(round: int, query: str)` frozen dataclasses in `suggestion.py`, with `as_json()`
  - `OptionSuggestion.research_searches: tuple[str, ...] = ()` (the model's ask; never stored) and `OptionSuggestion.research: OptionResearch | None = None` (stored in `as_json()` as `"research"` when not None); `OptionSuggestion.from_json(raw: Mapping[str, Any]) -> OptionSuggestion`
  - `is_backed(option: OptionSuggestion, context: Mapping[UUID, ContextSignal]) -> bool`
  - `plan_research(options: Sequence[OptionSuggestion], *, searched: Mapping[str, Sequence[Mapping[str, Any]]], per_answer: int, counts: Counter[str]) -> dict[str, list[str]]`
  - `attach_research(options: Sequence[OptionSuggestion], *, backed: Mapping[str, bool], searched: Mapping[str, Sequence[Mapping[str, Any]]], researching: bool) -> list[OptionSuggestion]`
  - `prompt_suggestion_question(request, tags_by_option, vocabulary, *, searched=None)`; `suggestion_request(question, evidence, *, registry_tags, research_round, max_rounds, searches_per_answer)`

- [ ] **Step 1: Write the failing pure tests**

In the suggestion pure-test module add:

```python
from collections import Counter
from uuid import uuid4

from imageshield.intel.suggestion import (
    OptionResearch,
    OptionSuggestion,
    ResearchSearch,
    attach_research,
    is_backed,
    plan_research,
)


def _option(name: str, deduction: int | None, ids=(), searches=()) -> OptionSuggestion:
    return OptionSuggestion(name, deduction, "r", tuple(ids), (), None, research_searches=tuple(searches))


def test_an_answer_is_backed_only_with_a_number_and_corroborated_cited_evidence() -> None:
    listed = make_context_signal(trust="listed")  # the module's existing ContextSignal helper
    web = make_context_signal(trust="web", publisher="a.example")
    context = {listed.signal_id: listed, web.signal_id: web}
    assert is_backed(_option("A", 4, [listed.signal_id]), context)
    assert not is_backed(_option("A", None, [listed.signal_id]), context)
    assert not is_backed(_option("A", 4, []), context)
    assert not is_backed(_option("A", 4, [web.signal_id]), context)  # one web publisher


def test_plan_research_drops_people_repeats_and_overflow() -> None:
    counts: Counter[str] = Counter()
    planned = plan_research(
        [
            _option("A", None, searches=["first query", "call +44 20 7946 0958", "FIRST  query"]),
            _option("B", None, searches=["one", "two", "three"]),
            _option("C", None, searches=[]),
        ],
        searched={"B": [{"round": 1, "query": "one"}]},
        per_answer=1,
        counts=counts,
    )
    assert planned == {"A": ["first query"], "B": ["two"]}
    assert counts["research_search_names_a_person"] == 1
    assert counts["research_search_repeated"] == 2
    assert counts["research_search_over_cap"] == 1


def test_attach_research_counts_rounds_and_marks_unbacked_answers_exhausted() -> None:
    searched = {"A": [{"round": 1, "query": "q1"}, {"round": 2, "query": "q2"}]}
    out = attach_research(
        [_option("A", None), _option("B", 3)],
        backed={"A": False, "B": True},
        searched=searched,
        researching=True,
    )
    assert out[0].research == OptionResearch(
        rounds=2, searches=(ResearchSearch(1, "q1"), ResearchSearch(2, "q2")), exhausted=True
    )
    assert out[1].research == OptionResearch(rounds=0, searches=(), exhausted=False)
    assert out[0].as_json()["research"] == {
        "rounds": 2,
        "searches": [{"round": 1, "query": "q1"}, {"round": 2, "query": "q2"}],
        "exhausted": True,
    }


def test_with_research_off_nothing_is_exhausted() -> None:
    (only,) = attach_research(
        [_option("A", None)], backed={"A": False}, searched={}, researching=False
    )
    assert only.research == OptionResearch(rounds=0, searches=(), exhausted=False)


def test_an_option_round_trips_through_its_json() -> None:
    original = _option("A", 4, [uuid4()])
    assert OptionSuggestion.from_json(original.as_json()) == original
```

(`make_context_signal` — use the helper the module already has for building `ContextSignal`s; if none, construct `ContextSignal(...)` with the fields its dataclass declares, `status="active"`.) In the prompt fixed-text test module add:

```python
from imageshield.intel import prompts
from tests.question_fakes import QUESTION


def test_no_fixture_answer_appears_in_the_fixed_prompt_text() -> None:
    fixed = prompts._SOURCE_PROPOSAL_SYSTEM + prompts._SUGGEST_SYSTEM
    for answer in QUESTION["options"]:
        assert answer not in fixed
    for word in ("deepfake abuse targets by gender", "by age"):
        assert word not in prompts._SOURCE_PROPOSAL_SYSTEM


def test_rounds_zero_asks_for_no_searches() -> None:
    _, user = prompts.suggestion_request(
        {"key": "k", "prompt": "p", "type": None, "cap": None, "options": []},
        [],
        registry_tags=[],
        research_round=1,
        max_rounds=0,
        searches_per_answer=3,
    )
    assert json.loads(user)["research"] is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/Scripts/python -m pytest <suggestion pure-test module> <prompt test module> -v`
Expected: FAIL — names not defined.

- [ ] **Step 3: Implement the schema and prompt**

`schemas.py` `SuggestedOptionWeight` gains `research_searches: list[str] = Field(default_factory=list)`.

`prompts.py`: `SUGGEST_PROMPT_VERSION = "suggest-v2"`; `PromptSuggestionOption` gains `searched: list[str]`. Append to `_SUGGEST_SYSTEM`, after the `new_tag` bullet:

```
- research_searches: only when "research" is not null and you cannot back this option's deduction
  with evidence from at least two independent publishers. Up to research.searches_per_answer web
  searches that would find evidence about THIS option. In research.round 1, search for direct
  evidence. From round 2 on, search for the closest evidence and say in the query what angle it
  takes. Never repeat a search listed in the option's "searched", and never name a private
  individual. Give none when no search could help.

If no evidence speaks to an option directly, you may back it with the closest evidence, saying in
its rationale how that evidence applies. The corroboration rule still applies.
```

`suggestion_request` gains keyword-only `research_round: int`, `max_rounds: int`, `searches_per_answer: int`, and the payload gains:

```python
            "research": (
                {"round": research_round, "max_rounds": max_rounds,
                 "searches_per_answer": searches_per_answer}
                if max_rounds > 0 and research_round <= max_rounds
                else None
            ),
```

- [ ] **Step 4: Implement the suggestion helpers**

In `suggestion.py`:

```python
@dataclass(frozen=True)
class ResearchSearch:
    round: int
    query: str

    def as_json(self) -> dict[str, Any]:
        return {"round": self.round, "query": self.query}


@dataclass(frozen=True)
class OptionResearch:
    """What research did for one answer (spec 2026-10-10 §2.3). ``exhausted``: still unbacked
    after research ended. The backend reads the flag and never re-derives it."""

    rounds: int
    searches: tuple[ResearchSearch, ...]
    exhausted: bool

    def as_json(self) -> dict[str, Any]:
        return {
            "rounds": self.rounds,
            "searches": [s.as_json() for s in self.searches],
            "exhausted": self.exhausted,
        }
```

`OptionSuggestion` gains `research_searches: tuple[str, ...] = ()` and `research: OptionResearch | None = None` (defaults, after `new_tag`); `as_json` adds `"research": self.research.as_json()` only when `self.research is not None`; add:

```python
    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> OptionSuggestion:
        """A kept pass's entry back to an OptionSuggestion (spec 2026-10-10 §2.2.7)."""
        new_tag = raw.get("new_tag")
        return cls(
            option=str(raw["option"]),
            deduction=raw.get("deduction"),
            rationale=str(raw.get("rationale", "")),
            signal_ids=tuple(UUID(str(s)) for s in raw.get("signal_ids") or ()),
            suggested_tags=tuple(raw.get("suggested_tags") or ()),
            new_tag=SuggestedTag(**new_tag) if isinstance(new_tag, dict) else None,
        )
```

In `validate_suggestion`, pass `research_searches=tuple(chosen.research_searches)` to each `OptionSuggestion`. Then add:

```python
def is_backed(option: OptionSuggestion, context: Mapping[UUID, ContextSignal]) -> bool:
    """Backed (spec 2026-10-10 §2): a number, and its cited evidence passes the one
    corroboration predicate. The same judgement ``suggestion_options`` reports."""
    if option.deduction is None or not option.signal_ids:
        return False
    active = [context[i] for i in option.signal_ids if i in context and context[i].status == "active"]
    return bool(active) and not uncorroborated(active)


def plan_research(
    options: Sequence[OptionSuggestion],
    *,
    searched: Mapping[str, Sequence[Mapping[str, Any]]],
    per_answer: int,
    counts: Counter[str],
) -> dict[str, list[str]]:
    """Each unbacked answer's next searches (spec 2026-10-10 §2.2.3): the model's own, at most
    ``per_answer``, never one that names a person, never one this run already made."""
    seen = {query_key(str(s["query"])) for entries in searched.values() for s in entries}
    planned: dict[str, list[str]] = {}
    for option in options:
        kept: list[str] = []
        for raw in option.research_searches:
            query = normalise(raw)
            if not query:
                continue
            if contains_pii(query):
                counts["research_search_names_a_person"] += 1
                continue
            key = query_key(query)
            if key in seen:
                counts["research_search_repeated"] += 1
                continue
            if len(kept) >= per_answer:
                counts["research_search_over_cap"] += 1
                continue
            seen.add(key)
            kept.append(query)
        if kept:
            planned[option.option] = kept
    return planned


def attach_research(
    options: Sequence[OptionSuggestion],
    *,
    backed: Mapping[str, bool],
    searched: Mapping[str, Sequence[Mapping[str, Any]]],
    researching: bool,
) -> list[OptionSuggestion]:
    """Each option with its research record. ``researching``: research rounds were configured,
    so an answer still unbacked is ``exhausted``; with research off, none is."""
    out: list[OptionSuggestion] = []
    for option in options:
        entries = [
            ResearchSearch(int(s["round"]), str(s["query"])) for s in searched.get(option.option, ())
        ]
        out.append(
            replace(
                option,
                research=OptionResearch(
                    rounds=len({s.round for s in entries}),
                    searches=tuple(entries),
                    exhausted=researching and not backed.get(option.option, False),
                ),
            )
        )
    return out
```

Imports: `from dataclasses import dataclass, replace`; `from imageshield.intel.pii import contains_pii, mask`; `from imageshield.intel.source_choice import query_key` (source_choice does not import suggestion — no cycle).

`prompt_suggestion_question` gains `*, searched: Mapping[str, Sequence[Mapping[str, Any]]] | None = None` and each `PromptSuggestionOption` gets `searched=[str(s["query"]) for s in (searched or {}).get(option, ())]`.

`suggestion_options` adds `"research": raw.get("research")` to each row. `render_suggestion` adds `"research_round": run.research_round`.

- [ ] **Step 5: Run the tests**

Run: `.venv/Scripts/python -m pytest <suggestion pure-test module> <prompt test module> tests/test_intel_question_runs.py -k "suggest" -v`
Expected: PASS. Existing suggestion run tests still pass: `_suggest` is not changed yet, but `suggestion_request` now needs the three keywords — Task 5 wires them; to keep this task green, pass `research_round=1, max_rounds=ctx.deps.limits.suggestion_research_rounds, searches_per_answer=ctx.deps.limits.research_searches_per_answer` at the existing call site in `_suggest` now.

- [ ] **Step 6: Commit**

```bash
.venv/Scripts/ruff check src/imageshield/intel tests
.venv/Scripts/mypy src/imageshield/intel
git add src/imageshield/intel tests
git commit -m "feat(intel): suggest-v2 asks for research searches; each answer records its research

Spec 2026-10-10 §2.2.3, §2.3, §2.4: research_searches per unbacked option (general
rule, no examples), is_backed over the one corroboration predicate, plan_research
(no person, no repeat, capped), research {rounds, searches, exhausted} on every
option read, research_round on the poll.

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

### Task 5: Research rounds in the weight-suggestion run

**Files:**
- Modify: `src/imageshield/intel/question_runs.py` (`_weight_suggestion`, `_suggest`, `_unread`, new `_SuggestStep`, `_ResearchState`, `_write`)
- Modify: `src/imageshield/intel/question_store.py` (`QuestionStore.register_research_sources` protocol + Postgres implementation, `_REGISTER_RESEARCH_SQL`)
- Test: `tests/test_intel_question_runs.py`

**Interfaces:**
- Consumes: Task 1 `SearchLimits`; Task 2 `ResearchWait`, `RunWait.research`, `Run.research_*`; Task 4 `is_backed`, `plan_research`, `attach_research`, `OptionSuggestion.from_json`, `suggestion_request(..., research_round, max_rounds, searches_per_answer)`, `prompt_suggestion_question(..., searched=)`.
- Produces: `QuestionStore.register_research_sources(run_id: UUID, *, question_key: str, searches: Sequence[ResearchSourceRequest]) -> list[UUID]`; `question_store.ResearchSourceRequest(option: str, query: str, tags: tuple[str, ...])`.

- [ ] **Step 1: Write the failing integration tests**

Add to `tests/test_intel_question_runs.py` (reuse `_done_run`, `seed_signal`, `_rows`, `_open_runs`, `ARTICLE`, `_found_article`, `SUGGEST_REQUEST`):

```python
RESEARCH = replace(TEST_LIMITS, suggestion_research_rounds=2, research_searches_per_answer=1)


def _scripted_suggestions(*answers: Callable[[dict[str, Any]], SuggestionOutput]):
    """The n-th suggestion call answers with the n-th function (the last repeats)."""
    calls = {"n": 0}

    def answer(payload: dict[str, Any]) -> SuggestionOutput:
        fn = answers[min(calls["n"], len(answers) - 1)]
        calls["n"] += 1
        return fn(payload)

    return answer


def _instagram_backed_bumble_searches(query: str) -> Callable[[dict[str, Any]], SuggestionOutput]:
    def answer(payload: dict[str, Any]) -> SuggestionOutput:
        listed = [e["signal_id"] for e in payload["evidence"] if e["trust"] == "listed"]
        return SuggestionOutput(
            options=[
                SuggestedOptionWeight(option="Instagram", deduction=4, rationale="r", signal_ids=listed[:1]),
                SuggestedOptionWeight(option="Bumble", rationale="none yet", research_searches=[query]),
            ]
        )

    return answer


async def _seed_listed_instagram(pool: AsyncConnectionPool) -> None:
    done = await _done_run(pool)
    await seed_signal(pool, run_id=done, tags=("instagram",), trust="listed")


async def test_an_unbacked_answer_starts_a_research_round_and_the_run_waits(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    await _seed_listed_instagram(intel_pool)
    queued = await PostgresQuestionStore(intel_pool).register_and_queue_suggestion(
        SUGGEST_REQUEST, [], operator="ann"
    )
    model = FakeQuestionModel(
        make_signal(tags=["linkedin"]),
        discovery=_found_article(),
        suggest_with=_scripted_suggestions(_instagram_backed_bumble_searches("dating app photo misuse")),
    )
    deps = make_deps(intel_pool, FakeFetcher({ARTICLE: make_page(POLICY, ARTICLE)}), model, limits=RESEARCH)
    result = await run_once(intel_pool, deps)
    assert result.status == "waiting", result
    ((round_no, research_ids, kept, awaiting),) = await _rows(
        intel_pool,
        "SELECT research_round, research_source_ids, research_pass, awaiting_source_ids"
        " FROM intel_runs WHERE run_id = %s",
        queued.run_id,
    )
    assert round_no == 1 and research_ids == awaiting and len(research_ids) == 1
    assert kept["searches"] == {"Bumble": [{"round": 1, "query": "dating app photo misuse"}]}
    ((origin, enabled, reason, proposed_for, query),) = await _rows(
        intel_pool,
        "SELECT origin, enabled, disabled_reason, proposed_for, query_text FROM intel_sources"
        " WHERE source_id = %s",
        research_ids[0],
    )
    assert (origin, enabled, reason) == ("research", False, "research_once")
    assert proposed_for == {"question_key": "platforms", "option": "Bumble"}
    assert query == "dating app photo misuse"
    assert await _open_runs(intel_pool, research_ids[0]) == [("discovery", "queued", "schedule")]
    assert model.suggest_calls == 1 and not await _rows(
        intel_pool, "SELECT 1 FROM intel_proposals WHERE run_id = %s", queued.run_id
    )


async def test_research_ends_at_the_configured_rounds_and_writes_once_with_exhausted(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    await _seed_listed_instagram(intel_pool)
    queued = await PostgresQuestionStore(intel_pool).register_and_queue_suggestion(
        SUGGEST_REQUEST, [], operator="ann"
    )
    model = FakeQuestionModel(
        make_signal(tags=["linkedin"]),
        discovery=_found_article(),
        suggest_with=_scripted_suggestions(
            _instagram_backed_bumble_searches("round one query"),
            _instagram_backed_bumble_searches("round two query"),
            _instagram_backed_bumble_searches("round three query"),
        ),
    )
    deps = make_deps(intel_pool, FakeFetcher({ARTICLE: make_page(POLICY, ARTICLE)}), model, limits=RESEARCH)
    statuses = []
    for _ in range(8):  # suggestion, search, suggestion, search, final suggestion
        result = await run_once(intel_pool, deps)
        if result is None:
            break
        statuses.append(result.status)
        if result.status == "completed" and await _rows(
            intel_pool, "SELECT 1 FROM intel_proposals WHERE run_id = %s", queued.run_id
        ):
            break
    assert model.suggest_calls == 3  # two research rounds, then the write
    ((target,),) = await _rows(
        intel_pool,
        "SELECT target FROM intel_proposals WHERE kind = 'weight_suggestion' AND run_id = %s",
        queued.run_id,
    )
    instagram, bumble = target["options"]
    assert instagram["research"] == {"rounds": 0, "searches": [], "exhausted": False}
    assert bumble["research"]["rounds"] == 2 and bumble["research"]["exhausted"] is True
    assert [s["query"] for s in bumble["research"]["searches"]] == ["round one query", "round two query"]
    payloads = [json.loads(u) for u in model.suggestion_users]
    assert payloads[1]["question"]["options"][1]["searched"] == ["round one query"]


async def test_a_backed_question_runs_no_research(intel_pool: AsyncConnectionPool) -> None:
    await seed_quiz_vocabulary(intel_pool)
    await _seed_listed_instagram(intel_pool)
    request = {**SUGGEST_REQUEST, "options": ["Instagram"], "tags": {"Instagram": ["instagram"]}}
    queued = await PostgresQuestionStore(intel_pool).register_and_queue_suggestion(request, [], operator="ann")

    def backed(payload: dict[str, Any]) -> SuggestionOutput:
        listed = [e["signal_id"] for e in payload["evidence"] if e["trust"] == "listed"]
        return SuggestionOutput(
            options=[SuggestedOptionWeight(option="Instagram", deduction=4, rationale="r", signal_ids=listed)]
        )

    model = FakeQuestionModel(suggest_with=backed)
    result = await run_once(intel_pool, make_deps(intel_pool, FakeFetcher({}), model, limits=RESEARCH))
    assert result.status == "completed" and model.suggest_calls == 1
    ((target,),) = await _rows(intel_pool, "SELECT target FROM intel_proposals WHERE run_id = %s", queued.run_id)
    assert target["options"][0]["research"] == {"rounds": 0, "searches": [], "exhausted": False}


async def test_a_model_that_gives_no_searches_ends_research_and_writes(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    await _seed_listed_instagram(intel_pool)
    queued = await PostgresQuestionStore(intel_pool).register_and_queue_suggestion(SUGGEST_REQUEST, [], operator="ann")
    model = FakeQuestionModel(suggest_with=_cite_everything())  # Bumble cites none, asks for nothing
    result = await run_once(intel_pool, make_deps(intel_pool, FakeFetcher({}), model, limits=RESEARCH))
    assert result.status == "completed" and model.suggest_calls == 1
    ((target,),) = await _rows(intel_pool, "SELECT target FROM intel_proposals WHERE run_id = %s", queued.run_id)
    assert target["options"][1]["research"] == {"rounds": 0, "searches": [], "exhausted": True}


async def test_a_refused_second_round_writes_the_first_rounds_pass(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    await _seed_listed_instagram(intel_pool)
    queued = await PostgresQuestionStore(intel_pool).register_and_queue_suggestion(SUGGEST_REQUEST, [], operator="ann")
    model = FakeQuestionModel(
        make_signal(tags=["linkedin"]),
        discovery=_found_article(),
        suggest_with=_scripted_suggestions(_instagram_backed_bumble_searches("round one query")),
    )
    deps = make_deps(intel_pool, FakeFetcher({ARTICLE: make_page(POLICY, ARTICLE)}), model, limits=RESEARCH)
    assert (await run_once(intel_pool, deps)).status == "waiting"
    assert (await run_once(intel_pool, deps)).status == "completed"  # the research search's read
    model.suggest_unavailable = ModelUnavailable("down")
    resumed = await run_once(intel_pool, deps)
    assert resumed.status == "completed", resumed
    ((target,),) = await _rows(intel_pool, "SELECT target FROM intel_proposals WHERE run_id = %s", queued.run_id)
    instagram, bumble = target["options"]
    assert instagram["deduction"] == 4  # round 1's numbers, not nulls
    assert bumble["research"]["exhausted"] is True and bumble["research"]["rounds"] == 1


async def test_research_searches_that_reuse_read_sources_resume_at_once(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    await _seed_listed_instagram(intel_pool)
    store = PostgresIntelStore(intel_pool)
    existing = await store.create_source(
        kind="search_query", source_url=None, query_text="already read query", tags=(),
        check_every_hours=168, terms_note=None, operator="alice",
    )
    async with intel_pool.connection() as conn:
        await conn.execute(
            "UPDATE intel_sources SET last_checked_at = now(), last_run_status = 'checked'"
            " WHERE source_id = %s",
            (existing.source_id,),
        )
    queued = await PostgresQuestionStore(intel_pool).register_and_queue_suggestion(SUGGEST_REQUEST, [], operator="ann")
    model = FakeQuestionModel(
        suggest_with=_scripted_suggestions(_instagram_backed_bumble_searches("Already  read query"))
    )
    deps = make_deps(intel_pool, FakeFetcher({}), model, limits=RESEARCH)
    first = await run_once(intel_pool, deps)
    assert first.status == "waiting"
    ((research_ids,),) = await _rows(intel_pool, "SELECT research_source_ids FROM intel_runs WHERE run_id = %s", queued.run_id)
    assert research_ids == [existing.source_id]  # reused by its query, not registered again
    # A discovery read was queued for it (queue_source_reads queues any named source); once it
    # ends, the suggestion resumes.


async def test_a_rerun_of_a_research_round_registers_nothing_twice(
    intel_pool: AsyncConnectionPool,
) -> None:
    await seed_quiz_vocabulary(intel_pool)
    store = PostgresQuestionStore(intel_pool)
    queued = await store.register_and_queue_suggestion(SUGGEST_REQUEST, [], operator="ann")
    request = [ResearchSourceRequest(option="Bumble", query="dating app photo misuse", tags=())]
    first = await store.register_research_sources(queued.run_id, question_key="platforms", searches=request)
    again = await store.register_research_sources(queued.run_id, question_key="platforms", searches=request)
    assert first == again and len(first) == 1


async def test_an_awaited_research_read_left_unread_is_queued_again(
    intel_pool: AsyncConnectionPool,
) -> None:
    """A research source is disabled ('research_once'), and _unread still treats it as one the
    suggestion reads, as it does an 'unmapped' draft source."""
    source = Source.model_validate(
        {
            "source_id": uuid4(), "kind": "search_query", "source_url": None, "url_hash": None,
            "query_text": "q", "tags": [], "check_every_hours": 168, "next_check_at": NOW,
            "enabled": False, "terms_note": None, "last_content_sha256": None,
            "last_checked_at": None, "last_run_status": None, "consecutive_failures": 0,
            "disabled_reason": "research_once", "created_by": "system:research", "created_at": NOW,
            "origin": "research", "proposed_for": {"question_key": "platforms", "option": "Bumble"},
            "validation_run_id": None, "validated_at": None,
        }
    )
    assert _unread(source)
```

(Imports to add at the top: `from dataclasses import replace`, `from uuid import uuid4`, `from imageshield.intel.model import ModelUnavailable`, `from imageshield.intel.models import Source`, `from imageshield.intel.question_runs import _unread`, `from imageshield.intel.question_store import ResearchSourceRequest`, `from tests.intel_fakes import TEST_LIMITS`. Adjust the `Source.model_validate` dict to the model's declared fields if they differ — read `models.Source`. `create_source`'s signature: copy the call in `_registered` above and pass `query_text`.)

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_intel_question_runs.py -k "research or backed_question or no_searches" -v`
Expected: FAIL — `register_research_sources` / `ResearchSourceRequest` missing, the run writes immediately.

- [ ] **Step 3: Implement `register_research_sources`**

In `question_store.py`:

```python
@dataclass(frozen=True)
class ResearchSourceRequest:
    """One research search a weight suggestion asks for (spec 2026-10-10 §2.2.4)."""

    option: str
    query: str
    tags: tuple[str, ...]


_REGISTER_RESEARCH_SQL = """
    INSERT INTO intel_sources (kind, query_text, tags, check_every_hours, next_check_at, enabled,
        disabled_reason, created_by, origin, proposed_for)
    VALUES ('search_query', %(query)s, %(tags)s, %(every)s, now(), false, 'research_once',
            'system:research', 'research', %(proposed_for)s)
    RETURNING source_id
"""
```

Protocol method and implementation:

```python
    async def register_research_sources(
        self, run_id: UUID, *, question_key: str, searches: Sequence[ResearchSourceRequest]
    ) -> list[UUID]:
        """spec 2026-10-10 §2.2.4, in ONE transaction under the registration lock: each search
        reuses the saved search with the same normalised query (any origin), or is registered as
        a read-once research source -- disabled ('research_once'), so no schedule ever picks it
        up, while queue_source_reads reads it now. Returns the ids in order, each once."""
        ids: list[UUID] = []
        created = 0
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('intel_source_registration', 0))"
            )
            cur = await conn.execute(
                "SELECT source_id, query_text FROM intel_sources WHERE kind = 'search_query'"
            )
            queries: dict[str, UUID] = {query_key(q): sid for sid, q in await cur.fetchall()}
            for search in searches:
                query = normalise(search.query)
                found = queries.get(query_key(query))
                if found is None:
                    cur = await conn.execute(
                        _REGISTER_RESEARCH_SQL,
                        {
                            "query": query,
                            "tags": list(search.tags),
                            "every": DEFAULT_SOURCE_CHECK_EVERY_HOURS,
                            "proposed_for": Jsonb(
                                {"question_key": question_key, "option": search.option}
                            ),
                        },
                    )
                    row = await cur.fetchone()
                    assert row is not None
                    found = row[0]
                    queries[query_key(query)] = found
                    created += 1
                if found not in ids:
                    ids.append(found)
            if created:
                await conn.execute(
                    _AUDIT_SQL,
                    {
                        "actor_type": "system",
                        "action": "intel.research_sources_registered",
                        "resource_id": run_id,
                        "metadata": Jsonb({"question_key": question_key, "registered": created}),
                    },
                )
        return ids
```

(Import `DEFAULT_SOURCE_CHECK_EVERY_HOURS` from bounds and `normalise` from intel.text if not imported. Check `audit_log.actor_type` accepts `'system'` — read the CHECK in the migration that created `audit_log`; if it does not, use the value news watch uses for its `intel.news_watch_created` row.)

- [ ] **Step 4: Implement the rounds in `question_runs.py`**

Add:

```python
@dataclass(frozen=True)
class _SuggestStep:
    status: RunStatus
    error: str | None = None
    wait: RunWait | None = None


@dataclass(frozen=True)
class _ResearchState:
    """The run's research so far (migration 0053): round 0 before any."""

    round: int
    source_ids: tuple[UUID, ...]
    kept: dict[str, Any] | None
    searched: dict[str, list[dict[str, Any]]]

    @classmethod
    def of(cls, run: Run) -> _ResearchState:
        kept = run.research_pass
        searched = kept.get("searches", {}) if isinstance(kept, dict) else {}
        return cls(run.research_round, tuple(run.research_source_ids or ()), kept, searched)
```

Change `_weight_suggestion`'s tail:

```python
    step = await _suggest(ctx, request)
    if step.wait is not None:
        return _waiting(ctx, step.wait)
    status, error = step.status, step.error
```

Rewrite `_suggest` (keep its existing opening up to `tags_by_option`; the changes are the retrieval source list, the call's keywords, the failure fallbacks and the research branch):

```python
async def _suggest(ctx: _Ctx, request: SuggestionRunRequest) -> _SuggestStep:
    """Retrieval, ONE suggestion call outside the reading cap, validation in code, then either a
    research round (spec 2026-10-10 §2.2) or the write. A later round that cannot finish writes
    the pass that started it."""
    questions = ctx.deps.questions
    limits = ctx.deps.limits
    if await questions.suggestion_written(ctx.run.run_id):
        ctx.counts["suggestion_already_written"] += 1
        return _SuggestStep("completed")
    vocabulary = parse_vocabulary(ctx.vocabulary) if ctx.vocabulary is not None else None
    if vocabulary is None:
        return _SuggestStep("failed", "vocabulary_missing")
    now = _now(ctx)
    research = _ResearchState.of(ctx.run)
    tags_by_option = {...}  # unchanged
    option_tag_set = {...}  # unchanged
    candidates = await questions.suggestion_candidates(
        source_ids=[*request.source_ids, *research.source_ids],
        tags=sorted(option_tag_set | slugs_named_by(request.options, vocabulary)),
        since=now - timedelta(days=SUGGESTION_CONTEXT_DAYS),
        limit=SUGGESTION_POOL_MAX,
    )
    context = select_context(candidates, options=request.options, limit=SUGGESTION_CONTEXT_MAX_SIGNALS)
    ctx.counts["suggestion_evidence"] = len(context)
    if not context:
        if research.kept is not None:
            return await _write_kept(ctx, request, vocabulary, research)
        ctx.counts["suggestion_no_evidence"] += 1
        return _SuggestStep("failed", "no_evidence_found")
    relevant = option_tag_set | {t for s in context for t in s.tags} | set(vocabulary.mapped_tags)
    system, user = suggestion_request(
        prompt_suggestion_question(request, tags_by_option, vocabulary, searched=research.searched),
        [prompt_signal(s) for s in context],
        registry_tags=prompt_registry(vocabulary, relevant),
        research_round=research.round + 1,
        max_rounds=limits.suggestion_research_rounds,
        searches_per_answer=limits.research_searches_per_answer,
    )
    try:
        call = await _call_model(ctx, lambda: ctx.deps.model.suggest_weights(system, user), capped=False)
    except _Stop as stop:
        if research.kept is not None:
            ctx.counts[f"research_ended_{stop.reason}"] += 1
            return await _write_kept(ctx, request, vocabulary, research)
        return _SuggestStep("refused" if stop.gate else "failed", stop.reason)
    if call.output is None:
        ctx.counts[f"suggestion_model_{call.outcome}"] += 1
        if research.kept is not None:
            return await _write_kept(ctx, request, vocabulary, research)
        return _SuggestStep("failed", f"suggestion_{call.outcome}")
    options = validate_suggestion(
        call.output,
        options=request.options,
        cap=request.cap,
        context={s.signal_id: s for s in context},
        vocabulary=vocabulary,
        counts=ctx.counts,
    )
    by_id = {s.signal_id: s for s in context}
    backed = {o.option: is_backed(o, by_id) for o in options}
    unbacked = [o for o in options if not backed[o.option]]
    if unbacked and research.round < limits.suggestion_research_rounds:
        planned = plan_research(
            unbacked, searched=research.searched,
            per_answer=limits.research_searches_per_answer, counts=ctx.counts,
        )
        if planned:
            ids = await questions.register_research_sources(
                ctx.run.run_id,
                question_key=request.question_key,
                searches=[
                    ResearchSourceRequest(option, query, tuple(tags_by_option.get(option, ())))
                    for option, queries in planned.items()
                    for query in queries
                ],
            )
            await questions.queue_source_reads(ids, now=now)
            next_round = research.round + 1
            searched = {k: list(v) for k, v in research.searched.items()}
            for option, queries in planned.items():
                searched.setdefault(option, []).extend({"round": next_round, "query": q} for q in queries)
            ctx.counts["research_rounds"] += 1
            ctx.counts["research_searches"] += sum(len(q) for q in planned.values())
            kept = {
                "model_id": call.answered_by,
                "options": [o.as_json() for o in options],
                "backed": backed,
                "searches": searched,
            }
            return _SuggestStep(
                "waiting",
                wait=RunWait(
                    awaiting=tuple(ids),
                    deadline=now + timedelta(seconds=SUGGESTION_WAIT_MAX_SECONDS),
                    research=ResearchWait(
                        round=next_round,
                        source_ids=tuple(dict.fromkeys([*research.source_ids, *ids])),
                        kept=kept,
                        answers=len(planned),
                    ),
                ),
            )
    final = attach_research(
        options, backed=backed, searched=research.searched,
        researching=limits.suggestion_research_rounds > 0,
    )
    return await _write(ctx, request, vocabulary, final, model_id=call.answered_by)


async def _write_kept(
    ctx: _Ctx, request: SuggestionRunRequest, vocabulary: ScoringVocabulary, research: _ResearchState
) -> _SuggestStep:
    """The pass that started the round now ending (spec 2026-10-10 §2.2.8): its numbers, with
    every answer it left unbacked exhausted."""
    assert research.kept is not None
    options = [OptionSuggestion.from_json(o) for o in research.kept.get("options", [])]
    backed = {str(k): bool(v) for k, v in research.kept.get("backed", {}).items()}
    final = attach_research(
        options, backed=backed, searched=research.searched, researching=True
    )
    return await _write(ctx, request, vocabulary, final, model_id=str(research.kept.get("model_id", "")))


async def _write(
    ctx: _Ctx,
    request: SuggestionRunRequest,
    vocabulary: ScoringVocabulary,
    options: Sequence[OptionSuggestion],
    *,
    model_id: str,
) -> _SuggestStep:
    ctx.counts["research_exhausted_answers"] += sum(
        1 for o in options if o.research is not None and o.research.exhausted
    )
    written = await ctx.deps.questions.write_suggestion(
        ctx.run.run_id,
        question_key=request.question_key,
        options=options,
        against_scoring_version=vocabulary.scoring_version,
        against_release_no=vocabulary.release_no,
        model_id=model_id,
        prompt_version=SUGGEST_PROMPT_VERSION,
    )
    if written is None:
        ctx.counts["suggestion_already_written"] += 1
    else:
        ctx.counts["suggestions_superseded"] += len(written.superseded)
    return _SuggestStep("completed")
```

Guard the `research_exhausted_answers` counter with `if n:` as for other counters. Imports: `ResearchWait` from pipeline; `OptionSuggestion`, `attach_research`, `is_backed`, `plan_research` from suggestion; `ResearchSourceRequest` from question_store; `ScoringVocabulary` from vocabulary; `Run` from models.

`_unread`: change the last line to `return no_evidence and (source.enabled or source.disabled_reason in ("unmapped", "research_once"))` and add one sentence to its docstring naming research sources.

- [ ] **Step 5: Run the tests**

Run: `.venv/Scripts/python -m pytest tests/test_intel_question_runs.py tests/test_intel_research_state.py tests/test_admin_intel_question_routes.py -v`
Expected: PASS (new research tests, all existing suggestion tests — TEST_LIMITS keeps research off for them).

- [ ] **Step 6: Commit**

```bash
.venv/Scripts/ruff check src/imageshield/intel tests/test_intel_question_runs.py
.venv/Scripts/mypy src/imageshield/intel
git add src/imageshield/intel tests/test_intel_question_runs.py
git commit -m "feat(intel): a suggestion searches again for answers it could not back

Spec 2026-10-10 §2.2: up to INTEL_SUGGESTION_RESEARCH_ROUNDS rounds; each unbacked
answer's searches are registered as read-once research sources, read by their own
discovery runs, and the run waits; a later round that cannot finish writes the
pass that started it. Written once, research recorded per answer.

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

### Task 6: Docs and the spec correction

**Files:**
- Modify: `docs/superpowers/specs/2026-10-10-intel-every-answer-researched-design.md` (§2.2.4: disabled `research_once`, not `next_check_at='infinity'`; §3 adds the disabled_reason CHECK)
- Modify: `SCHEMA.md` (intel_sources origin/disabled_reason, intel_runs research columns, run event kind), `PROXY_INTEGRATION.md` (weight-suggestion poll `research_round`, per-option `research`), `CLAUDE.md` §6 table row for the suggestion pipeline (one dated amendment line), `docs/OPERATIONS.md` (the thirteen keys and what each does, if the intel keys are listed there)

- [ ] **Step 1: Amend the spec**

In §2.2.4 replace "with `next_check_at = 'infinity'`: it is read now, by the read queued for it, and never scheduled again" with "registered disabled (`enabled = false`, `disabled_reason = 'research_once'`): `queue_source_reads` reads it now, because it reads named sources whatever `enabled` says, and no schedule picks it up. (`next_check_at` cannot carry this: `queue_source_reads` advances it.)" In §3 add the `disabled_reason` CHECK gaining `'research_once'`.

- [ ] **Step 2: Update the maintained docs** (each a dated amendment in that file's own style; read the surrounding section first and edit in place — never overwrite)

- [ ] **Step 3: Final checks and commit**

```bash
.venv/Scripts/ruff check src tests
.venv/Scripts/mypy src
.venv/Scripts/python -m pytest tests/test_intel_limits_config.py tests/test_intel_config.py tests/test_intel_model.py tests/test_ecs_task_defs.py tests/test_intel_research_state.py tests/test_intel_question_runs.py tests/test_intel_pipeline.py tests/test_admin_intel_question_routes.py tests/test_boundaries.py -q
git add docs SCHEMA.md PROXY_INTEGRATION.md CLAUDE.md
git commit -m "docs(intel): every answer is researched — schema, contract and the spec's read-once mechanism

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

Report which test modules were not run.

---

## Deploy (after the backend plan's code is reviewed; owner approval per deploy)

Dev: build `services:<sha>` (arm64), retag the dev task defs (they carry the thirteen keys from Task 1), run migrate-services (0053), roll services, services-worker, fetcher, confirm. Prod: `docs/deploy/ROUTINE-DEPLOY.md` with `infra/ecs/prod/build-push.sh` and `render.sh`; prod's `claude_intel` stays disabled, so nothing spends there.
