# DEPLOY-RUNBOOK.md — how `services` was deployed, and how to do it again

**Written 2026-08-18, from the actual ap-south-1 dev deploy.** Every command here
was run for real; the failures recorded in §12 are ones that actually happened,
not hypotheticals. Read §12 before you start — four of the six hours went there.

Scope: **this repo's `services` deployable only.** The proxy's `api`, `worker`,
`image-worker` and `caddy` belong to the other repo (CLAUDE.md §3).

For production, read **§13 first** — several dev choices are deliberately wrong
for production, and one of them (D16) is a decision someone has to make on the
record.

---

## 1. What must already exist

The deploy assumes an environment built to `DEPLOY-DEV.md`'s shape. Verify before
starting; missing pieces here are cheaper to find now than mid-deploy.

```bash
export AWS_DEFAULT_REGION=ap-south-1        # production: set your region
export ACCT=225989356895
export CLUSTER=imageshield-dev
export REGISTRY=$ACCT.dkr.ecr.$AWS_DEFAULT_REGION.amazonaws.com

aws sts get-caller-identity --output json                       # right account?
aws ecs describe-clusters --clusters $CLUSTER \
  --query 'clusters[0].{Status:status,Instances:registeredContainerInstancesCount}'
aws ecr describe-repositories --query 'repositories[].repositoryName' --output json
aws rds describe-db-instances \
  --query 'DBInstances[].{Id:DBInstanceIdentifier,Engine:EngineVersion,Endpoint:Endpoint.Address}'
```

Then confirm the container instance's architecture — the image platform depends
on it and getting it wrong produces a misleading error (§12.1):

```bash
CI_ARN=$(aws ecs list-container-instances --cluster $CLUSTER \
  --query 'containerInstanceArns[0]' --output text)
aws ecs describe-container-instances --cluster $CLUSTER --container-instances $CI_ARN \
  --query 'containerInstances[0].{Ec2:ec2InstanceId,Arch:attributes[?name==`ecs.cpu-architecture`].value}'
```

Dev returned `arm64` (a Graviton `t4g.medium`). **Do not trust a document for
this — query it.** Everything in §2 depends on the answer.

### Secrets

`services` needs five. Get the real ARNs — they carry a random suffix, and a
stale one fails the task at launch with no application logs (§12.4):

```bash
aws secretsmanager list-secrets \
  --query "SecretList[?starts_with(Name,'imageshield/')].{Name:Name,ARN:ARN}" \
  --output table
```

You also need each secret's **JSON key names**, because an ECS `secrets` entry
extracts one key. Do not assume; the DB secrets surprised us (§12.3):

```bash
aws secretsmanager get-secret-value --secret-id imageshield/dev/db/app_services \
  --query SecretString --output text \
  | python -c 'import sys,json;print(sorted(json.load(sys.stdin)))'
```

Measured in dev:

| secret | keys |
|---|---|
| `db/app_services`, `db/migrator_services` | `dbname`, `host`, `password`, `port`, `username` — **no `url`** |
| `service-token/backend-to-services`, `service-token/admin` | `token` |
| `hive`, `google-vision` | `api_key` |

---

## 2. Build and push the image

**The platform must match §1's answer.** The pin is a constant in the Dockerfile
on purpose — the builder stage installs the venv, so building on amd64 and
copying to an arm64 runtime ships amd64 wheels (`psycopg[binary]`, `Pillow`) into
an arm64 container. `buildx` warns `FromPlatformFlagConstDisallowed`; ignore it.

```bash
cd <repo root>
SHA=$(git rev-parse --short HEAD)
aws ecr get-login-password | docker login --username AWS --password-stdin $REGISTRY

docker buildx build --platform linux/arm64 \
  -t $REGISTRY/imageshield/services:$SHA . --push
echo "buildx exit=$?"          # MUST be 0 — see §12.2, this bit us
```

**Never pipe `docker buildx` into `tail`/`grep` and then `&& echo success`.** The
pipeline returns the *filter's* exit code, so a failed push reports as a pass.
That happened here: a transient DNS failure produced `PUSHED` while nothing was
uploaded. Capture the exit code explicitly, as above.

Verify the artifact in the registry, not locally:

```bash
docker manifest inspect $REGISTRY/imageshield/services:$SHA | grep -m1 architecture
```

