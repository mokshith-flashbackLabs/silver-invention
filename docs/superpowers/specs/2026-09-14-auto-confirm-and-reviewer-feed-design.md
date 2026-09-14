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

---

# Part II — the reviewer feed, verdict labels, review stats, and an audited operator preview

*Appended 2026-09-14, same day, same owner conversation. Part I above is the auto-confirm half
(D3/D4); this is the half it was named after and did not yet contain.*

## 8. Why

Face matching runs on AWS Rekognition today. The team is building its own models, and **before
that swap they need a measured false-positive rate** — which means a human reviewing real hits
and recording whether the machine was right.

Two things stood in the way, and they are different problems with different fixes.

**Nobody could see the hits.** The control room had a queue depth and `GET /v1/admin/review/next`,
which is one task at a time in priority order. There was no way to look at every hit a person got,
no way to filter by severity or state, and no way to page. A sample you cannot select is not a
sample.

**Nobody could record the answer as a label.** `POST /v1/admin/review/{task_id}/decision` exists,
but it *decides* — it moves `confirm_state`, retires the task, and recomputes the person's score.
"The machine was wrong about this" and "this hit is not infringing" are not the same claim, and a
reviewer who has to express the first by performing the second corrupts both the product state
and the measurement.

**And nobody could see the image.** INVARIANTS #19 said staff never see hit imagery, blurred or
otherwise. That rule was written when the only staff need was adjudication.

## 9. D5 — the operator preview is the subject's render, audited

**Owner's decision.** Reviewers get **the same render the subject would get**: whole frame
blurred, the matched face sharpened on tap. Judging a false positive without seeing the face is
not review, it is guessing.

The decision comes with three conditions, and the first is the one that makes it implementable
without reopening #23.

**No new render mode. The same function, the same arguments.**
`GET /v1/admin/infringements/{id}/preview` resolves the same image address and the same
`review_tasks.triage -> 'best_face_bbox'` the subject route resolves, and calls
`crop_client.crop(url=…, bbox=…, blur=not reveal)` — the identical call. There is no operator
parameter the fetcher understands, because the fetcher was never told there are two kinds of
caller. **INVARIANTS #23 is therefore unchanged**, and unchanged structurally rather than by
promise: "no code path returns a fully sharp frame" is still a statement about one code path. A
change that widened it would have to be made in the fetcher, where it would break the subject's
render too — which is exactly the property worth having.

**Every view is audited, with the operator's name, before the render** (#31). One
`preview.rendered` action for both viewers, distinguished by `actor_type`, because the question
an audit of somebody's imagery has to answer is "who rendered *this hit*", and that must stay a
single filter. `subject_ref` names whose hit was rendered in both cases; the operator's name is
in the metadata, since the column is already spoken for.

**A quarantined hit renders to nobody.** `operator_target` excludes it. A restricted finding — a
confirmed `ncii_suspected` hit, D4's refusal — is *not* excluded, and that asymmetry is
deliberate: D4 protects the subject from being shown their own abuse imagery, and auto-confirm is
the one place in this pipeline where no human ever looked, so it is precisely what a
false-positive review must be able to check.

## 10. The controller's ruling — a per-operator render ceiling

The abuse case #32 guards against is a compromised account replayed as a browsing console. The
operator preview creates a second viewer with a worse blast radius: a compromised *operator*
account reaches everybody's hits, not one person's. An audit records that; it does not stop it.

So there are two ceilings, `REVIEW_OPERATOR_DAILY_RENDER_CEILING` (config, default 500) beside
`PREVIEW_DAILY_RENDER_CEILING` (200), counted separately — and the subject's count is narrowed to
`actor_type = 'subject'`, or a reviewer working through somebody's hits would exhaust that
person's own allowance and lock them out of their report.

The default is deliberately far above honest review throughput: a reviewer opening a different
hit every minute for a full eight-hour shift reaches 480. A brake that binds on somebody doing
the work gets raised until it does not, and then it is not a brake.

## 11. D6 — a verdict is a label, not a decision

**Owner's decision.** `true_positive` / `false_positive` / `unsure` is a **label**. It never
changes `confirm_state`, never touches `review_tasks`, and the existing decision route remains
the only override.

