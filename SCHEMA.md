# SCHEMA.md — ImageShield services, v1

Postgres 16. **One schema per module, distinct DB role per module.** No cross-service foreign keys —
`user_ref` crosses boundaries as an opaque UUID, and referential integrity across that boundary is
the proxy's responsibility, not the database's.

**`user_ref` is the only user identifier in this repo.** It is a UUID minted by the proxy. Phone
numbers never cross into these schemas — not as a column, not in a log line, not in an
`ExternalImageId` (CLAUDE.md §3.2). The old system used a phone number as a primary key in seven
tables under four different spellings; this is the change that fixes it.

---

## Conventions

| Rule | Reason |
|---|---|
| All PKs are `UUID DEFAULT gen_random_uuid()` | No guessable identifiers in URLs, S3 paths, or logs |
| All timestamps are `TIMESTAMPTZ`, never `TIMESTAMP` | The current system has no timezone discipline |
| Phone numbers are `TEXT` in **+E.164 only**, enforced by `CHECK` | Kills the bare/`+` normalisation matrix in `api.js` |
| Soft delete via `status` + `superseded_by`, never `DELETE` | Biometric enrolments are expensive to recreate |
| Every derived row carries its provenance | You must be able to trace a hit back to the search that produced it |
| `model_id` on every row that holds or references a vector | Cross-model similarity is meaningless — see INVARIANTS #4 |

---

## 1. Identity service

The only service holding Article 9 data. Separate database, separate credentials, narrowest API
surface in the system.

### `users` is NOT ours — it lives in the proxy

The proxy (`ImageShieldPhotoShare`) owns authentication, OTP, sessions, phone numbers, and the user
record. **This repo never holds a phone number** (CLAUDE.md §3.2).

Every table below keys on `user_ref UUID` — an opaque identifier minted by the proxy and passed on
every request. There is **no foreign key** to a users table, because that table is in a different
database owned by a different service. Referential integrity across that boundary is the proxy's
responsibility.

For reference only, the proxy's side of the contract:

```sql
-- IN THE PROXY'S DATABASE. Do not create this here.
-- The proxy adds user_ref to its existing user record and backfills it;
-- user_ref becomes the canonical user_id if the proxy is later rewritten.
users (
  user_ref          UUID UNIQUE NOT NULL,   -- what we receive
  phone_e164        TEXT UNIQUE,            -- never crosses to us
  email, first_name, last_name, date_of_birth,
  status, created_at, updated_at
)
```

`date_of_birth` enforces the v1 18+ floor and is checked **by the proxy** before it calls us. It is
self-declared; treat it as an assertion, not a fact. Our own `MIN_ENROLMENT_AGE` check is
defence-in-depth, not the primary gate.

### Consent — **not a table in this repo**

There is no `consent_records` table here, and there will not be one. An earlier draft of this
document specified one; that design is reversed.

**Consent lives in the proxy.** It holds `profile.persons`, `profile.guardianships` with
`subject_dob` triggers, and `profile.v_consent_eligibility` computing `required_signer_role` and
`blocked_reason` — the hard part, already built. It is also the only public ingress, so the DocuSeal
webhook must terminate there. This repo knows a `user_ref` and a face vector; it cannot determine who
is required to sign.

**We hold a reference, not a record.** Three columns on `enrolments` (migration 0010), all
`NOT NULL`:

| Column | Meaning |
|---|---|
| `consent_ref` | The proxy's `consent_id`. Indexed — "which enrolments does this consent cover" is the question asked after a withdrawal |
| `consent_document_sha256` | The hash **the proxy computed** of the exact rendered text. We never compute one, because we never see the document |
| `consent_signed_at` | When it was signed. Rejected at the API if in the future |

`consent_document_sha256` is what makes consent demonstrable. Storing `consentSigned: true` (what the
old system does) proves nothing about *what* was agreed to — and the hash is worth exactly as much as
the proxy's discipline in computing it over the real artifact.

`00000000-0000-0000-0000-000000000000` is reserved: 0010 backfills it onto rows written before
consent was required, so `NOT NULL` could be applied without deleting a biometric enrolment.
`enrolments_consent_not_sentinel` is `NOT VALID` — it grandfathers exactly those rows and enforces on
every write after. The proxy must never issue it.

Enforcement survives the move; the document does not cross the boundary. See `INVARIANTS.md` #2.

### Liveness

```sql
CREATE TYPE liveness_status AS ENUM ('created','pending','passed','failed','expired','abandoned');

CREATE TABLE liveness_sessions (
  session_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_ref            UUID NOT NULL,
  provider            TEXT NOT NULL DEFAULT 'rekognition',
  provider_session_id TEXT UNIQUE,
  status              liveness_status NOT NULL DEFAULT 'created',
  confidence          NUMERIC(5,2),
  failure_reason      TEXT,
  attempt_number      INT NOT NULL DEFAULT 1,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at        TIMESTAMPTZ,
  expires_at          TIMESTAMPTZ NOT NULL
);

CREATE INDEX liveness_user_idx ON liveness_sessions (user_ref, created_at DESC);
```

