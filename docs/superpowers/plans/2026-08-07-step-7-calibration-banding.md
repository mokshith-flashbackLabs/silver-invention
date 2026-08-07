# Step 7 — Calibration Harness and Banding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `infringements.band` capable of being something other than the hardcoded `'review'`, behind two independent gates that make an unsound calibration impossible to activate.

**Architecture:** A pure banding engine (`src/imageshield/calibration/bands.py`, `metrics.py`) with no I/O, called from the existing search write path via a per-run policy snapshot; a Postgres store for calibration configs and eval data; and a `devtools/calibrate` CLI over the same engine. Eval data lives in its own tables and is populated by the production adapter, never a reimplementation.

**Tech Stack:** Python 3.11+, pydantic 2, psycopg 3 (raw SQL, no ORM), pytest, argparse.

**Spec:** [`docs/superpowers/specs/2026-08-07-step-7-calibration-banding-design.md`](../specs/2026-08-07-step-7-calibration-banding-design.md) — read it before starting. This plan implements it and adds nothing.

## Global Constraints

- Python ≥3.11. `from __future__ import annotations` at the top of every module.
- `mypy --strict` must pass. No `Any` escapes without a comment naming the reason.
- Raw SQL only. No ORM. All SQL as module-level `_UPPER_SNAKE_SQL` string constants, matching [`search/store.py`](../../../src/imageshield/search/store.py).
- Every inbound model is pydantic with `ConfigDict(frozen=True)` for rows, `extra="forbid"` for payloads.
- Typed identifiers: `ProviderId`, `UserRef`, `UrlHash` from `imageshield.types`. No bare `str` for an identifier.
- **No arithmetic across providers.** No `mean`, `average`, or `avg` in `src/imageshield/search/` or `src/imageshield/calibration/`. Task 8 makes this a permanent test.
- **Nothing raises out of the banding engine.** Every failure mode returns `review`.
- Band vocabulary is exactly `drop | review | auto_confirm`. Ordering `drop < review < auto_confirm`.
- Scores are never rescaled, normalised, or compared across providers. Bands are authored in the provider's native units.
- Migration files are paired `.up.sql` / `.down.sql`, applied via `python scripts/migrate.py up`. Editing an applied migration is a deploy-blocking error.
- No `bytea` column, ever (invariant #9). `*_uri` and `*_url` names are explicitly allowed.
- Commit trailer on every commit: `Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>`. Never the Claude trailer.
- Branch is `step-7-calibration-banding`, already created, already holding the design commit.

## Running tests

```bash
# Pure-function tests — no database needed
python -m pytest tests/test_calibration_bands.py tests/test_calibration_metrics.py -v

# DB tests — needs the local compose Postgres on :15433
docker compose -f docker-compose.local.yml up -d
python -m pytest tests/test_calibration_activate.py -v

# Everything
python -m pytest -q && python -m mypy
```

DB tests **skip** when Postgres is unreachable unless `REQUIRE_DB=1` is set. If a DB test skips, that is not a pass — start compose and re-run.

## File Structure

| File | Responsibility |
|---|---|
| `migrations/0007_calibration_and_eval.up.sql` / `.down.sql` | Four new tables, three ALTERs |
| `src/imageshield/calibration/__init__.py` | Empty marker |
| `src/imageshield/calibration/models.py` | `Band`, `Label`, `LabelKind`, `ScoreDomain`, `NumericBand`, `CalibrationConfig`, `PolicyEntry`, `BandDecision`, `EvalRow`. Types only, no logic. |
| `src/imageshield/calibration/bands.py` | PURE. `band_for_attestation()`, `roll_up()`. No DB, no I/O, no clock. |
| `src/imageshield/calibration/metrics.py` | PURE. `Metric`, `confusion_*()`, `precision/recall/npv`, `sweep_numeric()`, `sweep_categorical()`. |
| `src/imageshield/calibration/store.py` | Postgres. Policy load, config CRUD, eval CRUD, re-band, activate, trust. |
| `devtools/calibrate/__init__.py` / `__main__.py` | argparse CLI: `observe sweep propose replay activate trust` |
| `src/imageshield/search/store.py` (modify) | `_fan_out`→`fan_out`, `_InfringementKey`→`InfringementKey`; `record_infringements` takes a policy and bands |
| `src/imageshield/search/runner.py` (modify) | Load policy once per run, pass it down |
| `src/imageshield/config.py` (modify) | `calibration_min_eval_items: int = 200` |

---

## Task 1: Migration 0007 and the config knob

**Files:**
- Create: `migrations/0007_calibration_and_eval.up.sql`
- Create: `migrations/0007_calibration_and_eval.down.sql`
- Modify: `src/imageshield/config.py` (add one field, add it to `_positive`)
- Test: `tests/test_migrations.py` (append), `tests/test_config.py` (append)

**Interfaces:**
- Consumes: nothing.
- Produces: tables `calibration_configs`, `eval_items`, `eval_observations`, `eval_seed_coverage`; columns `attestations.band`, `attestations.calibration_version`, `infringements.band_reason`; `Config.calibration_min_eval_items`.

- [ ] **Step 1: Write the failing migration tests**

Append to `tests/test_migrations.py`:

```python
CALIBRATION_TABLES = {
    "calibration_configs",
    "eval_items",
    "eval_observations",
    "eval_seed_coverage",
}


def test_0007_creates_calibration_tables(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with psycopg.connect(throwaway_db) as conn:
        assert CALIBRATION_TABLES <= _table_names(conn)


def test_0007_down_removes_them_and_the_added_columns(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    run_migrate(throwaway_db, "down", "--steps", "1")
    with psycopg.connect(throwaway_db) as conn:
        assert CALIBRATION_TABLES & _table_names(conn) == set()
        cols = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'attestations'"
            ).fetchall()
        }
        assert "band" not in cols
        assert "calibration_version" not in cols


def test_0007_eval_item_without_consent_basis_is_rejected(throwaway_db: str) -> None:
    """Invariant: no eval item without a traceable consent basis. NOT NULL
    alone lets '' through, so the btrim CHECK is what actually enforces it."""
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with psycopg.connect(throwaway_db) as conn:
        for bad in ("", "   ", "\t\n"):
            with pytest.raises(psycopg.errors.CheckViolation):
                with conn.transaction():
                    conn.execute(
                        "INSERT INTO eval_items (eval_set_id, seed_uri, candidate_url,"
                        " label, label_kind, consent_basis, labelled_by)"
                        " VALUES ('v1', 's3://seed', 'https://x.test/a',"
                        " 'true_match', 'same_person', %s, 'tester')",
                        (bad,),
                    )


@pytest.mark.parametrize(
    ("label_kind", "label", "allowed"),
    [
        ("same_person", "true_match", True),
        ("same_person", "false_match", False),
        ("derived_edit", "true_match", True),
        ("derived_edit", "false_match", False),   # the inversion that must be impossible
        ("derived_edit", "uncertain", True),
        ("novel_generation", "true_match", True),
        ("novel_generation", "false_match", False),
        ("lookalike", "false_match", True),
        ("lookalike", "true_match", False),
        ("lookalike", "uncertain", True),
        ("unrelated", "false_match", True),
        ("unrelated", "true_match", False),
    ],
)
def test_0007_label_kind_and_label_must_agree(
    throwaway_db: str, label_kind: str, label: str, allowed: bool
) -> None:
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with psycopg.connect(throwaway_db) as conn:
        stmt = (
            "INSERT INTO eval_items (eval_set_id, seed_uri, candidate_url,"
            " label, label_kind, consent_basis, labelled_by)"
            " VALUES ('v1', 's3://seed', %s, %s, %s, 'team member, written consent', 'tester')"
        )
        url = f"https://x.test/{label_kind}-{label}"
        if allowed:
            with conn.transaction():
                conn.execute(stmt, (url, label, label_kind))
        else:
            with pytest.raises(psycopg.errors.CheckViolation):
                with conn.transaction():
                    conn.execute(stmt, (url, label, label_kind))


def test_0007_only_one_active_config_per_provider(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    run_migrate(throwaway_db, "up")
    with psycopg.connect(throwaway_db) as conn:
        stmt = (
            "INSERT INTO calibration_configs (provider_id, version, score_kind, bands, active)"
            " VALUES ('hive', %s, 'numeric', '[]'::jsonb, true)"
        )
        with conn.transaction():
            conn.execute(stmt, ("v1",))
        with pytest.raises(psycopg.errors.UniqueViolation):
            with conn.transaction():
                conn.execute(stmt, ("v2",))
        # Inactive rows are unconstrained — many may coexist.
        with conn.transaction():
            conn.execute(
                "INSERT INTO calibration_configs (provider_id, version, score_kind, bands)"
                " VALUES ('hive', 'v3', 'numeric', '[]'::jsonb),"
                "        ('hive', 'v4', 'numeric', '[]'::jsonb)"
            )
```

- [ ] **Step 2: Run them to verify they fail**

```bash
python -m pytest tests/test_migrations.py -k 0007 -v
```

Expected: FAIL — `relation "eval_items" does not exist` / the table-set assertion.

- [ ] **Step 3: Write the up migration**

Create `migrations/0007_calibration_and_eval.up.sql`:

```sql
-- Step 7: banding stops being a hardcoded literal.
--
-- Until this migration every infringements.band is 'review', written
-- unconditionally by search/store.py. Two things have to exist before a band
-- can be anything else: a config saying what the provider's raw values MEAN,
-- and a labelled set proving that meaning holds. This migration is both.
--
-- No GRANTs here, matching 0004-0006: per-module DB roles are step 9.

CREATE TABLE calibration_configs (
  config_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  provider_id      TEXT NOT NULL REFERENCES providers(provider_id),
  version          TEXT NOT NULL,
  score_kind       TEXT NOT NULL CHECK (score_kind IN ('numeric','categorical')),
  -- Expressed in the provider's NATIVE units and validated against
  -- providers.score_domain at propose time. Hive's domain is 0.5-1.0 where
  -- 0.5 is the FLOOR, so a boundary of 0.72 means 0.72 on Hive's scale --
  -- there is no rescaling anywhere in this system.
  --   numeric:     [{"band":"drop","max":0.72},
  --                 {"band":"review","min":0.72,"max":0.94},
  --                 {"band":"auto_confirm","min":0.94}]
  --   categorical: {"full_match":"auto_confirm","partial_match":"review",
  --                 "page_match":"review"}
  bands            JSONB NOT NULL,
  eval_set_id      TEXT,
  eval_sample_size INT,
  -- ADVISORY ONLY: a record of what the proposer saw. `activate` never reads
  -- it. A machine check that trusts a JSONB column an operator can type into
  -- is defeated by editing a number, so the floor is recomputed from
  -- eval_observations every time.
  measured         JSONB,
  active           BOOLEAN NOT NULL DEFAULT false,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  activated_at     TIMESTAMPTZ,
  activated_by     TEXT,
  UNIQUE (provider_id, version)
);

CREATE UNIQUE INDEX calibration_one_active
  ON calibration_configs (provider_id) WHERE active;

-- The labelled set. `label` answers "is this the user's likeness, and should
-- they be told about it?" -- NOT "is this an authentic photograph of them".
-- That distinction is the whole reason derived_edit is a positive.
CREATE TABLE eval_items (
  item_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  eval_set_id    TEXT NOT NULL,
  seed_uri       TEXT NOT NULL,
  candidate_url  TEXT NOT NULL,
  label          TEXT NOT NULL
                 CHECK (label IN ('true_match','false_match','uncertain')),
  label_kind     TEXT NOT NULL
                 CHECK (label_kind IN ('same_person','derived_edit',
                                       'novel_generation','lookalike','unrelated')),
  -- NOT NULL alone permits ''. The btrim check is what actually rejects an
  -- item with no traceable consent basis. Consenting participants,
  -- public-domain, or synthetic only -- never real victim content, never
  -- scraped material.
  consent_basis  TEXT NOT NULL CHECK (btrim(consent_basis) <> ''),
  labelled_by    TEXT NOT NULL,
  labelled_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- A derived_edit labelled false_match would tune thresholds to suppress
  -- precisely what this product exists to catch, AND would make the precision
  -- figure look better for doing so. Make the inversion unrepresentable.
  -- 'uncertain' stays available for every kind: a labeller who genuinely
  -- cannot tell must have somewhere to put that.
  CONSTRAINT eval_label_kind_agrees CHECK (
    (label_kind IN ('same_person','derived_edit','novel_generation')
       AND label IN ('true_match','uncertain'))
    OR
    (label_kind IN ('lookalike','unrelated')
       AND label IN ('false_match','uncertain'))
  ),
  UNIQUE (eval_set_id, seed_uri, candidate_url)
);

CREATE INDEX eval_items_set_idx ON eval_items (eval_set_id);

-- What a provider actually said about a labelled item. Mirrors attestations:
-- one item, many providers, re-observation UPDATES rather than appends.
-- Kept out of infringements/attestations so nothing serving real users
-- carries test rows and `activate`'s re-band never touches eval data.
CREATE TABLE eval_observations (
  observation_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  item_id           UUID NOT NULL REFERENCES eval_items(item_id) ON DELETE CASCADE,
  provider_id       TEXT NOT NULL REFERENCES providers(provider_id),
  score_kind        TEXT NOT NULL,
  provider_score    NUMERIC(6,4),
  provider_category TEXT,
  query_quality     TEXT,
  score_version     TEXT NOT NULL,
  observed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (item_id, provider_id),
  CONSTRAINT eval_observation_score_shape CHECK (
    (score_kind = 'numeric'     AND provider_score    IS NOT NULL) OR
    (score_kind = 'categorical' AND provider_category IS NOT NULL)
  )
);

CREATE INDEX eval_observations_provider_idx ON eval_observations (provider_id);

-- Recall depends on counting the true_matches a provider FAILED to return.
-- An absent eval_observation is ambiguous on its own: either "asked and did
-- not return it" (a miss, and data) or "never asked" (not data). Only this
-- table separates them, and it is what makes the activate floor's coverage
-- condition checkable at all -- read against eval_observations that condition
-- would reject every honest set, because an honest set contains misses.
CREATE TABLE eval_seed_coverage (
  coverage_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  eval_set_id         TEXT NOT NULL,
  seed_uri            TEXT NOT NULL,
  provider_id         TEXT NOT NULL REFERENCES providers(provider_id),
  status              TEXT NOT NULL,   -- ok | error | timeout | rate_limited
  candidates_returned INT NOT NULL,
  observed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (eval_set_id, seed_uri, provider_id)
);

-- Which config produced a band. Without it a retune makes every historical
-- band uninterpretable.
ALTER TABLE attestations
  ADD COLUMN band TEXT NOT NULL DEFAULT 'review'
    CHECK (band IN ('drop','review','auto_confirm')),
  ADD COLUMN calibration_version TEXT;

-- infringements.band has carried no CHECK since 0005. Adding one while
-- 'review' is still the only value in existence is free.
ALTER TABLE infringements
  ADD COLUMN band_reason TEXT,
  ADD CONSTRAINT infringements_band_valid
    CHECK (band IN ('drop','review','auto_confirm'));
```

- [ ] **Step 4: Write the down migration**

Create `migrations/0007_calibration_and_eval.down.sql`:

```sql
-- Reverses 0007. Eval data and calibration configs are dropped outright:
-- they are derived artifacts, reproducible by re-running `calibrate observe`
-- against the labelled set, which lives in the eval_items rows themselves.
--
-- Bands computed under a config are lost with the columns. That is correct:
-- without calibration_version a stored band is uninterpretable anyway, and
-- everything reverts to the pre-step-7 state where 'review' is the only
-- value the write path produces.

ALTER TABLE infringements
  DROP CONSTRAINT infringements_band_valid,
  DROP COLUMN band_reason;

ALTER TABLE attestations
  DROP COLUMN calibration_version,
  DROP COLUMN band;

DROP TABLE eval_seed_coverage;
DROP TABLE eval_observations;
DROP TABLE eval_items;
DROP TABLE calibration_configs;
```

- [ ] **Step 5: Run the migration tests**

```bash
docker compose -f docker-compose.local.yml up -d
python -m pytest tests/test_migrations.py -k 0007 -v
```

Expected: all PASS. If any SKIP, Postgres is not up — fix that and re-run.

- [ ] **Step 6: Add the config knob**

In `src/imageshield/config.py`, after `raw_response_retention_days`:

```python
    # Floor on eval set size before a calibration config may activate a
    # non-review band. 200 is a policy choice, not a statistical derivation:
    # it is the point below which a precision figure is too weak to justify
    # alarming a person without a human looking. Config rather than a
    # constant so tightening it is an ops change, but note that LOOSENING it
    # still cannot bypass the zero-lookalike refusal, which is unconditional.
    calibration_min_eval_items: int = 200
```

Add `"calibration_min_eval_items"` to the existing `_positive` validator's `@field_validator` list.

- [ ] **Step 7: Test the config knob**

Append to `tests/test_config.py`:

```python
def test_calibration_min_eval_items_defaults_to_200() -> None:
    assert make_config().calibration_min_eval_items == 200


def test_calibration_min_eval_items_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        make_config(calibration_min_eval_items=0)
```

Match the import style already used in `tests/test_config.py` for `ValidationError` and `make_config`.

- [ ] **Step 8: Run the full suite and mypy**

```bash
python -m pytest -q && python -m mypy
```

Expected: all pass. The schema lint test must still pass — `seed_uri` is a `*_uri` and allowed under invariant #9(c), and no new column is `bytea`.

- [ ] **Step 9: Commit**

```bash
git add migrations/0007_calibration_and_eval.up.sql \
        migrations/0007_calibration_and_eval.down.sql \
        src/imageshield/config.py tests/test_migrations.py tests/test_config.py
git commit -F - <<'EOF'
Step 7: migration 0007 - calibration configs and the eval set

Four tables and three columns. Nothing reads them yet.

Two constraints are doing real work rather than documenting intent:

  consent_basis CHECK (btrim(...) <> '') -- NOT NULL alone lets '' through,
  and the rule is that an eval item with no traceable consent basis does not
  exist.

  eval_label_kind_agrees -- a derived_edit row labelled false_match is
  rejected. A nudify edit of someone's own photo is their likeness being
  abused; labelling it a negative would tune thresholds to suppress the
  flagship case and would improve the precision figure for doing so. The
  pairing is invalid, so make it unrepresentable.

eval_seed_coverage is not in the step-7 brief. It is needed because an absent
eval_observation means either "the provider was asked and did not return it"
(a miss, and the data recall is computed from) or "the provider was never
asked" (not data). Nothing else separates those.

measured JSONB is kept but demoted to advisory. The activate floor recomputes
from eval_observations instead, because a check that trusts a column an
operator can edit is not a check.

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>
EOF
```

---

## Task 2: The pure banding engine

**Files:**
- Create: `src/imageshield/calibration/__init__.py` (empty)
- Create: `src/imageshield/calibration/models.py`
- Create: `src/imageshield/calibration/bands.py`
- Test: `tests/test_calibration_bands.py`, `tests/test_calibration_rollup.py`

**Interfaces:**
- Consumes: `imageshield.types.ProviderId`; `imageshield.search.models.ScoreKind`.
- Produces:
  - `Band = Literal["drop","review","auto_confirm"]`, `BAND_ORDER: dict[Band,int]`
  - `Label`, `LabelKind`, `TRUE_MATCH_KINDS`, `FALSE_MATCH_KINDS`
  - `ScoreDomain(min, max, categories)`, `NumericBand(band, min, max)`
  - `CalibrationConfig(config_id, provider_id, version, score_kind, numeric_bands, categorical_bands)`
  - `PolicyEntry(provider_id, calibrated, score_domain, config)`
  - `BandingPolicy = Mapping[ProviderId, PolicyEntry]`
  - `BandDecision(band, reason, calibration_version)`
  - `band_for_attestation(entry, score_kind, provider_score, provider_category) -> BandDecision`
  - `roll_up(bands: Sequence[Band]) -> tuple[Band, str]`
  - `validate_numeric_bands(bands, domain) -> list[str]` (returns problems; empty = valid)

- [ ] **Step 1: Write `models.py`**

This task's models carry no logic, so they go in first; the failing tests in Step 2 import from them.

```python
"""Types for calibration and banding (CLAUDE.md §7.2, §7.3).

No logic lives here. The rules are in :mod:`bands` and :mod:`metrics`, both
of which are pure functions over these values — that is what lets the
safety-critical decisions be tested exhaustively without a database.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from imageshield.types import ProviderId

# Ordering matters and is used by roll_up. Nothing else may define it.
Band = Literal["drop", "review", "auto_confirm"]
BAND_ORDER: dict[Band, int] = {"drop": 0, "review": 1, "auto_confirm": 2}

Label = Literal["true_match", "false_match", "uncertain"]
LabelKind = Literal[
    "same_person", "derived_edit", "novel_generation", "lookalike", "unrelated"
]

# `label` answers "is this the user's likeness, and should they be told?" —
# not "is this an authentic photograph of them". A derived_edit (nudify-style
# alteration of the subject's own photo) and a novel_generation (diffusion
# output of their face) are both their likeness being abused. Mirrors the
# eval_label_kind_agrees CHECK in migration 0007; the DB is authoritative.
TRUE_MATCH_KINDS: frozenset[LabelKind] = frozenset(
    {"same_person", "derived_edit", "novel_generation"}
)
FALSE_MATCH_KINDS: frozenset[LabelKind] = frozenset({"lookalike", "unrelated"})

ScoreKind = Literal["numeric", "categorical"]


class ScoreDomain(BaseModel):
    """The range or vocabulary a provider's raw values actually occupy, from
    ``providers.score_domain`` (written by migration 0004).

    Hive Web Search reports 0.5–1.0 where **0.5 is the floor**, not a midpoint
    meaning "uncertain". A value below it is impossible — a malformed response
    or a key provisioned against the wrong Hive project — and must not be read
    as "a very low score", which would band it ``drop`` and discard it
    silently.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    min: Decimal | None = None
    max: Decimal | None = None
    categories: tuple[str, ...] | None = None


class NumericBand(BaseModel):
    """One half-open interval in the provider's NATIVE units.

    Convention: ``min`` inclusive, ``max`` exclusive, except the top band
    whose ``max`` is the domain maximum inclusive. Stated because 0.94 exactly
    is the difference between ``review`` and ``auto_confirm``.

    ``min=None`` means "from the domain floor"; ``max=None`` means "to the
    domain ceiling".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    band: Band
    min: Decimal | None = None
    max: Decimal | None = None


class CalibrationConfig(BaseModel):
    """An active or candidate mapping from a provider's raw values to bands.

    Exactly one of ``numeric_bands`` / ``categorical_bands`` is populated,
    determined by ``score_kind``.
    """

    model_config = ConfigDict(frozen=True)

    config_id: UUID
    provider_id: ProviderId
    version: str
    score_kind: ScoreKind
    numeric_bands: tuple[NumericBand, ...] = ()
    categorical_bands: Mapping[str, Band] = {}

    def declares(self, band: Band) -> bool:
        """Whether this config can ever produce ``band``.

        The activate floor skips the precision check for an ``auto_confirm``
        band the config never claims.
        """
        return band in {b.band for b in self.numeric_bands} or band in set(
            self.categorical_bands.values()
        )


class PolicyEntry(BaseModel):
    """Everything banding needs to know about one provider, snapshotted once
    per run so every attestation in that run is banded by the same rules."""

    model_config = ConfigDict(frozen=True)

    provider_id: ProviderId
    calibrated: bool
    score_domain: ScoreDomain
    config: CalibrationConfig | None  # None => no active config


BandingPolicy = Mapping[ProviderId, PolicyEntry]


class BandDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    band: Band
    reason: str
    calibration_version: str | None
```

- [ ] **Step 2: Write the failing per-attestation tests**

Create `tests/test_calibration_bands.py`:

```python
"""Per-attestation banding. Pure functions, no database.

Permanent tests — never delete. Each one corresponds to a way this product
can hurt someone.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from imageshield.calibration.bands import band_for_attestation, validate_numeric_bands
from imageshield.calibration.models import (
    CalibrationConfig,
    NumericBand,
    PolicyEntry,
    ScoreDomain,
)
from imageshield.types import ProviderId

HIVE = ProviderId("hive")
GOOGLE = ProviderId("google")

# Hive Web Search: 0.5 is the FLOOR of the reported range, not a midpoint.
HIVE_DOMAIN = ScoreDomain(min=Decimal("0.5"), max=Decimal("1.0"))
GOOGLE_DOMAIN = ScoreDomain(
    categories=("full_match", "partial_match", "page_match")
)

HIVE_BANDS = (
    NumericBand(band="drop", max=Decimal("0.72")),
    NumericBand(band="review", min=Decimal("0.72"), max=Decimal("0.94")),
    NumericBand(band="auto_confirm", min=Decimal("0.94")),
)


def hive_config() -> CalibrationConfig:
    return CalibrationConfig(
        config_id=uuid4(),
        provider_id=HIVE,
        version="hive-cal-v1",
        score_kind="numeric",
        numeric_bands=HIVE_BANDS,
    )


def google_config() -> CalibrationConfig:
    return CalibrationConfig(
        config_id=uuid4(),
        provider_id=GOOGLE,
        version="google-cal-v1",
        score_kind="categorical",
        categorical_bands={
            "full_match": "auto_confirm",
            "partial_match": "review",
            "page_match": "review",
        },
    )


def hive_entry(*, calibrated: bool = True, config: bool = True) -> PolicyEntry:
    return PolicyEntry(
        provider_id=HIVE,
        calibrated=calibrated,
        score_domain=HIVE_DOMAIN,
        config=hive_config() if config else None,
    )


def google_entry(*, calibrated: bool = True) -> PolicyEntry:
    return PolicyEntry(
        provider_id=GOOGLE,
        calibrated=calibrated,
        score_domain=GOOGLE_DOMAIN,
        config=google_config(),
    )


def band_hive(score: str, **kwargs: bool) -> tuple[str, str]:
    d = band_for_attestation(hive_entry(**kwargs), "numeric", Decimal(score), None)
    return d.band, d.reason


# ── Rule 1 & 2: the gates that force review ──────────────────────────────

def test_no_policy_entry_is_review() -> None:
    d = band_for_attestation(None, "numeric", Decimal("0.99"), None)
    assert d.band == "review"
    assert d.reason == "no_active_config"
    assert d.calibration_version is None


def test_no_active_config_is_review() -> None:
    assert band_hive("0.99", config=False) == ("review", "no_active_config")


@pytest.mark.parametrize("score", ["0.50", "0.71", "0.80", "0.94", "1.00"])
def test_uncalibrated_provider_produces_review_for_every_input(score: str) -> None:
    """CLAUDE.md §7.3. The config says auto_confirm at 0.94+; calibrated=false
    overrides it. This blocks `drop` too — "review band only" is literal, and
    a real infringement landing in drop is invisible to the user forever."""
    band, reason = band_hive(score, calibrated=False)
    assert band == "review"
    assert reason == "provider_uncalibrated"


def test_uncalibrated_categorical_provider_is_also_review() -> None:
    d = band_for_attestation(
        google_entry(calibrated=False), "categorical", None, "full_match"
    )
    assert d.band == "review"
    assert d.reason == "provider_uncalibrated"


# ── Rule 3: shape mismatch never crashes ─────────────────────────────────

def test_score_kind_mismatch_is_review_not_an_exception() -> None:
    d = band_for_attestation(hive_entry(), "categorical", None, "full_match")
    assert d.band == "review"
    assert d.reason == "score_kind_mismatch"


def test_numeric_kind_with_null_score_is_review() -> None:
    d = band_for_attestation(hive_entry(), "numeric", None, None)
    assert d.band == "review"
    assert d.reason == "score_kind_mismatch"


# ── Rule 4: the score_domain fixture where it changes the outcome ────────

def test_below_hive_floor_is_review_not_drop() -> None:
    """THE fixture the step-7 done-when asks for.

    0.4 is impossible for Hive — its floor is 0.5. Read against an assumed
    0–1 scale it is merely a low score and bands to `drop`, silently
    discarded and never seen by anyone. Read against score_domain it is a
    malformed response (or a key on the wrong Hive project, which returns
    plausible-looking wrong results rather than an error) and a human looks.
    """
    assert band_hive("0.40") == ("review", "score_out_of_domain")


def test_above_hive_ceiling_is_review_not_auto_confirm() -> None:
    assert band_hive("1.30") == ("review", "score_out_of_domain")


def test_in_domain_low_score_still_drops() -> None:
    """0.60 IS in Hive's domain, so domain-awareness does not suppress a
    genuine low score — it only rejects impossible ones."""
    assert band_hive("0.60") == ("drop", "numeric:drop")


# ── Rule 5: native units, exact boundaries ───────────────────────────────

@pytest.mark.parametrize(
    ("score", "expected"),
    [
        ("0.50", "drop"),          # exactly the domain floor
        ("0.7199", "drop"),
        ("0.72", "review"),        # min inclusive
        ("0.9399", "review"),
        ("0.94", "auto_confirm"),  # min inclusive — the boundary that matters
        ("1.00", "auto_confirm"),  # top band's max is inclusive
    ],
)
def test_numeric_boundaries_are_min_inclusive_max_exclusive(
    score: str, expected: str
) -> None:
    assert band_hive(score)[0] == expected


def test_scores_are_never_rescaled() -> None:
    """A 0.72 boundary means 0.72 on Hive's native scale. If anything
    rescaled the domain onto 0–1, native 0.72 would map to 0.44 and land in
    `drop` instead of `review`."""
    assert band_hive("0.72")[0] == "review"


# ── Rules 6 & 7: categorical ─────────────────────────────────────────────

@pytest.mark.parametrize(
    ("category", "expected"),
    [
        ("full_match", "auto_confirm"),
        ("partial_match", "review"),
        ("page_match", "review"),
    ],
)
def test_categorical_bands_come_from_lookup(category: str, expected: str) -> None:
    d = band_for_attestation(google_entry(), "categorical", None, category)
    assert d.band == expected
    assert d.reason == f"categorical:{expected}"


def test_categorical_never_touches_provider_score() -> None:
    """Google Web Detection reports no number. provider_score stays NULL all
    the way through — inventing one would be normalising in the adapter."""
    d = band_for_attestation(google_entry(), "categorical", None, "full_match")
    assert d.band == "auto_confirm"
    # And a stray score alongside a categorical kind is ignored, not used.
    d2 = band_for_attestation(
        google_entry(), "categorical", Decimal("0.99"), "page_match"
    )
    assert d2.band == "review"


def test_unknown_category_is_review() -> None:
    d = band_for_attestation(google_entry(), "categorical", None, "some_new_kind")
    assert d.band == "review"
    assert d.reason == "unknown_category"


def test_calibration_version_is_stamped_on_a_real_decision() -> None:
    d = band_for_attestation(hive_entry(), "numeric", Decimal("0.99"), None)
    assert d.calibration_version == "hive-cal-v1"


# ── Band JSON validation, used by `propose` ──────────────────────────────

def test_valid_bands_have_no_problems() -> None:
    assert validate_numeric_bands(HIVE_BANDS, HIVE_DOMAIN) == []


def test_boundary_outside_domain_is_rejected() -> None:
    bad = (
        NumericBand(band="drop", max=Decimal("0.20")),
        NumericBand(band="review", min=Decimal("0.20")),
    )
    problems = validate_numeric_bands(bad, HIVE_DOMAIN)
    assert any("outside score_domain" in p for p in problems)


def test_gap_in_coverage_is_rejected() -> None:
    bad = (
        NumericBand(band="drop", max=Decimal("0.70")),
        NumericBand(band="auto_confirm", min=Decimal("0.80")),
    )
    problems = validate_numeric_bands(bad, HIVE_DOMAIN)
    assert any("gap" in p for p in problems)


def test_overlap_is_rejected() -> None:
    bad = (
        NumericBand(band="drop", max=Decimal("0.80")),
        NumericBand(band="auto_confirm", min=Decimal("0.70")),
    )
    problems = validate_numeric_bands(bad, HIVE_DOMAIN)
    assert any("overlap" in p for p in problems)
```

- [ ] **Step 3: Run them to verify they fail**

```bash
python -m pytest tests/test_calibration_bands.py -v
```

Expected: collection error — `No module named 'imageshield.calibration.bands'`.

- [ ] **Step 4: Write `bands.py`**

```python
"""The banding rules. Pure functions — no DB, no I/O, no clock.

Two decisions live here and nothing else may make them:

  band_for_attestation  one provider's raw value -> a band
  roll_up               several providers' bands -> the infringement's band

**Nothing in this module raises.** Every failure mode — no config, a shape
mismatch, an impossible score, an unknown category — returns ``review``.
Banding sits in the write path of a scan, and a crash there would fail a run
over a provider's malformed row. ``review`` means a human looks, which is the
correct outcome for anything we do not understand.

**No arithmetic across providers.** No mean, no average, no max-of-scores —
only max-of-ordinal-band. Provider A's 0.92 and Provider B's 0.92 are
different quantities with different distributions (CLAUDE.md §7.2), and an
average of them is a meaningless number that looks entirely plausible.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from imageshield.calibration.models import (
    BAND_ORDER,
    Band,
    BandDecision,
    NumericBand,
    PolicyEntry,
    ScoreDomain,
    ScoreKind,
)

_REVIEW_NO_CONFIG = BandDecision(
    band="review", reason="no_active_config", calibration_version=None
)


def _review(reason: str, version: str | None) -> BandDecision:
    return BandDecision(band="review", reason=reason, calibration_version=version)


def band_for_attestation(
    entry: PolicyEntry | None,
    score_kind: ScoreKind,
    provider_score: Decimal | None,
    provider_category: str | None,
) -> BandDecision:
    """Rules in order, first match wins. See the spec's §5.1 table.

    ``entry`` is None when the provider has no row in the policy snapshot at
    all — treated identically to having no active config.
    """
    # Rule 1 — nothing has told us what this provider's numbers mean.
    if entry is None or entry.config is None:
        return _REVIEW_NO_CONFIG
    config = entry.config
    version = config.version

    # Rule 2 — CLAUDE.md §7.3. A provider we have not measured against a
    # labelled set must not be able to tell someone their face is in porn
    # without a human looking first. This blocks `drop` as well: "review band
    # only" is literal, and drop is the more dangerous edge because a real
    # infringement landing there is invisible to the user forever.
    if not entry.calibrated:
        return _review("provider_uncalibrated", version)

    # Rule 3 — the config was written for a different score shape, or the
    # adapter produced neither shape's required value.
    if config.score_kind != score_kind:
        return _review("score_kind_mismatch", version)

    if score_kind == "numeric":
        if provider_score is None:
            return _review("score_kind_mismatch", version)
        # Rule 4 — a value the provider cannot legitimately report.
        if not _in_domain(provider_score, entry.score_domain):
            return _review("score_out_of_domain", version)
        # Rule 5
        band = _numeric_band(provider_score, config.numeric_bands)
        if band is None:
            # Bands do not cover this in-domain value. propose() rejects such
            # a config, so this is a config that predates validation or was
            # inserted by hand.
            return _review("no_band_covers_score", version)
        return BandDecision(
            band=band, reason=f"numeric:{band}", calibration_version=version
        )

    # Rules 6 & 7 — categorical. provider_score is ignored entirely; the
    # provider reported no number and we do not invent one.
    if provider_category is None:
        return _review("score_kind_mismatch", version)
    mapped = config.categorical_bands.get(provider_category)
    if mapped is None:
        return _review("unknown_category", version)
    return BandDecision(
        band=mapped, reason=f"categorical:{mapped}", calibration_version=version
    )


def _in_domain(score: Decimal, domain: ScoreDomain) -> bool:
    if domain.min is not None and score < domain.min:
        return False
    if domain.max is not None and score > domain.max:
        return False
    return True


def _numeric_band(score: Decimal, bands: Sequence[NumericBand]) -> Band | None:
    """``min`` inclusive, ``max`` exclusive.

    The one exception is the band that owns the top of the domain — the band
    no other band starts above. Its ``max`` is inclusive, so the domain
    ceiling itself lands somewhere instead of falling through to
    ``no_band_covers_score``. For Hive that is the difference between 1.0
    banding as ``auto_confirm`` and 1.0 banding as ``review``.
    """
    starts = [b.min for b in bands if b.min is not None]
    highest_start = max(starts) if starts else None
    for band in bands:
        if band.min is not None and score < band.min:
            continue
        if band.max is not None:
            # This band tops the domain iff nothing starts at or above its max.
            tops_domain = highest_start is None or highest_start < band.max
            if score > band.max:
                continue
            if score == band.max and not tops_domain:
                continue
        return band.band
    return None


def roll_up(bands: Sequence[Band]) -> tuple[Band, str]:
    """Several providers' bands -> the infringement's band, plus the reason.

    - **Disagreement resolves downward.** Any spread at all yields ``review``
      — ``drop`` + ``auto_confirm``, and equally ``review`` +
      ``auto_confirm``. Providers disagreeing is evidence of uncertainty, and
      uncertainty means a human looks.
    - **Agreement does not promote.** Two providers at ``review`` stay
      ``review``. Concurrence between two image-search providers indexing
      overlapping corpora is not two independent observations.

    Evidence moves a band down easily and up reluctantly. That asymmetry is
    deliberate and is the correct bias for this product.
    """
    if not bands:
        # Unreachable — the write path always creates an attestation with the
        # infringement — but a total function has no unreachable branches.
        return "review", "no_attestations"
    lowest = min(bands, key=lambda b: BAND_ORDER[b])
    highest = max(bands, key=lambda b: BAND_ORDER[b])
    if lowest != highest:
        return "review", f"disagreement:{lowest}|{highest}->review"
    return highest, f"unanimous:{highest}(n={len(bands)})"


def validate_numeric_bands(
    bands: Sequence[NumericBand], domain: ScoreDomain
) -> list[str]:
    """Problems with a candidate band set, empty when valid. Used by
    ``propose`` — a config that tiles the domain wrongly would silently send
    scores to ``no_band_covers_score`` at runtime."""
    problems: list[str] = []
    for b in bands:
        for edge, value in (("min", b.min), ("max", b.max)):
            if value is None:
                continue
            if not _in_domain(value, domain):
                problems.append(
                    f"band {b.band} {edge}={value} is outside score_domain "
                    f"[{domain.min}, {domain.max}]"
                )
    ordered = sorted(
        bands, key=lambda b: b.min if b.min is not None else Decimal("-1e9")
    )
    cursor = domain.min
    for b in ordered:
        start = b.min if b.min is not None else domain.min
        if cursor is not None and start is not None:
            if start > cursor:
                problems.append(f"gap in coverage between {cursor} and {start}")
            elif start < cursor:
                problems.append(f"overlap at {start}: already covered up to {cursor}")
        cursor = b.max if b.max is not None else domain.max
    if cursor is not None and domain.max is not None and cursor < domain.max:
        problems.append(f"gap in coverage between {cursor} and {domain.max}")
    return problems
```

- [ ] **Step 5: Run the per-attestation tests**

```bash
python -m pytest tests/test_calibration_bands.py -v
```

Expected: all PASS.

- [ ] **Step 6: Write the roll-up tests**

Create `tests/test_calibration_rollup.py`:

```python
"""Attestation bands -> infringement band. Pure, no database.

Permanent — never delete. Rules 2 and 3 of the roll-up are deliberately
asymmetric and it would be easy to "fix" that asymmetry into a bug.
"""

from __future__ import annotations

import pytest

from imageshield.calibration.bands import roll_up
from imageshield.calibration.models import Band


def test_single_attestation_keeps_its_band() -> None:
    assert roll_up(["auto_confirm"]) == ("auto_confirm", "unanimous:auto_confirm(n=1)")


def test_drop_and_auto_confirm_disagreement_yields_review() -> None:
    """The done-when case. One provider says discard it, another says alarm
    the user unreviewed. That is not a reason to average them — it is the
    clearest possible signal that a human should look."""
    band, reason = roll_up(["drop", "auto_confirm"])
    assert band == "review"
    assert reason == "disagreement:drop|auto_confirm->review"


def test_band_reason_records_the_disagreement_in_both_orders() -> None:
    assert roll_up(["auto_confirm", "drop"])[1] == "disagreement:drop|auto_confirm->review"


def test_two_providers_at_review_is_not_a_promotion() -> None:
    """Concurrence between two image-search providers indexing overlapping
    corpora is not two independent observations."""
    band, reason = roll_up(["review", "review"])
    assert band == "review"
    assert reason == "unanimous:review(n=2)"


def test_unanimous_auto_confirm_stays_auto_confirm() -> None:
    """Agreement adds nothing, but it also takes nothing away: each of these
    alone would already have been auto_confirm."""
    assert roll_up(["auto_confirm", "auto_confirm"])[0] == "auto_confirm"


def test_unanimous_drop_stays_drop() -> None:
    assert roll_up(["drop", "drop", "drop"])[0] == "drop"


@pytest.mark.parametrize(
    "bands",
    [
        ["review", "auto_confirm"],
        ["drop", "review"],
        ["drop", "review", "auto_confirm"],
        ["auto_confirm", "auto_confirm", "drop"],
    ],
)
def test_any_spread_at_all_resolves_to_review(bands: list[Band]) -> None:
    assert roll_up(bands)[0] == "review"


def test_empty_is_review_rather_than_an_exception() -> None:
    assert roll_up([]) == ("review", "no_attestations")


def test_roll_up_never_returns_a_band_no_provider_gave() -> None:
    """A guard against anyone introducing averaging here later: the output is
    always either a band that was in the input, or review."""
    for bands in (["drop"], ["drop", "drop"], ["auto_confirm", "review"]):
        result, _ = roll_up(bands)
        assert result in set(bands) | {"review"}
```

- [ ] **Step 7: Run the roll-up tests**

```bash
python -m pytest tests/test_calibration_rollup.py -v
```

Expected: all PASS (no new implementation needed — `roll_up` was written in Step 4).

- [ ] **Step 8: mypy and full suite**

```bash
python -m pytest -q && python -m mypy
```

- [ ] **Step 9: Commit**

```bash
git add src/imageshield/calibration/ tests/test_calibration_bands.py tests/test_calibration_rollup.py
git commit -F - <<'EOF'
Step 7: the banding engine - pure, total, and biased downward

band_for_attestation and roll_up. No DB, no I/O, no clock, and nothing in
either raises: every failure mode returns 'review'. Banding runs inside the
write path of a scan, and a crash there would fail a run over one malformed
provider row. 'review' means a human looks, which is the right answer for
anything we do not understand.

The score_domain rule is the one worth reading. Hive's floor is 0.5. A 0.4 is
impossible - a malformed response, or a key provisioned against the wrong
Hive project, which returns plausible-looking wrong results rather than an
error. Read against an assumed 0-1 scale it is just a low score and bands to
'drop', discarded where nobody will ever see it. Read against score_domain it
bands to 'review'. Same input, opposite outcome, and the harmful reading is
the one you get by not thinking about it. Covered by
test_below_hive_floor_is_review_not_drop.

roll_up is deliberately asymmetric and it would be easy to "fix" that into a
bug. Any disagreement resolves to review, including review + auto_confirm.
Agreement never promotes: two providers at review stay at review, because
concurrence between two image-search providers indexing overlapping corpora
is not two independent observations. Evidence moves a band down easily and up
reluctantly.

No mean, no average, no max-of-scores anywhere - only max-of-ordinal-band.

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>
EOF
```

---

## Task 3: Metrics — every figure carries its sample size

**Files:**
- Create: `src/imageshield/calibration/metrics.py`
- Test: `tests/test_calibration_metrics.py`

**Interfaces:**
- Consumes: `Band`, `Label`, `LabelKind`, `NumericBand`, `ScoreDomain`, `TRUE_MATCH_KINDS` from `calibration.models`.
- Produces:
  - `Metric(value, numerator, denominator, wilson_lower_95)`, `metric(n, d) -> Metric`
  - `EvalRow(label, label_kind, observed, provider_score, provider_category)`
  - `Confusion(tp, fp, fn, tn, uncertain)`, `precision(c)`, `recall(c)`, `npv(c)`
  - `confusion_at_threshold(rows, threshold) -> Confusion`
  - `confusion_for_categories(rows, positive: frozenset[str]) -> Confusion`
  - `SetComposition`, `composition(rows) -> SetComposition`
  - `recall_by_label_kind(rows, threshold) -> dict[LabelKind, Metric]`
  - `ThresholdPoint`, `NumericSweep`, `sweep_numeric(rows, domain, target=Decimal("0.99")) -> NumericSweep`
  - `CategoryPoint`, `CategoricalSweep`, `sweep_categorical(rows, categories, target) -> CategoricalSweep`
  - `effective_sample_size(rows) -> int`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_calibration_metrics.py`:

```python
"""Calibration metrics. Pure, no database.

The load-bearing property: a figure without its sample size is not reportable
here. A precision of 1.0 over 40 items is a weak signal and must never appear
as a bare number.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from imageshield.calibration.metrics import (
    Confusion,
    EvalRow,
    composition,
    confusion_at_threshold,
    confusion_for_categories,
    effective_sample_size,
    metric,
    npv,
    precision,
    recall,
    recall_by_label_kind,
    sweep_categorical,
    sweep_numeric,
)
from imageshield.calibration.models import ScoreDomain

HIVE_DOMAIN = ScoreDomain(min=Decimal("0.5"), max=Decimal("1.0"))
GOOGLE_CATEGORIES = ("full_match", "partial_match", "page_match")


def num(label: str, kind: str, score: str | None) -> EvalRow:
    return EvalRow(
        label=label,
        label_kind=kind,
        observed=score is not None,
        provider_score=Decimal(score) if score is not None else None,
        provider_category=None,
    )


def cat(label: str, kind: str, category: str | None) -> EvalRow:
    return EvalRow(
        label=label,
        label_kind=kind,
        observed=category is not None,
        provider_score=None,
        provider_category=category,
    )


# ── Metric: never a bare number ──────────────────────────────────────────

def test_metric_carries_numerator_and_denominator() -> None:
    m = metric(39, 40)
    assert m.value == pytest.approx(0.975)
    assert (m.numerator, m.denominator) == (39, 40)


def test_zero_denominator_is_none_not_zero() -> None:
    """0.0 and "no data" are different claims. A band with no items must not
    report precision 0.0 — that reads as measured-and-terrible rather than
    unmeasured."""
    m = metric(0, 0)
    assert m.value is None
    assert m.wilson_lower_95 is None


def test_wilson_lower_bound_makes_a_small_perfect_set_look_small() -> None:
    """40-for-40 is not evidence of 0.99. The point estimate is 1.0; the
    honest reading is the interval."""
    m = metric(40, 40)
    assert m.value == 1.0
    assert m.wilson_lower_95 is not None
    assert 0.89 < m.wilson_lower_95 < 0.92


def test_wilson_lower_bound_tightens_as_n_grows() -> None:
    small = metric(40, 40).wilson_lower_95
    large = metric(400, 400).wilson_lower_95
    assert small is not None and large is not None
    assert large > small


# ── Confusion counting: a missing observation is a miss, not an absence ──

def test_unreturned_true_match_counts_as_a_false_negative() -> None:
    """The whole reason eval_seed_coverage exists. A true_match the provider
    never returned must land in FN — if it silently left the denominator,
    recall would be computed only over what the provider already found, which
    is guaranteed to look excellent."""
    rows = [
        num("true_match", "same_person", "0.95"),
        num("true_match", "novel_generation", None),  # provider missed it
    ]
    c = confusion_at_threshold(rows, Decimal("0.90"))
    assert (c.tp, c.fn) == (1, 1)
    assert recall(c).denominator == 2


def test_unreturned_false_match_counts_as_a_true_negative() -> None:
    rows = [num("false_match", "lookalike", None)]
    c = confusion_at_threshold(rows, Decimal("0.90"))
    assert (c.tn, c.fp) == (1, 0)


def test_uncertain_is_excluded_from_every_cell_and_counted_separately() -> None:
    rows = [
        num("true_match", "same_person", "0.95"),
        num("uncertain", "derived_edit", "0.95"),
        num("uncertain", "lookalike", None),
    ]
    c = confusion_at_threshold(rows, Decimal("0.90"))
    assert (c.tp, c.fp, c.fn, c.tn) == (1, 0, 0, 0)
    assert c.uncertain == 2


def test_threshold_is_inclusive_at_or_above() -> None:
    rows = [num("true_match", "same_person", "0.90")]
    assert confusion_at_threshold(rows, Decimal("0.90")).tp == 1
    assert confusion_at_threshold(rows, Decimal("0.9001")).fn == 1


def test_precision_recall_npv_arithmetic() -> None:
    c = Confusion(tp=8, fp=2, fn=4, tn=6, uncertain=0)
    assert precision(c).value == pytest.approx(0.8)     # 8/10
    assert recall(c).value == pytest.approx(8 / 12)
    assert npv(c).value == pytest.approx(6 / 10)


def test_effective_sample_size_excludes_uncertain() -> None:
    """The activate floor counts items that actually enter the arithmetic. A
    set of 200 rows where 150 are uncertain is 50 items of evidence."""
    rows = [num("true_match", "same_person", "0.9")] * 3 + [
        num("uncertain", "lookalike", None)
    ] * 2
    assert effective_sample_size(rows) == 3


# ── Composition, reported before any metric ──────────────────────────────

def test_composition_counts_every_label_kind() -> None:
    rows = [
        num("true_match", "same_person", "0.9"),
        num("true_match", "derived_edit", "0.8"),
        num("true_match", "novel_generation", None),
        num("false_match", "lookalike", "0.7"),
        num("false_match", "unrelated", None),
        num("uncertain", "lookalike", "0.6"),
    ]
    comp = composition(rows)
    assert comp.total == 6
    assert comp.observed == 4
    assert comp.uncertain == 1
    assert comp.by_label_kind["lookalike"] == 2
    assert comp.lookalike_count == 2


def test_zero_lookalikes_produces_a_warning() -> None:
    """Random negatives are easy for any provider to reject and will make a
    bad threshold look excellent. A set without hard negatives cannot produce
    a meaningful precision figure and has to say so."""
    rows = [
        num("true_match", "same_person", "0.99"),
        num("false_match", "unrelated", None),
    ]
    comp = composition(rows)
    assert comp.lookalike_count == 0
    sweep = sweep_numeric(rows, HIVE_DOMAIN)
    assert any("lookalike" in w for w in sweep.warnings)


# ── Recall by label_kind: the coverage gap as a number ───────────────────

def test_recall_is_broken_out_by_label_kind() -> None:
    """A nudify edit preserves background, body, and composition, so image
    search plausibly finds it. A novel generation shares no pixels with
    anything we hold, and recall there will be near zero. Averaging those two
    into one figure hides the product's real limitation."""
    rows = [
        num("true_match", "same_person", "0.99"),
        num("true_match", "same_person", "0.98"),
        num("true_match", "derived_edit", "0.95"),
        num("true_match", "derived_edit", None),
        num("true_match", "novel_generation", None),
        num("true_match", "novel_generation", None),
        num("false_match", "lookalike", "0.60"),
    ]
    by_kind = recall_by_label_kind(rows, Decimal("0.90"))
    assert by_kind["same_person"].value == pytest.approx(1.0)
    assert by_kind["same_person"].denominator == 2
    assert by_kind["derived_edit"].value == pytest.approx(0.5)
    assert by_kind["novel_generation"].value == pytest.approx(0.0)
    assert by_kind["novel_generation"].denominator == 2
    assert "lookalike" not in by_kind   # recall is over positives only


# ── Numeric sweep and its recommendation ─────────────────────────────────

def test_sweep_reports_a_point_per_observed_score() -> None:
    rows = [
        num("true_match", "same_person", "0.95"),
        num("false_match", "lookalike", "0.70"),
    ]
    sweep = sweep_numeric(rows, HIVE_DOMAIN)
    thresholds = {p.threshold for p in sweep.points}
    assert Decimal("0.95") in thresholds
    assert Decimal("0.70") in thresholds


def test_sweep_recommends_the_lowest_threshold_meeting_the_precision_floor() -> None:
    """Lowest, not highest: subject to precision >= 0.99, more recall is
    strictly better, so take the loosest boundary that still clears the bar."""
    rows = (
        [num("true_match", "same_person", "0.96") for _ in range(200)]
        + [num("true_match", "same_person", "0.94") for _ in range(200)]
        + [num("false_match", "lookalike", "0.80") for _ in range(200)]
    )
    sweep = sweep_numeric(rows, HIVE_DOMAIN)
    assert sweep.recommended_auto_confirm_min == Decimal("0.94")


def test_sweep_refuses_to_recommend_when_the_floor_is_unreachable() -> None:
    """The correct outcome when the data cannot support a band is to say so,
    not to loosen the target until a number appears."""
    rows = [
        num("true_match", "same_person", "0.95"),
        num("false_match", "lookalike", "0.95"),   # identical score, opposite label
    ]
    sweep = sweep_numeric(rows, HIVE_DOMAIN)
    assert sweep.recommended_auto_confirm_min is None
    assert any("precision" in w for w in sweep.warnings)


def test_sweep_refuses_when_recommended_bands_would_overlap() -> None:
    rows = [
        num("true_match", "same_person", "0.60"),
        num("false_match", "lookalike", "0.99"),
    ]
    sweep = sweep_numeric(rows, HIVE_DOMAIN)
    assert not (
        sweep.recommended_auto_confirm_min is not None
        and sweep.recommended_drop_max is not None
        and sweep.recommended_auto_confirm_min <= sweep.recommended_drop_max
    )


def test_every_sweep_point_carries_sample_size() -> None:
    rows = [
        num("true_match", "same_person", "0.95"),
        num("false_match", "lookalike", "0.70"),
    ]
    for p in sweep_numeric(rows, HIVE_DOMAIN).points:
        assert p.precision_at_or_above.denominator >= 0
        assert p.recall_at_or_above.denominator >= 0
        assert p.npv_below.denominator >= 0


# ── Categorical sweep ────────────────────────────────────────────────────

def test_categorical_sweep_reports_precision_per_category() -> None:
    rows = [
        cat("true_match", "same_person", "full_match"),
        cat("true_match", "derived_edit", "partial_match"),
        cat("false_match", "lookalike", "partial_match"),
        cat("false_match", "unrelated", "page_match"),
    ]
    sweep = sweep_categorical(rows, GOOGLE_CATEGORIES)
    by_cat = {p.category: p for p in sweep.points}
    assert by_cat["full_match"].precision.value == pytest.approx(1.0)
    assert by_cat["full_match"].precision.denominator == 1
    assert by_cat["partial_match"].precision.value == pytest.approx(0.5)


def test_categorical_sweep_never_reads_provider_score() -> None:
    rows = [cat("true_match", "same_person", "full_match")]
    sweep = sweep_categorical(rows, GOOGLE_CATEGORIES)
    assert all(r.provider_score is None for r in rows)
    assert sweep.points


def test_categorical_recommendation_only_auto_confirms_at_the_floor() -> None:
    rows = [cat("true_match", "same_person", "full_match") for _ in range(200)] + [
        cat("false_match", "lookalike", "partial_match") for _ in range(200)
    ]
    sweep = sweep_categorical(rows, GOOGLE_CATEGORIES)
    assert sweep.recommended["full_match"] == "auto_confirm"
    assert sweep.recommended["partial_match"] == "drop"
    # A category with no items at all cannot be promoted or dropped.
    assert sweep.recommended["page_match"] == "review"
```

- [ ] **Step 2: Run to verify failure**

```bash
python -m pytest tests/test_calibration_metrics.py -v
```

Expected: collection error — `No module named 'imageshield.calibration.metrics'`.

- [ ] **Step 3: Write `metrics.py`**

```python
"""Calibration metrics. Pure functions, no database.

One rule governs this module: **a figure never leaves it without its sample
size**. `Metric` cannot represent a bare proportion, so a precision of 1.0
over 40 items renders as ``1.000 (40/40, 95% lower bound 0.912)`` and reads
like what it is — a weak signal — rather than like a passing grade.

The Wilson lower bound is displayed, not gated on. The activate floor tests
the point estimate plus a minimum sample size (spec §6.5); gating on the
lower bound instead is a possible future tightening, deliberately not taken
now.

Counting rule that matters more than it looks: **predicted-positive means an
observation exists AND clears the threshold.** An eval item with no
observation is predicted negative. That is how a true_match the provider
never returned becomes a false negative rather than vanishing from the
denominator — and it is why eval_seed_coverage has to exist, since without it
"not returned" and "never asked" are indistinguishable.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from imageshield.calibration.models import (
    Band,
    Label,
    LabelKind,
    ScoreDomain,
    TRUE_MATCH_KINDS,
)

# 97.5th percentile of the standard normal — the two-sided 95% interval.
_Z_95 = 1.959963984540054

# "No threshold applies": categorical providers report no number, so an item
# counts as found when the provider returned it at all. A named constant
# rather than a magic -1e9 sprinkled through the call sites.
_NO_THRESHOLD = Decimal("-Infinity")


class Metric(BaseModel):
    """A proportion that cannot be reported without its denominator."""

    model_config = ConfigDict(frozen=True)

    value: float | None
    numerator: int
    denominator: int
    wilson_lower_95: float | None

    def render(self) -> str:
        if self.value is None:
            return f"n/a (0/{self.denominator})"
        lower = (
            f", 95% lower bound {self.wilson_lower_95:.3f}"
            if self.wilson_lower_95 is not None
            else ""
        )
        return f"{self.value:.3f} ({self.numerator}/{self.denominator}{lower})"


def metric(numerator: int, denominator: int) -> Metric:
    if denominator <= 0:
        # None, never 0.0: "unmeasured" and "measured and terrible" are
        # different claims and must not render the same.
        return Metric(value=None, numerator=numerator, denominator=denominator,
                      wilson_lower_95=None)
    return Metric(
        value=numerator / denominator,
        numerator=numerator,
        denominator=denominator,
        wilson_lower_95=_wilson_lower_95(numerator, denominator),
    )


def _wilson_lower_95(k: int, n: int) -> float:
    """Wilson score interval, lower edge. Chosen over the normal
    approximation because it stays sane at p = 1.0, which is exactly the case
    a small eval set produces."""
    p = k / n
    denom = 1.0 + _Z_95 * _Z_95 / n
    centre = p + _Z_95 * _Z_95 / (2 * n)
    margin = _Z_95 * math.sqrt(p * (1.0 - p) / n + _Z_95 * _Z_95 / (4 * n * n))
    return max(0.0, (centre - margin) / denom)


class EvalRow(BaseModel):
    """One eval_item LEFT JOINed to this provider's observation of it.

    ``observed=False`` means the provider was asked (eval_seed_coverage says
    so) and did not return this candidate.
    """

    model_config = ConfigDict(frozen=True)

    label: Label
    label_kind: LabelKind
    observed: bool
    provider_score: Decimal | None
    provider_category: str | None


class Confusion(BaseModel):
    model_config = ConfigDict(frozen=True)

    tp: int
    fp: int
    fn: int
    tn: int
    uncertain: int


def precision(c: Confusion) -> Metric:
    return metric(c.tp, c.tp + c.fp)


def recall(c: Confusion) -> Metric:
    return metric(c.tp, c.tp + c.fn)


def npv(c: Confusion) -> Metric:
    return metric(c.tn, c.tn + c.fn)


def _tally(rows: Sequence[EvalRow], predicted: Sequence[bool]) -> Confusion:
    tp = fp = fn = tn = unc = 0
    for row, positive in zip(rows, predicted, strict=True):
        if row.label == "uncertain":
            unc += 1
            continue
        is_positive = row.label == "true_match"
        if positive:
            tp += is_positive
            fp += not is_positive
        else:
            fn += is_positive
            tn += not is_positive
    return Confusion(tp=tp, fp=fp, fn=fn, tn=tn, uncertain=unc)


def confusion_at_threshold(rows: Sequence[EvalRow], threshold: Decimal) -> Confusion:
    """Predicted positive = observed AND score >= threshold."""
    predicted = [
        row.observed and row.provider_score is not None and row.provider_score >= threshold
        for row in rows
    ]
    return _tally(rows, predicted)


def confusion_for_categories(
    rows: Sequence[EvalRow], positive: frozenset[str]
) -> Confusion:
    predicted = [
        row.observed
        and row.provider_category is not None
        and row.provider_category in positive
        for row in rows
    ]
    return _tally(rows, predicted)


def effective_sample_size(rows: Sequence[EvalRow]) -> int:
    """Items that actually enter the arithmetic. The activate floor counts
    these, not raw rows — 200 rows of which 150 are uncertain is 50 items of
    evidence."""
    return sum(1 for row in rows if row.label != "uncertain")


class SetComposition(BaseModel):
    model_config = ConfigDict(frozen=True)

    total: int
    observed: int
    uncertain: int
    by_label_kind: Mapping[LabelKind, int]
    lookalike_count: int

    def render(self) -> str:
        kinds = "  ".join(f"{k} {v}" for k, v in sorted(self.by_label_kind.items()))
        return (
            f"items {self.total}   observations {self.observed}   "
            f"uncertain {self.uncertain} (excluded)\n  {kinds}"
        )


def composition(rows: Sequence[EvalRow]) -> SetComposition:
    counts: dict[LabelKind, int] = {}
    for row in rows:
        counts[row.label_kind] = counts.get(row.label_kind, 0) + 1
    return SetComposition(
        total=len(rows),
        observed=sum(1 for r in rows if r.observed),
        uncertain=sum(1 for r in rows if r.label == "uncertain"),
        by_label_kind=counts,
        lookalike_count=counts.get("lookalike", 0),
    )


def recall_by_label_kind(
    rows: Sequence[EvalRow], threshold: Decimal
) -> dict[LabelKind, Metric]:
    """Recall over positives only, split by kind.

    A nudify edit preserves background, body, and composition, so image search
    plausibly finds it. A novel generation shares no pixels with anything we
    hold and recall there will be near zero. One averaged figure hides that;
    reporting the split puts the product's real coverage gap in every
    calibration report as a number.
    """
    out: dict[LabelKind, Metric] = {}
    for kind in sorted(TRUE_MATCH_KINDS):
        subset = [r for r in rows if r.label_kind == kind and r.label == "true_match"]
        if not subset:
            continue
        found = sum(
            1
            for r in subset
            if r.observed
            and (
                r.provider_score is None or r.provider_score >= threshold
            )
        )
        out[kind] = metric(found, len(subset))
    return out


class ThresholdPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    threshold: Decimal
    precision_at_or_above: Metric
    recall_at_or_above: Metric
    npv_below: Metric


class NumericSweep(BaseModel):
    model_config = ConfigDict(frozen=True)

    composition: SetComposition
    points: tuple[ThresholdPoint, ...]
    recall_by_kind: Mapping[LabelKind, Metric]
    recommended_auto_confirm_min: Decimal | None
    recommended_drop_max: Decimal | None
    warnings: tuple[str, ...]


def sweep_numeric(
    rows: Sequence[EvalRow],
    domain: ScoreDomain,
    target: Decimal = Decimal("0.99"),
) -> NumericSweep:
    """Candidate boundaries are the observed scores plus the domain edges."""
    candidates = sorted(
        {r.provider_score for r in rows if r.provider_score is not None}
        | {v for v in (domain.min, domain.max) if v is not None}
    )
    points: list[ThresholdPoint] = []
    for t in candidates:
        c = confusion_at_threshold(rows, t)
        # NPV below t is the mirror image: everything NOT predicted positive.
        points.append(
            ThresholdPoint(
                threshold=t,
                precision_at_or_above=precision(c),
                recall_at_or_above=recall(c),
                npv_below=npv(c),
            )
        )

    warnings: list[str] = []
    comp = composition(rows)
    if comp.lookalike_count == 0:
        warnings.append(
            "eval set contains ZERO lookalike items — a set without hard "
            "negatives cannot produce a meaningful precision figure. Random "
            "negatives are easy for any provider to reject and will make a "
            "bad threshold look excellent."
        )

    target_f = float(target)
    # Lowest boundary clearing the precision floor: subject to that floor,
    # more recall is strictly better.
    auto_min = next(
        (
            p.threshold
            for p in points
            if p.precision_at_or_above.value is not None
            and p.precision_at_or_above.value >= target_f
        ),
        None,
    )
    # Highest boundary clearing the NPV floor: subject to it, dropping more
    # saves more review capacity.
    drop_max = next(
        (
            p.threshold
            for p in reversed(points)
            if p.npv_below.value is not None and p.npv_below.value >= target_f
        ),
        None,
    )
    if auto_min is None:
        warnings.append(
            f"no threshold reaches precision >= {target} — auto_confirm is not "
            "supportable by this set. The provider stays uncalibrated and "
            "everything stays review."
        )
    if drop_max is None:
        warnings.append(
            f"no threshold reaches NPV >= {target} — drop is not supportable "
            "by this set."
        )
    if auto_min is not None and drop_max is not None and auto_min <= drop_max:
        warnings.append(
            f"recommended bands overlap (drop < {drop_max}, auto_confirm >= "
            f"{auto_min}); no recommendation issued"
        )
        auto_min = None
        drop_max = None

    return NumericSweep(
        composition=comp,
        points=tuple(points),
        # Recall is reported at the recommended auto_confirm boundary when
        # there is one, and at "returned at all" when there is not — a recall
        # figure at a threshold nobody would ship is not informative.
        recall_by_kind=recall_by_label_kind(
            rows, auto_min if auto_min is not None else _NO_THRESHOLD
        ),
        recommended_auto_confirm_min=auto_min,
        recommended_drop_max=drop_max,
        warnings=tuple(warnings),
    )


class CategoryPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    category: str
    count: int
    precision: Metric


class CategoricalSweep(BaseModel):
    model_config = ConfigDict(frozen=True)

    composition: SetComposition
    points: tuple[CategoryPoint, ...]
    recall_by_kind: Mapping[LabelKind, Metric]
    recommended: Mapping[str, Band]
    warnings: tuple[str, ...]


def sweep_categorical(
    rows: Sequence[EvalRow],
    categories: Sequence[str],
    target: Decimal = Decimal("0.99"),
) -> CategoricalSweep:
    """Greedy and deterministic, documented as such: auto_confirm any category
    whose own precision clears the floor; then drop the remaining categories,
    largest-NPV-first, while the drop set keeps NPV at the floor."""
    target_f = float(target)
    points: list[CategoryPoint] = []
    for category in categories:
        c = confusion_for_categories(rows, frozenset({category}))
        points.append(
            CategoryPoint(
                category=category,
                count=sum(1 for r in rows if r.provider_category == category),
                precision=precision(c),
            )
        )

    recommended: dict[str, Band] = {c: "review" for c in categories}
    for p in points:
        if p.precision.value is not None and p.precision.value >= target_f:
            recommended[p.category] = "auto_confirm"

    remaining = [p for p in points if recommended[p.category] == "review"]
    drop_set: set[str] = set()
    for p in sorted(remaining, key=lambda x: x.precision.value or 0.0):
        candidate = drop_set | {p.category}
        # NPV of "predicted negative" when the positive set is everything not
        # dropped: an item is predicted negative exactly when it landed in a
        # dropped category.
        c = confusion_for_categories(rows, frozenset(candidate))
        # Flip: precision over the drop set measures how many true matches we
        # would be discarding, so NPV of the drop decision is 1 - that.
        wrong = c.tp                       # true matches inside the drop set
        total = c.tp + c.fp
        if total == 0:
            continue
        if metric(total - wrong, total).value is not None and (
            (total - wrong) / total
        ) >= target_f:
            drop_set = candidate
    for category in drop_set:
        recommended[category] = "drop"

    comp = composition(rows)
    warnings: list[str] = []
    if comp.lookalike_count == 0:
        warnings.append(
            "eval set contains ZERO lookalike items — a set without hard "
            "negatives cannot produce a meaningful precision figure."
        )
    return CategoricalSweep(
        composition=comp,
        points=tuple(points),
        # A categorical row has no score, so "found" means the provider
        # returned it at all — _NO_THRESHOLD makes that explicit rather than
        # relying on a magic number.
        recall_by_kind=recall_by_label_kind(rows, _NO_THRESHOLD),
        recommended=recommended,
        warnings=tuple(warnings),
    )
```

- [ ] **Step 4: Run the metrics tests**

```bash
python -m pytest tests/test_calibration_metrics.py -v
```

Expected: all PASS. If `test_categorical_recommendation_only_auto_confirms_at_the_floor` fails on `page_match`, check that a category with zero items short-circuits via the `total == 0: continue` branch — an empty category must stay `review`, never be dropped.

- [ ] **Step 5: mypy and full suite**

```bash
python -m pytest -q && python -m mypy
```

- [ ] **Step 6: Commit**

```bash
git add src/imageshield/calibration/metrics.py tests/test_calibration_metrics.py
git commit -F - <<'EOF'
Step 7: metrics - a figure cannot leave this module without its denominator

Metric has no representation for a bare proportion. precision 1.0 over 40
items renders as "1.000 (40/40, 95% lower bound 0.912)", which reads like the
weak signal it is instead of like a passing grade. Wilson rather than the
normal approximation because it stays sane at p = 1.0, and p = 1.0 is exactly
what a small eval set produces.

The bound is displayed, not gated on. The activate floor tests the point
estimate plus a minimum sample size; gating on the lower bound is a tightening
available later and deliberately not taken now.

Counting rule that carries more weight than it looks: predicted-positive
requires an observation to EXIST and clear the threshold. An item with no
observation is predicted negative, so a true_match the provider never returned
lands in FN rather than dropping out of the denominator. Computing recall only
over what the provider already found would guarantee an excellent-looking
number. This is also why eval_seed_coverage exists - without it "not returned"
and "never asked" are the same absence.

Recall is reported per label_kind. A nudify edit preserves background, body,
and composition so image search plausibly finds it; a novel generation shares
no pixels with anything we hold and recall there will be near zero. One
averaged figure hides that. Split, it appears as a number in every report.

metric(0, 0) is None, never 0.0. Unmeasured and measured-and-terrible are
different claims and must not render identically.

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>
EOF
```

---

## Task 4: Wire banding into the search write path

**Files:**
- Create: `src/imageshield/calibration/store.py` (first slice: policy loading + JSONB parsing)
- Modify: `src/imageshield/search/store.py` — rename `_fan_out`→`fan_out`, `_InfringementKey`→`InfringementKey`; `record_infringements` takes a policy and bands
- Modify: `src/imageshield/search/models.py`, `src/imageshield/search/runner.py`, `src/imageshield/search/worker.py`
- Modify: `src/imageshield/http/models.py`, `src/imageshield/http/routes/search.py`
- Modify: `tests/test_search_store.py`, `tests/test_search_runner.py`, `tests/conftest.py`
- Test: `tests/test_calibration_write_path.py`

**Interfaces:**
- Consumes: `band_for_attestation`, `roll_up`, `BandingPolicy`, `PolicyEntry`, `CalibrationConfig`, `NumericBand`, `ScoreDomain` from Task 2.
- Produces:
  - `calibration.store.parse_score_domain(raw) -> ScoreDomain`
  - `calibration.store.parse_bands(score_kind, raw) -> tuple[tuple[NumericBand,...], dict[str,Band]]`
  - `calibration.store.load_active_policy(conn) -> BandingPolicy`
  - `calibration.store.PostgresCalibrationStore(pool).load_active_policy()`
  - `search.store.fan_out(matches) -> list[InfringementKey]` (public)
  - `SearchStore.record_infringements(run_id, user_ref, provider, matches, policy)` — **signature change**
  - `execute_run(claim, providers, store, policy)` — **signature change**
  - `AttestationRow.band: str`, `AttestationRow.calibration_version: str | None`, `InfringementRow.band_reason: str | None`

- [ ] **Step 1: Write the failing write-path tests**

Create `tests/test_calibration_write_path.py`:

```python
"""Banding inside record_infringements, against real Postgres.

The roll-up runs on every attestation write, so there is never a moment where
a stored infringement band disagrees with its own attestations.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from imageshield.calibration.models import (
    CalibrationConfig,
    NumericBand,
    PolicyEntry,
    ScoreDomain,
)
from imageshield.search.models import ProviderDescriptor
from imageshield.search.provider import ProviderMatch
from imageshield.types import ProviderId

HIVE = ProviderId("hive")
GOOGLE = ProviderId("google")

HIVE_DESC = ProviderDescriptor(
    provider_id=HIVE, score_kind="numeric", score_version="hive-web-search-v1"
)
GOOGLE_DESC = ProviderDescriptor(
    provider_id=GOOGLE, score_kind="categorical", score_version="google-web-detection-v1"
)


def hive_policy(*, calibrated: bool) -> dict[ProviderId, PolicyEntry]:
    return {
        HIVE: PolicyEntry(
            provider_id=HIVE,
            calibrated=calibrated,
            score_domain=ScoreDomain(min=Decimal("0.5"), max=Decimal("1.0")),
            config=CalibrationConfig(
                config_id=uuid4(),
                provider_id=HIVE,
                version="hive-cal-v1",
                score_kind="numeric",
                numeric_bands=(
                    NumericBand(band="drop", max=Decimal("0.72")),
                    NumericBand(band="review", min=Decimal("0.72"), max=Decimal("0.94")),
                    NumericBand(band="auto_confirm", min=Decimal("0.94")),
                ),
            ),
        )
    }


def hive_match(url: str, score: str) -> ProviderMatch:
    return ProviderMatch(
        image_url=f"{url}/img.jpg",
        page_urls=[url],
        provider_score=Decimal(score),
        provider_category=None,
        query_quality=None,
    )


def google_match(url: str, category: str) -> ProviderMatch:
    return ProviderMatch(
        image_url=f"{url}/img.jpg",
        page_urls=[url],
        provider_score=None,
        provider_category=category,
        query_quality=None,
    )


async def test_empty_policy_still_writes_review(search_fixture) -> None:
    """Nothing configured -> rule 1 -> review. This is the state the repo
    ships in, and it must hold without any calibration row existing."""
    store, run_id, user_ref = search_fixture
    await store.record_infringements(
        run_id, user_ref, HIVE_DESC, [hive_match("https://x.test/a", "0.99")], {}
    )
    rows = await store.list_infringements(user_ref, None)
    assert rows[0].band == "review"
    assert rows[0].attestations[0].band == "review"
    assert rows[0].attestations[0].calibration_version is None


async def test_uncalibrated_provider_cannot_auto_confirm(search_fixture) -> None:
    """CLAUDE.md §7.3, asserted against the real write path rather than only
    against the pure function."""
    store, run_id, user_ref = search_fixture
    await store.record_infringements(
        run_id, user_ref, HIVE_DESC, [hive_match("https://x.test/a", "0.99")],
        hive_policy(calibrated=False),
    )
    rows = await store.list_infringements(user_ref, None)
    assert rows[0].band == "review"
    assert rows[0].band_reason == "unanimous:review(n=1)"


async def test_calibrated_provider_bands_and_stamps_the_version(search_fixture) -> None:
    store, run_id, user_ref = search_fixture
    await store.record_infringements(
        run_id, user_ref, HIVE_DESC, [hive_match("https://x.test/a", "0.99")],
        hive_policy(calibrated=True),
    )
    rows = await store.list_infringements(user_ref, None)
    assert rows[0].band == "auto_confirm"
    assert rows[0].attestations[0].band == "auto_confirm"
    assert rows[0].attestations[0].calibration_version == "hive-cal-v1"


async def test_below_hive_floor_is_review_through_the_real_write_path(
    search_fixture,
) -> None:
    """0.4 is impossible for Hive. It must not be discarded as a low score."""
    store, run_id, user_ref = search_fixture
    await store.record_infringements(
        run_id, user_ref, HIVE_DESC, [hive_match("https://x.test/a", "0.40")],
        hive_policy(calibrated=True),
    )
    rows = await store.list_infringements(user_ref, None)
    assert rows[0].band == "review"


async def test_disagreement_across_providers_resolves_to_review(
    search_fixture, google_policy
) -> None:
    """One provider auto_confirms, the other drops. The stored infringement
    must be review, and band_reason must say why — a reviewer needs to
    understand at a glance which rule fired."""
    store, run_id, user_ref = search_fixture
    policy = {**hive_policy(calibrated=True), **google_policy("drop")}
    url = "https://x.test/shared"
    await store.record_infringements(
        run_id, user_ref, HIVE_DESC, [hive_match(url, "0.99")], policy
    )
    await store.record_infringements(
        run_id, user_ref, GOOGLE_DESC, [google_match(url, "page_match")], policy
    )
    rows = await store.list_infringements(user_ref, None)
    assert len(rows) == 1
    assert rows[0].band == "review"
    assert rows[0].band_reason == "disagreement:drop|auto_confirm->review"
    assert len(rows[0].attestations) == 2


async def test_two_providers_at_review_is_not_promoted(
    search_fixture, google_policy
) -> None:
    store, run_id, user_ref = search_fixture
    policy = {**hive_policy(calibrated=True), **google_policy("review")}
    url = "https://x.test/shared"
    await store.record_infringements(
        run_id, user_ref, HIVE_DESC, [hive_match(url, "0.80")], policy
    )
    await store.record_infringements(
        run_id, user_ref, GOOGLE_DESC, [google_match(url, "page_match")], policy
    )
    rows = await store.list_infringements(user_ref, None)
    assert rows[0].band == "review"
    assert rows[0].band_reason == "unanimous:review(n=2)"


async def test_roll_up_is_correct_after_each_write_not_only_at_run_end(
    search_fixture, google_policy
) -> None:
    """After the FIRST provider's write the stored band is already consistent
    with the attestations that exist at that moment. This is the property
    end-of-run roll-up would not have."""
    store, run_id, user_ref = search_fixture
    policy = {**hive_policy(calibrated=True), **google_policy("drop")}
    url = "https://x.test/shared"
    await store.record_infringements(
        run_id, user_ref, HIVE_DESC, [hive_match(url, "0.99")], policy
    )
    mid = await store.list_infringements(user_ref, None)
    assert mid[0].band == "auto_confirm"          # correct for one attestation
    await store.record_infringements(
        run_id, user_ref, GOOGLE_DESC, [google_match(url, "page_match")], policy
    )
    end = await store.list_infringements(user_ref, None)
    assert end[0].band == "review"                # correct for two
```

Add two fixtures to `tests/conftest.py`. `search_fixture` must yield `(PostgresSearchStore, run_id, user_ref)` against a migrated throwaway database — **read `tests/test_search_store.py` first and lift its existing arrangement into the fixture rather than inventing a second setup path.**

```python
@pytest.fixture
def google_policy() -> Callable[[str], dict[Any, Any]]:
    """A Google policy mapping every category to one band, for roll-up tests."""
    from uuid import uuid4

    from imageshield.calibration.models import (
        CalibrationConfig,
        PolicyEntry,
        ScoreDomain,
    )
    from imageshield.types import ProviderId

    def _make(band: str) -> dict[Any, Any]:
        gid = ProviderId("google")
        return {
            gid: PolicyEntry(
                provider_id=gid,
                calibrated=True,
                score_domain=ScoreDomain(
                    categories=("full_match", "partial_match", "page_match")
                ),
                config=CalibrationConfig(
                    config_id=uuid4(),
                    provider_id=gid,
                    version="google-cal-v1",
                    score_kind="categorical",
                    categorical_bands={
                        "full_match": band,
                        "partial_match": band,
                        "page_match": band,
                    },
                ),
            )
        }

    return _make
```

- [ ] **Step 2: Run to verify failure**

```bash
python -m pytest tests/test_calibration_write_path.py -v
```

Expected: FAIL — `record_infringements() takes 5 positional arguments but 6 were given`, and `AttestationRow` has no field `band`.

- [ ] **Step 3: Write the policy loader in `calibration/store.py`**

```python
"""Persistence for calibration configs and eval data — raw SQL, no ORM.

This module is the only place that turns the ``bands`` and ``score_domain``
JSONB columns into typed values. Parsing lives here rather than in
:mod:`models` so the pure modules stay free of anything that can fail on
malformed input.

A malformed config is **skipped, not raised**: the provider ends up with
``config=None``, rule 1 fires, and everything lands in ``review``. A bad row
in this table must not be able to fail a scan.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import structlog
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from imageshield.calibration.models import (
    Band,
    BandingPolicy,
    CalibrationConfig,
    NumericBand,
    PolicyEntry,
    ScoreDomain,
    ScoreKind,
)
from imageshield.types import ProviderId, parse_provider_id

log = structlog.get_logger("imageshield.calibration")

_VALID_BANDS: frozenset[str] = frozenset({"drop", "review", "auto_confirm"})

_LOAD_POLICY_SQL = """
    SELECT p.provider_id, p.calibrated, p.score_domain,
           c.config_id, c.version, c.score_kind, c.bands
    FROM providers p
    LEFT JOIN calibration_configs c
      ON c.provider_id = p.provider_id AND c.active
"""


def parse_score_domain(raw: dict[str, Any] | None) -> ScoreDomain:
    if not raw:
        return ScoreDomain()
    categories = raw.get("categories")
    return ScoreDomain(
        min=Decimal(str(raw["min"])) if raw.get("min") is not None else None,
        max=Decimal(str(raw["max"])) if raw.get("max") is not None else None,
        categories=tuple(categories) if categories else None,
    )


def parse_bands(
    score_kind: ScoreKind, raw: Any
) -> tuple[tuple[NumericBand, ...], dict[str, Band]]:
    """Raises ValueError on anything unrecognised; the caller logs and skips."""
    if score_kind == "numeric":
        if not isinstance(raw, list):
            raise ValueError("numeric bands must be a JSON array")
        parsed: list[NumericBand] = []
        for entry in raw:
            band = entry.get("band")
            if band not in _VALID_BANDS:
                raise ValueError(f"unknown band {band!r}")
            parsed.append(
                NumericBand(
                    band=band,
                    min=Decimal(str(entry["min"])) if entry.get("min") is not None else None,
                    max=Decimal(str(entry["max"])) if entry.get("max") is not None else None,
                )
            )
        return tuple(parsed), {}
    if not isinstance(raw, dict):
        raise ValueError("categorical bands must be a JSON object")
    for value in raw.values():
        if value not in _VALID_BANDS:
            raise ValueError(f"unknown band {value!r}")
    return (), dict(raw)


async def load_active_policy(conn: AsyncConnection[Any]) -> BandingPolicy:
    """Snapshot every provider's calibrated flag, score domain, and active
    config. Taken once per run so every attestation in that run is banded by
    one consistent set of rules — a config activated mid-run cannot split a
    run's results across two rulesets."""
    cur = await conn.execute(_LOAD_POLICY_SQL)
    rows = await cur.fetchall()
    policy: dict[ProviderId, PolicyEntry] = {}
    for (
        provider_id,
        calibrated,
        score_domain,
        config_id,
        version,
        score_kind,
        bands,
    ) in rows:
        pid = parse_provider_id(provider_id)
        config: CalibrationConfig | None = None
        if config_id is not None:
            try:
                numeric, categorical = parse_bands(score_kind, bands)
                config = CalibrationConfig(
                    config_id=config_id,
                    provider_id=pid,
                    version=version,
                    score_kind=score_kind,
                    numeric_bands=numeric,
                    categorical_bands=categorical,
                )
            except (ValueError, KeyError, TypeError) as exc:
                log.error(
                    "calibration.malformed_active_config",
                    provider_id=provider_id,
                    version=version,
                    error=str(exc),
                )
        policy[pid] = PolicyEntry(
            provider_id=pid,
            calibrated=calibrated,
            score_domain=parse_score_domain(score_domain),
            config=config,
        )
    return policy


class PostgresCalibrationStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def load_active_policy(self) -> BandingPolicy:
        async with self._pool.connection() as conn:
            return await load_active_policy(conn)
```

- [ ] **Step 4: Modify `search/store.py`**

Four edits.

**(a)** Rename `_InfringementKey` → `InfringementKey` and `_fan_out` → `fan_out` at every call site, and append to `fan_out`'s docstring:

```
    Public because ``calibrate observe`` must map provider responses to
    candidate URLs through exactly this code. An eval measurement made
    against a reimplementation measures the reimplementation.
```

**(b)** Replace `_UPSERT_ATTESTATION_SQL` and add two statements:

```python
_UPSERT_ATTESTATION_SQL = """
    INSERT INTO attestations (infringement_id, provider_id, score_kind,
                              provider_score, provider_category, query_quality,
                              score_version, last_run_id, band, calibration_version)
    VALUES (%(infringement_id)s, %(provider_id)s, %(score_kind)s,
            %(provider_score)s, %(provider_category)s, %(query_quality)s,
            %(score_version)s, %(run_id)s, %(band)s, %(calibration_version)s)
    ON CONFLICT (infringement_id, provider_id) DO UPDATE
      SET last_confirmed_at = now(),
          confirm_count = attestations.confirm_count + 1,
          provider_score = EXCLUDED.provider_score,
          provider_category = EXCLUDED.provider_category,
          query_quality = EXCLUDED.query_quality,
          score_version = EXCLUDED.score_version,
          last_run_id = EXCLUDED.last_run_id,
          band = EXCLUDED.band,
          calibration_version = EXCLUDED.calibration_version
"""

# Every provider's band for this infringement — including ones written
# earlier in the same run by a different provider — so the roll-up sees the
# whole picture and not just what this call wrote.
_ATTESTATION_BANDS_SQL = """
    SELECT band FROM attestations WHERE infringement_id = %(infringement_id)s
"""

_SET_INFRINGEMENT_BAND_SQL = """
    UPDATE infringements SET band = %(band)s, band_reason = %(band_reason)s
    WHERE infringement_id = %(infringement_id)s
"""
```

**(c)** Update the `SearchStore` Protocol signature and the implementation:

```python
    async def record_infringements(
        self,
        run_id: UUID,
        user_ref: UserRef,
        provider: ProviderDescriptor,
        matches: Sequence[ProviderMatch],
        policy: BandingPolicy,
    ) -> int: ...
```

In `PostgresSearchStore.record_infringements`, replace the body of the loop from the attestation upsert onward:

```python
                infringement_id: UUID = row[0]
                decision = band_for_attestation(
                    policy.get(provider.provider_id),
                    provider.score_kind,
                    key.match.provider_score,
                    key.match.provider_category,
                )
                await conn.execute(
                    _UPSERT_ATTESTATION_SQL,
                    {
                        "infringement_id": infringement_id,
                        "provider_id": provider.provider_id,
                        "score_kind": provider.score_kind,
                        "provider_score": key.match.provider_score,
                        "provider_category": key.match.provider_category,
                        "query_quality": key.match.query_quality,
                        "score_version": provider.score_version,
                        "run_id": run_id,
                        "band": decision.band,
                        "calibration_version": decision.calibration_version,
                    },
                )
                # Roll up here rather than at end of run: otherwise, between
                # provider A's write and provider B's, the stored band
                # disagrees with the attestations backing it and a reader can
                # observe that window.
                cur = await conn.execute(
                    _ATTESTATION_BANDS_SQL, {"infringement_id": infringement_id}
                )
                rolled, reason = roll_up([r[0] for r in await cur.fetchall()])
                await conn.execute(
                    _SET_INFRINGEMENT_BAND_SQL,
                    {
                        "infringement_id": infringement_id,
                        "band": rolled,
                        "band_reason": reason,
                    },
                )
```

Replace the module docstring's third bullet — it currently claims the band is written `'review'` unconditionally, which stops being true here:

```
- ``record_infringements`` bands each attestation through the calibration
  policy snapshot and rolls the infringement up from its attestations (step
  7). With no active config, or an uncalibrated provider, every band is still
  ``review`` — the state the repo ships in.
```

**(d)** Extend `_LIST_INFRINGEMENTS_SQL` to also select `i.band_reason`, `a.band`, `a.calibration_version`. Add `band_reason: str | None` to `InfringementRow`, and `band: str` + `calibration_version: str | None` to `AttestationRow` in `search/models.py`. Update `_group_infringements`' index offsets accordingly — **the positional indices shift, so re-derive every index from the SELECT list rather than patching them individually.** Then add the same fields to `InfringementItem` / `AttestationItem` in `http/models.py` and carry them through in `http/routes/search.py`.

- [ ] **Step 5: Modify `runner.py` and `worker.py`**

`execute_run` takes the policy by injection, matching how it already takes `providers` and `store` — that keeps it testable without a DB:

```python
async def execute_run(
    claim: ClaimedRun,
    providers: Mapping[ProviderId, SearchProvider],
    store: SearchStore,
    policy: BandingPolicy,
) -> RunOutcome:
```

Pass `policy` through to `store.record_infringements(...)`.

In `worker.py`, construct `PostgresCalibrationStore(pool)` next to the search store and load the snapshot once per claimed run, immediately after `claim_run` returns non-None:

```python
    policy = await calibration_store.load_active_policy()
    await execute_run(claim, providers, store, policy)
```

- [ ] **Step 6: Update existing callers in tests**

`tests/test_search_store.py` and `tests/test_search_runner.py` call `record_infringements` / `execute_run`. Add `{}` (an empty policy) at each call site. An empty policy fires rule 1, so every band is `review` — exactly what those tests already assert. Signatures change; assertions do not.

- [ ] **Step 7: Run the write-path tests, then everything**

```bash
python -m pytest tests/test_calibration_write_path.py -v
python -m pytest -q && python -m mypy
```

Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add src/imageshield/calibration/store.py src/imageshield/search/ \
        src/imageshield/http/models.py src/imageshield/http/routes/search.py \
        tests/test_calibration_write_path.py tests/test_search_store.py \
        tests/test_search_runner.py tests/conftest.py
git commit -F - <<'EOF'
Step 7: band on write, roll up on every attestation

record_infringements stops hardcoding 'review'. It bands each attestation
through a policy snapshot taken once per run, then re-reads every attestation
on that infringement and writes the rolled-up band and band_reason - all
inside the transaction that was already there.

Rolling up per write rather than once at end of run costs an extra read per
infringement per provider, which at ~20 infringements x 2 providers is
nothing. What it buys is that no window exists where a stored band disagrees
with the attestations backing it. End-of-run leaves one: after Hive writes
auto_confirm and before Google writes drop, a reader sees auto_confirm on
something that should read review.

Nothing changes for the deployed system yet. calibration_configs is empty and
both providers are calibrated = false, so rules 1 and 2 both fire and every
band is still 'review'. test_empty_policy_still_writes_review pins that so it
cannot drift silently.

A malformed calibration row is logged and skipped, never raised. The provider
falls back to config = None and everything goes to review. A bad row in a
config table must not be able to fail a scan.

_fan_out is now public fan_out. `calibrate observe` has to map provider
responses to candidate URLs through exactly this code, because a measurement
taken against a reimplementation measures the reimplementation.

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>
EOF
```

---

## Task 5: Eval store and `calibrate observe`

**Files:**
- Modify: `src/imageshield/calibration/store.py` (append eval CRUD)
- Create: `devtools/calibrate/__init__.py` (empty), `devtools/calibrate/__main__.py`
- Test: `tests/test_calibration_eval_store.py`, `tests/test_calibrate_observe.py`

**Interfaces:**
- Consumes: `fan_out`, `url_hash` from `search.store` / `search.urlhash`; `SearchProvider`, `ProviderResult` from `search.provider`; `EvalRow` from `calibration.metrics`.
- Produces on `PostgresCalibrationStore`:
  - `insert_eval_item(eval_set_id, seed_uri, candidate_url, label, label_kind, consent_basis, labelled_by) -> UUID`
  - `eval_seeds(eval_set_id) -> tuple[str, ...]`
  - `eval_items_for_seed(eval_set_id, seed_uri) -> tuple[tuple[UUID, str], ...]` (item_id, candidate_url)
  - `upsert_eval_observation(item_id, provider_id, score_kind, provider_score, provider_category, query_quality, score_version) -> None`
  - `record_seed_coverage(eval_set_id, seed_uri, provider_id, status, candidates_returned) -> None`
  - `eval_rows(eval_set_id, provider_id) -> tuple[EvalRow, ...]`
  - `uncovered_seeds(eval_set_id, provider_id) -> tuple[str, ...]`
- Produces in `devtools/calibrate/__main__.py`: `build_parser()`, `cmd_observe(args) -> int`, `main(argv) -> int`, and `observe_seed(store, provider, eval_set_id, seed_uri) -> int` (the testable core, no argparse).

- [ ] **Step 1: Write the failing eval-store tests**

Create `tests/test_calibration_eval_store.py`:

```python
"""Eval item and observation persistence. Real Postgres.

The consent and taxonomy constraints are asserted at the DB level in
test_migrations.py; these tests assert the store surfaces them as errors
rather than swallowing them, and that eval_rows() produces the LEFT JOIN
semantics the metrics module depends on.
"""

from __future__ import annotations

from decimal import Decimal

import psycopg
import pytest

from imageshield.types import ProviderId

HIVE = ProviderId("hive")


async def test_insert_rejects_a_blank_consent_basis(calibration_store) -> None:
    """No eval item without a traceable consent basis. Sourcing is consenting
    participants, public-domain, or synthetic only."""
    with pytest.raises(psycopg.errors.CheckViolation):
        await calibration_store.insert_eval_item(
            "v1", "s3://seed", "https://x.test/a",
            "true_match", "same_person", "   ", "tester",
        )


async def test_insert_rejects_derived_edit_labelled_false_match(
    calibration_store,
) -> None:
    """The inversion that would tune thresholds against the flagship case."""
    with pytest.raises(psycopg.errors.CheckViolation):
        await calibration_store.insert_eval_item(
            "v1", "s3://seed", "https://x.test/a",
            "false_match", "derived_edit", "team member, written consent", "tester",
        )


async def test_eval_rows_marks_an_unobserved_item_as_not_observed(
    calibration_store,
) -> None:
    """The LEFT JOIN that makes a miss countable. An item the provider never
    returned must come back with observed=False, not be absent."""
    found = await calibration_store.insert_eval_item(
        "v1", "s3://seed", "https://x.test/found",
        "true_match", "same_person", "team member, written consent", "tester",
    )
    await calibration_store.insert_eval_item(
        "v1", "s3://seed", "https://x.test/missed",
        "true_match", "novel_generation", "synthetic, public domain", "tester",
    )
    await calibration_store.upsert_eval_observation(
        found, HIVE, "numeric", Decimal("0.95"), None, None, "hive-web-search-v1"
    )
    rows = await calibration_store.eval_rows("v1", HIVE)
    assert len(rows) == 2
    by_observed = {r.observed for r in rows}
    assert by_observed == {True, False}
    missed = next(r for r in rows if not r.observed)
    assert missed.label == "true_match"
    assert missed.provider_score is None


async def test_reobserving_updates_rather_than_appends(calibration_store) -> None:
    item = await calibration_store.insert_eval_item(
        "v1", "s3://seed", "https://x.test/a",
        "true_match", "same_person", "team member, written consent", "tester",
    )
    await calibration_store.upsert_eval_observation(
        item, HIVE, "numeric", Decimal("0.80"), None, None, "hive-web-search-v1"
    )
    await calibration_store.upsert_eval_observation(
        item, HIVE, "numeric", Decimal("0.95"), None, None, "hive-web-search-v1"
    )
    rows = await calibration_store.eval_rows("v1", HIVE)
    assert len(rows) == 1
    assert rows[0].provider_score == Decimal("0.9500")


async def test_uncovered_seeds_reports_seeds_never_run(calibration_store) -> None:
    """The activate floor's coverage condition. A seed with no ok coverage row
    means its items' absences are not evidence of anything."""
    await calibration_store.insert_eval_item(
        "v1", "s3://seed-a", "https://x.test/a",
        "true_match", "same_person", "team member, written consent", "tester",
    )
    await calibration_store.insert_eval_item(
        "v1", "s3://seed-b", "https://x.test/b",
        "false_match", "lookalike", "team member, written consent", "tester",
    )
    await calibration_store.record_seed_coverage("v1", "s3://seed-a", HIVE, "ok", 12)
    assert await calibration_store.uncovered_seeds("v1", HIVE) == ("s3://seed-b",)


async def test_a_failed_seed_run_does_not_count_as_coverage(
    calibration_store,
) -> None:
    """status='timeout' means we did not learn anything about that seed. Its
    items' absences must not be read as misses."""
    await calibration_store.insert_eval_item(
        "v1", "s3://seed-a", "https://x.test/a",
        "true_match", "same_person", "team member, written consent", "tester",
    )
    await calibration_store.record_seed_coverage("v1", "s3://seed-a", HIVE, "timeout", 0)
    assert await calibration_store.uncovered_seeds("v1", HIVE) == ("s3://seed-a",)
```

Add a `calibration_store` fixture to `tests/conftest.py` yielding a `PostgresCalibrationStore` over a migrated throwaway database — same construction as the `search_fixture` added in Task 4.

- [ ] **Step 2: Run to verify failure**

```bash
python -m pytest tests/test_calibration_eval_store.py -v
```

Expected: FAIL — `PostgresCalibrationStore` has no attribute `insert_eval_item`.

- [ ] **Step 3: Append the eval SQL and methods to `calibration/store.py`**

```python
_INSERT_EVAL_ITEM_SQL = """
    INSERT INTO eval_items (eval_set_id, seed_uri, candidate_url, label,
                            label_kind, consent_basis, labelled_by)
    VALUES (%(eval_set_id)s, %(seed_uri)s, %(candidate_url)s, %(label)s,
            %(label_kind)s, %(consent_basis)s, %(labelled_by)s)
    RETURNING item_id
"""

_EVAL_SEEDS_SQL = """
    SELECT DISTINCT seed_uri FROM eval_items
    WHERE eval_set_id = %(eval_set_id)s ORDER BY seed_uri
"""

_EVAL_ITEMS_FOR_SEED_SQL = """
    SELECT item_id, candidate_url FROM eval_items
    WHERE eval_set_id = %(eval_set_id)s AND seed_uri = %(seed_uri)s
"""

# Mirrors the attestations upsert: re-observation UPDATES, never appends.
_UPSERT_EVAL_OBSERVATION_SQL = """
    INSERT INTO eval_observations (item_id, provider_id, score_kind,
                                   provider_score, provider_category,
                                   query_quality, score_version)
    VALUES (%(item_id)s, %(provider_id)s, %(score_kind)s, %(provider_score)s,
            %(provider_category)s, %(query_quality)s, %(score_version)s)
    ON CONFLICT (item_id, provider_id) DO UPDATE
      SET score_kind = EXCLUDED.score_kind,
          provider_score = EXCLUDED.provider_score,
          provider_category = EXCLUDED.provider_category,
          query_quality = EXCLUDED.query_quality,
          score_version = EXCLUDED.score_version,
          observed_at = now()
"""

_RECORD_COVERAGE_SQL = """
    INSERT INTO eval_seed_coverage (eval_set_id, seed_uri, provider_id, status,
                                    candidates_returned)
    VALUES (%(eval_set_id)s, %(seed_uri)s, %(provider_id)s, %(status)s,
            %(candidates_returned)s)
    ON CONFLICT (eval_set_id, seed_uri, provider_id) DO UPDATE
      SET status = EXCLUDED.status,
          candidates_returned = EXCLUDED.candidates_returned,
          observed_at = now()
"""

# LEFT JOIN, deliberately: an item with no observation is a provider MISS and
# must reach the metrics module as observed=False rather than not arriving at
# all. Computing recall over only what a provider returned guarantees an
# excellent-looking number.
_EVAL_ROWS_SQL = """
    SELECT i.label, i.label_kind, (o.observation_id IS NOT NULL) AS observed,
           o.provider_score, o.provider_category
    FROM eval_items i
    LEFT JOIN eval_observations o
      ON o.item_id = i.item_id AND o.provider_id = %(provider_id)s
    WHERE i.eval_set_id = %(eval_set_id)s
    ORDER BY i.item_id
"""

# A seed with no ok coverage row was never successfully asked, so its items'
# absences are not evidence of anything.
_UNCOVERED_SEEDS_SQL = """
    SELECT DISTINCT i.seed_uri
    FROM eval_items i
    WHERE i.eval_set_id = %(eval_set_id)s
      AND NOT EXISTS (
        SELECT 1 FROM eval_seed_coverage c
        WHERE c.eval_set_id = i.eval_set_id
          AND c.seed_uri = i.seed_uri
          AND c.provider_id = %(provider_id)s
          AND c.status = 'ok')
    ORDER BY i.seed_uri
"""
```

Methods on `PostgresCalibrationStore`:

```python
    async def insert_eval_item(
        self,
        eval_set_id: str,
        seed_uri: str,
        candidate_url: str,
        label: str,
        label_kind: str,
        consent_basis: str,
        labelled_by: str,
    ) -> UUID:
        """CheckViolation propagates. A rejected item is a labelling error the
        operator must see, not something to log and continue past."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                _INSERT_EVAL_ITEM_SQL,
                {
                    "eval_set_id": eval_set_id,
                    "seed_uri": seed_uri,
                    "candidate_url": candidate_url,
                    "label": label,
                    "label_kind": label_kind,
                    "consent_basis": consent_basis,
                    "labelled_by": labelled_by,
                },
            )
            row = await cur.fetchone()
        assert row is not None
        item_id: UUID = row[0]
        return item_id

    async def eval_seeds(self, eval_set_id: str) -> tuple[str, ...]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_EVAL_SEEDS_SQL, {"eval_set_id": eval_set_id})
            return tuple(r[0] for r in await cur.fetchall())

    async def eval_items_for_seed(
        self, eval_set_id: str, seed_uri: str
    ) -> tuple[tuple[UUID, str], ...]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                _EVAL_ITEMS_FOR_SEED_SQL,
                {"eval_set_id": eval_set_id, "seed_uri": seed_uri},
            )
            return tuple((r[0], r[1]) for r in await cur.fetchall())

    async def upsert_eval_observation(
        self,
        item_id: UUID,
        provider_id: ProviderId,
        score_kind: str,
        provider_score: Decimal | None,
        provider_category: str | None,
        query_quality: str | None,
        score_version: str,
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                _UPSERT_EVAL_OBSERVATION_SQL,
                {
                    "item_id": item_id,
                    "provider_id": provider_id,
                    "score_kind": score_kind,
                    "provider_score": provider_score,
                    "provider_category": provider_category,
                    "query_quality": query_quality,
                    "score_version": score_version,
                },
            )

    async def record_seed_coverage(
        self,
        eval_set_id: str,
        seed_uri: str,
        provider_id: ProviderId,
        status: str,
        candidates_returned: int,
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                _RECORD_COVERAGE_SQL,
                {
                    "eval_set_id": eval_set_id,
                    "seed_uri": seed_uri,
                    "provider_id": provider_id,
                    "status": status,
                    "candidates_returned": candidates_returned,
                },
            )

    async def eval_rows(
        self, eval_set_id: str, provider_id: ProviderId
    ) -> tuple[EvalRow, ...]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                _EVAL_ROWS_SQL,
                {"eval_set_id": eval_set_id, "provider_id": provider_id},
            )
            rows = await cur.fetchall()
        return tuple(
            EvalRow(
                label=r[0],
                label_kind=r[1],
                observed=r[2],
                provider_score=r[3],
                provider_category=r[4],
            )
            for r in rows
        )

    async def uncovered_seeds(
        self, eval_set_id: str, provider_id: ProviderId
    ) -> tuple[str, ...]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                _UNCOVERED_SEEDS_SQL,
                {"eval_set_id": eval_set_id, "provider_id": provider_id},
            )
            return tuple(r[0] for r in await cur.fetchall())
```

Add the needed imports at the top of `calibration/store.py`: `from uuid import UUID` and `from imageshield.calibration.metrics import EvalRow`.

- [ ] **Step 4: Run the eval-store tests**

```bash
python -m pytest tests/test_calibration_eval_store.py -v
```

Expected: all PASS.

- [ ] **Step 5: Write the failing `observe` test**

Create `tests/test_calibrate_observe.py`:

```python
"""`calibrate observe` — fill eval_observations by calling the REAL adapter.

The adapter and the URL-matching code are the production ones. A measurement
taken against a reimplementation measures the reimplementation, so the fake
here is the provider's HTTP response, never our parsing of it.
"""

from __future__ import annotations

from decimal import Decimal

from imageshield.search.provider import ProviderMatch, ProviderResult
from imageshield.types import ProviderId

from devtools.calibrate.__main__ import observe_seed

HIVE = ProviderId("hive")


class FakeProvider:
    """Stands in for HiveWebSearchProvider at the SearchProvider boundary —
    the same Protocol the worker uses."""

    id = HIVE
    kind = "image_search"
    score_kind = "numeric"
    score_version = "hive-web-search-v1"

    def __init__(self, result: ProviderResult) -> None:
        self._result = result
        self.calls: list[str] = []

    async def search(self, seed_url: str, max_results: int | None = None) -> ProviderResult:
        self.calls.append(seed_url)
        return self._result


def result(*pages: str) -> ProviderResult:
    return ProviderResult(
        provider_id=HIVE,
        status="ok",
        matches=[
            ProviderMatch(
                image_url=f"{p}/img.jpg",
                page_urls=[p],
                provider_score=Decimal("0.93"),
                provider_category=None,
                query_quality=None,
            )
            for p in pages
        ],
        raw_response={"stub": True},
        http_status=200,
        latency_ms=10,
    )


async def test_observe_writes_an_observation_for_a_returned_candidate(
    calibration_store,
) -> None:
    await calibration_store.insert_eval_item(
        "v1", "https://seed.test/a.jpg", "https://x.test/found",
        "true_match", "same_person", "team member, written consent", "tester",
    )
    provider = FakeProvider(result("https://x.test/found"))
    written = await observe_seed(
        calibration_store, provider, "v1", "https://seed.test/a.jpg"
    )
    assert written == 1
    rows = await calibration_store.eval_rows("v1", HIVE)
    assert rows[0].observed is True
    assert rows[0].provider_score == Decimal("0.9300")


async def test_observe_leaves_an_unreturned_candidate_unobserved(
    calibration_store,
) -> None:
    """And still records coverage — that is what turns this absence into a
    countable miss rather than an unknown."""
    await calibration_store.insert_eval_item(
        "v1", "https://seed.test/a.jpg", "https://x.test/missed",
        "true_match", "novel_generation", "synthetic, public domain", "tester",
    )
    provider = FakeProvider(result("https://x.test/something-else"))
    written = await observe_seed(
        calibration_store, provider, "v1", "https://seed.test/a.jpg"
    )
    assert written == 0
    assert await calibration_store.uncovered_seeds("v1", HIVE) == ()
    rows = await calibration_store.eval_rows("v1", HIVE)
    assert rows[0].observed is False


async def test_observe_matches_through_url_normalisation(calibration_store) -> None:
    """The labelled URL and the provider's URL differ only by tracking params
    and a trailing slash. Production dedup treats them as one page, so the
    eval matcher must too — otherwise the measurement disagrees with the
    system being measured."""
    await calibration_store.insert_eval_item(
        "v1", "https://seed.test/a.jpg", "https://X.test/Found/",
        "true_match", "same_person", "team member, written consent", "tester",
    )
    provider = FakeProvider(result("https://x.test/Found?utm_source=twitter"))
    written = await observe_seed(
        calibration_store, provider, "v1", "https://seed.test/a.jpg"
    )
    assert written == 1


async def test_a_failed_provider_call_records_coverage_as_not_ok(
    calibration_store,
) -> None:
    await calibration_store.insert_eval_item(
        "v1", "https://seed.test/a.jpg", "https://x.test/a",
        "true_match", "same_person", "team member, written consent", "tester",
    )
    failed = ProviderResult(
        provider_id=HIVE, status="timeout", matches=[],
        raw_response={"error": "timeout"}, http_status=None, latency_ms=120_000,
    )
    written = await observe_seed(
        calibration_store, FakeProvider(failed), "v1", "https://seed.test/a.jpg"
    )
    assert written == 0
    # NOT covered: we learned nothing about this seed, so its items' absences
    # must not later be counted as misses.
    assert await calibration_store.uncovered_seeds("v1", HIVE) == (
        "https://seed.test/a.jpg",
    )
```

- [ ] **Step 6: Run to verify failure**

```bash
python -m pytest tests/test_calibrate_observe.py -v
```

Expected: collection error — `No module named 'devtools.calibrate'`.

- [ ] **Step 7: Write `devtools/calibrate/__main__.py` with `observe`**

Create `devtools/calibrate/__init__.py` (empty) and `devtools/calibrate/__main__.py`:

```python
"""The calibration harness.

    calibrate observe   --provider hive --eval-set v1 --confirm
    calibrate sweep     --provider hive --eval-set v1
    calibrate propose   --provider hive --eval-set v1 --version v2
    calibrate replay    --config <id>
    calibrate activate  --config <id> --confirm --by <name>
    calibrate trust     --provider hive --confirm --by <name> --reason <text>

Devtools, not a deployable: it holds no HTTP surface and is never imported by
the service. It does use the production engine — ``imageshield.calibration``
and the real ``SearchProvider`` adapters — because a calibration measured
against a reimplementation measures the reimplementation.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

import httpx

from imageshield.calibration.store import PostgresCalibrationStore
from imageshield.config import Config, load_config
from imageshield.db.connection import make_async_pool
from imageshield.search.google import GoogleWebDetectionProvider
from imageshield.search.hive import HiveWebSearchProvider
from imageshield.search.provider import SearchProvider
from imageshield.search.store import fan_out
from imageshield.search.urlhash import url_hash
from imageshield.types import ProviderId, parse_provider_id


def build_provider(
    provider_id: ProviderId, config: Config, client: httpx.AsyncClient
) -> SearchProvider:
    """The same adapters the worker constructs. Adding a provider here without
    adding it to the worker would calibrate something we do not run."""
    if provider_id == "hive":
        return HiveWebSearchProvider(
            client=client,
            api_key=config.hive_api_key,
            base_url=config.hive_base_url,
            timeout_seconds=config.provider_timeout_seconds,
        )
    if provider_id == "google":
        return GoogleWebDetectionProvider(
            client=client,
            api_key=config.google_vision_api_key,
            endpoint=config.google_vision_endpoint,
            timeout_seconds=config.provider_timeout_seconds,
        )
    raise SystemExit(f"no adapter for provider {provider_id!r}")


async def observe_seed(
    store: PostgresCalibrationStore,
    provider: SearchProvider,
    eval_set_id: str,
    seed_uri: str,
) -> int:
    """Call the provider once for one seed; write an observation for every
    labelled candidate it returned, and a coverage row either way.

    Candidate matching goes through the production ``fan_out`` + ``url_hash``.
    If the eval matcher normalised URLs differently from the dedup key, the
    measurement would disagree with the system being measured.

    Returns the number of observations written.
    """
    result = await provider.search(seed_uri)
    await store.record_seed_coverage(
        eval_set_id, seed_uri, provider.id, result.status, len(result.matches)
    )
    if result.status != "ok":
        # We learned nothing about this seed. No coverage means its items'
        # absences are correctly excluded from the recall denominator.
        return 0

    returned = {key.url_hash: key for key in fan_out(result.matches)}
    written = 0
    for item_id, candidate_url in await store.eval_items_for_seed(
        eval_set_id, seed_uri
    ):
        key = returned.get(url_hash(candidate_url))
        if key is None:
            continue
        await store.upsert_eval_observation(
            item_id,
            provider.id,
            provider.score_kind,
            key.match.provider_score,
            key.match.provider_category,
            key.match.query_quality,
            provider.score_version,
        )
        written += 1
    return written


async def run_observe(args: argparse.Namespace) -> int:
    config = load_config()
    provider_id = parse_provider_id(args.provider)
    pool = make_async_pool(config)
    async with pool, httpx.AsyncClient() as client:
        store = PostgresCalibrationStore(pool)
        provider = build_provider(provider_id, config, client)
        seeds = await store.eval_seeds(args.eval_set)
        if not seeds:
            print(f"eval set {args.eval_set!r} has no items — nothing to observe")
            return 1
        # Spends real provider money. Say how much before doing it.
        print(f"{len(seeds)} seed(s) x 1 call to {provider_id} = {len(seeds)} calls")
        if not args.confirm:
            print("refusing without --confirm (this spends provider budget)")
            return 1
        total = 0
        for seed_uri in seeds:
            written = await observe_seed(store, provider, args.eval_set, seed_uri)
            total += written
            print(f"  {seed_uri}: {written} observation(s)")
        uncovered = await store.uncovered_seeds(args.eval_set, provider_id)
        print(f"{total} observation(s) written; {len(uncovered)} seed(s) uncovered")
        if uncovered:
            print("  uncovered (provider call did not succeed):")
            for seed in uncovered:
                print(f"    {seed}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="calibrate")
    sub = parser.add_subparsers(dest="command", required=True)

    observe = sub.add_parser(
        "observe", help="call the real provider over an eval set's seeds"
    )
    observe.add_argument("--provider", required=True)
    observe.add_argument("--eval-set", required=True)
    observe.add_argument(
        "--confirm", action="store_true", help="required: this spends provider budget"
    )
    observe.set_defaults(func=run_observe)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
```

Check `make_async_pool`'s actual signature in [`db/connection.py`](../../../src/imageshield/db/connection.py) and the two adapters' `__init__` keyword names in [`hive.py:69`](../../../src/imageshield/search/hive.py#L69) and [`google.py:51`](../../../src/imageshield/search/google.py#L51) before writing `build_provider` — match them exactly rather than the names guessed above.

- [ ] **Step 8: Run the observe tests, then everything**

```bash
python -m pytest tests/test_calibrate_observe.py -v
python -m pytest -q && python -m mypy
```

Expected: all PASS. `mypy` covers `src/` only (`packages = ["imageshield"]`), so also run `python -m mypy devtools/calibrate` and fix what it reports.

- [ ] **Step 9: Commit**

```bash
git add src/imageshield/calibration/store.py devtools/calibrate/ \
        tests/test_calibration_eval_store.py tests/test_calibrate_observe.py \
        tests/conftest.py
git commit -F - <<'EOF'
Step 7: eval store and `calibrate observe`

observe calls the real adapter through the SearchProvider Protocol - the same
object the worker constructs - and matches returned candidates to labelled
ones through the production fan_out and url_hash. Both of those are
deliberate. A calibration measured against a reimplementation measures the
reimplementation, and if the eval matcher normalised URLs differently from
the dedup key, the measurement would disagree with the system it is measuring.
test_observe_matches_through_url_normalisation pins that: the labelled URL
and the provider's differ by case, a trailing slash, and a utm_source, and
production treats them as one page.

eval_rows LEFT JOINs. An item the provider did not return comes back with
observed=False rather than not arriving, which is what lets a missed
true_match land in FN. Computing recall over only what a provider returned
would guarantee an excellent-looking number.

A failed provider call records coverage with its real status and NOT 'ok', so
that seed stays in uncovered_seeds. We learned nothing about it, and its
items' absences must not later be counted as misses. That is the whole reason
eval_seed_coverage is a separate table.

observe requires --confirm and prints the call count first, because it spends
provider budget.

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>
EOF
```

---

## Task 6: `calibrate sweep` and `calibrate propose`

**Files:**
- Create: `src/imageshield/calibration/report.py` (rendering — kept out of `metrics.py` so that stays pure arithmetic)
- Modify: `src/imageshield/calibration/store.py` (append `insert_config`, `get_config`, `provider_meta`)
- Modify: `devtools/calibrate/__main__.py` (two subcommands)
- Test: `tests/test_calibration_report.py`, `tests/test_calibrate_propose.py`

**Interfaces:**
- Consumes: `sweep_numeric`, `sweep_categorical`, `NumericSweep`, `CategoricalSweep`, `Metric`, `composition`, `effective_sample_size` (Task 3); `eval_rows`, `uncovered_seeds` (Task 5); `validate_numeric_bands`, `parse_score_domain` (Tasks 2, 4).
- Produces:
  - `report.render_numeric_sweep(sweep, provider_id, eval_set_id, uncovered) -> str`
  - `report.render_categorical_sweep(sweep, provider_id, eval_set_id, uncovered) -> str`
  - `store.provider_meta(provider_id) -> ProviderMeta(score_kind, score_domain, calibrated)`
  - `store.insert_config(provider_id, version, score_kind, bands, eval_set_id, eval_sample_size, measured) -> UUID`
  - `store.get_config(config_id) -> CalibrationConfig | None` plus its `eval_set_id` / `provider_id`
  - CLI `run_sweep(args)`, `run_propose(args)`

- [ ] **Step 1: Write the failing report tests**

Create `tests/test_calibration_report.py`:

```python
"""Sweep rendering. Pure string formatting over Task 3's metrics.

The report is the artifact a human reads before deciding whether a provider
may alarm people unreviewed, so what it refuses to omit is load-bearing.
"""

from __future__ import annotations

from decimal import Decimal

from imageshield.calibration.metrics import EvalRow, sweep_categorical, sweep_numeric
from imageshield.calibration.models import ScoreDomain
from imageshield.calibration.report import (
    render_categorical_sweep,
    render_numeric_sweep,
)

HIVE_DOMAIN = ScoreDomain(min=Decimal("0.5"), max=Decimal("1.0"))


def num(label: str, kind: str, score: str | None) -> EvalRow:
    return EvalRow(
        label=label, label_kind=kind, observed=score is not None,
        provider_score=Decimal(score) if score is not None else None,
        provider_category=None,
    )


def test_report_opens_with_composition_before_any_metric() -> None:
    rows = [
        num("true_match", "same_person", "0.95"),
        num("false_match", "lookalike", "0.60"),
    ]
    text = render_numeric_sweep(
        sweep_numeric(rows, HIVE_DOMAIN), "hive", "v1", uncovered=()
    )
    assert text.index("items 2") < text.index("precision")


def test_no_figure_appears_without_its_sample_size() -> None:
    """Every proportion in the body carries (n/d). A bare 0.975 in this
    output would be read as a result rather than as two observations."""
    rows = [
        num("true_match", "same_person", "0.95"),
        num("false_match", "lookalike", "0.60"),
    ]
    text = render_numeric_sweep(
        sweep_numeric(rows, HIVE_DOMAIN), "hive", "v1", uncovered=()
    )
    for line in text.splitlines():
        if "precision" in line and "n/a" not in line:
            assert "/" in line, line


def test_zero_lookalikes_is_shouted_not_footnoted() -> None:
    rows = [
        num("true_match", "same_person", "0.99"),
        num("false_match", "unrelated", "0.60"),
    ]
    text = render_numeric_sweep(
        sweep_numeric(rows, HIVE_DOMAIN), "hive", "v1", uncovered=()
    )
    assert "ZERO lookalike" in text
    assert "WARNING" in text


def test_uncovered_seeds_are_listed() -> None:
    rows = [num("true_match", "same_person", "0.95")]
    text = render_numeric_sweep(
        sweep_numeric(rows, HIVE_DOMAIN), "hive", "v1",
        uncovered=("s3://seed-b", "s3://seed-c"),
    )
    assert "2 seed(s) never successfully observed" in text
    assert "s3://seed-b" in text


def test_report_says_so_when_no_recommendation_is_possible() -> None:
    """The required outcome when the set cannot demonstrate 0.99 — reported
    plainly, not by quietly emitting a looser boundary."""
    rows = [
        num("true_match", "same_person", "0.95"),
        num("false_match", "lookalike", "0.95"),
    ]
    text = render_numeric_sweep(
        sweep_numeric(rows, HIVE_DOMAIN), "hive", "v1", uncovered=()
    )
    assert "no auto_confirm boundary" in text
    assert "stays uncalibrated" in text


def test_recall_by_label_kind_appears_in_the_report() -> None:
    rows = [
        num("true_match", "same_person", "0.99"),
        num("true_match", "novel_generation", None),
        num("false_match", "lookalike", "0.55"),
    ]
    text = render_numeric_sweep(
        sweep_numeric(rows, HIVE_DOMAIN), "hive", "v1", uncovered=()
    )
    assert "novel_generation" in text


def test_categorical_report_shows_the_recommended_mapping() -> None:
    rows = [
        EvalRow(label="true_match", label_kind="same_person", observed=True,
                provider_score=None, provider_category="full_match"),
        EvalRow(label="false_match", label_kind="lookalike", observed=True,
                provider_score=None, provider_category="partial_match"),
    ]
    text = render_categorical_sweep(
        sweep_categorical(rows, ("full_match", "partial_match", "page_match")),
        "google", "v1", uncovered=(),
    )
    assert "full_match" in text
    assert "page_match" in text
```

- [ ] **Step 2: Run to verify failure**

```bash
python -m pytest tests/test_calibration_report.py -v
```

Expected: collection error — `No module named 'imageshield.calibration.report'`.

- [ ] **Step 3: Write `calibration/report.py`**

```python
"""Rendering for sweep output.

Separate from :mod:`metrics` so that module stays pure arithmetic with no
formatting decisions in it.

Two things this renderer will not do: print a proportion without its
denominator, and print a recommendation the data does not support. A sweep
over a set that cannot demonstrate 0.99 says so — the correct outcome there
is that the provider stays uncalibrated and everything stays ``review``, not
a looser boundary that produces a result.
"""

from __future__ import annotations

from collections.abc import Sequence

from imageshield.calibration.metrics import CategoricalSweep, NumericSweep

_RULE = "─" * 72


def _header(provider_id: str, eval_set_id: str, sweep_composition: str) -> list[str]:
    return [
        _RULE,
        f"eval set {eval_set_id} / {provider_id}",
        f"  {sweep_composition}",
    ]


def _coverage(uncovered: Sequence[str]) -> list[str]:
    if not uncovered:
        return ["  seed coverage: complete"]
    lines = [
        f"  WARNING: {len(uncovered)} seed(s) never successfully observed —",
        "    their items' absences are NOT misses and are not evidence of anything:",
    ]
    lines.extend(f"      {seed}" for seed in uncovered)
    return lines


def _warnings(warnings: Sequence[str]) -> list[str]:
    return [f"  WARNING: {w}" for w in warnings]


def render_numeric_sweep(
    sweep: NumericSweep,
    provider_id: str,
    eval_set_id: str,
    uncovered: Sequence[str],
) -> str:
    lines = _header(provider_id, eval_set_id, sweep.composition.render())
    lines.extend(_coverage(uncovered))
    lines.extend(_warnings(sweep.warnings))
    lines.append(_RULE)
    lines.append(
        f"{'threshold':>10}  {'precision >=':>34}  {'recall >=':>34}  {'NPV <':>34}"
    )
    for point in sweep.points:
        lines.append(
            f"{point.threshold!s:>10}  "
            f"{point.precision_at_or_above.render():>34}  "
            f"{point.recall_at_or_above.render():>34}  "
            f"{point.npv_below.render():>34}"
        )
    lines.append(_RULE)
    lines.append("recall by label_kind (positives only):")
    for kind, m in sweep.recall_by_kind.items():
        lines.append(f"  {kind:<18} {m.render()}")
    lines.append(_RULE)
    if sweep.recommended_auto_confirm_min is None:
        lines.append(
            "  no auto_confirm boundary reaches precision >= 0.99 on this set."
        )
        lines.append(
            "  The correct outcome is that the provider stays uncalibrated and "
            "everything stays review."
        )
    else:
        lines.append(
            f"  recommended auto_confirm: score >= {sweep.recommended_auto_confirm_min}"
        )
    if sweep.recommended_drop_max is None:
        lines.append("  no drop boundary reaches NPV >= 0.99 on this set.")
    else:
        lines.append(f"  recommended drop: score < {sweep.recommended_drop_max}")
    lines.append(_RULE)
    return "\n".join(lines)


def render_categorical_sweep(
    sweep: CategoricalSweep,
    provider_id: str,
    eval_set_id: str,
    uncovered: Sequence[str],
) -> str:
    lines = _header(provider_id, eval_set_id, sweep.composition.render())
    lines.extend(_coverage(uncovered))
    lines.extend(_warnings(sweep.warnings))
    lines.append(_RULE)
    lines.append(f"{'category':<18}{'n':>6}  {'precision':>34}  recommended")
    for point in sweep.points:
        lines.append(
            f"{point.category:<18}{point.count:>6}  "
            f"{point.precision.render():>34}  {sweep.recommended[point.category]}"
        )
    lines.append(_RULE)
    lines.append("recall by label_kind (positives only):")
    for kind, m in sweep.recall_by_kind.items():
        lines.append(f"  {kind:<18} {m.render()}")
    lines.append(_RULE)
    if not any(b == "auto_confirm" for b in sweep.recommended.values()):
        lines.append(
            "  no category reaches precision >= 0.99 — the provider stays "
            "uncalibrated and everything stays review."
        )
    lines.append(_RULE)
    return "\n".join(lines)
```

- [ ] **Step 4: Run the report tests**

```bash
python -m pytest tests/test_calibration_report.py -v
```

Expected: all PASS.

- [ ] **Step 5: Append config CRUD to `calibration/store.py`**

```python
_PROVIDER_META_SQL = """
    SELECT score_kind, score_domain, calibrated FROM providers
    WHERE provider_id = %(provider_id)s
"""

_INSERT_CONFIG_SQL = """
    INSERT INTO calibration_configs (provider_id, version, score_kind, bands,
                                     eval_set_id, eval_sample_size, measured)
    VALUES (%(provider_id)s, %(version)s, %(score_kind)s, %(bands)s,
            %(eval_set_id)s, %(eval_sample_size)s, %(measured)s)
    RETURNING config_id
"""

_GET_CONFIG_SQL = """
    SELECT config_id, provider_id, version, score_kind, bands, eval_set_id,
           active
    FROM calibration_configs WHERE config_id = %(config_id)s
"""
```

```python
class ProviderMeta(BaseModel):
    model_config = ConfigDict(frozen=True)

    score_kind: ScoreKind
    score_domain: ScoreDomain
    calibrated: bool


class StoredConfig(BaseModel):
    """A config row plus the provenance the activate floor needs. Distinct
    from CalibrationConfig, which is only what banding needs."""

    model_config = ConfigDict(frozen=True)

    config: CalibrationConfig
    eval_set_id: str | None
    active: bool
```

Methods:

```python
    async def provider_meta(self, provider_id: ProviderId) -> ProviderMeta | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_PROVIDER_META_SQL, {"provider_id": provider_id})
            row = await cur.fetchone()
        if row is None:
            return None
        return ProviderMeta(
            score_kind=row[0],
            score_domain=parse_score_domain(row[1]),
            calibrated=row[2],
        )

    async def insert_config(
        self,
        provider_id: ProviderId,
        version: str,
        score_kind: ScoreKind,
        bands: Any,
        eval_set_id: str | None,
        eval_sample_size: int | None,
        measured: dict[str, Any] | None,
    ) -> UUID:
        """Always INACTIVE. Activation is a separate, gated command."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                _INSERT_CONFIG_SQL,
                {
                    "provider_id": provider_id,
                    "version": version,
                    "score_kind": score_kind,
                    "bands": Jsonb(bands),
                    "eval_set_id": eval_set_id,
                    "eval_sample_size": eval_sample_size,
                    "measured": Jsonb(measured) if measured is not None else None,
                },
            )
            row = await cur.fetchone()
        assert row is not None
        config_id: UUID = row[0]
        return config_id

    async def get_config(self, config_id: UUID) -> StoredConfig | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_GET_CONFIG_SQL, {"config_id": config_id})
            row = await cur.fetchone()
        if row is None:
            return None
        numeric, categorical = parse_bands(row[3], row[4])
        return StoredConfig(
            config=CalibrationConfig(
                config_id=row[0],
                provider_id=parse_provider_id(row[1]),
                version=row[2],
                score_kind=row[3],
                numeric_bands=numeric,
                categorical_bands=categorical,
            ),
            eval_set_id=row[5],
            active=row[6],
        )
```

Add imports: `from psycopg.types.json import Jsonb` and `from pydantic import BaseModel, ConfigDict`.

- [ ] **Step 6: Write the failing propose tests**

Create `tests/test_calibrate_propose.py`:

```python
"""`calibrate propose` writes an INACTIVE config, or refuses."""

from __future__ import annotations

from decimal import Decimal

import pytest

from imageshield.types import ProviderId

from devtools.calibrate.__main__ import build_bands_json, propose_config

HIVE = ProviderId("hive")


async def seed_items(store, n_pos: int, n_look: int) -> None:
    for i in range(n_pos):
        item = await store.insert_eval_item(
            "v1", "s3://seed", f"https://x.test/p{i}",
            "true_match", "same_person", "team member, written consent", "tester",
        )
        await store.upsert_eval_observation(
            item, HIVE, "numeric", Decimal("0.96"), None, None, "hive-web-search-v1"
        )
    for i in range(n_look):
        item = await store.insert_eval_item(
            "v1", "s3://seed", f"https://x.test/l{i}",
            "false_match", "lookalike", "team member, written consent", "tester",
        )
        await store.upsert_eval_observation(
            item, HIVE, "numeric", Decimal("0.60"), None, None, "hive-web-search-v1"
        )
    await store.record_seed_coverage("v1", "s3://seed", HIVE, "ok", n_pos + n_look)


async def test_propose_writes_an_inactive_row(calibration_store) -> None:
    """Nothing propose does can change what users see. Activation is a
    separate, gated command."""
    await seed_items(calibration_store, 100, 100)
    config_id = await propose_config(
        calibration_store, HIVE, "v1", "hive-cal-v2", bands_json=None
    )
    stored = await calibration_store.get_config(config_id)
    assert stored is not None
    assert stored.active is False
    assert stored.eval_set_id == "v1"


async def test_propose_records_the_effective_sample_size(calibration_store) -> None:
    """Excluding uncertain — the number the activate floor tests."""
    await seed_items(calibration_store, 100, 100)
    await calibration_store.insert_eval_item(
        "v1", "s3://seed", "https://x.test/u0",
        "uncertain", "lookalike", "team member, written consent", "tester",
    )
    config_id = await propose_config(
        calibration_store, HIVE, "v1", "hive-cal-v3", bands_json=None
    )
    async with calibration_store._pool.connection() as conn:  # noqa: SLF001
        cur = await conn.execute(
            "SELECT eval_sample_size FROM calibration_configs WHERE config_id = %s",
            (config_id,),
        )
        row = await cur.fetchone()
    assert row[0] == 200


async def test_propose_rejects_bands_outside_the_score_domain(
    calibration_store,
) -> None:
    """A 0.2 boundary is meaningless for Hive, whose floor is 0.5 — and would
    silently send in-domain scores to no_band_covers_score at runtime."""
    await seed_items(calibration_store, 100, 100)
    bad = build_bands_json(Decimal("0.20"), Decimal("0.30"))
    with pytest.raises(ValueError, match="outside score_domain"):
        await propose_config(
            calibration_store, HIVE, "v1", "hive-cal-v4", bands_json=bad
        )


async def test_propose_refuses_when_the_sweep_recommends_nothing(
    calibration_store,
) -> None:
    """Identical scores on opposite labels — no boundary separates them. The
    correct outcome is a refusal, not a config nobody can justify."""
    for label, kind in (("true_match", "same_person"), ("false_match", "lookalike")):
        item = await calibration_store.insert_eval_item(
            "v1", "s3://seed", f"https://x.test/{kind}",
            label, kind, "team member, written consent", "tester",
        )
        await calibration_store.upsert_eval_observation(
            item, HIVE, "numeric", Decimal("0.95"), None, None, "hive-web-search-v1"
        )
    await calibration_store.record_seed_coverage("v1", "s3://seed", HIVE, "ok", 2)
    with pytest.raises(ValueError, match="no recommendation"):
        await propose_config(
            calibration_store, HIVE, "v1", "hive-cal-v5", bands_json=None
        )
```

- [ ] **Step 7: Add `sweep` and `propose` to the CLI**

Append to `devtools/calibrate/__main__.py`:

```python
def build_bands_json(drop_max: Decimal, auto_min: Decimal) -> list[dict[str, str]]:
    """The three-band numeric shape, in the provider's NATIVE units."""
    return [
        {"band": "drop", "max": str(drop_max)},
        {"band": "review", "min": str(drop_max), "max": str(auto_min)},
        {"band": "auto_confirm", "min": str(auto_min)},
    ]


async def load_sweep(
    store: PostgresCalibrationStore, provider_id: ProviderId, eval_set_id: str
) -> tuple[
    ProviderMeta,
    tuple[EvalRow, ...],
    tuple[str, ...],
    NumericSweep | CategoricalSweep,
]:
    meta = await store.provider_meta(provider_id)
    if meta is None:
        raise ValueError(f"unknown provider {provider_id!r}")
    rows = await store.eval_rows(eval_set_id, provider_id)
    if not rows:
        raise ValueError(f"eval set {eval_set_id!r} has no items")
    uncovered = await store.uncovered_seeds(eval_set_id, provider_id)
    sweep = (
        sweep_numeric(rows, meta.score_domain)
        if meta.score_kind == "numeric"
        else sweep_categorical(rows, meta.score_domain.categories or ())
    )
    return meta, rows, uncovered, sweep


async def run_sweep(args: argparse.Namespace) -> int:
    """Writes nothing. Ever."""
    config = load_config()
    provider_id = parse_provider_id(args.provider)
    async with make_async_pool(config) as pool:
        store = PostgresCalibrationStore(pool)
        meta, _rows, uncovered, sweep = await load_sweep(
            store, provider_id, args.eval_set
        )
        render = (
            render_numeric_sweep
            if meta.score_kind == "numeric"
            else render_categorical_sweep
        )
        print(render(sweep, provider_id, args.eval_set, uncovered))
    return 0


async def propose_config(
    store: PostgresCalibrationStore,
    provider_id: ProviderId,
    eval_set_id: str,
    version: str,
    bands_json: object | None,
) -> UUID:
    """Write an INACTIVE config. Raises ValueError rather than writing
    something the data does not support."""
    meta, rows, _uncovered, sweep = await load_sweep(store, provider_id, eval_set_id)

    if bands_json is None:
        # Narrow on the sweep type, not on meta.score_kind — mypy strict does
        # not learn the union member from a string comparison on another
        # object, and an isinstance check here is also the honest guard if the
        # two ever disagree.
        if isinstance(sweep, NumericSweep):
            if (
                sweep.recommended_auto_confirm_min is None
                or sweep.recommended_drop_max is None
            ):
                raise ValueError(
                    "sweep produced no recommendation on this set — refusing to "
                    "invent boundaries. Report the gap; do not loosen the target."
                )
            bands_json = build_bands_json(
                sweep.recommended_drop_max, sweep.recommended_auto_confirm_min
            )
        else:
            bands_json = dict(sweep.recommended)

    if meta.score_kind == "numeric":
        numeric, _ = parse_bands("numeric", bands_json)
        problems = validate_numeric_bands(numeric, meta.score_domain)
        if problems:
            raise ValueError("; ".join(problems))

    measured = {
        "auto_confirm_precision": _band_precision(sweep, "auto_confirm"),
        "drop_npv": _band_npv(sweep),
        "note": "ADVISORY ONLY — activate recomputes from eval_observations",
    }
    return await store.insert_config(
        provider_id=provider_id,
        version=version,
        score_kind=meta.score_kind,
        bands=bands_json,
        eval_set_id=eval_set_id,
        eval_sample_size=effective_sample_size(rows),
        measured=measured,
    )


async def run_propose(args: argparse.Namespace) -> int:
    config = load_config()
    provider_id = parse_provider_id(args.provider)
    bands_json = json.loads(args.bands) if args.bands else None
    async with make_async_pool(config) as pool:
        store = PostgresCalibrationStore(pool)
        try:
            config_id = await propose_config(
                store, provider_id, args.eval_set, args.version, bands_json
            )
        except ValueError as exc:
            print(f"refusing to propose: {exc}")
            return 1
        print(f"wrote INACTIVE config {config_id} ({args.version})")
        print("  activate it with: calibrate activate --config "
              f"{config_id} --confirm --by <name>")
    return 0
```

`_band_precision` / `_band_npv` are small helpers reading the recommended boundary's point off `sweep.points`; write them to return `float | None` and `None` when there is no recommendation. Register both subcommands in `build_parser()` — `sweep` takes `--provider --eval-set`; `propose` takes `--provider --eval-set --version [--bands JSON]`.

- [ ] **Step 8: Run the propose tests, then everything**

```bash
python -m pytest tests/test_calibrate_propose.py tests/test_calibration_report.py -v
python -m pytest -q && python -m mypy && python -m mypy devtools/calibrate
```

- [ ] **Step 9: Commit**

```bash
git add src/imageshield/calibration/report.py src/imageshield/calibration/store.py \
        devtools/calibrate/__main__.py tests/test_calibration_report.py \
        tests/test_calibrate_propose.py
git commit -F - <<'EOF'
Step 7: `calibrate sweep` and `calibrate propose`

sweep writes nothing and refuses to print a proportion without its
denominator or a recommendation the data does not support. When no boundary
reaches 0.99 it says the provider stays uncalibrated and everything stays
review, which is the required outcome - loosening the target until a number
appears is the failure this whole step exists to prevent.

The report opens with set composition before any metric, shouts when the set
contains zero lookalike items, lists seeds that were never successfully
observed, and breaks recall out by label_kind so the novel_generation gap is
a number rather than a caveat.

propose always writes active = false. Nothing it does can change what a user
sees. It rejects bands whose boundaries fall outside the provider's
score_domain, or that leave a gap or overlap in it - such a config would
silently send in-domain scores to no_band_covers_score at runtime.
eval_sample_size is the count of non-uncertain items, because that is what
enters the arithmetic and what the activate floor will test.

`measured` is written and labelled ADVISORY ONLY. activate recomputes.

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>
EOF
```

---

## Task 7: `replay`, `activate`, and `trust` — the two keys

**Files:**
- Modify: `src/imageshield/calibration/store.py` (append re-band, activate, trust, checksum)
- Modify: `devtools/calibrate/__main__.py` (three subcommands + the floor)
- Test: `tests/test_calibrate_replay.py`, `tests/test_calibrate_activate.py`, `tests/test_calibrate_trust.py`

**Interfaces:**
- Consumes: everything from Tasks 2–6.
- Produces:
  - `store.attestations_for_provider(provider_id) -> tuple[StoredAttestation, ...]` where `StoredAttestation(attestation_id, infringement_id, user_ref, score_kind, provider_score, provider_category, band)`
  - `store.all_attestation_bands() -> dict[UUID, list[tuple[ProviderId, Band]]]` keyed by infringement_id
  - `store.band_checksum() -> str`
  - `store.apply_reband(decisions, infringement_bands) -> int`
  - `store.set_active(config_id, activated_by) -> None`
  - `store.set_calibrated(provider_id, value, actor, reason) -> None`
  - `store.audit(action, actor, resource_id, metadata) -> None`
  - CLI: `FloorResult`, `check_floor(store, stored_config, min_items) -> FloorResult`, `plan_reband(store, provider_id, entry) -> RebandPlan`, `run_replay`, `run_activate`, `run_trust`

- [ ] **Step 0: Build the fixtures these tests need**

Everything in this task depends on eval sets shaped to trip exactly one floor
condition each. Write them first, in `tests/conftest.py`, as one builder plus
thin wrappers — eight hand-written fixtures would drift apart.

```python
from decimal import Decimal
from typing import Any

import pytest


async def build_eval_set(
    store: Any,
    *,
    eval_set_id: str = "v1",
    n_true: int = 150,
    n_lookalike: int = 100,
    true_score: str = "0.96",
    lookalike_score: str = "0.60",
    cover: bool = True,
) -> None:
    """One consenting-style eval set with observations, shaped by parameters.

    Defaults are a SOUND set: 250 non-uncertain items, 100 lookalike hard
    negatives, and a clean score separation so a 0.72/0.94 config clears both
    edges. Each unsound fixture below changes exactly one thing, so a failing
    floor test names its own cause.
    """
    from imageshield.types import ProviderId

    hive = ProviderId("hive")
    seed = "s3://seed-a"
    for i in range(n_true):
        item = await store.insert_eval_item(
            eval_set_id, seed, f"https://x.test/t{i}",
            "true_match", "same_person", "team member, written consent", "tester",
        )
        await store.upsert_eval_observation(
            item, hive, "numeric", Decimal(true_score), None, None,
            "hive-web-search-v1",
        )
    for i in range(n_lookalike):
        item = await store.insert_eval_item(
            eval_set_id, seed, f"https://x.test/l{i}",
            "false_match", "lookalike", "team member, written consent", "tester",
        )
        await store.upsert_eval_observation(
            item, hive, "numeric", Decimal(lookalike_score), None, None,
            "hive-web-search-v1",
        )
    if cover:
        await store.record_seed_coverage(
            eval_set_id, seed, hive, "ok", n_true + n_lookalike
        )


HIVE_BANDS_JSON = [
    {"band": "drop", "max": "0.72"},
    {"band": "review", "min": "0.72", "max": "0.94"},
    {"band": "auto_confirm", "min": "0.94"},
]


async def make_config(store: Any, **kwargs: Any):
    """Insert an inactive Hive config with the standard three bands."""
    from imageshield.types import ProviderId

    defaults: dict[str, Any] = {
        "provider_id": ProviderId("hive"),
        "version": "hive-cal-v1",
        "score_kind": "numeric",
        "bands": HIVE_BANDS_JSON,
        "eval_set_id": "v1",
        "eval_sample_size": 250,
        "measured": None,
    }
    return await store.insert_config(**{**defaults, **kwargs})


@pytest.fixture
async def sound_eval_set(calibration_store):
    await build_eval_set(calibration_store)
    return calibration_store, await make_config(calibration_store)


@pytest.fixture
async def weak_precision_eval_set(calibration_store):
    """Lookalikes score as high as true matches — nothing separates them."""
    await build_eval_set(calibration_store, lookalike_score="0.96")
    return calibration_store, await make_config(calibration_store)


@pytest.fixture
async def weak_npv_eval_set(calibration_store):
    """True matches sit below the drop boundary, so dropping loses them."""
    await build_eval_set(calibration_store, true_score="0.60")
    return calibration_store, await make_config(calibration_store)


@pytest.fixture
async def small_eval_set(calibration_store):
    await build_eval_set(calibration_store, n_true=20, n_lookalike=15)
    return calibration_store, await make_config(
        calibration_store, eval_sample_size=35
    )


@pytest.fixture
async def no_lookalike_eval_set(calibration_store):
    """250 items, clean separation, precision 1.0 — and meaningless, because
    every negative is an easy one. This is the set the floor must refuse."""
    from imageshield.types import ProviderId

    await build_eval_set(calibration_store, n_lookalike=0)
    hive = ProviderId("hive")
    for i in range(100):
        item = await calibration_store.insert_eval_item(
            "v1", "s3://seed-a", f"https://x.test/u{i}",
            "false_match", "unrelated", "public domain", "tester",
        )
        await calibration_store.upsert_eval_observation(
            item, hive, "numeric", Decimal("0.55"), None, None, "hive-web-search-v1"
        )
    return calibration_store, await make_config(calibration_store)


@pytest.fixture
async def orphan_config(calibration_store):
    await build_eval_set(calibration_store)
    return calibration_store, await make_config(calibration_store, eval_set_id=None)


@pytest.fixture
async def uncovered_eval_set(calibration_store):
    await build_eval_set(calibration_store, cover=False)
    return calibration_store, await make_config(calibration_store)


@pytest.fixture
async def tampered_measured(calibration_store):
    """measured claims a perfect result; eval_observations disagree. The floor
    must derive from the data, so the claim changes nothing."""
    await build_eval_set(calibration_store, lookalike_score="0.96")
    return calibration_store, await make_config(
        calibration_store,
        measured={"auto_confirm_precision": 1.0, "drop_npv": 1.0},
    )


@pytest.fixture
async def review_only_config(calibration_store):
    await build_eval_set(calibration_store, n_true=1, n_lookalike=0)
    return calibration_store, await make_config(
        calibration_store,
        version="hive-review-only",
        bands=[{"band": "review"}],
    )


@pytest.fixture
async def no_drop_band_config(calibration_store):
    await build_eval_set(calibration_store)
    return calibration_store, await make_config(
        calibration_store,
        version="hive-no-drop",
        bands=[
            {"band": "review", "max": "0.94"},
            {"band": "auto_confirm", "min": "0.94"},
        ],
    )


@pytest.fixture
async def two_sound_configs(calibration_store):
    await build_eval_set(calibration_store)
    first = await make_config(calibration_store, version="hive-cal-v1")
    second = await make_config(calibration_store, version="hive-cal-v2")
    return calibration_store, first, second


class BandedFixture:
    """Real infringements with Hive attestations spanning three scores, all
    currently 'review'. Under a 0.72/0.94 config they move in BOTH directions
    (0.60 -> drop, 0.99 -> auto_confirm, 0.80 stays review), so a replay delta
    is non-trivial and by_direction has more than one key."""

    def __init__(self, store: Any, pool: Any) -> None:
        self._store = store
        self._pool = pool

    def entry(self) -> Any:
        from decimal import Decimal
        from uuid import uuid4

        from imageshield.calibration.models import (
            CalibrationConfig,
            NumericBand,
            PolicyEntry,
            ScoreDomain,
        )
        from imageshield.types import ProviderId

        hive = ProviderId("hive")
        return PolicyEntry(
            provider_id=hive,
            calibrated=True,
            score_domain=ScoreDomain(min=Decimal("0.5"), max=Decimal("1.0")),
            config=CalibrationConfig(
                config_id=uuid4(),
                provider_id=hive,
                version="hive-cal-v1",
                score_kind="numeric",
                numeric_bands=(
                    NumericBand(band="drop", max=Decimal("0.72")),
                    NumericBand(band="review", min=Decimal("0.72"), max=Decimal("0.94")),
                    NumericBand(band="auto_confirm", min=Decimal("0.94")),
                ),
            ),
        )

    async def counts(self) -> tuple[int, int]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT (SELECT count(*) FROM attestations),"
                "       (SELECT count(*) FROM infringements)"
            )
            row = await cur.fetchone()
        return (row[0], row[1])


@pytest.fixture
async def banded_infringements(calibration_store, search_fixture):
    from decimal import Decimal

    from imageshield.search.models import ProviderDescriptor
    from imageshield.search.provider import ProviderMatch
    from imageshield.types import ProviderId, UserRef

    store, run_id, user_ref = search_fixture
    hive = ProviderId("hive")
    desc = ProviderDescriptor(
        provider_id=hive, score_kind="numeric", score_version="hive-web-search-v1"
    )
    # Two users, because "how many people does this retune affect" is the
    # number replay exists to report and a single-user fixture cannot prove it.
    second_user = UserRef(uuid4())
    for owner, scores in (
        (user_ref, ("0.60", "0.80", "0.99")),
        (second_user, ("0.65", "0.97")),
    ):
        for i, score in enumerate(scores):
            url = f"https://x.test/{owner}/{i}"
            # Empty policy: every row starts at 'review', exactly as the
            # shipped system produces them.
            await store.record_infringements(
                run_id,
                owner,
                desc,
                [
                    ProviderMatch(
                        image_url=f"{url}/img.jpg",
                        page_urls=[url],
                        provider_score=Decimal(score),
                        provider_category=None,
                        query_quality=None,
                    )
                ],
                {},
            )
    return BandedFixture(calibration_store, calibration_store._pool)  # noqa: SLF001
```

`search_fixture`'s run belongs to `user_ref`, but `infringements.user_ref` is
written from the argument passed to `record_infringements`, not from the run —
so the second user's rows land correctly without a second run. If a future
change makes the store derive `user_ref` from the run row instead, this
fixture needs a second run and the test asserting `users_affected >= 1`
becomes `>= 2`.

- [ ] **Step 1: Write the failing replay tests**

Create `tests/test_calibrate_replay.py`:

```python
"""`calibrate replay` — read-only, verified by checksum.

This command is the difference between "we tightened the threshold" and "we
tightened the threshold and 340 users will lose an alert they have already
seen." If it can write, it is not that.
"""

from __future__ import annotations

from decimal import Decimal

from imageshield.types import ProviderId

from devtools.calibrate.__main__ import plan_reband

HIVE = ProviderId("hive")


async def test_replay_writes_nothing(calibration_store, banded_infringements) -> None:
    """Row counts and a checksum over every mutable banding column, before
    and after. Both must be identical."""
    before_counts = await banded_infringements.counts()
    before_sum = await calibration_store.band_checksum()

    plan = await plan_reband(calibration_store, HIVE, banded_infringements.entry())
    assert plan.attestations_changed > 0   # the plan is not vacuous

    assert await banded_infringements.counts() == before_counts
    assert await calibration_store.band_checksum() == before_sum


async def test_replay_reports_the_delta_by_direction(
    calibration_store, banded_infringements
) -> None:
    plan = await plan_reband(calibration_store, HIVE, banded_infringements.entry())
    assert plan.attestations_changed >= 1
    assert plan.infringements_changed >= 1
    assert set(plan.by_direction) <= {
        "review->auto_confirm", "review->drop", "auto_confirm->review",
        "drop->review", "auto_confirm->drop", "drop->auto_confirm",
    }


async def test_replay_counts_distinct_users_affected(
    calibration_store, banded_infringements
) -> None:
    """The number a human actually needs: how many people's reports change."""
    plan = await plan_reband(calibration_store, HIVE, banded_infringements.entry())
    assert plan.users_affected >= 1
```

`banded_infringements` is a fixture that creates two users, several infringements with Hive attestations at a spread of scores, all currently banded `review`, and exposes `.entry()` returning a calibrated `PolicyEntry` whose bands would move some of them, plus `.counts()` returning `(attestation_count, infringement_count)`.

- [ ] **Step 2: Write the failing activate tests**

Create `tests/test_calibrate_activate.py`:

```python
"""`calibrate activate` — the floor, recomputed fresh.

Six refusal conditions, each asserted independently. The floor is in code so
loosening it is a code change with a review and a git blame.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from imageshield.types import ProviderId

from devtools.calibrate.__main__ import activate_config, check_floor

HIVE = ProviderId("hive")
MIN_ITEMS = 200


async def test_a_sound_config_passes_the_floor(sound_eval_set) -> None:
    store, config_id = sound_eval_set
    stored = await store.get_config(config_id)
    result = await check_floor(store, stored, MIN_ITEMS)
    assert result.ok, result.problems


async def test_refuses_when_auto_confirm_precision_is_below_target(
    weak_precision_eval_set,
) -> None:
    store, config_id = weak_precision_eval_set
    result = await check_floor(store, await store.get_config(config_id), MIN_ITEMS)
    assert not result.ok
    assert any("precision" in p for p in result.problems)


async def test_refuses_when_drop_npv_is_below_target(weak_npv_eval_set) -> None:
    store, config_id = weak_npv_eval_set
    result = await check_floor(store, await store.get_config(config_id), MIN_ITEMS)
    assert not result.ok
    assert any("NPV" in p for p in result.problems)


async def test_refuses_below_the_minimum_sample_size(small_eval_set) -> None:
    store, config_id = small_eval_set
    result = await check_floor(store, await store.get_config(config_id), MIN_ITEMS)
    assert not result.ok
    assert any("sample size" in p for p in result.problems)


async def test_refuses_a_set_with_zero_lookalikes(no_lookalike_eval_set) -> None:
    """The condition that closes the real failure. A sweep over items with no
    hard negatives yields precision 1.0 trivially, because random negatives
    are easy to reject. The arithmetic passes; the measurement is meaningless.
    This refusal is unconditional and no sample size compensates for it."""
    store, config_id = no_lookalike_eval_set
    result = await check_floor(store, await store.get_config(config_id), MIN_ITEMS)
    assert not result.ok
    assert any("lookalike" in p for p in result.problems)


async def test_refuses_a_config_with_no_eval_set_id(orphan_config) -> None:
    store, config_id = orphan_config
    result = await check_floor(store, await store.get_config(config_id), MIN_ITEMS)
    assert not result.ok
    assert any("eval_set_id" in p for p in result.problems)


async def test_refuses_when_a_seed_was_never_successfully_observed(
    uncovered_eval_set,
) -> None:
    store, config_id = uncovered_eval_set
    result = await check_floor(store, await store.get_config(config_id), MIN_ITEMS)
    assert not result.ok
    assert any("coverage" in p for p in result.problems)


async def test_the_floor_ignores_the_measured_column(tampered_measured) -> None:
    """If the check trusted `measured`, editing a number in a JSONB column
    would defeat it. The data is in eval_observations; derive it there."""
    store, config_id = tampered_measured    # measured says precision 1.0; data says 0.5
    result = await check_floor(store, await store.get_config(config_id), MIN_ITEMS)
    assert not result.ok


async def test_a_review_only_config_skips_the_floor_entirely(
    review_only_config,
) -> None:
    """It alarms nobody, so there is nothing to gate."""
    store, config_id = review_only_config
    result = await check_floor(store, await store.get_config(config_id), MIN_ITEMS)
    assert result.ok


async def test_a_config_without_a_drop_band_is_not_held_to_drop_npv(
    no_drop_band_config,
) -> None:
    """Conditions 1 and 2 are each skipped when the config does not declare
    that band."""
    store, config_id = no_drop_band_config
    result = await check_floor(store, await store.get_config(config_id), MIN_ITEMS)
    assert not any("NPV" in p for p in result.problems)


# ── Activation itself ────────────────────────────────────────────────────

async def test_activate_requires_confirm(sound_eval_set) -> None:
    from devtools.calibrate.__main__ import build_parser, run_activate
    store, config_id = sound_eval_set
    args = build_parser().parse_args(
        ["activate", "--config", str(config_id), "--by", "tester"]
    )
    assert args.confirm is False
    # run_activate must return non-zero and write nothing without --confirm.
    before = await store.band_checksum()
    assert await run_activate_with_store(store, args) != 0
    assert await store.band_checksum() == before


async def test_activate_records_activated_by_and_flips_active(
    sound_eval_set, banded_infringements
) -> None:
    store, config_id = sound_eval_set
    await activate_config(store, config_id, activated_by="tester", min_items=MIN_ITEMS)
    stored = await store.get_config(config_id)
    assert stored.active is True
    async with store._pool.connection() as conn:  # noqa: SLF001
        cur = await conn.execute(
            "SELECT activated_by, activated_at FROM calibration_configs "
            "WHERE config_id = %s",
            (config_id,),
        )
        row = await cur.fetchone()
    assert row[0] == "tester"
    assert row[1] is not None


async def test_activate_stamps_calibration_version_on_every_rebanded_row(
    sound_eval_set, banded_infringements
) -> None:
    store, config_id = sound_eval_set
    await activate_config(store, config_id, activated_by="tester", min_items=MIN_ITEMS)
    async with store._pool.connection() as conn:  # noqa: SLF001
        cur = await conn.execute(
            "SELECT count(*) FROM attestations "
            "WHERE provider_id = 'hive' AND calibration_version IS NULL"
        )
        row = await cur.fetchone()
    assert row[0] == 0


async def test_activate_never_touches_providers_calibrated(
    sound_eval_set, banded_infringements
) -> None:
    """The second key is a separate command. Sound config and may-alarm-people
    are different claims — the first is arithmetic, the second is judgement
    about whether the eval set resembles the real world, and no code can
    check that."""
    store, config_id = sound_eval_set
    before = (await store.provider_meta(HIVE)).calibrated
    await activate_config(store, config_id, activated_by="tester", min_items=MIN_ITEMS)
    assert (await store.provider_meta(HIVE)).calibrated == before


async def test_activating_a_second_config_deactivates_the_first(
    two_sound_configs,
) -> None:
    store, first, second = two_sound_configs
    await activate_config(store, first, activated_by="tester", min_items=MIN_ITEMS)
    await activate_config(store, second, activated_by="tester", min_items=MIN_ITEMS)
    assert (await store.get_config(first)).active is False
    assert (await store.get_config(second)).active is True


async def test_bands_stay_review_while_the_provider_is_untrusted(
    sound_eval_set, banded_infringements
) -> None:
    """Activation alone changes nothing a user sees: rule 2 still forces
    review until `trust` runs. Two keys, both required."""
    store, config_id = sound_eval_set
    await activate_config(store, config_id, activated_by="tester", min_items=MIN_ITEMS)
    async with store._pool.connection() as conn:  # noqa: SLF001
        cur = await conn.execute("SELECT DISTINCT band FROM attestations")
        bands = {r[0] for r in await cur.fetchall()}
    assert bands == {"review"}
```

`run_activate_with_store` is a thin test helper in the same file that calls the same code path as `run_activate` but against the fixture's store instead of building its own pool — write it alongside the fixtures.

- [ ] **Step 3: Write the failing trust tests**

Create `tests/test_calibrate_trust.py`:

```python
"""`calibrate trust` — the only writer of providers.calibrated."""

from __future__ import annotations

import pytest

from imageshield.types import ProviderId

from devtools.calibrate.__main__ import trust_provider

HIVE = ProviderId("hive")


async def test_trust_flips_calibrated_and_audits(
    sound_eval_set, banded_infringements
) -> None:
    store, _config_id = sound_eval_set
    await trust_provider(
        store, HIVE, trusted=True, actor="tester",
        reason="eval set v1 reviewed; 61 lookalikes sourced from consenting team",
    )
    assert (await store.provider_meta(HIVE)).calibrated is True
    async with store._pool.connection() as conn:  # noqa: SLF001
        cur = await conn.execute(
            "SELECT actor_type, action, metadata FROM audit_log "
            "WHERE action = 'calibration.trusted'"
        )
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0][2]["reason"].startswith("eval set v1 reviewed")
    assert rows[0][2]["actor"] == "tester"


async def test_trust_requires_a_reason(sound_eval_set) -> None:
    store, _ = sound_eval_set
    with pytest.raises(ValueError, match="reason"):
        await trust_provider(store, HIVE, trusted=True, actor="tester", reason="  ")


async def test_bands_move_off_review_only_after_both_keys(
    sound_eval_set, banded_infringements
) -> None:
    """The whole two-key design in one assertion."""
    from devtools.calibrate.__main__ import activate_config

    store, config_id = sound_eval_set
    await activate_config(store, config_id, activated_by="tester", min_items=200)
    await trust_provider(
        store, HIVE, trusted=True, actor="tester", reason="eval set reviewed"
    )
    async with store._pool.connection() as conn:  # noqa: SLF001
        cur = await conn.execute("SELECT DISTINCT band FROM attestations")
        bands = {r[0] for r in await cur.fetchall()}
    assert bands != {"review"}


async def test_revoke_sets_everything_back_to_review(
    sound_eval_set, banded_infringements
) -> None:
    """A safety flag you cannot withdraw is not one."""
    from devtools.calibrate.__main__ import activate_config

    store, config_id = sound_eval_set
    await activate_config(store, config_id, activated_by="tester", min_items=200)
    await trust_provider(store, HIVE, trusted=True, actor="tester", reason="ok")
    await trust_provider(
        store, HIVE, trusted=False, actor="tester", reason="eval set was not diverse"
    )
    assert (await store.provider_meta(HIVE)).calibrated is False
    async with store._pool.connection() as conn:  # noqa: SLF001
        cur = await conn.execute("SELECT DISTINCT band FROM attestations")
        assert {r[0] for r in await cur.fetchall()} == {"review"}
```

- [ ] **Step 4: Run all three to verify failure**

```bash
python -m pytest tests/test_calibrate_replay.py tests/test_calibrate_activate.py tests/test_calibrate_trust.py -v
```

Expected: import errors for `plan_reband`, `check_floor`, `activate_config`, `trust_provider`.

- [ ] **Step 5: Append re-band and audit machinery to `calibration/store.py`**

The row model `plan_reband` consumes, defined alongside the other store models:

```python
class StoredAttestation(BaseModel):
    """One attestation as the re-band pass sees it: enough to recompute its
    band, plus the user_ref needed to answer "how many people does this
    retune affect" — the number that makes a retune a decision rather than a
    deploy."""

    model_config = ConfigDict(frozen=True)

    attestation_id: UUID
    infringement_id: UUID
    user_ref: UUID
    score_kind: ScoreKind
    provider_score: Decimal | None
    provider_category: str | None
    band: str
```

```python
_ATTESTATIONS_FOR_PROVIDER_SQL = """
    SELECT a.attestation_id, a.infringement_id, i.user_ref, a.score_kind,
           a.provider_score, a.provider_category, a.band
    FROM attestations a
    JOIN infringements i ON i.infringement_id = a.infringement_id
    WHERE a.provider_id = %(provider_id)s
    ORDER BY a.attestation_id
"""

_ALL_ATTESTATION_BANDS_SQL = """
    SELECT infringement_id, attestation_id, band FROM attestations
    ORDER BY infringement_id, attestation_id
"""

_SET_ATTESTATION_BAND_SQL = """
    UPDATE attestations
    SET band = %(band)s, calibration_version = %(calibration_version)s
    WHERE attestation_id = %(attestation_id)s
"""

# Every mutable banding column, in a stable order. `replay` asserts this is
# unchanged before and after; if replay can move it, replay is not read-only.
_BAND_CHECKSUM_SQL = """
    SELECT
      (SELECT count(*) FROM attestations)::text || '/' ||
      (SELECT count(*) FROM infringements)::text || ':' ||
      coalesce((SELECT md5(string_agg(
          attestation_id::text || '|' || band || '|' ||
          coalesce(calibration_version, ''), E'\\n' ORDER BY attestation_id))
        FROM attestations), '') || ':' ||
      coalesce((SELECT md5(string_agg(
          infringement_id::text || '|' || band || '|' ||
          coalesce(band_reason, ''), E'\\n' ORDER BY infringement_id))
        FROM infringements), '')
"""

_DEACTIVATE_SQL = """
    UPDATE calibration_configs SET active = false
    WHERE provider_id = %(provider_id)s AND active
"""

_ACTIVATE_SQL = """
    UPDATE calibration_configs
    SET active = true, activated_at = now(), activated_by = %(activated_by)s
    WHERE config_id = %(config_id)s
"""

_SET_CALIBRATED_SQL = """
    UPDATE providers SET calibrated = %(calibrated)s
    WHERE provider_id = %(provider_id)s
"""

_AUDIT_SQL = """
    INSERT INTO audit_log (actor_type, action, resource_id, metadata)
    VALUES ('operator', %(action)s, %(resource_id)s, %(metadata)s)
"""
```

`apply_reband` takes the decisions computed by `plan_reband` and writes them in **one transaction**, deactivating the previous config, activating the new one, updating every attestation, and rolling up every affected infringement. Sketch:

```python
    async def apply_reband(
        self,
        provider_id: ProviderId,
        config_id: UUID | None,
        activated_by: str | None,
        decisions: Sequence[AttestationDecision],
        infringement_bands: Mapping[UUID, tuple[str, str]],
    ) -> int:
        """One transaction. Either the config is active and every band under
        it is written, or neither happened — a half-applied retune would
        leave rows banded by a config that is not the active one."""
        async with self._pool.connection() as conn, conn.transaction():
            if config_id is not None:
                await conn.execute(_DEACTIVATE_SQL, {"provider_id": provider_id})
                await conn.execute(
                    _ACTIVATE_SQL,
                    {"config_id": config_id, "activated_by": activated_by},
                )
            for d in decisions:
                await conn.execute(
                    _SET_ATTESTATION_BAND_SQL,
                    {
                        "attestation_id": d.attestation_id,
                        "band": d.band,
                        "calibration_version": d.calibration_version,
                    },
                )
            for infringement_id, (band, reason) in infringement_bands.items():
                await conn.execute(
                    _SET_INFRINGEMENT_BAND_SQL_LOCAL,
                    {
                        "infringement_id": infringement_id,
                        "band": band,
                        "band_reason": reason,
                    },
                )
        return len(decisions)
```

`_SET_INFRINGEMENT_BAND_SQL_LOCAL` is the same statement as `search/store.py`'s `_SET_INFRINGEMENT_BAND_SQL`; import it rather than duplicating the string.

`audit`, `set_calibrated`, `band_checksum`, `attestations_for_provider`, and `all_attestation_bands` are direct wrappers over the SQL above. `set_calibrated` writes the provider row and the `audit_log` row **in one transaction**.

- [ ] **Step 6: Add the floor, replay, activate, and trust to the CLI**

```python
class FloorResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool
    problems: tuple[str, ...]
    auto_confirm_precision: Metric | None
    drop_npv: Metric | None
    sample_size: int
    lookalike_count: int


async def check_floor(
    store: PostgresCalibrationStore, stored: StoredConfig, min_items: int
) -> FloorResult:
    """The machine half of the two keys, recomputed FRESH from
    eval_observations joined to eval_items.

    Never reads calibration_configs.measured. A check that trusts a JSONB
    column an operator can type into is defeated by editing a number.

    A review-only config skips every condition: it alarms nobody.
    """
    config = stored.config
    declares_auto = config.declares("auto_confirm")
    declares_drop = config.declares("drop")
    if not declares_auto and not declares_drop:
        return FloorResult(
            ok=True, problems=(), auto_confirm_precision=None, drop_npv=None,
            sample_size=0, lookalike_count=0,
        )

    problems: list[str] = []
    if stored.eval_set_id is None:
        return FloorResult(
            ok=False,
            problems=("config has no eval_set_id — nothing to measure against",),
            auto_confirm_precision=None, drop_npv=None,
            sample_size=0, lookalike_count=0,
        )

    rows = await store.eval_rows(stored.eval_set_id, config.provider_id)
    comp = composition(rows)
    n = effective_sample_size(rows)

    # Condition 4 first in the narrative because it is the one that closes
    # the real failure: a set with no hard negatives produces precision 1.0
    # trivially, and no amount of sample size compensates.
    if comp.lookalike_count == 0:
        problems.append(
            "eval set contains ZERO lookalike items — a set without hard "
            "negatives cannot produce a meaningful precision figure"
        )
    if n < min_items:
        problems.append(
            f"effective sample size {n} is below the floor of {min_items} "
            "(non-uncertain items only)"
        )
    uncovered = await store.uncovered_seeds(stored.eval_set_id, config.provider_id)
    if uncovered:
        problems.append(
            f"{len(uncovered)} seed(s) have no successful coverage row — their "
            "items' absences are not evidence and cannot enter recall"
        )

    ac_precision = _precision_of_band(rows, config, "auto_confirm")
    drop_npv_metric = _npv_of_band(rows, config, "drop")
    if declares_auto:
        if ac_precision.value is None or ac_precision.value < 0.99:
            problems.append(
                f"auto_confirm precision {ac_precision.render()} is below 0.99"
            )
    if declares_drop:
        if drop_npv_metric.value is None or drop_npv_metric.value < 0.99:
            problems.append(f"drop NPV {drop_npv_metric.render()} is below 0.99")

    return FloorResult(
        ok=not problems,
        problems=tuple(problems),
        auto_confirm_precision=ac_precision if declares_auto else None,
        drop_npv=drop_npv_metric if declares_drop else None,
        sample_size=n,
        lookalike_count=comp.lookalike_count,
    )
```

`_precision_of_band(rows, config, band)` applies `band_for_attestation` to each eval row under a **calibrated** `PolicyEntry` built from this config, treats rows landing in `band` as predicted-positive, and returns `precision(...)`. `_npv_of_band` is the mirror using `npv(...)` over rows landing in `drop`. Building the entry with `calibrated=True` is correct here and only here — the question the floor asks is "*if* we trusted this provider, would this config be sound", which is exactly what `trust` then decides separately.

```python
class AttestationDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    attestation_id: UUID
    infringement_id: UUID
    user_ref: UUID
    old_band: str
    band: str
    calibration_version: str | None


class RebandPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    decisions: tuple[AttestationDecision, ...]
    infringement_bands: Mapping[UUID, tuple[str, str]]
    attestations_changed: int
    infringements_changed: int
    users_affected: int
    by_direction: Mapping[str, int]


async def plan_reband(
    store: PostgresCalibrationStore, provider_id: ProviderId, entry: PolicyEntry
) -> RebandPlan:
    """Compute what a config WOULD do. Writes nothing — `replay` is exactly
    this function plus rendering, and `activate` is this function plus
    apply_reband."""
    stored = await store.attestations_for_provider(provider_id)
    existing = await store.all_attestation_bands()

    decisions: list[AttestationDecision] = []
    proposed: dict[UUID, dict[UUID, str]] = {}
    for a in stored:
        d = band_for_attestation(
            entry, a.score_kind, a.provider_score, a.provider_category
        )
        decisions.append(
            AttestationDecision(
                attestation_id=a.attestation_id,
                infringement_id=a.infringement_id,
                user_ref=a.user_ref,
                old_band=a.band,
                band=d.band,
                calibration_version=d.calibration_version,
            )
        )
        proposed.setdefault(a.infringement_id, {})[a.attestation_id] = d.band

    infringement_bands: dict[UUID, tuple[str, str]] = {}
    infringements_changed = 0
    for infringement_id, current in existing.items():
        # Other providers' attestations keep their existing bands; only this
        # provider's move. That is the point of a per-provider config.
        bands = [
            proposed.get(infringement_id, {}).get(attestation_id, band)
            for attestation_id, band in current
        ]
        rolled, reason = roll_up(bands)
        infringement_bands[infringement_id] = (rolled, reason)

    changed = [d for d in decisions if d.band != d.old_band]
    by_direction: dict[str, int] = {}
    for d in changed:
        key = f"{d.old_band}->{d.band}"
        by_direction[key] = by_direction.get(key, 0) + 1
    infringements_changed = sum(
        1 for iid in infringement_bands if iid in {d.infringement_id for d in changed}
    )

    return RebandPlan(
        decisions=tuple(decisions),
        infringement_bands=infringement_bands,
        attestations_changed=len(changed),
        infringements_changed=infringements_changed,
        users_affected=len({d.user_ref for d in changed}),
        by_direction=by_direction,
    )
```

`run_replay` loads the config, builds a calibrated `PolicyEntry` from it, calls `plan_reband`, and prints the delta — never `apply_reband`.

```python
async def activate_config(
    store: PostgresCalibrationStore,
    config_id: UUID,
    activated_by: str,
    min_items: int,
) -> None:
    stored = await store.get_config(config_id)
    if stored is None:
        raise ValueError(f"no config {config_id}")
    floor = await check_floor(store, stored, min_items)
    if not floor.ok:
        raise ValueError("floor not met: " + "; ".join(floor.problems))
    meta = await store.provider_meta(stored.config.provider_id)
    assert meta is not None
    # Re-band under the provider's REAL calibrated flag, not the floor's
    # hypothetical one: activation alone must not move anything off review.
    entry = PolicyEntry(
        provider_id=stored.config.provider_id,
        calibrated=meta.calibrated,
        score_domain=meta.score_domain,
        config=stored.config,
    )
    plan = await plan_reband(store, stored.config.provider_id, entry)
    await store.apply_reband(
        stored.config.provider_id, config_id, activated_by,
        plan.decisions, plan.infringement_bands,
    )
    await store.audit(
        "calibration.activated",
        actor=activated_by,
        resource_id=config_id,
        metadata={
            "provider_id": stored.config.provider_id,
            "version": stored.config.version,
            "eval_set_id": stored.eval_set_id,
            "sample_size": floor.sample_size,
            "lookalike_count": floor.lookalike_count,
            "attestations_rebanded": len(plan.decisions),
            "users_affected": plan.users_affected,
        },
    )


async def trust_provider(
    store: PostgresCalibrationStore,
    provider_id: ProviderId,
    *,
    trusted: bool,
    actor: str,
    reason: str,
) -> None:
    """The second key, and the only writer of providers.calibrated.

    "This config is sound" and "this provider may now alarm people without a
    human looking" are different claims. The first is arithmetic and the
    floor checks it. The second is judgement about whether the eval set
    resembles the real world, and no code can check that.
    """
    if not reason.strip():
        raise ValueError("--reason is required and must not be blank")
    await store.set_calibrated(provider_id, trusted, actor, reason)
    # Re-band under the new flag: revoking must actually put everything back
    # to review, or the flag is decorative.
    meta = await store.provider_meta(provider_id)
    assert meta is not None
    active = await store.active_config(provider_id)
    entry = PolicyEntry(
        provider_id=provider_id,
        calibrated=meta.calibrated,
        score_domain=meta.score_domain,
        config=active,
    )
    plan = await plan_reband(store, provider_id, entry)
    await store.apply_reband(
        provider_id, None, None, plan.decisions, plan.infringement_bands
    )
```

Add `store.active_config(provider_id) -> CalibrationConfig | None` reading the row where `active` — reuse `_LOAD_POLICY_SQL`'s parsing.

Register the three subcommands: `replay --config`; `activate --config --confirm --by`; `trust --provider --confirm --by --reason [--revoke]`. Both `run_activate` and `run_trust` return `1` and write nothing when `--confirm` is absent.

- [ ] **Step 7: Run all three test files, then everything**

```bash
python -m pytest tests/test_calibrate_replay.py tests/test_calibrate_activate.py tests/test_calibrate_trust.py -v
python -m pytest -q && python -m mypy && python -m mypy devtools/calibrate
```

- [ ] **Step 8: Commit**

```bash
git add src/imageshield/calibration/store.py devtools/calibrate/__main__.py \
        tests/test_calibrate_replay.py tests/test_calibrate_activate.py \
        tests/test_calibrate_trust.py tests/conftest.py
git commit -F - <<'EOF'
Step 7: replay, activate, trust - two keys that defend different failures

The floor lives in code, so loosening it is a code change with a review and a
git blame. That defends against deadline pressure. It does not defend against
a bad eval set producing good-looking numbers, which is the likelier failure:
a sweep over 40 items with no lookalikes yields precision 1.0 trivially,
because random negatives are easy to reject. The arithmetic passed and the
measurement was meaningless. So `calibrated` stays a separate human command.

check_floor recomputes from eval_observations joined to eval_items and never
reads calibration_configs.measured. A check that trusts a JSONB column an
operator can type into is defeated by editing a number, and the data is right
there. Six refusals: auto_confirm precision, drop NPV, effective sample size,
zero lookalikes, missing eval_set_id, and any seed without a successful
coverage row. The first two are skipped when the config does not declare that
band. A review-only config skips all of them - it alarms nobody.

The zero-lookalike refusal is unconditional. No sample size compensates for a
set that cannot produce a meaningful precision figure.

activate never touches providers.calibrated, so activation alone moves
nothing off review - rule 2 still fires. test_bands_stay_review_while_the_
provider_is_untrusted pins that, and test_bands_move_off_review_only_after_
both_keys pins the pair.

trust requires a non-blank --reason and writes actor and reason to audit_log.
--revoke sets calibrated false and re-bands everything back to review: a
safety flag you cannot withdraw is not one.

replay is plan_reband plus rendering and nothing else. Read-only is asserted
by row counts and an md5 over every mutable banding column before and after -
if replay can move that checksum, it is not the thing that makes a retune
safe to ship.

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>
EOF
```

---

## Task 8: The cross-provider tripwire and the docs

**Files:**
- Modify: `tests/test_boundaries.py` (new permanent grep)
- Modify: `SCHEMA.md`, `CLAUDE.md`, `NEAR-TERM-BUILD.md`, `INVARIANTS.md`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing importable. This task is the enforcement and the record.

- [ ] **Step 1: Write the failing tripwire test**

Append to `tests/test_boundaries.py`:

```python
# CLAUDE.md §7.2: Provider A's 0.92 and Provider B's 0.92 are different
# quantities with different distributions. Averaging them, or comparing them,
# produces a number with no meaning that will look entirely plausible — and
# calibration is where the temptation lives, because "just normalise onto a
# common scale" is the obvious-looking move. NEAR-TERM-BUILD.md §2.3 proposed
# exactly that with a calibrated_score column; we do not build it.
#
# No allowlist. If a legitimate `average` ever needs to exist in these two
# directories, adding it should cost a code review.
FORBIDDEN_CROSS_PROVIDER_MATHS = re.compile(r"\bmean\b|\baverage\b|\bavg\b", re.I)

SCORE_DIRS = ("search", "calibration")


def _scored_source_files() -> list[Path]:
    files = [p for d in SCORE_DIRS for p in sorted((SRC / "imageshield" / d).rglob("*.py"))]
    assert files, "search/ and calibration/ scan found nothing — paths wrong?"
    return files


def test_no_cross_provider_averaging_in_scoring_code() -> None:
    offenders = [
        f"{path}:{i}: {line.strip()}"
        for path in _scored_source_files()
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if FORBIDDEN_CROSS_PROVIDER_MATHS.search(line)
    ]
    assert offenders == []
```

- [ ] **Step 2: Run it**

```bash
python -m pytest tests/test_boundaries.py -v
```

Expected: PASS. **If it fails, do not add an allowlist** — read the offending line and remove the arithmetic. The one likely false positive is prose in a docstring saying "average"; reword the prose.

- [ ] **Step 3: Verify the tripwire actually catches something**

Temporarily add `avg_score = sum(scores) / len(scores)` to `src/imageshield/calibration/bands.py`, run the test, confirm it FAILS naming that line, then remove it. Same discipline as step 2's `photo bytea` check for the schema lint — a tripwire nobody has seen fire is not known to work.

- [ ] **Step 4: Update `SCHEMA.md`**

Add the four tables (`calibration_configs`, `eval_items`, `eval_observations`, `eval_seed_coverage`) with the DDL from migration 0007 and their comments, and add `band` / `calibration_version` to the `attestations` block and `band_reason` to `infringements`. Follow the existing document's ordering and prose style.

- [ ] **Step 5: Update `CLAUDE.md`**

Two edits:

- §8 build order: mark step 7 complete, in the same style used for steps 1–6.
- §7.3, replace the paragraph with the mechanism now that it exists:

```markdown
### 7.3 Uncalibrated providers reach `review` band only

Never `auto_confirm`, and never `drop` either — a real infringement in `drop`
is invisible to the user forever, which is the worse of the two edges.

Two keys move a provider off review-only, and they defend different failures.
`calibrate activate` enforces a floor **recomputed from `eval_observations`**,
never from the stored `measured` column: precision ≥ 0.99 on any declared
`auto_confirm` band, NPV ≥ 0.99 on any declared `drop` band, effective sample
size ≥ `CALIBRATION_MIN_EVAL_ITEMS`, at least one `lookalike` item, and full
seed coverage. That defends against loosening the bar under deadline pressure,
because the floor is code.

`calibrate trust` is separate and human, and is the only writer of
`providers.calibrated`. That defends against a bad eval set producing
good-looking numbers — a sweep over 40 items with no hard negatives yields
precision 1.0 trivially. *This config is sound* and *this provider may alarm
people unreviewed* are different claims; no code can check the second.
```

- [ ] **Step 6: Correct `NEAR-TERM-BUILD.md` §2.3**

It currently says to "derive each provider's mapping onto the common scale" and shows a `calibrated_score NUMERIC(5,2)` column and a `calibrate(rawScore): number` interface method. That is the cross-provider comparison §7.2 forbids and the tripwire in Step 1 now blocks. Replace with:

```markdown
## 2.3 Calibration

**Do not merge raw scores, and do not map them onto a common scale.** Provider
A's 0.92 and Provider B's 0.92 are different quantities with different
distributions; a shared 0–100 scale makes them look comparable when they are
not. Each provider gets its own band boundaries in its own native units, from
its own labelled measurements. Nothing is rescaled and nothing is averaged —
`tests/test_boundaries.py` enforces that.

Bands are `drop | review | auto_confirm`. An infringement's band is the
roll-up of its attestations: any disagreement resolves to `review`, and
agreement never promotes. `attestations.calibration_version` records which
config produced each band, so a retune leaves history interpretable.

Until a provider is calibrated its results go into `review` only. See
`CLAUDE.md` §7.3 for the two keys, and
`docs/superpowers/specs/2026-08-07-step-7-calibration-banding-design.md` for
the full design.
```

Also delete the `calibrate(rawScore: number): number` line from the provider interface sketch above it, and change `search_attestations.calibrated_score` to `band TEXT` + `calibration_version TEXT` so the document matches the schema that shipped.

- [ ] **Step 7: Update `INVARIANTS.md`**

Give the uncalibrated-provider invariant its enforcement point, in the style the other entries use — name the mechanism (`bands.py` rule 2, `check_floor`'s six conditions, `trust` as sole writer of `providers.calibrated`) and the tests that pin it.

- [ ] **Step 8: Full verification**

```bash
python -m pytest -q && python -m mypy && python -m mypy devtools/calibrate && python -m ruff check .
```

Every test passes, nothing skipped. Confirm no DB test skipped by checking the summary line for `skipped` — if any did, Postgres was down and the run proves nothing.

- [ ] **Step 9: Confirm the shipped state matches what the spec promised**

```bash
python -m pytest -q -k "empty_policy or uncalibrated" -v
```

Then against a locally migrated database:

```sql
SELECT count(*) FROM calibration_configs;          -- expect 0
SELECT provider_id, calibrated FROM providers;     -- expect both false
SELECT DISTINCT band FROM attestations;            -- expect {review} or empty
```

If any of those disagree, the repo is not in the state §2 of the spec describes and the discrepancy must be reported, not tidied away.

- [ ] **Step 10: Commit**

```bash
git add tests/test_boundaries.py SCHEMA.md CLAUDE.md NEAR-TERM-BUILD.md INVARIANTS.md
git commit -F - <<'EOF'
Step 7: cross-provider averaging tripwire, and the docs

A permanent grep for mean|average|avg over src/imageshield/search/ and
src/imageshield/calibration/, with no allowlist. Calibration is exactly where
the temptation lives - "just normalise onto a common scale" is the
obvious-looking move, and it produces numbers with no meaning that look
entirely plausible. Verified by adding an average, watching it fail, and
removing it; a tripwire nobody has seen fire is not known to work.

NEAR-TERM-BUILD.md §2.3 proposed that exact mistake: derive each provider's
mapping onto the common scale, with a calibrated_score column and a
calibrate(rawScore): number interface method. Corrected, because leaving it
there means the next person to read the build spec builds the thing the test
now blocks.

CLAUDE.md §7.3 gains the mechanism now that it exists. SCHEMA.md gains the
four tables. INVARIANTS.md gains the enforcement point for the
uncalibrated-provider rule.

Step 7 is complete and the system is not calibrated. calibration_configs is
empty, both providers are calibrated = false, and every band is 'review' for
two independent reasons. What shipped is the harness and the engine. The
first real sweep needs a consented labelled set with lookalike hard
negatives, which does not exist yet and is the actual critical path to any
band moving.

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>
EOF
```

---

## Done when

Working through the spec's §7 done-when list, each mapped to where it is proven:

| Requirement | Proven by |
|---|---|
| `sweep` reports precision/recall/NPV with sample size, over a set containing lookalikes | `test_calibration_report.py::test_no_figure_appears_without_its_sample_size`, `test_calibration_metrics.py` |
| An eval item without `consent_basis` is rejected at insert | `test_migrations.py::test_0007_eval_item_without_consent_basis_is_rejected`, `test_calibration_eval_store.py` |
| Hive bands computed against `score_domain` 0.5–1.0, with a fixture where it changes the outcome | `test_calibration_bands.py::test_below_hive_floor_is_review_not_drop` |
| Google bands from category lookup, `provider_score IS NULL` throughout | `test_calibration_bands.py::test_categorical_never_touches_provider_score` |
| `calibrated = false` cannot produce any band except `review` | `test_calibration_bands.py::test_uncalibrated_provider_produces_review_for_every_input`, `test_calibration_write_path.py::test_uncalibrated_provider_cannot_auto_confirm` |
| `drop` + `auto_confirm` yields `review` with `band_reason` recording it | `test_calibration_rollup.py`, `test_calibration_write_path.py::test_disagreement_across_providers_resolves_to_review` |
| Two providers at `review` yields `review`, not a promotion | `test_calibration_rollup.py::test_two_providers_at_review_is_not_a_promotion` |
| `replay` reports a delta without writing, verified by row count and checksum | `test_calibrate_replay.py::test_replay_writes_nothing` |
| `activate` requires `--confirm`, records `activated_by`, stamps `calibration_version` | `test_calibrate_activate.py` (three tests) |
| Exactly one active config per provider | `test_migrations.py::test_0007_only_one_active_config_per_provider`, `test_calibrate_activate.py::test_activating_a_second_config_deactivates_the_first` |
| No code path averages or compares scores across providers | `test_boundaries.py::test_no_cross_provider_averaging_in_scoring_code` — a test, not a hand review |

**Stop at the end of Task 8.** Cost tracking, budgets, circuit breakers, and kill switches are step 8.
