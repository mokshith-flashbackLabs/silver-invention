# Every answer is researched — design

Date: 2026-10-10. Owner request, reading the Dynamic Score coverage screen on dev ("Cited 15,
Uncited 10", every uncited answer labelled "Before Dynamic Score"): "no this should not happen
they should come from research". Then, asked how far the research should go: up to four extra
rounds, "Prefer not to say might not get the research but some obvious things should get", and
"do not hardcode anything".

Two coordinated changes, one per repo, each on its own side of the contract:

- **Services (this spec):** Suggest points makes sure every answer is searched for, and searches
  again for any answer it could not back.
- **Backend** (`image_backend/docs/superpowers/specs/2026-10-10-published-citations-confirmed-and-researched-design.md`):
  a publish records a citation for a number the evidence confirms as it already is, and records
  "researched, no evidence found" for an answer research looked at and could not back.

## 1. What dev showed

Dev's live quiz (`likeness-health-v17`, published 2026-10-09 05:25 UTC) has 25 scored answers: 15
cited, 10 uncited. For the 10, the newest suggestions (2026-10-09 05:20 runs) said:

| Answer | Points | Suggested | Publishers | Why it is uncited |
|---|---|---|---|---|
| Male | 1 | 1 | 3 | same number: a publish cites changes only (backend half) |
| TikTok | 3 | 3 | 3 | same |
| YouTube | 2 | 2 | 5 | same |
| Daily or multiple times a day | 8 | 8 | 3 | same |
| Repeated pattern | 10 | 10 | 2 | same |
| I don't have a way to monitor my likeness | 8 | 8 | 4 | same |
| 25-34 | 4 | 4 | 1 | same, and not corroborated |
| 35-54 | 3 | none | 0 | no evidence |
| Prefer not to say | 2 | none | 0 | no evidence |
| No known incidents | 0 | none | 0 | no evidence |

The research gaps are this repo's:

1. **Stage 1 never checks that every answer was searched for.** The age question's sources were
   nine pages, one document each: children and teenagers (three), people in their twenties (one),
   older adults (two), executives, regulators. None is about adults aged 35–54, so stage 4 had
   nothing to read for that answer, and 25-34 had one publisher. Nothing in the pipeline noticed:
   `_source_proposal` fails only when *no* answer has a source (`no_sources_found`).
2. **Stage 4 gives up after one pass.** An answer with no evidence, or with fewer than two
   independent publishers, is written as `deduction: null` / `why_not: no_evidence` or
   `uncorroborated`, and nothing searches again.
3. Two answers are hard to research at all: nothing found speaks to people who withhold their
   gender, and "No known incidents" is the reference point the others are measured from. The owner
   accepts that such an answer may end with nothing found, but only after the research really ran.

## 2. The rules

**Every answer gets a source aimed at it, and an answer that ends a pass unbacked is searched for
again, up to a configured number of rounds, before it is given up.** An answer is **backed** when
its suggested number cites evidence that `corroboration.uncorroborated` does not reject (at least
`CORROBORATION_MIN_PUBLISHERS` independent publishers, or a listed source). That predicate is
unchanged and is the only definition of backed.

Nothing in this spec names a question, an answer or an example. Every rule below works over
whatever answers the question has, and every limit is configuration (§5).

### 2.1 Stage 1: every answer gets a source

1. After the source-proposal call is cleaned (`clean_candidates`, the known-hit drop and the
   per-answer caps), an answer is **covered** when it has at least one proposed candidate or one
   existing registry source.
2. If any answer is uncovered, the run asks again, for **the uncovered answers only**, up to
   `INTEL_SOURCE_PROPOSAL_COVERAGE_ASKS` more times. Each ask is the same metered
   `propose_sources` call, with the same prompt rules, the question, and only those answers. The
   prompt adds one general sentence: the earlier search found nothing aimed at these answers, so
   search for each one specifically and, when nothing speaks to it directly, for the closest
   evidence. What comes back goes through the same cleaning and caps, and the search budget
   (`INTEL_MAX_SOURCE_PROPOSAL_SEARCHES`) applies per call.
3. An answer still uncovered after the asks is reported on the run: `options[].uncovered: true`,
   counted as `source_proposal_uncovered`. The run still completes: stage 4's rounds (§2.2) are
   the second line.
4. A gate refusal or an unavailable model during an ask ends the asking. The run keeps what it
   has and says why, exactly as the first call does today.

### 2.2 Stage 4: research rounds for unbacked answers

The weight-suggestion run (`_weight_suggestion`) keeps its read → wait → suggest → write order.
A suggestion pass that leaves answers unbacked now leads to another round instead of a write.

1. **A pass.** Retrieval and ONE suggestion call, as today (`_suggest`). Before writing, code
   judges each answer: backed, or **unbacked** (no deduction, no cited evidence, evidence
   retracted, or not corroborated), with the same predicate `suggestion_options` uses.
2. **Another round, or the write.** If any answer is unbacked and the run has used fewer than
   `INTEL_SUGGESTION_RESEARCH_ROUNDS` rounds, the run starts a round (3–5). Otherwise it writes
   the suggestion (§2.3).
3. **The round's searches come from the same call.** The suggestion prompt (`suggest-v2`) asks the
   model, for every answer it cannot back, for up to `INTEL_RESEARCH_SEARCHES_PER_ANSWER` web
   searches that would find evidence about that answer:
   - in round 1, searches for direct evidence about that answer;
   - from round 2 on, searches for the closest evidence, each with one line on how it would
     apply. The model is told which searches it already made for the answer, so it does not
     repeat them.

   The output schema gains `research_searches: list[str]` per option (`SuggestedOptionWeight`). A
   search that `contains_pii` flags is dropped and counted (`research_search_names_a_person`);
   so are duplicates of a search the run already made. If the model gives no searches for any
   unbacked answer, the run stops researching and writes.
4. **Each search is a saved search, read once.** Code registers it as a `search_query` source,
   `origin = 'research'`, tagged with the answer's tags (`option_tags`, possibly none),
   `proposed_for` the question, with `next_check_at = 'infinity'`: it is read now, by the read
   queued for it, and never scheduled again. Its id joins the run's `research_source_ids`, which
   retrieval reads alongside `request.source_ids` from then on.
5. **The run waits for those reads**, using the existing wait machinery unchanged
   (`queue_source_reads`, `RunWait`, `awaiting_source_ids`, `SUGGESTION_WAIT_MAX_SECONDS`,
   `SUGGESTION_WAIT_RETRY_SECONDS`). On resuming, `research_round` is incremented and the next pass
   runs (1). An awaited read that timed out counts as read-and-empty for that round.
6. **One suggestion call per round,** outside the reading cap as today. The rounds' reads are
   their own discovery runs, under their own caps and the provider gate.
7. **The pass that starts a round is kept on the run** (`intel_runs.research_pass`, the validated
   options and the model id), because a resumed run starts with a fresh context.
8. **A gate refusal or an unusable answer ends the research, never the suggestion.** If a round's
   suggestion call is refused by the gate, the model is unavailable, or its answer fails
   validation, the run writes the kept pass (7), with every unbacked answer `exhausted: true`.
   Only a run that never completed a pass behaves as today (refused or failed, nothing written).

### 2.3 What a written suggestion records

Every option entry in `target.options`, and so every per-answer read (`suggestion_options`,
`GET /v1/admin/intel/weight-suggestions/{run_id}`, `GET /v1/admin/intel/proposals/{id}`), gains:

```json
"research": {
  "rounds": 2,
  "searches": [{"round": 1, "query": "…"}, {"round": 2, "query": "…"}],
  "exhausted": true
}
```

- `rounds`: research rounds in which this answer was searched for (0 when its first pass backed
  it).
- `searches`: what was searched for it, in order. Never a person's name (filtered above).
- `exhausted`: **true when the answer is still unbacked after its research ended**: the configured
  rounds ran out, the model gave no further searches, or the gate stopped the run. False for a
  backed answer. The backend reads this flag and never re-derives it.

The poll (`render_suggestion`) gains `research_round` (the round in progress, 0 before the first)
while the run is queued, waiting or running.

The run log gains one event kind, `research_round`: "Searching again for N answers (round R of
MAX)". The run's outcome counts `research_rounds`, `research_searches`,
`research_search_names_a_person`, `research_exhausted_answers`.