Rate limit on `attempt_number` — repeated failures are the signal for a presentation attack, not a
bad camera. Lock the user out after 5 attempts in 24h and require support contact.

### Enrolment

```sql
CREATE TYPE enrolment_status AS ENUM ('active','superseded','deleted');

CREATE TABLE enrolments (
  enrolment_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_ref            UUID NOT NULL,
  liveness_session_id UUID NOT NULL REFERENCES liveness_sessions(session_id),
  -- Consent lives in the PROXY. We hold the reference and the hash it computed,
  -- never a FK to a local table and never the document. See "Consent" above.
  consent_ref             UUID NOT NULL,
  consent_document_sha256 TEXT NOT NULL,
  consent_signed_at       TIMESTAMPTZ NOT NULL,
  model_id            TEXT NOT NULL,             -- 'rekognition:identity-v1'
  collection_id       TEXT NOT NULL,             -- Rekognition collection
  external_face_id    TEXT NOT NULL,             -- Rekognition FaceId
  quality_score       NUMERIC(5,2),
  pose_label          TEXT,                      -- 'frontal','left','right','up','down'
  source_object_uri   TEXT NOT NULL,             -- consented first-party selfie; migration path
  status              enrolment_status NOT NULL DEFAULT 'active',
  superseded_by       UUID REFERENCES enrolments(enrolment_id),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at          TIMESTAMPTZ
);

CREATE INDEX enrolments_user_active_idx
  ON enrolments (user_ref, model_id) WHERE status = 'active';
CREATE UNIQUE INDEX enrolments_face_uniq ON enrolments (collection_id, external_face_id);
```

Both FKs are `NOT NULL`. **The schema makes it impossible to enrol a face without a liveness pass and
a signed consent record.** That's the invariant from three messages ago, expressed where it can't be
forgotten.

**Built in v1 (migration 0003):** the "no enrolment without a passed session" half of that invariant
already holds at the DB level. `liveness_sessions` carries `UNIQUE (session_id, status)`, and the v1
`enrolments` table carries `session_status liveness_status NOT NULL DEFAULT 'consumed'` with
`CHECK (session_status = 'consumed')` plus a composite FK `(session_id, session_status) →
liveness_sessions (session_id, status)`. An enrolment row can only reference a session whose current
status is `'consumed'` — and only passed sessions are ever consumed. The FK also pins a consumed
session's status while its enrolment exists. The v2 migration must preserve this mechanism (or an
equivalent constraint) when the `consent_id` FK is added.

`source_object_uri` points at the user's own selfie in your bucket. Keep it — it is the only way to
re-embed the whole userbase when you move off Rekognition without making everyone redo liveness.

---

## 2. Match service

No PII beyond the opaque `user_ref`. There is no `users` table in this repo to join to.

```sql
CREATE TABLE content_items (
  content_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  content_hash   TEXT NOT NULL UNIQUE,      -- perceptual hash from partner
  source_url     TEXT NOT NULL,
  source_domain  TEXT NOT NULL,
  site_id        TEXT,
  partner_ref    TEXT NOT NULL,             -- partner's own id, for reconciliation
  first_seen_at  TIMESTAMPTZ NOT NULL,
  last_seen_at   TIMESTAMPTZ NOT NULL,
  url_alive      BOOLEAN NOT NULL DEFAULT true,
  last_checked_at TIMESTAMPTZ,
  ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX content_domain_idx ON content_items (source_domain);
CREATE INDEX content_ingested_idx ON content_items (ingested_at DESC);

CREATE TABLE content_faces (
  content_face_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  content_id      UUID NOT NULL REFERENCES content_items(content_id) ON DELETE CASCADE,
  bbox            JSONB NOT NULL,            -- {x,y,w,h} normalised 0-1
  model_id        TEXT NOT NULL,
  embedding_ref   TEXT,                      -- pointer into vector store; null if Rekognition-side
  face_index      INT NOT NULL DEFAULT 0,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX content_faces_uniq ON content_faces (content_id, face_index, model_id);
```

**No image bytes anywhere in this schema.** There is no `image_data`, no `thumbnail_uri`, no
`local_path`. If a column like that appears in review, reject it.

`bbox` is what makes face-crop display possible without storage — see the report service.

### Candidates

