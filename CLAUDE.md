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
- The **consent reference** on an enrolment — `consent_ref`, the proxy's hash, and the signing time.
  The record and the document are the proxy's; we hold a pointer and enforce that it exists.
- The content index, matching, candidates, search runs. *(specified, not yet built)*
- The review queue and adjudication decisions. *(specified, not yet built)*
- Reports, hits, recheck state, evidence export. *(specified, not yet built)*
- Producing onto all queues.

### What the proxy (other repo) owns

- Auth, OTP, sessions, `users`. **The proxy is the auth boundary.**
- Phone numbers, profile fields, billing, push delivery.
- **Consent: the records, the signed artifacts, and the DocuSeal relationship.** It has
  `profile.persons`, `profile.guardianships` with `subject_dob` triggers, and
  `profile.v_consent_eligibility` computing `required_signer_role` — the hard part, already built.
  It is also the only public ingress, so the webhook must terminate there. We hold a `consent_ref`
  and the hash *it* computed; we never render, fetch or hash the document, and we cannot determine
  who is required to sign.
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
1a. **Face search is permitted for attribution, and only in `attribution/`.** Matching a face in a
   *third-party photo* against a caller-supplied list of *already-enrolled* `user_ref`s cannot
   corrupt an identity: nothing is created or reassigned, and the worst case is a seed not
   registered. Five conditions attach, and the load-bearing one is that matches outside the
   candidate list are discarded **before they can influence anything** — that filter is what stops
   one user's photo becoming another user's monitored seed. Full text in `INVARIANTS.md` #1a.
1b. **One threshold per purpose, from config.** No inline literals. The 90/95/99 spread across call
   sites is what makes #1's failure fire.
2. **No enrolment without a passed liveness session and a consent reference from the proxy.**
   Enforced by the `UNIQUE` FK on `liveness_enrolments.session_id` and by 0010's three `NOT NULL`
   consent columns. The constraints do the work, not the application code. The consent *document*
   lives in the proxy — we hold `consent_ref` plus the hash it computed, and never compute one.
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
8. **Both age floors are read from config at request time.** There are two, and the split is
   load-bearing: `MIN_ENROLMENT_AGE = 13` (who may enrol — consent, guardianship, household seats)
   and `MIN_DISCOVERY_AGE = 18` (who may be *searched*). Neither inline. Boot refuses if discovery
   sits below enrolment.
8b. **Discovery never runs for an ineligible subject, and the refusal rests on data we own.**
   `subjects.discovery_eligible` is asserted once at enrolment from the proxy's required
   `subject_is_adult` field. `POST /v1/search` refuses first: no row → `409 subject_unknown`, false →
   `403 discovery_not_available`, **no `search_runs` row, no provider call, one `audit_log` row**.
   Minors enrol in v1; discovery for them would put CSAM in this pipeline, and screening/reporting are
   deferred — so nothing looks. Lowering `MIN_DISCOVERY_AGE` alone does not enable it: the mapping
   minor → ineligible is unconditional in code (`subjects/eligibility.py`).
9. **No image bytes persisted. Anywhere.** We store pointers into the proxy's S3, never payloads.
   The schema lint has three parts: (a) **no `bytea` column anywhere** — the real rule, since it
   catches the failure whatever the column is named; (b) a name gate on
   `/_(data|blob|bytes|b64)$|thumbnail|local_path/`; (c) `*_uri` and `*_url` explicitly allowed.
   `reference_image_uri` and `source_object_uri` must pass. Match on suffix and type, never on the
   substring "image". See `INVARIANTS.md` #9.
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
37. **Every pre-dispatch check runs in one place, in one order** —
   ELIGIBILITY → ENABLED → BREAKER → BUDGET → DISPATCH. The order is the invariant: the cheapest and
   most absolute come first, so an eligibility refusal never consumes budget or trips a breaker.
   Step 1 runs **twice**: in the route before any run row exists, and again on `claim_run` at
   dispatch, because the flag is mutable and a run can sit queued for minutes. Steps 2–4 are
   per-provider, in `providers/gate.py`, at *dispatch* time — which is what makes the kill switch bite
   on an already-enqueued run. Inside the chain the same rule applies recursively: the breaker's
   durable half-open **claim** happens last, after BUDGET, so a budget refusal cannot burn the one
   probe.
