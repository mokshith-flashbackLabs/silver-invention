# ImageShield services — operations runbook

Written for someone at 3am who did not build this.

Every scenario says what the symptom looks like, what to check, what to do, and
what *not* to do. Where an action is risky, the risk is stated rather than
implied.

**The one to read first if you read only one:** [A provider has returned zero
successful calls for 24h](#5-a-provider-has-returned-zero-successful-calls-for-24h).
It is the only failure here that is invisible from the outside and harmful
while invisible.

---

## Orientation

Seven processes, all from one image except where noted. The first four are v1; the last three are the
2026-08-19 protection-score push (`imageshield-dev-confirm`, `-fetcher` task defs). An eighth,
the control-room console (`-console`), shipped in the same push and was retired 2026-08-29 — staff
now reach its admin API through the backend's `/v1/admin/*` operator proxy.

| Process | Command | What it does |
|---|---|---|
| API | `uvicorn imageshield.http.app:create_app --factory` | Everything the proxy calls |
| Outbox relay | `python -m imageshield.relay` | Postgres outbox → SQS |
| Search worker | `python -m imageshield.search.worker` | Consumes `search:runs`, dispatches providers |
| Recheck worker | `python -m imageshield.recheck.worker` | Weekly HEAD sweep setting `url_alive` |
| Confirm worker | `python -m imageshield.confirm.worker` | Consumes `confirm:hits`: fetch → pHash → face-match (via `attribution/`) → moderation → severity triage into `review_tasks` |
| Score tick | `python -m imageshield.score.tick` | Daily drift-healer: re-runs score recompute for aging effects and any trigger whose recompute crashed after commit |
| Fetcher | `uvicorn imageshield.fetcher.app:create_app --factory --port 8083` | Standalone deployable, no DB credentials. Hands the confirm worker image bytes; renders the subject's blurred face crops live (services preview endpoint — the console's crop access was removed 2026-08-21: staff never see hit imagery) |

The API logs the AWS **account, region and collection** at startup as a
`WARNING` (`event: aws.identity`). If you are unsure which environment a
console is attached to, that line is the answer. It is a warning rather than an
info line so it survives a scrolling console.

Admin endpoints are all under `/v1/admin/*` and need **both** `X-Service-Token`
and `X-Admin-Service-Token`.

---

## Health and readiness

Two unauthenticated routes exist, and they answer different questions.

**`GET /health`** — is the process up. Postgres reachability only; always
`200`, even when the db is `degraded`, because the proxy reads this body and a
degraded db must not look like "service absent" to its retry logic.

**This is what the ECS health check targets**
(`infra/ecs/imageshield-dev-services.json` polls
`http://localhost:8081/health`), deliberately not `/readyz`. A container
waiting on the `services` migration to create the `svc` schema is still a
healthy, running process — killing and restarting it on a loop would not make
that migration run any sooner, only hide that it hasn't.

**`GET /readyz`** — may a deploy proceed. Checks Postgres reachability *and*
that the four `svc` contract views (migration 0016) exist **as views**, with the
columns and types the proxy's own views join against, **and that
`imageshield_proxy_ro` can still SELECT them**. Returns `503` — not `/health`'s
always-`200` — when the db is unreachable or the contract is broken, because
this is a deploy gate, not a liveness signal, and those are different
questions with different right answers.

**Reading the `problems` array** (empty when ready), one entry per violation:

- `missing_view: svc.<view>` — the relation does not exist at all.
- `not_a_view: svc.<view> is a table, expected a view` — something with the
  right name and the right columns is standing in for the projection. Usually a
  stub fixture; `DEPLOY-DEV-HANDOFF.md` §7 forbids `svc._stub_*` in any deployed
  environment, and this is the check that enforces it. It serves a fixture's
  rows, not this database's.
- `missing_column: svc.<view>.<column>` — the view exists, the column is
  missing.
