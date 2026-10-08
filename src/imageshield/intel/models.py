"""Row and value types shared by the intel store, the pipeline and the admin routes.

Frozen pydantic models over ``intel_sources`` / ``intel_runs`` / ``intel_vocabulary`` rows
(migration 0039, task-3-report.md's column list is binding for the store's SQL), the run log's
``intel_run_events`` rows (migration 0046), plus one
non-persisted read (``SpendToday``, a projection over ``providers``/``provider_spend``).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from imageshield.intel.tags import TagRegistry


class Source(BaseModel):
    """One ``intel_sources`` row (spec §3.2). ``source_url``/``url_hash``/``query_text``
    are jointly nullable: a ``policy_page``/``feed``/... source carries a URL, a
    ``search_query`` source carries ``query_text`` instead — the DB's own CHECKs enforce
    which, this model only carries whatever the row has. ``origin`` is ``suggested`` when a
    stage-1 source-proposal run proposed the source, else ``operator``; ``proposed_for`` is
    ``{question_key, option}`` provenance, never used for matching (0043).

    ``terms_note`` is optional since 0047 (2026-10-03). ``validation_run_id`` and
    ``validated_at`` are the automatic evidence instead: the source_validation run that found the
    source ready, and when that run completed, set together when stage 4 registers a source and
    null for one created on the Sources screen (``POST /sources``, never validated)."""

    model_config = ConfigDict(frozen=True)

    source_id: UUID
    kind: str
    source_url: str | None
    url_hash: str | None
    query_text: str | None
    tags: tuple[str, ...]
    check_every_hours: int
    next_check_at: datetime
    enabled: bool
    terms_note: str | None
    last_content_sha256: str | None
    last_checked_at: datetime | None
    last_run_status: str | None
    consecutive_failures: int
    disabled_reason: str | None
    created_by: str
    created_at: datetime
    # Migration 0043 (spec §4.10). Defaults, so a row or a fake without them still reads.
    origin: str = "operator"
    proposed_for: dict[str, Any] | None = None
    # Migration 0047. Defaults for the same reason.
    validation_run_id: UUID | None = None
    validated_at: datetime | None = None


class Run(BaseModel):
    """One ``intel_runs`` row. ``source_id`` is null for an ``adhoc_url`` run; ``request``
    and ``outcome`` are the run's own JSONB payload and result, never re-typed here."""

    model_config = ConfigDict(frozen=True)

    run_id: UUID
    kind: str
    source_id: UUID | None
    request: dict[str, Any]
    status: str
    attempts: int
    requested_by: str
    outcome: dict[str, Any]
    error_code: str | None
    created_at: datetime
    completed_at: datetime | None
    # A waiting weight suggestion's awaited sources and deadline (migration 0050); null on
    # every other run, and on a suggestion that never waited.
    awaiting_source_ids: list[UUID] | None = None
    wait_deadline: datetime | None = None


class RunEvent(BaseModel):
    """One ``intel_run_events`` row (migration 0046, spec 2026-10-03 §3.2). ``kind`` and the
    ``detail`` keys are the contract; ``text`` is server-authored English for display only."""

    model_config = ConfigDict(frozen=True)

    seq: int
    kind: str
    text: str
    detail: dict[str, Any]
    at: datetime
    updated_at: datetime


class RunEvents(BaseModel):
    """A run's whole log, ``seq`` ascending, with the run's kind and status read BEFORE the rows:
    a run's terminal status is written after its ``run_finished`` row, so a read that sees the
    terminal status also sees the complete log."""

    model_config = ConfigDict(frozen=True)

    run_id: UUID
    kind: str
    status: str
    events: tuple[RunEvent, ...]


class Vocabulary(BaseModel):
    """The singleton ``intel_vocabulary`` row (spec §3.1) — the backend's own tag/quiz/
    scoring identity as of the last push this repo accepted, per ``put_vocabulary``'s
    pair ordering."""

    model_config = ConfigDict(frozen=True)

    release_no: int
    map_version: int
    scoring_version: str
    quiz_version: str
    document: dict[str, Any]

    def registry(self) -> TagRegistry:
        tags = self.document.get("tags", [])
        return TagRegistry(
            active=frozenset(t["slug"] for t in tags if not t.get("retired")),
            retired=frozenset(t["slug"] for t in tags if t.get("retired")),
        )


class SpendToday(BaseModel):
    """A projection over ``providers``/``provider_spend`` for ``claude_intel``, never a
    persisted row of its own — ``daily_budget_usd`` is ``None`` until an operator sets one
    (invariant #38's fail-closed budget, applied to this provider)."""

    model_config = ConfigDict(frozen=True)

    spend_date: date
    call_count: int
    spent_today_usd: Decimal
    daily_budget_usd: Decimal | None


class SourcePause(BaseModel):
    """What one pause pass changed (spec §4.9, §4.10): sources paused because their tags all left
    the live quiz, and sources resumed because one came back (or their tags were cleared)."""

    model_config = ConfigDict(frozen=True)

    paused: tuple[UUID, ...] = ()
    resumed: tuple[UUID, ...] = ()
