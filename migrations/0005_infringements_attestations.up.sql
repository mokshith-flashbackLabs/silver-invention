-- Step 6 (expand half): separate the thing found from the observation of it.
--
-- search_matches is keyed UNIQUE (run_id, url_hash, provider_id) — per RUN —
-- so a weekly rescan finding the same unchanged URL writes a new row every
-- week: 100k users x 20 matches x 2 providers x 52 weeks is ~208M rows/year,
-- growing with TIME. The same failure class as the old system's
-- matches[].seenInScans, which appends one date string per scan forever
-- against DynamoDB's 400 KB item cap with the write error swallowed
-- (weeklyInfringementScanner.js:1016).
--
-- Split into a stable infringement plus per-provider attestations and a
-- rescan becomes an UPDATE: ~4M rows TOTAL, growing with CONTENT.
--
-- search_matches itself is dropped by 0006, after the code stops writing to
-- it — expand here, contract there, so no commit is ever broken.

-- Stable. One row per (user_ref, url_hash). The thing the user acts on.
CREATE TABLE infringements (
  infringement_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_ref         UUID NOT NULL,
  url_hash         TEXT NOT NULL REFERENCES content_urls(url_hash),
  page_url         TEXT NOT NULL,
  image_url        TEXT,
  -- The dedup key is the PAGE when a provider reports one (Hive backlinks),
  -- because the page is what a user acts on — a takedown notice, a lawyer, a
  -- report. With no backlink we fall back to the image URL and record which
  -- was used here, so a later audit can tell the two populations apart.
  keyed_on         TEXT NOT NULL DEFAULT 'page_url'
                   CHECK (keyed_on IN ('page_url', 'image_url')),
  first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  seen_count       INT NOT NULL DEFAULT 1,
  -- Set by the recheck loop, which is NOT in v1. Nothing writes false yet.
  url_alive        BOOLEAN NOT NULL DEFAULT true,
  last_checked_at  TIMESTAMPTZ,
  -- Calibration and banding are step 7; everything lands in 'review' until
  -- then, because an uncalibrated provider must not be able to tell someone
  -- their face is in porn without a human looking first (CLAUDE.md §7.3).
  band             TEXT NOT NULL DEFAULT 'review',
  status           TEXT NOT NULL DEFAULT 'new',
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- Cross-user is NOT dedup. The same URL found for two users is two
  -- infringements; collapsing across users is the boundary that leaks one
  -- person's matches to another.
  UNIQUE (user_ref, url_hash)
);

CREATE INDEX infringements_user_idx ON infringements (user_ref, last_seen_at DESC);
CREATE INDEX infringements_review_idx ON infringements (band) WHERE band = 'review';

-- One row per (infringement, provider). UPDATED on rescan, never appended.
-- The same URL found by three providers is one infringement with three
-- attestations, and provider_count is a genuine agreement signal.
CREATE TABLE attestations (
  attestation_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  infringement_id    UUID NOT NULL REFERENCES infringements(infringement_id) ON DELETE CASCADE,
  provider_id        TEXT NOT NULL REFERENCES providers(provider_id),
  score_kind         TEXT NOT NULL,
  provider_score     NUMERIC(6,4),
  provider_category  TEXT,
  query_quality      TEXT,
  score_version      TEXT NOT NULL,
  first_confirmed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_confirmed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  confirm_count      INT NOT NULL DEFAULT 1,
  last_run_id        UUID REFERENCES search_runs(run_id),
  UNIQUE (infringement_id, provider_id),
  CONSTRAINT attestation_score_shape CHECK (
    (score_kind = 'numeric'     AND provider_score    IS NOT NULL) OR
    (score_kind = 'categorical' AND provider_category IS NOT NULL)
  )
);

CREATE INDEX attestations_infringement_idx ON attestations (infringement_id);
-- complete_run counts a run's attestations; without this that is a seq scan
-- over every attestation ever written.
CREATE INDEX attestations_run_idx ON attestations (last_run_id);

-- Versioned, because changing normalisation invalidates every stored hash.
-- canonical_url is stored alongside: debugging a dedup failure without it
-- means re-deriving the normalisation by hand.
ALTER TABLE content_urls
  ADD COLUMN normalisation_version TEXT NOT NULL DEFAULT 'v1',
  ADD COLUMN canonical_url TEXT;

-- Rows predating this migration were hashed by the step-5 INTERIM hash
-- (sha256 of the UNNORMALISED url). Label them so v1 hashes never silently
-- mix with them; canonical_url = the raw url is the honest value for them.
UPDATE content_urls
SET normalisation_version = 'v0-interim', canonical_url = url;

-- The retention job nulls raw_response past RAW_RESPONSE_RETENTION_DAYS
-- while keeping the metadata row. 0001 declared the column NOT NULL.
ALTER TABLE provider_calls ALTER COLUMN raw_response DROP NOT NULL;

-- ── Migrate existing search_matches rows ──────────────────────────────────
-- Dev and E2E data only; this repo has never deployed. Grouping follows the
-- new dedup key as closely as the old shape allows. The url_hash is carried
-- across untouched — it stays whatever it was, which is why the rows above
-- are labelled v0-interim rather than rehashed.

WITH grouped AS (
  SELECT user_ref, url_hash,
         min(created_at) AS first_at, max(created_at) AS last_at,
         count(*) AS n
  FROM search_matches
  GROUP BY user_ref, url_hash
), first_rows AS (
  SELECT DISTINCT ON (user_ref, url_hash)
         user_ref, url_hash, page_url, image_url
  FROM search_matches
  ORDER BY user_ref, url_hash, created_at
)
INSERT INTO infringements (user_ref, url_hash, page_url, image_url, keyed_on,
                           first_seen_at, last_seen_at, seen_count)
SELECT g.user_ref, g.url_hash,
       COALESCE(f.page_url, f.image_url), f.image_url,
       CASE WHEN f.page_url IS NULL THEN 'image_url' ELSE 'page_url' END,
       g.first_at, g.last_at, g.n
FROM grouped g
JOIN first_rows f USING (user_ref, url_hash);

WITH latest AS (
  SELECT DISTINCT ON (user_ref, url_hash, provider_id)
         user_ref, url_hash, provider_id, score_kind, provider_score,
         provider_category, query_quality, score_version, run_id
  FROM search_matches
  ORDER BY user_ref, url_hash, provider_id, created_at DESC
), counts AS (
  SELECT user_ref, url_hash, provider_id,
         count(*) AS n, min(created_at) AS first_at, max(created_at) AS last_at
  FROM search_matches
  GROUP BY user_ref, url_hash, provider_id
)
INSERT INTO attestations (infringement_id, provider_id, score_kind,
                          provider_score, provider_category, query_quality,
                          score_version, first_confirmed_at, last_confirmed_at,
                          confirm_count, last_run_id)
SELECT i.infringement_id, l.provider_id, l.score_kind,
       l.provider_score, l.provider_category, l.query_quality,
       l.score_version, c.first_at, c.last_at, c.n, l.run_id
FROM latest l
JOIN counts c USING (user_ref, url_hash, provider_id)
JOIN infringements i
  ON i.user_ref = l.user_ref AND i.url_hash = l.url_hash;
