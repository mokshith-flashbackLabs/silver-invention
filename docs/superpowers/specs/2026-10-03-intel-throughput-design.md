# Likeness intel — throughput: several runs at once, reads side by side, searches in the background

**Date:** 2026-10-03. **Owner decision, same day:** "do all of them" (the five changes below).
Services only (this repo). No migration, no backend change, no console change required.

## 1. What was wrong

On dev, one Suggest-points "Ask for points" (stage 4, a `weight_suggestion` run) for the platforms
question took over fifteen minutes, and every other run waited behind it. The run log
(`intel_run_events`, spec `2026-10-03-intel-run-log-design.md`) showed why, step by step:

1. **One run at a time.** The intel worker claimed ONE run per tick and executed it before the next
   tick. A long run held the whole queue.
2. **One source at a time.** A stage-4 run read the sources it registered (or reused unread) one
   after another, each extraction waiting for the last.
3. **A saved search is a research call.** Reading a `search_query` source is a Claude call with web
   search (up to five searches), and each took five to eight minutes. Stage 4 read them inline.
4. **Claude thought hard about searches.** The search calls ran at the model's default effort.

And one latent fault the fix depends on:

5. **The lease was never renewed.** A run was claimed with a 900 s lease (`INTEL_LEASE_SECONDS`).
   A run longer than that could be claimed AGAIN by another claimer while still executing; prod
   already runs two services-worker tasks.

## 2. The lease is renewed while a run executes

- While a run executes, its worker renews `lease_expires_at` every third of the lease
  (`worker._Lease`, `store.renew_lease`): `now + INTEL_LEASE_SECONDS`, guarded
  `WHERE run_id = … AND status = 'running' AND attempts = <this claim's attempts>`. A run longer
  than its lease is therefore never claimed a second time while it is still running. A lease still
  lapses when its worker dies, and the reclaim follows as before (`MAX_RUN_ATTEMPTS` unchanged).
- A renewal that finds the run no longer this claim's (another claimer took it over, or
  `expire_exhausted` ended it) logs `intel.run_lease_lost` and stops renewing. The run is allowed to
  finish, but its result is dropped (`intel.run_result_dropped`) and no `run_finished` row is
  written: the newer holder's result is the one that stands.
- `finish_run` is guarded the same way (`attempts`), so a holder that lost its lease between its
  last beat and its finish writes nothing over the newer holder's row. A renewal that errors
  (the database blipped) is logged (`intel.run_lease_renewal_failed`) and tried again next beat.
- The heartbeat stops before the finish write and on a crash. A crashed run is left leased exactly
  as before; its lease simply stops being extended.
- An expired lease nobody has taken yet is still renewable by its holder, so a slow beat never
  loses a run that nobody else wanted.

## 3. Several runs at once

- `worker.serve` keeps up to `INTEL_RUN_CONCURRENCY` runs executing in one process. Each loop pass
  (`fill`) does housekeeping ONCE — reconcile, resolve gaps, expire exhausted runs, pause unmapped
  sources, schedule due sources, schedule renewals — then claims runs while a slot is free.
  `claim_next`'s `FOR UPDATE SKIP LOCKED` keeps any two claimers (two slots, or two tasks) off one
  run.
- A pass runs again as soon as a run ends (its slot is refilled at once) and at least every
  `INTEL_POLL_SECONDS`, so housekeeping keeps running while every slot is busy.
- **Each run is independent.** It executes in its own asyncio task, which runs in a copy of the
  context: `execute()` binds that run's OWN `current_run_log` inside the task, writes its own
  `run_started` and `run_finished`, and keeps its own lease heartbeat. A run that crashes is logged
  (`intel.run_crashed`) and left leased; the loop and the other runs carry on. Anything escaping
  `execute` (a DB error writing the finish) is logged as `intel.run_task_failed` and ends that run's
  task only.
- **Shutdown.** SIGTERM stops claiming. In-flight runs get 20 s (`_DRAIN_SECONDS`, inside ECS's
  default 30 s stop timeout; the task definitions set none) to finish; any still running are
  cancelled and left leased (`intel.runs_left_leased`), to be reclaimed by the next worker once
  their lease lapses — the same outcome a deploy had before, when the container was killed mid-run.
