# ImageShield Services

Backend services for ImageShield, a likeness-monitoring product. This repo owns
liveness, enrolment, the Rekognition collection, and third-party search-provider
integration. It sits **behind** the ImageShieldPhotoShare proxy — the client
never calls us, every request carries a service token and an opaque `user_ref`,
and we hold **no S3 credentials**.

Read [CLAUDE.md](CLAUDE.md) before touching code. System shape:
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (most of it is specified, not in scope).

## Stack

Python ≥3.11 · FastAPI + uvicorn · Postgres 16 (psycopg 3, raw SQL, no ORM) ·
pydantic 2 · structlog · SQS via transactional outbox. The scaffold deliberately
mirrors the Flashback agent service (`Project/AgentMeeMaw`).

## Quickstart

```bash
# 1. Local infrastructure: Postgres (:15433) + LocalStack SQS (:14566)
docker compose -f docker-compose.local.yml up -d

# 2. Python environment
python -m venv .venv
.venv\Scripts\activate          # Windows; source .venv/bin/activate elsewhere
pip install -e ".[dev]"

# 3. Configuration
copy .env.example .env.local    # then edit; existing env vars always win

# 4. Checks (all blocking in CI)
pytest                          # unit + boot-contract + mypy gates
mypy                            # strict; config in pyproject.toml
ruff check .

# 5. Run
python -m imageshield           # validates env, exits non-zero on bad config
```

Migrations: `python scripts/migrate.py up` / `down` (none exist yet — Phase 2).

## Layout

```
src/imageshield/
  config.py        pydantic-settings; every knob from env, fatal on bad key
  types.py         NewType identifiers (UserRef, SessionId, ProviderId, UrlHash)
  redaction.py     phone-shape log tripwire — we must never hold one
  db/connection.py psycopg async pool factory
  http/
    app.py         FastAPI factory (uvicorn --factory entrypoint)
    auth.py        X-Service-Token / X-Admin-Service-Token dependencies
    logging.py     structlog + request-id middleware + redaction processor
    routes/        health (unauthenticated), v1 + admin placeholders
migrations/        paired .up.sql/.down.sql, checksummed ledger
scripts/migrate.py migration runner (up/down)
```

## Boundary rules (the short version)

1. Client → Proxy → Services. No CORS, no public ingress, no per-user auth.
2. We never see a phone number — `user_ref` (opaque UUID) only. Logs are
   tripwired: phone-shaped values are redacted.
3. No S3 credentials, no S3 client. Presigned URLs from the proxy only.
4. No user model. The proxy is the auth boundary.

Build phases and "done when" criteria live in the build spec; current status:
**Phase 1 (foundations) complete** — no feature code yet.