- `wrong_column_type: svc.<view>.<column> is <found>, expected <type>`.
- `missing_select_grant: imageshield_proxy_ro cannot SELECT svc.<view>` — the
  view is present and correct and the proxy cannot read it. Most often a
  migration that widened a view with `DROP VIEW` + `CREATE VIEW` rather than
  `CREATE OR REPLACE VIEW`: the destructive form discards the grant. Re-run
  0016's `GRANT SELECT`.
- `missing_schema_usage: imageshield_proxy_ro has no USAGE on svc` — denies all
  four at once, whatever each view's own grant says.
- `missing_grant_role: imageshield_proxy_ro does not exist` — 0016 creates it,
  0017 grants the proxy's login roles membership in it. Until it exists the
  contract reaches nobody, and it presents on the proxy's side as "the svc views
  are missing", which is the wrong place to look.

Columns may be added freely (the check is expected-subset-of-actual), so only
a removal or a retype ever produces an entry here.

**First move on `missing_view`: run the migration task** — not a rollback, not
a redeploy. `services` migrates first and creates the `svc` schema and its four
views; the backend cannot pass its own readiness without them
(`DEPLOY-DEV-HANDOFF.md` §7). A freshly stood-up environment is expected to
report `missing_view` until that task has run.

**`missing_view` is a claim about the database, not about this container's
privileges.** The check reads `pg_catalog`, which is not privilege-filtered.
It used to read `information_schema`, which is: a role sees only the columns it
owns or holds a privilege on, and `app_services` — the role this service connects
as — holds nothing on `svc` (0016 grants it to `imageshield_proxy_ro` alone).
That made a correct database report four `missing_view` entries, and sent
whoever was on call to re-run a migration that had already succeeded. If you are
reading a `missing_view` from an older image, verify with
`SELECT * FROM pg_views WHERE schemaname = 'svc'` as the migration runner before
re-running anything.

**The SQS gap in dev.** Neither `imageshield-dev-identity-index` nor
`imageshield-dev-search-runs` exists yet in the dev account (verified against
the live account 2026-08-17), even though both URLs are already wired into the
task definition. Config validates that an SQS URL is *shaped* like one, not
that the queue behind it exists, and neither `/health` nor `/readyz` touches
SQS — so the container boots healthy and reports ready, and every enqueue then
fails at runtime. Invariant #33 makes that fail quiet rather than loud: the
write that triggered the enqueue still succeeds, the job retries with
backoff, and the user sees "still setting up," never a 500. If enqueues in dev
appear to silently do nothing, this is why — create the two queues first.

---

## 1. A DLQ has depth

**Symptom** — `imageshield-<env>-<queue>-dlq-depth` alarm. Any depth above zero
fires; this is not a threshold to tune, because a message here has already
failed every retry.

**What is in it.** Messages carry **IDs, never payloads** — the body is
`{"event": ..., "id": ...}` and the authoritative state is the Postgres row.
So a DLQ message tells you *which* run failed, not *why*.

```bash
aws sqs receive-message --queue-url "$DLQ_URL" --max-number-of-messages 10 \
  --visibility-timeout 0            # 0: look without claiming
```

Then read the real state:

```sql
SELECT run_id, status, providers_attempted, providers_succeeded, started_at
FROM search_runs WHERE run_id = '<id from the message>';
```

**How to replay.** Every consumer is idempotent (SQS is at-least-once and the
outbox makes duplicates normal), so replaying is safe by construction:

```bash
aws sqs start-message-move-task \
  --source-arn "$DLQ_ARN" --destination-arn "$MAIN_QUEUE_ARN"
```

**When NOT to replay.** If the run is already `completed` or `refused`, the
message is stale — `claim_run` will return nothing and the message will simply
be deleted, which is fine but pointless. And if the DLQ filled because a
provider was returning malformed responses, replaying before fixing the
provider just refills it. Fix the cause, then replay.

---

## 2. A provider is returning garbage

