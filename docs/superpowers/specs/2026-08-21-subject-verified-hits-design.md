# Subject-verified hits — design

**Date:** 2026-08-21 · **Status:** approved (owner, in-session), building on branch
`subject-verified-hits` · **Supersedes in part:** the 2026-08-19 protection-score spec §8 (operator
review as the only confirm path) and `docs/prompts/BACKEND-SCORE-SURFACE.md`'s presentation rule
("everything non-confirmed renders at most as 'being checked'").

## 0. The decision, on the record

Three product calls were made by the owner on 2026-08-21, deliberately, with the trade-offs named:

1. **The subject is the deciding human.** A hit's confirm/reject decision belongs to the person whose
   likeness it is, not to an operator. INVARIANTS #19/#47 required *a human* decision; they assumed an
   operator because no subject-facing surface existed. The subject is a human, the schema CHECK
   (`infringements_confirmed_needs_human`) is satisfied by `confirm_decided_by = 'subject'`, and the
   operator queue survives only as the quarantine lane and the override path.
2. **Staff never see hit imagery. At all.** Not blurred, not explicit-flagged, not for QA. The console
   review screen goes metadata-only and its crop route is removed. The only pair of eyes that ever
   sees a hit's pixels is the subject's own — StopNCII discipline applied to ourselves.
3. **The subject sees everything, blur is the buffer — including explicit-flagged hits, including
   face-match failures.** The named risk is accepted: on a failed face-match the crop may be a
   *stranger's* face (the 2026-08-20 weibook hit is live proof), and on a failed match over explicit
   content it may be a stranger's intimate image, blurred until tapped. The owner chose showing over
   withholding because the ask-copy is honest ("we found a similar photo — is this you?"), because
   a deepfake of the subject can legitimately fail face-match (novel face ≠ enrolment vector — the
   core product case), and because the alternative leaves the subject deciding blind. The
   countervailing harm — CLAUDE.md §1's "shown someone else's abuse material" — is mitigated by
   blur-by-default, per-item tap reveal, subject-only access, per-render audit, and a render ceiling;
   it is not eliminated, and this paragraph is the record that it was weighed.

A false "yes" and a false "no" both remain recoverable: decisions are one-shot for the subject in v1
(no re-decide) and the operator override path can correct either, on request, through the team.

## 1. Every hit gets triaged (enqueue gate opens)