**Tag with the git SHA, never `latest`.** ECS caches by tag, so `latest` gives
deploys that silently do nothing.

One image serves the server and the migration task, differing only by `command`,
which is what makes migrations provably the same commit as the server.

---

## 3. IAM: two roles per task, and they are not the same thing

**Execution role** — used by the ECS agent *before* your code runs, to pull the
image and resolve `secrets` into env vars. One role, shared. In dev
`imageshield-dev-exec` already existed; create it if not:

```bash
cat > /tmp/ecs-trust.json <<'JSON'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
 "Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}
JSON

aws iam create-role --role-name imageshield-dev-exec \
  --assume-role-policy-document file:///tmp/ecs-trust.json \
  --tags Key=Env,Value=dev Key=Project,Value=imageshield
aws iam attach-role-policy --role-name imageshield-dev-exec \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
aws iam put-role-policy --role-name imageshield-dev-exec \
  --policy-name read-dev-secrets \
  --policy-document file://infra/ecs/policies/exec-role-secrets.json
```

**Task role** — used by *your code* at runtime. Created fresh in this deploy:

```bash
aws iam create-role --role-name imageshield-dev-services \
  --assume-role-policy-document file:///tmp/ecs-trust.json \
  --tags Key=Env,Value=dev Key=Project,Value=imageshield
aws iam put-role-policy --role-name imageshield-dev-services \
  --policy-name services-task-role \
  --policy-document file://infra/ecs/policies/services-task-role.json
```

Verify the rendered policy — this is one of only three places the data boundary
is enforced by something other than discipline:

```bash
aws iam get-role-policy --role-name imageshield-dev-services \
  --policy-name services-task-role --query 'PolicyDocument.Statement[].{Sid:Sid,Action:Action,Resource:Resource}'
```

It must show `s3:GetObject` and **nothing else on S3** — no `PutObject`, no
`DeleteObject`, no `ListBucket` — scoped to the liveness bucket only. This
service writes to S3 exclusively through presigned URLs minted by the proxy
(CLAUDE.md §3.3); grant it a write and the handshake becomes optional, which
means one day it gets skipped. `tests/test_ecs_task_defs.py` asserts this against
the same JSON, so run that suite rather than eyeballing it.

Rekognition collection actions must be scoped to the collection ARNs, never
`Resource: "*"`. The three Face Liveness *session* actions are not
collection-scoped in IAM and legitimately take `"*"` — that split is deliberate
and the test knows about it.

---

## 4. SQS queues

**Neither of this repo's queues existed in dev** — `DEPLOY-DEV-HANDOFF.md`'s
`services` env block names no SQS variable, so the environment was built without
them, while `Config` requires both URLs. This fails *quietly*: config validates
URL shape, not existence, so the container boots healthy and every enqueue fails
at runtime, where invariant #33 surfaces it as "still setting up" rather than an
error.

```bash
for q in identity-index search-runs; do
  dlq_url=$(aws sqs create-queue --queue-name "imageshield-dev-$q-dlq" \
    --query QueueUrl --output text)
  dlq_arn=$(aws sqs get-queue-attributes --queue-url "$dlq_url" \
    --attribute-names QueueArn --query 'Attributes.QueueArn' --output text)
  aws sqs create-queue --queue-name "imageshield-dev-$q" \
    --attributes "{\"RedrivePolicy\":\"{\\\"deadLetterTargetArn\\\":\\\"$dlq_arn\\\",\\\"maxReceiveCount\\\":\\\"5\\\"}\"}"
done
aws sqs list-queues --query 'QueueUrls' --output json
```

Do not reuse the backend's queues. Different repo, different message shape, and
#33's retry semantics assume ours are ours.

**Naming:** use whatever the environment already uses. Dev is `imageshield-dev-*`
across all twelve queues, but `infra/terraform/variables.tf` only accepts
`development|staging|production` and would mint `imageshield-development-*` —
reconcile that before ever applying that module.

---

## 5. Rekognition collections

```bash
aws rekognition create-collection --collection-id identity-dev-v1
aws rekognition create-collection --collection-id discovered-dev-v1
aws rekognition list-collections --query 'CollectionIds'
```

`discovered-dev-v1` is created **empty and unused** — the module that writes to
it is out of scope (CLAUDE.md §6). It exists because the env block names it and
nothing may write to a collection that does not exist.