```sql
CREATE TYPE match_band AS ENUM ('auto_confirm','review','drop');
CREATE TYPE search_direction AS ENUM ('forward','backfill');

CREATE TABLE match_candidates (
  candidate_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_ref        UUID NOT NULL,             -- opaque; no FK, ever
  content_face_id UUID NOT NULL REFERENCES content_faces(content_face_id) ON DELETE CASCADE,
  score           NUMERIC(5,2) NOT NULL,
  model_id        TEXT NOT NULL,
  band            match_band NOT NULL,
  direction       search_direction NOT NULL,
  search_run_id   UUID NOT NULL,             -- provenance
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX candidates_uniq
  ON match_candidates (user_ref, content_face_id, model_id);
CREATE INDEX candidates_band_idx ON match_candidates (band, created_at)
  WHERE band = 'review';

CREATE TABLE search_runs (
  search_run_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  direction      search_direction NOT NULL,
  model_id       TEXT NOT NULL,
  threshold_config JSONB NOT NULL,           -- the exact bands used
  user_ref       UUID,                       -- set for backfill only
  started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at   TIMESTAMPTZ,
  candidates_emitted INT
);
```

`search_runs.threshold_config` is why this table exists. When you retune bands you need to know which
config produced any historical candidate, or every score in the system becomes uninterpretable.

The unique index on `(user_ref, content_face_id, model_id)` is what stops the forward and backfill
loops emitting duplicates for the same pair. Use `ON CONFLICT DO NOTHING`.

---

## 2b. Provider search — **built** (steps 5–6)

Everything above in §2 is the partner-embedding match module: **specified, not built**. This section
is the third-party provider search that exists in the code today, and the two are separate pipelines
that will eventually feed the same report surface.

Tables: `search_seeds`, `search_runs`, `content_urls`, `provider_calls`, `infringements`,
`attestations`, `subjects`, `provider_spend`, `infringement_feedback`. Migrations `0001`, `0004`,
`0005`, `0006`, `0007`, `0008`, `0009`, `0011`, `0012`, `0013`.

### Feedback on a hit, and the one thing it must not do

```sql
CREATE TABLE infringement_feedback (
  feedback_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  infringement_id  UUID NOT NULL REFERENCES infringements(infringement_id) ON DELETE CASCADE,
  user_ref         UUID NOT NULL,
  signal           TEXT NOT NULL CHECK (signal IN ('not_me','confirmed','uncertain')),
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**Append-only.** A user changing their mind writes a second row; the history is the record, and there
is deliberately no `UNIQUE (infringement_id, user_ref)` to force one opinion per person.

`signal` sets `infringements.status`: `not_me` → `dismissed_not_me`, `confirmed` → `acknowledged`,
`uncertain` → unchanged but still recorded.

**`not_me` never adjusts identity vectors, never suppresses a domain, and never feeds banding.** Users
reject *true* positives under distress, and it is common; if rejections retrained the index, the users
most affected by real abuse would systematically degrade their own protection, invisibly. Keep the
signal, do not act on it. Enforced by a test that checksums `enrolments`, `attestations`,
`content_urls`, `subjects` and `search_runs` either side of the write.

### Attribution (migration 0014)

`attribution_runs` + `attributed_faces`, and `search_seeds.attributed_face_id` linking a seed back to
the face that produced it.

`detect_confidence` and `match_score` are **different quantities in different columns**, and the
`CHECK ((resolved_user_ref IS NULL) = (match_score IS NULL))` pairs the second with its person. The
first is "this region is a face"; the second is "this face is that person". Conflating them is how a
confident detection of a *stranger* reads as a confident identification of a *user*.

`resolved_user_ref IS NULL` is first-class and expected — it is most faces in most photos. Every
detected face gets a row and a bbox regardless, because dropping the unattributed ones makes "we saw
three faces and matched one" indistinguishable from "we saw one".

`match_threshold`, `max_candidates` and `model_id` are recorded **on the run**, for the same reason
`search_runs.threshold_config` exists: a later retune otherwise makes every historical attribution
uninterpretable.

Run, faces and seeds commit in **one transaction**. A half-written run — some faces present, some
missing — would be indistinguishable from "those faces matched nobody", and those two must never be
confusable: the first is a normal result, the second is a lost seed.

### The two recheck timestamps

`infringements.last_checked_at` means *we learned something* — only a definite verdict (2xx/3xx, or
404/410) writes it, and it is exposed on `GET /v1/search/infringements` so the proxy can decide
whether it may say "this came down".

`infringements.last_attempted_at` (migration 0013) means *we tried*, and moves on every probe
including the failures. The due-queue orders by it. Ordering by `last_checked_at` alone starves: a
permanently unreachable host keeps its NULL, sits at the front of every batch forever, and once there
are more such rows than `RECHECK_BATCH_SIZE` nothing else is ever checked — a loop that looks healthy
and has stopped working.

### The durable reference vs. the expiring credential

`search_seeds.source_object_ref` (migration 0011, renamed from `source_object_uri`) is an **opaque
durable reference** to the proxy's object — an object key. `search_runs.seed_url` is a **presigned
GET, minted per run** by the proxy at enqueue.

They are separate because they have different lifetimes and merging them is a bug that hides. The
seed row outlives everything; a presigned URL expires in at most 7 days (SigV4's cap). Stored
together, a seed works for one week and then every scan of it fails with a 403 that reads as a
provider outage — invisible in testing, because fresh seeds always work.

`ClaimedRun.seed_url` reads `search_runs`, never the seed. If the URL expires before dispatch the
providers fail normally, the run completes with an empty `providers_succeeded`, and cadence is left
alone (`should_retier` — a run where nothing succeeded is not evidence of an empty scan). The proxy
re-enqueues. There is no refresh path here: building one would need S3 credentials this service does
not hold.

Note this is `search_seeds` only. `enrolments.source_object_uri` is a different column — the
ReferenceImage pointer named in INVARIANTS #9 — and is unchanged.

### The thing found vs. the observation of it

This split is the whole point of step 6 and it is worth stating plainly, because the alternative was
already built once and had to be replaced.

`search_matches` (migration 0001, dropped in 0006) was keyed `UNIQUE (run_id, url_hash,
provider_id)` — **per run**. A weekly rescan finding the same unchanged URL wrote a new row every
week:

```
100k users x 20 matches x 2 providers x 52 weeks  =  ~208M rows/year, growing with TIME
```

That is the same failure class as the old system's `matches[].seenInScans`, which appends one date
string per scan forever against DynamoDB's 400 KB item cap with the write error swallowed
(`weeklyInfringementScanner.js:1016`).

Separating the stable infringement from the per-provider attestation makes a rescan an `UPDATE`:

```
100k users x 20 matches x 2 providers  =  ~4M rows TOTAL, growing with CONTENT
```

`tests/test_search_store.py::test_52_weekly_rescans_over_static_corpus_add_zero_rows` is the
permanent regression test for this. Do not delete it.

```sql
-- Stable. One row per (user_ref, url_hash). The thing the user acts on.
CREATE TABLE infringements (
  infringement_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_ref         UUID NOT NULL,
  url_hash         TEXT NOT NULL REFERENCES content_urls(url_hash),
  page_url         TEXT NOT NULL,
  image_url        TEXT,
  keyed_on         TEXT NOT NULL DEFAULT 'page_url'
                   CHECK (keyed_on IN ('page_url', 'image_url')),
  first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  seen_count       INT NOT NULL DEFAULT 1,
  url_alive        BOOLEAN NOT NULL DEFAULT true,
  last_checked_at  TIMESTAMPTZ,
  band             TEXT NOT NULL DEFAULT 'review',
  status           TEXT NOT NULL DEFAULT 'new',
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_ref, url_hash)
);

