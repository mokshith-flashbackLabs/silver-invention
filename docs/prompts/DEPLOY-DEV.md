# DEPLOY-DEV.md — ImageShield development environment

**As-built, 13 August 2026.** This describes what exists in AWS, not a proposal.
§3 records where it diverges from the original plan and why. §7 lists what is
still outstanding.

Rules live in `CLAUDE.md`, shape in `ARCHITECTURE.md`, tables in `SCHEMA.md`,
repo boundaries in `REPO-SPLIT.md`.

---

## 1. Shape

```
AWS account 225989356895 · ap-south-1 (Mumbai)

                      internet
                          │ 80, 443
              ┌───────────▼──────────────┐
              │  Elastic IP 13.126.66.13 │  survives stop/start
              └───────────┬──────────────┘
┌─────────────────────────▼────────────────────────────────────┐
│ public2  10.20.3.0/24  ap-south-1b                            │
│                                                               │
│  i-0d277703b778392ef · t4g.medium · arm64 · ECS agent · SSM    │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │ caddy  ✅ dev.imageshield.com · Let's Encrypt · host mode │  │
│  │ api · worker · services      networkMode: host           │  │  ← not built
│  │ image-worker                 networkMode: awsvpc, own SG │  │  ← not built
│  └─────────────────────────────────────────────────────────┘  │
└─────────────────────────┬─────────────────────────────────────┘
                          │ 5432, cross-AZ
        ┌─────────────────▼────────────────────┐
        │ private1 / private2 — no IGW route    │
        │ RDS Postgres 16.14 · db.t4g.micro     │
        │ single-AZ ap-south-1a · encrypted     │
        └───────────────────────────────────────┘

S3 via gateway endpoint (free) · no NAT gateway · no ALB
```

The database is reachable only from things wearing the host security group —
not from a CIDR, from a group reference. That single rule is the whole database
security model, and it means a replacement host inherits the permission
automatically while nothing else ever gets it.

---

## 2. Inventory

Everything below exists and is verified. Keep this section current; it is the
part people actually come back for.

### Network

| | |
|---|---|
| VPC | `vpc-0caaf057832c20c36` — `imageshield-dev-vpc`, `10.20.0.0/16` |
| public1 · 1a | `subnet-0dbe4c3c66e3af522` |
| public2 · 1b | `subnet-038bba8373da41a05` — **the host lives here** |
| private1 · 1a | `subnet-0b67025cc37e36ce8` |
| private2 · 1b | `subnet-064af0bc4c4ee9ae1` |
| Endpoints | S3 gateway only |
| NAT | **none** |

### Security groups

| Name | Id | Rules |
|---|---|---|
| `imageshield-dev-host` | `sg-08c8a715af3deb57d` | in: 80, 443 from `0.0.0.0/0` |
| `imageshield-dev-rds` | `sg-022501fbda5133bc1` | in: 5432 from `sg-08c8a715af3deb57d` **only** |

No port 22 anywhere. Shell access is SSM Session Manager — no keypair to lose,
and every session is logged.

### Compute

| | |
|---|---|
| Instance | `i-0d277703b778392ef` · `t4g.medium` · ap-south-1b |
| AMI | `ami-0854b84ad02b95c3e` — ECS-optimised AL2023 **arm64** |
| Root volume | 30 GiB gp3, encrypted, delete-on-termination |
| Metadata | **IMDSv2 required** (`HttpTokens=required`) |
| Elastic IP | `eipalloc-019fbfceaf98c83e0` → `13.126.66.13` |
| ECS cluster | `imageshield-dev` — one registered container instance |
| ECS service | `caddy` — 1 task, `networkMode: host`, steady state |
| Hostname | **`dev.imageshield.com`** → `13.126.66.13`, Cloudflare **DNS-only** |
| Certificate | Let's Encrypt, obtained 17 Aug 2026 via `tls-alpn-01`, stored in `/var/lib/caddy` |
| Role / profile | `imageshield-dev-host` (both) |
| Managed policies | `AmazonSSMManagedInstanceCore`, `AmazonEC2ContainerServiceforEC2Role` |
| Inline policy | `read-dev-db-secrets` — `GetSecretValue` on `rds!db-*` and `imageshield/dev/*`, `kms:Decrypt` on the dev CMK |

