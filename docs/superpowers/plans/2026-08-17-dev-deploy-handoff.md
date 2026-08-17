# Dev Deploy Handoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `services` deployable boot correctly in the ap-south-1 dev environment, with a readiness probe that fails on a broken `svc` contract, and deploy it.

**Architecture:** Config gains the handoff's env names plus four boot assertions (region match, log level, stub-in-dev, non-default match threshold). A new `/readyz` returns 503 unless the DB is up and all four `svc` views exist with the expected columns. The Dockerfile pins arm64 and port 8081, and the same image runs migrations under a different command. Task definitions and the task-role policy land under `infra/ecs/` with tests asserting no write-S3 grant and no secret in `environment`.

**Tech Stack:** Python 3.11+, FastAPI, pydantic-settings, psycopg 3, pytest, Docker buildx, AWS ECS on EC2, Rekognition, Secrets Manager.

**Spec:** `docs/superpowers/specs/2026-08-17-dev-deploy-handoff-design.md`

## Global Constraints

- Region: `ap-south-1`. Account: `225989356895`. Cluster: `imageshield-dev`.
- Image platform: `linux/arm64` — the host is Graviton `t4g.medium`.
- `services` binds host port **8081**. `networkMode: host`, so no port mapping.
- Image tag is the git SHA, never `latest`.
- ECR: `225989356895.dkr.ecr.ap-south-1.amazonaws.com/imageshield/services`.
- Collections: `identity-dev-v1`, `discovered-dev-v1`.
- `ENROLMENT_QUALITY_FILTER` is accepted and **ignored** — invariant #5 fixes `QualityFilter: HIGH` permanently.
- Provider keys stay unconditionally required; `imageshield/dev/hive` and `imageshield/dev/google-vision` both already exist.
- Secrets arrive via the task definition's `secrets` block, never `environment`.
- `mypy` runs `strict = true`; `ruff` line-length 100.
- All four CI gates on every commit: `ruff check`, `mypy` (both scopes), `REQUIRE_DB=1 pytest`, docker build.
- **D16:** no `IndexFaces` until the AWS AI services opt-out returns `optOut`. Creating an empty collection is not enrolment.
- Never `DELETE`; never log a phone number; `user_ref` only.

---

## File Structure

**Modified:**
- `src/imageshield/config.py` — new fields, renames, four new `model_validator`s
- `src/imageshield/http/routes/health.py` — add `/readyz` beside `/health`
- `src/imageshield/http/app.py:107` — `identity_collection` rename at the call site
- `src/imageshield/http/routes/liveness.py:384,425,443` — rename
- `src/imageshield/http/routes/attribution.py:57` — rename
- `tests/conftest.py:55-96` — `VALID_ENV` and `make_config` gain the new keys
- `tests/test_config.py` — rename assertion at line 25, env-name case at line 65
- `Dockerfile` — arm64, port 8081, migration command
- `.env.example` — every new key
- `docs/OPERATIONS.md` — `/readyz` semantics

**Created:**
- `src/imageshield/http/svc_contract.py` — the expected `svc` view shape, one module so the handoff's `grep svc.v_person_` check finds exactly this file and the migration
- `tests/test_readyz.py`
- `tests/test_ecs_task_defs.py`
- `infra/ecs/imageshield-dev-services.json`
- `infra/ecs/imageshield-dev-migrate-services.json`
- `infra/ecs/policies/services-task-role.json`
- `infra/ecs/policies/exec-role-secrets.json`
- `docs/DEPLOY-DEV-HANDOFF.md` — the copy the handoff asks for

Task 1 is config (everything else reads it). Task 2 is the contract module plus `/readyz`. Task 3 is Docker. Task 4 is task definitions and IAM. Task 5 is docs. Task 6 is the deploy, which is operational and gated on 1-5 being green.

---

## Task 1: Config — renames, new fields, four boot assertions

**Files:**
- Modify: `src/imageshield/config.py`
- Modify: `src/imageshield/http/app.py:107`
- Modify: `src/imageshield/http/routes/liveness.py:384,425,443`
- Modify: `src/imageshield/http/routes/attribution.py:57`
- Modify: `tests/conftest.py:55-96`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `Config.identity_collection: str` (replaces `rekognition_collection_id`), `Config.rekognition_region: str`, `Config.discovered_collection: str`, `Config.enrolment_collision_threshold: float`, `Config.search_match_threshold: float`, `Config.attribution_max_inflight: int`, `Config.search_provider: Literal["stub","hive","google"]`, `Config.dev_face_ceiling: int`, `Config.log_level: Literal["debug","info","warning","error"]`. `Config.aws_region` is unchanged and still present.

- [ ] **Step 1: Write the failing tests for the four new assertions**

Add to `tests/test_config.py`:

```python
def test_rekognition_region_must_equal_aws_region() -> None:
    """D7. A regional collection reached from the wrong region is empty, and an
    empty collection is indistinguishable from 'no matches'."""
    with pytest.raises(ValidationError, match="REKOGNITION_REGION"):
        make_config(aws_region="ap-south-1", rekognition_region="us-east-1")


def test_matching_regions_are_accepted() -> None:
    cfg = make_config(aws_region="ap-south-1", rekognition_region="ap-south-1")
    assert cfg.rekognition_region == "ap-south-1"


def test_debug_logging_is_refused_in_production() -> None:
    with pytest.raises(ValidationError, match="LOG_LEVEL"):
        make_config(environment="production", log_level="debug")


def test_debug_logging_is_allowed_in_development() -> None:
    cfg = make_config(environment="development", log_level="debug", search_provider="stub")
    assert cfg.log_level == "debug"


def test_development_refuses_a_live_provider() -> None:
    """The dev Hive key is real and Hive has no sandbox; its price is NULL so the
    budget guard fails closed and caps nothing."""
    with pytest.raises(ValidationError, match="SEARCH_PROVIDER"):
        make_config(environment="development", search_provider="hive")


def test_production_allows_a_live_provider() -> None:
    cfg = make_config(environment="production", search_provider="hive")
    assert cfg.search_provider == "hive"


def test_search_match_threshold_refuses_the_rekognition_default() -> None:
    """80 is the value you get when nobody chose one."""
    with pytest.raises(ValidationError, match="SEARCH_MATCH_THRESHOLD"):
        make_config(search_match_threshold=80.0)


def test_search_match_threshold_accepts_a_deliberate_value() -> None:
    assert make_config(search_match_threshold=88.5).search_match_threshold == 88.5


def test_identity_collection_replaces_the_old_name() -> None:
    assert make_config().identity_collection == "identity-v1"
    assert not hasattr(make_config(), "rekognition_collection_id")


def test_unread_fields_are_still_validated() -> None:
    """DISCOVERED_COLLECTION and ENROLMENT_COLLISION_THRESHOLD have no reader in
    v1, but a blank or out-of-range value must still refuse to boot."""
    with pytest.raises(ValidationError):
        make_config(discovered_collection="   ")
    with pytest.raises(ValidationError):
        make_config(enrolment_collision_threshold=101.0)
```

Update the existing rename sites in the same file: line 25's
`cfg.rekognition_collection_id == "identity-v1"` becomes
`cfg.identity_collection == "identity-v1"`, and the line-65 env-name case
`("AWS_REGION", "not-a-region")` gains a sibling
`("REKOGNITION_REGION", "not-a-region")`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL — `TypeError`/`ValidationError` on unknown keyword `rekognition_region`, and `AttributeError` on `identity_collection`.

