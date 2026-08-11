# INVARIANTS.md — ImageShield services

Numbered rules an implementation is held to. Each is written so a reviewer can check compliance
mechanically. This document is **living** — expect it roughly to double once the system runs against
real data.

Violations are ordered by consequence, not by likelihood.

---

## A. Identity and enrolment

**1. Identity never comes from a similarity score.**
`user_id` is derived from the authenticated session, always. There is no code path where a face search
result determines which user a face belongs to.

The old system violates this in both directions. Verified by audit:

- *On a miss* — thresholds differ across call sites (90, 95, 99). A returning user scoring 92 on a
  95-threshold path is treated as new, gets a fresh `uuidv4()`, and **overwrites their existing
  `userId`**. Their monitoring history is orphaned; their old face records remain in the collection
  under a dead ID. This is the dominant observed failure. The guard meant to catch it reads
  `.Similarity` off an array rather than `[0].Similarity`, so it is permanently `undefined`.
- *On a match* — two people above threshold share one `userId`, so one receives the other's matches.
  Rarer, worse when it happens.

Both are the same root cause. Deriving identity from the session fixes both.

Check: grep the enrolment path for `SearchFacesByImage` and `searchUsersByImage`. Neither may appear.

**1b. There is exactly one similarity threshold per purpose, read from config.**
Not per call site. The old system's 90/95/99 spread across paths is what makes #1's miss case fire.
A threshold literal appearing inline anywhere is a defect.

**2. No enrolment without a passed liveness session and a consent reference supplied by the proxy.**
Two halves, enforced separately. The session half is migration 0003's composite FK to
`(session_id, status)`, pinned to `'consumed'`. The consent half is migration 0010:
`enrolments.consent_ref`, `consent_document_sha256` and `consent_signed_at`, all `NOT NULL`, written
in the same transaction as the index and the session consume. Also assert in application code before
calling `IndexFaces`, because a future migration could relax the constraint.

**The consent document itself lives in the proxy and does not cross the boundary.** The proxy owns
`profile.persons`, `profile.guardianships` and `profile.v_consent_eligibility` — which is what
computes who is *required* to sign — and it is the only public ingress, so the DocuSeal webhook
terminates there. This repo knows a `user_ref` and a face vector; it cannot determine the required
signer, and it never renders, fetches or hashes the document. `consent_document_sha256` is a hash the
proxy computed. We record the evidence and make its absence structurally impossible; we do not verify
the signature, because we never see what was signed.

`00000000-0000-0000-0000-000000000000` is reserved. Migration 0010 backfills it onto enrolments
written before consent was required, so `NOT NULL` could be applied without deleting a biometric
enrolment. The proxy must never issue it, and `enrolments_consent_not_sentinel` (`NOT VALID`, so it
grandfathers those rows and enforces on every subsequent write) refuses it at the database.

Check: attempt to create an enrolment with a `failed` liveness session. It must raise. Attempt one
with no `consent_ref`, and one with the sentinel, directly in SQL. Both must raise.

**3. Liveness must be a fresh session, not a replayed one.**
`liveness_sessions.session_id` is single-use. Once an enrolment references it, it cannot be referenced
again. `expires_at` is 10 minutes from creation.

Check: submit the same `provider_session_id` twice. Second attempt must be rejected.

**4. Never mix vectors from different models.**
Every search filters on `model_id`. A cosine similarity between an AdaFace vector and a Rekognition
face is a number with no meaning, and it will look plausible enough to pass review.

Check: `model_id` appears in the `WHERE` clause of every candidate query and in every unique index on
vector-bearing tables.

**5. `QualityFilter: HIGH` on every `IndexFaces` call.**
A poor enrolment vector degrades every match that user will ever receive, permanently, and the user
has no way to know. Reject and ask for a retake rather than accepting a marginal frame.

**6. `ExternalImageId` is the `user_id` UUID and nothing else.**
It is returned in every search response and lands in logs. Never phone, never email, never anything
phone-derived.

**7. Account deletion calls `DeleteFaces` before flipping status.**
Order matters: if the status flip succeeds and the Rekognition call fails, the face stays searchable
in a collection with no user record pointing at it. Delete first, then tombstone, then verify.

