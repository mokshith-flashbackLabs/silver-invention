# Dev deploy: building against DEPLOY-DEV-HANDOFF.md

**Date:** 2026-08-17
**Status:** approved, pending implementation
**Scope:** this repo (`services`) only. The proxy repo does its own side.

The dev environment exists (`docs/DEPLOY-DEV.md`, as-built 13 Aug 2026). This
spec covers what `services` must change to deploy into it, and the deploy
itself.

---

## 1. What the handoff requires that this repo does not have

| Handoff requirement | Current state | § |
|---|---|---|
| `linux/arm64` image | `Dockerfile` unpinned — builds host arch | 4 |
| `PORT=8081` | `HTTP_PORT=8000` | 2, 4 |
| `REKOGNITION_REGION`, boot refusal on region mismatch (D7) | `AWS_REGION` only, no cross-check | 2 |
| `IDENTITY_COLLECTION=identity-dev-v1` | `REKOGNITION_COLLECTION_ID` | 2 |
| `DISCOVERED_COLLECTION=discovered-dev-v1` | absent | 2 |
| `ENROLMENT_COLLISION_THRESHOLD` | absent | 2 |
| `SEARCH_MATCH_THRESHOLD` ("not 80") | absent | 2 |
| `ATTRIBUTION_MAX_INFLIGHT=4` | absent | 2 |
| `SEARCH_PROVIDER=stub` | absent — providers always live | 2 |
| `DEV_FACE_CEILING=50` | absent | 2 |
| `LOG_LEVEL`, invalid as `debug` under production | not a config field | 2 |
| `/readyz` failing on missing/wrong-shaped `svc` view | no `/readyz` at all | 3 |
| migration as a one-off ECS task | `scripts/` copied, no entrypoint | 4 |
| task definitions, task role | nothing under `infra/ecs/` | 5 |

`ENROLMENT_QUALITY_FILTER=HIGH` needs no work: invariant #5 hardcodes
`QualityFilter: HIGH` deliberately, and it must not become tunable. The env var
is accepted and ignored, documented in §2.4.

---

## 2. Config

### 2.1 Renames

`rekognition_collection_id` → `identity_collection`. Six real call sites:
`http/app.py:107`, `http/routes/liveness.py:384,425,443`,
`http/routes/attribution.py:57`, plus `.env.example` and two `tests/conftest.py`
entries. A hard rename, no alias — the old name is not yet in any deployed
environment, so there is nothing to be compatible with, and an alias would be a
second name for one value, which is the drift §9 of CLAUDE.md warns about.

`aws_region` **stays**. SQS and STS both use it, and it is the deployment region.
`rekognition_region` is added alongside it, not as a replacement.

### 2.2 New fields

```python
rekognition_region: str            # required; validated as a region
identity_collection: str           # required, non-empty (renamed)
discovered_collection: str         # required, non-empty — NOT READ in v1
enrolment_collision_threshold: float   # 0-100 — NOT READ in v1
search_match_threshold: float      # 0-100, refused at exactly 80
attribution_max_inflight: int      # positive
search_provider: Literal["stub", "hive", "google"]
dev_face_ceiling: int              # positive
log_level: Literal["debug", "info", "warning", "error"] = "info"
```

Two of these are validated but unread, because the modules that would read them
are out of scope per CLAUDE.md §6:

- `discovered_collection` — `discovered-v1` and clustering are "specified, do not
  build yet".
- `enrolment_collision_threshold` — there is no collision check in the enrolment
  path, and adding one is invariant #1 territory (identity must never come from a
  similarity score). **This value must not acquire a reader without re-reading
  invariant #1 and #1a first.**

Each carries a comment saying exactly that. They are declared so the deployed env
block matches the handoff literally and the deploy side does not set a variable
that silently vanishes.

`search_match_threshold` is declared and read by the search dispatch path when a
non-stub provider is selected. It is distinct from `face_match_threshold`
(enrolment) and `attribution_match_threshold` (attribution) — invariant #1b, one
threshold per purpose.

### 2.3 New boot assertions

Each is a `model_validator(mode="after")` and each gets a test that boot refuses.

1. **`REKOGNITION_REGION` must equal `AWS_REGION`** (D7). A Rekognition
   collection is regional; a mismatch means enrolment writes to a collection in
   one region and search reads a different, empty one. Silent, and it looks like
   "no matches".