- [ ] **Step 3: Update the test fixtures**

In `tests/conftest.py`, `VALID_ENV` (line 55): replace
`"REKOGNITION_COLLECTION_ID": "identity-v1"` with the new keys.

```python
    "IDENTITY_COLLECTION": "identity-v1",
    "REKOGNITION_REGION": "ap-south-1",
    "DISCOVERED_COLLECTION": "discovered-v1",
    "ENROLMENT_COLLISION_THRESHOLD": "99",
    "SEARCH_MATCH_THRESHOLD": "88.5",
    "ATTRIBUTION_MAX_INFLIGHT": "4",
    "SEARCH_PROVIDER": "stub",
    "DEV_FACE_CEILING": "50",
```

In `make_config` (line 76), replace `"rekognition_collection_id": "identity-v1"`
with the same set as Python values:

```python
        "identity_collection": "identity-v1",
        "rekognition_region": "ap-south-1",
        "discovered_collection": "discovered-v1",
        "enrolment_collision_threshold": 99.0,
        "search_match_threshold": 88.5,
        "attribution_max_inflight": 4,
        "search_provider": "stub",
        "dev_face_ceiling": 50,
```

`make_config` defaults `environment="test"`, so the stub-in-dev assertion does
not fire for the whole existing suite — only the tests that pass
`environment="development"` exercise it.

- [ ] **Step 4: Implement the config changes**

In `src/imageshield/config.py`, rename the field and add the new ones. Replace
the `rekognition_collection_id: str` line (currently line 55) with:

```python
    # Renamed from REKOGNITION_COLLECTION_ID to match the deploy contract
    # (DEPLOY-DEV-HANDOFF §5). Hard rename, no alias: the old name is not in any
    # deployed environment, and two names for one value is the drift CLAUDE.md §9
    # warns about.
    identity_collection: str

    # The region the identity collection lives in. A Rekognition collection is
    # regional: enrol into one region, search another, and the second is empty.
    # Nothing errors — it reads as "no matches", which is the one wrong answer
    # this product must never give silently. Asserted equal to aws_region below.
    rekognition_region: str

    # ── Declared, validated, and NOT READ in v1 ───────────────────────────
    # Both exist so the deployed env block matches DEPLOY-DEV-HANDOFF §5
    # literally; a variable the operator sets that silently vanishes is worse
    # than one that is documented as inert.
    #
    # discovered_collection: `discovered-v1` and clustering are "specified, do
    # not build yet" (CLAUDE.md §6).
    discovered_collection: str
    # enrolment_collision_threshold: THERE IS NO COLLISION CHECK, deliberately.
    # Wiring this to one means a similarity score influencing enrolment, which is
    # invariant #1 ("identity never comes from a similarity score") and the
    # fragmentation bug the old system shipped. Read #1 and #1a before giving
    # this a reader.
    enrolment_collision_threshold: float

    # Threshold for provider face-search matching. DISTINCT from
    # face_match_threshold (enrolment) and attribution_match_threshold
    # (attribution) — one threshold per purpose, invariant #1b. Refused at
    # exactly 80: that is Rekognition's default, i.e. the value nobody chose.
    search_match_threshold: float

    # Concurrent in-flight attribution searches.
    attribution_max_inflight: int

    # 'stub' dispatches no provider call at all. In development this is the only
    # thing standing between a test run and billable Hive traffic — the dev key
    # is real, Hive has no sandbox, and its cost_per_call_usd is NULL so the
    # step-8 budget guard fails closed and caps nothing. Asserted below.
    search_provider: Literal["stub", "hive", "google"] = "stub"

    # Dev-only guard on collection size, so a runaway test cannot enrol
    # thousands of faces into a shared dev collection.
    dev_face_ceiling: int

    log_level: Literal["debug", "info", "warning", "error"] = "info"
```

Add `rekognition_region` to the existing `_region` validator's decorator (line
252) so it gets the same regex, and `identity_collection` plus
`discovered_collection` to the existing `_non_empty` validator (line 259).

Add `enrolment_collision_threshold` and `search_match_threshold` to the existing
`_confidence` validator (line 277) for the 0-100 bound, and
`attribution_max_inflight` and `dev_face_ceiling` to `_positive` (line 291).

Then the four new validators, after `_scan_thresholds_ordered`:

```python
    @model_validator(mode="after")
    def _rekognition_region_matches_deployment(self) -> Config:
        """D7. Refused at boot because the failure is silent everywhere else.

        A collection is regional. Enrol into ap-south-1, search us-east-1, and
        the search succeeds against an empty collection: no error, no alarm,
        just "no matches in monitored sources" forever. CLAUDE.md §1 calls a
        false negative a broken promise, and this is the cheapest way to make one.
        """
        if self.rekognition_region != self.aws_region:
            raise ValueError(
                "REKOGNITION_REGION must equal AWS_REGION — a collection is"
                " regional, and searching the wrong region returns an empty"
                " result that is indistinguishable from 'no matches'"
            )
        return self

    @model_validator(mode="after")
    def _no_debug_logging_in_production(self) -> Config:
        """Debug logs in this service carry user_ref, bounding boxes and
        provider payloads. The redaction processor covers known keys; debug
        level widens what reaches the log in the first place."""
        if self.environment == "production" and self.log_level == "debug":
            raise ValueError(
                "LOG_LEVEL must not be 'debug' when ENVIRONMENT=production"
            )
        return self

    @model_validator(mode="after")
    def _development_uses_the_stub_provider(self) -> Config:
        """The dev Hive credential is a REAL key — Hive has no sandbox.

        Hive Web Search is contract-priced and `hive.cost_per_call_usd` is NULL,
        so a budget set against it fails closed and caps nothing (§7.6). That
        makes config the only cheap place to stop a dev run spending real money,
        and an env edit alone must not be enough to do it.
        """
        if self.environment == "development" and self.search_provider != "stub":
            raise ValueError(
                "SEARCH_PROVIDER must be 'stub' when ENVIRONMENT=development —"
                " the dev provider keys are real and Hive has no sandbox"
            )
        return self

    @model_validator(mode="after")
    def _search_threshold_is_deliberate(self) -> Config:
        """80 is Rekognition's FaceMatchThreshold default.

        The handoff says "pin it — not 80" because the default is the value you
        get when nobody made a decision, and for the threshold that decides
        whether someone is told their face is in porn, an accidental value is
        not acceptable. Any other number is fine; this only refuses the one that
        means "unset".
        """
        if self.search_match_threshold == 80.0:
            raise ValueError(
                "SEARCH_MATCH_THRESHOLD must not be exactly 80 — that is"
                " Rekognition's default, i.e. an unchosen value; pin a measured one"
            )
        return self
```

- [ ] **Step 5: Update the four call sites for the rename**

`src/imageshield/http/app.py:107`:

```python
    log_aws_identity(region=cfg.aws_region, collection_id=cfg.identity_collection)
```

`src/imageshield/http/routes/liveness.py` lines 384 and 425:

```python
            collection_id=cfg.identity_collection,
```

`src/imageshield/http/routes/liveness.py:443`:

```python
        await face_index.delete_faces(cfg.identity_collection, (indexed.face_id,))
```

`src/imageshield/http/routes/attribution.py:57`:

```python
            collection_id=cfg.identity_collection,
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS

Run: `python -m pytest tests/ -q`
Expected: PASS — the rename is compile-visible, so any missed site shows here.

- [ ] **Step 7: Run mypy to catch missed rename sites**

Run: `python -m mypy src tests`
Expected: no errors. `mypy --strict` is what proves the rename is complete;
`grep` is not.

- [ ] **Step 8: Commit**

```bash
git add src/imageshield/config.py src/imageshield/http/app.py \
        src/imageshield/http/routes/liveness.py \
        src/imageshield/http/routes/attribution.py \
        tests/conftest.py tests/test_config.py
git commit -m "Config: the deploy contract's names, and four refusals at boot

REKOGNITION_COLLECTION_ID becomes IDENTITY_COLLECTION, and REKOGNITION_REGION
is asserted equal to AWS_REGION: a collection is regional, so the wrong region
searches an empty one and reports 'no matches' with nothing logged.

ENVIRONMENT=development now requires SEARCH_PROVIDER=stub. The dev Hive key is
real, Hive has no sandbox, and its cost_per_call_usd is NULL — so the budget
guard fails closed and caps nothing. Config is the cheap place to stop that.

DISCOVERED_COLLECTION and ENROLMENT_COLLISION_THRESHOLD are declared and
validated but have no reader; the latter carries a warning that giving it one
walks into invariant #1.

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

## Task 2: `/readyz` and the `svc` contract shape check

**Files:**
- Create: `src/imageshield/http/svc_contract.py`
- Modify: `src/imageshield/http/routes/health.py`
- Modify: `src/imageshield/http/models.py` (add `ReadyResponse`)
- Test: `tests/test_readyz.py`

**Interfaces:**
- Consumes: `Config` from Task 1 (unused here, but the route is mounted on the same app).
- Produces: `EXPECTED_VIEWS: dict[str, dict[str, str]]` mapping view name → column → `information_schema` `data_type`; `async def check_svc_contract(conn) -> list[str]` returning a list of human-readable problems, empty when the contract holds; `ReadyResponse` pydantic model with fields `status: str`, `version: str`, `db: str`, `problems: list[str]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_readyz.py`:

```python
"""/readyz — the deploy gate for the svc contract.

Unlike /health (always 200, because a degraded DB must not look like "service
absent" to the proxy's retry logic), /readyz returns 503 when the contract is
broken. A deploy must not succeed into a broken contract: the proxy's own views
JOIN against ours, so a dropped column breaks them at runtime while breaking
nothing here.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from imageshield.http.svc_contract import EXPECTED_VIEWS, check_svc_contract


def test_the_four_views_are_all_declared() -> None:
    assert set(EXPECTED_VIEWS) == {
        "v_person_enrolment_state",
        "v_person_report_summary",
        "v_person_hits",
        "v_person_liveness_attempts",
    }


@pytest.mark.asyncio
@pytest.mark.usefixtures("migrated_db")
async def test_contract_holds_on_a_migrated_database(db_conn) -> None:
    assert await check_svc_contract(db_conn) == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("migrated_db")
async def test_a_dropped_view_is_reported_by_name(db_conn) -> None:
    await db_conn.execute("DROP VIEW svc.v_person_hits")
    problems = await check_svc_contract(db_conn)
    assert any("v_person_hits" in p for p in problems)


@pytest.mark.asyncio
@pytest.mark.usefixtures("migrated_db")
async def test_a_retyped_column_is_reported_with_the_column(db_conn) -> None:
    """The contract is types as well as names: the proxy JOINs on these."""
    await db_conn.execute("DROP VIEW svc.v_person_liveness_attempts")
    await db_conn.execute(
        """
        CREATE VIEW svc.v_person_liveness_attempts AS
        SELECT user_ref AS person_ref,
               count(*)::bigint AS attempts_24h,
               max(created_at) AS last_attempt_at
        FROM liveness_sessions
        WHERE created_at > now() - interval '24 hours'
        GROUP BY user_ref
        """
    )
    problems = await check_svc_contract(db_conn)
    assert any("attempts_24h" in p for p in problems)


@pytest.mark.asyncio
@pytest.mark.usefixtures("migrated_db")
async def test_an_added_column_is_not_a_problem(db_conn) -> None:
    """Additions are safe by contract; only removals and retypes break the
    proxy. The check asserts expected-subset-of-actual, not equality."""
    await db_conn.execute("DROP VIEW svc.v_person_enrolment_state")
    await db_conn.execute(
        """
        CREATE VIEW svc.v_person_enrolment_state AS
        SELECT e.user_ref AS person_ref, e.status, e.model_id,
               e.created_at AS enrolled_at, 'extra'::text AS future_column
        FROM enrolments e WHERE e.status = 'active'
        """
    )
    assert await check_svc_contract(db_conn) == []


def test_readyz_returns_503_when_the_database_is_down(client: TestClient) -> None:
    """No db_check wired means the pool is not open."""
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["db"] == "degraded"


def test_readyz_needs_no_service_token(client: TestClient) -> None:
    """Same posture as /health — a readiness probe cannot carry a secret."""
    response = client.get("/readyz")
    assert response.status_code in (200, 503)
```

