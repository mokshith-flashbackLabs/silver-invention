# Auto-confirming an `ncii_suspected` hit, and hiding it from its subject — design

**Date:** 2026-09-14 · **Status:** approved (owner, in-session), building on branch
`feat/control-room-split` · **Amends:** `INVARIANTS.md` #19, #44 and #47, `CLAUDE.md` §1/§4/§6,
`SCHEMA.md` (the `confirm_state` state machine, the audit vocabulary) and `PROXY_INTEGRATION.md`
(the preview and decision rows, the presentation rule).
**Builds on:** `2026-08-21-subject-verified-hits-design.md`, `2026-08-19-protection-score-design.md`.

> **Scope note.** This document covers the auto-confirm half only. A later task in the same cycle
> adds the reviewer feed, verdicts and the operator preview, and records them **in this spec** —
> the file name is deliberately wider than its current contents.

## 0. The decision, on the record

Today every machine-triaged hit becomes an **ask-card**: the subject sees a blurred frame and
answers "is this your photo?", and their answer writes the confirm/reject decision (spec
2026-08-21). Two owner calls on 2026-09-14 change that for one severity.

**D3 — auto-confirm applies to exactly one severity, `ncii_suspected`.** That is explicit content
(Rekognition moderation, `is_explicit`) AND a face match at or above `confirm_face_match_threshold`.
When the pipeline has established both, putting "is this your photo?" in front of the person is the
wrong product: it asks a victim to look at their own abuse imagery to tell us something we already
know. We mark it infringing ourselves.

`explicit_unmatched` — explicit, but the face match **failed** — deliberately stays a human
decision. A failed face match is the strongest false-positive signal this pipeline has, and §1 of
`CLAUDE.md` is unambiguous about what a false positive costs here. The other three severities are
not close to the question.

**D4 — the subject gets nothing on such a hit.** No preview, no decision, no action. The backend
refuses the user-facing routes (a separate task, a separate repo); this repo refuses the subject
preview as defence in depth and keeps refusing a subject decision.

**A reviewer override stays possible.** `record_auto_confirmed` still writes a `pending`
`review_tasks` row, so `review/store.py::decide` can reject the hit exactly as it could before.
The machine's verdict is a default, not a terminus.

**The safety implications of confirming without a human are being handled by counsel, not by us.**
That was stated explicitly when the decision was taken. What this repo owes is honesty: the
invariants this contradicts are amended in the same commit rather than quietly broken. #19, #44 and
#47 all moved; #23 did not, and says why.

## 1. What the worker does

`confirm/worker.py` step 9 forks after `classify`:

| Severity | What happens |
|---|---|
| `ncii_suspected` | `record_auto_confirmed(...)`, an info log `confirm.auto_confirmed`, then a score recompute under a new `cause_kind='auto_confirm'` |
| everything else | `record_triage(...)`, byte for byte as before |

Both branches build the **same** `triage` payload — `image_url`, `best_face_bbox`,
`face_match_score`, the moderation label names, `phash_degenerate`. A reviewer's working notes
should not depend on which branch ran.

The recompute is wrapped: a failure **logs and does not redeliver**. The confirm has already
committed by then, so redelivering would re-run steps 3–8 — a second fetch and a second Rekognition
bundle, billed — only for the guarded UPDATE to refuse the write the second time round. The score
tick heals the drift. This is the same contract `search/worker.py` takes after `execute_run`.

`ConfirmDeps` gains a `score` field, constructed in `build_deps` the way `search/worker.py`'s
`run_forever` already constructs one: weights and `config_version` off the same `Config`, so the two
workers cannot journal under different rulesets.

## 2. What the store writes

`confirm/store.py::record_auto_confirmed`, one transaction, mirroring `record_quarantine`:

1. A guarded `UPDATE` on `infringements` keeping the module's existing
   `AND confirm_state IN ('unconfirmed', 'machine_triaged')` predicate — a human decision is never
   clobbered and a redelivered SQS message is a no-op (rowcount 0 returns silently). It sets
   `confirm_state='confirmed'`, `severity='ncii_suspected'`, the phash, the score, the labels, and
   `confirm_decided_by`/`confirm_decided_at` **in the same statement**, which is what makes 0021's
   `infringements_confirmed_needs_human` CHECK unfalsifiable rather than something application code
   remembers.
2. The existing review-task upsert: `pending`, `severity='ncii_suspected'` (so it sorts first in
   the queue), `triage` carrying an `auto_confirmed: true` marker. **This is the override lane.**
3. One `audit_log` row — `actor_type='service'`, `action='confirm.auto_confirmed'`, metadata
   `{severity, face_match_score, decided_by}`. The machine deciding something about a person is a
   decision about them and is recorded as one.

**`status` is deliberately NOT touched** — it stays `new`. `status` is the *subject's own position*
on a hit (`acknowledged` / `dismissed_not_me` / `authorised` / `user_resolved`) and nothing the
machine does may author it. `confirm_state` is the lifecycle column and it is the only one moving.

`AUTO_CONFIRM_DECIDED_BY = "auto:nsfw"` and `AUTO_CONFIRM_SEVERITY = "ncii_suspected"` live in
`confirm/models.py`, one definition each. The marker is the only non-human value
`infringements.confirm_decided_by` may ever carry.

### 2a. The dedup set gains the machine confirm, on purpose

`_DECIDED_PHASHES_SQL` selects `confirm_state IN ('confirmed', 'rejected')`, and its comment said
only a HUMAN decision seeds that set. That is no longer true and should not be made true again:
near-duplicates of an auto-confirmed image now collapse onto it as a `duplicate` instead of being
auto-confirmed N times over. One picture, one finding — the same reason a human confirm seeds it.
The comment says so.

