# Dynamic Score — live run log, run filters, and Suggest points that remember

**Date:** 2026-10-03. **Owner asks, same day:** "i want the claude like logs to be shown and i want
to do all the quiz question with the option of doing one at a time"; then "i dont see the report
in control room". **Owner decisions:** live steps (not after-the-fact, not a raw transcript);
"Run all" starts every question together plus a Run button per question; build the server and
hand the console a contract.

Both repos. This spec is the brief for the services half (this repo) and the backend half
(`image_backend`, `release/sep-1` then `release/prod-sep15`); the console half is a contract in
the backend repo (`docs/CONTROL-ROOM-CONTRACT-2026-10-03-INTEL-RUN-LOG.md`).

## 1. What was wrong

1. **Nothing to watch.** A Suggest-points question is ONE Claude call of 2–9 minutes. The call is
   not streamed, so nothing is known until it returns, and the run records only final counters
   (`outcome.cost_usd`, `model_calls`). The console draws a moving bar for minutes.
2. **Failures were invisible.** On 2026-10-03 every run failed `PermissionDeniedError:403`; the
   reason ("not authorized to perform sts:GetWebIdentityToken") was only recoverable by a one-off
   probe task, because the worker logs the exception class and nothing else.
3. **Results are lost on reload.** The console's Suggest points keeps run ids in page memory only.
   Leave the page during a 2–9 minute run and the result is unreachable except as a raw record on
   the Runs screen; running again costs money again (~$1.60 for the six live questions).
4. **No way to find a question's runs.** `GET /v1/admin/intel/runs` takes `cursor` and `limit`
   only.

## 2. Decisions

- **The log is stored, then polled** (owner chose "live steps"; approach A). Services write one row
  per step AS THE STREAM ARRIVES; the console polls every 2 s while a run is open. A true push (SSE)
  was rejected: the intel worker is not an HTTP server, so it would still need storage, and a long
  connection through the load balancer is fragile.
- **Every Claude call is streamed**, through the one seam (`ClaudeIntelModel._send`), so every run
  kind that calls Claude gets a log: Suggest points stages 1/3/4, source checks, discovery, gap
  regeneration. Same answer, same cost, same error mapping as today.
- **Claude's reasoning summary is shown.** `thinking: {"type": "adaptive", "display":
  "summarized"}` returns a readable summary (the default on Claude 5 models is `omitted`, empty
  text). Billing is unchanged by `display`. Intel prompts carry no personal data.
- **Not built:** the raw transcript; log pruning (rows are capped and small, and kept with their
  run, so a past proposal stays explainable).
- **Logging never fails a run.** A log write that errors is a warning; the run continues.

## 3. Services (this repo)

### 3.1 Migration `0046_intel_run_events`

```sql
CREATE TABLE intel_run_events (
  run_id     UUID NOT NULL REFERENCES intel_runs(run_id),
  seq        INTEGER NOT NULL CHECK (seq >= 1),
  kind       TEXT NOT NULL CHECK (kind IN ('run_started', 'model_call_started', 'thinking',
                'search', 'search_results', 'writing', 'continuing', 'model_call_finished',
                'model_call_failed', 'model_call_skipped', 'run_finished', 'truncated')),
  text       TEXT NOT NULL CHECK (char_length(text) <= 4000),
  detail     JSONB NOT NULL DEFAULT '{}'::jsonb,
  at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, seq)
);
CREATE INDEX intel_runs_kind_created_idx ON intel_runs (kind, created_at DESC, run_id DESC);
CREATE INDEX intel_runs_question_idx ON intel_runs ((request->>'question_key'), created_at DESC)
  WHERE request ? 'question_key';
