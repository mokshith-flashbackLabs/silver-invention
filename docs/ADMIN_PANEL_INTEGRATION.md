# Admin Panel Integration — contract + build prompt

**Audience:** whoever builds an admin/ops panel UI against the ImageShield services admin API —
a frontend team, another repo, or an AI agent handed the prompt at the bottom of this file.

**Status of the surface:** every endpoint below is live on `main` (protection-score push,
2026-08-20). The reference client used to be a minimal server-rendered console shipped in this
repo (`src/imageshield/console/`) — **retired 2026-08-29.** Staff now reach this surface through
the backend's `/v1/admin/*` operator proxy (`image_backend` spec `2026-08-29-admin-proxy-design.md`
§12); the panel is the frontend team's build against that proxy. The API below does not change
either way — it is still the contract the backend's admin routes mirror.

---

## 1. Hard rules — read before designing anything

0. **Topology (owner's decision, 2026-08-20): nothing external talks to the services — the
   panel goes through the backend.** The admin panel's browser frontend talks ONLY to the
   backend (the Node/proxy repo); the backend holds `X-Service-Token` and `X-Admin-Service-Token`
   and proxies these admin calls over the private network. (The backend separately holds
   `X-Fetcher-Token` for the unrelated subject-facing preview relay — see rule 5 and §7; that
   token is never used for anything in this contract.) Every endpoint below is therefore a
   contract between the BACKEND and the services; the frontend's contract is whatever admin
   routes the backend exposes on top of it. **The backend's `/v1/admin/*` proxy is now the ONLY
   client of this surface** (`image_backend` spec `2026-08-29-admin-proxy-design.md`) — the
   co-located ops console that used to be this repo's reference implementation of the proxying
   pattern was retired 2026-08-29 once that proxy shipped. For the panel, build against the
   backend's own collection, `postman/imageshield-admin.postman_collection.json` (`image_backend`
   repo); the collection below (§2) stays the services-side upstream reference.
1. **This surface is operator-facing, never user-facing.** Reachable only on the private
   network / VPN; no end user, end-user session, or public DNS name may ever reach it — and per
   rule 0, not even an operator's browser reaches the services directly.
2. **Two machine tokens on every request:** `X-Service-Token` and `X-Admin-Service-Token`
   (they must differ; the service refuses to boot otherwise). These identify the *panel*, not
   the person.
3. **Operator identity is data, not auth.** Every write carries an `operator` string in the
   body. It lands in `audit_log` and in `decided_by`/`created_by` columns. The backend proxy
   authenticates its human operators itself (an account + `iam.operator_grants` grant, not HTTP
   Basic — `image_backend` admin-proxy design §5) and injects the granted `display_name` into
   this field server-side; clients of the backend never send `operator` at all.
4. **CSRF is your job.** The API is token-authenticated and stateless; whoever holds these
   tokens server-side and exposes browser forms must protect the forms. The retired console used
   HTTP Basic with per-operator tokens plus CSRF double-submit; the backend proxy sits behind its
   own session model instead.
5. **Staff see ONE image, through ONE route, audited — and `image_url` is never it.**
   *Rewritten 2026-09-14 (spec `2026-09-14-auto-confirm-and-reviewer-feed-design.md`); this
   rule previously read "pixels are never shown to staff".* Face matching runs on Rekognition
   today and is being replaced, and a false-positive rate cannot be measured by a reviewer who
   cannot see the face. So a reviewer may render **the identical blurred image the subject
   would see** — whole frame blurred, the matched face sharpened only on `reveal=true` — and
   only through `GET /v1/admin/infringements/{id}/preview` (§3b). Every view writes an audit
   row naming the operator *before* it renders, and each operator has a daily render ceiling
   (`429 preview_rate_limited`). A quarantined hit renders to nobody, ever.

   **`image_url` remains text-only evidence.** It ships on the reviewer feed and the review
   card for URL-context review, and a panel must **never fetch, proxy, cache, render, hotlink
   or persist** it — nor any other infringing address. The only pixels a panel may display are
   the bytes the preview route returns, and it must send them `Cache-Control: no-store,
   private` onward: do not put them in a blob URL that outlives the view, a service worker
   cache, or a CDN. Article pictures (below) are operator-pasted URLs rendered as links, not
   images.
