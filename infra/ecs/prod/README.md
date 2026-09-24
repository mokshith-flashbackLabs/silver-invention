# Production task definitions

Five files, one per deployable, mirroring `infra/ecs/imageshield-dev-*.json`. Each is the dev file
with only the environment-specific values changed, so `diff` against its dev counterpart shows
exactly what differs and nothing else.

| File | Family | Containers | Task role |
|---|---|---|---|
| `services.json` | `imageshield-prod-services` | `services` (8081) | `imageshield-prod-services` |
| `services-worker.json` | `imageshield-prod-services-worker` | `relay`, `search-worker` | `imageshield-prod-services` |
| `confirm.json` | `imageshield-prod-confirm` | `confirm-worker` | `imageshield-prod-services` |
| `fetcher.json` | `imageshield-prod-fetcher` | `fetcher` (8083) | `imageshield-prod-no-aws` |
| `migrate-services.json` | `imageshield-prod-migrate-services` | `migrate-services` | `imageshield-prod-no-aws` |

The infrastructure these reference — both Rekognition collections, all three queues with their DLQs,
every role, every secret container, the cluster, the log group and the registry — is created by the
backend repo's Terraform (`deploy/terraform/services.tf` there), not by this repo. `infra/terraform/`
here is superseded; see the note in that folder before running anything in it.

## Placeholders

Secret ARNs carry a random six-character suffix that AWS assigns, so they are resolved at render
time rather than written down: a stale literal fails as a generic execution-role error ("unable to
pull secrets or registry auth") that names neither the secret nor the key.

| Placeholder | Resolves to |
|---|---|
| `${IMAGE_TAG}` | the image tag being deployed |
| `${SECRET_DB_APP_SERVICES}` | ARN of `imageshield/prod/db/app_services` |
| `${SECRET_DB_MIGRATOR_SERVICES}` | ARN of `imageshield/prod/db/migrator_services` |
| `${SECRET_SERVICE_TOKEN_BACKEND}` | ARN of `imageshield/prod/service-token/backend-to-services` |
| `${SECRET_SERVICE_TOKEN_ADMIN}` | ARN of `imageshield/prod/service-token/admin` |
| `${SECRET_HIVE}` | ARN of `imageshield/prod/hive` |
| `${SECRET_GOOGLE_VISION}` | ARN of `imageshield/prod/google-vision` |

`render.sh` resolves them and registers the result:

```sh
infra/ecs/prod/render.sh services da897d6            # look up, render, register
infra/ecs/prod/render.sh confirm da897d6 --dry-run   # print the rendered JSON, register nothing
```

It looks up only the secrets the chosen family references, refuses a file still
carrying dev identifiers, refuses an unset or empty placeholder by name, refuses a
`valueFrom` that is not a real ARN, and strips `x-notes` (which
`register-task-definition` would reject) using `jq` or `python`, whichever is present.
Every refusal names the thing that is wrong, because the error ECS gives instead is a
generic execution-role message naming neither the secret nor the key.

## Secrets these files expect

ECS reads a **named key** out of each secret. Two were wrong until 2026-09-16 and could not be fixed
from this repo — Terraform deliberately never manages secret values — and the backend team has since
filled both. All six secrets resolve; `render.sh --dry-run` is how you confirm that.

| Secret | Keys read |
|---|---|
| `db/app_services`, `db/migrator_services` | `host`, `port`, `dbname`, `username`, `password` |
| `service-token/backend-to-services` | `token`, `FETCHER_TOKEN` — the second was missing until 2026-09-16 |
| `service-token/admin` | `token` — held `secret_key` until 2026-09-16 |
| `hive`, `google-vision` | `api_key` |

`SERVICE_TOKEN` and `ADMIN_SERVICE_TOKEN` come from two different secrets and **must not hold the
same value** — `config.py` refuses to boot when they match.

## Values that are not a free choice

- `ENVIRONMENT=production` with `SEARCH_PROVIDER=hive`. `config.py` refuses `production` + `stub`
  (the stub searches nothing, so every report would read "no matches in monitored sources" with
  nothing failing anywhere) and equally refuses `development` + a live provider.
- `LOG_LEVEL=info`. `config.py` refuses `production` + `debug`.
- `DEV_FACE_CEILING` no longer exists, in either environment. It was a required config field that
  nothing read, so it enforced no ceiling while forcing every container to carry a number. The
  scheduled face-count tripwire `docs/DEPLOY-DEV.md` describes was never built; a real one belongs
  in dev tooling rather than the boot-validated set.
- `ATTRIBUTION_MATCH_THRESHOLD` and `ATTRIBUTION_MAX_CANDIDATES` are **not set**, here or in dev, so
  both environments run the code defaults (92 and 5) that dev has been tested at. The backend
  declares 99 and 20 in its own task definitions, and `GET /v1/config/floors` publishes ours to it,
  so the two repos currently state different numbers. That is worth raising with them, but it is
  settled by measuring this system, not by copying a value across the boundary.

## Memory

Every long-running container shares one instance, because they talk to each other over localhost.
ECS places against `memoryReservation` where set, otherwise `memory`:

| Task | Placement |
|---|---|
| `services` | 576 |
| `services-worker` (relay 160 + search-worker 288) | 448 |
| `confirm` (confirm-worker 160) | 160 |
| `fetcher` | 128 |
| **services side total** | **1312** |

With the backend's `api` (512), `worker` (512) and `image-worker` (1024) that is 3360 MiB of the
3835 MiB a `t4g.medium` offers — 475 MiB free, enough for the 256 MiB migration task to run
alongside everything else, but not much more. If any of these grow, redo this arithmetic.

## Order

Services migrates first: the backend reads nine `svc` views that live in this schema and fails its
readiness check without them.

```
build and push both images
  -> imageshield-prod-migrate-services
  -> the backend's migration
  -> start all eight services
```

`build-push.sh` builds and pushes this repo's image; `render.sh` registers one task definition
against a tag:

```sh
infra/ecs/prod/build-push.sh --dry-run    # is the tag free, is the tree clean
infra/ecs/prod/build-push.sh              # build linux/arm64, push, verify in ECR
for f in migrate-services services services-worker confirm fetcher; do
  infra/ecs/prod/render.sh "$f" <tag>
done
```

Read `DEPLOY-RUNBOOK.md` §12.10 before doing this by hand. This repository is **IMMUTABLE**, unlike
dev's, and `docker buildx` exits 1 on a push that fully succeeded — the exit code is not the signal,
`aws ecr describe-images` is. `build-push.sh` exists so that is settled once rather than rediscovered.