CREATE INDEX infringements_user_idx ON infringements (user_ref, last_seen_at DESC);
CREATE INDEX infringements_review_idx ON infringements (band) WHERE band = 'review';

-- One row per (infringement, provider). UPDATED on rescan, never appended.
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
  -- Added by 0007. `band` is this ONE provider's verdict; the infringement's
  -- band is the roll-up of every attestation on it.
  band               TEXT NOT NULL DEFAULT 'review'
                     CHECK (band IN ('drop','review','auto_confirm')),
  -- Which config produced that band. Without it a retune makes every
  -- historical band uninterpretable.
  calibration_version TEXT,
  UNIQUE (infringement_id, provider_id),
  CONSTRAINT attestation_score_shape CHECK (
    (score_kind = 'numeric'     AND provider_score    IS NOT NULL) OR
    (score_kind = 'categorical' AND provider_category IS NOT NULL)
  )
);

CREATE INDEX attestations_infringement_idx ON attestations (infringement_id);
CREATE INDEX attestations_run_idx ON attestations (last_run_id);
```

`provider_score` is **raw and provider-native**. Provider A's 0.92 and Provider B's 0.92 are
different quantities; calibration is a separate, versioned step and nothing rescales or combines
them. The `attestation_score_shape` CHECK is what lets one table hold both a numeric provider (Hive
Web Search, 0.5–1.0) and a categorical one (Google Web Detection, which returns `score: null`)
without an adapter inventing a number.

### Calibration and the evaluation set — **built** (step 7, migration 0007)

`infringements.band` moved off the hardcoded `'review'` in step 7. Four tables make that possible.

```sql
CREATE TABLE calibration_configs (
  config_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  provider_id      TEXT NOT NULL REFERENCES providers(provider_id),
  version          TEXT NOT NULL,
  score_kind       TEXT NOT NULL CHECK (score_kind IN ('numeric','categorical')),
  -- In the provider's NATIVE units, validated against providers.score_domain.
  --   numeric:     [{"band":"drop","max":0.72}, …]
  --   categorical: {"full_match":"auto_confirm", …}
  bands            JSONB NOT NULL,
  eval_set_id      TEXT,
  eval_sample_size INT,
  -- ADVISORY ONLY. `activate` recomputes from eval_observations and never
  -- reads this: a check that trusts a column an operator can edit is not a
  -- check.
  measured         JSONB,
  active           BOOLEAN NOT NULL DEFAULT false,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  activated_at     TIMESTAMPTZ,
  activated_by     TEXT,
  UNIQUE (provider_id, version)
);

