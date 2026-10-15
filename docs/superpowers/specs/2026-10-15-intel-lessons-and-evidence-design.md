# Lessons and published evidence for the weekly report — design

Date: 2026-10-15. Owner request, after reading the mobile team's mock-ups of the 2026-10-13 weekly
report: "it just looks like any other data in the app being reused … no value for the user is added
… does not express the complexity of our work … let's make this feature a standout … every week they
learn something or get value in some way or other."

This is the services half of two coordinated changes. The backend half,
`image_backend/docs/superpowers/specs/2026-10-15-weekly-report-standout-design.md`, assembles the
report, holds the on/off switches and serves the app. This half gives it three things it cannot
produce itself: **lessons** (§3–§5), **the evidence behind each approved event and lesson** (§6), and
**the week's research totals** (§7), all through three new read-only `svc` views (§8). Nothing here
reads or writes person data, and nothing reaches a person without an operator's approval.

## 1. Why

The report the backend built on 2026-10-13 repeats what the app already shows: score history's lines,
the recommendations card, the photos screen's counts. What this repo knows and the report never says
is the work behind each line: the dated articles the AI read, from which publishers, the quotes it
verified against the text, how many independent sources agree, and that a person approved it. None of
that is on any `svc` view, so an event reaches the app as a title and two sentences.

## 2. What the owner decided

| # | Decision |
|---|---|
| D1 | **Lessons come from two places.** Every threat and protection proposal carries a drafted lesson, decided with the event; and a seventh proposal kind, `lesson`, drafts standalone lessons the research supports (a technique, a defence, a right) into the approval queue. The backend picks one unseen lesson per person per week. |
| D2 | **Evidence is published for every approved event and lesson**: publisher, date, title, verified quotes, and how many independent sources agree. |
| D3 | **Only vetted sources get a link.** A document fetched from a source an operator registered (`trust = 'listed'`) carries its URL on the view; a web-search find (`trust = 'web'`) carries publisher and date only. An unvetted page can advertise the very apps an incident is about or carry explicit imagery. |
| D4 | **The week's research totals are published as counts**: articles read, publishers, risks found, events and lessons approved and rejected. |
| D5 | **Three new views**, granted to `imageshield_proxy_ro`. This is the rule-5 "ask first" (CLAUDE.md §3.6, backend §4 rule 5) and the owner's approval of this spec is that approval. |

## 3. A lesson

One shape for every lesson, wherever it comes from:

| Field | Shape and bound |
|---|---|
| `title` | ≤ 60 characters. |
| `intro` | ≤ 400 characters. For a threat, how it works; for a protection, what it is; for a general lesson, what it is about. |
| `sections` | 1–2 of `{kind, items}`. `kind` is `warning_signs`, `what_to_do` or `how_to_use`, each at most once; `items` is 1–5 strings of ≤ 160 characters. A threat lesson has `warning_signs` and `what_to_do`; a protection lesson has `how_to_use`; a general lesson any one or two. The headings are not stored here: the backend's playbook words them. |
| `link` | `{url, label}` or null. `url` must be the `final_url` of one of the lesson's own cited documents **from a listed source**; `label` ≤ 40 characters. The same rule a drafted threat action's link already follows, narrowed to vetted sources (D3). |

**Every word is checked** by `intel/user_copy.py`, the rules the score page's words pass:
`copy_problems` on the title, intro, every item and the link label (no reassurance, no "found",
"match", "hit" or "your photo", no "your answer", no figure spelled in words, no address), and
`unquoted_figures` against the lesson's cited quotes, so every number in a lesson appears in a quote
the AI verified. A lesson that fails is refused at draft (the model gets its existing one repair turn)
and again at decision (`422 lesson_invalid`, `problems` naming the field and the rule).

## 4. Event lessons

- `ThreatEventSuggested` and `ProtectionEventSuggested` gain `lesson: LessonDraft | None`. The propose
  step drafts one for every threat and protection it proposes, from that proposal's own evidence;
  `None` only when the model cannot back one, and the proposal read says so (`lesson_missing: true`).
