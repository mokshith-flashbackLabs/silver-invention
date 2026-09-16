# Production task definitions

Five files, one per deployable, mirroring `infra/ecs/imageshield-dev-*.json`. Each is the dev file
with only the environment-specific values changed, so `diff` against its dev counterpart shows
exactly what differs and nothing else.

| File | Family | Containers | Task role |
|---|---|---|---|
| `services.json` | `imageshield-prod-services` | `services` (8081) | `imageshield-prod-services` |
| `services-worker.json` | `imageshield-prod-services-worker` | `relay`, `search-worker` | `imageshield-prod-services` |
| `confirm.json` | `imageshield-prod-confirm` | `confirm-worker`, `score-tick` | `imageshield-prod-services` |
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

To render and register one file:

```sh
export AWS_DEFAULT_REGION=us-east-1
export IMAGE_TAG=<tag>
arn_for() { aws secretsmanager list-secrets --filters Key=name,Values="$1" \
              --query 'SecretList[0].ARN' --output text; }

export SECRET_DB_APP_SERVICES=$(arn_for imageshield/prod/db/app_services)
export SECRET_DB_MIGRATOR_SERVICES=$(arn_for imageshield/prod/db/migrator_services)
export SECRET_SERVICE_TOKEN_BACKEND=$(arn_for imageshield/prod/service-token/backend-to-services)
export SECRET_SERVICE_TOKEN_ADMIN=$(arn_for imageshield/prod/service-token/admin)
export SECRET_HIVE=$(arn_for imageshield/prod/hive)
export SECRET_GOOGLE_VISION=$(arn_for imageshield/prod/google-vision)

envsubst < infra/ecs/prod/services.json \
  | python -c 'import json,sys; d=json.load(sys.stdin); d.pop("x-notes",None); json.dump(d,sys.stdout)' \
  | aws ecs register-task-definition --cli-input-json file:///dev/stdin
```

Check for a surviving `${` in the rendered output before registering. `envsubst` replaces an *unset*
variable with the empty string, which registers cleanly and then fails at pull time.

## Secrets these files expect

ECS reads a **named key** out of each secret. Two of these were wrong in the account as of
2026-09-16 and cannot be fixed from this repo — Terraform deliberately never manages secret values.

| Secret | Keys read |
|---|---|
| `db/app_services`, `db/migrator_services` | `host`, `port`, `dbname`, `username`, `password` |
| `service-token/backend-to-services` | `token`, **`FETCHER_TOKEN`** — the second was missing |
| `service-token/admin` | `token` — held `secret_key` instead |
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
- `ATTRIBUTION_MATCH_THRESHOLD` and `ATTRIBUTION_MAX_CANDIDATES` are set explicitly although dev
  leaves them on their code defaults (92 and 5). `GET /v1/config/floors` publishes both to the
  backend, whose own production task definition declares 99 and 20. **Confirm with the backend team
  before the first deploy** — an unset default is a silent second opinion on a number two repos have
  to agree on. `MIN_ENROLMENT_AGE` is the same class of disagreement: 13 here, 0 in the backend's
  production `api.json`.

## Memory

Every long-running container shares one instance, because they talk to each other over localhost.
ECS places against `memoryReservation` where set, otherwise `memory`:

| Task | Placement |
|---|---|
| `services` | 576 |
| `services-worker` (relay 160 + search-worker 288) | 448 |
| `confirm` (confirm-worker 160 + score-tick 64) | 224 |
| `fetcher` | 128 |
| **services side total** | **1376** |

With the backend's `api` (512), `worker` (512) and `image-worker` (1024) that is 3424 MiB of the
3835 MiB a `t4g.medium` offers — 411 MiB free, enough for the 256 MiB migration task to run
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
