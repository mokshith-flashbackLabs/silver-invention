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