**Disable it. No deploy, effective within 30 seconds** (the provider config
cache TTL is capped at 30s in code).

**From the panel (preferred):** Provider health → the provider's row → *Disable* with a reason —
the backend's `/v1/admin/providers/{id}/disable`, proxied to the services route below. The audit
row names the signed-in operator. The curl below is the **break-glass** path for when the panel or
the backend itself is down — hit the services route directly, naming yourself, since there is no
operator session to inject a name for you:

```bash
curl -X POST "$BASE/v1/admin/providers/hive/disable" \
  -H "X-Service-Token: $TOKEN" -H "X-Admin-Service-Token: $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"reason": "returning Media Search results, not Web Search — key on wrong project", "operator": "<your name>"}'
```

The reason is mandatory (min 3 characters) and it is not paperwork.
`providers.enabled = false` with no recorded reason is the state where nobody
remembers whether the provider is off because of a billing surprise, a vendor
breach, or a test somebody forgot to undo — and the difference decides whether
turning it back on is safe.

**What this does not do:** it does not fail runs. A disabled provider is
*skipped*, recorded in `provider_calls` with status `provider_disabled`, and
stays in `providers_attempted` while being absent from `providers_succeeded`.
Partial coverage stays visible, which is the whole point.

**Re-enable** with `/enable` and a reason. Note that enable and breaker-reset
are separate calls on purpose — see below.

---

## 3. The circuit breaker opened

**What it means.** Five consecutive *failures* — timeout, 5xx, connection
error, malformed response. **Not** a 429, **not** a 200 with zero matches, and
**not** any skip. The breaker opens on brokenness, never on an ordinary result.

**What to check:**

```sql
SELECT provider_id, breaker_state, breaker_reason, breaker_opened_at,
       breaker_consecutive_failures, breaker_cooldown_seconds
FROM providers;

SELECT status, http_status, error_detail, created_at
FROM provider_calls WHERE provider_id = 'hive'
ORDER BY created_at DESC LIMIT 20;
```

`error_detail` and the verbatim `raw_response` are stored on every call
including failures, so the actual provider response is there.

**Recovery is automatic.** After the cooldown the breaker goes `half_open` and
allows exactly one probe, claimed by a conditional `UPDATE` so only one worker
gets it across all of them. A successful probe closes it. An *inconclusive*
probe (a 429 — neutral, not a verdict) returns it to `open` with the clock
restarted and the cooldown **not** doubled. There is no terminal state: a probe
abandoned by a dead worker is reclaimed after cooldown + grace.

**To force it closed** once you know the provider is fixed:

**From the panel:** Provider health → *Reset breaker* (shown only while the breaker is not closed)
— the backend's `/v1/admin/providers/{id}/breaker-reset`. Break-glass, straight to services,
naming yourself:

```bash
curl -X POST "$BASE/v1/admin/providers/hive/breaker/reset" \
  -H "X-Service-Token: $TOKEN" -H "X-Admin-Service-Token: $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"reason": "vendor confirmed incident resolved", "operator": "<your name>"}'
```

This is **separate from `/enable` deliberately**: *"this provider is fixed, let
it back in before the cooldown expires"* and *"this provider should be
receiving traffic at all"* are different decisions, and collapsing them means
one of them gets made by accident.

---

## 4. Daily spend alarm fired

**Where to look:**

```bash
curl "$BASE/v1/admin/providers/health" \
  -H "X-Service-Token: $TOKEN" -H "X-Admin-Service-Token: $ADMIN_TOKEN"
```

The panel's provider health screen renders the same payload, via the backend's
`GET /v1/admin/providers/health`: every firing alarm first, then spend, headroom and latency per
provider.

Money crosses as decimal **strings**, not floats. Per-provider per-day call
count, cost, success rate, p50/p99, breaker state, budget headroom and every
firing alarm are all there.

**How to raise a budget:** `UPDATE providers SET daily_budget_usd = ... WHERE
provider_id = ...`. Effective within the config cache TTL, no deploy.

