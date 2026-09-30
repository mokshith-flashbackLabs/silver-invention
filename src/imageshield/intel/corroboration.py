"""INVARIANTS #50: a web-only claim needs corroboration. ONE predicate, for every caller: the
approvable flag on both reads, the in-transaction re-check on decision, and (step 5) the
per-option ``corroborated`` flag on weight suggestions."""

from __future__ import annotations

from collections.abc import Sequence

from imageshield.intel.bounds import CORROBORATION_MIN_PUBLISHERS
from imageshield.intel.proposal_models import ContextSignal


def uncorroborated(signals: Sequence[ContextSignal]) -> bool:
    """True when the active signals are all ``trust = web`` and come from fewer than
    ``CORROBORATION_MIN_PUBLISHERS`` distinct publishers. ``publisher_domain`` is the
    registrable domain stored at fetch time (intel/publisher.py), so two subdomains of one
    publisher are one publisher. A listed signal corroborates on its own."""
    active = [s for s in signals if s.status == "active"]
    if any(s.trust == "listed" for s in active):
        return False
    return len({s.publisher_domain for s in active}) < CORROBORATION_MIN_PUBLISHERS
