# CLAUDE.md — ImageShield Services

This file is the operating manual for any contributor — Claude Code or human — working in **this
repo**. Read it before touching code.

> **Repo scope:** This repo is the **ImageShield services only**. The proxy
> (`ImageShieldPhotoShare`, Node/Express) lives in a **separate repo**. We do not edit proxy code
> here. We define the contract with the proxy and stay on our side of it.

---

## 1. Product context

ImageShield monitors how a person's likeness is used online and tells them when their face appears
somewhere they didn't consent to — deepfakes, AI-generated pornography, impersonation, likeness theft.

This is **victim-facing safety infrastructure**, not a search product. Our peer set is StopNCII.org,
NCMEC's Take It Down, Loti, and Ceartas — not reverse image search. Several rules flow from that:

- A **false positive is a serious harm**, not a UX annoyance. Telling someone their face is in porn
  when it isn't is a psychological injury, and if we show them the image to prove it, we've either
  shown a victim a fake of themselves or shown them someone else's abuse material.
- A **false negative is a broken promise**. Users read "no matches" as "I'm safe." We never say that.
  Every report states its scope: *"no matches in monitored sources."*
- We do **not** build search-by-arbitrary-face. A system that takes any face and returns where it
  appears online is a stalking tool. That's Clearview, and the Dutch DPA fined them ~€30.5M for it.
- We do **not** store image bytes of infringing content. Hashes, embeddings, URLs, and bounding boxes
  only. A database of NCII indexed by identity is the highest-value breach target imaginable.
- We do **not** show full images. Face crops, rendered live, blurred by default, revealed on tap.
- We do **not** do takedown in v1. Detection only. The product must say so in onboarding, in plain
  words — not buried in a ToS.
- We do **not** serve users under 18 in v1. A hit on a minor is CSAM, which is a mandatory-reporting
  pipeline we haven't built. The age floor is config, not a constant.
- Notifications are **batched digests**, never real-time, never 22:00–08:00 local. "New match found"
  at 2am is a harm. Cadence here is a safety decision, not a growth lever.

---

## 2. Tech stack (this repo)

Deliberately mirrors the Flashback agent service so the scaffolding, CI, and deploy patterns are
copyable rather than re-derived.

| Concern | Choice |
|---|---|
| Language / runtime | Python ≥3.11 |
| HTTP | FastAPI + uvicorn |
| Datastore | Postgres 16, `psycopg[binary,pool]` 3.x, raw SQL — no ORM |
| Validation | pydantic 2 at every boundary |
| Logging | structlog |
| AWS | boto3 |
| HTTP client | httpx |
| Liveness | AWS Rekognition Face Liveness |
| Face index | AWS Rekognition collections (`identity-v1`) |
| Queues | AWS SQS — `identity:index`, `search:runs` — with a transactional outbox |
| Tests | pytest, pytest-postgresql, pytest-asyncio, httpx |
| Local dev | docker compose — Postgres + LocalStack (SQS) |

Not used here, unlike Flashback: pgvector (not until we own embeddings), Valkey (no working-memory
concept), and any LLM SDK — see §10.

External dependencies we **call** but do not own: the proxy (REST), Rekognition, Hive, Postgres, SQS.

We hold **no AWS S3 credentials**. If they aren't in the environment, the mistake can't be made.

---

## 3. Service boundaries — what's ours, what isn't

### What this repo owns

- Liveness session lifecycle and the provider relationship.
- Enrolment: quality gate, `IndexFaces`, the `identity-v1` collection, `DeleteFaces`.
- Consent records and their versioned artifacts.
- The content index, matching, candidates, search runs. *(specified, not yet built)*
- The review queue and adjudication decisions. *(specified, not yet built)*
- Reports, hits, recheck state, evidence export. *(specified, not yet built)*
- Producing onto all queues.

### What the proxy (other repo) owns

