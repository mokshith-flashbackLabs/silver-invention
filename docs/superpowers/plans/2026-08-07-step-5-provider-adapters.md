# Step 5 — Provider Adapter Interface + Hive & Google Adapters — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A provider-agnostic search adapter layer with two working adapters (Hive Web Search, Google
Vision Web Detection), the score-shape migration that lets one `search_matches` table hold numeric
and categorical results, the four search endpoints, and the SQS worker that executes runs
asynchronously.

**Architecture:** New `src/imageshield/search/` package. HTTP routes write seeds/runs and enqueue via
the step-2 outbox (never call providers synchronously). A new worker process
(`python -m imageshield.search.worker`, mirroring `relay.py`) consumes SQS `search:runs`, re-reads
the run row from Postgres (messages carry IDs, never payloads), claims it, fans out to the adapters
concurrently, and persists `provider_calls` (raw_response verbatim, always) + `search_matches`
(band `'review'`, everything). Adapters translate provider responses into `ProviderMatch` and stop —
no normalising, banding, thresholding, rescaling, or comparing.

**Tech Stack:** Python 3.11, FastAPI, psycopg 3 raw SQL, httpx (adapters), boto3 (worker SQS receive
only), pydantic 2, pytest (+ throwaway-Postgres fixture), mypy strict, ruff.

## Global Constraints

- Migration numbering: the spec's "Migration 0002" lands as **0004** — 0002/0003 already exist.
- Adapters return **raw** scores. `grep -rE "\* *100|score *\* " src/` must return nothing.
- Hive's numeric domain is **0.5–1.0** (0.5 is the floor, not a midpoint); `providers.score_domain`
  for hive must read `{"min": 0.5, "max": 1.0}`.
- Google matches: `provider_score IS NULL` always; `provider_category` one of
  `full_match | partial_match | page_match`. `webEntities` are never read.
- `raw_response` stored verbatim on every `provider_calls` row, including failures.
- Every `search_matches` row written in step 5 has `band = 'review'` (uncalibrated providers cannot
  auto-confirm).
- One provider failing/timing out never fails the run; `providers_succeeded` reflects reality.
- URL normalisation and dedup are **step 6**: step 5 hashes the *raw* URL in one clearly marked
  function (`interim_url_hash`) that step 6 replaces. Dedup within a run comes from the existing
  unique index `(run_id, url_hash, provider_id)` + `ON CONFLICT DO NOTHING`.
- Which Hive product a key hits is determined by the **Hive project the key belongs to**, not the
  URL — documented in the Hive adapter docstring and in CLAUDE.md §7 / ARCHITECTURE.md.
- `X-Service-Token` on every route; `user_ref` only, never a phone. Typed identifiers
  (`UserRef`, `ProviderId`) at every boundary; inbound bodies `extra='forbid'`.
- boto3 only in `relay.py`, `liveness/provider.py`, `enrolment/faceindex.py`, and (new)
  `search/worker.py` — pyproject TID251 per-file-ignores updated in the same task that adds the
  worker.
- Commits end with `Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>`.

**Old-repo citations** (read, not ported): working Hive request construction at
`server/lambda/weeklyInfringementScannerLambda/weeklyInfringementScanner.js:1078-1160`. Its three
defects, not reproduced: `similarity_score * 100` rescale (line 1129), unnormalised URL hashing,
unbounded recursive 429 retry (line 1148 — recursion with no depth counter). Working request shapes
also proven live by `devtools/harness/server.py:244-382` (2026-08-06).

---

### Task 1: Migration 0004 — score shape, run status, provider seed rows

**Files:**
- Create: `migrations/0004_provider_score_shape.up.sql`
- Create: `migrations/0004_provider_score_shape.down.sql`
- Test: `tests/test_migrations.py` (extend)

**Interfaces:**
- Produces: `search_matches` columns `score_kind TEXT NOT NULL`, `provider_category TEXT`,
  `query_quality TEXT`, nullable `provider_score`, CHECK `search_matches_score_shape`;
  `providers.score_kind`, `providers.score_domain JSONB`; `search_runs.status TEXT NOT NULL
  DEFAULT 'queued'` (`queued|running|completed`) and `search_runs.claimed_at TIMESTAMPTZ`;
  seeded provider rows `'hive'` and `'google'`.

- [ ] **Step 1: Write failing tests** (append to `tests/test_migrations.py`)