### Data

| | |
|---|---|
| Instance | `imageshield-dev` · PostgreSQL **16.14** · `db.t4g.micro` |
| Endpoint | `imageshield-dev.cdk8oguayyeg.ap-south-1.rds.amazonaws.com:5432` |
| Database | `imageshield` |
| Storage | 20 GiB gp3, autoscale ceiling 60 |
| Placement | single-AZ **ap-south-1a**, not publicly accessible |
| Master user | `isadmin`, password managed by RDS in Secrets Manager |
| Master secret | `arn:aws:secretsmanager:ap-south-1:225989356895:secret:rds!db-9ef90c0b-67fb-4c28-b944-21f449f273ba-83e3We` |
| Subnet group | `imageshield-dev` — both private subnets |
| Parameter group | `imageshield-dev-pg16` |
| Backups | 1 day, window 20:00–20:30 UTC · maintenance Sun 21:00–22:00 UTC |
| Perf Insights | on, 7-day retention (free tier) |
| Log export | `postgresql` → CloudWatch |
| Deletion protection | off |

Parameter group values: `rds.force_ssl = 1`,
`log_min_duration_statement = 200`,
`shared_preload_libraries = pg_stat_statements,pg_tle`.

### Encryption

| | |
|---|---|
| CMK | `5b32f333-b48d-4ea0-b4a7-7b1c8e591c74` · `alias/imageshield-dev` |

Used by: RDS storage, the RDS master secret, all three S3 buckets, all eight SQS
queues, and the six role secrets.

### Buckets

All `ap-south-1`, SSE-KMS with the dev CMK, **bucket keys enabled**, ACLs
disabled, bucket-owner enforced, all public access blocked, **unversioned**.

| Bucket | Lifecycle |
|---|---|
| `imageshield-dev-uploads-225989356895` | abort incomplete MPU after 1 day |
| `imageshield-dev-derived-225989356895` | none |
| `imageshield-dev-liveness-225989356895` | expire 30 days |

Bucket keys matter: without them you pay a KMS request per object operation,
which on a photo pipeline is a real line item rather than a rounding error.

No expiry on `uploads` or `derived` **on purpose.** `CLUSTER_TTL_DAYS` is 90, so
expiring a face crop earlier leaves `media.face_clusters` rows pointing at
absent objects and the claim flow with nothing to render. Photo rows are
soft-deleted, so the bytes must outlive them.

Unversioned **on purpose.** A versioned bucket means a "deleted" object leaves a
version behind, and P19 requires deletion to be real.

### Queues

Prefix `imageshield-dev-`. All standard, SSE-KMS with the dev CMK. Each has a
`-dlq` at `maxReceiveCount = 5`. Live retention 4 days, DLQ retention 14 days.

| Queue | Visibility | Producer → consumer |
|---|---|---|
| `domain-events` | 30s | outbox relay → worker |
| `image-jobs` | 180s | worker → image-worker |
| `image-results` | 60s | image-worker → worker |
| `notifications` | 60s | worker → worker |

`image-jobs` at 180s is what sets the presign floor — see D1.

### Registry and logs

| | |
|---|---|
| ECR | `225989356895.dkr.ecr.ap-south-1.amazonaws.com/imageshield/backend` |
| | `225989356895.dkr.ecr.ap-south-1.amazonaws.com/imageshield/services` |
| ECR policy | scan on push; untagged images expire after 7 days |
| Log group | `/imageshield/dev`, 7-day retention, no CMK (see §3.7) |

ECR carries no `Env` tag — one registry serves both environments, with image
tags being git SHAs.

### Secrets

| Path | Contents |
|---|---|
| `imageshield/dev/db/app_backend` | `{username, password, host, port, dbname}` |
| `imageshield/dev/db/app_worker` | same shape |
| `imageshield/dev/db/app_services` | same shape |
| `imageshield/dev/db/migrator_backend` | same shape |
| `imageshield/dev/db/migrator_services` | same shape |
| `imageshield/dev/db/readonly_debug` | same shape |
| `rds!db-9ef90c0b-…` | RDS-managed master credential |

