# The AI proposes quiz questions — design

Date: 2026-10-13. Owner request: "we should make AI generate articles and suggested question". Then,
on what that means: "the suggested questions should be the 6th dimension to what AI produces ... like
how it proposes coverage gaps it should propose questions to ask the user and when accepted lets
completely recalculate or redo the quiz scoring lifecycle with this question added to the 6
questions".

This is the services half of two coordinated changes. The backend half,
`image_backend/docs/superpowers/specs/2026-10-13-ai-proposed-quiz-questions-design.md`, relays the
decision, keeps approved questions waiting, and publishes them as a new quiz version. This half
makes the AI propose a question, researches its answers' points, and lets an operator decide it.
(Articles were raised in the same message and are not part of this design.)

## 1. What the owner decided

| # | Decision |
|---|---|
| D1 | **A sixth proposal kind, `quiz_question`**, beside `weight_change`, `threat_event`, `protection_event`, `weight_suggestion` and `coverage_gap`. Not a field on a gap: a gap's one-line `suggested_question` stays as it is. |
| D2 | **Proposed from evidence**, in the same pass that proposes threats, protections and gaps, when several independent publishers describe an exposure the live quiz does not ask about. Not only from gaps, and not only on an operator's press. |
| D3 | **It arrives complete.** The question, its answers, each answer's tags, and each answer's points researched and cited the way Suggest points does it. The points come from the existing Suggest points run, started by services on the not-yet-live question. |
| D4 | **Approve, then publish together.** Approving puts a question on the backend's waiting list; one publish releases every chosen question as one new quiz version (backend spec). |
| D5 | **Full retake.** A published question makes everyone retake the quiz, the existing lifecycle. Nothing here changes it. |
| D6 | **Only plain points, only additions.** A proposed question is `mutable`, required, never conditional, never escrowed, decaying or recoverable. Nothing here edits or removes an existing question or answer. |

## 2. The kind

`intel_proposals.kind` gains `quiz_question` (migration 0054, §8). It uses the existing statuses:
`pending` → `approved` (an operator) → `applied` (the backend published it and acknowledged), or
`rejected` (dismissed, or withdrawn while approved and unpublished), or `superseded`.

| Field | Shape |
|---|---|
| `target` | `{question_key, subject, research_run_id}`. What the proposal is about: the key the question will be published under, a short subject (≤ 120 characters) used to recognise repeats, and the research run services started for it (§4). |
| `suggested` | `{prompt, multi, options: [{option, tags, new_tag}]}`. The model's question, kept forever. `prompt` ≤ 300 characters; 2 to `INTEL_QUESTION_MAX_OPTIONS` answers of ≤ 200 characters, distinct; `tags` registered slugs; `new_tag` `{slug, label, kind}` or null, a tag the answer would carry that is not registered yet. |
| `decided` | `{prompt, multi, cap, options: [{option, deduction, tags}], suggestion_proposal_id}`. The exact question an operator approved (§5). |
| `applied_ref` | The `scoring_version` of the backend release that published it. |

`against_scoring_version` and `against_release_no` stay NULL, as for every kind but `weight_change`.
`quiz_question` is not an overlap kind (`intel/overlap.py`): an approval supersedes nothing.

## 3. Generation (`propose-v11` → `propose-v12`)

`ProposalOutput` gains `quiz_questions: list[ProposedQuizQuestion]`:

```
ProposedQuizQuestion { question_key, subject, prompt, multi = false,
                       options: [ { option, tags = [], new_tag = null } ], rationale, signal_ids }
```

No bounds in the schema, for §4.5's reason (one bad proposal must not make the answer unparseable);
code bounds them.

### 3.1 The prompt

Generic, by the owner's 2026-10-09 rule: no platform, product or case is named.

- **When:** several pieces of evidence, from at least two publishers, show that how exposed a
  person is depends on something the quiz does not ask about: a platform, service, setting or
  practice. Propose a new question that asks about it. Never one an existing quiz question, a
  `pending_questions` entry or a `dismissed_questions` entry already asks.
- **What:** `question_key` (lowercase letters, digits and underscores, starting with a letter, 2 to
  40 characters, unused), `subject`, `prompt` (one short plain sentence asked of the person as
  "you"), `multi` (true only when a person may pick several), and answers that together cover
  everyone, each with the registry tags it exposes a person to, or `new_tag` when none fits.
- **Topic rules:** ask only about exposure (where and how photos and likeness can be seen,
  collected or misused). Never about ethnicity, religion, beliefs, politics, health, sex life or
  sexual orientation; never about children; never a question whose answer would imply the person
  did something wrong or caused misuse. Name no private individual, no contact details, no links;
  never say anyone is safe; never use the banned words (§3.3). Give no points: they are researched
  separately.