38. **The budget check happens BEFORE the call, off one pre-aggregated row.** `provider_spend` by
   primary key, never `SUM(provider_calls)`. A budget with an unknown `cost_per_call_usd` fails
   **closed** — an operator who asked for a cap must not get unbounded spend because we cannot price
   the calls. The accumulator's scale must be ≥ the price's scale (`NUMERIC(14,6)` against a
   `NUMERIC(10,6)` price): the upsert coerces each increment before adding, so a narrower column
   rounds every call and, under ~0.00005/call, never grows at all — a silent fail-open in the one
   check that must fail closed.
39. **Spend, the call row, and the breaker transition commit together**, under a row lock on
   `providers`. A rolled-back call records no spend; the lock stops N workers each needing N failures
   to open one breaker. Likewise **run completion and the cadence update commit together**: a
   completed run is unclaimable, so a crash between them loses the tier change with no retry path.
40. **A breaker opens on brokenness, never on an ordinary result.** Failure = timeout, 5xx,
   connection error, malformed response. Not 429, not a 200 with zero matches, not any skip.
   Half-open allows exactly one probe, claimed by a conditional `UPDATE` in Postgres. **The breaker
   has no terminal state**: an inconclusive probe (a 429 — neutral, not a verdict) returns it to
   `open` with the clock restarted and the cooldown *not* doubled, and a probe abandoned by a dead
   worker is reclaimed after cooldown + grace. A `half_open` row nothing can leave is a provider
   skipped forever, i.e. permanent silent partial coverage. A failure arriving while already open is
   counted but never re-doubles the cooldown or re-alarms.
41. **A skipped provider is recorded, never silent.** `budget_exceeded` / `breaker_open` /
   `provider_disabled` land in `provider_calls`, and the provider stays in `providers_attempted` while
   absent from `providers_succeeded` — like a timeout. §7.5 again.
42. **Tiering is never silent.** `scan_tier` and `next_scan_after` are on the run-status response so
   the proxy can state a user's real cadence. A run where no provider succeeded never changes the
   tier — it produced no evidence, and demoting for our own outage takes the saving from the wrong
   place. The counter is read under `FOR UPDATE`: two overlapping runs on one seed must not both read
   the same count and both write `count + 1`.
43. **A run refused at dispatch is `refused`, never `completed`.** A completed run with zero results
   reads as "we looked and found nothing", which about a search that never ran is a false
   reassurance. Same reasoning as #8b's "no run row at all" for a refusal at the route.

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
| URL normalisation + dedup | Digests |
| Cost tracking + circuit breakers | Partner ingest adapter |
| Subject eligibility (step 8) | CSAM screening + mandatory reporting |
| Adaptive cadence mechanism | The scheduler that reads `next_scan_after` |
| Infringement feedback (`not_me`) | |
| URL recheck loop (`url_alive`) | |

Two notes on that right-hand column. **CSAM screening and reporting are what gate minor
discovery** — `MINOR_DISCOVERY_SUPPORTED` stays `False` until both exist, and flipping it without them
raises. And **"recheck" now means two different things — do not confuse them:**

| | What it does | State |
|---|---|---|
| **URL recheck loop** (`src/imageshield/recheck/`) | HEADs each infringement's `page_url` weekly and sets `url_alive` false on a 404/410 | **Built** (out-of-band task 03) |
| **Cadence scheduler** | Reads `next_scan_after` and triggers a new *search* | **Not built.** Step 8 writes and exposes `scan_tier` / `next_scan_after`; nothing reads them to trigger anything |

An earlier version of this doc called the cadence scheduler "the recheck loop", which is why the two
are spelled out. They share no code.

The build spec for what's in scope is `NEAR-TERM-BUILD.md`. It is the authoritative task list.

**The proxy must add `user_ref UUID` to its user record and backfill it before any of this runs.**
One column. Without it the only identifier available is a phone number and rule §3.2 collapses on day
one.

---

## 7. Search provider rules

The adapter layer is where this repo's near-term value lives. Get these wrong and adding a third
provider becomes a re-audit rather than a config change.

### 7.1 Face search and image search are different products