**What breaks if you do not.** The budget check happens **before** the call. At
the cap, dispatch stops for that provider for the rest of the UTC day: runs
still complete, the provider is recorded as `budget_exceeded` in
`provider_calls`, and it is absent from `providers_succeeded`. So coverage
silently narrows to whichever providers still have headroom. **A run where no
provider succeeded does not change the seed's scan tier** (`should_retier`), so
at least a budget outage will not also relax everyone's cadence.

**Two traps:**

- **`hive.cost_per_call_usd` is NULL and that is deliberate** — Hive Web Search
  is contract-priced and no measured figure exists in this repo. A budget set
  with an unknown cost **fails closed**: an operator who asked for a cap must
  not get unbounded spend because we cannot price the calls. So setting a Hive
  budget without first filling in its cost stops Hive dispatching entirely.
- **`monthly_budget_usd` is reported, not enforced at dispatch.** The dispatch
  guard is one indexed row by design; a month is a range scan. Month-to-date is
  an admin read only.

---

## 5. A provider has returned zero successful calls for 24h

**THIS IS THE ONE THAT MATTERS.**

**Why.** It looks *exactly* like a quiet week for infringements. Both produce
reports with nothing in them. In a safety product, an undetected outage means
users are told they are clear when nothing actually looked — and they will
believe it, because that is what the product is for.

Nothing else in the system distinguishes the two. `providers_succeeded` is the
only field that does, and nobody reads it per-run.

**Step by step:**

1. **Confirm it is real, not a quiet corpus.**

   ```sql
   SELECT provider_id,
          count(*) FILTER (WHERE status = 'ok')   AS ok,
          count(*)                                AS attempts,
          max(created_at)                         AS last_call
   FROM provider_calls
   WHERE created_at > now() - interval '24 hours'
   GROUP BY provider_id;
   ```

   `attempts` high and `ok` zero → the provider is failing.
   `attempts` **zero** → nothing even tried; go to step 3.

