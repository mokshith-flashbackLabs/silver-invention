# Near-term build: Liveness + multi-provider search

**This is the entire v1 scope of the services repo.** Nothing else is built here yet — no match
module, no adjudication queue, no report surface. Those are specified in `ARCHITECTURE.md` and come
later.

Both features are new surface. Neither touches `server.js`. Both are built in the services repo behind
the proxy, per `PROXY_INTEGRATION.md`.

## Ground rules for both

```
- Services repo only. The proxy (ImageShieldPhotoShare) is touched ONLY to
  add the service client, the user_ref column, and the presigned-URL minting.
- Services NEVER see a phone number. Every request carries user_ref, an
  opaque UUID. This is not a style preference — it is the boundary.
- Services hold NO AWS credentials for S3. The proxy mints presigned URLs;
  services PUT/GET through them and discard. Same pattern as the AgentMeeMaw
  tribute renderer.
- Postgres, schema-per-module, distinct DB role per module.
- TypeScript, strict mode. Branded types for UserRef, SessionId, ProviderId.
- Config from environment, validated at boot. Process exits on a missing key.
- Service-token auth on every endpoint (PROXY_INTEGRATION §2). No CORS, no
  public ingress, no user model.
```

### The one thing the proxy must add first

Services key on `user_ref`, a UUID. The proxy's `users` and `imageshield_mobile_users` have no such
column today.

**Add `user_ref UUID` to the proxy's user record, backfill it, and pass it on every service call.**
One column, one backfill, and it becomes the v2 `user_id` unchanged. Without it, the only identifier
available is a phone number, and the boundary collapses on day one.

---

# Part 1 — Liveness

## 1.0 Prerequisite — proxy-side, not ours

Liveness binds a face to an account. If the account is takeable, liveness proves a live human enrolled
against someone else's identity.

The proxy's OTP is stored as a plain attribute (`server.js:7408`), returned unprojected by
`GET /users/:phone` (`8534`), and compared with `===` (`9065`). Trigger an OTP for any phone number,
read it back over HTTP, verify.

**This is tracked in `PROXY_INTEGRATION.md` §9 and is not a task in this repo.** Noted here because it
determines what the feature is actually worth. Shipping liveness against a takeable account is
defensible only if you know that's what you're doing.

## 1.1 Flow

```
client              proxy                 services              Rekognition
  │  start liveness   │                       │                      │
  ├──────────────────►│  POST /v1/liveness    │                      │
  │                   │  { user_ref }         │                      │
  │                   ├──────────────────────►│ CreateFaceLiveness   │
  │                   │                       ├─────────────────────►│
  │                   │  201 { session_id,    │◄─────────────────────┤
  │                   │    provider_sid,      │                      │
  │◄──────────────────┤    region }           │                      │
  │                                                                  │
  │  Amplify FaceLivenessDetector — video streams direct             │
  ├─────────────────────────────────────────────────────────────────►│
  │                   │                       │                      │
  │  challenge done   │  POST /v1/liveness/   │                      │
  ├──────────────────►│    {id}/result        │                      │
  │                   │  { reference_put_url, │                      │
  │                   │    audit_put_urls[] } │                      │
  │                   ├──────────────────────►│ GetFaceLiveness      │
  │                   │                       │   SessionResults     │
  │                   │                       ├─────────────────────►│
  │                   │                       │◄── Confidence,       │
  │                   │                       │    ReferenceImage,   │
  │                   │                       │    AuditImages[]     │
  │                   │                       │                      │
  │                   │                       │ PUT ReferenceImage   │
  │                   │                       │  to proxy's S3 ──────┤
  │                   │                       │                      │
  │                   │                       │ IndexFaces(RefImage) │
  │                   │                       ├─────────────────────►│
  │                   │  200 { status,        │                      │
  │◄──────────────────┤    confidence,        │                      │
  │                   │    enrolled }         │                      │
```

**Services never proxy video.** The client talks to Rekognition directly using the session ID.

