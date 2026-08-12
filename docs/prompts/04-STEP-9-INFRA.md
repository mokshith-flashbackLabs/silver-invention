# Step 9 — Infrastructure and CI

Steps 1–8 complete. Step 9 of the 9 in `CLAUDE.md` §8 (canonical). **Last step of v1.**

Run this after tasks 01–03 in this directory, because it gates on the rest existing.

This step is where the boundaries stop being conventions. Three of the invariants in this system are
currently enforced by discipline and code review; after this step, two of them are enforced by AWS
and by the build.

The old system deployed five Lambdas by manual PowerShell `Compress-Archive` with no IaC at all. We
are not doing that.

---

## 1. Infrastructure as code

Terraform or CDK — your choice, but **committed, not clicked**. If it was created in a console, it
does not exist.

### Queues

```
identity:index    (reserved, unused in v1 — see step 4)
search:runs
+ a DLQ for each, redrive policy, depth alarm
```

### IAM — the important part

```
ALLOW:
  rekognition: CreateFaceLivenessSession, GetFaceLivenessSessionResults,
               IndexFaces, DeleteFaces, ListFaces, DetectFaces
  sqs:         SendMessage, ReceiveMessage, DeleteMessage, GetQueueAttributes
  secretsmanager: GetSecretValue  (scoped to our secret ARNs, not *)

EXPLICITLY ABSENT:
  s3:*         — no grant of any kind, not even GetObject
```

**The absence of the S3 grant is a feature.** `CLAUDE.md` §3.3 says this service holds no S3
credentials; the presigned-URL handshake exists so it never needs them. With no grant, a future
"let's just read it from S3" cannot work even if someone writes the code. Make the mistake impossible
rather than forbidden.

Scope Rekognition to the `identity-v1` collection where the API permits resource-level scoping.

### Rekognition collection

`identity-v1` created by IaC, not by hand. A hand-created collection is untracked, and step 4's
done-when includes asserting the collection contains exactly as many faces as active `enrolments` —
which is meaningless if nobody knows how the collection came to exist.

### Database roles

One role per module, least privilege, defined in IaC alongside the migrations that create the schemas:

```
identity_rw    liveness_sessions, enrolments, subjects
search_rw      content_urls, infringements, attestations, provider_calls,
               search_seeds, search_runs, providers, provider_spend,
               infringement_feedback
calibration_rw calibration_configs, eval_items, eval_observations
audit_w        INSERT ONLY on audit_log — no UPDATE, no DELETE grant
```

The `audit_w` grant shape is already asserted by a step-2 test. IaC makes it reproducible rather than
a one-time `psql` incantation.

---

## 2. Secrets

AWS Secrets Manager, matching the pattern the old system got right — it is the one thing in that
codebase with no findings against it.

Never committed, never in a config file, never in a `.env` outside local dev. `HIVE_API_KEY`,
`GOOGLE_*`, `SERVICE_TOKEN`, `ADMIN_SERVICE_TOKEN`, `DATABASE_URL`.

Rotation is deploy-both-sides-together. There is no in-flight rotation protocol and this step does not
invent one — document that in `OPERATIONS.md` rather than leaving it to be discovered.

---

## 3. CI — every check blocking

```
typecheck      mypy or pyright, STRICT
               NewType is erased at runtime; without this the typed identifiers
               are decoration and the phone-number boundary is convention again
lint           ruff
unit           pytest
integration    pytest against a real Postgres via pytest-postgresql
migrations     forward AND backward, from empty
schema-lint    the three-part rule from step 2:
                 photo bytea              -> FAILS
                 thumbnail_b64 text       -> FAILS
                 reference_image_uri text -> PASSES
route-auth     parameterised over app.routes — a new route without the
               service-token dependency fails the build
consistency    every infringement band recomputed from its attestations,
               zero disagreements (step 7)

grep gates, all failing the build:
  search_faces_by_image | SearchFacesByImage | search_users_by_image   in src/
  boto3 client("s3") / resource("s3") / any S3 import                  in src/
  phone-number-shaped literals                                          in src/ and migrations/
  inline age literals — MIN_ENROLMENT_AGE and MIN_DISCOVERY_AGE from config only
```

The grep gates are crude and that is the point. They are unambiguous, they run in a second, and they
catch the specific reintroductions this system is most exposed to.

---

## 4. Local development

