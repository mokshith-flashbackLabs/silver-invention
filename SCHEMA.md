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

### Consent

```sql
CREATE TYPE consent_status AS ENUM ('pending', 'signed', 'withdrawn', 'expired');

CREATE TABLE consent_records (
  consent_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_ref               UUID NOT NULL,
  signer_user_ref        UUID NOT NULL,
  document_version       TEXT NOT NULL,          -- 'biometric-consent-v1.0'
  document_sha256        TEXT NOT NULL,          -- hash of the exact rendered text
  document_uri           TEXT NOT NULL,          -- immutable store of the signed artifact
  provider               TEXT NOT NULL DEFAULT 'docuseal',
  provider_submission_id TEXT,
  status                 consent_status NOT NULL DEFAULT 'pending',
  signed_at              TIMESTAMPTZ,
  withdrawn_at           TIMESTAMPTZ,
  signer_ip              INET,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX consent_user_idx ON consent_records (user_ref, status);
CREATE UNIQUE INDEX consent_provider_uniq
  ON consent_records (provider, provider_submission_id)
  WHERE provider_submission_id IS NOT NULL;
```

`signer_user_ref = user_ref` for self-consent. They diverge only for guardian co-signature (v2). Both
are real authenticated accounts — never a typed-in name.

`document_sha256` is the column that makes consent demonstrable. Storing `consentSigned: true` (what
the current system does) proves nothing about *what* was agreed to.

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
  consent_id          UUID NOT NULL REFERENCES consent_records(consent_id),
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
`attestations`. Migrations `0001`, `0004`, `0005`, `0006`.

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
different quantities; calibration is a separate, versioned step (step 7) and `band` stays `'review'`
until it lands. The `attestation_score_shape` CHECK is what lets one table hold both a numeric
provider (Hive Web Search, 0.5–1.0) and a categorical one (Google Web Detection, which returns
`score: null`) without an adapter inventing a number.

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