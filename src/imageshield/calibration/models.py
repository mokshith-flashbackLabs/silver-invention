"""Types for calibration and banding (CLAUDE.md §7.2, §7.3).

No logic lives here. The rules are in :mod:`bands` and :mod:`metrics`, both
of which are pure functions over these values — that is what lets the
safety-critical decisions be tested exhaustively without a database.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from types import MappingProxyType
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from imageshield.types import ProviderId

# Ordering matters and is used by roll_up. Nothing else may define it.
Band = Literal["drop", "review", "auto_confirm"]
BAND_ORDER: dict[Band, int] = {"drop": 0, "review": 1, "auto_confirm": 2}

Label = Literal["true_match", "false_match", "uncertain"]
LabelKind = Literal[
    "same_person", "derived_edit", "novel_generation", "lookalike", "unrelated"
]

# `label` answers "is this the user's likeness, and should they be told?" —
# not "is this an authentic photograph of them". A derived_edit (nudify-style
# alteration of the subject's own photo) and a novel_generation (diffusion
# output of their face) are both their likeness being abused. Mirrors the
# eval_label_kind_agrees CHECK in migration 0007; the DB is authoritative.
TRUE_MATCH_KINDS: frozenset[LabelKind] = frozenset(
    {"same_person", "derived_edit", "novel_generation"}
)
FALSE_MATCH_KINDS: frozenset[LabelKind] = frozenset({"lookalike", "unrelated"})

ScoreKind = Literal["numeric", "categorical"]


class ScoreDomain(BaseModel):
    """The range or vocabulary a provider's raw values actually occupy, from
    ``providers.score_domain`` (written by migration 0004).

    Hive Web Search reports 0.5–1.0 where **0.5 is the floor**, not a midpoint
    meaning "uncertain". A value below it is impossible — a malformed response
    or a key provisioned against the wrong Hive project — and must not be read
    as "a very low score", which would band it ``drop`` and discard it
    silently.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    min: Decimal | None = None
    max: Decimal | None = None
    categories: tuple[str, ...] | None = None


class NumericBand(BaseModel):
    """One half-open interval in the provider's NATIVE units.

    Convention: ``min`` inclusive, ``max`` exclusive, except the top band
    whose ``max`` is the domain maximum inclusive. Stated because 0.94 exactly
    is the difference between ``review`` and ``auto_confirm``.

    ``min=None`` means "from the domain floor"; ``max=None`` means "to the
    domain ceiling".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    band: Band
    min: Decimal | None = None
    max: Decimal | None = None


class CalibrationConfig(BaseModel):
    """An active or candidate mapping from a provider's raw values to bands.

    Exactly one of ``numeric_bands`` / ``categorical_bands`` is populated,
    determined by ``score_kind``.

    ``categorical_bands`` is frozen into a ``MappingProxyType`` on
    validation (see ``_freeze_categorical_bands`` below), not just typed as
    ``Mapping``. ``model_config = ConfigDict(frozen=True)`` only stops
    reassigning the *attribute*; a plain ``dict`` stored in it would stay
    mutable in place, which would let a caller holding a reference change
    ``declares()``'s answer for a config snapshotted mid-run. This closes
    that: ``self.categorical_bands["x"] = "auto_confirm"`` raises
    ``TypeError`` at runtime, matching the frozen contract this whole model
    otherwise implies. (The tuple already used for ``numeric_bands`` has the
    same property for free — tuples are immutable — which is why only
    ``categorical_bands`` needed this treatment.)
    """

    model_config = ConfigDict(frozen=True)

    config_id: UUID
    provider_id: ProviderId
    version: str
    score_kind: ScoreKind
    numeric_bands: tuple[NumericBand, ...] = ()
    # default_factory=dict (not a bare MappingProxyType default) plus
    # validate_default=True: pydantic deep-copies non-"safe" default values
    # on every instantiation unless given a factory, and copy.deepcopy
    # cannot pickle a mappingproxy — a factory sidesteps that, and
    # validate_default makes the freeze validator below run even when the
    # caller never passes this field, so the invariant holds unconditionally.
    categorical_bands: Mapping[str, Band] = Field(
        default_factory=dict, validate_default=True
    )

    @field_validator("categorical_bands", mode="after")
    @classmethod
    def _freeze_categorical_bands(
        cls, value: Mapping[str, Band]
    ) -> Mapping[str, Band]:
        return MappingProxyType(dict(value))

    def declares(self, band: Band) -> bool:
        """Whether this config can ever produce ``band``.

        The activate floor skips the precision check for an ``auto_confirm``
        band the config never claims.
        """
        return band in {b.band for b in self.numeric_bands} or band in set(
            self.categorical_bands.values()
        )


class PolicyEntry(BaseModel):
    """Everything banding needs to know about one provider, snapshotted once
    per run so every attestation in that run is banded by the same rules."""

    model_config = ConfigDict(frozen=True)

    provider_id: ProviderId
    calibrated: bool
    score_domain: ScoreDomain
    config: CalibrationConfig | None  # None => no active config


BandingPolicy = Mapping[ProviderId, PolicyEntry]


class BandDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    band: Band
    reason: str
    calibration_version: str | None