Passwords are 32-character CSPRNG, generated in CloudShell, never displayed, and
never written to a file that survives. The host reads them; only you can create
or modify them.

---

## 3. Where this diverges from the original plan

Each of these was a deliberate call made during the build. The cost is named.

**3.1 Fargate → ECS on one EC2 instance.** ECS itself is free; Fargate was the
$55/month line. One `t4g.medium` runs all containers for $25. Task definitions
are the same files you will use in production — switching to Fargate is a
capacity-provider setting, not a rewrite. Cost: a dead instance takes dev with
it until replaced, and `networkMode` differs for the bridge-mode tasks.

**3.2 ALB → Caddy container.** Built and working. Saves $18/month and gets
automatic Let's Encrypt certificates — it chose `tls-alpn-01` over port 443 by
itself rather than the HTTP-01 challenge. Cost: the ALB's idle timeout and
connection-draining behaviour go unexercised until production. Caddy has
equivalents, so *"does a 25-second held request survive a reverse proxy"* is
still testable — just not against the exact proxy production runs.

Two things this required:

- **`/var/lib/caddy` mounted at `/data`.** Certificates and the ACME account key
  live there. Without a persistent mount every container restart requests a fresh
  certificate and burns Let's Encrypt's five-duplicates-per-week limit in about
  four restarts.
- **`minimumHealthyPercent=0`.** With host networking on fixed ports and one
  instance, ECS cannot place a replacement task while the old one holds 443. Left
  at the default 100, every deploy hangs forever waiting for a placement that
  cannot happen. Cost: a few seconds of downtime per Caddy deploy.

**3.2a Bridge → host networking.** The plan said `bridge`; `host` is what got
built for Caddy and is what `api`, `worker` and `services` should use too. On
bridge, `localhost` inside the Caddy container means the container itself, so
reaching `api` needs container links or the docker bridge IP — an hour of
confusion for no benefit. On host, Caddy reaches `api` at `localhost:8080`
directly. `image-worker` stays `awsvpc` so it can keep its own security group.
Cost: `networkMode` differs from Fargate's required `awsvpc`, on top of the
divergence already accepted in 3.1.

**3.3 No NAT gateway.** Saves ~$40/month. The instance sits in a public subnet
with an Elastic IP and reaches the internet directly. Cost: outbound path
differs from production. No behavioural impact.

**3.4 Host in ap-south-1b, database in 1a.** Not a choice — `t4g.medium`
capacity was exhausted in 1a at build time. Every query now crosses an
availability zone: roughly 1 ms extra and $0.01/GB each way, which is cents per
month here. Not worth moving the database for. In production, pin the writer and
the primary tasks to one AZ.

**3.5 Four subnets, not five.** The VPC wizard produced 2 public + 2 private.
`image-worker` will run `awsvpc` in `private2` with its own security group rather
than in a dedicated isolated tier — the isolation comes from the security group,
not the subnet, so nothing is lost.

**3.6 SQS interface endpoint deferred.** $7/month for something only
`image-worker` uses, and `image-worker` does not exist yet. Create it with
`image-worker`. Until then the `no VPC access beyond S3` property (D10) is
**unimplemented, not implemented-differently.**

**3.7 Log group is not CMK-encrypted.** A customer-managed key requires its
policy to name the CloudWatch Logs service principal. Skipped for dev logs that
are already redaction-filtered. Production should do it properly.

**3.8 Cost: $146/month planned → ~$52 actual.** See §9.

---

## 4. Postgres

### Roles, as created

| Role | Conn limit | `statement_timeout` | `idle_in_transaction` | Used by |
|---|---|---|---|---|
| `app_backend` | 20 | 3s | 10s | `api` — request loop, p95 < 300 ms |
| `app_worker` | 10 | 120s | 60s | `worker` — periodic and batch |
| `app_services` | 20 | 30s | 30s | `services` |
| `migrator_backend` | 5 | 0 | 0 | backend migrations |
| `migrator_services` | 5 | 0 | 0 | services migrations |
| `readonly_debug` | 3 | 10s | 30s | humans |