- Auth, OTP, sessions, `users`. **The proxy is the auth boundary.**
- Phone numbers, profile fields, billing, push delivery.
- All S3 buckets, all credentials, all object lifecycle.
- All user-facing reads for the report UI (read-only Postgres access to our schemas).

### Hard rules

1. **The client never talks to us.** Every call is Client → Proxy → Services. No CORS surface, no
   public ingress, no per-user auth. We live on a private subnet reachable only from the proxy.
2. **We never see a phone number.** Every request carries `user_ref`, an opaque UUID minted by the
   proxy. Not in a column, not in a log line, not in an `ExternalImageId`. This is the boundary, not
   a style preference.
3. **We never write to S3 directly.** The proxy mints presigned PUT/GET URLs; we read or write through
   them and discard the bytes. Same pattern as the AgentMeeMaw tribute renderer.
4. **We have no user model.** We accept `user_ref` and trust the proxy authorised the caller. If the
   user is wrong, the proxy is wrong.
5. **`server.js` is read-only reference, never a source to port from.** It is 16,206 lines, 154 routes,
   one file, serving two products, with one authenticated route that throws. Read it to learn what a
   feature did. Never copy its logic.
6. **Ask before** adding a fourth queue, a new deployable, a new external dependency, or any
   cross-boundary read/write.

---

## 4. Invariants — the working set

`INVARIANTS.md` is authoritative and covers modules not yet built. These are the ones that apply to
code being written **now**, carrying the same numbers.

1. **Identity never comes from a similarity score.** `user_ref` comes from the request. There is no
   code path where a face search result determines who someone is. The old system's
   `processFaceRecognition` (`server.js:9585`) does exactly this with thresholds varying 90/95/99 —
   that's the fragmentation bug. `SearchFacesByImage` must not appear in the enrolment path.
1b. **One threshold per purpose, from config.** No inline literals. The 90/95/99 spread across call
   sites is what makes #1's failure fire.
2. **No enrolment without a passed liveness session.** Enforced by the `UNIQUE` FK on
   `liveness_enrolments.session_id`. The constraint does the work, not the application code.
3. **Liveness sessions are single-use, 10-minute TTL.** Once an enrolment references one, it's
   consumed. Replay returns `410`.
4. **Never mix vectors or scores from different models.** Every row carries `model_id`. A similarity
   between two models is a plausible-looking number that will quietly wreck your thresholds.
5. **`QualityFilter: HIGH` on every `IndexFaces`.** Not `AUTO`. A poor enrolment vector degrades every
   match that user will ever get, permanently, and they have no way to know.
6. **`ExternalImageId` is `user_ref` and nothing else.** It comes back in every search response and
   lands in logs.
7. **Deletion calls `DeleteFaces` first, verifies absence, then tombstones.** If the tombstone
   succeeds and the Rekognition call fails, the face stays searchable with no record pointing at it.
   The old repo calls `DeleteFaces` nowhere, under a comment claiming BIPA compliance.
8. **`MIN_ENROLMENT_AGE` is read from config at request time.** v1 ships at 18. v2 lowers it. That
   must not be a code change.
9. **No image bytes persisted outside the identity module.** A schema lint test fails the build on any
   column matching `image|thumbnail|blob|photo|local_path`. If you're tempted to add one, you're
   solving the wrong problem.
19. **Nothing reaches a user from the `review` band without a human decision.** There is no timeout
   that auto-promotes. If the queue backs up, the queue backs up.
21. **Scores are computed server-side, once.** The old system computes one client-side and one
   server-side that disagree by −18 per active report. For a product whose entire output is a score,
   that is disqualifying.
26. **Every report states its scope.** The corpus is a partner-supplied set of known sites. Absence is
   reported as "no matches in monitored sources," never "you're safe." The marketing copy says
   "across the web"; the system does not do that.
33. **A failed index job never blocks the write that triggered it.** Enqueue, retry with backoff,
   surface as pending. The user sees "still setting up," not a 500.
