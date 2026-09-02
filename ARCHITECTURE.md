# ARCHITECTURE.md — ImageShield Services

System-level reference for the ImageShield services repo. Companion to `CLAUDE.md` (operating manual),
`SCHEMA.md` (table-level detail), `INVARIANTS.md` (the hard rules), and `PROXY_INTEGRATION.md` (the
handoff brief for the proxy).

> **Repo scope:** This is the **services** repo. The proxy (`ImageShieldPhotoShare`, Node/Express) is
> a separate repo and remains the user-facing surface — login, OTP, sessions, profile, billing. We
> document the full system here so our contract surface is clear, but only the service components are
> implemented here.

> **⚠ Specified ≠ in scope.** This document describes the whole system. **v1 of this repo is liveness
> + third-party search provider integration only.** The match module and the partner ingest adapter
> are designed here but **not built**. The crop fetcher (§3.7) and a minimal adjudication queue
> (§3.4/§3.8) were pulled into scope by the 2026-08-19 protection-score push, alongside four pieces
> this document did not originally describe at all — the confirm pipeline, the score engine, threat
> events, and the control-room console (§3.8–§3.11, the console **retired** 2026-08-29 — see §3.11).
> See `CLAUDE.md` §6 for the current scope table
> and `NEAR-TERM-BUILD.md` for the v1 task list. Do not build the match module or partner ingest
> because this document describes them.

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
Adjudication (human-in-loop), and Periodic (recheck + digest). The 2026-08-19 protection-score push
adds a sixth, the **Confirm** loop (async, `confirm:hits`) — §3.8 — sitting between a completed search
run and the adjudication loop, plus a continuously-recomputed **score** that reacts to all of the
above (§3.9).

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

### 2.4 Adjudication loop (human) — **built, minimal, as of 2026-08-19**

Originally specified against the (still unbuilt) match module's `review`-band candidates. What
actually ships against the provider-search pipeline is narrower and described in §3.8/§3.10: a
review-band infringement's most-similar hits are enqueued to `confirm:hits`, machine-triaged
(severity, face-match, pHash dedup, moderation), and land in `review_tasks` for a human — reached via
the panel, through the backend's `/v1/admin/*` proxy (§3.11: the control-room console that used to
serve this was retired 2026-08-29). The reviewer sees a **face crop only**, rendered live by the crop
fetcher, and decides `confirmed` / `rejected` / `uncertain`.

There is no timeout that auto-promotes a review-band candidate or a triaged hit (INVARIANTS #19, #47).
If the queue backs up, the queue backs up.

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

### 3.4 Adjudication module — **built, minimal (2026-08-19)**

The shape actually built is `src/imageshield/review/` — see §3.10 for the full description. This
paragraph's original claim (review queue, reviewer tooling, decisions, crop-only display, reviewer
welfare as a budgeted operating cost) all held true of what shipped; only the schema underneath it
changed from the match-module sketch in `SCHEMA.md` §3 to the confirm-pipeline shape in `SCHEMA.md`
§2d.

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

### 3.6d Attribution (v1 — built)