`api` and `app_worker` deliberately do **not** share a role. One
`statement_timeout` cannot serve both a 300 ms request loop and subscription
reconciliation, and a shared connection limit lets `api` starve the worker.
Per-role `statement_timeout` is the only control against a heavy `services`
query starving `api` reads on the shared cluster (`ARCHITECTURE.md` §11).

`idle_in_transaction_session_timeout` was not in the original plan. It is here
because a transaction left open holding locks is how you lose an afternoon on a
100-connection instance.

### Also applied

```sql
REVOKE ALL ON DATABASE imageshield FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT CREATE ON DATABASE imageshield TO migrator_backend, migrator_services;
```

Verified negatively: `app_backend` attempting `CREATE SCHEMA` returns
`permission denied for database imageshield`. A permission you have not seen
refused is a permission you do not know you have.

### What the bootstrap deliberately does not do

No schemas. No table grants. Those belong in each repo's migrations, because
`CLAUDE.md` §8 makes migrations the single source of truth for schema —
including the `INSERT`-only grant on `shared.audit_log` (P25) and the `SELECT`
grants on the four `svc` views. A bootstrap that half-creates schemas gives you
two places to look when a grant is wrong.

### No `svc` stubs in dev

`services` creates the real `svc` schema and the four contract views, in dev
exactly as in production — `REPO-SPLIT.md` §5 step 1. The `svc._stub_*` fixture
stays in docker compose and the test harness only (D12).

---

## 5. Runbook

**Shell onto the host**

```bash
aws ssm start-session --target i-0d277703b778392ef
```

**Connect to Postgres** — only from the host; there is no path from CloudShell,
and that is correct.

```bash
DB_HOST=imageshield-dev.cdk8oguayyeg.ap-south-1.rds.amazonaws.com
export PGPASSWORD=$(aws secretsmanager get-secret-value --region ap-south-1 \
  --secret-id imageshield/dev/db/readonly_debug \
  --query SecretString --output text | jq -r .password)
psql "host=$DB_HOST port=5432 dbname=imageshield user=readonly_debug sslmode=require"
```

`sslmode=require` is mandatory — `rds.force_ssl = 1`. Use `readonly_debug` for
looking, the master only for role changes, and `unset PGPASSWORD` when done.

**CloudShell preamble.** Paste this first, every time — or put it in
`~/.bashrc`, which survives session resets:

```bash
export AWS_PAGER="" AWS_DEFAULT_REGION=ap-south-1
ACCT=225989356895
VPC_ID=vpc-0caaf057832c20c36
KEY_ID=5b32f333-b48d-4ea0-b4a7-7b1c8e591c74
SG_HOST=sg-08c8a715af3deb57d
SG_RDS=sg-022501fbda5133bc1
SUBNET_PUBLIC=subnet-038bba8373da41a05
DB_HOST=imageshield-dev.cdk8oguayyeg.ap-south-1.rds.amazonaws.com
INSTANCE_ID=i-0d277703b778392ef
: "${KEY_ID:?}" "${ACCT:?}" "${VPC_ID:?}"
```

That last line aborts on an unset variable. **This mattered three times during
the build** — an empty variable produces a half-applied resource whose loop
still prints "ready", which is worse than an error.

**Two operational lessons worth keeping:**

- `AWS_PAGER=""` or the CLI pipes output through `less`, and anything you paste
  while it is open goes into `less` instead of the shell. That silently ate a
  security-group creation.
- Before overwriting a list-valued RDS parameter, read the current value. The
  default `shared_preload_libraries` is `pg_stat_statements,pg_tle`; setting it
  to just `pg_stat_statements` silently drops `pg_tle`.

---

## 6. Verified

Not assumed — actually observed:

- Postgres reachable from the host over TLS; `imageshield` database exists
- All six roles present with correct connection limits and both timeouts
- `app_backend` logs in with its Secrets Manager password and inherits `3s`/`10s`
- `app_backend` cannot `CREATE SCHEMA`
- ECS pulls an image and places a task; `ecs.cpu-architecture: arm64`
- nginx on the host answered `HTTP/1.1 200` on `13.126.66.13` — the full ingress
  path proven end to end before TLS was added to the picture