34. **Partner ingest is pluggable, behind an interface.** One partner is a single point of failure.
   Do not hardcode their response shape.

---

## 5. Schema rules

- All PKs are `UUID DEFAULT gen_random_uuid()`. No guessable identifiers in URLs, S3 paths, or logs.
- All timestamps `TIMESTAMPTZ`, never `TIMESTAMP`.
- Soft delete via `status` + `superseded_by`. Never `DELETE` — biometric enrolments are expensive to
  recreate.
- Every derived row carries provenance. You must be able to trace a hit back to the search that
  produced it and the decision that confirmed it.
- Every vector-bearing or vector-referencing row carries `model_id`, and it appears in the `WHERE`
  clause of every query against it.
- Migrations are versioned, reversible, and run in CI. Not a folder of `.sql` files applied by hand.

Full DDL in `SCHEMA.md`.

---

## 6. Current scope — read this before building anything

**v1 of this repo is liveness + third-party search provider integration. Nothing else.**

`ARCHITECTURE.md` describes the full system, including a match module, an adjudication queue, a report
surface, and a crop fetcher. **Those are specified, not built, and not in scope.** Do not build them
because the architecture doc describes them.

| Build now | Specified, do not build yet |
|---|---|
| Liveness sessions + enrolment | Match module (forward + backfill) |
| `DeleteFaces` path | Adjudication queue and reviewer tooling |
| Provider adapter interface | Report surface, hits, evidence export |
| Score calibration + banding | Crop fetcher deployable |
| URL normalisation + dedup | Recheck loop, digests |
| Cost tracking + circuit breakers | Partner ingest adapter |

The build spec for what's in scope is `NEAR-TERM-BUILD.md`. It is the authoritative task list.

**The proxy must add `user_ref UUID` to its user record and backfill it before any of this runs.**
One column. Without it the only identifier available is a phone number and rule §3.2 collapses on day
one.

---

## 7. Search provider rules

The adapter layer is where this repo's near-term value lives. Get these wrong and adding a third
provider becomes a re-audit rather than a config change.

### 7.1 Face search and image search are different products

- **Image search** (TinEye, Google Vision Web Detection) finds *this image* and near-duplicates.
- **Face search** (PimEyes-class) finds *this person* in images that never existed before.

A deepfake is a novel image. Pixel-wise it has no relationship to anything the user uploaded. Image
search returns nothing for it, silently, and the scan looks clean. **Our product promise is about
AI-generated likeness abuse, which is face search almost exclusively.** Every adapter declares its
`kind` and the orchestrator knows the difference.

### 7.2 Adapters return raw scores. They never normalise

Provider A's 0.92 and Provider B's 0.92 are different quantities with different distributions.
Calibration is a separate, versioned, config-driven step — because if adapters normalise, you cannot
recalibrate without redeploying.

Store `raw_payload` verbatim on every attestation. It is the only way to recalibrate historical
results when a provider retunes.

### 7.3 Uncalibrated providers reach `review` band only

Never `auto_confirm`. A provider we haven't measured against a labelled set must not be able to tell
someone their face is in porn without a human looking first.

### 7.4 One infringement, many attestations

The same URL found by three providers is **one hit with three attestations**, not three hits. Dedup on
a normalised `url_hash`. `provider_count` is an agreement signal and it is useful; three independent
providers agreeing is meaningfully different from one.

### 7.5 Partial coverage must be visible

`search_runs.providers_succeeded` exists because a silent provider outage otherwise looks identical to
"no infringements found." That distinction matters more here than in most systems.

### 7.6 Cost is a first-class concern

Per-provider daily budget enforced **before** dispatch, circuit-breaking when exceeded. Per-provider
kill switch in config, hot-reloadable. One provider failing never fails the run. Log `cost_usd` per
run — N providers × M users × weekly cadence multiplies fast.

### 7.7 Check the terms before writing the adapter