```

Grants as 0039's rule: `GRANT SELECT, INSERT, UPDATE ON intel_run_events TO intel_rw`, plus
whatever role the admin routes read through (follow how `intel_runs` is granted). No DELETE. The
down migration drops the table and both indexes. Schema lint (no `bytea`, name gate) must pass.

### 3.2 The event vocabulary (closed; the console renders on `kind`, never on `text`)

| kind | text (server-authored, English) | detail |
|---|---|---|
| `run_started` | "Started: find sources for 'platforms'" (per run kind; attempt N > 1 adds "(attempt N)") | `{run_kind, attempt}` |
| `model_call_started` | "Asked claude-sonnet-5 (up to 5 web searches)" / "Asked claude-opus-5-5" | `{model, max_searches}` (`max_searches` null without the search tool) |
| `thinking` | Claude's reasoning summary for one thinking block, growing as it streams | `{chars}` |
| `search` | "Searched: bumble data breach 2026" | `{query}` |
| `search_results` | "8 results: bumble.com, techcrunch.com, theverge.com" (first 3 domains) or "Search failed: max_uses_exceeded" | `{count, results: [{title, url}] (max 10), error_code}` |
| `writing` | "Writing the answer" (once per model call, at the first text block) | `{}` |
| `continuing` | "Continuing — Claude paused after its searches" (a `pause_turn` resume) | `{pause_turns}` |
| `model_call_finished` | "Answered in 3m 12s · $0.23 · 2 searches" | `{model, stop_reason, input_tokens, output_tokens, web_search_requests, cost_usd, latency_ms}` |
| `model_call_failed` | "Claude call failed: permission denied (403) — <API message, ≤ 500 chars>" | `{status, error_type, http_status, message}` |
| `model_call_skipped` | "Not sent: the provider is switched off" / "…breaker is open" / "…today's budget is spent" / "…no daily budget is set" | `{reason}` |
| `run_finished` | "Finished in 3m 40s · $0.23" / "Failed: <error_code>" / "Refused: <error_code>" | `{status, error_code, cost_usd, duration_ms}` |
| `truncated` | "Log limit reached; later steps were not recorded" | `{}` |

`at` is when a row was first written; `updated_at` moves only on a `thinking` row while its block
streams. `text` is for display and may change wording; `kind` and `detail` keys are the contract.

### 3.3 The recorder (`intel/run_log.py`)

- `RunLog(run_id, store)`: `append(kind, text, detail) -> seq | None` and `update(seq, text,
  detail)`. One short autocommit write per call (never inside a run's transaction), so a poll sees
  a step the moment it is written.
- A `ContextVar[RunLog | None]` (`current_run_log`). The worker sets it for the duration of
  `run(claimed, deps)`; `metered()` and `ClaudeIntelModel` read it. Absent (tests, scripts), every
  log call is a no-op.
- `seq` continues from `max(seq) + 1` for the run, so a run reclaimed after a crash appends rather
  than collides.
- **Caps:** 300 rows per run (the 300th is `truncated`, then nothing more); `text` ≤ 2000 chars
  (4000 for `thinking`); `detail` ≤ 8 KB serialised (larger detail drops `results` first, then is
  replaced by `{"oversize": true}`).
- **Thinking is throttled:** one row per thinking block, written at block start, `update`d at most
  every 2 s while deltas arrive, and finalised at block stop.
- **Never raises.** A write error logs `intel.run_log_write_failed` (run id and kind only) and the
  run continues.

### 3.4 Streaming the model call (`intel/model.py`)

- `_send` uses `self._client.messages.stream(...)` (async context manager on `AsyncAnthropicAWS`)
  with the same arguments it passes to `create` today, plus `thinking={"type": "adaptive",
  "display": "summarized"}`. It iterates the events, feeding the translator, then
  `await stream.get_final_message()` gives the same `Message` it reads today: same `ModelCall`,
  same usage, same pricing, same `outcome`.
- The retry loop wraps the whole stream; the same exception types map to the same
  `ModelUnavailable` statuses, now raised from entering or iterating the stream.
- `ModelUnavailable` gains `message` (the API error's message, ≤ 500 chars) for the
  `model_call_failed` row ONLY. `provider_calls.error_detail` is unchanged (`PermissionDeniedError:403`).
- Translation: a `server_tool_use` block's query (complete at its `content_block_stop`) →
  `search`; a `web_search_tool_result` block → `search_results` (a list = results; an object = an
  error with its `error_code`); thinking deltas → the throttled `thinking` row; the first text block
  → `writing`. `_search`'s `pause_turn` resume → `continuing`. Verify event and block shapes against
  the installed `anthropic` (1.8.0) source, not memory.
- The test doubles that fake `messages.create` must fake `messages.stream` instead (an async
  context manager yielding scripted events and returning a final `Message`).

### 3.5 `metered()`

`budget_unset` and `Skip` → `model_call_skipped` with the reason; `ModelUnavailable` →
`model_call_failed`; a returned call → `model_call_finished`. `model_call_started` is written by
`_send`, which knows the model and the search limit.

### 3.6 Admin routes

- `GET /v1/admin/intel/runs` gains three optional filters, combined with AND: `kind` (repeatable,
  one of the run kinds), `status` (repeatable, one of the run statuses), `question_key` (exact
  match on `request->>'question_key'`; `^[a-z][a-z0-9_]{1,39}$`). Keyset paging and `spend` are
  unchanged. An unknown value is a 422.
- `GET /v1/admin/intel/runs/{run_id}/events` → `{ "run_id", "kind", "status", "events": [{seq,
  kind, text, detail, at, updated_at}], "truncated": bool }`, `seq` ascending, the whole log (≤ 300
  rows). An unknown run is `404 intel_run_not_found`; a run with no rows yet is `200` with
  `events: []`.

### 3.7 Tests (services)

- Translation from a scripted stream: a search with its query, results with titles/urls, a search
  error object, thinking deltas into ONE growing row, the first text block → `writing`, a
  `pause_turn` resume → `continuing`; the final `ModelCall` equals today's for the same message.
- A 403 raised from the stream → `ModelUnavailable("error", "PermissionDeniedError:403")` exactly
  as today, and a `model_call_failed` row carrying the API message; `provider_calls.error_detail`
  unchanged.
- `metered()` writes the skipped / failed / finished rows; with no recorder bound nothing is
  written and nothing fails.
- Caps: the 300th row is `truncated` and nothing follows; oversize detail is trimmed; a log write
  that raises leaves the run's result unchanged.
- The worker writes `run_started` and `run_finished` around a run, and seq continues after a
  reclaim.
- Route: filters (each alone, combined, repeated, unknown → 422, a question key that matches
  nothing → empty page); the events route (order, 404, empty).
- Migration up/down; schema lint; the app role can insert and update (not only the superuser).

### 3.8 Docs (services)

`PROXY_INTEGRATION.md` (the admin surface), `SCHEMA.md` (the table), `CLAUDE.md` §2's Pillow/SDK
notes if affected, and this spec.

## 4. Backend (`image_backend`)

- `GET /v1/admin/intel/runs` (business): the strict query schema gains `kind` (repeatable enum),
  `status` (repeatable enum) and `question_key` (the `questionParams` regex), forwarded as repeated
  query parameters. A repeated parameter arrives as a string or an array; accept both.
- **New:** `GET /v1/admin/intel/runs/:runId/events`, **business** tier (the Runs screen is business;
  the log carries no person data), relayed verbatim; services' `intel_run_not_found` →
  `404 INTEL_RUN_NOT_FOUND`. Activity sentence: "read a Dynamic Score run's step log". It joins
  `OPERATOR_ROUTES`, the fake, the services client; the closed-world intel route count goes
  **29 → 30**.
- Docs: `docs/CLAUDE.md` §9 bullet; the consolidated `docs/CONTROL-ROOM-CONTRACT-DYNAMIC-SCORE.md`
  edited IN PLACE (route table, §4.4, §4.11, §5); the new console contract for this change.
- Ported to `release/prod-sep15` by hunks-only `git apply --3way` (no migration on this side).

## 5. Console (contract only — no UI is built in either repo)

1. **Run modes.** Suggest points keeps "Run all questions" (queues every question at once; the
   worker takes them one after another, so the rest read "Waiting to run") and gains a **Run**
   button per question.
2. **Live log.** Under each question that has a run, a log panel polls
   `GET /v1/admin/intel/runs/:runId/events` every 2 s while the run is `queued`/`running`, and once
   more when it ends. It renders by `kind`; a `thinking` row is shown collapsible.
3. **Reopen past results.** On opening Suggest points, per question:
   `GET /v1/admin/intel/runs?kind=source_proposal&question_key=<key>&limit=1`. A `queued`/`running`
   run is resumed (poll it, show its log); a `completed` one whose request `options` equal the
   draft's options (same texts, any order) opens at stage 2 with its proposals; otherwise the
   question starts fresh. Same for `kind=weight_suggestion` (stage 4). Validation runs are not
   reopened (a check is honoured 24 h and costs no model call).
4. **Readable results.** The Runs screen renders a `source_proposal` run's `outcome.options` as a
   list per answer (source, kind, link, reason; "already registered" for `existing`), and every
   run row expands to its log.

## 6. Deploy order

Services first (migration 0046, then services + services-worker); then backend api. Dev now;
`release/prod-sep15` is pushed, prod deploys when the owner asks. Rolling back: backend first, then
services; 0046's down drops the log.

## 7. As built — services (2026-10-03)

§3 is built as written (`PROXY_INTEGRATION.md` "the run log and run filters" is the contract the
backend relays). Where the build had to choose, it chose this:

- **0046's three CHECKs are named** (`intel_run_events_seq_positive`, `_kind_valid`, `_text_length`)
  so a later migration finds them by name, not by definition as 0042 and 0043 had to. Same rules.
- **`model_call_started` is the first leg only.** A `pause_turn` resume is announced by its
  `continuing` row, not a second "Asked". **`writing` is once per API call**, so a resumed leg may
  write its own.
- **`thinking` is written at block start with empty text** (the summary arrives only as deltas),
  rewritten at most every 2 s, finalised at the block's stop with the full summary.
- **Two failure shapes only a stream has.** An `error` event inside an open stream arrives as an
  `APIStatusError` carrying the stream's own 200 (e.g. `overloaded_error`): it is retried like a
  5xx, where the literal "same mapping" would have made it a final `error`. A transport error
  mid-body is raised by the SDK unwrapped (`httpx2`): a timeout maps to `timeout`, anything else to
  a retried connection failure, as the SDK maps the same errors before a body. Without this a
  dropped stream would crash the run instead of metering it.
- **`model_call_finished.latency_ms` is the wall time of the whole metered call**, `pause_turn`
  continuations included; `ModelCall.latency_ms` (the last leg's) and `provider_calls` are
  unchanged. The text adds a note when the answer was a refusal, cut off, or unreadable.
- **`search_results`:** an empty list reads "No results"; an oversize detail drops the `results`
  KEY (absent, not `[]`), then becomes `{"oversize": true}`.
- **`run_finished` is written BEFORE `finish_run`**, and the events route reads the run's status
  before its rows, so a response whose status is terminal always carries the whole log. A crashed
  run gets no `run_finished` (it is not finished; its reclaim appends "(attempt N)"), nor does a
  run whose log already hit the cap.
- **The 404 is `intel_run_not_found`** with the message "No run with this id." (any run kind; the
  question polls keep their own "No run of this kind with this id.").
