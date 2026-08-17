# DEPLOY-DEV-HANDOFF.md — what the repos build against

The dev environment exists and works. This is the contract between it and the two
repos: what an image must look like, what it will be given, and what it is
forbidden from holding.

Environment inventory and rationale live in `DEPLOY-DEV.md`. This document is
only the interface.

Copy this into `docs/` in **both** repos.

---

## 1. Hard requirements

Four things will fail immediately if you get them wrong.

**1. Images must be `linux/arm64`.** The host is Graviton (`t4g.medium`). An
amd64 image fails to start with an exec-format error that reads like a broken
entrypoint.

```bash
docker buildx build --platform linux/arm64 -t <tag> .
```

**2. Each service binds a fixed port on the host.** `networkMode` is `host`, so
there is no port mapping — the container binds the host's interface directly.

| Deployable | Port | Notes |
|---|---|---|
| `caddy` | 80, 443 | already running |
| `api` | **8080** | Caddy reverse-proxies to `localhost:8080` |
| `services` | **8081** | reached by `api` and `worker` at `localhost:8081` |
| `worker` | none | no listener |
| `image-worker` | none | `awsvpc`, no inbound at all |

Two containers cannot bind the same port. If `api` hardcodes 3000, change it or
tell me and I'll adjust Caddy.

**3. The backend holds no Rekognition credentials.** Not read-only, none. If a
task role for `api`, `worker` or `image-worker` contains any `rekognition:`
action, that is a bug, not a shortcut (D8, D15). Anything needing the identity
collection is an HTTP call to `services`.

**4. `image-worker` holds no database credential and no S3 permission.** It
receives presigned URLs in the SQS message and works from those alone (D9). It
also gets a hard memory limit and must self-enforce a per-message deadline,
because ECS-on-EC2 enforces memory but not time.

---

## 2. Build and push

```bash
export AWS_DEFAULT_REGION=ap-south-1
ACCT=225989356895
REGISTRY=$ACCT.dkr.ecr.ap-south-1.amazonaws.com

aws ecr get-login-password | docker login --username AWS --password-stdin $REGISTRY

# backend repo
SHA=$(git rev-parse --short HEAD)
docker buildx build --platform linux/arm64 -t $REGISTRY/imageshield/backend:$SHA . --push

# services repo
docker buildx build --platform linux/arm64 -t $REGISTRY/imageshield/services:$SHA . --push
```

**Tag with the git SHA, never `latest`.** ECS caches by tag; `latest` gives you
deploys that silently do nothing. Untagged images expire after 7 days
automatically.

One image serves all three backend deployables — they differ by entrypoint, not
by artifact. That keeps `api`, `worker` and `image-worker` provably built from the
same commit.

---

## 3. Two IAM roles, and they are not the same thing

This trips people up. Each task definition needs both.

**Execution role** — used by the ECS agent *before* your code runs, to pull the
image and to resolve `secrets` into environment variables. One role, shared by
all task definitions. **It does not exist yet — create it:**

```bash
cat > /tmp/ecs-tasks-trust.json <<'JSON'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
 "Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}
JSON

aws iam create-role --role-name imageshield-dev-exec \
  --assume-role-policy-document file:///tmp/ecs-tasks-trust.json \
  --tags Key=Env,Value=dev Key=Project,Value=imageshield

aws iam attach-role-policy --role-name imageshield-dev-exec \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy

cat > /tmp/exec-secrets.json <<'JSON'
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["secretsmanager:GetSecretValue"],
  "Resource":"arn:aws:secretsmanager:ap-south-1:225989356895:secret:imageshield/dev/*"},
 {"Effect":"Allow","Action":["kms:Decrypt"],
  "Resource":"arn:aws:kms:ap-south-1:225989356895:key/5b32f333-b48d-4ea0-b4a7-7b1c8e591c74"}
]}
JSON

aws iam put-role-policy --role-name imageshield-dev-exec \
  --policy-name read-dev-secrets --policy-document file:///tmp/exec-secrets.json
```

