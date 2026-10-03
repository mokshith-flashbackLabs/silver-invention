-- 0046 -- likeness intel: the run log, and the two lookups the runs list filters on
-- (spec docs/superpowers/specs/2026-10-03-intel-run-log-design.md section 3.1).
--
-- One row per step of a run, written by the worker AS THE MODEL STREAM ARRIVES
-- (intel/run_log.py), and polled by the control room while the run is open. Every row
-- is server-authored English plus a closed kind and a small detail object: never a
-- prompt, never the model's answer, never a person. Kept with its run, never pruned, so
-- a past proposal stays explainable. intel/run_log.py caps a run at 300 rows (the last
-- one 'truncated'), a row's text at 2000 characters (4000 for 'thinking') and its detail
-- at 8 KB; the CHECK below is the database's own ceiling on the text.
--
-- `at` is when the row was first written; `updated_at` moves only on a 'thinking' row
-- while its block streams. `seq` starts at 1 per run and continues from max(seq) + 1, so
-- a run reclaimed after a crash appends rather than collides.

CREATE TABLE intel_run_events (
  run_id     UUID NOT NULL REFERENCES intel_runs(run_id),
  seq        INTEGER NOT NULL CONSTRAINT intel_run_events_seq_positive CHECK (seq >= 1),
  kind       TEXT NOT NULL CONSTRAINT intel_run_events_kind_valid
             CHECK (kind IN ('run_started', 'model_call_started', 'thinking', 'search',
                             'search_results', 'writing', 'continuing', 'model_call_finished',
                             'model_call_failed', 'model_call_skipped', 'run_finished',
                             'truncated')),
  text       TEXT NOT NULL CONSTRAINT intel_run_events_text_length
             CHECK (char_length(text) <= 4000),
  detail     JSONB NOT NULL DEFAULT '{}'::jsonb,
  at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, seq)
);

-- GET /v1/admin/intel/runs?kind=... and ?question_key=... (section 3.6), both in the
-- list's keyset order. The question index is partial on the key's presence, and the
-- store's filter states that predicate too, so the planner can prove the index applies.
CREATE INDEX intel_runs_kind_created_idx ON intel_runs (kind, created_at DESC, run_id DESC);
CREATE INDEX intel_runs_question_idx ON intel_runs ((request->>'question_key'), created_at DESC)
  WHERE request ? 'question_key';

-- 0039's rule: enumerated, no DELETE. intel_rw is the role both the worker (which writes
-- the log) and the admin routes (which read it) hold, through app_services (0039), the
-- same way they reach intel_runs.
GRANT SELECT, INSERT, UPDATE ON intel_run_events TO intel_rw;