- **Decision**: the existing `POST /v1/admin/intel/proposals/{proposal_id}/decision` takes
  `values.lesson`: a whole lesson replaces the drafted one, `null` removes it, absent keeps it. The
  approved lesson is written to `intel_lessons` (§8) **in the decision's transaction**, beside the
  event, with `event_id` set.
- **A renewal** carries the prior protection's lesson forward unless the decision replaces it, as it
  already carries `body`.
- **An event that already exists** gets its lesson through §5's `lesson_draft` run.
- A hand-created threat (no `proposal_id`) has no evidence, so a lesson cannot be drafted for it.

## 5. The `lesson` proposal kind

`intel_proposals.kind` gains `lesson` (migration 0055). Statuses as every decidable kind: `pending` →
`approved` or `rejected`, or `superseded`.

| Field | Shape |
|---|---|
| `target` | `{topic, event_id}`. `topic` is a slug (≤ 60, `[a-z0-9_]`) naming what the lesson teaches, used to recognise repeats; `event_id` is set only for a lesson drafted for an existing event (below). |
| `suggested` | `{lesson, tags, is_global}`. `tags` are registered slugs the lesson is relevant to (may be empty); `is_global` true when it applies to everybody. |
| `decided` | The same shape, as the operator approved it. |

- **Generation (`propose-v13` → `propose-v14`)**: the propose step may draft a general lesson when
  the evidence describes a technique, a defence or a right rather than one incident (signal categories
  `tooling`, `protection`, `law`, `research`). It needs the corroboration a threat needs (a listed
  source, or `CORROBORATION_MIN_PUBLISHERS` independent ones; `intel/corroboration.py`), and the same
  recency rule (`evidence_stale`). A pending or approved lesson on the same `topic` makes a new one a
  repeat: it is not written, the existing overlap pattern (`intel/overlap.py`).
- **For an existing event: a new run kind, `lesson_draft`.** `POST /v1/admin/intel/lesson-drafts`
  `{event_ids?: UUID[]}` → `202 {run_id}`; without ids it covers every live event with a proposal and
  no lesson. Each lesson is drafted from that event's own evidence and written as a `lesson` proposal
  with `target.event_id`, decided in the queue like any other. Metered by the provider gate as every
  intel call is.
- **Decision**: the existing decision route. Approve (with optional `values` edits) writes the
  `intel_lessons` row; reject records the reason. An approved general lesson stays live until retired.
- **Retire**: `POST /v1/admin/intel/lessons/{lesson_id}/retire` `{reason}`. The lesson leaves the
  view at once. An event lesson also leaves the view when its event stops being live (expired,
  retracted, a protection past its review date).
- **List**: `GET /v1/admin/intel/lessons?status=live|retired` for the control room's library.

## 6. Published evidence

What a person is shown must be what the operator approved on, and the independence count is computed
in Python (`intel/corroboration.py` `independent_sources`: echoes collapsed, a company's own domains
one organisation), so it cannot be a view expression. So **the decision snapshots it**:

- In the transaction that approves a threat, a protection or a lesson, services write
  `intel_published_evidence` (one row per cited excerpt: owner, document, excerpt) and
  `intel_published_owners` (one row per owner: `independent_sources`, `approved_at`).
- **A retraction updates it.** `POST /v1/admin/intel/signals/{signal_id}/retract` removes that
  signal's rows from every owner that cites it and recomputes each owner's `independent_sources` in
  the same transaction, so a retracted article never reaches a report.
- **Events approved before this change** get theirs from a one-off script,
  `scripts/backfill_published_evidence.py` (idempotent), run once per environment after the migration.

## 7. The week's research totals

Counted per Mon–Sun week (UTC), the backend's report week:

| Column | Counts |
|---|---|
| `documents_read` | `intel_documents` fetched in the week |
| `publishers` | distinct `publisher_domain` among them |
| `risks_found` | active `intel_signals` created in the week with `direction = 'risk_up'` |
| `proposed` | `threat_event`, `protection_event` and `lesson` proposals created in the week |
| `approved` / `rejected` | those kinds decided in the week, by status |