Reuse whatever DB fixtures `tests/db.py` already provides (`throwaway_db`,
`run_migrate`); if a `db_conn`/`migrated_db` fixture pair does not exist yet,
add it to `tests/conftest.py` following the pattern the existing DB tests use.
Do not invent a second migration mechanism.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_readyz.py -v`
Expected: FAIL — `ModuleNotFoundError: imageshield.http.svc_contract`.

- [ ] **Step 3: Write the contract module**

Create `src/imageshield/http/svc_contract.py`:

```python
"""The expected shape of the four `svc` contract views.

This is the ONE place outside migration 0016 that names `svc.v_person_*`, which
is what the deploy checklist's `grep svc.v_person_` check relies on.

Why a shape check and not just an existence check: the views are a versioned
cross-repo contract (CLAUDE.md §3), and the tightest coupling in the
architecture. The proxy's own views JOIN against ours, so dropping or retyping a
column breaks the proxy at runtime while breaking nothing here — the failure
lands in the repo that did not cause it. Asserting the shape at readiness moves
that failure back to the deploy that caused it.

Columns may be ADDED freely, so the check is expected-subset-of-actual rather
than equality. Removals and retypes are the breaking changes and both are
caught.

Types are `information_schema.columns.data_type` spellings, taken from
migration 0016.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from psycopg import AsyncConnection

SVC_SCHEMA = "svc"

EXPECTED_VIEWS: dict[str, dict[str, str]] = {
    "v_person_enrolment_state": {
        "person_ref": "uuid",
        "status": "text",
        "model_id": "text",
        "enrolled_at": "timestamp with time zone",
    },
    "v_person_report_summary": {
        "person_ref": "uuid",
        "active_reports": "integer",
        "unresolved_matches": "integer",
        "live_exposure_count": "integer",
        "last_run_at": "timestamp with time zone",
        "first_scan_completed_at": "timestamp with time zone",
        "monitored_sources": "integer",
    },
    "v_person_hits": {
        "hit_id": "uuid",
        "report_id": "uuid",
        "person_ref": "uuid",
        "source_photo_id": "text",
        "hit_status": "text",
        "last_checked_at": "timestamp with time zone",
        "match_id": "uuid",
        "source_domain": "text",
        "host_page_url": "text",
        "face_bbox": "jsonb",
        "title": "text",
        "detected_at": "timestamp with time zone",
        "match_status": "text",
        "match_action": "text",
        "match_lifecycle": "text",
        "resolved_at": "timestamp with time zone",
        "resolution_note": "text",
        "provider_count": "integer",
        "score": "numeric",
    },
    "v_person_liveness_attempts": {
        "person_ref": "uuid",
        "attempts_24h": "integer",
        "last_attempt_at": "timestamp with time zone",
    },
}

_ACTUAL_SQL = """
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_schema = %s
"""


async def check_svc_contract(conn: AsyncConnection) -> list[str]:
    """Return one message per contract violation; empty list means healthy.

    Messages name the view and column because this endpoint is internal and on a
    private subnet — a readiness probe that says only "not ready" costs an hour
    of digging.
    """
    actual: dict[str, dict[str, str]] = {}
    async with conn.cursor() as cur:
        await cur.execute(_ACTUAL_SQL, (SVC_SCHEMA,))
        for table_name, column_name, data_type in await cur.fetchall():
            actual.setdefault(table_name, {})[column_name] = data_type

    problems: list[str] = []
    for view, columns in EXPECTED_VIEWS.items():
        if view not in actual:
            problems.append(f"missing_view: {SVC_SCHEMA}.{view}")
            continue
        for column, expected_type in columns.items():
            found = actual[view].get(column)
            if found is None:
                problems.append(f"missing_column: {SVC_SCHEMA}.{view}.{column}")
            elif found != expected_type:
                problems.append(
                    f"wrong_column_type: {SVC_SCHEMA}.{view}.{column}"
                    f" is {found}, expected {expected_type}"
                )
    return problems
```

Verify each declared type against `migrations/0016_svc_contract_views.up.sql`
before moving on. If a real type differs from what is written above, the
migration is authoritative — fix this table, not the migration. Run the
happy-path test to confirm rather than trusting the list.

- [ ] **Step 4: Add the response model**

In `src/imageshield/http/models.py`, beside `HealthResponse`:

```python
class ReadyResponse(BaseModel):
    """Readiness. Unlike HealthResponse this is allowed to be a 503 — it gates
    a deploy, and a deploy must not succeed into a broken svc contract."""

    status: str
    version: str
    db: str
    problems: list[str] = []
```

- [ ] **Step 5: Add the route**

In `src/imageshield/http/routes/health.py`, extend the module docstring to say
`/readyz` differs deliberately in returning 503, then add:

```python
@router.get("/readyz", response_model=ReadyResponse)
async def readyz(
    response: Response,
    db_pool: AsyncConnectionPool = Depends(get_db_pool),
) -> ReadyResponse:
    """Deploy gate: DB reachable AND the four svc views present and correct.

    503 rather than /health's always-200. /health tells the proxy whether we are
    answering; this tells a deploy whether it may proceed, and those are
    different questions with different right answers.
    """
    try:
        async with db_pool.connection() as conn:
            problems = await check_svc_contract(conn)
    except Exception:  # readiness must not propagate
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadyResponse(
            status="not_ready", version=APP_VERSION, db="degraded", problems=[]
        )

    if problems:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadyResponse(
            status="not_ready", version=APP_VERSION, db="ok", problems=problems
        )
    return ReadyResponse(status="ready", version=APP_VERSION, db="ok", problems=[])
```

Add the imports this needs: `Response`, `status` from `fastapi`,
`AsyncConnectionPool` from `psycopg_pool`, `get_db_pool` from
`imageshield.http.deps`, `ReadyResponse` from `imageshield.http.models`, and
`check_svc_contract` from `imageshield.http.svc_contract`.

Confirm `/readyz` is exempt from service-token auth the same way `/health` is —
check `src/imageshield/http/auth.py` (its docstring at line 15 names `/health`
explicitly) and add `/readyz` to the same exemption. A readiness probe cannot
carry a secret.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `REQUIRE_DB=1 python -m pytest tests/test_readyz.py -v`
Expected: PASS, all eight.

- [ ] **Step 7: Verify the grep check the handoff asks for**

Run: `grep -rn "svc.v_person_" src/ migrations/`
Expected: hits only in `src/imageshield/http/svc_contract.py` and
`migrations/0016_svc_contract_views.up.sql` / `.down.sql`.

- [ ] **Step 8: Commit**

```bash
git add src/imageshield/http/svc_contract.py \
        src/imageshield/http/routes/health.py \
        src/imageshield/http/models.py \
        src/imageshield/http/auth.py tests/test_readyz.py
git commit -m "/readyz: the svc contract, asserted at deploy time

The four views are a versioned cross-repo contract and the tightest coupling in
this architecture: the proxy's views JOIN against ours, so a dropped or retyped
column breaks the proxy at runtime while breaking nothing here. This moves that
failure back to the deploy that caused it.

Checks names AND types, expected-subset-of-actual — additions are safe by
contract, removals and retypes are not. 503 rather than /health's always-200,
because a deploy must not succeed into a broken contract.

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

## Task 3: Dockerfile — arm64, port 8081, migration command

**Files:**
- Modify: `Dockerfile`

**Interfaces:**
- Consumes: Task 1's `HTTP_PORT` handling (unchanged name).
- Produces: an arm64 image whose default command serves uvicorn on 8081, and which runs migrations when given `["python", "scripts/migrate.py", "up"]`.

- [ ] **Step 1: Pin the platform and the port**

In `Dockerfile`, both `FROM` lines take an explicit platform:

```dockerfile
FROM --platform=linux/arm64 python:3.12-slim AS builder
```

```dockerfile
FROM --platform=linux/arm64 python:3.12-slim AS runtime
```

Change the runtime `ENV` block's `HTTP_PORT=8000` to `HTTP_PORT=8081`, and
`EXPOSE 8000` to `EXPOSE 8081`. Add a comment above the platform pin:

```dockerfile
# linux/arm64 is not a preference: the dev host is Graviton (t4g.medium) and an
# amd64 image fails with an exec-format error that reads like a broken
# entrypoint, which is an hour of debugging the wrong thing.
```

And above the port:

```dockerfile
# 8081 on the host — networkMode: host means the container binds the host
# interface directly, and `api` already holds 8080.
```

- [ ] **Step 2: Confirm the migration command works from the same image**

The image already copies `migrations/` and `scripts/`. The migration task
definition overrides `command`; no Dockerfile change is needed beyond confirming
the path. Verify:

Run: `docker run --rm --entrypoint python <tag> scripts/migrate.py --help`
Expected: the usage text from `scripts/migrate.py`.

One image, two commands, so migrations are provably built from the same commit
as the server — and migrations never run on container start (handoff §7).

- [ ] **Step 3: Build for arm64 and verify the architecture**

```bash
docker buildx build --platform linux/arm64 -t imageshield/services:local-arm64 .
docker image inspect imageshield/services:local-arm64 --format '{{.Architecture}}'
```

Expected: `arm64`.

If buildx cannot emulate arm64 locally, note it and rely on the ECR push in
Task 6 plus `docker manifest inspect`; do not silently drop the platform flag.

- [ ] **Step 4: Commit**

```bash
git add Dockerfile
git commit -m "Dockerfile: arm64 and host port 8081

The dev host is Graviton; an amd64 image fails with an exec-format error that
reads like a broken entrypoint. 8081 because networkMode host binds the host
interface directly and api holds 8080.

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

## Task 4: Task definitions, IAM policies, and their tests

**Files:**
- Create: `infra/ecs/imageshield-dev-services.json`
- Create: `infra/ecs/imageshield-dev-migrate-services.json`
- Create: `infra/ecs/policies/services-task-role.json`
- Create: `infra/ecs/policies/exec-role-secrets.json`
- Test: `tests/test_ecs_task_defs.py`

**Interfaces:**
- Consumes: the env var names from Task 1; port 8081 from Task 3.
- Produces: task-definition JSON registered in Task 6. Secret ARNs carry a
  placeholder `-XXXXXX` suffix resolved during Task 6.

- [ ] **Step 1: Write the failing test**

Create `tests/test_ecs_task_defs.py`:

```python
"""The dev task definitions, asserted from the JSON that ECS will register.