In the old system `DeleteFaces` is **never called anywhere**, beneath a comment asserting BIPA
compliance. Every face vector ever enrolled is still in the collection, including those of deleted
accounts. Treat the existing collection as unsalvageable — v2 starts an empty `identity-v1`.

Check: kill the process between the two operations. The face must not survive. Separately, assert in
CI that `DeleteFaces` has at least one call site.

**8. Both age floors are read from config at request time.**
Not constants, not inlined. There are **two**, and the split is load-bearing (step 8):

```
MIN_ENROLMENT_AGE = 13   # who may enrol: consent, guardianship, household seats
MIN_DISCOVERY_AGE = 18   # who may be SEARCHED
```

They were one number until step 8, which is why minors were blocked from enrolling at all. Minors
enrol in v1; **discovery must not run for them** (#8b). `MIN_DISCOVERY_AGE` drops in v2.

Check: `grep -rn "\b18\b"` over `src/imageshield/{subjects,http}/` and `config.py` finds no age
literal in executable code (`tests/test_boundaries.py` enforces this with `tokenize`, so prose
explaining the policy is exempt and a comparison is not). Boot refuses if
`MIN_DISCOVERY_AGE < MIN_ENROLMENT_AGE` — a discovery age below the enrolment age gates nobody.

**8b. Discovery never runs for an ineligible subject, and the refusal rests on data we own.**
`subjects.discovery_eligible` is asserted **once, at enrolment**, from the proxy's required
`subject_is_adult` field, and stored. `POST /v1/search` refuses before anything else happens: no row
in `subjects` → `409 subject_unknown`; `false` → `403 discovery_not_available`. **No `search_runs`
row is created, no provider is called, and exactly one `audit_log` row is written.**

Two reasons this is an invariant rather than a feature:

- *Why the flag and not a per-request assertion.* This service holds `user_ref` and no DOB, and that
  boundary is correct (CLAUDE.md §3.2). But a per-request assertion means a proxy bug silently scans
  a minor with nothing in our data to show it. A refusal has to rest on state we hold.
- *Why refusal and not filtering.* Discovery finds images resembling the seed and nudify sites alter
  real photos, so for an enrolled minor a *successful* result is CSAM inside this pipeline. CSAM
  screening and reporting are deferred until the partner corpus connects; until they exist the
  correct behaviour is that **nothing looks**, so nothing is found and no mandatory-reporting
  obligation starts. A filter on results implies a search ran.

A `search_runs` row with zero results reads as "we looked and found nothing" — for a subject nobody
searched that is a false reassurance, and for a minor it is a false reassurance about the exact thing
the refusal exists to prevent (cf. #26).

Lowering `MIN_DISCOVERY_AGE` does **not** by itself enable minor discovery: the proxy sends a boolean
and `subjects/eligibility.py` maps minor → ineligible unconditionally, gated on the module constant
`MINOR_DISCOVERY_SUPPORTED = False`. v2 needs a config change *plus* the minor-specific handling
code, never config alone.

Check: `search_seeds.user_ref` has a foreign key to `subjects` (migration 0008), so an unparented
seed fails at the database even if the route guard were removed. Follow-up: the same FK on
`enrolments` — enrolment creates the subject row, so the intra-transaction ordering had to be
established first (it now is: subject before enrolment, `liveness/store.py`).

---

## B. Data boundary

**9. No image bytes are ever persisted. Anywhere. By any module.**
The only images ImageShield holds are the user's own enrolment frames, and those live in **the
proxy's** S3, referenced by URI. We store pointers, never payloads.

The schema lint enforces this in three parts, in priority order:

1. **No column of type `bytea`, anywhere in the schema.** This is the real rule — it catches the
   failure regardless of what the column is called, and a name-only gate does not.
2. **Name gate**, rejecting `/_(data|blob|bytes|b64)$|thumbnail|local_path/`. Catches binary smuggled
   into `text` as base64.
3. **Explicitly allowed:** any `*_uri` or `*_url` column. These hold pointers into the proxy's S3.
   `reference_image_uri` and `source_object_uri` are correct and must pass.

The naive pattern `image|thumbnail|blob|photo|local_path` is **wrong** — it rejects
`reference_image_uri`, which is the one column that has to exist. Match on suffix and type, not on
substring.

Check: adding `photo bytea` to any table fails the build. Adding `thumbnail_b64 text` fails. Adding
`reference_image_uri text` passes.

**10. Face crops are rendered in-memory and never cached.**
No CDN, no `Cache-Control: public`, no disk write, no temp file. Response headers are
`Cache-Control: no-store, private`.

Check: hit the crop endpoint twice and confirm two fetcher invocations.

**11. The crop fetcher runs with no VPC access to any internal service.**
Separate egress path, deny-by-default network policy, domain allowlist sourced from
`content_items.source_domain`, hard 5-second timeout, max response size 20 MB, redirect limit 2, and
explicit SSRF guards rejecting private IP ranges after DNS resolution — not before.

Check: point the fetcher at `http://169.254.169.254/`. It must refuse.

**12. The crop is bbox + 15% margin, computed from `face_bbox`, and nothing outside it leaves the
fetcher.**
The full image exists only as a local variable inside the fetcher process. It is never returned to
the caller even on error paths.

**13. The BFF never receives an embedding, a vector reference, or an image byte.**
Its response bodies contain `score`, `source_domain`, `first_seen_at`, `status`, and a crop *URL* —
never crop data and never vector data.

Check: assert on the BFF response schema. Any `embedding`, `vector`, or base64-shaped field is a
failure.

**14. Only the Identity service's DB role may connect to the Identity database.**
Enforced with distinct Postgres roles and network policy. Not convention, not code review.

---

## C. Matching

**15. Threshold bands are read from config and recorded per search run.**
`search_runs.threshold_config` stores the exact bands used. Retuning must not make historical scores
uninterpretable. As of step 7 the per-row record is finer-grained still:
`attestations.calibration_version` names the exact config that produced each band.

**15b. An uncalibrated provider produces `review` and nothing else — enforced in code, not by
discipline.**
Not `auto_confirm`, and not `drop` either: a real infringement in `drop` is invisible to the user
forever, so it is the worse of the two edges. Enforcement points, all of which have permanent tests:

- `calibration/bands.py` rule 2 returns `review`/`provider_uncalibrated` before any score is
  examined. Every other failure mode in that module — no active config, score-kind mismatch, a score
  outside `providers.score_domain`, a non-finite score, an unbounded domain, an unknown category —
  also returns `review`. **Nothing in that module raises**, because banding runs inside a scan's
  write path and a crash there would fail a whole run over one malformed provider row.
- `calibrate activate` refuses on six conditions, each recomputed fresh from `eval_observations`
  joined to `eval_items` and never from the stored `measured` column. The zero-`lookalike` refusal is
  unconditional: a set without hard negatives yields precision 1.0 trivially, and no sample size
  compensates for a measurement that means nothing.
- `calibrate trust` is the **only** writer of `providers.calibrated`, and `activate` never touches
  it. Two keys, because they defend different failures — see `CLAUDE.md` §7.3.

**15c. No code path combines scores across providers.**
Provider A's 0.92 and Provider B's 0.92 are different quantities on different scales; combining them
produces a plausible-looking number with no meaning. An infringement's band is the roll-up of its
attestations' *bands*, never of their scores: any disagreement resolves to `review`, and agreement
never promotes. `tests/test_boundaries.py` greps `src/imageshield/calibration/` and
`src/imageshield/search/` for the arithmetic vocabulary, with **no allowlist** — if a legitimate use
ever needs to exist, adding it should cost a code review.

**16. `FaceMatchThreshold` is set explicitly on every Rekognition call.**
The 80% default is far too loose for this product. Auto-confirm requires 95+. Never rely on the
default.

**17. Candidate insertion is `ON CONFLICT DO NOTHING` on
`(user_id, content_face_id, model_id)`.**
The forward loop and the backfill loop will find the same pair. Duplicates reaching the report
surface means a user sees the same hit twice, which reads as "it's spreading."

**18. The forward loop and the backfill loop never share a queue or a worker pool.**
Different latency budgets (minutes vs hours) and different failure modes. A backfill for a new user
must not delay live ingest.

**19. Nothing enters the `review` band without a human decision before it reaches a user.**
There is no timeout that auto-promotes a review-band candidate to a hit. If the queue backs up, the
queue backs up.

**20. Backfill is priority-tiered and rate-limited per user.**
A new enrolment triggers a search across the entire content index. Without a cap, ten signups
saturate the cluster.

---

## D. Reports and user-facing surface

**21. The score shown to the user is computed server-side, once.**
The current implementation computes it client-side in `LikenessHealthQuizScreen.computeScore` *and*
server-side in `calculateLikenessScoreWithReports` with a −18 per active report adjustment, so the two
disagree. For a product whose entire output is a score, that is disqualifying. Delete the client
computation.

Check: no scoring arithmetic exists in the mobile client.

**22. No hit is ever hot-linked.**
Domain, title, and date are shown. The URL is available behind an interstitial via an explicit "copy
for my lawyer" action. There is no one-tap path from a report to the content.

**23. Crops are blurred by default, revealed per-item on explicit tap, and never appear in a digest,
an email, a push notification, or a list view.**

**24. Notifications are batched digests. Never real-time, never between 22:00 and 08:00 local.**
"New match found" at 2am is a harm. Cadence here is a safety decision, not a growth lever.

**25. `dismissed_not_me` never adjusts the user's identity vectors.**
It writes to `hit_feedback` for reviewer calibration only. Users rejecting true positives under
distress is common; letting that retrain the index means the most-affected users degrade their own
protection.

**26. Every report response states its scope explicitly.**
The corpus is a partner-supplied set of known sites. The UI must never imply the whole web was
searched. Absence of hits is reported as "no matches in monitored sources," never "you're safe."

**27. The payer sees billing state and nothing else.**
For household plans: the enrolled person sees their own hits. The payer sees `monitoring_active` and
the invoice. There is no view, export, or notification that shows one user another user's matches.

---

## E. Auth and audit

**28. Every endpoint authorises on the session's `user_id`, never on a request parameter.**
No endpoint accepts a phone number or `user_id` as an authorisation input. The current
`GET /api/reports?phone=` pattern is an IDOR that leaks match data and must not survive the rewrite.

Check: for every route, attempt access with a valid session for user A and a body/query referencing
user B. All must 403.

**29. `POST /check-user` does not exist.**
It is an enumeration oracle returning `registered`, `selfieUploaded`, `consentSigned`, and `userName`
for any phone number. Replace with authenticated `GET /me`.

**29b. An OTP is never readable back over any endpoint.**
Store a hash with a salt, never the code. No route returns a user record unprojected — every read
declares its fields explicitly. In the old system the OTP was a plain attribute on the user record,
`GET /users/:phone` returned the record whole, and `/verify-otp` compared with `===`, which is
complete account takeover for any phone number.

Check: for every read route, the field list is explicit. `SELECT *` and unprojected returns fail
review.

**29c. OTPs and all security tokens come from `crypto.randomInt`, never `Math.random()`.**
And rate limiting is applied, not merely imported — the old system imports `express-rate-limit` at
line 26 and never uses it.

Check: `Math.random` appears nowhere in an auth path. The rate limiter has at least one call site and
covers OTP initiation.

**29d. No password is ever stored or logged in plaintext.**
Argon2id or bcrypt at the write. The old system writes plaintext to DynamoDB and commits one line
before an unreachable throw, so signup never worked *and* accumulated credentials. If that table
holds real rows, purging it is an incident task, not a migration task.

**30. Refresh tokens are stored hashed. Access tokens are short-lived (15 min) and never persisted
client-side beyond memory.**

**31. `audit_log` is append-only.**
No `UPDATE` or `DELETE` grant for the application role. Every crop render, report view, and enrolment
writes a row.

**32. Rate limits on enrolment, liveness attempts, and crop renders are per-`user_id`, not per-IP.**
A compromised account used as a search console is the abuse case. IP limits do not catch it; a
per-user crop-render ceiling does.

---

## F. Operational

**33. A failed embedding or index job never blocks the write that triggered it.**
Enqueue, retry with backoff, surface as `pending_enrolment`. The user should see "still setting up,"
not a 500.

**34. Partner ingest is a pluggable source behind an interface.**
One partner today is a single point of failure. If they go dark, the index goes stale that day.
Do not hardcode their response shape into the match service.

**35. Reconcile against `partner_ref` on a schedule.**
Content removed at the source should be detectable. Silent drift between the two indexes is invisible
until a user asks why a dead URL is still listed.

**36. The recheck loop marks `url_alive = false`; it never deletes hits.**
A dead URL is the only good news v1 can deliver. It is also evidence. Keep it.

**37. Every pre-dispatch check runs in one place, in one order.** *(step 8)*

```
POST /v1/search
  1. ELIGIBILITY   subjects.discovery_eligible   -> 409 / 403, whole request  (#8b)
  2. ENABLED       providers.enabled = false     -> skip that provider
  3. BREAKER       breaker_state = 'open'        -> skip, record in provider_calls
  4. BUDGET        spend + cost > daily          -> skip, status='budget_exceeded'
  5. DISPATCH
```

The **order** is the invariant, not just the checks. The cheapest and most absolute come first, so an
eligibility refusal can never consume budget or trip a breaker — it stops before either exists.
Step 1 refuses the whole request and lives in the route (it must precede the `search_runs` row);
steps 2–4 are per-provider, live in `providers/gate.py`, run at *dispatch* time in the worker, and
**never fail the run**.

Running 2–4 at dispatch rather than at request time is what makes the kill switch bite on a run that
was enqueued before the switch was flipped.

**38. The budget check happens BEFORE the call, off one pre-aggregated row.** *(step 8)*
Checking after means the money is already spent. `provider_spend` is read by primary key
(`provider_id`, `spend_date`) — never `SUM(provider_calls)`, which grows with every call ever made, so
the guard protecting spend would slow down in proportion to how much has been spent.

A budget set with `cost_per_call_usd` unknown **fails closed**. An operator who asked for a cap must
not get unbounded spend because we cannot price the calls; failing open would leave the one provider
nobody could price as the only one with no ceiling.

Known bound: the check is check-then-act, so a daily budget can be overshot by at most
(concurrent dispatches) × `cost_per_call_usd`. Closing it entirely means holding a row lock across a
provider HTTP call — recorded, not discovered.

Check: `grep -r "SUM(" src/imageshield/providers/` finds nothing; a mocked client records zero
invocations when the budget is exhausted.

**39. Provider spend, the call row, and the breaker transition commit together.** *(step 8)*
One transaction, under a row lock on `providers`. A rolled-back call records no spend, and spend
recorded without its call is money with no provenance. The lock also means N workers cannot each read
the same failure count and each write count+1, which would need N × threshold failures to open one
breaker.

The same rule for the other read-modify-write in this step: **run completion and the seed's cadence
update commit together**, with the seed row taken `FOR UPDATE`. Two failure modes, one fix:

- *Lost tier change.* Completing the run makes it unclaimable, so a crash after that commit and
  before the cadence commit dropped the tier change permanently — SQS redelivers and `claim_run`
  correctly declines a completed run, so nothing retries it.
- *Lost update.* `POST /v1/search` does not serialise per seed and `claim_run` row-locks
  `search_runs`, not the joined seed. Without the lock, two overlapping runs both read the same
  `consecutive_empty_scans` and both write `count + 1`; at the threshold that is the difference
  between demoting a user to fortnightly and leaving them weekly, decided by a race.

**39b. The spend accumulator's scale is ≥ the price's scale.** *(step 8)*
`provider_spend.cost_usd` is `NUMERIC(14,6)`, matching `providers.cost_per_call_usd`'s
`NUMERIC(10,6)`. The upsert coerces each increment to the column type **before** adding, so a
narrower accumulator rounds every individual call instead of rounding the total once. Measured against
Postgres 16: ten calls at 0.000250 stored 0.0030 rather than 0.0025 (+20%, so the cap binds early and
skips a provider that still had headroom), and ten at 0.000040 stored 0.0000 — the accumulator never
grows, `spent_today` reads 0 forever, and a configured daily budget silently stops binding.

That last case is a **fail-open in the one check #38 requires to fail closed**. Google's 0.003500 is
exact at four decimals, so nothing would have shown until the first contract price quoted per thousand
calls with an odd third cent digit.

**40. A breaker opens on brokenness, never on an ordinary result.** *(step 8)*
Failure = timeout, 5xx, connection error, malformed response. **Not** 429 (that is rate limiting,
retried within `PROVIDER_MAX_RETRIES` with jitter), **not** a 200 with zero matches, **not** any skip.

Conflating "no matches" with failure would open every breaker on a quiet week — and stop the scans
that exist to notice a quiet week is not normal.

Half-open allows **exactly one** probe, claimed by a conditional `UPDATE` in Postgres rather than a
per-process flag, because "one probe" is meaningless across N workers otherwise. A failed probe
re-opens with a doubled cooldown, capped: without the cap a provider down over a weekend ends up with
a cooldown longer than the outage, so recovery is never noticed.

**40b. The breaker has no terminal state.** *(step 8)*
Every way into `half_open` has a way out, because a `half_open` row that nothing can leave is a
provider skipped on every run forever — permanent partial coverage waiting on a human noticing, which
is the failure #41 and CLAUDE.md §7.5 exist to prevent. Four edges make that true:

- **An inconclusive probe returns to `open`.** A persistent 429 gives `rate_limited`, which classifies
  as *neutral* — correctly, a provider enforcing its rate limit is not broken. But the probe was
  spent, and the claim query only matches `open`, so "change nothing" would wedge it. The clock
  restarts; the cooldown is **not** doubled, because no verdict about brokenness was obtained.
- **An abandoned probe is reclaimed.** A worker can claim the probe and then be killed before
  recording. The claim's second disjunct takes a `half_open` row whose `breaker_opened_at` is older
  than cooldown + a grace period equal to one more cooldown — long enough that it cannot steal a probe
  that is merely slow.
- **A failure while already `open` is counted and nothing more.** Reachable through the runtime cache:
  another worker can still hold a `closed` snapshot for up to `PROVIDER_CONFIG_CACHE_SECONDS`. Never
  re-double the cooldown and never re-alarm, or one outage with N workers races the cooldown to its cap
  and delays recovery for reasons unrelated to the provider.
- **`breaker_opened_at` is only written deliberately.** The apply step is three-valued —
  set-to-now / clear / leave-alone. As a boolean it read as "now() or else NULL", so the transition
  that merely bumps a failure counter wiped the very column the cooldown is measured from.

The probe flag on the dispatch decision is informational only. The transition reads the **locked
row's** state, which already says `half_open` for a probe and cannot be stale.

**41. A skipped provider is recorded, never silent.** *(step 8)*
`provider_calls.status` carries `budget_exceeded`, `breaker_open` and `provider_disabled`, and a
skipped provider stays in `providers_attempted` while being absent from `providers_succeeded` —
exactly like a timeout. Partial coverage has to be visible (CLAUDE.md §7.5); a silent skip is
indistinguishable from "found nothing", which is the distinction this whole subsystem defends.

**42. Tiering is never silent.** *(step 8)*
`search_seeds.scan_tier` and `next_scan_after` are exposed on `GET /v1/search/runs/{run_id}` so the
proxy can state a user's real monitoring cadence. Someone on `dormant` who believes they are scanned
weekly is being misled about a safety product, which is a worse failure than the cost the tier saves.

A run where **no provider succeeded** never changes the tier. It produced no evidence either way, and
demoting a user's cadence because our own integration was down would take the cost saving out of
exactly the wrong place.

A seed created by `POST /v1/seeds` is written with `scan_tier = 'new'` explicitly rather than left to
the column default. The default (`'standard'`) is the safe fallback for a row nobody tiered; only the
creating write knows the seed is genuinely new. Left to the default, the whole `new` branch of
`search/cadence.py` was unreachable, `SCAN_NEW_TIER_WEEKS` had no effect at any value, and the API
advertised a tier it could never return.

**43. A run refused at dispatch is `refused`, never `completed`.** *(step 8)*
Eligibility is re-read on `claim_run`, not trusted from run creation: `subjects.discovery_eligible` is
deliberately mutable (a DOB correction at re-enrolment writes it), and a queued backlog or a stale
claim can put minutes between the route's check and the dispatch. Without the re-read, the route's 403
guarantee holds only at the instant of the request, and #8b promises more than that.

The run row already exists, so it cannot be made to disappear. What it must not do is read as
`completed`: a completed run with zero results says "we looked and found nothing" about a search that
never ran. `refused` + one `audit_log` row carrying `refused_at: dispatch` is the honest record, and
the proxy can render it.

A missing subject row reads as **ineligible**, via `LEFT JOIN` + `COALESCE(..., false)`. An inner join
would return no row, which `claim_run` reports as "not claimable" — indistinguishable from a duplicate
delivery, so the run would be retried forever instead of refused once.
