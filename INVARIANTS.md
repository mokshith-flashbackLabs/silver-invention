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

**2. No enrolment without a passed liveness session and a signed consent record.**
Enforced by `NOT NULL` FKs on `enrolments`. Also assert in application code before calling
`IndexFaces`, because a future migration could relax the constraint.

Check: attempt to create an enrolment with a `failed` liveness session. It must raise.

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

**8. `MIN_ENROLMENT_AGE` is read from config at request time.**
Not a constant, not inlined. v1 ships at 18. v2 lowers it. That must not be a code change.

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
uninterpretable.

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