`review_verdicts` (migration 0033) is append-only — `GRANT SELECT, INSERT` to `search_rw`, the
`score_events` shape, because an editable measurement is not a measurement. `record_verdict`
writes the verdict row and one audit row and nothing else; the test asserts the `infringements`
and `review_tasks` rows are **byte-identical** either side of the call, whole rows rather than a
field list, because a field list only catches the columns somebody thought to name.

The route recomputes no score, unlike every other write on this admin surface. A verdict changes
no user-facing fact, so there is nothing to recompute — that is D6 expressed in code rather than
in a comment.

**Three snapshot columns** (`machine_severity`, `face_match_score`, `confirm_state_at_verdict`)
are copied at the moment of the verdict rather than joined at read time. A false-positive rate
computed against a severity an operator later overrode would measure the override, not the
machine.

## 12. The statistics, and the rate that must not be fabricated

`GET /v1/admin/review/stats` reports three groupings over a window, and the arithmetic has two
opinions in it.

**`unsure` is not a disagreement.** It counts in `total` and on neither side of
`fp / (tp + fp)`, and it is excluded from the subject-agreement comparison. A reviewer who could
not tell has told us something real, and scoring it as "the machine was wrong" would make a
cautious reviewer look wrong.

**A zero denominator is `null`, never `0.0`.** "We measured no false positives" and "we measured
nothing" are different claims, and only one of them belongs anywhere near a decision about
replacing a matcher. The same holds for `agreement_rate`.

`by_severity` and `subject_agreement` read the **latest verdict per hit** — a reviewer who looked
twice changed their mind once, and counting both would let one hit move the machine's measured
accuracy twice. `by_operator` reads every row, because that is throughput, and somebody who
looked twice did the work twice.

## 13. What did not change

- **INVARIANTS #23**, for the reason in §9: one render path, one set of arguments.
- **The decision route.** `decide` is untouched; only its docstring gained the note that an
  auto-confirmed hit's task stays `pending`, which is what makes `decide('rejected')` the
  override lane.
- **No `svc` view.** The feed is an admin surface; the proxy reads nothing new.
- **The quarantine lane.** Quarantined hits are absent from the feed, unlabellable, and
  unrenderable — by the store's `WHERE`, not by a route check, so no filter combination reaches
  one.
- **`image_url` is still never rendered by a panel.** It ships on the feed as text evidence for
  URL-context review. The audited preview is the only path by which pixels reach a reviewer, and
  `docs/ADMIN_PANEL_INTEGRATION.md` rule 5 now says exactly that instead of "never".

## 14. Testing

- `tests/test_review.py` — feed paging stable across a page boundary (keyset, not offset);
  quarantined hits absent under four filter shapes including `confirm_state='quarantined'`;
  filters compose rather than last-wins; a verdict leaves both rows byte-identical and writes one
  audit row; two verdicts both survive and the feed shows the latest; stats count the latest
  verdict per hit, measure subject agreement excluding `unsure` and excluding operator-decided
  hits, and answer `None` rather than a fabricated rate.
- `tests/test_admin_hits_routes.py` — the cursor round-trips; a corrupt and a
  well-formed-but-nonsense cursor are both `422 invalid_cursor` and reach no store; verdict
  `201`/`404`/`422`; the preview's audit row precedes the crop call and names the operator;
  `reveal=true` passes the subject path's own `blur=False`; a missing `operator` is `422` and
  renders nothing; the ceiling is `429` with **no** audit row; the cache headers are pinned.
- `tests/test_preview_store.py` — `operator_target` needs no ownership, refuses a quarantine, and
  still offers a restricted finding; an operator render does not spend the subject's ceiling; the
  operator ceiling counts per operator and per window; both viewers write one action
  distinguished by `actor_type`.
- `tests/test_migrations.py` — `review_verdicts` is insert-only for `search_rw`; the two CHECKs
  refuse an unnamed operator and an unknown verdict; both new indexes exist and the down half
  drops them without touching 0024's.

## 15. Not doing

- **No route that edits or deletes a verdict.** Append-only means a correction is a new row.
- **No auto-computed "the machine is good enough" threshold.** The stats are a measurement; the
  decision to swap matchers is a human one.
- **No verdict on a quarantined hit**, and no operator render of one. Escalation from a
  quarantine is the manual legal process in `docs/OPERATIONS.md`.
- **No `svc` view for verdicts.** The subject has no business reading what a reviewer thought of
  their hit, and the proxy has no use for it.
