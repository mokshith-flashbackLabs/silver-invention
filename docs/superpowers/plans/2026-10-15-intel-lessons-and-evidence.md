# Lessons and published evidence — services implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Services draft a lesson for every threat and protection they propose and standalone lessons in a seventh proposal kind (`lesson`), snapshot the evidence behind every approved event and lesson, and publish lessons, evidence and the week's research totals on three new `svc` views for the backend's weekly report.

**Architecture:** The propose step gains a THIRD structured call (`propose-v14`, `ProposalLessonsOutput`), because the two halves sit under the API's grammar ceiling and the size pin says "splitting further is the remedy". Code validates every lesson (bounds, the score page's word rules, every figure quoted, links only to vetted documents) in a new pure module, `intel/lessons.py`. Decisions write `intel_lessons` and snapshot evidence (`intel_published_owners` / `intel_published_evidence`) in the approval's own transaction; a signal retraction recomputes the snapshot's independence count. A new run kind, `lesson_draft`, drafts lessons for events already live. Migration 0055 adds the tables, widens the CHECKs and creates the three views.

**Tech Stack:** Python ≥3.11, FastAPI, psycopg 3 (async pool, raw SQL), pydantic 2 (`extra='forbid'`), structlog, pytest + pytest-postgresql (real Postgres 16), ruff, mypy strict.

**Spec:** `docs/superpowers/specs/2026-10-15-intel-lessons-and-evidence-design.md` (read it with this plan). Backend half: `image_backend/docs/superpowers/specs/2026-10-15-weekly-report-standout-design.md`.

## Global Constraints

- Repo: `image_flashbacklabs` only. Base branch `dev`; work on branch `feat/report-standout`, cut from `docs/report-standout` (which carries the spec and this plan).
- Migration: `0055_intel_lessons.up.sql` / `.down.sql`. Every new table is granted to `intel_rw` in the migration itself. The three views are granted `SELECT` to `imageshield_proxy_ro`.
- **0055's down DELETES, never refuses** (0053/0054 precedent): every DB test fixture runs `migrate down --all` on the shared database. *This corrects spec §11, which said the down refuses; Task 7 amends the spec.*
- Lesson bounds (spec §3), exactly: `title` ≤ 60; `intro` ≤ 400; 1–2 `sections`, each `kind` in `warning_signs | what_to_do | how_to_use` at most once, 1–5 `items` of ≤ 160; `link` `{url, label}` or null, `url` the `final_url` of a cited document **with `trust = 'listed'`**, `label` ≤ 40. Topic slug `^[a-z][a-z0-9_]{1,59}$`.
- Every lesson text passes `intel/user_copy.copy_problems` and `unquoted_figures` against the cited quotes. Refused at draft (dropped, counted) and at decision (`422 lesson_invalid` with `problems: [{field, rule}]`).
- No person data anywhere: no view, prompt, log line or audit row carries a `user_ref`.
- `propose-v13` → `propose-v14`. Each structured-output schema stays under 4,800 transformed characters (`tests/test_intel_model.py` pins every half, the new one included).
- Never `ruff format` an existing file; format NEW files only. Run `python -m mypy` (never the bare shim) and `ruff check`. Never add a second `-q` to pytest. One DB-backed pytest session at a time; use `REQUIRE_DB=1`.
- Do NOT run the whole suite after every task (owner preference): each task runs its own new test file once. Task 7 runs the affected files and reports what never ran.
- No per-task review subagents (owner preference). One final review is offered at the end, never run unasked.
- Commit trailer, exactly: `Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>`. No Claude trailer.
- `CLAUDE.md`, `PROXY_INTEGRATION.md`, `SCHEMA.md` are amended in place, never overwritten.

## Review Focus

1. **A proposal pass where the events half returns nothing parseable** (`events.output is None`): the lessons call still runs against the un-augmented payload, its `event_lessons` find no key and are dropped (`lesson_event_unknown`), and its general lessons are still validated. Pinned in Task 3's model test.
2. **A threat approved with `values.lesson: null`** writes the event and NO lesson, and snapshots the event's evidence anyway; a later `lesson_draft` run may draft one for it. Pinned in Task 4.
3. **A signal retracted after approval** that was an event's ONLY evidence: the event's evidence rows leave `svc.v_evidence` (joined on active signals) and `independent_sources` drops to 0; the event itself stays live. Pinned in Task 5.
4. **A lesson whose link is a web-search (unvetted) page**: at draft the link is dropped and the lesson kept (`lesson_link_not_vetted`); at decision an operator-typed link to it is `422 lesson_invalid` with `{field: "link", rule: "link_not_vetted"}`. Pinned in Tasks 2 and 4.
5. **A protection renewal approved without touching the lesson** carries the renewed credit's lesson to the new event (a new `intel_lessons` row on the new event id), and `values.lesson: null` on the renewal drops it. Pinned in Task 4.

---

## File map

| File | Responsibility |
|---|---|
| `migrations/0055_intel_lessons.up.sql` / `.down.sql` | Kind + run-kind CHECKs; `intel_lessons`, `intel_published_owners`, `intel_published_evidence`; the three `svc` views |
| `src/imageshield/http/svc_contract.py` | The three views' shapes for `/readyz` |
| `src/imageshield/intel/bounds.py` | Lesson bounds |
| `src/imageshield/intel/proposal_models.py` | `LessonDraft` and friends; `lesson` on threat/protection suggested/values; `LessonTarget/Suggested/Decided/Values`; `DecisionRefused.problems` |
| `src/imageshield/intel/schemas.py` | Model output: `ProposedLesson…`, `ProposalLessonsOutput`; `ProposalOutput` gains `event_lessons`, `lessons` |
| `src/imageshield/intel/lessons.py` (new) | Pure: `lesson_from`, `lesson_problems`, `general_lesson`; the `lesson_draft` run |
| `src/imageshield/intel/lesson_store.py` (new) | SQL: insert/retire/list lessons, evidence snapshot, independence recompute, open topics, events needing lessons, queue a draft run |
| `src/imageshield/intel/generation.py` | Event lessons by key; general lessons; `GeneratedBatch.lessons` |
| `src/imageshield/intel/prompts.py` | `propose-v14`, lesson rules, `PROPOSE_LESSONS_PART`, `with_proposed_events`, `open_lesson_topics`, `lesson_draft_request` |
| `src/imageshield/intel/model.py`, `stub.py` | Third call in `propose`; `draft_lessons` |
| `src/imageshield/intel/proposal_store.py` | Topic re-check under the write lock; `open_lesson_topics` |
| `src/imageshield/intel/pipeline.py` | Pass open topics; dispatch `lesson_draft` |
| `src/imageshield/intel/approvable.py` | `lesson` decidable |
| `src/imageshield/intel/decisions.py` | Lesson approval; event lessons; renewal carry-forward; evidence snapshot |
| `src/imageshield/intel/evidence_store.py` | Retraction recomputes the snapshot |
| `src/imageshield/intel/backfill_evidence.py` (new) | One-shot CLI: snapshot evidence for events approved before 0055 |
| `src/imageshield/http/models.py`, `routes/admin_intel.py` | Kind/run-kind literals; `lesson_invalid`; lessons list, retire, lesson-drafts |
| `tests/intel_fakes.py` | `FakeModel.draft_lessons`; `seed_lesson` |
| `tests/test_intel_lessons_*.py` (new) | Per task |
| docs | `CLAUDE.md` §3/§6, `PROXY_INTEGRATION.md` §6, `SCHEMA.md` §2c, the spec's corrections |

---

### Task 0: Implementation worktree

**Files:** none (environment).

- [ ] **Step 1: Create the worktree from the docs branch**

```bash
cd "C:/Users/Mokshith work/Project/imageShield/image_flashbacklabs"
git fetch -q origin
git worktree add -b feat/report-standout C:/tmp/svc-standout docs/report-standout
cd C:/tmp/svc-standout
git rebase origin/dev
```

- [ ] **Step 2: Point the shared venv at THIS worktree's code**

The main checkout's `.venv` has an editable install of the main checkout's `src`. `PYTHONPATH=src` puts the worktree's `src` first, so every command below is run as:

```bash
VENV="C:/Users/Mokshith work/Project/imageShield/image_flashbacklabs/.venv/Scripts/python.exe"
PYTHONPATH=src REQUIRE_DB=1 "$VENV" -m pytest tests/test_intel_user_copy.py
PYTHONPATH=src "$VENV" -c "import imageshield, sys; print(imageshield.__file__)"
```

Expected: the test file passes, and the printed path is under `C:\tmp\svc-standout\src`. If it prints the main checkout's path, stop: every later test would test the wrong code.

---

### Task 1: Migration 0055 and the readiness shapes

**Files:**
- Create: `migrations/0055_intel_lessons.up.sql`, `migrations/0055_intel_lessons.down.sql`, `tests/test_intel_lessons_schema.py`
- Modify: `src/imageshield/http/svc_contract.py` (the shapes dict, after `v_active_scoped_events`)

