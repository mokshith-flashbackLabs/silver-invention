"""Adaptive scan cadence — pure, no I/O.

Everything else in step 8 controls spend. This is the only piece that *reduces*
it, and it is the 4-10x lever::

    new          scan on enrolment, then weekly for 4 weeks
    standard     weekly
    relaxed      fortnightly  -- 8 consecutive empty scans
    dormant      monthly      -- 20 consecutive empty scans
    priority     weekly, never demoted while a recent hit stands

Any non-empty scan promotes to ``priority`` and resets the counter. A user with
no hits in six months does not need weekly scans; a user who has just been found
in a deepfake needs every scan we can give them.

**Tiering must never be silent.** ``GET /v1/search/runs/{run_id}`` exposes
``scan_tier`` and ``next_scan_after`` so the proxy can state a user's real
monitoring cadence. Someone on ``dormant`` who believes they are scanned weekly
is being misled about a safety product, and that is a worse failure than the
cost this saves.

Plan tier can override — a paid tier holding ``standard`` as a floor is a
proxy-side decision. We expose the mechanism and do not implement the policy.

**One v1 approximation, stated plainly.** The brief defines ``priority`` as "any
confirmed infringement in 90 days". *Confirmed* means adjudicated, and the review
queue is specified but not built (CLAUDE.md §6). So v1 promotes on any non-empty
scan and releases after ``SCAN_PRIORITY_RELEASE_AFTER_EMPTY`` consecutive empty
scans — 13 at the priority cadence of one week, i.e. ~91 days. Replace the
release rule with a query against confirmed infringements when the queue lands;
the promotion rule stays.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict

from imageshield.config import Config

ScanTier = Literal["new", "standard", "relaxed", "dormant", "priority"]


class CadencePolicy(BaseModel):
    """The tier thresholds and intervals, lifted out of :class:`Config` so this
    module stays pure and testable without constructing an app config."""

    model_config = ConfigDict(frozen=True)

    standard_days: int
    relaxed_days: int
    dormant_days: int
    new_tier_weeks: int
    relaxed_after_empty: int
    dormant_after_empty: int
    priority_release_after_empty: int


class CadenceUpdate(BaseModel):
    model_config = ConfigDict(frozen=True)

    scan_tier: ScanTier
    consecutive_empty_scans: int
    next_scan_after: datetime


class CadenceInput(BaseModel):
    """Everything the store needs to re-tier a seed inside the run-completion
    transaction, minus the seed's own current state — that has to be read under
    the row lock, not passed in, or the lost-update race comes straight back.

    ``None`` in place of this object means "this run is not evidence": no
    provider succeeded, so the tier must not move (:func:`should_retier`).
    """

    model_config = ConfigDict(frozen=True)

    found_matches: bool
    now: datetime
    policy: CadencePolicy


def interval_days(tier: ScanTier, policy: CadencePolicy) -> int:
    """How long until the next scan for a seed on this tier.

    ``new`` and ``priority`` both run at the standard interval. They are
    separate tiers because of how they *transition*, not how often they run:
    ``new`` is protected from demotion for its first weeks, and ``priority``
    is protected for as long as a recent hit stands.
    """
    if tier == "relaxed":
        return policy.relaxed_days
    if tier == "dormant":
        return policy.dormant_days
    return policy.standard_days


def next_tier(
    *,
    current: ScanTier,
    consecutive_empty_scans: int,
    found_matches: bool,
    seed_age_days: int,
    policy: CadencePolicy,
) -> tuple[ScanTier, int]:
    """The tier and empty-scan counter after one completed scan.

    ``found_matches`` is "did this run record at least one infringement", not
    "did the run succeed". A run where every provider was skipped found nothing
    but also did not *look*, and counting that as an empty scan would demote a
    seed because our provider integration was broken. That case never reaches
    here at all — :func:`should_retier` filters it out first.
    """
    if found_matches:
        # Promotion is unconditional and skips every intermediate tier: someone
        # who has just been found needs the most frequent cadence available, not
        # a gradual climb back from dormant.
        return "priority", 0

    empties = consecutive_empty_scans + 1

    if current == "new":
        # Held at the new-user cadence for its first weeks regardless of the
        # counter — the first month after enrolment is when a user is most
        # likely to be checking, and when the corpus has had least time to turn
        # something up.
        if seed_age_days < policy.new_tier_weeks * 7:
            return "new", empties
        return _demotion_for(empties, policy), empties

    if current == "priority":
        if empties < policy.priority_release_after_empty:
            return "priority", empties
        # Released back to standard, never straight to relaxed or dormant: a
        # seed with a hit in living memory re-earns its demotion from the top.
        return "standard", empties

    return _demotion_for(empties, policy), empties


def _demotion_for(empties: int, policy: CadencePolicy) -> ScanTier:
    if empties >= policy.dormant_after_empty:
        return "dormant"
    if empties >= policy.relaxed_after_empty:
        return "relaxed"
    return "standard"


def should_retier(providers_succeeded: int) -> bool:
    """Whether a completed run's outcome may change the seed's tier.

    False when no provider succeeded. A run where every provider timed out, was
    budget-skipped or had an open breaker produced no evidence either way, and
    treating it as an empty scan would relax a user's cadence *because* our
    provider integration was broken — the cost saving would come out of exactly
    the wrong place.
    """
    return providers_succeeded > 0


def update_for(
    *,
    current: ScanTier,
    consecutive_empty_scans: int,
    found_matches: bool,
    seed_age_days: int,
    now: datetime,
    policy: CadencePolicy,
) -> CadenceUpdate:
    tier, empties = next_tier(
        current=current,
        consecutive_empty_scans=consecutive_empty_scans,
        found_matches=found_matches,
        seed_age_days=seed_age_days,
        policy=policy,
    )
    return CadenceUpdate(
        scan_tier=tier,
        consecutive_empty_scans=empties,
        next_scan_after=now + timedelta(days=interval_days(tier, policy)),
    )


def policy_from_config(cfg: Config) -> CadencePolicy:
    """One place that maps the env knobs onto the policy.

    Here rather than in the worker so the HTTP surface and the worker cannot end
    up disagreeing about a user's cadence — the proxy reads the tier from one and
    the other writes it.
    """
    return CadencePolicy(
        standard_days=cfg.scan_interval_standard_days,
        relaxed_days=cfg.scan_interval_relaxed_days,
        dormant_days=cfg.scan_interval_dormant_days,
        new_tier_weeks=cfg.scan_new_tier_weeks,
        relaxed_after_empty=cfg.scan_relaxed_after_empty,
        dormant_after_empty=cfg.scan_dormant_after_empty,
        priority_release_after_empty=cfg.scan_priority_release_after_empty,
    )
