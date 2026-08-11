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

Four processes, all from one image:

| Process | Command | What it does |
|---|---|---|
| API | `uvicorn imageshield.http.app:create_app --factory` | Everything the proxy calls |
| Outbox relay | `python -m imageshield.relay` | Postgres outbox → SQS |
| Search worker | `python -m imageshield.search.worker` | Consumes `search:runs`, dispatches providers |
| Recheck worker | `python -m imageshield.recheck.worker` | Weekly HEAD sweep setting `url_alive` |

The API logs the AWS **account, region and collection** at startup as a
`WARNING` (`event: aws.identity`). If you are unsure which environment a
console is attached to, that line is the answer. It is a warning rather than an
info line so it survives a scrolling console.

Admin endpoints are all under `/v1/admin/*` and need **both** `X-Service-Token`
and `X-Admin-Service-Token`.

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

```bash
curl -X POST "$BASE/v1/admin/providers/hive/disable" \
  -H "X-Service-Token: $TOKEN" -H "X-Admin-Service-Token: $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"reason": "returning Media Search results, not Web Search — key on wrong project"}'
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

```bash
curl -X POST "$BASE/v1/admin/providers/hive/breaker/reset" \
  -H "X-Service-Token: $TOKEN" -H "X-Admin-Service-Token: $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' -d '{"reason": "vendor confirmed incident resolved"}'
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

## 9. Who to call, and about what

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