**Interfaces:**
- Produces tables `intel_lessons`, `intel_published_owners`, `intel_published_evidence`; views `svc.v_evidence`, `svc.v_lessons`, `svc.v_intel_weekly_totals` (columns exactly as below — the backend's contract pins them).

- [ ] **Step 1: Write the failing schema test**

```python
"""Migration 0055 (spec 2026-10-15-intel-lessons-and-evidence §8): the lesson and evidence tables,
the widened CHECKs and the three svc views, read as the proxy reads them."""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest

from tests.db import run_migrate

VIEWS = {
    "v_evidence": [
        "owner_kind", "owner_id", "document_id", "publisher_domain", "document_title",
        "published_at", "excerpt_id", "quote", "link_url", "independent_sources", "approved_at",
    ],
    "v_lessons": [
        "lesson_id", "event_id", "kind", "topic", "title", "intro", "sections", "link_url",
        "link_label", "tags", "is_global", "approved_at",
    ],
    "v_intel_weekly_totals": [
        "week_start", "documents_read", "publishers", "risks_found", "proposed", "approved",
        "rejected",
    ],
}


def test_views_have_exactly_the_contract_columns(intel_db: str) -> None:
    with psycopg.connect(intel_db) as conn:
        for view, columns in VIEWS.items():
            rows = conn.execute(
                "SELECT a.attname FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid"
                " JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = 'svc' AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped"
                " ORDER BY a.attnum",
                (view,),
            ).fetchall()
            assert [r[0] for r in rows] == columns, view


def test_views_are_granted_to_the_proxy_and_carry_no_user_ref(intel_db: str) -> None:
    with psycopg.connect(intel_db) as conn:
        for view, columns in VIEWS.items():
            granted = conn.execute(
                "SELECT has_table_privilege('imageshield_proxy_ro', %s, 'SELECT')", (f"svc.{view}",)
            ).fetchone()
            assert granted == (True,), view
            assert "user_ref" not in columns and "person_ref" not in columns


def test_kind_and_run_kind_checks_admit_lessons(intel_db: str) -> None:
    with psycopg.connect(intel_db) as conn:
        conn.execute(
            "INSERT INTO intel_runs (kind, request, requested_by) VALUES ('lesson_draft', '{}', 'op')"
        )
        conn.execute(
            "INSERT INTO intel_proposals (kind, status, target, suggested, rationale, model_id,"
            " prompt_version) VALUES ('lesson', 'pending', '{\"topic\": \"spot_sextortion\"}',"
            " '{}', 'r', 'm', 'propose-v14')"
        )


def test_a_general_lesson_needs_a_topic_and_no_event(intel_db: str) -> None:
    with psycopg.connect(intel_db) as conn:
        proposal = conn.execute(
            "INSERT INTO intel_proposals (kind, status, target, suggested, rationale, model_id,"
            " prompt_version) VALUES ('lesson', 'pending', '{}', '{}', 'r', 'm', 'v')"
            " RETURNING proposal_id"
        ).fetchone()
        assert proposal is not None
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO intel_lessons (proposal_id, event_id, kind, topic, title, intro,"
                " sections, approved_by) VALUES (%s, %s, 'general', 'x_topic', 't', 'i', '[]', 'op')",
                (proposal[0], uuid4()),
            )


def test_0055_down_and_up_again(intel_db: str) -> None:
    down = run_migrate(intel_db, "down", "1")
    assert down.returncode == 0, down.stderr
    up = run_migrate(intel_db, "up")
    assert up.returncode == 0, up.stderr
```

- [ ] **Step 2: Run it to verify it fails**

Run: `PYTHONPATH=src REQUIRE_DB=1 "$VENV" -m pytest tests/test_intel_lessons_schema.py`
Expected: FAIL (`v_evidence` absent: empty column list; the CHECK refuses `lesson_draft`).

- [ ] **Step 3: Write `migrations/0055_intel_lessons.up.sql`**

```sql
-- 0055 -- likeness intel: lessons and published evidence for the weekly report (spec
-- docs/superpowers/specs/2026-10-15-intel-lessons-and-evidence-design.md section 8).
--
-- A seventh proposal kind, lesson, and a run kind, lesson_draft. intel_lessons holds every approved
-- lesson: an event's (drafted with it) or a general one. The evidence an operator approved on is
-- SNAPSHOTTED at the decision (intel_published_owners / intel_published_evidence): the independence
-- count is Python (intel/corroboration.py) and cannot be a view expression. Three read-only views
-- publish lessons, evidence and the week's research totals to the backend. Events, never people.

-- The two intel_proposals CHECKs, found by name as 0054 named them.
ALTER TABLE intel_proposals DROP CONSTRAINT intel_proposals_kind_check;
ALTER TABLE intel_proposals ADD CONSTRAINT intel_proposals_kind_check
  CHECK (kind IN ('weight_change', 'threat_event', 'protection_event', 'weight_suggestion',
                  'coverage_gap', 'quiz_question', 'lesson'));
ALTER TABLE intel_proposals DROP CONSTRAINT intel_proposals_decided_check;
ALTER TABLE intel_proposals ADD CONSTRAINT intel_proposals_decided_check
  CHECK (NOT (status IN ('approved', 'applied')
              AND kind IN ('weight_change', 'threat_event', 'protection_event', 'quiz_question',
                           'lesson'))
         OR decided IS NOT NULL);

ALTER TABLE intel_runs DROP CONSTRAINT intel_runs_kind_valid;
ALTER TABLE intel_runs ADD CONSTRAINT intel_runs_kind_valid
  CHECK (kind IN ('source_check', 'discovery', 'adhoc_url', 'weight_suggestion', 'renewal_check',
                  'gap_regenerate', 'source_proposal', 'source_validation', 'lesson_draft'));

CREATE TABLE intel_lessons (
  lesson_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  proposal_id   UUID NOT NULL REFERENCES intel_proposals (proposal_id),
  -- A threat_events or protection_events id (two tables, so no FK), or NULL for a general lesson.
  event_id      UUID,
  kind          TEXT NOT NULL CHECK (kind IN ('threat', 'protection', 'general')),
  topic         TEXT CHECK (topic ~ '^[a-z][a-z0-9_]{1,59}$'),
  title         TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 60),
  intro         TEXT NOT NULL CHECK (length(intro) BETWEEN 1 AND 400),
  sections      JSONB NOT NULL CHECK (jsonb_typeof(sections) = 'array'),
  link_url      TEXT CHECK (link_url IS NULL OR link_url LIKE 'https://%'),
  link_label    TEXT CHECK (link_label IS NULL OR length(link_label) BETWEEN 1 AND 40),
  tags          TEXT[] NOT NULL DEFAULT '{}' CHECK (intel_tags_well_formed(tags)),
  is_global     BOOLEAN NOT NULL DEFAULT false,
  approved_by   TEXT NOT NULL CHECK (approved_by <> ''),
  approved_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  retired_by    TEXT,
  retired_at    TIMESTAMPTZ,
  retire_reason TEXT,
  CHECK ((kind = 'general') = (event_id IS NULL)),
  CHECK ((kind = 'general') = (topic IS NOT NULL)),
  CHECK ((link_url IS NULL) = (link_label IS NULL)),
  CHECK ((retired_at IS NULL) = (retired_by IS NULL)),
  CHECK ((retired_at IS NULL) = (retire_reason IS NULL))
);
CREATE UNIQUE INDEX intel_lessons_one_live_per_event ON intel_lessons (event_id)
  WHERE event_id IS NOT NULL AND retired_at IS NULL;
CREATE UNIQUE INDEX intel_lessons_one_live_per_topic ON intel_lessons (topic)
  WHERE topic IS NOT NULL AND retired_at IS NULL;

CREATE TABLE intel_published_owners (
  owner_kind          TEXT NOT NULL CHECK (owner_kind IN ('event', 'lesson')),
  owner_id            UUID NOT NULL,
  proposal_id         UUID NOT NULL REFERENCES intel_proposals (proposal_id),
  independent_sources INT NOT NULL CHECK (independent_sources >= 0),
  approved_at         TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (owner_kind, owner_id)
);

CREATE TABLE intel_published_evidence (
  owner_kind TEXT NOT NULL,
  owner_id   UUID NOT NULL,
  signal_id  UUID NOT NULL REFERENCES intel_signals (signal_id),
  excerpt_id UUID NOT NULL REFERENCES intel_excerpts (excerpt_id),
  PRIMARY KEY (owner_kind, owner_id, excerpt_id),
  FOREIGN KEY (owner_kind, owner_id) REFERENCES intel_published_owners (owner_kind, owner_id)
);
CREATE INDEX intel_published_evidence_signal_idx ON intel_published_evidence (signal_id);

-- Each new table its own grant (the migration-grant trap). No DELETE, ever.
GRANT SELECT, INSERT, UPDATE ON intel_lessons TO intel_rw;
GRANT SELECT, INSERT, UPDATE ON intel_published_owners TO intel_rw;
GRANT SELECT, INSERT ON intel_published_evidence TO intel_rw;

-- Evidence: only signals still active. A retracted signal's rows leave the view at once; the
-- retraction recomputes independent_sources (intel/evidence_store.py). link_url is a vetted
-- document's URL and NULL for every web-search find (spec D3).
CREATE VIEW svc.v_evidence AS
SELECT o.owner_kind,
       o.owner_id,
       d.document_id,
       d.publisher_domain,
       d.title                                              AS document_title,
       d.published_at,
       x.excerpt_id,
       x.quote_text                                         AS quote,
       CASE WHEN d.trust = 'listed' THEN d.final_url END    AS link_url,
       o.independent_sources,
       o.approved_at
  FROM intel_published_owners o
  JOIN intel_published_evidence pe ON pe.owner_kind = o.owner_kind AND pe.owner_id = o.owner_id
  JOIN intel_signals s ON s.signal_id = pe.signal_id AND s.status = 'active'
  JOIN intel_excerpts x ON x.excerpt_id = pe.excerpt_id
  JOIN intel_documents d ON d.document_id = s.document_id;

-- Live lessons only: not retired, and an event's lesson only while its event is live.
CREATE VIEW svc.v_lessons AS
SELECT l.lesson_id, l.event_id, l.kind, l.topic, l.title, l.intro, l.sections, l.link_url,
       l.link_label, l.tags, l.is_global, l.approved_at
  FROM intel_lessons l
 WHERE l.retired_at IS NULL
   AND (l.event_id IS NULL
        OR EXISTS (SELECT 1 FROM threat_events t
                    WHERE t.event_id = l.event_id AND t.status = 'active'
                      AND t.starts_at <= now() AND t.expires_at > now())
        OR EXISTS (SELECT 1 FROM protection_events p
                    WHERE p.event_id = l.event_id AND p.status = 'active'
                      AND p.starts_at <= now() AND p.review_by > now()));

-- Counts per Mon-Sun week (UTC). No title, no URL, no operator.
CREATE VIEW svc.v_intel_weekly_totals AS
WITH docs AS (
  SELECT date_trunc('week', fetched_at AT TIME ZONE 'UTC')::date AS week_start,
         count(*)::int AS documents_read,
         count(DISTINCT publisher_domain)::int AS publishers
    FROM intel_documents GROUP BY 1
), risks AS (
  SELECT date_trunc('week', created_at AT TIME ZONE 'UTC')::date AS week_start,
         count(*)::int AS risks_found
    FROM intel_signals WHERE status = 'active' AND direction = 'risk_up' GROUP BY 1
), proposed AS (
  SELECT date_trunc('week', created_at AT TIME ZONE 'UTC')::date AS week_start,
         count(*)::int AS proposed
    FROM intel_proposals WHERE kind IN ('threat_event', 'protection_event', 'lesson') GROUP BY 1
), decided AS (
  SELECT date_trunc('week', decided_at AT TIME ZONE 'UTC')::date AS week_start,
         count(*) FILTER (WHERE status IN ('approved', 'applied'))::int AS approved,
         count(*) FILTER (WHERE status = 'rejected')::int AS rejected
    FROM intel_proposals
   WHERE kind IN ('threat_event', 'protection_event', 'lesson') AND decided_at IS NOT NULL
   GROUP BY 1
), weeks AS (
  SELECT week_start FROM docs UNION SELECT week_start FROM risks
  UNION SELECT week_start FROM proposed UNION SELECT week_start FROM decided
)
SELECT w.week_start,
       coalesce(docs.documents_read, 0) AS documents_read,
       coalesce(docs.publishers, 0)     AS publishers,
       coalesce(risks.risks_found, 0)   AS risks_found,
       coalesce(proposed.proposed, 0)   AS proposed,
       coalesce(decided.approved, 0)    AS approved,
       coalesce(decided.rejected, 0)    AS rejected
  FROM weeks w
  LEFT JOIN docs ON docs.week_start = w.week_start
  LEFT JOIN risks ON risks.week_start = w.week_start
  LEFT JOIN proposed ON proposed.week_start = w.week_start
  LEFT JOIN decided ON decided.week_start = w.week_start;

GRANT SELECT ON svc.v_evidence, svc.v_lessons, svc.v_intel_weekly_totals TO imageshield_proxy_ro;
```

- [ ] **Step 4: Write `migrations/0055_intel_lessons.down.sql`**

```sql
-- Reverses 0055. DELETES rather than refuses (0053/0054 precedent): every DB test fixture migrates
-- the shared database down, and a rollback must need no hand-deletion. The lesson proposals and
-- their evidence links go; the signals and the events stay. A queued lesson_draft run would
-- otherwise run after the rollback and spend budget, so lesson_draft runs go too.
DROP VIEW IF EXISTS svc.v_intel_weekly_totals;
DROP VIEW IF EXISTS svc.v_lessons;
DROP VIEW IF EXISTS svc.v_evidence;
DROP TABLE intel_published_evidence;
DROP TABLE intel_published_owners;
DROP TABLE intel_lessons;
DELETE FROM intel_proposal_signals
 WHERE proposal_id IN (SELECT proposal_id FROM intel_proposals WHERE kind = 'lesson');
DELETE FROM intel_proposals WHERE kind = 'lesson';
DELETE FROM intel_run_events WHERE run_id IN (SELECT run_id FROM intel_runs WHERE kind = 'lesson_draft');
DELETE FROM intel_runs WHERE kind = 'lesson_draft';
ALTER TABLE intel_runs DROP CONSTRAINT intel_runs_kind_valid;
ALTER TABLE intel_runs ADD CONSTRAINT intel_runs_kind_valid
  CHECK (kind IN ('source_check', 'discovery', 'adhoc_url', 'weight_suggestion', 'renewal_check',
                  'gap_regenerate', 'source_proposal', 'source_validation'));
ALTER TABLE intel_proposals DROP CONSTRAINT intel_proposals_decided_check;
ALTER TABLE intel_proposals DROP CONSTRAINT intel_proposals_kind_check;
ALTER TABLE intel_proposals ADD CONSTRAINT intel_proposals_kind_check
  CHECK (kind IN ('weight_change', 'threat_event', 'protection_event', 'weight_suggestion',
                  'coverage_gap', 'quiz_question'));
ALTER TABLE intel_proposals ADD CONSTRAINT intel_proposals_decided_check
  CHECK (NOT (status IN ('approved', 'applied')
              AND kind IN ('weight_change', 'threat_event', 'protection_event', 'quiz_question'))
         OR decided IS NOT NULL);
```

Before writing it, confirm the run-events table name: `grep -n "CREATE TABLE intel_run_events" migrations/0046_intel_run_events.up.sql` (expected: present). If a later migration renamed it, use that name.

- [ ] **Step 5: Add the three shapes to `src/imageshield/http/svc_contract.py`** (after `"v_active_scoped_events": {...}`, inside the same dict)

```python
    # ── 0055: the weekly report's lessons and evidence (spec 2026-10-15) ───────────
    # Events and lessons, never people. The backend reads all three as optional.
    "v_evidence": {
        "owner_kind": "text",
        "owner_id": "uuid",
        "document_id": "uuid",
        "publisher_domain": "text",
        "document_title": "text",
        "published_at": "timestamp with time zone",
        "excerpt_id": "uuid",
        "quote": "text",
        "link_url": "text",
        "independent_sources": "integer",
        "approved_at": "timestamp with time zone",
    },
    "v_lessons": {
        "lesson_id": "uuid",
        "event_id": "uuid",
        "kind": "text",
        "topic": "text",
        "title": "text",
        "intro": "text",
        "sections": "jsonb",
        "link_url": "text",
        "link_label": "text",
        "tags": "text[]",
        "is_global": "boolean",
        "approved_at": "timestamp with time zone",
    },
    "v_intel_weekly_totals": {
        "week_start": "date",
        "documents_read": "integer",
        "publishers": "integer",
        "risks_found": "integer",
        "proposed": "integer",
        "approved": "integer",
        "rejected": "integer",
    },
```

- [ ] **Step 6: Run the test and the readiness test**

Run: `PYTHONPATH=src REQUIRE_DB=1 "$VENV" -m pytest tests/test_intel_lessons_schema.py tests/test_svc_contract.py`
Expected: PASS. (If `tests/test_svc_contract.py` has another name, find it with `grep -rln "check_svc_contract" tests`.)

- [ ] **Step 7: Commit**

```bash
git add migrations/0055_intel_lessons.up.sql migrations/0055_intel_lessons.down.sql src/imageshield/http/svc_contract.py tests/test_intel_lessons_schema.py
git commit -m "feat(intel): 0055 — lessons, published evidence and three svc views

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

### Task 2: The lesson shape and its rules (pure)

**Files:**
- Modify: `src/imageshield/intel/bounds.py`, `src/imageshield/intel/proposal_models.py`, `src/imageshield/intel/schemas.py`
- Create: `src/imageshield/intel/lessons.py`, `tests/test_intel_lessons_rules.py`

**Interfaces:**
- Produces in `proposal_models.py`: `LessonSection`, `LessonLink`, `LessonDraft`, `LessonTarget`, `LessonSuggested`, `LessonDecided`, `LessonValues`; `ThreatEventSuggested.lesson`, `ThreatEventValues.lesson`, `ProtectionEventSuggested.lesson`, `ProtectionEventValues.lesson` (all `LessonDraft | None = None`); `NewProposal.kind` admits `"lesson"`; `DecisionRefusal` admits `"lesson_invalid"`; `DecisionRefused(..., problems=...)`.
- Produces in `schemas.py`: `ProposedLessonSection`, `ProposedLesson`, `ProposedEventLesson`, `ProposedGeneralLesson`, `ProposalLessonsOutput`; `ProposalOutput.event_lessons`, `ProposalOutput.lessons`.
- Produces in `lessons.py`: `lesson_problems(draft: LessonDraft, cited: Sequence[ContextSignal]) -> list[dict[str, str]]`, `lesson_from(item: ProposedLesson | None, cited: Sequence[ContextSignal], counts: Counter[str]) -> dict[str, Any] | None`, `general_lesson(item, context, vocabulary, counts, taken_topics) -> NewProposal | None`.

- [ ] **Step 1: Write the failing tests**

```python
"""A lesson's shape and rules (spec 2026-10-15-intel-lessons-and-evidence §3, §5)."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from imageshield.intel.lessons import general_lesson, lesson_from, lesson_problems
from imageshield.intel.proposal_models import ContextSignal, LessonDraft
from imageshield.intel.recency import Recency
from imageshield.intel.schemas import (
    ProposedGeneralLesson,
    ProposedLesson,
    ProposedLessonSection,
)
from imageshield.search.urlhash import url_hash
from tests.intel_fakes import NOW, scoring

V = scoring()
VETTED = "https://help.example.com/remove-ai-video"
WEB = "https://blog.example.net/story"


def _signal(url: str, trust: str, quote: str, publisher: str = "example.com") -> ContextSignal:
    return ContextSignal(
        signal_id=uuid4(),
        category="protection",
        direction="risk_down",
        tags=("instagram",),
        unregistered_subjects=(),
        summary="s",
        trust=trust,  # type: ignore[arg-type]
        publisher_domain=publisher,
        status="active",
        created_at=NOW - timedelta(days=1),
        document_key=url_hash(url),
        excerpts=(quote,),
        document_url=url,
    )


CITED = [
    _signal(VETTED, "listed", "Requests are reviewed within 48 hours by the privacy team."),
    _signal(WEB, "web", "Scammers send a link to a fake sign-in page.", publisher="example.net"),
]


def _lesson(**over: object) -> ProposedLesson:
    fields: dict[str, object] = {
        "title": "Ask for an AI video of you to be removed",
        "intro": "A platform can take down a realistic AI video of your face after a privacy request.",
        "sections": [
            ProposedLessonSection(kind="how_to_use", items=["Open the privacy complaint form.", "Name the video."])
        ],
        "link_url": VETTED,
        "link_label": "Privacy complaint form",
    }
    fields.update(over)
    return ProposedLesson.model_validate(fields)


def test_a_clean_lesson_is_kept_with_its_vetted_link() -> None:
    counts: Counter[str] = Counter()
    kept = lesson_from(_lesson(), CITED, counts)
    assert kept is not None
    assert kept["link"] == {"url": VETTED, "label": "Privacy complaint form"}
    assert counts == Counter()


def test_a_link_to_an_unvetted_page_is_dropped_and_the_lesson_kept() -> None:
    counts: Counter[str] = Counter()
    kept = lesson_from(_lesson(link_url=WEB), CITED, counts)
    assert kept is not None and kept["link"] is None
    assert counts["lesson_link_not_vetted"] == 1


def test_a_figure_no_cited_quote_carries_drops_the_lesson() -> None:
    counts: Counter[str] = Counter()
    assert lesson_from(_lesson(intro="Requests are reviewed within 24 hours."), CITED, counts) is None
    assert counts["lesson_dropped_forbidden_words"] == 1


def test_a_quoted_figure_is_allowed() -> None:
    assert lesson_from(_lesson(intro="Requests are reviewed within 48 hours."), CITED, Counter()) is not None


@pytest.mark.parametrize(
    "intro",
    ["You are safe once it is removed.", "If your photo was found, ask for removal.", "Visit www.example.com now."],
)
def test_reassurance_a_finding_or_an_address_drops_the_lesson(intro: str) -> None:
    counts: Counter[str] = Counter()
    assert lesson_from(_lesson(intro=intro), CITED, counts) is None


def test_out_of_bounds_drops_the_lesson_never_truncates() -> None:
    counts: Counter[str] = Counter()
    assert lesson_from(_lesson(title="x" * 61), CITED, counts) is None
    assert counts["lesson_dropped_out_of_bounds"] == 1


def test_a_missing_lesson_is_counted() -> None:
    counts: Counter[str] = Counter()
    assert lesson_from(None, CITED, counts) is None
    assert counts["lesson_missing"] == 1


def test_a_section_kind_may_appear_once() -> None:
    with pytest.raises(ValidationError):
        LessonDraft.model_validate(
            {
                "title": "t",
                "intro": "i",
                "sections": [{"kind": "what_to_do", "items": ["a"]}, {"kind": "what_to_do", "items": ["b"]}],
            }
        )


def test_lesson_problems_names_the_field_and_the_rule() -> None:
    draft = LessonDraft.model_validate(
        {
            "title": "Stay safe",
            "intro": "Requests take 9 days.",
            "sections": [{"kind": "what_to_do", "items": ["Report it."]}],
            "link": {"url": WEB, "label": "Read"},
        }
    )
    problems = lesson_problems(draft, CITED)
    assert {"field": "title", "rule": "reassurance"} in problems
    assert {"field": "intro", "rule": "figure_not_quoted"} in problems
    assert {"field": "link", "rule": "link_not_vetted"} in problems


def test_a_general_lesson_becomes_a_lesson_proposal() -> None:
    counts: Counter[str] = Counter()
    context = {s.signal_id: s for s in CITED}
    proposal = general_lesson(
        ProposedGeneralLesson(
            topic="remove_ai_videos",
            tags=["instagram"],
            is_global=False,
            lesson=_lesson(),
            rationale="Two publishers describe the removal route.",
            signal_ids=[str(s.signal_id) for s in CITED],
        ),
        context,
        V,
        counts,
        taken_topics=set(),
        recency=Recency(NOW, 90),
    )
    assert proposal is not None and proposal.kind == "lesson"
    assert proposal.target == {"topic": "remove_ai_videos"}
    assert proposal.suggested["tags"] == ["instagram"] and proposal.suggested["is_global"] is False


def test_a_repeated_topic_and_an_unscoped_lesson_are_dropped() -> None:
    counts: Counter[str] = Counter()
    context = {s.signal_id: s for s in CITED}
    item = ProposedGeneralLesson(
        topic="remove_ai_videos", lesson=_lesson(), rationale="r",
        signal_ids=[str(s.signal_id) for s in CITED],
    )
    fresh = Recency(NOW, 90)
    assert general_lesson(item, context, V, counts, taken_topics={"remove_ai_videos"}, recency=fresh) is None
    assert counts["proposal_dropped_lesson_repeat"] == 1
    assert general_lesson(item, context, V, counts, taken_topics=set(), recency=fresh) is None
    assert counts["proposal_dropped_lesson_unscoped"] == 1


def test_a_general_lesson_on_stale_evidence_is_dropped() -> None:
    counts: Counter[str] = Counter()
    old = [
        replace(s, published_at=NOW - timedelta(days=400)) for s in CITED
    ]
    item = ProposedGeneralLesson(
        topic="remove_ai_videos", tags=["instagram"], lesson=_lesson(), rationale="r",
        signal_ids=[str(s.signal_id) for s in old],
    )
    assert general_lesson(
        item, {s.signal_id: s for s in old}, V, counts, taken_topics=set(), recency=Recency(NOW, 90)
    ) is None
    assert counts["proposal_dropped_evidence_stale"] == 1
```

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHONPATH=src "$VENV" -m pytest tests/test_intel_lessons_rules.py`
Expected: FAIL with `ModuleNotFoundError: imageshield.intel.lessons`.

- [ ] **Step 3: Add the bounds** (end of `src/imageshield/intel/bounds.py`)

```python
# ── lessons (spec 2026-10-15-intel-lessons-and-evidence §3) ──────────────────
# A lesson is user-facing copy once approved: dropped past a bound, never truncated. The backend's
# report carries the same numbers (image_backend src/reports/contents/types.ts).
MAX_LESSON_TITLE_CHARS = 60
MAX_LESSON_INTRO_CHARS = 400
MAX_LESSON_ITEM_CHARS = 160
MAX_LESSON_ITEMS = 5
MAX_LESSON_SECTIONS = 2
MAX_LESSON_LINK_CHARS = 500
MAX_LESSON_LINK_LABEL_CHARS = 40
LESSON_TOPIC_PATTERN = r"^[a-z][a-z0-9_]{1,59}$"
# Open lesson topics a generation call is shown, newest first. A prompt bound, never a rule.
MAX_PROMPT_LESSON_TOPICS = 200
# Events one lesson_draft run drafts for; a run named with more ids is refused at the route.
MAX_LESSON_DRAFT_EVENTS = 20
```

- [ ] **Step 4: Add the models to `src/imageshield/intel/proposal_models.py`**

Add to the `bounds` import: `LESSON_TOPIC_PATTERN, MAX_LESSON_INTRO_CHARS, MAX_LESSON_ITEM_CHARS, MAX_LESSON_ITEMS, MAX_LESSON_LINK_CHARS, MAX_LESSON_LINK_LABEL_CHARS, MAX_LESSON_SECTIONS, MAX_LESSON_TITLE_CHARS`. Then, directly after `ThreatActionSuggested`:

```python
LessonSectionKind = Literal["warning_signs", "what_to_do", "how_to_use"]


def _lesson_text(value: str, limit: int) -> str:
    text = normalise(value)
    if not text or len(text) > limit:
        raise ValueError(f"must be 1 to {limit} characters")
    return text


class LessonSection(_Stored):
    kind: LessonSectionKind
    items: tuple[str, ...] = Field(min_length=1, max_length=MAX_LESSON_ITEMS)

    @field_validator("items")
    @classmethod
    def _items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_lesson_text(item, MAX_LESSON_ITEM_CHARS) for item in value)


class LessonLink(_Stored):
    url: str = Field(max_length=MAX_LESSON_LINK_CHARS)
    label: str

    @field_validator("url")
    @classmethod
    def _https(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("a link must be https")
        return value

    @field_validator("label")
    @classmethod
    def _label(cls, value: str) -> str:
        return _lesson_text(value, MAX_LESSON_LINK_LABEL_CHARS)


class LessonDraft(_Stored):
    """One lesson, wherever it comes from (spec 2026-10-15-intel-lessons-and-evidence §3). The
    section headings are not stored here: the backend's playbook words them."""

    title: str
    intro: str
    sections: tuple[LessonSection, ...] = Field(min_length=1, max_length=MAX_LESSON_SECTIONS)
    link: LessonLink | None = None

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        return _lesson_text(value, MAX_LESSON_TITLE_CHARS)

    @field_validator("intro")
    @classmethod
    def _intro(cls, value: str) -> str:
        return _lesson_text(value, MAX_LESSON_INTRO_CHARS)

    @model_validator(mode="after")
    def _distinct_sections(self) -> LessonDraft:
        kinds = [s.kind for s in self.sections]
        if len(set(kinds)) != len(kinds):
            raise ValueError("each section kind appears at most once")
        return self
```

Add `lesson: LessonDraft | None = None` as the LAST field of `ThreatEventSuggested`, `ThreatEventValues`, `ProtectionEventSuggested` and `ProtectionEventValues` (the decided models inherit it). Then, after `QuizQuestionValues`:

```python
class LessonTarget(_Stored):
    """What a lesson proposal is about (spec §5): a topic for a general lesson, or the live event a
    ``lesson_draft`` run drafted it for. Exactly one."""

    topic: str | None = Field(default=None, pattern=LESSON_TOPIC_PATTERN)
    event_id: UUID | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> LessonTarget:
        if (self.topic is None) == (self.event_id is None):
            raise ValueError("a lesson is about a topic or an event, exactly one")
        return self


class LessonSuggested(_Stored):
    lesson: LessonDraft
    tags: tuple[str, ...] = ()
    is_global: StrictBool = False

    @field_validator("tags")
    @classmethod
    def _slugs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _distinct_slugs(value)


class LessonDecided(LessonSuggested):
    """The exact lesson an approval stores and inserts into intel_lessons."""


class LessonValues(_Stored):
    """An operator's edit of a lesson approval: any subset of ``{lesson, tags, is_global}``. The
    lesson is replaced whole."""

    lesson: LessonDraft | None = None
    tags: tuple[str, ...] | None = None
    is_global: StrictBool | None = None
```

Widen `NewProposal.kind` to include `"lesson"`. Add `"lesson_invalid"` to `DecisionRefusal`. Give `DecisionRefused` a `problems` keyword:

```python
class DecisionRefused(Exception):
    """A decision the transaction refused. ``code`` is the §4.7 error code, verbatim, ``slugs``
    names the offending tags of ``unknown_tag`` / ``tag_retired`` (§3.1), and ``problems`` names
    each field and rule of a ``lesson_invalid`` (spec 2026-10-15 §3)."""

    def __init__(
        self,
        code: DecisionRefusal,
        message: str,
        *,
        slugs: tuple[str, ...] = (),
        problems: tuple[dict[str, str], ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.slugs = slugs
        self.problems = problems
```

- [ ] **Step 5: Add the model-output schemas to `src/imageshield/intel/schemas.py`** (after `ProposedQuizQuestion`)

```python
class ProposedLessonSection(_Out):
    kind: Literal["warning_signs", "what_to_do", "how_to_use"]
    items: list[str] = Field(default_factory=list)


class ProposedLesson(_Out):
    """propose-v14 (spec 2026-10-15-intel-lessons-and-evidence §3): a lesson as the model drafts it.
    No bounds here, for ProposedWeightChange's reason: intel/lessons.py checks them and drops the
    lesson, never the event it belongs to."""

    title: str
    intro: str
    sections: list[ProposedLessonSection] = Field(default_factory=list)
    link_url: str | None = None
    link_label: str | None = None


class ProposedEventLesson(_Out):
    """``event`` is the key the call was shown: T1, P1… for an event proposed in this run, or an
    event id in a lesson_draft run."""

    event: str
    lesson: ProposedLesson


class ProposedGeneralLesson(_Out):
    topic: str
    tags: list[str] = Field(default_factory=list)
    is_global: bool = False
    lesson: ProposedLesson
    rationale: str
    signal_ids: list[str] = Field(default_factory=list)


class ProposalLessonsOutput(_Out):
    """propose-v14: the lessons call, the proposal step's third (the two halves sit under the
    grammar ceiling; splitting further is the remedy, tests/test_intel_model.py). Also the whole
    answer of a lesson_draft run, which returns event_lessons only."""

    event_lessons: list[ProposedEventLesson] = Field(default_factory=list)
    lessons: list[ProposedGeneralLesson] = Field(default_factory=list)
```

Add to `ProposalOutput`, after `quiz_questions`:

```python
    # propose-v14: optional, so an answer without them still parses.
    event_lessons: list[ProposedEventLesson] = Field(default_factory=list)
    lessons: list[ProposedGeneralLesson] = Field(default_factory=list)
```

- [ ] **Step 6: Create `src/imageshield/intel/lessons.py`**

```python
"""Lessons (spec 2026-10-15-intel-lessons-and-evidence §3-§5): code, never the model, decides what
a lesson may say.

A lesson is checked for its bounds, for the score page's word rules (intel/user_copy.py) and for
every figure appearing in a quote of its own cited evidence. Its link must be the final URL of one
of those documents that came from a source an operator registered (``trust = 'listed'``): an
unvetted page can advertise the very apps an incident is about. At draft an unvetted link is
dropped and the lesson kept; anything else that fails drops the lesson, never the event it belongs
to, and is counted on the run's outcome.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from imageshield.intel.bounds import LESSON_TOPIC_PATTERN, MAX_RATIONALE_CHARS
from imageshield.intel.corroboration import uncorroborated
from imageshield.intel.pii import mask
from imageshield.intel.proposal_models import (
    ContextSignal,
    LessonDraft,
    LessonSuggested,
    LessonTarget,
    NewProposal,
)
from imageshield.intel.recency import Recency, evidence_stale
from imageshield.intel.schemas import ProposedGeneralLesson, ProposedLesson
from imageshield.intel.tags import is_well_formed, membership_problems
from imageshield.intel.text import normalise
from imageshield.intel.user_copy import copy_problems, unquoted_figures
from imageshield.intel.vocabulary import ScoringVocabulary
from imageshield.search.urlhash import url_hash


def _vetted_pages(cited: Sequence[ContextSignal]) -> set[str]:
    return {s.document_key for s in cited if s.trust == "listed" and s.document_key is not None}


def _texts(draft: LessonDraft) -> list[tuple[str, str]]:
    texts = [("title", draft.title), ("intro", draft.intro)]
    for i, section in enumerate(draft.sections):
        texts.extend((f"sections[{i}].items[{j}]", item) for j, item in enumerate(section.items))
    if draft.link is not None:
        texts.append(("link.label", draft.link.label))
    return texts


def lesson_problems(draft: LessonDraft, cited: Sequence[ContextSignal]) -> list[dict[str, str]]:
    """Every rule a lesson breaks, by field: the word rules, any figure no cited quote carries,
    and a link that is not one of the cited vetted pages. Empty: the lesson may be shown."""
    quotes = [quote for signal in cited for quote in signal.excerpts]
    problems: list[dict[str, str]] = []
    for name, text in _texts(draft):
        problems.extend({"field": name, "rule": rule} for rule in copy_problems(text))
        if unquoted_figures(text, quotes):
            problems.append({"field": name, "rule": "figure_not_quoted"})
    if draft.link is not None and url_hash(draft.link.url) not in _vetted_pages(cited):
        problems.append({"field": "link", "rule": "link_not_vetted"})
    return problems


def _masked(raw: str, counts: Counter[str]) -> str:
    masked, masks = mask(raw)
    if masks:
        counts["pii_masked_lesson"] += 1
    return normalise(masked)


def lesson_from(
    item: ProposedLesson | None, cited: Sequence[ContextSignal], counts: Counter[str]
) -> dict[str, Any] | None:
    """A drafted lesson as ``suggested.lesson`` stores it, or None (counted)."""
    if item is None:
        counts["lesson_missing"] += 1
        return None
    link: dict[str, str] | None = None
    raw_link = (item.link_url or "").strip()
    if raw_link:
        if raw_link.startswith("https://") and url_hash(raw_link) in _vetted_pages(cited) and (
            item.link_label or ""
        ).strip():
            link = {"url": raw_link, "label": _masked(item.link_label or "", counts)}
        else:
            counts["lesson_link_not_vetted"] += 1
    try:
        draft = LessonDraft.model_validate(
            {
                "title": _masked(item.title, counts),
                "intro": _masked(item.intro, counts),
                "sections": [
                    {"kind": s.kind, "items": [_masked(i, counts) for i in s.items]}
                    for s in item.sections
                ],
                "link": link,
            }
        )
    except ValidationError:
        counts["lesson_dropped_out_of_bounds"] += 1
        return None
    if lesson_problems(draft, cited):
        counts["lesson_dropped_forbidden_words"] += 1
        return None
    return draft.model_dump(mode="json")


def general_lesson(
    item: ProposedGeneralLesson,
    context: Mapping[UUID, ContextSignal],
    vocabulary: ScoringVocabulary,
    counts: Counter[str],
    *,
    taken_topics: Collection[str],
    recency: Recency,
) -> NewProposal | None:
    """A ``lesson`` proposal for a general lesson (spec §5), or None (counted). Its evidence must
    corroborate as a threat's does and pass the same recency rule (all of it dated and older than
    ``INTEL_THREAT_RECENCY_DAYS`` is ``evidence_stale``); its tags must be registered and live; and
    it must reach somebody: tags, or global."""
    import re

    if not re.fullmatch(LESSON_TOPIC_PATTERN, item.topic):
        counts["proposal_dropped_lesson_topic_malformed"] += 1
        return None
    if item.topic in taken_topics:
        counts["proposal_dropped_lesson_repeat"] += 1
        return None
    if any(not is_well_formed(t) for t in item.tags) or len(set(item.tags)) != len(item.tags):
        counts["proposal_dropped_tag_malformed"] += 1
        return None
    unknown, retired = membership_problems(item.tags, vocabulary.registry())
    if unknown or retired:
        counts["proposal_dropped_unknown_tag" if unknown else "proposal_dropped_tag_retired"] += 1
        return None
    if not item.tags and not item.is_global:
        counts["proposal_dropped_lesson_unscoped"] += 1
        return None
    cited_ids: list[UUID] = []
    for raw in item.signal_ids:
        try:
            signal_id = UUID(raw)
        except ValueError:
            counts["proposal_dropped_unknown_signal"] += 1
            return None
        signal = context.get(signal_id)
        if signal is None or signal.status != "active":
            counts["proposal_dropped_unknown_signal"] += 1
            return None
        if signal_id not in cited_ids:
            cited_ids.append(signal_id)
    if not cited_ids:
        counts["proposal_dropped_no_signals"] += 1
        return None
    cited = [context[i] for i in cited_ids]
    if uncorroborated(cited):
        counts["proposal_dropped_uncorroborated"] += 1
        return None
    if evidence_stale(cited, recency):
        counts["proposal_dropped_evidence_stale"] += 1
        return None
    lesson = lesson_from(item.lesson, cited, counts)
    if lesson is None:
        return None
    rationale = _masked(item.rationale, counts)
    if not rationale or len(rationale) > MAX_RATIONALE_CHARS:
        counts["proposal_dropped_rationale"] += 1
        return None
    return NewProposal(
        kind="lesson",
        target=LessonTarget(topic=item.topic).model_dump(mode="json", exclude_none=True),
        suggested=LessonSuggested.model_validate(
            {"lesson": lesson, "tags": item.tags, "is_global": item.is_global}
        ).model_dump(mode="json"),
        rationale=rationale,
        signal_ids=tuple(cited_ids),
        document_keys=tuple(dict.fromkeys(s.document_key for s in cited if s.document_key)),
    )
```

Move the `import re` to the module's import block (it is shown inline only to keep the step readable).

- [ ] **Step 7: Run the tests**

Run: `PYTHONPATH=src "$VENV" -m pytest tests/test_intel_lessons_rules.py tests/test_intel_threat_models.py`
Expected: PASS. If `copy_problems` returns the rule `"finding"` for "found", the reassurance/finding/address parametrised test passes on any non-empty list — it asserts the drop, not the rule.

- [ ] **Step 8: Lint, type-check, format the new files only, commit**

```bash
"$VENV" -m ruff format src/imageshield/intel/lessons.py tests/test_intel_lessons_rules.py
"$VENV" -m ruff check src tests
PYTHONPATH=src "$VENV" -m mypy src/imageshield/intel
git add src/imageshield/intel/bounds.py src/imageshield/intel/proposal_models.py src/imageshield/intel/schemas.py src/imageshield/intel/lessons.py tests/test_intel_lessons_rules.py
git commit -m "feat(intel): the lesson shape and its rules

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

### Task 3: Drafting lessons in the propose step (`propose-v14`)

**Files:**
- Modify: `src/imageshield/intel/prompts.py`, `src/imageshield/intel/model.py`, `src/imageshield/intel/stub.py`, `src/imageshield/intel/generation.py`, `src/imageshield/intel/proposal_store.py`, `src/imageshield/intel/pipeline.py`, `tests/intel_fakes.py`, `tests/test_intel_model.py`
- Create: `tests/test_intel_lessons_generation.py`

**Interfaces:**
- Consumes: Task 2's `lesson_from`, `general_lesson`, `ProposalLessonsOutput`.
- Produces: `IntelModel.draft_lessons(system, user) -> ModelCall[ProposalLessonsOutput]`; `merged_proposal(quiz, events, lessons=None)`; `with_proposed_events(user, events) -> str`; `proposal_request(..., open_lesson_topics: Sequence[str] = ())`; `PROPOSE_LESSONS_PART`; `validate_proposals(..., open_lesson_topics: Collection[str] = ())`; `GeneratedBatch.lessons`; `ProposalStore.open_lesson_topics(limit) -> list[str]`; `WriteResult.dropped_lessons`.

- [ ] **Step 1: Write the failing tests**

```python
"""Lessons in the propose step (spec 2026-10-15-intel-lessons-and-evidence §4, §5)."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from uuid import uuid4

from imageshield.intel.generation import validate_proposals
from imageshield.intel.prompts import PROPOSE_PROMPT_VERSION, with_proposed_events
from imageshield.intel.proposal_models import ContextSignal
from imageshield.intel.recency import Recency
from imageshield.intel.schemas import (
    ProposalEventsOutput,
    ProposalOutput,
    ProposedEventLesson,
    ProposedGeneralLesson,
    ProposedLesson,
    ProposedLessonSection,
    ProposedThreatEvent,
)
from imageshield.search.urlhash import url_hash
from tests.intel_fakes import NOW, scoring

V = scoring()


def _signal(publisher: str) -> ContextSignal:
    url = f"https://{publisher}/a"
    return ContextSignal(
        signal_id=uuid4(), category="incident", direction="risk_up", tags=("instagram",),
        unregistered_subjects=(), summary="s", trust="listed", publisher_domain=publisher,
        status="active", created_at=NOW - timedelta(days=1), document_key=url_hash(url),
        excerpts=(f"Scammers on {publisher} send a fake sign-in link.",), document_url=url,
    )


SIGNALS = [_signal("a.example"), _signal("b.example")]
LESSON = ProposedLesson(
    title="Spot a fake sponsorship offer",
    intro="A fake brand deal leads to a sign-in page that steals the account.",
    sections=[
        ProposedLessonSection(kind="warning_signs", items=["A deal you never applied for."]),
        ProposedLessonSection(kind="what_to_do", items=["Sign in only from the app itself."]),
    ],
)


def _threat() -> ProposedThreatEvent:
    return ProposedThreatEvent(
        kind="platform_incident", title="Fake sponsorship offers", body="b", severity=3,
        expires_in_days=30, tags=["instagram"], rationale="r",
        signal_ids=[str(s.signal_id) for s in SIGNALS],
    )


def _validate(output: ProposalOutput, *, events_only: bool = False, topics: set[str] | None = None):
    counts: Counter[str] = Counter()
    batch = validate_proposals(
        output, context={s.signal_id: s for s in SIGNALS}, gap_pool=[], vocabulary=V, now=NOW,
        counts=counts, recency=Recency(NOW, 90), events_only=events_only,
        open_lesson_topics=topics or set(),
    )
    return batch, counts


def test_the_prompt_version_is_v14() -> None:
    assert PROPOSE_PROMPT_VERSION == "propose-v14"


def test_an_event_lesson_rides_on_its_threat_by_key() -> None:
    batch, counts = _validate(
        ProposalOutput(threat_events=[_threat()], event_lessons=[ProposedEventLesson(event="T1", lesson=LESSON)])
    )
    [threat] = batch.threat_events
    assert threat.suggested["lesson"]["title"] == "Spot a fake sponsorship offer"
    assert counts["lesson_missing"] == 0


def test_a_threat_with_no_lesson_is_kept_and_counted() -> None:
    batch, counts = _validate(ProposalOutput(threat_events=[_threat()]))
    assert batch.threat_events[0].suggested["lesson"] is None
    assert counts["lesson_missing"] == 1


def test_a_lesson_for_an_unknown_key_is_counted() -> None:
    _, counts = _validate(ProposalOutput(event_lessons=[ProposedEventLesson(event="T9", lesson=LESSON)]))
    assert counts["lesson_event_unknown"] == 1


def test_general_lessons_are_written_and_dropped_on_an_events_only_pass() -> None:
    general = ProposedGeneralLesson(
        topic="fake_sponsorships", tags=["instagram"], lesson=LESSON, rationale="r",
        signal_ids=[str(s.signal_id) for s in SIGNALS],
    )
    batch, _ = _validate(ProposalOutput(lessons=[general]))
    assert [p.kind for p in batch.lessons] == ["lesson"]
    batch, counts = _validate(ProposalOutput(lessons=[general]), events_only=True)
    assert batch.lessons == [] and counts["proposal_dropped_not_an_event"] == 1


def test_two_general_lessons_on_one_topic_keep_the_first() -> None:
    general = ProposedGeneralLesson(
        topic="fake_sponsorships", tags=["instagram"], lesson=LESSON, rationale="r",
        signal_ids=[str(s.signal_id) for s in SIGNALS],
    )
    batch, counts = _validate(ProposalOutput(lessons=[general, general]))
    assert len(batch.lessons) == 1 and counts["proposal_dropped_lesson_repeat"] == 1


def test_with_proposed_events_keys_every_event() -> None:
    user = with_proposed_events('{"new_evidence": []}', ProposalEventsOutput(threat_events=[_threat()]))
    assert '"key": "T1"' in user and "Fake sponsorship offers" in user
```

In `tests/test_intel_model.py`, extend the size pin's parametrisation to `[ProposalQuizOutput, ProposalEventsOutput, ProposalLessonsOutput]`, and add (beside the existing two-call test at line ~460):

```python
async def test_propose_makes_three_calls_and_merges_the_lessons(clean_env: pytest.MonkeyPatch) -> None:
    lesson = ProposedEventLesson(event="T1", lesson=ProposedLesson(title="t", intro="i"))
    model, fake = _model(
        [
            _answer(ProposalQuizOutput()),
            _answer(ProposalEventsOutput()),
            _answer(ProposalLessonsOutput(event_lessons=[lesson])),
        ],
        clean_env,
    )
    call = await model.propose("sys", '{"new_evidence": []}')
    assert [_schema_title(c) for c in fake.calls] == [
        "ProposalQuizOutput", "ProposalEventsOutput", "ProposalLessonsOutput",
    ]
    assert call.output is not None and call.output.event_lessons == [lesson]


async def test_the_lessons_call_runs_when_the_events_half_answered_nothing(
    clean_env: pytest.MonkeyPatch,
) -> None:
    refused = _refusal()
    model, fake = _model(
        [_answer(ProposalQuizOutput()), refused, _answer(ProposalLessonsOutput())], clean_env
    )
    await model.propose("sys", '{"new_evidence": []}')
    assert len(fake.calls) == 3
```

Use the file's existing helpers (`_answer`, `_model`, `_schema_title`); if the refusal helper is named differently, reuse the one the test at line ~513 uses (`refused = …`).

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHONPATH=src "$VENV" -m pytest tests/test_intel_lessons_generation.py tests/test_intel_model.py`
Expected: FAIL (`PROPOSE_PROMPT_VERSION == "propose-v13"`; `with_proposed_events` missing; two calls made).

- [ ] **Step 3: Prompts** (`src/imageshield/intel/prompts.py`)

Set `PROPOSE_PROMPT_VERSION = "propose-v14"` and extend the version comment: `# propose-v14 (spec 2026-10-15-intel-lessons-and-evidence §4): a third call drafts lessons.`

Add the shared lesson rules (module level, before `_PROPOSE_SYSTEM`):

```python
_LESSON_RULES = """
A lesson teaches a person something useful about one risk or one protection, in plain words, for
the weekly report of people it may concern. It never reaches a person until an operator approves
it. Give:
- title: at most 60 characters.
- intro: at most 400 characters. For a threat, how it works; for a protection, what it is; for a
  general lesson, what it is about.
- sections: one or two of {kind, items}. kind is warning_signs, what_to_do or how_to_use, each at
  most once; items are 1 to 5 short sentences of at most 160 characters each. A threat's lesson has
  warning_signs and what_to_do; a protection's has how_to_use.
- link_url and link_label: optional. link_url must be the url of one of the cited evidence pages
  whose source is "listed"; link_label at most 40 characters. Any other link is discarded.
Describe the risk or the right, never the reader. Write any figure in digits, copied exactly as it
appears in a "quotes" entry of evidence the lesson cites, with its unit; never round, combine or
work one out. Never say anyone is safe, protected, guaranteed or in the clear; never say that
anything of the reader's was found, detected, matched or hit; never say "your photo", "your
answer" or "you answered"; name no private individual; put no contact details, web address or
domain in the words themselves."""
```

Append to `_PROPOSE_SYSTEM` (its last section, before the closing `"""`) a "Lessons" section that says: event_lessons gives one lesson per proposed threat or protection, keyed by the `key` in `proposed_events`; `lessons` gives general lessons with `topic` (a slug, `^[a-z][a-z0-9_]{1,59}$`, never one in `open_lesson_topics`), `tags` (registered slugs) or `is_global`, `rationale` and `signal_ids` (at least two independent publishers, or one listed source); then `_LESSON_RULES`. Edit `PROPOSE_QUIZ_PART` and `PROPOSE_EVENTS_PART` to add: `"event_lessons and lessons are asked for in a third call."`, and add:

```python
PROPOSE_LESSONS_PART = """

This call returns event_lessons and lessons only. The threat_events and protection_events proposed
in this run are listed in proposed_events, each with its key (T1, T2… for threats, P1, P2… for
protections): give one event_lesson per key whose evidence supports a lesson, citing nothing else.
Every other list is asked for in the other calls."""


def with_proposed_events(user: str, events: Any) -> str:
    """The proposal payload with the events call's threats and protections keyed T1…/P1…, for the
    lessons call (propose-v14). ``events`` is a ProposalEventsOutput, not yet validated."""
    payload = json.loads(user)
    payload["proposed_events"] = [
        *(
            {"key": f"T{i}", "kind": "threat", "title": e.title, "body": e.body,
             "signal_ids": list(e.signal_ids)}
            for i, e in enumerate(events.threat_events, start=1)
        ),
        *(
            {"key": f"P{i}", "kind": "protection", "title": e.title, "body": e.body,
             "signal_ids": list(e.signal_ids)}
            for i, e in enumerate(events.protection_events, start=1)
        ),
    ]
    return json.dumps(payload, ensure_ascii=False)
```

Give `proposal_request` a keyword `open_lesson_topics: Sequence[str] = ()` and put `"open_lesson_topics": list(open_lesson_topics)` into the payload. Add the lesson_draft prompt:

```python
_LESSON_DRAFT_SYSTEM = (
    """You write lessons for live incidents and protections that a likeness-protection service
already approved. Each event in "events" has a key (its id), its title and body, and the ids of
the evidence it was approved on; the evidence is in "evidence". Return event_lessons only: one per
event whose evidence supports a lesson, keyed by the event's id, citing only that event's evidence.
Return lessons empty."""
    + _LESSON_RULES
)


def lesson_draft_request(
    events: Sequence[dict[str, Any]], evidence: Sequence[PromptSignal], *, today: str
) -> tuple[str, str]:
    """A lesson_draft run's call (spec §5): live events and their cited evidence. Never a person."""
    user = json.dumps(
        {"today": today, "events": list(events), "evidence": list(evidence)}, ensure_ascii=False
    )
    return _LESSON_DRAFT_SYSTEM, user
```

- [ ] **Step 4: The model** (`src/imageshield/intel/model.py`)

Import `ProposalLessonsOutput` and `PROPOSE_LESSONS_PART`, `with_proposed_events`. Add to the `IntelModel` Protocol:

```python
    async def draft_lessons(self, system: str, user: str) -> ModelCall[ProposalLessonsOutput]: ...
```

Change `merged_proposal` to take a third half and merge it:

```python
def merged_proposal(
    quiz: ModelCall[ProposalQuizOutput] | None,
    events: ModelCall[ProposalEventsOutput],
    lessons: ModelCall[ProposalLessonsOutput] | None = None,
) -> ModelCall[ProposalOutput]:
```

— `halves` includes `lessons` when not None (so usage, cost and latency sum all three); the no-output case is "no half gave an output"; the merged `ProposalOutput` gains `event_lessons=lessons_out.event_lessons if lessons_out is not None else []` and the same for `lessons`. Keep the events call as answering model and stop reason of record.

In `propose`, after `events = await self._send(...)`:

```python
        lessons_user = (
            with_proposed_events(events_user, events.output) if events.output is not None else events_user
        )
        lessons = await self._send(
            ProposalLessonsOutput,
            model=self._config.intel_proposal_model,
            effort=_PROPOSAL_EFFORT,
            system=system + PROPOSE_LESSONS_PART,
            messages=[{"role": "user", "content": lessons_user}],
        )
        return merged_proposal(quiz, events, lessons)
```

Update the docstring: "THREE structured calls (propose-v14)". Add:

```python
    async def draft_lessons(self, system: str, user: str) -> ModelCall[ProposalLessonsOutput]:
        return await self._send(
            ProposalLessonsOutput,
            model=self._config.intel_proposal_model,
            effort=_PROPOSAL_EFFORT,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
```

In `src/imageshield/intel/stub.py` add the same method returning `ModelCall(ProposalLessonsOutput(), "ok", "stub", "end_turn", _ZERO, Decimal("0"), 0)`.

In `tests/intel_fakes.py`, give `FakeModel.__init__` a keyword `lessons_with: Callable[[dict[str, Any]], ProposalLessonsOutput] | None = None` (stored as `self.lessons_with`, plus `self.lesson_users: list[str] = []`) and add:

```python
    async def draft_lessons(self, system: str, user: str) -> ModelCall[ProposalLessonsOutput]:
        self.lesson_users.append(user)
        output = self.lessons_with(json.loads(user)) if self.lessons_with else ProposalLessonsOutput()
        return ModelCall(output, "ok", "claude-opus-5-5", "end_turn", Usage(1, 1, 0, 0, 0), Decimal("0.05"), 1)
```

- [ ] **Step 5: Generation** (`src/imageshield/intel/generation.py`)

Import `general_lesson`, `lesson_from` from `imageshield.intel.lessons` and `ProposedLesson` from schemas. Add `lessons: list[NewProposal] = field(default_factory=list)` to `GeneratedBatch` and `*self.lessons` at the end of `proposals`. Give `validate_proposals` a keyword `open_lesson_topics: Collection[str] = ()`. Replace the two event loops with:

```python
    pending = pending_events or {}
    event_lessons = {item.event: item.lesson for item in output.event_lessons}
    keys = {f"T{i}" for i in range(1, len(output.threat_events) + 1)} | {
        f"P{i}" for i in range(1, len(output.protection_events) + 1)
    }
    unknown_keys = [k for k in event_lessons if k not in keys]
    if unknown_keys:
        counts["lesson_event_unknown"] += len(unknown_keys)
    for index, event in enumerate(output.threat_events, start=1):
        proposal = _threat_event(event, context, vocabulary, counts, recency, lesson=event_lessons.get(f"T{index}"))
        if proposal is not None:
            _keep_event(proposal, batch.threat_events, batch, pending, new_signal_ids, counts)
    for index, credit in enumerate(output.protection_events, start=1):
        proposal = _protection_event(credit, context, vocabulary, counts, lesson=event_lessons.get(f"P{index}"))
        if proposal is not None:
            _keep_event(proposal, batch.protection_events, batch, pending, new_signal_ids, counts)
    if events_only:
        if output.lessons:
            counts["proposal_dropped_not_an_event"] += len(output.lessons)
    else:
        topics = set(open_lesson_topics)
        for item in output.lessons:
            lesson = general_lesson(
                item, context, vocabulary, counts, taken_topics=topics, recency=recency
            )
            if lesson is not None:
                topics.add(lesson.target["topic"])
                batch.lessons.append(lesson)
```

Give `_threat_event` and `_protection_event` a keyword `lesson: ProposedLesson | None = None`. In `_threat_event`, add `"lesson"` to the `exclude` set of the `model_dump` and add beside `"action"`:

```python
            "lesson": lesson_from(lesson, [context[i] for i in signal_ids], counts),
```

In `_protection_event`, build `suggested` as `{**ProtectionEventSuggested(...).model_dump(mode="json", exclude={"lesson"}), "lesson": lesson_from(lesson, [context[i] for i in signal_ids], counts)}`.

- [ ] **Step 6: The write's topic re-check and the open topics** (`src/imageshield/intel/proposal_store.py`)

Add `dropped_lessons: int = 0` to `WriteResult` (in `proposal_models.py`). In `write_generated`, inside the per-proposal loop and before the INSERT, for `proposal.kind == "lesson"`:

```python
                if proposal.kind == "lesson":
                    topic = proposal.target["topic"]
                    taken = await conn.execute(
                        "SELECT 1 FROM intel_proposals WHERE kind = 'lesson'"
                        " AND status IN ('pending', 'approved', 'applied') AND target->>'topic' = %s"
                        " UNION ALL SELECT 1 FROM intel_lessons WHERE topic = %s AND retired_at IS NULL"
                        " LIMIT 1",
                        (topic, topic),
                    )
                    if await taken.fetchone() is not None:
                        dropped_lessons += 1
                        continue
```

(declare `dropped_lessons = 0` beside `dropped_questions`, and return it in the `WriteResult`). Add to the store and its Protocol:

```python
    async def open_lesson_topics(self, *, limit: int) -> list[str]:
        """Topics a new general lesson may not take (spec §5): pending, approved or applied lesson
        proposals, and live lessons. Newest first, for the prompt."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT topic FROM ("
                "  SELECT target->>'topic' AS topic, created_at FROM intel_proposals"
                "   WHERE kind = 'lesson' AND status IN ('pending', 'approved', 'applied')"
                "     AND target ? 'topic'"
                "  UNION ALL SELECT topic, approved_at FROM intel_lessons"
                "   WHERE topic IS NOT NULL AND retired_at IS NULL"
                ") t GROUP BY topic ORDER BY max(created_at) DESC LIMIT %s",
                (limit,),
            )
            return [row[0] for row in await cur.fetchall()]
```

- [ ] **Step 7: The pipeline** (`src/imageshield/intel/pipeline.py` `_generate`)

Before `proposal_request(...)`: `open_topics = await store.open_lesson_topics(limit=MAX_PROMPT_LESSON_TOPICS)` (import the bound). Pass `open_lesson_topics=open_topics` to `proposal_request` and to `validate_proposals`. After the existing count lines: `if result.dropped_lessons: ctx.counts["proposal_dropped_lesson_at_write"] += result.dropped_lessons`.

- [ ] **Step 8: Run the tests**

Run: `PYTHONPATH=src REQUIRE_DB=1 "$VENV" -m pytest tests/test_intel_lessons_generation.py tests/test_intel_model.py tests/test_intel_generation.py tests/test_intel_proposals_pipeline.py`
Expected: PASS. The size pin must hold for `ProposalLessonsOutput` (< 4,800). Existing tests that assert `PROPOSE_PROMPT_VERSION == "propose-v13"` or two calls must be updated to v14 / three calls; change only those assertions.

- [ ] **Step 9: Lint, type-check, commit**

```bash
"$VENV" -m ruff format tests/test_intel_lessons_generation.py
"$VENV" -m ruff check src tests
PYTHONPATH=src "$VENV" -m mypy src/imageshield/intel
git add -A src/imageshield/intel tests/intel_fakes.py tests/test_intel_model.py tests/test_intel_lessons_generation.py tests/test_intel_generation.py tests/test_intel_proposals_pipeline.py
git commit -m "feat(intel): propose-v14 drafts lessons in a third call

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

### Task 4: Decisions — lessons, event lessons, renewals, the evidence snapshot

**Files:**
- Create: `src/imageshield/intel/lesson_store.py`, `tests/test_intel_lessons_decisions.py`
- Modify: `src/imageshield/intel/approvable.py`, `src/imageshield/intel/decisions.py`, `src/imageshield/http/routes/admin_intel.py` (the refusal map), `tests/intel_fakes.py`

**Interfaces:**
- Produces in `lesson_store.py`: `insert_lesson(conn, *, proposal_id, event_id, kind, topic, decided, operator) -> UUID`, `carry_lesson(conn, *, from_event_id, to_event_id, proposal_id, operator) -> UUID | None`, `snapshot_evidence(conn, *, owner_kind, owner_id, proposal_id, active) -> None`, `event_kind(conn, event_id) -> Literal["threat", "protection"] | None`.
- Produces in `decisions.py`: a `lesson` approval moves to `applied` with `applied_ref = str(lesson_id)`.

- [ ] **Step 1: Write the failing tests**

```python
"""Deciding lessons (spec 2026-10-15-intel-lessons-and-evidence §4-§6), against real Postgres."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from psycopg_pool import AsyncConnectionPool

from imageshield.intel.decisions import PostgresDecisionStore
from imageshield.intel.proposal_models import DecisionRefused
from tests.intel_fakes import (
    NOW,
    THREAT_SUGGESTED,
    seed_proposal,
    seed_signal,
    seed_threat_proposal,
)

LESSON = {
    "title": "Spot a fake sponsorship offer",
    "intro": "A fake brand deal leads to a sign-in page that steals the account.",
    "sections": [{"kind": "what_to_do", "items": ["Sign in only from the app itself."]}],
    "link": None,
}


def _store(pool: AsyncConnectionPool) -> PostgresDecisionStore:
    return PostgresDecisionStore(pool, threat_recency_days=90, clock=lambda: NOW)


async def _two_signals(pool: AsyncConnectionPool) -> list[UUID]:
    return [
        await seed_signal(pool, tags=("instagram",), publisher="a.example", category="incident"),
        await seed_signal(pool, tags=("instagram",), publisher="b.example", category="incident"),
    ]


async def _rows(pool: AsyncConnectionPool, sql: str, *args: Any) -> list[tuple[Any, ...]]:
    async with pool.connection() as conn:
        cur = await conn.execute(sql, args)
        return await cur.fetchall()


async def test_a_threat_approval_writes_its_lesson_and_snapshots_both(intel_pool: AsyncConnectionPool) -> None:
    signals = await _two_signals(intel_pool)
    proposal = await seed_threat_proposal(
        intel_pool, signal_ids=signals, suggested={**THREAT_SUGGESTED, "lesson": LESSON}
    )
    decided = await _store(intel_pool).decide(
        proposal, decision="approved", values=None, reason="ok", operator="Op"
    )
    event_id = UUID(str(decided.applied_ref))
    lessons = await _rows(intel_pool, "SELECT kind, title FROM intel_lessons WHERE event_id = %s", event_id)
    assert lessons == [("threat", "Spot a fake sponsorship offer")]
    owners = await _rows(
        intel_pool, "SELECT owner_kind, independent_sources FROM intel_published_owners ORDER BY owner_kind"
    )
    assert owners == [("event", 2), ("lesson", 2)]
    shown = await _rows(intel_pool, "SELECT count(*) FROM svc.v_evidence WHERE owner_id = %s", event_id)
    assert shown[0][0] >= 2


async def test_values_lesson_null_writes_no_lesson(intel_pool: AsyncConnectionPool) -> None:
    signals = await _two_signals(intel_pool)
    proposal = await seed_threat_proposal(
        intel_pool, signal_ids=signals, suggested={**THREAT_SUGGESTED, "lesson": LESSON}
    )
    await _store(intel_pool).decide(
        proposal, decision="approved", values={"lesson": None}, reason="ok", operator="Op"
    )
    assert await _rows(intel_pool, "SELECT 1 FROM intel_lessons") == []
    assert len(await _rows(intel_pool, "SELECT 1 FROM intel_published_owners")) == 1


async def test_an_edited_lesson_that_breaks_a_rule_is_refused(intel_pool: AsyncConnectionPool) -> None:
    signals = await _two_signals(intel_pool)
    proposal = await seed_threat_proposal(intel_pool, signal_ids=signals)
    with pytest.raises(DecisionRefused) as refused:
        await _store(intel_pool).decide(
            proposal, decision="approved", reason="ok", operator="Op",
            values={"lesson": {**LESSON, "intro": "You are safe after 9 days."}},
        )
    assert refused.value.code == "lesson_invalid"
    assert {"field": "intro", "rule": "reassurance"} in refused.value.problems
    assert {"field": "intro", "rule": "figure_not_quoted"} in refused.value.problems
    assert await _rows(intel_pool, "SELECT 1 FROM threat_events") == []


async def test_a_general_lesson_approval_is_applied_with_its_lesson_id(intel_pool: AsyncConnectionPool) -> None:
    signals = await _two_signals(intel_pool)
    proposal = await seed_proposal(
        intel_pool, signal_ids=signals, kind="lesson", target={"topic": "fake_sponsorships"},
        suggested={"lesson": LESSON, "tags": ["instagram"], "is_global": False},
    )
    decided = await _store(intel_pool).decide(
        proposal, decision="approved", values=None, reason="ok", operator="Op"
    )
    assert decided.status == "applied"
    rows = await _rows(intel_pool, "SELECT lesson_id, kind, topic FROM intel_lessons")
    assert rows == [(UUID(str(decided.applied_ref)), "general", "fake_sponsorships")]
    assert len(await _rows(intel_pool, "SELECT 1 FROM svc.v_lessons")) == 1


async def test_a_lesson_for_an_event_replaces_its_live_lesson(intel_pool: AsyncConnectionPool) -> None:
    signals = await _two_signals(intel_pool)
    event_proposal = await seed_threat_proposal(
        intel_pool, signal_ids=signals, suggested={**THREAT_SUGGESTED, "lesson": LESSON}
    )
    event = (await _store(intel_pool).decide(
        event_proposal, decision="approved", values=None, reason="ok", operator="Op"
    )).applied_ref
    redraft = await seed_proposal(
        intel_pool, signal_ids=signals, kind="lesson", target={"event_id": event},
        suggested={"lesson": {**LESSON, "title": "A better lesson"}, "tags": [], "is_global": False},
    )
    await _store(intel_pool).decide(redraft, decision="approved", values=None, reason="ok", operator="Op")
    live = await _rows(intel_pool, "SELECT title FROM svc.v_lessons WHERE event_id = %s", UUID(str(event)))
    assert live == [("A better lesson",)]
```

Add a protection-renewal test beside the existing renewal tests' helpers (`seed_protection_event`, `seed_protection_proposal` with `target={"renews_event_id": …}`): approve a protection with `"lesson": LESSON`, then approve its renewal with `values=None` → the new event has a lesson with the same title; a second renewal approved with `values={"lesson": None}` → no lesson on its event. Use the existing renewal test in `tests/test_intel_protection_*` as the template for seeding a renewable credit.

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHONPATH=src REQUIRE_DB=1 "$VENV" -m pytest tests/test_intel_lessons_decisions.py`
Expected: FAIL (`lesson` not decidable: `proposal_not_decidable`; no `intel_lessons` row).

- [ ] **Step 3: Create `src/imageshield/intel/lesson_store.py`**

```python
"""Lesson and published-evidence SQL (spec 2026-10-15-intel-lessons-and-evidence §5, §6). Every
function takes the caller's connection, so a decision writes its event, its lesson and its evidence
snapshot in ONE transaction."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from imageshield.intel.corroboration import independent_sources
from imageshield.intel.proposal_models import ContextSignal

LessonKind = Literal["threat", "protection", "general"]


async def event_kind(
    conn: AsyncConnection[Any], event_id: UUID
) -> Literal["threat", "protection"] | None:
    cur = await conn.execute(
        "SELECT 'threat' FROM threat_events WHERE event_id = %s"
        " UNION ALL SELECT 'protection' FROM protection_events WHERE event_id = %s",
        (event_id, event_id),
    )
    row = await cur.fetchone()
    return None if row is None else row[0]


async def insert_lesson(
    conn: AsyncConnection[Any],
    *,
    proposal_id: UUID,
    event_id: UUID | None,
    kind: LessonKind,
    topic: str | None,
    decided: dict[str, Any],
    operator: str,
) -> UUID:
    """An approved lesson. An event's earlier live lesson is retired first ('replaced'), so the
    one-live-per-event index holds and the newest approval is what people read."""
    if event_id is not None:
        await conn.execute(
            "UPDATE intel_lessons SET retired_by = %s, retired_at = now(), retire_reason = 'replaced'"
            " WHERE event_id = %s AND retired_at IS NULL",
            (operator, event_id),
        )
    lesson = decided["lesson"]
    link = lesson.get("link")
    cur = await conn.execute(
        "INSERT INTO intel_lessons (proposal_id, event_id, kind, topic, title, intro, sections,"
        " link_url, link_label, tags, is_global, approved_by)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::text[], %s, %s) RETURNING lesson_id",
        (
            proposal_id,
            event_id,
            kind,
            topic,
            lesson["title"],
            lesson["intro"],
            Jsonb(lesson["sections"]),
            link["url"] if link else None,
            link["label"] if link else None,
            list(decided.get("tags", [])),
            bool(decided.get("is_global", False)),
            operator,
        ),
    )
    row = await cur.fetchone()
    assert row is not None
    lesson_id: UUID = row[0]
    return lesson_id


async def carry_lesson(
    conn: AsyncConnection[Any],
    *,
    from_event_id: UUID,
    to_event_id: UUID,
    proposal_id: UUID,
    operator: str,
) -> UUID | None:
    """A renewal keeps the renewed credit's live lesson (spec §4), on the new event."""
    cur = await conn.execute(
        "INSERT INTO intel_lessons (proposal_id, event_id, kind, topic, title, intro, sections,"
        " link_url, link_label, tags, is_global, approved_by)"
        " SELECT %s, %s, kind, topic, title, intro, sections, link_url, link_label, tags,"
        " is_global, %s FROM intel_lessons WHERE event_id = %s AND retired_at IS NULL"
        " RETURNING lesson_id",
        (proposal_id, to_event_id, operator, from_event_id),
    )
    row = await cur.fetchone()
    return None if row is None else row[0]


async def snapshot_evidence(
    conn: AsyncConnection[Any],
    *,
    owner_kind: Literal["event", "lesson"],
    owner_id: UUID,
    proposal_id: UUID,
    active: Sequence[ContextSignal],
) -> None:
    """What the operator approved on, frozen with the approval (spec §6): every quote of the
    proposal's active signals, and how many independent sources they come from."""
    await conn.execute(
        "INSERT INTO intel_published_owners (owner_kind, owner_id, proposal_id,"
        " independent_sources, approved_at) VALUES (%s, %s, %s, %s, now())"
        " ON CONFLICT (owner_kind, owner_id) DO NOTHING",
        (owner_kind, owner_id, proposal_id, independent_sources(active)),
    )
    await conn.execute(
        "INSERT INTO intel_published_evidence (owner_kind, owner_id, signal_id, excerpt_id)"
        " SELECT %s, %s, e.signal_id, e.excerpt_id FROM intel_proposal_signals ps"
        " JOIN intel_signals s ON s.signal_id = ps.signal_id AND s.status = 'active'"
        " JOIN intel_excerpts e ON e.signal_id = s.signal_id"
        " WHERE ps.proposal_id = %s ON CONFLICT DO NOTHING",
        (owner_kind, owner_id, proposal_id),
    )
```

- [ ] **Step 4: Make `lesson` decidable** (`src/imageshield/intel/approvable.py`): add `"lesson"` to `APPROVABLE_KINDS` and `REJECTABLE_KINDS`. `why_not` needs no other change: a lesson is refused for retracted or uncorroborated evidence like any kind, is never `evidence_stale` (only threats are) and never `tags_unmapped` (only events are).

- [ ] **Step 5: Decisions** (`src/imageshield/intel/decisions.py`)

Import `LessonDecided, LessonDraft, LessonTarget, LessonValues` from `proposal_models`, `lesson_problems` from `lessons`, and `carry_lesson, event_kind, insert_lesson, snapshot_evidence` from `lesson_store`. Add the message: `"lesson_invalid": "This lesson breaks a rule: see problems."`.

Add a helper and call it from `_threat_decided` and `_protection_decided`, which each gain a parameter `active: Sequence[ContextSignal]` (pass `active` from `_approval_decided`, which already holds it):

```python
def _check_lesson(raw: dict[str, Any] | None, active: Sequence[ContextSignal]) -> None:
    """A decided lesson must pass every rule against the evidence it was approved on (spec §3)."""
    if raw is None:
        return
    problems = lesson_problems(LessonDraft.model_validate(raw), active)
    if problems:
        raise DecisionRefused(
            "lesson_invalid", _MESSAGES["lesson_invalid"], problems=tuple(problems)
        )
```

— in `_threat_decided` and `_protection_decided`, call `_check_lesson(decided.model_dump(mode="json").get("lesson"), active)` just before `return decided.model_dump(mode="json")`.

Add the lesson kind's values:

```python
def _lesson_decided(
    proposal: ProposalRecord,
    vocabulary: ScoringVocabulary | None,
    values: dict[str, Any] | None,
    active: Sequence[ContextSignal],
) -> dict[str, Any]:
    """The exact lesson an approval stores (spec §5): the edit merged over ``suggested``; every tag
    registered and live; a general lesson reaches somebody (tags or global)."""
    try:
        target = LessonTarget.model_validate(proposal.target)
        edit = LessonValues.model_validate(values or {})
        decided = LessonDecided.model_validate(
            {**_known(proposal.suggested, LessonDecided), **edit.model_dump(exclude_unset=True)}
        )
    except ValidationError as exc:
        raise _refuse("values_out_of_bounds") from exc
    registry = (
        vocabulary.registry() if vocabulary is not None else TagRegistry(frozenset(), frozenset())
    )
    unknown, retired = membership_problems(list(decided.tags), registry)
    if unknown:
        raise DecisionRefused("unknown_tag", _MESSAGES["unknown_tag"], slugs=tuple(unknown))
    if retired:
        raise DecisionRefused("tag_retired", _MESSAGES["tag_retired"], slugs=tuple(retired))
    if target.topic is not None and not decided.tags and not decided.is_global:
        raise _refuse("values_out_of_bounds")
    _check_lesson(decided.lesson.model_dump(mode="json"), active)
    return decided.model_dump(mode="json")
```

and route it in `_approval_decided` (before the threat branch): `if proposal.kind == "lesson": return _lesson_decided(proposal, vocabulary, values, active)`.

In `decide()`, keep `lesson_id: UUID | None = None`. After `event_id = await _insert_threat_event(...)`:

```python
                        await snapshot_evidence(
                            conn, owner_kind="event", owner_id=event_id, proposal_id=proposal_id, active=active
                        )
                        if decided.get("lesson") is not None:
                            lesson_id = await insert_lesson(
                                conn, proposal_id=proposal_id, event_id=event_id, kind="threat",
                                topic=None, decided=decided, operator=operator,
                            )
                            await snapshot_evidence(
                                conn, owner_kind="lesson", owner_id=lesson_id,
                                proposal_id=proposal_id, active=active,
                            )
```

After `event_id = await _insert_protection_event(...)`, the same with `kind="protection"`, plus the renewal carry-forward when the decision did not mention the lesson:

```python
                        elif renews is not None and "lesson" not in (values or {}):
                            lesson_id = await carry_lesson(
                                conn, from_event_id=renews, to_event_id=event_id,
                                proposal_id=proposal_id, operator=operator,
                            )
                            if lesson_id is not None:
                                await snapshot_evidence(
                                    conn, owner_kind="lesson", owner_id=lesson_id,
                                    proposal_id=proposal_id, active=active,
                                )
```

(Structure it as `if decided.get("lesson") is not None: … elif renews is not None and "lesson" not in (values or {}): …`.)

Add a branch for `proposal.kind == "lesson"` before the final `else` (`_APPROVE_SQL`):

```python
                    elif proposal.kind == "lesson":
                        lesson_target = LessonTarget.model_validate(proposal.target)
                        kind = (
                            "general"
                            if lesson_target.event_id is None
                            else await event_kind(conn, lesson_target.event_id)
                        )
                        if kind is None:
                            raise _refuse("proposal_not_pending")  # its event is gone
                        lesson_id = await insert_lesson(
                            conn, proposal_id=proposal_id, event_id=lesson_target.event_id,
                            kind=kind, topic=lesson_target.topic, decided=decided, operator=operator,
                        )
                        await snapshot_evidence(
                            conn, owner_kind="lesson", owner_id=lesson_id,
                            proposal_id=proposal_id, active=active,
                        )
                        await cur.execute(
                            _APPLY_EVENT_SQL,
                            {
                                "proposal_id": proposal_id,
                                "decided": Jsonb(decided),
                                "applied_ref": str(lesson_id),
                                "operator": operator,
                                "reason": reason,
                            },
                        )
```

Add `**({"lesson_id": str(lesson_id)} if lesson_id is not None else {})` to the audit metadata. In `admin_intel.py`'s `_REFUSAL_STATUS` add `"lesson_invalid": 422`, and in `decide_proposal`'s `except` pass the problems too:

```python
            extra=(
                {"slugs": list(refused.slugs)} if refused.slugs
                else {"problems": list(refused.problems)} if refused.problems
                else None
            ),
```

- [ ] **Step 6: Run the tests**

Run: `PYTHONPATH=src REQUIRE_DB=1 "$VENV" -m pytest tests/test_intel_lessons_decisions.py tests/test_intel_decisions.py tests/test_intel_protection_decisions.py`
Expected: PASS. (Use `ls tests | grep -i decision` for the exact names of the existing decision tests; run each once.)

- [ ] **Step 7: Lint, type-check, commit**

```bash
"$VENV" -m ruff format src/imageshield/intel/lesson_store.py tests/test_intel_lessons_decisions.py
"$VENV" -m ruff check src tests
PYTHONPATH=src "$VENV" -m mypy src/imageshield
git add src/imageshield/intel/lesson_store.py src/imageshield/intel/approvable.py src/imageshield/intel/decisions.py src/imageshield/http/routes/admin_intel.py tests/test_intel_lessons_decisions.py
git commit -m "feat(intel): decisions write lessons and snapshot their evidence

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

### Task 5: Retraction updates the snapshot; backfill for events approved before 0055

**Files:**
- Modify: `src/imageshield/intel/lesson_store.py`, `src/imageshield/intel/evidence_store.py`
- Create: `src/imageshield/intel/backfill_evidence.py`, `tests/test_intel_lessons_evidence.py`

**Interfaces:**
- Produces: `lesson_store.recompute_after_retraction(conn, signal_id) -> int` (owners updated); `backfill_evidence.backfill(pool) -> int` (owners written); CLI `python -m imageshield.intel.backfill_evidence`.

- [ ] **Step 1: Write the failing tests**

```python
"""Published evidence after approval (spec 2026-10-15-intel-lessons-and-evidence §6)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from imageshield.intel.backfill_evidence import backfill
from imageshield.intel.decisions import PostgresDecisionStore
from imageshield.intel.evidence_store import PostgresEvidenceStore
from tests.intel_fakes import NOW, seed_signal, seed_threat_event, seed_threat_proposal


async def _scalar(pool: AsyncConnectionPool, sql: str, *args: Any) -> Any:
    async with pool.connection() as conn:
        cur = await conn.execute(sql, args)
        row = await cur.fetchone()
        return None if row is None else row[0]


async def _approved(pool: AsyncConnectionPool) -> tuple[UUID, list[UUID]]:
    signals = [
        await seed_signal(pool, tags=("instagram",), publisher="a.example", category="incident"),
        await seed_signal(pool, tags=("instagram",), publisher="b.example", category="incident"),
    ]
    proposal = await seed_threat_proposal(pool, signal_ids=signals)
    decided = await PostgresDecisionStore(pool, threat_recency_days=90, clock=lambda: NOW).decide(
        proposal, decision="approved", values=None, reason="ok", operator="Op"
    )
    return UUID(str(decided.applied_ref)), signals


async def test_a_retraction_removes_its_rows_and_lowers_the_count(intel_pool: AsyncConnectionPool) -> None:
    event, signals = await _approved(intel_pool)
    await PostgresEvidenceStore(intel_pool).retract_signal(signals[0], operator="Op", reason="wrong page")
    assert await _scalar(
        intel_pool, "SELECT independent_sources FROM intel_published_owners WHERE owner_id = %s", event
    ) == 1
    assert await _scalar(
        intel_pool, "SELECT count(DISTINCT publisher_domain) FROM svc.v_evidence WHERE owner_id = %s", event
    ) == 1


async def test_retracting_the_only_evidence_leaves_the_event_live(intel_pool: AsyncConnectionPool) -> None:
    event, signals = await _approved(intel_pool)
    for signal in signals:
        await PostgresEvidenceStore(intel_pool).retract_signal(signal, operator="Op", reason="wrong")
    assert await _scalar(intel_pool, "SELECT count(*) FROM svc.v_evidence WHERE owner_id = %s", event) == 0
    assert await _scalar(intel_pool, "SELECT status FROM threat_events WHERE event_id = %s", event) == "active"


async def test_the_backfill_snapshots_old_events_once(intel_pool: AsyncConnectionPool) -> None:
    signals = [
        await seed_signal(intel_pool, tags=("instagram",), publisher="a.example"),
        await seed_signal(intel_pool, tags=("instagram",), publisher="b.example"),
    ]
    proposal = await seed_threat_proposal(intel_pool, signal_ids=signals, status="applied", decided={})
    await seed_threat_event(intel_pool, proposal_id=proposal)
    assert await backfill(intel_pool) == 1
    assert await backfill(intel_pool) == 0
    assert await _scalar(intel_pool, "SELECT independent_sources FROM intel_published_owners") == 2
```

(If `seed_threat_proposal` rejects `status="applied"` with an empty `decided`, pass the decided dict the existing applied-threat tests use.)

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHONPATH=src REQUIRE_DB=1 "$VENV" -m pytest tests/test_intel_lessons_evidence.py`
Expected: FAIL (`ModuleNotFoundError: imageshield.intel.backfill_evidence`).

- [ ] **Step 3: Recompute on retraction**

Add to `lesson_store.py`:

```python
async def recompute_after_retraction(conn: AsyncConnection[Any], signal_id: UUID) -> int:
    """Every owner citing a just-retracted signal gets its independence count recomputed from its
    proposal's still-active evidence, in the retraction's transaction (spec §6). The rows stay;
    svc.v_evidence drops them because it joins active signals only."""
    from imageshield.intel.proposal_store import fetch_linked_signals

    cur = await conn.execute(
        "SELECT DISTINCT o.owner_kind, o.owner_id, o.proposal_id FROM intel_published_evidence pe"
        " JOIN intel_published_owners o ON o.owner_kind = pe.owner_kind AND o.owner_id = pe.owner_id"
        " WHERE pe.signal_id = %s",
        (signal_id,),
    )
    owners = await cur.fetchall()
    if not owners:
        return 0
    linked = await fetch_linked_signals(conn, list({row[2] for row in owners}))
    for kind, owner, proposal in owners:
        active = [s for s in linked[proposal] if s.status == "active"]
        await conn.execute(
            "UPDATE intel_published_owners SET independent_sources = %s"
            " WHERE owner_kind = %s AND owner_id = %s",
            (independent_sources(active), kind, owner),
        )
    return len(owners)
```

In `evidence_store.retract_signal`, inside `if updated:` and before the audit insert: `await recompute_after_retraction(conn, signal_id)` (import from `lesson_store`).

- [ ] **Step 4: Create `src/imageshield/intel/backfill_evidence.py`**

```python
"""Snapshot the evidence of every event approved before 0055 (spec 2026-10-15 §6).

A one-shot CLI (``python -m imageshield.intel.backfill_evidence``), run once per environment after
the migration, the shape of imageshield/search/retention.py. Idempotent: an event whose snapshot
exists is skipped, so a second run writes nothing."""

from __future__ import annotations

import asyncio
import sys

import structlog
from psycopg_pool import AsyncConnectionPool

from imageshield.config import ConfigError, load_config
from imageshield.db.connection import make_async_pool
from imageshield.http.logging import configure_logging
from imageshield.intel.lesson_store import snapshot_evidence
from imageshield.intel.proposal_store import fetch_linked_signals

log = structlog.get_logger("imageshield.intel")

_MISSING_SQL = """
    SELECT event_id, proposal_id FROM threat_events
     WHERE proposal_id IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM intel_published_owners o
                        WHERE o.owner_kind = 'event' AND o.owner_id = threat_events.event_id)
    UNION ALL
    SELECT event_id, proposal_id FROM protection_events
     WHERE NOT EXISTS (SELECT 1 FROM intel_published_owners o
                        WHERE o.owner_kind = 'event' AND o.owner_id = protection_events.event_id)
"""


async def backfill(pool: AsyncConnectionPool) -> int:
    written = 0
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(_MISSING_SQL)
        rows = await cur.fetchall()
        linked = await fetch_linked_signals(conn, list({r[1] for r in rows}))
        for event_id, proposal_id in rows:
            active = [s for s in linked[proposal_id] if s.status == "active"]
            await snapshot_evidence(
                conn, owner_kind="event", owner_id=event_id, proposal_id=proposal_id, active=active
            )
            written += 1
    log.info("intel.evidence_backfilled", events=written)
    return written


async def run_once() -> int:
    config = load_config()
    pool = make_async_pool(config.database_url, min_size=1, max_size=1)
    await pool.open()
    try:
        return await backfill(pool)
    finally:
        await pool.close()


def main() -> int:
    configure_logging()
    try:
        load_config()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if sys.platform == "win32":
        import selectors

        with asyncio.Runner(
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())
        ) as runner:
            runner.run(run_once())
        return 0
    asyncio.run(run_once())
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Check how `retention.py`'s `run_once` builds its pool (the config attribute holding the DSN, and `make_async_pool`'s signature) and match it exactly; the names above are the likely ones, not verified.