```python
def test_0004_score_shape_check_constraint(throwaway_db: str) -> None:
    """A match row must carry a numeric score OR a category — never neither."""
    run_migrate(throwaway_db, "down", "--all")
    up = run_migrate(throwaway_db, "up")
    assert up.returncode == 0, up.stderr

    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO search_seeds (seed_id, user_ref, seed_kind, source_object_uri)"
            " VALUES ('00000000-0000-0000-0000-000000000001',"
            "  '00000000-0000-0000-0000-0000000000aa', 'user_supplied', 'https://x/img.jpg')"
        )
        conn.execute(
            "INSERT INTO search_runs (run_id, seed_id, user_ref, providers_attempted,"
            " threshold_config) VALUES ('00000000-0000-0000-0000-000000000002',"
            " '00000000-0000-0000-0000-000000000001',"
            " '00000000-0000-0000-0000-0000000000aa', '{hive}', '{}')"
        )
        conn.execute(
            "INSERT INTO content_urls (url_hash, url, source_domain)"
            f" VALUES ('{'a' * 64}', 'https://x/img.jpg', 'x')"
        )

        common = (
            "INSERT INTO search_matches (run_id, url_hash, user_ref, provider_id,"
            " image_url, score_version, band, score_kind, provider_score, provider_category)"
            " VALUES ('00000000-0000-0000-0000-000000000002', %s,"
            " '00000000-0000-0000-0000-0000000000aa', %s, 'https://x/img.jpg', 'v1',"
            " 'review', %s, %s, %s)"
        )
        # numeric with a score: fine
        conn.execute(common, ("a" * 64, "hive", "numeric", "0.8712", None))
        # categorical with a category: fine
        conn.execute(common, ("a" * 64, "google", "categorical", None, "full_match"))
        # neither: rejected by the CHECK
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(common, ("a" * 64, "hive", "numeric", None, None))


def test_0004_providers_seeded_with_score_domain(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    up = run_migrate(throwaway_db, "up")
    assert up.returncode == 0, up.stderr

    with psycopg.connect(throwaway_db, autocommit=True) as conn:
        rows = dict(
            conn.execute(
                "SELECT provider_id, score_domain FROM providers"
                " WHERE provider_id IN ('hive', 'google')"
            ).fetchall()
        )
    assert rows["hive"] == {"min": 0.5, "max": 1.0}
    assert rows["google"] == {"categories": ["full_match", "partial_match", "page_match"]}
```

