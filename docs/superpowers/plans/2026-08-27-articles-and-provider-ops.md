# Articles + Provider Ops Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Operators author and publish articles from the control room, every app user reads them via `GET /v1/articles`; and the dashboard gains the provider kill switch, breaker reset and alarms.

**Architecture:** Services (`image_flashbacklabs`) owns the `articles` table, a `content_rw` role, `/v1/admin/articles` and the console pages, and projects published rows through a ninth contract view `svc.v_articles`. The proxy (`image_backend`) reads that view through `src/services/contract/` (optional for readiness) and serves a keyset-paged feed. Everything is additive: new table/view/role, new routers, new pages, one new optional view on the proxy.

**Tech Stack:** Python 3.11+ / FastAPI / psycopg 3 / Jinja2 / pytest (services); Node 22 / TypeScript strict / Fastify 5 / zod / pg / vitest + testcontainers (proxy).

**Spec:** `docs/superpowers/specs/2026-08-27-articles-design.md` (this repo). Read it first.

**Owner's ordering for this cycle (overrides the default TDD order):** build the APIs and pages first (Phases 1–2), then write and run the tests (Phase 3), then fix gaps and finish docs (Phase 4). Every build task still ends with lint + typecheck + the existing suite green before its commit.

## Global Constraints

- Branch `feat/articles` in BOTH repos, off `main`. Never commit at the workspace level; never mix the two repos' changes in one commit. Services HEAD is already on `feat/articles` (spec committed as `03136f2`).
- Additive only: no existing DDL, route, reader or readiness rule changes; new things only.
- No `user_ref` anywhere in `src/imageshield/articles/` or `src/imageshield/http/routes/admin_articles.py`.
- The console renders NO `<img>` tag on any page. Pasted picture URLs are links.
- Every URL in an article is `https://` only, ≤ 2 000 chars. Title 1–200, summary ≤ 500, body ≤ 20 000, ≤ 10 images, ≤ 10 sources, source name 1–120, alt ≤ 300, operator 1–64, archive reason 3–500.
- Every services write names an operator and lands exactly one `audit_log` row in the same transaction.
- Services tooling: `ruff check .` / `ruff format <new files only>` — NEVER format a modified existing file and NEVER `ruff format .`: the repo is not format-clean and CI checks `ruff check .` only, so formatting an existing file drags unrelated reformat hunks into the commit (Task 3's one-line fix became a 60-line diff) / `mypy` (strict) / `pytest` (DB tests need `docker compose -f docker-compose.local.yml up -d`; they skip when Postgres is unreachable — run with `REQUIRE_DB=1` when Docker is up so a skip cannot pass for a pass).
- Only ONE DB-backed pytest session may run at a time against the local Postgres: migrations create cluster-global roles and concurrent sessions collide. Implementers run the focused test files their task names and skip the full suite; the controller runs the full suite serially between tasks.
- Proxy tooling: `npm run typecheck` / `npm run lint` / `npm run test:unit` / `npm run test:integration` (Docker for testcontainers).
- Commit messages end with `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`.
- Working directories: services = `c:/Users/Mokshith work/Project/imageShield/image_flashbacklabs`; proxy = `c:/Users/Mokshith work/Project/imageShield/image_backend`. Every command below states which.

## File Structure

**Services (`image_flashbacklabs`)**

| File | Responsibility |
|---|---|
| `migrations/0026_articles.up.sql` / `.down.sql` | `articles` table, `content_rw` role, `svc.v_articles`, proxy grant |
| `src/imageshield/articles/__init__.py`, `store.py` | `ArticleStore` protocol + Postgres impl — the one writer of `articles` |
| `src/imageshield/http/models.py` | `ProviderReasonRequest.operator`; `Article*` request/response models |
| `src/imageshield/http/routes/admin_providers.py` | actor from body |
| `src/imageshield/http/routes/admin_articles.py` | `/v1/admin/articles` router |
| `src/imageshield/http/deps.py`, `app.py` | `get_article_store`, wiring |
| `src/imageshield/http/svc_contract.py` | `v_articles` joins the readiness contract |
| `src/imageshield/console/client.py` | provider writes + article methods |
| `src/imageshield/console/app.py` | provider routes, article routes, form parsing |
| `src/imageshield/console/templates/base.html`, `dashboard.html`, `articles.html`, `article_form.html` | nav, alarms/actions, article pages |
| `tests/test_admin_providers.py`, `tests/providers_fakes.py`, `tests/test_console.py`, `tests/test_readyz.py`, `tests/test_svc_views.py`, `tests/test_migrations.py` | extended |
| `tests/test_admin_articles_routes.py`, `tests/test_articles_store.py` | new |
| `SCHEMA.md`, `PROXY_INTEGRATION.md`, `CLAUDE.md`, `docs/ADMIN_PANEL_INTEGRATION.md`, `docs/OPERATIONS.md`, `docs/superpowers/specs/2026-08-20-recommendation-campaigns-design.md` | docs |

**Proxy (`image_backend`)**

| File | Responsibility |
|---|---|
| `src/services/contract/views.ts`, `readers.ts` | ninth view, two readers |
| `src/http/cursor.ts` | shared keyset cursor (new; score/support keep their private copies for now) |
| `src/articles/service.ts`, `routes.ts` | feed + single read |
| `src/errors/codes.ts`, `src/http/app.ts` | `ARTICLE_NOT_FOUND`, wiring |
| `fixtures/svc/10-svc-stubs.sql`, `fixtures/svc/README.md` | stub table + view + grant |
| `tests/repo-lint/contract-confinement.test.ts`, `tests/helpers/idor.ts` | six views; two IDOR entries |
| `tests/unit/cursor.test.ts`, `tests/integration/articles.test.ts` | new |
| `docs/CLAUDE.md` | §6 table row |

---

# Phase 1 — Services build

### Task 1: `operator` on the provider admin writes

**Files:**
- Modify: `src/imageshield/http/models.py:412-427`
- Modify: `src/imageshield/http/routes/admin_providers.py:58-118`

**Interfaces:**
- Produces: `ProviderReasonRequest.operator: str | None` (also on `ProviderDisableRequest`, which subclasses it). Audit `actor` is `body.operator or "admin_service_token"`.

- [ ] **Step 1: Add the field**

Replace the `ProviderReasonRequest` class body so it reads:

```python
class ProviderReasonRequest(ServiceModel):
    """A reason is mandatory on every admin write.

    Not paperwork: ``providers.enabled = false`` with no recorded reason is the
    state where nobody remembers whether the provider is off because of a
    billing surprise, a vendor breach, or a test somebody forgot to undo — and
    the difference decides whether turning it back on is safe.
    """

    reason: str = Field(min_length=3, max_length=500)
    # Who asked, when the caller can say. The console sends its Basic-auth
    # operator name (console/auth.py); a curl caller omits it and the audit
    # row records the token-holder fallback in routes/admin_providers.py.
    # Optional so every existing ``{"reason": ...}`` caller keeps working —
    # ServiceModel is extra='forbid', so the field must be declared to be sent.
    operator: str | None = Field(default=None, min_length=1, max_length=64)
```

- [ ] **Step 2: Use it in the three routes**

In `admin_providers.py` replace the `_ACTOR` comment + constant with:

```python
# The audit trail records who asked. The console names its operator in the
# body (ProviderReasonRequest.operator); a caller that names nobody — curl
# during an incident — is recorded as the token it held, rather than as a
# name we would be inventing.
_ACTOR = "admin_service_token"
```

and in `disable_provider`, `enable_provider`, `reset_breaker` change every `actor=_ACTOR` to `actor=body.operator or _ACTOR`.

- [ ] **Step 3: Verify**

Services dir: `ruff check .` → clean; `ruff format <new files only>`; `mypy` → clean; `pytest tests/test_admin_providers.py -q` → all pass (existing tests send no operator, so behaviour is unchanged).

- [ ] **Step 4: Commit**

```
git add src/imageshield/http/models.py src/imageshield/http/routes/admin_providers.py
git commit -m "feat(admin): provider writes may name the operator; audit actor falls back to the token"
```

---

### Task 2: Console provider ops — client, routes, dashboard

**Files:**
- Modify: `src/imageshield/console/client.py` (append after `open_hits`)
- Modify: `src/imageshield/console/app.py` (dashboard route; three new POST routes after `dashboard`)
- Rewrite: `src/imageshield/console/templates/dashboard.html`

**Interfaces:**
- Consumes: Task 1's `operator` body field.
- Produces: `ServicesClient.disable_provider(provider_id: str, *, reason: str, operator: str) -> None`, `enable_provider(...)`, `reset_breaker(...)`; console routes `POST /providers/{provider_id}/disable|enable|breaker-reset` (form `reason`, `csrf_token`) → `303 /`. Dashboard context gains `alarms`, `as_of`, `window_hours`, `csrf_token`.

- [ ] **Step 1: Client methods** — append to `ServicesClient`:

```python
    async def disable_provider(self, provider_id: str, *, reason: str, operator: str) -> None:
        await self._provider_write(provider_id, "disable", reason=reason, operator=operator)

    async def enable_provider(self, provider_id: str, *, reason: str, operator: str) -> None:
        await self._provider_write(provider_id, "enable", reason=reason, operator=operator)

    async def reset_breaker(self, provider_id: str, *, reason: str, operator: str) -> None:
        await self._provider_write(
            provider_id, "breaker/reset", reason=reason, operator=operator
        )

    async def _provider_write(
        self, provider_id: str, action: str, *, reason: str, operator: str
    ) -> None:
        # The operator travels in the body so the services audit row names a
        # person (ProviderReasonRequest.operator), not the shared admin token.
        response = await self._client.post(
            f"{self._base_url}/v1/admin/providers/{provider_id}/{action}",
            json={"reason": reason, "operator": operator},
            headers=self._headers,
        )
        self._raise_for_status(response)
```

- [ ] **Step 2: Dashboard route** — replace the `dashboard` handler with:

```python
@router.get("/")
async def dashboard(
    request: Request,
    operator: str = Depends(require_operator),
    services_client: ServicesClient = Depends(get_services_client),
    cfg: ConsoleConfig = Depends(get_console_config),
) -> HTMLResponse:
    health_data = await services_client.provider_health()
    queue = await services_client.review_queue()
    providers: list[dict[str, Any]] = list(health_data.get("providers", []))
    # Flattened so the page can lead with every alarm across every provider.
    # no_successful_calls_24h is the one that matters most: a dead provider
    # and a quiet week look identical without it.
    alarms = [
        {"provider_id": p.get("provider_id"), **a}
        for p in providers
        for a in p.get("alarms", [])
    ]
    return _templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "operator": operator,
            "providers": providers,
            "alarms": alarms,
            "as_of": health_data.get("as_of"),
            "window_hours": health_data.get("window_hours"),
            "queue": queue,
            "csrf_token": make_csrf_token(cfg, operator),
        },
    )
```

- [ ] **Step 3: Three write routes** — add directly after `dashboard`:

```python
@router.post("/providers/{provider_id}/disable")
async def provider_disable(
    provider_id: str,
    reason: str = Form(...),
    csrf_token: str = Form(""),
    operator: str = Depends(require_operator),
    services_client: ServicesClient = Depends(get_services_client),
    cfg: ConsoleConfig = Depends(get_console_config),
) -> RedirectResponse:
    """The kill switch, from the dashboard. Services validate provider_id
    (422 invalid_provider_id) and record the operator on the audit row."""
    verify_csrf_token(cfg, operator, csrf_token)
    await services_client.disable_provider(provider_id, reason=reason, operator=operator)
    return RedirectResponse(url="/", status_code=303)


@router.post("/providers/{provider_id}/enable")
async def provider_enable(
    provider_id: str,
    reason: str = Form(...),
    csrf_token: str = Form(""),
    operator: str = Depends(require_operator),
    services_client: ServicesClient = Depends(get_services_client),
    cfg: ConsoleConfig = Depends(get_console_config),
) -> RedirectResponse:
    verify_csrf_token(cfg, operator, csrf_token)
    await services_client.enable_provider(provider_id, reason=reason, operator=operator)
    return RedirectResponse(url="/", status_code=303)


@router.post("/providers/{provider_id}/breaker-reset")
async def provider_breaker_reset(
    provider_id: str,
    reason: str = Form(...),
    csrf_token: str = Form(""),
    operator: str = Depends(require_operator),
    services_client: ServicesClient = Depends(get_services_client),
    cfg: ConsoleConfig = Depends(get_console_config),
) -> RedirectResponse:
    """Separate from enable on purpose (admin_providers.py): "it is fixed, let
    it back in now" and "it should receive traffic at all" are different
    decisions."""
    verify_csrf_token(cfg, operator, csrf_token)
    await services_client.reset_breaker(provider_id, reason=reason, operator=operator)
    return RedirectResponse(url="/", status_code=303)
```

- [ ] **Step 4: Dashboard template** — replace `dashboard.html` entirely:

```html
{% extends "base.html" %}
{% block title %}Dashboard - ImageShield Console{% endblock %}
{% block content %}
<h1>Alarms</h1>
<table>
<tr><th>Provider</th><th>Alarm</th><th>Detail</th></tr>
{% for a in alarms %}
<tr class="flag"><td>{{ a.provider_id }}</td><td>{{ a.kind }}</td><td>{{ a.detail }}</td></tr>
{% else %}
<tr><td colspan="3" class="muted">No alarms firing.</td></tr>
{% endfor %}
</table>

<h1>Provider health</h1>
{% if as_of %}<p class="muted">as of {{ as_of }} · {{ window_hours }}h window · money is USD</p>{% endif %}
<table>
<tr>
  <th>Provider</th><th>Enabled</th><th>Breaker</th><th>Calls</th><th>Success</th>
  <th>OK (24h)</th><th>p50 / p99 ms</th><th>Cost today</th><th>Daily budget</th>
  <th>Headroom</th><th>Month to date</th><th>Monthly budget</th><th>Actions</th>
</tr>
{% for p in providers %}
<tr{% if not p.enabled or p.breaker_state != "closed" %} class="flag"{% endif %}>
  <td>{{ p.provider_id }}</td>
  <td>{{ "yes" if p.enabled else "no" }}</td>
  <td>{{ p.breaker_state }}{% if p.breaker_reason %} <span class="muted">({{ p.breaker_reason }})</span>{% endif %}</td>
  <td>{{ p.window_call_count }}</td>
  <td>{{ "%.0f%%" | format(p.success_rate * 100) if p.success_rate is not none else "—" }}</td>
  <td>{{ p.successful_calls_24h }}</td>
  <td>{{ p.latency_p50_ms if p.latency_p50_ms is not none else "—" }} / {{ p.latency_p99_ms if p.latency_p99_ms is not none else "—" }}</td>
  <td>{{ p.cost_usd }}</td>
  <td>{{ p.daily_budget_usd if p.daily_budget_usd is not none else "—" }}</td>
  <td>{{ p.budget_headroom_usd if p.budget_headroom_usd is not none else "—" }}</td>
  <td>{{ p.month_to_date_cost_usd }}</td>
  <td>{{ p.monthly_budget_usd if p.monthly_budget_usd is not none else "—" }}</td>
  <td>
    {% if p.enabled %}
    <form class="inline" method="post" action="/providers/{{ p.provider_id }}/disable">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <input type="text" name="reason" placeholder="reason (min 3 chars)" required minlength="3">
      <button type="submit">Disable</button>
    </form>
    {% else %}
    <form class="inline" method="post" action="/providers/{{ p.provider_id }}/enable">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <input type="text" name="reason" placeholder="reason (min 3 chars)" required minlength="3">
      <button type="submit">Enable</button>
    </form>
    {% endif %}
    {% if p.breaker_state != "closed" %}
    <form class="inline" method="post" action="/providers/{{ p.provider_id }}/breaker-reset">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <input type="text" name="reason" placeholder="reason (min 3 chars)" required minlength="3">
      <button type="submit">Reset breaker</button>
    </form>
    {% endif %}
  </td>
</tr>
{% else %}
<tr><td colspan="13" class="muted">No providers configured.</td></tr>
{% endfor %}
</table>

<h1>Review queue depths</h1>
<table>
<tr><th>Severity</th><th>Pending</th></tr>
{% for severity, count in queue.items() %}
<tr><td>{{ severity }}</td><td>{{ count }}</td></tr>
{% else %}
<tr><td colspan="2" class="muted">Queue is empty.</td></tr>
{% endfor %}
</table>
{% endblock %}
```

`—` is a literal em dash (the file is UTF-8). A `None` success rate renders `—`, never `0%`: the model's own comment says those are different facts.

- [ ] **Step 5: Verify** — services dir: `ruff check .`; `ruff format <new files only>`; `mypy`; `pytest tests/test_console.py -q` → existing tests pass (the fake returns `{"providers": []}`, which the new `.get` defaults tolerate).

- [ ] **Step 6: Commit**

```
git add src/imageshield/console/client.py src/imageshield/console/app.py src/imageshield/console/templates/dashboard.html
git commit -m "feat(console): alarms, spend and latency on the dashboard; kill switch, enable and breaker reset as forms"
```

---

### Task 3: Migration 0026 + the readiness contract

**Files:**
- Create: `migrations/0026_articles.up.sql`, `migrations/0026_articles.down.sql`
- Modify: `src/imageshield/http/svc_contract.py` (`EXPECTED_VIEWS`, docstring first line)

**Interfaces:**
- Produces: table `public.articles`, role `content_rw`, view `svc.v_articles(article_id uuid, title text, summary text, body text, images jsonb, sources jsonb, published_at timestamptz, updated_at timestamptz)` granted to `imageshield_proxy_ro`.

- [ ] **Step 1: Up migration** — `migrations/0026_articles.up.sql`:

```sql
-- Articles: operator-authored content for the app's feed, plus the ninth
-- svc contract view that carries published rows to the proxy.
-- Design: docs/superpowers/specs/2026-08-27-articles-design.md §4.
--
-- ADDITIVE ONLY. New table, new view, new role; no existing relation changes.
-- Articles are not identity data: no user_ref anywhere here, so the view
-- needs no per-person scoping (spec §3).

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'content_rw') THEN
    CREATE ROLE content_rw NOLOGIN;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO content_rw;

CREATE TABLE articles (
  article_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  title        TEXT NOT NULL CHECK (title <> ''),
  summary      TEXT NOT NULL DEFAULT '',
  body         TEXT NOT NULL DEFAULT '',
  -- [{"url": "https://...", "alt": "..."}], hero first. Pasted URLs, never
  -- bytes (INVARIANTS #9): this repo holds no S3 credentials, and this table
  -- holds no picture, only where one is.
  images       JSONB NOT NULL DEFAULT '[]'::jsonb,
  -- [{"name": "...", "url": "https://..."}]
  sources      JSONB NOT NULL DEFAULT '[]'::jsonb,
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

-- The feed's order and its keyset cursor: newest first, id as the tiebreak.
CREATE INDEX articles_published_idx ON articles (published_at DESC, article_id DESC)
  WHERE status = 'published';

-- Enumerated grants (0015's rule). No DELETE: archive is the soft delete.
GRANT SELECT, INSERT, UPDATE ON articles TO content_rw;

-- Reach the deployed login role, 0018/0022-style: conditional, idempotent, noisy.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_services') THEN
    GRANT content_rw TO app_services;
    RAISE NOTICE 'granted content_rw to app_services';
  ELSE
    RAISE NOTICE 'role app_services absent; content_rw not granted to it';
  END IF;
END
$$;

-- The ninth contract view. Published rows only: a draft or an archived
-- article is not the proxy's to see. Same versioned-contract rule as the
-- other eight (0016): add columns freely, never remove or retype one without
-- a coordinated deploy.
CREATE VIEW svc.v_articles AS
SELECT article_id, title, summary, body, images, sources, published_at, updated_at
FROM articles
WHERE status = 'published';

GRANT SELECT ON svc.v_articles TO imageshield_proxy_ro;
```

- [ ] **Step 2: Down migration** — `migrations/0026_articles.down.sql`:

```sql
-- Reverses 0026. Dropping svc.v_articles breaks the proxy's articles reader
-- (optional on their side -- the feed degrades to empty with a warn log) and
-- nothing here. Coordinated deploy, not a solo rollback.

REVOKE SELECT ON svc.v_articles FROM imageshield_proxy_ro;
DROP VIEW IF EXISTS svc.v_articles;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_services') THEN
    REVOKE content_rw FROM app_services;
  END IF;
END
$$;

DROP TABLE articles;

-- 0022's rule: a cluster-global role survives if another database on the
-- cluster still grants it anything. That is not a failure.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'content_rw') THEN
    REVOKE USAGE ON SCHEMA public FROM content_rw;
    BEGIN
      DROP ROLE content_rw;
    EXCEPTION WHEN dependent_objects_still_exist THEN
      RAISE NOTICE 'role content_rw still holds grants in another database; left in place';
    END;
  END IF;
END
$$;
```

- [ ] **Step 3: Readiness contract** — in `svc_contract.py` change the docstring's first line to `"""The expected shape of the nine `svc` contract views.` and append to `EXPECTED_VIEWS` after `"v_person_threat_context"`:

```python
    # ── 0026: articles — operator content, not identity data ───────────────
    "v_articles": {
        "article_id": "uuid",
        "title": "text",
        "summary": "text",
        "body": "text",
        "images": "jsonb",
        "sources": "jsonb",
        "published_at": "timestamp with time zone",
        "updated_at": "timestamp with time zone",
    },
```

- [ ] **Step 4: Keep the contract tests green in the same commit** — in `tests/test_readyz.py` rename `test_the_eight_views_are_all_declared` to `test_the_nine_views_are_all_declared` and add `"v_articles",` to its set. In `tests/test_svc_views.py` add `"v_articles",` to the `VIEWS` tuple; add to `FROZEN_CONTRACT_COLUMNS` (hand-maintained on purpose — read its comment before editing):

```python
    "v_articles": {
        "article_id",
        "title",
        "summary",
        "body",
        "images",
        "sources",
        "published_at",
        "updated_at",
    },
```

and add `"public.articles",` to the forbidden-table tuple in `test_the_proxy_role_reads_the_views_and_nothing_else`.

- [ ] **Step 5: Verify** — services dir with Docker up: `REQUIRE_DB=1 pytest tests/test_migrations.py tests/test_schema_lint.py tests/test_readyz.py tests/test_svc_views.py -q` → green (schema lint must accept `images`/`sources` jsonb; the readiness gate must see the grant). Without Docker those tests skip — say so in the report rather than reporting green. `ruff check .`; `mypy`.

- [ ] **Step 6: Commit**

```
git add migrations/0026_articles.up.sql migrations/0026_articles.down.sql src/imageshield/http/svc_contract.py tests/test_readyz.py tests/test_svc_views.py
git commit -m "feat(schema): 0026 articles table, content_rw role, svc.v_articles as the ninth contract view"
```

---

### Task 4: `articles/store.py`

**Files:**
- Create: `src/imageshield/articles/__init__.py` (one docstring line: `"""Articles — operator-authored content for the app feed."""`)
- Create: `src/imageshield/articles/store.py`

**Interfaces:**
- Produces:
  - `ArticleStore` protocol: `create_article(*, title, summary, body, images: list[dict[str, str]], sources: list[dict[str, str]], operator) -> UUID`; `update_article(article_id, *, title, summary, body, images, sources, operator) -> bool`; `publish_article(article_id, *, operator) -> str | None`; `archive_article(article_id, *, operator, reason) -> str | None`; `get_article(article_id) -> dict[str, Any] | None`; `list_articles(*, limit=50) -> list[dict[str, Any]]`.
  - Constants `ARTICLE_CREATED_ACTION = "article.created"`, `ARTICLE_UPDATED_ACTION`, `ARTICLE_PUBLISHED_ACTION`, `ARTICLE_ARCHIVED_ACTION`.
  - Row dict keys: `article_id, title, summary, body, images, sources, status, published_at, created_by, updated_by, created_at, updated_at`.

- [ ] **Step 1: Write the store**

```python
"""Articles — operator-authored content for the app feed (spec 2026-08-27).

The one writer of ``articles``. Every write is one transaction: the row
change plus one ``audit_log`` row naming the operator, the same shape as
``threats/store.py``. Reads are plain projections.

Not identity data: nothing here takes or returns a ``user_ref``. Pictures are
pasted URLs (INVARIANTS #9 -- no bytes, anywhere); the proxy reads published
rows through ``svc.v_articles`` (migration 0026), never this table.
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

import structlog
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

log = structlog.get_logger("imageshield.articles")

ARTICLE_CREATED_ACTION = "article.created"
ARTICLE_UPDATED_ACTION = "article.updated"
ARTICLE_PUBLISHED_ACTION = "article.published"
ARTICLE_ARCHIVED_ACTION = "article.archived"

_INSERT_SQL = """
    INSERT INTO articles (title, summary, body, images, sources, created_by, updated_by)
    VALUES (%(title)s, %(summary)s, %(body)s, %(images)s, %(sources)s,
            %(operator)s, %(operator)s)
    RETURNING article_id
"""

_UPDATE_SQL = """
    UPDATE articles
       SET title = %(title)s, summary = %(summary)s, body = %(body)s,
           images = %(images)s, sources = %(sources)s,
           updated_by = %(operator)s, updated_at = now()
     WHERE article_id = %(article_id)s
    RETURNING article_id
"""

# COALESCE keeps the original date on a re-publish from archived; a first
# publish stamps now(). The status filter makes an already-published row a
# no-op rather than a second audit entry for nothing.
_PUBLISH_SQL = """
    UPDATE articles
       SET status = 'published',
           published_at = COALESCE(published_at, now()),
           updated_by = %(operator)s, updated_at = now()
     WHERE article_id = %(article_id)s AND status <> 'published'
    RETURNING article_id
"""

_ARCHIVE_SQL = """
    UPDATE articles
       SET status = 'archived', updated_by = %(operator)s, updated_at = now()
     WHERE article_id = %(article_id)s AND status <> 'archived'
    RETURNING article_id
"""

_STATUS_SQL = "SELECT status FROM articles WHERE article_id = %(article_id)s"

_GET_SQL = """
    SELECT article_id, title, summary, body, images, sources, status, published_at,
           created_by, updated_by, created_at, updated_at
    FROM articles
    WHERE article_id = %(article_id)s
"""

_LIST_SQL = """
    SELECT article_id, title, summary, body, images, sources, status, published_at,
           created_by, updated_by, created_at, updated_at
    FROM articles
    ORDER BY updated_at DESC
    LIMIT %(limit)s
"""

_AUDIT_SQL = """
    INSERT INTO audit_log (actor_type, action, resource_id, metadata)
    VALUES ('operator', %(action)s, %(article_id)s, %(metadata)s)
"""


class ArticleStore(Protocol):
    async def create_article(
        self,
        *,
        title: str,
        summary: str,
        body: str,
        images: list[dict[str, str]],
        sources: list[dict[str, str]],
        operator: str,
    ) -> UUID: ...

    async def update_article(
        self,
        article_id: UUID,
        *,
        title: str,
        summary: str,
        body: str,
        images: list[dict[str, str]],
        sources: list[dict[str, str]],
        operator: str,
    ) -> bool: ...

    async def publish_article(self, article_id: UUID, *, operator: str) -> str | None: ...

    async def archive_article(
        self, article_id: UUID, *, operator: str, reason: str
    ) -> str | None: ...

    async def get_article(self, article_id: UUID) -> dict[str, Any] | None: ...

    async def list_articles(self, *, limit: int = 50) -> list[dict[str, Any]]: ...


def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "article_id": row[0],
        "title": row[1],
        "summary": row[2],
        "body": row[3],
        "images": list(row[4]),
        "sources": list(row[5]),
        "status": row[6],
        "published_at": row[7],
        "created_by": row[8],
        "updated_by": row[9],
        "created_at": row[10],
        "updated_at": row[11],
    }


class PostgresArticleStore:
    """The one writer of ``articles``."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def create_article(
        self,
        *,
        title: str,
        summary: str,
        body: str,
        images: list[dict[str, str]],
        sources: list[dict[str, str]],
        operator: str,
    ) -> UUID:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                _INSERT_SQL,
                {
                    "title": title,
                    "summary": summary,
                    "body": body,
                    "images": Jsonb(images),
                    "sources": Jsonb(sources),
                    "operator": operator,
                },
            )
            row = await cur.fetchone()
            assert row is not None
            article_id: UUID = row[0]
            await conn.execute(
                _AUDIT_SQL,
                {
                    "action": ARTICLE_CREATED_ACTION,
                    "article_id": article_id,
                    "metadata": Jsonb({"operator": operator, "title": title}),
                },
            )
        log.info("article.created", article_id=str(article_id), operator=operator)
        return article_id

    async def update_article(
        self,
        article_id: UUID,
        *,
        title: str,
        summary: str,
        body: str,
        images: list[dict[str, str]],
        sources: list[dict[str, str]],
        operator: str,
    ) -> bool:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                _UPDATE_SQL,
                {
                    "article_id": article_id,
                    "title": title,
                    "summary": summary,
                    "body": body,
                    "images": Jsonb(images),
                    "sources": Jsonb(sources),
                    "operator": operator,
                },
            )
            if await cur.fetchone() is None:
                return False
            await conn.execute(
                _AUDIT_SQL,
                {
                    "action": ARTICLE_UPDATED_ACTION,
                    "article_id": article_id,
                    "metadata": Jsonb({"operator": operator, "title": title}),
                },
            )
        log.info("article.updated", article_id=str(article_id), operator=operator)
        return True

    async def publish_article(self, article_id: UUID, *, operator: str) -> str | None:
        return await self._transition(
            article_id,
            _PUBLISH_SQL,
            action=ARTICLE_PUBLISHED_ACTION,
            metadata={"operator": operator},
            operator=operator,
        )

    async def archive_article(
        self, article_id: UUID, *, operator: str, reason: str
    ) -> str | None:
        return await self._transition(
            article_id,
            _ARCHIVE_SQL,
            action=ARTICLE_ARCHIVED_ACTION,
            metadata={"operator": operator, "reason": reason},
            operator=operator,
        )

    async def _transition(
        self,
        article_id: UUID,
        transition_sql: str,
        *,
        action: str,
        metadata: dict[str, Any],
        operator: str,
    ) -> str | None:
        """Apply a status change and audit it, or report the unchanged status.

        Returns the article's status after the call, or ``None`` for an
        unknown id. A no-op (already in the target state) writes no audit
        row: an audit entry for a write that did not happen is the same
        half-applied state ``threats/store.py`` refuses to leave behind.
        """
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                transition_sql, {"article_id": article_id, "operator": operator}
            )
            changed = await cur.fetchone() is not None
            if changed:
                await conn.execute(
                    _AUDIT_SQL,
                    {"action": action, "article_id": article_id, "metadata": Jsonb(metadata)},
                )
            cur = await conn.execute(_STATUS_SQL, {"article_id": article_id})
            row = await cur.fetchone()
        if row is None:
            return None
        status = str(row[0])
        log.info(action, article_id=str(article_id), operator=operator, changed=changed)
        return status

    async def get_article(self, article_id: UUID) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_GET_SQL, {"article_id": article_id})
            row = await cur.fetchone()
        return None if row is None else _row_to_dict(row)

    async def list_articles(self, *, limit: int = 50) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_LIST_SQL, {"limit": limit})
            rows = await cur.fetchall()
        return [_row_to_dict(row) for row in rows]
```

- [ ] **Step 2: Verify** — `ruff check .`; `ruff format <new files only>`; `mypy` → clean. `pytest tests/test_boundaries.py -q` → green (no phone-shaped literals, no face search, no S3).

- [ ] **Step 3: Commit**

```
git add src/imageshield/articles/
git commit -m "feat(articles): ArticleStore — one writer, one audit row per write"
```

---

### Task 5: Models, admin router, wiring

**Files:**
- Modify: `src/imageshield/http/models.py` (append after `ThreatEventsResponse`, before the review section)
- Create: `src/imageshield/http/routes/admin_articles.py`
- Modify: `src/imageshield/http/deps.py` (TYPE_CHECKING import + `get_article_store` after `get_threat_store`)
- Modify: `src/imageshield/http/app.py` (import, lifespan wiring after `threat_store`, `include_router` after `admin_threat_events_router`)

**Interfaces:**
- Consumes: Task 4's `ArticleStore`.
- Produces: `/v1/admin/articles` — `GET ""` → `{articles: [ArticleItem]}`; `POST ""` → `201 {article_id, status: "draft"}`; `GET /{id}` → `ArticleItem`; `PUT /{id}` → `ArticleItem`; `POST /{id}/publish` body `{operator}` → `{article_id, status}`; `POST /{id}/archive` body `{operator, reason}` → `{article_id, status}`; `404 article_not_found`. `app.state.article_store`.

- [ ] **Step 1: Models** — append to `models.py`:

```python
# ── articles (spec 2026-08-27) ────────────────────────────────────────────

_URL_MAX = 2000


def _https_only(value: str) -> str:
    # The app opens these. An http:// picture or source is a 422 here rather
    # than a mixed-content failure on a phone.
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.netloc:
        raise ValueError("must be an https:// URL")
    return value


class ArticleImage(ServiceModel):
    url: str = Field(min_length=9, max_length=_URL_MAX)
    alt: str = Field(default="", max_length=300)

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        return _https_only(value)


class ArticleSource(ServiceModel):
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=9, max_length=_URL_MAX)

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        return _https_only(value)


class ArticleUpsertRequest(ServiceModel):
    """Create and edit share one shape: an article is its whole content."""

    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=500)
    body: str = Field(default="", max_length=20_000)
    images: tuple[ArticleImage, ...] = Field(default=(), max_length=10)
    sources: tuple[ArticleSource, ...] = Field(default=(), max_length=10)
    operator: str = Field(min_length=1, max_length=64)


class ArticlePublishRequest(ServiceModel):
    operator: str = Field(min_length=1, max_length=64)


class ArticleArchiveRequest(ServiceModel):
    operator: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=3, max_length=500)


class ArticleItem(BaseModel):
    article_id: UUID
    title: str
    summary: str
    body: str
    images: list[dict[str, str]]
    sources: list[dict[str, str]]
    status: str
    published_at: datetime | None
    created_by: str
    updated_by: str
    created_at: datetime
    updated_at: datetime


class ArticlesResponse(BaseModel):
    articles: list[ArticleItem]


class ArticleCreateResponse(BaseModel):
    article_id: UUID
    status: Literal["draft"] = "draft"


class ArticleStatusResponse(BaseModel):
    article_id: UUID
    status: str
```

- [ ] **Step 2: Router** — `src/imageshield/http/routes/admin_articles.py`:

```python
"""Articles — operator CRUD for the app's feed (spec 2026-08-27 §6).

Same posture as ``admin_threat_events.py``: both tokens at router level, so a
route added here is guarded structurally. Nothing here touches a score, a
subject or a hit — an article is operator content published to every user
with no per-person state — so there is no recompute loop and no ``user_ref``
anywhere in this file.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Query

from imageshield.articles.store import ArticleStore
from imageshield.http.auth import require_admin_service_token, require_service_token
from imageshield.http.deps import get_article_store
from imageshield.http.errors import ServiceError
from imageshield.http.models import (
    ArticleArchiveRequest,
    ArticleCreateResponse,
    ArticleItem,
    ArticlePublishRequest,
    ArticlesResponse,
    ArticleStatusResponse,
    ArticleUpsertRequest,
)

log = structlog.get_logger("imageshield.articles")

router = APIRouter(
    prefix="/v1/admin/articles",
    dependencies=[Depends(require_service_token), Depends(require_admin_service_token)],
)


def _not_found() -> ServiceError:
    return ServiceError(404, "article_not_found", "No article with this id.", retryable=False)


def _item(row: dict[str, Any]) -> ArticleItem:
    return ArticleItem(
        article_id=row["article_id"],
        title=row["title"],
        summary=row["summary"],
        body=row["body"],
        images=list(row["images"]),
        sources=list(row["sources"]),
        status=row["status"],
        published_at=row["published_at"],
        created_by=row["created_by"],
        updated_by=row["updated_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _content(body: ArticleUpsertRequest) -> dict[str, Any]:
    return {
        "title": body.title,
        "summary": body.summary,
        "body": body.body,
        "images": [image.model_dump() for image in body.images],
        "sources": [source.model_dump() for source in body.sources],
        "operator": body.operator,
    }


@router.get("")
async def list_articles(
    limit: int = Query(50, ge=1, le=200),
    store: ArticleStore = Depends(get_article_store),
) -> ArticlesResponse:
    rows = await store.list_articles(limit=limit)
    return ArticlesResponse(articles=[_item(row) for row in rows])


@router.post("", status_code=201)
async def create_article(
    body: ArticleUpsertRequest,
    store: ArticleStore = Depends(get_article_store),
) -> ArticleCreateResponse:
    article_id = await store.create_article(**_content(body))
    log.info("article.created_via_admin", article_id=str(article_id), operator=body.operator)
    return ArticleCreateResponse(article_id=article_id)


@router.get("/{article_id}")
async def get_article(
    article_id: UUID,
    store: ArticleStore = Depends(get_article_store),
) -> ArticleItem:
    row = await store.get_article(article_id)
    if row is None:
        raise _not_found()
    return _item(row)


@router.put("/{article_id}")
async def update_article(
    article_id: UUID,
    body: ArticleUpsertRequest,
    store: ArticleStore = Depends(get_article_store),
) -> ArticleItem:
    if not await store.update_article(article_id, **_content(body)):
        raise _not_found()
    row = await store.get_article(article_id)
    if row is None:  # nothing deletes an article; this keeps the type honest
        raise _not_found()
    return _item(row)


@router.post("/{article_id}/publish")
async def publish_article(
    article_id: UUID,
    body: ArticlePublishRequest,
    store: ArticleStore = Depends(get_article_store),
) -> ArticleStatusResponse:
    status = await store.publish_article(article_id, operator=body.operator)
    if status is None:
        raise _not_found()
    return ArticleStatusResponse(article_id=article_id, status=status)


@router.post("/{article_id}/archive")
async def archive_article(
    article_id: UUID,
    body: ArticleArchiveRequest,
    store: ArticleStore = Depends(get_article_store),
) -> ArticleStatusResponse:
    status = await store.archive_article(
        article_id, operator=body.operator, reason=body.reason
    )
    if status is None:
        raise _not_found()
    return ArticleStatusResponse(article_id=article_id, status=status)
```

- [ ] **Step 3: deps.py** — inside the `if TYPE_CHECKING:` block add `from imageshield.articles.store import ArticleStore` (alphabetical: first line of the block, before `enrolment`). After `get_threat_store` add:

```python
def get_article_store(request: Request) -> ArticleStore:
    store: ArticleStore = _required_state(request, "article_store")  # type: ignore[assignment]
    return store
```

- [ ] **Step 4: app.py** — add imports `from imageshield.articles.store import PostgresArticleStore` (before `imageshield.attribution.fetch`) and `from imageshield.http.routes.admin_articles import router as admin_articles_router` (before `admin_providers`). In `_lifespan`, after the `threat_store` guard:

```python
    if getattr(app.state, "article_store", None) is None:
        app.state.article_store = PostgresArticleStore(pool)
```

In `create_app`, after `app.include_router(admin_threat_events_router)`: `app.include_router(admin_articles_router)`.

- [ ] **Step 5: Verify** — `ruff check .` (it will reorder imports if needed — accept with `ruff check --fix .`); `ruff format <new files only>`; `mypy` → clean; `pytest tests/test_auth.py tests/test_error_envelope.py -q` → green (route-auth gates walk the router table).

- [ ] **Step 6: Commit**

```
git add src/imageshield/http/models.py src/imageshield/http/routes/admin_articles.py src/imageshield/http/deps.py src/imageshield/http/app.py
git commit -m "feat(admin): /v1/admin/articles — create, edit, publish, archive; dual-token, operator on every write"
```

---

### Task 6: Console articles pages

**Files:**
- Modify: `src/imageshield/console/client.py` (append)
- Modify: `src/imageshield/console/app.py` (helpers + routes after `scores_get`; `Response` import)
- Modify: `src/imageshield/console/templates/base.html` (nav)
- Create: `src/imageshield/console/templates/articles.html`, `article_form.html`

**Interfaces:**
- Consumes: Task 5's admin API.
- Produces: `ServicesClient.list_articles() -> list[dict]`, `get_article(UUID) -> dict | None`, `create_article(payload) -> dict`, `update_article(UUID, payload) -> dict`, `publish_article(UUID, *, operator) -> None`, `archive_article(UUID, *, operator, reason) -> None`. Console routes: `GET /articles`, `GET /articles/new`, `POST /articles`, `GET /articles/{id}`, `POST /articles/{id}`, `POST /articles/{id}/publish`, `POST /articles/{id}/archive`.

- [ ] **Step 1: Client methods** — append to `ServicesClient`:

```python
    async def list_articles(self) -> list[dict[str, Any]]:
        response = await self._client.get(
            f"{self._base_url}/v1/admin/articles", headers=self._headers
        )
        self._raise_for_status(response)
        data: dict[str, Any] = response.json()
        return list(data.get("articles", []))

    async def get_article(self, article_id: UUID) -> dict[str, Any] | None:
        response = await self._client.get(
            f"{self._base_url}/v1/admin/articles/{article_id}", headers=self._headers
        )
        if response.status_code == 404:
            return None
        self._raise_for_status(response)
        data: dict[str, Any] = response.json()
        return data

    async def create_article(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(
            f"{self._base_url}/v1/admin/articles", json=payload, headers=self._headers
        )
        self._raise_for_status(response)
        data: dict[str, Any] = response.json()
        return data

    async def update_article(self, article_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.put(
            f"{self._base_url}/v1/admin/articles/{article_id}",
            json=payload,
            headers=self._headers,
        )
        self._raise_for_status(response)
        data: dict[str, Any] = response.json()
        return data

    async def publish_article(self, article_id: UUID, *, operator: str) -> None:
        response = await self._client.post(
            f"{self._base_url}/v1/admin/articles/{article_id}/publish",
            json={"operator": operator},
            headers=self._headers,
        )
        self._raise_for_status(response)

    async def archive_article(self, article_id: UUID, *, operator: str, reason: str) -> None:
        response = await self._client.post(
            f"{self._base_url}/v1/admin/articles/{article_id}/archive",
            json={"operator": operator, "reason": reason},
            headers=self._headers,
        )
        self._raise_for_status(response)
```

- [ ] **Step 2: Helpers + routes** — in `console/app.py` change the responses import to `from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response`, then append after `scores_get`. ROUTE ORDER MATTERS: `GET /articles/new` must be registered before `GET /articles/{article_id}` (Starlette matches in order and `new` is not a UUID).

```python
# ── articles (spec 2026-08-27 §7) ─────────────────────────────────────────


def _parse_pairs(raw: str, first: str, second: str) -> list[dict[str, str]]:
    """One ``left | right`` per line into ``[{first: left, second: right}]``.

    The console's form encoding for the plural article fields: ``url | alt``
    for pictures, ``name | url`` for sources. A line with no bar keeps its
    whole text as ``first`` and an empty ``second``; blank lines are skipped.
    Validation is the API's -- a bad URL comes back as its 422, rendered
    through the upstream-error page.
    """
    pairs: list[dict[str, str]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        left, _, right = line.partition("|")
        pairs.append({first: left.strip(), second: right.strip()})
    return pairs


def _pairs_text(items: list[dict[str, Any]], first: str, second: str) -> str:
    return "\n".join(f"{item.get(first, '')} | {item.get(second, '')}" for item in items)


def _article_payload(
    *, title: str, summary: str, body: str, images: str, sources: str, operator: str
) -> dict[str, Any]:
    return {
        "title": title,
        "summary": summary,
        "body": body,
        "images": _parse_pairs(images, "url", "alt"),
        "sources": _parse_pairs(sources, "name", "url"),
        "operator": operator,
    }


def _render_article_form(
    request: Request, operator: str, cfg: ConsoleConfig, *, article: dict[str, Any] | None
) -> HTMLResponse:
    return _templates.TemplateResponse(
        request,
        "article_form.html",
        {
            "operator": operator,
            "article": article,
            "images_text": (
                _pairs_text(list(article.get("images", [])), "url", "alt") if article else ""
            ),
            "sources_text": (
                _pairs_text(list(article.get("sources", [])), "name", "url") if article else ""
            ),
            "csrf_token": make_csrf_token(cfg, operator),
        },
    )


@router.get("/articles")
async def articles_list(
    request: Request,
    operator: str = Depends(require_operator),
    services_client: ServicesClient = Depends(get_services_client),
) -> HTMLResponse:
    articles = await services_client.list_articles()
    return _templates.TemplateResponse(
        request, "articles.html", {"operator": operator, "articles": articles}
    )


@router.get("/articles/new")
async def articles_new(
    request: Request,
    operator: str = Depends(require_operator),
    cfg: ConsoleConfig = Depends(get_console_config),
) -> HTMLResponse:
    return _render_article_form(request, operator, cfg, article=None)


@router.post("/articles")
async def articles_create(
    title: str = Form(...),
    summary: str = Form(""),
    body: str = Form(""),
    images: str = Form(""),
    sources: str = Form(""),
    csrf_token: str = Form(""),
    operator: str = Depends(require_operator),
    services_client: ServicesClient = Depends(get_services_client),
    cfg: ConsoleConfig = Depends(get_console_config),
) -> RedirectResponse:
    verify_csrf_token(cfg, operator, csrf_token)
    created = await services_client.create_article(
        _article_payload(
            title=title, summary=summary, body=body, images=images, sources=sources,
            operator=operator,
        )
    )
    return RedirectResponse(url=f"/articles/{created['article_id']}", status_code=303)


@router.get("/articles/{article_id}")
async def articles_edit(
    article_id: UUID,
    request: Request,
    operator: str = Depends(require_operator),
    services_client: ServicesClient = Depends(get_services_client),
    cfg: ConsoleConfig = Depends(get_console_config),
) -> Response:
    article = await services_client.get_article(article_id)
    if article is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "article_not_found", "message": "No article with this id."}},
        )
    return _render_article_form(request, operator, cfg, article=article)


@router.post("/articles/{article_id}")
async def articles_update(
    article_id: UUID,
    title: str = Form(...),
    summary: str = Form(""),
    body: str = Form(""),
    images: str = Form(""),
    sources: str = Form(""),
    csrf_token: str = Form(""),
    operator: str = Depends(require_operator),
    services_client: ServicesClient = Depends(get_services_client),
    cfg: ConsoleConfig = Depends(get_console_config),
) -> RedirectResponse:
    verify_csrf_token(cfg, operator, csrf_token)
    await services_client.update_article(
        article_id,
        _article_payload(
            title=title, summary=summary, body=body, images=images, sources=sources,
            operator=operator,
        ),
    )
    return RedirectResponse(url=f"/articles/{article_id}", status_code=303)


@router.post("/articles/{article_id}/publish")
async def articles_publish(
    article_id: UUID,
    csrf_token: str = Form(""),
    operator: str = Depends(require_operator),
    services_client: ServicesClient = Depends(get_services_client),
    cfg: ConsoleConfig = Depends(get_console_config),
) -> RedirectResponse:
    verify_csrf_token(cfg, operator, csrf_token)
    await services_client.publish_article(article_id, operator=operator)
    return RedirectResponse(url=f"/articles/{article_id}", status_code=303)


@router.post("/articles/{article_id}/archive")
async def articles_archive(
    article_id: UUID,
    reason: str = Form(...),
    csrf_token: str = Form(""),
    operator: str = Depends(require_operator),
    services_client: ServicesClient = Depends(get_services_client),
    cfg: ConsoleConfig = Depends(get_console_config),
) -> RedirectResponse:
    verify_csrf_token(cfg, operator, csrf_token)
    await services_client.archive_article(article_id, operator=operator, reason=reason)
    return RedirectResponse(url=f"/articles/{article_id}", status_code=303)
```

- [ ] **Step 3: Nav** — in `base.html` add `<a href="/articles">Articles</a>` after the Scores link.

- [ ] **Step 4: `articles.html`**

```html
{% extends "base.html" %}
{% block title %}Articles - ImageShield Console{% endblock %}
{% block content %}
<h1>Articles</h1>
<p class="muted">A published article appears in every user's in-app feed on their next open — no
notification (digests only, INVARIANTS #24). Pictures are pasted URLs; the console shows them as
links, never as images.</p>
<p><a href="/articles/new">New article</a></p>
<table>
<tr><th>Title</th><th>Status</th><th>Published</th><th>Updated</th><th>By</th><th></th></tr>
{% for a in articles %}
<tr>
  <td>{{ a.title }}</td>
  <td>{{ a.status }}</td>
  <td>{{ a.published_at if a.published_at else "—" }}</td>
  <td>{{ a.updated_at }}</td>
  <td>{{ a.updated_by }}</td>
  <td><a href="/articles/{{ a.article_id }}">Edit</a></td>
</tr>
{% else %}
<tr><td colspan="6" class="muted">No articles yet.</td></tr>
{% endfor %}
</table>
{% endblock %}
```

- [ ] **Step 5: `article_form.html`**

```html
{% extends "base.html" %}
{% block title %}{{ "Edit article" if article else "New article" }} - ImageShield Console{% endblock %}
{% block content %}
<h1>{{ article.title if article else "New article" }}</h1>
{% if article %}
<p class="muted">Status: <strong>{{ article.status }}</strong>{% if article.published_at %} · published {{ article.published_at }}{% endif %}
 · last edited by {{ article.updated_by }} at {{ article.updated_at }}</p>
{% endif %}

<div class="card">
  <form method="post" action="{{ '/articles/' ~ article.article_id if article else '/articles' }}">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <label>Title <input type="text" name="title" value="{{ article.title if article else '' }}" required maxlength="200"></label>
    <label>Summary (the card blurb, up to 500 characters)
      <textarea name="summary" rows="2" maxlength="500">{{ article.summary if article else '' }}</textarea></label>
    <label>Body (markdown, up to 20000 characters)
      <textarea name="body" rows="12" maxlength="20000">{{ article.body if article else '' }}</textarea></label>
    <label>Pictures — one per line as <code>https://url | alt text</code>; hero first; https only; max 10
      <textarea name="images" rows="3">{{ images_text }}</textarea></label>
    <label>Sources — one per line as <code>Name | https://url</code>; https only; max 10
      <textarea name="sources" rows="3">{{ sources_text }}</textarea></label>
    <button type="submit">{{ "Save changes" if article else "Create draft" }}</button>
  </form>
</div>

{% if article %}
<div class="card">
  <h2>Pictures and sources</h2>
  <p class="muted">Links only — the console never renders a picture.</p>
  <ul>
  {% for i in article.images %}
    <li><a href="{{ i.url }}" target="_blank" rel="noopener noreferrer">{{ i.url }}</a>{% if i.alt %} — {{ i.alt }}{% endif %}</li>
  {% endfor %}
  {% for s in article.sources %}
    <li>{{ s.name }}: <a href="{{ s.url }}" target="_blank" rel="noopener noreferrer">{{ s.url }}</a></li>
  {% endfor %}
  </ul>
</div>

<div class="card">
  <h2>Lifecycle</h2>
  {% if article.status != "published" %}
  <form class="inline" method="post" action="/articles/{{ article.article_id }}/publish">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <button type="submit">Publish</button>
  </form>
  {% endif %}
  {% if article.status != "archived" %}
  <form class="inline" method="post" action="/articles/{{ article.article_id }}/archive">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <input type="text" name="reason" placeholder="reason (min 3 chars)" required minlength="3">
    <button type="submit">Archive</button>
  </form>
  {% endif %}
</div>
{% endif %}
{% endblock %}
```

- [ ] **Step 6: Verify** — `ruff check .`; `ruff format <new files only>`; `mypy`; `pytest tests/test_console.py -q` → existing tests still green. Then a manual smoke: `python -c "from imageshield.console.app import create_app"` imports cleanly.

- [ ] **Step 7: Commit**

```
git add src/imageshield/console/
git commit -m "feat(console): articles — list, draft, edit, publish, archive; pictures as links only"
```

---

# Phase 2 — Proxy build

### Task 7: Proxy branch, contract module, fixture

**Working dir:** `image_backend`.

**Files:**
- Modify: `src/services/contract/views.ts` (`CONTRACT_VIEWS`, `CONTRACT_VIEW_COLUMNS`, comment on `READINESS_REQUIRED`)
- Modify: `src/services/contract/readers.ts` (append)
- Modify: `fixtures/svc/10-svc-stubs.sql` (stub table after `_stub_liveness_attempts`; view after `v_person_threat_context`; grant line)
- Modify: `tests/repo-lint/contract-confinement.test.ts`

**Interfaces:**
- Produces: `CONTRACT_VIEWS.articles = 'svc.v_articles'`; `readPublishedArticles(pool, {limit, after: {publishedAt, id} | null}) => PublishedArticle[] | 'unavailable'`; `readPublishedArticle(pool, id) => PublishedArticle | null | 'unavailable'`; `interface PublishedArticle { article_id; title; summary; body; images: {url; alt}[]; sources: {name; url}[]; published_at: Date; updated_at: Date }`.

- [ ] **Step 1: Branch** — `git status --short` (expect only the pre-existing untracked `.claude/` and root `CLAUDE.md`), then `git checkout -b feat/articles main`. If git refuses because untracked `CLAUDE.md` would be overwritten, `git stash push -u -- CLAUDE.md` first and `git stash pop` after; the file is a two-line pointer to `docs/CLAUDE.md`.

- [ ] **Step 2: views.ts** — in `CONTRACT_VIEWS` after `threatContext` add:

```ts
  // ARTICLES — their 0026 (spec 2026-08-27 in image_flashbacklabs). Operator
  // content for the app feed: not identity data, no person column, no
  // scoping. OPTIONAL for readiness — see READINESS_REQUIRED.
  articles: 'svc.v_articles',
```

In `CONTRACT_VIEW_COLUMNS` add:

```ts
  'svc.v_articles': [
    'article_id',
    'title',
    'summary',
    'body',
    'images',
    'sources',
    'published_at',
    'updated_at',
  ],
```

Above `READINESS_REQUIRED` append to its doc comment: ` * svc.v_articles is optional for the same reason liveness attempts is: an api deployed against a database without their 0026 must boot, and GET /v1/articles degrades to an empty feed with a warn log. Present-but-wrong-shaped still fails readiness.`

- [ ] **Step 3: readers.ts** — append:

```ts
export interface PublishedArticle {
  article_id: string;
  title: string;
  summary: string;
  body: string;
  images: { url: string; alt: string }[];
  sources: { name: string; url: string }[];
  published_at: Date;
  updated_at: Date;
}

/**
 * Published articles, newest first, keyset-paged on (published_at, article_id).
 * `'unavailable'` when their 0026 has not landed: the feed degrades to empty
 * (P24) — same sentinel and same reasoning as readPersonLivenessAttempts.
 * Operator content for every user, so there is deliberately no person filter.
 */
export async function readPublishedArticles(
  pool: pg.Pool,
  page: { limit: number; after: { publishedAt: string; id: string } | null },
): Promise<PublishedArticle[] | 'unavailable'> {
  try {
    const result = await pool.query<PublishedArticle>(
      `SELECT article_id, title, summary, body, images, sources, published_at, updated_at
         FROM svc.v_articles
        WHERE ($1::timestamptz IS NULL
               OR (published_at, article_id) < ($1::timestamptz, $2::uuid))
        ORDER BY published_at DESC, article_id DESC
        LIMIT $3`,
      [
        page.after?.publishedAt ?? null,
        page.after?.id ?? '00000000-0000-0000-0000-000000000000',
        page.limit,
      ],
    );
    return result.rows;
  } catch (error) {
    if (isMissingView(error)) return 'unavailable';
    throw error;
  }
}

export async function readPublishedArticle(
  pool: pg.Pool,
  articleId: string,
): Promise<PublishedArticle | null | 'unavailable'> {
  try {
    const result = await pool.query<PublishedArticle>(
      `SELECT article_id, title, summary, body, images, sources, published_at, updated_at
         FROM svc.v_articles
        WHERE article_id = $1`,
      [articleId],
    );
    return result.rows[0] ?? null;
  } catch (error) {
    if (isMissingView(error)) return 'unavailable';
    throw error;
  }
}
```

- [ ] **Step 4: Fixture** — after `CREATE TABLE svc._stub_liveness_attempts (...)` add:

```sql
-- Backing store for CONTRACT VIEW 9 (their 0026): published articles. Their
-- base table is public.articles with a status column and the view shows
-- published rows only, so the stub carries exactly what the view exposes.
CREATE TABLE svc._stub_articles (
  article_id   UUID PRIMARY KEY,
  title        TEXT NOT NULL,
  summary      TEXT NOT NULL DEFAULT '',
  body         TEXT NOT NULL DEFAULT '',
  images       JSONB NOT NULL DEFAULT '[]'::jsonb,
  sources      JSONB NOT NULL DEFAULT '[]'::jsonb,
  published_at TIMESTAMPTZ NOT NULL,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

After the `svc.v_person_threat_context` view add:

```sql
CREATE VIEW svc.v_articles AS
SELECT article_id, title, summary, body, images, sources, published_at, updated_at
FROM svc._stub_articles;
```

After the 0023 grant statement add `GRANT SELECT ON svc.v_articles TO imageshield_proxy_ro;`.

- [ ] **Step 5: Confinement lint** — in `contract-confinement.test.ts` change the regex `/svc\.v_person_/` to `/svc\.v_/` (so `svc.v_articles` is confined too; update the `it(...)` title to `names svc.v_* only inside src/services/contract/`), and add `'svc.v_articles',` as the first entry of the expected sorted list (rename the test to `...the six views this line reads`, and add a sentence to the comment: `2026-08-27: svc.v_articles joins, read by src/articles/ through this module.`).

- [ ] **Step 6: Verify** — `npm run typecheck`; `npm run lint`; `npm run test:unit` → green (confinement passes with six). `npm run test:integration -- contract-readiness` → green (the optional view is present in the fixture).

- [ ] **Step 7: Commit**

```
git add src/services/contract/views.ts src/services/contract/readers.ts fixtures/svc/10-svc-stubs.sql tests/repo-lint/contract-confinement.test.ts
git commit -m "feat(contract): svc.v_articles — ninth view, optional for readiness, two readers"
```

---

### Task 8: Proxy articles service, routes, wiring

**Files:**
- Create: `src/http/cursor.ts`, `src/articles/service.ts`, `src/articles/routes.ts`
- Modify: `src/errors/codes.ts` (before `] as const;`), `src/http/app.ts` (imports; wiring before `return app;`), `tests/helpers/idor.ts` (two entries before the `DELETE /v1/me` block)

**Interfaces:**
- Consumes: Task 7's readers.
- Produces: `GET /v1/articles?limit&cursor` → `{items: ArticleCard[], next_cursor: string | null}`; `GET /v1/articles/:articleId` → `ArticleCard` or `404 ARTICLE_NOT_FOUND`. `encodeCursor(at: Date, id: string): string`, `decodeCursor(cursor: string | null): {at: string; id: string} | null`.

- [ ] **Step 1: `src/http/cursor.ts`**

```ts
import { AppError } from '../errors/app-error.js';