**Services never hold S3 credentials.** The proxy mints presigned PUTs on the result call; services
PUT the `ReferenceImage` and audit frames through them, then discard the bytes. Presigned URLs must
live ≥15 minutes — a result call may be retried.

**Enrol from `ReferenceImage`.** Not from a separate selfie upload. If they differ, liveness and
enrolment are decoupled and the feature proves nothing about the face that gets indexed. This is the
single most important line in Part 1.

## 1.2 Tables

```sql
CREATE TABLE liveness_sessions (
  session_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_ref             UUID NOT NULL,      -- opaque; supplied by the proxy
  provider_session_id  TEXT UNIQUE NOT NULL,
  status               TEXT NOT NULL,      -- created|pending|passed|failed|expired|consumed
  confidence           NUMERIC(5,2),
  failure_reason       TEXT,
  attempt_number       INT NOT NULL DEFAULT 1,
  reference_image_uri  TEXT,               -- proxy's S3, written via presigned PUT
  audit_image_uris     TEXT[],
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at         TIMESTAMPTZ,
  expires_at           TIMESTAMPTZ NOT NULL,
  consumed_at          TIMESTAMPTZ         -- set when an enrolment references it
);

CREATE INDEX liveness_user_idx ON liveness_sessions (user_ref, created_at DESC);

CREATE TABLE liveness_enrolments (
  enrolment_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id        UUID NOT NULL UNIQUE REFERENCES liveness_sessions(session_id),
  user_ref          UUID NOT NULL,
  collection_id     TEXT NOT NULL,
  external_face_id  TEXT NOT NULL,
  quality_score     NUMERIC(5,2),
  model_id          TEXT NOT NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at        TIMESTAMPTZ
);

CREATE UNIQUE INDEX enrolments_face_uniq ON liveness_enrolments (collection_id, external_face_id);
```

`session_id UNIQUE` on `liveness_enrolments` enforces single-use at the schema level — the constraint
does the work, not the application code.

**No phone column.** `user_ref` is the only identifier services hold, and it maps 1:1 onto the v2
`user_id`. These two tables migrate into `SCHEMA.md`'s `liveness_sessions` and `enrolments` with a
column rename and the addition of the `consent_id` FK.

## 1.3 Endpoints

All require `X-Service-Token`. All take `user_ref`, never a phone.

```
POST /v1/liveness/sessions
  { user_ref }
  → 201 { session_id, provider_session_id, region, expires_at }
  409 if a passed-but-unconsumed session exists for this user_ref
  429 if >5 attempts in 24h (cost control as well as anti-abuse)

POST /v1/liveness/:session_id/result
  { reference_put_url, audit_put_urls[] }
  → 200 { status: 'passed'|'failed', confidence, enrolled: bool }
  Calls GetFaceLivenessSessionResults. On pass AND confidence >= threshold:
    - PUT ReferenceImage + AuditImages through the presigned URLs
    - IndexFaces(ReferenceImage), QualityFilter: HIGH,
      ExternalImageId = user_ref, MaxFaces: 1
    - write liveness_enrolments, set session consumed_at
    - NOTIFY enrolment_complete
  400 if presigned URLs are absent
  410 if already consumed

GET /v1/liveness/:session_id
  → 200 { status, confidence, enrolled }

DELETE /v1/enrolments/:user_ref
  → 204. Calls DeleteFaces, verifies absence, then tombstones.
  Build this now even though nothing calls it yet — see 1.4 #8.
```

Not idempotent: `POST /v1/liveness/sessions` creates a provider session and burns an attempt. Do not
retry blind. `POST /.../result` accepts `Idempotency-Key`.

## 1.4 Non-negotiables

1. **No `SearchFacesByImage` in this path.** Identity is the `user_ref` the proxy supplies. The
   existing `processFaceRecognition` (`server.js:9585`) derives identity from a search result with
   thresholds varying 90/95/99 across call sites — that is the fragmentation bug. Do not reuse it,
   do not port it, do not reference it.
