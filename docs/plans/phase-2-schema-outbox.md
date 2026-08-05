# Phase 2 — Schema and outbox (implementation plan)

Source spec: the Phase 2 prompt (schema + outbox) for the ImageShield services repo.
Phase 1 (scaffold, config, auth middleware, migration runner) is complete on `main`. This plan
builds: the initial migration (0001), the schema lint test, the transactional outbox helper, and
the outbox relay process. When this plan is done, STOP — do not begin Phase 3.

## Global Constraints

These bind every task. Copy into every reviewer dispatch.

- **`user_ref`, never `user_id`.** No users table, no FK to the proxy, no phone number anywhere —
  not in a column, not in a log line.
- **No image bytes persisted anywhere** (INVARIANTS #9): no `bytea` column; column names must not
  match `/_(data|blob|bytes|b64)$|thumbnail|local_path/`; `*_uri`/`*_url` (and plural `_uris`/`_urls`)
  columns are explicitly allowed — they are pointers into the proxy's S3.
- **No S3 client, no AWS S3 credentials** in this repo. boto3 is used for SQS only, and only from
  the relay/SQS module — never from request handlers.
- **Apply the Task 1 DDL verbatim** — do not rename columns/tables or add columns beyond the
  role/grant statements Task 1 specifies. These shapes must grow into SCHEMA.md (v2) untouched.
- All timestamps `TIMESTAMPTZ`; all PKs `UUID DEFAULT gen_random_uuid()` (outbox/audit are
  BIGSERIAL by design, per the DDL).
- Migrations are paired `NNNN_name.up.sql` / `NNNN_name.down.sql`, applied via
  `python scripts/migrate.py up|down` (checksummed ledger; editing an applied migration is a
  deploy-blocking error). Both directions must run clean.
- Messages carry IDs, never payloads. Workers re-read from Postgres; the stored row wins.
- Every SQS consumer/producer path is idempotent-tolerant: SQS is at-least-once and the outbox
  makes duplicates normal.
- `ruff check` and `mypy` (strict, already configured in pyproject.toml) must pass.
- Tests live under `tests/`, pytest with `asyncio_mode = "auto"`.
- DB-backed tests connect to `TEST_DATABASE_URL` (default
  `postgresql://imageshield:imageshield@localhost:15433/postgres`, the docker-compose server) and
  create/drop a throwaway database per session. If the server is unreachable, tests **skip with a
  loud reason** unless `REQUIRE_DB=1` is set (CI sets it, making them blocking there).
- Commit discipline: commit only files you created or modified for your task. Never `git add -A`
  or `git add .` — the repo carries unrelated untracked docs at the root.
- Cite `file:line` when describing existing behaviour; mark anything not read directly as INFERRED.

## Task 1 — Migration 0001: full DDL, audit grants, DB test harness

**Deliverables**

1. `migrations/0001_initial_schema.up.sql` containing this DDL **verbatim** (plus the role/grant
   block below it):

```sql
-- ── Liveness and enrolment ────────────────────────────────────────────────

CREATE TYPE liveness_status AS ENUM (
  'created','pending','passed','failed','expired','consumed'
);

CREATE TABLE liveness_sessions (
  session_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_ref             UUID NOT NULL,
  provider             TEXT NOT NULL DEFAULT 'rekognition',
  provider_session_id  TEXT UNIQUE NOT NULL,
  status               liveness_status NOT NULL DEFAULT 'created',
  confidence           NUMERIC(5,2),
  failure_reason       TEXT,
  attempt_number       INT NOT NULL DEFAULT 1,
  reference_image_uri  TEXT,
  audit_image_uris     TEXT[],
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at         TIMESTAMPTZ,
  expires_at           TIMESTAMPTZ NOT NULL,
  consumed_at          TIMESTAMPTZ
);

CREATE INDEX liveness_user_idx ON liveness_sessions (user_ref, created_at DESC);
CREATE INDEX liveness_attempts_idx ON liveness_sessions (user_ref, created_at)
  WHERE status IN ('failed','expired');

CREATE TABLE enrolments (
  enrolment_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id        UUID NOT NULL UNIQUE REFERENCES liveness_sessions(session_id),
  user_ref          UUID NOT NULL,
  collection_id     TEXT NOT NULL,
  external_face_id  TEXT NOT NULL,
  quality_score     NUMERIC(5,2),
  model_id          TEXT NOT NULL,
  source_object_uri TEXT NOT NULL,
  status            TEXT NOT NULL DEFAULT 'active',
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at        TIMESTAMPTZ
);

CREATE UNIQUE INDEX enrolments_face_uniq
  ON enrolments (collection_id, external_face_id);
CREATE INDEX enrolments_user_active_idx
  ON enrolments (user_ref) WHERE status = 'active';

-- ── Providers and classification ──────────────────────────────────────────

CREATE TYPE provider_kind AS ENUM (
  'image_search',   -- finds THIS IMAGE and near-variants (Hive media search, TinEye)
  'face_search',    -- finds a PERSON in novel images (PimEyes-class) — none integrated yet
  'classifier'      -- judges a given image (Hive deepfake detection) — later
);

CREATE TABLE providers (
  provider_id     TEXT PRIMARY KEY,
  kind            provider_kind NOT NULL,
  enabled         BOOLEAN NOT NULL DEFAULT false,
  calibrated      BOOLEAN NOT NULL DEFAULT false,
  score_version   TEXT NOT NULL,
  daily_budget_usd NUMERIC(10,2),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- What we search WITH. The seed determines what can be found.
CREATE TABLE search_seeds (
  seed_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_ref          UUID NOT NULL,
  seed_kind         TEXT NOT NULL,   -- enrolment|user_supplied|public_profile
  source_object_uri TEXT NOT NULL,   -- proxy's S3; presigned at search time
  status            TEXT NOT NULL DEFAULT 'active',
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX seeds_user_idx ON search_seeds (user_ref) WHERE status = 'active';

CREATE TABLE search_runs (
  run_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  seed_id         UUID NOT NULL REFERENCES search_seeds(seed_id),
  user_ref        UUID NOT NULL,
  providers_attempted TEXT[] NOT NULL,
  providers_succeeded TEXT[] NOT NULL DEFAULT '{}',
  threshold_config JSONB NOT NULL,
  matches_found   INT NOT NULL DEFAULT 0,
  started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at    TIMESTAMPTZ
);

CREATE TABLE content_urls (
  url_hash        TEXT PRIMARY KEY,   -- sha256 of the NORMALISED url
  url             TEXT NOT NULL,
  source_domain   TEXT NOT NULL,
  first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX content_domain_idx ON content_urls (source_domain);

CREATE TABLE provider_calls (
  call_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id        UUID REFERENCES search_runs(run_id),
  provider_id   TEXT NOT NULL REFERENCES providers(provider_id),
  status        TEXT NOT NULL,   -- ok|error|rate_limited|timeout|budget_exceeded
  http_status   INT,
  latency_ms    INT,
  cost_usd      NUMERIC(10,4),
  attempt       INT NOT NULL DEFAULT 1,
  error_detail  TEXT,
  raw_response  JSONB NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX provider_calls_cost_idx ON provider_calls (provider_id, created_at);

-- One row per (page found, provider that found it). Same page from two
-- providers is two attestations of one infringement, not two infringements.
CREATE TABLE search_matches (
  match_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id          UUID NOT NULL REFERENCES search_runs(run_id),
  url_hash        TEXT NOT NULL REFERENCES content_urls(url_hash),
  user_ref        UUID NOT NULL,
  provider_id     TEXT NOT NULL REFERENCES providers(provider_id),
  image_url       TEXT NOT NULL,   -- the matched image itself
  page_url        TEXT,            -- host page, from backlinks[0] when present
  provider_score  NUMERIC(6,4) NOT NULL,  -- RAW, provider-native. NEVER rescaled
  score_version   TEXT NOT NULL,
  band            TEXT NOT NULL,   -- auto_confirm|review|drop
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX search_matches_uniq
  ON search_matches (run_id, url_hash, provider_id);
CREATE INDEX search_matches_user_idx ON search_matches (user_ref, created_at DESC);
CREATE INDEX search_matches_review_idx ON search_matches (band, created_at)
  WHERE band = 'review';

-- ── Outbox ────────────────────────────────────────────────────────────────

CREATE TABLE outbox (
  outbox_id     BIGSERIAL PRIMARY KEY,
  queue_name    TEXT NOT NULL,
  payload       JSONB NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  published_at  TIMESTAMPTZ,
  attempts      INT NOT NULL DEFAULT 0,
  last_error    TEXT
);

CREATE INDEX outbox_unpublished_idx ON outbox (created_at)
  WHERE published_at IS NULL;

-- ── Audit ─────────────────────────────────────────────────────────────────

CREATE TABLE audit_log (
  audit_id      BIGSERIAL PRIMARY KEY,
  actor_type    TEXT NOT NULL,
  action        TEXT NOT NULL,
  subject_ref   UUID,
  resource_id   UUID,
  metadata      JSONB,
  occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

2. Append to the same up migration an **application-role block**: create role `imageshield_app`
   (`NOLOGIN`), guarded so re-creation is not an error (`DO $$ ... IF NOT EXISTS`-style guard —
   roles are cluster-global). Grant it `INSERT` on `audit_log` and `USAGE` on
   `audit_log_audit_id_seq`. **No UPDATE, no DELETE, no TRUNCATE** on `audit_log`. Grant nothing
   else in this migration.

3. `migrations/0001_initial_schema.down.sql`: reverse everything — revoke the grants, drop the
   role (`DROP ROLE IF EXISTS imageshield_app`), drop all tables and types in dependency order.
   After `down`, the database must contain no Phase 2 objects (only `schema_migrations`, which the
   runner owns).

4. DB test harness in `tests/` (extend `tests/conftest.py` or add `tests/db.py`):
   - Session-scoped fixture that connects to `TEST_DATABASE_URL` (default
     `postgresql://imageshield:imageshield@localhost:15433/postgres`), creates a uniquely named
     throwaway database, and drops it on teardown.
   - Skip DB tests with a clear message when the server is unreachable, but **fail** instead of
     skipping when env `REQUIRE_DB=1`.
   - Helper to run `python scripts/migrate.py up` / `down` against the throwaway DB via
     subprocess with `DATABASE_URL` set (subprocess, so the real CLI is what's tested).

5. Tests in `tests/test_migrations.py`:
   - `up` from empty applies 0001 cleanly.
   - `down --all` after `up` leaves no tables (besides `schema_migrations`), no custom types, and
     no `imageshield_app` grants.
   - `up` → `down --all` → `up` runs clean (reversibility round-trip).
   - Under `SET ROLE imageshield_app`: `INSERT INTO audit_log` succeeds; `UPDATE audit_log` and
     `DELETE FROM audit_log` both fail with insufficient privilege.

**Notes**

- `scripts/migrate.py` strips bare `BEGIN/COMMIT` lines and runs each file inside one
  transaction; do not add explicit transaction markers.
- The DDL block above is verbatim from the Phase 2 spec — do not editorialise it. The role/grant
  block is the only addition.
- Local Postgres: `docker compose -f docker-compose.local.yml up -d` exposes 15433. It is already
  running on this machine.

**Verification:** `python -m pytest tests/test_migrations.py -v` green against the local compose
Postgres; `ruff check`; `mypy` clean.

## Task 2 — Schema lint test (INVARIANTS #9) + CI workflow

**Deliverables**

1. `src/imageshield/schema_lint.py` — a pure function
   `lint_columns(rows: Iterable[ColumnInfo]) -> list[Violation]` (shapes up to you, keep them
   typed) implementing, in priority order:
   - **(a) Type gate — the real rule:** any column with `data_type = 'bytea'` (including array
     `udt_name = '_bytea'`) is a violation, whatever its name.
   - **(b) Name gate:** reject any column name matching the regex
     `_(data|blob|bytes|b64)$` (suffix) or containing `thumbnail` or `local_path`.
   - **(c) Allowlist:** names ending `_uri`, `_url`, `_uris`, `_urls` are explicitly allowed and
     must pass (they are pointers into the proxy's S3) — **unless** they also trip (a) or (b)
     (a `thumbnail_uri` column still fails; per CLAUDE.md §8 step 2 that is the fixture that
     proves the gate works). Never match on the substring `image` — `reference_image_uri` must
     pass.
2. `tests/test_schema_lint.py`:
   - Blocking check: run the lint over `information_schema.columns` of the **migrated throwaway
     DB** (Task 1 harness, all non-system schemas) and assert zero violations. This is the test
     that makes `thumbnail_blob` on `search_matches` fail the build.
   - Fixture cases, each exercised through the real DB (create table in the throwaway DB, lint,
     drop) **and all three must exist**:
     - `photo bytea` → FAILS (violation reported)
     - `thumbnail_b64 TEXT` → FAILS
     - `reference_image_uri TEXT` → PASSES
   - Unit cases direct against `lint_columns` for: `source_object_uri` passes, `audit_image_uris`
     (array of text) passes, `thumbnail_uri` fails, `image_data` fails, `raw_b64` fails.
3. `.github/workflows/ci.yml` — none exists yet; the lint "must run in CI as a blocking check",
   so create the minimal workflow: on push/PR — ruff, mypy, then pytest with a `postgres:16`
   service container, env `REQUIRE_DB=1` and `TEST_DATABASE_URL` pointing at the service. No
   LocalStack in CI — SQS-touching tests use an injected stub client (Task 4).

**Verification:** all of `tests/test_schema_lint.py` green locally; demonstrate the gate by
temporarily adding a `thumbnail_blob TEXT` column to `search_matches` in a scratch copy of the
DDL applied to a throwaway DB (inside a test fixture case, not by editing 0001) and showing the
lint fails; `ruff check`; `mypy` clean.

## Task 3 — Transactional outbox helper + producer lint rule

**Deliverables**

1. `src/imageshield/outbox.py`:
   - `QUEUES`: the two known queue names `identity:index` and `search:runs` (module constant;
     these map to `Config.sqs_identity_index_url` / `Config.sqs_search_runs_url`).
   - A pydantic payload model (`extra='forbid'`) enforcing the messages-carry-IDs rule: fields
     `event: str` and `id: UUID` only. Workers re-read state from Postgres.
   - `async def enqueue(conn: psycopg.AsyncConnection, queue_name: str, payload: OutboxPayload) -> int`
     — validates `queue_name` against `QUEUES`, INSERTs into `outbox` **on the caller's
     connection inside the caller's open transaction**, returns `outbox_id`. It must not commit,
     open its own connection, or touch SQS. A sync twin for non-async callers is fine if trivial.
   - This helper is the ONLY way a domain write enqueues a message: writing the domain row and
     the outbox row in one transaction is the entire point.
2. Lint rule forbidding direct SQS sends from application code: ruff `TID251` banned-api entry
   for `boto3` in `pyproject.toml` (message pointing at the outbox helper), with
   `per-file-ignores` allowing it only in the relay module (`src/imageshield/relay.py`, created
   in Task 4 — add the ignore now with a comment). Add `TID` to the ruff `select` list.
3. `tests/test_outbox.py` (DB-backed via Task 1 harness):
   - enqueue inside a transaction that **rolls back** → no outbox row (the rolled-back-txn
     "done when" case).
   - enqueue inside a committed transaction → exactly one row, `published_at IS NULL`,
     `attempts = 0`, payload round-trips.
   - unknown `queue_name` → raises before any INSERT.
   - payload with extra fields → pydantic rejects.

**Verification:** tests green; `ruff check` (proves the TID251 config parses and the codebase
passes it); `mypy` clean.

## Task 4 — Outbox relay process

**Deliverables**

1. Config additions in `src/imageshield/config.py` (validated like the rest):
   `outbox_poll_interval_seconds: float = 1.0`, `outbox_batch_size: int = 50`,
   `outbox_max_attempts: int = 8` (positive-value validators). Update `tests/conftest.py`
   defaults only if required.
2. `src/imageshield/relay.py` — the only module allowed to import boto3:
   - Runs as a separate process: `python -m imageshield.relay` (add a `__main__` guard or a
     small `relay/__main__.py`-style entry; keep consistent with `imageshield/__main__.py`
     conventions). Never imported by the HTTP app.
   - Sync psycopg connection (`Config.database_url`). Poll loop:
     `SELECT outbox_id, queue_name, payload FROM outbox WHERE published_at IS NULL AND attempts < %(max)s ORDER BY outbox_id LIMIT %(batch)s FOR UPDATE SKIP LOCKED`
     (SKIP LOCKED so two relays never double-send a locked row).
   - For each row: resolve queue URL from queue name (`identity:index` →
     `sqs_identity_index_url`, `search:runs` → `sqs_search_runs_url`; unknown name → treat as
     failure, record error), `SendMessage` with the JSON payload, then set
     `published_at = now()`. **Publish first, mark second, commit after the batch row** — a
     crash between send and mark leaves the row unpublished and it is retried (at-least-once;
     duplicates are normal, consumers are idempotent).
   - On send failure: `attempts = attempts + 1`, `last_error = <message>`, leave
     `published_at` NULL, continue with remaining rows (one bad row never stalls the queue).
   - Exponential backoff per row between retries. The DDL has no `next_attempt_at` column (spec
     is verbatim), so hold backoff state in process memory (row id → earliest next try,
     `base * 2^attempts`, capped at ~5 min); a restart resetting backoff is acceptable and must
     be documented in the module docstring.
   - Dead-letter: rows with `attempts >= outbox_max_attempts` are excluded by the poll query and
     logged at error level with `outbox_id` and `queue_name` (structlog) the first time they are
     seen dead. Ops finds them via `published_at IS NULL AND attempts >= N`.
   - The SQS client is injectable (constructor/function parameter with a boto3 default) so tests
     stub it; boto3 client built with `region_name=config.aws_region` and, when the queue URL
     host is localhost/localstack, `endpoint_url` derived from it.
3. `tests/test_relay.py` (DB-backed, stub SQS client — no LocalStack dependency in CI):
   - happy path: unpublished row → stub receives message → row marked published exactly once;
     re-running the poll publishes nothing further.
   - failing client: attempts increments, `last_error` recorded, `published_at` stays NULL, and
     a later poll (with a now-working client and backoff elapsed/bypassed) retries and publishes
     — the "killed mid-publish → retries" done-when expressed deterministically.
   - a row at `attempts = outbox_max_attempts` is never selected.
   - mixed batch: one failing row does not prevent the others publishing.
   - unknown queue_name: recorded as failure, no crash.
4. Optional (nice-to-have, env-gated, skipped by default): one integration test against the
   local LocalStack (`SQS_*_URL` env) proving an end-to-end publish. Skip silently when
   unavailable; CI does not run it.

**Verification:** tests green; `ruff check` passes (relay's boto3 import allowed via
per-file-ignore only); `mypy` clean; manual smoke against the running compose stack:
insert an outbox row, run the relay once, see the message in LocalStack
(`awslocal sqs receive-message`).
