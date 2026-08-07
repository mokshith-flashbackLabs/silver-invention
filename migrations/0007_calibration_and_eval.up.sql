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
  -- NOT NULL alone permits ''. The regex is what actually rejects an item
  -- with no traceable consent basis. Consenting participants, public-domain,
  -- or synthetic only -- never real victim content, never scraped material.
  --
  -- '\S' (any non-whitespace character), not an enumerated trim charset:
  -- Postgres's single-argument btrim() strips only the space character, so a
  -- tab/newline-only value like E'\t\n' passed bare
  -- btrim(consent_basis) <> ''. Enumerating " \t\n\r" fixed that case but is
  -- still a list, not the rule -- it still admitted a lone vertical tab
  -- (chr(11)) or form feed (chr(12)). '\S' expresses "has non-whitespace
  -- content" directly. NOTE: it does not close every gap -- U+00A0
  -- (non-breaking space, chr(160)) matches '\S' in this database's
  -- collation, i.e. is NOT treated as whitespace by Postgres's regex engine,
  -- so a consent_basis of a lone NBSP still passes. See task-1-report.md for
  -- the verification and the decision to document rather than patch it.
  consent_basis  TEXT NOT NULL CHECK (consent_basis ~ '\S'),
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