Counts only: no title, no URL, no operator.

## 8. Migration 0055 and the views

- `intel_proposals.kind` CHECK gains `lesson`; the `decided` CHECK requires it for an approved `lesson`.
- `intel_runs.kind` CHECK gains `lesson_draft`.
- `intel_lessons`: `lesson_id` PK, `proposal_id` (the event's or the lesson's proposal), `event_id`
  (nullable), `kind` (`threat`, `protection`, `general`), `topic`, the §3 fields, `tags`, `is_global`,
  `approved_by`, `approved_at`, `retired_by`, `retired_at`, `retire_reason`. Soft retirement only.
- `intel_published_evidence` and `intel_published_owners` (§6), `owner_kind` in (`event`, `lesson`).
- Each new table gets its own grant to `intel_rw` (the backend's migration-grant trap applies here too).

**The views**, each appended to the contract and granted `SELECT` to `imageshield_proxy_ro`:

| View | Columns |
|---|---|
| `svc.v_evidence` | `owner_kind`, `owner_id`, `document_id`, `publisher_domain`, `document_title`, `published_at`, `excerpt_id`, `quote`, `link_url` (the document's `final_url` when `trust = 'listed'`, else NULL), `independent_sources`, `approved_at` |
| `svc.v_lessons` | `lesson_id`, `event_id`, `kind`, `topic`, `title`, `intro`, `sections` (JSONB), `link_url`, `link_label`, `tags`, `is_global`, `approved_at`. Live lessons only: not retired, and an event lesson only while its event is live. |
| `svc.v_intel_weekly_totals` | `week_start` (DATE, a Monday), `documents_read`, `publishers`, `risks_found`, `proposed`, `approved`, `rejected` |

`owner_id` is an event id or a lesson id. The backend treats all three as OPTIONAL for readiness:
absent, the sections they feed are empty. `GET /readyz` here asserts their shape like every `svc`
view.

## 9. Manual amendments (same change)

- CLAUDE.md §3 (the proxy's grant list) and §6 (the intel row): lessons, `lesson_draft`, the three
  views.
- `PROXY_INTEGRATION.md` §6 and `SCHEMA.md` §2c: the views and their columns.

## 10. Tests that must exist

- A lesson with a reassurance, a result word, an address, a spelled figure or a figure no cited quote
  carries is refused at draft and at decision; a link to a web-only document is refused.
- Approving a threat with `values.lesson` writes the event and the lesson in one transaction;
  `null` writes no lesson; a renewal carries the lesson forward.
- A `lesson` proposal on a topic already pending or approved is not written.
- `lesson_draft` drafts only for live events with a proposal and no lesson, and each result waits in
  the queue.
- Retiring a lesson, and expiring or retracting its event, removes it from `svc.v_lessons`.
- Approval snapshots the evidence and `independent_sources`; retracting a cited signal removes its
  rows and lowers the count; the backfill script is idempotent.
- `svc.v_evidence.link_url` is NULL for every `trust = 'web'` document.
- `svc.v_intel_weekly_totals` counts a fixed fixture exactly, week by week.
- The three views are readable as `imageshield_proxy_ro` and expose no base table; `/readyz` fails on a
  wrong-shaped one.
- No view carries a `user_ref`.

## 11. Deploy order and rollback

1. Services first: 0055, then services and services-worker together (`propose-v14`, the routes, the
   snapshot on decision). Then `scripts/backfill_published_evidence.py` once.
2. Then the backend half.

Rollback: the backend first, then services. 0055's down refuses while `intel_lessons` holds a row, the
guard 0077's down uses on the backend for push units, so an approved lesson is never dropped by a
routine rollback. Where lessons exist, rolling back the code is enough: the tables and views are
inert without a reader.

## 12. Not in this change

- Evidence for weight-change lines (a `weight_change` proposal's evidence); events and lessons only.
- Any person data, per-person drafting, or a lesson written for one person.
- Takedown help beyond what a protection lesson explains.
