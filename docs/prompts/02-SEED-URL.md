# Out-of-band task — seed URL lifetime, and wiring screen 16

**Not a numbered step.** Two changes that share one contract, so they land together.
Run on `main`, after 01-CONSENT-REF (they touch the same endpoints).

---

## Part 1 — the bug

`search_seeds.source_object_uri` stores a **presigned GET URL**. `SeedCreateRequest` validates it as
`https://` and its comment says *"proxy's S3, http(s) presigned GET — never s3://"*. `ClaimedRun`
reads it into `seed_url`, and `hive.py` posts it as `data={"url": seed_url}`.

The intent is right — no image bytes transit this service. The **storage location** is wrong.

A presigned URL is a short-lived credential, not a durable identifier. SigV4 caps at 7 days. So:

| | |
|---|---|
| Week 1 | Seed created, URL valid, Hive fetches, works |
| Week 2+ | Expired. S3 returns 403. Hive cannot fetch. Permanently. |

**This does not appear in testing** — fresh seeds work. It surfaces on the second scheduled scan of
any seed, a week later, and presents as *Hive is failing* rather than *our URLs expired*. Step 8's
zero-successful-calls alarm would fire and point at the wrong thing.

### The fix — separate the identifier from the credential

```
search_seeds.source_object_ref   durable, opaque. NOT a URL. Reconciliation only.
search_runs.seed_url             the presigned GET. Per-run, expiring, minted by
                                 the proxy at enqueue.
ClaimedRun.seed_url              reads from the RUN, not the seed.
```

The expiring thing lives on the expiring object. S3 credentials stay entirely on the proxy. No
services-to-proxy call is introduced.

### Migration

Number it to follow whatever is currently highest in `migrations/`.

```sql
-- up
ALTER TABLE search_seeds RENAME COLUMN source_object_uri TO source_object_ref;

COMMENT ON COLUMN search_seeds.source_object_ref IS
  'Opaque durable reference to the proxy''s S3 object. NEVER a presigned URL — '
  'those expire and this column does not. The proxy resolves it and mints a '
  'fresh presigned GET per search run.';

ALTER TABLE search_runs ADD COLUMN seed_url TEXT;

-- Existing rows hold expired or soon-to-expire presigned URLs. They are dev
-- data and cannot be repaired — a presigned URL cannot be turned back into an
-- object key. Report the count rather than silently rewriting them.
UPDATE search_runs SET seed_url = '' WHERE seed_url IS NULL;
ALTER TABLE search_runs ALTER COLUMN seed_url SET NOT NULL;
```

Report how many `search_seeds` rows still hold a presigned URL in `source_object_ref`. Those seeds
are dead and the proxy must re-register them; do not attempt to salvage.

### Contract changes

```
POST /v1/seeds
  { user_ref, seed_kind, source_object_ref }
  source_object_ref: opaque string, NOT a URL.
  REJECT with 422 anything starting http:// or https://, and anything containing
  X-Amz-Signature. The old validator REQUIRED a URL; invert it. A presigned URL
  arriving here is the exact bug this task fixes, so it must fail loudly.

POST /v1/search
  { user_ref, seed_id, seed_url, providers? }
  seed_url: REQUIRED. A freshly-minted presigned GET, https:// only.
  Stored on search_runs. Used at dispatch. Never persisted beyond the run.
  Absent -> 400 seed_url_required. No default, no fallback to the seed.
```

`ClaimedRun.seed_url` now reads `search_runs.seed_url`. `SeedRow.source_object_uri` becomes
`source_object_ref`.

**If the URL expires between enqueue and dispatch**, providers fail normally — the run completes,
`providers_succeeded` is empty, cadence is unchanged (`should_retier` already guards this). The proxy
re-enqueues with a fresh URL. Do not add a refresh path on this side; that would need S3 credentials.

The proxy must mint with a **minimum 15-minute TTL**. Document it in `PROXY_INTEGRATION.md`; we
cannot enforce it.

---

## Part 2 — wiring screen 16

Screen 16 of the mobile onboarding says: *"Add up to 50 photos that feature your face that you've
recently shared on social media. Include your profile pictures. We'll monitor for their use and abuse
online."*

It collects the photos and writes their local file paths to AsyncStorage. **Nothing is uploaded.**
So the only seed monitoring has ever had is the liveness `ReferenceImage` — a selfie taken thirty
seconds earlier that nobody has ever reposted.

This matters more than its size suggests. Hive is image search: it finds *that image*, altered or
reposted. Searching a private selfie finds nothing by construction. Searching the photos someone
actually published is what makes the `derived_edit` case — a nudify edit of a real photo — findable
at all.

**Nothing in this repo changes for Part 2.** `POST /v1/seeds` already accepts
`seed_kind = 'user_supplied'`. The work is proxy-side and mobile-side. It is described here so the
contract lives in one place.

---

## Done when

- the migration runs clean forward and backward
- `POST /v1/seeds` **rejects** an `https://` value for `source_object_ref` with 422 — assert with a
  real presigned-URL-shaped string
- `POST /v1/search` without `seed_url` returns `400 seed_url_required`
- `POST /v1/search` with a non-https `seed_url` returns 422
- `ClaimedRun.seed_url` demonstrably comes from `search_runs`, not `search_seeds` — assert with a run
  whose seed row holds a deliberately wrong value
- a run whose `seed_url` 403s completes with empty `providers_succeeded` and **no cadence change**
- the count of `search_seeds` rows still holding a URL-shaped `source_object_ref` is reported
- `PROXY_INTEGRATION.md` documents: durable ref at seed creation, fresh presigned GET per search,
  ≥15-minute TTL, proxy re-enqueues on expiry

---

## Proxy-side, for the handoff

Not our code. Send with the contract change.

**Seed registration**

- The upload endpoint already exists (`ARCHITECTURE.md:333` — limit check, presigned PUTs, client
  PUTs direct to S3). After an upload confirms, call `POST /v1/seeds` with the **durable object key**,
  not a URL.
- Store the returned `seed_id` on `media.photos` so the two systems reconcile.
- `seed_kind = 'user_supplied'` for screen-16 photos; `'enrolment'` for the liveness ReferenceImage.

**Search dispatch**

- Mint a fresh presigned GET at the moment of enqueue, ≥15 minutes, and pass it as `seed_url`.
- On a run completing with empty `providers_succeeded` and fetch-shaped failures, re-enqueue with a
  fresh URL rather than treating it as a provider outage.

**Mobile**

- Screen 16 currently writes URIs to AsyncStorage in `proceed()`. Replace with: request presigned
  PUTs, upload directly to S3, confirm to the proxy. Delete the AsyncStorage path.
- The consent gate on photo selection stays as-is.

---

## Standing rules

```
- Cite file:line when describing existing behaviour. Mark anything not read
  directly as INFERRED.
- If anything here conflicts with CLAUDE.md §4 (invariants), STOP AND ASK.
- Doc corrections land in the same commit as the code they describe.
- When the task is done, STOP. Report before starting the next one.
```