> **Creating a collection is safe. Enrolling a face is not.** See §13.1 — the AI
> services opt-out gate (D16) applies before any `IndexFaces`.

---

## 6. The four bootstrap grants — the part that is not in any other document

**As built, `migrator_services` could not migrate at all.** `DEPLOY-DEV.md` §4
records "no table grants — those belong in each repo's migrations" as deliberate,
which is right about tables and wrong by omission about everything needed to
create one. Four grants were missing. Two are migrations; two are not and cannot
be.

### 6a. Two that must be run by hand, as the RDS master

Neither can live in a migration: this repo's runner **is** `migrator_services`,
so it cannot grant these to itself.

```sql
GRANT USAGE, CREATE ON SCHEMA public TO migrator_services WITH GRANT OPTION;
ALTER ROLE migrator_services CREATEROLE;
```

- `GRANT CREATE ON DATABASE` (which the bootstrap did apply) permits
  `CREATE SCHEMA`, **not** creating a table inside `public`, and
  `REVOKE ALL ON SCHEMA public FROM PUBLIC` removed the fallback. Symptom:
  `InvalidSchemaName: no schema has been selected to create in` at
  `CREATE TABLE schema_migrations`.
- `WITH GRANT OPTION` is load-bearing: migration 0015 grants `USAGE` on `public`
  onward to the four module roles, and only a grantee holding the option may do
  that.
- `CREATEROLE` because 0001 creates `imageshield_app`, 0015 the four module roles,
  0016 `imageshield_proxy_ro`. Symptom:
  `InsufficientPrivilege: permission denied to create role`. On PG16 it is narrow
  — a `CREATEROLE` role may only alter or drop roles it created — and it is also
  what makes 0017/0018/0020 work, since granting membership needs `ADMIN OPTION`,
  which a role's creator holds implicitly.

**There is no route to Postgres except from the container host** (by design —
`DEPLOY-DEV.md` §11). Run `docs/deploy/grant-public-schema.sh` there. Its header
documents three traps that each cost real time; read §12.5–12.7 before pasting.

```bash
aws ssm start-session --target i-0d277703b778392ef
sudo bash
uname -m          # MUST print aarch64 (or your host's arch) — see §12.5
```

### 6b. Two that are migrations (already in the repo)

- **0018** — `app_services` held nothing. 0001's `imageshield_app` and 0015's four
  module roles are all `NOLOGIN` with membership granted to nobody, so the
  least-privilege model applied to no one.
- **0017 / 0020** — same defect on the proxy's side: `imageshield_proxy_ro` was
  `NOLOGIN` with membership granted to nobody, so 0016's grant reached no one.
  0017 covers their runtime roles, 0020 their migrator (needed because creating a
  view requires `SELECT` on the underlying relations at creation time).

Both were invisible until this deploy, because the proxy previously connected as
the database owner, where ownership and a correct grant chain look identical.

### 6c. The proxy's migrator needs one too

Measured: `migrator_backend` already holds `public` USAGE+CREATE but
`rolcreaterole` is **false**, so their migrations will fail on
`permission denied to create role`. Not applied by us — their deploy, their call.
See `PROXY-HANDOFF-2026-08-17.md`.

### 6d. Verify the whole grant state in one shot

```bash
aws ecs run-task --cluster $CLUSTER --launch-type EC2 \
  --task-definition imageshield-dev-migrate-services \
  --overrides file://<a container override running the probe below>
```

The probe (composes the URL from `DB_*`, since there is no `DATABASE_URL`):

```python
import os, psycopg
from urllib.parse import quote
u = (f"postgresql://{quote(os.environ['DB_USER'],safe='')}:"
     f"{quote(os.environ['DB_PASSWORD'],safe='')}"
     f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}?sslmode=require")
with psycopg.connect(u, autocommit=True) as c:
    for r in ('migrator_backend','migrator_services','app_backend','app_worker',
              'app_services','imageshield_proxy_ro'):
        row = c.execute("SELECT rolcanlogin,rolcreaterole FROM pg_roles WHERE rolname=%s",(r,)).fetchone()
        if row is None:
            print(f'{r}: ABSENT'); continue
        usg = c.execute("SELECT has_schema_privilege(%s,'public','USAGE')",(r,)).fetchone()[0]
        crt = c.execute("SELECT has_schema_privilege(%s,'public','CREATE')",(r,)).fetchone()[0]
        print(f'{r}: login={row[0]} createrole={row[1]} pub_usage={usg} pub_create={crt}')
```

