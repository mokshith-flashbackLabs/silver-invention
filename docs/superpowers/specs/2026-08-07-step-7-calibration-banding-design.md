# Step 7 — Calibration harness and banding

**Date:** 2026-08-07
**Build step:** 7 of 9 (`CLAUDE.md` §8, canonical numbering)
**Status:** design approved, not implemented

---

## 1. What this step is

Every `infringements.band` is currently the hardcoded literal `'review'`
([`store.py:72`](../../../src/imageshield/search/store.py#L72),
[`store.py:129`](../../../src/imageshield/search/store.py#L129)). This step is what allows a band
to be anything else.

Banding is a safety decision wearing an ML costume. A false positive here tells someone their face
is in pornography when it isn't, which is a psychological injury, not a UX annoyance. A false
negative is a broken promise. No threshold avoids both. This step chooses, explicitly and with
measurements, which error we would rather make — and, because no labelled data exists yet, its
honest outcome is that we do not yet get to choose. See §9.

## 2. Outcome of this step, stated plainly

No labelled evaluation set exists as of 2026-08-07. Therefore:

- `calibration_configs` ships **empty**. No provider has an active config.
- Both providers stay `calibrated = false`.
- Every band stays `review`, for two independent and individually sufficient reasons: rule 1
  (`no_active_config`) and rule 2 (`provider_uncalibrated`) in §5.1.
- **This step produces no measured precision or NPV figure**, because there is nothing to measure.

What is delivered is the harness, the engine, and the schema that make a calibration possible and
make an unsound one impossible to activate. "Step 7 complete" must not be read as "the system is
tuned". The commit message says so too.

## 3. Corrections to the step-7 brief

| Brief says | Reality | Resolution |
|---|---|---|
| "Migration 0004" | 0004–0006 exist (`provider_score_shape`, `infringements_attestations`, `drop_search_matches`) | Migration is **0007** |
| `grep src/providers/` | No such directory | Target `src/imageshield/search/` and `src/imageshield/calibration/`, as a permanent test not a hand review |
| `eval_items` holds label only, no score | Sweep needs a provider score per item | New `eval_observations` table (§4.3), populated by the **production adapter** |
| `derived_edit` listed under TRUE NEGATIVES | An AI edit of the subject's own photo is their likeness abused — the flagship case | `derived_edit` → `label = true_match`, enforced by CHECK (§4.2) |
| Taxonomy has four `label_kind`s | A diffusion-generated image of the subject derives from no photo we hold | Added `novel_generation` → `label = true_match` (§4.2) |

`NEAR-TERM-BUILD.md` §2.3 also describes calibration as deriving "each provider's mapping onto the
common scale" with a `calibrated_score` column. That is exactly the cross-provider comparison
`CLAUDE.md` §7.2 forbids. This design does not build it, and corrects that document in the same PR.

## 4. Migration 0007 — `0007_calibration_and_eval`

### 4.1 `calibration_configs`

```sql
CREATE TABLE calibration_configs (
  config_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  provider_id      TEXT NOT NULL REFERENCES providers(provider_id),
  version          TEXT NOT NULL,
  score_kind       TEXT NOT NULL CHECK (score_kind IN ('numeric','categorical')),
  bands            JSONB NOT NULL,
  -- numeric:     [{"band":"drop","max":0.72},
  --               {"band":"review","min":0.72,"max":0.94},
  --               {"band":"auto_confirm","min":0.94}]
  --   expressed in the provider's NATIVE units and validated against
  --   providers.score_domain at propose time (§5.1).
  -- categorical: {"full_match":"auto_confirm","partial_match":"review",
  --               "page_match":"review"}
  eval_set_id      TEXT,
  eval_sample_size INT,
  measured         JSONB,     -- advisory only; activate recomputes (below)
  active           BOOLEAN NOT NULL DEFAULT false,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  activated_at     TIMESTAMPTZ,
  activated_by     TEXT,
  UNIQUE (provider_id, version)
);
```

`measured JSONB` is retained but **demoted to an advisory record of what the proposer saw**.
`activate` never reads it. If a machine check trusts a JSONB column an operator can type into, the
check is defeated by editing a number; the data is in `eval_observations` and is recomputed from
there (§6.5).

`CREATE UNIQUE INDEX calibration_one_active ON calibration_configs (provider_id) WHERE active`
enforces exactly one active config per provider.

### 4.2 `eval_items`

```sql
CREATE TABLE eval_items (
  item_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  eval_set_id    TEXT NOT NULL,
  seed_uri       TEXT NOT NULL,
  candidate_url  TEXT NOT NULL,
  label          TEXT NOT NULL
                 CHECK (label IN ('true_match','false_match','uncertain')),
  label_kind     TEXT NOT NULL
                 CHECK (label_kind IN ('same_person','derived_edit','novel_generation',
                                       'lookalike','unrelated')),
  consent_basis  TEXT NOT NULL CHECK (btrim(consent_basis) <> ''),
  labelled_by    TEXT NOT NULL,
  labelled_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT eval_label_kind_agrees CHECK (
    (label_kind IN ('same_person','derived_edit','novel_generation')
       AND label IN ('true_match','uncertain'))
    OR
    (label_kind IN ('lookalike','unrelated')
       AND label IN ('false_match','uncertain'))
  ),
  UNIQUE (eval_set_id, seed_uri, candidate_url)
);
```

**The taxonomy.** `label` answers "is this the user's likeness, and should they be told?" — not "is
this an authentic photograph of them".

| `label_kind` | `label` | Why |
|---|---|---|
| `same_person` | `true_match` | Authentic photo, reposted |
| `derived_edit` | `true_match` | Their face, altered (nudify-style). The flagship case. |
| `novel_generation` | `true_match` | Diffusion-generated from no photo we hold. Recall here will be near zero and that is the point of measuring it. |
| `lookalike` | `false_match` | Different person, similar face — **the hard negative that determines precision** |
| `unrelated` | `false_match` | Different person |

`eval_label_kind_agrees` makes the inversion unrepresentable. A `derived_edit` row labelled
`false_match` would tune thresholds to suppress precisely what the product exists to catch, *and
would make the precision figure look better for doing so*. `uncertain` remains available for any
kind, because a labeller genuinely unable to tell must have somewhere to put that.

`consent_basis TEXT NOT NULL` alone permits `''`. The `btrim` CHECK is what actually rejects an item
with no consent basis at insert. Sourcing is consenting participants, public-domain, or synthetic
imagery only — never real victim content, never scraped material. `novel_generation` items are
sourced as **non-sexual** synthetic portraits: the retrieval question is "can image search find a
novel generation of this face", and the answer does not depend on what the image depicts.

### 4.3 `eval_observations`

```sql
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
```

Mirrors `attestations`: one item, many providers, re-observation updates rather than appends. Eval
data stays entirely out of `infringements`/`attestations`, so nothing that serves real users carries
test rows and `activate`'s re-band pass never touches eval data.

**Populated by the production adapter, not a reimplementation** (§6.1). This is the constraint that
makes the measurement mean anything: a parallel copy of the response-parsing would measure the copy.

### 4.4 `eval_seed_coverage`

Not in the brief. Necessary.

```sql
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
```

Recall depends on counting the `true_match` items a provider **failed to return**. An absent
`eval_observation` is ambiguous on its own — it means either "the provider was asked and didn't
return it" (a miss, and data) or "the provider was never asked" (not data). Only coverage
distinguishes them.

This is also what makes the activate floor's *"observations don't cover every item"* condition
checkable. Read literally against `eval_observations` it would reject every honest set, since an
honest set contains misses. Read against coverage it becomes: every distinct `seed_uri` in the set
has a `status = 'ok'` row for this provider.

### 4.5 ALTERs

```sql
ALTER TABLE attestations
  ADD COLUMN band TEXT NOT NULL DEFAULT 'review'
    CHECK (band IN ('drop','review','auto_confirm')),
  ADD COLUMN calibration_version TEXT;

ALTER TABLE infringements
  ADD COLUMN band_reason TEXT,
  ADD CONSTRAINT infringements_band_valid
    CHECK (band IN ('drop','review','auto_confirm'));
```

`attestations.calibration_version` records which config produced a band; without it a retune makes
every historical band uninterpretable. `infringements.band` has carried no CHECK since 0005; adding
one now, while `'review'` is the only value in existence, is free.

Schema lint (invariant #9) passes untouched: no `bytea` anywhere, and `seed_uri` is a `*_uri` under
9(c).

### 4.6 Config

One new knob in `src/imageshield/config.py`:

```python
calibration_min_eval_items: int = 200
```

Defaulted rather than required — it is a policy floor, not a secret or an environment difference.

## 5. The banding engine

### 5.1 Per attestation — `band_for_attestation`

Rules in order, first match wins. Every failure mode lands in `review`; nothing raises.

| # | Condition | Band | `band_reason` |
|---|---|---|---|
| 1 | No active config for the provider | `review` | `no_active_config` |
| 2 | `providers.calibrated = false` | `review` | `provider_uncalibrated` |
| 3 | Config `score_kind` ≠ attestation `score_kind` | `review` | `score_kind_mismatch` |
| 4 | Numeric, value outside `providers.score_domain` | `review` | `score_out_of_domain` |
| 5 | Numeric, in domain | range lookup | `numeric:{band}` |
| 6 | Categorical, category present in `bands` | dict lookup | `categorical:{band}` |
| 7 | Categorical, unknown category | `review` | `unknown_category` |

Rule 2 blocks `drop` as well as `auto_confirm`. §7.3 says an uncalibrated provider reaches "review
band only", and `drop` is the more dangerous edge of the two: a real infringement landing there is
invisible forever and the user never learns.

**Score domain.** Bands are authored in the provider's **native units** and nothing is ever
rescaled. Hive Web Search reports 0.5–1.0 where 0.5 is the floor, recorded in
`providers.score_domain` by migration 0004 — which until now no code has read. A value of `0.4` is
*impossible* for Hive: a malformed response, or a key provisioned against the wrong Hive project.
Read against an assumed 0–1 scale it is merely a low score and bands to `drop`, silently discarded.
Read against `score_domain` it bands to `review`. That is the fixture where the distinction changes
the outcome.

**Boundary convention:** `min` inclusive, `max` exclusive; the top band's `max` is the domain
maximum, inclusive. Stated explicitly because `0.94` exactly is the difference between `review` and
`auto_confirm`.

`propose` rejects a `bands` JSON whose boundaries fall outside `score_domain`, or that leaves a gap
or an overlap within it.

### 5.2 Roll-up — `roll_up(bands) -> (Band, reason)`

Total function. Ordering `drop < review < auto_confirm`.

```
no attestations   -> review, "no_attestations"          (unreachable; still defined)
all agree on X    -> X,      "unanimous:{X}(n=k)"
any disagreement  -> review, "disagreement:{lowest}|{highest}->review"
```

Any spread at all resolves to `review` — `drop` + `auto_confirm`, and equally `review` +
`auto_confirm`. Providers disagreeing is evidence of uncertainty, and uncertainty means a human
looks.

Unanimity returns the shared band unchanged. Two providers at `review` stay `review`: concurrence
between two image-search providers indexing overlapping corpora is not two independent observations.
Two at `auto_confirm` stay `auto_confirm`, because each alone already was — agreement adds nothing
but takes nothing away.

Evidence moves a band down easily and up reluctantly. That asymmetry is deliberate.

**No arithmetic in this module.** No mean, no average, no max-of-scores — only max-of-ordinal-band.
Scores from different providers are different quantities on different scales, and an average of them
is a meaningless number that looks entirely plausible.

### 5.3 Write path

The worker loads a `BandingPolicy` snapshot once per run (`provider_id →` active config + calibrated
flag) and passes it into `record_infringements`. Per key, inside the existing transaction:

```
upsert content_urls
upsert infringement                       -> infringement_id
band = band_for_attestation(policy[provider], match)
upsert attestation  (band, calibration_version)
all  = SELECT band FROM attestations WHERE infringement_id = ?
b, reason = roll_up(all)
UPDATE infringements SET band = b, band_reason = reason
```

Rolling up on every attestation write means there is never a moment where a stored infringement band
disagrees with its own attestations. At ~20 infringements × 2 providers per run the extra read costs
nothing. The same `roll_up()` serves `activate`'s re-band pass, so there is one implementation of the
rule.

## 6. The harness — `devtools/calibrate/`

### 6.0 Module layout

```
src/imageshield/calibration/
  models.py    Band, BandDecision, CalibrationConfig, BandingPolicy, eval row models
  bands.py     PURE. band_for_attestation(), roll_up(). No DB, no I/O, no clock.
  metrics.py   PURE. Metric, confusion(), sweep_numeric(), sweep_categorical().
  store.py     Postgres. load_active_policy(), propose(), activate(), trust(), reband()
devtools/calibrate/__main__.py    argparse: observe | sweep | propose | replay | activate | trust
```

The engine cannot live in `devtools/` — the production write path calls it. `bands.py` and
`metrics.py` are pure functions over plain values, which is what lets the safety-critical logic be
tested exhaustively without a database.

One shared-code change: `_fan_out` and `_InfringementKey` in
[`search/store.py`](../../../src/imageshield/search/store.py#L184) become public `fan_out` /
`InfringementKey`, so `observe` maps provider responses to candidate URLs through exactly the code
the production path uses.

### 6.1 `observe --provider <id> --eval-set <id> --confirm`

Groups `eval_items` by `seed_uri`; calls the real adapter through the `SearchProvider` protocol once
per seed; runs each result through the shared `fan_out` + `url_hash`; upserts an `eval_observation`
for every item whose `url_hash(candidate_url)` matches a returned key; writes an `eval_seed_coverage`
row per seed carrying the adapter's status and candidate count.

Spends real provider money. Prints the call count and requires `--confirm`.

### 6.2 `sweep --provider <id> --eval-set <id>` — writes nothing

**Every figure is a `Metric`, never a bare float:**

```python
class Metric(BaseModel):
    value: float | None      # None when the denominator is 0 — never 0.0
    numerator: int
    denominator: int
    wilson_lower_95: float | None
```

Rendered `precision 1.000 (40/40, 95% lower bound 0.912)`. The Wilson lower bound is a few lines of
arithmetic and it is the honest reading of a small set: 40-for-40 is not evidence of 0.99. It is
**displayed**, not gated on; the gate remains the point estimate plus the `n ≥ 200` floor (§6.5).
Gating on the lower bound instead is a possible future tightening, deliberately not taken now.

**Confusion counting.** Predicted-positive at threshold `t` means *an observation exists AND
score ≥ t*. An item with no observation is predicted negative — that is how a `true_match` the
provider never returned becomes an FN rather than silently vanishing from the denominator.
`uncertain` items are excluded from all four cells and reported as a separate count.

```
TP = predicted positive & true_match      FP = predicted positive & false_match
FN = predicted negative & true_match      TN = predicted negative & false_match

precision = TP/(TP+FP)   recall = TP/(TP+FN)   NPV = TN/(TN+FN)
```

**Every sweep opens with set composition, before any metric:**

```
eval set v1 / hive        items 247   observations 191   uncertain 12 (excluded)
  same_person       94    derived_edit   38    novel_generation 15
  lookalike         61    unrelated      39
  seed coverage     28/28 seeds status=ok
```

If `lookalike == 0`, a prominent line stating the set cannot produce a meaningful precision figure.
Random negatives are easy for any provider to reject and will make a bad threshold look excellent;
`lookalike` is the category that produces real-world false positives.

**Recall is broken out by `label_kind`.** A nudify edit preserves background, body, and composition,
so image search plausibly finds it; a novel generation shares no pixels with anything and recall
there will be near zero. Reporting `recall 0.80 overall / 0.35 derived_edit / 0.04
novel_generation` makes the product's real coverage gap a number in every report rather than a
caveat in a document nobody reads.

**Recommendation.** Numeric: candidate boundaries are the observed score values plus the domain
endpoints. Recommend the *lowest* `a` with `precision(≥a) ≥ 0.99` (maximises recall under the floor)
and the *highest* `d` with `NPV(<d) ≥ 0.99`. Refuse to recommend if either does not exist, or if
`a ≤ d` (the bands would overlap). Categorical: report per-category precision and count, then assign
`auto_confirm` to categories with precision ≥ 0.99, `drop` where the union of drop-assigned
categories keeps NPV ≥ 0.99, and `review` otherwise. Greedy and deterministic, documented as such.

If the set cannot demonstrate 0.99 on either edge, the correct outcome is that the provider stays
uncalibrated and everything stays `review`. `sweep` says that plainly rather than loosening the
target to produce a result.

### 6.3 `propose --provider <id> --eval-set <id> --version <v> [--bands <json>]`

Writes one **inactive** `calibration_configs` row with `measured` attached and `eval_sample_size`
recorded. Validates the `bands` JSON against `score_domain` (§5.1). `--bands` defaults to the
recommendation from the sweep.

### 6.4 `replay --config <id>` — read-only

Recomputes every attestation and infringement band **in memory** under the candidate config and
reports the delta: attestations changed by direction, infringements changed by direction, and the
distinct `user_ref` count affected.

This command is the difference between "we tightened the threshold" and "we tightened the threshold
and 340 users will lose an alert they have already seen." Read-only is verified by a row count and
an `md5(string_agg(...))` checksum over the mutable columns, asserted identical before and after.

### 6.5 `activate --config <id> --confirm --by <name>`

**Two independent gates protect the move off `review`, because they defend different failures and
each is blind to the other's.**

The floor in code defends against deadline pressure — loosening the bar becomes a code change with a
review and a `git blame`. The human key defends against a bad eval set producing good-looking
numbers, which is the likelier failure: a sweep over 40 items with no lookalikes yields precision
1.0 trivially, because random negatives are easy to reject. The arithmetic passed; the measurement
was meaningless. No code can judge whether an eval set resembles the real world.

**The floor.** Computed **fresh from `eval_observations ⋈ eval_items`** — never from the stored
`measured` JSONB. Refuse if:

1. The config declares an `auto_confirm` band and its recomputed precision < 0.99
2. The config declares a `drop` band and its recomputed NPV < 0.99
3. Effective sample size < `CALIBRATION_MIN_EVAL_ITEMS` (default 200), where **effective sample
   size** is the count of `eval_items` in the set whose `label != 'uncertain'` — the items that
   actually enter the arithmetic, not the raw row count
4. The eval set contains **zero** `label_kind = 'lookalike'` items
5. `eval_set_id` is null
6. Any distinct `seed_uri` in the set lacks a `status = 'ok'` `eval_seed_coverage` row for this
   provider

Conditions 1 and 2 are each skipped when the config does not declare that band — a config with only
`drop` and `review` is not held to an `auto_confirm` precision it never claims. Conditions 3–6 apply
to every non-review-only config regardless of which edge bands it declares, because they are about
whether the *set* can support any measurement at all.

Condition 4 is the one that closes the failure above: a set without hard negatives cannot produce a
meaningful precision figure, so it must not activate a non-review band regardless of what the
arithmetic says.

**A review-only config always activates.** No floor applies — it alarms nobody.

On success, in one transaction: flip `active` (the partial unique index deactivates the previous
one), stamp `activated_at` / `activated_by`, re-band every attestation for that provider and roll up
every affected infringement stamping `calibration_version`, and write an `audit_log` row.

**`activate` never touches `providers.calibrated`.**

### 6.6 `trust --provider <id> --confirm --by <name> --reason <text> [--revoke]`

The second key, and the only thing in the system that writes `providers.calibrated`. `--confirm`,
`--by`, and a mandatory `--reason` all land in `audit_log`
([0001:154](../../../migrations/0001_initial_schema.up.sql#L154), append-only, INSERT-only grant).

*This config is sound* and *this provider may now alarm people without a human looking* are
different claims. The first is arithmetic; the second is judgement.

`--revoke` sets `calibrated = false` and re-bands everything back to `review`. A safety flag you
cannot withdraw is not one.

The two-key cost is paid **once per provider**: after `calibrated = true`, subsequent config
activations for that provider still face the floor, but not the human key again.

## 7. Tests

Pure-function tests need no database; only store and CLI tests take `throwaway_db`.

| File | Asserts |
|---|---|
| `test_calibration_bands.py` | Hive `0.4` (below the 0.5 floor) → `review`/`score_out_of_domain`, **not** `drop`. Hive `0.60` → `drop` in native units. Google categorical with `provider_score IS NULL` throughout. A `calibrated = false` provider whose active config says `auto_confirm` still yields `review` for every input. Boundary values at exactly `min` and `max`. |
| `test_calibration_rollup.py` | `drop` + `auto_confirm` → `review`, `band_reason` naming the spread. `review` + `review` → `review`, no promotion. `auto_confirm` + `auto_confirm` → `auto_confirm`. Single attestation. Empty. |
| `test_calibration_metrics.py` | Every emitted figure carries numerator and denominator. A missing observation counts as FN, not as absent. `uncertain` excluded and separately counted. Recall split by `label_kind`. Zero-lookalike set emits the warning. Zero denominator yields `None`, never `0.0`. |
| `test_calibration_eval_store.py` | `consent_basis` `''` and `'   '` rejected at insert. `derived_edit` + `false_match` rejected. Full label/label_kind matrix, both directions. |
| `test_calibration_activate.py` | Each of the six refusal conditions independently. `--confirm` required. `activated_by` recorded. `calibration_version` stamped on every re-banded row. A second active config for one provider violates the partial unique index. `activate` leaves `providers.calibrated` untouched. `trust` is the only writer of it. |
| `test_calibration_replay.py` | Row count and `md5(string_agg(...))` checksum identical before and after. |
| `test_calibration_write_path.py` | `record_infringements` under an empty policy still writes `review`. Under a policy with an active config, attestation `band` and `calibration_version` are written and the infringement rolls up in the same transaction. |
| `test_migrations.py` | 0007 up/down round trip. |
| `test_boundaries.py` | New permanent grep: `mean\|average\|avg` over `src/imageshield/search/` and `src/imageshield/calibration/`. No allowlist — if a legitimate `average` ever needs to exist, adding it should cost a code review. |

## 8. Docs updated in the same PR (§10)

- `SCHEMA.md` — the four new tables and three ALTERs
- `CLAUDE.md` — §8 marks step 7 done; §7.3 gains the two-key mechanic
- `NEAR-TERM-BUILD.md` — §2.3 corrected: calibration does **not** map providers onto a common scale
- `INVARIANTS.md` — the uncalibrated-provider rule gains its enforcement point

## 9. Explicitly not in this step

- Cost tracking, per-provider budgets, circuit breakers, kill switches — **step 8**
- The review queue and reviewer tooling that consumes the `review` band — not in v1 scope
  (`CLAUDE.md` §6)
- Any cross-provider score comparison, averaging, or common scale — forbidden, §7.2
- Producing an actual calibration for Hive or Google — impossible without labelled data (§2)