- All three buckets report `aws:kms` with bucket keys enabled
- `dig +short dev.imageshield.com` → `13.126.66.13`
- Caddy obtained a Let's Encrypt certificate for `dev.imageshield.com` and
  reached ECS steady state; HTTPS answers in a browser

---

## 7. Outstanding

Infrastructure is complete. What follows is application work plus two
configuration items.

### Do this first

1. **The AI services opt-out policy (D16).** Highest priority of anything on
   this list. Dev will hold **real consented faces** (§8), and by default AWS AI
   services may store and use customer content for service improvement, possibly
   outside the region. Create an Organization containing this account, apply
   `{"services":{"rekognition":{"opt_out_policy":{"@@assign":"optOut"}}}}` to the
   root, and verify:

   ```bash
   aws organizations describe-effective-policy \
     --policy-type AISERVICES_OPT_OUT_POLICY --target-id 225989356895
   ```

   If that does not return `optOut` for `rekognition`, the policy does not reach
   the management account and you need one member account to run workloads in.
   **No face is enrolled until this returns `optOut`.**

### Then

2. **CORS on `imageshield-dev-uploads`** — allow `PUT` from
   `https://dev.imageshield.com` only. A presigned PUT from a browser is
   cross-origin by definition. Serving the test web app from the same Caddy
   origin as the API means no CORS on the API itself.
3. **Provider sandbox accounts and their secrets** — Twilio subaccount with a
   test number, Stripe test-mode keys *and* the test-mode webhook signing secret,
   Apple sandbox, dev DocuSeal template with a dev
   `CONSENT_DOCUMENT_VERSION`, Zendesk sandbox, dev Firebase project, SES
   verified recipients only. Hive stays stubbed (`SEARCH_PROVIDER=stub`) — it
   has no sandbox and it is a paid API against real adult-content indexes.
4. **Task definitions and task roles** for `api`, `worker`, `image-worker`,
   `services`, with D8/D9/D15 enforced at creation. See
   `DEPLOY-DEV-HANDOFF.md` — that document is what the two repos build against.
5. **SQS interface endpoint** plus `image-worker`'s `awsvpc` security group in
   `private2`, at the point `image-worker` is built (D10).
6. **Schemas**, via each repo's migrations. `services` first — the backend cannot
   pass readiness without the four `svc` views.
7. **The `/readyz` contract check** (D13), inside the contract module.
8. **Point Caddy at `api`** — replace the placeholder `respond` with
   `reverse_proxy localhost:8080` for `/v1/*`, `/public/*` and `/webhooks/*`, and
   set read/write timeouts to 60s (D6).
9. **Overnight shutdown schedule** — EventBridge stopping the instance and the
   database at 21:00 IST, starting at 09:00 IST, weekends off. RDS cannot stay
   stopped beyond 7 days.
10. **Synthetic seed**, then team consent and enrolment (§8), then the
    `DEV_FACE_CEILING` check.

---

## 8. Faces and data in dev

**Real, consented team faces for enrolment. Synthetic data for everything else.**

There was no alternative. Face Liveness requires a live human in front of a
camera — `ARCHITECTURE.md` §7 calls it *"the only evidence the subject was
physically present."* Synthetic faces cannot pass it, so a synthetic-only dev
environment can never execute the enrolment loop, which makes `REPO-SPLIT.md`
§5's step-5 gate and `CLAUDE.md` §10's *"do not start 7 before 6 is verified end
to end"* unreachable.

| Data | Dev source |
|---|---|
| Accounts, households, subscriptions, quiz responses | synthetic seed script |
| Photos for attribution and clustering | synthetic or stock, never scraped |
| **Enrolled faces** | **the team's own, each with a real signed consent record** |
| Search results | stubbed provider |

What this obliges:

1. **You are your own first consent subjects.** Each team member signs the real
   document through the real DocuSeal flow. You find out immediately whether
   that flow is tolerable, before a stranger meets it.
2. **The opt-out policy lands first** (§7.3).
3. **Teardown runs a real, confirmed `DeleteFaces`**, then verifies absence, in
   the order P19 requires. `DeleteFaces` has **zero call sites anywhere in the
   legacy repo**, beneath a comment asserting BIPA compliance. Dev teardown is
   your first chance to prove the delete path works — treat it as a test, not a
   chore.
