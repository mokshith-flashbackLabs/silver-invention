# Task 06 — The `svc` contract views, a fourth feedback signal, and a floors endpoint

Run on `main`, after 01–05. Three asks from the proxy team, in one task because the first two are
coupled.

Their full document is worth reading first if it is to hand — it is careful, it cites `file:line`,
and it corrected two of our own docs against our code. This brief is the answer, not a summary.

---

## Ask 1 — the `svc` contract views (BLOCKING for them)

### What is actually wrong

`PROXY_INTEGRATION.md` §6 grants the proxy `SELECT` on `report.reports`, `report.report_hits`, and
`report.hit_feedback`.

**None of those tables has ever existed here.** They come from an early `SCHEMA.md` draft that had a
"report module". We built `infringements` and `attestations` instead — different names, different
shape. The grant names a schema that was never created.

Separately, the proxy's `src/services/contract/readers.ts` reads four views in an `svc` schema. Those
were specified in *their* docs as a contract surface and never appeared in any of our nine build
steps. They built against a contract nobody asked us to implement.

Neither is their mistake. Fix §6 as part of this task.

### The decision: build the views here (their option A)

The proxy proposed three shapes. **A** — we own the views — is correct, and the deciding reason is not
the one they gave.

They argued single-source-of-truth for `live_exposure_count`, which is true. The stronger reason:
**their own views JOIN against ours.** `v_person_enrolment_state` feeds their `v_covered_persons`, and
their migration 0001 already does `LEFT JOIN profile.v_consent_eligibility`. You cannot JOIN against
HTTP, which also rules out the option nobody listed — extending `GET /v1/search/infringements`
instead. That works for the report screen and fails for coverage.

**State the cost on the record in `PROXY_INTEGRATION.md`:** two repos now share a database. These four
views become a **versioned contract**. We may add columns freely; we may never remove or retype one
without a coordinated deploy. That is tighter coupling than anything else in this architecture and it
is being accepted deliberately, not by default.

### Migration

Number it to follow whatever is currently highest.

```sql
CREATE SCHEMA IF NOT EXISTS svc;

CREATE ROLE imageshield_proxy_ro NOLOGIN;   -- if absent
GRANT USAGE ON SCHEMA svc TO imageshield_proxy_ro;
```

**`SELECT` on the four views only.** No grant on any base table, no grant on `public`. If the proxy
can reach `enrolments` or `attributed_faces` directly, the contract is not a contract.

Verify by attempting `SELECT * FROM public.enrolments` as that role — it must fail with a permission
error, not be caught by application logic.

### The four views

Column lists are the proxy's, taken verbatim from their document. Do not rename to match our internal
vocabulary — see the note on translation at the end.

```sql
CREATE VIEW svc.v_person_enrolment_state AS
SELECT
  e.user_ref                              AS person_ref,
  e.status,
  e.model_id,
  e.created_at                            AS enrolled_at
FROM enrolments e
WHERE e.status = 'active';
```

Never a vector, never an `external_face_id`. `INVARIANTS.md` #14 holds — this carries a status and a
timestamp and nothing else.

```sql
CREATE VIEW svc.v_person_report_summary AS
SELECT
  i.user_ref                              AS person_ref,
  count(*) FILTER (WHERE i.status = 'new')            AS active_reports,
  count(*) FILTER (WHERE i.status IN ('new','acknowledged'))
                                                      AS unresolved_matches,
  count(*) FILTER (WHERE i.url_alive
                     AND i.status NOT IN ('dismissed_not_me','authorised'))
                                                      AS live_exposure_count,
  max(r.completed_at)                     AS last_run_at,
  min(r.completed_at) FILTER (WHERE r.status = 'completed')
                                          AS first_scan_completed_at,
  ...                                     AS monitored_sources
FROM infringements i
LEFT JOIN search_runs r ON r.user_ref = i.user_ref
GROUP BY i.user_ref;
```

Sketch, not final — get the aggregation shape right yourself, and make sure a person with zero
infringements still appears with zeroes rather than vanishing from the view. A missing row and a row
of zeroes render very differently on a home screen.

