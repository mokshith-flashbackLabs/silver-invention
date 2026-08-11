"""What a user said about a hit, and the one thing we must not do with it.

Pure policy: the signal-to-status mapping and nothing else. Separate from the
store for the same reason ``cadence.py`` is — the rule is worth reading without
SQL around it.

**The rule that matters.** ``not_me`` NEVER adjusts the user's identity
vectors, never suppresses future matches from that domain, and never feeds
banding. It writes a feedback row for reviewer calibration and sets the
infringement's status so the user stops seeing it. That is all it does.

The reason is specific and not hypothetical. Users reject *true* positives
under distress, and it is common. If rejections retrained the identity index or
suppressed a domain, the users most affected by real abuse would systematically
degrade their own protection — and the failure would be invisible, concentrating
on exactly the people this product exists for. Nobody would see it happen,
least of all them.

Keep the signal. Do not act on it automatically. A human reads it.
"""

from __future__ import annotations

from typing import Literal, get_args

FeedbackSignal = Literal["not_me", "confirmed", "uncertain"]

FEEDBACK_SIGNALS: tuple[str, ...] = get_args(FeedbackSignal)

# None means "record the feedback, leave the status alone". 'uncertain' is a
# real answer — the user looked and could not tell — and it must not be
# collapsed into either of the other two. Someone who is unsure about a match
# of their own face has told us something; forcing it to 'dismissed' or
# 'acknowledged' would be inventing a position they did not take.
_STATUS_BY_SIGNAL: dict[str, str | None] = {
    "not_me": "dismissed_not_me",
    "confirmed": "acknowledged",
    "uncertain": None,
}


def status_for(signal: str) -> str | None:
    """The infringement status this signal sets, or ``None`` to leave it."""
    return _STATUS_BY_SIGNAL[signal]