- [ ] **Step 5: Run the tests**

Run: `PYTHONPATH=src REQUIRE_DB=1 "$VENV" -m pytest tests/test_intel_lessons_evidence.py tests/test_intel_evidence_store.py`
Expected: PASS.

- [ ] **Step 6: Lint, type-check, commit**

```bash
"$VENV" -m ruff format src/imageshield/intel/backfill_evidence.py tests/test_intel_lessons_evidence.py
"$VENV" -m ruff check src tests
PYTHONPATH=src "$VENV" -m mypy src/imageshield
git add src/imageshield/intel/lesson_store.py src/imageshield/intel/evidence_store.py src/imageshield/intel/backfill_evidence.py tests/test_intel_lessons_evidence.py
git commit -m "feat(intel): a retraction updates published evidence; backfill for older events

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

### Task 6: The `lesson_draft` run and the lesson routes

**Files:**
- Modify: `src/imageshield/intel/lesson_store.py`, `src/imageshield/intel/lessons.py`, `src/imageshield/intel/pipeline.py`, `src/imageshield/http/models.py`, `src/imageshield/http/routes/admin_intel.py`, `src/imageshield/http/deps.py` (if the lesson store needs a getter), `tests/intel_fakes.py`
- Create: `tests/test_intel_lessons_routes.py`

**Interfaces:**
- Produces routes (all `X-Admin-Service-Token`):
  - `POST /v1/admin/intel/lesson-drafts` `{event_ids?: UUID[] (≤ 20), operator}` → `202 {run_id}`.
  - `GET /v1/admin/intel/lessons?status=live|retired&cursor&limit` → `{lessons: [...], next_cursor}`.
  - `POST /v1/admin/intel/lessons/{lesson_id}/retire` `{reason, operator}` → `{lesson_id, status: "retired"}`; `404 lesson_not_found`.
- Produces: `IntelProposalKind` and `IntelRunKind` admit `"lesson"` / `"lesson_draft"`.

- [ ] **Step 1: Write the failing tests**

```python
"""The lesson_draft run and the lesson routes (spec 2026-10-15-intel-lessons-and-evidence §5)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from imageshield.intel.lesson_store import queue_lesson_draft
from imageshield.intel.schemas import ProposalLessonsOutput, ProposedEventLesson, ProposedLesson, ProposedLessonSection
from tests.intel_fakes import (
    FakeFetcher,
    FakeModel,
    make_deps,
    run_once,
    seed_signal,
    seed_threat_event,
    seed_threat_proposal,
)


def _lesson() -> ProposedLesson:
    return ProposedLesson(
        title="Spot a fake sponsorship offer",
        intro="A fake brand deal leads to a sign-in page that steals the account.",
        sections=[ProposedLessonSection(kind="what_to_do", items=["Sign in only from the app itself."])],
    )


async def _live_event(pool: AsyncConnectionPool) -> UUID:
    signals = [
        await seed_signal(pool, tags=("instagram",), publisher="a.example"),
        await seed_signal(pool, tags=("instagram",), publisher="b.example"),
    ]
    proposal = await seed_threat_proposal(pool, signal_ids=signals, status="applied", decided={})
    return await seed_threat_event(pool, proposal_id=proposal)


async def test_a_draft_run_writes_one_lesson_proposal_per_event(intel_pool: AsyncConnectionPool) -> None:
    event = await _live_event(intel_pool)

    def answer(payload: dict[str, Any]) -> ProposalLessonsOutput:
        [shown] = payload["events"]
        return ProposalLessonsOutput(event_lessons=[ProposedEventLesson(event=shown["key"], lesson=_lesson())])

    model = FakeModel(lessons_with=answer)
    await queue_lesson_draft(intel_pool, event_ids=(), operator="Op")
    result = await run_once(intel_pool, make_deps(intel_pool, FakeFetcher({}), model))
    assert result.status == "completed"
    async with intel_pool.connection() as conn:
        cur = await conn.execute("SELECT kind, status, target FROM intel_proposals WHERE kind = 'lesson'")
        rows = await cur.fetchall()
    assert rows == [("lesson", "pending", {"event_id": str(event)})]


async def test_an_event_with_a_lesson_is_not_drafted_again(intel_pool: AsyncConnectionPool) -> None:
    await _live_event(intel_pool)
    model = FakeModel(lessons_with=lambda p: ProposalLessonsOutput(
        event_lessons=[ProposedEventLesson(event=p["events"][0]["key"], lesson=_lesson())]
    ))
    await queue_lesson_draft(intel_pool, event_ids=(), operator="Op")
    await run_once(intel_pool, make_deps(intel_pool, FakeFetcher({}), model))
    await queue_lesson_draft(intel_pool, event_ids=(), operator="Op")
    result = await run_once(intel_pool, make_deps(intel_pool, FakeFetcher({}), model))
    assert result.outcome.get("lesson_draft_nothing_to_draft") == 1
    assert len(model.lesson_users) == 1


async def test_an_ended_event_takes_its_lesson_off_the_view(intel_pool: AsyncConnectionPool) -> None:
    event = await _live_event(intel_pool)
    async with intel_pool.connection() as conn:
        proposal = (await (await conn.execute(
            "SELECT proposal_id FROM threat_events WHERE event_id = %s", (event,)
        )).fetchone())
        assert proposal is not None
        await conn.execute(
            "INSERT INTO intel_lessons (proposal_id, event_id, kind, title, intro, sections, approved_by)"
            " VALUES (%s, %s, 'threat', 't', 'i', '[]', 'op')",
            (proposal[0], event),
        )
        shown = await (await conn.execute("SELECT count(*) FROM svc.v_lessons")).fetchone()
        assert shown == (1,)
        await conn.execute("UPDATE threat_events SET expires_at = now() - interval '1 minute' WHERE event_id = %s", (event,))
        gone = await (await conn.execute("SELECT count(*) FROM svc.v_lessons")).fetchone()
        assert gone == (0,)
```

Add route tests in the file that covers the admin intel routes with a FastAPI `TestClient` (find it with `grep -ln "proposals/{proposal_id}/decision\|/v1/admin/intel/proposals" tests/test_admin_intel*.py`), mirroring its fixtures: `POST /v1/admin/intel/lesson-drafts` with 21 ids → 422; with none → 202 and a `run_id`; `POST /lessons/{random}/retire` → `404 lesson_not_found`; a seeded lesson retired → it leaves `GET /lessons?status=live` and appears under `status=retired`; a decision refused `lesson_invalid` → 422 with `error.problems` listed.

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHONPATH=src REQUIRE_DB=1 "$VENV" -m pytest tests/test_intel_lessons_routes.py`
Expected: FAIL (`ImportError: queue_lesson_draft`).

- [ ] **Step 3: The store** (`lesson_store.py`)

```python
async def queue_lesson_draft(
    pool: AsyncConnectionPool, *, event_ids: Sequence[UUID], operator: str
) -> UUID:
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            "INSERT INTO intel_runs (kind, request, requested_by) VALUES ('lesson_draft', %s, %s)"
            " RETURNING run_id",
            (Jsonb({"event_ids": [str(e) for e in event_ids]}), operator),
        )
        row = await cur.fetchone()
    assert row is not None
    run_id: UUID = row[0]
    return run_id


_NEEDING_SQL = """
    SELECT e.event_id, e.kind, e.title, e.body, e.proposal_id FROM (
      SELECT event_id, 'threat' AS kind, title, body, proposal_id FROM threat_events
       WHERE status = 'active' AND starts_at <= now() AND expires_at > now() AND proposal_id IS NOT NULL
      UNION ALL
      SELECT event_id, 'protection', title, body, proposal_id FROM protection_events
       WHERE status = 'active' AND starts_at <= now() AND review_by > now()
    ) e
     WHERE (cardinality(%(ids)s::uuid[]) = 0 OR e.event_id = ANY(%(ids)s::uuid[]))
       AND NOT EXISTS (SELECT 1 FROM intel_lessons l WHERE l.event_id = e.event_id AND l.retired_at IS NULL)
       AND NOT EXISTS (SELECT 1 FROM intel_proposals p WHERE p.kind = 'lesson' AND p.status = 'pending'
                        AND p.target->>'event_id' = e.event_id::text)
     ORDER BY e.event_id
     LIMIT %(limit)s
