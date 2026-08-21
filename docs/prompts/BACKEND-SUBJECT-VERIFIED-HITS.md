# Backend task — the subject decides their own hits (paste into a backend-repo session)

**Status of the services side: merged to `main` 2026-08-21 (`8a3549d`), NOT yet deployed.** Dev
still runs image `:486c1b2`, which predates all of this — both endpoints below will 404 against dev
until that image ships, and it will be a *route-miss* 404 with code `not_found`, **not**
`infringement_not_found`. Do not wire your not-found handling against what dev returns today.
Build against this contract, but expect to integration-test only after the services deploy lands. Nothing here needs a new database grant: the two endpoints are HTTP, and the
`svc` view columns they pair with (`confirm_state`, `severity`, `decided_at`) have been live since
0023.

This supersedes item 5 of `BACKEND-SCORE-SURFACE.md` ("only `confirmed` presents as a finding").

---

## The prompt

> Add the subject-verified hits flow to the backend. ImageShield services changed who decides
> whether a match is real: it is now **the subject themselves**, not an operator. After the
> machine triages a hit (Rekognition face-match + moderation), the user is shown a **blurred face
> crop** of it and asked *"is this your photo?"* — and their answer writes the confirm/reject
> decision. Staff never see hit imagery at all; the operator console is metadata-only.
>
> Your job is the pass-through and the card. Two new services endpoints, one presentation rule,
> one copy line.
>
> ### The flow, end to end — **your steps are 5–8**
>
> ```
>  1. SEARCH RUN        a provider (Hive/Google) returns a URL for one of the user's seed photos
>                       -> one infringement + one attestation per provider   [services]
>  2. RUN COMPLETES     EVERY still-unconfirmed hit of that run is enqueued for triage
>                       (the old "most similar" score bars are gone)         [services]
>  3. CONFIRM WORKER    fetch bytes -> pHash dedup -> transcode to JPEG ->
>                       Rekognition face-match against THIS user only + moderation
>                       -> CSAM tripwire -> severity + the hit-image face bbox   [services]
>  4. HIT IS TRIAGED    confirm_state = 'machine_triaged', severity set; the row
>                       surfaces in svc.v_person_hits                        [services]
> ─────────────────────────────────────────────────────────────────────────────────────
>  5. ASK-CARD          you read the view and render the card + question     [BACKEND]
>  6. BLURRED CROP      you call GET /preview and stream it, blurred         [BACKEND]
>  7. REVEAL            on the user's explicit tap, re-call with reveal=true [BACKEND]
>  8. ANSWER            you forward yes/not-me to the decision endpoint      [BACKEND]
> ─────────────────────────────────────────────────────────────────────────────────────
>  9. DECISION LANDS    confirm_state = confirmed|rejected, decided_by='subject',
>                       audit row, review task closed                        [services]
> 10. SCORE MOVES       recompute (cause_kind='subject_decision'); Exposure adjusts,
>                       recommendations re-derive                            [services]
> ```
>
> Steps 1–4 and 9–10 are already built and need nothing from you. Note what is **absent**: there
> is no operator between step 4 and step 5 any more. The user is the reviewer.
>
> **A hit can sit at `unconfirmed` for a long time, and that is not a bug.** If the spend gate at
> step 3 skips (daily budget hit, breaker open, provider killed), the hit deliberately stays
> `unconfirmed` — reviewable but unchecked — until a later run re-enqueues it. Render "being
> checked" indefinitely rather than escalating or timing out; there is no auto-promotion anywhere
> in this system by design.
>
> ### 0. The rule that matters most: `user_ref` comes from the session, never from the client
>
> Both endpoints take the subject's `user_ref` as a parameter, and services trusts it — it is the
> **only** thing standing between a user and someone else's abuse material. Take it from the
> authenticated session server-side and inject it. If a `user_ref` (or `person_ref`, or any
> equivalent) can arrive from the client's query, body, or headers and reach services, that is an
> IDOR against NCII, not a validation bug. Services deliberately answers "not yours" and "does not
> exist" with a byte-identical 404 so it leaks nothing — do not add an error path that
> distinguishes them, and do not log the two differently.
>
> Same topology as everything else: **client → backend → services**. The client never talks to
> services or the fetcher, and `X-Service-Token` stays server-side.
>
> ### 1. `GET /v1/hits/:hitId/preview` — pass through the blurred crop
>
> Proxies `GET {SERVICES}/v1/infringements/{id}/preview?user_ref=<from session>&reveal=<bool>`.
> Streams back `image/jpeg`.
>
> - **Pass `Cache-Control: no-store, private` straight through, and add nothing that caches it.**
>   No CDN, no reverse-proxy cache, no service-worker cache, no disk, no `/tmp`. The bytes exist
>   for one response and then they are gone. Never log the image URL or the response body.
> - `reveal` defaults to **false** (blurred). It flips to true only on an explicit, per-item tap
>   by that user — never on scroll, never on hover, never for a whole list at once, and never in a
>   digest, email, or push notification.
> - Error mapping (all carry the standard `{error:{code,message,retryable,request_id}}` envelope):
>
>   | Services returns | Means | Render |
>   |---|---|---|
>   | `404 infringement_not_found` | absent, not theirs, or invisible (quarantined/duplicate) | generic not-found; never "this belongs to someone else" |
>   | `404 preview_unavailable` | no crop renderable yet (not triaged, no face found, or unfetchable) | the card **without** an image — the user can still answer |
>   | `429 preview_rate_limited` | per-user render ceiling (retryable) | "try again later"; do not retry in a loop |
>   | `502 preview_unavailable_upstream` | fetch/render failed upstream (retryable) | same card, image slot empty; one retry is fine |
>
>   The ceiling is **200 renders per `user_ref` per rolling 24 hours** (config, not a constant),
>   and it counts *attempts*: the render is audit-logged before the image is fetched, so a `502`
>   still consumes one. Budget accordingly — a blurred load plus a reveal is two.
>
>   **`preview_unavailable` is not an error state in the UI.** It is the normal case for a hit
>   that has not triaged yet, and for the "no face detected" case. Show the card, show the domain,
>   omit the image.
>
> ### 2. `PATCH /v1/hits/:hitId` (or a new sub-route) — forward the decision
>
> Posts `{user_ref: <from session>, decision: "confirmed" | "rejected"}` to
> `POST {SERVICES}/v1/infringements/{id}/decision`. Response:
> `{infringement_id, decision, severity, idempotent_replay}`.
>
> - **Idempotent on retry.** The subject re-sending *their own identical* decision returns `200`
>   with `idempotent_replay: true` — treat it as success, not as a double-submit error. Safe to
>   retry on a dropped connection.
> - **`409 decision_conflict`** means a decision already stands that this request cannot replay:
>   either a different decision, **or any operator decision at all — including an operator who
>   recorded the same answer.** Only the subject's own identical decision replays. There is no
>   re-decide in v1: render "this has already been decided — contact support to change it", not a
>   retry button.
> - **`422`** is an invalid body — a `decision` outside the two values, or any extra field
>   (services rejects unknown fields outright). The envelope adds `error.details` with `loc`/`msg`.
>   Map it as a client bug, never as a generic 500.
> - `severity` in the response is the machine's classification, carried through unchanged — the
>   user's answer never edits it. **It can be `null`** (a hit decided before triage ever ran).
>   Urgency copy must have a null branch.
> - Only two values are accepted. "Not sure" is **not** a decision: leave the hit undecided and
>   let them answer later. (If you want to record uncertainty, that is the existing
>   `POST /v1/infringements/{id}/feedback` signal `uncertain`, which is a separate lane and does
>   not decide anything.)
>
> ### 3. The presentation rule — what each state renders
>
> From `svc.v_person_hits.confirm_state` (+ `severity`). This replaces "only confirmed is a
> finding":
>
> | `confirm_state` | Card | Copy |
> |---|---|---|
> | `unconfirmed` | "being checked" — no image (preview 404s) | they may still answer; do not promise a verdict |
> | `machine_triaged`, `severity = ncii_suspected` / `explicit_unmatched` / `benign_copy` | **ask-card + blurred crop** | *"We found a photo that appears to be you — is it?"* |
> | `machine_triaged`, `severity = likely_not_subject` | **ask-card + blurred crop** | *"We found a **similar** photo — is this you?"* — never "we found you" |
> | `machine_triaged`, `severity = unassessed` | ask-card, **no image** (preview 404s) | *"We found a photo we could not check — is this you?"* — nothing was matched; never "appears to be you" |
> | `confirmed` | finding, urgency by `severity` (`ncii_suspected` ≫ `benign_copy`) | decided by them; no ask-card |
> | `rejected` | retire from default views | reachable in a "dismissed" filter at most |
> | quarantined / duplicate | never appear in the view at all | build nothing |
>
> **Do not treat `severity` as a single scale — `unassessed` is not a low score, it is the absence
> of a check.** It means the image could not be fetched or decoded, so no face-match and no
> moderation ever ran on it. Defaulting it into the "appears to be you" copy would assert a match
> the system never made.
>
> The `likely_not_subject` wording is load-bearing, not a nicety. That state means the face-match
> **failed** — the photo may well be a stranger. Framing it as "we found you" would be telling
> someone a stranger's photo is them, which is the exact harm this product exists to avoid. It is
> still shown (and still worth asking about) because a deepfake of the subject can legitimately
> fail face-match — a novel face has no relationship to their enrolment vector.
>
> ### 4. One new score-history copy line
>
> `BACKEND-SCORE-SURFACE.md`'s `cause_kind` map gains: **`subject_decision`** → *"you answered a
> match"*. Expect it to carry a negative `delta` on the `exposure` component when they answer
> **yes** — that is correct and must not be presented as a penalty: confirming a real match means
> the exposure was always there and the number now tells the truth. Answering **no** can only ever
> raise or hold the number.
>
> Three things that will otherwise look like bugs:
> - **A "no" usually journals nothing at all.** A rejected hit was never counted as exposure in
>   the first place, so there is often no delta and therefore no history row. Do not build UI that
>   waits for one.
> - **A "yes" can also journal nothing** — if exposure is already floored at zero, there is no
>   room left to spend.
> - **A `subject_decision` row is not always *about* the decision.** Recompute journals whatever
>   has drifted since the last one under the cause it was called with, so a decaying threat event
>   can surface as a `threat`-component delta labelled `subject_decision`. Key your copy off the
>   `component` field, not off `cause_kind` alone.
>
> ### Copy rules (safety, non-negotiable)
> - Never hot-link the hit. Domain, title and date are shown; the URL stays behind an explicit
>   "copy for my lawyer"-style action (services invariant #22).
> - The crop is blurred by default and revealed only per-item on explicit tap (#23, as amended
>   2026-08-21). A **blurred** crop renders only on the hit's own detail/ask card — never in a
>   summary list, digest, email, or push. An **un-blurred** crop renders on that same card and
>   nowhere else, only after that user's explicit tap on that specific hit.
> - Never state or imply that a hit is confirmed before the user has answered. "Being checked"
>   must not read as "we found you", and no state may read as "you're safe".
> - Scope discipline still applies everywhere (#26): absence is "no matches in monitored sources",
>   never "you're clean".
>
> ### Explicitly out of scope for this repo
> Rendering crops for staff or admin users — **staff never see hit imagery**, so do not proxy
> `/preview` for an operator session under any circumstance, and do not add an admin crop view to
> the panel. Also out: computing or adjusting scores, writing to any base table, bulk decide,
> re-decide, and takedown of any kind (not built anywhere yet).

---

Contract authority: `PROXY_INTEGRATION.md` (services repo) — the route table entries for
`/v1/infringements/{id}/preview` and `/v1/infringements/{id}/decision`, and the section
"Presentation rule, as of 2026-08-21". Design rationale, including why the subject sees
face-match failures and explicit-flagged hits: `docs/superpowers/specs/2026-08-21-subject-verified-hits-design.md`
§0. If a needed column is missing from a `svc` view, ask the services side to ADD it (additive is
free); never work around it with a base table.

**One trap worth naming:** `svc.v_person_hits.face_bbox` is the face box in the user's **own seed
photo**, not in the hit image — it comes from the attribution provenance chain. It is useless for
rendering a hit crop and must not be used for one. The hit image's bbox never leaves the services
side at all; that is why the preview endpoint exists.
