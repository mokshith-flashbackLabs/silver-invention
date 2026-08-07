# Step 6 — URL Normalisation, Dedup, Attestations — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace per-run `search_matches` with stable `infringements` + per-provider `attestations` (rescan = UPDATE, never INSERT), ship URL normalisation v1 as the dedup key, and add the `raw_response` retention job.

**Architecture:** Expand/contract migration pair (0005 creates the new tables and columns; 0006 drops `search_matches` after no code references it) so every commit stays green. `search/urlhash.py` is rewritten in place — it was built as the single swap point. The store gains upsert-based `record_infringements` / `list_infringements`; the provider model gains `page_urls: list[str]` so one Hive match with N backlinks fans out to N infringements.

**Tech Stack:** Python 3.11, FastAPI, Postgres 16 via psycopg 3 (raw SQL, no ORM), pydantic 2, pytest against a real throwaway Postgres (`tests/db.py`).

## Global Constraints

- Work on branch `step-6-url-dedup` off `main`.
- CI gates that must pass at every commit: `ruff check .`, `mypy` (strict), `pytest tests/ -v` (Postgres running: `docker compose -f docker-compose.local.yml up -d`).
- Commits end with `Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>` — never the Claude trailer.
- Adapters return RAW scores, never normalise (CLAUDE.md §7.2). Nothing in this step touches score values.
- `band = 'review'` unconditionally — calibration/banding is step 7. Cost controls are step 8. Nothing sets `url_alive = false` (recheck loop is not in v1).
- Never collapse across users: `UNIQUE (user_ref, url_hash)` — cross-user is not dedup.
- Invariant #9: no `bytea` columns; `*_url`/`*_uri` column names are allowed (`canonical_url`, `page_url`, `image_url` all pass the lint).
- Messages carry IDs, never payloads; every consumer stays idempotent.
- Rescans must never produce a duplicate-key error and never insert a duplicate: `INSERT ... ON CONFLICT ... DO UPDATE` everywhere.
- STOP at the end of this plan. Do not begin step 7.

## Deviations from the step-6 prompt (all deliberate, all small)