Several reverse-face-search providers explicitly prohibit third-party monitoring services. Worth
finding out before the integration is built rather than after.

---

## 8. Build order (this repo)

Per `NEAR-TERM-BUILD.md`:

1. Repo scaffold, config validation at boot, service-token auth middleware, migrations in CI.
2. Schema lint test (invariant #9) — verify it fails when you add a `thumbnail_uri` column.
3. Liveness session lifecycle + Rekognition integration.
4. Enrolment from `ReferenceImage`, `DeleteFaces` path.
5. Provider adapter interface + first adapter.
6. URL normalisation, dedup, attestations.
7. Calibration harness + banding.
8. Cost tracking, circuit breakers, kill switches.

Do not start step 5 before step 4 is verified end-to-end on a real device.

---

## 9. API contract with the proxy

Full detail in `PROXY_INTEGRATION.md`. The shape:

- Every endpoint requires `X-Service-Token`. `/admin/*` additionally requires
  `X-Admin-Service-Token`. The two values must differ — we refuse to boot if they match.
- Every request carries `user_ref`. Never a phone, never a user object.
- `Idempotency-Key` on `POST /v1/liveness/:id/result`. **Not** on
  `POST /v1/liveness/sessions` — that creates a provider session and burns an attempt.
- Presigned URLs from the proxy must live ≥15 minutes; we retry.
- Completion is signalled by Postgres `NOTIFY`, not polling. The proxy holds a session-pinned
  `LISTEN` connection and treats the notify as a wake-up, reading authoritative state from the table.

---

## 10. Conventions

- **Source of truth:** `ARCHITECTURE.md` for system shape, this doc + `INVARIANTS.md` for rules,
  `SCHEMA.md` for tables, code for behaviour. Update them in the same PR.
- **Code over LLM.** There is almost no LLM in this system. Detection, embedding, search, thresholding,
  banding, dedup — all deterministic code and small vision models. If an LLM ends up in the matching
  path, something has gone wrong. Its legitimate places are the periphery: drafting takedown notices,
  summarising a report into plain language.
- **Docs we maintain:** `CLAUDE.md` (this), `ARCHITECTURE.md`, `SCHEMA.md`, `INVARIANTS.md`,
  `NEAR-TERM-BUILD.md`, `PROXY_INTEGRATION.md`, `MIGRATION-MAP.md`.
- **Typed identifiers at every boundary.** `UserRef = NewType("UserRef", UUID)`, plus `SessionId`,
  `ProviderId`, `UrlHash`. `NewType` gives static discipline; **pydantic models give the runtime
  enforcement that actually matters**, because a compile-time type says nothing about a payload that
  arrived over HTTP. Every inbound body is a pydantic model with `extra='forbid'`. No function takes a
  bare `str` for an identifier. This is the structural fix for the fifteen reimplementations of phone
  normalisation in the old system.
- **Run mypy or pyright in CI, strict.** `NewType` is worthless without it.
- **Every consumer is idempotent.** SQS is at-least-once and the outbox makes duplicates normal, not
  exceptional.
- **Messages carry IDs, never payloads.** Workers re-read from Postgres; the stored row wins.
- **Tests that must exist and never be deleted:** two lookalike faces enrolling as two distinct
  `user_ref`s; a photo-of-a-photo failing liveness; a consumed session returning `410`; enrolment
  using `ReferenceImage` rather than a separately uploaded selfie.

---

## 11. When in doubt

- Re-read §3 (boundaries) and §4 (invariants).
- Check §6 before building — most of `ARCHITECTURE.md` is not in scope yet.
- If a requirement seems to need one user seeing another user's matches, it's wrong. Raise it before
  building.
- If a deadline forces a shortcut, take it in the **presentation** layer. Never in the identity
  binding or the dedup key — those are the two things that are expensive to unwind.
- The open blocker across the whole project: **we do not know what model produces the partner's
  embeddings.** Nothing in v1 scope depends on it. The match module cannot start without it.