2. **If it is failing, find out how.** `status` separates the causes:
   `timeout` / `error` (provider broken), `rate_limited` (us, too fast),
   `budget_exceeded` / `breaker_open` / `provider_disabled` (we skipped it —
   these are *our* doing, not the vendor's).

   ```sql
   SELECT status, count(*), max(error_detail)
   FROM provider_calls
   WHERE provider_id = 'hive' AND created_at > now() - interval '24 hours'
   GROUP BY status;
   ```

3. **If nothing was attempted, the pipeline is stalled upstream.** In order:
   is the search worker running? Is the relay running (check the outbox lag
   alarm)? Are there `queued` runs?

   ```sql
   SELECT status, count(*) FROM search_runs
   WHERE started_at > now() - interval '24 hours' GROUP BY status;

   SELECT count(*), min(created_at) FROM outbox WHERE published_at IS NULL;
   ```

4. **Check it is not a seed problem.** Since task 02, the presigned `seed_url`
   lives on the *run* and expires. If every provider is failing with
   fetch-shaped errors, the URLs expired between enqueue and dispatch —
   **the proxy must re-enqueue with fresh URLs**; there is no refresh path on
   this side, deliberately, because one would need S3 credentials.

5. **Tell someone before you fix it.** If discovery has genuinely been down for
   24h, every report generated in that window overstates safety. That is a
   product/comms decision, not an ops one. See *Who to call*.

**Do not** silence this alarm to stop the pages. It is the only signal for the
failure mode this product most needs to avoid.

---

## 6. Rotating `SERVICE_TOKEN` (or `ADMIN_SERVICE_TOKEN`)

**Both sides deploy together. There is no in-flight rotation protocol, and this
step did not invent one.** The service accepts exactly one value at a time.

1. Put the new value in Secrets Manager (`imageshield/<env>/service-token`).
2. Deploy the proxy and these services **in the same window**. Requests in
   flight with the old token get 401.
3. Verify with any authenticated route before declaring done.

The two tokens **must differ** — boot refuses to start if they match, so a
rotation that accidentally sets both to the same value fails loudly at deploy
rather than quietly granting admin to every caller.

---

## 7. Verifying a user's face is actually gone after DELETE

`DELETE /v1/enrolments/{user_ref}` calls `DeleteFaces`, **verifies absence via
`ListFaces`, and only then tombstones** (INVARIANTS #7). The order matters: if
the tombstone succeeded and the Rekognition call failed, the face would stay
searchable with no record pointing at it. The old repo called `DeleteFaces`
nowhere, under a comment claiming BIPA compliance.

To verify by hand:

```sql
SELECT external_face_id, status, deleted_at
FROM enrolments WHERE user_ref = '<user_ref>';
```

```bash
aws rekognition list-faces --collection-id identity-v1 \
  --query "Faces[?FaceId=='<external_face_id>']"
```

Expect `[]`. Rows are **soft-deleted** (`status='deleted'`), never removed —
the row is the only record the face ever existed, which is what makes this
check possible at all.

---

## 8. Restoring from a failed migration

Migrations run in a transaction with a checksummed `schema_migrations` ledger.
A failed migration rolls back; it does not half-apply.

```bash
python scripts/migrate.py up            # re-run; the ledger skips applied ones
python scripts/migrate.py down --steps 1
```

**Editing an applied migration is a deploy-blocking error** — the checksum will
not match and the runner refuses. Write a new migration instead. Always.

**Two down-migrations lose real data, and both say so in the file** — read the
`.down.sql` before running it:

- `0010` (consent) — drops the only record in this repo that consent was
  collected.
- `0012` (feedback) — drops every recorded user reaction; there is no other
  copy.

`0014` down drops attribution provenance but keeps the seeds. `0008` down is
the dangerous one for a different reason: re-applying `up` backfills every
subject as `adult`, which is **wrong for a minor**, so a down/up cycle must be
followed by a proxy-driven re-assertion of `subject_is_adult`.

---

## 9. The `confirm-hits` DLQ has depth

**Symptom** — `imageshield-<env>-confirm-hits-dlq-depth` alarm. Same rule as scenario 1: any depth
above zero fires, because a message here already failed every retry.

**What is in it.** Same convention as every other queue — the body is `{"event": ...,
"infringement_id": ...}`, never a payload. Read the real state from Postgres:

```sql
SELECT infringement_id, confirm_state, severity, confirm_decided_by, created_at
FROM infringements WHERE infringement_id = '<id from the message>';
```

**Common causes, in the order to check them:**

1. **The fetcher is down or unreachable.** The confirm worker calls `FETCHER_BASE_URL` for every hit;
   if the fetcher task isn't running (or crash-looped, or the placement never happened — see scenario
   12), every confirm job fails identically. Check the fetcher's own health:

   ```bash
   curl -s http://localhost:8083/health   # from a host-network task, same posture as /readyz checks
   ```

2. **`rekognition_confirm` hit its budget or breaker.** It is a normal `providers` row and goes
   through the same gate chain as Hive/Google (INVARIANTS #37–#41). A `budget_exceeded` or
   `breaker_open` skip does **not** DLQ the message on its own — it lands as a triage state and the
   hit stays `unconfirmed`, retryable — so a DLQ'd message alongside a healthy-looking provider row
   usually points at cause 1 or 3, not this one. Still worth ruling out:

   ```sql
   SELECT provider_id, enabled, breaker_state, breaker_reason FROM providers
   WHERE provider_id = 'rekognition_confirm';
   ```

3. **A genuinely unfetchable or oversized image** exhausted retries. Expected and not alarming on its
   own — the hit lands `unfetchable`/`unassessed` and stays reviewable URL-only. A DLQ message for
   this cause is a sign the *retry* path (not the fetch itself) is broken, since a normal unfetchable
   result should have resolved to a triage state, not a redelivery loop.

**How to replay.** Same as every other queue — every consumer is idempotent, so replay is safe:

```bash
aws sqs start-message-move-task \
  --source-arn "$CONFIRM_HITS_DLQ_ARN" --destination-arn "$CONFIRM_HITS_QUEUE_ARN"
```

**Do not** replay before fixing cause 1 (a down fetcher) — it will refill immediately.

---

## 10. A hit was quarantined

**Symptom** — a log line `confirm.quarantined` from the confirm worker, **and, since `alarms.tf`'s
`confirm_quarantined` metric filter/alarm, a page** — a log metric filter over the same event, firing
on `>= 1` in a 5-minute window. This is the ops alarm for the CSAM tripwire (`ARCHITECTURE.md` §3.8
step 7); the log line is still the source of truth, the alarm just means a human no longer has to be
tailing logs to catch it.

**What it means.** Rekognition's `DetectModerationLabels` returned labels suggesting the subject may
be a minor, combined with explicit content. The confirm worker set
`infringements.confirm_state = 'quarantined'` and stopped: no score effect, no `review_tasks` row (or
an existing one is pulled), and the hit is excluded from **every** `svc` view — the proxy's UI cannot
surface it even by accident, because `v_person_hits` and `v_person_report_summary` filter
`confirm_state NOT IN ('quarantined', 'duplicate')` at the query level (`SCHEMA.md` §2d, migration
0023).

**What is retained.** The URL and the moderation label text only. **The image bytes were already
discarded** by the fetcher before the worker ever wrote a row — there is nothing to "not look at"
because nothing holding pixels exists past the in-memory classification step.

**What to do:**

1. **Do not download the URL. Do not open it. Do not attach it to a ticket or paste it into chat.**
   Same rule as `docs/OPERATIONS.md` §9's existing CSAM guidance — treat this exactly like that
   scenario, because it is that scenario, just discovered through a different path (an automated
   moderation call instead of a human reviewer's judgement).
2. **Escalate to the engineering lead and legal counsel the same hour.** v1 has **no automated
   reporting pipeline** (NCMEC or otherwise) — this is a known, documented gap (`docs/OPERATIONS.md`
   §9's "CSAM path has no owner" box applies verbatim here). Escalation from a quarantine is a
   **manual legal process**, not a system action.
3. **Do not manually flip `confirm_state` back** to get the hit "out of quarantine" without legal
   sign-off. The state is doing exactly what it is for.
4. **Confirm the subject's discovery eligibility is what you expect.** A quarantine on an enrolled
   *adult's* scan surfacing a third party who appears to be a minor is a different (and still
   serious) situation from a minor somehow reaching discovery at all (INVARIANTS #8b) — check
   `subjects.discovery_eligible` for the user the hit belongs to as part of the same escalation, not
   as a substitute for it.

**Frequency expectation.** This should be rare. If it is firing often, that is itself something to
raise with product/legal — it may mean the corpus or a seed source needs review, separate from any
individual quarantine's handling.

---

## 11. The score looks wrong

**Symptom** — a user's protection score doesn't match what support or a reviewer expects, or jumped
by an amount nobody can explain.

**The journal is the source of truth, never the materialized row.** Read it first, always:

```sql
SELECT score_event_id, delta, component, cause_kind, cause_ref, config_version, score_after, created_at
FROM score_events
WHERE user_ref = '<user_ref>'
ORDER BY score_event_id DESC
LIMIT 20;
```

Every row explains itself: `component` (which of Posture/Coverage/Exposure/Threat moved),
`cause_kind` (`feedback` / `enrolment` / `seed_registered` / `run_completed` / `review_decision` /
`threat_event` / `threat_retracted` / `tick`), and `cause_ref` (the infringement, run, or threat event
that triggered it). `config_version` on each row matters if weights have since been retuned — a score
computed under an old config is not wrong, it is historical.

**Sanity check the row against the journal**, since `protection_scores` should always equal the sum:

```sql
SELECT p.score AS materialized, (
  SELECT sum(delta) FROM score_events WHERE user_ref = p.user_ref
) AS summed
FROM protection_scores p WHERE p.user_ref = '<user_ref>';
```

These two columns must match (INVARIANTS #44, `tests/test_score_store.py::test_journal_sums_to_the_materialized_score`
is the permanent regression test). If they do not on a real environment, that is a bug in
`score/store.py`, not a config problem — stop and escalate rather than trying to patch the row by
hand; there is no writer other than `score/store.py` and hand-editing `protection_scores` directly
creates exactly the drift the journal exists to prevent.

**If the journal is internally consistent but the *number itself* looks wrong** (e.g. a user insists a
confirmed hit should have cost more, or a stale seed shouldn't be dinging Posture as much as it is),
that is very likely a **config/weights** question, not a bug — check `SCORE_WEIGHT_*` and the severity
sub-weights currently loaded, and remember `config_version` on the affected `score_events` rows tells
you which config produced the number being questioned.

**If a change should have moved the score and nothing did** (no new journal row after an action that
should trigger a recompute): the trigger path runs synchronously after commit, so a missing row means
either the trigger call didn't fire (a bug worth filing) or it hit an exception after its own commit —
the daily **score tick** (`python -m imageshield.score.tick`) is the designed healer for exactly this
case and will pick it up on its next run; you do not need to force anything by hand. If it's urgent,
the tick's `run_once()` can be invoked out-of-band rather than waiting for the interval — check
`score/tick.py` for the entry point.

**Never write directly to `protection_scores` or `score_events` to "fix" a number.** Any writer other
than `score/store.py` is exactly the boundary violation `tests/test_boundaries.py::test_only_the_score_store_writes_the_score`
exists to catch, and a hand-edited materialized row with no journal entry breaks the invariant that
lets support trust the history feed at all.

---

## 12. Who to call, and about what

| Situation | Who |
|---|---|
| Provider outage, spend, breakers, queues | On-call engineer |
| Discovery down >24h (scenario 5) — users may have been told they are clear | Engineering lead **and** product, same message |
| A hit involving someone who appears to be a minor | **NOBODY IS NAMED. SEE BELOW.** |
| Suspected breach of the identity collection or the database | Security lead, immediately; treat as Article 9 biometric data |
| Proxy contract questions | Proxy repo owner |

> **⚠ The CSAM path has no owner and no process, and that is a known gap.**
>
> This row is here rather than omitted so the gap is *visible* rather than
> assumed. v1 does not serve discovery to minors at all — `subjects.discovery_eligible`
> refuses at the route and again at dispatch — precisely because CSAM screening
> and mandatory reporting are not built. So this should not arise.
>
> If it arises anyway (a minor mis-asserted as an adult by the proxy, or an
> adult's scan surfacing material involving a child): **stop, do not download,
> do not share the URL internally, do not attach it to a ticket.** Escalate to
> the engineering lead and legal counsel the same hour. Mandatory reporting
> obligations may attach and they have deadlines.
>
> **Fill in a name here before this system serves real users.**

---

## Appendix — deploying from empty

```bash
cd infra/terraform
terraform init
terraform plan  -var environment=staging
terraform apply -var environment=staging
```

Then, in order:

1. Set the secret **values** (Terraform creates containers only, never values —
   a value in a `.tf` is a value in git history, and `terraform state` stores it
   in plain text besides).
2. Run the migrations: `DATABASE_URL=... python scripts/migrate.py up`. This
   creates the per-module database roles (`0015`) as well as the schema.
3. Deploy the four processes with the role from the `service_role_arn` output.
4. Confirm the startup log line names the account and region you expect.

**`prevent_destroy` is set on the Rekognition collection.** Removing it to let
a `destroy` through deletes every enrolled face vector in that environment, and
re-enrolment means every user redoing a liveness check.