"""


async def events_needing_lessons(
    conn: AsyncConnection[Any], event_ids: Sequence[UUID], *, limit: int
) -> list[tuple[UUID, str, str, str, UUID]]:
    cur = await conn.execute(_NEEDING_SQL, {"ids": list(event_ids), "limit": limit})
    return [tuple(row) for row in await cur.fetchall()]  # type: ignore[misc]


async def list_lessons(
    pool: AsyncConnectionPool, *, live: bool, limit: int, cursor: tuple[datetime, UUID] | None
) -> list[dict[str, Any]]:
    clause = "retired_at IS NULL" if live else "retired_at IS NOT NULL"
    after = " AND (approved_at, lesson_id) < (%(at)s, %(id)s)" if cursor else ""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT lesson_id, proposal_id, event_id, kind, topic, title, intro, sections, link_url,"
            " link_label, tags, is_global, approved_by, approved_at, retired_by, retired_at,"
            f" retire_reason FROM intel_lessons WHERE {clause}{after}"
            " ORDER BY approved_at DESC, lesson_id DESC LIMIT %(limit)s",
            {"limit": limit, **({"at": cursor[0], "id": cursor[1]} if cursor else {})},
        )
        return await cur.fetchall()


async def retire_lesson(
    pool: AsyncConnectionPool, lesson_id: UUID, *, operator: str, reason: str
) -> bool:
    """Retire a live lesson; True when it moved. Audited in the same transaction."""
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            "UPDATE intel_lessons SET retired_by = %s, retired_at = now(), retire_reason = %s"
            " WHERE lesson_id = %s AND retired_at IS NULL RETURNING 1",
            (operator, reason, lesson_id),
        )
        moved = await cur.fetchone() is not None
        if moved:
            await conn.execute(
                "INSERT INTO audit_log (actor_type, action, resource_id, metadata)"
                " VALUES ('operator', 'intel.lesson_retired', %s, %s)",
                (lesson_id, Jsonb({"operator": operator, "reason": reason})),
            )
    return moved
```

(Imports: `datetime`, `AsyncConnectionPool`, `dict_row`.) `retire_lesson` of an already-retired lesson returns False; the route answers 404 only when no row has that id at all, so check existence on False and answer `{status: "retired"}` for an already-retired one.

- [ ] **Step 4: The run** (`lessons.py`)

```python
class LessonDraftRequest(BaseModel):
    """``intel_runs.request`` of a lesson_draft run (spec §5). Never person data."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    event_ids: tuple[UUID, ...] = ()