Expected after 6a+6b in dev:

```
migrator_services: login=True createrole=True pub_usage=True pub_create=True
app_services:      login=True createrole=False pub_usage=True pub_create=False
imageshield_proxy_ro members: app_backend, app_worker, migrator_services, migrator_backend
```

`app_services` having no `pub_create` is correct — it reads and writes tables, it
does not create them.

---

## 7. Task definitions

Both live in `infra/ecs/`, versioned. Substitute the image SHA and the real secret
ARN suffixes, then register:

```bash
aws ecs register-task-definition \
  --cli-input-json file://infra/ecs/imageshield-dev-migrate-services.json \
  --query 'taskDefinition.revision'
aws ecs register-task-definition \
  --cli-input-json file://infra/ecs/imageshield-dev-services.json \
  --query 'taskDefinition.revision'
```

Non-obvious choices, all deliberate:

- **`networkMode: host`, port 8081.** No port mapping; the container binds the
  host interface. `api` holds 8080.
- **`DB_*` parts in `secrets`, not `DATABASE_URL`.** The RDS secret has no `url`
  key, and a composed URL cannot go in `environment` because the password would
  be readable via `describe-task-definition`. `Config` composes the URL from the
  parts, URL-encoding the password, `sslmode=require`.
- **Health check hits `/health`, not `/readyz`.** `/health` is always 200 when the
  process answers; `/readyz` returns 503 until the DB and the `svc` contract are
  good. ECS would kill and restart a container that is up but waiting on
  migrations — a crash loop instead of a signal.
- **Health check uses `python -c`, not `wget`.** The `python:3.12-slim` base has
  no wget.
- **Every required config field must be present** or the container crash-loops.
  `tests/test_ecs_task_defs.py` derives the list from `Config.model_fields` and
  asserts it, so run it rather than checking by hand.

---

## 8. Migrations

**`services` migrates first.** It creates `svc` and the four contract views, and
the proxy cannot pass readiness without them.

```bash
TASK=$(aws ecs run-task --cluster $CLUSTER --launch-type EC2 \
  --task-definition imageshield-dev-migrate-services \
  --query 'tasks[0].taskArn' --output text)
aws ecs wait tasks-stopped --cluster $CLUSTER --tasks "$TASK"
aws ecs describe-tasks --cluster $CLUSTER --tasks "$TASK" \
  --query 'tasks[0].{Exit:containers[0].exitCode,Stopped:stoppedReason}'
```

`Exit: 0` is required. On anything else, read the logs (§11) before changing
anything — the two failures we hit were both grant problems from §6, not code.

Expected output: `applied 0001_…` through `applied 0020_…`.

Never run migrations on container start, and never from a laptop.

---

## 9. Create the service

```bash
aws ecs create-service --cluster $CLUSTER --service-name services \
  --task-definition imageshield-dev-services --desired-count 1 --launch-type EC2 \
  --deployment-configuration 'minimumHealthyPercent=0,maximumPercent=100'
aws ecs wait services-stable --cluster $CLUSTER --services services
```

**`minimumHealthyPercent=0, maximumPercent=100` is not optional.** With one
instance and fixed host ports, ECS cannot start a replacement while the old task
holds 8081 — the default 100 makes every deploy hang forever.

Subsequent deploys:

```bash
aws ecs update-service --cluster $CLUSTER --service services \
  --task-definition imageshield-dev-services --force-new-deployment
aws ecs wait services-stable --cluster $CLUSTER --services services
```

---

## 10. Verify

`/readyz` is on `localhost:8081` on a private interface, and Caddy only proxies to
`api` on 8080 — so probe it from a **host-network ECS task**:

```bash
# container override command:
python -c "
import urllib.request, urllib.error
try:
    r = urllib.request.urlopen('http://localhost:8081/readyz', timeout=10)
    code, body = r.status, r.read().decode()
except urllib.error.HTTPError as e:
    code, body = e.code, e.read().decode()
print('READYZ_STATUS', code); print('READYZ_BODY', body)"
```

Required result:

```
READYZ_STATUS 200
READYZ_BODY {"status":"ready","version":"0.1.0","db":"ok","problems":[]}
```

