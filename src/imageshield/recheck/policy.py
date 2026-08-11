"""What an HTTP status means about whether an infringement is still online.

Pure, and separate from the fetching, because the asymmetry here is the whole
feature and it should be readable without a network client around it.

**Only 404 and 410 mark a URL dead.** Everything else leaves the row exactly as
it was. That is not caution for its own sake:

- A **timeout or DNS failure** is not evidence of removal. It is evidence that
  we could not reach the host from here, right now.
- A **5xx** is the site having trouble. Sites recover.
- A **403 or 401** is *gated, not gone* — very often the opposite of removal,
  because a host that has started requiring a login still has the material.

Marking a hit resolved because a site was briefly down would tell a victim
their problem is fixed when it is not. Of the two errors available, leaving a
dead URL marked live costs the user a stale row; wrongly marking a live one
dead costs them a false all-clear about their own abuse. The asymmetry runs the
same direction as everywhere else in this system.

A dead URL is also the only unambiguously good news v1 can deliver. Detection
without takedown is otherwise alerts-with-no-remedy, and *"this came down"* is
the one thing the product can tell someone that is purely positive — which is
exactly why it must be true when we say it.
"""

from __future__ import annotations

from typing import Literal

# 'unchanged' means: do not write. Not even last_checked_at — see
# `RecheckStore.mark_checked`. A check that could not reach the host has not
# checked anything, and recording a timestamp for it would make the row look
# freshly verified.
Verdict = Literal["alive", "dead", "unchanged"]

# 410 Gone is the explicit one; 404 is what almost every host actually returns
# after a takedown.
_DEAD_STATUSES = frozenset({404, 410})


def verdict_for_status(status_code: int) -> Verdict:
    """Map one HTTP status to a verdict. The only place that mapping exists."""
    if status_code in _DEAD_STATUSES:
        return "dead"
    if 500 <= status_code <= 599:
        # Server trouble, not removal.
        return "unchanged"
    if status_code in (401, 403):
        # Gated, not gone. The material is still there, behind a wall.
        return "alive"
    if 200 <= status_code <= 399:
        return "alive"
    # Anything else (429, 418, a malformed status) is not a statement about
    # removal either. Default to leaving the row alone: the fail-safe direction
    # is always "we did not learn anything".
    return "unchanged"