4. **Production data still never reaches dev.** Consenting to your own face is
   not consent to hold a user's. No snapshot-share or cross-region restore
   resource exists in dev; adding one is a reviewable diff.

The face-count tripwire runs **inside `services`**, on a schedule, asserting
`DescribeCollection('identity-dev-v1').FaceCount < DEV_FACE_CEILING` (50 — you
know how many people are on the team). It is not a backend Lambda, because
`ARCHITECTURE.md` §1 is explicit: *"the credentials are not present, so the
mistake cannot be made."* A second Rekognition-credentialed principal would
break that (D15).

Redaction applies at every log level including `debug`, because dev holds real
faces and real consent records.

---

## 9. Logging

The important records are **not** in CloudWatch. `shared.service_calls` and
`shared.audit_log` are Postgres tables — the audit trail and the cross-repo
debugging trail, queryable with SQL at roughly $0.10/GB/month. That is what
makes CloudWatch disposable: it holds only *"what happened inside this
process,"* which you need for days, not months.

| | dev | prod |
|---|---|---|
| `LOG_LEVEL` | `debug` | `info` |
| Retention | 7 days | 30 days |
| Kysely SQL logging | on | off |
| SQS message bodies | on | off |
| Request/response envelope (redacted) | on | off |
| RDS `log_min_duration_statement` | 200 ms | 1000 ms, not exported |

Ingestion is the cost, not storage — about $0.60/GB in Mumbai. At `info` with one
line per request, 5M requests a month at ~500 bytes is roughly **$2**. It only
explodes at `debug`.

So the control that matters is not trimming production logs. It is that
**`LOG_LEVEL=debug` in production fails config validation at boot** (D23), the
same way every other bad config value does. Otherwise someone flips it on to
chase a bug and forgets for three weeks.

Two traps: never log the EXIF blob on an `image-worker` decode failure — that is
the noisiest path and the payload is large; log `photo_id` and the error class.
And the redaction allowlist applies at `debug` too.

---

## 10. Config keys

`CLAUDE.md` forbids literals outside the config module, so every number here is
a validated key. The **Owner** column matters — a services-side key in the
backend's required set makes the backend refuse to boot over something it never
reads.

| Key | Owner | Dev value |
|---|---|---|
| `LOG_LEVEL` | both | `debug` (production rejects `debug` — D23) |
| `ENROLMENT_POLL_WINDOW_MS` | backend | `25000` |
| `PRESIGN_TTL_SECONDS` | backend | `1800` — see D1 |
| `IMAGE_WORKER_DEADLINE_MS` | image-worker | `20000` |
| `REKOGNITION_REGION` | **services** | `ap-south-1`, must equal deployment region |
| `ATTRIBUTION_MAX_INFLIGHT` | **services** | `4` |
| `SEARCH_PROVIDER` | **services** | `stub` |
| `DEV_FACE_CEILING` | services tooling, **not** §9 | `50` |

`DEV_FACE_CEILING` stays out of the boot-validated set — a dev-only threshold in
`ARCHITECTURE.md` §9 would make production refuse to start without it.

---

## 11. Cost

Actual, as built:

| Item | ≈ |
|---|---|
| EC2 `t4g.medium` | $25 |
| EBS 30 GiB gp3 | $3 |
| Elastic IP (public IPv4 charge) | $4 |
| RDS `db.t4g.micro` + 20 GiB | $16 |
| Secrets Manager, 7 entries | $3 |
| KMS CMK | $1 |
| S3, SQS, ECR, CloudWatch at rest | ~$1 |
| **Total** | **≈ $52/month** |

Still to come: the SQS interface endpoint (+$7) and the remaining provider
secrets (+$3), landing around **$62**.

Levers that do not change topology:

- **Weekday shutdown** — stop the instance and the database at 21:00 IST, start
  at 09:00 IST, weekends off. **≈ −$22 → $30/month.** The Elastic IP is what
  makes this safe (D21).
- **Parameter Store instead of Secrets Manager** (−$3).