const HTTP_BAD_REQUEST = 400;
const CURSOR_UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * Keyset cursor over (timestamp, uuid) — the shape src/score/service.ts and
 * src/support/service.ts each carry privately. Extracted here for the
 * articles feed rather than copied a third time; those two can move onto it
 * when next touched. Opaque to the client, validated HERE so a tampered
 * cursor is a 400 and never a 22007 from Postgres.
 */
export interface KeysetCursor {
  at: string;
  id: string;
}

export function encodeCursor(at: Date, id: string): string {
  return Buffer.from(`${at.toISOString()}|${id}`).toString('base64url');
}

export function decodeCursor(cursor: string | null): KeysetCursor | null {
  if (cursor === null) return null;
  const [at, id] = Buffer.from(cursor, 'base64url').toString().split('|');
  if (at === undefined || id === undefined || !CURSOR_UUID_RE.test(id) || Number.isNaN(Date.parse(at))) {
    throw new AppError('VALIDATION_FAILED', HTTP_BAD_REQUEST, 'malformed cursor');
  }
  return { at, id };
}
```

- [ ] **Step 2: `src/articles/service.ts`**

```ts
import type pg from 'pg';
import { AppError } from '../errors/app-error.js';
import { decodeCursor, encodeCursor } from '../http/cursor.js';
import type { Logger } from '../logging/logger.js';
import {
  readPublishedArticle,
  readPublishedArticles,
  type PublishedArticle,
} from '../services/contract/readers.js';