CREATE UNIQUE INDEX calibration_one_active
  ON calibration_configs (provider_id) WHERE active;

-- `label` answers "is this the user's likeness, and should they be told?" —
-- NOT "is this an authentic photograph of them". That distinction is why a
-- derived_edit is a POSITIVE.
CREATE TABLE eval_items (
  item_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  eval_set_id    TEXT NOT NULL,
  seed_uri       TEXT NOT NULL,
  candidate_url  TEXT NOT NULL,
  label          TEXT NOT NULL CHECK (label IN ('true_match','false_match','uncertain')),
  label_kind     TEXT NOT NULL CHECK (label_kind IN ('same_person','derived_edit',
                                       'novel_generation','lookalike','unrelated')),
  -- NOT NULL alone permits ''. The regex is what actually rejects an item
  -- with no traceable consent basis.
  consent_basis  TEXT NOT NULL CHECK (consent_basis ~ '\S'),
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

-- What a provider actually said about a labelled item. Mirrors attestations:
-- one item, many providers, re-observation UPDATES rather than appends. Kept
-- out of infringements/attestations so nothing serving real users carries
-- test rows.
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

-- Recall depends on counting the true_matches a provider FAILED to return.
-- An absent eval_observation is ambiguous alone: either "asked and did not
-- return it" (a miss, and data) or "never asked" (not data). Only coverage
-- separates them, which is also what makes the activate floor's coverage
-- condition checkable without rejecting every honest set.
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

`infringements` also gained `band_reason TEXT` and a CHECK pinning `band` to the same three values.

**Sourcing is a hard constraint, not a preference.** Eval imagery comes from consenting
participants, public-domain, or synthetic sources only — never real victim content, never scraped
material. `novel_generation` items are non-sexual synthetic portraits: the question being measured is
whether image search can find a novel generation of a face, and the answer does not depend on what
the image depicts.

### Dedup rules

| Case | Result |
|---|---|
| One match with 3 backlinks | **Three** infringements — three places to act |
| Same page twice in one run, different image URLs | **One** infringement, collapsed before the write |
| No backlink available | Key on `image_url`, record that in `keyed_on` |
| Hive and Google both find page X for user Y | **One** infringement, **two** attestations |
| Same URL found for two different users | **Two** infringements — cross-user is never dedup |

The last row is enforced by `UNIQUE (user_ref, url_hash)`, and it is a safety boundary rather than a
modelling preference: collapsing across users leaks one person's matches to another.

Two counters are easy to confuse. `seen_count` counts provider observations of an infringement (two
providers in one run bump it twice), so it answers "how often has anything seen this". `confirm_count`
is per-provider and is the clean per-provider signal. `provider_count` on the API surface is simply
the number of attestations — an agreement signal, not a hit count.

### Rescan semantics

```
Found again  -> UPDATE infringements SET last_seen_at, seen_count + 1
                UPDATE attestations  SET last_confirmed_at, confirm_count + 1,
                                         provider_score (may have moved)
Not found    -> touch nothing. A stale last_confirmed_at is the signal.
                Do NOT delete, do NOT mark dead — url_alive is the recheck
                loop's job, and that is not in v1.
New          -> INSERT both
```

All writes are `INSERT ... ON CONFLICT ... DO UPDATE`. A rescan must never raise a duplicate-key
error and must never insert.

### URL normalisation and `content_urls`

```sql
ALTER TABLE content_urls
  ADD COLUMN normalisation_version TEXT NOT NULL DEFAULT 'v1',
  ADD COLUMN canonical_url TEXT;
```

`url_hash = sha256(canonical_url)` under the v1 rules in `src/imageshield/search/urlhash.py`:
lowercase scheme and host, punycode the host, strip default ports, strip the fragment, resolve dot
segments, **preserve path case**, strip tracking params (`utm_*`, `fbclid`, `gclid`, `msclkid`,
`mc_eid`, `ref`, `ref_src`, `source`, `igshid`, `_ga`, `yclid`), sort the remaining query params,
normalise percent-encoding, and strip one trailing slash except on a bare root.

The scheme is deliberately preserved: `http://example.com/a` and `https://example.com/a` are
different origins and must not collapse.

`canonical_url` is stored because debugging a dedup failure without it means re-deriving the
normalisation by hand. `normalisation_version` is stored per row because changing the rules
invalidates every stored hash — a future v2 must bump it rather than silently splitting the dedup
into two populations that never match. Rows written before step 6 carry `'v0-interim'`: they were
hashed from the raw, unnormalised URL.

### `provider_calls.raw_response` retention

`raw_response` is stored verbatim on every call, including failures — it is the only way to recompute
bands over history when a provider retunes. One row per (run, provider) is bounded by runs, but the
JSONB is not, so `python -m imageshield.search.retention` nulls payloads older than
`RAW_RESPONSE_RETENTION_DAYS` (default 90) and leaves the metadata row — status, http_status,
latency, cost — intact.

### Subject eligibility — **built** (step 8, migration 0008)

The first table in this schema that parents a `user_ref`. Before it,
`enrolments.user_ref`, `search_seeds.user_ref` and `infringements.user_ref` were unparented UUIDs with
no subject record anywhere.

```sql
CREATE TABLE subjects (
  user_ref            UUID PRIMARY KEY,
  discovery_eligible  BOOLEAN NOT NULL,
  eligibility_reason  TEXT NOT NULL
                      CHECK (eligibility_reason IN ('adult','minor_discovery_deferred')),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- The pairing is enforced by the database, not by every writer: a row
  -- claiming 'adult' AND ineligible is the exact corruption that would let a
  -- minor be scanned while the reason column reads reassuringly.
  CONSTRAINT subjects_reason_matches_flag CHECK (
    (discovery_eligible = true  AND eligibility_reason = 'adult') OR
    (discovery_eligible = false AND eligibility_reason = 'minor_discovery_deferred')
  )
);

CREATE INDEX subjects_ineligible_idx ON subjects (user_ref) WHERE discovery_eligible = false;

ALTER TABLE search_seeds
  ADD CONSTRAINT search_seeds_subject_fk FOREIGN KEY (user_ref) REFERENCES subjects(user_ref);
```

Written **only** by the enrolment transaction (`liveness/store.py::finalize_enrolled`), from the
required `subject_is_adult` field on `POST /v1/liveness/{sid}/result`, and in the same transaction as
the `enrolments` row — subject first, so the follow-up FK from `enrolments` is a one-line migration.
A failed or quality-rejected session writes no subject row, which is the safe direction: no subject
means no seed and no discovery.

`ON CONFLICT DO UPDATE ... WHERE discovery_eligible IS DISTINCT FROM EXCLUDED.discovery_eligible` —
so a DOB correction applies, and `updated_at` means "the assertion changed" rather than "the user
enrolled again". See INVARIANTS #8 / #8b for why the flag lives here and not in the request.

Because the flag is mutable, **`discovery_eligible` is re-read on `claim_run`** and a run whose subject
stopped being eligible between enqueue and dispatch is set to `search_runs.status = 'refused'` — never
`completed`, which would read as "we looked and found nothing" about a search that never ran. The
`LEFT JOIN` + `COALESCE(..., false)` makes a missing subject row read as ineligible rather than as an
unclaimable run. INVARIANTS #43.

The FK means `POST /v1/seeds` can now fail where it previously could not. `create_seed` translates
`ForeignKeyViolation` into a domain error so the route answers `409 subject_unknown` — the same code
`POST /v1/search` uses for the identical condition — rather than letting psycopg's exception escape as
a bare `500` with none of the error envelope. Note that a **minor may hold seeds**: only discovery is
refused for them, so `/v1/seeds` checks existence, not eligibility.

**Follow-up:** the same FK on `enrolments`. Deferred in 0008 because enrolment is what creates the
subject row; the intra-transaction ordering had to exist and be tested first.

### Cost, breakers, kill switches, cadence — **built** (step 8, migration 0009)

```sql
ALTER TABLE providers
  ADD COLUMN cost_per_call_usd  NUMERIC(10,6),
  ADD COLUMN monthly_budget_usd NUMERIC(10,2),
  ADD COLUMN rate_limit_per_min INT,
  ADD COLUMN breaker_state      TEXT NOT NULL DEFAULT 'closed'
                                CHECK (breaker_state IN ('closed','open','half_open')),
  ADD COLUMN breaker_opened_at  TIMESTAMPTZ,
  ADD COLUMN breaker_reason     TEXT,
  -- Not in the step-8 sketch, and both required by the state machine:
  -- the counter has to be durable (in-process state resets on deploy and is
  -- per-worker), and doubling the cooldown needs the CURRENT cooldown.
  ADD COLUMN breaker_consecutive_failures INT NOT NULL DEFAULT 0,
  ADD COLUMN breaker_cooldown_seconds INT;

-- `daily_budget_usd` already existed (0001) and is NOT re-added. It was
-- declared before anything enforced it; 0009 is what starts enforcing it.

ALTER TABLE provider_calls
  ADD CONSTRAINT provider_calls_status_valid CHECK (
    status IN ('ok','error','rate_limited','timeout',
               'budget_exceeded','breaker_open','provider_disabled')
  );

-- Pre-aggregated, one row per provider per day.
CREATE TABLE provider_spend (
  provider_id   TEXT NOT NULL REFERENCES providers(provider_id),
  spend_date    DATE NOT NULL,          -- UTC; a DST-shifting day boundary
  call_count    INT NOT NULL DEFAULT 0, -- gives two 23h and two 25h days a year
  cost_usd      NUMERIC(14,6) NOT NULL DEFAULT 0,   -- scale >= the price's scale
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (provider_id, spend_date)
);

ALTER TABLE search_seeds
  ADD COLUMN scan_tier TEXT NOT NULL DEFAULT 'standard'
             CHECK (scan_tier IN ('new','standard','relaxed','dormant','priority')),
  ADD COLUMN next_scan_after TIMESTAMPTZ,
  ADD COLUMN consecutive_empty_scans INT NOT NULL DEFAULT 0;

CREATE INDEX seeds_due_idx ON search_seeds (next_scan_after) WHERE status = 'active';
```

Three things worth stating about these columns:

**The accumulator's scale must be >= the price's scale.** `cost_usd` is `NUMERIC(14,6)` against a
`NUMERIC(10,6)` `cost_per_call_usd`, and it was `NUMERIC(12,4)` until measurement showed why that is
wrong: the upsert coerces each increment to the column type *before* adding, so a narrower column
rounds every individual call rather than rounding the total once. Against Postgres 16 — ten calls at
0.000250 stored 0.0030 (+20%, cap binds early, a provider with headroom gets skipped); ten at 0.000040
stored 0.0000, so the accumulator never grows and a configured daily budget silently stops binding.
That last one is a fail-open in the one check INVARIANTS #38 requires to fail closed. Google's 0.003500
is exact at four decimals, which is the only reason it would not have shown today. See INVARIANTS #39b.

**`provider_spend` exists so the request path never aggregates.** The budget guard reads exactly one
row by primary key. `provider_calls` grows with every call ever made, so a `SUM` over it would make
the guard protecting spend slower in proportion to how much has been spent (INVARIANTS #38). The
`provider_calls` insert, the `provider_spend` upsert and the breaker transition share **one
transaction** under a row lock on `providers` (#39).

**`provider_calls.status`'s CHECK is load-bearing, not hygiene.** `providers_succeeded` is derived
from it, so a typo'd status silently becomes a not-ok row — which reads downstream as coverage we did
not have. The three step-8 values are skips: a provider that was *not called*, recorded rather than
silent (#41).

**Cost figures as shipped.** `google.cost_per_call_usd = 0.003500` — Cloud Vision Web Detection list
price, USD 3.50 per 1000 units, one annotate request with one `WEB_DETECTION` feature = one unit.
`hive.cost_per_call_usd` is deliberately **NULL**: Hive Web Search is contract-priced and no measured
or quoted figure exists in this repo (the devtools harness measured the *liveness* cost, ≈USD 0.015,
and nothing else). A budget enforced against an unsourced number is worse than no budget, because the
error surfaces on an invoice. Both `daily_budget_usd` values are NULL, so behaviour is unchanged; a
budget set *without* a cost fails closed (#38).

**Follow-up:** fill `hive.cost_per_call_usd` and both `daily_budget_usd` values from the signed Hive
agreement. Until then the mechanism is built and tested but enforcing nothing for hive.

`monthly_budget_usd` is reported and alarmed on (`monthly_spend_near_budget`, the column's ONLY
enforcement), **not** enforced at dispatch: the dispatch guard is
deliberately one indexed row, and widening it to a month of rows would put a range scan on the request
path. Month-to-date is computed in the admin observability read
(`providers/observability.py`), which sums at most 31 pre-aggregated rows in Python — so there is no
SQL aggregation over spend anywhere in `src/imageshield/providers/`, and the rule holds by inspection
of the directory rather than by remembering which module is on which path.

---

## 3. Adjudication service

```sql
CREATE TYPE task_status AS ENUM ('queued','assigned','decided','expired');
CREATE TYPE review_decision AS ENUM ('confirmed','rejected','uncertain');

CREATE TABLE review_tasks (
  task_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  candidate_id UUID NOT NULL UNIQUE,
  user_ref     UUID NOT NULL,
  score        NUMERIC(5,2) NOT NULL,
  status       task_status NOT NULL DEFAULT 'queued',
  assigned_to  UUID,
  assigned_at  TIMESTAMPTZ,
  priority     INT NOT NULL DEFAULT 0,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX tasks_queue_idx ON review_tasks (status, priority DESC, created_at)
  WHERE status = 'queued';

CREATE TABLE review_decisions (
  decision_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id      UUID NOT NULL REFERENCES review_tasks(task_id),
  reviewer_id  UUID NOT NULL,
  decision     review_decision NOT NULL,
  notes        TEXT,
  decided_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Reviewers see the face crop only, never the full image. Same isolated-fetcher path the user-facing
surface uses — reviewer welfare is an operational cost, and this is the cheapest mitigation available.

---

## 4. Report service

```sql
CREATE TYPE hit_status AS ENUM (
  'new','acknowledged','dismissed_not_me','url_dead','withdrawn'
);

CREATE TABLE reports (
  report_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_ref    UUID NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX reports_user_uniq ON reports (user_ref);

CREATE TABLE report_hits (
  hit_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  report_id       UUID NOT NULL REFERENCES reports(report_id),
  user_ref        UUID NOT NULL,
  candidate_id    UUID NOT NULL,              -- provenance into match service
  decision_id     UUID,                       -- provenance into adjudication; null if auto_confirm
  source_url      TEXT NOT NULL,
  source_domain   TEXT NOT NULL,
  face_bbox       JSONB NOT NULL,
  score           NUMERIC(5,2) NOT NULL,
  model_id        TEXT NOT NULL,
  status          hit_status NOT NULL DEFAULT 'new',
  first_seen_at   TIMESTAMPTZ NOT NULL,
  last_checked_at TIMESTAMPTZ,
  url_alive       BOOLEAN NOT NULL DEFAULT true,
  notarised_at    TIMESTAMPTZ,
  notary_receipt  TEXT,                       -- RFC 3161 token
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX hits_candidate_uniq ON report_hits (candidate_id);
CREATE INDEX hits_user_status_idx ON report_hits (user_ref, status);
CREATE INDEX hits_recheck_idx ON report_hits (last_checked_at)
  WHERE status IN ('new','acknowledged') AND url_alive = true;
```

`face_bbox` is denormalised here deliberately — the display path must not have to call the match
service to render a crop.

### What happens to a rejected match

This was the open question from earlier. The answer:

`dismissed_not_me` sets the status and **feeds reviewer calibration only**. It does *not* adjust the
user's identity vectors, and it does not suppress future candidates from the same domain.

Reason: a user rejecting a true positive under distress is a real and common behaviour. If rejections
retrained the identity index, the users most affected would systematically degrade their own
protection. Keep the signal, don't act on it automatically.

```sql
CREATE TABLE hit_feedback (
  feedback_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  hit_id       UUID NOT NULL REFERENCES report_hits(hit_id),
  user_ref     UUID NOT NULL,
  signal       TEXT NOT NULL,      -- 'not_me','uncertain','confirmed'
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

## 5. Audit

Sessions live in the proxy. We keep only the audit log.

```sql
CREATE TABLE audit_log (
  audit_id    BIGSERIAL PRIMARY KEY,
  actor_user_ref UUID,
  actor_type  TEXT NOT NULL,        -- 'user','reviewer','system','admin'
  action      TEXT NOT NULL,        -- 'report.view','crop.render','enrolment.create'
  subject_user_ref UUID,
  resource_id UUID,
  ip_address  INET,
  metadata    JSONB,
  occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX audit_subject_idx ON audit_log (subject_user_ref, occurred_at DESC);
CREATE INDEX audit_action_idx ON audit_log (action, occurred_at DESC);
```

There is no `sessions` table here — the proxy owns sessions entirely.

`audit_log` is append-only. No `UPDATE` or `DELETE` grant on it for the application role. Every crop
render gets a row — it is the only way to detect a compromised account being used as a search console.

---

## Cross-service contracts enforced in application code

Postgres cannot enforce these. Tests must.

1. `match_candidates.user_ref` must correspond to an active user. **We cannot check this** — there
   is no users table here. The proxy filters before calling us.
2. `report_hits.candidate_id` must exist in `match_candidates` and its `model_id` must match.
3. A `report_hits` row with `decision_id IS NULL` is only legal when its candidate's band was
   `auto_confirm`.
4. `users.status = 'deleted'` must trigger `DeleteFaces` in Rekognition and cascade-tombstone every
   `enrolments` row. A flag flip alone leaves the face searchable.
5. No service other than Identity may open a connection to the Identity database. Enforce with
   distinct DB roles, not convention.

---

## Migration from the current implementation

The existing store is DynamoDB keyed by phone. This is a **backfill, not a migration** — the schemas
don't correspond.

1. Read `imageshield_mobile_users`, mint a `user_ref` per record **in the proxy**, normalise phone to +E.164. Phone normalisation happens there and never here.
2. Records where two phone formats collided into one identity: flag for manual review, do not merge.
3. `userId` values produced by the old search-then-index path are **not trustworthy** — any of them
   may be a cross-user collision. Do not carry them into `enrolments`.
4. Every migrated user re-enrols: new liveness session, new consent under a versioned document, fresh
   `IndexFaces`. There is no path around this, because no existing enrolment has a liveness pass
   behind it.
5. Old Rekognition collections are abandoned wholesale, not migrated. Create `identity-v1` empty.