6. **Errors** (4xx/5xx) arrive as `{"error": {"code", "message", "retryable", "request_id"}}`.
   `422` adds `error.details` (`loc` + `msg`). Show `request_id` in error toasts — it is the
   log-correlation handle.
7. **Decimals cross as strings** (`penalty`, spend figures). Never parse them as floats for
   anything you send back.

## 2. Base URLs and auth

| Upstream | Base (dev) | Auth header(s) |
|---|---|---|
| Services admin API | `http://<services-host>:8081` | `X-Service-Token`, `X-Admin-Service-Token` |

All admin routes live under `/v1/admin/…`. Missing/wrong tokens → `401` envelope.

A Postman collection of this whole surface (22 requests, six folders, test scripts that chain the created ids) lives in the backend repo at `image_backend/postman/imageshield-services-admin.postman_collection.json` (beside the client collection the frontend team already uses), with an environment file beside it whose token values are deliberately empty — fill them from Secrets Manager, never commit them. Its description repeats rule 0: the panel's browser goes through the backend; the collection is the services-side contract the backend's admin routes mirror. The fetcher
(crops) is a separate upstream used by the subject-facing preview flow, not by this contract —
see rule 5 and §7.

## 3. Review queue — the human-only confirm gate

Machine triage orders this queue; **only a decision posted here can make a hit `confirmed` and
user-visible** (INVARIANTS #19, enforced by a database CHECK — the API cannot bypass it).

### `GET /v1/admin/review/next`
The single highest-priority pending task, or **`204` with no body when the queue is empty**
(empty is a normal state — do not render it as an error).

```json
200 {
  "task_id": "uuid",
  "infringement_id": "uuid",
  "user_ref": "uuid",
  "severity": "ncii_suspected | explicit_unmatched | unassessed | benign_copy | likely_not_subject",
  "triage": { "image_url": "...", "best_face_bbox": {"x":0.1,"y":0.2,"w":0.3,"h":0.4},
               "face_match_score": 93.5, "moderation_labels": ["..."],
               "phash_degenerate": false, "skipped": "...", "unfetchable": "..." },
  "image_url": "https://... | null",
  "page_url": "https://...",
  "face_match_score": 93.5,
  "source_domain": "example.com"
}
```
`triage` keys are situational (an unfetchable hit has no bbox). Queue priority is fixed:
`ncii_suspected` → `explicit_unmatched` → `unassessed` → `benign_copy` → `likely_not_subject`,
oldest first within a severity.

### `GET /v1/admin/review/queue`
Per-severity pending counts: `{"ncii_suspected": 2, "benign_copy": 7, ...}` — render as the
queue-depth widget.

### `POST /v1/admin/review/{task_id}/decision`
```json
{ "decision": "confirmed | rejected | uncertain",
  "operator": "alice",
  "severity": "ncii_suspected | ... | null"   // optional OVERRIDE; omitted keeps triage's value
}
```
- `confirmed` / `rejected`: the task closes, the infringement's `confirm_state` changes, the
  person's protection score recomputes (`cause review_decision`). Response:
  `{"infringement_id", "user_ref", "decision", "severity"}`.
- `uncertain`: recorded in the audit log, the task **stays pending** and will come back from
  `/next`. Nothing else changes. There is no timeout that auto-promotes — if the queue backs
  up, the queue backs up.
- `404 review_task_not_found`: the task is not pending any more (another operator decided it,
  or it was quarantined). Refresh `/next` — this is the normal two-operators race, not a bug.

**UI obligations:** **Rewritten 2026-09-14 — see rule 5.** `image_url` and
`triage.best_face_bbox` are still evidence, shown as plain text/numbers, never fetched,
proxied, cached, rendered or hotlinked. What changed is that a reviewer may now render the
hit's blurred preview through `GET /v1/admin/infringements/{id}/preview` (§3b) — the same
image the subject would see, audited per view. That route, and nothing else. The
severity override select defaults to the triage value; `confirmed` on an `ncii_suspected` hit is
the highest-consequence action in the whole product — make it deliberate, never one accidental
click.

## 3b. The reviewer hit feed, verdicts, the preview and the stats *(2026-09-14)*

**Why this exists:** face matching runs on Rekognition today and the team is replacing it with
its own models. Before that swap they need a *measured* false-positive rate, which means a human
looking at real hits and recording whether the machine was right. §3's queue shows one task at a
time and no image; this surface shows every hit, the picture, and a place to record the answer.

**A verdict is a LABEL, not a decision** (owner decision D6). It never changes `confirm_state`
and never touches a review task — §3's decision route is still the only override. A panel must
present the two as different actions with different words; "reject" and "the machine was wrong"
are not the same claim, and conflating them in the UI is how the measurement gets poisoned.

### `GET /v1/admin/hits`
Query: `limit` (default 50, 1–200), `cursor`, `severity`, `confirm_state` (one of
`unconfirmed | machine_triaged | confirmed | rejected | duplicate`), `user_ref`, `since`
(ISO-8601). Every filter is optional and they compose.

```json
200 {
  "hits": [{
    "infringement_id": "uuid", "user_ref": "uuid",
    "source_domain": "example.com", "page_url": "https://...",
    "image_url": "https://... | null",        // TEXT EVIDENCE. Never rendered — rule 5.
    "first_seen_at": "…", "status": "new",
    "confirm_state": "machine_triaged", "severity": "explicit_unmatched | … | null",
    "confirm_decided_by": "subject | <operator> | auto:nsfw | null",
    "confirm_decided_at": "… | null",
    "face_match_score": 91.25,
    "moderation_labels": ["Explicit Nudity"],  // names only
    "duplicate_of": "uuid | null",
    "preview_available": true,                 // can §3b's preview actually render this?
    "review_task":      { "task_id", "status", "decision", "decided_by", "decided_at", "severity" } | null,
    "subject_decision": { "decision", "decided_at" } | null,   // iff the SUBJECT answered
    "latest_verdict":   { "verdict_id", "operator", "verdict", "note", "created_at" } | null,
    "source_object_ref": "photo/… | null", "seed_kind": "face_crop | user_supplied | … | null"
  }],
  "next_cursor": "opaque string | null"
}
```
- **`quarantined` hits never appear**, whatever the filters say, and `quarantined` is not an
  accepted `confirm_state` value (it is a `422`). A quarantined hit is CSAM-suspected;
  escalation out of one is a manual legal process, not a listing.
- **Paging is keyset, and the cursor is opaque.** Pass the `next_cursor` you were given, byte
  for byte. `null` means last page. A cursor you construct or mangle is a
  `422 invalid_cursor` — deliberately not ignored, because silently restarting at page one
  would have a reviewer re-read the same fifty hits believing they had reached the tail.
- `preview_available: false` means the hit has no image address or no face box. Do not offer a
  preview button for it; the route would answer `404 preview_unavailable`.

### `POST /v1/admin/hits/{infringement_id}/verdict` → `201`
```json
{ "verdict": "true_positive | false_positive | unsure", "operator": "alice", "note": "optional" }
```
Returns the created row, including the snapshot of what the machine said **at the moment of the
verdict** (`machine_severity`, `face_match_score`, `confirm_state_at_verdict`) — so a later
severity override cannot rewrite what was measured. `404 infringement_not_found` for an absent
or quarantined hit. `operator` is required (rule 3): a measurement nobody signed is not a
measurement. Verdicts are append-only — a second look writes a second row and the feed shows the
latest.

### `GET /v1/admin/infringements/{id}/preview`
Query `operator` (**required**) and `reveal` (default `false`). Returns `image/jpeg` with
`Cache-Control: no-store, private`.

The same render the subject would get: whole frame blurred, and on `reveal=true` **only the
matched face box** is sharpened — never the frame. There is no parameter that returns a sharp
image, from any caller.

- `404 infringement_not_found` — absent, or quarantined.
- `404 preview_unavailable` — no image address or no face box (i.e. `preview_available: false`).
- `429 preview_rate_limited` — this operator's daily render ceiling. Surface it as "you have
  viewed a lot of hits today", not as an outage; nothing is broken.
- Every successful call writes an audit row naming the operator *before* rendering. A refusal
  writes none.

### `GET /v1/admin/review/stats`
Query `since` (ISO-8601, default 30 days ago). The measurement, three ways:
```json
200 {
  "since": "…",
  "by_severity": [{ "machine_severity", "total", "true_positive", "false_positive",
                    "unsure", "false_positive_rate": 0.5 | null }],
  "subject_agreement": { "compared", "agreed", "disagreed", "agreement_rate": 0.5 | null },
  "by_operator":  [{ "operator", "total", "true_positive", "false_positive", "unsure" }]
}
```
- `by_severity` and `subject_agreement` count the **latest verdict per hit**; `by_operator`
  counts every verdict row, because it is throughput rather than measurement.
- **A `null` rate means nothing was measured. Render it as "—", never as 0%.** `unsure` counts
  in `total` and on neither side of a rate; `subject_agreement` compares only hits the subject
  themselves decided, and excludes `unsure`.
- Rates are plain floats. (Rule 7's decimal-string rule is about money, not rates.)

## 4. Threat events

### `GET /v1/admin/threat-events`
`{"events": [ThreatEventItem, ...]}` where each item is:
`{event_id, kind, title, body, severity(1-5), domains[], is_global, penalty("2.00" string),
starts_at, expires_at, decay_days, status(draft|active|expired|retracted), created_by,
created_at, updated_at}`.

### `POST /v1/admin/threat-events` → `201`
```json
{ "kind": "leak | deepfake_wave | platform_incident | other",
  "title": "…", "body": "…",
  "severity": 3,
  "domains": ["site-a.example", "site-b.example"],   // OR is_global: true — one of the two is required (422 otherwise)
  "is_global": false,
  "penalty": "2.00",                                  // decimal STRING, > 0, NUMERIC(5,2)
  "expires_at": "2026-09-20T00:00:00Z",
  "decay_days": 14,
  "operator": "alice" }
→ { "event_id": "uuid", "matched_count": 41 }
```
Creating an event immediately matches it against users' live hits on those domains (or every
subject when global), drops their scores (bounded, decaying) and spawns their recommendations.
`matched_count` is your confirmation copy ("this will touch 41 people" — show it AFTER, and
consider a preview/confirm step in the UI since the API applies immediately).

### `POST /v1/admin/threat-events/{event_id}/retract`
`{"operator": "alice", "reason": "≥3 chars, ≤500"}` →
`{"event_id", "matched_count", "status": "retracted"}` — reverses the score effect **exactly**
for every matched person. `404 threat_event_not_found` when the event is not active.

## 5. Protection score inspector

### `GET /v1/admin/scores/{user_ref}`
```json
{ "score":  { "score": 78, "components": {"posture":33,"coverage":20,"exposure":25,"threat":0},
              "config_version": "score-v1", "computed_at": "…" },
  "events": [ { "score_event_id": 812, "delta": -6, "component": "exposure",
                "cause_kind": "review_decision", "cause_ref": "uuid",
                "config_version": "score-v1", "score_after": 78, "created_at": "…" }, … ] }
```
`events` is the newest-first journal (limit 50) — render it as the "why the score moved" feed.
`404 score_not_found` = nothing computed yet for that ref (a real state for new users).
`cause_kind` vocabulary: `feedback, enrolment, seed_registered, run_completed, review_decision,
threat_event, threat_retracted, tick`.

## 6. Provider health (pre-existing, unchanged)

- `GET /v1/admin/providers/health` — per-provider spend/breaker/success stats + alarms.
- `POST /v1/admin/providers/{provider_id}/disable` / `/enable` — body
  `{"reason": "…", "operator": "…"}`. The kill switch.
- `POST /v1/admin/providers/{provider_id}/breaker/reset` — body
  `{"reason": "…", "operator": "…"}`.

`operator` is optional; omitted, the audit row records `admin_service_token` instead of a name.
The backend proxy always sends it (injected from the operator's `iam.operator_grants.display_name`).

`rekognition_confirm` appears here like any provider — its budget/breaker govern the confirm
pipeline's Rekognition spend.

## 6b. Articles — operator content for the app feed

Every user sees every published article; nothing here is per-person. Both tokens; `operator` in
every write body.

- `GET /v1/admin/articles?limit=` → `{articles: [...]}` all statuses, newest edited first.
- `POST /v1/admin/articles` → `201 {article_id, status: "draft"}`. Body: `title` (1–200), `summary`
  (≤500), `body` (≤20000, markdown), `images: [{url, alt}]` (≤10), `sources: [{name, url}]` (≤10),
  `operator`. **Every URL must be `https://`** — `http://` is a 422.
- `GET /v1/admin/articles/{id}`, `PUT /v1/admin/articles/{id}` (same body as create; editing a
  published article changes it live).
- `POST /v1/admin/articles/{id}/publish` body `{operator}` → `{article_id, status}`. Draft or
  archived → published; already published → no-op. A re-publish keeps the original `published_at`.
- `POST /v1/admin/articles/{id}/archive` body `{operator, reason}` → removes it from the feed.
- Unknown id → `404 article_not_found`.

Rendering pictures in an ops panel is your call, but show them as links only, never fetched or
embedded, so "no imagery in the control room" stays a rule without carve-outs.

## 7. Fetcher (crop rendering)

**SUPERSEDED 2026-08-21 (rule 5) — not part of this contract.** Staff never see hit imagery, so
no admin/ops surface calls `/v1/crop`; the section below is background on what the endpoint is,
not an instruction to a panel builder. `POST {fetcher}/v1/crop` exists. Its callers are the
**subject-facing** preview route
(`GET /v1/infringements/{id}/preview`, relayed by the proxy as `GET /v1/hits/{id}/preview`) and
the confirm pipeline's own internal moderation pass — neither is an operator surface. No admin/ops
surface — the retired console held no fetcher client and no `X-Fetcher-Token`, and the backend's
admin proxy does not either — has any reason to hold one. Do not build a
crop-fetching path into an ops panel: if staff ever need to see a pixel, that is a rule-5 change
to raise before building, not an integration detail to infer from this endpoint's existence.

## 8. Things the panel must NOT build

- No auto-decision, bulk-confirm, or timeout-driven promotion of review tasks (#19).
- No storing/caching of fetched images or crops — render and discard.
- No end-user-facing anything: this contract is ops-only.
- No score editing — the score has exactly one writer and it is not an HTTP endpoint.
- Quarantined hits never appear in any of these responses by design; do not add a way to list
  them (that surface is deliberately absent until the legal process exists).

---

## 9. Copy-paste integration prompt

**SUPERSEDED 2026-08-21 (rule 5):** the Review screen below no longer fetches or renders a crop
— it was rewritten in place rather than left contradicting rule 5, since this block is meant to
be pasted verbatim to an external team or agent.

> Build an internal admin panel ("control room") for ImageShield operators against the API
> contract in `docs/ADMIN_PANEL_INTEGRATION.md` of the `image_flashbacklabs` repo — read that
> file first; it is the requirements document and its §1 hard rules are non-negotiable.
> Topology is fixed: the browser frontend talks ONLY to the backend (the Node/proxy repo);
> you implement admin routes in the backend that proxy to the services admin API over the
> private network, holding both machine tokens server-side. The fetcher is a separate upstream
> for an unrelated flow (§7) — this panel never talks to it.
>
> Screens: (1) **Review** — poll `GET /v1/admin/review/next`, render the task card as metadata
> only: `image_url` and `source_domain` as plain text (a link if you like, never an embedded or
> fetched image), `triage.best_face_bbox` and `face_match_score` as numbers, never drawn on a
> pixel — rule 5, no crop, no fetcher call, ever — plus a decision form (confirmed + severity
> override / rejected / uncertain); treat 204 as "queue empty" and 404-on-decide as the normal
> operator race. Show
> `GET /v1/admin/review/queue` depths in the nav. (2) **Threat events** — list, create (domains
> or global, penalty as a decimal string, a confirm step that warns the effect is immediate),
> retract with reason. (3) **Score inspector** — lookup by user_ref, show the score, its four
> components, and the journal as a human-readable "why it moved" feed. (4) **Provider health**
> — the health table plus enable/disable/breaker-reset with reasons.
>
> Constraints: the panel's backend holds `X-Service-Token` and `X-Admin-Service-Token`
> server-side only — never in the browser, and it never needs a fetcher token: the fetcher's
> crop route is not part of this contract (rule 5; §7). Authenticate operators yourself, pass
> the real operator name in every write's `operator` field, and CSRF-protect every form. All
> errors follow `{"error":{code,message,retryable,request_id}}` — surface `request_id`.
> Decimals are strings. No pixel of a hit ever reaches an operator, blurred or not; article
> picture URLs are shown as plain links only, never fetched, embedded, cached, or hotlinked. Do
> not build bulk-confirm, auto-decide, quarantine listing, or any score-editing affordance.
> Deploy target is the private network only.