**Task role** — used by *your code* at runtime for AWS API calls. One per
deployable, scoped tightly:

| Deployable | Task role may do | Must not have |
|---|---|---|
| `api` | `s3:PutObject`/`GetObject` on the three dev buckets (for presigning), `sqs:SendMessage` on the four queues, `kms:Decrypt`/`GenerateDataKey` on the dev key | any `rekognition:*` |
| `worker` | as `api`, plus `sqs:ReceiveMessage`/`DeleteMessage`, `ses:SendEmail`, `sns:Publish` | any `rekognition:*` |
| `image-worker` | `sqs:ReceiveMessage`/`DeleteMessage` on `image-jobs` and `image-results`, `kms:Decrypt` for those queues | **any `s3:*`**, any DB secret, any `rekognition:*` |
| `services` | `rekognition:*` scoped to the two dev collection ARNs, `s3:GetObject` on the liveness bucket, `kms:Decrypt` | anything in the backend's schemas |

Add an explicit `Deny` on `rekognition:*` to the three backend roles rather than
just omitting it. Omission is invisible in review; a `Deny` is greppable, which is
what D8's CI assertion checks.

---

## 4. Secrets

Get the exact ARNs — they carry a random suffix:

```bash
aws secretsmanager list-secrets --filters Key=name,Values=imageshield/dev/ \
  --query 'SecretList[].{Name:Name,ARN:ARN}' --output table
```

ECS can extract a single JSON key, so your code receives the password directly
rather than parsing a blob. Append `:password::` to the ARN:

```json
"secrets": [
  {"name": "DB_PASSWORD",
   "valueFrom": "arn:aws:secretsmanager:ap-south-1:225989356895:secret:imageshield/dev/db/app_backend-XXXXXX:password::"}
]
```

| Deployable | Uses secret |
|---|---|
| `api` | `imageshield/dev/db/app_backend` |
| `worker` | `imageshield/dev/db/app_worker` |
| `services` | `imageshield/dev/db/app_services` |
| migration task (backend) | `imageshield/dev/db/migrator_backend` |
| migration task (services) | `imageshield/dev/db/migrator_services` |
| `image-worker` | **none** |

Secrets go in the `secrets` block, never in `environment`. An `environment` value
is visible in `describe-task-definition` to anyone with read access.

Still to be created when you wire up the providers: `imageshield/dev/twilio`,
`/stripe`, `/apple`, `/docuseal`, `/zendesk`, `/hive`, `/fcm`,
`/jwt/access-signing`, `/service-token/backend-to-services`,
`/service-token/admin`.

---

## 5. Environment

`sslmode=require` is mandatory — the parameter group sets `rds.force_ssl = 1`.

### Backend — `api` and `worker`

```
NODE_ENV=development
LOG_LEVEL=debug

DB_HOST=imageshield-dev.cdk8oguayyeg.ap-south-1.rds.amazonaws.com
DB_PORT=5432
DB_NAME=imageshield
DB_USER=app_backend            # app_worker for the worker
DB_SSLMODE=require
DB_POOL_MAX=5                  # see the connection budget below

AWS_REGION=ap-south-1
S3_BUCKET_UPLOADS=imageshield-dev-uploads-225989356895
S3_BUCKET_DERIVED=imageshield-dev-derived-225989356895
S3_BUCKET_LIVENESS=imageshield-dev-liveness-225989356895

SQS_DOMAIN_EVENTS=https://sqs.ap-south-1.amazonaws.com/225989356895/imageshield-dev-domain-events
SQS_IMAGE_JOBS=https://sqs.ap-south-1.amazonaws.com/225989356895/imageshield-dev-image-jobs
SQS_IMAGE_RESULTS=https://sqs.ap-south-1.amazonaws.com/225989356895/imageshield-dev-image-results
SQS_NOTIFICATIONS=https://sqs.ap-south-1.amazonaws.com/225989356895/imageshield-dev-notifications

SERVICES_BASE_URL=http://localhost:8081
PUBLIC_BASE_URL=https://dev.imageshield.com

PORT=8080                      # api only

MIN_ENROLMENT_AGE=18
ATTRIBUTION_MATCH_THRESHOLD=<measure it — see ARCHITECTURE §11>
ATTRIBUTION_MAX_CANDIDATES=20
PHOTO_UPLOAD_LIMIT=50
PHOTO_PROTECTION_REQUIRES_ALL_FACES=true
ACCESS_TOKEN_TTL=15m
REFRESH_TOKEN_TTL=30d
OTP_TTL=5m
OTP_MAX_ATTEMPTS=5
OTP_RESEND_COOLDOWN=30s
LIVENESS_SESSION_TTL=10m
LIVENESS_MAX_ATTEMPTS_24H=5
INVITE_TTL_DAYS=30
CLUSTER_TTL_DAYS=90
DIGEST_QUIET_HOURS=22:00-08:00
SCORING_VERSION=<pin it>
QUIZ_VERSION=<pin it>
CONSENT_DOCUMENT_VERSION=<dev-only value>
ENROLMENT_POLL_WINDOW_MS=25000
PRESIGN_TTL_SECONDS=1800
```

