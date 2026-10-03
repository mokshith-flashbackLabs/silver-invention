"""Safety limits for likeness intel (spec §4.5). CODE CONSTANTS, not env: moving one
costs a code change, a review and a `git blame`, the argument REPORT_FACE_MATCH_MIN
makes on the backend. Proposal bounds are step 2's; threat-event and regeneration
bounds are step 3's; protection and renewal bounds are step 4's."""

from __future__ import annotations

MIN_QUOTE_CHARS = 20
MAX_QUOTE_CHARS = 600
MAX_RUN_ATTEMPTS = 3
MIN_POLICY_TEXT_CHARS = 500
FEED_MAX_ITEMS_PER_RUN = 10
FEED_MAX_ITEM_AGE_DAYS = 30
MAX_SOURCE_CONSECUTIVE_FAILURES = 10
MAX_PROMPT_TAGS = 300
DISCOVERY_DEDUP_DAYS = 30
INTEL_STALE_GRACE_HOURS = 6

# ── sources per question and weight suggestions (step 5, spec §4.6, §4.10) ───
MIN_SOURCE_TEXT_CHARS = 200
MAX_PROPOSED_SOURCES_PER_OPTION = 5
# A validation result is honoured this long (§4.10 stage 4).
VALIDATION_TTL_HOURS = 24
# How far back stage 4 looks for the question's stage-1 runs, to record a chosen source they
# proposed as origin 'suggested'. Provenance only: nothing is matched or scored by origin.
PROPOSAL_ORIGIN_DAYS = 7
# How many of a validation search's result pages are fetched before the search counts as empty.
MAX_SEARCH_RESULTS_CHECKED = 5
DEFAULT_SOURCE_CHECK_EVERY_HOURS = 168
SUGGESTION_CONTEXT_DAYS = 365
SUGGESTION_CONTEXT_MAX_SIGNALS = 80
# How many recent candidate signals the subject and category classes read. A read bound, never
# sent to the model.
SUGGESTION_POOL_MAX = 2000
# Request bounds. The backend enforces 50 options and 250 candidates or sources itself; these accept
# at least that.
MAX_QUESTION_OPTIONS = 50
MAX_VALIDATION_CANDIDATES = 250
MAX_SUGGESTION_SOURCES = 250
MAX_QUERY_TEXT_CHARS = 300
# A source's optional terms note, after trimming (0047; amended 2026-10-03, the note was required
# and at least 10 characters until then). Migration 0047's CHECK is the database's own ceiling.
MAX_TERMS_NOTE_CHARS = 500
MAX_CANDIDATE_REASON_CHARS = 300
MAX_TAG_LABEL_CHARS = 80

# ── proposals (step 2, spec §4.5) ────────────────────────────────────────────
WEIGHT_DELTA_MIN = -2
WEIGHT_DELTA_MAX = 2
DEDUCTION_MIN = 0
DEDUCTION_MAX = 10
CORROBORATION_MIN_PUBLISHERS = 2
COVERAGE_GAP_MIN_SIGNALS = 3
COVERAGE_GAP_MIN_PUBLISHERS = 2
COVERAGE_GAP_WINDOW_DAYS = 90
# How many recent candidate signals a coverage-gap check reads from the database. A read
# bound, never sent to the model.
COVERAGE_GAP_POOL_MAX = 2000
PROPOSAL_CONTEXT_DAYS = 90
PROPOSAL_CONTEXT_MAX_SIGNALS = 60
# A run's own new signals the generation prompt carries, newest first; the rest are counted on
# the run (proposal_new_signals_over_cap) and are never attach evidence for it (final review
# M7, 2026-10-01). A weight suggestion may read 60 calls' worth of signals. Bounded, the input
# stays inside the 60k tokens behind 0041's worst-case estimate: about 180 tokens a signal for
# 40 new and 60 related (~18k), 40 each of pending proposals, live threats and live credits
# (~14k), a registry of up to 300 tags (~12k), the quiz and the instructions (~8k).
PROPOSAL_CONTEXT_MAX_NEW_SIGNALS = 40
MAX_RATIONALE_CHARS = 2000
MAX_GAP_SUBJECT_CHARS = 120
MAX_SUGGESTED_QUESTION_CHARS = 300

# ── threat events (step 3, spec §4.5) ─────────────────────────────────────────
THREAT_SEVERITY_MIN = 1
THREAT_SEVERITY_MAX = 5
THREAT_EXPIRES_MIN_DAYS = 1
THREAT_EXPIRES_MAX_DAYS = 90
# A model-written headline that may become user-facing copy once approved. Dropped past it,
# never truncated (spec note 2026-09-30).
MAX_EVENT_TITLE_CHARS = 200
# Pending event proposals and live threat events a generation call is shown (spec §4.3).
PROPOSAL_CONTEXT_MAX_EVENTS = 40
# A regeneration refused or failed without writing proposals is queued again after this many
# hours, up to GAP_REGENERATE_MAX_RUNS runs per gap: five runs six hours apart always span a
# UTC-midnight budget reset (spec §4.9, note 2026-09-30).
GAP_REGENERATE_RETRY_HOURS = 6
GAP_REGENERATE_MAX_RUNS = 5

# ── protection credits (step 4, spec §4.5, §4.8) ─────────────────────────────
PROTECTION_STRENGTH_MIN = 1
PROTECTION_STRENGTH_MAX = 5
PROTECTION_REVIEW_MIN_DAYS = 30
PROTECTION_REVIEW_MAX_DAYS = 366
# A live credit whose review_by is this close is renewal_due, and the worker queues its one
# renewal_check (spec §4.8).
PROTECTION_RENEWAL_WINDOW_DAYS = 30
# A renewal check that could not decide -- the fetcher down, a cited page unreachable -- is
# queued again after this many hours while the credit is still due, up to RENEWAL_MAX_RUNS
# checks per credit: a week of daily retries inside the thirty-day window (spec note
# 2026-09-30).
RENEWAL_RETRY_HOURS = 24
RENEWAL_MAX_RUNS = 7