- **Image search** (TinEye, Google Vision Web Detection, Hive **Web Search** — reverse image search
  over ~25B indexed images) finds *this image* and near-duplicates.
- **Face search** (PimEyes-class) finds *this person* in images that never existed before.

A deepfake is a novel image. Pixel-wise it has no relationship to anything the user uploaded. Image
search returns nothing for it, silently, and the scan looks clean. **Our product promise is about
AI-generated likeness abuse, which is face search almost exclusively.** Every adapter declares its
`kind` and the orchestrator knows the difference.

**Hive naming trap:** our Hive product is **Web Search**. Hive's separately-named "Media Search"
matches movies and TV content — not ours. Which product a key hits is determined by **the Hive
project the key belongs to, not the URL** — both go to `POST {HIVE_BASE_URL}/api/v2/task/sync`. A
key provisioned against the wrong project returns plausible-looking wrong results rather than an
error; the adapter treats a 200 without the Web Search `matches` path as `error`, never as "no
matches found".

### 7.2 Adapters return raw scores. They never normalise

Provider A's 0.92 and Provider B's 0.92 are different quantities with different distributions.
Calibration is a separate, versioned, config-driven step — because if adapters normalise, you cannot
recalibrate without redeploying.

Store `raw_payload` verbatim on every attestation. It is the only way to recalibrate historical
results when a provider retunes.

### 7.3 Uncalibrated providers reach `review` band only

Never `auto_confirm`, and never `drop` either — a real infringement in `drop` is invisible to the
user forever, which is the worse of the two edges. A provider we haven't measured against a labelled
set must not be able to tell someone their face is in porn without a human looking first.

Two keys move a provider off review-only, and they defend different failures.

`calibrate activate` enforces a floor **recomputed from `eval_observations`**, never from the stored
`measured` column — a check that trusts a JSONB column an operator can type into is defeated by
editing a number. It refuses unless: precision ≥ 0.99 on any declared `auto_confirm` band, NPV ≥ 0.99
on any declared `drop` band, effective sample size (non-`uncertain` items) ≥
`CALIBRATION_MIN_EVAL_ITEMS`, at least one `lookalike` item, a non-null `eval_set_id`, and every seed
covered by a successful provider run. A review-only config skips the floor — it alarms nobody. The
floor lives in code so loosening it costs a code change, a review, and a `git blame`.

`calibrate trust` is separate, human, and the only writer of `providers.calibrated`. *This config is
sound* and *this provider may alarm people unreviewed* are different claims. The first is arithmetic
and the floor checks it. The second is judgement about whether the eval set resembles the real world
— a sweep over 40 items with no hard negatives yields precision 1.0 trivially, because random
negatives are easy to reject. No code can check that.

### 7.4 One infringement, many attestations

The same URL found by three providers is **one hit with three attestations**, not three hits. Dedup on
a normalised `url_hash`. `provider_count` is an agreement signal and it is useful; three independent
providers agreeing is meaningfully different from one.

### 7.5 Partial coverage must be visible

`search_runs.providers_succeeded` exists because a silent provider outage otherwise looks identical to
"no infringements found." That distinction matters more here than in most systems.

### 7.6 Cost is a first-class concern — **built** (step 8)

Per-provider daily budget enforced **before** dispatch, circuit-breaking when exceeded. Per-provider
kill switch in config, hot-reloadable. One provider failing never fails the run. Log `cost_usd` per
run — N providers × M users × weekly cadence multiplies fast.

The mechanism lives in `src/imageshield/providers/`: `gate.py` (the guard chain), `budget.py`,
`breaker.py`, `ratelimit.py` (one shared bounded-jittered 429 driver, so Hive and Google cannot drift
apart on retry policy again), `store.py` (the one transaction), `observability.py`. Invariants #37–#42.

Two things to know before touching it:

- **`hive.cost_per_call_usd` is NULL and that is deliberate.** Hive Web Search is contract-priced and
  no measured figure exists in this repo. Google's is list price (0.003500). A budget set without a
  cost fails closed, so filling Hive's in is a prerequisite for capping Hive's spend — not a nicety.
- **`monthly_budget_usd` is reported, not enforced at dispatch.** The dispatch guard is one indexed
  row by design; a month is a range scan. Month-to-date is an admin read.