`monitored_sources` should be the count of distinct enabled providers that have actually returned for
this person, not the count of configured ones. "We monitor 2 sources" when one has an open breaker is
a false claim.

**`live_exposure_count` depends on Ask 2.** See below.

```sql
CREATE VIEW svc.v_person_hits AS
SELECT
  i.infringement_id                       AS hit_id,
  NULL::uuid                              AS report_id,      -- no reports table; see note
  i.user_ref                              AS person_ref,
  s.source_object_ref                     AS source_photo_id,
  i.status                                AS hit_status,
  i.last_checked_at,
  a.attestation_id                        AS match_id,
  c.source_domain,
  i.page_url                              AS host_page_url,
  af.bbox                                 AS face_bbox,
  NULL::text                              AS title,
  i.first_seen_at                         AS detected_at,
  a.band                                  AS match_status,
  f.signal                                AS match_action,
  <derived>                               AS match_lifecycle,
  <derived>                               AS resolved_at,
  NULL::text                              AS resolution_note,
  (SELECT count(*) FROM attestations x
    WHERE x.infringement_id = i.infringement_id)  AS provider_count,
  a.provider_score                        AS score
FROM infringements i
...;
```

Several of their columns have no source here — `report_id`, `title`, `resolution_note`. Return them
as typed NULLs rather than omitting them; a missing column breaks their reader, a NULL does not.
Tell them which are permanently null so they do not build UI expecting them.

**`v_person_hits` deliberately omits `image_url`, `thumbnail_url`, and `evidence_image_url`. Keep it
that way.** They reached the same conclusion we did in the 0005 comment: the column stays on the row
as evidence and does not travel on a user-facing read. If those ever need to reach a client it is
through a separate, access-logged path with its own authorisation — never by widening this view.

```sql
CREATE VIEW svc.v_person_liveness_attempts AS
SELECT
  user_ref                                AS person_ref,
  count(*)                                AS attempts_24h,
  max(created_at)                         AS last_attempt_at
FROM liveness_sessions
WHERE created_at > now() - interval '24 hours'
GROUP BY user_ref;
```

Shape is ours to choose; they only need the pre-check. Match whatever
`LIVENESS_MAX_ATTEMPTS_24H` actually enforces rather than hardcoding 24 hours in two places.

### `match_lifecycle` — two of its four values cannot occur in v1

They want `'open' | 'takedown_requested' | 'removed' | 'url_dead'`.

```
open                url_alive = true, status not terminal
url_dead            url_alive = false
takedown_requested  UNREACHABLE — takedown is not in v1
removed             UNREACHABLE — same
```

Build the column with all four values so their reader does not need changing later. **Document that
two never appear yet**, and tell them, so nobody ships UI for a state the system cannot produce.

### Why they need these two columns at all

Worth understanding rather than just implementing. In the legacy system, marking a hit as
"this is abuse of me" left it unresolved, and every unresolved match cost 18 points. Dismissing a hit
**improved** your score while reporting abuse permanently depressed it.

Their scoring now reads `live_exposure_count` so that an item still up costs points and one removed or
dead does not, regardless of what the user clicked. **Reporting abuse must never make the number
worse.** That is what these columns are for.

---

## Ask 2 — a fourth feedback signal

`POST /v1/infringements/{id}/feedback` takes `not_me | confirmed | uncertain`. Their action
vocabulary has one value with nowhere to go:

```
theirs:  'infringement'  'not_infringement'  'not_me'
ours:    'confirmed'      ???                'not_me'
```

`not_infringement` means **"this is me, and it is authorised"** — my own post, a licensed use, a photo
I published myself.

Mapping it to `uncertain` would be wrong twice: it records the opposite of what the user said, and
`uncertain` leaves `infringements.status` unchanged, so the hit never resolves. They have dropped the
action from their client rather than record a false statement, which was the right call.

**Add a fourth signal.**

```
signal 'authorised'  ->  infringements.status = 'authorised'
```

Requirements:

- It **terminates**. The hit is resolved, not left open.
- It is **excluded from `live_exposure_count`** — hence the filter in the view above. Without this,
  a user whose own licensed photo is flagged keeps paying exposure points with no way to clear it:
  a milder version of the exact inversion Ask 1 exists to fix.