`_ENQUEUE_CONFIRM_HITS_SQL` (search/store.py) drops its per-provider criteria block (`hive ≥
CONFIRM_HIVE_MIN_SCORE` / `google kind ∈ CONFIRM_GOOGLE_KINDS`). What remains: `a.last_run_id =
%(run_id)s` (this run's attestations only), `i.confirm_state = 'unconfirmed'` (the re-enqueue guard),
`DISTINCT`. Every new hit therefore reaches Rekognition triage; the existing `rekognition_confirm`
provider row's budget/breaker/kill-switch machinery (#37–#42) is the spend control, unchanged.

`confirm_hive_min_score` and `confirm_google_kinds` (and the `confirm_google_kind_set` property and
its validator) are **removed** from `config.py` — dead config is a lie waiting to be believed. No task
definition passes either env var (verified), so removal is deploy-safe.

Not built (owner-scoped out): a backfill/sweep for hits that predate this change. They re-enter
naturally when a future run re-touches the same URL (the attestation upsert refreshes `last_run_id`
while `confirm_state` is still `unconfirmed`). The one existing stranded hit (weibook, in the
confirm-hits DLQ) is recoverable by a manual ops redrive after deploy.

## 2. Transcode before Rekognition (the WebP fix)

Rekognition accepts JPEG/PNG bytes only. The weibook hit's confirm run failed five times on
`DetectFaces → InvalidImageFormatException` (a `.webp`), opened the `rekognition_confirm` breaker, and
dead-lettered — with the gate in §1 open, format failures would only multiply.

`attribution/crop.py` (the sanctioned Pillow site) gains `to_rekognition_jpeg(image: bytes) -> bytes`:
JPEG/PNG bytes pass through untouched (sniffed by magic bytes, not trusted content-type); anything
else Pillow can decode is re-encoded to JPEG in memory; anything it cannot decode raises
`UndecodableImage`. Call sites, exactly two:

- `confirm/worker.py`, once, at the top of the Rekognition bundle (before `detect_faces`,
  `search_face`, `moderation.assess`). pHash keeps reading the *original* bytes — dHash decodes WebP
  fine and re-encoding first would silently shift historical pHash comparability for no gain.
- `attribution/service.py`, before `detect_faces` — the proxy's presigned photo can be WebP/HEIC too.

An undecodable image in the confirm worker takes the existing `record_unfetchable` path (the bytes
are unusable as an image; same user-facing meaning), not the retry-to-DLQ path.

## 3. The worker persists the best *detected* bbox, matched or not

Today `best_face_bbox` lands in `review_tasks.triage` only when a face **matched**; on a failed match
every detected bbox is computed and discarded, which leaves the §4 preview with nothing to crop for
exactly the "similar photo" case. Change: when no face matched, `best_face_bbox` falls back to the
largest *detected* face's bbox. `triage.face_match_score` stays `NULL` in that case, so
matched-vs-similar remains distinguishable downstream. Floats about the image, never pixels — #9 holds.

## 4. `GET /v1/infringements/{id}/preview` — the subject's crop

Service-token gated, same router family as feedback. Query params: `user_ref` (UUID, required),
`reveal` (bool, default false).

- **Ownership**: `user_ref` in the `WHERE` clause; absent-or-not-yours is one indistinguishable
  `404 infringement_not_found` (the feedback endpoint's oracle discipline, verbatim).
- **State**: `quarantined` and `duplicate` rows are excluded in the same `WHERE` — to the subject
  they do not exist. Any other `confirm_state` may preview (a confirmed hit stays viewable).
- **Availability**: no triage row, no `best_face_bbox`, or no `image_url` →
  `404 preview_unavailable` (a *different* code than the ownership 404 — only reachable once
  ownership passed, so it leaks nothing cross-user). The app falls back to domain + "no preview";
  the subject can still decide.
- **Render**: services looks up `image_url` + bbox server-side (neither ever reaches the client),
  calls the fetcher's existing `POST /v1/crop` (`blur = not reveal`), and streams the JPEG back with
  `Cache-Control: no-store, private`. Nothing persisted anywhere — #9/#10/#12/#13 all hold; the bbox
  travels services→fetcher only.
- **Audit (#31)**: every render writes one `audit_log` row — `actor_type='subject'`,
  `action='preview.rendered'`, `subject_ref=user_ref`, `resource_id=infringement_id`,
  `metadata={"reveal": bool}` — written **before** the fetcher call so a render that then fails
  still shows an attempt.
- **Ceiling (#32)**: `PREVIEW_DAILY_RENDER_CEILING` (config, default 200) — renders per `user_ref`
  per rolling 24h, counted from those audit rows; over the ceiling → `429 preview_rate_limited`,
  `retryable=true`. Migration `0024` adds the supporting partial index on
  `audit_log (subject_ref, occurred_at) WHERE action = 'preview.rendered'`.
- Fetcher refusals map through: SSRF/timeout/size refusals and undecodable bytes →
  `502 preview_unavailable_upstream` (`retryable=true`); `crop_too_small` → `404 preview_unavailable`.

The fetcher itself is unchanged — one more caller (services API), same single token. Its docstring's
"exactly one caller" claim is corrected in place.

## 5. `POST /v1/infringements/{id}/decision` — the answer is the decision

Body `{user_ref, decision: "confirmed" | "rejected"}` (pydantic, `extra='forbid'`). One transaction in
`review/store.py` (`subject_decide`), mirroring `decide`:

1. `SELECT ... FROM infringements WHERE infringement_id = %s AND user_ref = %s FOR UPDATE` — absent
   or not-yours → `None` → the same oracle-safe `404`.
2. State gate: from `unconfirmed` or `machine_triaged` only. Already `confirmed`/`rejected` **by
   'subject'** with the *same* decision → idempotent no-op, `200` with the stored outcome (the proxy
   retries; SQS-world discipline applies to HTTP too). Same decider, *different* decision → `409
   decision_conflict` (no re-decide in v1 — changes go through the team). Decided by an *operator* →
   `409 decision_conflict` (a subject cannot overturn an operator). `quarantined`/`duplicate` →
   the ownership `404` (invisible is invisible).
3. Write: `confirm_state = confirmed|rejected`, `confirm_decided_by = 'subject'`,
   `confirm_decided_at = now()`, **severity untouched** (stays machine-triaged; the schema CHECK is
   satisfied). `rejected` additionally sets `status = 'dismissed_not_me'` so the existing view
   arithmetic retires it from the user's counts.
4. Any `pending` `review_tasks` row for the infringement is marked `decided` with
   `decided_by = 'subject'` — the operator queue stays clean.
5. Audit row: `actor_type='subject'`, `action='review.subject_decided'`,
   `metadata={"decision", "severity", "source_domain"}` (domain denormalised in so §6's feed is one
   indexed read).
6. After commit: `score_store.recompute(user_ref, cause_kind='subject_decision', cause_ref=id)`,
   swallow-and-log like feedback (tick heals). The engine computes from state — no engine change. A
   subject's `confirmed` moves Exposure exactly as an operator confirm would; see §7 for the #45
   amendment.

## 6. Control room becomes observer

- Console `/crop` route, its `FetcherClient`, `review.html`'s crop/reveal block, and the console's
  `FETCHER_BASE_URL`/`FETCHER_TOKEN` config + task-def entries are **removed**. The review screen
  (quarantine lane + override path) renders metadata only: domain, severity, score, triage notes.
- New `GET /v1/admin/review/subject-decisions` (admin token): reads `review.subject_decided` audit
  rows, newest first, `limit` param (default 50) — decision, severity, source_domain, decided_at,
  infringement id. The console gets a panel off it; `ncii_suspected`/`explicit_unmatched` rows are
  flagged prominently as takedown-campaign candidates (campaigns spec, 2026-08-20, still unbuilt).

## 7. Amendments written in the same PR

- **INVARIANTS #19**: gains "The subject is a valid deciding human for hits on their own likeness. A
  blurred face crop may be shown to the subject — and only the subject — for the purpose of
  deciding." Staff-never-see-imagery is stated in the same breath.
- **INVARIANTS #23**: "never in a list view" scoped to un-blurred crops; blurred previews render only
  on the hit's own card, reveal stays per-item explicit tap.
- **INVARIANTS #45**: gains the decision-lane distinction: feedback signals still never lower the
  score; a subject **decision** is not feedback — their `confirmed` moves Exposure exactly as an
  operator's would. Without this line the feature reads as a #45 violation.
- **INVARIANTS #47**: machine triage still neither confirms nor drops; the *subject* now can.
- **CLAUDE.md**: §2's Pillow sentence (crop.py now also transcodes), §4's #19 summary, §6 scope table
  row for this push (dated note pattern, as 2026-08-19 did).
- **ARCHITECTURE.md §3.10**: review is now the exception path (quarantine + override), not the gate.
- **PROXY_INTEGRATION.md**: the two new endpoints; the specified-never-built `GET /v1/hits/{id}/crop`
  entry superseded by `/preview`; the presentation rule updated (triaged hits carry the ask-card).
- **BACKEND-SCORE-SURFACE.md**: presentation rule superseded (see PROXY_INTEGRATION).
- **docs/OPERATIONS.md**: subject-decisions feed; quarantine runbook unchanged.

## 8. Out of scope (owner-scoped, 2026-08-21)

Proxy-repo work (pass-through `GET /v1/hits/:hitId/preview`, `PATCH /v1/hits/:hitId`
confirm/reject forwarding, the score-event copy line, the web UI card); the backfill/sweep worker;
the breaker input-error reclassification; the margin/limit doc-drift cleanup (noted, not fixed here);
takedown of any kind.

## 9. Tests

Store: `subject_decide` state gating (each `confirm_state` × each decision), idempotent replay,
operator-conflict 409, ownership-404 indistinguishability, review-task cleanup, audit shape, severity
untouched. Routes: preview 404-oracle, `preview_unavailable`, `no-store` header, reveal flag passed
through, audit row per render, 429 at the ceiling; decision request validation (`extra='forbid'`),
envelope shape on every error. Worker: opened gate enqueues a hit that the old criteria excluded;
transcode called before the bundle (WebP bytes reach Rekognition as JPEG); undecodable →
unfetchable path; bbox persisted on failed match. Attribution: `to_rekognition_jpeg` passthrough /
re-encode / raises. Console: crop route gone, subject-decisions panel renders. Migration 0024
up/down. Boundary tests still green (no new Rekognition call site, no S3, no bytea).