async def run_lesson_draft(ctx: Any) -> Any:
    """Draft lessons for live events that have none (spec §5). Each becomes a ``lesson`` proposal
    with ``target.event_id``, decided in the queue. Imported lazily by pipeline.run (the module
    imports pipeline's helpers, as question_runs does)."""
    from imageshield.intel.pipeline import RunResult, _call_model, _Stop
    from imageshield.intel.generation import prompt_signal
    from imageshield.intel.lesson_store import events_needing_lessons
    from imageshield.intel.prompts import lesson_draft_request
    from imageshield.intel.proposal_store import fetch_linked_signals

    try:
        request = LessonDraftRequest.model_validate(ctx.run.request)
    except ValidationError:
        return RunResult("failed", ctx.outcome(), "request_unreadable")
    pool = ctx.deps.proposals.pool
    async with pool.connection() as conn:
        events = await events_needing_lessons(conn, request.event_ids, limit=MAX_LESSON_DRAFT_EVENTS)
        linked = await fetch_linked_signals(conn, [e[4] for e in events])
    if not events:
        ctx.counts["lesson_draft_nothing_to_draft"] += 1
        return RunResult("completed", ctx.outcome())
    active = {e[0]: [s for s in linked[e[4]] if s.status == "active"] for e in events}
    evidence = {s.signal_id: s for signals in active.values() for s in signals}
    system, user = lesson_draft_request(
        [
            {"key": str(event_id), "kind": kind, "title": title, "body": body,
             "signal_ids": [str(s.signal_id) for s in active[event_id]]}
            for event_id, kind, title, body, _ in events
        ],
        [prompt_signal(s) for s in evidence.values()],
        today=ctx.deps.clock().date().isoformat(),
    )
    try:
        call = await _call_model(ctx, lambda: ctx.deps.model.draft_lessons(system, user), capped=False)
    except _Stop as stop:
        return RunResult("refused" if stop.gate else "failed", ctx.outcome(), stop.reason)
    proposals: list[NewProposal] = []
    by_key = {str(e[0]): e for e in events}
    for item in call.output.event_lessons if call.output is not None else []:
        event = by_key.get(item.event)
        if event is None:
            ctx.counts["lesson_event_unknown"] += 1
            continue
        cited = active[event[0]]
        lesson = lesson_from(item.lesson, cited, ctx.counts)
        if lesson is None:
            continue
        proposals.append(
            NewProposal(
                kind="lesson",
                target=LessonTarget(event_id=event[0]).model_dump(mode="json", exclude_none=True),
                suggested=LessonSuggested.model_validate({"lesson": lesson}).model_dump(mode="json"),
                rationale=f"A lesson drafted for the live event {event[2]!r} from its own evidence.",
                signal_ids=tuple(s.signal_id for s in cited),
            )
        )
    vocabulary = ctx.vocabulary
    result = await ctx.deps.proposals.write_generated(
        ctx.run.run_id,
        proposals,
        against_scoring_version=vocabulary["scoring_version"] if vocabulary else "",
        against_release_no=vocabulary["release_no"] if vocabulary else 0,
        model_id=call.answered_by,
        prompt_version=LESSON_DRAFT_PROMPT_VERSION,
    )
    ctx.counts["lessons_drafted"] += 0 if result is None else len(result.written)
    return RunResult("completed", ctx.outcome())