- `tick()` stays: housekeeping, one claim, `execute()`. Scripts and tests use it; the deployed loop
  is `serve`.
- **Accepted edge:** a suggestion's inline read of a page source and that source's own scheduled
  check can now run at the same time in one worker (two prod tasks could already overlap them).
  Both read the page and may both be billed for it; every write they share is an upsert
  (`record_unit`'s snapshot and hash), so neither run fails.

## 4. The run log under concurrency

- Parallel reads inside one run (§5) append to the SAME `RunLog`. Allocating a `seq` and writing
  its row happen under one `asyncio.Lock`, so two appends never take the same number; the table's
  `(run_id, seq)` primary key stays the backstop.
- A `thinking` row is rewritten in place by its own `seq`, held by its own `StreamTranslator` (one
  per model call), unlocked. Two calls streaming at once each finalise their own row.
- Rows from parallel reads interleave in `seq` order. That is expected: nothing in the log's
  contract (PROXY_INTEGRATION.md, the run-log subsection) promised one source's steps would be
  contiguous.

## 5. Several reads at once in one run

- `pipeline.read_each` reads up to `INTEL_SOURCE_READ_CONCURRENCY` items at once, started in the
  items' order. It is used wherever one run reads several sources or pages in a loop:
  - a weight suggestion's sources (stage 4, `_read_new_sources`);
  - a discovery run's result pages (`_discover`);
  - a feed source's items (`_feed`);
  - stage 3's candidates (`_source_validation`): every candidate is still judged, the call cap's
    `run_call_cap` included, and the results stay in the submitted order. A gate refusal is still
    carried forward to every search that STARTS after it; searches already asking keep their own
    answer, so with several in flight the gate may be asked more than once.
- **One run never has more than `INTEL_SOURCE_READ_CONCURRENCY` reads in flight**, however reads
  nest: a `read_each` inside a lane (a feed's items, read as one of a suggestion's sources) runs its
  items one after another in that lane.
- **The call cap stays exact.** A capped model call now RESERVES its slot before it is sent — the
  check and the count happen with no `await` between them — so reads side by side can never send
  more calls than `INTEL_MAX_CALLS_PER_RUN` / `INTEL_MAX_CALLS_PER_SUGGESTION_RUN` between them. A
  call the gate does not send gives its slot back; one sent and failed keeps it, as before. A
  search's `pause_turn` continuations are counted when it returns, as before.
- **A read that fails never cancels another.** Once one read stops the run (`_Stop`: a gate
  refusal, the model or the fetcher down), meets the cap, or raises a bug, nothing new starts and
  the reads in flight finish. The stop is then raised (a bug re-raised) exactly as the sequential
  loop raised it.
- **The counters keep their meaning.** `call_cap_deferred` counts the items the cap left (refused
  by it, or never started under it) and `stopped_call_cap` is still 1; items left unstarted by a
  stop are not counted, as before. A feed or search that left items marks its source in
  `ctx.partly_read`, keyed by source, so reads side by side cannot mistake one another's deferrals.
  `stopped_<reason>` in stage 4 is counted once, for the first stop.
- **Single-source runs are unchanged**, and a concurrency of one reads exactly as the sequential
  loop did.
- **Not parallel, on purpose:** a renewal check's page fetches. It makes no model call, so its pages
  are seconds of fetching, and it shares a page two cited URLs reach (`by_final`), which parallel
  fetches would fetch twice.
- **Accepted edge:** two pages read side by side whose redirects land on the same final URL may
  both be extracted (two calls) before `record_unit`'s `ON CONFLICT` keeps one document. Read one
  after another, the second was caught by `recorded_in_run` first.
- **Accepted edge:** while the `claude_intel` breaker is half-open it admits ONE probe call; a second
  call in flight at that moment is refused (`breaker_open`) and its run ends `refused`, where read
  one after another it would have followed the successful probe.

## 6. Stage 4 hands its saved searches to their own runs

- A `search_query` source a `weight_suggestion` run registered, or reuses unread (`_unread`), is NOT
  read during that run. Before the run reads anything, it is handed to its own `discovery` run
  (`QuestionStore.queue_source_reads`), which a free worker slot claims beside the suggestion (§3).
- It is counted in `sources_deferred` (the counter the S6 poll already serves) and in a new
  `search_sources_deferred`. Page and feed sources are still read inline (§5).
- The suggestion is written from the pages read plus the evidence already collected. Asking again
  once the search has been read includes what it found (`suggestion_candidates` reads every named
  source's signals), and that press does not defer it again (it is no longer `_unread`).
- **The read is queued, not only made due** (a deviation from the brief, §13). `queue_source_reads`
  is the scheduler's own statement for the named sources: it queues the source's run as
  `requested_by 'schedule'` and advances `next_check_at` by one interval, as `schedule_due` does when
  a source falls due — but without `schedule_due`'s `enabled` condition. A draft option's source is
  paused as `unmapped` until the draft publishes, `schedule_due` never queues a paused source, and
  the suggestion used to read such a source inline (spec §4.10, `_unread`); made due only, its search
  would never be read before the publish.
- A source that already has a run open gets no second one: the statement's `NOT EXISTS` skips it,
  and `intel_runs_one_open_per_source` makes a race a no-op (`ON CONFLICT DO NOTHING`).

## 7. Claude's effort on search READS

- `INTEL_SEARCH_READ_EFFORT` (`low` · `medium` · `high` · `xhigh` · `max`) is sent as
  `output_config.effort` on the two web-search calls that READ or CHECK: `discover` (a saved
  search's read) and `search_once` (stage 3's validation search), on the call and on every
  `pause_turn` continuation of it.
- Not on `propose_sources` (stage 1 proposes sources; it keeps the model's default), not on
  extraction (none), not on proposal or suggestion (their explicit `high`).
- The task definitions set `medium`.

## 8. Configuration and the DB pool

| Key | Rule | Dev and prod |
|---|---|---|
| `INTEL_RUN_CONCURRENCY` | int ≥ 1, **required, no default** | `3` |
| `INTEL_SOURCE_READ_CONCURRENCY` | int ≥ 1, **required, no default** | `4` |
| `INTEL_SEARCH_READ_EFFORT` | `low` · `medium` · `high` · `xhigh` · `max`, **required, no default** | `medium` |
| `DB_POOL_MAX_SIZE` (intel-worker container) | ≥ `RUN × (READ + 1) + 1` | `16` (was `2`) |

- Required, like every key a spec gives no default (`IntelConfig`'s rule): each environment states
  how much it runs at once. A task definition without them crash-loops at boot, and
  `tests/test_ecs_task_defs.py` holds both intel-worker containers to the required set.
- **The pool.** Every store call holds a connection for one short statement and none across a model
  call, so the most the worker can want at once is one connection per read in flight and one per
  run's lease heartbeat, for every run, plus the loop's own: `RUN × (READ + 1) + 1`
  (`IntelConfig.pool_size_needed()`), 16 for 3 and 4. The worker logs
  `intel.pool_below_concurrency` at boot when `DB_POOL_MAX_SIZE` is smaller (a smaller pool queues
  rather than fails), and `test_ecs_task_defs` holds both task definitions to the formula.
- Against the shared cluster: two prod services-worker tasks can hold up to 32 intel connections at
  the ceiling, against `max_connections` of about 225 on `db.t4g.small` (the backend's
  `rds_connections` alarm fires at 150). The ceiling is reached only when every slot is
  mid-statement at once, and idle connections close after ten minutes (`max_idle`).

## 9. Budget

Every call still passes the provider gate (kill switch, breaker, `daily_budget_usd`) before it is
sent (`intel/metering.py`, `metered`). Calls in flight at the same time each pass the gate before
any of them is recorded, so **the daily cap can be overshot by at most the calls in flight**:
`INTEL_RUN_CONCURRENCY × INTEL_SOURCE_READ_CONCURRENCY` per worker (12), times the number of
services-worker tasks. No reservation system is built; that bound is the design. (The 2026-09-27
spec's §5 said "with one worker claiming one run at a time, that is one call"; it is amended in
place.)

## 10. What an operator and the console notice

- **The queue is no longer strictly one at a time.** Up to three runs per worker run together, and
  a run may start before an older one finishes. Runs are still claimed oldest first.
- **Logs interleave.** A run reading several sources at once writes their steps into its log as
  they happen, so "Asked…", "Searched…" and "Answered…" rows of different sources interleave.
- **`sources_deferred` on a Suggest-points result (S6) now includes every saved search**, which is
  read in its own `discovery` run (visible on `GET /v1/admin/intel/runs`, `requested_by:
  'schedule'`) rather than inside the suggestion. A result can therefore say "N sources deferred"
  on a first press even with the call cap far away. The outcome also carries
  `search_sources_deferred` (absent when zero, like the other counters).
- **A Suggest-points press is faster**: its pages are read four at a time and its searches no
  longer hold it.
- **The worker logs** `intel.run_lease_lost`, `intel.run_result_dropped`,
  `intel.run_lease_renewal_failed`, `intel.run_task_failed`, `intel.draining`,
  `intel.runs_left_leased` and `intel.pool_below_concurrency`, and `intel.started` names both
  concurrencies.

## 11. Deploy and rollback

No migration and no backend change. Deploy the services image with the new task definitions (the
three keys and `DB_POOL_MAX_SIZE=16` on the intel-worker container). Rolling back is the previous
image with the previous task definitions; a run queued by §6 is an ordinary `discovery` run that the
old code reads as it always has.

## 12. Tests

- **Lease** (`tests/test_intel_worker.py`): a run held past two and a half one-second leases is not
  claimed by a second claimer and finishes on attempt 1; the heartbeat stops at finish and on a
  crash; a takeover is logged, the old holder's result is dropped and the new holder's row is
  untouched; a guarded finish and renewal are refused once another claim holds the run.
- **Several runs** (same file): two runs are mid-read at the same moment and neither is claimed
  twice; each writes its own log; one crashing leaves the other finishing; a freed slot is refilled
  without waiting a poll; SIGTERM drains in-flight runs and claims nothing new; runs still going
  after the drain are left leased.
- **Run log** (`tests/test_intel_run_log.py`): forty concurrent appends take forty unique,
  contiguous `seq`s with no failed write; two concurrent thinking rows are each finalised by their
  own `seq`.
- **Reads side by side** (`tests/test_intel_pipeline.py`, `tests/test_intel_question_runs.py`): a
  discovery's pages, a feed's items, a suggestion's sources and a validation's candidates never
  exceed the bound in flight; the cap is exact with slow, overlapping extractions (a test that
  fails without the reservation); one read's stop lets the reads in flight finish and starts
  nothing new; one source's outage never cancels the reads beside it.
- **Searches in the background** (`tests/test_intel_question_runs.py`): no discovery call inside the
  suggestion, a queued `discovery` run for the search, `sources_deferred` and
  `search_sources_deferred`, and a later press that includes the search's evidence; a draft option's
  paused search is still queued and read; a search with a run already open gets no second.
- **Effort** (`tests/test_intel_model.py`): sent on `discover` (and its continuation) and
  `search_once`; absent on `propose_sources` and extraction; `high` on proposal and suggestion.
- **Config** (`tests/test_intel_config.py`, `tests/test_ecs_task_defs.py`): the three keys are
  required, the concurrencies must be at least 1, the effort is one of five levels; both task
  definitions set 3, 4 and medium with a pool that covers them.
- The existing pipeline and question tests run at a concurrency of one (`make_deps`' default), which
  keeps their pinned fetch order deterministic.

## 13. Deviations from the brief

- **§6: queued, not only made due.** The brief said to set the search source's `next_check_at` to
  now so the scheduler queues its read on the next pass. The scheduler skips a paused source, and a
  draft option's sources are paused as `unmapped`, so their searches would never have been read
  before the publish. The read is queued directly with the scheduler's own statement instead; the
  outcome for an enabled source is the same (its own `discovery` run, claimed by the next pass).
- **§6: a new counter.** `search_sources_deferred` beside `sources_deferred`, so a result can tell
  "read in the background" from "left for the call cap".
- **§5: stage 3 and feeds too.** The brief asked for the same bounded parallelism wherever one run
  reads several sources in a loop; besides stage 4 and discovery that is a feed's items and stage
  3's candidates. Renewal fetches are left sequential (§5).