2. **`LOG_LEVEL=debug` is refused when `ENVIRONMENT=production`.** Debug logging
   in this service is the one that carries `user_ref`, bounding boxes and provider
   payloads.

3. **`ENVIRONMENT=development` requires `SEARCH_PROVIDER=stub`.** The dev Hive
   secret holds a *real* key — its own description reads "real key, no sandbox
   exists, keep SEARCH_PROVIDER=stub". There is no Hive sandbox, so any non-stub
   dev run bills real money against a contract-priced product whose
   `cost_per_call_usd` is NULL, meaning the step-8 budget guard fails closed and
   caps nothing. Config is the only place this can be stopped cheaply.

4. **`SEARCH_MATCH_THRESHOLD` must not be exactly 80.** The handoff says "pin it
   — not 80". 80 is Rekognition's `FaceMatchThreshold` default: the value you get
   when nobody chose one. Refusing it forces a deliberate number.

Existing assertions (`_tokens_distinct`, `_ages_ordered`,
`_attribution_max_candidates_floor`, `_breaker_cooldown_ordered`,
`_scan_thresholds_ordered`, `_cache_ttl_capped`) are untouched.

### 2.4 Deliberately not implemented

`ENROLMENT_QUALITY_FILTER` is accepted from the environment and ignored.
Invariant #5 requires `QualityFilter: HIGH` on every `IndexFaces`, permanently. A
poor enrolment vector degrades every match that user will ever get and they have
no way to know, so this is not a knob. Documented in the copied handoff.

### 2.5 Provider keys — no change

`hive_api_key` and `google_vision_api_key` stay unconditionally required.
Verified 2026-08-17: `imageshield/dev/hive` and `imageshield/dev/google-vision`
both exist in Secrets Manager (`ap-south-1`, account 225989356895). ECS resolves
them into env vars via the execution role before the process starts, so the task
launches and boot validates them.

`SEARCH_PROVIDER=stub` governs whether a key is *spent*, not whether it must be
*present*. That is the stronger arrangement: the key is validated at boot, and
stub is a dispatch decision rather than a hole in config.

---

## 3. `/readyz`

New route in `http/routes/health.py`, unauthenticated, alongside `/health`.

**`/health` and `/readyz` differ deliberately.** `/health` is always 200 with a
body the proxy reads (a degraded DB must not look like "service absent" to retry
logic). `/readyz` returns **503 when not ready** — it is the ECS/deploy gate, and
a deploy must not succeed into a broken contract.

Checks, in order:

1. DB reachable.
2. All four `svc` views exist.
3. Each view has the expected columns with the expected types.

The shape check queries `information_schema.columns` against a table of expected
`(view, column, data_type)` derived from migration 0016. This makes CLAUDE.md §3's
"the views are a versioned contract" mechanical: a dropped or retyped column
fails readiness here, in the repo that owns the views, instead of failing the
proxy at runtime after a deploy has already gone out.

The expected-shape table is the authority and lives beside the route. Columns may
be **added** freely (the check asserts expected ⊆ actual, not equality) — that
matches the contract rule that additions are safe and removals are not.

Response body names the specific failure (`missing_view`, `wrong_column_type`,
with the view and column) — this is an internal, private-subnet endpoint and a
readiness probe that says only "not ready" costs an hour of digging.

`grep svc.v_person_` must find only this module and the migration, per the
handoff's §10 checklist.

---

## 4. Dockerfile

- `--platform=linux/arm64` on both `FROM` lines. The host is Graviton
  (`t4g.medium`); an amd64 image fails with an exec-format error that reads like
  a broken entrypoint.
- `HTTP_PORT=8081`, `EXPOSE 8081`.
- Migration entrypoint: the same image runs the migration task with a different
  `command`, so migrations are provably from the same commit as the server. No
  migration on container start (handoff §7).

Build: `docker buildx build --platform linux/arm64`, tagged with the git SHA,
never `latest`.

---

## 5. Task definitions and IAM

New, under `infra/ecs/`:

- `imageshield-dev-services.json` — `networkMode: host`, memory 768, port 8081,
  `healthCheck` against `/health`, awslogs to `/imageshield/dev` with prefix
  `services`, DB password + both provider keys + both service tokens via
  `secrets`, everything else via `environment`.
- `imageshield-dev-migrate-services.json` — same image, migration command,
  `migrator_services` secret.