```

Before writing it, read three things and match them exactly rather than trusting the names above: how `run_question` reaches the pool (if `ctx.deps.proposals` exposes no `pool`, add a `PipelineDeps.lessons` store or give the run its own `PostgresProposalStore`-style method), how `_generate` reads `vocabulary.scoring_version` / `release_no` from `ctx.vocabulary` (it calls `parse_vocabulary(ctx.vocabulary)`; do the same), and whether `write_generated` requires `against_*` for a non-weight kind (it stores them only for weight kinds; pass what `_generate` passes). Add `LESSON_DRAFT_PROMPT_VERSION = "lesson-draft-v1"` to `prompts.py`.

In `pipeline.run`, add before the `else:` fallback:

```python
        elif claimed.kind == "lesson_draft":
            # One model call, no reading (spec 2026-10-15 §5): returns its own result.
            from imageshield.intel.lessons import run_lesson_draft

            return await run_lesson_draft(ctx)
```

- [ ] **Step 5: The routes** (`http/models.py`, `routes/admin_intel.py`)

Add `"lesson"` to `IntelProposalKind` and `"lesson_draft"` to `IntelRunKind`. Add models:

```python
class IntelLessonDraftRequest(ServiceModel):
    event_ids: list[UUID] = Field(default_factory=list, max_length=MAX_LESSON_DRAFT_EVENTS)
    operator: str = Field(min_length=1, max_length=64)