2. **One threshold, from config.** `LIVENESS_MIN_CONFIDENCE`, `FACE_MATCH_THRESHOLD`. No inline
   literals anywhere.
3. **`QualityFilter: HIGH`**, not `AUTO`.
4. **New collection: `identity-v1`.** Do not write into the existing one — it holds fragmented IDs
   and faces from deleted accounts (`DeleteFaces` is called nowhere in the old repo).
5. **`ExternalImageId` is `user_ref`.** Never phone, never anything phone-derived. It comes back in
   every search response and lands in logs.
6. **Services never write to S3 directly.** Presigned PUTs only. No AWS S3 credentials in this repo's
   environment at all — if they're not there, the mistake can't be made.
7. **Session single-use, 10-minute TTL.**
8. **Write the `DeleteFaces` path now**, even though v1 has no caller. The old repo never had one, and
   a face-indexing system without a deletion path accumulates vectors it can never remove.

## 1.5 Done when

- End-to-end enrolment works from a real device
- A photo-of-a-photo fails
- A video replay fails
- The same session cannot be consumed twice
- Enrolment uses the ReferenceImage — verify by diffing against a deliberately different selfie
- **Two lookalike test faces produce two distinct identifiers** — permanent regression test
- Region support confirmed for Face Liveness before sprint commit — ✅ confirmed 2026-08-05:
  deploying to us-east-1, which supports Face Liveness

---

# Part 2 — Multi-provider search

## 2.1 The interface

```ts
interface SearchProvider {
  readonly id: string;                    // 'pimeyes' | 'facecheck' | ...
  readonly costPerSearch: number;         // USD, for budget tracking
  readonly rateLimit: { perMinute: number; perDay: number };

  search(input: {
    faceImageUri: string;
    externalUserId: string;
  }): Promise<RawProviderResult[]>;

  /** NO calibrate() method. Adapters return raw scores and stop (§7.2).
   *  Banding is a separate, versioned, config-driven step: an adapter that
   *  normalises makes recalibration impossible without a redeploy, and a
   *  "common scale" across providers is a number with no meaning. */
}

interface RawProviderResult {
  sourceUrl: string;
  rawScore: number;
  thumbnailUrl?: string;
  firstSeenAt?: string;
  rawPayload: unknown;    // keep verbatim for recalibration later
}
```

Read `server/lambda/weeklyInfringementScannerLambda` before writing this. It is the current search
implementation and the only code in the repo that has run against real infringement data. Whatever it
knows about recall, retries, or site quirks is the most valuable thing here. Document what it does
before replacing it.

## 2.2 URL normalisation and dedup

Same infringement found by three providers is **one hit with three attestations**, not three hits.

```
normalise(url):
  lowercase host, strip www.
  drop protocol
  drop query params except a provider-specific keep-list
  strip trailing slash, fragment
  → sha256 → url_hash
```

```sql
CREATE TABLE search_hits (
  hit_id           UUID PRIMARY KEY,
  external_user_id UUID NOT NULL,
  url_hash         TEXT NOT NULL,
  source_url       TEXT NOT NULL,
  source_domain    TEXT NOT NULL,
  -- NO best_score. There is no cross-provider scale to take a "highest" on.
  provider_count   INT NOT NULL,            -- agreement signal
  first_seen_at    TIMESTAMPTZ NOT NULL,
  last_seen_at     TIMESTAMPTZ NOT NULL,
  url_alive        BOOLEAN NOT NULL DEFAULT true,
  band             TEXT NOT NULL,           -- auto_confirm|review|drop
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ON search_hits (external_user_id, url_hash);

CREATE TABLE search_attestations (
  attestation_id  UUID PRIMARY KEY,
  hit_id          UUID NOT NULL REFERENCES search_hits(hit_id),
  provider_id     TEXT NOT NULL,
  raw_score       NUMERIC,                 -- RAW and provider-native. Never rescaled.
  band            TEXT NOT NULL,           -- drop|review|auto_confirm
  calibration_version TEXT,                -- which config produced that band
  raw_payload     JSONB NOT NULL,
  observed_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ON search_attestations (hit_id, provider_id, observed_at);

CREATE TABLE search_runs (
  run_id             UUID PRIMARY KEY,
  external_user_id   UUID NOT NULL,
  providers_queried  TEXT[] NOT NULL,
  providers_succeeded TEXT[] NOT NULL,      -- coverage is not guaranteed
  calibration_version TEXT NOT NULL,
  band_config        JSONB NOT NULL,
  cost_usd           NUMERIC(10,4),
  started_at         TIMESTAMPTZ NOT NULL,
  completed_at       TIMESTAMPTZ
);
```