### `image-worker`

```
LOG_LEVEL=debug
AWS_REGION=ap-south-1
SQS_IMAGE_JOBS=<url above>
SQS_IMAGE_RESULTS=<url above>
IMAGE_WORKER_DEADLINE_MS=20000
```

No database, no buckets, no tokens. If it needs anything else, question the design
before adding it.

### `services`

```
LOG_LEVEL=debug
REKOGNITION_REGION=ap-south-1        # must equal the deployment region — D7
IDENTITY_COLLECTION=identity-dev-v1
DISCOVERED_COLLECTION=discovered-dev-v1
ENROLMENT_QUALITY_FILTER=HIGH
ENROLMENT_COLLISION_THRESHOLD=<pin it>
SEARCH_MATCH_THRESHOLD=<pin it — not 80>
ATTRIBUTION_MAX_INFLIGHT=4
SEARCH_PROVIDER=stub
DEV_FACE_CEILING=50
PORT=8081
```

The collections do not exist yet — create them when `services` first deploys.

### Two numbers that constrain each other

`PRESIGN_TTL_SECONDS` must exceed `image-jobs` visibility × maxReceiveCount plus
slack: 180 × 5 = 900, so **1800 minimum** (D1). Assert it at boot. Get it wrong
and the last retry of an image job fails on an expired URL and looks exactly like
a decode bug in the DLQ.

**Connection budget.** `db.t4g.micro` gives ~100 connections. Role limits are
`app_backend` 20, `app_worker` 10, `app_services` 20. With one task each, keep
pools at 5 and you are nowhere near the ceiling. The rule that matters later:
`tasks × pool ≤ the role's limit` (D2).

### Memory on a 4 GiB box

| | MiB |
|---|---|
| `caddy` | 256 |
| `api` | 512 |
| `worker` | 512 |
| `services` | 768 |
| `image-worker` | 1024 |
| **total** | **3072** |

That leaves headroom for the ECS agent and the host. `image-worker`'s 1024 is a
**hard limit and a security control** — a 48 MP HEIF decode is meant to be killed
rather than allowed to consume the box. If decodes start OOM-killing, raise the
number; do not remove the limit.

---

## 6. Task definition shape

```json
{
  "family": "imageshield-dev-api",
  "networkMode": "host",
  "requiresCompatibilities": ["EC2"],
  "executionRoleArn": "arn:aws:iam::225989356895:role/imageshield-dev-exec",
  "taskRoleArn": "arn:aws:iam::225989356895:role/imageshield-dev-api",
  "containerDefinitions": [{
    "name": "api",
    "image": "225989356895.dkr.ecr.ap-south-1.amazonaws.com/imageshield/backend:<sha>",
    "essential": true,
    "memory": 512,
    "command": ["node", "dist/api.js"],
    "environment": [ { "name": "PORT", "value": "8080" } ],
    "secrets": [ { "name": "DB_PASSWORD", "valueFrom": "<arn>:password::" } ],
    "healthCheck": {
      "command": ["CMD-SHELL", "wget -qO- http://localhost:8080/healthz || exit 1"],
      "interval": 30, "timeout": 5, "retries": 3, "startPeriod": 30
    },
    "logConfiguration": {
      "logDriver": "awslogs",
      "options": {
        "awslogs-group": "/imageshield/dev",
        "awslogs-region": "ap-south-1",
        "awslogs-stream-prefix": "api"
      }
    }
  }]
}
```