IntelLessonStatus = Literal["live", "retired"]
```

Routes, after the protection-events block:

```python
# -- lessons (spec 2026-10-15-intel-lessons-and-evidence §5) ---------------------


@router.post("/lesson-drafts", status_code=202)
async def draft_lessons(body: IntelLessonDraftRequest, request: Request) -> dict[str, UUID]:
    run_id = await queue_lesson_draft(
        get_pool(request), event_ids=body.event_ids, operator=body.operator
    )
    log.info("intel.lesson_draft_queued", run_id=str(run_id), events=len(body.event_ids))
    return {"run_id": run_id}


@router.get("/lessons")
async def list_intel_lessons(
    request: Request,
    status: IntelLessonStatus = Query(default="live"),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> dict[str, Any]:
    rows = await list_lessons(
        get_pool(request), live=status == "live", limit=limit, cursor=_decode_cursor(cursor)
    )
    next_cursor = (
        _encode_cursor(rows[-1]["approved_at"], rows[-1]["lesson_id"]) if len(rows) == limit else None
    )
    return {"lessons": rows, "next_cursor": next_cursor}


@router.post("/lessons/{lesson_id}/retire")
async def retire_intel_lesson(
    lesson_id: UUID, body: IntelRetractRequest, request: Request
) -> dict[str, Any]:
    pool = get_pool(request)
    if not await retire_lesson(pool, lesson_id, operator=body.operator, reason=body.reason):
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT 1 FROM intel_lessons WHERE lesson_id = %s", (lesson_id,))
            if await cur.fetchone() is None:
                raise ServiceError(404, "lesson_not_found", "No lesson with this id.", retryable=False)
    return {"lesson_id": lesson_id, "status": "retired"}
```

`get_pool` stands for however this module reaches the pool today: read `http/deps.py` and use the existing getter (or add `get_lesson_pool` beside `get_protection_store`, the way the protection routes get theirs). `_decode_cursor` / `_encode_cursor` are the module's existing helpers.

- [ ] **Step 6: Run the tests**

Run: `PYTHONPATH=src REQUIRE_DB=1 "$VENV" -m pytest tests/test_intel_lessons_routes.py tests/test_admin_intel_routes.py tests/test_intel_worker.py`
Expected: PASS (use the actual names of the admin-route and worker test files).

- [ ] **Step 7: Lint, type-check, commit**

```bash
"$VENV" -m ruff format tests/test_intel_lessons_routes.py
"$VENV" -m ruff check src tests
PYTHONPATH=src "$VENV" -m mypy src/imageshield
git add -A src/imageshield tests/intel_fakes.py tests/test_intel_lessons_routes.py
git commit -m "feat(intel): the lesson_draft run and the lesson routes

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

### Task 7: Docs, the spec's corrections, the affected tests once

**Files:**
- Modify: `CLAUDE.md` (§3 grant list, §6 intel row), `PROXY_INTEGRATION.md` (§6), `SCHEMA.md` (§2c), `docs/superpowers/specs/2026-10-15-intel-lessons-and-evidence-design.md`

- [ ] **Step 1: Amend the manuals in place** (never overwrite)

- `CLAUDE.md` §3, the proxy's grant list: after `v_active_scoped_events (0042 …)`, add "`v_evidence`, `v_lessons` and `v_intel_weekly_totals` (0055: approved lessons, the evidence snapshot behind every approved event and lesson, and the week's research counts; events and lessons, never people)".
- `CLAUDE.md` §6, the intel row: append "*Lessons, 2026-10-15:* every threat and protection proposal carries a drafted lesson (a third call, `propose-v14`), decided with the event; a seventh kind, `lesson`, drafts general lessons; `lesson_draft` drafts for live events. Approval snapshots the evidence (`intel_published_owners` / `_evidence`); a retraction recomputes it. Spec `docs/superpowers/specs/2026-10-15-intel-lessons-and-evidence-design.md`."
- `PROXY_INTEGRATION.md` §6 and `SCHEMA.md` §2c: the three views with their columns exactly as Task 1's migration creates them.

