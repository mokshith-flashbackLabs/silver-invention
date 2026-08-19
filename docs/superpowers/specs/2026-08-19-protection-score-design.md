# Protection score, confirm pipeline, threat events, review & control room — design

**Date:** 2026-08-19
**Status:** approved in brainstorming (all four sections), pending spec review
**Delivery:** one push (approach C) — score engine + confirm pipeline + events + console ship together

---

## 1. Problem and context

The product needs a dynamic, per-person **protection score** that is the engagement hook: it rises
when the user completes recommendations, falls when their coverage goes stale or real exposure
exists, and reacts to external threat events (e.g. a database leak at a site where they have hits).

The old system (`server.js`) had a score and it is the cautionary tale this design must not
recreate:

- Computed twice — client-side and server-side — diverging by −18 per active report.
- Every **unresolved** match cost 18 points, so marking a hit "this is abuse of me" permanently
  depressed the score while *dismissing* it improved it. Reporting abuse made the number worse.

Current v1 state this design builds on:

- Hits land as `infringements` (URL-hash deduped) + per-provider `attestations`, all in `review`
  band (no provider calibrated). The human review queue does not exist, so per INVARIANTS #19
  nothing is user-presentable as a confirmed finding today.
- `svc.v_person_hits` deliberately omits any image URL; pixels-to-user is specified as a blurred,
  live-rendered face crop (crop fetcher — specified in `ARCHITECTURE.md` §3.7, not built).
- Dedup is URL-level only: the same picture reposted at a **new** URL is a brand-new hit.
- The only score-like number is `live_exposure_count`, whose exclusion semantics (dead URLs,
  `dismissed_not_me`, `authorised`) already encode "reporting abuse must never make the number
  worse."

## 2. Decisions taken during brainstorming

| Question | Decision |
|---|---|
| What is "the score"? | One per-person home-screen **protection score** (0–100). Per-hit confidence/severity is a separate internal quantity that feeds it. |
| External events vs the score | **Hybrid**: a relevant event both drops affected users' scores directly (bounded, decaying, relevance-scoped) *and* spawns the recommendations that restore it. Guardrails: the drop must trace to a fact about this user (their hit domains), be bounded, decay with age, and be reversible on retraction. |
| Can machine confirmation reach the user? | **No. Machine triages, human confirms.** Rekognition face-match + moderation orders the review queue and attaches severity; only a human decision makes a hit user-visible and score-moving. INVARIANTS #19 stands, enforced by schema. |
| Event source (v1) | **Admin-curated** via `/v1/admin/threat-events`, operated from a new internal **control room** console. Automated feeds are a later bolt-on behind the same table. |
| Delivery | Everything in one push (score engine, confirm pipeline, events, review, console, fetcher). |

Ask-first items sanctioned by the user in this brainstorm: two new deployables (fetcher, control
room), one new queue (`confirm:hits`), Rekognition `DetectModerationLabels` as a new AWS call.

## 3. System shape

Five new modules in this repo, two new deployables, one new queue.

```
search run completes ──▶ attestations/infringements (existing)
        │ most-similar review-band hits
        ▼
  outbox ──▶ confirm:hits queue ──▶ confirm/ worker
        │  fetch (fetcher deployable) → pHash → face-match (attribution/) → moderation
        ▼
  triage (severity, face_match, dup) ──▶ review_tasks (ordered)
        │ human decision in control room (crop-only, blurred, live-rendered)
        ▼
  hit confirm_state = confirmed(severity) ──▶ score/ engine trigger
        ▼
  score_events journal + protection_scores materialized ──▶ svc views ──▶ proxy UI

threat event posted in control room ──▶ threats/ matcher (event domains ∩ user's live-hit domains)
        ──▶ journaled bounded score drop + spawned recommendations ──▶ completion restores
```

### Modules