1. **Migration numbering:** the prompt says "Migration 0003" but the repo is already at `0004_provider_score_shape`. The work lands as `0005_infringements_attestations` (expand) + `0006_drop_search_matches` (contract). Two migrations, not one, so the commit that drops the table comes *after* the commit that stops writing to it.
2. **`keyed_on` column added to `infringements`** (not in the prompt's DDL): the prompt's dedup rules require "fall back to keying on image_url, and record which was used" — this column is that record.
3. **`provider_calls.raw_response` NOT NULL is dropped** in 0005: the retention job must be able to null it (0001 declared it `NOT NULL`).
4. **Pre-existing `content_urls` rows are tagged `normalisation_version = 'v0-interim'`**, not `'v1'`: they were hashed by the step-5 raw-URL hash, and labelling them v1 would be the "silent split" the version column exists to prevent.
5. **`GET /v1/search/matches` is replaced by `GET /v1/search/infringements`**: the response shape must change anyway once `search_matches` is gone, and the infringement is the thing a user acts on.

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `src/imageshield/search/urlhash.py` | rewrite | normalisation v1: `canonicalise`, `url_hash`, `NORMALISATION_VERSION`, `source_domain` |
| `migrations/0005_infringements_attestations.{up,down}.sql` | create | new tables, content_urls columns, raw_response nullability, data migration |
| `migrations/0006_drop_search_matches.{up,down}.sql` | create | drop superseded table |
| `src/imageshield/search/provider.py` | modify | `ProviderMatch.page_url` → `page_urls: list[str]` |
| `src/imageshield/search/hive.py` | modify | all backlinks, not `backlinks[0]` |
| `src/imageshield/search/google.py` | modify | `page_urls=[url]` for page_match, `[]` otherwise |
| `src/imageshield/search/models.py` | modify | `MatchRow` → `InfringementRow` + `AttestationRow` |
| `src/imageshield/search/store.py` | modify | `record_infringements`, `list_infringements`, run-count from attestations |
| `src/imageshield/search/runner.py` | modify | call rename only |
| `src/imageshield/search/retention.py` | create | raw_response retention job + CLI |
| `src/imageshield/config.py` | modify | `raw_response_retention_days: int = 90` |
| `src/imageshield/http/models.py` | modify | `InfringementItem`/`AttestationItem`/`InfringementsResponse` replace match models |
| `src/imageshield/http/routes/search.py` | modify | `GET /v1/search/infringements` |
| `devtools/run_search_e2e.py` | modify | read infringements instead of matches |
| `SCHEMA.md` | modify | document new tables |
| tests | per task | see tasks |

**Execution order matters:** Task 1 (pure normalisation) → Task 2 (expand migration) → Task 3 (page_urls) → Task 4 (store write path) → Task 5 (read path + routes) → Task 6 (contract migration) → Task 7 (52-rescan regression) → Task 8 (retention) → Task 9 (docs + final gates). Tasks 3–5 leave `search_matches` present but unwritten; task 6 removes it.

---

### Task 0: Branch

- [ ] **Step 1:** `git checkout -b step-6-url-dedup` (from up-to-date `main`).

---

### Task 1: URL normalisation v1 (`urlhash.py` rewrite)

**Files:**
- Rewrite: `src/imageshield/search/urlhash.py`
- Rewrite: `tests/test_urlhash.py`
- Modify: `src/imageshield/search/store.py` (call-site rename only: `interim_url_hash` → `url_hash`)

**Interfaces:**
- Produces: `NORMALISATION_VERSION: str = "v1"`, `canonicalise(url: str) -> str`, `url_hash(url: str) -> UrlHash`, `source_domain(url: str) -> str`. Later tasks import all four from `imageshield.search.urlhash`.
- Consumes: `imageshield.types.UrlHash`, `parse_url_hash`.

- [ ] **Step 1: Write the failing tests** — replace `tests/test_urlhash.py` entirely:

```python
"""Normalisation v1 — the dedup key. url_hash = sha256(canonical_url).

These tests pin the v1 rules permanently: changing any of them invalidates
every stored hash, so a failure here means you are shipping v2, not fixing
a bug (see NORMALISATION_VERSION)."""

from __future__ import annotations

import hashlib

from imageshield.search.urlhash import (
    NORMALISATION_VERSION,
    canonicalise,
    source_domain,
    url_hash,
)


def test_version_is_v1() -> None:
    assert NORMALISATION_VERSION == "v1"


def test_hash_is_sha256_of_canonical_url() -> None:
    url = "https://Example.com/a?b=1#frag"
    assert url_hash(url) == hashlib.sha256(canonicalise(url).encode()).hexdigest()


def test_five_tracking_variants_produce_one_hash() -> None:
    base = "https://example.com/gallery/image?id=42"
    variants = [
        "https://example.com/gallery/image?id=42&utm_source=x",
        "https://example.com/gallery/image?utm_medium=y&id=42&utm_campaign=z",
        "https://example.com/gallery/image?fbclid=abc123&id=42",
        "https://example.com/gallery/image?id=42&gclid=g1&igshid=i1",
        "https://example.com/gallery/image?_ga=1.2&id=42&ref=home&yclid=9",
    ]
    hashes = {url_hash(v) for v in variants}
    assert hashes == {url_hash(base)}


def test_spec_example_canonicalises_but_scheme_is_preserved() -> None:
    # The two URLs from the step-6 spec. Scheme differs -> NOT equal.
    messy = "http://Example.COM:80/a/./b/../c/?b=2&utm_source=x&a=1#frag"
    assert canonicalise(messy) == "http://example.com/a/c?a=1&b=2"
    https_twin = "https://example.com/a/c?a=1&b=2"
    assert canonicalise(https_twin) == https_twin
    assert url_hash(messy) != url_hash(https_twin)  # http vs https is a real difference
    assert url_hash(messy) == url_hash("http://example.com/a/c?a=1&b=2")


def test_host_is_lowercased_and_punycoded_path_case_preserved() -> None:
    assert canonicalise("https://ExÄmple.com/Photo.JPG") == (
        "https://xn--exmple-cua.com/Photo.JPG"
    )
    # paths are case-sensitive: these must NOT collapse
    assert url_hash("https://example.com/A") != url_hash("https://example.com/a")


def test_default_ports_stripped_others_kept() -> None:
    assert canonicalise("http://example.com:80/x") == "http://example.com/x"
    assert canonicalise("https://example.com:443/x") == "https://example.com/x"
    assert canonicalise("https://example.com:8443/x") == "https://example.com:8443/x"


def test_fragment_stripped_and_query_sorted() -> None:
    assert canonicalise("https://example.com/p?b=2&a=1#top") == "https://example.com/p?a=1&b=2"


def test_percent_encoding_unreserved_decoded_reserved_uppercased() -> None:
    # %7e is '~' (unreserved) -> decoded; %2f is '/' (reserved) -> kept, hex uppercased
    assert canonicalise("https://example.com/%7euser/a%2fb") == (
        "https://example.com/~user/a%2Fb"
    )


def test_trailing_slash_stripped_except_bare_root() -> None:
    assert canonicalise("https://example.com/a/") == "https://example.com/a"
    assert canonicalise("https://example.com/") == "https://example.com/"
    assert canonicalise("https://example.com") == "https://example.com/"
    assert url_hash("https://example.com") == url_hash("https://example.com/")


def test_garbage_is_canonicalised_verbatim_never_raises() -> None:
    assert canonicalise("not a url") == "not a url"
    assert canonicalise("http://bad:port:99999/") == "http://bad:port:99999/"
    assert len(url_hash("not a url")) == 64


def test_source_domain_uses_canonical_host() -> None:
    assert source_domain("https://Sub.Example.com:443/x/y.jpg") == "sub.example.com"
    assert source_domain("not a url") == "unknown"
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_urlhash.py -v`
Expected: FAIL — `ImportError: cannot import name 'canonicalise'`.

- [ ] **Step 3: Rewrite `src/imageshield/search/urlhash.py`:**

```python
"""URL normalisation v1 — the dedup key (CLAUDE.md §8 step 6).

``url_hash = sha256(canonical_url)``. Changing any rule here invalidates
every stored hash, so the rules are versioned: ``NORMALISATION_VERSION`` is
stored on every ``content_urls`` row, and a future v2 must bump it rather
than silently splitting the dedup.

v1 rules, in order:
 1. lowercase the scheme and host
 2. host to punycode (IDN normalisation)
 3. strip default ports (:80 on http, :443 on https)
 4. strip the fragment entirely
 5. resolve dot segments (/a/./b/../c -> /a/c)
 6. PRESERVE path case — paths are case-sensitive, hosts are not
 7. strip tracking params (utm_*, fbclid, gclid, msclkid, mc_eid, ref,
    ref_src, source, igshid, _ga, yclid)
 8. sort remaining query params by key
 9. normalise percent-encoding: uppercase hex, decode unreserved chars
10. strip a single trailing slash EXCEPT on a bare-root path

Deliberate edge behaviour: an empty path becomes "/" (so example.com and
example.com/ collapse); userinfo (user:pass@) is dropped; a URL that cannot
be parsed at all canonicalises to itself verbatim — the store must never
crash on a garbage provider URL, it just dedups that URL exact-match only.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit

from imageshield.types import UrlHash, parse_url_hash

NORMALISATION_VERSION = "v1"

_TRACKING_EXACT = frozenset(
    {"fbclid", "gclid", "msclkid", "mc_eid", "ref", "ref_src",
     "source", "igshid", "_ga", "yclid"}
)
_TRACKING_PREFIX = "utm_"
_DEFAULT_PORTS = {"http": 80, "https": 443}
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
_PCT_RE = re.compile(r"%([0-9A-Fa-f]{2})")


def url_hash(url: str) -> UrlHash:
    return parse_url_hash(
        hashlib.sha256(canonicalise(url).encode("utf-8")).hexdigest()
    )


def canonicalise(url: str) -> str:
    raw = url.strip()
    try:
        parts = urlsplit(raw)
        port = parts.port  # raises ValueError on a malformed port
    except ValueError:
        return raw
    if not parts.scheme or not parts.hostname:
        return raw

    scheme = parts.scheme.lower()
    host = _idna(parts.hostname)  # urlsplit already lowercased it
    netloc = (
        host
        if port is None or port == _DEFAULT_PORTS.get(scheme)
        else f"{host}:{port}"
    )
    path = _strip_trailing_slash(
        _resolve_dot_segments(_normalise_pct(parts.path)) or "/"
    )
    query = _normalise_query(parts.query)
    return f"{scheme}://{netloc}{path}" + (f"?{query}" if query else "")


def source_domain(url: str) -> str:
    try:
        host = urlsplit(canonicalise(url)).hostname
    except ValueError:
        return "unknown"
    return host or "unknown"


def _idna(host: str) -> str:
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def _normalise_pct(value: str) -> str:
    """Uppercase percent-escape hex; decode escapes of unreserved chars.
    Never decodes reserved chars (%2F must not become a path separator)."""

    def _one(match: re.Match[str]) -> str:
        char = chr(int(match.group(1), 16))
        return char if char in _UNRESERVED else f"%{match.group(1).upper()}"

    return _PCT_RE.sub(_one, value)


def _resolve_dot_segments(path: str) -> str:
    if not path:
        return ""
    segments = path.split("/")
    out: list[str] = []
    for seg in segments:
        if seg == ".":
            continue
        if seg == "..":
            if len(out) > 1:
                out.pop()
            continue
        out.append(seg)
    # A trailing "." or ".." names a directory — keep its trailing slash
    # (rule 10 usually strips it again; this keeps /a/b/.. == /a/).
    if segments[-1] in (".", "..") and (not out or out[-1] != ""):
        out.append("")
    return "/".join(out)


def _strip_trailing_slash(path: str) -> str:
    return path[:-1] if path.endswith("/") and len(path) > 1 else path


def _normalise_query(query: str) -> str:
    if not query:
        return ""
    kept: list[str] = []
    for param in query.split("&"):
        if not param:
            continue
        key = param.split("=", 1)[0].lower()
        if key.startswith(_TRACKING_PREFIX) or key in _TRACKING_EXACT:
            continue
        kept.append(_normalise_pct(param))
    # sort by key, then whole param, so duplicate keys are deterministic
    kept.sort(key=lambda p: (p.split("=", 1)[0], p))
    return "&".join(kept)
```

- [ ] **Step 4: Update the one call site.** In `src/imageshield/search/store.py`, change the import and the one use (module docstring bullet about the interim hash should be updated too):

```python
from imageshield.search.urlhash import source_domain, url_hash
```
and in `record_matches`: `url_hash_value = url_hash(match.image_url)` (rename the local so it doesn't shadow the function; pass `url_hash_value` to both SQL params).

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_urlhash.py tests/test_search_store.py -v && ruff check . && mypy`
Expected: PASS (store tests skip if Postgres is down — start compose if so).

- [ ] **Step 6: Commit**

```bash
git add src/imageshield/search/urlhash.py src/imageshield/search/store.py tests/test_urlhash.py
git commit -m "Step 6: URL normalisation v1 replaces the interim raw hash

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

### Task 2: Migration 0005 — expand (new tables, columns, data migration)

**Files:**
- Create: `migrations/0005_infringements_attestations.up.sql`
- Create: `migrations/0005_infringements_attestations.down.sql`
- Modify: `tests/test_migrations.py` (add 0005 assertions; do NOT touch `CORE_TABLES` yet — `search_matches` still exists)

**Interfaces:**
- Produces: tables `infringements` (with `keyed_on`), `attestations`; `content_urls.normalisation_version`/`canonical_url`; nullable `provider_calls.raw_response`. Task 4's SQL depends on these exact names.

- [ ] **Step 1: Write failing tests** — append to `tests/test_migrations.py`:

```python
def test_0005_creates_infringements_and_attestations(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    result = run_migrate(throwaway_db, "up")
    assert result.returncode == 0, result.stderr
    with psycopg.connect(throwaway_db) as conn:
        tables = _table_names(conn)
        assert {"infringements", "attestations"} <= tables
        assert "search_matches" in tables  # dropped by 0006, not 0005
        # content_urls carries the normalisation contract
        cols = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'content_urls'"
            ).fetchall()
        }
        assert {"normalisation_version", "canonical_url"} <= cols
        # retention job must be able to null raw_response
        nullable = conn.execute(
            "SELECT is_nullable FROM information_schema.columns"
            " WHERE table_name = 'provider_calls' AND column_name = 'raw_response'"
        ).fetchone()
        assert nullable == ("YES",)


def test_0005_migrates_search_matches_rows(throwaway_db: str) -> None:
    """Rows written under step 5 become one infringement per (user, url)
    with one attestation per provider, counts preserved."""
    run_migrate(throwaway_db, "down", "--all")
    result = run_migrate(throwaway_db, "up", "--target", "0004")
    if result.returncode != 0:
        # migrate.py may not support --target; apply 0001-0004 by hand then
        pytest.skip("migrate.py has no --target; covered by manual seed below")
```

**NOTE to implementer:** check `scripts/migrate.py` for a `--target`/`--steps` option first. If `up` has no way to stop at 0004, seed the data differently: run `down --all`, then `up` fully, and instead test the *migration SQL's data transform* by seeding `search_matches` is impossible (already dropped? no — 0006 not written yet, table exists). So: `run_migrate(db, "down", "--steps", "1")` to revert 0005 only, seed two `search_matches` rows (same user+url_hash, providers hive and google — remember to first insert the `content_urls` row and a `search_runs` row and its `search_seeds` row to satisfy FKs), then `run_migrate(db, "up")` and assert: 1 row in `infringements` with `seen_count = 2`, 2 rows in `attestations` each with `confirm_count = 1`, and `content_urls.normalisation_version = 'v0-interim'` on the pre-existing row. Write the final test with whichever mechanism works; the assertion set is what matters.

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_migrations.py -v`
Expected: FAIL — 0005 files don't exist.

- [ ] **Step 3: Write `migrations/0005_infringements_attestations.up.sql`:**

```sql
-- Step 6 (expand half): separate the thing found (infringements) from the
-- observations of it (attestations). search_matches keys per RUN, so a
-- weekly rescan of an unchanged corpus inserts forever (~208M rows/yr at
-- 100k users) — the same failure class as the old system's
-- matches[].seenInScans (weeklyInfringementScanner.js:1016). The new shape
-- makes a rescan an UPDATE. search_matches itself is dropped by 0006, after
-- the code stops writing to it.

-- Stable. One row per (user_ref, url_hash). The thing the user acts on.
CREATE TABLE infringements (
  infringement_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_ref         UUID NOT NULL,
  url_hash         TEXT NOT NULL REFERENCES content_urls(url_hash),
  page_url         TEXT NOT NULL,
  image_url        TEXT,
  -- The dedup key is the PAGE when the provider reports one (backlinks);
  -- otherwise we fall back to the image URL and record that here.
  keyed_on         TEXT NOT NULL DEFAULT 'page_url'
                   CHECK (keyed_on IN ('page_url', 'image_url')),
  first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  seen_count       INT NOT NULL DEFAULT 1,
  url_alive        BOOLEAN NOT NULL DEFAULT true,
  last_checked_at  TIMESTAMPTZ,
  band             TEXT NOT NULL DEFAULT 'review',
  status           TEXT NOT NULL DEFAULT 'new',
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_ref, url_hash)
);

CREATE INDEX infringements_user_idx ON infringements (user_ref, last_seen_at DESC);
CREATE INDEX infringements_review_idx ON infringements (band) WHERE band = 'review';

-- One row per (infringement, provider). UPDATED on rescan, never appended.
CREATE TABLE attestations (
  attestation_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  infringement_id    UUID NOT NULL REFERENCES infringements(infringement_id) ON DELETE CASCADE,
  provider_id        TEXT NOT NULL REFERENCES providers(provider_id),
  score_kind         TEXT NOT NULL,
  provider_score     NUMERIC(6,4),
  provider_category  TEXT,
  query_quality      TEXT,
  score_version      TEXT NOT NULL,
  first_confirmed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_confirmed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  confirm_count      INT NOT NULL DEFAULT 1,
  last_run_id        UUID REFERENCES search_runs(run_id),
  UNIQUE (infringement_id, provider_id),
  CONSTRAINT attestation_score_shape CHECK (
    (score_kind = 'numeric'     AND provider_score    IS NOT NULL) OR
    (score_kind = 'categorical' AND provider_category IS NOT NULL)
  )
);

CREATE INDEX attestations_infringement_idx ON attestations (infringement_id);
-- complete_run counts a run's attestations; without this that count is a seq scan
CREATE INDEX attestations_run_idx ON attestations (last_run_id);

-- Versioned, because changing normalisation invalidates every stored hash.
ALTER TABLE content_urls
  ADD COLUMN normalisation_version TEXT NOT NULL DEFAULT 'v1',
  ADD COLUMN canonical_url TEXT;

-- Rows that predate this migration were hashed by the step-5 INTERIM raw
-- hash (sha256 of the unnormalised URL). Label them so a v1 hash never
-- silently mixes with them; canonical_url = the raw url is the honest value.
UPDATE content_urls
SET normalisation_version = 'v0-interim', canonical_url = url;

-- The retention job nulls raw_response past RAW_RESPONSE_RETENTION_DAYS
-- while keeping the metadata row; 0001 declared it NOT NULL.
ALTER TABLE provider_calls ALTER COLUMN raw_response DROP NOT NULL;

-- Migrate existing search_matches rows (dev/E2E data only — this repo has
-- never deployed). Grouping matches the new dedup key as closely as the old
-- shape allows; the url_hash stays whatever it was (v0-interim).
WITH grouped AS (
  SELECT user_ref, url_hash,
         min(created_at) AS first_at, max(created_at) AS last_at,
         count(*) AS n
  FROM search_matches
  GROUP BY user_ref, url_hash
), first_rows AS (
  SELECT DISTINCT ON (user_ref, url_hash)
         user_ref, url_hash, page_url, image_url
  FROM search_matches
  ORDER BY user_ref, url_hash, created_at
)
INSERT INTO infringements (user_ref, url_hash, page_url, image_url, keyed_on,
                           first_seen_at, last_seen_at, seen_count)
SELECT g.user_ref, g.url_hash,
       COALESCE(f.page_url, f.image_url), f.image_url,
       CASE WHEN f.page_url IS NULL THEN 'image_url' ELSE 'page_url' END,
       g.first_at, g.last_at, g.n
FROM grouped g
JOIN first_rows f USING (user_ref, url_hash);

WITH latest AS (
  SELECT DISTINCT ON (user_ref, url_hash, provider_id)
         user_ref, url_hash, provider_id, score_kind, provider_score,
         provider_category, query_quality, score_version, run_id
  FROM search_matches
  ORDER BY user_ref, url_hash, provider_id, created_at DESC
), counts AS (
  SELECT user_ref, url_hash, provider_id,
         count(*) AS n, min(created_at) AS first_at, max(created_at) AS last_at
  FROM search_matches
  GROUP BY user_ref, url_hash, provider_id
)
INSERT INTO attestations (infringement_id, provider_id, score_kind,
                          provider_score, provider_category, query_quality,
                          score_version, first_confirmed_at, last_confirmed_at,
                          confirm_count, last_run_id)
SELECT i.infringement_id, l.provider_id, l.score_kind,
       l.provider_score, l.provider_category, l.query_quality,
       l.score_version, c.first_at, c.last_at, c.n, l.run_id
FROM latest l
JOIN counts c USING (user_ref, url_hash, provider_id)
JOIN infringements i
  ON i.user_ref = l.user_ref AND i.url_hash = l.url_hash;
```

- [ ] **Step 4: Write `migrations/0005_infringements_attestations.down.sql`:**

```sql
-- Reverses 0005's shape. Data migrated INTO infringements/attestations is
-- not copied back into search_matches (lossy, documented — the up-migration
-- source rows are still there until 0006 drops the table).

DROP TABLE attestations;
DROP TABLE infringements;

ALTER TABLE content_urls
  DROP COLUMN normalisation_version,
  DROP COLUMN canonical_url;

UPDATE provider_calls SET raw_response = '{}'::jsonb WHERE raw_response IS NULL;
ALTER TABLE provider_calls ALTER COLUMN raw_response SET NOT NULL;
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_migrations.py -v`
Expected: PASS, including the existing up/down-cycle tests (they run `down --all` — verify the down file reverses cleanly).

- [ ] **Step 6: Commit**

```bash
git add migrations/0005_infringements_attestations.up.sql migrations/0005_infringements_attestations.down.sql tests/test_migrations.py
git commit -m "Step 6: migration 0005 — infringements + attestations (expand half)

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

### Task 3: `ProviderMatch.page_urls` — carry every backlink

**Files:**
- Modify: `src/imageshield/search/provider.py`
- Modify: `src/imageshield/search/hive.py` (`_to_match`)
- Modify: `src/imageshield/search/google.py` (`_to_matches`)
- Modify: `src/imageshield/search/store.py` (interim shim: `page_urls[0] if page_urls else None` into the old `page_url` param)
- Modify: `tests/test_hive_adapter.py`, `tests/test_google_adapter.py`, `tests/test_search_store.py`, `tests/test_search_runner.py` (fixture field rename)

**Interfaces:**
- Produces: `ProviderMatch.page_urls: list[str]` (replaces `page_url: str | None`). Order-preserving, deduplicated, may be empty. Task 4 fans one match out to one infringement per entry.

- [ ] **Step 1: Write the failing tests.** In `tests/test_hive_adapter.py`, find the assertions on `page_url` (lines ~69-73) and change to:

```python
    assert first.page_urls == [
        "https://page.example/post/1",
        "https://page.example/post/2",
        "https://mirror.example/copy",
    ]
    ...
    assert second.page_urls == []
```

and extend the canned Hive response fixture in that test so the first match's `backlinks` carries those three entries (objects with a `url` key each — follow the fixture's existing shape) plus one duplicate and one malformed entry (`{"noturl": true}`) to pin dedup-and-skip behaviour. In `tests/test_google_adapter.py` (~lines 67-69):