Same reasoning as tests/test_iam_policy.py: a policy asserted from a copy of the
intent is asserted about nothing. These parse the actual files.

The handoff's §10 checklist is mechanically checkable, so it is checked here
rather than in a reviewer's head.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

ECS_DIR = Path(__file__).resolve().parents[1] / "infra" / "ecs"
SERVICES_TASK = ECS_DIR / "imageshield-dev-services.json"
MIGRATE_TASK = ECS_DIR / "imageshield-dev-migrate-services.json"
TASK_ROLE = ECS_DIR / "policies" / "services-task-role.json"

LIVENESS_BUCKET = "imageshield-dev-liveness-225989356895"


def _load(path: Path) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return document


def _statements(path: Path) -> list[dict[str, Any]]:
    statements: list[dict[str, Any]] = _load(path)["Statement"]
    return statements


def _actions(path: Path) -> list[str]:
    actions: list[str] = []
    for statement in _statements(path):
        raw = statement.get("Action", [])
        actions.extend([raw] if isinstance(raw, str) else raw)
    return actions


@pytest.mark.parametrize("path", [SERVICES_TASK, MIGRATE_TASK, TASK_ROLE])
def test_the_files_exist_and_parse(path: Path) -> None:
    assert path.is_file(), f"{path} is missing"
    assert _load(path)


def test_the_task_role_can_only_read_s3_and_only_the_liveness_bucket() -> None:
    """This service writes to S3 through presigned URLs from the proxy
    (CLAUDE.md §3.3). GetObject on the liveness bucket is the one read it needs;
    any Put/Delete/List would make the presigned handshake optional, and an
    optional handshake is one that gets skipped."""
    for action in _actions(TASK_ROLE):
        if not action.lower().startswith("s3:"):
            continue
        assert action == "s3:GetObject", f"unexpected S3 action: {action}"

    for statement in _statements(TASK_ROLE):
        raw = statement.get("Action", [])
        actions = [raw] if isinstance(raw, str) else raw
        if not any(a.lower().startswith("s3:") for a in actions):
            continue
        resources = statement["Resource"]
        resources = [resources] if isinstance(resources, str) else resources
        for resource in resources:
            assert LIVENESS_BUCKET in resource, f"S3 grant outside liveness: {resource}"
            assert resource != "*", "S3 grant is unscoped"


def test_no_rekognition_action_is_unscoped() -> None:
    """Scoped to the two dev collection ARNs. `Resource: "*"` on rekognition
    would let this role reach a production collection from dev."""
    for statement in _statements(TASK_ROLE):
        raw = statement.get("Action", [])
        actions = [raw] if isinstance(raw, str) else raw
        if not any(a.lower().startswith("rekognition:") for a in actions):
            continue
        resources = statement["Resource"]
        resources = [resources] if isinstance(resources, str) else resources
        assert "*" not in resources, "rekognition granted on Resource: *"
        for resource in resources:
            assert "collection/" in resource, f"not a collection ARN: {resource}"
            assert "-dev-" in resource, f"not a dev collection: {resource}"


@pytest.mark.parametrize("path", [SERVICES_TASK, MIGRATE_TASK])
def test_no_secret_arrives_through_environment(path: Path) -> None:
    """An `environment` value is visible in describe-task-definition to anyone
    with read access. Handoff §4 and §10."""
    suspicious = ("password", "secret", "token", "api_key", "apikey", "credential")
    for container in _load(path)["containerDefinitions"]:
        for entry in container.get("environment", []):
            name = entry["name"].lower()
            assert not any(word in name for word in suspicious), (
                f"{entry['name']} looks like a secret but is in `environment`"
            )
            assert "arn:aws:secretsmanager" not in entry["value"]


@pytest.mark.parametrize("path", [SERVICES_TASK, MIGRATE_TASK])
def test_every_task_has_both_roles(path: Path) -> None:
    """Execution role pulls the image and resolves secrets before the code runs;
    task role is what the code itself uses. Two different things, both required."""
    document = _load(path)
    assert document["executionRoleArn"]
    assert document["taskRoleArn"]


def test_services_binds_8081_in_host_mode() -> None:
    document = _load(SERVICES_TASK)
    assert document["networkMode"] == "host"
    container = document["containerDefinitions"][0]
    ports = {entry["name"]: entry["value"] for entry in container["environment"]}
    assert ports["HTTP_PORT"] == "8081"


def test_services_declares_the_stub_provider_and_matching_regions() -> None:
    """Task 1's boot assertions refuse otherwise — better to catch it here than
    in a container that will not start."""
    container = _load(SERVICES_TASK)["containerDefinitions"][0]
    env = {entry["name"]: entry["value"] for entry in container["environment"]}
    assert env["SEARCH_PROVIDER"] == "stub"
    assert env["AWS_REGION"] == env["REKOGNITION_REGION"] == "ap-south-1"
    assert env["SEARCH_MATCH_THRESHOLD"] != "80"


def test_the_migration_task_does_not_serve_http() -> None:
    """It is a one-off task; a migration container that binds 8081 would
    collide with the running service on a host-mode instance."""
    container = _load(MIGRATE_TASK)["containerDefinitions"][0]
    assert "migrate" in " ".join(container["command"])
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_ecs_task_defs.py -v`
Expected: FAIL — the JSON files are missing.

- [ ] **Step 3: Write the task role policy**

Create `infra/ecs/policies/services-task-role.json`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "RekognitionOnDevCollectionsOnly",
      "Effect": "Allow",
      "Action": [
        "rekognition:CreateCollection",
        "rekognition:DescribeCollection",
        "rekognition:IndexFaces",
        "rekognition:DeleteFaces",
        "rekognition:SearchFacesByImage",
        "rekognition:ListFaces"
      ],
      "Resource": [
        "arn:aws:rekognition:ap-south-1:225989356895:collection/identity-dev-v1",
        "arn:aws:rekognition:ap-south-1:225989356895:collection/discovered-dev-v1"
      ]
    },
    {
      "Sid": "FaceLivenessIsNotCollectionScoped",
      "Effect": "Allow",
      "Action": [
        "rekognition:CreateFaceLivenessSession",
        "rekognition:StartFaceLivenessSession",
        "rekognition:GetFaceLivenessSessionResults"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ReadLivenessReferenceImagesOnly",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::imageshield-dev-liveness-225989356895/*"
    },
    {
      "Sid": "SqsProduceAndConsume",
      "Effect": "Allow",
      "Action": [
        "sqs:SendMessage",
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes"
      ],
      "Resource": [
        "arn:aws:sqs:ap-south-1:225989356895:imageshield-dev-identity-index",
        "arn:aws:sqs:ap-south-1:225989356895:imageshield-dev-search-runs"
      ]
    },
    {
      "Sid": "DecryptWithTheDevKey",
      "Effect": "Allow",
      "Action": ["kms:Decrypt", "kms:GenerateDataKey"],
      "Resource": "arn:aws:kms:ap-south-1:225989356895:key/5b32f333-b48d-4ea0-b4a7-7b1c8e591c74"
    }
  ]
}
```