const HTTP_NOT_FOUND = 404;

export interface ArticleCard {
  article_id: string;
  title: string;
  summary: string;
  body: string;
  images: { url: string; alt: string }[];
  sources: { name: string; url: string }[];
  published_at: string;
  updated_at: string;
}

export interface ArticlesServiceDeps {
  pool: pg.Pool;
  logger: Logger;
}

/**
 * The articles feed (spec 2026-08-27 §8 in image_flashbacklabs): operator
 * content read through svc.v_articles. Every user sees the same list — no
 * person appears in these queries, by design — so nothing here takes a
 * caller. A missing view (their 0026 not yet deployed) is an empty feed plus
 * a warn log, never a 5xx: an empty feed is a normal state (P24).
 */
export class ArticlesService {
  readonly #pool: pg.Pool;
  readonly #logger: Logger;

  constructor(deps: ArticlesServiceDeps) {
    this.#pool = deps.pool;
    this.#logger = deps.logger;
  }

  async list(page: {
    cursor: string | null;
    limit: number;
  }): Promise<{ items: ArticleCard[]; next_cursor: string | null }> {
    const after = decodeCursor(page.cursor);
    const rows = await readPublishedArticles(this.#pool, {
      limit: page.limit + 1,
      after: after === null ? null : { publishedAt: after.at, id: after.id },
    });
    if (rows === 'unavailable') {
      this.#logger.warn({ view: 'svc.v_articles' }, 'articles view unavailable; serving an empty feed');
      return { items: [], next_cursor: null };
    }
    const items = rows.slice(0, page.limit);
    const last = items[items.length - 1];
    const hasMore = rows.length > page.limit;
    return {
      items: items.map(toCard),
      next_cursor:
        hasMore && last !== undefined ? encodeCursor(last.published_at, last.article_id) : null,
    };
  }

  async get(articleId: string): Promise<ArticleCard> {
    const row = await readPublishedArticle(this.#pool, articleId);
    if (row === 'unavailable' || row === null) {
      throw new AppError('ARTICLE_NOT_FOUND', HTTP_NOT_FOUND, 'no such article');
    }
    return toCard(row);
  }
}