```python
    assert by_category["full_match"].page_urls == []
    assert by_category["page_match"].page_urls == ["https://a/page.html"]  # it IS a page
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_hive_adapter.py tests/test_google_adapter.py -v`
Expected: FAIL — `ProviderMatch` has no field `page_urls`.

- [ ] **Step 3: Implement.** In `provider.py`:

```python
class ProviderMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_url: str
    # EVERY page carrying the matched image (Hive backlinks[]), order
    # preserved, deduplicated. One infringement per entry (step 6 dedup
    # rules); empty -> the store keys on image_url instead.
    page_urls: list[str]
    provider_score: Decimal | None  # numeric providers only — RAW, never rescaled
    provider_category: str | None   # categorical providers only
    query_quality: str | None
```

In `hive.py` replace `_to_match`'s backlink handling:

```python
def _to_match(entry: dict[str, Any], query_quality: str | None) -> ProviderMatch:
    backlinks = entry.get("backlinks")
    page_urls: list[str] = []
    if isinstance(backlinks, list):
        for link in backlinks:
            if isinstance(link, dict) and link.get("url") is not None:
                url = str(link["url"])
                if url not in page_urls:
                    page_urls.append(url)
    return ProviderMatch(
        image_url=str(entry.get("url", "")),
        page_urls=page_urls,
        provider_score=_raw_score(entry),
        provider_category=None,
        query_quality=query_quality,
    )
```