### 2.4 The prompts

- `sources-v3`: sources-v2 plus the coverage sentence in §2.1 (sent only on a coverage ask).
- `suggest-v2`: suggest-v1 plus `research_searches` (§2.2.3) and one general rule: "If no evidence
  speaks to an answer directly, you may back it with the closest evidence, saying in its rationale
  how that evidence applies. The corroboration rule still applies."

Neither prompt names a question, an answer, a platform or an example, so nothing steers what the
research finds (owner rule, 2026-10-09). A test asserts that no answer text from the test fixtures
appears in either prompt's fixed text.

## 3. Schema (migration 0053)

- `intel_sources.origin` CHECK gains `'research'`, plus a shape CHECK:
  `origin <> 'research' OR kind = 'search_query'`.
- `intel_runs` gains `research_round SMALLINT NOT NULL DEFAULT 0`, `research_source_ids UUID[]`
  and `research_pass JSONB` (§2.2.7), with a CHECK that only a `weight_suggestion` run carries
  any of them.
- `intel_run_events.kind` CHECK gains `'research_round'`.
- The down drops the columns and CHECKs, and refuses while any `research` source exists.

## 4. What does not change

- The corroboration predicate, the per-answer caps of stage 1, the suggestion's validation in code
  (`validate_suggestion`), and the backend's contract views.