Note the Face Liveness statement: those three actions are not
collection-scoped in IAM, so they take `Resource: "*"`. The test only requires
scoping on statements that also carry collection-shaped actions — verify the
test passes with this split rather than assuming it. If the test rejects it,
the test is right about the shape and the fix is to keep the Sids separate as
written here.

Cross-check the SQS queue names against `infra/terraform/` and the handoff
before committing; the handoff lists the backend's four queues, and ours are
the two from CLAUDE.md §2. Use the real names.

- [ ] **Step 4: Write the execution role policy**

Create `infra/ecs/policies/exec-role-secrets.json`, matching handoff §3:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue"],
      "Resource": "arn:aws:secretsmanager:ap-south-1:225989356895:secret:imageshield/dev/*"
    },
    {
      "Effect": "Allow",
      "Action": ["kms:Decrypt"],
      "Resource": "arn:aws:kms:ap-south-1:225989356895:key/5b32f333-b48d-4ea0-b4a7-7b1c8e591c74"
    }
  ]
}
```

- [ ] **Step 5: Write the services task definition**

Create `infra/ecs/imageshield-dev-services.json`. Secret ARN suffixes are
`-XXXXXX` placeholders, resolved in Task 6 Step 2.

```json
{
  "family": "imageshield-dev-services",
  "networkMode": "host",
  "requiresCompatibilities": ["EC2"],
  "executionRoleArn": "arn:aws:iam::225989356895:role/imageshield-dev-exec",
  "taskRoleArn": "arn:aws:iam::225989356895:role/imageshield-dev-services",
  "containerDefinitions": [
    {
      "name": "services",
      "image": "225989356895.dkr.ecr.ap-south-1.amazonaws.com/imageshield/services:PLACEHOLDER_SHA",
      "essential": true,
      "memory": 768,
      "environment": [
        { "name": "ENVIRONMENT", "value": "development" },
        { "name": "LOG_LEVEL", "value": "debug" },
        { "name": "HTTP_HOST", "value": "0.0.0.0" },
        { "name": "HTTP_PORT", "value": "8081" },
        { "name": "AWS_REGION", "value": "ap-south-1" },
        { "name": "REKOGNITION_REGION", "value": "ap-south-1" },
        { "name": "IDENTITY_COLLECTION", "value": "identity-dev-v1" },
        { "name": "DISCOVERED_COLLECTION", "value": "discovered-dev-v1" },
        { "name": "ENROLMENT_QUALITY_FILTER", "value": "HIGH" },
        { "name": "SEARCH_PROVIDER", "value": "stub" },
        { "name": "DEV_FACE_CEILING", "value": "50" },
        { "name": "ATTRIBUTION_MAX_INFLIGHT", "value": "4" },
        { "name": "ATTRIBUTION_MAX_CANDIDATES", "value": "20" },
        { "name": "MIN_ENROLMENT_AGE", "value": "13" },
        { "name": "MIN_DISCOVERY_AGE", "value": "18" },
        { "name": "LIVENESS_MIN_CONFIDENCE", "value": "90" },
        { "name": "LIVENESS_SESSION_TTL_SECONDS", "value": "600" },
        { "name": "LIVENESS_MAX_ATTEMPTS_24H", "value": "5" },
        { "name": "DB_POOL_MAX_SIZE", "value": "5" },
        { "name": "HIVE_BASE_URL", "value": "https://api.thehive.ai" }
      ],
      "secrets": [
        { "name": "DATABASE_URL", "valueFrom": "arn:aws:secretsmanager:ap-south-1:225989356895:secret:imageshield/dev/db/app_services-XXXXXX:url::" },
        { "name": "SERVICE_TOKEN", "valueFrom": "arn:aws:secretsmanager:ap-south-1:225989356895:secret:imageshield/dev/service-token/backend-to-services-XXXXXX:token::" },
        { "name": "ADMIN_SERVICE_TOKEN", "valueFrom": "arn:aws:secretsmanager:ap-south-1:225989356895:secret:imageshield/dev/service-token/admin-XXXXXX:token::" },
        { "name": "HIVE_API_KEY", "valueFrom": "arn:aws:secretsmanager:ap-south-1:225989356895:secret:imageshield/dev/hive-XXXXXX:api_key::" },
        { "name": "GOOGLE_VISION_API_KEY", "valueFrom": "arn:aws:secretsmanager:ap-south-1:225989356895:secret:imageshield/dev/google-vision-XXXXXX:api_key::" }
      ],
      "healthCheck": {
        "command": ["CMD-SHELL", "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8081/health').status==200 else 1)\""],
        "interval": 30,
        "timeout": 5,
        "retries": 3,
        "startPeriod": 30
      },
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/imageshield/dev",
          "awslogs-region": "ap-south-1",
          "awslogs-stream-prefix": "services"
        }
      }
    }
  ]
}
```

The health check uses `python` rather than the handoff's `wget`, because the
`python:3.12-slim` base has no wget. It targets `/health` (always 200 when the
process answers) not `/readyz` — ECS would kill and restart a container that is
up but waiting on migrations, which is a crash loop rather than a signal.

`SEARCH_MATCH_THRESHOLD`, `FACE_MATCH_THRESHOLD` and
`ENROLMENT_COLLISION_THRESHOLD` are **deliberately absent above** — they are
required with no default and the handoff says to pin them, not to guess. Add
them in Task 6 Step 4 with the operator's chosen numbers. The test asserting
`SEARCH_MATCH_THRESHOLD != "80"` will fail until they are added, which is the
intended forcing function.

- [ ] **Step 6: Write the migration task definition**

Create `infra/ecs/imageshield-dev-migrate-services.json` — same image, migration
command, migrator credential, no ports.

```json
{
  "family": "imageshield-dev-migrate-services",
  "networkMode": "host",
  "requiresCompatibilities": ["EC2"],
  "executionRoleArn": "arn:aws:iam::225989356895:role/imageshield-dev-exec",
  "taskRoleArn": "arn:aws:iam::225989356895:role/imageshield-dev-services",
  "containerDefinitions": [
    {
      "name": "migrate-services",
      "image": "225989356895.dkr.ecr.ap-south-1.amazonaws.com/imageshield/services:PLACEHOLDER_SHA",
      "essential": true,
      "memory": 256,
      "command": ["python", "scripts/migrate.py", "up"],
      "environment": [
        { "name": "ENVIRONMENT", "value": "development" },
        { "name": "LOG_LEVEL", "value": "debug" }
      ],
      "secrets": [
        { "name": "DATABASE_URL", "valueFrom": "arn:aws:secretsmanager:ap-south-1:225989356895:secret:imageshield/dev/db/migrator_services-XXXXXX:url::" }
      ],
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/imageshield/dev",
          "awslogs-region": "ap-south-1",
          "awslogs-stream-prefix": "migrate-services"
        }
      }
    }
  ]
}
```

- [ ] **Step 7: Run the tests**

Run: `python -m pytest tests/test_ecs_task_defs.py -v`
Expected: all PASS except `test_services_declares_the_stub_provider_and_matching_regions`, which fails on the missing `SEARCH_MATCH_THRESHOLD` until Task 6 Step 4. Leave it failing and say so at the task gate — do not add a placeholder threshold to make it green.

- [ ] **Step 8: Commit**

```bash
git add infra/ecs tests/test_ecs_task_defs.py
git commit -m "infra/ecs: dev task definitions for services and its migration

