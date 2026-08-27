# Articles + provider ops in the control room — design

**Date:** 2026-08-27
**Status:** approved in discussion (owner, 2026-08-27); building on `feat/articles` in both repos
**Supersedes:** `2026-08-20-recommendation-campaigns-design.md` — campaigns and the LLM ideas
panel are not being built. Articles replace them: operator-authored content, no targeting, no
score effect, no LLM.
**Spans:** this repo (table, view, admin API, console) and the proxy (`image_backend`: reader,
`GET /v1/articles`). Two coordinated changes, one per repo, each under its own manual.

## 1. What this adds

1. **Articles.** Operators write articles in the control room — title, summary, body, pictures,
   source links — and publish them. Every logged-in app user sees the published feed, newest
   first. Nothing is targeted, nothing moves a score, nothing notifies anyone: an article
   appears on the next app open. Pictures are URLs the operator pastes (the source article's
   hero image); there is no upload, because this repo holds no S3 credentials by design
   (CLAUDE.md §2) and the console talks only to this repo's admin API.
2. **Provider ops on the dashboard.** The kill switch, re-enable and breaker reset already exist
   as admin routes (`admin_providers.py`) and are curl-only. They get forms on the dashboard,
   the alarms the health payload already carries get rendered, and the audit row names the
   operator instead of `admin_service_token`.

## 2. Decisions taken (owner, 2026-08-27)

