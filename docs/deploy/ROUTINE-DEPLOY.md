# ROUTINE-DEPLOY.md — shipping a commit to production

**Scope: a redeploy of an estate that already exists.** The cluster, the services, the roles, the
secrets, the queues and the collections are all standing; you have a new commit and you want it
running. That is this document.

It is **not** the bring-up. If you are creating a service, a role, a secret or a database grant for
the first time, read `DEPLOY-RUNBOOK.md` — it is long because it records what went wrong, and the
two failures that cost real time (§6 grants, §8 migrations) are both invisible from here.

Everything below is production, `us-east-1`. Dev is `ap-south-1` and differs in ways that matter:
its ECR repository is MUTABLE, so a tag can be overwritten there and cannot be here, and the dev
task definitions in `infra/ecs/` carry literal secret ARNs and are registered directly rather than
rendered. Do not carry a dev habit across.

---

## 0. Before you start

```sh
cd /path/to/silver-invention
export AWS_DEFAULT_REGION=us-east-1
export AWS_PAGER=""
export MSYS_NO_PATHCONV=1     # Git Bash rewrites anything path-shaped, log group names included
CLUSTER=imageshield-prod
TAG=$(git rev-parse --short HEAD)
```

Three preconditions, and the scripts enforce the first two so you do not have to remember them:

- **The tree is clean and the commit is pushed.** A tag built from a dirty tree names a commit whose
  contents were never what shipped, and because the repository is immutable that lie is permanent.
- **The tag is free.** `build-push.sh` checks before spending forty minutes on a build.
- **Your AWS identity is the one you think it is** — `aws sts get-caller-identity`. Both environments
  live in account `225989356895`; the region is the only thing separating them.

---

## 1. Build and push the image

```sh
infra/ecs/prod/build-push.sh --dry-run    # is the tag free, is the tree clean
infra/ecs/prod/build-push.sh              # build linux/arm64, push, verify in ECR
```

**Cold, this is roughly forty minutes** — the cluster is Graviton, the build is `linux/arm64` under
emulation, and the builder stage installs `psycopg[binary]` and Pillow wheels under QEMU. Do not run
the test suite alongside a cold build; it starves (`DEPLOY-RUNBOOK.md` §12.9).

**Warm, it is seconds.** The expensive layer is the venv, and it is invalidated only by
`pyproject.toml` or `src/`. A commit touching just `migrations/` or `scripts/` re-runs the last two
COPYs and exports — about twenty seconds end to end. Do not read a fast build as a build that
skipped something: the check that matters is `VERIFIED IN REGISTRY`, and the digest it prints is one
you can compare against `aws ecr describe-images`.

Wait for `VERIFIED IN REGISTRY`. **Ignore the buildx exit code the script prints above it.** Against
an immutable repository buildx exits 1 on a push that fully succeeded — it writes the tag, then
re-PUTs the same manifest, which immutability rejects. The script reports that number and then asks
the registry instead; the registry is the verdict. Following the exit code leads to deleting a tag
that was fine, which turns a non-problem into an outage (§12.10).

---

## 2. Register the task definitions

```sh
for f in migrate-services services services-worker confirm fetcher; do
  infra/ecs/prod/render.sh "$f" $TAG
done
```

Each prints a new task-definition ARN. `render.sh` resolves every secret ARN against the account at
render time — the six-character suffix AWS appends cannot be derived from the name, and a stale one
fails as a generic "unable to pull secrets or registry auth" that names neither the secret nor the
key. It also refuses a file still carrying dev identifiers, an unset placeholder, and a `valueFrom`
that is not a real ARN.

`--dry-run` as a third argument prints the rendered JSON and registers nothing. Use it if you want
to see what changed before committing to it.

---

## 3. Run the migration

**Always, even when you are sure the commit contains no migration.** It is idempotent — the runner
applies what is pending and prints nothing otherwise — and the alternative is finding out from a
crash-looping container which grant or column was missing.

```sh
TASK=$(aws ecs run-task --cluster $CLUSTER --launch-type EC2 \
  --task-definition imageshield-prod-migrate-services \
  --query 'tasks[0].taskArn' --output text)
aws ecs wait tasks-stopped --cluster $CLUSTER --tasks "$TASK"
aws ecs describe-tasks --cluster $CLUSTER --tasks "$TASK" \
  --query 'tasks[0].{Exit:containers[0].exitCode,Stopped:stoppedReason}'
```

`Exit: 0` is required. The task runs `python scripts/migrate.py up` as `migrator_services` and
prints one `applied NNNN_…` line per migration.

**On any other exit code, read the logs before changing anything.** Both production migration
failures so far were database grants, not code: the migrator lacked `CREATE ON SCHEMA public`
(`InvalidSchemaName: no schema has been selected to create in`) and then `CREATEROLE`
(`permission denied to create role`). Both are fixed by `grant-public-schema.sh prod`, which runs on
the host as the RDS master, is idempotent, and is described in `DEPLOY-RUNBOOK.md` §6a.

Never run migrations on container start, and never from a laptop.

---

## 4. Roll the services