In `google.py`:

```python
            matches.append(
                ProviderMatch(
                    image_url=url,
                    # a page_match entry IS a page; full/partial are images
                    # whose host page Google does not report.
                    page_urls=[url] if category == "page_match" else [],
                    provider_score=None,  # NULL. Always. Never synthesised.
                    provider_category=category,
                    query_quality=None,
                )
            )
```

In `store.py` `record_matches` (temporary until Task 4): `"page_url": match.page_urls[0] if match.page_urls else None`. Update `_hive_match`/`_google_match` helpers in `tests/test_search_store.py` and the `ProviderMatch` in `tests/test_search_runner.py` to build `page_urls=[...]` instead of `page_url=...`.

- [ ] **Step 4: Run all gates**

Run: `pytest tests/ -v && ruff check . && mypy`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "Step 6: ProviderMatch carries all backlinks, not backlinks[0]

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

### Task 4: Store write path — `record_infringements` upserts

**Files:**
- Modify: `src/imageshield/search/store.py` (replace `record_matches` + `_INSERT_MATCH_SQL` + `_UPSERT_URL_SQL` + `_COMPLETE_RUN_SQL`)
- Modify: `src/imageshield/search/runner.py` (call rename)
- Modify: `tests/test_search_runner.py`, `tests/test_search_worker.py` (fake-store method rename)
- Modify: `tests/test_search_store.py` (rewrite match-recording tests as the four dedup acceptance tests)

**Interfaces:**
- Consumes: Task 1's `canonicalise`/`url_hash`/`NORMALISATION_VERSION`/`source_domain`; Task 2's tables; Task 3's `page_urls`.
- Produces: `SearchStore.record_infringements(run_id: UUID, user_ref: UserRef, provider: ProviderDescriptor, matches: Sequence[ProviderMatch]) -> int` (returns infringements touched). `complete_run` unchanged signature, now counts `attestations WHERE last_run_id = run_id`. `list_matches` is UNTOUCHED in this task (Task 5 replaces it; `search_matches` still exists so it still compiles and runs).

- [ ] **Step 1: Write the failing acceptance tests.** In `tests/test_search_store.py`, delete `test_record_matches_band_review_dedupes_and_null_scores` and update the two other tests that call `record_matches` (`test_complete_run_sets_status_succeeded_and_count`, `test_list_matches_filters_by_user_and_since` — for the latter just switch the call; its read side is replaced in Task 5). Update `_hive_match` to accept pages:

```python
def _hive_match(
    url: str, score: str = "0.87", pages: list[str] | None = None
) -> ProviderMatch:
    return ProviderMatch(
        image_url=url,
        page_urls=pages if pages is not None else [f"{url}?page=1"],
        provider_score=Decimal(score),
        provider_category=None,
        query_quality="good",
    )
```

Add the four dedup acceptance tests (spec "Done when"):

```python
async def test_cross_provider_is_one_infringement_two_attestations(
    store: PostgresSearchStore, migrated_db: str
) -> None:
    """Hive and Google both finding page X for user Y -> 1 infringement,
    2 attestations. provider_count is an agreement signal."""
    user_ref = _user()
    _, run_id = await _seeded_run(store, user_ref)
    page = "https://site.example/post/9"

    n_hive = await store.record_infringements(
        run_id, user_ref, HIVE_DESC,
        [_hive_match("https://cdn.example/img.jpg", pages=[page])],
    )
    n_google = await store.record_infringements(
        run_id, user_ref, GOOGLE_DESC,
        [_google_match(page, "page_match")],
    )
    assert (n_hive, n_google) == (1, 1)

    infringements = _query(
        migrated_db,
        "SELECT infringement_id, page_url, keyed_on, seen_count FROM infringements"
        " WHERE user_ref = %s", (user_ref,),
    )
    assert len(infringements) == 1
    assert infringements[0][1] == page
    assert infringements[0][2] == "page_url"
    assert infringements[0][3] == 2  # two provider observations

    attestations = _query(
        migrated_db,
        "SELECT provider_id, confirm_count, last_run_id FROM attestations"
        " WHERE infringement_id = %s ORDER BY provider_id",
        (infringements[0][0],),
    )
    assert [(a[0], a[1]) for a in attestations] == [("google", 1), ("hive", 1)]
    assert all(a[2] == run_id for a in attestations)


async def test_cross_user_is_never_dedup(
    store: PostgresSearchStore, migrated_db: str
) -> None:
    """Same URL for two users -> 2 infringements. The boundary that keeps
    one person's matches from leaking to another."""
    user_a, user_b = _user(), _user()
    _, run_a = await _seeded_run(store, user_a)
    _, run_b = await _seeded_run(store, user_b)
    match = _hive_match("https://cdn.example/x.jpg", pages=["https://site.example/p"])

    await store.record_infringements(run_a, user_a, HIVE_DESC, [match])
    await store.record_infringements(run_b, user_b, HIVE_DESC, [match])

    rows = _query(migrated_db, "SELECT DISTINCT user_ref FROM infringements")
    assert len(rows) == 2


async def test_three_backlinks_are_three_infringements(
    store: PostgresSearchStore, migrated_db: str
) -> None:
    user_ref = _user()
    _, run_id = await _seeded_run(store, user_ref)
    pages = [f"https://site{i}.example/post" for i in range(3)]

    touched = await store.record_infringements(
        run_id, user_ref, HIVE_DESC,
        [_hive_match("https://cdn.example/img.jpg", pages=pages)],
    )
    assert touched == 3

    rows = _query(
        migrated_db,
        "SELECT page_url, image_url, keyed_on FROM infringements"
        " WHERE user_ref = %s ORDER BY page_url", (user_ref,),
    )
    assert [r[0] for r in rows] == sorted(pages)
    assert all(r[1] == "https://cdn.example/img.jpg" for r in rows)
    assert all(r[2] == "page_url" for r in rows)


async def test_same_page_twice_in_one_run_collapses_before_writing(
    store: PostgresSearchStore, migrated_db: str
) -> None:
    """Same page with two different image URLs in ONE batch -> ONE
    infringement, seen_count 1, confirm_count 1 (collapsed, not re-counted).
    Tracking-param variants of the page collapse too (normalisation v1)."""
    user_ref = _user()
    _, run_id = await _seeded_run(store, user_ref)

    touched = await store.record_infringements(
        run_id, user_ref, HIVE_DESC,
        [
            _hive_match("https://cdn.example/a.jpg", pages=["https://site.example/p"]),
            _hive_match(
                "https://cdn.example/b.jpg",
                pages=["https://site.example/p?utm_source=share"],
            ),
        ],
    )
    assert touched == 1

    rows = _query(
        migrated_db,
        "SELECT seen_count FROM infringements WHERE user_ref = %s", (user_ref,),
    )
    assert rows == [(1,)]
    counts = _query(
        migrated_db,
        "SELECT confirm_count FROM attestations a JOIN infringements i"
        " ON a.infringement_id = i.infringement_id WHERE i.user_ref = %s",
        (user_ref,),
    )
    assert counts == [(1,)]


async def test_image_url_fallback_when_no_backlink(
    store: PostgresSearchStore, migrated_db: str
) -> None:
    user_ref = _user()
    _, run_id = await _seeded_run(store, user_ref)

    await store.record_infringements(
        run_id, user_ref, HIVE_DESC,
        [_hive_match("https://cdn.example/only-image.jpg", pages=[])],
    )
    rows = _query(
        migrated_db,
        "SELECT page_url, keyed_on FROM infringements WHERE user_ref = %s",
        (user_ref,),
    )
    assert rows == [("https://cdn.example/only-image.jpg", "image_url")]


async def test_content_urls_carry_canonical_and_version(
    store: PostgresSearchStore, migrated_db: str
) -> None:
    user_ref = _user()
    _, run_id = await _seeded_run(store, user_ref)
    await store.record_infringements(
        run_id, user_ref, HIVE_DESC,
        [_hive_match("https://cdn.example/i.jpg",
                     pages=["https://Site.example/p/?utm_source=x"])],
    )
    rows = _query(
        migrated_db,
        "SELECT url, canonical_url, normalisation_version FROM content_urls",
    )
    assert rows == [
        ("https://Site.example/p/?utm_source=x", "https://site.example/p", "v1")
    ]
```

