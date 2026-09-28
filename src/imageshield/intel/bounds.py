"""Safety limits for likeness intel (spec §4.5). CODE CONSTANTS, not env: moving one
costs a code change, a review and a `git blame`, the argument REPORT_FACE_MATCH_MIN
makes on the backend. Weight bounds arrive with proposals in step 2."""

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