```sh
for s in services services-worker confirm fetcher; do
  aws ecs update-service --cluster $CLUSTER --service $s \
    --task-definition imageshield-prod-$s --force-new-deployment >/dev/null
done
aws ecs wait services-stable --cluster $CLUSTER \
  --services services services-worker confirm fetcher
```

Omitting the revision makes ECS take the newest ACTIVE one, which is what step 2 just registered.

| Service | Family | Containers |
|---|---|---|
| `services` | `imageshield-prod-services` | `services` (8081) |
| `services-worker` | `imageshield-prod-services-worker` | `relay`, `search-worker` |
| `confirm` | `imageshield-prod-confirm` | `confirm-worker`, `score-tick` |
| `fetcher` | `imageshield-prod-fetcher` | `fetcher` (8083) |

**`api`, `worker` and `image-worker` in the same cluster belong to the backend repo. Do not touch
them from here.**

Two things about this cluster that will otherwise waste an afternoon:

- Every long-running container shares one `t4g.medium`, because they talk to each other over
  localhost. `infra/ecs/prod/README.md` has the memory arithmetic; about 400 MiB is spare, which is
  enough for the 256 MiB migration task and not much else.
- The services carry `minimumHealthyPercent=0`. With one instance and fixed host ports ECS cannot
  start a replacement while the old task holds 8081, so the default of 100 makes a deploy hang
  forever. If you ever recreate a service, carry that setting across.

---

## 5. Verify

Every container in this repo is `essential: true` and several share a task, so **one container
failing presents as a different container being down.** A relay that cannot reach its table takes
`search-worker` — which is perfectly healthy — down with it about 200ms after it starts, and the
symptom reads as "search is not running". Check the logs, not the service's running count: a
crash-looping task reports `runningCount` equal to `desiredCount` for most of each cycle.

```sh
# Readiness: 200 with an empty `problems` array. 503 means the svc contract is broken,
# which is a deploy gate — the proxy reads nine views out of that schema.
curl -s localhost:8081/readyz       # from the host, via SSM

# Did a container start and then say nothing? That is the shape of a crash loop.
aws logs describe-log-streams --log-group-name /imageshield/prod \
  --log-stream-name-prefix relay --max-items 50 \
  --query "logStreams[].{s:logStreamName,t:lastEventTimestamp}" --output text

aws logs get-log-events --log-group-name /imageshield/prod \
  --log-stream-name "<newest stream>" --limit 20 \
  --query "events[].message" --output text
```

A healthy relay logs past `relay.started`. A log stream whose last and only line is that banner is a
container that died on its first poll.

Service-level churn is visible without reading any logs at all — repeated `has started N tasks`
events a few minutes apart, with no `reached a steady state` holding:

```sh
aws ecs describe-services --cluster $CLUSTER --services services-worker \
  --query "services[0].events[0:6].[createdAt,message]" --output text
```

---

## 6. Rollback

**The image rolls back cleanly. The migration does not.** Task definitions are versioned, so
reverting the code is one call per service:

```sh
aws ecs update-service --cluster $CLUSTER --service services-worker \
  --task-definition imageshield-prod-services-worker:2   # the previous revision
```

Reverting a migration is a deliberate act with a container override, and you should be certain the
down file is right before running it — several in this repo drop roles, which are cluster-global:

```sh
aws ecs run-task --cluster $CLUSTER --launch-type EC2 \
  --task-definition imageshield-prod-migrate-services \
  --overrides file://rollback-override.json
```

with `rollback-override.json`:

```json
{ "containerOverrides": [ { "name": "migrate-services",
  "command": ["python", "scripts/migrate.py", "down", "--steps", "1", "--dry-run"] } ] }
```

Run it with `--dry-run` first, every time. Drop the flag only once the printed list is exactly what
you meant.

In practice the safer instinct is to roll forward: the old image against a newer schema is usually
fine, because migrations here add rather than retype.

---

## 7. The shortest path, when you know what changed

The full sequence above is the default because it is uniform and hard to get half-right. When the
commit is a single migration and no application code moved, steps 2 and 4 for the other four
families buy you tag consistency and nothing else:

```sh
infra/ecs/prod/build-push.sh
infra/ecs/prod/render.sh migrate-services $TAG
# run the migration (step 3)
aws ecs update-service --cluster $CLUSTER --service services-worker \
  --task-definition imageshield-prod-services-worker --force-new-deployment
```

Leaving the other services on an older tag is legitimate, but it means `describe-services` no longer
answers "what is deployed" with one tag. Write down what you did.

---

## Where the answers are

| Question | Document |
|---|---|
| A build, push or ECS error I have not seen before | `DEPLOY-RUNBOOK.md` §12 — ten failures, each with its cause |
| Which secret holds which key, what the placeholders resolve to, memory budget | `infra/ecs/prod/README.md` |
| A database grant is missing | `DEPLOY-RUNBOOK.md` §6, then `grant-public-schema.sh` |
| What a quarantined hit or an alarm means operationally | `docs/OPERATIONS.md` |
| Why an environment value is not a free choice | `infra/ecs/prod/README.md`, "Values that are not a free choice" |