Also in `test_complete_run_sets_status_succeeded_and_count`, switch the call to `record_infringements` and keep `matches_found == 2` (two distinct pages → two attestations for this run).

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_search_store.py -v`
Expected: FAIL — `PostgresSearchStore` has no `record_infringements`.

- [ ] **Step 3: Implement in `store.py`.** Replace `_UPSERT_URL_SQL`, `_INSERT_MATCH_SQL`, `_COMPLETE_RUN_SQL`; add the two upserts and the pure fan-out helper; replace `record_matches` in both the protocol and the implementation; update the module docstring (the interim-hash bullet and the per-run-unique-index bullet are now wrong — rewrite them to describe the upsert model):

```python
_UPSERT_URL_SQL = """
    INSERT INTO content_urls (url_hash, url, source_domain, canonical_url,
                              normalisation_version)
    VALUES (%(url_hash)s, %(url)s, %(source_domain)s, %(canonical_url)s,
            %(normalisation_version)s)
    ON CONFLICT (url_hash) DO UPDATE SET last_seen_at = now()
"""

# Rescan semantics (step 6): found again -> UPDATE, never a second row.
# Not found -> touched by nothing; a stale last_seen_at IS the signal.
_UPSERT_INFRINGEMENT_SQL = """
    INSERT INTO infringements (user_ref, url_hash, page_url, image_url, keyed_on)
    VALUES (%(user_ref)s, %(url_hash)s, %(page_url)s, %(image_url)s, %(keyed_on)s)
    ON CONFLICT (user_ref, url_hash) DO UPDATE
      SET last_seen_at = now(),
          seen_count = infringements.seen_count + 1
    RETURNING infringement_id
"""

_UPSERT_ATTESTATION_SQL = """
    INSERT INTO attestations (infringement_id, provider_id, score_kind,
                              provider_score, provider_category, query_quality,
                              score_version, last_run_id)
    VALUES (%(infringement_id)s, %(provider_id)s, %(score_kind)s,
            %(provider_score)s, %(provider_category)s, %(query_quality)s,
            %(score_version)s, %(run_id)s)
    ON CONFLICT (infringement_id, provider_id) DO UPDATE
      SET last_confirmed_at = now(),
          confirm_count = attestations.confirm_count + 1,
          provider_score = EXCLUDED.provider_score,
          provider_category = EXCLUDED.provider_category,
          query_quality = EXCLUDED.query_quality,
          score_version = EXCLUDED.score_version,
          last_run_id = EXCLUDED.last_run_id
"""

_COMPLETE_RUN_SQL = """
    UPDATE search_runs
    SET status = 'completed',
        providers_succeeded = %(providers_succeeded)s,
        matches_found = (SELECT count(*) FROM attestations
                         WHERE last_run_id = %(run_id)s),
        completed_at = now()
    WHERE run_id = %(run_id)s
"""
```

The fan-out helper (module-level, pure — the within-run collapse happens here, before any SQL):

```python
class _InfringementKey(NamedTuple):
    url_hash: UrlHash
    key_url: str
    keyed_on: str  # 'page_url' | 'image_url'
    match: ProviderMatch


def _fan_out(matches: Sequence[ProviderMatch]) -> list[_InfringementKey]:
    """One key per page the image was found on; image_url fallback when the
    provider reported no page. Collapsed on url_hash within the batch —
    first occurrence wins (provider ordering is relevance order, and
    raw_response keeps everything anyway)."""
    seen: dict[UrlHash, _InfringementKey] = {}
    for match in matches:
        targets = (
            [(page, "page_url") for page in match.page_urls]
            if match.page_urls
            else [(match.image_url, "image_url")]
        )
        for key_url, keyed_on in targets:
            digest = url_hash(key_url)
            if digest not in seen:
                seen[digest] = _InfringementKey(digest, key_url, keyed_on, match)
    return list(seen.values())
```

The store method (replaces `record_matches`; same position in the Protocol):

```python
    async def record_infringements(
        self,
        run_id: UUID,
        user_ref: UserRef,
        provider: ProviderDescriptor,
        matches: Sequence[ProviderMatch],
    ) -> int:
        keys = _fan_out(matches)
        async with self._pool.connection() as conn, conn.transaction():
            for key in keys:
                await conn.execute(
                    _UPSERT_URL_SQL,
                    {
                        "url_hash": key.url_hash,
                        "url": key.key_url,
                        "source_domain": source_domain(key.key_url),
                        "canonical_url": canonicalise(key.key_url),
                        "normalisation_version": NORMALISATION_VERSION,
                    },
                )
                cur = await conn.execute(
                    _UPSERT_INFRINGEMENT_SQL,
                    {
                        "user_ref": user_ref,
                        "url_hash": key.url_hash,
                        "page_url": key.key_url,
                        "image_url": key.match.image_url,
                        "keyed_on": key.keyed_on,
                    },
                )
                row = await cur.fetchone()
                assert row is not None
                await conn.execute(
                    _UPSERT_ATTESTATION_SQL,
                    {
                        "infringement_id": row[0],
                        "provider_id": provider.provider_id,
                        "score_kind": provider.score_kind,
                        "provider_score": key.match.provider_score,
                        "provider_category": key.match.provider_category,
                        "query_quality": key.match.query_quality,
                        "score_version": provider.score_version,
                        "run_id": run_id,
                    },
                )
        return len(keys)