`image-worker` differs: `networkMode: awsvpc`, `networkConfiguration` in the
*service* pointing at `subnet-064af0bc4c4ee9ae1` (private2, ap-south-1b) with its
own security group, and no `executionRoleArn` secret access beyond the queue key.
That subnet has no internet route, so it also needs the SQS interface endpoint
created at the same time (D10).

Services are created with `minimumHealthyPercent=0, maximumPercent=100`. On one
instance with fixed host ports, ECS cannot start a replacement while the old task
holds the port — the default 100 makes every deploy hang forever.

---

## 7. Migrations

Run as a one-off ECS task, not from a laptop and not on container start.

```bash
aws ecs run-task --cluster imageshield-dev --launch-type EC2 \
  --task-definition imageshield-dev-migrate-backend \
  --query 'tasks[0].taskArn' --output text
```

Four rules:

**`services` migrates first.** It creates the `svc` schema and the four contract
views. The backend cannot pass readiness without them — `REPO-SPLIT.md` §5 step 1.

**The backend never creates anything in `svc`** (D12). No exceptions, not even in
dev, not even "temporarily". `services` owns that schema and runs in the same
cluster.

**No `svc._stub_*` in any deployed environment.** The stub fixture belongs to
docker compose and the test harness only. In dev, `services` creates the real
views.

**Every migration is N-1 safe** (D11). A rolling deploy runs old and new
simultaneously, so expand-contract always: add and backfill, then read, then drop
— three deploys, never one.

Schema-level grants belong in migrations, not in a bootstrap script: the
`INSERT`-only grant on `shared.audit_log` (P25), and `SELECT` for `app_backend`
and `app_worker` on the four `svc` views and nothing else in `svc`.

Roles, connection limits and statement timeouts are **already applied** — see
`DEPLOY-DEV.md` §4. Do not recreate them.

---

## 8. Deploy

```bash
aws ecs update-service --cluster imageshield-dev --service api \
  --task-definition imageshield-dev-api --force-new-deployment

aws ecs wait services-stable --cluster imageshield-dev --services api
```

When something won't start, this says why in plain words before you go digging:

```bash
aws ecs describe-services --cluster imageshield-dev --services api \
  --query 'services[0].{Running:runningCount,Events:events[0:4].message}' --output json

aws logs tail /imageshield/dev --since 10m --filter-pattern api
```

---

## 9. Caddy

Currently serving a placeholder. When `api` is up, replace `/etc/caddy/Caddyfile`
on the host:

```
{
	email mokshith@flashbacklabs.com
}

dev.imageshield.com {
	encode zstd gzip

	@app path /v1/* /public/* /webhooks/*
	handle @app {
		reverse_proxy localhost:8080 {
			transport http {
				read_timeout 60s
				write_timeout 60s
			}
		}
	}

	handle {
		root * /srv/www
		file_server
		try_files {path} /index.html
	}
}
```

Then `aws ecs update-service --cluster imageshield-dev --service caddy
--force-new-deployment`.

The 60-second timeouts exist because of `ENROLMENT_POLL_WINDOW_MS=25000` (D6). If
enrolment becomes a client poll instead of a long-poll, they can drop — that
decision is still open.

The test web app is served from the same origin as the API, which is what removes
CORS from the API entirely. You still need CORS on the uploads bucket, allowing
`PUT` from `https://dev.imageshield.com` only, because a presigned PUT from a
browser is cross-origin by definition.

---

## 10. Before calling it done

Mechanically checkable:

