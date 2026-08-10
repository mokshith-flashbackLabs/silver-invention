"""Row models for ``subjects``.

Frozen pydantic models mirroring the table shape — the runtime half of the
typed-identifier discipline (CLAUDE.md §10).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from imageshield.types import UserRef

# Kept in lockstep with migration 0008's CHECK constraint. Two values, not a
# free-text field: 'why is this person not being scanned' is a question a
# support agent and an auditor both ask, and an open string would answer it
# fifteen different ways.
EligibilityReason = Literal["adult", "minor_discovery_deferred"]


class Eligibility(BaseModel):
    """The decision, before it is stored. ``discovery_eligible`` and
    ``eligibility_reason`` are two views of one fact and the database CHECKs
    that they agree — this model is the in-process half of the same pairing."""

    model_config = ConfigDict(frozen=True)

    discovery_eligible: bool
    eligibility_reason: EligibilityReason


class SubjectRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_ref: UserRef
    discovery_eligible: bool
    eligibility_reason: EligibilityReason
    created_at: datetime
    updated_at: datetime