```

Imports to update: `from imageshield.search.urlhash import NORMALISATION_VERSION, canonicalise, source_domain, url_hash`; `from typing import NamedTuple`. **Semantics note for the docstring:** `seen_count` counts provider-observations (two providers in one run bump it twice — it is "how often has anything seen this", not "how many runs"); `confirm_count` is per-provider and is the clean per-provider signal.

In `runner.py` change `store.record_matches(...)` → `store.record_infringements(...)` (same arguments). In `tests/test_search_runner.py` and `tests/test_search_worker.py`, rename the fake stores' `record_matches` to `record_infringements`.

- [ ] **Step 4: Run all gates**

Run: `pytest tests/ -v && ruff check . && mypy`
Expected: PASS. (`test_list_matches_filters_by_user_and_since` still passes: `list_matches` still reads `search_matches`, which now receives no rows from `record_infringements` — the test seeds via `record_infringements` so it will return empty. **Adjust that test now**: mark its read-side assertions as replaced in Task 5 by simply deleting the test in this task; Task 5 adds `test_list_infringements_*` covering the same ground.)

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "Step 6: record_infringements — rescan is an UPDATE, never an INSERT

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

### Task 5: Read path — `list_infringements` + HTTP surface

**Files:**
- Modify: `src/imageshield/search/models.py` (delete `MatchRow`; add `AttestationRow`, `InfringementRow`)
- Modify: `src/imageshield/search/store.py` (delete `list_matches` + `_LIST_MATCHES_SQL` + `_to_match_row`; add `list_infringements`)
- Modify: `src/imageshield/http/models.py` (delete `SearchMatchItem`/`SearchMatchesResponse`; add `AttestationItem`/`InfringementItem`/`InfringementsResponse`)
- Modify: `src/imageshield/http/routes/search.py` (replace `GET /v1/search/matches` with `GET /v1/search/infringements`)
- Modify: `tests/test_search_routes.py` (fake store + endpoint tests)
- Modify: `devtools/run_search_e2e.py` (line ~89: `list_matches` → `list_infringements`; print infringement/attestation counts)

**Interfaces:**
- Produces:

```python
class AttestationRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider_id: str
    score_kind: str
    provider_score: Decimal | None
    provider_category: str | None
    query_quality: str | None
    score_version: str
    first_confirmed_at: datetime
    last_confirmed_at: datetime
    confirm_count: int


class InfringementRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    infringement_id: UUID
    page_url: str
    image_url: str | None
    keyed_on: str
    first_seen_at: datetime
    last_seen_at: datetime
    seen_count: int
    band: str
    status: str
    attestations: tuple[AttestationRow, ...]
```

- `SearchStore.list_infringements(user_ref: UserRef, since: datetime | None) -> tuple[InfringementRow, ...]` — `since` filters on `last_seen_at`, newest first.

- [ ] **Step 1: Write the failing tests.** In `tests/test_search_routes.py`: rename the fake store's `list_matches` to `list_infringements`, store `InfringementRow`s, change the auth-check URL (line ~121) and the listing test (~250-280) to `/v1/search/infringements`, asserting the nested shape:

```python
def _infringement(page: str, providers: list[str]) -> InfringementRow:
    now = datetime.now(UTC)
    return InfringementRow(
        infringement_id=uuid4(),
        page_url=page,
        image_url="https://cdn/img.jpg",
        keyed_on="page_url",
        first_seen_at=now,
        last_seen_at=now,
        seen_count=len(providers),
        band="review",
        status="new",
        attestations=tuple(
            AttestationRow(
                provider_id=p,
                score_kind="numeric" if p == "hive" else "categorical",
                provider_score=Decimal("0.87") if p == "hive" else None,
                provider_category=None if p == "hive" else "full_match",
                query_quality=None,
                score_version=f"{p}-v1",
                first_confirmed_at=now,
                last_confirmed_at=now,
                confirm_count=1,
            )
            for p in providers
        ),
    )


def test_list_infringements_returns_nested_attestations(...) -> None:
    ...  # seed fake store with _infringement("https://a/page", ["hive", "google"])
    response = client.get(f"/v1/search/infringements?user_ref={user_ref}", headers=AUTH)
    assert response.status_code == 200
    body = response.json()["infringements"]
    assert len(body) == 1
    assert body[0]["provider_count"] == 2
    assert {a["provider_id"] for a in body[0]["attestations"]} == {"hive", "google"}
    assert body[0]["band"] == "review"
```

In `tests/test_search_store.py` add the DB-backed read test:

```python
async def test_list_infringements_filters_by_user_and_since(
    store: PostgresSearchStore,
) -> None:
    user_ref, other = _user(), _user()
    _, run_id = await _seeded_run(store, user_ref)
    _, other_run = await _seeded_run(store, other)
    await store.record_infringements(
        run_id, user_ref, HIVE_DESC,
        [_hive_match("https://x/a.jpg", pages=["https://site/a"])],
    )
    await store.record_infringements(
        run_id, user_ref, GOOGLE_DESC, [_google_match("https://site/a", "page_match")]
    )
    await store.record_infringements(
        other_run, other, HIVE_DESC,
        [_hive_match("https://x/b.jpg", pages=["https://site/b"])],
    )

    mine = await store.list_infringements(user_ref, None)
    assert len(mine) == 1
    assert mine[0].page_url == "https://site/a"
    assert {a.provider_id for a in mine[0].attestations} == {"hive", "google"}
    assert mine[0].attestations[0].confirm_count == 1

    future = datetime.now(UTC) + timedelta(hours=1)
    assert await store.list_infringements(user_ref, future) == ()
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_search_store.py tests/test_search_routes.py -v`
Expected: FAIL — no `list_infringements`, no `InfringementRow`.

- [ ] **Step 3: Implement.** `store.py` — one joined query, grouped in Python:

```python
_LIST_INFRINGEMENTS_SQL = """
    SELECT i.infringement_id, i.page_url, i.image_url, i.keyed_on,
           i.first_seen_at, i.last_seen_at, i.seen_count, i.band, i.status,
           a.provider_id, a.score_kind, a.provider_score, a.provider_category,
           a.query_quality, a.score_version, a.first_confirmed_at,
           a.last_confirmed_at, a.confirm_count
    FROM infringements i
    JOIN attestations a ON a.infringement_id = i.infringement_id
    WHERE i.user_ref = %(user_ref)s
      AND (%(since)s::timestamptz IS NULL OR i.last_seen_at >= %(since)s)
    ORDER BY i.last_seen_at DESC, i.infringement_id, a.provider_id
"""


    async def list_infringements(
        self, user_ref: UserRef, since: datetime | None
    ) -> tuple[InfringementRow, ...]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                _LIST_INFRINGEMENTS_SQL, {"user_ref": user_ref, "since": since}
            )
            rows = await cur.fetchall()

        grouped: dict[UUID, tuple[tuple[Any, ...], list[AttestationRow]]] = {}
        order: list[UUID] = []
        for row in rows:
            if row[0] not in grouped:
                grouped[row[0]] = (row, [])
                order.append(row[0])
            grouped[row[0]][1].append(
                AttestationRow(
                    provider_id=row[9], score_kind=row[10], provider_score=row[11],
                    provider_category=row[12], query_quality=row[13],
                    score_version=row[14], first_confirmed_at=row[15],
                    last_confirmed_at=row[16], confirm_count=row[17],
                )
            )
        return tuple(
            InfringementRow(
                infringement_id=head[0], page_url=head[1], image_url=head[2],
                keyed_on=head[3], first_seen_at=head[4], last_seen_at=head[5],
                seen_count=head[6], band=head[7], status=head[8],
                attestations=tuple(atts),
            )
            for head, atts in (grouped[key] for key in order)
        )
```

`http/models.py`:

```python
class AttestationItem(BaseModel):
    provider_id: str
    score_kind: Literal["numeric", "categorical"]
    # Presentation-layer float; the DB keeps the exact NUMERIC and
    # raw_response keeps the verbatim provider value.
    provider_score: float | None
    provider_category: str | None
    query_quality: str | None
    score_version: str
    first_confirmed_at: datetime
    last_confirmed_at: datetime
    confirm_count: int


class InfringementItem(BaseModel):
    infringement_id: UUID
    page_url: str
    image_url: str | None
    keyed_on: Literal["page_url", "image_url"]
    first_seen_at: datetime
    last_seen_at: datetime
    seen_count: int
    band: str
    status: str
    provider_count: int  # agreement signal: independent providers, not hits
    attestations: list[AttestationItem]


class InfringementsResponse(BaseModel):
    infringements: list[InfringementItem]