- [ ] `docker manifest inspect` shows `arm64` for both images
- [ ] no `rekognition:` string in the rendered `api`, `worker` or `image-worker`
      task role documents
- [ ] `image-worker`'s task role contains no `s3:` action and no `db/` secret ARN
- [ ] `sg-rds` ingress does not reference `image-worker`'s security group
- [ ] every secret arrives via `secrets`, none via `environment`
- [ ] deleting any single required env var makes the process exit non-zero and
      name that variable
- [ ] `PRESIGN_TTL_SECONDS` ≥ 1800, asserted at boot
- [ ] `services` refuses to boot if `REKOGNITION_REGION` ≠ `ap-south-1`
- [ ] `LOG_LEVEL=debug` would fail config validation with `NODE_ENV=production`
- [ ] `/readyz` fails when any of the four `svc` views is missing or wrong-shaped,
      and `grep svc.v_person_` finds only the contract module
- [ ] a log line containing a phone number, email, token, receipt and OTP emits
      none of them
- [ ] `migrate up → down → up` against the real dev database produces an
      identical schema

---

## 11. Do not do these

- Do not put the database on a public subnet or add your IP to `sg-rds`. There is
  no path from CloudShell to Postgres **on purpose**; work from the host.
- Do not load production data into dev. Ever, not for debugging (D14).
- Do not give `image-worker` a database credential because it would be
  convenient. Taking a credential away later is much harder than never granting it.
- Do not enrol a face — including your own — before the AWS AI services opt-out
  policy returns `optOut` for Rekognition (D16). `DEPLOY-DEV.md` §7 has the
  command.
- Do not tune any threshold or size any pool from a dev measurement. Dev is a
  single burstable instance in a different region with a Face Liveness quota one
  fifth of production's.

---

## 12. What the services repo deliberately does not implement

Recorded here so the deploy side does not set a variable expecting an effect.

- **`ENROLMENT_QUALITY_FILTER`** — accepted and ignored. Invariant #5 fixes
  `QualityFilter: HIGH` on every `IndexFaces`, permanently. A poor enrolment
  vector degrades every match that user will ever get and they have no way to
  know, so this is not a knob.
- **`DISCOVERED_COLLECTION`** — validated, no reader. `discovered-v1` and
  clustering are "specified, do not build yet" (CLAUDE.md §6). The dev
  collection is created empty so nothing writes to a missing collection later.
- **`ENROLMENT_COLLISION_THRESHOLD`** — validated, no reader, and giving it one
  is invariant #1 territory: identity must never come from a similarity score.
  Read #1 and #1a before wiring it.
- **`SEARCH_MATCH_THRESHOLD`** — implemented and required, with no default, so
  the process refuses to boot until someone pins it. Not derivable from a dev
  measurement (§11).

`SEARCH_PROVIDER=stub` is enforced in config when `ENVIRONMENT=development`: the
dev Hive key is real, Hive has no sandbox, and `hive.cost_per_call_usd` is NULL,
so the budget guard fails closed and caps nothing.

That assertion is only half the switch, and for one release it was the only half:
nothing outside `config.py` read the value, and `search/worker.py:build_providers`
constructed the real Hive and Google adapters unconditionally. It now builds
`search/stub.py` **instead of** them, so no object in the worker process holds a
live provider key. The reverse edge is refused too — `SEARCH_PROVIDER=stub` with
`ENVIRONMENT=production` will not boot, because the stub searches nothing and a
deploy carrying it would report "no matches in monitored sources" for every user
with no error anywhere.

One consequence to expect in dev: `providers_attempted` comes from
`providers.enabled` in the database, which lists `hive` and `google` and not
`stub`. So a `POST /v1/search` under the stub records one `provider_calls` row per
provider with `status='error'` and
`error_detail='no adapter registered for this provider'`, and the run completes
with `providers_succeeded = []` — no network call, no attestation, and no cadence
change (invariant #42: a run where nothing succeeded is not evidence of an empty
scan). That is the honest outcome, not a bug to paper over; the stub exists to
make billable traffic impossible, not to make a dev run look successful.
