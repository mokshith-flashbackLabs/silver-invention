# Likeness intel — design

*2026-09-27. Approved in conversation, section by section, and revised the same day in three passes:*
1. *after a first adversarial review (88 verified findings), before owner review;*
2. *at owner review, to adapt to quiz changes (decision 10): exposure tags replace the fixed platform list;*
3. *after a second adversarial review of that change (26 verified findings, plus 2 found in unverified leftovers).*

*Awaiting the owner's final look.*

*Two repos. This one, `image_flashbacklabs`, carries most of the work. The backend half has its own companion spec,
`image_backend/docs/superpowers/specs/2026-09-27-likeness-intel-backend-design.md`. Each half ships under its own
repo's rules, as two coordinated changes (workspace `CLAUDE.md`). The step table in §8 is shared by both specs.*

## 0. What was asked, and why

**Owner, 2026-09-27:** today the Likeness Health Score gives a fixed number of points per quiz answer. The numbers
were chosen by hand, have no outside backing, are not standardized, and never move. The owner wants the score to be
the selling point, which means making it **evidence-backed and live**. Two systems:

1. **In the control room**, when quiz questions or options change, a system *suggests* the points for each option,
   says why, and cites its sources.
2. **A monitor** reads what is happening to the platforms and practices people told us about: ToS and privacy-policy
   changes, breaches, news, new deepfake tooling, new protections, law, research. It moves each person's score up or
   down according to their own answers. If they picked Instagram, an Instagram event moves them and nobody else. Good
   news can raise a score; bad news lowers it.

The feature lives in services. ToS changes and LinkedIn were given as examples, not as the scope.

### Decisions taken in conversation

| # | Question | Decision |
|---|---|---|
| 1 | How automatic? | **A human approves everything.** The bot only proposes. |
| 2 | What does the user see? | **The movement, in score history, with plain server-written copy.** Citations and sources stay in the control room. |
| 3 | Where does material come from? | **A source registry that operators curate, plus model-driven web search.** |
| 4 | How does good news raise a score? | **Both ways.** A platform-specific improvement shrinks that option's deduction. A broad protection adds a small capped credit. |
| 5 | Which repo does what? | **Services gathers and proposes. The backend applies**, and the score keeps one implementation. |
| 6 | Model access | **Claude Platform on AWS**: Anthropic-operated, SigV4 with the task role, AWS Marketplace billing. Amazon Bedrock was considered and rejected because it offers no web search or web fetch tool. |
| 7 | Changes forced by the code, confirmed before writing | Protection credits are events, not config entries (§3.7). Web search only discovers URLs; we fetch everything ourselves (§4.2). Weight changes apply to `mutable` questions only (§4.5). Attribution runs the engine twice on a weights-only version change (backend spec). PII in excerpts is dropped (§6.3). Cost goes through the provider gate (§5). |
| 8 | The "ask first" items in §3 rule 6 | **Approved:** a new `intel-worker` container (§4.1, no fourth queue); reversing "services deployables stay LLM-free" (§1). |
| 9 | A third dependency | **Approved at spec review:** `publicsuffixlist` (pinned, bundled list, no network), so corroboration counts publishers by registrable domain (§3.6). |
| 10 | **It must adapt when the quiz changes** (owner, at spec review) | Nothing in intel may assume a fixed quiz. The things an event can target are **exposure tags**: live data the backend owns, which an operator creates and maps to *any* option of *any* question, with no deploy and no retake (§3.1). Every proposal is generated against the vocabulary the backend pushes after each change, and services react to every change on their own (§4.9). A question or option added tomorrow becomes a target for evidence, suggestions and events tomorrow. |
| 11 | **Sources come from the questions** (owner, 2026-09-30, after steps 0–1 were built) | Today's platform question may not exist tomorrow, so sources are not a registry curated apart from the quiz. Pressing Suggest points also **proposes sources for each option**, lets the operator **add more**, **validates** that each can be read, and only then reads them and suggests points. Chosen sources then join the weekly checks, and they **pause on their own** when the options they belong to leave the quiz (§4.10). The registry stays as the store; `POST /sources` remains for general, untagged sources. |

**Why the human gate is the whole design.** A false positive here is a psychological injury (§1). A model misreading
one news story must not lower thousands of scores unattended. The model writes rows into intel tables and nothing
else. No score moves until a named operator approves exact stored values, and the arithmetic stays in the backend's
deterministic engine. That is what keeps the model *upstream of a human* rather than *in the scoring path*.

## 1. Governance amendments — land with the first code, in the same PR

Three recorded decisions say "no LLM": the 2026-08-20 campaigns spec ("Services deployables stay LLM-free"), the
2026-08-27 articles spec ("no LLM"), and this repo's §2, §10 and `pyproject.toml:14`. This feature reverses them for
services, by owner decision. The amendments are dated and written in place, never as a silent edit:

- **`CLAUDE.md` §2.** "any LLM SDK" is replaced by: *the `anthropic` SDK, confined to `intel/model.py`, calling Claude
  Platform on AWS.* Add Claude Platform on AWS to the "External dependencies we call" line.
- **`CLAUDE.md` §6.** Move the "Automated threat-event feeds" row out of *do not build yet*, with the note: *built
  2026-09-27 as model-proposed, human-approved intel proposals (this spec). Nothing auto-publishes.* Add an "Intel"
  row to the build-now column.
- **`CLAUDE.md` §10 "Code over LLM".** Add: *The one sanctioned place a model runs is `intel/`, and there it only
  proposes. It reads public documents, never person data. Its output is a proposal that a named operator must approve.
  Detection, matching, banding, dedup and every score stay deterministic.*
- **`pyproject.toml`**, comment and dependency list:
  - Add `anthropic[aws]`, pinned to a 1.x release verified in §8 step 0. The 1.x SDK runs on `httpx2`, so it arrives
    as a second HTTP stack beside `httpx`. That is accepted, and the fakes sit at our own `IntelModel` seam (§4.4),
    never at the transport.
  - Add `publicsuffixlist`, pinned exactly (decision 9).
- **`INVARIANTS.md`**: add #48–#50 and amend #9 and #11, as in §7.
- **The articles decision stands for articles.** The feed is still operator content, not model output.
- **Backend** amendments are in the backend spec.

## 2. Shape

```
                      ┌───────────── services ─────────────────────────────────────────────┐
 operator ─▶ backend  │  API (/v1/admin/intel/*)      intel-worker              fetcher     │
 /v1/admin/intel/* ──▶│  DB only, no model, no     ─▶ DB + model, never   ─▶  third-party   │
                      │  third-party fetch            fetches third-party       fetch only,  │
                      │                               URLs itself               no DB,       │
                      │                                   │                     no model     │
                      │                                   ▼                                  │
                      │                    Claude Platform on AWS (SigV4)                    │
                      └──────────────────────────────────┬─────────────────────────────────┘
                                   signals ─▶ proposals ─┘
          approved threat/protection event ──▶ svc.v_active_scoped_events ──▶ backend matches
                                                                             its own quiz answers
          approved weight change ──▶ backend weights-only release ──▶ backend recompute sweep
```

**Three processes, each missing what the others hold**, following the pattern INVARIANTS #11 already uses:

| Process | Holds | Never holds |
|---|---|---|
| `services` API | DB. Serves `/v1/admin/intel/*` and queues runs. | The model client, third-party fetch |
| `intel-worker` *(new container)* | DB and the model client | Any in-process third-party fetch. Pages arrive through the fetcher's HTTP API. |
| `fetcher` | SSRF-guarded egress to third-party URLs | DB credentials, the model client |

**Services never sees a person in this feature.** Prompts carry public document text, signals and the backend's
published scoring vocabulary: the questions, options and weights (which `GET /v1/quiz` already serves publicly in
part), the exposure-tag registry and the option-to-tag map. Services never receives a quiz answer. Matching a tag to
a person happens in the backend.

**Data is kept indefinitely as the evidence trail** (services §5: never DELETE, provenance). The backend's
`scoring_provenance` cites proposal ids forever, so nothing an approval rested on may disappear. Intel's
`provider_calls` rows fall under the existing `raw_response` retention job, and for kind `llm` that column holds
metadata only (§5).

## 3. Data model

### 3.0 Migrations

**`scripts/migrate.py` runs every pending migration inside one transaction.** The connection's first statement opens
a transaction, so each per-file `conn.transaction()` is a savepoint. A value added to an enum type therefore cannot be
used by a later pending file in the same run. So **`providers.kind` stops being an enum**. The only code that names
the type is the `CUSTOM_TYPES` pin in `tests/test_migrations.py:47`.

> **Renumbered 2026-09-30.** origin/main took services 0038 (global threats reach late subjects) before this branch merged, so the intel schema is **0039** and the claude_intel budget **0040**. The step-3 and step-4 migrations take "the next free number at the time"; references to 0038 elsewhere in this spec mean 0039.

| File | Ships in | Contents |
|---|---|---|
| `0039_likeness_intel` | step 1 | `providers.kind` becomes TEXT (below); the `claude_intel` row; `provider_calls.intel_run_id`; every `intel_*` table in §3.1–§3.6 and §3.8; the `intel_rw` role and its grants. **No threat or protection change, and no view.** |
| `<next free>_intel_scoped_threats` | step 3 | `threat_events.tags` and `proposal_id`; the relevance-CHECK swap; `intel_rw` grants on `threat_events`; `svc.v_active_scoped_events` with **the threat half only**, and its grant. |
| `<next free>_intel_protection_events` | step 4 | `protection_events`; the view dropped and re-created as the UNION, **with its grant re-issued in the same file**, because `DROP VIEW` loses it. |

If steps 3 and 4 ship together, the first of the two numbers carries both halves and the second is not used. The four hand-maintained contract
pins change in the file that creates the view (§3.7).

**0039 converts `providers.kind`:** `ALTER COLUMN kind TYPE text USING kind::text`, then `CHECK (kind IN
('image_search','face_search','classifier','llm'))`, then `DROP TYPE provider_kind`. The `CUSTOM_TYPES` pin loses
`provider_kind`. The down leg recreates the type and casts back. It runs after the down has removed the only `llm`
row (§3.9), and after it has dropped the calibration CHECK that names `'llm'`.

**One module role, `intel_rw`** (NOLOGIN), created idempotently. It gets enumerated table grants and no `DELETE`, and
is granted to `app_services` conditionally, following the 0026 and 0015 pattern. **Every intel table's grant is
written in the migration that creates it.** A DB test connects as `app_services` and exercises each grant, including
the column grant on `content_urls` (§6.1), because superuser test runs hide a missing grant (0035).