`src/imageshield/attribution/` — the only module permitted to call face search (INVARIANTS #1a).

Hive is image search: it finds *the image*, reposted or altered. The enrolment `ReferenceImage` is a
selfie taken thirty seconds earlier that nobody has ever reposted, so searching it finds nothing,
correctly, forever. The seeds that matter are the social photos screen 16 asks for, and attribution is
what says which enrolled person a photo should be a seed *for*. Without it, monitoring runs perfectly
and reports nothing.

**The face is the unit, not the photo.** One enrolled face among strangers is a valid seed for that
person; two enrolled household members produce two independent seeds; a face matching nobody is
ignored, which is the common case and not an error.

Household scoping is a **result filter, not a search parameter** — `SearchFacesByImage` accepts only
`CollectionId`, `Image`, `FaceMatchThreshold`, `MaxFaces` and `QualityFilter`, so the search runs
against the whole collection and non-candidates are discarded in `resolve.py` before they can
influence anything. That filter is the only thing between "a stranger outranked the household member"
and "person A's photo became person B's seed", which is why it is pure, isolated, and tested with a
planted non-candidate that outscores the real one.

**Each face is isolated by cropping before it is searched.** `SearchFacesByImage` "first detects the
largest face in the image", so three calls on one photo would otherwise search the same face three
times. `crop.py` cuts the bbox plus a 25% margin — enough surrounding head for the embedding to be
good, tight enough that a neighbour does not become the largest face in the crop and get attributed
instead — clamping boxes that Rekognition projects past the frame edge. That crop is the only reason
Pillow is a dependency, and it is used nowhere else.

Rekognition takes `Bytes` or `S3Object` and has no URL form, so the photo is read through the proxy's
presigned GET into memory and discarded — the same shape as the enrolment path, and subject to the
same rule: bytes in memory are fine, bytes on disk or in a column are not (INVARIANTS #9). There is
no S3 client.

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

### 3.7 Crop fetcher — **built** (2026-08-19)

Its own deployable (`src/imageshield/fetcher/`, `uvicorn imageshield.fetcher.app:create_app`, port
8083), on its own egress path, with **no VPC access to any internal service** and **no database
credentials of any kind** — `FetcherConfig` has no `database_url` field and cannot be given one without
a code change, which is the cheapest way to make the "no internal access" property hold even under a
future misconfiguration.

- Domain allowlist sourced from `content_items.source_domain` / `content_urls.source_domain`
- SSRF guards applied **after** DNS resolution, not before, on every redirect hop
- 5s timeout, 10MB cap (`fetch_max_bytes`), 2 redirects
- `POST /v1/fetch` returns raw bytes to the confirm worker (transient, in-memory only); `POST /v1/crop`
  returns **the whole frame, blurred end to end** — long edge capped at 1024px, Gaussian radius a
  fraction of that edge (floored), JPEG quality 80 — and `blur=false` sharpens **only** the
  `face_bbox` region (+8% margin) over that blurred base. **No parameter returns a fully sharp
  frame.** *(Changed 2026-09-02, spec
  `docs/superpowers/specs/2026-09-02-whole-frame-blur-design.md`; previously a `face_bbox` + margin
  crop blurred at a constant radius 12. The radius became proportional because a constant tuned for
  a crop is nearly transparent over a full frame, and the 1024 cap keeps the proxy's buffered relay
  honest.)* The pixel work lives in `fetcher/render.py`, not the route module
- `Cache-Control: no-store, private`. No CDN, no disk, no temp file
- Both routes are gated on `X-Fetcher-Token` (`hmac.compare_digest`); `GET /health` is not, matching
  every other deployable's rule that a load-balancer probe cannot carry a secret

The full image exists only as a local variable inside the fetcher process and is never returned to the
caller, even on error paths. Two jobs, one process: hand the confirm worker bytes, and render blurred
face crops live — the same isolated path serves both callers, so there is exactly one place in the
whole system that touches a hostile image byte. *Callers, as of 2026-08-21:* the confirm worker
(`/v1/fetch`) and the services API's subject preview endpoint (`/v1/crop`). The console's crop route
was **removed** with the subject-verified-hits decision — staff never see hit imagery.

### 3.8 Confirm pipeline — **built** (2026-08-19)

`src/imageshield/confirm/` — a worker (`python -m imageshield.confirm.worker`) consuming `confirm:hits`
(the third application queue, via the outbox — same idiom as `identity:index` and `search:runs`;
messages carry IDs only, the worker re-reads Postgres). Triggered after a search run's attestations
land: **every** still-unconfirmed infringement the run touched is enqueued (spec 2026-08-21 §1 — the
per-provider "most similar" criteria were removed, because the subject's crop-and-decide surface
starves on any hit that never triages; the `rekognition_confirm` provider row's budget/breaker/kill
switch is the spend control).

Per hit, in order:

1. **Fetch** the image via the fetcher deployable (§3.7). Unfetchable → triage `unfetchable`
   (`unassessed` severity), the hit stays reviewable URL-only; retries with backoff, then DLQ. A
   failed confirm never blocks anything else (INVARIANTS #33's spirit).
2. **Perceptual hash** — `confirm/phash.py`, a 64-bit dHash via Pillow, stored as `infringements.phash
   BIGINT` (SCHEMA.md §2d; INVARIANTS #9 unaffected — a hash, not bytes). If it matches, within a
   Hamming-distance threshold, a prior image **for the same user** that **a human already decided**,
   the new URL inherits that decision as `duplicate_of`: no re-review, no second score movement. This
   is the cross-run, cross-URL "same picture, we already answered this" case, and the dedup lookup
   never crosses users (`tests/test_confirm_store.py::test_decided_phashes_is_isolated_per_user`).
3. **Face-match** through `attribution/` — never a direct `SearchFacesByImage` call — against
   `identity-v1`, candidate list = the hit's own owner only. Recorded as a triage score, never an
   identity (INVARIANTS #1/#1a hold; the CI face-search grep gate is unchanged: `confirm/` imports
   `attribution/`, it does not call face search itself).
4. **Moderation** — Rekognition `DetectModerationLabels` on the same in-memory bytes. Labels stored as
   text (`infringements.moderation_labels JSONB`); pixels discarded immediately after.
5. **Severity** — `confirm/triage.py` classifies into `ncii_suspected` (face matched + explicit, top
   of queue) / `explicit_unmatched` (explicit, face not matched — a possible embedding miss) /
   `unassessed` / `benign_copy` / `likely_not_subject` (bottom of queue). **Machine ordering only** —
   nothing here is machine-confirmed or machine-dropped (INVARIANTS #47).
6. **Cost** — governed by the existing budget/breaker/spend machinery (§3.6b) via the
   `rekognition_confirm` provider row, priced as the worst-case bundle so the one-row budget check
   stays conservative. Skips land as `budget_exceeded` / `breaker_open` triage states — visible,
   retryable, never silent (INVARIANTS #41).
7. **CSAM tripwire** — moderation labels suggesting minors + explicit routes the hit to
   `confirm_state = 'quarantined'`: excluded from every `svc` view and the default review queue, an
   ops alarm logged (`docs/OPERATIONS.md`), no score effect, bytes already discarded. v1 escalation is
   a **manual legal process** — a reporting pipeline (NCMEC) does not exist and is out of scope,
   consistent with the existing minor-discovery gate (INVARIANTS #8b).

### 3.9 Score engine — **built** (2026-08-19)

`src/imageshield/score/` owns `protection_scores` (materialized, one row per `user_ref`) and
`score_events` (append-only journal) — exactly **one** code path writes either (`score/store.py`,
INVARIANTS #21 extended, #44). A 0–100 integer across four components — Posture, Coverage, Exposure,
Threat — computed from config-driven weights (`score_config_version` stamped on every journal row, so
a later retune does not make historical scores uninterpretable, the same reasoning as
`search_runs.threshold_config`).

Recompute (`score/engine.py`, pure) runs **immediately after its trigger commits, in its own short
transaction** — a hit decided, feedback written, a seed added, an enrolment change, a run completed, a
threat event created or retracted — never via a queue, because a score-recompute queue would be a
fourth application queue nobody sanctioned and recompute itself is cheap (a handful of indexed reads
plus two writes). Idempotent and total: compute from state, journal the diff; an unchanged state
journals nothing (`tests/test_score_store.py::test_recompute_twice_writes_nothing_new`).

`score/tick.py` is a separate process (`python -m imageshield.score.tick`, daily interval by default)
— the drift healer. It re-runs recompute for aging effects (stale seeds, aged-open recommendations,
decaying threat penalties) and for any trigger whose recompute crashed after the trigger itself
committed. It is not the primary path; it is what keeps a score from silently going stale if a
recompute call is ever missed.

`recommendations/catalog.py` is the companion: typed kinds in code
(`complete_enrolment`/`add_seed_photos`/`refresh_seeds`/`respond_to_hits`/`run_priority_scan`),
per-user instances in a table, completion **detected from data**, never self-reported by the proxy.

**No feedback signal a user gives ever lowers the score** (INVARIANTS #45) — the direct fix for the
old system's −18-per-active-report bug that made reporting abuse worsen a user's own number.

### 3.10 Threat events and review — **built** (2026-08-19)

`src/imageshield/threats/` — `threat_events` + a relevance matcher (event `domains[]` ∩ the user's own
**live** hit domains, or `is_global`) + `threat_event_matches`. Admin CRUD under the existing
`/v1/admin/*` auth. A relevant event both drops the affected user's score directly (bounded, decaying,
reversible — INVARIANTS #46) and spawns the recommendations that restore it, even while the event
stays live. Retraction reverses through the score journal, exactly.

`src/imageshield/review/` — `review_tasks` + `/v1/admin/review/*`. A human decision
(`confirmed(severity)` / `rejected` / `uncertain`) is the only way an infringement's `confirm_state`
becomes `'confirmed'`, enforced at the database (migration 0021's `infringements_confirmed_needs_human`
CHECK, INVARIANTS #47) rather than trusted from the route. An `uncertain` decision returns the task to
`pending` in place — no timeout, no auto-promotion (INVARIANTS #19).

*As of 2026-08-21 (subject-verified hits), operator review is the exception path, not the gate.* The
deciding human for a hit is normally its own subject (`subject_decide`, INVARIANTS #19/#47 as
amended): the subject sees a blurred crop via `GET /v1/infringements/{id}/preview` and answers via
`POST /v1/infringements/{id}/decision`. The operator queue survives for the CSAM quarantine lane and
as the override/correction path — metadata-only in both cases, because staff never see hit imagery —
and the `/decisions` observer view observes what subjects decided (explicit-severity confirmations
flagged as takedown-campaign candidates), now served via the panel through the backend's
`/v1/admin/*` proxy (§3.11).

### 3.11 Control room console — **built 2026-08-19, retired 2026-08-29**

This admin API used to have its own deployable (`src/imageshield/console/`, port 8082): its own
internal ingress, never routed through the proxy, no database credentials, server-rendered
(Jinja2) over the admin API — threat events CRUD, the review queue (metadata-only), the
`/decisions` subject-decisions observer page, provider spend/breaker health, and a per-user score
journal inspector, with HTTP Basic operator auth against `CONSOLE_OPERATORS`.

It is retired: staff now reach every one of those screens through the backend's `/v1/admin/*`
operator proxy (`image_backend` spec `2026-08-29-admin-proxy-design.md` §12), which authenticates
operators via an account + `iam.operator_grants` grant and injects the granted `display_name`
into every write server-side. This repo's admin routes (§3.6b and the rest of `/v1/admin/*`) are
unchanged — only the direct-to-services client is gone. `CONSOLE_OPERATORS` stays in Secrets
Manager (an audit artifact of what was granted); no code reads it any more.

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
| Review queue and decisions | **Services** | Postgres (`review_tasks`, migration 0021 — §3.10) |
| Reports, hits, recheck state | **Services** | Postgres |
| Protection score, journal, recommendations | **Services** | Postgres (`score_rw`, migration 0022 — §3.9). Journal is `INSERT`-only for the role |
| Threat events + matches | **Services** | Postgres (admin-curated — §3.10) |
| Confirm-pipeline triage (severity, pHash, moderation labels) | **Services** | Postgres, on `infringements` (migration 0021 — §3.8). No image bytes; text and a 64-bit hash only |
| Hostile-image fetch + live crop render | **Services** | Nothing persisted — the fetcher deployable (§3.7) holds no DB credentials at all |
| Report reads for the UI | **Proxy** | Postgres (read-only, `svc` views — migrations 0016 + 0023) |
| Pushing onto any queue | **Services** | SQS (via outbox) |

Admin/operator reads and writes (threat events, review, provider health, score inspector) own no
data of their own — they are Services' existing `/v1/admin/*` routes over the tables above. Until
2026-08-29 a co-located console called them directly; that deployable is retired, and the backend's
`/v1/admin/*` proxy is now the only caller (§3.11).

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

This table is the original match-module-era design (`match:forward`, `match:backfill`) and predates
the provider-search pivot; it is kept for the design reasoning, not as the current queue list. **The
three queues actually built and running are `identity:index`, `search:runs`, and — since the
2026-08-19 protection-score push — `confirm:hits`** (`src/imageshield/confirm/worker.py`, §3.8), each
Standard (not FIFO — ordering is irrelevant here and the FIFO throughput ceiling isn't worth it), each
with its own worker and its own DLQ, each fed through the same outbox described below.

| Queue | Producer | Message | Latency budget |
|---|---|---|---|
| `identity:index` | Enrolment handler | `{enrolment_id}` | Seconds |
| `match:forward` *(unbuilt)* | Ingest adapter | `{content_face_id}` | Minutes |
| `match:backfill` *(unbuilt)* | `enrolment_complete` | `{user_id, page_from, page_to}` | Hours |
| `confirm:hits` *(built, 2026-08-19)* | Search-run completion, via the outbox | `{infringement_id}` | Minutes |

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