- `uncertain` stays exactly as it is. Keeping it distinct is right; do not widen it.

Update the CHECK constraint on `infringement_feedback.signal`, the status vocabulary, and the
`PROXY_INTEGRATION.md` table.

---

## Ask 3 — publish the floors

Neither side can verify the other's numbers today. `ATTRIBUTION_MAX_CANDIDATES` on their side
documents *our* floor and enforces nothing; `MIN_DISCOVERY_AGE` is carried independently in both
repos, and if v2 changes it the `subject_is_adult` boolean means something different on each side of
the boundary.

```
GET /v1/config/floors        X-Service-Token
-> 200 {
     min_discovery_age:              int,
     min_enrolment_age:              int,
     attribution_max_candidates:     int,
     attribution_match_threshold:    string   # decimal as string
   }
```

Read straight from config — no separate constant, or the endpoint lies the moment someone edits one
and not the other. They assert against it at boot and refuse to start on a mismatch, which turns a
silent divergence into a failed deploy.

Decimals cross as strings, matching `/v1/admin/providers/health`.

---

## Their §4 — two things they found by reading our source

Neither is a request. Both are places our doc and our code differ. They followed the code, which was
correct.

**Empty `candidate_refs` is a 422 our doc does not mention.** Our validator refuses it, rightly — a
zero-candidate call is a no-op that still bills a `DetectFaces` and N searches. But they were going to
send it: a household with a lapsed subscription has no covered persons, which is normal rather than
exceptional. Add a line to `PROXY_INTEGRATION.md` §4 under `/v1/attribute`.

**`401` and `422` keep FastAPI's default envelope**, not our `{error:{code,message,retryable,
request_id}}` shape. Their client handles both. Either bring those two into the standard envelope or
document the exception — a consumer parsing `error.code` unconditionally reads an empty string there.
Bringing them into the envelope is better; it is a handler and an exception hook.

---

## Doc updates, same commit

- **`PROXY_INTEGRATION.md` §6** — rewrite. Delete the `report.*` grants; they name tables that never
  existed. Replace with the four `svc` views, the `imageshield_proxy_ro` role, and the versioned-
  contract statement.
- **`PROXY_INTEGRATION.md` §4** — fourth feedback signal, the floors endpoint, empty
  `candidate_refs` → 422.
- **`SCHEMA.md`** — the `svc` schema and the four views, with which columns are permanently null.
- **`CLAUDE.md` §3** — the proxy now has read access to `svc`, and to nothing else.

---

## One thing to raise with them, not decide alone

These views translate our vocabulary into theirs: `infringements` → `hits`, `attestations` →
`matches`. That is a legitimate use of a view, and it also **freezes the split permanently.**

Worth a deliberate choice while there is no production data: translate in the view forever, or rename
on one side now. Not ours to decide unilaterally — put it to them.

---

## Done when

- the four views exist and `readers.ts` reads them unchanged — confirm the column names against their
  document, not against our table names
- `imageshield_proxy_ro` can `SELECT` the four views and **nothing else**; a direct
  `SELECT * FROM public.enrolments` under that role fails with a permission error
- a person with zero infringements appears in `v_person_report_summary` with zeroes, not absent
- `live_exposure_count` excludes both `dismissed_not_me` and `authorised`
- `signal = 'authorised'` resolves the infringement, and a subsequent
  `v_person_report_summary` shows `live_exposure_count` one lower
- `match_lifecycle` returns `open` and `url_dead` correctly; the other two are documented as
  unreachable in v1
- `GET /v1/config/floors` returns values read from config, not constants — assert by changing config
  and seeing the response change
- `401` and `422` either use the standard error envelope or the exception is documented
- the four doc updates are in the same commit

Stop when done.

---

## Standing rules

```
- Cite file:line when describing existing behaviour. Mark anything not read
  directly as INFERRED.
- If anything here conflicts with CLAUDE.md §4, STOP AND ASK.
- Doc corrections land in the same commit as the code they describe.
- When the task is done, STOP.
```
