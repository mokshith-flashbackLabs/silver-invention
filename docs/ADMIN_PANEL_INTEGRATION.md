# Admin Panel Integration — contract + build prompt

**Audience:** whoever builds an admin/ops panel UI against the ImageShield services admin API —
a frontend team, another repo, or an AI agent handed the prompt at the bottom of this file.

**Status of the surface:** every endpoint below is live on `main` (protection-score push,
2026-08-20). A minimal server-rendered console already ships in this repo
(`src/imageshield/console/` — port 8082); it is the **reference client** for these contracts. A
richer panel replaces or sits beside it; the API does not change either way.

---

## 1. Hard rules — read before designing anything

0. **Topology (owner's decision, 2026-08-20): nothing external talks to the services — the
   panel goes through the backend.** The admin panel's browser frontend talks ONLY to the
   backend (the Node/proxy repo); the backend holds `X-Service-Token`, `X-Admin-Service-Token`
   and `X-Fetcher-Token` and proxies these admin calls over the private network. Every endpoint
   below is therefore a contract between the BACKEND and the services; the frontend's contract
   is whatever admin routes the backend exposes on top of it. The only direct-to-services
   client is the co-located ops console shipped in this repo (`console/`, same box, private) —
   the reference implementation of the proxying pattern, not a license for external callers.
1. **This surface is operator-facing, never user-facing.** Reachable only on the private
   network / VPN; no end user, end-user session, or public DNS name may ever reach it — and per
   rule 0, not even an operator's browser reaches the services directly.
2. **Two machine tokens on every request:** `X-Service-Token` and `X-Admin-Service-Token`
   (they must differ; the service refuses to boot otherwise). These identify the *panel*, not
   the person.
3. **Operator identity is data, not auth.** Every write carries an `operator` string in the
   body. It lands in `audit_log` and in `decided_by`/`created_by` columns. Your panel must
   authenticate its human operators itself (the shipped console uses HTTP Basic with
   per-operator tokens + CSRF double-submit — copy that or better) and must pass the real
   operator name, never a shared constant.
4. **CSRF is your job.** The API is token-authenticated and stateless; if your panel holds
   those tokens server-side and exposes browser forms, protect the forms
   (`console/auth.py::make_csrf_token` shows the shipped pattern).
5. **Pixels are rendered live through the fetcher, blurred by default.** Never persist, proxy,
   cache, or hotlink an infringing image. The review card gives you `image_url` +
   `triage.best_face_bbox`; render a crop via the fetcher's `/v1/crop` with `blur=true`, and
   offer reveal (`blur=false`) only behind an explicit operator click. Reviewer welfare is a
   design requirement, not a styling choice.
6. **Errors** (4xx/5xx) arrive as `{"error": {"code", "message", "retryable", "request_id"}}`.
   `422` adds `error.details` (`loc` + `msg`). Show `request_id` in error toasts — it is the
   log-correlation handle.
7. **Decimals cross as strings** (`penalty`, spend figures). Never parse them as floats for
   anything you send back.

## 2. Base URLs and auth

| Upstream | Base (dev) | Auth header(s) |
|---|---|---|
| Services admin API | `http://<services-host>:8081` | `X-Service-Token`, `X-Admin-Service-Token` |
| Fetcher (crops) | `http://<fetcher-host>:8083` | `X-Fetcher-Token` |

All admin routes live under `/v1/admin/…`. Missing/wrong tokens → `401` envelope.

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

**UI obligations:** blurred crop by default with click-to-reveal; the severity override select
defaults to the triage value; `confirmed` on an `ncii_suspected` hit is the highest-consequence
action in the whole product — make it deliberate, never one accidental click.

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
- `POST /v1/admin/providers/{provider_id}/disable` / `/enable` — body `{"reason": "…"}`. The
  kill switch.
- `POST /v1/admin/providers/{provider_id}/breaker/reset` — body `{"reason": "…"}`.

`rekognition_confirm` appears here like any provider — its budget/breaker govern the confirm
pipeline's Rekognition spend.

## 7. Fetcher (crop rendering)

`POST {fetcher}/v1/crop` with `X-Fetcher-Token`:
```json
{ "url": "<image_url from the review card>",
  "bbox": {"x":0.1,"y":0.2,"w":0.3,"h":0.4},        // triage.best_face_bbox
  "blur": true }
→ 200 image/jpeg bytes
```
Errors: `400 refused_private_address | not_an_image | crop_too_small | redirect_limit`,
`413 too_large`, `502 unfetchable`, `401` bad token. Your panel proxies this server-side (the
browser must never hold `X-Fetcher-Token`); render failures as "crop unavailable — review by
URL context" rather than an error page, because unfetchable hits are still decidable.

## 8. Things the panel must NOT build

- No auto-decision, bulk-confirm, or timeout-driven promotion of review tasks (#19).
- No storing/caching of fetched images or crops — render and discard.
- No end-user-facing anything: this contract is ops-only.
- No score editing — the score has exactly one writer and it is not an HTTP endpoint.
- Quarantined hits never appear in any of these responses by design; do not add a way to list
  them (that surface is deliberately absent until the legal process exists).

---

## 9. Copy-paste integration prompt

> Build an internal admin panel ("control room") for ImageShield operators against the API
> contract in `docs/ADMIN_PANEL_INTEGRATION.md` of the `image_flashbacklabs` repo — read that
> file first; it is the requirements document and its §1 hard rules are non-negotiable.
> Topology is fixed: the browser frontend talks ONLY to the backend (the Node/proxy repo);
> you implement admin routes in the backend that proxy to the services API and the fetcher
> over the private network, holding all three machine tokens server-side.
>
> Screens: (1) **Review** — poll `GET /v1/admin/review/next`, render the task card with a
> blurred face crop fetched server-side via the fetcher `/v1/crop` (reveal on explicit click),
> triage facts, and a decision form (confirmed + severity override / rejected / uncertain);
> treat 204 as "queue empty" and 404-on-decide as the normal operator race. Show
> `GET /v1/admin/review/queue` depths in the nav. (2) **Threat events** — list, create (domains
> or global, penalty as a decimal string, a confirm step that warns the effect is immediate),
> retract with reason. (3) **Score inspector** — lookup by user_ref, show the score, its four
> components, and the journal as a human-readable "why it moved" feed. (4) **Provider health**
> — the health table plus enable/disable/breaker-reset with reasons.
>
> Constraints: the panel's backend holds `X-Service-Token`, `X-Admin-Service-Token`, and
> `X-Fetcher-Token` server-side only — never in the browser. Authenticate operators yourself,
> pass the real operator name in every write's `operator` field, and CSRF-protect every form.
> All errors follow `{"error":{code,message,retryable,request_id}}` — surface `request_id`.
> Decimals are strings. No image is ever stored, cached, or hotlinked; crops are rendered live
> and blurred by default. Do not build bulk-confirm, auto-decide, quarantine listing, or any
> score-editing affordance. Deploy target is the private network only.