One image, two commands, so migrations are provably from the same commit as the
server. The task role reads S3 only, only the liveness bucket, and Rekognition
only on the two dev collection ARNs — asserted from the JSON ECS registers,
following tests/test_iam_policy.py, because a policy asserted from a copy of the
intent is asserted about nothing.

SEARCH_MATCH_THRESHOLD is deliberately unset: it is required with no default and
the handoff says to pin a measured value, not guess one. Its test fails until
an operator picks a number.

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

## Task 5: Docs

**Files:**
- Create: `docs/DEPLOY-DEV-HANDOFF.md`
- Modify: `.env.example`
- Modify: `docs/OPERATIONS.md`
- Modify: `CLAUDE.md`
- Modify: `infra/terraform/rekognition.tf:27` (stale env var name in a description)

**Interfaces:**
- Consumes: the field names from Task 1, `/readyz` from Task 2.
- Produces: no code interface.

- [ ] **Step 1: Copy the handoff into `docs/`**

Copy the handoff document verbatim to `docs/DEPLOY-DEV-HANDOFF.md` (it says to
put it in both repos), then append a section recording what this repo declines:

```markdown
---

## 12. What the services repo deliberately does not implement

Recorded here so the deploy side does not set a variable expecting an effect.

- **`ENROLMENT_QUALITY_FILTER`** — accepted and ignored. Invariant #5 fixes
  `QualityFilter: HIGH` on every `IndexFaces`, permanently. A poor enrolment
  vector degrades every match that user will ever get and they have no way to
  know, so this is not a knob.
- **`DISCOVERED_COLLECTION`** — validated, no reader. `discovered-v1` and
  clustering are "specified, do not build yet" (CLAUDE.md §6). The dev
  collection is created empty so nothing writes to a missing collection later.
- **`ENROLMENT_COLLISION_THRESHOLD`** — validated, no reader, and giving it one
  is invariant #1 territory: identity must never come from a similarity score.
  Read #1 and #1a before wiring it.
- **`SEARCH_MATCH_THRESHOLD`** — implemented and required, with no default, so
  the process refuses to boot until someone pins it. Not derivable from a dev
  measurement (§11).

`SEARCH_PROVIDER=stub` is enforced in config when `ENVIRONMENT=development`: the
dev Hive key is real, Hive has no sandbox, and `hive.cost_per_call_usd` is NULL,
so the budget guard fails closed and caps nothing.
```

- [ ] **Step 2: Update `.env.example`**

Replace the `AWS_REGION` / `REKOGNITION_COLLECTION_ID` block with the new keys,
each with the comment explaining what it is for. Keep the existing style —
prose comments above each group, values that work for local docker compose.

```bash
AWS_REGION=us-east-1
# Must EQUAL AWS_REGION — boot refuses otherwise. A Rekognition collection is
# regional, so the wrong region searches an empty collection and reports "no
# matches" with nothing logged (D7).
REKOGNITION_REGION=us-east-1
IDENTITY_COLLECTION=identity-v1
# Validated, not read in v1 — clustering is out of scope (CLAUDE.md §6).
DISCOVERED_COLLECTION=discovered-v1
# Validated, NOT READ, and giving it a reader is invariant #1 territory:
# identity must never come from a similarity score.
ENROLMENT_COLLISION_THRESHOLD=99
# Provider face-search threshold. Distinct from FACE_MATCH_THRESHOLD
# (enrolment) and ATTRIBUTION_MATCH_THRESHOLD — one threshold per purpose
# (#1b). Exactly 80 is refused: that is Rekognition's default, i.e. unchosen.
SEARCH_MATCH_THRESHOLD=88.5
ATTRIBUTION_MAX_INFLIGHT=4
# 'stub' dispatches no provider call. REQUIRED when ENVIRONMENT=development:
# the dev keys are real, Hive has no sandbox, and its price is NULL so the
# budget guard caps nothing.
SEARCH_PROVIDER=stub
DEV_FACE_CEILING=50
# 'debug' is refused when ENVIRONMENT=production.
LOG_LEVEL=debug
```

- [ ] **Step 3: Document `/readyz` in `docs/OPERATIONS.md`**

Add a subsection covering: what it checks, why it is 503 rather than 200, that
ECS health checks target `/health` instead (a container waiting on migrations
must not be killed and restarted), and how to read the `problems` array. Include
the operator's first move when it reports `missing_view`: run the migration
task, because `services` migrates first and creates `svc`.

- [ ] **Step 4: Fix the stale Terraform description**

`infra/terraform/rekognition.tf:27` reads
`description = "Set as REKOGNITION_COLLECTION_ID."` — the old env var name.
Update it to `IDENTITY_COLLECTION`. It is only an output description, so nothing
breaks, but a doc string naming a variable that no longer exists is exactly the
drift the hard rename was meant to avoid.

- [ ] **Step 5: Update `CLAUDE.md`**

Two small edits:
- §2's stack table or §9 gains `/readyz` beside the `/health` mention.
- §9's API contract list gains one line: `GET /readyz` — unauthenticated,
  503 when the `svc` contract is broken.

Do not restate the deploy details; `docs/DEPLOY-DEV-HANDOFF.md` owns them.

- [ ] **Step 6: Commit**

```bash
git add docs/DEPLOY-DEV-HANDOFF.md docs/OPERATIONS.md .env.example CLAUDE.md \
        infra/terraform/rekognition.tf
git commit -m "Docs: the dev deploy contract, and what we decline to implement

Records the four env vars this repo accepts but does not honour, and why each
refusal is deliberate — ENROLMENT_QUALITY_FILTER because invariant #5 makes
HIGH permanent, ENROLMENT_COLLISION_THRESHOLD because a reader for it walks
into invariant #1.

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

## Task 6: Deploy

**Files:** none in the repo except the SHA and threshold substitutions.

**Interfaces:**
- Consumes: everything above.
- Produces: a running `services` task answering `/readyz` with 200.

This task makes real, billed, hard-to-reverse AWS changes. Confirm with the
operator before each `create`/`register`/`run` step. Do not proceed past a
failure — read the error.

- [ ] **Step 1: Verify the four CI gates before touching AWS**

```bash
python -m ruff check .
python -m mypy src tests
REQUIRE_DB=1 python -m pytest -q
docker buildx build --platform linux/arm64 -t imageshield/services:preflight .
```

All four must be green. `pytest` alone has previously hidden 13 lint errors, so
all four, every time. The one expected failure from Task 4 Step 7 is fixed by
Step 4 below — do not deploy with it still failing.

- [ ] **Step 2: Resolve the real secret ARN suffixes**

```bash
export AWS_DEFAULT_REGION=ap-south-1
aws secretsmanager list-secrets \
  --query 'SecretList[?starts_with(Name, `imageshield/dev/`)].{Name:Name,ARN:ARN}' \
  --output table
```

Substitute each `-XXXXXX` in both task definitions with the real suffix. Also
confirm the JSON **key name** inside each secret matches what the ARN's
`:key::` fragment claims — the plan assumes `url` for the DB secrets, `token`
for the service tokens, `api_key` for the providers. If a key differs, fix the
task definition, not the secret. A wrong key name fails at task start with
`ResourceInitializationError` and no application logs, which is a confusing
failure worth avoiding.

- [ ] **Step 3: Create the execution role and the task role**

Per handoff §3 for the execution role (`imageshield-dev-exec` +
`read-dev-secrets` from `infra/ecs/policies/exec-role-secrets.json`), then the
task role `imageshield-dev-services` with the same trust policy and
`infra/ecs/policies/services-task-role.json` attached.

Verify:

```bash
aws iam get-role-policy --role-name imageshield-dev-services \
  --policy-name services-task-role --query 'PolicyDocument' --output json