Not paid for: NAT gateway (−$40), ALB (−$18), Fargate premium (−$30), second AZ,
Multi-AZ.

---

## 12. Where dev diverges from production

| | dev | prod | Changes behaviour? |
|---|---|---|---|
| Region | ap-south-1 | us-east-1 | no — but forces region/ARN/bucket to be config, which is the point |
| Capacity | ECS on one EC2 | ECS on Fargate | no. Same task definitions; `networkMode` differs |
| Ingress | Caddy container | ALB + WAF | no, except ALB timeouts unexercised |
| Egress | host public IP | NAT gateway | no |
| AZs | 1 | 3 | no |
| Database | single-AZ `t4g.micro` | Multi-AZ | no |
| Host/DB co-location | split across AZs | same AZ | no, ~1 ms |
| Face Liveness quota | 5 TPS / 15 concurrent | 25 / 75 | **yes, at load** |
| Search provider | stubbed | Hive | **yes — unproven until production** |
| Autoscaling | none | on the correct signals | yes, at load |
| Log level | `debug` | `info` | no |

**What dev therefore cannot answer:** any performance question, any capacity
question, and whether the Hive integration works. Do not conclude those three
from a green dev environment.

---

## 13. Production decisions locked

Sizing, autoscaling targets, deploy strategy and DR numbers belong to the
production discussion. These do not, because they are expensive to unwind:

1. **us-east-1, single region, multi-AZ, no multi-region.** us-east-1 and
   us-west-2 are the only regions with 25 TPS / 75 concurrent Face Liveness —
   everywhere else is 5 / 15. The region is justified by quota, not habit.
   us-west-2 is the swap if you would rather not sit in us-east-1: same quota,
   better availability record, ~70 ms further from US-East users. **Multi-region
   is cut** — Rekognition collections do not replicate, and every replica of
   biometric data is a second place `DeleteFaces` must be confirmed (P19). Cost:
   a regional outage is a full outage. Compensating: Multi-AZ, PITR, and a
   status page that says so.
2. **No RDS Proxy anywhere** (D4).
3. **`api` is never Lambda** (D5).
4. **`api`'s autoscaling ceiling is the connection budget, not CPU** (D2).
5. **`worker` stays at one task until the outbox relay uses `FOR UPDATE SKIP
   LOCKED`** (D3).
6. **The Rekognition fan-out cap lives in `services`** (D19).
7. **Collections are environment-and-region namespaced**; `services` asserts
   `REKOGNITION_REGION` matches its region (D7).
8. **Backend task roles carry an explicit `Deny` on `rekognition:*`** (D8).
9. **The AI opt-out policy is in effect before any enrolment**, dev included (D16).
10. **Every migration is N-1 safe** (D11).
11. **Connection draining ≥ `ENROLMENT_POLL_WINDOW_MS`**, and the client tolerates
    a severed long-poll and re-polls — a client change (D6).

---

## 14. Pitfalls

Same checkable form as the P-numbers in `CLAUDE.md` §7. Status column reflects
this environment as built.

