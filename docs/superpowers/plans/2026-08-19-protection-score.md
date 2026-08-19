# Protection Score, Confirm Pipeline, Threat Events, Review & Control Room — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One push that ships the per-person protection score (journaled, config-versioned), the
machine-triage/human-confirm pipeline over provider hits (fetch → pHash dedup → face-match →
moderation → severity-ordered review), admin-curated threat events, the fetcher and control-room
deployables, and four new `svc` contract views.

**Architecture:** Five new modules (`score/`, `recommendations/`, `threats/`, `confirm/`,
`review/`) inside the existing FastAPI service, plus two new deployables (`fetcher/`, `console/`)
that run from the same image with their own tiny `BaseSettings` configs and **no DB
credentials**. One new queue (`confirm:hits`) through the existing transactional outbox → relay →
SQS worker pattern. Score recompute runs synchronously right after each trigger commits; a
`score/tick.py` poll loop is the drift healer. Machine triage can order the review queue but a
schema CHECK makes human `decided_by` the only path to `confirmed`.

**Tech Stack:** Python ≥3.11, FastAPI + uvicorn, psycopg 3 raw SQL (no ORM), pydantic 2,
structlog, boto3 (Rekognition only), httpx, Pillow (dHash + crop/blur), Jinja2 (console only,
new dep), pytest with the hand-rolled `tests/db.py` harness (NOT pytest-postgresql fixtures).

**Spec:** `docs/superpowers/specs/2026-08-19-protection-score-design.md` — read it first; every
task below argues from it.

## Global Constraints

Copied from the spec and the repo's standing rules — every task inherits these:

- **No `bytea` column anywhere; no image bytes persisted.** `phash` is `BIGINT`. Moderation output
  is stored as label text in JSONB. The schema lint (`tests/test_schema_lint.py`) must stay green.
- **Column-name gate:** never name a column matching `_(data|blob|bytes|b64)$` or containing
  `thumbnail`/`local_path`.