- [ ] **Step 2: Correct the spec in place**

In §11 replace "0055's down refuses while `intel_lessons` holds a row … inert without a reader." with: "*Corrected 2026-10-15 while planning:* 0055's down DELETES the lesson rows, lesson proposals and `lesson_draft` runs (0053/0054 precedent): every DB test fixture migrates the shared database down, so a refusing down would fail the suite." In §6 replace `scripts/backfill_published_evidence.py` with `python -m imageshield.intel.backfill_evidence` (this repo keeps one-shot CLIs as modules, like `search/retention.py`). In §5 add "*Planning, 2026-10-15:* the propose step drafts lessons in a THIRD structured call (`ProposalLessonsOutput`): the two existing halves sit under the API's grammar ceiling, and their size pin says splitting further is the remedy." In §4 replace "and the proposal read says so (`lesson_missing: true`)" with "and the proposal read shows `suggested.lesson: null`, which the console reads as no lesson drafted; the run's outcome counts it (`lesson_missing`)".

- [ ] **Step 3: Run the affected files once and record what never ran**

```bash
PYTHONPATH=src REQUIRE_DB=1 "$VENV" -m pytest tests/test_intel_lessons_schema.py tests/test_intel_lessons_rules.py tests/test_intel_lessons_generation.py tests/test_intel_lessons_decisions.py tests/test_intel_lessons_evidence.py tests/test_intel_lessons_routes.py tests/test_intel_model.py tests/test_intel_generation.py tests/test_intel_quiz_questions.py tests/test_boundaries.py
"$VENV" -m ruff check src tests
PYTHONPATH=src "$VENV" -m mypy src/imageshield
```

Expected: all pass. Report the full suite as NOT run (owner preference), and name any file touched but not in that list.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md PROXY_INTEGRATION.md SCHEMA.md docs/superpowers/specs/2026-10-15-intel-lessons-and-evidence-design.md
git commit -m "docs(intel): lessons, published evidence and the three views; spec corrections

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

## Deploy (after the owner approves, not part of the tasks)

Services first, then the backend half. 0055, then services and services-worker together (`propose-v14`, the decision snapshot, the routes); then `python -m imageshield.intel.backfill_evidence` once per environment. On the way down: the backend first, then services, then 0055's down.
