# ARCHITECTURE.md — ImageShield Services

System-level reference for the ImageShield services repo. Companion to `CLAUDE.md` (operating manual),
`SCHEMA.md` (table-level detail), `INVARIANTS.md` (the hard rules), and `PROXY_INTEGRATION.md` (the
handoff brief for the proxy).

> **Repo scope:** This is the **services** repo. The proxy (`ImageShieldPhotoShare`, Node/Express) is
> a separate repo and remains the user-facing surface — login, OTP, sessions, profile, billing. We
> document the full system here so our contract surface is clear, but only the service components are
> implemented here.

> **⚠ Specified ≠ in scope.** This document describes the whole system. **v1 of this repo is liveness
> + third-party search provider integration only.** The match module, adjudication queue, report
> surface, crop fetcher, and partner ingest adapter are designed here but **not built yet**. See
> `CLAUDE.md` §6 for the scope table and `NEAR-TERM-BUILD.md` for the authoritative task list. Do not
> build a component because this document describes it.

---

## 1. System overview

```
┌────────────┐     ┌────────────────────┐     ┌──────────────────────────┐
│ Mobile /   │ ──▶ │  Proxy             │ ──▶ │  THIS REPO               │
│ Web client │     │  (separate repo)   │     │  ImageShield Services    │
└────────────┘     └────┬───────────────┘     └────┬─────────────────────┘
                        │                          │
                  ┌─────┼──────┐          ┌────────┼──────────┬──────────┐
                  ▼     ▼      ▼          ▼        ▼          ▼          ▼
              Dynamo   S3   Postgres   Postgres  Rekognition  SQS      Crop Fetcher
             (sessions)    (UI reads)  (canonical  + Liveness  (3, via  (isolated
                                        writes)               outbox)  deployable)
```

The proxy is the gateway for every external request. **The client never speaks to services directly.**
Services never speak to DynamoDB, never hold AWS credentials for S3, and have no user model.

Every request carries `user_ref` — an opaque UUID minted by the proxy. **Services never see a phone
number.** `user_ref` maps 1:1 onto what becomes `user_id` in v2, so the two names refer to the same
value at different stages of the migration.

Services run five loops: Enrolment (synchronous), Forward Match (async), Backfill (async, long),
Adjudication (human-in-loop), and Periodic (recheck + digest).

---

## 2. The five loops

### 2.1 Enrolment loop (synchronous)

Runs when a user completes liveness. The proxy has already authenticated them.

1. **Proxy** creates a session via `POST /v1/liveness/sessions` with `{user_ref}`. Services call
   `CreateFaceLivenessSession` and return the provider session ID.
2. **Client** completes the challenge against Rekognition directly, using the Amplify
   `FaceLivenessDetector`. **Services never proxy video.**
3. **Proxy** calls `POST /v1/liveness/:session_id/result`, supplying **presigned PUT URLs** for the
   reference and audit frames.
4. **Services** call `GetFaceLivenessSessionResults`, receiving `Confidence`, `ReferenceImage`, and
   `AuditImages[]`.
5. On pass and `Confidence >= LIVENESS_MIN_CONFIDENCE`: PUT the frames through the presigned URLs,
   then `IndexFaces` on the **`ReferenceImage`** into `identity-v1` with `ExternalImageId = user_ref`,
   `QualityFilter: HIGH`, `MaxFaces: 1`. Write `liveness_enrolments`, set `consumed_at`, discard the
   bytes.
6. `NOTIFY enrolment_complete` and enqueue a backfill job via the outbox.

**Enrol from `ReferenceImage`, never from a separately uploaded selfie.** If the indexed face is not
the frame Rekognition validated, liveness proves a human was present and nothing about who got
enrolled. This is the load-bearing detail of the whole loop.

**Services hold no S3 credentials.** Presigned URLs only, minted by the proxy, valid ≥15 minutes
because a result call may be retried.