```

Confirm no `s3:PutObject` appears and every `rekognition:` collection statement
names a `-dev-` ARN.

- [ ] **Step 4: Pin the thresholds**

Ask the operator for `SEARCH_MATCH_THRESHOLD`, `FACE_MATCH_THRESHOLD` and
`ENROLMENT_COLLISION_THRESHOLD`. Add them to
`infra/ecs/imageshield-dev-services.json`, then confirm
`tests/test_ecs_task_defs.py` is fully green and commit.

Do not invent these numbers. The handoff's §11 says explicitly not to tune a
threshold from a dev measurement, and §2.2 of the spec makes them boot-required
precisely so they cannot be defaulted into existence.

- [ ] **Step 5: Build and push the real image**

```bash
export AWS_DEFAULT_REGION=ap-south-1
REGISTRY=225989356895.dkr.ecr.ap-south-1.amazonaws.com
SHA=$(git rev-parse --short HEAD)
aws ecr get-login-password | docker login --username AWS --password-stdin $REGISTRY
docker buildx build --platform linux/arm64 -t $REGISTRY/imageshield/services:$SHA . --push
docker manifest inspect $REGISTRY/imageshield/services:$SHA | grep -A2 platform
```

Expected: `"architecture": "arm64"`. Substitute `$SHA` for `PLACEHOLDER_SHA` in
both task definitions.

If the ECR repository does not exist, create it first
(`aws ecr create-repository --repository-name imageshield/services`).

- [ ] **Step 6: Create the Rekognition collections**

```bash
aws rekognition create-collection --collection-id identity-dev-v1 --region ap-south-1
aws rekognition create-collection --collection-id discovered-dev-v1 --region ap-south-1
aws rekognition list-collections --region ap-south-1
```

`discovered-dev-v1` stays empty — the module that would write to it is out of
scope. Creating it is not a decision to build clustering.

**D16 reminder:** creating a collection is safe; enrolling a face is not, until
the AI services opt-out returns `optOut` for Rekognition. Check before any
enrolment test:

```bash
aws organizations list-policies-for-target --filter AISERVICES_OPT_OUT_POLICY \
  --target-id $(aws organizations describe-account --account-id 225989356895 \
  --query 'Account.Id' --output text) 2>&1 || echo "see docs/DEPLOY-DEV.md §7"
```

- [ ] **Step 7: Register both task definitions and run the migration**

```bash
aws ecs register-task-definition --cli-input-json file://infra/ecs/imageshield-dev-migrate-services.json
aws ecs register-task-definition --cli-input-json file://infra/ecs/imageshield-dev-services.json

TASK=$(aws ecs run-task --cluster imageshield-dev --launch-type EC2 \
  --task-definition imageshield-dev-migrate-services \
  --query 'tasks[0].taskArn' --output text)
aws ecs wait tasks-stopped --cluster imageshield-dev --tasks $TASK
aws ecs describe-tasks --cluster imageshield-dev --tasks $TASK \
  --query 'tasks[0].containers[0].{Exit:exitCode,Reason:reason}' --output json
```

Expected: `Exit: 0`. `services` migrates first — it creates `svc` and the four
views, and the backend cannot pass readiness without them.

On a non-zero exit: `aws logs tail /imageshield/dev --since 10m --filter-pattern migrate-services`.

- [ ] **Step 8: Create the service**

```bash
aws ecs create-service --cluster imageshield-dev --service-name services \
  --task-definition imageshield-dev-services --desired-count 1 \
  --launch-type EC2 \
  --deployment-configuration 'minimumHealthyPercent=0,maximumPercent=100'
aws ecs wait services-stable --cluster imageshield-dev --services services
```

`minimumHealthyPercent=0, maximumPercent=100` is not optional: with one
instance and fixed host ports, ECS cannot start a replacement while the old task
holds 8081, and the default 100 makes every deploy hang forever.

- [ ] **Step 9: Verify readiness on the host**

The service is on a private interface with no public ingress, so this runs on
the host via SSM, not from a laptop:

```bash
aws ssm start-session --target i-0d277703b778392ef
# on the host:
curl -s localhost:8081/health
curl -s -o /dev/null -w '%{http_code}\n' localhost:8081/readyz
curl -s localhost:8081/readyz
```

Expected: `/health` reports `db: ok`; `/readyz` returns **200** with
`status: ready` and an empty `problems` array.

If `/readyz` reports `missing_view`, the migration did not run or ran against a
different database — check Step 7's exit code before changing anything.

- [ ] **Step 10: Confirm the handoff's mechanical checklist**

Walk handoff §10, recording actual output for each item that applies to
`services`: arm64 manifest, no unscoped S3, secrets only via `secrets`,
`/readyz` failing on a missing view (test it by proving the check works, not by
dropping a view in dev), region refusal, and the redaction check that a log line
containing a phone number emits none of it.

Report which items pass, which do not, and which belong to the proxy repo. Do
not report the deploy as done with an unchecked item unless it is explicitly
out of scope.

- [ ] **Step 11: Commit the resolved task definitions**

```bash
git add infra/ecs
git commit -m "infra/ecs: pin the deployed image SHA, secret ARNs and thresholds

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

## Self-Review

**Spec coverage:** §2.1 renames → Task 1 Steps 4-5. §2.2 new fields → Task 1
Step 4. §2.3 four assertions → Task 1 Steps 1/4. §2.4 quality filter → Task 5
Step 1. §2.5 provider keys → no code change, documented Task 5 Step 1 and
enforced by Task 1's stub assertion. §3 `/readyz` → Task 2. §4 Dockerfile →
Task 3. §5 task defs + IAM + tests → Task 4. §6 deploy sequence → Task 6.
§7 docs → Task 5. §8 testing → each task's own steps plus Task 6 Step 1.
§9 risks → the rename is mypy-checked (Task 1 Step 7); the unread fields carry
comments (Task 1 Step 4) and docs (Task 5 Step 1); `search_match_threshold` is
operator-supplied (Task 6 Step 4).

**Placeholder scan:** `PLACEHOLDER_SHA` and `-XXXXXX` are intentional and
resolved in Task 6 Steps 2 and 5. The three unpinned thresholds are intentional
and resolved in Task 6 Step 4 by the operator, per the spec. No TBDs.

**Type consistency:** `identity_collection` is used identically in Task 1
Steps 3-5 and Task 4's env block (`IDENTITY_COLLECTION`).
`check_svc_contract(conn) -> list[str]` and `EXPECTED_VIEWS` are defined in
Task 2 Step 3 and consumed in Steps 1 and 5. `ReadyResponse` fields match
between Step 4's model and Step 5's constructions. Port 8081 is consistent
across Tasks 3, 4 and 6.

**Known gap flagged rather than hidden:** the `svc` column types in Task 2
Step 3 are read from migration 0016 but not yet verified against a live
`information_schema`; Step 3 instructs the implementer to verify and treat the
migration as authoritative. `face_bbox` (jsonb), `score` (numeric) and
`source_photo_id` (text) are the three most likely to differ.