function toCard(row: PublishedArticle): ArticleCard {
  return {
    article_id: row.article_id,
    title: row.title,
    summary: row.summary,
    body: row.body,
    images: row.images,
    sources: row.sources,
    published_at: row.published_at.toISOString(),
    updated_at: row.updated_at.toISOString(),
  };
}
```

If `Logger` from `../logging/logger.js` is not pino-shaped (`warn(obj, msg)`), match whatever signature `src/reports/service.ts` uses for its logger — check before changing the call.

- [ ] **Step 3: `src/articles/routes.ts`**

```ts
import { z } from 'zod';
import type { FastifyInstance, preHandlerAsyncHookHandler } from 'fastify';
import { parseOrThrow } from '../http/validate.js';
import type { ArticlesService } from './service.js';

/** A feed, not an export. Not a tunable. */
const ARTICLES_PAGE_SIZE = 20;

/** Strict: an unknown parameter (e.g. `?person_id=`) is a 400. */
const listQuery = z
  .object({
    cursor: z.string().min(1).max(200).optional(),
    limit: z.coerce.number().int().min(1).max(ARTICLES_PAGE_SIZE).optional(),
  })
  .strict();

const articleParams = z.object({ articleId: z.string().uuid() }).strict();

/**
 * GET /v1/articles, GET /v1/articles/:articleId — the in-app feed of
 * operator-published articles. Session-authenticated like all of /v1/*, but
 * the person is never read: every user sees the same feed, so there is
 * nothing to scope and no identifier to accept.
 */
