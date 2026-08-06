"""Persistence for ``enrolments`` — raw SQL, no ORM (CLAUDE.md §2).

:class:`EnrolmentStore` is the Protocol the deletion route depends on; the
Postgres implementation lands with the DELETE path. The enrolment INSERT
itself lives on the liveness store's ``finalize_enrolled`` — it must share a
transaction with the session consumption (migration 0003's composite FK).
"""

from __future__ import annotations

from typing import Protocol

from imageshield.enrolment.models import EnrolmentRow
from imageshield.types import UserRef


class EnrolmentStore(Protocol):
    async def get_active_enrolments(self, user_ref: UserRef) -> tuple[EnrolmentRow, ...]: ...

    async def tombstone_enrolments(self, user_ref: UserRef) -> int: ...