Copy the Flashback pattern — `docker-compose.local.yml` with Postgres 16 (plain, not pgvector) and
LocalStack `SERVICES=sqs`, queues created on boot by `scripts/localstack/init-sqs.sh`.

Liveness needs real Rekognition. Document how to run against a dev AWS account, and **print which
account and region are configured at startup**. Someone will eventually run this against production
by accident; make it loud.

---

## 5. `docs/OPERATIONS.md`

Write it as a runbook for someone at 3am who did not build this.

```
- A DLQ has depth. What is in it, how to inspect, how to replay, when not to.
- A provider is returning garbage. Disable it: providers.enabled = false,
  effective within 30s, no deploy.
- The circuit breaker opened. What that means, what to check, how to reset.
- Daily spend alarm fired. Where to look, how to raise a budget, what breaks
  if you do not.
- A provider has returned zero successful calls for 24h. THIS IS THE ONE THAT
  MATTERS — it looks exactly like a quiet week for infringements, and in a
  safety product an undetected outage means users are told they are clear when
  nothing actually looked.
- Rotating SERVICE_TOKEN. Both sides deploy together; there is no in-flight
  protocol.
- Verifying a user's face is actually gone after DELETE — ListFaces against the
  FaceIds, expect none.
- Restoring from a failed migration.
- Who to call, and about what. Name the person for the CSAM path even though
  that path is not built, so the gap is visible rather than assumed.
```

---

## 6. Observability

```
Per provider, per day: call_count, cost_usd, success rate, p50/p99 latency,
                       breaker state, budget headroom
Per queue:             depth, age of oldest message, DLQ depth
Per service:           request rate, error rate, p99, DB pool saturation

Alarms:
  breaker opens
  daily spend > 80% of budget
  provider success rate < 90% over 1h
  ANY provider with zero successful calls in 24h
  DLQ depth > 0
  outbox rows unpublished for > 5 minutes
```

That last one is the outbox failing silently, which means enrolments are not triggering searches and
nothing else would tell you.

---

## Done when

- `terraform plan` (or `cdk diff`) is clean from a fresh state, and applying it from empty produces a
  working environment
- **the IAM role has no `s3:*` permissions of any kind, and the full test suite still passes** — this
  is the single most important assertion in the step
- `identity-v1` exists because IaC created it, not because someone ran a CLI command
- every CI gate above is blocking, and each one has been demonstrated failing at least once —
  deliberately add a bad route, a `bytea` column, a `SearchFacesByImage` call, confirm each fails,
  revert
- `UPDATE audit_log` fails under the application role, in an environment built entirely from IaC
- startup logs the AWS account and region, prominently
- a new engineer goes from `git clone` to green tests using only `README.md`
- `OPERATIONS.md` covers all nine scenarios, and someone who did not write it can follow the
  zero-successful-calls one end to end

Stop at the end of step 9. **This completes v1 of this repo.**

---

## What v1 is, and is not

Do not let the completion of nine steps read as completion of the product. On finishing this step the
repo has: liveness-verified enrolment, discovery via Hive and Google, dedup, calibration and banding,
cost controls, feedback, recheck, and infrastructure.

It does not have, and none of these are in the nine steps:

- **The match module.** Blocked on the partner organisation's embedding model — vectors from
  different models are not comparable.
- **Attribution and clustering** (the photo-shield rule). Specified in the proxy's phase 9 and
  deliberately unbuilt here. Needs a product decision plus a narrowing of invariant #1.
- **Adjudication queue, crop fetcher, evidence export, digests.** All specified in `ARCHITECTURE.md`,
  all deliberately unbuilt.
- **CSAM screening and reporting.** Deferred until the partner corpus connects. Minors enrol but
  discovery refuses at dispatch (step 8) precisely because this does not exist.
- **Any measurement of whether discovery finds deepfakes.** The step-7 harness is built and
  `eval_items` / `eval_observations` are migrated. They need ~200 labelled images to produce a number.

That last one is the largest unknown in the product, and it is the only remaining task whose output is
an answer rather than more system.

---

## Standing rules

```
- Cite file:line when describing existing behaviour. Mark anything not read
  directly as INFERRED.
- If anything here conflicts with CLAUDE.md §4 (invariants), STOP AND ASK.
- Doc corrections land in the same commit as the code they describe.
- When the task is done, STOP.
```