| Question | Decision |
|---|---|
| Pictures | Pasted `https://` URLs. Upload can follow later via the proxy, which owns S3. |
| "Push to the app" | Publish to an in-app feed. No notification — digests-only stands (INVARIANTS #24). |
| Audience | Every user. No targeting menu, no per-user state. |
| Read path to the proxy | A ninth `svc` view, `svc.v_articles`, read through `src/services/contract/` like every other cross-boundary read. Not an HTTP relay. |
| Sequencing | Build the APIs first, then run the suites and write the tests, then close gaps. Owner's explicit ordering for this cycle. |
| Branching | `feat/articles` off `main` in both repos. The proxy half ships on BOTH lines (owner, 2026-08-27): merge to `main`, then forward-merge `main` → `release/sep-1` — never cherry-pick. Services has one line. |

## 3. Hard rules

- **Additive only.** New table, new view, new role, new routers, new console pages, one new
  optional contract view on the proxy. No existing DDL, route, reader or readiness rule
  changes. The feature is dark until an operator publishes and the proxy route ships.
- **Articles are not identity data.** No `user_ref` anywhere in the module. The view carries no
  per-person column and needs no scoping.
- **The console stays pixel-free.** Pasted image URLs render as links in the console, never as
  `<img>`. The "no imagery in the control room" property (spec 2026-08-21 §0.2) stays a blanket
  rule with no carve-outs, so a future test can assert *no `<img>` anywhere* rather than
  *no hit imagery*.
- **URLs are `https://` only**, validated at the boundary. An `http://` picture or source is a
  422, not something the app opens.
- **Every write names an operator** and lands one `audit_log` row in the same transaction.

## 4. Schema — migration `0026_articles`

```sql
CREATE TABLE articles (
  article_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  title        TEXT NOT NULL CHECK (title <> ''),
  summary      TEXT NOT NULL DEFAULT '',          -- card blurb
  body         TEXT NOT NULL DEFAULT '',          -- full text, markdown
  images       JSONB NOT NULL DEFAULT '[]',       -- [{"url","alt"}], hero first
  sources      JSONB NOT NULL DEFAULT '[]',       -- [{"name","url"}]
  status       TEXT NOT NULL DEFAULT 'draft'
               CHECK (status IN ('draft', 'published', 'archived')),
  published_at TIMESTAMPTZ,
  created_by   TEXT NOT NULL CHECK (created_by <> ''),
  updated_by   TEXT NOT NULL CHECK (updated_by <> ''),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (jsonb_typeof(images) = 'array'),
  CHECK (jsonb_typeof(sources) = 'array'),
  -- A draft has never been published; a published row always says when.
  -- Archived carries whichever it had: a draft archived unpublished has
  -- none, a published article archived keeps its date.
  CHECK (status <> 'draft' OR published_at IS NULL),
  CHECK (status <> 'published' OR published_at IS NOT NULL)
);
CREATE INDEX articles_published_idx ON articles (published_at DESC, article_id DESC)
  WHERE status = 'published';

CREATE VIEW svc.v_articles AS
  SELECT article_id, title, summary, body, images, sources, published_at, updated_at
  FROM articles WHERE status = 'published';
GRANT SELECT ON svc.v_articles TO imageshield_proxy_ro;

-- content_rw: guarded CREATE ROLE, USAGE on public, SELECT/INSERT/UPDATE on
-- articles, granted to app_services when that role exists (0022's pattern).
```

`images` and `sources` are JSONB arrays so pictures and links are plural without a child table;
pydantic validates their shape at the boundary, the CHECKs only guarantee they are arrays.
Schema lint passes: `jsonb`, no forbidden name suffix. No `DELETE` anywhere — archive is the
soft delete. No pinning in v1: newest first is the whole ordering.

`svc.v_articles` is the ninth contract view. Same versioned-contract rule as the other eight:
columns may be added, none removed or retyped without a coordinated deploy.

## 5. Services module — `src/imageshield/articles/`

`store.py`: `ArticleStore` protocol and `PostgresArticleStore`, the one writer of `articles`.

| Method | Transaction |
|---|---|
| `create(title, summary, body, images, sources, operator) -> UUID` | insert draft + audit `article.created` |
| `update(id, …, operator) -> bool` | update content columns (any status; a published article changes live) + audit `article.updated` |
| `publish(id, operator) -> str \| None` | draft or archived → published, `published_at = COALESCE(published_at, now())` so a re-publish keeps its original date; published→no-op. Returns the resulting status, `None` if not found. Audit `article.published` when a change happened. |
| `archive(id, operator, reason) -> bool` | draft or published → archived + audit `article.archived` with the reason |
| `get(id)`, `list(limit)` | reads; `list` orders `updated_at DESC` |

Audit rows use `actor_type = 'operator'`, `resource_id = article_id`, metadata `{operator, …}` —
the same statement shape as `threats/store.py`.

## 6. Admin API — `/v1/admin/articles`

Router-level `require_service_token` + `require_admin_service_token`, like every admin router.

| Route | Body | Response |
|---|---|---|
| `GET ""` | — (`?limit=`, default 50, max 200) | `{articles: [ArticleItem]}` all statuses |
| `POST ""` | `ArticleUpsertRequest` | `201 {article_id, status: "draft"}` |
| `GET /{id}` | — | `ArticleItem` |
| `PUT /{id}` | `ArticleUpsertRequest` | `ArticleItem` |
| `POST /{id}/publish` | `{operator}` | `{article_id, status}` |
| `POST /{id}/archive` | `{operator, reason}` (reason ≥ 3 chars) | `{article_id, status: "archived"}` |

`ArticleUpsertRequest(ServiceModel)`: `title` 1–200, `summary` ≤ 500, `body` ≤ 20 000,
`images: list[ArticleImage]` ≤ 10, `sources: list[ArticleSource]` ≤ 10, `operator` ≥ 1.
`ArticleImage {url, alt=''}`, `ArticleSource {name 1–120, url}`; both `url`s are `https://`
only, ≤ 2 000 chars. Unknown id → `404 article_not_found`. Standard error envelope.

## 7. Console

Nav gains **Articles**. Routes, all operator-authenticated, writes CSRF-verified:

| Route | Page |
|---|---|
| `GET /articles` | list with status badges, "New article" link |
| `GET /articles/new` | blank form |
| `POST /articles` | create → `303 /articles/{id}` |
| `GET /articles/{id}` | edit form + Publish / Archive (with reason) buttons; image and source URLs shown as links, never `<img>` |
| `POST /articles/{id}` | update → `303 /articles/{id}` |
| `POST /articles/{id}/publish`, `POST /articles/{id}/archive` | → `303 /articles/{id}` |

Form encoding for the plural fields: one entry per line in a textarea — `url | alt` for images,
`name | url` for sources. The console parses lines into the JSON arrays; validation is the
API's, surfaced as the existing 502 envelope page when it fails.

`ServicesClient` gains `list_articles`, `get_article`, `create_article`, `update_article`,
`publish_article`, `archive_article`.

### Provider ops (part 2 of §1)

- `ProviderReasonRequest` gains `operator: str | None` (`ServiceModel` is `extra='forbid'`, so
  it must be declared). The three routes pass `actor=body.operator or _ACTOR`; omitted, they
  record `admin_service_token` exactly as today, so curl callers are unchanged.
- `ServicesClient` gains `disable_provider`, `enable_provider`, `reset_breaker`.
- Console `POST /providers/{id}/disable`, `/enable`, `/breaker-reset` — form `reason` (≥ 3),
  CSRF, `303 /`.
- `dashboard.html`: an alarms panel first on the page (every `alarms[]` entry across providers,
  `no_successful_calls_24h` included); the table adds breaker reason, daily/monthly budget,
  month-to-date, headroom, window calls, 24h successes, p50/p99, an `as of … · Nh window`
  caption; nullable numbers render `—`, `success_rate: null` especially; an actions column
  with Disable-or-Enable and Reset breaker (only when the breaker is not closed).

## 8. Proxy (`image_backend`)

- `src/services/contract/views.ts`: `articles: 'svc.v_articles'` in `CONTRACT_VIEWS`; columns
  `article_id, title, summary, body, images, sources, published_at, updated_at`; **not** in
  `READINESS_REQUIRED` — an api deployed against a database without 0026 boots, and the feed
  degrades to empty with a warn log (the `v_person_liveness_attempts` pattern). Present but
  wrong-shaped still fails readiness, as for every other view.
- `readers.ts`: `readPublishedArticles(db, {limit, after?})` → rows or `'unavailable'`;
  `readPublishedArticle(db, id)` → row, `null` or `'unavailable'`. Explicit column lists (P3).
- `src/articles/`: `GET /v1/articles?limit&cursor` → `{items, next_cursor}`, keyset cursor over
  `(published_at, article_id)` encoded opaquely; `GET /v1/articles/{id}` → `404 ARTICLE_NOT_FOUND`.
  Strict zod on query and params; session auth like all of `/v1/*`; an empty feed and an
  unavailable view both return `200 {items: []}` (P24). Registered in `http/app.ts`.
- `fixtures/svc/10-svc-stubs.sql`: `svc._stub_articles` + `svc.v_articles` + the grant, so the
  proxy's tests run standalone.

## 9. Error handling

Services: the standard `{error:{code,message,retryable,request_id}}` envelope; codes
`article_not_found` (404) and pydantic's 422 with `loc`/`msg`. Console: upstream failures render
the existing 502 envelope page; CSRF failures the existing 403. Proxy: `ARTICLE_NOT_FOUND` joins
the closed error union; a missing view is a warn log, never a 5xx.

## 10. Testing (written after the APIs exist — owner's ordering)

Services:
- `tests/test_admin_articles_routes.py` over a fake store: both tokens required; create 201;
  publish/archive state transitions and the no-op; 404 shape; `http://` image → 422;
  operator reaches the store.
- `tests/test_articles_store.py` against real Postgres: full lifecycle; one audit row per write
  naming the operator; `svc.v_articles` shows published rows only and drops an archived one;
  `imageshield_proxy_ro` can read the view and not the table.
- `tests/test_migrations.py`: `articles` in the table set; up/down round-trips.
- `tests/test_admin_providers.py`: body `operator` becomes the audit actor; omitted → token.
- `tests/test_console.py`: articles list/new/create/update/publish/archive post through with the
  logged-in operator and redirect; CSRF-less POST is 403; the edit page contains no `<img>`;
  provider disable/enable/reset post through; alarms render; `success_rate: null` renders `—`.

Proxy:
- unit: cursor encode/decode round-trip and rejection of a tampered cursor.
- integration: newest-first ordering; cursor walks the whole feed without gaps or repeats;
  unknown query param → 400; unknown id → 404 `ARTICLE_NOT_FOUND`; the view absent → `200 []`
  plus a warn log; the IDOR walk still passes for the two new routes (nothing person-keyed).

## 11. Docs updated in the same PRs

This repo: `SCHEMA.md` (table + view), `PROXY_INTEGRATION.md` §6 (ninth view),
`ADMIN_PANEL_INTEGRATION.md` (articles section; `operator` on provider writes; rule 5's stale
crop guidance corrected to the 2026-08-21 decision), `CLAUDE.md` §3 view list and §6 scope
table, `docs/OPERATIONS.md` (console as the primary kill-switch path, curl as break-glass), and a
superseded banner on the 2026-08-20 campaigns spec.
Proxy: `docs/CLAUDE.md` §6 view table (ninth row, optional), `SERVICES-CONTRACT.md`.

## 12. Out of scope

Picture upload; targeting; read/dismiss state; pinning; notifications or digest mentions;
any LLM drafting; article search; localisation; scheduling a future publish.
