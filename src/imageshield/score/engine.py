"""The protection score arithmetic — pure, no I/O.

Design: ``docs/superpowers/specs/2026-08-19-protection-score-design.md`` §4.
Every number this module produces is derived from :class:`ScoreState` (a
snapshot the store assembles from the database) and :class:`ScoreWeights`
(config, loaded once at boot). Nothing here reads a clock, a database, or an
environment variable — that is what makes the arithmetic exhaustively
testable and what makes a historical score reproducible given the
``config_version`` stamped alongside it (migration 0022).

Four components, each computed independently and summed:

- **posture** — is the person set up correctly (enrolled, seeded, no hit
  awaiting their feedback, no recommendation left to rot)?
- **coverage** — are we actually looking (a recent scan, providers
  responding)?
- **exposure** — a penalty ledger, one entry per *human-confirmed* hit
  (INVARIANTS #19/#47 — never machine triage). ``url_dead`` or ``authorised``
  feedback removes an entry; nothing else does.
- **threat** — decays external, ambient risk (:mod:`imageshield` threat
  events) linearly over each event's ``decay_days``, with a hard cap on how
  much a single *global* event (one that names no domain) may cost — a
  global event alone must never zero this component.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from imageshield.config import Config


class ConfirmedHit(BaseModel):
    """One human-confirmed hit, reduced to exactly what the exposure
    component needs. ``counts`` is computed by the caller (the store): true
    when the hit is live, the user has not authorised or dismissed it, and it
    is not suspended under a pending ``not_me`` review."""

    model_config = ConfigDict(frozen=True)

    severity: str | None
    counts: bool


class ThreatPenalty(BaseModel):
    """One threat event as it applies to this person — already resolved to a
    single penalty/age/decay triple by the store; the engine only decays and
    caps it."""

    model_config = ConfigDict(frozen=True)

    penalty: Decimal
    age_days: int
    decay_days: int
    is_global: bool


class ScoreState(BaseModel):
    """Everything :func:`compute` needs for one person, and nothing else.

    Field names here are load-bearing: Task 12's store constructs this model
    directly from database rows, by these exact names.
    """

    model_config = ConfigDict(frozen=True)

    enrolment_active: bool
    seed_count: int
    seeds_fresh: bool
    has_overdue_scan: bool
    monitored_sources: int
    confirmed_hits: tuple[ConfirmedHit, ...]
    awaiting_feedback_count: int
    aged_open_recs: int
    threats: tuple[ThreatPenalty, ...]


class ScoreWeights(BaseModel):
    """One field per ``SCORE_*`` config knob (``score_`` prefix dropped), so
    :func:`compute` never carries an inline literal a retune would miss."""

    model_config = ConfigDict(frozen=True)

    weight_posture: int
    weight_coverage: int
    weight_exposure: int
    weight_threat: int

    posture_enrolment: int
    posture_seeds: int
    posture_feedback: int
    posture_recommendations: int

    coverage_scan: int
    coverage_providers: int

    exposure_weight_ncii: int
    exposure_weight_explicit: int
    exposure_weight_benign: int
    exposure_weight_default: int

    threat_global_max_penalty: int

    seed_target: int
    seed_fresh_days: int
    rec_soft_age_days: int
    scan_grace_days: int

    @classmethod
    def from_config(cls, cfg: Config) -> ScoreWeights:
        return cls(
            weight_posture=cfg.score_weight_posture,
            weight_coverage=cfg.score_weight_coverage,
            weight_exposure=cfg.score_weight_exposure,
            weight_threat=cfg.score_weight_threat,
            posture_enrolment=cfg.score_posture_enrolment,
            posture_seeds=cfg.score_posture_seeds,
            posture_feedback=cfg.score_posture_feedback,
            posture_recommendations=cfg.score_posture_recommendations,
            coverage_scan=cfg.score_coverage_scan,
            coverage_providers=cfg.score_coverage_providers,
            exposure_weight_ncii=cfg.score_exposure_weight_ncii,
            exposure_weight_explicit=cfg.score_exposure_weight_explicit,
            exposure_weight_benign=cfg.score_exposure_weight_benign,
            exposure_weight_default=cfg.score_exposure_weight_default,
            threat_global_max_penalty=cfg.score_threat_global_max_penalty,
            seed_target=cfg.score_seed_target,
            seed_fresh_days=cfg.score_seed_fresh_days,
            rec_soft_age_days=cfg.score_rec_soft_age_days,
            scan_grace_days=cfg.score_scan_grace_days,
        )


class Components(BaseModel):
    """The four scored components. ``total`` is what the proxy shows the
    user; the components are what lets it explain the number."""

    model_config = ConfigDict(frozen=True)

    posture: int
    coverage: int
    exposure: int
    threat: int

    @property
    def total(self) -> int:
        return max(0, min(100, self.posture + self.coverage + self.exposure + self.threat))

    def as_dict(self) -> dict[str, int]:
        return {
            "posture": self.posture,
            "coverage": self.coverage,
            "exposure": self.exposure,
            "threat": self.threat,
        }


def exposure_weight(severity: str | None, w: ScoreWeights) -> int:
    return {
        "ncii_suspected": w.exposure_weight_ncii,
        "explicit_unmatched": w.exposure_weight_explicit,
        "benign_copy": w.exposure_weight_benign,
    }.get(severity or "", w.exposure_weight_default)


def compute(state: ScoreState, w: ScoreWeights) -> Components:
    posture = w.posture_enrolment if state.enrolment_active else 0
    seed_pts = round(w.posture_seeds * min(state.seed_count, w.seed_target) / w.seed_target)
    if state.seed_count and not state.seeds_fresh:
        seed_pts = min(seed_pts, w.posture_seeds // 2)  # stale portfolio caps at half
    posture += seed_pts
    posture += w.posture_feedback if state.awaiting_feedback_count == 0 else 0
    posture += max(0, w.posture_recommendations - 2 * state.aged_open_recs)

    coverage = w.coverage_scan if (state.seed_count > 0 and not state.has_overdue_scan) else 0
    coverage += round(w.coverage_providers * min(state.monitored_sources, 2) / 2)

    spent = sum(exposure_weight(h.severity, w) for h in state.confirmed_hits if h.counts)
    exposure = max(0, w.weight_exposure - spent)

    global_burn = Decimal(0)
    targeted_burn = Decimal(0)
    for t in state.threats:
        factor = Decimal(max(0.0, 1.0 - t.age_days / t.decay_days)).quantize(Decimal("0.01"))
        burn = t.penalty * factor
        if t.is_global:
            global_burn += burn
        else:
            targeted_burn += burn
    global_burn = min(global_burn, Decimal(w.threat_global_max_penalty))
    threat = max(0, w.weight_threat - int(targeted_burn + global_burn))

    return Components(posture=posture, coverage=coverage, exposure=exposure, threat=threat)


__all__ = [
    "Components",
    "ConfirmedHit",
    "ScoreState",
    "ScoreWeights",
    "ThreatPenalty",
    "compute",
    "exposure_weight",
]