- [ ] **Step 2: Run tests, verify they fail** (`.venv/Scripts/python -m pytest tests/test_migrations.py -k 0004 -v` — fails: migration files don't exist, columns missing)

- [ ] **Step 3: Write the migration**

`migrations/0004_provider_score_shape.up.sql`:

```sql
-- Step 5: one search_matches table must hold BOTH score shapes without the
-- adapter inventing a number for categorical providers (Google Web Detection
-- returns score: null — normalising in the adapter would make recalibration
-- impossible without a redeploy, CLAUDE.md §7.2).

ALTER TABLE search_matches
  ALTER COLUMN provider_score DROP NOT NULL,
  ADD COLUMN score_kind TEXT NOT NULL DEFAULT 'numeric',
  ADD COLUMN provider_category TEXT,
  ADD COLUMN query_quality TEXT,
  ADD CONSTRAINT search_matches_score_shape CHECK (
    (score_kind = 'numeric'     AND provider_score    IS NOT NULL) OR
    (score_kind = 'categorical' AND provider_category IS NOT NULL)
  );
ALTER TABLE search_matches ALTER COLUMN score_kind DROP DEFAULT;

-- Record each provider's score domain so step 7 calibrates against reality:
-- Hive Web Search reports 0.5–1.0 where 0.5 is the FLOOR (lowest reportable
-- score), not a midpoint. Banding it as 0–1 would read weak matches as
-- moderate ones.
ALTER TABLE providers
  ADD COLUMN score_kind   TEXT NOT NULL DEFAULT 'numeric',
  ADD COLUMN score_domain JSONB;

-- Async execution state for the search:runs worker. 'queued' -> 'running'
-- (claimed_at set) -> 'completed'. A 'running' row whose claimed_at is stale
-- is reclaimable (worker crash recovery).
ALTER TABLE search_runs
  ADD COLUMN status     TEXT NOT NULL DEFAULT 'queued',
  ADD COLUMN claimed_at TIMESTAMPTZ;

-- The two step-5 providers. Which Hive PRODUCT a key hits is determined by
-- the Hive project the key belongs to, not the URL — a key provisioned
-- against Hive's "Media Search" (movies/TV) returns plausible-looking wrong
-- results, not an error. Ours must be Web Search (reverse image search).
INSERT INTO providers (provider_id, kind, enabled, calibrated, score_version,
                       score_kind, score_domain)
VALUES
  ('hive',   'image_search', true, false, 'hive-web-search-v1',
   'numeric',     '{"min": 0.5, "max": 1.0}'),
  ('google', 'image_search', true, false, 'google-web-detection-v1',
   'categorical', '{"categories": ["full_match", "partial_match", "page_match"]}')
ON CONFLICT (provider_id) DO NOTHING;
```

`migrations/0004_provider_score_shape.down.sql`:

```sql
-- Reversal deletes rows that the restored NOT NULL / dropped columns cannot
-- represent (categorical matches, seeded providers). Down migrations run in
-- dev/CI only; the data loss is deliberate and documented.

DELETE FROM search_matches WHERE provider_score IS NULL;

ALTER TABLE search_matches
  DROP CONSTRAINT search_matches_score_shape,
  DROP COLUMN score_kind,
  DROP COLUMN provider_category,
  DROP COLUMN query_quality;
ALTER TABLE search_matches ALTER COLUMN provider_score SET NOT NULL;

ALTER TABLE providers DROP COLUMN score_kind, DROP COLUMN score_domain;

ALTER TABLE search_runs DROP COLUMN status, DROP COLUMN claimed_at;

DELETE FROM search_matches WHERE provider_id IN ('hive', 'google');
DELETE FROM provider_calls WHERE provider_id IN ('hive', 'google');
DELETE FROM providers WHERE provider_id IN ('hive', 'google');
```

- [ ] **Step 4: Run tests, verify pass** — including the existing round-trip tests
  (`pytest tests/test_migrations.py -v`) and the schema lint (`pytest tests/test_schema_lint.py -v`;
  the new TEXT/JSONB columns pass invariant #9's name+type gates).
- [ ] **Step 5: Commit** `feat: migration 0004 — provider score shape, run status, provider seed rows`

---

### Task 2: Config — Google Vision key/endpoint, provider timeout

**Files:**
- Modify: `src/imageshield/config.py`
- Modify: `tests/conftest.py` (VALID_ENV + make_config)
- Test: `tests/test_config.py` (extend)

**Interfaces:**
- Produces: `Config.google_vision_api_key: str` (required, sentinel-rejected),
  `Config.google_vision_endpoint: str` (default `https://vision.googleapis.com/v1/images:annotate`),
  `Config.provider_timeout_seconds: float` (default `120.0` — Hive's sync endpoint is slow; the
  harness used 120s).

- [ ] **Step 1: Failing test** (append to `tests/test_config.py`, mirroring the existing
  hive_api_key sentinel test style)

```python
def test_google_vision_api_key_required_and_sentinel_rejected() -> None:
    with pytest.raises(ValidationError):
        make_config(google_vision_api_key="changeme")


def test_provider_timeout_default_positive() -> None:
    cfg = make_config()
    assert cfg.provider_timeout_seconds == 120.0
    assert cfg.google_vision_endpoint.startswith("https://vision.googleapis.com/")
```

- [ ] **Step 2: Run, verify fail** (unknown field / missing attr)
- [ ] **Step 3: Implement** — add the three fields; extend the `_secret_not_placeholder` validator
  to cover `google_vision_api_key`; add `google_vision_endpoint` to the `_http_url` validator list
  and `provider_timeout_seconds` to `_positive_float`. Add
  `"GOOGLE_VISION_API_KEY": "google-key-for-tests"` to `VALID_ENV` and
  `google_vision_api_key="google-key-for-tests"` to `make_config`.
- [ ] **Step 4: Run full `tests/test_config.py` + `tests/test_boot.py`, verify pass**
- [ ] **Step 5: Commit** `feat: config for Google Vision + provider timeout`

---

### Task 3: Provider interface — `search/provider.py`

**Files:**
- Create: `src/imageshield/search/__init__.py` (empty)
- Create: `src/imageshield/search/provider.py`
- Test: `tests/test_provider_models.py`

**Interfaces (produces — verbatim from the spec, consumed by every later task):**

```python
class ProviderMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image_url: str
    page_url: str | None            # backlinks[0].url for Hive when present
    provider_score: Decimal | None  # numeric providers only — RAW, never rescaled
    provider_category: str | None   # categorical providers only
    query_quality: str | None

class ProviderResult(BaseModel):
    provider_id: ProviderId
    status: Literal["ok", "error", "rate_limited", "timeout", "budget_exceeded"]
    matches: list[ProviderMatch]
    raw_response: dict[str, Any]    # VERBATIM, always, even on error
    http_status: int | None
    latency_ms: int

class SearchProvider(Protocol):
    id: ProviderId
    kind: Literal["image_search", "face_search", "classifier"]
    score_kind: Literal["numeric", "categorical"]
    score_version: str
    async def search(self, seed_url: str, max_results: int | None = None) -> ProviderResult: ...
```

Module docstring states the adapter contract: translate and stop — no normalise/band/threshold/
rescale/compare (that is step 7, from config).

- [ ] **Step 1: Failing test**

```python
from decimal import Decimal

import pytest
from pydantic import ValidationError

from imageshield.search.provider import ProviderMatch, ProviderResult
from imageshield.types import ProviderId


def test_provider_match_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ProviderMatch(
            image_url="https://x/a.jpg", page_url=None, provider_score=Decimal("0.9"),
            provider_category=None, query_quality=None, normalised_score=0.5,
        )


def test_provider_result_status_is_closed_enum() -> None:
    with pytest.raises(ValidationError):
        ProviderResult(
            provider_id=ProviderId("hive"), status="partial", matches=[],
            raw_response={}, http_status=200, latency_ms=1,
        )


def test_provider_score_stays_decimal_raw() -> None:
    match = ProviderMatch(
        image_url="https://x/a.jpg", page_url=None,
        provider_score=Decimal("0.5001"), provider_category=None, query_quality=None,
    )
    assert match.provider_score == Decimal("0.5001")
```

- [ ] **Step 2: Run, verify fail** (module missing)
- [ ] **Step 3: Implement** exactly the models above (plus module docstring).
- [ ] **Step 4: Run, verify pass**
- [ ] **Step 5: Commit** `feat: provider-agnostic search interface (ProviderMatch/Result/Protocol)`

---

### Task 4: Interim URL hash — `search/urlhash.py`

**Files:**
- Create: `src/imageshield/search/urlhash.py`
- Test: `tests/test_urlhash.py`

**Interfaces:**
- Produces: `interim_url_hash(url: str) -> UrlHash` (sha256 of the raw URL, lowercase hex) and
  `source_domain(url: str) -> str` (hostname or `"unknown"`). Module docstring: **step-6 replaces
  this file** with real normalisation; this is the single call site to swap.

- [ ] **Step 1: Failing test**

```python
import hashlib

from imageshield.search.urlhash import interim_url_hash, source_domain


def test_interim_hash_is_sha256_of_raw_url() -> None:
    url = "https://Example.com/a?b=1"
    assert interim_url_hash(url) == hashlib.sha256(url.encode()).hexdigest()


def test_source_domain_extracts_hostname() -> None:
    assert source_domain("https://sub.example.com/x/y.jpg") == "sub.example.com"
    assert source_domain("not a url") == "unknown"
```

- [ ] **Step 2: Run, verify fail**
- [ ] **Step 3: Implement** (`hashlib.sha256(url.encode("utf-8")).hexdigest()` →
  `parse_url_hash(...)`; `urlsplit(url).hostname or "unknown"`).
- [ ] **Step 4: Run, verify pass**
- [ ] **Step 5: Commit** `feat: interim raw-URL hash (step-6 swaps in normalisation here)`

---

### Task 5: Hive adapter — `search/hive.py`

**Files:**
- Create: `src/imageshield/search/hive.py`
- Test: `tests/test_hive_adapter.py`

**Interfaces:**
- Consumes: Task 3 models; `Config.hive_base_url`, `hive_api_key`, `provider_timeout_seconds`.
- Produces: `HiveWebSearchProvider(base_url, api_key, timeout_seconds, client=None)` satisfying
  `SearchProvider`; `id=ProviderId("hive")`, `kind="image_search"`, `score_kind="numeric"`,
  `score_version="hive-web-search-v1"`.

**Behaviour (request shape proven by the harness, `devtools/harness/server.py:274-302`):**
- `POST {base}/api/v2/task/sync`, header `authorization: token {key}`, form field `url=seed_url`
  (presigned GET preferred — no image bytes pass through this service).
- Parse `status[0].response.output.matches[]`, each `{url, score, backlinks: [{url}]}` (harness,
  2026-08-06). Fallback key `similarity_score` (old lambda, weeklyInfringementScanner.js:1129) is
  *read* if `score` is absent — reading an alternate key is not rescaling; the value stays raw
  `Decimal`. A match with neither key is skipped (raw_response keeps it; the numeric CHECK cannot
  store a score-less numeric row).
- `page_url` = `backlinks[0].url` when present, else None.
- `query_quality`: INFERRED field name — first present of `output["query_quality"]` /
  `output["quality"]`, stringified; None otherwise. raw_response preserves the truth either way.
- A 200 whose body lacks the `status[0].response.output.matches` path → status **"error"** (not
  "ok, empty"): this is the wrong-Hive-project tripwire — a key provisioned against Media Search
  returns plausible wrong shapes, and that must not read as "nothing found".
- 429 → wait `Retry-After` (default 60s, capped `_RATE_LIMIT_WAIT_CAP_SECONDS = 30.0` via
  `min()`), retry **once** (`_MAX_RATE_LIMIT_RETRIES = 1` — bounded, unlike the old repo's
  unbounded recursion at line 1148); still 429 → status `"rate_limited"`.
- `httpx.TimeoutException` → status `"timeout"`, raw_response `{"exception": str(exc)}`.
- Other non-200 → `"error"` with body (JSON, else `{"non_json_body": text[:2000]}`).
- `max_results` slices the parsed match list.
- Module docstring carries the key→project warning verbatim.

- [ ] **Step 1: Failing tests** (httpx.MockTransport injected via `client=`; asserts include:
  raw score `Decimal("0.87")` stored unchanged, page_url from backlinks, 429-then-200 succeeds
  with exactly two requests, 429-twice → `rate_limited`, timeout → `timeout` with verbatim-ish
  raw_response, malformed-200 → `error`, `max_results=1` slices, request carried
  `authorization: token ...` and `url` form field)
- [ ] **Step 2: Run, verify fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, verify pass**
- [ ] **Step 5: Commit** `feat: Hive Web Search adapter — raw 0.5–1.0 scores, bounded 429 retry`

---

### Task 6: Google adapter — `search/google.py`

**Files:**
- Create: `src/imageshield/search/google.py`
- Test: `tests/test_google_adapter.py`

**Interfaces:**
- Consumes: Task 3 models; `Config.google_vision_endpoint`, `google_vision_api_key`,
  `provider_timeout_seconds`.
- Produces: `GoogleWebDetectionProvider(endpoint, api_key, timeout_seconds, client=None)`;
  `id=ProviderId("google")`, `kind="image_search"`, `score_kind="categorical"`,
  `score_version="google-web-detection-v1"`.

**Behaviour (request shape proven by the harness, `devtools/harness/server.py:308-382`):**
- `POST {endpoint}?key={api_key}` with JSON body
  `{"requests": [{"image": {"source": {"imageUri": seed_url}},
  "features": [{"type": "WEB_DETECTION", "maxResults": max_results or 50}]}]}`.
- Parse `responses[0].webDetection`: `fullMatchingImages` → `provider_category="full_match"`,
  `partialMatchingImages` → `"partial_match"`, `pagesWithMatchingImages` → `"page_match"`
  (for page_match, `page_url` = the entry's url too — it IS a page).
- **`provider_score` is None. Always. Never synthesised.**
- **`webEntities` and `bestGuessLabels` are never read** — knowledge-graph lookups resolve famous
  people only; not evidence about our user (harness finding 2026-08-06). The word `webEntities`
  appears in the module only in the comment explaining why it is ignored.
- `responses[0].error` present → status `"error"` (Vision reports per-image errors inside a 200).
- Missing/empty webDetection sections on a clean 200 → `"ok"` with zero matches (unlike Hive there
  is no wrong-product failure mode; an absent section is Google's way of saying none).
- Timeout / non-200 / non-JSON handled as in the Hive adapter.

- [ ] **Step 1: Failing tests** (MockTransport; asserts: three categories mapped, every
  `provider_score is None`, webEntities present in the fixture payload but produce no match rows,
  `responses[0].error` → status "error", key sent as query param, imageUri in body)
- [ ] **Step 2: Run, verify fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, verify pass**
- [ ] **Step 5: Commit** `feat: Google Web Detection adapter — categorical, no synthetic scores`

---

### Task 7: Search store — `search/store.py`

**Files:**
- Create: `src/imageshield/search/models.py` (row dataclass-style pydantic models)
- Create: `src/imageshield/search/store.py`
- Test: `tests/test_search_store.py` (throwaway db, migrations applied via `run_migrate`)

**Interfaces:**
- Consumes: `imageshield.outbox.enqueue` (same-transaction rule), Task 3/4 modules.
- Produces `SearchStore` Protocol + `PostgresSearchStore(pool)`:

```python
class SearchStore(Protocol):
    async def create_seed(self, user_ref: UserRef, seed_kind: str, source_object_uri: str) -> UUID: ...
    async def get_seed(self, seed_id: UUID) -> SeedRow | None: ...
    async def create_run(self, user_ref: UserRef, seed_id: UUID,
                         providers_attempted: Sequence[ProviderId]) -> UUID: ...
    async def get_run(self, run_id: UUID) -> RunRow | None: ...
    async def claim_run(self, run_id: UUID) -> ClaimedRun | None: ...
    async def enabled_provider_ids(self) -> tuple[ProviderId, ...]: ...
    async def record_provider_call(self, run_id: UUID, result: ProviderResult) -> None: ...
    async def record_matches(self, run_id: UUID, user_ref: UserRef,
                             provider: ProviderDescriptor, matches: Sequence[ProviderMatch]) -> int: ...
    async def complete_run(self, run_id: UUID,
                           providers_succeeded: Sequence[ProviderId]) -> None: ...
    async def list_matches(self, user_ref: UserRef, since: datetime | None) -> tuple[MatchRow, ...]: ...
```

Key semantics:
- `create_run`: INSERT `search_runs` (status `'queued'`, `threshold_config =
  '{"band": "review", "reason": "uncalibrated_v1"}'`) **and** `outbox` row
  (`OutboxPayload(event="search.run_requested", id=run_id)`, queue `search:runs`) in one
  transaction.
- `claim_run`: `UPDATE search_runs SET status='running', claimed_at=now() WHERE run_id=%s AND
  (status='queued' OR (status='running' AND claimed_at < now() - interval '15 minutes'))
  RETURNING ...` joined to the seed for `source_object_uri` — at-least-once delivery makes
  duplicates normal; a completed run is never re-executed, a stale 'running' claim (worker died)
  is reclaimable after `_STALE_CLAIM` (module constant, 15 minutes).
- `record_matches`: per match — upsert `content_urls` (`interim_url_hash`, `source_domain`,
  `ON CONFLICT (url_hash) DO UPDATE SET last_seen_at = now()`), then INSERT `search_matches`
  with `band='review'`, `score_kind=provider.score_kind`, `score_version=provider.score_version`,
  `ON CONFLICT (run_id, url_hash, provider_id) DO NOTHING`; returns rows actually inserted.
- `complete_run`: sets `status='completed'`, `providers_succeeded`, `matches_found`
  (counted from `search_matches` for the run), `completed_at=now()`.
- `record_provider_call`: INSERT `provider_calls` with `Jsonb(result.raw_response)` verbatim,
  `status`, `http_status`, `latency_ms`.

`ProviderDescriptor` (in `models.py`): `provider_id`, `score_kind`, `score_version` — what
`record_matches` needs without holding an adapter instance.

- [ ] **Step 1: Failing tests** — the load-bearing ones:

```python
async def test_create_run_writes_outbox_row_in_same_transaction(...)  # outbox row exists, queue_name search:runs, payload {event, id}
async def test_claim_run_transitions_queued_to_running_once(...)      # second claim returns None
async def test_claim_run_skips_completed(...)
async def test_record_matches_band_is_review_and_dedupes(...)         # same URL twice -> 1 row; band review; ON CONFLICT
async def test_record_matches_categorical_null_score(...)             # google row: score NULL, category set
async def test_complete_run_sets_status_succeeded_and_count(...)
async def test_list_matches_filters_by_user_and_since(...)
```

- [ ] **Step 2: Run, verify fail**
- [ ] **Step 3: Implement** (raw SQL, style of `enrolment/store.py` / `liveness/store.py`)
- [ ] **Step 4: Run, verify pass**
- [ ] **Step 5: Commit** `feat: search store — seeds, runs, provider calls, matches (band=review)`

---

### Task 8: Run executor — `search/runner.py`

**Files:**
- Create: `src/imageshield/search/runner.py`
- Test: `tests/test_search_runner.py` (fake adapters + fake store, no DB)

**Interfaces:**
- Consumes: `SearchProvider`, `SearchStore`, `ClaimedRun`.
- Produces: `async def execute_run(claim: ClaimedRun, providers: Mapping[ProviderId,
  SearchProvider], store: SearchStore) -> RunOutcome` where `RunOutcome` carries
  `providers_succeeded: tuple[ProviderId, ...]` and `matches_recorded: int`.

**Behaviour:**
- Fans out `asyncio.gather` over `claim.providers_attempted ∩ providers` (an attempted provider
  with no registered adapter records a synthetic `"error"` ProviderResult — visible, never silent).
- Each adapter call wrapped: an unexpected exception becomes
  `ProviderResult(status="error", raw_response={"exception": str(exc)}, ...)` — one provider
  failing never fails the run.
- Every result → `record_provider_call` (raw_response verbatim including failures);
  `status == "ok"` additionally → `record_matches`.
- `complete_run` always runs, `providers_succeeded` = providers whose status was `"ok"`.

- [ ] **Step 1: Failing tests** — the spec's done-when in miniature: one fake provider returns two
  matches, the other raises `httpx.TimeoutException` (via a fake returning status "timeout") →
  run completes, `providers_succeeded == ("hive",)`, both provider_calls recorded, matches
  recorded only for the ok provider.
- [ ] **Step 2: Run, verify fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, verify pass**
- [ ] **Step 5: Commit** `feat: run executor — partial results, explicit per-provider status`

---

### Task 9: SQS worker — `search/worker.py`

**Files:**
- Create: `src/imageshield/search/worker.py` (runnable: `python -m imageshield.search.worker`)
- Modify: `pyproject.toml` (TID251 per-file-ignore + banned-api message)
- Test: `tests/test_search_worker.py` (stub SQS consumer + fake store/providers)

**Interfaces:**
- Consumes: `Config.sqs_search_runs_url`, `claim_run`/`execute_run`, relay's LocalStack endpoint
  trick (`relay._localstack_endpoint_url` is private — reimplement the 4-line helper or import it;
  import it, it's in-repo).
- Produces: `SqsConsumer` Protocol (`receive_message`, `delete_message`),
  `async def handle_message(body: str, store, providers, log) -> bool` (True → delete message),
  `run_forever(config)`, `main()`.

**Behaviour:**
- Long-poll `receive_message(WaitTimeSeconds=10, MaxNumberOfMessages=1)` via
  `asyncio.to_thread` (boto3 is sync; adapters/store are async on one shared pool).
- Message body is `OutboxPayload` JSON (`{event, id}`): messages carry IDs; the worker re-reads
  the run row via `claim_run` — the stored row wins.
- `event != "search.run_requested"` → log error, delete (poison-pill removal).
- `claim_run` returns None (already completed / claimed recently / unknown id) → log, delete —
  this is the idempotency contract under at-least-once delivery.
- Claim OK → `execute_run`; on success delete the message. On an unexpected exception: log, do
  **not** delete — visibility timeout redelivers, and the stale-claim window (Task 7) makes the
  redelivery able to reclaim.
- Signal handling + structlog identical in shape to `relay.py`.

- [ ] **Step 1: Failing tests** (stub consumer yields one message then empty batches; asserts:
  claimed+executed+deleted; unknown event deleted without execute; unclaimable run deleted
  without execute; execution failure → not deleted)
- [ ] **Step 2: Run, verify fail**
- [ ] **Step 3: Implement** + pyproject:
  `"src/imageshield/search/worker.py" = ["TID251"]` and extend the banned-api message to name the
  worker as the fourth sanctioned importer (SQS receive only, never S3).
- [ ] **Step 4: Run, verify pass** (plus `ruff check .`)
- [ ] **Step 5: Commit** `feat: search:runs SQS worker — idempotent, claim-based, at-least-once`

---

### Task 10: HTTP endpoints + wiring

**Files:**
- Create: `src/imageshield/http/routes/search.py`
- Modify: `src/imageshield/http/models.py` (request/response models)
- Modify: `src/imageshield/http/deps.py` (`get_search_store`)
- Modify: `src/imageshield/http/app.py` (wire `PostgresSearchStore`, include router)
- Test: `tests/test_search_routes.py` (fake store via `app.state`), `tests/test_boundaries.py`
  (extend route-auth coverage if it enumerates routes)

**Interfaces (consumes Task 7's store Protocol):**

```
POST /v1/seeds        { user_ref, seed_kind, source_object_uri } -> 201 { seed_id }
POST /v1/search       { user_ref, seed_id, providers? }          -> 202 { run_id }
GET  /v1/search/runs/{run_id} -> 200 { status, providers_attempted, providers_succeeded, matches_found }
GET  /v1/search/matches?user_ref=&since= -> 200 { matches: [...] }
```

Models (`http/models.py`):

```python
class SeedCreateRequest(ServiceModel):
    user_ref: UserRef
    seed_kind: Literal["enrolment", "user_supplied", "public_profile"]
    source_object_uri: str          # http(s) URL into the proxy's S3 (presigned GET)

class SeedCreateResponse(BaseModel):
    seed_id: UUID

class SearchCreateRequest(ServiceModel):
    user_ref: UserRef
    seed_id: UUID
    providers: list[str] | None = None

class SearchCreateResponse(BaseModel):
    run_id: UUID

class SearchRunStatusResponse(BaseModel):
    status: Literal["queued", "running", "completed"]
    providers_attempted: list[str]
    providers_succeeded: list[str]   # MUST be distinguishable from attempted
    matches_found: int

class SearchMatchItem(BaseModel):
    match_id: UUID
    run_id: UUID
    provider_id: str
    image_url: str
    page_url: str | None
    score_kind: Literal["numeric", "categorical"]
    provider_score: float | None     # presentation-layer float; DB keeps exact NUMERIC
    provider_category: str | None
    query_quality: str | None
    band: str
    created_at: datetime

class SearchMatchesResponse(BaseModel):
    matches: list[SearchMatchItem]
```

Route rules:
- All under `Depends(require_service_token)`.
- POST /v1/seeds: `source_object_uri` must start `http://`/`https://` else 422 (pydantic
  validator on the model).
- POST /v1/search: seed must exist AND belong to `user_ref` else 404 `seed_not_found`
  (not-yours is indistinguishable from not-exists — no cross-user probing). `providers` entries
  validated via `parse_provider_id` + membership in `enabled_provider_ids()`; unknown → 422
  `unknown_provider`. Default = all enabled. **202**: the route only enqueues (store.create_run
  writes run + outbox row transactionally); discovery is slow and rate-limited, never synchronous.
- GET run: 404 `run_not_found` when absent.
- GET matches: `user_ref` required query param (UUID), `since` optional ISO datetime.

- [ ] **Step 1: Failing tests** — auth 401 without token on all four; 201/202 happy paths (fake
  store records call args, run create returns UUID); wrong-user seed → 404; unknown provider →
  422; run status echoes attempted vs succeeded distinctly; matches serialisation carries
  score_kind + one numeric and one categorical row.
- [ ] **Step 2: Run, verify fail**
- [ ] **Step 3: Implement** routes + deps + app wiring.
- [ ] **Step 4: Run, verify pass** (whole `tests/`, not just the new file)
- [ ] **Step 5: Commit** `feat: seeds + search endpoints — async runs via outbox`

---

### Task 11: Docs — Web Search naming, adapter layer reference

**Files:**
- Modify: `CLAUDE.md` §7.1 (and §2's external-deps line if wording warrants)
- Modify: `ARCHITECTURE.md` (component reference — new §3.7)

Neither file literally says "Media Search" today (verified by grep 2026-08-07) — CLAUDE.md §7
simply never names Hive's product. The correction is therefore *additive*: name the product
correctly and record the trap.

- [ ] **Step 1: CLAUDE.md §7.1** — extend the image-search bullet to name **Hive Web Search**
  (reverse image search, ~25B indexed images) alongside TinEye/Google, and add the warning: Hive's
  separately-named "Media Search" product matches movies/TV content — not ours — and **which
  product a key hits is determined by the Hive project the key belongs to, not the URL**; a key
  provisioned against the wrong project returns plausible-looking wrong results, not an error.
- [ ] **Step 2: ARCHITECTURE.md** — add §3.7 "Search provider adapters (v1, built)": adapter
  layer summary (raw scores, score_kind numeric|categorical, raw_response verbatim, review-band
  only until calibrated), the two integrated providers with their correct product names, and the
  Hive key→project note.
- [ ] **Step 3: Commit** `docs: Hive product is Web Search; key's project selects the product`

---

### Task 12: Full verification + real-provider E2E

- [ ] **Step 1: Full gates**
  - `.venv/Scripts/python -m pytest` (all green; DB tests need compose Postgres up)
  - `.venv/Scripts/python -m mypy` (strict, zero errors)
  - `.venv/Scripts/ruff check .`
  - `grep -rE "\* *100|score *\* " src/` → nothing
  - grep `webEntities` in `src/` → only the ignore-comment in `google.py`
  - grep `SearchFacesByImage` in `src/` → nothing
- [ ] **Step 2: Real E2E (environment-gated)** — `devtools/run_search_e2e.py`: against compose
  Postgres + real keys from `.env.local`, create a seed with a **publicly reachable** image URL
  (providers fetch the URL themselves; localhost fake-s3 is unreachable to them), create a run,
  execute it in-process via `claim_run` + `execute_run` with real adapters (no SQS needed for the
  gate), then print per-provider status + match counts and assert both providers returned ok.
  Costs real money (Hive per-call). If Docker/keys are unavailable, report the gate open —
  do not fake it.
- [ ] **Step 3: Commit** `Step 5: provider adapter interface + Hive and Google adapters`

## Self-review notes

- Spec coverage: migration ✔ (Task 1, renumbered 0004), interface ✔ (3), Hive ✔ (5), Google ✔ (6),
  endpoints ✔ (10), async-via-outbox ✔ (7+9), partial-coverage visibility ✔ (7,8,10),
  band=review ✔ (7), raw_response verbatim ✔ (5,6,7), docs ✔ (11), done-when ✔ (12).
- Deliberate additions beyond the spec's literal text, each forced by a spec requirement:
  `search_runs.status/claimed_at` (GET run must answer `status`; at-least-once needs a claim),
  the worker process (202-async contract needs a consumer; `search:runs` queue exists since
  step 1 — no new queue, and the worker is that queue's specified consumer per ARCHITECTURE §6),
  Google config keys (second adapter needs credentials).
- Out of scope, confirmed absent: URL normalisation (step 6 — `urlhash.py` is the marked swap
  point), calibration/banding (step 7 — band hardcoded 'review' with threshold_config recording
  why), budgets/circuit breakers/kill switches (step 8 — `budget_exceeded` exists in the status
  enum only so the type doesn't change shape at step 8).