| | Rule | Status |
|---|---|---|
| **D1** | `PRESIGN_TTL_SECONDS > visibility × maxReceiveCount + 300`. `image-jobs` is 180 × 5 = 900, so **≥ 1800**. Asserted at boot. *Violated: the last retry fails on an expired URL and looks like a decode failure in the DLQ.* | live |
| **D2** | Task count × pool size across `api` **and** `worker` stays within their roles' combined limits, and the whole set within `max_connections` (~100 here). Computed in Terraform, not chosen. | live |
| **D3** | `worker` runs one task until the outbox relay uses `FOR UPDATE SKIP LOCKED`. | live |
| **D4** | No RDS Proxy on any path that `LISTEN`s — it pins on session state, so it gives no multiplexing on the one path you want it for. | ✅ absent |
| **D5** | `api` is never deployed to Lambda. A session-pinned `LISTEN` cannot survive it. | live |
| **D6** | Reverse-proxy read timeout > `ENROLMENT_POLL_WINDOW_MS` + 10s; production draining ≥ the poll window. | **set when Caddy proxies `api`** |
| **D7** | `services` refuses to boot when `REKOGNITION_REGION` differs from its deployment region. Face Liveness will not write reference frames to an out-of-region bucket. | live |
| **D8** | Backend task roles carry an explicit `Deny` on `rekognition:*`, asserted in CI over the rendered plan. | **not yet — roles don't exist** |
| **D9** | `image-worker` has no `s3:*` and no database secret; it works from presigned URLs only. | **not yet** |
| **D10** | `image-worker`'s security group reaches only the S3 prefix list and the SQS endpoint, and `sg-rds` does not accept it. | **not yet — §3.6** |
| **D11** | Every migration is N-1 safe. A rolling deploy on one instance runs two copies of a task, so dev does exercise this. | live |
| **D12** | Neither repo's migrations create anything in the other's schema. The backend never creates `svc`; stubs live only in docker compose and the test harness. | live |
| **D13** | `/readyz` fails when any of the four `svc` views is absent or wrong-shaped, and the check lives inside the contract module — `grep svc.v_person_` must find one module. | live |
| **D14** | Dev holds no production data; teardown runs a confirmed `DeleteFaces` and asserts a zero face count. | live |
| **D15** | Only `services` holds Rekognition credentials. No backend deployable, no side-car Lambda. | live |
| **D16** | The AI opt-out policy is in effect before any face is enrolled. | **not yet — §7.3** |
| **D17** | User-content buckets are unversioned. | ✅ |
| **D18** | `statement_timeout` and `connection_limit` are per role, never per cluster, and `api` and `worker` have different roles. | ✅ |
| **D19** | `ATTRIBUTION_MAX_INFLIGHT` is enforced inside `services`, where the Rekognition credential is — not on `image-worker`, which makes no Rekognition calls. | live |
| **D20** | Container images are built for `linux/arm64`. *Violated: tasks fail with an exec-format error that reads like a broken entrypoint.* | ✅ proven |
| **D21** | The host has an Elastic IP, so the overnight shutdown does not break DNS and every provider webhook URL. | ✅ |
| **D22** | The browser's Rekognition credential allows exactly `StartFaceLivenessSession` and nothing else. | live |
| **D23** | `LOG_LEVEL=debug` fails config validation in production. | live |

---

## 15. Not covered here

Four things this document deliberately does not address. Each crosses a boundary
and needs an owner.

1. **The staff-facing review tool.** `services` owns the review queue
   (`CLAUDE.md` §4) and `shared.audit_log` covers crop rendering
   (`ARCHITECTURE.md` §10), so the *data* side has an owner. What has none is the
   **UI reviewers use and how they reach it** — and "its own ingress" would breach
   hard rule 1, *we are the only public ingress*. Given §11 names review
   throughput as the product's binding growth constraint and reviewers are
   looking at NCII, this needs a design, and the ingress question needs answering
   rather than assuming.
2. **Cutover from the monolith.** `REPO-SPLIT.md` §5.11 rules out a dual-run,
   because every route's auth model changes and a shim would keep the IDOR alive
   behind a flag. A hard switch on a live userbase is a migration plan, not a
   deployment plan.
3. **Legacy DynamoDB → Postgres data migration.** Where existing subscribers,
   photos, infringements and `ScanningStatus` land.
4. **DR targets.** No RTO or RPO is stated, so "Multi-AZ plus PITR" is an
   implementation without a requirement.

### One open design question

Whether enrolment uses a **long-poll** (the current `ARCHITECTURE.md` §3
reading: *"we hold a session-pinned LISTEN"*) or the client polls a status
endpoint every couple of seconds. The long-poll is what makes D2, D4, D5 and D6
load-bearing; a client poll makes three of the four irrelevant. Enrolment is
once-per-person, so the volume argument for long-polling is weak. It is cheap to
change now and expensive later.

### One open infrastructure question

The browser's credential path to Rekognition (D22). Face Liveness streams video
from the client **directly to Rekognition** — the video never passes through the
backend. `ARCHITECTURE.md` §1's *"the client never talks to services"* still
holds, but the client **does** talk to Rekognition, and that path appears in no
design document. Probably a Cognito identity pool scoped to one action; whoever
writes the liveness integration should confirm the mechanism and write it down.