No `SearchFacesByImage` anywhere in this path (INVARIANTS #1). Identity is the `user_ref` the proxy
supplies; a similarity score never determines who someone is. Sessions are single-use with a
10-minute TTL — replay returns `410`.

Face search *is* permitted in one place — `attribution/`, matching a face in a third-party photo
against a caller-supplied list of already-enrolled `user_ref`s (INVARIANTS #1a). That is a different
operation with a different failure cost, and it is barred from this path specifically.

### 2.2 Forward match loop (async, minutes)

Continuous. Drains `match:forward`.

New content arrives from the partner ingest adapter → `content_items` + `content_faces` written
(hashes and embeddings only, never bytes) → search against the identity index → candidates banded
`auto_confirm` / `review` / `drop`.

Latency budget: minutes. This is the loop that must not be starved.

### 2.3 Backfill loop (async, hours)

Drains `match:backfill`. Triggered by `enrolment_complete`.

A new enrolment searches the entire content index. Rate-limited per user and priority-tiered, because
ten signups would otherwise saturate the cluster. **Separate queue and separate worker pool from
forward** (INVARIANTS #18) — a backfill must never delay live ingest.

### 2.4 Adjudication loop (human)

`review`-band candidates become `review_tasks`. A trained reviewer sees a **face crop only**, rendered
live by the crop fetcher, and decides `confirmed` / `rejected` / `uncertain`.

There is no timeout that auto-promotes a review-band candidate (INVARIANTS #19). If the queue backs
up, the queue backs up.

### 2.5 Periodic loop (cron)

- **Recheck** — weekly re-probe of live hit URLs. Marks `url_alive = false`, never deletes. This is
  the only good news v1 can deliver.
- **Digest** — batched notification assembly. Never real-time, never 22:00–08:00 local
  (INVARIANTS #24). Services assemble; the proxy delivers.

---

## 3. Component reference

### 3.1 Identity module

The only module holding Article 9 data. Own database, own credentials, own DB role. Nothing else may
connect to it (INVARIANTS #14).

Owns: liveness session lifecycle, enrolment, the Rekognition collection, the identity vector index,
and the **consent reference** on an enrolment. Consent records and signed artifacts belong to the
proxy — see `PROXY_INTEGRATION.md` §4.

Does **not** own: `users`, phone numbers, OTP, sessions. Those are the proxy's. Identity keys everything on
the opaque `user_ref` the proxy supplies.

### 3.2 Partner ingest adapter

Behind an interface (INVARIANTS #34). Consumes
`{content_hash, source_url, face_bbox[], first_seen_at, partner_ref}` from the partner organisation.
Idempotent on `content_hash`.

**No image bytes cross this boundary.** If bytes ever cross, the legal separation between the two
organisations is fiction.

### 3.3 Match module

Two workers over two queues. Owns `content_items`, `content_faces`, `match_candidates`, `search_runs`.

Never joins to any user table — it holds `user_id` as an opaque value and nothing else.

`search_runs.threshold_config` records the exact bands used per run, so retuning doesn't orphan the
meaning of historical scores.

### 3.4 Adjudication module

Review queue, reviewer tooling, decisions. Crop-only display. Budget for reviewer welfare — this is an
operating cost people forget, and crop-only rather than full-image is the cheapest mitigation
available.

### 3.5 Report module

Owns `reports`, `report_hits`, `hit_feedback`, the recheck loop, and evidence export.

**Server-authoritative scoring only.** The old system computed a score client-side *and* server-side
with a −18 per active report divergence. For a product whose entire output is a score, that is
disqualifying.

### 3.6 Search provider adapters (v1 — built)

The step-5 adapter layer: `SearchProvider` implementations translate a provider's response into
match rows and stop. Adapters never normalise, band, threshold, rescale, or compare — calibration
is a separate, versioned, config-driven step (step 7), and `raw_response` is stored verbatim on
every call (including failures) so history can be recomputed when a provider retunes. Uncalibrated
providers reach `review` band only.

Two providers are integrated, with different score shapes recorded in `providers.score_kind` /
`score_domain`:

| Provider | Product | Score shape |
|---|---|---|
| `hive` | Hive **Web Search** (reverse image search, ~25B images) | numeric, raw 0.5–1.0 (0.5 is the floor, not a midpoint) |
| `google` | Google Vision **Web Detection** | categorical: `full_match` / `partial_match` / `page_match`; no number, ever |

**Hive naming trap:** Hive's separately-named "Media Search" matches movies/TV — not ours. Which
product a key hits is determined by the **Hive project the key belongs to, not the URL** (all
task-based products share `POST /api/v2/task/sync`), so a key provisioned against the wrong project
returns plausible-looking wrong results rather than an error.

Google's `webEntities` are never read: knowledge-graph lookups name famous people only, and are not
evidence about our user.

Both are `image_search` kind — they find copies of a known photo and can never find a deepfake
(§7.1 of `CLAUDE.md`); face-search coverage requires a face-search provider, none integrated yet.

Runs execute asynchronously: `POST /v1/search` writes the run and its outbox row in one
transaction, and the `search:runs` worker (the queue's consumer, a separate process like the
relay) claims and executes it. One provider failing never fails the run;
`search_runs.providers_succeeded` stays distinguishable from `providers_attempted`.

### 3.6b Provider control plane (v1 — built, step 8)

`src/imageshield/providers/` — the module that decides **whether to call a provider at all**. The
adapters in §3.6 know how to talk to a provider; this knows whether they are allowed to.

The whole of it is one ordered chain, run per provider at *dispatch* time in the worker:

```
ELIGIBILITY (whole request, in the route) → ENABLED → BREAKER → BUDGET → DISPATCH
```

Ordering is load-bearing: the cheapest and most absolute checks come first, so an eligibility refusal
never consumes budget or trips a breaker. Steps 2–4 never fail the run — a skipped provider is
recorded in `provider_calls` and stays in `providers_attempted` while being absent from
`providers_succeeded`, exactly like a timeout, because partial coverage must stay visible.

| Concern | Where | Notes |
|---|---|---|
| Guard chain | `gate.py` | The one place a pre-dispatch decision is made |
| Budget | `budget.py` | One indexed `provider_spend` row, before the call. Unknown cost + set budget → fails closed |
| Circuit breaker | `breaker.py` + `store.py` | 5 consecutive failures → open. 429 and zero-match 200s are **not** failures. Half-open probe claimed by a conditional `UPDATE`, so exactly one across N workers |
| Rate limiting | `ratelimit.py` | One shared bounded, jittered 429 driver for both adapters |
| Kill switch | `store.py` + `/v1/admin/providers/*` | `providers.enabled`, re-read within ≤30s. No deploy |
| Metrics + alarms | `observability.py` | Calls, cost, success rate, p50/p99, headroom, month-to-date |

Running the chain at dispatch rather than at request time is what makes a kill switch flipped *after*
enqueue still prevent the call.

Two subject-facing pieces sit alongside it. **`subjects`** (`subjects/`, migration 0008) is the first
table in this schema to parent a `user_ref`, and it carries the one flag that stops discovery running
for a minor — asserted once at enrolment, in the same transaction as the enrolment row. **Adaptive
cadence** (`search/cadence.py`) demotes seeds that keep coming back empty and promotes any seed with a
hit; it is the only cost lever here that reduces spend rather than capping it, and the tier is exposed
on the run-status response because a user must not be told a cadence they are not on.

The alarm that matters most is `no_successful_calls_24h`. A provider silently returning nothing looks
exactly like a quiet week for infringements, and an undetected outage means users are told they are
clear when nothing actually looked. Delivery of these alarms to CloudWatch is step 9.

### 3.6c URL recheck loop (v1 — built)

`src/imageshield/recheck/` — its own process (`python -m imageshield.recheck.worker`), polled rather
than queued: "which rows are due" is a question `infringements` answers directly.

`url_alive` existed from migration 0005 and nothing ever set it false, so every infringement was
permanently "live" and any count of live exposure equalled the total. A dead URL is also the only
unambiguously good news v1 can deliver — detection without takedown is otherwise
alerts-with-no-remedy, and *"this came down"* is the one purely positive thing the system can say.

| Status | Verdict |
|---|---|
| 404, 410 | **dead** — `url_alive = false` |
| 2xx, 3xx | alive |
| 401, 403 | alive — *gated, not gone* |
| 5xx, timeout, DNS failure, anything else | **unchanged** — not evidence of removal |

Only 404 and 410 mark a URL dead. Telling a victim their problem is fixed because a site was briefly
down is the wrong error to make, and the asymmetry runs the same direction as everywhere else here.
**Nothing ever deletes an infringement** — a dead URL is still evidence, and the user has already been
told about it.

Network posture matches §3.7's crop fetcher, and for the same reason — this probes hostile domains:
HEAD only (never GET; there is no `get` on the transport protocol to call), domain allowlist sourced
from `content_urls.source_domain`, SSRF guards applied **after** DNS resolution, 5s timeout, 2
redirects — with both guards re-applied to **every redirect hop**, since a guard on the first URL
alone is what makes `302 → 169.254.169.254` work. Per-domain rate limiting, because probing one site's
400 URLs in a burst is the traffic shape a scanner makes.

Two timestamps, deliberately: `last_checked_at` ("we learned something", exposed to the proxy) moves
only on a definite verdict, while `last_attempted_at` (migration 0013) moves on every probe and is
what the due-queue orders by — otherwise a permanently unreachable host pins the front of every batch
and the loop silently stops draining.

### 3.7 Crop fetcher

Its own deployable, on its own egress path, with **no VPC access to any internal service**.

- Domain allowlist sourced from `content_items.source_domain`
- SSRF guards applied **after** DNS resolution, not before
- 5s timeout, 20MB cap, 2 redirects
- Crops to `face_bbox` + 15% margin, returns the crop, discards everything else
- `Cache-Control: no-store, private`. No CDN, no disk, no temp file
- Runs on a read-only filesystem so a disk write fails loudly

The full image exists only as a local variable inside the fetcher process and is never returned to the
caller, even on error paths.

---

## 4. Data ownership

| Concern | Owner | Store |
|---|---|---|
| Auth, OTP, sessions, `users` | **Proxy** | Proxy tables / DynamoDB |
| Phone numbers, profile fields | **Proxy** | Proxy tables |
| Billing, Stripe, Apple IAP | **Proxy** | Proxy tables |
| Push tokens, notification delivery | **Proxy** | Proxy tables |
| Enrolment selfies (S3) | **Proxy** | S3 |
| Liveness sessions, enrolments, vectors | **Services** | Postgres (identity DB) |
| Consent records + signed artifacts | **Proxy** | Proxy tables + DocuSeal. Services hold only `enrolments.consent_ref` and the hash the proxy computed |
| Content index, candidates, search runs | **Services** | Postgres |
| Review queue and decisions | **Services** | Postgres |
| Reports, hits, recheck state | **Services** | Postgres |
| Report reads for the UI | **Proxy** | Postgres (read-only) |
| Pushing onto any queue | **Services** | SQS (via outbox) |

Services receive `user_ref` on every request and trust that the proxy has authorised the caller. **If the
user is wrong, the proxy is wrong.**

---

## 5. Storage model

Postgres, schema-per-module, distinct DB role per module. Full DDL in `SCHEMA.md`.

Three properties that are load-bearing:

**`enrolments` cannot exist without both a consumed liveness session and consent evidence.** The
session half is a composite FK pinned to `status = 'consumed'`; the consent half is three `NOT NULL`
columns — `consent_ref`, `consent_document_sha256`, `consent_signed_at` — supplied by the proxy and
written in the same transaction. The schema makes it structurally impossible to index a face without
both. The consent *document* stays in the proxy; we hold the reference (INVARIANTS #2).

**Every row that holds or references a vector carries `model_id`.** Vectors from different models are
not comparable, and a cosine similarity between an AdaFace vector and anything else is a plausible-
looking number that will quietly wreck your thresholds (INVARIANTS #4).

**No image bytes anywhere outside the identity module.** A schema lint test fails the build on any
column matching `image|thumbnail|blob|photo|local_path` in match, adjudication, or report.

---

## 6. Queues

Three SQS queues, Standard (not FIFO — ordering is irrelevant here and the FIFO throughput ceiling
isn't worth it). Each has its own worker and its own DLQ.

| Queue | Producer | Message | Latency budget |
|---|---|---|---|
| `identity:index` | Enrolment handler | `{enrolment_id}` | Seconds |
| `match:forward` | Ingest adapter | `{content_face_id}` | Minutes |
| `match:backfill` | `enrolment_complete` | `{user_id, page_from, page_to}` | Hours |

### The outbox is mandatory

SQS gives no transactional enqueue, so producers write to an `outbox` table **in the same transaction
as the domain row**, and a relay publishes and marks rows published.

Without it, three failure modes are live:

- Row committed, `SendMessage` fails → user enrolled, backfill never runs, **silently unmonitored**
- Message published, transaction rolls back → job references a row that doesn't exist
- Worker consumes before the write is visible → no-op or spurious failure

Delivery is at-least-once, so **every consumer must be idempotent**. `match_candidates` gets this from
`ON CONFLICT DO NOTHING` on `(user_id, content_face_id, model_id)`; `enrolments` from the unique index
on `(collection_id, external_face_id)`.

### Rules

- **Messages carry IDs, never payloads.** The worker re-reads from Postgres — the row may have changed
  since enqueue, and the stored value wins. Also keeps clear of the 256 KB message ceiling.
- **Backfill is chunked by page range.** A whole-index backfill can run for tens of minutes; if
  processing exceeds the visibility timeout the message is redelivered and N workers duplicate the
  same run. Short jobs make the timeout easy to size and let progress survive a worker dying.
- **Visibility timeout ≥ 3× measured p99 processing time** per queue, sized independently.
- **DLQ on every queue, with a depth alarm.** Nothing polls the DLQ; an unwatched gauge means
  permanently failing jobs are invisible.
- **Reconciliation sweep as the backstop.** A periodic job finds `content_items` with no covering
  `search_run` and `users` marked active with no completed backfill. This catches what the outbox
  misses and is the only thing that detects a silently unmonitored user.

---

## 7. Async completion — NOTIFY, don't poll

Services emit Postgres `NOTIFY` inside the writing transaction:

| Channel | Fired when | Proxy's response |
|---|---|---|
| `enrolment_complete` | Third pose indexed, user active | Update UI, stop showing "setting up" |
| `match_batch_complete` | A backfill run finishes | Refresh the report surface |
| `hit_created` | A new hit reaches a report | Queue a digest entry — **never an immediate push** |

The proxy holds one dedicated `LISTEN` connection on a **session-pinned** connection. A transaction-mode
pooler silently drops `LISTEN`; the listener must bypass it.

Treat `NOTIFY` as a wake-up only — read the authoritative state from the table. On listener reconnect,
re-query for rows newer than the last-seen watermark, because `NOTIFY` is fire-and-forget and the row
is the truth.

A backfill that finds **zero** matches is a normal and common outcome. Polling for the appearance of
hit rows cannot distinguish "still running" from "nothing will ever come."

---

## 8. What's deferred from v1

- Takedown / DMCA — detection only
- Minors under 18, guardian co-signature, the CSAM reporting pipeline
- Age assurance vendor (self-declared DOB, `DetectFaces` `AgeRange` as a soft flag)
- Non-partner corpus sources — social platforms, image hosts, search
- Self-hosted embedding stack (staying on Rekognition means rented embeddings and vendor-side
  thresholds; `enrolments.source_object_uri` is the migration path)
- Kafka, service mesh, Kubernetes, event sourcing, CQRS, gRPC

---

## 9. Open questions

- **The partner's embedding model.** Blocks the match module entirely. Determines whether
  `content_faces.embedding_ref` points into a vector store we run, whether matching goes through
  `SearchFacesByImage` behind the isolated fetcher, and which `model_id` values are legal.
- Recall on AI-generated faces. Nothing in this document solves it; the architecture exists to let us
  *measure* it honestly. A held-out eval set of known deepfakes of consenting test subjects should
  exist before the product is sold against a recall claim.
- Rekognition demographic accuracy disparities (NIST FRVT). A fairness exposure to measure rather than
  assume away, and one that can't be tuned around while the threshold is a single vendor-side number.

---

*This document evolves with the system. When the boundary or schema changes shape, update here in the
same PR.*