- **`score/`** — the score engine. Owns `protection_scores` (materialized, one row per
  `user_ref`) and `score_events` (append-only journal). Exactly **one** code path writes a score
  (INVARIANTS #21 extended). Recompute runs **immediately after its trigger commits, in its own
  short transaction** — hit decided, feedback written, seed added, enrolment change, run
  completed, event created/retracted — not via a queue: a score-recompute queue would be a fourth
  application queue nobody sanctioned, and recompute is cheap (a handful of indexed reads + two
  writes). A `score/tick.py` process (recheck-style poll loop, daily interval) is the drift
  healer: it re-runs recompute for aging/decay and for any trigger whose recompute crashed after
  the trigger committed. Recompute is idempotent and total: compute from state, journal the diff;
  an unchanged state journals nothing.
- **`recommendations/`** — catalog in code (typed kinds), per-user instances in a table,
  completion **detected from data** (a seed row appeared, feedback was given, a run completed),
  never self-reported.
- **`threats/`** — `threat_events` + relevance matcher + `threat_event_matches` materialization.
  Admin CRUD under existing `/v1/admin/*` auth (`X-Admin-Service-Token`).
- **`confirm/`** — worker consuming `confirm:hits` (messages carry IDs only; worker re-reads
  Postgres — existing convention). Orchestrates fetch → pHash → face-match → moderation → triage
  write. Face search goes **through `attribution/`'s existing machinery** with candidate list =
  the hit's owner, so INVARIANTS #1a and both CI face-search grep gates stand unchanged.
- **`review/`** — `review_tasks` + decision API (`/v1/admin/review/*`). Every decision carries
  operator identity into `audit_log`.

### Deployables

- **Fetcher** — this is `ARCHITECTURE.md` §3.7's crop fetcher, pulled into scope. Stateless, no
  VPC access, **no DB credentials**, hostile-domain egress posture (private-IP egress blocked,
  size caps, content-type checks, timeouts). Two jobs: hand the confirm worker image bytes
  (transient, in-memory only) and render blurred face crops live for the review screen. It never
  persists anything.
- **Control room** — internal ops console (its own deployable, own internal ingress, never
  routed through the proxy; the client-never-talks-to-us rule is untouched). Features: threat
  events CRUD, review queue, provider spend/breaker health (existing admin reads), per-user score
  journal inspector. Operator auth v1: network-restricted + per-operator credentials; every write
  is audit-logged with operator identity.

### Queue

`confirm:hits` + DLQ. Third application queue (`identity:index`, `search:runs`, `confirm:hits`).

## 4. The score

**0–100 integer, presented as "protection score" — never "safety."** Copy stays inside
INVARIANTS #26: it describes posture and coverage in *monitored sources*; 100 is never rendered
as "you're safe."

Structure is fixed here; **all numbers live in config**, and `score_config_version` is stamped on
every journal row so historical scores stay interpretable after retuning (same reasoning as
`search_runs.threshold_config`).

| Component | Initial weight | Full marks when | Moves down when |
|---|---|---|---|
| Posture | 40 | Enrolment complete; seed portfolio at config target count and fresh (< 90d since last added); no confirmed hits awaiting the user's feedback; no aged open recommendations | Seeds stale, recommendations ignored. Every point recoverable by acting. |
| Coverage | 25 | A completed run exists within the current tier window plus a config grace (measured from `next_scan_after`, regardless of who triggered the run — the cadence scheduler stays unbuilt) and providers succeeding for this person (reuses `monitored_sources` semantics) | A provider silently failing for them; scans not running. Deliberately **not** reduced by cadence demotion itself — fortnightly-because-clean must not read as worse protection. |
| Exposure | 25 | No confirmed live hits | Per **human-confirmed** hit, severity-weighted (initial config: NCII-class 12, benign copy 2), floor 0. `url_dead` restores the weight. `authorised` removes it. `not_me` suspends the weight pending re-review. **No feedback the user gives ever lowers it.** |
| Threat | 10 | No active matched events | Bounded per-event penalty × relevance × age-decay, restored by completing the event's linked recommendations even while the event is live. A global event alone can never zero this component past a config floor. |

**Journal is the product surface.** `score_events(score_event_id, user_ref, delta, component,
cause_kind, cause_ref, config_version, score_after, created_at)` — append-only, one row per
movement, rendered by the proxy as the score-history feed ("−6 — confirmed match found on
{domain}", "+4 — you added 5 recent photos", "−3 — leak reported at {site}; 2 recommended
actions"). No delta exists without a cause row; the journal sums to the materialized score,
enforced by test.

**Boot validation:** weights sum to 100; severity weights ≤ component weight; threat floor and
caps present; refuses to boot otherwise (same pattern as the existing floor checks).

## 5. Recommendations

Catalog in code (v1): `complete_enrolment`, `add_seed_photos` (to config target),
`refresh_seeds` (nothing added in 90d), `respond_to_hits`, event-linked `run_priority_scan`
(completing = a completed run after the event's `starts_at`; the proxy triggers the run via the
existing `POST /v1/search`).

`recommendations(rec_id, user_ref, kind, params jsonb, status open|completed|expired|dismissed,
source_event_id nullable, created_at, completed_at, expires_at)`.

- Generated by a deterministic rules pass inside the score engine. **No LLM anywhere in this
  loop** (Code over LLM).
- Completion detected from data changes only. The proxy never writes.
- Aging: open recommendations past a config soft-age reduce Posture (bounded). This — plus seed
  staleness and coverage staleness — is the entire "user doesn't care → score drops" mechanic.
- `dismissed` stops the nagging; the points stay unearned. Only doing the thing earns them.

## 6. Threat events

`threat_events(event_id, kind leak|deepfake_wave|platform_incident|other, title, body, severity
smallint, matcher jsonb {domains[], global bool}, penalty numeric, starts_at, expires_at, decay,
status draft|active|expired|retracted, created_by, created_at, updated_at)`.

- **Relevance matching:** event domains ∩ the user's live hits' `source_domain`s — this is what
  makes the drop personal rather than ambient. Matches materialize in
  `threat_event_matches(event_id, user_ref, matched_via, penalty_applied)`.
- Global events allowed, small capped penalty.
- **Retraction reverses through the journal** — a mistaken event is visibly undone, never
  silently edited.
- CRUD `/v1/admin/threat-events` behind `X-Admin-Service-Token`; control room is the UI; every
  write audit-logged with operator identity.

## 7. Confirm pipeline

Trigger: after a run's attestations land, each new/updated review-band infringement meeting
per-provider "most similar" criteria (config: `CONFIRM_HIVE_MIN_SCORE`;
`CONFIRM_GOOGLE_KINDS = {full_match, partial_match}`) enqueues to `confirm:hits` via the outbox.

Worker steps per hit:

1. **Fetch** the image via the fetcher deployable. Transient bytes, size/content-type caps,
   private-IP egress blocked. Unfetchable → triage `unfetchable`, hit stays reviewable URL-only.
   Retries with backoff → DLQ. A failed confirm never blocks anything (INVARIANTS #33's spirit).
2. **Perceptual hash** — 64-bit dHash via Pillow (already a dependency; ~20 lines; no new
   package), stored as `phash BIGINT` (passes the no-`bytea` lint and the name gate). If it
   matches a prior image **for this user** that **a human already decided** (Hamming distance ≤
   config), the new URL inherits that decision as `duplicate_of`: no re-review, no second score
   movement. Cross-run, cross-URL "same picture, we don't care."
3. **Face-match** through `attribution/` against `identity-v1`, candidate list = the hit's owner
   only. Result recorded as a triage score, never an identity (INVARIANTS #1/#1a intact; CI grep
   gates unchanged — `confirm/` imports `attribution/`, never calls Rekognition search directly).
4. **Moderation** — Rekognition `DetectModerationLabels` on the same bytes. Labels stored as
   text; pixels discarded.
5. **Severity:** `ncii_suspected` (face matched + explicit) → top of queue;
   `explicit_unmatched` (explicit, face not matched — could be an embedding miss) → reviewed;
   `benign_copy`; `likely_not_subject` → bottom of queue. **Machine ordering only — nothing is
   machine-dropped** (§7.3: a real infringement in drop is invisible forever) **and nothing is
   machine-confirmed** (#19).
6. **Cost:** `rekognition_confirm` is a `providers` row (kind `classifier` — the enum value 0001
   reserved for exactly this) with `cost_per_call_usd` priced as the **worst-case bundle**: one
   confirm = up to 1 `DetectFaces` + N face searches (config cap) + 1 `DetectModerationLabels`,
   so the row's per-call price is the bundle ceiling and the gate's one-row budget check stays
   unchanged and conservative. Governed by the existing budget/breaker/spend machinery (one
   transaction, fail-closed budget, no silent skips). Skips land as `budget_exceeded` /
   `breaker_open` triage — visible, retryable.
7. **CSAM tripwire:** moderation labels suggesting minors + explicit → hit status `quarantined`:
   excluded from every svc view and the default review queue, ops alarm fired, no score effect,
   bytes already discarded (URL + labels retained as the only record). v1 escalation is a manual
   legal process. **Open obligation: counsel must confirm reporting duties before launch** — a
   reporting pipeline (NCMEC) does not exist and is explicitly out of scope, consistent with the
   existing minor-discovery gate.

## 8. Review

`review_tasks(task_id, infringement_id, user_ref, severity, triage jsonb {face_match_score,
moderation_labels, phash_dup}, status pending|decided|quarantined, decision
confirmed|rejected|uncertain, decided_by, decided_at)`, ordered by severity then face-match.

- Reviewer sees a **blurred face crop only**, rendered live by the fetcher, reveal-on-click
  (reviewer welfare, `ARCHITECTURE.md` §2.4). Never a full image, never a stored image.
- Decisions: `confirmed(severity)` / `rejected` / `uncertain`. An `uncertain` decision is
  recorded and returns the task to `pending` (it stays in the queue; no timeout auto-promotes —
  #19).
- **Schema-enforced #19:** a CHECK/trigger makes `confirmed` require `decided_by IS NOT NULL`.
  `confirmed` is the only state that moves Exposure and becomes user-visible.

## 9. Proxy contract additions — all additive

Four **new** views (no edits to existing definitions): `v_person_score` (score, components,
`computed_at`, `config_version`), `v_person_score_events` (history feed),
`v_person_recommendations`, `v_person_threat_context` (active matched events: title, body,
severity, expiry). Additive columns on `v_person_hits`: `confirm_state`, `severity`,
`decided_at`. Grants extend `imageshield_proxy_ro`'s existing pattern (SELECT on the views,
nothing else). Quarantined hits are excluded in every view's WHERE, asserted by test.
`PROXY_INTEGRATION.md` §6 updated in the same PR.

Notifications: score changes and new-event hooks ride the **existing batched-digest rule** —
never real-time, never 22:00–08:00 local (INVARIANTS #24). Stated here deliberately so the
"hook" framing never turns into a 2am push.

## 10. Failure modes

- Score engine down → views serve the last materialized score; `computed_at` staleness visible.
- Confirm worker: retries with backoff → DLQ; every skip is a first-class recorded triage state.
- Fetcher is the only component touching hostile bytes; it holds no DB credentials and no state —
  compromise blast radius is one stateless box.
- Rekognition breaker open → hits remain reviewable URL-only; nothing waits on a broken
  dependency.
- Event matcher failure → no score movement (absence of a journal row, never a wrong one).

## 11. Tests that must exist and never be deleted

- Journal sums to the materialized score, always.
- No user feedback (`not_me`, `authorised`, anything) ever lowers the score — checksum-style,
  like the existing `not_me` test.
- Event retraction restores exactly what it took.
- A global event alone cannot move a score below the config floor.
- `confirmed` without `decided_by` is a constraint violation (#19 by schema).
- pHash inheritance never crosses users and never inherits from an undecided hit.
- Fetcher refuses private IPs, oversize bodies, non-image content types.
- Schema lint still green — no `bytea` anywhere; `phash BIGINT` passes.
- Face-search grep gates unchanged: `confirm/` contains no Rekognition search call.
- Quarantined hits appear in no svc view.
- Exactly one module writes `protection_scores` / `score_events` (import/boundary test).

## 12. New invariants (to land in `INVARIANTS.md`)

- **#44** Every score movement is journaled with a user-readable cause. No unexplained deltas.
- **#45** Reporting abuse never worsens any user-facing number — generalizes the
  `live_exposure_count` rule to the score and everything after it.
- **#46** Threat penalties are bounded, decaying, relevance-scoped, and reversible on retraction.
- **#47** Machine triage orders review but can neither confirm nor drop.

## 13. Scope changes this design makes

Pulled from "specified, do not build yet" into scope: a **minimal** adjudication queue + reviewer
tooling (review module + control room screen), the **crop fetcher** deployable, hit severity.
`CLAUDE.md` §6 must be updated in the implementing PR.

Still out of scope: takedown, digests **delivery** (proxy's side), automated threat feeds, the
cadence scheduler (nothing new reads `next_scan_after` to trigger runs), the match module /
partner ingest, clustering / `discovered-v1`, the proxy's shield-rule surface, minor discovery
(unchanged: gated on CSAM screening + reporting), any LLM anywhere in this loop.

## 14. Open questions / risks

- **CSAM tripwire legal review** — running moderation creates knowledge; counsel must confirm
  obligations before launch (§7 step 7).
- **Reviewer staffing** — human-confirm gating means hits wait on a reviewer; that is an
  operating cost accepted in brainstorming, and the queue backing up is the designed behaviour.
- **Severity weights are unvalidated** — initial config values (12/2) are judgement; expect a
  tuning pass once real confirmed hits exist. Config-versioned journal makes retuning safe.
- **Event-hook pressure** — the hybrid (events drop scores directly) is deliberately the sharper
  edge of the two options considered; the guardrails (relevance scoping, bounds, decay, journal,
  reversibility) are what keep it defensible. If trust telemetry later shows users treat drops as
  noise, the fallback posture is events → recommendations only, with no direct drop.