## 3. Two behaviours that already worked

Neither needed a code change. Both are pinned by tests because D3 and D4 rest on them and either
could be broken by an innocuous-looking change elsewhere.

- **An operator can still reject an auto-confirmed hit.** `review/store.py::decide` locks its task
  with a `status = 'pending'` predicate, and its `infringements` UPDATE carries no `confirm_state`
  guard — so because the auto-confirm leaves the task `pending`, an operator `rejected` overwrites
  both the state and `confirm_decided_by`.
- **A subject cannot overturn a machine confirm.** `subject_decide`'s existing predicate is
  "already decided, and `decided_by != 'subject'` means conflict". `'auto:nsfw'` is not
  `'subject'`, so it already yields the conflict the route maps to `409 decision_conflict` — for
  *both* decision values, since the same-answer replay branch is reached only for a subject's own
  earlier answer.

## 4. Exposure yes, posture no

The posture component counts a confirmed hit as "awaiting the subject's feedback" when nobody has
given feedback and the subject did not decide it. An auto-confirmed hit can **never** receive the
subject's feedback — we refuse to show it to them and refuse to take their answer. Charging posture
for it would penalise a person for not answering a question we never asked, which is exactly the
shape INVARIANTS #45's amendment forbids; the same counter drives the `respond_to_hits`
recommendation, which would otherwise tell them to open a hit they cannot open.

So `_CONFIRMED_HITS_SQL`'s `no_feedback` predicate gains
`AND i.confirm_decided_by IS DISTINCT FROM %(auto_marker)s`, beside the `'subject'` clause that is
already there for the mirror-image reason.

**Exposure still counts the hit.** The exposure is real whoever decided it, and understating a real
harm to be tidy about posture would be the worse error. The test asserts both halves against an
operator-confirmed control, because a change that withheld exposure too would pass a posture-only
check.

No `SCORE_CONFIG_VERSION` bump. The predicate is keyed on a marker no historical row can carry —
nothing could write `'auto:nsfw'` before this change — so every existing journal row stays
interpretable under the version it was written with.

## 5. The subject refusal, and why the predicate is what it is

`preview/store.py::_TARGET_SQL` gains a selected column
`(i.confirm_state = 'confirmed' AND i.severity = 'ncii_suspected') AS restricted`, and
`PreviewTarget` gains `restricted: bool = False` (defaulted, so existing constructions still mean
"an ordinary hit").

The **route** refuses, not the store: `GET /v1/infringements/{id}/preview` raises
`404 preview_unavailable` after the not-found check and **before** the bbox check, the render
ceiling, the audit row and the render. A refused render is not an attempt — nothing is audited
(#31 is about renders that happened) and nothing is charged to the ceiling (#32). The code and
message are identical to the ordinary unrenderable case, so the response distinguishes nothing.

**Why the state+severity predicate rather than the `auto:nsfw` marker alone.** Every auto-confirmed
row is `confirmed` + `ncii_suspected`, so D4's requirement is met either way. The state+severity
form *also* covers an **operator**-confirmed `ncii_suspected` hit, which the backend presents the
same restricted way — and keeping the two sides of the boundary on one predicate is what stops them
drifting. This is the contract: **no `svc` view changed at all**, and the backend derives everything
from two columns it already reads on `svc.v_person_hits`.

**`target()` does NOT return `None` for a restricted hit**, and that is load-bearing.
`svc.v_person_hits.preview_available` (0031) mirrors `_TARGET_SQL`'s renderability logic, and
`tests/test_svc_preview_available.py` asserts the two agree row by row. That column means *"a
picture exists"*. Collapsing a restricted hit to `None` would silently redefine it into "the subject
may look at this" — a different question, published to another repo, with no migration announcing
the change.

## 6. Testing

- `tests/test_confirm_store.py` — the confirmed row with the machine marker and **both** decided
  columns non-null (the 0021 CHECK held on a row no human touched), `status` left at `new`, a
  `pending` task carrying `triage.auto_confirmed`, exactly one audit row; a no-op against an
  already-decided row; an auto-confirmed row appearing in the decided-phash dedup set.
- `tests/test_confirm_worker.py` — an `ncii_suspected` outcome auto-confirms, recomputes with
  `cause_kind='auto_confirm'`, and records **no** triage; `explicit_unmatched` still only triages
  and moves no score; a score store that raises does not redeliver.
- `tests/test_review.py` — an operator rejects an auto-confirmed hit; a subject decision on one
  returns conflict for **both** decision values.
- `tests/test_score_store.py` — an auto-confirmed hit costs the same exposure as an
  operator-confirmed one but is not counted as awaiting feedback, and `respond_to_hits` stays shut.
- `tests/test_preview_routes.py` / `tests/test_preview_store.py` — a restricted finding answers
  `404 preview_unavailable`, renders nothing, writes **no** audit row, and is byte-identical to the
  no-bbox refusal; the flag is set for an operator-confirmed `ncii_suspected` hit too and unset for
  a machine-triaged one.
- `tests/test_svc_preview_available.py` — a restricted-finding case expecting `True` on both sides,
  documenting that the view column still means "a picture exists".

## 7. Not doing

- No migration. Nothing in the schema changes; `score_events.cause_kind` has no CHECK, so
  `auto_confirm` is additive.
- **No `svc` view change, and no change to `svc_contract.EXPECTED_VIEWS`.** The narrowness of the
  contract is the point (§5).
- No auto-*rejection* and no machine-written dropped or invisible state. §7.3's reasoning is
  unchanged: a real infringement made invisible is worse than one left in front of a human late.
- No change to the quarantine lane, the render ceiling, or the per-render audit.
- No widening to `explicit_unmatched`. That is D3's explicit boundary, not an omission.