```

`routes/search.py` — replace `list_search_matches`:

```python
@router.get("/search/infringements")
async def list_infringements(
    user_ref: UUID,
    since: datetime | None = Query(default=None),
    store: SearchStore = Depends(get_search_store),
) -> InfringementsResponse:
    rows = await store.list_infringements(UserRef(user_ref), since)
    return InfringementsResponse(
        infringements=[
            InfringementItem(
                infringement_id=row.infringement_id,
                page_url=row.page_url,
                image_url=row.image_url,
                keyed_on=row.keyed_on,
                first_seen_at=row.first_seen_at,
                last_seen_at=row.last_seen_at,
                seen_count=row.seen_count,
                band=row.band,
                status=row.status,
                provider_count=len(row.attestations),
                attestations=[
                    AttestationItem(
                        provider_id=a.provider_id,
                        score_kind=a.score_kind,
                        provider_score=(
                            float(a.provider_score)
                            if a.provider_score is not None else None
                        ),
                        provider_category=a.provider_category,
                        query_quality=a.query_quality,
                        score_version=a.score_version,
                        first_confirmed_at=a.first_confirmed_at,
                        last_confirmed_at=a.last_confirmed_at,
                        confirm_count=a.confirm_count,
                    )
                    for a in row.attestations
                ],
            )
            for row in rows
        ]
    )
```

(mypy: `keyed_on`/`score_kind` come out of the row models as `str` — validate through pydantic by passing them straight in; pydantic coerces `Literal` at runtime. If mypy strict objects, type the two row-model fields as the same `Literal`s instead of `str`.)

`devtools/run_search_e2e.py` line ~89: `matches = await store.list_infringements(user_ref, None)` and adjust the printout to `page_url` + attestation count per row.

- [ ] **Step 4: Run all gates**

Run: `pytest tests/ -v && ruff check . && mypy`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "Step 6: GET /v1/search/infringements replaces the matches read surface

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

### Task 6: Migration 0006 — contract (drop `search_matches`)

**Files:**
- Create: `migrations/0006_drop_search_matches.up.sql`
- Create: `migrations/0006_drop_search_matches.down.sql`
- Modify: `tests/test_migrations.py` (`CORE_TABLES`: remove `search_matches`, add `infringements`, `attestations`; 0005 test's `assert "search_matches" in tables` needs updating — that assertion was about 0005 alone, change it to assert the table is gone after full `up`)

**Interfaces:** none — pure schema contraction. Grep first: `grep -rn "search_matches" src/ tests/ devtools/` must return nothing except migrations and this plan before writing the DROP.

- [ ] **Step 1: Write the failing test** — in `tests/test_migrations.py` update `CORE_TABLES`:

```python
CORE_TABLES = {
    "liveness_sessions",
    "enrolments",
    "providers",
    "search_seeds",
    "search_runs",
    "content_urls",
    "provider_calls",
    "infringements",
    "attestations",
    "outbox",
    "audit_log",
}
```

and add:

```python
def test_0006_drops_search_matches(throwaway_db: str) -> None:
    run_migrate(throwaway_db, "down", "--all")
    result = run_migrate(throwaway_db, "up")
    assert result.returncode == 0, result.stderr
    with psycopg.connect(throwaway_db) as conn:
        assert "search_matches" not in _table_names(conn)
```

In the Task-2 test `test_0005_creates_infringements_and_attestations`, delete the line `assert "search_matches" in tables` (it holds only between 0005 and 0006).

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_migrations.py -v`
Expected: FAIL — `search_matches` still present after `up`.

- [ ] **Step 3: Write the migrations.** `0006_drop_search_matches.up.sql`:

```sql
-- Step 6 (contract half): search_matches is superseded by
-- infringements + attestations (0005 migrated its rows). Nothing in the
-- codebase references it as of this migration.
DROP TABLE search_matches;
```

`0006_drop_search_matches.down.sql` — recreate the 0001+0004 shape, empty (data is not restored; its content lives on in infringements/attestations):

```sql
CREATE TABLE search_matches (
  match_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id          UUID NOT NULL REFERENCES search_runs(run_id),
  url_hash        TEXT NOT NULL REFERENCES content_urls(url_hash),
  user_ref        UUID NOT NULL,
  provider_id     TEXT NOT NULL REFERENCES providers(provider_id),
  image_url       TEXT NOT NULL,
  page_url        TEXT,
  provider_score  NUMERIC(6,4),
  score_version   TEXT NOT NULL,
  band            TEXT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  score_kind      TEXT NOT NULL,
  provider_category TEXT,
  query_quality   TEXT,
  CONSTRAINT search_matches_score_shape CHECK (
    (score_kind = 'numeric'     AND provider_score    IS NOT NULL) OR
    (score_kind = 'categorical' AND provider_category IS NOT NULL)
  )
);

CREATE UNIQUE INDEX search_matches_uniq
  ON search_matches (run_id, url_hash, provider_id);
CREATE INDEX search_matches_user_idx ON search_matches (user_ref, created_at DESC);
CREATE INDEX search_matches_review_idx ON search_matches (band, created_at)
  WHERE band = 'review';
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_migrations.py tests/ -v`
Expected: PASS, including the full down-up-down cycle tests.

- [ ] **Step 5: Commit**

```bash
git add migrations/0006_drop_search_matches.up.sql migrations/0006_drop_search_matches.down.sql tests/test_migrations.py
git commit -m "Step 6: migration 0006 — drop superseded search_matches (contract half)

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

### Task 7: The 52-rescan regression test (permanent)

**Files:**
- Modify: `tests/test_search_store.py` (one test, marked as permanent)

**Interfaces:** consumes only Task 4's `record_infringements` and Task 5's `list_infringements`.

- [ ] **Step 1: Write the test** (it should PASS immediately — it is a regression net, not TDD of new code; if it fails, Task 4 has a bug):

```python
async def test_52_weekly_rescans_over_static_corpus_add_zero_rows(
    store: PostgresSearchStore, migrated_db: str
) -> None:
    """THE regression test for the defect step 6 exists to fix — the old
    system's matches[].seenInScans grew one entry per scan forever
    (weeklyInfringementScanner.js:1016). Row count must grow with CONTENT,
    never with TIME: 52 rescans of a static corpus add zero rows; only
    seen_count, confirm_count, last_seen_at, last_confirmed_at move.
    DO NOT DELETE OR WEAKEN THIS TEST (step-6 spec, 'Done when')."""
    user_ref = _user()
    seed_id = await store.create_seed(user_ref, "user_supplied", "https://s3/img.jpg")
    corpus = [
        _hive_match(f"https://cdn.example/{i}.jpg", pages=[f"https://site{i}.example/p"])
        for i in range(3)
    ]
    google_corpus = [
        _google_match(f"https://site{i}.example/p", "page_match") for i in range(3)
    ]

    def _counts() -> tuple[int, int, int]:
        return (
            _query(migrated_db, "SELECT count(*) FROM infringements")[0][0],
            _query(migrated_db, "SELECT count(*) FROM attestations")[0][0],
            _query(migrated_db, "SELECT count(*) FROM content_urls")[0][0],
        )

    first_week_counts: tuple[int, int, int] | None = None
    for week in range(52):
        run_id = await store.create_run(user_ref, seed_id, (HIVE, GOOGLE))
        await store.record_infringements(run_id, user_ref, HIVE_DESC, corpus)
        await store.record_infringements(run_id, user_ref, GOOGLE_DESC, google_corpus)
        await store.complete_run(run_id, (HIVE, GOOGLE))
        if week == 0:
            first_week_counts = _counts()

    assert first_week_counts == (3, 6, 3)  # 3 pages x (1 infringement + 2 attestations)
    assert _counts() == first_week_counts  # 51 more rescans added ZERO rows

    rows = _query(
        migrated_db,
        "SELECT seen_count FROM infringements WHERE user_ref = %s", (user_ref,),
    )
    assert all(r[0] == 104 for r in rows)  # 2 providers x 52 observations
    confirms = _query(migrated_db, "SELECT confirm_count FROM attestations")
    assert all(r[0] == 52 for r in confirms)
```

- [ ] **Step 2: Run it**

Run: `pytest tests/test_search_store.py::test_52_weekly_rescans_over_static_corpus_add_zero_rows -v`
Expected: PASS. If it fails, fix `record_infringements`, not the test.

- [ ] **Step 3: Commit**

```bash
git add tests/test_search_store.py
git commit -m "Step 6: permanent regression test — rescans grow rows with content, not time

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

### Task 8: `raw_response` retention job