export function registerArticleRoutes(
  app: FastifyInstance,
  deps: { service: ArticlesService; requireAuth: preHandlerAsyncHookHandler },
): void {
  app.get(
    '/v1/articles',
    { config: { authPolicy: 'session' }, preHandler: deps.requireAuth },
    async (request) => {
      const query = parseOrThrow(listQuery, request.query);
      return deps.service.list({
        cursor: query.cursor ?? null,
        limit: query.limit ?? ARTICLES_PAGE_SIZE,
      });
    },
  );

  app.get(
    '/v1/articles/:articleId',
    { config: { authPolicy: 'session' }, preHandler: deps.requireAuth },
    async (request) => {
      const params = parseOrThrow(articleParams, request.params);
      return deps.service.get(params.articleId);
    },
  );
}
```

- [ ] **Step 4: Error code** — in `codes.ts` before `] as const;`:

```ts
  // Articles (spec 2026-08-27, image_flashbacklabs). ARTICLE_NOT_FOUND covers
  // "no such id" and "not published" identically: a draft or archived article
  // is not the client's to tell apart from a nonexistent one.
  'ARTICLE_NOT_FOUND',
```

- [ ] **Step 5: Wire** — in `app.ts` add imports `import { ArticlesService } from '../articles/service.js';` and `import { registerArticleRoutes } from '../articles/routes.js';` beside the other feature imports. Before `return app;`:

```ts
  // Articles (spec 2026-08-27): operator content read through svc.v_articles.
  // No person in the query — every user sees the same feed.
  const articlesService = new ArticlesService({ pool: deps.pool, logger: deps.logger });
  registerArticleRoutes(app, { service: articlesService, requireAuth });
```

- [ ] **Step 6: IDOR registry** — in `tests/helpers/idor.ts`, before the `// ── Deletion (phase 9 Part E)` comment:

```ts
  // ── Articles (spec 2026-08-27) ────────────────────────────────────────────
  'GET /v1/articles': {
    selfScoped: {
      reason:
        'lists operator-published articles — the same feed for every user; reads no person and takes no identifier, and the query schema is strict so ?person_id= is a 400',
    },
  },
  'GET /v1/articles/:articleId': {
    // There is no victim identifier to substitute: an article has no owner.
    // The probe fetches an id that does not exist and the fixture asserts the
    // 404 — which is also what a draft or archived id gets.
    probe: () => ({
      method: 'GET',
      url: '/v1/articles/00000000-0000-4000-8000-000000000000',
    }),
  },
```

- [ ] **Step 7: Verify** — `npm run typecheck`; `npm run lint`; `npm run test:unit` → green. `npm run test:integration -- idor` → green (both new routes classified).

- [ ] **Step 8: Commit**

```
git add src/http/cursor.ts src/articles/ src/errors/codes.ts src/http/app.ts tests/helpers/idor.ts
git commit -m "feat(articles): GET /v1/articles feed and single read over svc.v_articles; empty when the view is absent"
```

---

# Phase 3 — Tests (owner's ordering: written now that the APIs exist)

### Task 9: Services tests — provider ops

**Files:**
- Modify: `tests/providers_fakes.py:70-140` (record actors)
- Modify: `tests/test_admin_providers.py` (append two tests)
- Modify: `tests/test_console.py` (fake + five tests)

- [ ] **Step 1: Record actors in the fake** — in `FakeControlStore.__init__` add `self.actors: list[str] = []`; in `set_enabled` and `reset_breaker`, before `return True`, add `self.actors.append(actor)`.

- [ ] **Step 2: Route tests** — append to `test_admin_providers.py`:

```python
def test_operator_in_the_body_becomes_the_audit_actor() -> None:
    """The console names a person; the audit row should say who, not which
    token was held."""
    client, control = make_client()

    response = client.post(
        "/v1/admin/providers/hive/disable",
        json={"reason": "vendor breach notice", "operator": "alice"},
        headers=ADMIN,
    )

    assert response.status_code == 200
    assert control.actors == ["alice"]
    assert control.enabled_writes == [(HIVE, False, "vendor breach notice")]


def test_an_omitted_operator_still_records_the_token_holder() -> None:
    """curl callers are unchanged: no operator, the fallback actor."""
    client, control = make_client()

    client.post(
        "/v1/admin/providers/hive/breaker/reset",
        json={"reason": "verified by hand"},
        headers=ADMIN,
    )

    assert control.actors == ["admin_service_token"]
```

- [ ] **Step 3: Console fake** — in `test_console.py` `FakeServicesClient.__init__` add a keyword `providers: list[dict[str, Any]] | None = None`, store `self._providers = providers if providers is not None else []`, add `self.provider_writes: list[dict[str, Any]] = []`; replace `provider_health` and add three methods:

```python
    async def provider_health(self) -> dict[str, Any]:
        return {
            "as_of": "2026-08-27T10:00:00+00:00",
            "window_hours": 24,
            "providers": self._providers,
        }

    async def disable_provider(self, provider_id: str, *, reason: str, operator: str) -> None:
        self.provider_writes.append(
            {"action": "disable", "provider_id": provider_id, "reason": reason, "operator": operator}
        )

    async def enable_provider(self, provider_id: str, *, reason: str, operator: str) -> None:
        self.provider_writes.append(
            {"action": "enable", "provider_id": provider_id, "reason": reason, "operator": operator}
        )

    async def reset_breaker(self, provider_id: str, *, reason: str, operator: str) -> None:
        self.provider_writes.append(
            {
                "action": "breaker/reset",
                "provider_id": provider_id,
                "reason": reason,
                "operator": operator,
            }
        )
```

- [ ] **Step 4: Console tests** — append a new section:

```python
# ── provider ops on the dashboard ─────────────────────────────────────────

_QUIET_PROVIDER: dict[str, Any] = {
    "provider_id": "hive",
    "enabled": True,
    "breaker_state": "closed",
    "breaker_reason": None,
    "call_count": 0,
    "cost_usd": "0.00",
    "daily_budget_usd": "10.00",
    "monthly_budget_usd": None,
    "month_to_date_cost_usd": "14.00",
    "budget_headroom_usd": "10.00",
    # None, not 0.0: no calls in the window is a different fact from a 0% rate.
    "success_rate": None,
    "window_call_count": 0,
    "successful_calls_24h": 0,
    "latency_p50_ms": None,
    "latency_p99_ms": None,
    "alarms": [{"kind": "no_successful_calls_24h", "detail": "0 successful calls in 24h"}],
}


def test_dashboard_leads_with_the_alarms_and_renders_a_null_rate_as_a_dash() -> None:
    response = _client(services=FakeServicesClient(providers=[_QUIET_PROVIDER])).get(
        "/", auth=ALICE
    )

    assert response.status_code == 200
    body = response.content.decode()
    assert "no_successful_calls_24h" in body
    assert "—" in body
    # Not a bare "0%": base.html's CSS carries `width: 100%`.
    assert "0%</td>" not in body
    assert "24h window" in body


def test_dashboard_offers_reset_only_while_the_breaker_is_not_closed() -> None:
    closed = _client(services=FakeServicesClient(providers=[_QUIET_PROVIDER])).get(
        "/", auth=ALICE
    )
    assert b"/providers/hive/breaker-reset" not in closed.content
    assert b"/providers/hive/disable" in closed.content

    opened = dict(_QUIET_PROVIDER, breaker_state="open", breaker_reason="timeout")
    response = _client(services=FakeServicesClient(providers=[opened])).get("/", auth=ALICE)
    assert b"/providers/hive/breaker-reset" in response.content


def test_provider_disable_posts_through_with_the_operator_and_redirects_home() -> None:
    fake = FakeServicesClient()
    response = _client(services=fake).post(
        "/providers/hive/disable",
        data={"reason": "returning garbage", "csrf_token": _csrf("alice")},
        auth=ALICE,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert fake.provider_writes == [
        {"action": "disable", "provider_id": "hive", "reason": "returning garbage",
         "operator": "alice"}
    ]


def test_provider_enable_and_breaker_reset_post_through() -> None:
    fake = FakeServicesClient()
    client = _client(services=fake)
    client.post(
        "/providers/hive/enable",
        data={"reason": "vendor confirmed fixed", "csrf_token": _csrf("bob")},
        auth=BOB,
        follow_redirects=False,
    )
    client.post(
        "/providers/hive/breaker-reset",
        data={"reason": "verified by hand", "csrf_token": _csrf("bob")},
        auth=BOB,
        follow_redirects=False,
    )

    assert [w["action"] for w in fake.provider_writes] == ["enable", "breaker/reset"]
    assert {w["operator"] for w in fake.provider_writes} == {"bob"}


def test_provider_disable_without_csrf_is_403_and_writes_nothing() -> None:
    fake = FakeServicesClient()
    response = _client(services=fake).post(
        "/providers/hive/disable",
        data={"reason": "returning garbage"},
        auth=ALICE,
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert fake.provider_writes == []
```

- [ ] **Step 5: Run** — `pytest tests/test_admin_providers.py tests/test_console.py -q` → all pass. Fix anything the templates got wrong (a missing key in `_QUIET_PROVIDER` shows up here as a Jinja `Undefined` arithmetic error).

- [ ] **Step 6: Commit**

```
git add tests/providers_fakes.py tests/test_admin_providers.py tests/test_console.py
git commit -m "test: provider ops — operator becomes the audit actor; dashboard alarms, dashes and forms"
```

---

### Task 10: Services tests — articles routes and console pages

**Files:**
- Create: `tests/test_admin_articles_routes.py`
- Modify: `tests/test_console.py` (fake article methods + six tests)

- [ ] **Step 1: Route tests over a fake store**

```python
"""Admin article routes — behaviour over an in-memory fake store.

Repo convention: TestClient never runs the lifespan; the store is pre-wired on
``app.state``. The auth assertion is load-bearing — an article reaches every
user's feed, so both tokens are required at router level.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from imageshield.http.app import create_app
from tests.conftest import ADMIN_SERVICE_TOKEN, SERVICE_TOKEN, make_config

AUTH = {"X-Service-Token": SERVICE_TOKEN}
ADMIN = {**AUTH, "X-Admin-Service-Token": ADMIN_SERVICE_TOKEN}

_NOW = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)

_BODY: dict[str, Any] = {
    "title": "Older photos of you circulate too",
    "summary": "Why a photo from five years ago still matters.",
    "body": "Image search can only find copies of what we hold.",
    "images": [{"url": "https://cdn.example/hero.jpg", "alt": "A photo album"}],
    "sources": [{"name": "Example News", "url": "https://news.example/story"}],
    "operator": "alice",
}


class FakeArticleStore:
    def __init__(self) -> None:
        self.rows: dict[UUID, dict[str, Any]] = {}
        self.audit: list[tuple[str, str]] = []  # (action, operator)

    async def create_article(self, **kwargs: Any) -> UUID:
        article_id = uuid4()
        operator = kwargs.pop("operator")
        self.rows[article_id] = {
            "article_id": article_id,
            **kwargs,
            "status": "draft",
            "published_at": None,
            "created_by": operator,
            "updated_by": operator,
            "created_at": _NOW,
            "updated_at": _NOW,
        }
        self.audit.append(("article.created", operator))
        return article_id

    async def update_article(self, article_id: UUID, **kwargs: Any) -> bool:
        row = self.rows.get(article_id)
        if row is None:
            return False
        operator = kwargs.pop("operator")
        row.update(kwargs, updated_by=operator)
        self.audit.append(("article.updated", operator))
        return True

    async def publish_article(self, article_id: UUID, *, operator: str) -> str | None:
        row = self.rows.get(article_id)
        if row is None:
            return None
        if row["status"] != "published":
            row["status"] = "published"
            row["published_at"] = row["published_at"] or _NOW
            self.audit.append(("article.published", operator))
        return str(row["status"])

    async def archive_article(
        self, article_id: UUID, *, operator: str, reason: str
    ) -> str | None:
        row = self.rows.get(article_id)
        if row is None:
            return None
        if row["status"] != "archived":
            row["status"] = "archived"
            self.audit.append(("article.archived", operator))
        return str(row["status"])

    async def get_article(self, article_id: UUID) -> dict[str, Any] | None:
        return self.rows.get(article_id)

    async def list_articles(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return list(self.rows.values())[:limit]


def make_client() -> tuple[TestClient, FakeArticleStore]:
    app = create_app(config=make_config())
    store = FakeArticleStore()
    app.state.article_store = store
    return TestClient(app), store


def test_every_article_route_needs_both_tokens() -> None:
    client, store = make_client()
    some_id = uuid4()
    calls = [
        ("GET", "/v1/admin/articles", None),
        ("POST", "/v1/admin/articles", _BODY),
        ("GET", f"/v1/admin/articles/{some_id}", None),
        ("PUT", f"/v1/admin/articles/{some_id}", _BODY),
        ("POST", f"/v1/admin/articles/{some_id}/publish", {"operator": "alice"}),
        ("POST", f"/v1/admin/articles/{some_id}/archive", {"operator": "alice", "reason": "old"}),
    ]
    for method, path, body in calls:
        assert client.request(method, path, json=body).status_code == 401, path
        assert client.request(method, path, json=body, headers=AUTH).status_code == 401, path
    assert store.rows == {}


def test_create_returns_201_and_a_draft_named_after_the_operator() -> None:
    client, store = make_client()

    response = client.post("/v1/admin/articles", json=_BODY, headers=ADMIN)

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "draft"
    row = store.rows[UUID(payload["article_id"])]
    assert row["created_by"] == "alice"
    assert row["images"] == _BODY["images"]
    assert store.audit == [("article.created", "alice")]


def test_an_http_picture_url_is_a_422_and_writes_nothing() -> None:
    client, store = make_client()
    body = dict(_BODY, images=[{"url": "http://cdn.example/hero.jpg", "alt": ""}])

    assert client.post("/v1/admin/articles", json=body, headers=ADMIN).status_code == 422
    body = dict(_BODY, sources=[{"name": "x", "url": "ftp://news.example/story"}])
    assert client.post("/v1/admin/articles", json=body, headers=ADMIN).status_code == 422
    assert store.rows == {}


def test_publish_archive_republish_and_the_no_op() -> None:
    client, store = make_client()
    article_id = client.post("/v1/admin/articles", json=_BODY, headers=ADMIN).json()["article_id"]

    published = client.post(
        f"/v1/admin/articles/{article_id}/publish", json={"operator": "bob"}, headers=ADMIN
    )
    assert published.status_code == 200
    assert published.json()["status"] == "published"
    first_published_at = store.rows[UUID(article_id)]["published_at"]

    # Publishing again changes nothing and audits nothing.
    again = client.post(
        f"/v1/admin/articles/{article_id}/publish", json={"operator": "bob"}, headers=ADMIN
    )
    assert again.json()["status"] == "published"

    archived = client.post(
        f"/v1/admin/articles/{article_id}/archive",
        json={"operator": "carol", "reason": "superseded"},
        headers=ADMIN,
    )
    assert archived.json()["status"] == "archived"

    client.post(
        f"/v1/admin/articles/{article_id}/publish", json={"operator": "dave"}, headers=ADMIN
    )
    assert store.rows[UUID(article_id)]["published_at"] == first_published_at
    assert store.audit == [
        ("article.created", "alice"),
        ("article.published", "bob"),
        ("article.archived", "carol"),
        ("article.published", "dave"),
    ]


def test_edit_updates_content_and_names_the_editor() -> None:
    client, store = make_client()
    article_id = client.post("/v1/admin/articles", json=_BODY, headers=ADMIN).json()["article_id"]

    response = client.put(
        f"/v1/admin/articles/{article_id}",
        json=dict(_BODY, title="A better title", operator="erin"),
        headers=ADMIN,
    )

    assert response.status_code == 200
    assert response.json()["title"] == "A better title"
    assert response.json()["updated_by"] == "erin"
    assert response.json()["created_by"] == "alice"


def test_unknown_id_is_404_article_not_found_on_every_route() -> None:
    client, _ = make_client()
    missing = uuid4()
    responses = [
        client.get(f"/v1/admin/articles/{missing}", headers=ADMIN),
        client.put(f"/v1/admin/articles/{missing}", json=_BODY, headers=ADMIN),
        client.post(
            f"/v1/admin/articles/{missing}/publish", json={"operator": "a"}, headers=ADMIN
        ),
        client.post(
            f"/v1/admin/articles/{missing}/archive",
            json={"operator": "a", "reason": "gone"},
            headers=ADMIN,
        ),
    ]
    for response in responses:
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "article_not_found"


def test_the_list_carries_every_status() -> None:
    client, _ = make_client()
    draft = client.post("/v1/admin/articles", json=_BODY, headers=ADMIN).json()["article_id"]
    live = client.post("/v1/admin/articles", json=_BODY, headers=ADMIN).json()["article_id"]
    client.post(f"/v1/admin/articles/{live}/publish", json={"operator": "a"}, headers=ADMIN)

    listed = client.get("/v1/admin/articles", headers=ADMIN).json()["articles"]

    assert {a["article_id"]: a["status"] for a in listed} == {draft: "draft", live: "published"}


def test_articles_never_mention_a_user_ref() -> None:
    """Spec §3: articles are not identity data. Enforced as a grep, like the
    other boundary gates in tests/test_boundaries.py."""
    root = Path(__file__).resolve().parents[1] / "src" / "imageshield"
    for path in (
        root / "articles" / "store.py",
        root / "http" / "routes" / "admin_articles.py",
    ):
        assert "user_ref" not in path.read_text(encoding="utf-8"), path
```

- [ ] **Step 2: Console fake + tests** — in `FakeServicesClient.__init__` add `self.articles: dict[UUID, dict[str, Any]] = {}` and `self.article_calls: list[dict[str, Any]] = []`; add methods:

```python
    async def list_articles(self) -> list[dict[str, Any]]:
        return list(self.articles.values())

    async def get_article(self, article_id: UUID) -> dict[str, Any] | None:
        return self.articles.get(article_id)

    async def create_article(self, payload: dict[str, Any]) -> dict[str, Any]:
        article_id = uuid4()
        self.articles[article_id] = {
            "article_id": str(article_id),
            **payload,
            "status": "draft",
            "published_at": None,
            "created_by": payload["operator"],
            "updated_by": payload["operator"],
            "created_at": "2026-08-27T10:00:00+00:00",
            "updated_at": "2026-08-27T10:00:00+00:00",
        }
        self.article_calls.append({"action": "create", **payload})
        return {"article_id": str(article_id), "status": "draft"}

    async def update_article(self, article_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        self.article_calls.append({"action": "update", "article_id": article_id, **payload})
        return self.articles.get(article_id, {})

    async def publish_article(self, article_id: UUID, *, operator: str) -> None:
        self.article_calls.append(
            {"action": "publish", "article_id": article_id, "operator": operator}
        )

    async def archive_article(self, article_id: UUID, *, operator: str, reason: str) -> None:
        self.article_calls.append(
            {"action": "archive", "article_id": article_id, "operator": operator, "reason": reason}
        )
```

Append tests:

```python
# ── articles ──────────────────────────────────────────────────────────────


def test_articles_list_and_new_form_render() -> None:
    client = _client(services=FakeServicesClient())
    assert client.get("/articles", auth=ALICE).status_code == 200
    new = client.get("/articles/new", auth=ALICE)
    assert new.status_code == 200
    assert b'name="images"' in new.content


def test_articles_create_parses_the_line_encoded_pictures_and_sources() -> None:
    fake = FakeServicesClient()
    response = _client(services=fake).post(
        "/articles",
        data={
            "title": "Older photos of you circulate too",
            "summary": "blurb",
            "body": "text",
            "images": "https://cdn.example/a.jpg | album\n\nhttps://cdn.example/b.jpg",
            "sources": "Example News | https://news.example/story",
            "csrf_token": _csrf("alice"),
        },
        auth=ALICE,
        follow_redirects=False,
    )

    assert response.status_code == 303
    created = fake.article_calls[0]
    assert response.headers["location"] == f"/articles/{next(iter(fake.articles))}"
    assert created["operator"] == "alice"
    assert created["images"] == [
        {"url": "https://cdn.example/a.jpg", "alt": "album"},
        {"url": "https://cdn.example/b.jpg", "alt": ""},
    ]
    assert created["sources"] == [{"name": "Example News", "url": "https://news.example/story"}]


def test_article_edit_page_links_pictures_and_never_renders_an_img() -> None:
    fake = FakeServicesClient()
    client = _client(services=fake)
    client.post(
        "/articles",
        data={
            "title": "T",
            "images": "https://cdn.example/a.jpg | album",
            "sources": "",
            "csrf_token": _csrf("alice"),
        },
        auth=ALICE,
        follow_redirects=False,
    )
    article_id = next(iter(fake.articles))

    response = client.get(f"/articles/{article_id}", auth=ALICE)

    assert response.status_code == 200
    assert b'href="https://cdn.example/a.jpg"' in response.content
    assert b"<img" not in response.content
    # Prefilled textarea round-trips the line encoding.
    assert b"https://cdn.example/a.jpg | album" in response.content


def test_article_publish_and_archive_post_through_with_the_operator() -> None:
    fake = FakeServicesClient()
    client = _client(services=fake)
    client.post(
        "/articles", data={"title": "T", "csrf_token": _csrf("alice")}, auth=ALICE,
        follow_redirects=False,
    )
    article_id = next(iter(fake.articles))

    published = client.post(
        f"/articles/{article_id}/publish", data={"csrf_token": _csrf("bob")}, auth=BOB,
        follow_redirects=False,
    )
    archived = client.post(
        f"/articles/{article_id}/archive",
        data={"reason": "superseded", "csrf_token": _csrf("bob")},
        auth=BOB,
        follow_redirects=False,
    )

    assert published.status_code == 303 and archived.status_code == 303
    assert published.headers["location"] == f"/articles/{article_id}"
    assert [c["action"] for c in fake.article_calls] == ["create", "publish", "archive"]
    assert fake.article_calls[1] == {"action": "publish", "article_id": article_id, "operator": "bob"}
    assert fake.article_calls[2]["reason"] == "superseded"


def test_article_writes_without_csrf_are_403_and_reach_nothing() -> None:
    fake = FakeServicesClient()
    client = _client(services=fake)
    assert client.post("/articles", data={"title": "T"}, auth=ALICE).status_code == 403
    missing = uuid4()
    assert client.post(f"/articles/{missing}/publish", data={}, auth=ALICE).status_code == 403
    assert fake.article_calls == []


def test_unknown_article_is_a_404_envelope() -> None:
    response = _client(services=FakeServicesClient()).get(f"/articles/{uuid4()}", auth=ALICE)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "article_not_found"


def test_articles_pages_require_credentials() -> None:
    client = _client()
    assert client.get("/articles").status_code == 401
    assert client.get("/articles/new").status_code == 401
```

- [ ] **Step 3: Run** — `pytest tests/test_admin_articles_routes.py tests/test_console.py -q` → all pass. `ruff check tests`; `mypy` (tests are type-checked only if `mypy` is configured over them — `packages = ["imageshield"]` says no; run `ruff` regardless).

- [ ] **Step 4: Commit**

```
git add tests/test_admin_articles_routes.py tests/test_console.py
git commit -m "test(articles): admin routes over a fake store; console pages, line encoding, no <img>"
```

---

### Task 11: Services tests — store, view, migration, readiness (real Postgres)

**Files:**
- Create: `tests/test_articles_store.py`
- Modify: `tests/test_migrations.py` (append one test), `tests/test_readyz.py:70-81`, `tests/test_svc_views.py` (`VIEWS`, `FROZEN_CONTRACT_COLUMNS`, the forbidden-table list in `test_the_proxy_role_reads_the_views_and_nothing_else`)

Requires Docker: `docker compose -f docker-compose.local.yml up -d`, then run with `REQUIRE_DB=1`.

- [ ] **Step 1: Store + view tests**

```python
"""``PostgresArticleStore`` and ``svc.v_articles`` against real Postgres.

What is under test is the transaction shape (one audit row per write, none
for a no-op), the status CHECKs the database enforces, and the projection the
proxy reads — published rows only, readable by ``imageshield_proxy_ro`` and
by nobody through the base table.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from imageshield.articles.store import (
    ARTICLE_ARCHIVED_ACTION,
    ARTICLE_CREATED_ACTION,
    ARTICLE_PUBLISHED_ACTION,
    ARTICLE_UPDATED_ACTION,
    PostgresArticleStore,
)
from imageshield.db.connection import make_async_pool
from tests.db import run_migrate


@pytest.fixture
def migrated_db(throwaway_db: str) -> str:
    down_result = run_migrate(throwaway_db, "down", "--all")
    assert down_result.returncode == 0, down_result.stderr
    up_result = run_migrate(throwaway_db, "up")
    assert up_result.returncode == 0, up_result.stderr
    return throwaway_db


@pytest.fixture
async def pool(migrated_db: str) -> AsyncIterator[AsyncConnectionPool]:
    p = make_async_pool(migrated_db, min_size=1, max_size=2)
    await p.open()
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture
def store(pool: AsyncConnectionPool) -> PostgresArticleStore:
    return PostgresArticleStore(pool)


def _rows(db_url: str, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(db_url, autocommit=True) as conn:
        return list(conn.execute(sql, params).fetchall())


_IMAGES = [{"url": "https://cdn.example/a.jpg", "alt": "album"}]
_SOURCES = [{"name": "Example News", "url": "https://news.example/story"}]


async def _create(store: PostgresArticleStore, *, operator: str = "alice") -> UUID:
    return await store.create_article(
        title="Older photos of you circulate too",
        summary="blurb",
        body="text",
        images=_IMAGES,
        sources=_SOURCES,
        operator=operator,
    )


async def test_every_write_lands_exactly_one_audit_row_naming_the_operator(
    store: PostgresArticleStore, migrated_db: str
) -> None:
    article_id = await _create(store, operator="alice")
    assert await store.update_article(
        article_id, title="T2", summary="", body="", images=[], sources=[], operator="bob"
    )
    assert await store.publish_article(article_id, operator="carol") == "published"
    assert await store.publish_article(article_id, operator="carol") == "published"  # no-op
    assert await store.archive_article(article_id, operator="dave", reason="superseded") == (
        "archived"
    )

    audit = _rows(
        migrated_db,
        "SELECT action, metadata->>'operator', actor_type FROM audit_log"
        " WHERE resource_id = %s ORDER BY audit_id",
        (article_id,),
    )
    assert audit == [
        (ARTICLE_CREATED_ACTION, "alice", "operator"),
        (ARTICLE_UPDATED_ACTION, "bob", "operator"),
        (ARTICLE_PUBLISHED_ACTION, "carol", "operator"),
        (ARTICLE_ARCHIVED_ACTION, "dave", "operator"),
    ]
    assert _rows(migrated_db, "SELECT metadata->>'reason' FROM audit_log WHERE action = %s",
                 (ARTICLE_ARCHIVED_ACTION,)) == [("superseded",)]


async def test_the_view_shows_published_rows_only(
    store: PostgresArticleStore, migrated_db: str
) -> None:
    article_id = await _create(store)
    assert _rows(migrated_db, "SELECT count(*) FROM svc.v_articles") == [(0,)]

    await store.publish_article(article_id, operator="alice")
    rows = _rows(
        migrated_db,
        "SELECT article_id, title, images, sources FROM svc.v_articles",
    )
    assert rows == [(article_id, "Older photos of you circulate too", _IMAGES, _SOURCES)]

    await store.archive_article(article_id, operator="alice", reason="superseded")
    assert _rows(migrated_db, "SELECT count(*) FROM svc.v_articles") == [(0,)]


async def test_a_republish_keeps_the_original_published_at(
    store: PostgresArticleStore, migrated_db: str
) -> None:
    article_id = await _create(store)
    await store.publish_article(article_id, operator="alice")
    (first,) = _rows(migrated_db, "SELECT published_at FROM articles WHERE article_id = %s",
                     (article_id,))[0]
    await store.archive_article(article_id, operator="alice", reason="pause")
    assert await store.publish_article(article_id, operator="alice") == "published"
    (again,) = _rows(migrated_db, "SELECT published_at FROM articles WHERE article_id = %s",
                     (article_id,))[0]
    assert again == first


async def test_unknown_ids_return_none_and_write_no_audit_row(
    store: PostgresArticleStore, migrated_db: str
) -> None:
    missing = uuid4()
    assert await store.publish_article(missing, operator="a") is None
    assert await store.archive_article(missing, operator="a", reason="gone") is None
    assert await store.update_article(
        missing, title="T", summary="", body="", images=[], sources=[], operator="a"
    ) is False
    assert await store.get_article(missing) is None
    assert _rows(migrated_db, "SELECT count(*) FROM audit_log WHERE resource_id = %s",
                 (missing,)) == [(0,)]


def test_the_proxy_role_reads_the_view_and_not_the_table(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        conn.execute("SET ROLE imageshield_proxy_ro")
        conn.execute("SELECT * FROM svc.v_articles")  # must not raise
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM public.articles")
        conn.execute("RESET ROLE")


def test_the_database_refuses_a_published_row_with_no_date_and_a_dated_draft(
    migrated_db: str,
) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO articles (title, status, created_by, updated_by)"
                " VALUES ('t', 'published', 'a', 'a')"
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO articles (title, status, published_at, created_by, updated_by)"
                " VALUES ('t', 'draft', now(), 'a', 'a')"
            )


def test_the_database_refuses_non_array_pictures(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, autocommit=True) as conn, pytest.raises(
        psycopg.errors.CheckViolation
    ):
        conn.execute(
            "INSERT INTO articles (title, images, created_by, updated_by)"
            " VALUES ('t', '{\"url\": \"https://x.example/a.jpg\"}'::jsonb, 'a', 'a')"
        )
```

- [ ] **Step 2: Migration round-trip** — append to `tests/test_migrations.py`:

```python
def test_0026_creates_articles_and_the_view_and_down_removes_them(throwaway_db: str) -> None:
    assert run_migrate(throwaway_db, "down", "--all").returncode == 0
    assert run_migrate(throwaway_db, "up").returncode == 0
    with psycopg.connect(throwaway_db) as conn:
        assert "articles" in _table_names(conn)
        assert conn.execute(
            "SELECT 1 FROM pg_views WHERE schemaname = 'svc' AND viewname = 'v_articles'"
        ).fetchone() is not None
        assert conn.execute(
            "SELECT has_table_privilege('imageshield_proxy_ro', 'svc.v_articles', 'SELECT')"
        ).fetchone() == (True,)

    assert run_migrate(throwaway_db, "down", "--steps", "1").returncode == 0
    with psycopg.connect(throwaway_db) as conn:
        assert "articles" not in _table_names(conn)
        assert conn.execute(
            "SELECT 1 FROM pg_views WHERE schemaname = 'svc' AND viewname = 'v_articles'"
        ).fetchone() is None

    assert run_migrate(throwaway_db, "up").returncode == 0
```

- [ ] **Step 3: Readiness + contract tests** — already updated in Task 3 (`tests/test_readyz.py` names `v_articles`; `FROZEN_CONTRACT_COLUMNS` carries it). Confirm both are present; nothing to change here.

- [ ] **Step 4: Run** — `REQUIRE_DB=1 pytest tests/test_articles_store.py tests/test_migrations.py tests/test_readyz.py tests/test_svc_views.py tests/test_schema_lint.py -q` → all pass. Then the whole suite: `REQUIRE_DB=1 pytest -q` → green.

- [ ] **Step 5: Commit**

```
git add tests/test_articles_store.py tests/test_migrations.py
git commit -m "test(articles): store transactions, the published-only view, proxy grant, 0026 round-trip"
```

---

### Task 12: Proxy tests

**Working dir:** `image_backend`.

**Files:**
- Create: `tests/unit/cursor.test.ts`, `tests/integration/articles.test.ts`

- [ ] **Step 1: Cursor unit test**

```ts
import { describe, expect, it } from 'vitest';
import { decodeCursor, encodeCursor } from '../../src/http/cursor.js';

describe('keyset cursor', () => {
  const id = '7f6c1a2e-3b4d-4c5e-9f80-112233445566';

  it('round-trips a timestamp and an id', () => {
    const at = new Date('2026-08-27T10:00:00.000Z');
    expect(decodeCursor(encodeCursor(at, id))).toEqual({ at: '2026-08-27T10:00:00.000Z', id });
  });

  it('null in, null out', () => {
    expect(decodeCursor(null)).toBeNull();
  });

  it('rejects a tampered cursor as a 400, never as a database error', () => {
    expect(() => decodeCursor(Buffer.from('hello|world').toString('base64url'))).toThrowError(
      /malformed cursor/,
    );
    expect(() => decodeCursor(Buffer.from(`not-a-date|${id}`).toString('base64url'))).toThrowError(
      /malformed cursor/,
    );
    expect(() => decodeCursor('%%%')).toThrowError(/malformed cursor/);
  });
});
```

- [ ] **Step 2: Integration test**