That single response proves three things: the app's DB role can actually reach the
database (i.e. §6's grant chain works), all four `svc` views exist, and all 33
columns match the expected types. A 503 lists the specific problems —
`missing_view`, `missing_column`, `wrong_column_type` — by name.

`/readyz` reads `pg_catalog`, **not** `information_schema`, deliberately:
`information_schema` is privilege-filtered, so a role without a grant on `svc`
sees zero columns and a perfectly good contract reports as four missing views.

Then check the logs look right (§11) — specifically that the assumed role is the
task role you created, the region and collection match, and timestamps and
`request_id`s are intact rather than redacted.

---

## 11. Reading logs

```bash
export MSYS_NO_PATHCONV=1     # Git Bash mangles /imageshield/dev into a path

aws logs describe-log-streams --log-group-name "/imageshield/dev" \
  --order-by LastEventTime --descending --max-items 10 \
  --query 'logStreams[].logStreamName' --output text

aws logs get-log-events --log-group-name "/imageshield/dev" \
  --log-stream-name "services/services/<task-id>" \
  --limit 40 --query 'events[].message' --output text | tr '\t' '\n'
```

`--order-by LastEventTime` cannot be combined with `--log-stream-name-prefix`;
list, then filter client-side.

A healthy startup:

```
INFO:  Uvicorn running on http://0.0.0.0:8081
{"event":"service.started","environment":"development","timestamp":"2026-08-18T05:38:32.951971Z"}
{"event":"aws.identity","region":"ap-south-1","collection_id":"identity-dev-v1",
 "arn":"...assumed-role/imageshield-dev-services/...","account":"[REDACTED:phone-shaped]"}
{"event":"request.completed","path":"/health","status":200,
 "request_id":"aef3bac3-09f9-48a6-b762-1559fcaf78ce"}
```

The redacted `account` is **correct and deliberate**: a 12-digit run is
shape-identical to a phone number, and this service must never hold one, so the
processor over-redacts rather than risk leaking. ISO timestamps and canonical
UUIDs are explicitly preserved so logs stay debuggable.

---

## 12. What actually went wrong — read this first

### 12.1 An amd64 image on arm64 fails as `exec format error`

Reads like a broken entrypoint. Query the container instance's
`ecs.cpu-architecture` (§1) rather than trusting a document.

### 12.2 A piped `docker buildx` reports success when the push failed

`docker buildx build ... | tail -3 && echo PUSHED` prints `PUSHED` on failure,
because the pipeline returns `tail`'s status. A transient DNS error produced
exactly that. Capture `$?` from buildx itself and verify with
`docker manifest inspect`.

### 12.3 The DB secret has no `url` key

It is RDS-shaped: `dbname`/`host`/`password`/`port`/`username`. `Config` requires
one `DATABASE_URL`, and a composed URL must not go in `environment` (the password
would be readable). Fixed by composing in `Config` from `DB_*` parts.
`scripts/migrate.py` has its **own** `os.environ` reader — it needed the same fix,
and it runs first, so missing it means the migration task crash-loops.

### 12.4 A stale secret ARN fails with no application logs

`ResourceInitializationError` from the agent, nothing from your code, because
nothing ran. Resolve real ARNs at deploy time; the `-XXXXXX` suffixes in the
committed JSON are placeholders.

### 12.5 `set -e` in a pasted script kills the SSM session — silently

The session exits, the prompt drops back to **CloudShell**, and everything you
paste next runs there: x86_64, no route to RDS, but Secrets Manager still works,
so it *looks* like it is working. This produced a false "the host is amd64"
conclusion and cost several rounds. **Assert `uname -m` before trusting any host
command**, and do not use `set -e` in a paste-into-interactive script.

### 12.6 Bash history expansion mangles the master secret's name

`rds!db-…` inside double quotes → `bash: !db: event not found`, leaving the
variable empty and every later step failing on empty input. Single-quote it and
`set +H`.

### 12.7 `docker run` without `-i` discards a heredoc and exits 0

The script reports success having done nothing. Observed once with the grant
script: it printed `GRANT STEP COMPLETE` and granted nothing. On the host, also
run as **root** — the interactive SSM user is `ssm-user`, not in the docker group
— and pass `--platform` explicitly if the daemon's default differs.

### 12.8 Killing a test run leaves cluster-wide state behind

`TaskStop` on pytest skips fixture teardown, orphaning scratch databases and —
because Postgres roles are cluster-global — leftover LOGIN roles. That produced
flaky, non-reproducing failures. Clean up before believing a flaky result:

```sql
DROP DATABASE IF EXISTS imageshield_test_… WITH (FORCE);
DROP ROLE IF EXISTS <leftover>;
```

### 12.9 A concurrent arm64 QEMU build starves the test suite

The suite is ~7 minutes idle. Run alongside emulated builds it took **9h37m**.
Sequence them; never run both.

---

## 13. Production differences — do not copy dev blindly

### 13.1 D16: the AI services opt-out is a real decision

Dev **waived** it (see `DEPLOY-DEV.md` §7.3): the account is not in an AWS
Organization, and `AISERVICES_OPT_OUT_POLICY` exists only at that level, so it
could not be applied at all. The consequence, stated plainly: any face submitted
to Rekognition may be retained and used by AWS for service improvement, possibly
outside the region.

**For production that should be closed before the first enrolment.** Create an
Organization containing the account (single-account is enough), enable the policy
type, attach:

```json
{"services":{"rekognition":{"opt_out_policy":{"@@assign":"optOut"}}}}
```

```bash
aws organizations describe-effective-policy \
  --policy-type AISERVICES_OPT_OUT_POLICY --target-id <account-id>
```

Nothing in this repo changes. If it is waived again, waive it **on the record**,
the way §7.3 does, so it is a decision and not an omission.

### 13.2 Provider state

Dev runs `SEARCH_PROVIDER=stub` with `stub` enabled and `hive`/`google`
**disabled**. That is correct for dev and wrong for production:

- Migration 0019 seeds `stub` **disabled**. Do not enable it in production:
  `v_person_report_summary.monitored_sources` counts providers that succeeded and
  are enabled, so an enabled stub tells users you monitor one more source than you
  do — the false claim CLAUDE.md §7.5 forbids.
- `hive`/`google` were disabled in dev only because no adapter is built under
  `SEARCH_PROVIDER=stub`, and a dispatch with no adapter records `error`, which
  counts as a breaker failure. Invariant #40: a breaker opens on brokenness, never
  on our own misconfiguration.

### 13.3 Thresholds are unmeasured

`SEARCH_MATCH_THRESHOLD=95` in dev was chosen to match the existing enrolment
threshold. **It is not a measured value**, and
`DEPLOY-DEV-HANDOFF.md` §11 says explicitly not to tune a threshold from a dev
measurement. Production needs a value derived from a labelled eval set.

### 13.4 `hive.cost_per_call_usd` is NULL

Hive is contract-priced and no measured figure exists here. A budget set without
a cost **fails closed** (invariant #38), so filling this in is a prerequisite for
capping Hive spend, not a nicety.

### 13.5 No provider is calibrated and no eval set exists

Everything therefore lands in the `review` band only — never `auto_confirm`, never
`drop` (§7.3). That is correct behaviour, not a gap to work around, but it means
there is no unattended path to alarming a user yet, and `review` requires a human
queue that is **not built** (CLAUDE.md §6).

### 13.6 Sizing, and `LOG_LEVEL`

Dev is one burstable instance with `LOG_LEVEL=debug`. Config **refuses to boot**
with `debug` under `ENVIRONMENT=production` — deliberate, since debug logs here
carry `user_ref`, bounding boxes and provider payloads. Do not size any pool from
a dev measurement (`DEPLOY-DEV-HANDOFF.md` §11); the rule that matters is
`tasks × pool ≤ the role's connection limit`.

---

## 14. Order of operations, condensed

1. Verify prerequisites and the host architecture (§1)
2. Build, push, verify the manifest (§2)
3. Execution role + task role; assert the policy shape (§3)
4. SQS queues + DLQs (§4)
5. Rekognition collections (§5) — **no enrolment yet**
6. The two master-only grants, from the host (§6a)
7. Register both task definitions (§7)
8. Run migrations; require exit 0 (§8)
9. Create the service with `minimumHealthyPercent=0` (§9)
10. `/readyz` = 200 with empty `problems` (§10)
11. Read the logs; confirm role, region, collection, and that timestamps and
    request ids survive (§11)
12. Hand the proxy team `PROXY-HANDOFF-*.md` — they need
    `ALTER ROLE migrator_backend CREATEROLE` before their first migration (§6c)

Steps 6 and 8 are where both real failures happened. Everything else went first
time.