**Files:**
- Create: `src/imageshield/search/retention.py`
- Modify: `src/imageshield/config.py` (add `raw_response_retention_days: int = 90`, validate positive)
- Create: `tests/test_retention.py`
- Modify: `tests/test_config.py` (one validation case)

**Interfaces:**
- Produces: `async def null_expired_raw_responses(pool: AsyncConnectionPool, *, retention_days: int) -> int` and a `python -m imageshield.search.retention` one-shot CLI. Config knob `RAW_RESPONSE_RETENTION_DAYS` (default 90).

- [ ] **Step 1: Write the failing tests** — `tests/test_retention.py`:

```python
"""Retention: null provider_calls.raw_response past the window, keep the
metadata row. Recalibration over history needs recent payloads, not all of
them — and the JSONB is the unbounded part of an otherwise bounded table."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from imageshield.db.connection import make_async_pool
from imageshield.search.provider import ProviderResult
from imageshield.search.retention import null_expired_raw_responses
from imageshield.search.store import PostgresSearchStore
from imageshield.types import ProviderId, UserRef
from tests.db import run_migrate

HIVE = ProviderId("hive")


@pytest.fixture
def migrated_db(throwaway_db: str) -> str:
    run_migrate(throwaway_db, "down", "--all")
    up_result = run_migrate(throwaway_db, "up")
    assert up_result.returncode == 0, up_result.stderr
    return throwaway_db


def _query(db_url: str, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(db_url, autocommit=True) as conn:
        return conn.execute(sql, params).fetchall()


async def test_nulls_only_rows_past_the_window(migrated_db: str) -> None:
    pool = make_async_pool(migrated_db, min_size=1, max_size=2)
    await pool.open()
    try:
        store = PostgresSearchStore(pool)
        user_ref = UserRef(uuid4())
        seed_id = await store.create_seed(user_ref, "user_supplied", "https://s3/i.jpg")
        run_id = await store.create_run(user_ref, seed_id, (HIVE,))
        for _ in range(2):
            await store.record_provider_call(
                run_id,
                ProviderResult(
                    provider_id=HIVE, status="ok", matches=[],
                    raw_response={"big": "payload"}, http_status=200, latency_ms=5,
                ),
            )
        # age ONE of the two rows past the window
        _query(
            migrated_db,
            "UPDATE provider_calls SET created_at = now() - interval '91 days'"
            " WHERE call_id = (SELECT call_id FROM provider_calls LIMIT 1)"
            " RETURNING call_id",
        )

        nulled = await null_expired_raw_responses(pool, retention_days=90)
        assert nulled == 1

        rows = _query(
            migrated_db,
            "SELECT raw_response, status, latency_ms FROM provider_calls"
            " ORDER BY created_at",
        )
        assert rows[0][0] is None            # payload gone
        assert rows[0][1:] == ("ok", 5)      # metadata row intact
        assert rows[1][0] == {"big": "payload"}  # inside the window: untouched

        # idempotent: a second pass finds nothing to null
        assert await null_expired_raw_responses(pool, retention_days=90) == 0
    finally:
        await pool.close()
```

In `tests/test_config.py`, add a case following the file's existing pattern: `RAW_RESPONSE_RETENTION_DAYS=0` → `ConfigError` naming the key; default is 90 when unset.

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_retention.py tests/test_config.py -v`
Expected: FAIL — module doesn't exist / config field missing.

- [ ] **Step 3: Implement.** `config.py`: add field `raw_response_retention_days: int = 90` (place near the provider settings) and add it to the `_positive` validator's field list. `src/imageshield/search/retention.py`:

```python
"""Null provider_calls.raw_response past RAW_RESPONSE_RETENTION_DAYS.

One-shot CLI (``python -m imageshield.search.retention``) meant for a
scheduler (cron / ECS scheduled task). The metadata row survives — status,
latency, cost — only the JSONB payload is dropped. Recalibration over
history (CLAUDE.md §7.2) needs RECENT payloads, not all of them; 90 days is
the default window.
"""

from __future__ import annotations

import asyncio
import sys

import structlog
from psycopg_pool import AsyncConnectionPool

from imageshield.config import Config, ConfigError, load_config
from imageshield.db.connection import make_async_pool
from imageshield.http.logging import configure_logging

_NULL_EXPIRED_SQL = """
    UPDATE provider_calls
    SET raw_response = NULL
    WHERE raw_response IS NOT NULL
      AND created_at < now() - make_interval(days => %(days)s)
"""


async def null_expired_raw_responses(
    pool: AsyncConnectionPool, *, retention_days: int
) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute(_NULL_EXPIRED_SQL, {"days": retention_days})
        return cur.rowcount


async def run_once(config: Config) -> None:
    log = structlog.get_logger("imageshield.search.retention")
    pool = make_async_pool(
        config.database_url,
        min_size=1,
        max_size=1,
    )
    await pool.open()
    try:
        nulled = await null_expired_raw_responses(
            pool, retention_days=config.raw_response_retention_days
        )
    finally:
        await pool.close()
    log.info(
        "retention.raw_responses_nulled",
        nulled=nulled,
        retention_days=config.raw_response_retention_days,
    )


def main() -> int:
    configure_logging()
    try:
        config = load_config()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if sys.platform == "win32":
        # Same Proactor-loop constraint (and fix) as search.worker.main.
        import selectors

        with asyncio.Runner(
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())
        ) as runner:
            runner.run(run_once(config))
        return 0

    asyncio.run(run_once(config))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run all gates**

Run: `pytest tests/ -v && ruff check . && mypy`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/imageshield/search/retention.py src/imageshield/config.py tests/test_retention.py tests/test_config.py
git commit -m "Step 6: raw_response retention job (RAW_RESPONSE_RETENTION_DAYS, default 90)

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

### Task 9: Docs + final verification

**Files:**
- Modify: `SCHEMA.md` (add `infringements`, `attestations`, the two `content_urls` columns, note `search_matches` dropped, note `provider_calls.raw_response` nullable + retention)

**Steps:**

- [ ] **Step 1:** Update `SCHEMA.md`: paste the 0005 DDL for the two new tables (including `keyed_on` and the score-shape CHECK), document the rescan semantics table (found again → UPDATE; not found → untouched; new → INSERT both), the `normalisation_version` contract, and the retention window. Follow the document's existing formatting.
- [ ] **Step 2:** Grep gates by hand:
  - `grep -rn "search_matches" src/ tests/ devtools/` → only historical mentions in comments should remain; remove any that describe current behaviour.
  - `grep -rn "interim_url_hash" src/ tests/ devtools/ docs/` → none outside old plan docs.
- [ ] **Step 3:** Full gate run: `ruff check . && mypy && REQUIRE_DB=1 pytest tests/ -v` (Postgres up). All green.
- [ ] **Step 4:** Walk the spec's "Done when" list and check every line against a passing test (see Coverage Map below).
- [ ] **Step 5: Commit**

```bash
git add SCHEMA.md
git commit -m "Step 6: document infringements/attestations schema and retention

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

- [ ] **Step 6:** STOP. Do not begin step 7 (calibration/banding). Report the step as complete with the branch name; merging follows the repo's existing flow (previous steps merged to `main` after review).

---

## Coverage Map (spec "Done when" → test)

| Done-when | Test |
|---|---|
| 5 tracking variants → 1 hash | `test_five_tracking_variants_produce_one_hash` |
| spec example pair, http/https preserved | `test_spec_example_canonicalises_but_scheme_is_preserved` |
| Hive+Google page X user Y → 1 infringement, 2 attestations | `test_cross_provider_is_one_infringement_two_attestations` |
| same URL, 2 users → 2 infringements | `test_cross_user_is_never_dedup` |
| 1 match, 3 backlinks → 3 infringements | `test_three_backlinks_are_three_infringements` |
| 52 rescans → constant rows, only counters move | `test_52_weekly_rescans_over_static_corpus_add_zero_rows` (permanent) |
| canonical_url + normalisation_version on every content_urls row | `test_content_urls_carry_canonical_and_version` + 0005 backfill test |
| search_matches gone, nothing references it | `test_0006_drops_search_matches` + Task 9 grep |
| retention nulls payload, keeps metadata | `test_nulls_only_rows_past_the_window` |
| within-run collapse (same page, two images) | `test_same_page_twice_in_one_run_collapses_before_writing` |
| image_url fallback recorded | `test_image_url_fallback_when_no_backlink` |