```ts
import { randomUUID } from 'node:crypto';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { startMigratedDatabase, type MigratedDatabase } from '../helpers/db.js';
import { buildTestApp, loginAs, nextPhone, type LoginContext, type TestApp } from '../helpers/auth-app.js';

/**
 * GET /v1/articles and GET /v1/articles/:articleId — the operator-published
 * feed read through svc.v_articles (their 0026; the fixture's stub here).
 * Every user sees the same list, so there is no per-person case to test —
 * the IDOR fixture covers the two routes' classification.
 */
describe('articles feed', () => {
  let db: MigratedDatabase;
  let test: TestApp;
  let alice: LoginContext;

  beforeAll(async () => {
    db = await startMigratedDatabase();
    test = buildTestApp(db);
    alice = await loginAs(test, nextPhone());
  }, 180_000);

  afterAll(async () => {
    await test.app.close();
    await db.stop();
  });

  const get = (url: string) =>
    test.app.inject({ method: 'GET', url, headers: { authorization: `Bearer ${alice.accessToken}` } });

  interface Page {
    items: { article_id: string; title: string; images: { url: string; alt: string }[] }[];
    next_cursor: string | null;
  }

  async function publish(title: string, publishedAt: string): Promise<string> {
    const id = randomUUID();
    await db.pool.query(
      `INSERT INTO svc._stub_articles (article_id, title, summary, body, images, sources, published_at)
       VALUES ($1, $2, 'blurb', 'body', $3::jsonb, $4::jsonb, $5)`,
      [
        id,
        title,
        JSON.stringify([{ url: 'https://cdn.example/a.jpg', alt: 'album' }]),
        JSON.stringify([{ name: 'Example News', url: 'https://news.example/story' }]),
        publishedAt,
      ],
    );
    return id;
  }

  it('requires a session', async () => {
    expect((await test.app.inject({ method: 'GET', url: '/v1/articles' })).statusCode).toBe(401);
  });

  it('P24: an empty feed is 200 with no items, never a 404', async () => {
    const response = await get('/v1/articles');
    expect(response.statusCode).toBe(200);
    expect(response.json<Page>()).toEqual({ items: [], next_cursor: null });
  });

  it('lists newest first and the cursor walks the whole feed without gaps or repeats', async () => {
    const ids = new Set<string>();
    for (let i = 0; i < 5; i += 1) {
      ids.add(await publish(`A${i}`, `2026-08-1${i}T00:00:00Z`));
    }

    const first = await get('/v1/articles?limit=2');
    expect(first.statusCode).toBe(200);
    const page1 = first.json<Page>();
    expect(page1.items.map((a) => a.title)).toEqual(['A4', 'A3']);
    expect(page1.items[0]?.images).toEqual([{ url: 'https://cdn.example/a.jpg', alt: 'album' }]);
    expect(page1.next_cursor).not.toBeNull();

    const page2 = (
      await get(`/v1/articles?limit=2&cursor=${encodeURIComponent(page1.next_cursor ?? '')}`)
    ).json<Page>();
    expect(page2.items.map((a) => a.title)).toEqual(['A2', 'A1']);
    expect(page2.next_cursor).not.toBeNull();

    const page3 = (
      await get(`/v1/articles?limit=2&cursor=${encodeURIComponent(page2.next_cursor ?? '')}`)
    ).json<Page>();
    expect(page3.items.map((a) => a.title)).toEqual(['A0']);
    expect(page3.next_cursor).toBeNull();

    const seen = [...page1.items, ...page2.items, ...page3.items].map((a) => a.article_id);
    expect(new Set(seen).size).toBe(5);
    expect(new Set(seen)).toEqual(ids);
  });

  it('an unknown query parameter is a 400 — the schema is strict', async () => {
    expect((await get('/v1/articles?person_id=abc')).statusCode).toBe(400);
  });

  it('a tampered cursor is a 400', async () => {
    const response = await get(`/v1/articles?cursor=${Buffer.from('hello|world').toString('base64url')}`);
    expect(response.statusCode).toBe(400);
    expect(response.json<{ error: string }>().error).toBe('VALIDATION_FAILED');
  });

  it('GET /v1/articles/:id returns a published article, and ARTICLE_NOT_FOUND for an unknown one', async () => {
    const id = await publish('Single', '2026-08-20T00:00:00Z');
    const found = await get(`/v1/articles/${id}`);
    expect(found.statusCode).toBe(200);
    expect(found.json<{ title: string; sources: unknown[] }>().title).toBe('Single');

    const missing = await get(`/v1/articles/${randomUUID()}`);
    expect(missing.statusCode).toBe(404);
    expect(missing.json<{ error: string }>().error).toBe('ARTICLE_NOT_FOUND');

    expect((await get('/v1/articles/not-a-uuid')).statusCode).toBe(400);
  });

  it('degrades to an empty feed while the view is absent, and recovers', async () => {
    await db.pool.query('DROP VIEW svc.v_articles');
    try {
      const response = await get('/v1/articles');
      expect(response.statusCode).toBe(200);
      expect(response.json<Page>().items).toEqual([]);
      expect((await get(`/v1/articles/${randomUUID()}`)).statusCode).toBe(404);
    } finally {
      await db.pool.query(`
        CREATE VIEW svc.v_articles AS
        SELECT article_id, title, summary, body, images, sources, published_at, updated_at
        FROM svc._stub_articles`);
      await db.pool.query('GRANT SELECT ON svc.v_articles TO imageshield_proxy_ro');
    }
    expect((await get('/v1/articles')).json<Page>().items.length).toBeGreaterThan(0);
  });
});
```

- [ ] **Step 3: Run** — `npm run test:unit` → green; `npm run test:integration -- articles idor contract-readiness` → green; then the full `npm run test:integration`.

- [ ] **Step 4: Commit**

```
git add tests/unit/cursor.test.ts tests/integration/articles.test.ts
git commit -m "test(articles): feed ordering and cursor walk, strict query, 404 code, empty when the view is absent"
```

---

# Phase 4 — Docs and finish

### Task 13: Services docs

**Files:** `SCHEMA.md`, `PROXY_INTEGRATION.md`, `CLAUDE.md`, `docs/ADMIN_PANEL_INTEGRATION.md`, `docs/OPERATIONS.md`, `docs/superpowers/specs/2026-08-20-recommendation-campaigns-design.md`.

- [ ] **Step 1: `SCHEMA.md`** — before `## 3. Adjudication service` insert:

```markdown
## 2e. Articles — **built** (migration 0026)

Operator-authored content for the app's feed (`docs/superpowers/specs/2026-08-27-articles-design.md`).
Not identity data: no `user_ref` in the table, the view or the module.

```sql
CREATE TABLE articles (
  article_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  title        TEXT NOT NULL CHECK (title <> ''),
  summary      TEXT NOT NULL DEFAULT '',
  body         TEXT NOT NULL DEFAULT '',
  images       JSONB NOT NULL DEFAULT '[]',   -- [{"url","alt"}], https URLs only, never bytes
  sources      JSONB NOT NULL DEFAULT '[]',   -- [{"name","url"}]
  status       TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','published','archived')),
  published_at TIMESTAMPTZ,
  created_by / updated_by TEXT NOT NULL,      -- operator names
  created_at / updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (status <> 'draft' OR published_at IS NULL),
  CHECK (status <> 'published' OR published_at IS NOT NULL)
);
CREATE VIEW svc.v_articles AS SELECT … FROM articles WHERE status = 'published';
```

`content_rw` owns the table's SELECT/INSERT/UPDATE (no DELETE — archive is the soft delete) and is
granted to `app_services`. `svc.v_articles` is the **ninth contract view**, granted to
`imageshield_proxy_ro`, same rules as §2c. The one writer is `articles/store.py`; every write lands
one `audit_log` row (`article.created|updated|published|archived`) naming the operator. A re-publish
keeps the original `published_at`.
```

- [ ] **Step 2: `PROXY_INTEGRATION.md` §6** — change the heading `### The eight `svc` views` to `### The nine `svc` views`; add a table row `| `svc.v_articles` *(0026)* | `article_id`, `title`, `summary`, `body`, `images`, `sources`, `published_at`, `updated_at` — published rows only; operator content for every user, no person column |`; in the grant code block add `-- 0026, same role, same idiom:` / `GRANT SELECT ON svc.v_articles TO imageshield_proxy_ro;`; change `**`SELECT` on the eight views. Nothing else.**` to nine. Add one paragraph after the table: `**`v_articles` is optional on the proxy side by agreement:** their `GET /v1/articles` serves an empty feed with a warn log while the view is absent, so a database migrated one release behind does not hold their api off.`

- [ ] **Step 3: `CLAUDE.md`** — in §3 change the sentence listing four views so it reads `… `SELECT` on `v_person_enrolment_state`, `v_person_report_summary`, `v_person_hits`, `v_person_liveness_attempts` (0016), the four 0023 score/threat views, and `v_articles` (0026).`; in the §6 scope table add a "Build now" row: `| **Articles** — `articles/`, `/v1/admin/articles`, the console's Articles pages, `svc.v_articles` (0026). Operator content for the app feed; no targeting, no score effect, no LLM (spec 2026-08-27, supersedes the 2026-08-20 campaigns spec) | |`.

- [ ] **Step 4: `docs/ADMIN_PANEL_INTEGRATION.md`** — replace rule 5 with: `5. **Pixels are never shown to staff.** Since 2026-08-21 the subject decides their own hits and staff never see hit imagery, blurred or otherwise; the fetcher's crop route is not part of this contract and the review card is metadata-only. Article pictures (below) are operator-pasted URLs rendered as links, not images.` In §6 change the two body descriptions to `body {"reason": "…", "operator": "…"}` and add `` `operator` is optional; omitted, the audit row records `admin_service_token` instead of a name. The shipped console always sends it.`` Insert after §6:

```markdown
## 6b. Articles — operator content for the app feed

Every user sees every published article; nothing here is per-person. Both tokens; `operator` in
every write body.

- `GET /v1/admin/articles?limit=` → `{articles: [...]}` all statuses, newest edited first.
- `POST /v1/admin/articles` → `201 {article_id, status: "draft"}`. Body: `title` (1–200), `summary`
  (≤500), `body` (≤20000, markdown), `images: [{url, alt}]` (≤10), `sources: [{name, url}]` (≤10),
  `operator`. **Every URL must be `https://`** — `http://` is a 422.
- `GET /v1/admin/articles/{id}`, `PUT /v1/admin/articles/{id}` (same body as create; editing a
  published article changes it live).
- `POST /v1/admin/articles/{id}/publish` body `{operator}` → `{article_id, status}`. Draft or
  archived → published; already published → no-op. A re-publish keeps the original `published_at`.
- `POST /v1/admin/articles/{id}/archive` body `{operator, reason}` → removes it from the feed.
- Unknown id → `404 article_not_found`.

Rendering pictures in an ops panel is your call, but the shipped console deliberately shows them
as links only so "no imagery in the control room" stays a rule without carve-outs.
```

- [ ] **Step 5: `docs/OPERATIONS.md`** — in §2, before the curl block, add: `**From the console (preferred):** Dashboard → the provider's row → *Disable* with a reason. The audit row names you. The curl below is the break-glass path for when the console itself is down:`. In §3, before the breaker-reset curl: `**From the console:** Dashboard → *Reset breaker* (shown only while the breaker is not closed). Break-glass:`. In §4 add one line after the curl: `The console's dashboard renders the same payload: every firing alarm first, then spend, headroom and latency per provider.`

- [ ] **Step 6: Campaigns spec banner** — change its `**Status:**` line to `**Status:** SUPERSEDED 2026-08-27 by `2026-08-27-articles-design.md`. Not built and not planned: campaigns, audience targeting and the LLM ideas panel were replaced by operator-authored articles published to every user.`

- [ ] **Step 7: Commit**

```
git add SCHEMA.md PROXY_INTEGRATION.md CLAUDE.md docs/ADMIN_PANEL_INTEGRATION.md docs/OPERATIONS.md docs/superpowers/specs/2026-08-20-recommendation-campaigns-design.md
git commit -m "docs: articles (0026, ninth view, admin + console), operator on provider writes, console-first kill switch"
```

---

### Task 14: Proxy docs

**Working dir:** `image_backend`. **Files:** `docs/CLAUDE.md`, `fixtures/svc/README.md`.

- [ ] **Step 1:** In `docs/CLAUDE.md` §6, change `granted \`SELECT\` on exactly eight views` to `nine views` and add a table row after `svc.v_person_threat_context`: `| \`svc.v_articles\` | the in-app articles feed, \`GET /v1/articles\` (\`src/articles/\`); OPTIONAL for readiness — absent, the feed is empty with a warn log |`. Add one sentence to the paragraph about optional views: `\`v_articles\` is the second optional view, for the same reason: their 0026 lagging a release must not hold the api off, and an empty feed is a normal state (P24).`

- [ ] **Step 2:** In `fixtures/svc/README.md` change `exactly four views` to `the contract views below` and add a bullet `- \`svc.v_articles\` (their 0026 — the articles feed; stub table \`svc._stub_articles\`)`.

- [ ] **Step 3: Commit**

```
git add docs/CLAUDE.md fixtures/svc/README.md
git commit -m "docs: svc.v_articles — ninth contract view, optional for readiness"
```

---

### Task 15: Full verification and gap closure

- [ ] **Step 1: Services** — Docker up; `ruff check .`; `ruff format` on NEW files only; `mypy`; `REQUIRE_DB=1 pytest -q`. Every failure is a gap: fix it in the file that owns the behaviour (not by loosening the test), re-run, and commit with a `fix:` message naming the gap.
- [ ] **Step 2: Proxy** — `npm run typecheck`; `npm run lint`; `npm run test:unit`; `npm run test:integration`. Same rule.
- [ ] **Step 3: Boundary re-check** — services: `grep -rn "user_ref" src/imageshield/articles src/imageshield/http/routes/admin_articles.py` → no output; `grep -rn "<img" src/imageshield/console/templates` → no output. Proxy: `grep -rn "svc\.v_" src --include=*.ts | grep -v "src/services/contract/"` → no output.
- [ ] **Step 4: Manual smoke (services, local compose)** — run the API and console locally per `docs/DEPLOY-DEV.md`/`devtools/`; in the console create an article with one `https://` picture and one source, publish it, confirm `SELECT * FROM svc.v_articles` shows it, archive it, confirm it disappears; on the dashboard disable and re-enable the `stub` provider and confirm two `audit_log` rows carry your operator name.
- [ ] **Step 5: Finish** — invoke `superpowers:finishing-a-development-branch` for each repo separately. Do NOT merge either branch yourself; present the options. The owner has already decided the targets (2026-08-27): services → `main`; proxy → `main`, then forward-merge `main` → `release/sep-1` (never cherry-pick). Expect one textual conflict on that forward-merge: `tests/repo-lint/contract-confinement.test.ts` lists five views on `release/sep-1` (no threat context) — resolve to that line's list plus `svc.v_articles`, and `docs/CLAUDE.md` §6's table on that line lacks the threat-context row. Note for the deploy: migration 0026 must be applied on dev before the console's Articles page can create anything, and no ECS task def gains an env key.
