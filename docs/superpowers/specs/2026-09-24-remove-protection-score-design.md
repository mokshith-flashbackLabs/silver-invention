# Remove the services protection score — design

*2026-09-24. Approved in conversation; awaiting written-spec review.*
*Three repos: `image_flashbacklabs` (this one, most of the work), `image_backend`,
`imageshieldConsole`. Each half ships under its own repo's rules.*

## 0. What was asked, and why

**Owner, 2026-09-24:** "why there is internal score, remove that … we are not using
that anywhere." It started with the threat-event form: next to Severity, which
does move the user's score, sat Penalty ("moves the services protection score.
Control room only, no user ever sees this one"). Nobody could say what that
number was for.

**Verified, not assumed.** Services have kept a *protection score* per person
since the 2026-08-19 push (`score/`: engine, journal, `tick` drift-healer,
`recommendations/catalog.py`). It stopped being the user's number on
2026-08-26: the app's **Likeness Health Score** is computed only in
`image_backend/src/score/`. The backend is granted `svc.v_person_score`,
`v_person_score_events` and `v_person_recommendations` and **reads none of them**
(its `src/services/contract/views.ts` says so, and none of them is in its
readiness set). The only place the protection score can be seen is one
developer-tier debug relay, `GET /v1/admin/scores/{userRef}`, plus the console's
"Score lookup" page built on it. Since 2026-09-17 threat events have moved the
user's score by **severity** through the backend's own term (0049), so
`penalty` feeds only the unused number.

**Decision (owner, option A):** delete all the code now. **Keep the tables and
views for one release**, written by nothing and read by nothing, and drop them in
a later migration once dev and prod have run cleanly without them. That keeps
this change reversible. Option B, dropping them now, was rejected: it can't be
undone, it deletes the score journal history on both environments, and it forces
a coordinated change to the `svc` contract.

## 1. Services (`image_flashbacklabs`)

### 1.1 Delete

- `src/imageshield/score/` (engine, store, tick) and
  `src/imageshield/recommendations/`.
- `http/routes/admin_scores.py` and its router registration.
- `score_store` construction in `http/app.py` and `get_score_store` in
  `http/deps.py`.
- Every `score_*` setting in `config.py`, along with its validation and its
  boot-time checks. Before deleting an env var from the task defs, check that
  config ignores unknown env: a strict settings model would refuse to boot on a
  leftover key. Remove the keys from `infra/ecs/*.json` in the same change.
- The `score-tick` container in `infra/ecs/imageshield-dev-confirm.json` and
  `infra/ecs/prod/confirm.json`.

### 1.2 Unwire the recompute calls

Remove the score recompute from each of these: `confirm/worker.py`,
`search/worker.py`, and the routes `attribution.py`, `infringements.py`,
`liveness.py`, `search.py`, `admin_review.py` and `admin_threat_events.py`.

Every one of those calls is already swallow-and-log: it runs after the
triggering write has committed, and it can never change the response. So
deleting them changes no request's outcome. Where a response carries a field
derived from the score (check each route), the field goes too, and the change is
recorded in `PROXY_INTEGRATION.md`.

**Machine triage stays.** `confirm/`'s severity triage and review-queue ordering
(INVARIANTS #47) have nothing to do with the protection score.

### 1.3 Threat events stop taking a penalty

- `ThreatEventCreateRequest.penalty` is removed. The create body is
  `extra='forbid'`, so for one release the field is accepted **and ignored**
  (`penalty: Decimal | None = None`, unused), so an older backend still sending
  it gets 201 rather than 422. Drop it after both sides deploy.
- `threats/store.py` stops writing `penalty` and `penalty_applied`, and the list
  response stops returning `penalty`.
- **Migration `0037_threat_penalty_optional`.** It makes
  `threat_events.penalty` and `threat_event_matches.penalty_applied` nullable,
  and drops the `penalty > 0` CHECK only in the form `penalty IS NULL OR penalty
  > 0`. Down: set NULLs to `0.01`, then restore `NOT NULL`. It's reversible.

### 1.4 Tables and views stay

`protection_scores`, `score_events`, `recommendations` and the three `svc` views
over them are **untouched**: still granted, now never written. The follow-up
migration that drops them is recorded as open in `SCHEMA.md` and the manual, not
built here.

### 1.5 Docs

Edit the files in place, never rewriting them:
- `CLAUDE.md` §6: drop the "Protection score + recommendations" row and the
  "Threat events … score effect" wording, and add a dated note.
- `INVARIANTS.md` #44–47: #44–46 are retired with a dated reason, and #47's
  triage half stays. #21 stays; it is about the backend's single score.
- `SCHEMA.md` §2b/2c: note the dormant tables and the nullable penalty.
- `PROXY_INTEGRATION.md`: the removed route and the ignored `penalty`.

### 1.6 Tests

- Delete the tests for the removed modules.
- Threat-event create and retract succeed with no penalty, and also with an
  ignored penalty. They write no row to `protection_scores` or `score_events`.
- `GET /v1/admin/scores/{ref}` is gone.
- The boundary tests still pass.
- The migration applies up, down and up again.

## 2. Backend (`image_backend`, `release/sep-1`)

- Remove the `/v1/admin/scores/:userRef` route, `AdminServicesClient.score`, its
  fake, its IDOR registry entry, and the comments in `src/admin/persons/routes.ts`
  that point at it.
- `threatEventBody`: `penalty` becomes optional and is **ignored**, never
  forwarded. That covers one release while the console still sends it.
  `GET /v1/admin/threat-events` strips `penalty` from each item, so no client
  can show it.
- Tests: create with and without `penalty` both give 201. The body relayed to
  the services fake carries no `penalty`. A list item has no `penalty`. The
  scores route answers the unknown-route 404.
- Docs, edited in place: `docs/CONTROL-ROOM-API-SHAPES.md`,
  `docs/CONTROL-ROOM-CONTRACT-THREAT-ACTIONS.md` (§2's example body) and
  `docs/CLAUDE.md` §6 (the three views stay granted and unread; the protection
  score no longer exists upstream).

## 3. Console (`imageshieldConsole`)

A prompt for whoever owns it:
`image_backend/docs/prompts/CONTROL-ROOM-REMOVE-PENALTY-PROMPT.md`. It covers:
- remove the Penalty field, its state, its validation and the `penalty` type
  field;
- remove the "matched for the services protection score" line;
- remove the Score lookup page, its route, its nav item and the `score`
  endpoint.

The console's threat screens were being refactored, uncommitted, on
2026-09-24, so the prompt names symbols to search for, not line numbers.

## 4. Deploy order, dev first

**Services first, because the services running today reject a create without
`penalty > 0`.** Until new services are live, a backend that stopped forwarding
a penalty would break every threat-event create.

1. **Services**, following the runbook. Migration `0037` runs before the new
   code (the migrate-services task already does this). From then on, a missing
   penalty is fine and a sent one is ignored. The backend running at that point
   still sends the operator's penalty, which is harmless.
2. **Backend.** It stops forwarding `penalty` and ignores one sent by a client.
   Deploy it only once step 1 is live on the same environment.
3. **Console**, any time after step 2. The backend ignores a `penalty` from the
   old console, and a console without the field sends none.

If the backend has to go out first for some other reason, it can forward a
fixed `0.08` until services are confirmed, then drop that in a one-line
follow-up. Services-first means that isn't needed.

Prod follows the same order. It waits on the in-flight `release/prod-sep15`
port.

## 5. Out of scope

- Dropping the dormant tables and views. That is the follow-up migration.
- Any change to the user's Likeness Health Score. It never read the protection
  score, so nothing moves.
