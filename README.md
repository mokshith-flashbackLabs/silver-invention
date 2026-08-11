# ImageShield Services

Backend services for ImageShield, a likeness-monitoring product: it tells someone
when their face appears somewhere they did not consent to. This repo owns
liveness, enrolment, the Rekognition collection, third-party search-provider
integration, and the infringement lifecycle.

It sits **behind** the ImageShieldPhotoShare proxy — the client never calls us,
every request carries a service token and an opaque `user_ref`, and we hold
**no S3 credentials**.

Read [CLAUDE.md](CLAUDE.md) before touching code. It is the operating manual, and
§4 (invariants) is the part that matters most. System shape:
[ARCHITECTURE.md](ARCHITECTURE.md). Running a deployment:
[docs/OPERATIONS.md](docs/OPERATIONS.md).

## Stack

Python ≥3.11 · FastAPI + uvicorn · Postgres 16 (psycopg 3, raw SQL, no ORM) ·
pydantic 2 · structlog · SQS via a transactional outbox · Terraform. The scaffold
deliberately mirrors the Flashback agent service (`Project/AgentMeeMaw`).

## Clone to green tests

Everything below runs against local Postgres. No AWS account is needed for the
test suite — only for actually exercising liveness.

```bash
# 1. Local infrastructure: Postgres (:15433) + LocalStack SQS (:14566)
docker compose -f docker-compose.local.yml up -d

# 2. Python environment
python -m venv .venv
.venv\Scripts\activate            # Windows; source .venv/bin/activate elsewhere
pip install -e ".[dev]"

# 3. Configuration
cp .env.example .env.local        # then edit; real env vars always win

# 4. The four checks CI runs, all blocking
ruff check .
mypy                              # strict; NewType is worthless without it
REQUIRE_DB=1 pytest tests/        # REQUIRE_DB=1 turns "Postgres unreachable"
                                  # from a SKIP into a FAILURE — without it the
                                  # DB-backed half of the suite silently no-ops
pip install -e ".[dev]"           # the build itself

# 5. Run
python -m imageshield             # validates env, exits non-zero on bad config
```

`REQUIRE_DB=1` matters more than it looks: a bare `pytest` skips every
migration, schema-lint and store test if Postgres is down, and reports success.

Migrations: `python scripts/migrate.py up` / `down [--steps N | --all]`. They are
paired, reversible, checksummed, and run forward *and* backward in CI. Editing an
applied migration is a deploy-blocking error — write a new one.

## What is built

| | |
|---|---|
| Liveness sessions + enrolment | Rekognition Face Liveness, `IndexFaces`, `DeleteFaces` |
| Consent | We hold a *reference* (`enrolments.consent_ref`); the proxy owns the document |
| Discovery | Hive Web Search + Google Vision Web Detection, behind an adapter interface |
| Dedup | Normalised `url_hash`; one infringement, many attestations |
| Calibration + banding | Harness built; **no provider is calibrated yet** |
| Cost control | Per-provider budgets, circuit breakers, kill switches, adaptive cadence |
| Subject eligibility | Discovery never runs for a minor |
| Feedback + recheck | `not_me` / `confirmed` / `uncertain`; weekly `url_alive` sweep |
| Attribution | Face → enrolled person → seed |
| Infrastructure | Terraform, per-module DB roles, blocking CI gates |

Not built, and deliberately: the match module (blocked on the partner's
embedding model), the adjudication queue, the crop fetcher, evidence export,
digests, CSAM screening, and the scheduler that reads `next_scan_after`. See
CLAUDE.md §6 — most of `ARCHITECTURE.md` describes things that are specified,
not in scope.

## Processes

Four, all from one image:

```
uvicorn imageshield.http.app:create_app --factory   the API
python -m imageshield.relay                         outbox -> SQS
python -m imageshield.search.worker                 consumes search:runs
python -m imageshield.recheck.worker                weekly url_alive sweep
```

## Layout

```
src/imageshield/
  config.py          pydantic-settings; every knob from env, fatal on a bad key
  types.py           NewType identifiers (UserRef, SessionId, ProviderId, UrlHash)
  redaction.py       phone-shape log tripwire — we must never hold one
  aws_identity.py    logs the AWS account/region at boot, loudly
  schema_lint.py     INVARIANTS #9 — no bytea column anywhere
  liveness/          session lifecycle, Rekognition provider, presigned PUTs
  enrolment/         IndexFaces, the collection, the DeleteFaces path
  subjects/          discovery eligibility
  search/            seeds, runs, providers, dedup, cadence, banding
  providers/         the dispatch guard chain: budgets, breakers, kill switches
  calibration/       score calibration and banding config
  recheck/           the url_alive sweep (own process, own egress posture)
  attribution/       face -> enrolled person -> seed (the ONE face-search module)
  http/              FastAPI app, auth, routes
migrations/          paired .up.sql/.down.sql, checksummed ledger
infra/terraform/     queues, IAM, collection, secrets, alarms
scripts/migrate.py   migration runner
devtools/            spike harnesses — outside the numbered build steps
```

## Boundary rules (the short version)

1. Client → Proxy → Services. No CORS, no public ingress, no per-user auth.
2. We never see a phone number — `user_ref` (opaque UUID) only. Logs are
   tripwired, and so is the build.
3. No S3 credentials, no S3 client. Presigned URLs from the proxy only, and the
   IAM role grants no `s3:` action of any kind.
4. No user model. The proxy is the auth boundary.
5. Identity never comes from a similarity score. Face search is permitted in
   `attribution/` and nowhere else (INVARIANTS #1a).

Three of those are enforced by something other than discipline: the schema lint,
the log redaction processor, and the IAM policy. The rest are enforced by tests
in `tests/test_boundaries.py`, `tests/test_iam_policy.py` and
`tests/test_route_auth_coverage.py` — all permanent, none to be deleted.

## Status

**v1 complete** — all nine steps in CLAUDE.md §8, plus the out-of-band consent,
seed-URL, lifecycle and attribution tasks.

The largest open question is not a system: nobody has measured whether discovery
actually finds deepfakes. The step-7 harness exists and `eval_items` /
`eval_observations` are migrated; they need ~200 labelled images to produce a
number. Until then no provider is calibrated, and every result lands in the
`review` band by design.