**Column names pass the schema lint (#9):** `*_text` and `*_url` are fine. No `*_data`, `*_blob`, `*_bytes`, `*_b64`
or `bytea`.

### 3.1 Exposure tags — the vocabulary events target, owned by the backend as data

An **exposure tag** is something a person can be exposed through: a platform (`instagram`, `linkedin`, `bumble`), a
kind of service (`dating_apps`, `cloud_photo_backup`) or a practice (`public_profile_photo`). **Tags are live data, not
code.**
- **The registry** (slug, label, description, kind, retired) lives in the backend (`profile.exposure_tags`). So does
  **the option-to-tag map** (`profile.option_tags`), which says which answer of which question exposes a person to
  which tags.
- **Both are edited in the control room**, with no deploy in either repo and no quiz retake (backend spec §2).
- **Services receive both** in every vocabulary push (§3.8).

**Rules that keep a data vocabulary as safe as the closed list it replaces:**
- **A slug is immutable and never deleted.** It matches `^[a-z][a-z0-9_]{0,39}$`, the same pattern on both sides, so
  `x` is valid.
- **Retiring a tag refuses *new* uses only.** A retired tag cannot be added to a mapping, a new proposal or an
  operator's edited `values`. An event, mapping or pending proposal that already names it keeps working unchanged, so
  retiring moves nobody.
- **Every `tags text[]` column** in this repo carries `CHECK (intel_tags_well_formed(tags))`, an immutable SQL function
  that checks each element's shape and that the array holds no duplicates.
  - **Membership is checked in code** against the loaded vocabulary, with one diff rule: a tag a write *adds* must be
    registered and not retired, while a tag already in the proposal's own `target` may be retired. That happens at
    generation and again on every operator's `values`, and it is refused as `422 unknown_tag` or `422 tag_retired`,
    naming the slugs.
  - The database cannot check membership, because the registry lives in the other repo. The backend re-checks
    membership before relaying any operator write that names tags. A tag that fails is refused, never silently
    matched to nobody (the threat-domain lesson).
- **"Mapped"** means at least one option of the *live* quiz maps to the tag. A registered but unmapped tag moves
  nobody, and every read says so (`unmapped_tags`, and the backend's `reach`).

**What this buys.** When a quiz-editor publish adds "LinkedIn" to a question and an operator maps it to `linkedin`,
**every active event already tagged `linkedin` starts reaching those people** on their next recompute, and every
pending `coverage_gap` about LinkedIn closes itself (§4.9). Nothing in services is edited or deployed.

### 3.2 Sources

`intel_sources`:

| Column | Notes |
|---|---|
| `source_id` | UUID PK |
| `kind` | TEXT NOT NULL: `policy_page` · `feed` · `news` · `breach_index` · `regulator` · `research` · `search_query` |
| `source_url` | Canonicalised with `search/urlhash.py`. `CHECK ((kind = 'search_query') = (source_url IS NULL))` |
| `url_hash`, `normalisation_version` | `CHECK ((source_url IS NULL) = (url_hash IS NULL AND normalisation_version IS NULL))`. Unique where not NULL. The functions are reused; the `content_urls` **table is not** (it feeds recheck's allowlist and threat matching). |
| `query_text` | `CHECK ((kind = 'search_query') = (query_text IS NOT NULL))`. Refused with `422 query_names_a_person` if it contains a phone- or email-shaped run (§6.1). |
| `tags` | A hint for the extractor. May be empty. Well-formed by CHECK; membership checked on write. |
| `check_every_hours` | `CHECK BETWEEN 6 AND 720` |
| `next_check_at`, `enabled`, `created_by`, `created_at`, `updated_at` | |
| `terms_note` | TEXT NOT NULL, `CHECK (length(terms_note) >= 10)`. §7.7: whoever adds a source records that automated access to it is permitted. |
| `last_content_sha256` | The normalised hash from the last *consumed* check (§4.3). Not used for `policy_page` (its snapshot holds it) or `feed` (unseen items gate it instead). |
| `last_checked_at`, `last_run_status`, `consecutive_failures` | `consecutive_failures SMALLINT NOT NULL DEFAULT 0`, reset on success |
| `disabled_reason` | `CHECK (disabled_reason IN ('too_short','unreachable'))`, `CHECK (disabled_reason IS NULL OR NOT enabled)`. An operator's `PATCH enabled=true` clears it. |

**Sources are added through the admin API, never seeded in a migration.** The phone-shaped build gate
(`test_boundaries.py:395`) fails any migration literal holding a digit run, such as a dated URL or an article id.

*Amended 2026-09-30 (decision 11):* sources tied to a quiz option now arrive through the Suggest points flow (§4.10),
which adds `origin` and `proposed_for` and a third `disabled_reason`, `unmapped`, for a source paused because none of
its tags maps to a live option. `POST /sources` stays for general sources.

### 3.3 Snapshots — only for `policy_page`

`intel_snapshots`, one row per source (latest only): `source_id` PK/FK, `content_sha256`, `snapshot_text`,
`content_type`, `truncated`, `fetched_at`.

- **Only the latest snapshot is kept.** Diffing needs one predecessor.
- **Phone-shaped and email-shaped runs are masked before storage** (`«masked»`), using `redaction.py`'s patterns plus
  an email pattern. Masking is applied identically to every version, so diffs stay valid. A snapshot exists to detect a
  change and is never quoted from.
- **It is replaced only when its change is consumed** (§4.3), never after a failed or gate-refused run.
- Every other source kind persists **no document text**. It is held in memory for one extraction and dropped. What
  remains is the URL, a content hash, the time fetched, the masked title and the verified excerpts.

### 3.4 Runs

`intel_runs`:

| Column | Notes |
|---|---|
| `run_id` | UUID PK |
| `kind` | `source_check` · `discovery` · `adhoc_url` · `weight_suggestion` · `renewal_check` · `gap_regenerate`; step 5 adds `source_proposal` and `source_validation` (§4.10) |
| `source_id` | NULL for every kind except `source_check` and `discovery` |
| `request` | JSONB. `adhoc_url`: `{url}`. `weight_suggestion`: the question payload (§4.6). `renewal_check`: `{event_id}`. `gap_regenerate`: `{coverage_gap_id, tag, signal_ids}` (§4.9). **Never person data.** |
| `vocabulary_release_no`, `vocabulary_map_version` | The vocabulary pair the run loaded, NULL when none existed. Every signal (through its document) and every proposal, event kinds included, inherits it, so what registry an output was produced against is always answerable. |
| `status` | `queued` · `running` · `completed` · `failed` · `refused` |
| `attempts` | SMALLINT NOT NULL DEFAULT 0, incremented by each claim |
| `lease_expires_at` | Claimed with `FOR UPDATE SKIP LOCKED` |
| `proposals_written_at` | Set in the same transaction that writes the run's proposals (§4.3) |
| `requested_by` | The operator's display name, or `schedule` |
| `outcome` | JSONB counts: documents fetched, unchanged, signals kept, quotes dropped *by reason*, masks *by field*, proposals written, drops *by reason*, model calls, cost, `refused_by: 'model' \| 'gate'` |
| `error_code`, `created_at`, `started_at`, `completed_at` | |

- **One open check per source.** A partial unique index `ON intel_runs (source_id) WHERE status IN ('queued','running')`.
  `POST /sources/{id}/check` returns the open run's id rather than a second run, so two clicks buy one run.
- **Poison runs end.** Only runs with `attempts < MAX_RUN_ATTEMPTS` (a code constant, 3) are claimable. An expired
  lease at the cap becomes `failed` with `error_code = 'attempts_exhausted'`, logged once at error level, mirroring
  the outbox dead-letter.
- **The single-worker assumption is written down.** `services-worker` runs desired count 1. The lease protects
  against a crashed worker, not against two live ones, and no heartbeat is needed under that assumption. The
  scheduler step still advances `next_check_at` with one atomic `UPDATE … RETURNING`, so an overlapping task during a
  rolling deploy cannot create duplicate runs for one source.

### 3.5 Documents, signals, excerpts

`intel_documents`, one per page fetched and read:
- `document_id`, `run_id`, `document_url`, `final_url`, `url_hash`, `normalisation_version`
- `publisher_domain`: see §3.6
- `trust`: `listed` (from the registry, pasted by an operator, or re-verified on renewal) or `web` (found by model
  search)
- `content_sha256`, `truncated`, `title` (masked, §6.3), `published_at` (nullable), `fetched_at`
- **`UNIQUE (run_id, url_hash)`.** A reclaimed run skips any document already recorded for its run.

`intel_signals`, one piece of evidence:
- `signal_id`, `document_id`
- `category`: `policy` · `incident` · `tooling` · `protection` · `law` · `research`
- `direction`: `risk_up` · `risk_down` · `neutral`
- `tags`: the exposure tags the evidence concerns, from the registry loaded at extraction. May be empty for a signal
  about everyone.
- `unregistered_subjects`: text[] of things the evidence concerns that **no registered tag covers**, such as a platform
  nobody has tagged yet, masked (§6.3). This is what `coverage_gap` is built from.
- `summary`: model-written, ≤ 500 characters, masked (§6.3), **control room only**
- `model_id`: **the model that actually answered**, `response.model`, never the configured id
- `prompt_version`
- `status`: `active` · `retracted`. `retracted_by`, `retracted_at`, `retract_reason`, with a shape CHECK in the style
  of `protection_events`.
- `created_at`

`intel_excerpts`, the citations:
- `excerpt_id`, `signal_id`
- `quote_text`: 20–600 characters
- `char_start`, `char_end`: offsets into the normalised document text. `CHECK (char_end > char_start)`.
- `quote_sha256`

A signal with zero surviving excerpts is never written (§4.3).

**Signal retraction is a real operator write** (§4.7). A retracted signal is excluded everywhere: proposal-generation
context, weight-suggestion retrieval, the corroboration count and the `coverage_gap` threshold. A pending proposal
left with no active signal reads `approvable = false, why_not = 'evidence_retracted'`, and approving it is `409
proposal_evidence_retracted`. **An approved or applied proposal is never changed automatically.** Its detail read
shows `evidence_retracted: true` so an operator can decide whether to retract the event or roll the weight back.

### 3.6 Proposals

`intel_proposals`:

| Column | Notes |
|---|---|
| `proposal_id` | UUID PK |
| `kind` | `weight_change` · `threat_event` · `protection_event` · `weight_suggestion` · `coverage_gap` |
| `status` | `pending` · `approved` · `rejected` · `superseded` · `applied` · `delivered` |
| `target` | JSONB. **What the proposal is about, never the model's numbers.** Validated per kind by pydantic (§4.5). |
| `suggested` | JSONB. **The model's numbers and text**, kept forever, even when an operator edits them |
| `decided` | JSONB. **The exact values an operator approved.** The only field anything downstream applies. |
| `rationale` | Model-written, masked, control room only |
| `against_scoring_version`, `against_release_no` | `weight_change` and `weight_suggestion` only: the vocabulary snapshot it was generated against (§3.8) |
| `run_id`, `model_id`, `prompt_version`, `created_at` | |
| `decided_by`, `decided_at`, `decision_reason` | |
| `applied_ref` | The `scoring_version` for `weight_change`; the `event_id` for events |

**Shape rules, in the schema, so no route can forget them:**
- `status IN ('approved','rejected','applied')` ⟹ `decided_by`, `decided_at` and `decision_reason` are NOT NULL.
- `status = 'pending'` ⟹ those three are NULL.
- `status IN ('approved','applied') AND kind IN ('weight_change','threat_event','protection_event')` ⟹ `decided IS NOT
  NULL`.
- `status = 'delivered'` ⟺ `kind = 'weight_suggestion'`. A suggestion is born `delivered` (below).

So **an approved proposal cannot exist without a name on it and its exact numbers stored.**

**Target, suggested and decided, per kind:**

| Kind | `target` | `suggested` | `decided` |
|---|---|---|---|
| `weight_change` | `{question_key, option, current}` | `{delta}` | `{delta}`. An edit may change only `delta`. |
| `threat_event` | `{tags}` | `{kind, title, severity, expires_in_days}` | the same keys, as inserted, plus `tags` |
| `protection_event` | `{tags, is_global, renews_event_id?}` | `{title, strength, review_in_days}` | the same keys, as inserted, plus `tags`, `is_global` and `applies_regardless_of_location` |
| `weight_suggestion` | `{question_key, options: [{option, deduction \| null, rationale, signal_ids, suggested_tags, new_tag?}]}` | — | — |
| `coverage_gap` | `{subject, suggested_tag?: {slug, label, kind}, suggested_question?, regenerated_by_run_id?}` | — | — |

**Supersession needs a reason.** `supersede_reason` is non-NULL exactly when `status = 'superseded'`. It is one of
`newer_proposal`, `cell_changed` or `resolved_by_quiz` (§4.9).

**`intel_proposal_signals (proposal_id, signal_id)` PK.** Every proposal needs at least one when it is written. Rows
may be **added** later to a still-pending event proposal (the `attach` path, §4.3). Adding evidence never changes
`target`, `suggested` or `rationale`, so an operator never approves text that changed under them.

**At most one approved, unapplied weight change per cell.** A partial unique index on `((target->>'question_key'),
(target->>'option')) WHERE kind = 'weight_change' AND status = 'approved'`. Two reviewers approving different
proposals for one cell get one success and one clean `409 proposal_cell_awaiting_publish`. An approved, not yet
applied `weight_change` can be **withdrawn** to `rejected` through the decision route, so a stale approval never
blocks its cell forever.

**Supersession, always in code, never judged by the model:**
- A new pending `weight_change` for the same `(question_key, option)` supersedes the older pending one.
- A new `weight_suggestion` for the same `question_key` supersedes the older delivered one.
- A new pending `coverage_gap` with the same normalised `subject` supersedes the older pending one.

**`weight_suggestion` is born `delivered`.** It is advice for the quiz editor, not a decision, so it never enters the
review queue: `GET /proposals` omits the kind unless it is asked for explicitly. It is never decidable (`409
proposal_not_decidable`) and never dismissable, because a published config may already cite it. Provenance lives in
the backend.

**Publishers and corroboration.**
- `publisher_domain` is the **registrable domain** (eTLD+1, ICANN section of the Public Suffix List only), computed by
  one function, `intel/publisher.py`, from the fetcher's **`final_url`** after redirects. That uses `publicsuffixlist`
  with its bundled list and **never fetches the list at runtime**. The intel-worker makes no third-party request.
- Private suffixes (`github.io`, `blogspot.com`) collapse to their owner, so they count as one publisher. An IP literal
  or a bare suffix falls back to the hostname.
- The value is stored once and never recomputed at decision time.
- **The corroboration predicate** is `intel/corroboration.py:uncorroborated(signals) -> bool`, using the code constant
  `CORROBORATION_MIN_PUBLISHERS = 2` from `intel/bounds.py`.
- **The approvability predicate** is one pure function, `intel/approvable.py:why_not(proposal, active_signals,
  vocabulary) -> str | None`. It answers every refusal knowable at read time, in a fixed order: `not_decidable`,
  `evidence_retracted`, `uncorroborated`, `tags_unmapped` (event kinds, §4.5).
- **Every caller uses them**: the `approvable` and `why_not` fields on both proposal reads, the in-transaction re-check
  on decision, and the per-option `corroborated` flag on weight suggestions (which calls the corroboration predicate
  per option). So **a proposal the panel shows as approvable is never one the decision refuses**, except for races the
  transaction itself catches (`proposal_not_pending`, `proposal_cell_awaiting_publish`).

### 3.7 Events and the contract view

**`threat_events` gains two columns (step 3's migration):**
- `tags text[] NOT NULL DEFAULT '{}'`, with the well-formed CHECK (§3.1);
- `proposal_id uuid NULL UNIQUE REFERENCES intel_proposals`. It is nullable because operators can still create events
  by hand.

**The relevance CHECK.** The unnamed CHECK `(is_global OR cardinality(domains) > 0)` from 0022:65 is **looked up by
definition in `pg_constraint` inside a DO block**, never assumed to be `threat_events_check1`. It is replaced with
`is_global OR cardinality(domains) > 0 OR cardinality(tags) > 0`. `ThreatEventCreateRequest._domains_or_global`
changes the same way, and `tags` is added to the create model (shape-validated), the store Protocol, the route kwargs
and `ThreatEventItem`.

**Step 3's down leg, in order:**
1. **Guard.** RAISE if any row has `cardinality(tags) > 0 AND NOT is_global AND cardinality(domains) = 0 AND status IN
   ('active','draft')`. It checks `status` alone, not `expires_at`: the code never writes `expired`, so an event past
   its expiry still reads `active`, and refusing on it is the conservative choice.
2. Drop the widened CHECK, looked up by definition.
3. **Restore the old CHECK as `NOT VALID`.** Retracted tag-only rows keep their shape, and nothing updates them,
   because retract only touches active rows. Validating the constraint would fail on them.

**`intel_rw` gains `SELECT, INSERT` on `threat_events`**, because approving a proposal creates the event in one
transaction. **The follow-up migration that drops the dormant score tables must re-home the `threat_events` and
`threat_event_matches` grants off `score_rw` before revoking it.** That note goes into `SCHEMA.md`'s open-items list.

**`protection_events`, new (step 4's migration):**

| Column | Notes |
|---|---|
| `event_id` | UUID PK |
| `title`, `body` | `body` is control room only |
| `strength` | SMALLINT, 1–5 |
| `tags`, `is_global` | Well-formed CHECK. `CHECK (is_global OR cardinality(tags) > 0)`. `CHECK (NOT (is_global AND cardinality(tags) > 0))` |
| `starts_at`, `review_by` | `CHECK (review_by > starts_at AND review_by <= starts_at + interval '366 days')` |
| `status` | `active` · `retracted` |
| `proposal_id` | **NOT NULL UNIQUE** REFERENCES `intel_proposals`. Every credit has citations, by construction. There is no hand-created protection event. |
| `renews_event_id` | NULL or a `protection_events` id, UNIQUE. Set on a renewal (§4.8). |
| `created_by`, `created_at`, `retracted_by`, `retracted_at`, `retract_reason` | |

- **A credit lapses at `review_by` unless it is renewed** (§4.8). The failure mode is *less* reassurance, never stale
  reassurance. Retraction is terminal, as it is for threats.
- **The protection must apply to everyone it credits, wherever they are.** A law or protection limited to some
  jurisdictions or regions is **rejected**, never approved as global or tag-wide, because we hold no location.
  The approval body must carry `applies_regardless_of_location: true` (§4.7), and that attestation lands in the audit
  row.

**The contract view, `svc.v_active_scoped_events`** (step 3's migration creates the threat half; step 4's re-creates it as below):

```sql
CREATE VIEW svc.v_active_scoped_events AS
  SELECT event_id, 'threat'::text AS direction, kind, title, body,
         severity AS magnitude, tags, is_global, starts_at, expires_at AS ends_at
    FROM threat_events
   WHERE status = 'active' AND starts_at <= now() AND expires_at > now()
     AND cardinality(tags) > 0
  UNION ALL
  SELECT event_id, 'protection'::text, 'protection'::text, title, body,
         strength, tags, is_global, starts_at, review_by
    FROM protection_events
   WHERE status = 'active' AND starts_at <= now() AND review_by > now();
GRANT SELECT ON svc.v_active_scoped_events TO imageshield_proxy_ro;
```

- **It carries events, not people.** Matching happens in the backend, against answers only the backend holds.
- **Scope of each half.** It carries only threats scoped to tags; domain and global threats keep reaching people
  through `v_person_threat_context`. **An event that is both domain- and tag-scoped appears on both, so the backend
  dedupes by `event_id`.** Tags are matched against the backend's *current* option-to-tag map, so a mapping added
  after an event was approved reaches people with no change to the event. `v_person_threat_context` does not filter `starts_at`. That makes no difference
  today, because no writer sets a future `starts_at`, and future-dated threats are out of scope (§9).
- **One namespace, two directions.** Threat and protection event ids share it. **Every consumer filters on
  `direction`**; the backend's threat-action liveness check reads only `direction = 'threat'`.
- **It is the tenth view granted.** The four hand-maintained places change in the file that creates the view:
  `EXPECTED_VIEWS` (with types `text[]`, `smallint` and `timestamp with time zone`), the `test_readyz` set pin, the
  `VIEWS` tuple in `test_svc_views`, and `FROZEN_CONTRACT_COLUMNS`. The "nine" wording in `svc_contract.py`, the test
  names and `PROXY_INTEGRATION.md` §6 is corrected.
- **Readiness differs by side.** Services' `/readyz` requires it, because services has no optional tier. The backend
  declares it **optional**, like `v_articles`.
- **Deploy order:** services first on the way up, backend first on the way down.

### 3.8 Scoring vocabulary cache

`intel_vocabulary`, a single row (`id smallint PK CHECK (id = 1)`):
- `release_no` (bigint NOT NULL), `map_version` (bigint NOT NULL), `scoring_version`, `quiz_version`, `received_at`
- `reconciled_release_no`, `reconciled_map_version`: what the worker has already reacted to (§4.9)
- `document` JSONB: the backend-published vocabulary. It holds:
  - the questions (key, prompt, type, options), per-option deductions and caps, `dynamic.threat` and
    `dynamic.protection`;
  - **the exposure-tag registry** (`slug, label, description, kind, retired`);
  - **the option-to-tag map** for the live quiz;
  - **`renamed`**: the backend's **ordered rename log**, `[{release_no, question_key, old_option, new_option}]`, the
    whole history. Services apply the entries above `reconciled_release_no`, in order and chained (§4.9). A later
    weights release or a failed push therefore never loses a rename.

**The backend pushes it with `PUT /v1/admin/intel/vocabulary`:** after every release (quiz-editor publish, rollback,
weights release, bootstrap), **after every tag or mapping change**, once at worker boot, and **hourly**.
- **Pushes are ordered by the pair `(release_no, map_version)`.** `release_no` is the monotonic identity of the live
  release, and `map_version` is the backend's counter, bumped in the same transaction as any tag or mapping change.
  `scoring_version` goes backwards on a rollback and cannot order anything.
- The row is updated only when the incoming pair is not older than the stored one in either component. An equal pair
  overwrites, so a replay is harmless. An older one is a no-op answered `200`, so a stale pusher never retries. A lost
  or out-of-order push converges within an hour.
- **It is public, non-person data.** It is how the proposal step can name real question keys and options.
- A run **loads the vocabulary once** and validates against that loaded copy, never against a row a push may overwrite
  mid-run. `against_scoring_version` and `against_release_no` record which copy it was.
- **With no vocabulary yet** (before the first push), weight-kind proposals and suggestions are skipped, with the
  outcome `vocabulary_missing`. Signals and event proposals still flow.
- The backend **re-validates every proposal against the live model when applying it.** Stale is refused there, not
  trusted here.

### 3.9 Cost-control rows (0039)

- **The provider row.** `providers` gains `claude_intel`:
  - kind `llm`, `enabled = false`, `score_version = 'n/a'`;
  - `cost_per_call_usd`: the worst-case per-call estimate derived in §8 step 0, as an unquoted numeric literal (the
    0009 and 0029 form);
  - `daily_budget_usd` **NULL**. The owner picks the cap, as a finance decision, the same way SCHEMA.md frames Hive's.
    It lands through its own migration before the provider is enabled (§5). On prod that is the only write path,
    because prod DB access is read-only.
- **Call records.** `provider_calls` gains `intel_run_id uuid NULL REFERENCES intel_runs`, with
  `CHECK (num_nonnulls(run_id, intel_run_id) <= 1)`.
- **Never calibrated.** `CONSTRAINT providers_llm_never_calibrated CHECK (kind <> 'llm' OR NOT calibrated)`. It is
  enforced at the write, because `calibrate trust` calls `set_calibrated` with no kind check, and filtering the policy
  loader would not stop it. `trust_provider` also refuses an `llm` provider with a clear message. The seeded-provider
  pin (`test_migrations.py:1475`) gains `claude_intel`.
- **Grants.** `intel_rw` gets `SELECT, UPDATE` on `providers` and `SELECT, INSERT, UPDATE` on `provider_calls` and
  `provider_spend`.
- **0039's down deletes the provider's metering on purpose**, following 0004's down and carrying its comment: downs run
  in dev and CI, and rolling the feature back throws its metering away deliberately. In order:
  1. drop the calibration CHECK;
  2. drop the `num_nonnulls` CHECK and `provider_calls.intel_run_id`;
  3. `DELETE FROM provider_calls WHERE provider_id = 'claude_intel'`, then `DELETE FROM provider_spend …`, then `DELETE
     FROM providers …`;
  4. drop the intel tables;
  5. restore the enum type.

  A migration test inserts a `claude_intel` call and spend row first, so the ordering is proven rather than vacuous.

## 4. Behaviour

### 4.1 `intel-worker` (new container, no new queue)

- **Where it runs:** a third container in the existing `services-worker` task definition, dev and prod,
  `memoryReservation` 128 MiB. Prod has 475 MiB free (`infra/ecs/prod/README.md:95`), which leaves room for the 256 MiB
  migration task. That arithmetic is redone in the README in the same PR.
- **No SQS queue.** Like `recheck/worker.py`, it is a polled loop. Every `INTEL_POLL_SECONDS` (default 30) it:
  1. creates `source_check`, `discovery` or `renewal_check` runs that are due, advancing `next_check_at` atomically;
  2. claims **one** queued run under a lease and executes it.

  The API queues a run by inserting a `queued` row. It never calls the model, which matters because the backend's
  admin calls time out at 3 seconds.
- **Who schedules.** Intel cadence is intel's own fact, owned by the registry here. Migration 0032's objection was to
  two systems deciding *scan* timing, and it does not apply to this.
- **Its own settings class, `IntelConfig`**, like `FetcherConfig`. Only this container loads it, so model settings
  never spread into the API, relay, search or confirm containers. That avoids the shared-`Config` trap, where every
  `load_config()` container crash-loops over a key it never uses.

**`IntelConfig` keys.** None has a default unless one is given:

| Group | Keys |
|---|---|
| Database | Resolved **exactly as `Config` does**: `DATABASE_URL` if present, otherwise all five of `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` (a partial set is refused at boot), plus `DB_SSLMODE` (default `require`) and `DB_POOL_MAX_SIZE`. The DSN is composed with `imageshield.db.dsn.compose_database_url`. On deployed tasks the five parts arrive as `secrets` from the `app_services` RDS secret. **A DSN never goes in `environment`**, and a new test asserts that no container's `environment` carries `DATABASE_URL`. |
| Runtime | `ENVIRONMENT` (`development` · `test` · `production`, default `production`, as in `Config`) |
| Switches | `INTEL_ENABLED` (bool, **required**: an absent key is a boot failure, never a silent off); `INTEL_MODEL_PROVIDER` (`stub` \| `claude`) |
| Model access | `INTEL_ANTHROPIC_REGION` (may differ from `AWS_REGION`, see §8 step 0); `ANTHROPIC_AWS_WORKSPACE_ID`; `INTEL_EXTRACTION_MODEL` (`claude-sonnet-5`); `INTEL_PROPOSAL_MODEL` (`claude-opus-5-5`) |
| Web search | `INTEL_WEB_SEARCH_TOOL_TYPE` (`web_search_20260209`, **config rather than a literal**, because the phone-shaped build gate flags that string in `src/`); `INTEL_MAX_WEB_SEARCHES_PER_RUN` (default 5); `INTEL_BLOCKED_DOMAINS` (default empty) |
| Limits | `INTEL_MAX_CALLS_PER_RUN` (default 20); `INTEL_MAX_DOCUMENT_CHARS` (default 200 000) |
| Provider control | The four `PostgresProviderControlStore` settings with `Config`'s defaults: `PROVIDER_CONFIG_CACHE_SECONDS`, `PROVIDER_FAILURE_THRESHOLD`, `BREAKER_COOLDOWN_SECONDS`, `BREAKER_COOLDOWN_MAX_SECONDS`. The store is built from `providers.store` directly, never through `search.worker`. |
| Plumbing | `FETCHER_BASE_URL`, `FETCHER_TOKEN`, `INTEL_POLL_SECONDS` (default 30), `INTEL_LEASE_SECONDS` (default 900) |

**Safety constants live in `intel/bounds.py`, not env** (§4.5). A value that guards a promise costs a code change, a
review and a `git blame` to move.

**Boot refusals, mirroring `SEARCH_PROVIDER`:** `ENVIRONMENT=development` requires `INTEL_MODEL_PROVIDER=stub`, and
`ENVIRONMENT=production` refuses `stub`. The check is **enforced twice, as search's is**: in config, and in the
builder, which constructs `intel/stub.py` *instead of* the Claude client, so no object in a dev process holds a live
client. The stub proposes nothing and says so on every run's outcome. It is not a fixture generator.

**`test_ecs_task_defs` changes.** Four existing tests iterate every container in the worker task and would fail on
the new one:
- `test_worker_task_runs_exactly_the_relay_and_the_search_consumer` pins `{relay, search-worker, intel-worker}` and the
  command `python -m imageshield.intel.worker`, with `essential = true`.
- `test_worker_supplies_every_required_config_field_in_both_containers`, `test_worker_runs_live_providers_under_the_production_gates`
  and `test_worker_sets_no_variable_config_does_not_read` are scoped to the relay and search containers.
- **New intel mirrors**, driven by `IntelConfig.model_fields`: every required field is supplied, nothing outside
  `IntelConfig` is set, and the production gate holds (`ENVIRONMENT=production` with a non-stub provider).

### 4.2 Fetching — a new fetcher route, `POST /v1/text`

Everything the model reads arrives through the fetcher. That includes pages found by model web search. **Web search is
URL discovery only**, and the quotes are always checked against text *we* fetched. There is one verification path and
one isolation boundary.

`/v1/text` sits beside `/v1/page`. `/v1/page` keeps its exact contract, because confirm depends on it.

*Amended 2026-09-30:* step 5 adds a `robots.txt` check for source validation (§4.10): fetched under the same SSRF
guard, cached per host for 24 hours, and reported as `robots_disallowed`. It applies to validation, not to
`/v1/page`.

- **Content types:** `text/html`, `application/xhtml+xml`, `text/plain`, `application/rss+xml`,
  `application/atom+xml`, `application/xml`, `text/xml` and `application/json`. Anything else is `400
  unsupported_type`. PDF is out of v1 (§9).
- **The declared charset is honoured**, with UTF-8 as the fallback. `/v1/page` ignores it, which is wrong for intel.
- **Conversion happens in the fetcher**, so the hostile-input parsing stays in the isolated process:
  - HTML becomes visible text with stdlib `html.parser`. Script, style and template are dropped and block elements
    become line breaks, following `confirm/og_image.py`'s no-bs4 precedent.
  - A feed becomes `items: [{title, link, published}]`, parsed with stdlib `xml.etree`. **Any `<!DOCTYPE` is refused
    before parsing**, so no DTD or entity expansion runs.
  - JSON is returned re-serialised.
- **The response** carries `{text, content_type, final_url, truncated, items?}`. `truncated` is explicit. `/v1/page`
  truncates silently, and a diff over silently truncated text reports removals that never happened.
- **Guards:**
  - `recheck/ssrf.address_refusal` on every hop, with at most 2 redirects;
  - https only for intel;
  - `INTEL_TEXT_MAX_BYTES` (2 MB) and `INTEL_TEXT_TIMEOUT_SECONDS` (10), both in `FetcherConfig`;
  - `getaddrinfo` runs in a thread, so a burst cannot stall the loop that serves the subject's `/v1/crop`.
- **A route-level semaphore** (`INTEL_TEXT_MAX_CONCURRENCY`, default 2) caps intel's share of the fetcher's 8-connection
  pool. A crawl must never delay a victim's hit preview.
- **INVARIANTS #11 is amended rather than claimed to be met** (§7).

A boundary test forbids importing `imageshield.fetcher.fetch` outside `imageshield/fetcher/`. Today nothing
structural stops a process holding the DB from fetching in-process.

### 4.3 The pipeline

**Normalisation.** One function, `intel/text.py:normalise`, is used for both hashing and verification: NFC, NBSP to a
space, runs of whitespace collapsed to one space, trimmed.

**Known hit locations are never read** (§6.1). Before every fetch the worker starts, and again on `final_url` before
extraction, the canonical URL's `url_hash` is looked up in `content_urls`. A match is refused and counted as
`known_hit_location`: no document row, no model call, no excerpt. This applies to `adhoc_url`, feed item links,
discovered URLs, `source_url` on registration and each `source_check`.

**What a unit consumes.** A *unit* is one policy diff, one feed item, or one whole document. For each unit, the
snapshot replacement, `last_content_sha256`, the `intel_documents` row (which marks a feed item as seen), its signals
and its excerpts all commit in **one transaction with that unit's result**:
- **Deterministic results consume the unit.** They are counted on `outcome` and never re-billed at the next check:
  signals written, a model refusal, a `max_tokens` stop, every quote dropped by verification.
- **Transient results leave the unit unconsumed**, so the next check retries it: a gate `Skip` (`provider_disabled`,
  `breaker_open`, `budget_exceeded`), `budget_unset`, a timeout, a 5xx or connection error after retries,
  `rate_limited`, or a worker death before commit.

A run stopped mid-way by a gate skip ends `refused` with `refused_by = 'gate'`, keeping the units it consumed.

**`source_check`:**
1. Fetch through `/v1/text`, then normalise.
2. **Too short:** a `policy_page` whose normalised text is under `MIN_POLICY_TEXT_CHARS` (500, `intel/bounds.py`)
   disables the source in one transaction: `enabled = false`, `disabled_reason = 'too_short'`, no snapshot, no model
   call, and an `audit_log` row with `actor_type 'service'`. This catches single-page-app pages whose HTML is a shell.
3. **Unchanged:**
   - For `policy_page` and the other single-document kinds, **if the hash equals the last consumed hash, stop.** No
     model call is made. This is the main cost lever.
   - For `feed`, the gate is **"no unseen items"** instead: items whose canonical link has no document among the
     source's documents. The listing's own hash is not used.
4. `policy_page`: diff against the snapshot with `difflib`, and send **only the changed hunks plus context** to
   extraction.
5. `feed`, up to 10 items per run:
   - newest first by `published`, falling back to feed order when an item has no date;
   - items beyond 10 are counted as `feed_backlog` and taken on the next run;
   - items with a parseable `published` older than `FEED_MAX_ITEM_AGE_DAYS` (30) are counted as `too_old` and skipped
     without a record, which costs nothing to re-skip. Items with no date are not skipped. This stops a newly
     registered feed from backfilling months-old incidents as fresh proposals.
6. Other kinds: extract the whole text, up to `INTEL_MAX_DOCUMENT_CHARS`. Past that the document is `truncated` and
   only the head is read.
7. **Failure bookkeeping.** Each run updates `last_checked_at`, `last_run_status` and `consecutive_failures`. After
   `MAX_SOURCE_CONSECUTIVE_FAILURES` (10) the source is disabled with `disabled_reason = 'unreachable'` and an audit
   row. A source failing forever is visible on `GET /sources`, not rescheduled silently.

**`discovery` for a `search_query` source:**
1. One model call with the web search tool (`max_uses` and `blocked_domains` from config). The prompt holds the
   operator's saved query, the category taxonomy and the loaded tag registry. **Never person data.**
2. The structured output lists candidate URLs, each with a reason.
3. Each URL is https-only, deduplicated against documents from the last 30 days, checked against known hit locations,
   fetched through `/v1/text`, and extracted with `trust = web`.

The model's web-search text is never trusted as evidence. Only text we fetched is.

**`adhoc_url`:** an operator pastes a URL. It goes through the same checks, fetch and extraction, with `trust =
listed`, because a person chose it.

**Extraction**, with `INTEL_EXTRACTION_MODEL`, structured output (`output_config.format`) and adaptive thinking:
- **Input:** the normalised text (or diff), the source kind, the source's tag hints, the taxonomy, and **the loaded
  tag registry** (slug, label, description; retired tags omitted). A registry larger than `MAX_PROMPT_TAGS` (300,
  `intel/bounds.py`) is narrowed to tags whose label or slug appears in the text, plus the source's hints.
- **Output:** `signals: [{category, direction, tags, unregistered_subjects, summary, quotes: [string]}]`. The model
  uses a registered tag where one fits and names anything else as an unregistered subject. It never invents a slug.
- **Local verification. This is the rule; the prompt is not** (#49):
  1. Normalise each quote. It must be a **substring of the normalised document text**, and the offsets are recorded.
  2. Its length must be 20–600 characters.
  3. **It must contain no phone-shaped or email-shaped run** (§6.3). Otherwise the quote is dropped, never redacted,
     because redacting would break the verbatim property.
  4. A signal with no surviving quote is not written.
  5. Every tag must be in the loaded registry and not retired. An unknown tag is removed from the signal and counted
     (`unknown_tag`). It is never kept, and never silently turned into an unregistered subject.
  6. The summary is masked (§6.3).

  Every drop and mask is counted by reason on the run's `outcome`.
- The Citations feature is not used. It cannot be combined with structured output, and the local substring check is
  the guarantee either way.

**Proposal generation**, with `INTEL_PROPOSAL_MODEL`, structured output and effort set explicitly (Opus 5.5 defaults to
`medium`). It runs once per run that wrote new signals.
- **Input:**
  - the new signals;
  - related active signals from the last 90 days with overlapping tags or category, bounded to 60;
  - **pending event proposals and active threat and protection events with overlapping tags**, bounded to 40 each,
    with their `signal_ids`;
  - the loaded vocabulary, including **the set of mapped tags** and the questions with their `mutable` flag.

  The prompt says:
  - never re-propose a live event;
  - attach new evidence to a matching pending proposal instead;
  - evidence about **unregistered subjects** is a `coverage_gap`;
  - an event on **registered but unmapped** tags may be proposed, and it waits, unapprovable, until a mapping gives it
    reach (§4.5).
- **Output:** `proposals: [{kind, target, suggested, rationale, signal_ids}]` and `attach: [{proposal_id, signal_ids}]`.
- **Code validation (§4.5)** drops any proposal that fails, with the reason counted. Nothing is "fixed up".
- **`attach` is validated in code.** The target proposal must still be `pending` and of an event kind, and every
  attached signal must be new in this run, `active` and verified.
- **Duplicate detection is deterministic.** A new event proposal whose kind and tag set equal a pending
  proposal's, and which shares any signal document with it, is converted to an `attach` rather than written.
- **All of a run's proposals and attachments commit in one transaction** with `proposals_written_at`, so a reclaimed
  run never generates twice.

**Which kinds are generated depends on the build step.** `weight_change`, `weight_suggestion` and `coverage_gap` from
step 2, `threat_event` from step 3, `protection_event` from step 4. The decision route refuses an event kind whose
consumer has not shipped (`409 proposal_not_decidable`). So no approval can create an event nothing reads, and no
`review_by` or `expires_at` clock runs on an inert event.

### 4.4 The model seam

- **`intel/model.py`** is the only module that imports `anthropic`, and a boundary test enforces that. It builds
  `AnthropicAWS(aws_region=INTEL_ANTHROPIC_REGION, workspace_id=…)` with `max_retries=0`, and exposes an `IntelModel`
  Protocol: `extract(...)`, `discover(...)`, `propose(...)`, `suggest_weights(...)`.
- **Retries** are our own bounded, jittered driver around `RateLimitError` and 529 overload. The SDK's hidden retries
  would under-report `provider_calls.attempt`. `ratelimit.send_with_retry` takes an `httpx.Response` and cannot wrap
  the SDK, so this is a second retry driver, recorded as such.
- **`pause_turn`** continuations from the web search tool are driven to completion inside `discover()`, bounded by
  `INTEL_MAX_CALLS_PER_RUN`.
- **Stop reasons.** Every response has its `stop_reason` checked before `content` is read. `refusal` is recorded as the
  unit's outcome (neutral, consumed). `max_tokens` is an extraction failure for that unit (neutral, consumed).
- **Server-side fallbacks are off.** A declined document is recorded, not rescued by another model, so a row's
  `model_id` always names one model (#4, applied to intel).
- **Prompt builders** (`intel/prompts.py`) take typed public inputs only. No parameter may be a `UserRef`, a person or
  a phone. A test walks every builder's signature.

### 4.5 Validation, bounds and supersession — in code, on both sides

These are safety limits, so they are **code constants in `intel/bounds.py`**, not env. Changing one costs a code
change, a review and a `git blame`, the same argument as the backend's `REPORT_FACE_MATCH_MIN`. Every number is an
`int`, so the JSON schema sent to structured output declares integers and a fractional value is refused.

| Kind | Validated at generation and again on the operator's `values` |
|---|---|
| `weight_change` | The question must be **`mutable` in the loaded vocabulary**. The option must exist. `target.current` must **equal the loaded vocabulary's deduction** for that option (else dropped, `current_mismatch`). `delta` int, non-zero, within [−2, +2]. `current + delta` within [0, 10], and not above the question's cap when it has one. An operator edit may change `delta` only. |
| `threat_event` | `kind` ∈ `leak` · `deepfake_wave` · `platform_incident` · `other` (the 0022 CHECK; the signal category taxonomy is a different vocabulary). `title` non-empty. `severity` 1–5. `tags` non-empty, each registered and not retired (a global threat stays hand-created). `expires_in_days` 1–90. |
| `protection_event` | `strength` 1–5. `review_in_days` 30–366. **The model may not propose `is_global`** (dropped, `global_not_proposable`), because it cannot know a protection applies regardless of where a person lives. A global event exists only by an operator's edit on approval. A renewal (§4.8) is written by code, not the model, and carries forward the scope an operator already approved, including global. It still needs a fresh approval and a fresh location attestation. Exactly one of `tags` or global, and every tag registered and not retired. |
| event kinds | **Every tag unmapped:** a non-global event whose tags are all unmapped is **written `pending`, never dropped**. It reads `approvable = false, why_not = 'tags_unmapped'`, computed at read time against the current vocabulary, and approving it is `409 proposal_tags_unmapped`. **Once any of its tags is mapped it becomes approvable with no rewrite**, and the reviewer then sees its real reach. Its signals still count toward the `coverage_gap` threshold. An event whose tags are only *partly* mapped is approvable. This check runs at read and decision time. It is **not** a generation drop. |
| `weight_suggestion` | `deduction` int 0–10, or `null` meaning **no evidence, operator's call**, which is never an invented number. Up to the question's cap when it has one. `suggested_tags` are registered, non-retired tags only. `new_tag` is offered only when none fits, with a well-formed slug that is not already registered. |
| `coverage_gap` | Written only with at least `COVERAGE_GAP_MIN_SIGNALS` (3) active signals from at least `COVERAGE_GAP_MIN_PUBLISHERS` (2) publishers in 90 days, whose `unregistered_subjects` or unmapped tags concern the same subject. These are separate constants from the approval rule. `suggested_tag`, when present, is well-formed and unregistered, or registered but unmapped. |

- **`threat_events.decay_days`** is still NOT NULL and has fed nothing since 0037. The approval insert supplies it as
  `expires_in_days`, and the value is inert. It is never shown to or edited by an operator.
- **Escrowed and decaying questions (age, gender, prior misuse) never get a `weight_change`.** Changing them writes
  permanent escrow releases and bulk notifications on the backend. Evidence about them reaches the quiz editor as
  `weight_suggestion` instead.
- **Corroboration** uses one predicate (§3.6). A proposal whose active signals are all `trust = web` needs signals from
  at least `CORROBORATION_MIN_PUBLISHERS` distinct publishers. Otherwise it is written as `pending` and approval is
  refused (`409 proposal_uncorroborated`) until an `attach` from another publisher lands. Reads carry `approvable` and
  `why_not`.
- **Reads also carry `unmapped_tags`** for event kinds: tags in `target.tags` that are unmapped in the *current*
  vocabulary, and `retired_tags`. Both are non-blocking warnings, and the backend adds the live reach count.

### 4.6 Weight and tag suggestions for quiz drafts

*Amended 2026-09-30 (decision 11):* this retrieval and suggestion now run **after** the source stages of §4.10.
`POST /weight-suggestions` gains a `sources` list, the run first reads newly chosen sources, and its call cap is
`INTEL_MAX_CALLS_PER_SUGGESTION_RUN`. Everything below still holds for what happens after that read.

The backend's quiz editor calls `POST /v1/admin/intel/weight-suggestions` with `{question_key, prompt, type, options,
cap?, tags?, operator}`.
- `tags` is an optional `{option: [slug]}` map: the backend's option-to-tag rows for the draft's options. The map is
  keyed by question and option text, so it can hold rows for options that exist only in a draft.
- Every key must be one of `options`, and every slug well-formed (`422` otherwise).
- It is stored in `intel_runs.request`.

Services inserts a `weight_suggestion` run with `requested_by = operator`, and answers `202 {run_id}`.

**Retrieval** covers signals from the last 365 days, active only:
- **By tag:** signals whose tags intersect the options' tags. **When the request carries `tags`, even `{}`, it replaces
  the vocabulary's map for this run**, because the draft is authoritative for the draft. The cached map is used only
  when the field is absent.
- **By subject:** signals whose `unregistered_subjects` or tag labels mention an option's text. This is what lets a
  brand-new option ("Bumble") find evidence before anybody has tagged it.
- **By category:** signals whose category is `research`, `policy` or `incident` and whose summary mentions an option
  term.

At most 80 are used. `INTEL_PROPOSAL_MODEL` then suggests, **per option**:
- a deduction;
- **the registered tags that option should map to**;
- when none fits, a new tag (slug, label, kind) for an operator to create.

Everything is validated as in §4.5. The result is a `weight_suggestion` proposal, born `delivered`. **Accepting a
suggested tag or new tag is a backend action**: the quiz editor creates the tag and writes the mapping through the
backend's tag routes, never through services.

**The poll** is `GET /v1/admin/intel/weight-suggestions/{run_id}` and returns `{run_id, status, proposal_id | null,
error_code | null, options: [{option, deduction | null, rationale, signal_ids, suggested_tags, new_tag, corroborated,
why_not}] | null}`. `corroborated` comes from the one predicate (§3.6), computed per option from that option's own
signals.

### 4.7 Admin API — `/v1/admin/intel/*`

**The house rules.** Every route uses both tokens (`X-Service-Token`, `X-Admin-Service-Token`) and a pydantic
`extra='forbid'` body.
- **Every operator write** carries an `operator` body field, injected by the backend from `iam.operator_grants`. That
  includes every body not listed otherwise below. The write goes in **one transaction with its `audit_log` row**
  (`actor_type 'operator'`, `metadata.operator`, never `actor`).
- **There are exactly two system writes**, with no operator, audited as `actor_type 'service'`:
  - `PUT /vocabulary`;
  - `POST /proposals/applied`, with body `{scoring_version, proposal_ids}` and metadata of the same two fields. It is
    audited only for rows that actually move.

  Neither changes a score on its own: the vocabulary is derived from the live release, and the acknowledgement mirrors
  a release already committed, whose publishing operator the backend's audit row names.
- The worker's machine writes also audit as `actor_type 'service'`.
- **Lists are keyset-paged** on `(created_at, id)`. A malformed cursor is `422 invalid_cursor`.

| Route | Does |
|---|---|
| `GET /sources` · `POST /sources` · `PATCH /sources/{id}` | Registry. PATCH changes `enabled`, `check_every_hours`, `tags`, `terms_note` and `query_text`. `POST` refuses a known hit location (`422 known_hit_location`) and a PII-shaped `query_text` (`422 query_names_a_person`). |
| `POST /sources/{id}/check` | Queues a `source_check`, or returns the open one (§3.4) |
| `POST /documents` | Pastes a URL and queues an `adhoc_url` run. `422 known_hit_location` is checked synchronously. |
| `GET /runs` | Recent runs with outcomes, plus a top-level `spend` block: `{spend_date, call_count, spent_today_usd, daily_budget_usd, budget_headroom_usd}` for `claude_intel`. It is read from the same UTC-day `provider_spend` row the budget guard enforces, **never summed from runs**. Money crosses as decimal strings. `daily_budget_usd` is null while unset. |
| `GET /signals` · `GET /signals/{id}` | With excerpts and document provenance |
| `POST /signals/{id}/retract` | `{reason, operator}`. Guarded by `WHERE status = 'active'`. |
| `GET /proposals?status=&kind=&cursor=&limit=` | `status` and `kind` are repeatable closed enums; an unknown value is `422`. Omitting `kind` omits `weight_suggestion`. |
| `GET /proposals/{id}` | With signals, excerpts, `approvable`, `why_not`, `unmapped_tags`, `retired_tags` (informational), `evidence_retracted`, `stale` and `why_stale` (§4.9), and related active events with overlapping tags, so a reviewer sees what a new threat duplicates. **For `weight_suggestion`** it also returns `options: [{option, deduction, suggested_tags, new_tag, corroborated, why_not}]`, computed per option exactly as the poll does, while its proposal-level `approvable` is fixed `false` with `why_not = 'not_decidable'`. This is the read the backend's quiz-editor publish uses. |
| `POST /proposals/{id}/decision` | `{decision, values?, reason, applies_regardless_of_location?, operator}`. See below. |
| `POST /proposals/applied` | System write, see above. Moves `approved` `weight_change` rows to `applied`, **keeping the approval's `decided_by`, `decided_at` and `decision_reason`**. Already-applied rows are a no-op. |
| `PUT /vocabulary` | System write (§3.8) |
| `POST /weight-suggestions` · `GET /weight-suggestions/{run_id}` | §4.6, with the `sources` list and `sources_deferred` of §4.10 |
| `POST /source-proposals` · `GET /source-proposals/{run_id}` | Stage 1 of §4.10: candidate sources per option, existing ones first. Nothing is registered. |
| `POST /source-validations` · `GET /source-validations/{run_id}` | Stage 3 of §4.10: `ready` or `blocked` with a reason, per candidate. No model call. |
| `GET /protection-events` | Each row carries `renewal_due` (`review_by` within 30 days) and its renewal run's outcome (§4.8) |
| `POST /protection-events/{id}/retract` | Terminal, audited, guarded by `WHERE status = 'active'` |

**Decisions.** One transaction, guarded by `WHERE status = 'pending'` (or `'approved'` for a withdrawal), so two
reviewers produce one outcome and one clean `409 proposal_not_pending`. The corroboration predicate and §4.5 are
re-checked inside it.
- **`rejected`** works on any decidable kind. `values` is refused (`422`).
- **`approved` on an event kind.** `decided` is set to the operator's `values` if given, otherwise to a copy of
  `suggested`, then re-checked (`422 values_out_of_bounds`). The event row is inserted from `decided` only, with
  `proposal_id`. The proposal becomes `applied` with `applied_ref = event_id`, and one audit row is written.
  - A protection event requires `applies_regardless_of_location: true` (`422 values_out_of_bounds` otherwise), whether
    it is global or tag-scoped. The attestation is recorded in the audit metadata. Every tag in the final `values`
    must be registered and not retired in the loaded vocabulary (`422 values_out_of_bounds`).
  - An event kind whose consumer has not shipped is `409 proposal_not_decidable` (§4.3).
  - **A renewal** (`target.renews_event_id`) inserts the new event with `starts_at = old.review_by`, so the two never
    overlap: the view filters `starts_at <= now()`. That allows no double credit and no gap.
- **`approved` on `weight_change`.** `decided = {delta}`, from `values.delta` or `suggested.delta`, re-checked. The
  proposal becomes `approved`, waiting in the backend's publish queue. The cell index may answer `409
  proposal_cell_awaiting_publish`.
- **Withdrawal:** `rejected` on an `approved`, unapplied `weight_change`, with the withdrawing operator and reason.
- **`coverage_gap`** can only be dismissed (`rejected`). **`weight_suggestion`** is never decidable.

**Response:** `{proposal_id, kind, status, applied_ref, decided}`.

**Error codes** (the envelope from §9 of the services manual): `proposal_not_found`, `proposal_not_pending`,
`proposal_uncorroborated`, `proposal_not_decidable`, `proposal_evidence_retracted`, `proposal_cell_awaiting_publish`,
`proposal_tags_unmapped`, `values_out_of_bounds`, `unknown_tag`, `tag_retired`, `known_hit_location`,
`query_names_a_person`, `intel_source_not_found`, `intel_run_not_found`, `signal_not_found`, `signal_not_active`,
`protection_event_not_found`, and from step 5 `source_not_validated` (§4.10), naming the entries.
`unknown_tag` and `tag_retired` carry the offending slugs. **A hand-created threat's
`tags` are shape-checked only**; the backend, which owns the registry, is the authority on their membership. The
`intel_` prefixes exist because
`run_not_found` already means a missing search run. The backend maps every 404, 409 and semantic 422 code by name,
because its `mapServicesError` otherwise collapses a 409 into `CONFLICT` and a 422 into `VALIDATION_FAILED`. The plain
pydantic `validation_error` 422 stays unmapped on purpose.

### 4.8 Protection renewal

- **When:** a protection event whose `review_by` is within 30 days is `renewal_due`. The worker queues **one**
  `renewal_check` run for it automatically.
- **What it does, with no model call:** it re-fetches through `/v1/text` the documents behind the event's cited
  excerpts, and **re-verifies each excerpt verbatim** with the #49 substring check.
- **If the evidence still verifies:** it writes a pending `protection_event` proposal. Its `target` carries
  `renews_event_id`, its `suggested` values are the prior decided values, and it links the re-verified signals. Those
  are new signals with `trust = listed`, because the documents are the ones an operator already approved on.
- **If it does not verify:** it writes nothing, records `renewal_evidence_gone` on the run, and the event lapses at
  `review_by`. The operator sees why on `GET /protection-events`.

A renewal is approved like any other proposal (§4.7). No year-old evidence is ever re-cited without being re-checked.

### 4.9 Adapting to quiz changes — no deploy, no hand edits

The quiz will change: questions added and removed, options renamed, new platforms mapped. **Intel holds no copy of the
quiz that can go stale except the vocabulary**, and it reacts to every new vocabulary on its own.

**When it reacts.** On each loop, the worker compares `intel_vocabulary`'s `(release_no, map_version)` with its
`reconciled_*` pair. If either moved, it reconciles once, in **one transaction**, and records the new pair. The
reconcile is idempotent and deterministic; no model runs in it.

| What changed | What intel does |
|---|---|
| **A new question or option** (any question, any type) | Nothing needs doing. The next run's prompts carry it. A `mutable` question can receive `weight_change` proposals from then on, and the quiz editor can ask for suggestions for it before it is even published (§4.6). |
| **An option renamed** (entries in the `renamed` log above `reconciled_release_no`) | Entries are applied **in `release_no` order and chained**, so A → B then B → C lands a proposal on A at C. A pending `weight_change` on the old text is **retargeted** to its final text when the live deduction still equals its `current`. Otherwise it is superseded with `cell_changed`. Its signals stay attached either way. After a rollback to a pre-rename release, a proposal retargeted to the new text lands on a non-live option and is handled by the removed-option row below. |
| **An option or question removed, a question no longer `mutable`, or a deduction moved** | A pending `weight_change` on that cell is superseded with `cell_changed`. |
| **Any of the above, for an `approved`, unapplied `weight_change`** | **Nothing is changed automatically.** An approval is a human's decision and stays one.<br>• **First, a published-but-unacknowledged change is recognised.** When the live deduction equals `target.current + decided.delta` exactly, the proposal was just published and its acknowledgement has not landed yet. Reads show `applied_pending_ack: true`, **never `stale`**, and withdrawal is refused (`409 proposal_not_pending`).<br>• Otherwise the reads show `stale: true` with `why_stale` (`option_renamed`, `cell_removed`, `not_mutable`, `deduction_moved`), so an operator withdraws it or re-approves a fresh one. The backend would refuse it as `STALE` at publish anyway. |
| **A tag newly mapped** | **The evidence behind a closed gap is re-proposed, never lost.**<br>• A pending `coverage_gap` is resolved when its `suggested_tag.slug` equals the tag, or its normalised subject equals the tag's normalised slug or label. The comparison is exact on lowercase text with non-alphanumerics collapsed; nothing is fuzzy.<br>• For each gap it resolves, the reconcile **queues a `gap_regenerate` run** through the normal provider gate. The run's input is the gap's active `signal_ids`, plus active signals whose normalised `unregistered_subjects` equal the tag's normalised slug or label. They are passed explicitly, because signal tags are immutable and old signals would never match "overlapping tags".<br>• The gap is superseded with `resolved_by_quiz` **in the same transaction that queues the run**, and records `regenerated_by_run_id`. The run then writes event proposals against a vocabulary where the tag is mapped.<br>• Pending event proposals whose only unmapped tags are now mapped become approvable, with no rewrite.<br>• Active events already carrying the tag start reaching the newly mapped people **in the backend**, with no event edited. |
| **A tag registered** | From the next run, extraction uses it instead of reporting the subject as unregistered. |
| **A source's tags all unmapped, or one mapped again** *(2026-09-30, §4.10)* | A source whose non-empty `tags` are all unmapped is paused (`enabled = false, disabled_reason = 'unmapped'`) in the reconcile's transaction, and re-enabled when any of its tags is mapped again, only if `disabled_reason` is still `unmapped`. Untagged sources never pause this way. |
| **A tag retired** | New proposals and operator values may no longer *add* it. Existing events, mappings and pending proposals keep it and stay approvable, and reads show it in `retired_tags`, informational only. A renewal carries it forward. |
| **Question keys** | Keys are identities. A new key is a new question, and an old key's evidence stays attached to the old key. Renaming a *key* is out of scope (§9). |

**Why only `weight_change` is ever retargeted, and only while pending.** Event proposals target tags, not options, so
a quiz change cannot invalidate them; at most it changes their reach, which the backend shows live. Suggestions are
delivered advice with no state to repair. Approved rows belong to the person who approved them.

**The one-line promise:** after any quiz-editor publish or tag change, within one poll interval, intel's pending queue
matches the live quiz, and nothing an operator approved has been silently rewritten.

### 4.10 Sources chosen per question — amended 2026-09-30 (decision 11)

**Owner, 2026-09-30:** *"we take sources directly from the questions … when suggest button is clicked we also select
the sources and we also give option to add more sources and validate that we can gather information from there and
then we proceed."* A registry curated apart from the quiz drifts from it: sources for removed options keep costing
money, and a new option has no sources until somebody remembers to add them. So the Suggest points flow now **starts
by choosing sources**, and §4.6's retrieval runs only after them. The registry (§3.2) is where chosen sources live
afterwards; nothing about how a registered source is checked changes.

**The four stages, all behind the quiz editor's one button:**

1. **Propose.** `POST /source-proposals` with `{question_key, prompt, options, tags?, operator}` queues a
   `source_proposal` run and answers `202 {run_id}`.
   - One `INTEL_EXTRACTION_MODEL` call with web search (`max_uses` = `INTEL_MAX_SOURCE_PROPOSAL_SEARCHES`, config). The
     prompt holds the question text, its options, the tag registry and the source kinds. **Never person data.**
   - Output, per option: up to `MAX_PROPOSED_SOURCES_PER_OPTION` (5, `intel/bounds.py`) candidates
     `{kind, source_url | query_text, reason}`. Typical candidates are the platform's privacy policy, its terms, its
     safety or transparency pages, and one or two news searches.
   - Code, not the model, then: https only; canonicalised with `search/urlhash.py`; deduplicated; known hit locations
     dropped (§6.1); PII-shaped `query_text` dropped (§6.1). **Existing registry sources whose tags intersect the
     option's tags are listed first, pre-selected**, so a second press reuses what is already watched.
   - `GET /source-proposals/{run_id}` returns `{status, options: [{option, existing: [source], proposed:
     [candidate]}]}`. Nothing is registered at this stage.
2. **Edit.** The quiz editor shows the list per option. The operator removes candidates and adds their own: a URL of
   any kind, or a search query.
3. **Validate.** `POST /source-validations` with `{candidates: [{option, kind, source_url | query_text}], operator}`
   queues a `source_validation` run. **No model call.** For each candidate:
   - a known hit location is `blocked` (`known_hit_location`);
   - it is fetched through `/v1/text`, and the final URL must be https;
   - its readable text must reach `MIN_POLICY_TEXT_CHARS` (500) for `policy_page`, and `MIN_SOURCE_TEXT_CHARS` (200,
     `intel/bounds.py`) otherwise. This catches app shells before they cost a scheduled check;
   - **robots:** the host's `robots.txt` must not disallow the path for our user agent (`robots_disallowed`). The
     fetcher gains this check (§4.2, amended): it fetches `robots.txt` under the same SSRF guard and caches it per host
     for 24 hours. Robots is a floor, not a permission: the operator's `terms_note` (§3.2) is still required;
   - a `feed` must parse as RSS or Atom with at least one item;
   - a `search_query` passes the PII check, then **one** web search, metered through the gate like any call, must
     return at least one https URL that passes the checks above.

   `GET /source-validations/{run_id}` returns `[{candidate, status: ready | blocked, reason}]`. A result is honoured
   for 24 hours.
4. **Proceed.** `POST /weight-suggestions` gains `sources: [{option, kind, source_url | query_text,
   validation_run_id, terms_note, check_every_hours?}]`. Each entry must be `ready` in that validation run and inside
   its 24 hours, or the call is `422 source_not_validated`, naming the entries. In **one transaction**, services:
   - registers each source, or reuses the existing row with the same `url_hash` or `query_text`, with `origin`
     (`suggested` when it came from stage 1, `operator` otherwise), `proposed_for = {question_key, option}`, the
     option's tags from the request's `tags` map or the vocabulary, and `check_every_hours` defaulting to 168;
   - queues the `weight_suggestion` run.

   That run first performs an **immediate check of every newly registered source**, inside the run rather than as
   separate queued runs, then retrieval (§4.6) and the suggestion. **Its call cap is
   `INTEL_MAX_CALLS_PER_SUGGESTION_RUN`** (config, default 60) instead of `INTEL_MAX_CALLS_PER_RUN`, because a
   question's first read is legitimately larger than a weekly check. Units past the cap stay unconsumed and are read by
   the sources' first scheduled check. The suggestion proceeds with what was read, and the poll adds
   `sources_deferred: int`.

From then on the chosen sources are ordinary registry rows on the weekly schedule.

**Sources follow the quiz** (a new row in §4.9's table). A source whose `tags` are non-empty and **all unmapped** in
the current vocabulary is **paused**: the reconcile sets `enabled = false, disabled_reason = 'unmapped'` in its
transaction. When any of its tags is mapped again, the reconcile re-enables it, but only when `disabled_reason =
'unmapped'`; an operator's own disable is never overridden. A source with no tags (a breach tracker, a regulator's
feed) is general and never pauses this way. So a question removed tomorrow stops its sources costing money, and a
rollback or a re-added option brings them back, with no hand edits.

**Schema, in the migration that ships step 5:**
- `intel_sources.origin TEXT NOT NULL DEFAULT 'operator' CHECK (origin IN ('suggested','operator'))`, and
  `intel_sources.proposed_for JSONB`, provenance only (matching still goes through tags);
- `disabled_reason` gains `'unmapped'`;
- `intel_runs.kind` gains `source_proposal` and `source_validation`. Their results live in the run's `outcome`; no
  new table.

**Cost.** A proposal run is one model call plus its searches. A validation run makes no model call and one search per
search-query candidate. The suggestion run is bounded by its own cap. All of it goes through the provider gate and the
daily budget (§5), so a press that hits the budget waits and says so, like any run.

**Why the registry route stays.** `POST /sources` is still how an operator adds a general source that belongs to no
option. Everything tied to an option arrives through this flow, so it carries the option's tags and pauses with them.

## 5. Cost and controls — the existing provider gate, with a new kind

Every model call goes through `providers/gate.decide("claude_intel", ...)`: ENABLED → BREAKER → BUDGET, exactly as
search providers do (#37). The chain's step 1 (subject eligibility) does not apply: there is no subject.

- **A budget is required, and the provider ships off.**
  - The worker refuses to run (`outcome: budget_unset`, transient) while `claude_intel.daily_budget_usd` is NULL. For
    search providers a missing budget means no cap, and that rule is **inverted for `llm`** deliberately: a model that
    can loop on web search must not run uncapped.
  - **Turning intel on is two ordered steps**, per environment, after 0039: (a) a migration sets the owner's
    `daily_budget_usd`; (b) an operator enables it through `/v1/admin/providers/claude_intel/enable`, which the backend
    relays at developer tier. The health read already shows the budget, so the operator can confirm it before
    enabling.
  - The step goes into `OPERATIONS.md` §4 and `DEPLOY-RUNBOOK.md` §13, noting that for kind `llm` a NULL budget refuses
    every run, the inverse of Hive.
- **Actual cost is recorded, not the estimate.**
  - The guard checks the worst-case estimate (`cost_per_call_usd`). `record_outcome` records the **actual** cost
    computed from `usage`: input, output, cache-write and cache-read tokens at the model's rates, plus $10 per 1 000 web
    searches from `usage.server_tool_use.web_search_requests`.
  - The rates live in `intel/pricing.py`, a table keyed by model id and **written per million tokens**
    (`Decimal("3")`, divided in code). A per-token decimal string such as `"0.000003"` would trip the phone-shaped
    build gate. **An unknown model id refuses the call** (fail closed), so changing the model needs a deliberate code
    change to its price.
  - This amends models.py's "the recording path charges what the guard checked" for `llm` only, and the docstring says
    so. The overshoot bound becomes *concurrent calls × (actual − estimate)*. With one worker claiming one run at a
    time, that is one call.
- **`raw_response` for kind `llm` is metadata only:** `{model, stop_reason, usage, web_search_requests,
  pause_turns}`. It never holds content, thinking, quotes or web-search result blocks, so a PII-bearing quote that
  verification drops never lands in a column. `error_detail` is a stable class name plus HTTP status, never
  `str(exc)`, which can echo a body. `ProviderResult`'s "verbatim, always" docstring gains the `llm` carve-out,
  following the confirm worker's counts-only precedent.
- **The store Protocol widens** to `run_id: UUID | None, intel_run_id: UUID | None`, with exactly one non-null. **Do
  not copy the confirm worker's `run_id is None → skip metering`**: an unmetered call can never open its own breaker.
- **Status mapping.** `APITimeoutError` → `timeout`. Connection errors and 5xx after our retries → `error`.
  `RateLimitError` → `rate_limited`. **A refusal, a `max_tokens` stop and a quote failing verification are all `ok`.**
  The call worked, and the verdict lives in intel tables. Recording them as `error` would open the breaker on an
  ordinary result, which #40 forbids.
- **Per-run bounds:** `INTEL_MAX_CALLS_PER_RUN` and `INTEL_MAX_WEB_SEARCHES_PER_RUN`. One runaway run cannot spend the
  day.
- **Alarms.**
  - `no_successful_calls_24h` excludes kind `llm`. Its copy is about "no matches found", and an intel source may not
    change for days.
  - The replacement, **`intel_stale`**, is computed by the API from the database alone, inside
    `GET /v1/admin/providers/health`, since the API never loads `IntelConfig`. It fires when `claude_intel` is enabled
    and either an enabled source has `next_check_at < now() − INTEL_STALE_GRACE`, or a `queued` run is older than the
    grace. The grace is a named constant, 6 hours.
  - Keying it on overdue work catches a dead or stalled worker without firing on sources that legitimately check
    weekly. Like every provider alarm it is pull-only (the `alarms.tf` header).
  - The CloudWatch search alarm is unaffected.
- **Visibility.** Business reviewers see the day's intel spend in `GET /runs`'s `spend` block. The providers surface
  and its kill switch stay developer tier on the backend.

## 6. Security and privacy

### 6.1 No person data goes to the model (#48)

- **Imports.** No module under `intel/` imports any of these, prefix-matched on full dotted names, in the AST style of
  `test_the_collision_module_cannot_assign`:
  - `imageshield.subjects`, `imageshield.attribution`, `imageshield.liveness`, `imageshield.enrolment`,
    `imageshield.review`, `imageshield.confirm`, `imageshield.preview`, `imageshield.recheck`,
    `imageshield.threats.store`, `imageshield.http.routes`;
  - `imageshield.search.store`, `.models`, `.runner`, `.hive`, `.google`, `.worker`.

  `search.urlhash` and `search.provider` stay importable. A companion test asserts that every ban entry resolves to a
  real module, so a dead entry fails the build. The approval transaction that inserts a threat event does so with
  intel's own SQL, not by importing the threat store.
- **Prompt builders** accept typed public inputs only (§4.4).
- **Known hit locations are refused** (§4.3). `intel_rw` gets column-level `SELECT (url_hash) ON content_urls`. That
  is the only read it has of any infringement-adjacent table, and it learns only whether a hash exists. Intel refuses
  by exact URL, never by domain; a domain that predominantly hosts abuse goes into `INTEL_BLOCKED_DOMAINS` by hand.
- **Operator queries** are refused when they contain a phone- or email-shaped run. That a query must not name an
  individual is **policy, not enforcement**, and #48 says so.
- **`coverage_gap` is built from signals alone,** never from where hits land.

### 6.2 Hostile documents

A fetched page can try to instruct the model. That is bounded by construction:
- the model has **no write-capable tool**. The only tool is web search, during discovery.
- Its output is **structured data**, validated against closed schemas and code bounds.
- **Every proposal waits for a human.**

The worst a hostile page can do is produce a bad proposal that gets rejected, or pass a discovery URL to the fetcher,
where the SSRF guard and the known-hit check apply. Model-discovered URLs are https only.

### 6.3 PII in public documents

Breach coverage and news can contain phone numbers and email addresses. Rule §3.2 ("we never see a phone number … not
in a column") is honoured three ways:

1. **Excerpts: dropped.** An excerpt containing a phone-shaped run (the redactor's patterns) or an email-shaped run is
   dropped (reason `pii_in_excerpt`). Dropping protects the verbatim property.
2. **Snapshots: masked** (§3.3).
3. **Model-written and page-derived free text: masked.** Before insert, phone- and email-shaped runs are masked
   (`redact_string`, with its ISO-date and UUID carve-outs, plus the email pattern) in:
   - `intel_documents.title`, `intel_signals.summary`, `intel_proposals.rationale`;
   - every free-text leaf of `target` and `suggested`: event `title` and `body`, each `options[].rationale`, and
     `coverage_gap.subject` and `suggested_question`.

   Masks are counted per field on the run's `outcome`. These fields carry no verbatim guarantee, and dropping would
   discard real evidence over CVE ids, date ranges and support numbers. Closed-vocabulary fields (`question_key`,
   `option`, `kind`, `tags`) are validated against the loaded vocabulary or closed sets instead.

Operator-typed fields (`terms_note`, `decision_reason`, approved `values`) are treated as hand-created threat events
and articles are. The human gate on exact values covers them, and a masked suggested title reaches the operator
already clean. Logs keep going through the existing redaction processor. Document text is never logged.

### 6.4 Build-gate traps this code will hit

- **The phone-shaped gate** flags `web_search_20260209`, beta header strings, ISO dates, dated model ids and per-token
  decimal prices in any `src/` string literal or migration literal. The first four go in config; prices are written
  per million (§5).
- **`test_no_consent_module_hashes_a_document_or_speaks_to_docuseal`** fails any file whose code mentions "consent"
  together with `hashlib`. Hashing lives in `intel/text.py`, and taxonomy strings avoid the word.
- **`test_nothing_writes_the_dormant_score_tables`** matches `update|insert|delete` followed by
  `recommendations|score_events|protection_scores`, even in prose. Intel table names and docstrings avoid those words.

### 6.5 IAM

- **No secret.** Claude Platform on AWS authenticates with SigV4 using the task role. The workspace id is
  configuration, not a secret, so it arrives through `environment`.
- **Dev.** `infra/ecs/policies/services-task-role.json` gains one statement holding only the Claude Platform invoke
  actions recorded by §8 step 0, scoped to the workspace resource, never `*`.
  - **The test goes in `tests/test_ecs_task_defs.py`**, beside its task-role tests, which are the ones that read this
    file.
  - `test_the_task_role_can_only_read_s3_and_only_the_liveness_bucket` stays unchanged. That role *does* read the
    liveness bucket.
  - `tests/test_iam_policy.py` reads the superseded Terraform policy and is not the place for this.
- **Prod.** The role is defined in the backend repo: `release/prod-sep15:deploy/iam/prod/services-task-role.json`,
  rendered by `iam.tf`, which needs no edit. The backend spec owns that change (its §9). It must be applied before
  `claude_intel` is enabled on prod. Until then, prod's `services-worker.json` ships the intel container with
  `INTEL_ENABLED=false`.

## 7. Invariants — new and amended text

**#48. The model proposes; a named human disposes; no person data reaches it.**
- `intel/` is the only place a language model runs in this repo.
- It reads public documents, operator-authored queries and the backend's published scoring vocabulary, and nothing
  keyed to a person.
- It writes only `intel_documents`, `intel_signals`, `intel_excerpts`, `intel_proposals` and `intel_proposal_signals`.
- No proposal takes effect except through `decided`: values an operator approved and the schema stored.
- There is no timeout, confidence level or source trust that auto-approves.
- A known hit location is never fetched for it.
- An operator query must not name an individual. That is policy, and the PII-shape refusal is its only enforcement.

*Check:* the boundary tests (§6.1), the shape CHECKs (§3.6), and a test that no code path moves a proposal to
`approved` except the decision route.

**#49. Every citation is a verbatim substring of text we fetched.**
- An excerpt's normalised text is a substring of the normalised document text fetched through our fetcher, at the
  recorded offsets.
- Text returned by the model's own web tools is never evidence.
- A signal with no verified excerpt does not exist.
- An excerpt holding a phone- or email-shaped run is dropped, never redacted. Model-written text is masked.
- A renewal re-verifies every excerpt before it can be proposed.

*Check:* a fabricated quote is rejected; a paraphrase is rejected; a PII-bearing quote is dropped.

**#50. A web-only claim needs corroboration.** A proposal whose every active signal came from model web search cannot
be approved until signals from at least `CORROBORATION_MIN_PUBLISHERS` distinct registrable domains back it. One
predicate answers that question for every caller. *Check:* one web publisher → 409; two subdomains of one publisher
→ 409; two publishers → approvable.

**#9, amended.**
- "No image bytes persisted" stands unchanged, and `/v1/text` refuses every non-text type.
- The fetcher's claim that "a document is never persisted" gains one scoped exception: **the latest normalised text of
  a `policy_page` intel source**, PII-masked, one row per source, from public pages, persisted only so a change can be
  detected. It is never quoted from.
- **Known hit locations are refused by exact URL** before any fetch. That is what is enforced. The claim that "no
  infringing page is ever read" is *not* made: a page that hosts abuse but is not one of our hits can be read, and its
  text goes to the model provider. Only excerpts and masked summaries persist from it.

**#11, amended.** It is corrected to what is true, rather than cited as met:
- Third-party fetches run only in the no-DB fetcher, behind a post-DNS SSRF check on every hop, capped redirects, a
  byte cap and a timeout.
- The fetcher runs under host networking, so the network layer does not isolate it. The SSRF check is the control.
- Intel URLs are allowlisted by the registry when listed. When discovered by model search, they are allowlisted by
  nothing but the SSRF check, the known-hit check and `INTEL_BLOCKED_DOMAINS`.

The deviation from the original "allowlist from `content_items`" letter is recorded in place, with this spec's date.

## 8. Build order — shared by both specs, each step its own plan

| Step | Services | Backend | Moves a score? |
|---|---|---|---|
| **0** | Region and access check (`devtools/`, throwaway): Claude Platform on AWS in `ap-south-1` and `us-east-1`, with both models; the exact IAM action names; the web search price; the worst-case `cost_per_call_usd`; a 1.x `anthropic[aws]` pin; a `publicsuffixlist` pin | — | no |
| **1** | 0039. `/v1/text`, `intel-worker`, sources, runs, extraction and verification, discovery, cost gating, the reads, sources/documents/signal-retract writes, vocabulary, §1 amendments. Then the budget migration and enable, per environment. | 0062. The read relays and sources routes; **the exposure-tag registry, the option-to-tag map, their routes and `scripts/intel-seed-tags.ts`**; draft rename maps, the publish tag carry and the rename log; gate-applied answer storage plus its one-time cleanup; the vocabulary push (after each release or tag/mapping change, on worker boot, hourly); the `model.ts` `dynamic.protection` block may ship here | no |
| **2** | Proposal generation (`weight_change`, `weight_suggestion`, `coverage_gap`), decisions, `applied`, the §4.9 reconcile | 0063; `model.ts` on api **and** worker; the bootstrap release; weights publish and decision routes; provenance; two-pass attribution; the drift sweep's **version leg only**; stale-draft rule | **yes**, first |
| **3** | step-3 migration. `threat_event` generation and decisions, `gap_regenerate`; hand-created `tags` | Tag matching, the merged reader, `breakdown.scope`, the reach counterfactual and `scope_update`, the drift sweep's **map leg** (one expected catch-up pass), the threat half of event decisions, `threatEventBody.tags` | yes |
| **4** | step-4 migration. `protection_event` generation, decisions and renewal | 0064; the protection engine term, snapshot column, copy, protection routes, the drift sweep's event leg | yes |
| **5** | Weight suggestions, **starting with per-question source proposal, validation (including `robots.txt`) and the first read**; sources pausing with their tags (§4.10) | The quiz editor's suggest action **with its source picker**; provenance from drafts; citation coverage | no |
| **6** | Coverage-gap proposals surfaced | Their display | no |

**Per-step deploy gates:**
- **Services always deploy first** on the way up: every new body field is refused (`422`) by an older services build.
- **Step 1:** in production, `INTEL_ENABLED` stays false until the prod IAM grant is applied.
- **Step 2:** backend `model.ts` reaches api and worker before the bootstrap release runs. The bootstrap may carry
  `dynamic.protection`; it is inert until step 4's migration.
- **Step 4:** services step-4 migration, then backend 0064 and the engine term together, before any protection approval.

**If `ap-south-1` is not served**, dev sets `INTEL_ANTHROPIC_REGION` to the nearest served region, and this spec's
data note records that intel's public-document text is processed there. **Nothing proceeds on an unverified region.**

## 9. Out of scope, deliberately

- **Auto-approval of any kind**, including "high confidence from a trusted source". Decision 1.
- **User-facing citations or sources.** Decision 2.
- **Notifications about intel movements.** A weights sweep is silent, and no digest mentions intel.
- **PDF and JavaScript-rendered pages.** PDF would need a decoder in the fetcher. A single-page-app policy page is
  disabled as `too_short` after its first check (§4.3).
- **Model-driven changes to escrowed, decaying or D-rule weights, escrow milestones, decay tiers, bands, and threat or
  protection points.** Those stay with the quiz editor and migrations.
- **Server-side model fallbacks, the Message Batches API and Managed Agents.**
- **Jurisdiction targeting.** We hold no location, and a phone country code is not consent to be located. A protection
  limited to some places is rejected (§3.7).
- **Future-dated threat events**, which no writer creates today.
- **Anything about minors.** Discovery for minors stays refused. The backend's open P14 item, that coverage does not
  consult discovery eligibility, applies to protection credits too. Recorded, not fixed here.
- **A heartbeat for more than one intel worker.** The single-worker assumption is written down (§3.4).
- **Renaming a question key.** Keys are identities (§4.9). Carrying evidence across a key rename would need the quiz
  editor to record key renames, and nothing records them today.

## 10. Tests that must exist and never be deleted (added to §10's list)

**Evidence**
- An excerpt that is not a verbatim substring of our fetched text is rejected. A paraphrase is rejected. An excerpt
  with a phone- or email-shaped run is dropped, not redacted.
- Model-written summaries, rationales, titles and suggested event text holding a phone or email are persisted masked,
  and the signal survives.
- A signal with zero verified excerpts is never written.
- A fake model whose response holds a phone-bearing quote leaves no phone-shaped run in `provider_calls.raw_response`
  or `error_detail`.
- A URL whose hash is in `content_urls` is never fetched, on paste, discovery, feed item or redirect, and makes no model
  call.

**Isolation**
- `anthropic` is imported only by `intel/model.py`. No `intel/` module imports a banned module, and every ban entry
  resolves.
- `imageshield.fetcher.fetch` is imported only under `fetcher/`.
- `ENVIRONMENT=development` with `INTEL_MODEL_PROVIDER=claude` refuses to boot, and the dev builder constructs no live
  client.
- No container's `environment` carries `DATABASE_URL`.

**Approval**
- No code path moves a proposal to `approved` except the decision route.
- The shape CHECKs refuse an approved row without a name, and an approved event or weight proposal without `decided`.
- An event row is inserted from `decided` only: an operator's edit wins over `suggested`, and an out-of-bounds edit is
  refused.
- A web-only proposal backed by one publisher, or by two subdomains of one publisher, cannot be approved. Two
  publishers make it approvable. `approvable` on the read always equals "the decision does not 409".
- A `weight_change` against a non-mutable question, an unknown option, a `current` that is not the vocabulary's
  deduction, or a delta outside ±2 is never written.
- Two simultaneous approvals of one proposal produce one event and one `409 proposal_not_pending`. Two approvals of
  different proposals for one cell produce one `409 proposal_cell_awaiting_publish`.
- A model-proposed global protection is never written. A protection approval without the location attestation is
  refused.
- An event kind is refused before its step ships.

**Pipeline**
- An unchanged source hash makes no model call.
- A gate skip or timeout leaves the unit unconsumed and retried. A refusal consumes it.
- A feed's backlog past 10 items is processed on later runs even when the feed is unchanged.
- An item older than 30 days is skipped.
- A reclaimed run neither duplicates documents nor generates proposals twice.
- A run at `MAX_RUN_ATTEMPTS` fails for good.
- A `policy_page` under 500 characters disables itself without a model call.

**Cost**
- An `llm` provider with no daily budget runs nothing.
- A refusal and a verification failure never open the breaker.
- An unknown model id refuses the call.
- `calibrate trust claude_intel` is refused.

**Schema**
- Every intel table's grant, and the `content_urls` column grant, works as `app_services`.
- 0039's down succeeds after `claude_intel` has call and spend rows.
- Step 3's down refuses while a tag-only threat is active, and succeeds (restoring the old CHECK `NOT VALID`) once
  it is retracted.

**Contract**
- `svc.v_active_scoped_events` carries no person column. A retracted, expired or not-yet-started event of either
  direction is absent from it.
- **Vocabulary pushes are ordered by the pair.** A push of `(R, M−1)` after `(R, M)` is a no-op answered 200, and the
  stored map is unchanged. A lower `release_no` is a no-op even with a higher `map_version`. The equal pair `(R, M)`
  overwrites. `(R, M+1)` and `(R+1, M)` both apply.
- `/v1/text` refuses a private address on a redirect hop, a DTD in a feed, and an unsupported type, and reports
  `truncated` honestly.
- A renewal whose excerpts no longer verify writes no proposal. An approved renewal starts exactly at the old
  `review_by`.

**Adapting to the quiz (§4.9)**
- A vocabulary with a new `mutable` question yields `weight_change` proposals for it on the next run, with no code
  change.
- A rename in `renamed` retargets a pending `weight_change` whose `current` still matches, and supersedes one whose
  does not. An approved one is never modified, and reads `stale`.
- A removed option supersedes its pending `weight_change` with `cell_changed`.
- A newly mapped tag supersedes the pending `coverage_gap` about it with `resolved_by_quiz`.
- A second reconcile of the same `(release_no, map_version)` changes nothing.
- An extraction that returns an unregistered slug drops that tag (`unknown_tag`). A retired tag an operator *adds* in
  `values` is refused (`tag_retired`). A pending proposal whose own tag was retired after it was written is still
  approvable, with no `values`.
- `intel_tags_well_formed` accepts `x`, and refuses a malformed slug and a duplicate in the array.
- A threat proposal whose tags are all unmapped is written `pending`, reads `approvable = false` with `why_not =
  'tags_unmapped'`, is refused `409 proposal_tags_unmapped`, and becomes approvable as soon as a push maps one of its
  tags.
- Mapping a tag that resolves a pending `coverage_gap` queues exactly one `gap_regenerate` run in the same transaction
  as the supersession. That run writes the event proposal from the gap's signals.
- A rename log `A → B` at release 5 and `B → C` at release 6, reconciled together, retargets a pending proposal on A to
  C.
- Every signal and proposal can name the `(release_no, map_version)` of the vocabulary it was produced against.

**Sources chosen per question (§4.10, added 2026-09-30)**
- A source proposal lists existing sources whose tags intersect an option's first, drops a known hit location and a
  PII-shaped query, and registers nothing.
- Validation blocks a known hit location, a non-https final URL, an app shell under the text floor, a
  `robots.txt`-disallowed path, a feed with no items, and a search query whose search returns no fetchable page, each
  with its reason, and makes no model call.
- `POST /weight-suggestions` refuses a source that is not `ready` in the named validation run, or whose result is
  older than 24 hours (`422 source_not_validated`), and registers nothing when it refuses.
- A source already in the registry is reused, not duplicated, when chosen again.
- A suggestion run reads its new sources first, stops at `INTEL_MAX_CALLS_PER_SUGGESTION_RUN`, reports
  `sources_deferred`, and still returns a suggestion.
- Unmapping every tag of a source pauses it with `disabled_reason = 'unmapped'`; mapping one again re-enables it; an
  operator-disabled source is never re-enabled by the reconcile; an untagged source never pauses.