- A suggestion is still written once per run, and a newer press still wins (the waits spec §2.7).
- Stage 3 (validation) and the operator's choice of sources in stage 4: the research rounds add
  sources of their own and never remove one the operator chose.

## 5. Configuration

Three new required keys on the intel worker, with no code default, as `INTEL_MAX_SOURCE_PROPOSAL_SEARCHES`
already is (`intel/config.py`). They go in `.env.example`, `infra/ecs/imageshield-dev-services-worker.json`
and `infra/ecs/prod/services-worker.json`:

| Key | Starting value | Meaning | Validation |
|---|---|---|---|
| `INTEL_SOURCE_PROPOSAL_COVERAGE_ASKS` | 1 | extra stage-1 asks for uncovered answers | ≥ 0 (0 turns it off) |
| `INTEL_SUGGESTION_RESEARCH_ROUNDS` | 4 | research rounds per suggestion run (owner, 2026-10-10) | ≥ 0 (0 turns it off) |
| `INTEL_RESEARCH_SEARCHES_PER_ANSWER` | 2 | searches per unbacked answer per round | ≥ 1 |

## 6. Cost and time

A round costs one suggestion call (about $0.4–0.7 measured; budgeted at `claude_intel`'s
`cost_per_call_usd`) plus one discovery read per search (several minutes each, read side by side
by free worker slots). With two unbacked answers and the starting values, a question can add up to
4 × (1 + 2 × 2) = 20 metered calls and several waits of a few minutes. Every call passes the
provider gate (daily budget, breaker, kill switch), and prod's `claude_intel` is still disabled,
so none of this spends anything on prod until the owner enables it.

## 7. Tests

- Stage 1: an uncovered answer triggers exactly one coverage ask (with the key at 1), naming only
  the uncovered answers; a still-uncovered answer is `uncovered: true` and the run completes; with
  the key at 0 no ask is made; a gate refusal during an ask keeps the first call's sources.
- Stage 4:
  - An unbacked answer starts a round: research sources are registered (`origin 'research'`,
    `next_check_at` infinity, the answer's tags), queued and awaited, and the next pass reads
    their evidence through `research_source_ids`.
  - A backed answer is never searched for again, and is `research.rounds = 0`.
  - The round count stops at `INTEL_SUGGESTION_RESEARCH_ROUNDS`; an answer still unbacked is
    `exhausted: true`, and the suggestion is written once.
  - A model that gives no searches ends the research and writes, `exhausted: true`.
  - A PII-shaped search is dropped and counted, never registered.
  - A gate refusal, an unavailable model or an invalid answer in round 2 writes round 1's kept
    pass, with the unbacked answers exhausted; a run with no completed pass still writes nothing.
  - A reclaimed run is never billed twice for a round (the existing `suggestion_written` guard,
    plus `research_round` read from the row).
- Prompts: no fixture answer text appears in the fixed text of `sources-v3` or `suggest-v2`.
- Migration 0053 up/down, and the down's refusal.
- Config: each key missing refuses boot; a negative value refuses boot.

## 8. Rollout

1. Services first (this change, migration 0053, the three keys), then the backend half. The
   backend reads `research.exhausted` only when present, so either order is safe; services first
   gives the backend something to read.
2. On dev: press Suggest points again for every question. Success is:
   - 35-54 and 25-34 come back backed;
   - an answer still unbacked says what was searched, over how many rounds;
   - after a publish (backend half), the coverage screen shows no "Before Dynamic Score" for any
     answer Suggest points researched.
