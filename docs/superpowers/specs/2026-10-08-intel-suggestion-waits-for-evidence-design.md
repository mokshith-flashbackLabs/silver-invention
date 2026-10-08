# A points suggestion waits for its evidence — design

Date: 2026-10-08. Owner request: "some of the sources … say no evidence is available — that should
not happen", then "remove everything and run the AI analysis again". Services only; no backend
change (run statuses are unchanged, and the backend relays run events and outcomes verbatim).

## 1. What dev showed

Seven `weight_suggestion` proposals on dev carry no evidence at all, and three more have options
with none. Every threat, protection, points change and coverage gap has its evidence. All of them
come from 2026-10-03 and three causes:

1. **Answered from an empty pool.** Six runs (gender ×2, age, prior_misuse, q_il8uju, platforms,
   posting_volume) were pressed with no sources (`source_ids: []`) and nothing in the retrieval
   window. The model was still asked, said "no evidence" for every option, and the all-null answer
   was delivered (gender's is still the delivered one).
2. **Answered before its reads finished.** The platforms run read 3 of its 15 sources: the fetcher
   became unreachable mid-run (`stopped_fetcher_unreachable`), the other 12 were left for a later
   check, and the suggestion was written from what had been read, so TikTok and YouTube came back
   with no evidence.
3. **The same, by design, since 2026-10-03 23:36** (`f24e254`): a saved search is no longer read
   inside the suggestion. It is queued as its own discovery run and the suggestion is written
   straight away from "the evidence already collected"; asking again later includes what the
   search found. On a fresh database that is most of the evidence, so almost every first answer
   would read "No evidence".

## 2. The rule

**A suggestion never answers from evidence it has not read yet, and never answers from none.**

1. Stage 4's run reads what it can inline, exactly as now (`INTEL_SOURCE_READ_CONCURRENCY`, the
   per-claim `INTEL_MAX_CALLS_PER_SUGGESTION_RUN`). Everything it could not read — its saved
   searches, sources past the call cap, sources an outage stopped, a feed it read only in part —
   is queued as that source's own read (`queue_source_reads`; a source with a read already open
   keeps it). These are the run's **awaited sources**.
2. If any source is awaited, the run **waits**: it leaves the queue instead of answering. Its row
   goes back to `queued` with `awaiting_source_ids` and `wait_deadline = now +
   SUGGESTION_WAIT_MAX_SECONDS` (45 minutes), and the claim it held is given back (`attempts`
   unchanged, so waiting never uses up an attempt). It holds no worker slot while it waits.
3. **The queue decides when it resumes**, not a timer: a waiting run is claimable once none of its
   awaited sources has a read queued or running, or once its deadline passes, and not before its
   `not_before`.
4. On resuming it reads nothing itself. An awaited source whose read ended without the page being
   read (`deferred_<reason>`: the fetcher or the model was down, the gate refused) is queued
   again and the run waits again, with `not_before = now + SUGGESTION_WAIT_RETRY_SECONDS` (2
   minutes) so an outage is not retried in a tight loop. Past the deadline it stops waiting and
   answers from what it holds, counting `suggestion_wait_timed_out` and `suggestion_sources_unread`.
5. **An empty pool is never answered.** If retrieval finds no evidence at all, no model call is
   made and nothing is written: the run fails `no_evidence_found` (`suggestion_no_evidence` in its
   outcome). The question's previous suggestion, if any, stays delivered. Options that still have
   no evidence beside options that do are answered as before (`deduction: null`, `why_not:
   no_evidence`): that is the model's honest verdict on evidence it did read.
6. Generation (threat/protection proposals over what the run itself read) runs once, after the
   suggestion, never on a waiting pass.
7. **A newer press always wins.** Runs can now finish out of order, so a suggestion supersedes only
   delivered suggestions from runs created before its own; if a newer run's suggestion is already
   delivered, this one is written already superseded.

## 3. Schema (migration 0050)

- `intel_runs.awaiting_source_ids UUID[]`, `intel_runs.wait_deadline TIMESTAMPTZ`,
  `intel_runs.not_before TIMESTAMPTZ`, all null except on a waiting suggestion. A CHECK keeps the
  first two together.
- `intel_run_events` admits one more kind, `waiting` ("Waiting for 3 sources to be read before
  suggesting"). The console renders on `kind` and `detail` (`awaiting`, `deadline`), never `text`.

The down migration drops the columns and deletes `waiting` rows (the rest of each log stays).

## 4. What the operator sees

- The run's status reads `queued` again while it waits; `GET /runs` carries `awaiting_source_ids`
  and `wait_deadline`. Its log reads: Started → (reads) → **Waiting for N sources** → Resumed →
  the suggestion call → Finished.
- `no_evidence_found` is a new error code on a failed run: "Found no evidence for any option".
- Console copy for both is the console owner's (a prompt is handed over; no console change here).

## 5. Not in this change

- A per-option web search for options still without evidence after the wait (offered, not chosen).
- The scheduled checks (`source_check`, `discovery`) are untouched.