`providers_succeeded` matters: partial coverage must be visible, or a silent provider outage looks
like "no infringements found."

## 2.3 Calibration

**Do not merge raw scores, and do not map them onto a common scale.** Provider A's 0.92 and Provider
B's 0.92 are different quantities with different distributions; a shared 0–100 scale makes them look
comparable when they are not. Each provider gets its own band boundaries in its own **native units**,
from its own labelled measurements. Nothing is rescaled and nothing is combined across providers —
`tests/test_boundaries.py` enforces that with a grep that has no allowlist.

> This section previously specified a `calibrate(rawScore): number` adapter method and a
> `calibrated_score` column mapping every provider onto a common scale. That is exactly the
> cross-provider comparison `CLAUDE.md` §7.2 forbids, and it is not built. Corrected here so the
> next reader doesn't implement the thing the test now blocks.

Build a small labelled set — consenting participants, public-domain, or synthetic imagery only,
never real victim content, never scraped material — and record a `consent_basis` per item or the item
does not go in the set. It must contain `lookalike` hard negatives: random negatives are easy for any
provider to reject and will make a bad threshold look excellent.

Bands are `drop | review | auto_confirm`. An infringement's band is the roll-up of its attestations:
any disagreement resolves to `review`, and agreement never promotes — two providers at `review` stay
`review`, because concurrence between two image-search providers indexing overlapping corpora is not
two independent observations. `attestations.calibration_version` records which config produced each
band, so a retune leaves history interpretable.

Until a provider is calibrated its results go into `review` band only — never `auto_confirm`, and
never `drop`. See `CLAUDE.md` §7.3 for the two keys, and
`docs/superpowers/specs/2026-08-07-step-7-calibration-banding-design.md` for the full design.

## 2.4 Cost and failure isolation

- Per-provider daily budget cap, enforced before dispatch. Circuit-breaks when exceeded.
- Per-provider kill switch in config, hot-reloadable.
- One provider failing never fails the run — record it in `providers_succeeded` and continue.
- Log `cost_usd` per run. N providers × M users × weekly cadence multiplies fast.
- Exponential backoff with jitter; respect provider rate limits as hard caps, not targets.

## 2.5 Done when

- Two providers returning the same URL produce one `search_hits` row with `provider_count = 2`
- A provider timing out produces a completed run with reduced `providers_succeeded`
- Budget exhaustion circuit-breaks without failing the run
- An uncalibrated provider cannot place a hit in `auto_confirm`
- Replaying an identical provider response creates no duplicate hit
- `cost_usd` is queryable per user per month

---

## What both features deliberately do not do

- No writes to any proxy-owned table — `Monitoring`, `ImageShieldInfringements`,
  `imageshield_mobile_users`. Services own their schemas and nothing else.
- No phone number anywhere in this repo. Not in a column, not in a log line, not in a
  `ExternalImageId`.
- No S3 credentials in this repo's environment.
- No user-facing display of full images — crop or metadata only.
- No auto-notification from `review` band without human adjudication.
- No match module, no adjudication queue, no report surface. Those are `ARCHITECTURE.md`'s scope and
  are not built yet.

If a deadline forces a shortcut, take it in the *presentation* layer. Do not take it in the identity
binding or the dedup key — those are the two things that are expensive to unwind, and both are load-
bearing for everything specified in `ARCHITECTURE.md` that comes after.