- `services-task-role-policy.json` — `rekognition:*` scoped to the two dev
  collection ARNs, `s3:GetObject` on the liveness bucket only, `kms:Decrypt` on
  the dev key.

**Test** (`tests/test_ecs_task_defs.py`), mirroring the existing
`tests/test_iam_policy.py` pattern of asserting against the rendered JSON:

- the `services` task role has **no `s3:PutObject`** and no S3 grant beyond
  `GetObject` on the liveness bucket
- no `rekognition:` action is unscoped (`Resource: "*"`)
- every secret arrives via `secrets`, none via `environment` — the handoff's §10
  check, as a test rather than a manual review step
- `HTTP_PORT`/`PORT` is 8081

The existing whole-`src/` no-S3 grep (invariant, step 9) is unaffected.

---

## 6. Deploy sequence

1. Create execution role `imageshield-dev-exec` + `read-dev-secrets` inline
   policy (handoff §3 — it does not exist yet).
2. Create the `services` task role with the §5 policy.
3. `docker buildx build --platform linux/arm64`, push to
   `225989356895.dkr.ecr.ap-south-1.amazonaws.com/imageshield/services:<sha>`.
4. Create Rekognition collections `identity-dev-v1` and `discovered-dev-v1` in
   `ap-south-1`. `discovered-dev-v1` is created **empty and unused** — the module
   that would write to it is out of scope (CLAUDE.md §6). It exists because the
   handoff's env block names it and nothing may write to a collection that does
   not exist. Creating it is not a decision to build clustering.
5. Register both task definitions.
6. Run the migration task. **`services` migrates first** — it creates `svc` and
   the four views, and the backend cannot pass readiness without them.
7. Create the `services` service with `minimumHealthyPercent=0,
   maximumPercent=100` (with fixed host ports the default 100 makes every deploy
   hang forever).
8. Verify `/readyz` returns 200 from the host.

**D16 gate:** no face is enrolled — no `IndexFaces` call — until the AWS AI
services opt-out policy returns `optOut` for Rekognition. Creating an empty
collection is not enrolment and is safe. Verifying the opt-out is a prerequisite
for testing enrolment, and is called out to the operator rather than assumed.

**Not in this spec:** the `api`, `worker`, `image-worker` and `caddy`
deployables, and the Caddyfile. Those are the proxy repo's, per CLAUDE.md §3 —
this repo does not edit proxy code.

---

## 7. Docs

- Copy the handoff to `docs/DEPLOY-DEV-HANDOFF.md` (it says to put it in both
  repos).
- Add a section recording what this repo deliberately does not implement:
  `ENROLMENT_QUALITY_FILTER` (invariant #5), and the two validated-but-unread
  vars from §2.2 — so the deploy side does not set a variable expecting effect.
- Update `.env.example` with every new key.
- Update `CLAUDE.md` §2 if the region assumption changes materially, and
  `docs/OPERATIONS.md` with the `/readyz` semantics.

---

## 8. Testing

TDD throughout — test first, watch it fail, then implement.

| Area | Tests |
|---|---|
| Config | one refusal test per §2.3 assertion; rename coverage; the unread fields are still validated |
| `/readyz` | all-present → 200; one view dropped → 503 naming it; one column retyped → 503 naming it; extra column → still 200; DB down → 503 |
| Task defs | §5 assertions |
| Docker | `docker manifest inspect` shows `arm64` |

Then all four CI gates: `ruff`, `mypy` (both scopes), `REQUIRE_DB=1 pytest`,
build. Per prior experience, `pytest` alone has hidden lint errors — all four,
every time.

---

## 9. Risks

- **The rename touches enrolment and attribution routes.** Both are covered by
  existing tests; the compiler and `mypy --strict` catch the rest.
- **`search_match_threshold` has no measured value.** It is required with no
  default, so boot refuses until someone picks one. The handoff says not to tune
  it from a dev measurement — dev is a burstable instance in a different region
  with a Face Liveness quota one fifth of production's.
- **Two config fields exist with no reader.** Mitigated by comments and §7 docs,
  but it is real: a future contributor may wire `enrolment_collision_threshold`
  to a collision check and walk into invariant #1. The comment says so
  explicitly.
- **`discovered-dev-v1` is created for an unbuilt module.** Empty collections
  cost nothing and `prevent_destroy` semantics matter later; the alternative is a
  second AWS trip when clustering lands.
