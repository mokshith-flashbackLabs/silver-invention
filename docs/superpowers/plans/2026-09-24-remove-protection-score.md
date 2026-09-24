# Remove the Services Protection Score — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete the unused services protection-score subsystem and the threat-event `penalty` that only fed it. Keep the tables and views dormant for one release.

**Architecture:** Two repos, each changed under its own rules. **Services** (`image_flashbacklabs`, branch `main`): a reversible migration makes `penalty` nullable; `penalty` becomes accepted-and-ignored; every recompute call is unwired; then `score/`, `recommendations/`, the admin scores route, the score config and the `score-tick` container are deleted. **Backend** (`image_backend`, branch `release/sep-1`): the admin scores relay is deleted; `penalty` becomes accepted-and-ignored and is never forwarded; the list strips it. **No UI work**: the console half is the prompt at `image_backend/docs/prompts/CONTROL-ROOM-REMOVE-PENALTY-PROMPT.md`.

**Tech Stack:** Python 3.11, FastAPI, pydantic 2, psycopg 3, pytest (services). Node 22, TypeScript, Fastify, zod, vitest + testcontainers (backend).

**Spec:** `image_flashbacklabs/docs/superpowers/specs/2026-09-24-remove-protection-score-design.md`

## Global Constraints

- **Tables and views stay** (owner's option A): `protection_scores`, `score_events`, `recommendations` and the `svc` views `v_person_score`, `v_person_score_events`, `v_person_recommendations` are NOT dropped, altered or ungranted. No migration touches them.
- Services migration is **0037**, as a pair `migrations/0037_threat_penalty_optional.{up,down}.sql`. It is reversible; the down migration fills NULLs with `0.01` before restoring `NOT NULL`.
- Services `ThreatEventCreateRequest` is `extra='forbid'`, so `penalty` stays as an **optional, ignored** field for one release. It is never written.
- Backend `threatEventBody` is `.strict()`, so `penalty` stays as an **optional, ignored** field for one release. It is never forwarded to services.
- **Deploy order: services first, then backend, then the console.** The services running today require `penalty > 0`.
- **Machine triage stays** (services INVARIANTS #47, `confirm/` severity triage and queue ordering). Only its score recompute goes.
- The backend's own Likeness Health Score (`image_backend/src/score/`, `/v1/me/score*`) is **out of scope. Do not touch it.** In backend tests, `report_penalty_per_hit` and `score-schema`/`quiz-schema` are false positives for "penalty".
- `v_person_hits.score` / `attestations.provider_score` in services is a provider match score, **not** the protection score. Do not touch it.
- Services: run pytest with `REQUIRE_DB=1 .venv/Scripts/python -m pytest <file>`, never with an extra `-q`. Run only ONE DB-backed pytest session at a time. Use `python -m mypy`, never the bare `mypy` shim. Run `ruff format` on NEW files only; CI runs `ruff check .`.
- Backend: never run prettier. ESLint forbids non-null assertions.
- Commit trailer, both repos: `Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>`. Never a Claude trailer. Stage only the files each task names; both repos carry unrelated untracked files.
- **No UI files are edited** in any repo (`image_backend/web/`, `imageshieldConsole`).

## Review Focus

1. **An older backend still sends `penalty` to new services.** The create returns 201 and stores NULL, not the value. → Task S1 test `test_create_ignores_a_sent_penalty`.
2. **A pre-0037 row with a penalty survives a round trip through the migration**, and a down migration after NULL rows exist restores `NOT NULL` without failing. → Task S1 migration test.
3. **Removing a recompute call must not change any response.** Each route's existing status and body assertions stay untouched while only the score-store assertions go, so they now prove the response is unchanged. → Task S2 Step 1.
4. **`tests/test_threats.py` loses the helpers it imports from `tests/test_score_store.py`** when that file is deleted. They move first. → Task S3.
5. **The old console still sends `penalty` to the new backend.** The create returns 201, and the body relayed to services carries no `penalty`. → Task B1 test.

---

## PART S — services (`image_flashbacklabs`, branch `main`)

### Task S1: Migration 0037 and threat events stop taking a penalty

**Files:**
- Create: `migrations/0037_threat_penalty_optional.up.sql`, `migrations/0037_threat_penalty_optional.down.sql`
- Modify: `src/imageshield/http/models.py` (~L544-610: `ThreatEventCreateRequest.penalty`, `ThreatEventItem.penalty`)
- Modify: `src/imageshield/threats/store.py` (SQL constants L48-107, the `create_event` Protocol L111-124 and impl L139-190, the `list_events` mapping ~L251, the module docstring L1-27)
- Modify: `src/imageshield/http/routes/admin_threat_events.py:81` (`penalty=body.penalty`)
- Test: `tests/test_admin_threat_routes.py`, `tests/test_threats.py`, `tests/test_migrations.py`

**Interfaces:**
- Produces: `ThreatStore.create_event(*, kind, title, body, severity, domains, is_global, expires_at, decay_days, operator)`, with **no `penalty` parameter**. `list_events()` rows carry no `"penalty"` key. `ThreatEventItem` has no `penalty` field.

- [ ] **Step 1: Write the migration.**

`migrations/0037_threat_penalty_optional.up.sql`:
```sql
-- 0037 — threat events stop carrying a penalty (spec 2026-09-24,
-- remove-protection-score).
--
-- `penalty` fed exactly one thing: the `threat` component of the services
-- PROTECTION score, which is being deleted. The user-facing score was never
-- fed by it (the backend charges by severity since its 0049). So a new event
-- stores NULL. The column stays for one release, with the dormant score tables
-- (0022), so this is reversible; a later migration drops all of them together.
--
-- 0022 declared `penalty NUMERIC(5,2) NOT NULL CHECK (penalty > 0)` with an
-- unnamed CHECK, which Postgres named threat_events_penalty_check.
-- `penalty_applied` on the matches had no CHECK.

ALTER TABLE threat_events ALTER COLUMN penalty DROP NOT NULL;
ALTER TABLE threat_events DROP CONSTRAINT threat_events_penalty_check;
ALTER TABLE threat_events
  ADD CONSTRAINT threat_events_penalty_check CHECK (penalty IS NULL OR penalty > 0);
ALTER TABLE threat_event_matches ALTER COLUMN penalty_applied DROP NOT NULL;
```

`migrations/0037_threat_penalty_optional.down.sql`:
```sql
-- Reverse 0037. Rows written after 0037 carry NULL; give them the smallest
-- value 0022's CHECK allows before restoring NOT NULL, or the ALTER fails.
UPDATE threat_event_matches SET penalty_applied = 0.01 WHERE penalty_applied IS NULL;
ALTER TABLE threat_event_matches ALTER COLUMN penalty_applied SET NOT NULL;
UPDATE threat_events SET penalty = 0.01 WHERE penalty IS NULL;
ALTER TABLE threat_events DROP CONSTRAINT threat_events_penalty_check;
ALTER TABLE threat_events ADD CONSTRAINT threat_events_penalty_check CHECK (penalty > 0);
ALTER TABLE threat_events ALTER COLUMN penalty SET NOT NULL;
```

Before writing it, confirm the constraint name on a migrated test DB:
`SELECT conname FROM pg_constraint WHERE conrelid = 'threat_events'::regclass AND contype = 'c';`
If it differs, use the real name in both files.

- [ ] **Step 2: Write the failing tests.**

In `tests/test_migrations.py`, add a test next to the existing 0022 ones. Copy their fixture and connection helpers:

```python
def test_0037_penalty_is_optional_and_reversible(throwaway_db) -> None:
    # Up: a NULL penalty inserts cleanly; a non-positive one is still refused.
    with connect(throwaway_db) as conn:
        conn.execute(
            "INSERT INTO threat_events (kind, title, severity, is_global, penalty, "
            "expires_at, decay_days, status, created_by) VALUES "
            "('leak','t',3,true,NULL, now() + interval '1 day', 7, 'active', 'op')"
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO threat_events (kind, title, severity, is_global, penalty, "
                "expires_at, decay_days, status, created_by) VALUES "
                "('leak','t',3,true,0, now() + interval '1 day', 7, 'active', 'op')"
            )
    # Down past 0037 with a NULL row present: it must not fail, and the row is backfilled.
    run_migrate(throwaway_db, "down", "--steps", "1")
    with connect(throwaway_db) as conn:
        assert conn.execute("SELECT count(*) FROM threat_events WHERE penalty IS NULL").fetchone()[0] == 0
    run_migrate(throwaway_db, "up")
```

Use the real fixture and helper names from the neighbouring 0022 tests (`test_0022_score_tables_and_role`). If they wrap connections differently, follow them exactly.

In `tests/test_admin_threat_routes.py`:
- Change `_body()` so `penalty` is absent by default. Delete the `"penalty": "5.00"` line.
- Replace `test_create_carries_penalty_as_a_decimal_not_a_float` with:

```python
def test_create_needs_no_penalty() -> None:
    client, threats, _score = make_client()

    response = client.post("/v1/admin/threat-events", json=_body(), headers=ADMIN)

    assert response.status_code == 201
    assert "penalty" not in threats.create_calls[0]


def test_create_ignores_a_sent_penalty() -> None:
    # An older backend still sends one for a release; new services ignore it.
    client, threats, _score = make_client()

    response = client.post("/v1/admin/threat-events", json=_body(penalty="5.00"), headers=ADMIN)

    assert response.status_code == 201
    assert "penalty" not in threats.create_calls[0]
```

(`FakeThreatStore.create_calls` records the kwargs passed to `create_event`. If it stores a positional tuple, assert on its shape instead. Leave `FakeScoreStore` alone here; Task S2 removes it.)

In `tests/test_threats.py`, find the store-level create calls. Drop their `penalty=` arguments, and assert that the `threat_events.penalty` and `threat_event_matches.penalty_applied` columns read NULL for a new event. Leave the score assertions in that file alone for now; Task S3 rewrites them.

- [ ] **Step 3: Run them and confirm they fail.**

`REQUIRE_DB=1 .venv/Scripts/python -m pytest tests/test_admin_threat_routes.py tests/test_migrations.py -k "0037 or penalty"`

Expected: FAIL. The migration file is missing, or the create route still passes `penalty`.

- [ ] **Step 4: Implement.**

`models.py`: in `ThreatEventCreateRequest`, replace `penalty: Decimal = Field(gt=0)` with:

```python
    # ACCEPTED AND IGNORED for one release (spec 2026-09-24): it fed only the
    # protection score, which is gone. The backend stops sending it once these
    # services are live; drop the field after that.
    penalty: Decimal | None = None
```

Update the class docstring so it no longer says penalty crosses as a meaningful value. In `ThreatEventItem`, delete the `penalty: str` field and its comment.

`threats/store.py`:
- Remove `penalty` from `_INSERT_EVENT_SQL`: drop both the column name and the `%(penalty)s` value.
- `_MATCH_DOMAINS_SQL` and `_MATCH_GLOBAL_SQL` insert into `threat_event_matches (event_id, user_ref, matched_via, penalty_applied)`. Remove `penalty_applied` from the column list, and remove the selected `%(penalty)s`.
- Remove `penalty` from `_LIST_EVENTS_SQL` and shift the row indices in `list_events`'s mapping. Every `row[n]` after index 7 moves down by one. Check each one against the new SELECT order.
- Remove the `penalty: Decimal` parameter from the Protocol and from the implementation of `create_event`. Remove it from the params dicts too.
- Rewrite the module docstring's first paragraph: a threat event records who a wider incident affects, and the backend charges the user's score by severity. Delete the "`threat` component of every matched person's protection score" framing.

`admin_threat_events.py:81`: delete `penalty=body.penalty,`.

- [ ] **Step 5: Run the tests and lint.**

`REQUIRE_DB=1 .venv/Scripts/python -m pytest tests/test_admin_threat_routes.py tests/test_threats.py tests/test_migrations.py`, then `.venv/Scripts/python -m ruff check src tests migrations` and `.venv/Scripts/python -m mypy src/imageshield/threats src/imageshield/http`.

Expected: PASS. If `test_threats.py` still fails on score assertions that read `penalty_applied`, delete only those assertions and note it in the report; Task S3 finishes that file.

- [ ] **Step 6: Commit.**

```bash
git add migrations/0037_threat_penalty_optional.up.sql migrations/0037_threat_penalty_optional.down.sql src/imageshield/http/models.py src/imageshield/threats/store.py src/imageshield/http/routes/admin_threat_events.py tests/test_admin_threat_routes.py tests/test_threats.py tests/test_migrations.py
git commit -m "threats: an event no longer carries a penalty (0037, nullable, reversible)

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

### Task S2: Unwire every protection-score recompute

**Files (each block below is swallow-and-log, after the write commits):**
- `src/imageshield/confirm/worker.py`: imports L107-108, `ConfirmDeps.score` L181-184, construction L260-273, recompute block L671-693
- `src/imageshield/search/worker.py`: imports L50-51, the `handle_message(..., score_store: ScoreStore, ...)` parameter L144, block L186-193, construction L213-217, the argument passed at L245
- `src/imageshield/http/routes/admin_review.py`: imports L29/L38, param L137, block L151-163, module docstring L11-16
- `src/imageshield/http/routes/admin_threat_events.py`: the `_recompute_each` helper L49-65, calls in create (~L92) and retract (~L119), the `score_store` params L72/L103, imports L27/L37, module docstring L7-13
- `src/imageshield/http/routes/attribution.py`: imports L24-31/L40, param L54, block L93-105
- `src/imageshield/http/routes/infringements.py`: imports L31-38/L49, param L91 plus block L102-109 (`record_feedback`), param L195 plus block L224-236 (`subject_decision`)
- `src/imageshield/http/routes/liveness.py`: imports L69/L88, param L242, block L533-536
- `src/imageshield/http/routes/search.py`: imports L23-28/L40, param L54, block L76-83
- Tests, removing `FakeScoreStore`, its wiring and its assertions: `tests/test_confirm_worker.py` (L231+, L270, L281, L591-599), `tests/test_liveness_routes.py` (L392+, L427-438, L969/975/1000), `tests/test_search_routes.py` (L182+, L227-234, L288-300, L848-854), `tests/test_search_worker.py` (L102+, the `handle_message` calls L161-262), `tests/test_subject_decision_routes.py` (L45+, L66-73, L92-131, L169-170), `tests/test_attribution_routes.py` (L124+, L192-203, L248-302), `tests/test_admin_review_routes.py` (L110+, L164-174, L351-355), `tests/test_admin_threat_routes.py` (L55+, L93-99, L150-200), `tests/test_attribution_crop_seeds.py:236`

**Interfaces:**
- Produces: `search.worker.handle_message(...)` **without** a `score_store` parameter. `ConfirmDeps` **without** `score`. No route takes `Depends(get_score_store)`. (`get_score_store` and `app.state.score_store` still exist until Task S3 deletes them.)

- [ ] **Step 1: Replace the score assertions in the tests first.**

In every test file listed, delete `FakeScoreStore`, the fixture wiring that sets `app.state.score_store`, the `score` / `raising_score_store` factory parameters, and every `.calls` assertion on it.

For each test named `test_a_raising_score_store_…_does_not_change_…` / `…never_changes_the_response`, delete it. Keep the tests that assert on the response. Update `make_client` helpers to return one fewer item, for example in `test_admin_threat_routes.py`:

```python
def make_client(*, matched: tuple[UserRef, ...] = ()) -> tuple[TestClient, FakeThreatStore]:
    app = create_app(config=make_config())
    threats = FakeThreatStore(matched=matched)
    app.state.threat_store = threats
    return TestClient(app), threats
```

Fix every caller: `client, threats, score = make_client()` becomes `client, threats = make_client()`.

Every route test that asserted a **status code and body** stays exactly as it is. Those assertions are what prove removing the recompute changed no response, which is Review Focus #3. Delete only the lines that assert on the fake score store. If a test would be left asserting nothing, delete that test and name it in your report.

- [ ] **Step 2: Run the edited tests and confirm they fail.**

`REQUIRE_DB=1 .venv/Scripts/python -m pytest tests/test_admin_threat_routes.py tests/test_search_worker.py tests/test_confirm_worker.py`

Expected: FAIL. `handle_message()` still requires `score_store`, and the routes still `Depends(get_score_store)`, which raises the "required state missing" error when no `score_store` is set.

- [ ] **Step 3: Delete each recompute block and its plumbing** at the lines listed under Files: the try/except, the `score_store` / `deps.score` parameter or field, the imports and the construction. In `search/worker.py`, drop `score_store` from the `handle_message` signature, from the construction at L213-217, and from the receive-loop call at L245. In `admin_threat_events.py`, delete `_recompute_each` entirely, and rewrite the module docstring so it no longer claims every matched person's protection score is recomputed.

Where a comment above a deleted block says "tick will heal" or cites the score, delete the comment too.

- [ ] **Step 4: Prove nothing outside `score/` still reaches for it.**

Run: `grep -rn "score_store\|ScoreStore\|PostgresScoreStore\|ScoreWeights\|\.recompute(" src/imageshield --include=*.py | grep -v "^src/imageshield/score/"`

Expected: only `http/app.py` (the construction) and `http/deps.py` (`get_score_store`). Task S3 removes both.

- [ ] **Step 5: Run the tests and lint.**

`REQUIRE_DB=1 .venv/Scripts/python -m pytest tests/test_confirm_worker.py tests/test_liveness_routes.py tests/test_search_routes.py tests/test_search_worker.py tests/test_subject_decision_routes.py tests/test_attribution_routes.py tests/test_admin_review_routes.py tests/test_admin_threat_routes.py tests/test_attribution_crop_seeds.py`, then `.venv/Scripts/python -m ruff check src tests` and `.venv/Scripts/python -m mypy src`.

Expected: PASS and clean.

- [ ] **Step 6: Commit.**

```bash
git add src/imageshield/confirm/worker.py src/imageshield/search/worker.py src/imageshield/http/routes/admin_review.py src/imageshield/http/routes/admin_threat_events.py src/imageshield/http/routes/attribution.py src/imageshield/http/routes/infringements.py src/imageshield/http/routes/liveness.py src/imageshield/http/routes/search.py tests/test_confirm_worker.py tests/test_liveness_routes.py tests/test_search_routes.py tests/test_search_worker.py tests/test_subject_decision_routes.py tests/test_attribution_routes.py tests/test_admin_review_routes.py tests/test_admin_threat_routes.py tests/test_attribution_crop_seeds.py
git commit -m "score: nothing recomputes the protection score any more

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

### Task S3: Delete the score subsystem, its route, its config and the score-tick container

**Files:**
- Delete: `src/imageshield/score/` (`__init__.py`, `engine.py`, `store.py`, `tick.py`), `src/imageshield/recommendations/` (`__init__.py`, `catalog.py`), `src/imageshield/http/routes/admin_scores.py`
- Delete tests: `tests/test_score_engine.py`, `tests/test_score_store.py`, `tests/test_score_tick.py`, `tests/test_recommendations.py`, `tests/test_admin_scores_routes.py`
- Modify: `src/imageshield/http/app.py` (L43 router import, L62-63 imports, L116-121 construction, L193 `include_router`), `src/imageshield/http/deps.py` (L34, L133-135), `src/imageshield/http/models.py` (L893-913 `ScoreDetailResponse`, `ScoreEventItem`, `ScoreResponse`)
- Modify: `src/imageshield/config.py` (the `score_*` fields L347-408; only the `score_*` entries inside the shared `_positive` tuple L569-587 and the one `_positive_float` entry L608; the `_score_config_coherent` validator L851-888)
- Modify: `tests/test_config.py` (score tests ~L500-568; only the `score_*` parameters in `test_new_positive_int_fields_reject_zero`), `tests/test_threats.py` (score fixture L66-68, recompute assertions L335/343/351, the helper import L38), `tests/test_boundaries.py` (L393-418 `SCORE_WRITE` and `test_only_the_score_store_writes_the_score` ONLY; keep the generic `_scored_source_files` machinery L85-161)
- Modify: `infra/ecs/imageshield-dev-confirm.json` (the `score-tick` container, L174-338), `infra/ecs/prod/confirm.json` (L64-118, and the prose in `x-notes` at L3), `infra/ecs/prod/README.md` (L11, L91), `tests/test_ecs_task_defs.py` (`test_confirm_task_runs_exactly_the_confirm_worker_and_the_tick`, L380-392+)
- Keep untouched: `tests/test_migrations.py` 0022 tests, `tests/test_svc_views.py` (the tables and views stay), `tests/test_readyz.py`

- [ ] **Step 1: Move the helpers `test_threats.py` borrows.** It imports `_enrolment` and `_seed` from `tests/test_score_store.py` (L38). Copy those two functions verbatim into `tests/test_threats.py` (or into `tests/helpers` if the repo keeps such a module; follow what exists) and point the import there. Run `REQUIRE_DB=1 .venv/Scripts/python -m pytest tests/test_threats.py`; expected PASS before anything is deleted.

- [ ] **Step 2: Rewrite `tests/test_threats.py`'s score assertions.** Delete the `PostgresScoreStore` fixture (L66-68) and the tests or assertions that call `.recompute(...)` to check a score (L335, L343, L351). Where such a test also checked that **retraction flips the event and the matches leave `v_person_threat_context`**, keep that part as a view read. It is the behaviour the backend relies on. Run the file; expected PASS.

- [ ] **Step 3: Rewrite the ECS test to the new shape (failing first).**

```python
def test_confirm_task_runs_exactly_the_confirm_worker() -> None:
    # The protection score and its tick were removed (spec 2026-09-24).
    for path in CONFIRM_TASK_DEF_PATHS:  # the existing dev + prod paths list in this file
        containers = {c["name"] for c in load(path)["containerDefinitions"]}
        assert containers == {"confirm-worker"}
```

Use the file's real loader and path constants. Run `.venv/Scripts/python -m pytest tests/test_ecs_task_defs.py`; expected FAIL, because `score-tick` is still present.

- [ ] **Step 4: Delete.**
- Remove the packages, the route, the three response models, and the test files listed above.
- Remove the `app.py`/`deps.py` wiring.
- Remove the config fields, their validator entries, and the whole `_score_config_coherent` validator.
- Remove the config tests for those fields.
- Remove the boundary test block.
- Remove the `score-tick` container from both task defs, keeping the JSON valid.
- Edit `infra/ecs/prod/confirm.json`'s `x-notes` and `infra/ecs/prod/README.md` L11/L91, so the memory budget reads `confirm (confirm-worker 160) | 160`. Check the README's arithmetic line and total, and adjust the total.

Config is `extra="ignore"` (L73) and no task def sets a `SCORE_*` env var, so nothing else needs to change for boot.

- [ ] **Step 5: Prove it is gone.**

`grep -rn "imageshield\.score\|imageshield\.recommendations\|score_store\|ScoreStore\|score-tick\|score\.tick\|admin_scores\|ScoreResponse\|score_config_version\|score_weight_\|score_posture_\|score_coverage_\|score_exposure_\|score_threat_\|score_seed_\|score_rec_\|score_scan_\|score_tick" src tests infra scripts devtools`

Expected: no hits, except comments in `outbox.py:36` and `scripts/localstack/init-sqs.sh:5` naming the 2026-08-19 design doc. Reword those two to say the queue predates the score's removal, or leave them if they only cite the doc's history.

- [ ] **Step 6: Run the whole services suite serially, plus lint and types.**

`REQUIRE_DB=1 .venv/Scripts/python -m pytest tests/`, then `.venv/Scripts/python -m ruff check .` and `.venv/Scripts/python -m mypy src`.

Expected: all pass. If anything is red, check whether it is red on `main` before this plan (stash-free: run it in a detached worktree at the base commit) before blaming this change, and report both runs.

- [ ] **Step 7: Commit.**

```bash
git add -A src/imageshield/score src/imageshield/recommendations src/imageshield/http/routes/admin_scores.py tests/test_score_engine.py tests/test_score_store.py tests/test_score_tick.py tests/test_recommendations.py tests/test_admin_scores_routes.py
git add src/imageshield/http/app.py src/imageshield/http/deps.py src/imageshield/http/models.py src/imageshield/config.py tests/test_config.py tests/test_threats.py tests/test_boundaries.py tests/test_ecs_task_defs.py infra/ecs/imageshield-dev-confirm.json infra/ecs/prod/confirm.json infra/ecs/prod/README.md
git commit -m "score: delete the protection score, its route, config and tick (tables stay dormant)

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

(`git add -A <paths>` stages the deletions of those paths only. Do not `git add -A` the repo root; it has unrelated untracked files.)

---

### Task S4: Services docs, edited in place

**Files:** `CLAUDE.md`, `INVARIANTS.md`, `SCHEMA.md`, `PROXY_INTEGRATION.md`, `docs/OPERATIONS.md` (L30, L563, L566), `docs/deploy/DEPLOY-RUNBOOK.md` (L520-521), `ARCHITECTURE.md` (L411). The historical 2026-08-19 spec and plan are NOT edited.

- [ ] **Step 1: Edit each in place with targeted edits. Never rewrite a file.** Each change gets a dated note in the doc's own voice: *"Removed 2026-09-24 (spec 2026-09-24-remove-protection-score): …"*.
  - `CLAUDE.md`: in the §6 scope table, strike the "Protection score + recommendations" row (L367) into a dated note, and change "bounded/decaying/reversible score effect" on the Threat events row (L368) to say the backend charges by severity and services keep no score. In §4, rewrite the summary line for #44–47 so it says #44–46 are retired and #47's triage stands. Update L343's "four entirely new pieces" prose.
  - `INVARIANTS.md` §G (L691-780): mark #44, #45 and #46 **Retired 2026-09-24**, with one sentence each on why (no protection score exists; the user score is the backend's). Keep #47 whole, removing only its sentence about auto-confirmed hits "costing Exposure".
  - `SCHEMA.md` §2d, L1024-1119 and L1121-1156: add a "dormant since 2026-09-24" note at the top of the score-tables subsection; note `penalty` / `penalty_applied` are nullable since 0037; add a 0037 entry wherever migrations are listed. Do not touch the confirm-pipeline subsections or the `v_person_hits` / `v_person_report_summary` lines.
  - `PROXY_INTEGRATION.md`: trim the "Triggers a score recompute (cause_kind='subject_decision')" clause at L212. Mark `v_person_score`, `v_person_score_events` and `v_person_recommendations` "granted, no longer written" in the view table (L720-737) and in the §"These four views" wording (L803-815, L923-925). Note `penalty` on threat-event create is accepted and ignored for one release, and the list no longer returns it. Record that `GET /v1/admin/scores/{user_ref}` is removed.
  - `docs/OPERATIONS.md`, `docs/deploy/DEPLOY-RUNBOOK.md`, `ARCHITECTURE.md`: remove or annotate the score-tick and protection-score mentions at the lines listed.
  - In `SCHEMA.md` or `CLAUDE.md`, record the follow-up as an **open item**: "drop `protection_scores`, `score_events`, `recommendations`, their three `svc` views and `threat_events.penalty` / `threat_event_matches.penalty_applied` in one migration once dev and prod have run a release without them".

- [ ] **Step 2: Check.** `grep -n -i "protection score\|score-tick\|score_store" CLAUDE.md INVARIANTS.md SCHEMA.md PROXY_INTEGRATION.md docs/OPERATIONS.md docs/deploy/DEPLOY-RUNBOOK.md ARCHITECTURE.md` should show only dated notes and retirements.

- [ ] **Step 3: Commit.**

```bash
git add CLAUDE.md INVARIANTS.md SCHEMA.md PROXY_INTEGRATION.md docs/OPERATIONS.md docs/deploy/DEPLOY-RUNBOOK.md ARCHITECTURE.md
git commit -m "docs: the protection score is removed; tables dormant, follow-up drop recorded

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

## PART B — backend (`image_backend`, branch `release/sep-1`)

### Task B1: Remove the scores relay; `penalty` accepted, ignored and never forwarded

**Files:**
- Modify: `src/admin/routes.ts` (delete the `// ── scores` block L546-559; the `GET /v1/admin/threat-events` map ~L352-381; the create closure's `const { action, ...event } = body;`), `src/admin/services-client.ts` (interface L72, impl L178-180), `src/admin/fakes.ts` (L220-223), `src/admin/schemas.ts` (`userRefParams` L35, used only by that route; `threatEventBody.penalty` ~L139), `src/admin/persons/routes.ts` (the comment at L18-23)
- Modify tests: `tests/helpers/idor.ts` (delete L767-773; reword the analogy comment at ~L985), `tests/integration/admin-proxy.test.ts` (L142-144 and the L34 comment; the create body ~L159-163), `tests/integration/score-threat.test.ts:168`, `tests/integration/threat-actions.test.ts:324`, `tests/unit/admin-schemas.test.ts` (L30-33)

**Interfaces:**
- Produces: `AdminServicesClient` without `score()`. `threatEventBody` has `penalty: z.string().regex(/^\d+(\.\d{1,2})?$/).optional()`, parsed and then discarded. The body relayed by `createThreatEvent` never contains `penalty`. Items from `GET /v1/admin/threat-events` never contain `penalty`.

- [ ] **Step 1: Write the failing tests.**

In `tests/integration/admin-proxy.test.ts`, replace the scores assertion (L142-144) with:

```ts
    // Removed 2026-09-24: services no longer keep a protection score.
    const noScore = await call('GET', `/v1/admin/scores/${randomUUID()}`);
    expect(noScore.statusCode).toBe(404);
    expect(noScore.json<{ error: string }>().error).toBe('NOT_FOUND');
```

(`NOT_FOUND` is what `notFoundEnvelope` in `src/http/error-handler.ts` returns for an unknown route; checked when this plan was written.)

Where the same file asserts the relayed create body carries `penalty: '5.00'` (~L159-163), change it to send a penalty and assert the relayed body has none:

```ts
    expect(relayed).not.toHaveProperty('penalty');
```

Use the variable name the test already uses for the fake's recorded body.

In `tests/integration/threat-actions.test.ts`, drop `penalty: '2.00'` from the `EVENT()` body at L324. Add:

```ts
  it('a create with a penalty from an old console still succeeds, and the list never shows one', async () => {
    const operator = await loginAsOperator(test, db, { displayName: 'Rhea', role: 'business' });
    const created = await test.app.inject({
      method: 'POST', url: '/v1/admin/threat-events',
      headers: { authorization: `Bearer ${operator.accessToken}`, 'idempotency-key': randomUUID() },
      payload: { ...EVENT(), penalty: '2.00' },
    });
    expect(created.statusCode).toBe(201);
    const eventId = created.json<{ event_id: string }>().event_id;
    expect(test.admin.events.get(eventId)).not.toHaveProperty('penalty');

    // A services list row that still carries penalty must not reach the panel.
    const stored = test.admin.events.get(eventId);
    if (stored !== undefined) stored['penalty'] = '0.08';
    const list = await test.app.inject({
      method: 'GET', url: '/v1/admin/threat-events',
      headers: { authorization: `Bearer ${operator.accessToken}` },
    });
    const item = list.json<{ events: Record<string, unknown>[] }>().events.find((e) => e['event_id'] === eventId);
    expect(item).toBeDefined();
    expect(item).not.toHaveProperty('penalty');
  });
```

In `tests/integration/score-threat.test.ts:168`, drop `penalty: '2.00'` from the body.

In `tests/unit/admin-schemas.test.ts` (L30-33), replace the penalty assertions with:

```ts
    expect(threatEventBody.safeParse(ok).success).toBe(true);                         // no penalty
    expect(threatEventBody.safeParse({ ...ok, penalty: '5.00' }).success).toBe(true); // old console: accepted, ignored
    expect(threatEventBody.safeParse({ ...ok, penalty: 5 }).success).toBe(false);     // still a decimal string if sent
```

Also remove `penalty` from that file's `ok` fixture.

- [ ] **Step 2: Run them and confirm they fail.**

`npx vitest run tests/unit/admin-schemas.test.ts tests/integration/threat-actions.test.ts tests/integration/admin-proxy.test.ts`

Expected: FAIL. `penalty` is still required, the scores route still exists, and the relayed body carries `penalty`.

- [ ] **Step 3: Implement.**

`src/admin/schemas.ts`: in `threatEventBody`, replace the `penalty` line and its comment with:

```ts
    // ACCEPTED AND IGNORED for one release (spec 2026-09-24, services'
    // remove-protection-score): it fed only the services protection score,
    // which no longer exists. The console stops sending it; drop this after.
    penalty: z.string().regex(/^\d+(\.\d{1,2})?$/).optional(),
```

Delete `userRefParams`, first confirming it has no other use with `grep -rn userRefParams src tests`.

`src/admin/routes.ts`:
- In the create closure, change `const { action, ...event } = body;` to `const { action, penalty: _ignoredPenalty, ...event } = body;`, with a one-line comment: "never forwarded — services no longer take it". If ESLint flags the unused binding, follow the repo's convention for an intentionally unused destructure; check `.eslintrc` for `argsIgnorePattern` / `varsIgnorePattern`.
- In the GET list map, strip it:

```ts
      events: events.map((e) => {
        // A services row from before their 2026-09-24 removal can still carry
        // `penalty`; it measured nothing a user sees, so the panel never gets it.
        const { penalty: _penalty, ...rest } = e as { event_id: string; penalty?: unknown };
        const action = actions.get(rest.event_id);
        return { ...rest, action: action === undefined ? null : toWire(action), action_completions: completions.get(rest.event_id) ?? 0 };
      }),
```

- Delete the `// ── scores` block, L546-559, and the `userRefParams` import.

In `src/admin/services-client.ts`, delete the `score()` interface line and implementation. In `src/admin/fakes.ts`, delete `score()`.

In `src/admin/persons/routes.ts`, change "the only per-person admin read is GET /v1/admin/scores/:userRef, which relays a services control-room number and takes a person UUID an agent has no way to obtain" to past tense: before this surface there was no per-person admin read support could use.

In `tests/helpers/idor.ts`, delete the `'GET /v1/admin/scores/:userRef'` entry and reword the ~L985 comment that uses it as an analogy.

- [ ] **Step 4: Run the tests, typecheck and lint.**

`npx vitest run tests/unit/admin-schemas.test.ts tests/integration/threat-actions.test.ts tests/integration/admin-proxy.test.ts tests/integration/score-threat.test.ts tests/integration/idor.test.ts`, then `npm run typecheck` and `npx eslint src/admin tests/helpers/idor.ts tests/integration/threat-actions.test.ts tests/integration/admin-proxy.test.ts tests/unit/admin-schemas.test.ts`.

Expected: PASS and clean.

- [ ] **Step 5: Commit.**

```bash
git add src/admin/routes.ts src/admin/services-client.ts src/admin/fakes.ts src/admin/schemas.ts src/admin/persons/routes.ts tests/helpers/idor.ts tests/integration/admin-proxy.test.ts tests/integration/score-threat.test.ts tests/integration/threat-actions.test.ts tests/unit/admin-schemas.test.ts
git commit -m "admin: drop the protection-score relay; threat penalty accepted, ignored, never forwarded

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

### Task B2: Backend docs, edited in place

**Files:** `docs/CONTROL-ROOM-API-SHAPES.md` (§4a, L677-744, especially L679-689, which currently says "Do not drop the `penalty` field"), `docs/CONTROL-ROOM-CONTRACT-THREAT-ACTIONS.md` (the §2 example body, L66 `"penalty": "2.00"`), `docs/CLAUDE.md` (§6, ~L265-282)

- [ ] **Step 1: Edit in place.**
  - `CONTROL-ROOM-API-SHAPES.md` §4a: replace the "`penalty` and `severity` are both real" passage and its two-row table with a dated note. **Since 2026-09-24 `penalty` is gone.** Severity alone sets what an event costs (×2, capped at 10). A `penalty` sent by an older panel is accepted and ignored for one release. The list no longer returns it. Add a one-line note that `GET /v1/admin/scores/{userRef}` was removed.
  - `CONTROL-ROOM-CONTRACT-THREAT-ACTIONS.md` §2: delete the `"penalty": "2.00",` line from the example body.
  - `docs/CLAUDE.md` §6: after the table, add one dated sentence. Services deleted their protection score on 2026-09-24, so the three views stay granted and unread with nothing writing them either, until a coordinated drop.

- [ ] **Step 2: Check.** `grep -n "penalty\|admin/scores" docs/CONTROL-ROOM-API-SHAPES.md docs/CONTROL-ROOM-CONTRACT-THREAT-ACTIONS.md docs/CLAUDE.md` should show only the dated notes.

- [ ] **Step 3: Commit.**

```bash
git add docs/CONTROL-ROOM-API-SHAPES.md docs/CONTROL-ROOM-CONTRACT-THREAT-ACTIONS.md docs/CLAUDE.md
git commit -m "docs: threat penalty and the scores relay are gone

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

### Task V: Verify both repos

- [ ] Services: `REQUIRE_DB=1 .venv/Scripts/python -m pytest tests/` (serially), `.venv/Scripts/python -m ruff check .`, `.venv/Scripts/python -m mypy src`.
- [ ] Backend: `npm run typecheck`, then `npx eslint` on every file the B-tasks touched, then `npx vitest run`. Compare the red set against the known baseline: `report-period` (6), `reports-weekly` (8) and `no-config-literals` (1) were red before this plan. Anything else red is this plan's.
- [ ] Report the commits in each repo. **Nothing is pushed or deployed by this plan.** Deploying is a separate step in the order services, then backend, then console (Global Constraints).