- **Enum idiom:** `TEXT` + `CHECK (col IN (...))`, never a new Postgres `ENUM` type (reversibility;
  see 0016's down leg). All PKs `UUID DEFAULT gen_random_uuid()` except append-only journals which
  may be `BIGSERIAL` (like `audit_log`). All timestamps `TIMESTAMPTZ`.
- **Migrations:** next free number is `0021`. Paired `.up.sql`/`.down.sql`, lexicographic 4-digit
  names, no `BEGIN/COMMIT` needed (runner wraps), files read as `utf-8-sig`. Never edit an applied
  migration — checksums are enforced. Every new table's grants are **enumerated** in the migration
  (0015's rule: "a future table joins no role by accident").
- **boto3 is banned by ruff TID251** except sanctioned files. New sanctioned files must be added to
  `pyproject.toml` `[tool.ruff.lint.per-file-ignores]` AND the ban message updated. **Never an S3
  client anywhere** (`tests/test_boundaries.py::test_no_s3_client_anywhere_in_src`).
- **Face search (`SearchFacesByImage` etc.) only inside `src/imageshield/attribution/`.** The
  confirm pipeline calls attribution's functions; it never touches the Rekognition search API
  directly. Both grep gates in `tests/test_boundaries.py` must stay green unmodified.
- **No literal `18`** in code tokens under `src/imageshield/subjects/`, `src/imageshield/http/`,
  or `src/imageshield/config.py` (tokenize-based gate). `CSAM_AGE_LOW_THRESHOLD` is therefore a
  **required** config field with no default.
- **No phone-shaped string literals** (7–15 digits) in `src/` Python or migration SQL string
  literals. Keep decimal-string literals under 7 digits (e.g. `Decimal("0.005")`, never
  `Decimal("0.005000")`).
- **Inbound request bodies** extend `ServiceModel` (`extra="forbid", frozen=True` —
  `src/imageshield/http/models.py:22`). Domain models `ConfigDict(frozen=True)`. Money is
  `Decimal`, crossing HTTP as a decimal **string**.
- **Every route** (except `/health`, `/readyz`, and the two fetcher/console apps which have their
  own auth) sits on a router with `dependencies=[Depends(require_service_token)]`; admin routers
  add `require_admin_service_token`. `tests/test_route_auth_coverage.py` walks `app.routes` and
  will fail any unauthenticated route automatically.
- **Errors** are raised as `ServiceError(status_code, code, message, retryable=...)`
  (`src/imageshield/http/errors.py:36`) — the envelope handler formats them.
- **Config:** new knobs are fields on `Config` (`src/imageshield/config.py`), env-named,
  validated at boot; read from `cfg` at request time, never served from a constant beside a route.
- **One threshold per purpose:** the confirm pass gets its own `CONFIRM_FACE_MATCH_THRESHOLD`;
  never reuse `ATTRIBUTION_MATCH_THRESHOLD`.
- **Tests:** `asyncio_mode=auto` (no `@pytest.mark.asyncio`). DB tests: `throwaway_db` +
  `run_migrate(db, "down", "--all")` then `up` in a `migrated_db`-style fixture; direct asserts via
  sync `psycopg.connect(url, autocommit=True, row_factory=dict_row)`. Route tests: sync
  `TestClient` WITHOUT context manager, fakes injected via `app.state.*`. Postgres must be up:
  `docker compose -f docker-compose.local.yml up -d` (host port 15433).
- **Run targeted tests while iterating; the FULL suite exactly once, in the final task** (it takes
  ~7 minutes idle). `REQUIRE_DB=1` for the final run.
- **Commits:** small, per task step, message style `feat:`/`test:`/`docs:` as in `git log`. Every
  commit ends with the trailer line exactly:
  `Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>`
  (never a Claude trailer).
- **mypy strict** covers `src/imageshield` — every new module must typecheck strictly. `ruff
  check .` covers the repo.

## File Structure

New files (C=create, M=modify):

```
C migrations/0021_confirm_and_review.up.sql        confirm columns, review_tasks, provider row
C migrations/0021_confirm_and_review.down.sql
C migrations/0022_score_recs_threats.up.sql        score/recommendations/threat tables, score_rw
C migrations/0022_score_recs_threats.down.sql
C migrations/0023_svc_score_views.up.sql           4 new views + REPLACE hits/summary + grants
C migrations/0023_svc_score_views.down.sql
M src/imageshield/config.py                        new fields + validators
M src/imageshield/outbox.py                        QUEUE_CONFIRM_HITS
M src/imageshield/relay.py                         queue→config-field map entry
C src/imageshield/confirm/__init__.py
C src/imageshield/confirm/models.py                ConfirmCriteria, ConfirmContext, event const
C src/imageshield/confirm/phash.py                 64-bit dHash + hamming (Pillow)
C src/imageshield/confirm/moderation.py            DetectModerationLabels + AgeRange (boto3)
C src/imageshield/confirm/triage.py                pure severity classifier + CSAM tripwire
C src/imageshield/confirm/store.py                 PostgresConfirmStore
C src/imageshield/confirm/worker.py                confirm:hits SQS consumer (boto3 receive)
M src/imageshield/recheck/ssrf.py                  split out address_refusal()
C src/imageshield/fetcher/__init__.py
C src/imageshield/fetcher/config.py                FetcherConfig (NO database_url field at all)
C src/imageshield/fetcher/fetch.py                 hardened GET, size cap, manual redirects
C src/imageshield/fetcher/app.py                   /health, /v1/fetch, /v1/crop (blur default)
C src/imageshield/score/__init__.py
C src/imageshield/score/engine.py                  pure component math (ScoreState → Components)
C src/imageshield/score/store.py                   PostgresScoreStore.recompute (the ONE writer)
C src/imageshield/score/tick.py                    poll-loop process (drift healer + aging)
C src/imageshield/recommendations/__init__.py
C src/imageshield/recommendations/catalog.py       pure rules: desired()/RecSpec
C src/imageshield/threats/__init__.py
C src/imageshield/threats/store.py                 CRUD + domain matcher + audit
C src/imageshield/review/__init__.py
C src/imageshield/review/store.py                  next_task/decide (one txn, audit, #19)
C src/imageshield/console/__init__.py
C src/imageshield/console/config.py                ConsoleConfig (no DB)
C src/imageshield/console/auth.py                  HTTP Basic per-operator tokens
C src/imageshield/console/client.py                httpx client for services + fetcher
C src/imageshield/console/app.py                   server-rendered ops console
C src/imageshield/console/templates/*.html         base, dashboard, review, events, score
C src/imageshield/http/routes/admin_review.py      GET next / POST decision
C src/imageshield/http/routes/admin_threat_events.py
C src/imageshield/http/routes/admin_scores.py      GET score + journal
M src/imageshield/http/models.py                   request/response models
M src/imageshield/http/deps.py                     get_score_store, get_review_store, get_threat_store
M src/imageshield/http/app.py                      wire stores + include routers
M src/imageshield/http/svc_contract.py             EXPECTED_VIEWS additions
M src/imageshield/http/routes/infringements.py     feedback → recompute trigger
M src/imageshield/http/routes/liveness.py          enrolment success → recompute trigger
M src/imageshield/http/routes/search.py            seed create → recompute trigger
M src/imageshield/http/routes/attribution.py       registered seeds → recompute trigger
M src/imageshield/search/store.py                  complete_run enqueues confirm:hits
M src/imageshield/search/runner.py                 threads ConfirmCriteria through
M src/imageshield/search/worker.py                 builds criteria + score store, post-run recompute
M pyproject.toml                                   jinja2 dep, TID251 ignores, package-data
M scripts/localstack/init-sqs.sh                   imageshield-confirm-hits
M infra/terraform/queues.tf                        confirm-hits entry
M infra/terraform/iam.tf                           new templatefile vars
M infra/terraform/policies/service-role.json       DetectModerationLabels + confirm queue ARNs
M infra/ecs/imageshield-dev-services.json          new env/secrets
M infra/ecs/imageshield-dev-services-worker.json   new env/secrets
M infra/ecs/policies/services-task-role.json       moderation actions + confirm queue
C infra/ecs/imageshield-dev-confirm.json           confirm-worker + score-tick containers
C infra/ecs/imageshield-dev-fetcher.json           fetcher (NO DB secrets)
C infra/ecs/imageshield-dev-console.json           console (NO DB secrets)
M tests/conftest.py                                VALID_ENV additions
M .env.example                                     new vars
C tests/test_confirm_phash.py, test_confirm_triage.py, test_confirm_moderation.py,
  test_confirm_store.py, test_confirm_worker.py, test_fetcher.py, test_score_engine.py,
  test_score_store.py, test_recommendations.py, test_threats.py, test_review.py,
  test_admin_review_routes.py, test_admin_threat_routes.py, test_admin_scores_routes.py,
  test_console.py, test_score_tick.py
M tests/test_svc_views.py, tests/test_readyz.py, tests/test_migrations.py,
  tests/test_iam_policy.py, tests/test_ecs_task_defs.py, tests/test_search_store.py,
  tests/test_search_runner.py, tests/test_search_worker.py, tests/test_boundaries.py (score
  single-writer gate added), tests/test_recheck.py (ssrf split)
M docs: INVARIANTS.md (#44–47), CLAUDE.md (§6/§2), SCHEMA.md, ARCHITECTURE.md, OPERATIONS.md,
  PROXY_INTEGRATION.md (§6), docs/deploy/DEPLOY-RUNBOOK.md
```

Severity vocabulary used everywhere (5 values — `unassessed` is the unfetchable/URL-only case):
`ncii_suspected` > `explicit_unmatched` > `unassessed` > `benign_copy` > `likely_not_subject`.

---

### Task 1: Migration 0021 — confirm & review schema

**Files:**
- Create: `migrations/0021_confirm_and_review.up.sql`, `migrations/0021_confirm_and_review.down.sql`
- Modify: `src/imageshield/search/store.py` (the `enabled_provider_ids` SQL — see step 5)
- Modify: `tests/test_migrations.py` (provider seed pinning tests)
- Test: `tests/test_migrations.py` (extend), `tests/test_schema_lint.py` (already blocking)

**Interfaces:**
- Consumes: existing `infringements`, `providers` (0001/0004/0005/0009/0019), `search_rw` role (0015).
- Produces: `infringements.confirm_state/severity/confirm_decided_by/confirm_decided_at/duplicate_of/phash/face_match_score/moderation_labels`; table `review_tasks`; provider row `rekognition_confirm`. Later tasks rely on these exact column names and the severity CHECK list.

- [ ] **Step 1: Write the failing migration tests**

Append to `tests/test_migrations.py` (follow the file's existing style — it already has a
`migrated_db`-equivalent pattern via `run_migrate`):

```python
def test_0021_confirm_columns_and_review_tasks(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    up = run_migrate(throwaway_db, "up")
    assert up.returncode == 0, up.stderr
    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        cols = {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'infringements'"
            ).fetchall()
        }
        assert {
            "confirm_state", "severity", "confirm_decided_by", "confirm_decided_at",
            "duplicate_of", "phash", "face_match_score", "moderation_labels",
        } <= cols
        # confirmed without a human is a constraint violation (INVARIANTS #19 by schema)
        conn.execute(
            "INSERT INTO subjects (user_ref, discovery_eligible, eligibility_reason)"
            " VALUES ('00000000-0000-0000-0000-000000000001', true, 'adult')"
        )
        conn.execute(
            "INSERT INTO content_urls (url_hash, url, source_domain) VALUES"
            " (repeat('a', 64), 'https://x.example/a', 'x.example')"
        )
        conn.execute(
            "INSERT INTO infringements (user_ref, url_hash, page_url) VALUES"
            " ('00000000-0000-0000-0000-000000000001', repeat('a', 64), 'https://x.example/a')"
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "UPDATE infringements SET confirm_state = 'confirmed'"
            )


def test_0021_seeds_the_rekognition_confirm_provider(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    assert run_migrate(throwaway_db, "up").returncode == 0
    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        row = conn.execute(
            "SELECT kind, enabled, calibrated, cost_per_call_usd FROM providers"
            " WHERE provider_id = 'rekognition_confirm'"
        ).fetchone()
    assert row is not None
    assert row[0] == "classifier"
    assert row[1] is True
    assert row[2] is False
    assert row[3] == Decimal("0.005")
```

Also FIND the existing tests that pin the seeded provider set (three tests, added in commit
`53dbd8b`, around `tests/test_migrations.py:736-749` — search for `{"hive", "google", "stub"}`)
and extend each pinned set to `{"hive", "google", "stub", "rekognition_confirm"}`.

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_migrations.py -k "0021 or provider" -v`
Expected: the two new tests FAIL (columns missing / provider row absent); the pinned-set tests
FAIL until the migration exists (they now expect 4 providers).

- [ ] **Step 3: Write the migration**

`migrations/0021_confirm_and_review.up.sql`:

```sql
-- Confirm pipeline + review queue.
-- Design: docs/superpowers/specs/2026-08-19-protection-score-design.md §7–§8.
--
-- confirm_state is the machine/human lifecycle of a hit and is DISTINCT from
-- infringements.status (the user's position) and band (calibration). Only a
-- human decision can produce 'confirmed' — the CHECK below is INVARIANTS #19
-- enforced by schema, not by application code.

ALTER TABLE infringements
  ADD COLUMN confirm_state TEXT NOT NULL DEFAULT 'unconfirmed'
    CHECK (confirm_state IN
      ('unconfirmed', 'machine_triaged', 'confirmed', 'rejected', 'duplicate', 'quarantined')),
  ADD COLUMN severity TEXT
    CHECK (severity IN
      ('ncii_suspected', 'explicit_unmatched', 'unassessed', 'benign_copy', 'likely_not_subject')),
  ADD COLUMN confirm_decided_by TEXT,
  ADD COLUMN confirm_decided_at TIMESTAMPTZ,
  ADD COLUMN duplicate_of UUID REFERENCES infringements(infringement_id),
  -- 64-bit perceptual hash (dHash) of the fetched image. A hash, not bytes:
  -- INVARIANTS #9 stands. Signed BIGINT; Python converts with two's complement.
  ADD COLUMN phash BIGINT,
  ADD COLUMN face_match_score NUMERIC(5,2),
  -- Rekognition moderation label names + confidences. Labels are text about
  -- the image, never the image.
  ADD COLUMN moderation_labels JSONB;

ALTER TABLE infringements
  ADD CONSTRAINT infringements_confirmed_needs_human CHECK (
    confirm_state <> 'confirmed'
    OR (confirm_decided_by IS NOT NULL AND confirm_decided_at IS NOT NULL)
  ),
  ADD CONSTRAINT infringements_duplicate_needs_source CHECK (
    (confirm_state = 'duplicate') = (duplicate_of IS NOT NULL)
  );

CREATE INDEX infringements_confirm_state_idx
  ON infringements (user_ref, confirm_state);

-- pHash dedup lookup: "has a human already decided this picture for this user".
CREATE INDEX infringements_decided_phash_idx
  ON infringements (user_ref)
  WHERE phash IS NOT NULL AND confirm_state IN ('confirmed', 'rejected');

CREATE TABLE review_tasks (
  task_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  infringement_id  UUID NOT NULL REFERENCES infringements(infringement_id) ON DELETE CASCADE,
  user_ref         UUID NOT NULL,
  severity         TEXT NOT NULL
                   CHECK (severity IN
                     ('ncii_suspected', 'explicit_unmatched', 'unassessed',
                      'benign_copy', 'likely_not_subject')),
  -- face_match_score, moderation label names, fetched image_url, best-face
  -- bbox, unfetchable detail. Text about the image, never pixels.
  triage           JSONB NOT NULL,
  status           TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'decided', 'quarantined')),
  decision         TEXT CHECK (decision IN ('confirmed', 'rejected')),
  decided_by       TEXT,
  decided_at       TIMESTAMPTZ,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- One live task per hit; an 'uncertain' decision keeps the SAME row pending
  -- (the decision history is audit_log's job).
  UNIQUE (infringement_id),
  CONSTRAINT review_tasks_decided_shape CHECK (
    (status = 'decided')
    = (decision IS NOT NULL AND decided_by IS NOT NULL AND decided_at IS NOT NULL)
  )
);

CREATE INDEX review_tasks_queue_idx ON review_tasks (
  (CASE severity
     WHEN 'ncii_suspected'    THEN 0
     WHEN 'explicit_unmatched' THEN 1
     WHEN 'unassessed'        THEN 2
     WHEN 'benign_copy'       THEN 3
     ELSE 4
   END),
  created_at
) WHERE status = 'pending';

-- 0015's rule: a new table joins a role in a migration, in review. The review
-- queue is part of the search/discovery pipeline, so search_rw owns it.
GRANT SELECT, INSERT, UPDATE ON review_tasks TO search_rw;

-- The confirm pass as a provider row so the EXISTING budget/breaker/spend
-- machinery governs it (INVARIANTS #37–#41). kind 'classifier' is the enum
-- value 0001 reserved for exactly this shape. cost_per_call_usd is the
-- WORST-CASE BUNDLE price (1 DetectFaces + up to 3 SearchFacesByImage +
-- 1 DetectModerationLabels at list ~0.001 each) so the one-row budget check
-- stays conservative without modification.
INSERT INTO providers (provider_id, kind, enabled, calibrated, score_version,
                       cost_per_call_usd, score_kind, score_domain)
VALUES ('rekognition_confirm', 'classifier', true, false, 'rekognition-confirm-v1',
        0.005, 'numeric', NULL)
ON CONFLICT (provider_id) DO NOTHING;
```

`migrations/0021_confirm_and_review.down.sql`:

```sql
DELETE FROM providers WHERE provider_id = 'rekognition_confirm';

DROP TABLE review_tasks;

DROP INDEX infringements_decided_phash_idx;
DROP INDEX infringements_confirm_state_idx;

ALTER TABLE infringements
  DROP CONSTRAINT infringements_duplicate_needs_source,
  DROP CONSTRAINT infringements_confirmed_needs_human;

ALTER TABLE infringements
  DROP COLUMN moderation_labels,
  DROP COLUMN face_match_score,
  DROP COLUMN phash,
  DROP COLUMN duplicate_of,
  DROP COLUMN confirm_decided_at,
  DROP COLUMN confirm_decided_by,
  DROP COLUMN severity,
  DROP COLUMN confirm_state;
```

- [ ] **Step 4: Guard search dispatch against the classifier row**

`enabled_provider_ids()` in `src/imageshield/search/store.py` currently selects every enabled
provider; the new `rekognition_confirm` row would land in `search_runs.providers_attempted` and
produce a `no adapter registered` error row on every search run. Find its SQL (search for
`enabled_provider_ids`) and constrain it:

```sql
SELECT provider_id FROM providers
WHERE enabled AND kind IN ('image_search', 'face_search')
ORDER BY provider_id
```

Add a test beside the existing `enabled_provider_ids` coverage (in `tests/test_search_store.py`):

```python
async def test_enabled_provider_ids_excludes_classifier_rows(store, migrated_db) -> None:
    # rekognition_confirm is seeded enabled=true by 0021 but is not a search
    # provider; attempting it would fabricate a permanent per-run error row.
    ids = await store.enabled_provider_ids()
    assert ProviderId("rekognition_confirm") not in ids
```

(Adapt fixture names to that file's existing ones.)

- [ ] **Step 5: Run the affected tests**

Run: `python -m pytest tests/test_migrations.py tests/test_schema_lint.py tests/test_search_store.py -v`
Expected: PASS (including the forward-back-forward round trip and the schema lint over the
migrated schema — `phash BIGINT` and `moderation_labels JSONB` pass; nothing is `bytea`).

- [ ] **Step 6: Commit**

```bash
git add migrations/0021_confirm_and_review.up.sql migrations/0021_confirm_and_review.down.sql src/imageshield/search/store.py tests/test_migrations.py tests/test_search_store.py
git commit -m "feat: 0021 confirm/review schema — #19 by CHECK, rekognition_confirm provider row"
```
(with the standard trailer.)

### Task 2: Migration 0022 — score, recommendations, threat tables

**Files:**
- Create: `migrations/0022_score_recs_threats.up.sql`, `migrations/0022_score_recs_threats.down.sql`
- Test: `tests/test_migrations.py` (extend)

**Interfaces:**
- Consumes: `subjects` (0008), the 0015/0017/0018 role-grant idioms.
- Produces: tables `protection_scores`, `score_events`, `recommendations`, `threat_events`,
  `threat_event_matches`; role `score_rw` (granted to `app_services` when it exists). Later tasks
  rely on these exact column names, CHECK vocabularies, and on `score_events` being
  **INSERT-only** for `score_rw`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_migrations.py`:

```python
def test_0022_score_tables_and_role(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    assert run_migrate(throwaway_db, "up").returncode == 0
    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema = 'public'"
            ).fetchall()
        }
        assert {
            "protection_scores", "score_events", "recommendations",
            "threat_events", "threat_event_matches",
        } <= tables
        assert conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = 'score_rw'"
        ).fetchone() is not None


def test_0022_score_events_is_insert_only_for_score_rw(throwaway_db: str) -> None:
    """An editable journal is not a journal — same shape as the audit_log test."""
    run_migrate(throwaway_db, "down", "--all")
    assert run_migrate(throwaway_db, "up").returncode == 0
    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO subjects (user_ref, discovery_eligible, eligibility_reason)"
            " VALUES ('00000000-0000-0000-0000-000000000002', true, 'adult')"
        )
        conn.execute("SET ROLE score_rw")
        conn.execute(
            "INSERT INTO score_events"
            " (user_ref, delta, component, cause_kind, config_version, score_after)"
            " VALUES ('00000000-0000-0000-0000-000000000002', 5, 'posture',"
            "         'initialised', 'score-v1', 5)"
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE score_events SET delta = 100")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM score_events")
        conn.execute("RESET ROLE")
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_migrations.py -k 0022 -v` — Expected: FAIL (tables absent).

- [ ] **Step 3: Write the migration**

`migrations/0022_score_recs_threats.up.sql`:

```sql
-- Protection score, recommendations, threat events.
-- Design: docs/superpowers/specs/2026-08-19-protection-score-design.md §4–§6.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'score_rw') THEN
    CREATE ROLE score_rw NOLOGIN;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO score_rw;

-- Materialized current score. One row per person; exactly one code path
-- (score/store.py) writes it — INVARIANTS #21 extended, plus a boundary test.
CREATE TABLE protection_scores (
  user_ref       UUID PRIMARY KEY REFERENCES subjects(user_ref),
  score          INT NOT NULL CHECK (score BETWEEN 0 AND 100),
  -- {"posture": int, "coverage": int, "exposure": int, "threat": int}
  components     JSONB NOT NULL,
  config_version TEXT NOT NULL,
  computed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Append-only journal. Every point movement, with a user-readable cause
-- (INVARIANTS #44). BIGSERIAL like audit_log: an ordered journal, not an
-- addressable resource.
CREATE TABLE score_events (
  score_event_id BIGSERIAL PRIMARY KEY,
  user_ref       UUID NOT NULL REFERENCES subjects(user_ref),
  delta          INT NOT NULL,
  component      TEXT NOT NULL
                 CHECK (component IN ('posture', 'coverage', 'exposure', 'threat')),
  cause_kind     TEXT NOT NULL,
  cause_ref      TEXT,
  config_version TEXT NOT NULL,
  score_after    INT NOT NULL CHECK (score_after BETWEEN 0 AND 100),
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX score_events_user_idx ON score_events (user_ref, score_event_id DESC);

CREATE TABLE threat_events (
  event_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  kind        TEXT NOT NULL
              CHECK (kind IN ('leak', 'deepfake_wave', 'platform_incident', 'other')),
  title       TEXT NOT NULL CHECK (title <> ''),
  body        TEXT NOT NULL DEFAULT '',
  severity    SMALLINT NOT NULL CHECK (severity BETWEEN 1 AND 5),
  -- Relevance matcher: hit domains. Flattened from the spec's matcher jsonb so
  -- the join below is SQL, not JSON spelunking.
  domains     TEXT[] NOT NULL DEFAULT '{}',
  is_global   BOOLEAN NOT NULL DEFAULT false,
  penalty     NUMERIC(5,2) NOT NULL CHECK (penalty > 0),
  starts_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at  TIMESTAMPTZ NOT NULL,
  decay_days  INT NOT NULL CHECK (decay_days > 0),
  status      TEXT NOT NULL DEFAULT 'active'
              CHECK (status IN ('draft', 'active', 'expired', 'retracted')),
  created_by  TEXT NOT NULL CHECK (created_by <> ''),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (expires_at > starts_at),
  -- An event that matches nothing is a typo, not a threat.
  CHECK (is_global OR cardinality(domains) > 0)
);

CREATE INDEX threat_events_active_idx ON threat_events (status)
  WHERE status = 'active';

CREATE TABLE threat_event_matches (
  event_id        UUID NOT NULL REFERENCES threat_events(event_id) ON DELETE CASCADE,
  user_ref        UUID NOT NULL REFERENCES subjects(user_ref),
  matched_via     TEXT NOT NULL,   -- the matching domain, or 'global'
  penalty_applied NUMERIC(5,2) NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (event_id, user_ref)
);

CREATE INDEX threat_event_matches_user_idx ON threat_event_matches (user_ref);

CREATE TABLE recommendations (
  rec_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_ref        UUID NOT NULL REFERENCES subjects(user_ref),
  kind            TEXT NOT NULL CHECK (kind IN
    ('complete_enrolment', 'add_seed_photos', 'refresh_seeds',
     'respond_to_hits', 'run_priority_scan')),
  params          JSONB NOT NULL DEFAULT '{}'::jsonb,
  status          TEXT NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open', 'completed', 'expired', 'dismissed')),
  source_event_id UUID REFERENCES threat_events(event_id),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at    TIMESTAMPTZ,
  expires_at      TIMESTAMPTZ
);

-- One open instance per (person, kind, triggering event). The sentinel UUID
-- collapses NULL source_event_id into one bucket for the uniqueness check.
CREATE UNIQUE INDEX recommendations_open_uniq ON recommendations (
  user_ref, kind,
  COALESCE(source_event_id, '00000000-0000-0000-0000-000000000000'::uuid)
) WHERE status = 'open';

CREATE INDEX recommendations_user_open_idx ON recommendations (user_ref)
  WHERE status = 'open';

-- Grants, enumerated (0015's rule). score_events gets NO UPDATE and NO DELETE:
-- an editable journal is not a journal.
GRANT SELECT, INSERT, UPDATE ON
  protection_scores, recommendations, threat_events, threat_event_matches
  TO score_rw;
GRANT SELECT, INSERT ON score_events TO score_rw;
GRANT USAGE ON SEQUENCE score_events_score_event_id_seq TO score_rw;

-- Reach the deployed login role, 0018-style: conditional, idempotent, noisy.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_services') THEN
    GRANT score_rw TO app_services;
    RAISE NOTICE 'granted score_rw to app_services';
  ELSE
    RAISE NOTICE 'role app_services absent; score_rw not granted to it';
  END IF;
END
$$;
```

`migrations/0022_score_recs_threats.down.sql`:

```sql
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_services') THEN
    REVOKE score_rw FROM app_services;
  END IF;
END
$$;

DROP TABLE recommendations;
DROP TABLE threat_event_matches;
DROP TABLE threat_events;
DROP TABLE score_events;
DROP TABLE protection_scores;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'score_rw') THEN
    REVOKE USAGE ON SCHEMA public FROM score_rw;
    DROP ROLE score_rw;
  END IF;
END
$$;
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_migrations.py tests/test_schema_lint.py -v`
Expected: PASS, including the up-down-up round trip (the down leg above must leave a clean 0021
state).

- [ ] **Step 5: Commit**

```bash
git add migrations/0022_score_recs_threats.up.sql migrations/0022_score_recs_threats.down.sql tests/test_migrations.py
git commit -m "feat: 0022 score/recommendations/threat tables, insert-only score journal"
```

---

### Task 3: Config additions

**Files:**
- Modify: `src/imageshield/config.py`, `tests/conftest.py` (`VALID_ENV`), `.env.example`
- Test: `tests/test_config.py` (extend)

**Interfaces:**
- Produces (exact `Config` field names; env vars are the upper-cased forms):

```python
# protection score — all defaulted, structure fixed, numbers config
score_config_version: str = "score-v1"
score_weight_posture: int = 40
score_weight_coverage: int = 25
score_weight_exposure: int = 25
score_weight_threat: int = 10
score_posture_enrolment: int = 10
score_posture_seeds: int = 20
score_posture_feedback: int = 5
score_posture_recommendations: int = 5
score_coverage_scan: int = 15
score_coverage_providers: int = 10
score_exposure_weight_ncii: int = 12
score_exposure_weight_explicit: int = 6
score_exposure_weight_benign: int = 2
score_exposure_weight_default: int = 6
score_threat_global_max_penalty: int = 2
score_seed_target: int = 5
score_seed_fresh_days: int = 90
score_rec_soft_age_days: int = 14
score_scan_grace_days: int = 3
score_tick_interval_seconds: float = 3600.0
# confirm pipeline
sqs_confirm_hits_url: str                    # REQUIRED
fetcher_base_url: str                        # REQUIRED
fetcher_token: str                           # REQUIRED (token rules: >=16 chars, no sentinel)
confirm_hive_min_score: float = 0.80
confirm_google_kinds: str = "full_match,partial_match"
confirm_face_match_threshold: float = 92.0
confirm_max_faces: int = 3
confirm_phash_hamming_max: int = 8
csam_age_low_threshold: int                  # REQUIRED — no default on purpose:
                                             # the age-literal gate bans a bare 18 in config.py,
                                             # and CLAUDE.md §4#8 wants age floors in config.
```

- [ ] **Step 1: Write failing tests** (append to `tests/test_config.py`, using its existing
`make_config`/env-patching style):

```python
def test_score_weights_must_sum_to_100() -> None:
    with pytest.raises(ConfigError, match="SCORE_WEIGHT"):
        make_config(SCORE_WEIGHT_POSTURE="50")  # 50+25+25+10 = 110


def test_posture_subweights_must_sum_to_posture() -> None:
    with pytest.raises(ConfigError, match="SCORE_POSTURE"):
        make_config(SCORE_POSTURE_SEEDS="25")


def test_exposure_weight_cannot_exceed_component() -> None:
    with pytest.raises(ConfigError, match="SCORE_EXPOSURE"):
        make_config(SCORE_EXPOSURE_WEIGHT_NCII="30")


def test_google_confirm_kinds_must_be_known_categories() -> None:
    with pytest.raises(ConfigError, match="CONFIRM_GOOGLE_KINDS"):
        make_config(CONFIRM_GOOGLE_KINDS="full_match,made_up")


def test_new_required_fields_refuse_absence() -> None:
    # dropping any of the three required additions must fail boot
    for missing in ("SQS_CONFIRM_HITS_URL", "FETCHER_BASE_URL", "CSAM_AGE_LOW_THRESHOLD"):
        env = dict(VALID_ENV)
        env.pop(missing)
        with pytest.raises(ConfigError):
            load_config_from(env)  # use the file's existing helper for env-isolated loads
```

(If `test_config.py` has no `load_config_from` helper, follow whatever idiom it already uses for
"required field absent" cases — there are existing tests for `SERVICE_TOKEN` to copy.)

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_config.py -v` (new tests
FAIL: unknown fields / no validators).

- [ ] **Step 3: Implement**

In `src/imageshield/config.py`:
1. Add the fields exactly as in the Interfaces block, grouped under a
   `# --- protection score / confirm pipeline (design 2026-08-19) ---` comment, placed with the
   other domain fields.
2. Extend existing field validators by adding names to their decorator lists:
   - `_http_url`: add `"sqs_confirm_hits_url"`, `"fetcher_base_url"`.
   - `_token`: add `"fetcher_token"`.
   - `_age`: add `"csam_age_low_threshold"`.
   - `_positive`: add every new positive int field; `_positive_float` for
     `score_tick_interval_seconds`; `_confidence` for `confirm_face_match_threshold`.
3. Add a field validator for `confirm_hive_min_score` (must satisfy `0 < v <= 1` — Hive's raw
   domain is 0.5–1.0).
4. Add one model validator:

```python
@model_validator(mode="after")
def _score_config_coherent(self) -> Config:
    weights = (self.score_weight_posture + self.score_weight_coverage
               + self.score_weight_exposure + self.score_weight_threat)
    if weights != 100:
        raise ValueError("SCORE_WEIGHT_* must sum to 100 — the score is out of 100")
    posture = (self.score_posture_enrolment + self.score_posture_seeds
               + self.score_posture_feedback + self.score_posture_recommendations)
    if posture != self.score_weight_posture:
        raise ValueError("SCORE_POSTURE_* sub-weights must sum to SCORE_WEIGHT_POSTURE")
    if self.score_coverage_scan + self.score_coverage_providers != self.score_weight_coverage:
        raise ValueError("SCORE_COVERAGE_* sub-weights must sum to SCORE_WEIGHT_COVERAGE")
    heaviest = max(self.score_exposure_weight_ncii, self.score_exposure_weight_explicit,
                   self.score_exposure_weight_benign, self.score_exposure_weight_default)
    if heaviest > self.score_weight_exposure:
        raise ValueError("SCORE_EXPOSURE_WEIGHT_* may not exceed SCORE_WEIGHT_EXPOSURE")
    if self.score_threat_global_max_penalty > self.score_weight_threat:
        raise ValueError(
            "SCORE_THREAT_GLOBAL_MAX_PENALTY may not exceed SCORE_WEIGHT_THREAT"
        )
    known = {"full_match", "partial_match", "page_match"}
    kinds = {part.strip() for part in self.confirm_google_kinds.split(",") if part.strip()}
    if not kinds or not kinds <= known:
        raise ValueError(
            "CONFIRM_GOOGLE_KINDS must be a comma list drawn from"
            " full_match/partial_match/page_match"
        )
    return self
```

5. Add a convenience property:

```python
@property
def confirm_google_kind_set(self) -> frozenset[str]:
    return frozenset(part.strip() for part in self.confirm_google_kinds.split(",") if part.strip())
```

6. `tests/conftest.py` `VALID_ENV` additions (keep alphabetical-ish grouping used there):

```python
"SQS_CONFIRM_HITS_URL": "https://sqs.ap-south-1.amazonaws.com/000000000000/imageshield-confirm-hits",
"FETCHER_BASE_URL": "http://localhost:8083",
"FETCHER_TOKEN": "fetcher-token-for-tests-0003",
"CSAM_AGE_LOW_THRESHOLD": "18",
```

7. `.env.example`: same four keys with local values
   (`SQS_CONFIRM_HITS_URL=http://localhost:14566/000000000000/imageshield-confirm-hits`) plus a
   commented block naming every defaulted `SCORE_*`/`CONFIRM_*` knob.

- [ ] **Step 4: Run** — `python -m pytest tests/test_config.py tests/test_boot.py -v` → PASS.
Also `python -m pytest tests/test_boundaries.py -v` → PASS (no bare `18` token was added to
`config.py`; the value lives in env).

- [ ] **Step 5: Commit** — `feat: score + confirm config, boot-validated weights`

---

### Task 4: Queue plumbing — `confirm:hits`

**Files:**
- Modify: `src/imageshield/outbox.py`, `src/imageshield/relay.py`, `scripts/localstack/init-sqs.sh`
- Create: `src/imageshield/confirm/__init__.py`, `src/imageshield/confirm/models.py`
- Test: `tests/test_outbox.py`, `tests/test_relay.py` (extend)

**Interfaces:**
- Produces: `outbox.QUEUE_CONFIRM_HITS = "confirm:hits"` (member of `outbox.QUEUES`);
  relay maps it to config field `sqs_confirm_hits_url`;
  `confirm/models.py` exports:

```python
CONFIRM_REQUESTED_EVENT = "confirm.hit_requested"
REKOGNITION_CONFIRM_ID = ProviderId("rekognition_confirm")

class ConfirmCriteria(BaseModel):
    model_config = ConfigDict(frozen=True)
    hive_min_score: Decimal
    google_kinds: frozenset[str]

class ConfirmContext(BaseModel):
    model_config = ConfigDict(frozen=True)
    infringement_id: UUID
    user_ref: UserRef
    confirm_state: str
    image_url: str | None
    page_url: str
    run_id: UUID | None          # representative attestation's last_run_id
```

- [ ] **Step 1: Failing tests.** In `tests/test_outbox.py` add: enqueue with
`QUEUE_CONFIRM_HITS` succeeds (round-trips a row) and `"confirm:hits" in QUEUES`; an unknown
queue name still raises `ValueError`. In `tests/test_relay.py` add: a row with
`queue_name="confirm:hits"` publishes to `config.sqs_confirm_hits_url` (mirror the existing
per-queue routing test — there is one for `search:runs` to copy).

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_outbox.py tests/test_relay.py -v`.

- [ ] **Step 3: Implement.**
`outbox.py`: add `QUEUE_CONFIRM_HITS = "confirm:hits"` beside the two existing constants and add
it to `QUEUES`. `relay.py`: add `QUEUE_CONFIRM_HITS: "sqs_confirm_hits_url"` to
`_QUEUE_NAME_TO_CONFIG_FIELD` (import the constant). `scripts/localstack/init-sqs.sh`: add
`imageshield-confirm-hits` to the `queues=()` array. Create `confirm/__init__.py` (docstring
only, matching sibling packages) and `confirm/models.py` exactly as the Interfaces block.

- [ ] **Step 4: Run** — same two files → PASS. `python -m pytest tests/test_types.py -v` still PASS.

- [ ] **Step 5: Commit** — `feat: confirm:hits queue through outbox and relay`

### Task 5: Perceptual hash — `confirm/phash.py`

**Files:**
- Create: `src/imageshield/confirm/phash.py`
- Test: `tests/test_confirm_phash.py`

**Interfaces:**
- Produces:

```python
def dhash(image: bytes) -> int          # signed 64-bit (fits Postgres BIGINT); raises UndecodableImage
def hamming(a: int, b: int) -> int      # 0..64, works on the signed representation
```

`UndecodableImage` is imported from `imageshield.attribution.crop` (it already exists there) —
one exception type for "these bytes are not an image", not two.

- [ ] **Step 1: Failing test** — `tests/test_confirm_phash.py`:

```python
from __future__ import annotations

import io

import pytest
from PIL import Image

from imageshield.attribution.crop import UndecodableImage
from imageshield.confirm.phash import dhash, hamming


def _png(color: tuple[int, int, int], size: tuple[int, int] = (64, 64)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _gradient(size: tuple[int, int] = (64, 64)) -> bytes:
    image = Image.new("L", size)
    image.putdata([(x * 4) % 256 for y in range(size[1]) for x in range(size[0])])
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_same_image_same_hash_across_encodings() -> None:
    image = Image.open(io.BytesIO(_gradient()))
    as_png, as_jpeg = io.BytesIO(), io.BytesIO()
    image.save(as_png, format="PNG")
    image.convert("RGB").save(as_jpeg, format="JPEG", quality=90)
    assert hamming(dhash(as_png.getvalue()), dhash(as_jpeg.getvalue())) <= 4


def test_resized_copy_is_near() -> None:
    original = _gradient((128, 128))
    small = io.BytesIO()
    Image.open(io.BytesIO(original)).resize((40, 40)).save(small, format="PNG")
    assert hamming(dhash(original), dhash(small.getvalue())) <= 8


def test_different_content_is_far() -> None:
    assert hamming(dhash(_gradient()), dhash(_png((250, 10, 10)))) > 16


def test_fits_signed_bigint() -> None:
    value = dhash(_gradient())
    assert -(2**63) <= value < 2**63


def test_garbage_raises_undecodable() -> None:
    with pytest.raises(UndecodableImage):
        dhash(b"not an image")


def test_hamming_is_symmetric_and_zero_on_self() -> None:
    a, b = dhash(_gradient()), dhash(_png((0, 0, 0)))
    assert hamming(a, a) == 0
    assert hamming(a, b) == hamming(b, a)
```

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_confirm_phash.py -v`
(ModuleNotFoundError).

- [ ] **Step 3: Implement** — `src/imageshield/confirm/phash.py`:

```python
"""64-bit difference hash (dHash) for cross-URL duplicate detection.

A hash about the image, never the image: INVARIANTS #9 stands. Stored as a
signed Postgres BIGINT, so the unsigned 64-bit value is two's-complemented
here and masked back in :func:`hamming`.

Pillow is a dependency for exactly two jobs in this repo — attribution's face
crop and this hash (plus the fetcher's crop/blur, which reuses crop.py).
"""

from __future__ import annotations

import io

from PIL import Image, UnidentifiedImageError

from imageshield.attribution.crop import UndecodableImage

_HASH_SIZE = 8
_MASK = (1 << 64) - 1


def dhash(image: bytes) -> int:
    """Hash of horizontal gradient signs over an 9x8 grayscale downscale."""
    try:
        with Image.open(io.BytesIO(image)) as opened:
            gray = opened.convert("L").resize(
                (_HASH_SIZE + 1, _HASH_SIZE), Image.Resampling.LANCZOS
            )
            pixels = list(gray.getdata())
    except (UnidentifiedImageError, OSError) as exc:
        raise UndecodableImage(str(exc)) from exc

    bits = 0
    for row in range(_HASH_SIZE):
        for col in range(_HASH_SIZE):
            left = pixels[row * (_HASH_SIZE + 1) + col]
            right = pixels[row * (_HASH_SIZE + 1) + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return bits - (1 << 64) if bits >= (1 << 63) else bits


def hamming(a: int, b: int) -> int:
    return ((a ^ b) & _MASK).bit_count()
```

- [ ] **Step 4: Run** — PASS. Also `ruff check src/imageshield/confirm` and
`mypy` (bare — whole package) → clean.

- [ ] **Step 5: Commit** — `feat: 64-bit dHash for cross-URL duplicate detection`

---

### Task 6: SSRF split + the fetcher deployable

**Files:**
- Modify: `src/imageshield/recheck/ssrf.py` (extract `address_refusal`)
- Create: `src/imageshield/fetcher/__init__.py`, `src/imageshield/fetcher/config.py`,
  `src/imageshield/fetcher/fetch.py`, `src/imageshield/fetcher/app.py`
- Test: `tests/test_fetcher.py`; `tests/test_recheck.py` must stay green

**Interfaces:**
- Consumes: `imageshield.attribution.crop.crop_to_face(image: bytes, bbox: BoundingBox) -> bytes`,
  `imageshield.attribution.models.BoundingBox(x, y, w, h)`.
- Produces:

```python
# recheck/ssrf.py — NEW public function; refusal_reason() keeps its exact
# current signature and behaviour, now implemented as allowlist-check + this:
def address_refusal(url: str, resolver: Resolver | None = None) -> RefusalReason | None
    # 'not_an_http_url' | 'dns_failure' | 'dns_empty' | 'unparseable_address'
    # | 'private_address' | None

# fetcher/config.py
class FetcherConfig(BaseSettings):        # frozen, extra="ignore", case-insensitive
    fetcher_token: str                    # required, >=16 chars, sentinel-rejected
    fetch_max_bytes: int = 10 * 1024 * 1024
    fetch_timeout_seconds: float = 10.0
    fetch_max_redirects: int = 2
def load_fetcher_config() -> FetcherConfig     # ConfigError on invalid, like load_config()

# fetcher/fetch.py
class FetchRefused(Exception):            # .code: str, .detail: str
class FetchedImage(BaseModel):            # frozen: content_type: str, body: bytes — in memory ONLY
async def fetch_image(client: httpx.AsyncClient, url: str, *, max_bytes: int,
                      timeout_seconds: float, max_redirects: int,
                      resolver: Resolver | None = None) -> FetchedImage
    # FetchRefused codes: 'refused_private_address' (any ssrf refusal reason),
    # 'not_an_image', 'too_large', 'redirect_limit', 'unfetchable' (transport/4xx/5xx)

# fetcher/app.py
def create_app(config: FetcherConfig | None = None) -> FastAPI
# Routes (X-Fetcher-Token required on /v1/*, constant-time compare):
#   GET  /health                          -> 200 {"status": "ok"} (no dependencies to check)
#   POST /v1/fetch  {"url": str}          -> 200 image bytes, media_type = upstream content-type
#   POST /v1/crop   {"url": str, "bbox": {"x","y","w","h"}, "blur": bool = true}
#                                          -> 200 image/jpeg (crop + GaussianBlur(12) when blur)
# Errors: JSON {"error": {"code", "message"}} with 400 (refused/not image/too large),
#         401 (token), 502 (unfetchable), 413 (too_large).
```

**Why this shape:** the fetcher is ARCHITECTURE §3.7's crop fetcher. It has **no `database_url`
field at all** — the process cannot connect to Postgres even by misconfiguration. It is the only
component that touches hostile bytes; bytes live in one request's memory and are never written
anywhere.

- [ ] **Step 1: Failing tests for the ssrf split** — extend `tests/test_recheck.py` (or a new
section in it): `address_refusal("https://ok.example/x", resolver=fake_global)` is `None`;
`address_refusal` with a resolver returning `169.254.169.254` → `"private_address"`;
`refusal_reason` still refuses a non-allowlisted domain BEFORE resolving (behaviour unchanged —
reuse that file's existing fake resolver helpers, they exist for the current tests).

- [ ] **Step 2: Implement the split.** In `recheck/ssrf.py`, move the scheme-check + DNS-resolve
+ `is_global` loop into `address_refusal(url, resolver)`; `refusal_reason(url, allowed_domains,
resolver)` becomes: allowlist check (unchanged refusals `domain_not_allowlisted`), then `return
address_refusal(url, resolver)`. No behaviour change; run
`python -m pytest tests/test_recheck.py -v` → PASS.

- [ ] **Step 3: Failing fetcher tests** — `tests/test_fetcher.py` (use `TestClient`; inject a
fake transport/resolver via `app.state`, mirroring the repo's app.state injection convention):

```python
from __future__ import annotations

import io

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from imageshield.fetcher.app import create_app
from imageshield.fetcher.config import FetcherConfig

TOKEN = "fetcher-token-for-tests-0003"
AUTH = {"X-Fetcher-Token": TOKEN}


def _config() -> FetcherConfig:
    return FetcherConfig(fetcher_token=TOKEN)


def _png(size: tuple[int, int] = (64, 64)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (120, 30, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


def _client(handler, *, resolver=None) -> TestClient:
    app = create_app(config=_config())
    app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.resolver = resolver or (lambda host: ("93.184.216.34",))  # a global address
    return TestClient(app)


def _image_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=_png(), headers={"content-type": "image/png"})


def test_fetch_requires_the_token() -> None:
    client = _client(_image_handler)
    assert client.post("/v1/fetch", json={"url": "https://x.example/a.png"}).status_code == 401


def test_fetch_returns_the_image_bytes() -> None:
    client = _client(_image_handler)
    response = client.post("/v1/fetch", json={"url": "https://x.example/a.png"}, headers=AUTH)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == _png()


def test_fetch_refuses_private_addresses() -> None:
    client = _client(_image_handler, resolver=lambda host: ("169.254.169.254",))
    response = client.post("/v1/fetch", json={"url": "https://x.example/a.png"}, headers=AUTH)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "refused_private_address"


def test_fetch_refuses_non_image_content_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})

    client = _client(handler)
    response = client.post("/v1/fetch", json={"url": "https://x.example/a"}, headers=AUTH)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "not_an_image"


def test_fetch_caps_the_body_size() -> None:
    big = b"\xff" * (10 * 1024 * 1024 + 1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=big, headers={"content-type": "image/jpeg"})

    client = _client(handler)
    response = client.post("/v1/fetch", json={"url": "https://x.example/big"}, headers=AUTH)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "too_large"


def test_fetch_rechecks_every_redirect_hop() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "x.example":
            return httpx.Response(302, headers={"location": "https://evil.internal/a.png"})
        return httpx.Response(200, content=_png(), headers={"content-type": "image/png"})

    def resolver(host: str) -> tuple[str, ...]:
        return ("10.0.0.5",) if host == "evil.internal" else ("93.184.216.34",)

    client = _client(handler, resolver=resolver)
    response = client.post("/v1/fetch", json={"url": "https://x.example/a.png"}, headers=AUTH)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "refused_private_address"


def test_crop_returns_blurred_jpeg_by_default() -> None:
    client = _client(_image_handler)
    body = {"url": "https://x.example/a.png", "bbox": {"x": 0.25, "y": 0.25, "w": 0.5, "h": 0.5}}
    response = client.post("/v1/crop", json=body, headers=AUTH)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    blurred = Image.open(io.BytesIO(response.content))
    assert blurred.size[0] < 64  # cropped, not the whole frame


def test_health_needs_no_token() -> None:
    assert _client(_image_handler).get("/health").status_code == 200
```

- [ ] **Step 4: Run to verify failure**, then implement.

`fetcher/config.py` — mirror `config.py`'s token validator (≥16 chars, `SENTINEL_VALUES`
rejected — import `SENTINEL_VALUES` and `ConfigError` from `imageshield.config`), plus a
`load_fetcher_config()` that formats `ValidationError` the way `load_config()` does.

`fetcher/fetch.py` — manual redirect walk exactly like `recheck/client.py`'s (statuses
`{301, 302, 303, 307, 308}`), with `address_refusal` applied per hop BEFORE each request; GET via
`client.send(client.build_request("GET", url), stream=True)`; verify `content-type` starts with
`image/` before reading; read `aiter_bytes()` chunks accumulating with the cap; `httpx.HTTPError`
→ `FetchRefused("unfetchable", ...)`; non-2xx (after redirects) → `unfetchable`.

`fetcher/app.py` — FastAPI factory, docs disabled like the main app; request models extend a
local `ServiceModel`-shaped base (`extra="forbid"`); the token dependency mirrors
`http/auth.py`'s `hmac.compare_digest` pattern with header `X-Fetcher-Token`; lifespan opens
`httpx.AsyncClient(follow_redirects=False, limits=httpx.Limits(max_connections=8),
headers={"User-Agent": "ImageShield-Fetcher/1.0"})` unless `app.state.http_client` is pre-wired
(test convention), same for `app.state.resolver`. `/v1/crop`: `fetch_image(...)` → build
`BoundingBox(**bbox)` → `crop_to_face(body, bbox)` (attribution's 25% margin crop; `CropTooSmall`
→ 400 `crop_too_small`) → if `blur`: `Image.open` → `filter(ImageFilter.GaussianBlur(12))` →
JPEG quality 80 → `Response(content=..., media_type="image/jpeg")`.

- [ ] **Step 5: Run** — `python -m pytest tests/test_fetcher.py tests/test_recheck.py -v` → PASS;
`ruff check .` and `mypy` → clean.

- [ ] **Step 6: Commit** — `feat: fetcher deployable — hardened fetch + blurred live crops (ARCH §3.7)`

---

### Task 7: Moderation client + triage classifier

**Files:**
- Create: `src/imageshield/confirm/moderation.py`, `src/imageshield/confirm/triage.py`
- Modify: `pyproject.toml` (TID251 per-file-ignores + ban message)
- Test: `tests/test_confirm_moderation.py`, `tests/test_confirm_triage.py`

**Interfaces:**
- Produces:

```python
# confirm/moderation.py  (boto3 — needs a TID251 per-file ignore)
class ConfirmUnavailable(RuntimeError): ...
class ModerationLabel(BaseModel):        # frozen: name: str, parent_name: str, confidence: float
class ModerationSignal(BaseModel):       # frozen: labels: tuple[ModerationLabel, ...], min_age_low: float | None
class RekognitionModeration:
    def __init__(self, *, region: str, client: Any | None = None) -> None
    async def assess(self, image: bytes) -> ModerationSignal
        # detect_moderation_labels(MinConfidence=60) + detect_faces(Attributes=["AGE_RANGE"]),
        # both via asyncio.to_thread; ClientError -> ConfirmUnavailable

# confirm/triage.py  (pure — no I/O, no boto3)
Severity = Literal["ncii_suspected", "explicit_unmatched", "unassessed",
                   "benign_copy", "likely_not_subject"]
SEVERITY_RANK: dict[str, int]            # ncii 0 ... likely_not_subject 4
EXPLICIT_LABEL_PARENTS = frozenset({"Explicit Nudity", "Explicit"})
EXPLICIT_MIN_CONFIDENCE = 80.0
def is_explicit(labels: Sequence[ModerationLabel]) -> bool
def classify(*, explicit: bool, face_match_score: float | None,
             face_match_threshold: float) -> Severity
def csam_quarantine(*, explicit: bool, min_age_low: float | None,
                    age_low_threshold: int) -> bool
def find_duplicate(new_phash: int, decided: Sequence[tuple[UUID, int]],
                   hamming_max: int) -> UUID | None
```

- [ ] **Step 1: Failing triage tests** — `tests/test_confirm_triage.py` (pure, exhaustive):

```python
def test_explicit_and_matched_is_ncii() -> None:
    assert classify(explicit=True, face_match_score=95.0, face_match_threshold=92.0) == "ncii_suspected"

def test_explicit_unmatched_is_still_reviewed_high() -> None:
    assert classify(explicit=True, face_match_score=None, face_match_threshold=92.0) == "explicit_unmatched"
    assert classify(explicit=True, face_match_score=70.0, face_match_threshold=92.0) == "explicit_unmatched"

def test_benign_copy_when_face_matches_without_nudity() -> None:
    assert classify(explicit=False, face_match_score=93.0, face_match_threshold=92.0) == "benign_copy"

def test_likely_not_subject_when_nothing_matches() -> None:
    assert classify(explicit=False, face_match_score=None, face_match_threshold=92.0) == "likely_not_subject"

def test_is_explicit_needs_parent_and_confidence() -> None:
    strong = ModerationLabel(name="Exposed Genitalia", parent_name="Explicit Nudity", confidence=91.0)
    weak = ModerationLabel(name="Exposed Genitalia", parent_name="Explicit Nudity", confidence=61.0)
    unrelated = ModerationLabel(name="Violence", parent_name="Violence", confidence=99.0)
    top_level = ModerationLabel(name="Explicit Nudity", parent_name="", confidence=95.0)
    assert is_explicit([strong]) and is_explicit([top_level])
    assert not is_explicit([weak]) and not is_explicit([unrelated]) and not is_explicit([])

def test_csam_tripwire_requires_both_signals() -> None:
    assert csam_quarantine(explicit=True, min_age_low=12.0, age_low_threshold=18)
    assert not csam_quarantine(explicit=False, min_age_low=12.0, age_low_threshold=18)
    assert not csam_quarantine(explicit=True, min_age_low=25.0, age_low_threshold=18)
    assert not csam_quarantine(explicit=True, min_age_low=None, age_low_threshold=18)

def test_find_duplicate_honours_hamming_and_order() -> None:
    a, b = uuid4(), uuid4()
    decided = [(a, 0), (b, 1)]
    assert find_duplicate(0, decided, hamming_max=8) == a       # exact beats near
    assert find_duplicate(0b11111, [(b, 0)], hamming_max=4) is None

def test_severity_rank_total_order() -> None:
    assert [k for k, _ in sorted(SEVERITY_RANK.items(), key=lambda kv: kv[1])] == [
        "ncii_suspected", "explicit_unmatched", "unassessed", "benign_copy", "likely_not_subject"
    ]
```

- [ ] **Step 2: Failing moderation tests** — `tests/test_confirm_moderation.py` with a fake
boto3 client object (the repo convention — see `tests/test_attribution_rekognition.py` for the
fake-client shape): `assess` maps `ModerationLabels[].{Name,ParentName,Confidence}` and
`FaceDetails[].AgeRange.Low` (min across faces; `None` when absent); a raised
`botocore.exceptions.ClientError` → `ConfirmUnavailable`.

- [ ] **Step 3: Implement both modules.** `classify` decision table: explicit+match→ncii,
explicit→explicit_unmatched, match→benign_copy, else likely_not_subject (`unassessed` is only
ever assigned by the worker for unfetchable hits — `classify` never returns it).
`find_duplicate`: scan `decided`, return the id with the smallest hamming ≤ `hamming_max`
(ties: first). Moderation client follows `attribution/rekognition.py`'s exact constructor and
`asyncio.to_thread` idiom, `boto3.client("rekognition", region_name=region)`.

`pyproject.toml`: add per-file ignores

```toml
"src/imageshield/confirm/moderation.py" = ["TID251"]
"src/imageshield/confirm/worker.py" = ["TID251"]
```

and extend the TID251 ban message's sanctioned list to name the confirm worker (SQS receive
only) and the moderation client (Rekognition only, never S3).

- [ ] **Step 4: Run** — both new test files + `ruff check .` + `mypy` → PASS/clean.

- [ ] **Step 5: Commit** — `feat: moderation client + pure triage (severity, CSAM tripwire, dedup)`

### Task 8: Confirm store

**Files:**
- Create: `src/imageshield/confirm/store.py`
- Test: `tests/test_confirm_store.py`

**Interfaces:**
- Consumes: 0021 columns/tables; `ConfirmContext` (Task 4); severity vocabulary (Task 7).
- Produces:

```python
class ConfirmStore(Protocol):
    async def load_context(self, infringement_id: UUID) -> ConfirmContext | None: ...
    async def decided_phashes(self, user_ref: UserRef) -> tuple[tuple[UUID, int], ...]: ...
    async def record_duplicate(self, infringement_id: UUID, *, duplicate_of: UUID,
                               phash: int) -> None: ...
    async def record_quarantine(self, infringement_id: UUID, *, phash: int | None,
                                moderation_labels: list[dict[str, Any]],
                                min_age_low: float | None) -> None: ...
    async def record_triage(self, infringement_id: UUID, *, severity: str,
                            phash: int | None, face_match_score: float | None,
                            moderation_labels: list[dict[str, Any]] | None,
                            triage: dict[str, Any]) -> None: ...
    async def record_unfetchable(self, infringement_id: UUID, *, detail: str) -> None: ...
    async def record_skipped(self, infringement_id: UUID, *, reason: str,
                             detail: str) -> None: ...

class PostgresConfirmStore:
    def __init__(self, pool: AsyncConnectionPool) -> None
```

Semantics, each method one transaction:
- `load_context`: infringement row + representative attestation's `last_run_id` (highest
  `provider_score DESC NULLS LAST, provider_id` — same deterministic pick as the 0016 view).
  Returns `None` for an unknown id.
- `decided_phashes`: `SELECT infringement_id, phash FROM infringements WHERE user_ref = %s AND
  phash IS NOT NULL AND confirm_state IN ('confirmed','rejected')` — the partial index from 0021.
- `record_duplicate`: `UPDATE infringements SET confirm_state='duplicate', duplicate_of=%s,
  phash=%s WHERE infringement_id=%s AND confirm_state IN ('unconfirmed','machine_triaged')`;
  also delete any pending `review_tasks` row for it (`DELETE ... WHERE infringement_id=%s AND
  status='pending'`) — a duplicate needs no review.
- `record_quarantine`: `UPDATE infringements SET confirm_state='quarantined', phash=%s,
  moderation_labels=%s ...` + upsert `review_tasks` with `status='quarantined'`,
  `severity='ncii_suspected'`, triage carrying `{"quarantine": true, "min_age_low": ...}` + one
  `audit_log` INSERT (`actor_type='service'`, `action='confirm.quarantined'`,
  `resource_id=infringement_id`, metadata `{"min_age_low": ...}`) + `log.error("confirm.quarantined", ...)`
  from the caller (the log line is the ops alarm hook).
- `record_triage`: `UPDATE infringements SET confirm_state='machine_triaged', severity=%s,
  phash=%s, face_match_score=%s, moderation_labels=%s` + `INSERT INTO review_tasks
  (infringement_id, user_ref, severity, triage) VALUES (...) ON CONFLICT (infringement_id) DO
  UPDATE SET severity=EXCLUDED.severity, triage=EXCLUDED.triage WHERE review_tasks.status='pending'`
  (a decided task is never reopened by a re-run).
- `record_unfetchable`: like `record_triage` but `severity='unassessed'`, triage
  `{"unfetchable": detail}`, no phash/labels.
- `record_skipped`: upserts a `review_tasks` row (`severity='unassessed'`, triage
  `{"skipped": reason, "detail": detail}`) but **leaves `confirm_state='unconfirmed'`** — the hit
  stays reviewable URL-only while the breaker/budget blocks Rekognition (spec §10: nothing waits
  on a broken dependency), and the unchanged state means the next run's completion re-enqueues it
  for a real triage.

- [ ] **Step 1: Failing DB tests** — `tests/test_confirm_store.py`, using the `migrated_db` +
async-pool fixture idiom from `tests/test_search_store.py:36-50`. Seed helper: insert a subject,
`content_urls` row, an infringement (+ image_url), a search run and an attestation pointing at it
(copy the insert helpers already present in `tests/test_svc_views.py` — they build exactly this
chain). Cases: context loads with `run_id`; unknown id → `None`; duplicate transition removes
the pending task and refuses on already-decided rows (rowcount 0 → method returns without
touching); triage upsert updates a pending task's severity but not a decided one; quarantine
writes the audit row (`SELECT action FROM audit_log`); unfetchable lands `severity='unassessed'`;
`record_skipped` creates a pending task while `confirm_state` stays `unconfirmed`; and the
isolation pair from the spec's must-exist list — **`decided_phashes` returns only THIS user's
rows** (seed a second user with a decided phash and assert absence) **and only
`confirmed`/`rejected` states** (a `machine_triaged` row with a phash is not returned, so nothing
can inherit from an undecided hit).

- [ ] **Step 2: Run to verify failure**, **Step 3: implement** (raw SQL, psycopg named params,
`Jsonb()` for jsonb payloads — grep `from psycopg.types.json import Jsonb` in `search/store.py`
for the idiom), **Step 4: run to green**, then:

- [ ] **Step 5: Commit** — `feat: confirm store — triage/duplicate/quarantine transitions`

---

### Task 9: Confirm worker

**Files:**
- Create: `src/imageshield/confirm/worker.py`
- Test: `tests/test_confirm_worker.py`

**Interfaces:**
- Consumes: `ConfirmStore` (Task 8), `RekognitionModeration.assess` (Task 7), triage functions
  (Task 7), `dhash`/`hamming` (Task 5), attribution primitives
  (`RekognitionFaceAttribution.detect_faces/search_face` + `resolve_face` — exact signatures in
  `src/imageshield/attribution/{rekognition,resolve}.py`), the provider gate
  (`providers.gate.decide`) and `ProviderControlStore.record_outcome/record_skip`, the fetcher
  HTTP API (Task 6).
- Produces:

```python
# confirm/worker.py
async def handle_message(body: str, deps: ConfirmDeps, *, logger=None) -> bool
    # True = delete message; False = leave for redelivery (crash-shaped errors only)
class ConfirmDeps(BaseModel):  # arbitrary_types_allowed; carries store, control, provider,
                               # moderation, fetch (callable), cfg-derived thresholds
async def run_forever(config: Config, *, consumer: SqsConsumer | None = None) -> None
def main() -> int              # python -m imageshield.confirm.worker
```

**The orchestration, in order (each numbered branch returns True unless stated):**

1. Parse `OutboxPayload`; wrong event / malformed → poison pill, log, True. Load context; `None`
   or `confirm_state` not in `('unconfirmed','machine_triaged')` → already handled, True.
2. No `image_url` on the hit → `record_unfetchable(detail="no image_url recorded")`.
3. **Fetch** via the fetcher service (`POST {fetcher_base_url}/v1/fetch`, header
   `X-Fetcher-Token`, body `{"url": image_url}`, `httpx.AsyncClient` owned by the worker).
   Non-200 → `record_unfetchable(detail=...)`.
4. **pHash + dedup before any AWS spend:** `dhash(bytes)`; `find_duplicate(new, decided, max)` →
   `record_duplicate` and stop (no Rekognition call, no cost). `UndecodableImage` →
   `record_unfetchable(detail="undecodable")`.
5. **Gate:** `decision = await gate.decide(REKOGNITION_CONFIRM_ID, runtime=runtimes.get(...),
   store=control, now=utcnow)`. `Skip` → `control.record_skip(ctx.run_id, ..., reason, detail)`
   (guard: `ctx.run_id` is `UUID | None`; when `None` — a provenance gap that the write path makes
   near-impossible — skip the provider_calls record and log a warning instead) **plus**
   `store.record_skipped(infringement_id, reason=..., detail=...)` so the hit is reviewable
   URL-only immediately, then True — the state stays `unconfirmed`, so the next search run's
   completion re-enqueues it (Task 10's SQL), which is the retry path. The same `run_id is None`
   guard applies to step 7's `record_outcome`.
6. **Rekognition bundle** (all inside one try): `detect_faces(bytes)` → sort faces by bbox area
   desc, take `confirm_max_faces`; per face `search_face(bytes, face,
   collection_id=cfg.identity_collection, match_threshold=cfg.confirm_face_match_threshold,
   max_candidates=cfg.attribution_max_candidates)` → `resolve_face(face, matches,
   (ctx.user_ref,))`; best `match_score` among resolved (None when nobody matched);
   `moderation.assess(bytes)`. On `AttributionUnavailable`/`ConfirmUnavailable` → build a failed
   `ProviderResult(status="error", ...)`, `record_outcome`, and return **False** (redelivery —
   transient AWS trouble is crash-shaped).
7. `record_outcome(ctx.run_id, ProviderResult(provider_id=REKOGNITION_CONFIRM_ID, status="ok",
   matches=[], raw_response={"faces_searched": n, "face_match_score": ...,
   "moderation_label_count": len(labels), "min_age_low": ...}, http_status=None,
   latency_ms=..., attempts=1), cost_usd=runtime.cost_per_call_usd, spend_date=...,
   probe=decision.probe)`. Raw response carries **no URLs and no label text** beyond counts —
   provider_calls is retained evidence, keep it lean; the labels live on the infringement row.
8. **CSAM tripwire:** `csam_quarantine(...)` → `record_quarantine` (which audit-logs) +
   `log.error("confirm.quarantined", infringement_id=...)`; stop. No score effect.
9. `classify(...)` → `record_triage(severity=..., phash=..., face_match_score=...,
   moderation_labels=[label dicts], triage={"image_url": ..., "best_face_bbox":
   bbox.as_dict() or None, "face_match_score": ..., "moderation_labels": [names]})`.

`run_forever`/`main`: copy the skeleton of `search/worker.py:181-260` (same `SqsConsumer`
protocol import, `_localstack_endpoint_url` idiom, SIGTERM/SIGINT, `asyncio.to_thread` around
receive/delete, Windows `SelectorEventLoop` branch), queue URL `config.sqs_confirm_hits_url`.
Construct: pool → `PostgresConfirmStore`, `build_control_store(config, pool)`,
`RekognitionFaceAttribution(region=config.aws_region)`,
`RekognitionModeration(region=config.aws_region)`, one `httpx.AsyncClient` for the fetcher.

- [ ] **Step 1: Failing tests** — `tests/test_confirm_worker.py`, everything faked in memory
(fake ConfirmStore recording calls, fake control store, fake attribution provider returning
canned `DetectedFace`/`FaceMatch` tuples, fake moderation, fake fetch callable). Cases at
minimum: poison pill deletes; already-decided context deletes without side effects; duplicate
short-circuits BEFORE the fake provider is touched (assert provider.calls == 0); budget skip
records the skip and leaves state unconfirmed; happy path ncii → record_triage with
severity `ncii_suspected`; csam path → record_quarantine and NO record_triage;
`AttributionUnavailable` → returns False after record_outcome(status="error").

- [ ] **Step 2–4:** red → implement → green (`python -m pytest tests/test_confirm_worker.py -v`;
`ruff`; `mypy`).

- [ ] **Step 5: Commit** — `feat: confirm worker — fetch, dedup-first, gated Rekognition bundle, triage`

---

### Task 10: Enqueue confirm jobs at run completion

**Files:**
- Modify: `src/imageshield/search/store.py` (`complete_run`), `src/imageshield/search/runner.py`
  (`execute_run`), `src/imageshield/search/worker.py` (build criteria)
- Test: `tests/test_search_store.py`, `tests/test_search_runner.py` (extend)

**Interfaces:**
- `PostgresSearchStore.complete_run` gains a keyword arg, protocol updated identically:

```python
async def complete_run(self, run_id: UUID, seed_id: UUID,
                       providers_succeeded: Sequence[ProviderId], *,
                       retier: CadenceInput | None,
                       confirm: ConfirmCriteria | None) -> CadenceUpdate | None
```

- `execute_run(claim, providers, store, policy, control, cadence, *, confirm, now=None)` threads
  it through; `search/worker.py` builds `ConfirmCriteria(hive_min_score=
  Decimal(str(config.confirm_hive_min_score)), google_kinds=config.confirm_google_kind_set)`
  once at boot and passes it to every `execute_run`.

- [ ] **Step 1: Failing store test** (in `tests/test_search_store.py`, reusing its seeded-run
fixtures): complete a run whose attestations include a hive score above the floor → exactly one
outbox row `queue_name='confirm:hits'` with payload `{"event": "confirm.hit_requested", "id":
str(infringement_id)}`; a below-floor hive score and a `page_match` google category enqueue
nothing; an infringement already `machine_triaged` (UPDATE it first) IS re-enqueued? — **No**:
assert only `confirm_state='unconfirmed'` rows enqueue (a triaged hit already has a pending
review task; re-running Rekognition on it is pure spend). Two qualifying attestations on one hit
→ ONE outbox row.

- [ ] **Step 2: Implement.** Inside `complete_run`'s existing single transaction, after
`_COMPLETE_RUN_SQL`, when `confirm is not None`:

```sql
INSERT INTO outbox (queue_name, payload)
SELECT DISTINCT 'confirm:hits',
       jsonb_build_object('event', 'confirm.hit_requested', 'id', i.infringement_id)
FROM infringements i
JOIN attestations a ON a.infringement_id = i.infringement_id
WHERE a.last_run_id = %(run_id)s
  AND i.confirm_state = 'unconfirmed'
  AND (
        (a.provider_id = 'hive'
         AND a.provider_score IS NOT NULL
         AND a.provider_score >= %(hive_min)s)
     OR (a.provider_id = 'google'
         AND a.provider_category = ANY(%(google_kinds)s))
  )
```

(`google_kinds` passed as `list(...)`; `hive_min` as `Decimal`.) The transactional-outbox
property holds: run completion, cadence update, and the confirm enqueues commit together —
INVARIANTS #39's shape.

Thread the parameter through `runner.execute_run` (update its docstring and the call in
`worker.handle_message`); fix the signatures pinned in `tests/test_search_runner.py` /
`tests/test_search_worker.py` (pass `confirm=None` where those tests don't care).

- [ ] **Step 3: Run** — `python -m pytest tests/test_search_store.py tests/test_search_runner.py tests/test_search_worker.py tests/test_outbox.py -v` → PASS.

- [ ] **Step 4: Commit** — `feat: run completion enqueues most-similar review hits to confirm:hits`

### Task 11: Score engine + recommendations catalog (pure)

**Files:**
- Create: `src/imageshield/score/__init__.py`, `src/imageshield/score/engine.py`,
  `src/imageshield/recommendations/__init__.py`, `src/imageshield/recommendations/catalog.py`
- Test: `tests/test_score_engine.py`, `tests/test_recommendations.py`

**Interfaces:**
- Produces (`score/engine.py` — pure, no I/O):

```python
class ConfirmedHit(BaseModel):     # frozen
    severity: str | None           # 0021 vocabulary or None
    counts: bool                   # live AND user hasn't authorised/dismissed AND not not_me-suspended

class ThreatPenalty(BaseModel):    # frozen
    penalty: Decimal
    age_days: int
    decay_days: int
    is_global: bool

class ScoreState(BaseModel):       # frozen — everything compute() needs, nothing else
    enrolment_active: bool
    seed_count: int
    seeds_fresh: bool              # a seed was added within score_seed_fresh_days
    has_overdue_scan: bool         # an active seed's next_scan_after is > grace days past due
    monitored_sources: int
    confirmed_hits: tuple[ConfirmedHit, ...]
    awaiting_feedback_count: int   # counting confirmed hits with no feedback row at all
    aged_open_recs: int            # open recommendations older than score_rec_soft_age_days
    threats: tuple[ThreatPenalty, ...]

class ScoreWeights(BaseModel):     # frozen; one field per SCORE_* config knob
    @classmethod
    def from_config(cls, cfg: Config) -> ScoreWeights

class Components(BaseModel):       # frozen
    posture: int; coverage: int; exposure: int; threat: int
    @property
    def total(self) -> int         # clamped 0..100
    def as_dict(self) -> dict[str, int]

def compute(state: ScoreState, w: ScoreWeights) -> Components
def exposure_weight(severity: str | None, w: ScoreWeights) -> int
```

The arithmetic (this IS the definition — implement exactly):

```python
def exposure_weight(severity, w):
    return {
        "ncii_suspected": w.exposure_weight_ncii,
        "explicit_unmatched": w.exposure_weight_explicit,
        "benign_copy": w.exposure_weight_benign,
    }.get(severity or "", w.exposure_weight_default)   # unassessed/likely_not_subject/None -> default

def compute(state, w):
    posture = w.posture_enrolment if state.enrolment_active else 0
    seed_pts = round(w.posture_seeds * min(state.seed_count, w.seed_target) / w.seed_target)
    if state.seed_count and not state.seeds_fresh:
        seed_pts = min(seed_pts, w.posture_seeds // 2)      # stale portfolio caps at half
    posture += seed_pts
    posture += w.posture_feedback if state.awaiting_feedback_count == 0 else 0
    posture += max(0, w.posture_recommendations - 2 * state.aged_open_recs)

    coverage = w.coverage_scan if (state.seed_count > 0 and not state.has_overdue_scan) else 0
    coverage += round(w.coverage_providers * min(state.monitored_sources, 2) / 2)

    spent = sum(exposure_weight(h.severity, w) for h in state.confirmed_hits if h.counts)
    exposure = max(0, w.weight_exposure - spent)

    global_burn = Decimal(0)
    targeted_burn = Decimal(0)
    for t in state.threats:
        factor = Decimal(max(0.0, 1.0 - t.age_days / t.decay_days)).quantize(Decimal("0.01"))
        burn = t.penalty * factor
        if t.is_global:
            global_burn += burn
        else:
            targeted_burn += burn
    global_burn = min(global_burn, Decimal(w.threat_global_max_penalty))
    threat = max(0, w.weight_threat - int(targeted_burn + global_burn))

    return Components(posture=posture, coverage=coverage, exposure=exposure, threat=threat)
```

- Produces (`recommendations/catalog.py` — pure):

```python
RecKind = Literal["complete_enrolment", "add_seed_photos", "refresh_seeds",
                  "respond_to_hits", "run_priority_scan"]

class RecSpec(BaseModel):          # frozen
    kind: RecKind
    params: dict[str, Any]
    source_event_id: UUID | None = None
    expires_at: datetime | None = None

class EventNeedingScan(BaseModel): # frozen — an active matched event with no completed run
    event_id: UUID                 #   after its starts_at
    expires_at: datetime

def desired(state: ScoreState, events_needing_scan: Sequence[EventNeedingScan],
            w: ScoreWeights) -> tuple[RecSpec, ...]
```

Rules for `desired` (deterministic, order fixed):
1. `not state.enrolment_active` → `complete_enrolment {}`
2. `state.seed_count < w.seed_target` → `add_seed_photos {"target": w.seed_target,
   "have": state.seed_count}`
3. `state.seed_count > 0 and not state.seeds_fresh` → `refresh_seeds
   {"fresh_days": w.seed_fresh_days}`
4. `state.awaiting_feedback_count > 0` → `respond_to_hits {"count": ...}`
5. per `EventNeedingScan` → `run_priority_scan {"event_id": str(...)}` with
   `source_event_id`/`expires_at` set.

**Sync rule (implemented in Task 12's store, stated here because both tests assert it):** an
open recommendation whose `(kind, source_event_id)` is NOT in `desired(...)` any more is
*satisfied* → `completed`. Open + past `expires_at` → `expired`. A `dismissed` row with the same
`(kind, source_event_id)` blocks re-insertion forever. Only rows in `desired` and not blocked
and not already open get inserted.

- [ ] **Step 1: Failing engine tests** — `tests/test_score_engine.py`. Build `ScoreWeights` from
`make_config()` (conftest helper). Must include, with exact expected integers:

```python
def _weights() -> ScoreWeights:
    return ScoreWeights.from_config(make_config())

def test_full_marks_is_100() -> None:
    state = ScoreState(enrolment_active=True, seed_count=5, seeds_fresh=True,
                       has_overdue_scan=False, monitored_sources=2, confirmed_hits=(),
                       awaiting_feedback_count=0, aged_open_recs=0, threats=())
    c = compute(state, _weights())
    assert (c.posture, c.coverage, c.exposure, c.threat) == (40, 25, 25, 10)
    assert c.total == 100

def test_empty_person_scores_only_defaults() -> None:
    state = ScoreState(enrolment_active=False, seed_count=0, seeds_fresh=False,
                       has_overdue_scan=False, monitored_sources=0, confirmed_hits=(),
                       awaiting_feedback_count=0, aged_open_recs=0, threats=())
    c = compute(state, _weights())
    # no enrolment, no seeds, no scans -> posture 0+0+5+5, coverage 0, exposure/threat full
    assert (c.posture, c.coverage, c.exposure, c.threat) == (10, 0, 25, 10)

def test_ncii_hit_costs_twelve_and_dead_url_restores_it() -> None:
    live = ConfirmedHit(severity="ncii_suspected", counts=True)
    dead = ConfirmedHit(severity="ncii_suspected", counts=False)
    base = ...  # full-marks state as above
    assert compute(base.model_copy(update={"confirmed_hits": (live,)}), _weights()).exposure == 13
    assert compute(base.model_copy(update={"confirmed_hits": (dead,)}), _weights()).exposure == 25

def test_exposure_floors_at_zero() -> None:
    hits = tuple(ConfirmedHit(severity="ncii_suspected", counts=True) for _ in range(4))
    assert compute(base.model_copy(update={"confirmed_hits": hits}), _weights()).exposure == 0

def test_stale_seeds_cap_at_half() -> None: ...      # seed_count=5, seeds_fresh=False -> seed_pts 10
def test_threat_decays_linearly() -> None: ...       # penalty 4, age 5, decay 10 -> burn 2 -> threat 8
def test_global_threat_is_capped() -> None: ...      # global penalty 9 -> capped at 2 -> threat 8
def test_aged_recs_bleed_posture() -> None: ...      # 1 aged -> recs pts 3; 3 aged -> 0
```

- [ ] **Step 2: Failing catalog tests** — `tests/test_recommendations.py`: each rule fires and
clears on the right state; event rec carries `source_event_id` + `expires_at`; output order is
the rule order; a fully-set-up state desires nothing.

- [ ] **Step 3: Implement both modules exactly as specified.**
- [ ] **Step 4: Run both files + ruff + mypy** → green.
- [ ] **Step 5: Commit** — `feat: pure score engine and recommendation rules`

---

### Task 12: Score store — the ONE writer — and the tick process

**Files:**
- Create: `src/imageshield/score/store.py`, `src/imageshield/score/tick.py`
- Modify: `tests/test_boundaries.py` (single-writer gate)
- Test: `tests/test_score_store.py`, `tests/test_score_tick.py`

**Interfaces:**
- Produces:

```python
# score/store.py
class ScoreResult(BaseModel):      # frozen
    score: int
    components: Components
    changed: bool                  # False when recompute was a no-op

class ScoreStore(Protocol):
    async def recompute(self, user_ref: UserRef, *, cause_kind: str,
                        cause_ref: str | None = None,
                        now: datetime | None = None) -> ScoreResult | None: ...
    async def get_score(self, user_ref: UserRef) -> dict[str, Any] | None: ...
    async def list_events(self, user_ref: UserRef, *, limit: int = 50) -> list[dict[str, Any]]: ...
    async def all_subject_refs(self) -> tuple[UserRef, ...]: ...
    async def expire_due_threat_events(self, *, now: datetime) -> int: ...

class PostgresScoreStore:
    def __init__(self, pool: AsyncConnectionPool, *, weights: ScoreWeights,
                 config_version: str) -> None

# score/tick.py
async def run_forever(config: Config) -> None      # recheck-style poll loop
def main() -> int                                  # python -m imageshield.score.tick
```

**`recompute` — ONE transaction, this exact order:**
1. `SELECT ... FROM subjects WHERE user_ref = %s` — absent → return `None` (no score for an
   unknown subject; never invent one).
2. `SELECT score, components FROM protection_scores WHERE user_ref = %s FOR UPDATE` (row may be
   absent — baseline is all-zero components, score 0).
3. Load state (all reads inside the same txn):
   - enrolment: `EXISTS (SELECT 1 FROM enrolments WHERE user_ref=%s AND status='active')`
   - seeds: `count(*)`, `bool_or(created_at > now() - make_interval(days => %(fresh)s))`,
     `bool_or(next_scan_after IS NOT NULL AND next_scan_after < now() - make_interval(days => %(grace)s))`
     over `search_seeds WHERE user_ref=%s AND status='active'`
   - monitored: the 0016 summary subquery shape (distinct enabled providers across completed runs)
   - confirmed hits: per `infringements WHERE user_ref=%s AND confirm_state='confirmed'`,
     with `counts = url_alive AND status NOT IN ('dismissed_not_me','authorised') AND
     (latest feedback signal IS DISTINCT FROM 'not_me')` (latest via the 0016 DISTINCT ON idiom)
   - awaiting_feedback: those counting hits with zero feedback rows
   - threats: active, unexpired, matched events:
     `SELECT e.penalty, e.decay_days, e.is_global,
             GREATEST(0, EXTRACT(epoch FROM now() - e.starts_at) / 86400)::int AS age_days
      FROM threat_event_matches m JOIN threat_events e USING (event_id)
      WHERE m.user_ref=%s AND e.status='active' AND e.expires_at > now()`
   - events needing scan (for the catalog): matched active events with no
     `search_runs` row for this user `status='completed' AND completed_at > e.starts_at`
4. **Recommendation sync** (writes; see Task 11's sync rule): expire, complete, insert. Then
   re-read `aged_open_recs` so the computed posture reflects the sync.
5. `compute(state, weights)` → diff against stored components per component in the fixed order
   `posture, coverage, exposure, threat`; for each nonzero delta INSERT a `score_events` row with
   a running `score_after`; final `score_after` == new total.
6. Upsert `protection_scores` (`INSERT ... ON CONFLICT (user_ref) DO UPDATE SET score, components,
   config_version, computed_at = now()`).
7. Return `ScoreResult(changed=any deltas)`. No deltas → **write nothing** (idempotence).

**`tick.py`:** copy `recheck/worker.py`'s loop skeleton (signal handling, `asyncio.wait_for` on a
stop event, one pass must not kill the sweep). One pass = `expire_due_threat_events(now)` (an
`UPDATE threat_events SET status='expired', updated_at=now() WHERE status='active' AND
expires_at <= now()`), then `for user_ref in all_subject_refs(): recompute(user_ref,
cause_kind="tick")`. Interval `config.score_tick_interval_seconds`.

- [ ] **Step 1: Failing boundary gate.** Add to `tests/test_boundaries.py`, beside the existing
gates (same file conventions):

```python
SCORE_WRITE = re.compile(r"INSERT\s+INTO\s+(protection_scores|score_events)", re.IGNORECASE)

def test_only_the_score_store_writes_the_score() -> None:
    """INVARIANTS #21 extended: exactly one code path writes a score."""
    allowed = SRC / "imageshield" / "score" / "store.py"
    offenders = [
        str(path)
        for path in _source_files()
        if path != allowed and SCORE_WRITE.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
```

- [ ] **Step 2: Failing store tests** — `tests/test_score_store.py` (DB, `migrated_db` + pool
fixture; seed with the same helper chain as Task 8's tests plus `ensure_subject` from
`tests/db.py`). The spec's must-exist list, verbatim as tests:

```python
async def test_journal_sums_to_the_materialized_score(...) -> None:
    # recompute on a seeded person; assert sum(delta) == protection_scores.score
    # and the last score_after equals it too.

async def test_recompute_twice_writes_nothing_new(...) -> None:

async def test_user_feedback_never_lowers_the_score(...) -> None:
    # confirmed ncii hit -> recompute -> score X
    # write feedback 'not_me' (via PostgresSearchStore.record_feedback, the real path)
    # -> recompute -> score >= X;  same for 'authorised'

async def test_dead_url_restores_exposure(...) -> None:

async def test_unknown_subject_returns_none_and_writes_nothing(...) -> None:

async def test_recommendation_lifecycle(...) -> None:
    # empty person -> open add_seed_photos; add 5 seeds -> recompute -> completed
    # dismissed rec is never re-inserted

async def test_config_version_stamped_on_every_journal_row(...) -> None:
```

- [ ] **Step 3: Implement store + tick.** SQL in module constants like every other store; one
`conn.transaction()` for recompute; `Jsonb` for components/params.

- [ ] **Step 4: Failing tick test** — `tests/test_score_tick.py`: a single `run_once`-style pass
(factor the pass body into `async def run_once(store) -> None` for testability) expires a due
event and recomputes every subject (fake store recording calls).

- [ ] **Step 5: Run** — the three new test files + `tests/test_boundaries.py` → PASS.
- [ ] **Step 6: Commit** — `feat: score store (one writer, journaled diffs) + tick process`

### Task 13: Trigger wiring — recompute after every cause

**Files:**
- Modify: `src/imageshield/http/deps.py`, `src/imageshield/http/app.py`,
  `src/imageshield/http/routes/infringements.py`, `src/imageshield/http/routes/liveness.py`,
  `src/imageshield/http/routes/search.py`, `src/imageshield/http/routes/attribution.py`,
  `src/imageshield/search/worker.py`
- Test: `tests/test_infringement_feedback.py`, `tests/test_search_routes.py`,
  `tests/test_attribution_routes.py`, `tests/test_liveness_routes.py`, `tests/test_search_worker.py` (extend)

**Interfaces:**
- Consumes: `ScoreStore.recompute` (Task 12).
- Produces: `http/deps.py::get_score_store(request) -> ScoreStore` (the `_required_state` idiom);
  `app.state.score_store` wired in `_lifespan` with
  `PostgresScoreStore(pool, weights=ScoreWeights.from_config(cfg), config_version=cfg.score_config_version)`
  behind the existing `getattr(...) is None` guard so tests can pre-wire fakes.

Cause vocabulary (exact `cause_kind` strings — the journal is user-facing, keep them stable):
`"feedback"`, `"enrolment"`, `"seed_registered"`, `"run_completed"`, `"review_decision"`,
`"threat_event"`, `"threat_retracted"`, `"tick"`.

Wiring, one line-cluster per trigger, always AFTER the trigger's own transaction returned
success, always wrapped so a recompute failure never fails the user's request (the tick heals):

```python
try:
    await score_store.recompute(user_ref, cause_kind="feedback", cause_ref=str(infringement_id))
except Exception:  # deliberate: the trigger already committed; tick will heal
    log.warning("score.recompute_failed", user_ref=str(user_ref), cause="feedback")
```

- Feedback route (`infringements.py::record_feedback`) — after a non-None status.
- Liveness result route (`liveness.py`) — on the enrolment-success path only (find where the
  store reports the enrolment was created; recompute with `cause_kind="enrolment"`).
- Seed creation (`search.py::create_seed` handler) — `cause_kind="seed_registered"`.
- Attribution route (`attribution.py`) — once per distinct `RegisteredSeed.user_ref` in the
  outcome, `cause_kind="seed_registered"`.
- Search worker (`search/worker.py::handle_message`) — after a successful `execute_run`,
  `recompute(claim.user_ref, cause_kind="run_completed", cause_ref=str(claim.run_id))`; the
  worker constructs its own `PostgresScoreStore` in `run_forever` and threads it into
  `handle_message` (extend the signature; update `tests/test_search_worker.py` fakes).

- [ ] **Step 1: Failing tests.** In each route-test file, extend the existing happy-path test (or
add one) with a `FakeScoreStore` on `app.state.score_store` recording
`(user_ref, cause_kind)` tuples, asserting exactly one recompute with the right cause; plus one
test that a raising fake does NOT change the route's response code. In
`tests/test_search_worker.py`: the fake score store sees `run_completed` after a handled message.

- [ ] **Step 2: Implement**, **Step 3: run those five files**, then:
- [ ] **Step 4: Commit** — `feat: score recompute triggers on feedback/enrolment/seeds/runs`

---

### Task 14: Threat events — store, matcher, admin routes

**Files:**
- Create: `src/imageshield/threats/__init__.py`, `src/imageshield/threats/store.py`,
  `src/imageshield/http/routes/admin_threat_events.py`
- Modify: `src/imageshield/http/models.py`, `src/imageshield/http/deps.py`,
  `src/imageshield/http/app.py`
- Test: `tests/test_threats.py` (store), `tests/test_admin_threat_routes.py` (routes)

**Interfaces:**
- Produces (`threats/store.py`):

```python
THREAT_CREATED_ACTION = "threat_event.created"
THREAT_RETRACTED_ACTION = "threat_event.retracted"

class ThreatStore(Protocol):
    async def create_event(self, *, kind: str, title: str, body: str, severity: int,
                           domains: tuple[str, ...], is_global: bool, penalty: Decimal,
                           expires_at: datetime, decay_days: int,
                           operator: str) -> tuple[UUID, tuple[UserRef, ...]]: ...
    async def retract_event(self, event_id: UUID, *, operator: str,
                            reason: str) -> tuple[UserRef, ...] | None: ...
    async def list_events(self, *, limit: int = 50) -> list[dict[str, Any]]: ...

class PostgresThreatStore:
    def __init__(self, pool: AsyncConnectionPool) -> None
```

`create_event` — ONE transaction: INSERT the event (`status='active'`,
`created_by=operator`); materialise matches:

```sql
INSERT INTO threat_event_matches (event_id, user_ref, matched_via, penalty_applied)
SELECT DISTINCT ON (i.user_ref) %(event_id)s, i.user_ref, c.source_domain, %(penalty)s
FROM infringements i
JOIN content_urls c ON c.url_hash = i.url_hash
WHERE c.source_domain = ANY(%(domains)s) AND i.url_alive
ORDER BY i.user_ref, c.source_domain
ON CONFLICT DO NOTHING
```

plus, when `is_global`: `INSERT ... SELECT %(event_id)s, user_ref, 'global', %(penalty)s FROM
subjects ON CONFLICT DO NOTHING`; one `audit_log` row (`actor_type='operator'`,
`action=THREAT_CREATED_ACTION`, `resource_id=event_id`,
`metadata={"operator": ..., "title": ..., "matched": n}`). Returns the matched `user_ref`s.

`retract_event` — ONE transaction: `UPDATE threat_events SET status='retracted',
updated_at=now() WHERE event_id=%s AND status='active'` (rowcount 0 → return `None`); audit row
with the reason. Returns matched user_refs (they need recomputing — the engine reads only
`status='active'`, so retraction reverses through recompute, which is what makes the
"restores exactly" test below pass).

- Produces (route file): router
  `APIRouter(prefix="/v1/admin/threat-events", dependencies=[Depends(require_service_token),
  Depends(require_admin_service_token)])` with:
  - `POST ""` — body `ThreatEventCreateRequest(ServiceModel)`: `kind:
    Literal["leak","deepfake_wave","platform_incident","other"]`, `title: str`, `body: str = ""`,
    `severity: int` (1–5 via `Field(ge=1, le=5)`), `domains: tuple[str, ...] = ()`, `is_global:
    bool = False`, `penalty: Decimal` (Field gt=0, decimal string over HTTP), `expires_at:
    datetime`, `decay_days: int` (gt 0), `operator: str` (min_length 1). Response
    `{event_id, matched_count}`, 201. After the store call: chunked recompute loop over matched
    refs (`cause_kind="threat_event"`, `cause_ref=str(event_id)`), same
    swallow-and-log wrapper as Task 13.
  - `POST "/{event_id}/retract"` — body `{operator, reason}`; 404 `ServiceError` when the store
    returns `None`; recompute loop with `cause_kind="threat_retracted"`.
  - `GET ""` — list for the console.
- `deps.py::get_threat_store`, `app.state.threat_store` wiring, router included in `app.py`.

- [ ] **Step 1: Failing store tests** (`tests/test_threats.py`, DB): domain match materialises
exactly the users with live hits on those domains (seed two users, one matching); global matches
everyone in `subjects`; audit rows land with the operator; retract flips status once and returns
`None` the second time; **the reversal test** — full-marks user + event → recompute → score
drops; retract → recompute → score EXACTLY the pre-event value (spec test list: "Event
retraction restores exactly what it took").

- [ ] **Step 2: Failing route tests** (`tests/test_admin_threat_routes.py`, TestClient + fake
threat store + fake score store on `app.state`): 401 without both tokens (auth-coverage walks it
too); create returns 201 + matched_count and triggers one recompute per matched ref; retract 404
on unknown; `extra='forbid'` rejects an unknown field (422).

- [ ] **Step 3–4: implement, run to green** (both files + `tests/test_route_auth_coverage.py`).
- [ ] **Step 5: Commit** — `feat: threat events — admin CRUD, domain matcher, journal-reversible retraction`

---

### Task 15: Review decisions + score/journal admin reads

**Files:**
- Create: `src/imageshield/review/__init__.py`, `src/imageshield/review/store.py`,
  `src/imageshield/http/routes/admin_review.py`, `src/imageshield/http/routes/admin_scores.py`
- Modify: `src/imageshield/http/models.py`, `src/imageshield/http/deps.py`, `src/imageshield/http/app.py`
- Test: `tests/test_review.py`, `tests/test_admin_review_routes.py`, `tests/test_admin_scores_routes.py`

**Interfaces:**
- Produces (`review/store.py`):

```python
REVIEW_DECIDED_ACTION = "review.decided"

class DecisionOutcome(BaseModel):   # frozen
    infringement_id: UUID
    user_ref: UserRef
    decision: str                    # confirmed | rejected | uncertain
    severity: str | None

class ReviewStore(Protocol):
    async def next_task(self) -> dict[str, Any] | None: ...
    async def queue_depth(self) -> dict[str, int]: ...          # per-severity pending counts
    async def decide(self, task_id: UUID, *, decision: str, operator: str,
                     severity: str | None) -> DecisionOutcome | None: ...
```

`next_task`: highest-priority pending row (the 0021 partial index order: severity rank, then
`created_at`), joined to `infringements` for `image_url`/`page_url`/`face_match_score` and to
`content_urls` for `source_domain` — everything the console screen needs, including
`triage->>'best_face_bbox'`.

`decide` — ONE transaction (this is the #19 moment):
1. `SELECT ... FROM review_tasks WHERE task_id=%s AND status='pending' FOR UPDATE` → absent/decided → `None`.
2. `decision == "uncertain"` → keep status `pending`, write ONLY the audit row
   (`actor_type='operator'`, action `REVIEW_DECIDED_ACTION`,
   `metadata={"decision": "uncertain", "operator": ...}`) and return the outcome — the task
   stays queued, no timeout auto-promotes.
3. Else UPDATE the task (`status='decided'`, decision, `decided_by=operator`,
   `decided_at=now()`), then UPDATE the infringement:
   `confirm_state = 'confirmed' | 'rejected'`, `severity = COALESCE(%(override)s, severity)`,
   `confirm_decided_by=%(operator)s, confirm_decided_at=now()`.
   The 0021 CHECK makes a NULL operator impossible to commit.
4. Audit row with `{"decision", "severity", "operator"}`; return the outcome.

- Produces (routes):
  - `admin_review.py`: router `prefix="/v1/admin/review"`, both token deps.
    `GET /next` → 200 task JSON or 204; `GET /queue` → per-severity depths;
    `POST /{task_id}/decision` — body `ReviewDecisionRequest(ServiceModel)`:
    `decision: Literal["confirmed","rejected","uncertain"]`, `operator: str` (min 1),
    `severity: Literal["ncii_suspected","explicit_unmatched","unassessed","benign_copy",
    "likely_not_subject"] | None = None`. 404 when store returns `None`. On
    `confirmed`/`rejected`: recompute (`cause_kind="review_decision"`,
    `cause_ref=str(infringement_id)`), Task-13 wrapper; `uncertain` recomputes nothing.
  - `admin_scores.py`: router `prefix="/v1/admin/scores"`, both deps.
    `GET /{user_ref}` → `{"score": get_score(...), "events": list_events(..., limit=50)}`,
    404 when no score row.
- `deps.py::get_review_store`, `app.state.review_store` wiring.

- [ ] **Step 1: Failing store tests** (`tests/test_review.py`, DB): ordering (seed an
`ncii_suspected` after a `benign_copy`; `next_task` returns the ncii one); decide confirms →
infringement `confirm_state='confirmed'`, severity override applied, decided_by set; deciding a
decided task → `None`; **uncertain keeps it pending** and next_task still returns it; the DB
CHECK itself: a direct SQL `UPDATE infringements SET confirm_state='confirmed'` without
decided_by raises `CheckViolation` (already covered in Task 1 — here assert the store path never
trips it).

- [ ] **Step 2: Failing route tests**: 401s; decide → recompute called with
`review_decision`; uncertain → no recompute; 404 on unknown task; 422 on a bogus severity.

- [ ] **Step 3–4: implement, run to green.**
- [ ] **Step 5: Commit** — `feat: review decisions — human-only confirm, severity override, audit`

### Task 16: Migration 0023 — svc contract views v2

**Files:**
- Create: `migrations/0023_svc_score_views.up.sql`, `migrations/0023_svc_score_views.down.sql`
- Modify: `src/imageshield/http/svc_contract.py`, `tests/test_svc_views.py`, `tests/test_readyz.py`
  (only if its expectations enumerate views), `PROXY_INTEGRATION.md` §6
- Test: `tests/test_svc_views.py`, `tests/test_readyz.py`, `tests/test_migrations.py`

**Interfaces:**
- Produces four new views + two replaced views, all additive for the proxy:

```sql
-- 0023 up — new views
CREATE VIEW svc.v_person_score AS
SELECT user_ref AS person_ref, score, components, config_version, computed_at
FROM protection_scores;

CREATE VIEW svc.v_person_score_events AS
SELECT score_event_id, user_ref AS person_ref, delta, component, cause_kind,
       cause_ref, score_after, created_at
FROM score_events;

CREATE VIEW svc.v_person_recommendations AS
SELECT rec_id, user_ref AS person_ref, kind, params, status, source_event_id,
       created_at, completed_at, expires_at
FROM recommendations;

CREATE VIEW svc.v_person_threat_context AS
SELECT m.user_ref AS person_ref, e.event_id, e.kind, e.title, e.body, e.severity,
       e.starts_at, e.expires_at
FROM threat_event_matches m
JOIN threat_events e USING (event_id)
WHERE e.status = 'active' AND e.expires_at > now();

GRANT SELECT ON svc.v_person_score, svc.v_person_score_events,
                svc.v_person_recommendations, svc.v_person_threat_context
  TO imageshield_proxy_ro;
```

- `CREATE OR REPLACE VIEW svc.v_person_hits` — the 0016 definition **verbatim** (copy it from
  `migrations/0016_svc_contract_views.up.sql:186-253`; OR REPLACE requires the existing columns
  unchanged and in order) with exactly two changes:
  1. three appended columns at the END of the select list:
     `i.confirm_state`, `i.severity`, `i.confirm_decided_at AS decided_at`
  2. a new final WHERE clause: `WHERE i.confirm_state NOT IN ('quarantined', 'duplicate')`
     (quarantined content must never reach a user surface; a duplicate is the same picture the
     user already answered).
- `CREATE OR REPLACE VIEW svc.v_person_report_summary` — 0016's definition verbatim with the
  inner `infringements` aggregate gaining `WHERE confirm_state NOT IN ('quarantined','duplicate')`
  so `active_reports`/`unresolved_matches`/`live_exposure_count` cannot count them.
- Down leg: `DROP VIEW` the four new views; `CREATE OR REPLACE` hits + summary back to the 0016
  definitions verbatim (copy them into the down file — a down that cannot restore the exact 0016
  projection breaks `/readyz` on rollback).
- `svc_contract.py`: extend `EXPECTED_VIEWS` — append to `v_person_hits`:
  `("confirm_state", "text"), ("severity", "text"), ("decided_at", "timestamp with time zone")`;
  add the four new views with their full column/type lists (`score` → `integer`, `components` →
  `jsonb`, `config_version` → `text`, `computed_at` → `timestamp with time zone`;
  `score_event_id` → `bigint`, `delta` → `integer`, `component/cause_kind/cause_ref` → `text`,
  `score_after` → `integer`; `rec_id/source_event_id/event_id` → `uuid`, `params` → `jsonb`,
  `severity` (threat view) → `smallint`, `expires_at/starts_at/created_at/completed_at` →
  `timestamp with time zone`, `kind/title/body/status` → `text`).
- `tests/test_svc_views.py`: extend `VIEWS` to eight; extend `FROZEN_CONTRACT_COLUMNS` (the
  hand-maintained copy — it is asserted equal to `EXPECTED_VIEWS`, so both move together).

- [ ] **Step 1: Failing tests** — extend `tests/test_svc_views.py`:

```python
def test_quarantined_hits_appear_in_no_view(migrated_db: str) -> None:
    # seed a hit, UPDATE it to confirm_state='quarantined',
    # assert 0 rows in v_person_hits for it AND the summary counts exclude it
def test_duplicates_are_collapsed_out_of_the_report(migrated_db: str) -> None:
def test_confirmed_severity_travels_on_v_person_hits(migrated_db: str) -> None:
def test_score_views_read_under_the_proxy_role(migrated_db: str) -> None:
    # SET ROLE imageshield_proxy_ro; SELECT from all four new views must not raise;
    # UPDATE svc.v_person_score must raise InsufficientPrivilege
```

The existing `test_the_proxy_role_reads_the_views_and_nothing_else` loops `VIEWS`, so extending
`VIEWS` extends it automatically.

- [ ] **Step 2: verify failure, Step 3: write both migration files + contract updates,
Step 4: run** — `python -m pytest tests/test_svc_views.py tests/test_readyz.py tests/test_migrations.py -v` → PASS
(readyz now gates on eight views — deploy-order property preserved: migrate first).

- [ ] **Step 5: Update `PROXY_INTEGRATION.md` §6** — add the four views to the table with column
lists, document the two REPLACEs as additive + row-filtering (quarantine/duplicate exclusion),
state the new columns on `v_person_hits`, and extend "Columns the proxy must never surface"
with nothing new (score views are all user-safe). Note `score_events.cause_ref` is an opaque id
string for provenance, not a URL.

- [ ] **Step 6: Commit** — `feat: 0023 svc score views + hits/summary quarantine exclusion (contract v2, additive)`

---

### Task 17: Control-room console

**Files:**
- Create: `src/imageshield/console/__init__.py`, `config.py`, `auth.py`, `client.py`, `app.py`,
  `templates/{base,dashboard,review,events,score}.html`
- Modify: `pyproject.toml` (add `"jinja2>=3.1,<4.0"` to dependencies; add
  `"console/templates/*.html"` to `[tool.setuptools.package-data] imageshield`)
- Test: `tests/test_console.py`

**Interfaces:**

```python
# console/config.py
class ConsoleConfig(BaseSettings):        # frozen, extra="ignore"
    console_operators: str                # REQUIRED: "alice:token-a,bob:token-b"
    services_base_url: str                # e.g. http://localhost:8081
    service_token: str
    admin_service_token: str
    fetcher_base_url: str
    fetcher_token: str
def load_console_config() -> ConsoleConfig

# console/auth.py
def parse_operators(raw: str) -> dict[str, str]      # name -> token; ValueError on empty/dupes
def require_operator(...) -> str                     # FastAPI dep: HTTP Basic, constant-time,
                                                     # 401 + WWW-Authenticate: Basic on failure;
                                                     # returns the operator NAME

# console/client.py  — every console write flows through the services admin API;
# the console holds NO database access of any kind.
class ServicesClient:
    def __init__(self, client: httpx.AsyncClient, *, base_url: str,
                 service_token: str, admin_service_token: str) -> None
    async def provider_health(self) -> dict[str, Any]
    async def review_next(self) -> dict[str, Any] | None
    async def review_queue(self) -> dict[str, Any]
    async def decide(self, task_id: UUID, *, decision: str, operator: str,
                     severity: str | None) -> None
    async def list_events(self) -> list[dict[str, Any]]
    async def create_event(self, payload: dict[str, Any]) -> dict[str, Any]
    async def retract_event(self, event_id: UUID, *, operator: str, reason: str) -> None
    async def score(self, user_ref: str) -> dict[str, Any] | None

class FetcherClient:
    def __init__(self, client: httpx.AsyncClient, *, base_url: str, token: str) -> None
    async def crop(self, *, url: str, bbox: dict[str, float], blur: bool) -> tuple[bytes, str]

# console/app.py
def create_app(config: ConsoleConfig | None = None) -> FastAPI
```

Routes (all behind `require_operator` except nothing — the whole app is operator-only;
server-rendered Jinja2, forms POST + redirect):

| Route | Renders / does |
|---|---|
| `GET /health` | 200 `{"status": "ok"}`, tokenless — the ECS health-check target |
| `GET /` | dashboard: provider health table + review queue depths |
| `GET /review` | next task: severity, domain, triage facts, crop `<img src="/crop?...">` blurred; a "reveal" link re-requests with `blur=0`; decision form (confirmed + severity select / rejected / uncertain) |
| `POST /review/{task_id}` | form → `decide(...)` with the logged-in operator name → 303 back to `/review` |
| `GET /crop` | query `url,x,y,w,h,blur` → `FetcherClient.crop` → `Response(content, media_type)` — the ONLY pixels path, live-rendered, never stored |
| `GET /events` + `POST /events` | list + create form (domains comma-field → tuple) |
| `POST /events/{event_id}/retract` | reason form |
| `GET /scores` | `?user_ref=` lookup → score + journal table |

Template notes: `base.html` carries a nav and the operator name; keep the HTML dependency-free
(no CDN — this console may live on a box with no egress). Templates load via
`Jinja2Templates(directory=str(Path(__file__).parent / "templates"))`.

Lifespan: build one `httpx.AsyncClient` per upstream unless `app.state.services_client` /
`app.state.fetcher_client` are pre-wired (test convention).

- [ ] **Step 1: Failing tests** — `tests/test_console.py`: no credentials → 401 with
`WWW-Authenticate: Basic`; wrong password → 401; `parse_operators` rejects a duplicate name and
an empty token; `/review` renders the fake task's domain and severity; POSTing a decision calls
the fake `ServicesClient.decide` with `operator="alice"` (from Basic auth) and redirects 303;
`/crop` streams the fake fetcher's bytes with its media type; `/events` create posts through and
redirects. Use `TestClient` + `auth=("alice", "token-a")`.

- [ ] **Step 2–3: red → implement → green** (`ruff`, `mypy` — templates are package-data, not code).
- [ ] **Step 4: Commit** — `feat: control-room console — review, events, health, score inspector`

---

### Task 18: Infrastructure — queue, IAM, ECS task definitions

**Files:**
- Modify: `infra/terraform/queues.tf`, `infra/terraform/iam.tf`,
  `infra/terraform/policies/service-role.json`, `infra/ecs/policies/services-task-role.json`,
  `infra/ecs/imageshield-dev-services.json`, `infra/ecs/imageshield-dev-services-worker.json`,
  `tests/test_iam_policy.py`, `tests/test_ecs_task_defs.py`
- Create: `infra/ecs/imageshield-dev-confirm.json`, `infra/ecs/imageshield-dev-fetcher.json`,
  `infra/ecs/imageshield-dev-console.json`
- Test: `tests/test_iam_policy.py`, `tests/test_ecs_task_defs.py`

- [ ] **Step 1: Failing IAM tests.** In `tests/test_iam_policy.py`, add
`"rekognition:DetectModerationLabels"` to the parametrised required-action list. In
`tests/test_ecs_task_defs.py`: add path constants + parametrize entries for the three new task
defs; add:

```python
def test_confirm_task_runs_exactly_the_confirm_worker_and_the_tick() -> None:
    # containers {"confirm-worker", "score-tick"}, commands
    # ["python","-m","imageshield.confirm.worker"] / ["python","-m","imageshield.score.tick"]

def test_fetcher_and_console_hold_no_database_access() -> None:
    # for both new task defs: no DB_* secret, no DATABASE_URL anywhere in
    # environment or secrets — the fetcher/console no-DB property as a test

def test_fetcher_serves_8083_and_console_8082(...) -> None:
```

Extend the unscoped-Rekognition exemption list (`test_ecs_task_defs.py:80-84`) with
`rekognition:DetectFaces` and `rekognition:DetectModerationLabels` — neither is
collection-scopable.

- [ ] **Step 2: Terraform.** `queues.tf`: add to `local.queues`:

```hcl
    # Confirm bundle: one fetch + up to five Rekognition calls with retries.
    "confirm-hits" = { visibility_timeout = 300 }
```

(DLQ + redrive + both alarms materialise via the existing `for_each`.) `iam.tf`: add
`confirm_hits_queue_arn = aws_sqs_queue.main["confirm-hits"].arn` and
`confirm_hits_dlq_arn = aws_sqs_queue.dlq["confirm-hits"].arn` to the `templatefile` vars.
`policies/service-role.json`: append `rekognition:DetectModerationLabels` to the
`FaceDetectionIsNotCollectionScoped` statement's action list; append both new ARN template vars
to the `Queues` statement's resource list. Update `queues.tf`'s `queue_urls` output description
to name `SQS_CONFIRM_HITS_URL`.

- [ ] **Step 3: Dev-deployed role + task defs.**
`infra/ecs/policies/services-task-role.json`: add `rekognition:DetectFaces` and
`rekognition:DetectModerationLabels` as a new unscoped statement (sid
`DetectionIsNotCollectionScoped`), and the confirm queue ARN
(`arn:aws:sqs:ap-south-1:225989356895:imageshield-dev-confirm-hits`) to `SqsProduceAndConsume`.

`imageshield-dev-services.json` and `-worker.json` (every container): add environment
`SQS_CONFIRM_HITS_URL=https://sqs.ap-south-1.amazonaws.com/225989356895/imageshield-dev-confirm-hits`,
`FETCHER_BASE_URL=http://localhost:8083`, `CSAM_AGE_LOW_THRESHOLD=18`; add secret `FETCHER_TOKEN`
(`arn:...:secret:imageshield/dev/services-...:FETCHER_TOKEN::` — same secret container as
SERVICE_TOKEN, new key; note in the runbook that the key must be added to the secret before
deploy).

`imageshield-dev-confirm.json` — copy the worker task def envelope verbatim (family
`imageshield-dev-confirm`), two containers, full env/secrets duplicated per container like the
existing worker file, `ENVIRONMENT=production`, `DB_POOL_MAX_SIZE=2`:
`confirm-worker` (command `["python","-m","imageshield.confirm.worker"]`, memory 192) and
`score-tick` (command `["python","-m","imageshield.score.tick"]`, memory 96), awslogs prefixes
`confirm-worker`/`score-tick`, no health checks, both essential.

`imageshield-dev-fetcher.json` — one container `fetcher`, command
`["sh","-c","uvicorn imageshield.fetcher.app:create_app --factory --host 0.0.0.0 --port 8083"]`,
memory 192, env only `FETCH_*` knobs (none required) + `HTTP` nothing, secrets ONLY
`FETCHER_TOKEN`; `python -c` urllib health check on `http://localhost:8083/health`; **no DB_*
secrets, no SQS vars — this file is the no-DB property in deployable form.**

`imageshield-dev-console.json` — one container `console`, command
`["sh","-c","uvicorn imageshield.console.app:create_app --factory --host 0.0.0.0 --port 8082"]`,
memory 160, env `SERVICES_BASE_URL=http://localhost:8081`, `FETCHER_BASE_URL=http://localhost:8083`;
secrets `SERVICE_TOKEN`, `ADMIN_SERVICE_TOKEN`, `FETCHER_TOKEN`, `CONSOLE_OPERATORS`; health
check on `http://localhost:8082/` is WRONG (it 401s) — health-check
`http://localhost:8082/health`; add a `GET /health` (200, tokenless) route to the console app in
Task 17 (mirror the fetcher's).

- [ ] **Step 4: Run** — `python -m pytest tests/test_iam_policy.py tests/test_ecs_task_defs.py -v`
→ PASS. `terraform -chdir=infra/terraform fmt -check -recursive` and `validate` if the binary is
available locally; otherwise note it and rely on the CI `infra` job.

- [ ] **Step 5: Commit** — `feat: confirm-hits queue infra, moderation IAM, fetcher/console/confirm task defs`

**Deploy-order note for the runbook (Task 19):** memory on the dev `t4g.medium` is budgeted;
registering these task defs is safe, but *placing* all three new services alongside the existing
five containers may not fit — the runbook must say: deploy `fetcher` first (confirm worker
depends on it), then `confirm`, console last; if placement fails, the instance needs resizing —
that is an operator decision, not something this repo forces.

---

### Task 19: Documentation, invariants, and the final gate run

**Files:**
- Modify: `INVARIANTS.md`, `CLAUDE.md`, `SCHEMA.md`, `ARCHITECTURE.md`, `docs/OPERATIONS.md`,
  `docs/deploy/DEPLOY-RUNBOOK.md`, `.env.example` (verify), spec status line
- Test: the full suite

- [ ] **Step 1: `INVARIANTS.md`** — append, numbered and full-text:
  - **#44** Every score movement is journaled with a user-readable cause. `score_events` is
    append-only (INSERT-only grant), sums to the materialized score, and no delta exists without
    a cause row. Enforced by `tests/test_score_store.py` and the 0022 grant test.
  - **#45** Reporting abuse never worsens any user-facing number. Generalizes the
    `live_exposure_count` rule: no feedback signal may lower `protection_scores.score`.
    Enforced by `test_user_feedback_never_lowers_the_score`.
  - **#46** Threat penalties are bounded (component cap + global cap), decaying, relevance-scoped
    (matched via the user's own live hits), and reversible on retraction — retraction restores
    exactly.
  - **#47** Machine triage orders review but can neither confirm nor drop. `confirmed` requires
    `confirm_decided_by` by CHECK (0021); nothing machine-writes `drop`.
- [ ] **Step 2: `CLAUDE.md`** — §6 scope table: move "Adjudication queue and reviewer tooling
  (minimal)" and "Crop fetcher deployable" (as the fetcher) to the built column; add rows for
  protection score + recommendations + threat events + control room; add the confirm pipeline.
  §2 stack table: queues line now three (`identity:index`, `search:runs`, `confirm:hits`);
  add Jinja2 (console only) and note Pillow's three call sites. §4: reference the four new
  invariants by number. Keep the §8 build-order list intact; add a dated note that this push is
  post-v1 scope sanctioned by the 2026-08-19 design.
- [ ] **Step 3: `SCHEMA.md`** — document the 0021–0023 tables/columns/views in the file's style
  (state the confirm_state lifecycle and that `phash` is a hash, not bytes, under #9).
- [ ] **Step 4: `ARCHITECTURE.md`** — mark §3.7 crop fetcher as built (the fetcher deployable,
  two jobs); add short sections for the confirm worker, score engine (+tick), threats, review,
  console; update the process inventory.
- [ ] **Step 5: `docs/OPERATIONS.md`** — process table gains four rows (confirm worker, score
  tick, fetcher, console); new scenarios: "confirm-hits DLQ has depth", "a hit was quarantined"
  (what the `confirm.quarantined` error log means, who to contact — legal escalation is manual),
  "the score looks wrong" (read `score_events`, recompute via tick). `docs/deploy/DEPLOY-RUNBOOK.md`:
  new §: secrets keys to add (`FETCHER_TOKEN`, `CONSOLE_OPERATORS`), register + create-service
  commands for the three new task defs, the deploy order + placement caveat from Task 18.
- [ ] **Step 6: Spec status** — flip the spec's `**Status:**` line to
  `implemented (this plan); see git log`.
- [ ] **Step 7: The one full run.** In order, capturing output:

```bash
ruff check .
mypy
$env:REQUIRE_DB="1"; python -m pytest tests/ -v      # the ONE full-suite run (~7 min+)
```

All three must be clean. Fix anything that surfaces, re-run only the affected file, and do NOT
re-run the full suite more than once more.

- [ ] **Step 8: Final commit** — `docs: invariants #44–47, scope + ops docs for the score push`

---

## Self-review appendix (for the executor)

- Severity vocabulary is FIVE values everywhere (`ncii_suspected`, `explicit_unmatched`,
  `unassessed`, `benign_copy`, `likely_not_subject`) — 0021 CHECKs (both tables), triage.py,
  review routes, engine's `exposure_weight`.
- `confirm_state` vocabulary is SIX values (`unconfirmed`, `machine_triaged`, `confirmed`,
  `rejected`, `duplicate`, `quarantined`) — 0021, store transitions, 0023 view WHEREs.
- Cause kinds: `feedback`, `enrolment`, `seed_registered`, `run_completed`, `review_decision`,
  `threat_event`, `threat_retracted`, `tick` (+ `initialised` never used — first compute journals
  under its real trigger cause).
- The three new REQUIRED config fields appear in: `config.py`, `tests/conftest.py::VALID_ENV`,
  `.env.example`, BOTH existing ECS task defs, and all three new ones that load `Config`
  (confirm task only — fetcher/console have their own config classes and must NOT carry them).
- Nothing outside `score/store.py` INSERTs into `protection_scores`/`score_events`
  (boundary test, Task 12).
- Nothing outside `attribution/` matches the face-search regex — `confirm/` calls attribution
  functions only.