### 7.8 The lever that actually reduces cost is cadence, not capping

Budgets and breakers control spend. Adaptive cadence (`search/cadence.py`) reduces it — weekly →
fortnightly after 8 empty scans → monthly after 20, with any non-empty scan promoting to weekly
`priority`. That is a 4–10× reduction across a realistic population.

It is also the piece with a user-facing safety consequence, so invariant #42 applies: the tier and
`next_scan_after` are exposed on the run-status response and the proxy must tell the user the truth
about their cadence.

One stated v1 approximation: the brief defines `priority` as "any confirmed infringement in 90 days",
and *confirmed* means adjudicated, which is not built. v1 promotes on any non-empty scan and releases
after `SCAN_PRIORITY_RELEASE_AFTER_EMPTY` (13 ≈ 91 days at the weekly priority cadence). Replace the
release rule with a query against confirmed infringements when the review queue lands; the promotion
rule stays.

### 7.7 Check the terms before writing the adapter

Several reverse-face-search providers explicitly prohibit third-party monitoring services. Worth
finding out before the integration is built rather than after.

---

## 8. Build order (this repo)

**This numbering is canonical.** `BUILD-PROMPT-v1.md` groups the same work into 5 coarser phases
(its Phase 3 = steps 3–4, its Phase 4 = steps 5–8, its Phase 5 = step 9). When they disagree on
granularity, follow this list and say which step you are on.

1. Repo scaffold, config validation at boot, service-token auth, migrations in CI.
2. Migration 0001 + schema lint (invariant #9) + transactional outbox and relay.
   Verify the lint by adding `photo bytea` and watching the build fail — **not**
   `thumbnail_uri`, which is a legitimate column name under #9(c).
3. Liveness session lifecycle + Rekognition integration.
4. Enrolment from `ReferenceImage`, `DeleteFaces` path.
5. Provider adapter interface + first adapter.
6. URL normalisation, dedup, attestations.
7. Calibration harness + banding.
8. Cost tracking, circuit breakers, kill switches.
9. **Infrastructure and CI.** IaC for SQS + DLQs, the `identity-v1` collection, per-module DB roles,
   and an IAM role with **no `s3:*` permissions**. Blocking CI gates: mypy strict, schema lint,
   route-auth coverage, and greps for `SearchFacesByImage` and any S3 client. Plus
   `docs/OPERATIONS.md`.
   The face-search grep is **two** gates, not one, and must be built that way rather than built wide
   and narrowed later: a hard ban over the enrolment path (`liveness/`, `enrolment/`, `subjects/` and
   their routes), and a ban everywhere else in `src/` **except `attribution/`** — see §4 #1a. The S3
   grep stays whole-`src/`; there is no exemption to that one. `tests/test_boundaries.py` already
   enforces both shapes, so the CI step mirrors it rather than inventing its own scoping.

Step 9 is not optional and not last-by-importance. The IAM grant is one of only three places the
data boundary is enforced by something other than discipline — the other two are the schema lint
(step 2) and the structlog redaction processor (step 1).

Do not start step 5 before step 4 is verified end-to-end on a real device.

Spike work — throwaway harnesses, vendor evaluation, credential and region checks — belongs in
`devtools/`, outside the numbered steps. It does not advance the build order, and it should not be
reported as a step being complete.

---

## 9. API contract with the proxy

Full detail in `PROXY_INTEGRATION.md`. The shape:

- Every endpoint requires `X-Service-Token`. `/v1/admin/*` additionally requires
  `X-Admin-Service-Token`. The two values must differ — we refuse to boot if they match.
- Every request carries `user_ref`. Never a phone, never a user object.
- `Idempotency-Key` on `POST /v1/liveness/:id/result`. **Not** on
  `POST /v1/liveness/sessions` — that creates a provider session and burns an attempt.
- `subject_is_adult: bool` is **required** on `POST /v1/liveness/:id/result` (step 8). No default;
  absent is `400 subject_eligibility_required` and writes nothing. See §4 #8b.
- `POST /v1/search` can refuse the whole request: `409 subject_unknown` / `403
  discovery_not_available`. No `search_runs` row, no provider call, one `audit_log` row.
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