- **Closed:** the payload carries `questions_open`. When it is false the model returns
  `quiz_questions` empty.

New payload fields: `questions_open`, `max_question_options`, `pending_questions` (every pending
and approved `quiz_question`: `{proposal_id, status, question_key, subject, prompt}`) and
`dismissed_questions` (rejected within `INTEL_QUESTION_DISMISSED_DAYS`: `{question_key, subject,
prompt}`).

### 3.2 Which runs may propose a question

`questions_open` is true only for a `source_check`, an `adhoc_url`, or a `discovery` run whose
source is not a research search (`origin <> 'research'`), and only while fewer than
`INTEL_QUESTION_MAX_PENDING` questions are pending or approved. Never for a `weight_suggestion` run
(including a question's own research, §4), a research search's discovery read, or a
`gap_regenerate` run. That is the loop guard: a question's research can never propose a question.
`INTEL_QUESTION_MAX_PENDING = 0` closes question proposals entirely.

### 3.3 Code decides what is written (`intel/generation.py`, `_quiz_question`)

A question failing any rule is dropped whole, counted on the run's outcome, never fixed up:

- `question_key` matches `^[a-z][a-z0-9_]{1,39}$`, is not a live quiz key, and is not the key of a
  pending, approved or recently dismissed question, nor of another in the batch.
- `subject` is masked and normalised, 1 to 120 characters, and its normalised form is not that of a
  pending, approved, recently dismissed or same-batch question.
- `prompt` and every option are masked, normalised, within bounds, and pass
  `question_copy_problems` (`intel/user_copy.py`): the score page's word rules **without** the
  spelled-figure rule, because an answer may legitimately read "18-24" or "More than once a week".
  Options are distinct (case-insensitively), 2 to `INTEL_QUESTION_MAX_OPTIONS` of them.
- Every `tags` entry is well-formed, distinct, registered and not retired. `new_tag` is well-formed,
  unregistered, with a non-empty label.
- `signal_ids` are real and active, and **the evidence bar is recounted in code over the cited
  signals**: at least `INTEL_QUESTION_MIN_SIGNALS` of them created within
  `INTEL_QUESTION_WINDOW_DAYS`, from at least `INTEL_QUESTION_MIN_PUBLISHERS` distinct publisher
  domains. (A gap recounts from a pool of unregistered subjects; a question may be about something
  already tagged, so it counts what it cites.)
- `rationale` as every kind's.

The write (`write_generated`) re-checks the key, the subject and the cap under its lock against
every pending and approved question, so two runs cannot both add one.

## 4. Research (the points)

In the same transaction that inserts a `quiz_question`, services queue a `weight_suggestion` run for
it (`requested_by = 'system:intel'`, audit `intel.question_research_queued`, actor `service`) and
store its id as `target.research_run_id`. The request is the ordinary suggestion request with:

- `question_key`, `prompt`, `options`, `type: "mutable"`, `cap: null`, and `tags` from each
  option's registered tags (an answer with only a `new_tag` has none yet);
- `for_question`: the proposal id, and `signal_ids`: the question's cited signals, which join the
  run's evidence as part of the first retrieval class (beside research searches).

Everything else is the existing run: research rounds, citations, the score-page words, the
recommendation. Its suggestion is an ordinary `weight_suggestion` proposal for the new key. An
operator may also press Suggest points on the key; the decision uses the newest delivered
suggestion for the key either way.

**Reads.** Both proposal reads gain, on a `quiz_question` row,
`research: {run_id, status, suggestion_proposal_id}`: the research run's status, and the newest
delivered suggestion for the key (null when none). `why_not` gains `research_pending`, true while
that run is `queued`, `running` or `waiting`. A research run that failed or found nothing leaves the
question approvable: the operator types every number, and the backend publishes those as
operator-set.

## 5. The decision

The existing `POST /v1/admin/intel/proposals/{id}/decision`. `quiz_question` joins
`APPROVABLE_KINDS` and `REJECTABLE_KINDS`.

**Approve.** `values` is optional and partial:

```
{ prompt?, multi?, cap?, options?: [ { option?, deduction?, tags? } ] }   // options by position
```

It is merged over `suggested`, with each option's `deduction` defaulting to the number the newest
delivered suggestion for the key gave that answer (matched by the proposal's own option text).
`options`, when given, has exactly as many entries as `suggested.options`. The result must parse as
`QuizQuestionDecided`:

- `prompt` 1–300 and options 1–200 characters, distinct, passing `question_copy_problems`;
- every option has a `deduction` from 0 to 10, else **`422 question_incomplete`**;
- `multi: true` needs a `cap` from 0 to 10, else `question_incomplete`; a single choice stores
  `cap: null`;
- every tag registered and not retired, else `unknown_tag` / `tag_retired` naming the slugs;
- the key is still not a live quiz key, else **`409 question_key_taken`**;
- `suggestion_proposal_id` is the suggestion the defaults came from, or null.

The usual predicates run first: `evidence_retracted`, `uncorroborated`, and `research_pending`
(`409 proposal_not_decidable`, "Research for this question is still running.").

**Reject** from `pending` (a dismissal), or from `approved` (a withdrawal before publish). An
approved question the live quiz already shows (published, acknowledgement not yet arrived) is
never withdrawn: `409 proposal_not_pending`, the weight-change rule.

**Applied.** `POST /v1/admin/intel/proposals/applied` moves approved `quiz_question` proposals to
`applied` with the release's `scoring_version`, exactly as it does weight changes. Its answer
(`applied`, `already_applied`, `not_applied`) covers both kinds.

## 6. Reconcile

A **pending** `quiz_question` whose key the live quiz now has is superseded `resolved_by_quiz`. An
approved one is left for the acknowledgement: with its key live it is published, not stale.

## 7. Configuration

Six new required keys on `IntelConfig`, with no defaults in code (owner, 2026-10-10: "do not
hardcode anything"), set in `.env.example` and both environments' task definitions:

| Key | Value | Bound |
|---|---|---|
| `INTEL_QUESTION_MIN_SIGNALS` | 3 | ≥ 1 |
| `INTEL_QUESTION_MIN_PUBLISHERS` | 2 | ≥ 1 |
| `INTEL_QUESTION_WINDOW_DAYS` | 90 | ≥ 1 |
| `INTEL_QUESTION_MAX_OPTIONS` | 8 | ≥ 2 |
| `INTEL_QUESTION_MAX_PENDING` | 3 | ≥ 0 (0 closes question proposals) |
| `INTEL_QUESTION_DISMISSED_DAYS` | 90 | ≥ 0 |

They reach the pipeline as `QuestionLimits` (`intel/limits.py`), and `INTEL_QUESTION_MAX_OPTIONS`
reaches the decision store too.

## 8. Migration 0054

`0054_intel_quiz_questions.up.sql`: the kind CHECK gains `quiz_question`, and the "approved needs
`decided`" CHECK includes it. Both 0039 CHECKs are unnamed and are found by definition (0048's
precedent). The down deletes `quiz_question` proposals (their links and audit rows stay; the
published quiz lives in the backend and is untouched), then restores both CHECKs. No new table, no
grant, no `svc` view.

## 9. What this does not do

- Edit, rename or remove an existing question or answer, or add an answer to an existing question.
- Conditional follow-up questions, or any type but `mutable`.
- Write any person data, or read any: the model sees the public quiz, the tag registry and evidence.
- Ask the subject anything; publishing and the retake are the backend's.

## 10. Tests that must exist

- The output parses with and without `quiz_questions`, and `PROPOSE_PROMPT_VERSION` is `propose-v12`.
- The prompt names no platform, product or case (the existing no-steering test covers the new text).
- Each drop rule in §3.3, each with its count: bad key, live key, pending/approved/dismissed key or
  subject, batch duplicate, banned word in the prompt or an option, too few or too many options,
  duplicate options, unknown or retired tag, malformed `new_tag`, below the evidence bar (signals,
  publishers, window). An option "18-24" passes.
- `questions_open` is false for a weight suggestion, a research search's discovery, a gap
  regeneration, and at the cap; a closed pass drops any question the model returns anyway.
- The write re-checks key, subject and cap under its lock.
- A written question queues exactly one research run with `for_question` and its signals, and the
  question's signals reach that run's evidence.
- `research_pending` while the run is open; approvable once it ends, completed or failed.
- Approve with no values uses the suggestion's numbers; an unbacked answer is `question_incomplete`
  until the operator gives a number; a multi-select without a cap is `question_incomplete`; an
  edited option keeps its position; a wrong option count, a banned word or a bound breach is
  `values_out_of_bounds`; a live key is `question_key_taken`.
- A withdrawal works from `approved`, and is refused once the key is live.
- `mark_applied` moves approved questions with weight changes in one call.
- Reconcile supersedes a pending question whose key went live, and leaves an approved one.
- Migration 0054 up and down; the six config keys are required and bounded; both task definitions
  carry them.

## 11. Deploy

Services first: 0054 with `migrate-services`, then services and services-worker with the six keys.
The backend half follows. On the way down, the backend first, then services, then 0054's down.
Prod proposes nothing until its AI provider is switched on.
