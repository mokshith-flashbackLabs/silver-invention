"""Safety limits for likeness intel (spec §4.5). CODE CONSTANTS, not env: moving one
costs a code change, a review and a `git blame`, the argument REPORT_FACE_MATCH_MIN
makes on the backend. Proposal bounds are step 2's."""

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
