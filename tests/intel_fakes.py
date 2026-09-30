"""Shared fakes for the intel pipeline and worker tests (tasks 10 and 11).

A support module rather than fixtures imported out of a test module, the same shape
as ``providers_fakes.py``: importing a fixture from ``test_intel_pipeline`` into a
file whose test takes it as a parameter is ruff F811. The two DB fixtures built on
these helpers -- ``intel_db`` and ``intel_pool`` -- live in ``conftest.py``.

The fakes record what they were asked, because most pipeline assertions are about
what was NOT called: no fetch of a known hit, no second model call on an unchanged
page.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from imageshield.intel.evidence_store import DocumentRecord, PostgresEvidenceStore, SignalRecord
from imageshield.intel.fetch_client import FetchFailure, TextFetch
from imageshield.intel.model import ModelCall, ModelUnavailable
from imageshield.intel.models import Run, Vocabulary
from imageshield.intel.pipeline import PipelineDeps, RunResult, run
from imageshield.intel.pricing import Usage
from imageshield.intel.schemas import (
    DiscoveryOutput,
    ExtractedSignal,
    ExtractionOutput,
    ProposalOutput,
)
from imageshield.intel.store import PostgresIntelStore
from imageshield.intel.verify import VerifiedQuote
from imageshield.intel.vocabulary import ScoringVocabulary, parse_vocabulary
from imageshield.providers.store import PostgresProviderControlStore
from imageshield.search.urlhash import url_hash

NOW = datetime.now(UTC)
QUOTE = "Public profile photos may be used to train our AI models by default."
POLICY = ("Instagram Terms. " * 40) + QUOTE
VOCABULARY: dict[str, Any] = {
    "tags": [
        {"slug": "instagram", "label": "Instagram", "description": "photo app", "retired": False},
        {"slug": "myspace", "label": "Myspace", "description": "old", "retired": True},
    ],
    "questions": [],
    "option_tags": [],
    "renamed": [],
}


class FakeFetcher:
    """``pages`` maps a requested URL to what ``/v1/text`` would answer; anything
    unlisted is ``unfetchable``. ``fetched`` is every URL requested, in order."""

    def __init__(self, pages: dict[str, TextFetch | FetchFailure]) -> None:
        self.pages = pages
        self.fetched: list[str] = []

    async def fetch_text(self, url: str) -> TextFetch | FetchFailure:
        self.fetched.append(url)
        return self.pages.get(url, FetchFailure(code="unfetchable"))


class FakeModel:
    """Answers every extraction with ``extraction`` (``outcome`` other than ``ok``
    answers with no output, as the real seam does), or raises ``unavailable``."""

    def __init__(
        self,
        extraction: ExtractionOutput | None = None,
        outcome: str = "ok",
        discovery: DiscoveryOutput | None = None,
        unavailable: ModelUnavailable | None = None,
        *,
        propose_with: Callable[[dict[str, Any]], ProposalOutput] | None = None,
        proposal_outcome: str = "ok",
        propose_unavailable: ModelUnavailable | None = None,
    ) -> None:
        self.extraction = extraction
        self.outcome = outcome
        self.discovery = discovery
        self.unavailable = unavailable
        self.extract_calls = 0
        self.discover_calls = 0
        self.users: list[str] = []
        self.propose_with = propose_with
        self.proposal_outcome = proposal_outcome
        self.propose_unavailable = propose_unavailable
        self.propose_calls = 0
        self.proposal_users: list[str] = []

    async def extract(self, system: str, user: str) -> ModelCall[ExtractionOutput]:
        self.extract_calls += 1
        self.users.append(user)
        if self.unavailable is not None:
            raise self.unavailable
        output = self.extraction if self.outcome == "ok" else None
        stop = self.outcome if self.outcome in ("refusal", "max_tokens") else "end_turn"
        return ModelCall(
            output,
            self.outcome,  # type: ignore[arg-type]
            "claude-sonnet-5",
            stop,
            Usage(1, 1, 0, 0, 0),
            Decimal("0.01"),
            1,
        )

    async def discover(self, system: str, user: str) -> ModelCall[DiscoveryOutput]:
        self.discover_calls += 1
        if self.unavailable is not None:
            raise self.unavailable
        return ModelCall(
            self.discovery,
            "ok",
            "claude-sonnet-5",
            "end_turn",
            Usage(1, 1, 0, 0, 1),
            Decimal("0.02"),
            1,
        )

    async def propose(self, system: str, user: str) -> ModelCall[ProposalOutput]:
        """``propose_with`` receives the parsed user payload, so a test can cite the ids of
        signals the run under test just wrote."""
        self.propose_calls += 1
        self.proposal_users.append(user)
        if self.propose_unavailable is not None:
            raise self.propose_unavailable
        output: ProposalOutput | None = None
        if self.proposal_outcome == "ok":
            output = self.propose_with(json.loads(user)) if self.propose_with else ProposalOutput()
        stop = (
            self.proposal_outcome
            if self.proposal_outcome in ("refusal", "max_tokens")
            else "end_turn"
        )
        return ModelCall(
            output,
            self.proposal_outcome,  # type: ignore[arg-type]
            "claude-opus-5-5",
            stop,
            Usage(1, 1, 0, 0, 0),
            Decimal("0.05"),
            1,
        )


def make_page(
    text: str, url: str, final: str | None = None, items: list[dict[str, Any]] | None = None
) -> TextFetch:
    return TextFetch(
        text=text,
        content_type="text/html; charset=utf-8",
        final_url=final or url,
        truncated=False,
        items=items,
    )


def make_signal(
    quote: str = QUOTE,
    *,
    tags: list[str] | None = None,
    summary: str = "Default AI training; contact press@example.com",
    subjects: list[str] | None = None,
) -> ExtractionOutput:
    return ExtractionOutput(
        signals=[
            ExtractedSignal(
                category="policy",
                direction="risk_up",
                tags=["instagram", "madeup"] if tags is None else tags,
                unregistered_subjects=[] if subjects is None else subjects,
                summary=summary,
                quotes=[quote],
            )
        ]
    )


def make_deps(
    pool: AsyncConnectionPool,
    fetcher: FakeFetcher,
    model: FakeModel,
    *,
    max_calls_per_run: int = 20,
    max_document_chars: int = 200_000,
    clock: Callable[[], datetime] | None = None,
) -> PipelineDeps:
    control = PostgresProviderControlStore(
        pool,
        cache_seconds=0.0,
        failure_threshold=5,
        default_cooldown_seconds=300,
        max_cooldown_seconds=3600,
    )
    return PipelineDeps(
        store=PostgresIntelStore(pool),
        evidence=PostgresEvidenceStore(pool),
        fetcher=fetcher,
        model=model,
        control=control,
        clock=clock or (lambda: NOW),
        max_calls_per_run=max_calls_per_run,
        max_document_chars=max_document_chars,
    )


async def seed_vocabulary(pool: AsyncConnectionPool) -> None:
    await PostgresIntelStore(pool).put_vocabulary(
        release_no=1, map_version=1, scoring_version="s", quiz_version="q", document=VOCABULARY
    )


async def claim(pool: AsyncConnectionPool) -> Run:
    claimed = await PostgresIntelStore(pool).claim_next(NOW, lease_seconds=900)
    assert claimed is not None
    return claimed


async def run_once(pool: AsyncConnectionPool, deps: PipelineDeps) -> RunResult:
    """Claim, run AND finish -- a run left 'running' blocks the source's next check."""
    claimed = await claim(pool)
    result = await run(claimed, deps)
    await PostgresIntelStore(pool).finish_run(
        claimed.run_id,
        status=result.status,
        outcome=dict(result.outcome),
        error_code=result.error_code,
    )
    return result


# A quiz with a capped mutable question, an uncapped one and an escrowed one; one mapped tag
# (instagram), one registered-but-unmapped (linkedin), one retired (myspace). The shape is
# exactly what the backend's src/intel/vocabulary.ts pushes.
QUIZ_VOCABULARY: dict[str, Any] = {
    "questions": [
        {
            "key": "platforms",
            "prompt": "Where do you post photos of yourself?",
            "type": "mutable",
            "options": ["Instagram", "LinkedIn", "X (Twitter)", "Snapchat", "Threads"],
            "deductions": {
                "Instagram": 3, "LinkedIn": 2, "X (Twitter)": 4, "Snapchat": 7, "Threads": 1
            },
            "cap": 8,
        },
        {
            "key": "dating",
            "prompt": "Do you use dating apps?",
            "type": "mutable",
            "options": ["Yes", "No"],
            "deductions": {"Yes": 9, "No": 0},
            "cap": None,
        },
        {
            "key": "age",
            "prompt": "How old are you?",
            "type": "escrowed",
            "options": ["Under 25", "25 or over"],
            "deductions": {"Under 25": 5, "25 or over": 2},
            "cap": None,
        },
    ],
    "dynamic": {"threat": None, "protection": None},
    "tags": [
        {"slug": "instagram", "label": "Instagram", "description": "photo app", "kind": "platform",
         "retired": False},
        {"slug": "linkedin", "label": "LinkedIn", "description": "professional network",
         "kind": "platform", "retired": False},
        {"slug": "myspace", "label": "Myspace", "description": "old", "kind": "platform",
         "retired": True},
    ],
    "option_tags": [{"question_key": "platforms", "option": "Instagram", "tags": ["instagram"]}],
    "renamed": [],
}


def quiz_document(**overrides: Any) -> dict[str, Any]:
    """A deep copy of QUIZ_VOCABULARY with top-level keys replaced."""
    return {**copy.deepcopy(QUIZ_VOCABULARY), **overrides}


def scoring(
    document: dict[str, Any] | None = None, *, release_no: int = 2, map_version: int = 1
) -> ScoringVocabulary:
    parsed = parse_vocabulary(
        Vocabulary(
            release_no=release_no,
            map_version=map_version,
            scoring_version=f"s{release_no}",
            quiz_version="q",
            document=QUIZ_VOCABULARY if document is None else document,
        )
    )
    assert parsed is not None
    return parsed


async def seed_quiz_vocabulary(
    pool: AsyncConnectionPool,
    *,
    release_no: int = 2,
    map_version: int = 1,
    document: dict[str, Any] | None = None,
) -> None:
    await PostgresIntelStore(pool).put_vocabulary(
        release_no=release_no,
        map_version=map_version,
        scoring_version=f"s{release_no}",
        quiz_version="q",
        document=QUIZ_VOCABULARY if document is None else document,
    )


async def seed_signal(
    pool: AsyncConnectionPool,
    *,
    tags: tuple[str, ...] = (),
    subjects: tuple[str, ...] = (),
    trust: str = "listed",
    publisher: str = "p.example",
    category: str = "policy",
    created_at: datetime | None = None,
    run_id: UUID | None = None,
) -> UUID:
    """One active signal on its own document, through the real record_unit. Each call makes
    its own adhoc run unless ``run_id`` is given."""
    if run_id is None:
        run_id = await PostgresIntelStore(pool).queue_adhoc(
            f"https://{publisher}/seed", operator="seed"
        )
    url = f"https://{publisher}/{uuid4()}"
    document_id = await PostgresEvidenceStore(pool).record_unit(
        DocumentRecord(
            run_id=run_id,
            document_url=url,
            final_url=url,
            url_hash=url_hash(url),
            publisher_domain=publisher,
            trust=trust,  # type: ignore[arg-type]
            content_sha256="0" * 64,
            truncated=False,
            title="",
            published_at=None,
        ),
        [
            SignalRecord(
                category=category,  # type: ignore[arg-type]
                direction="risk_up",
                tags=tags,
                unregistered_subjects=subjects,
                summary="seeded",
                model_id="claude-sonnet-5",
                prompt_version="extract-v1",
                quotes=(VerifiedQuote(QUOTE, 0, len(QUOTE), "0" * 64),),
            )
        ],
        snapshot=None,
        source_hash=None,
    )
    assert document_id is not None
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT signal_id FROM intel_signals WHERE document_id = %s", (document_id,)
        )
        row = await cur.fetchone()
        assert row is not None
        if created_at is not None:
            await conn.execute(
                "UPDATE intel_signals SET created_at = %s WHERE signal_id = %s",
                (created_at, row[0]),
            )
    signal_id: UUID = row[0]
    return signal_id


async def seed_proposal(
    pool: AsyncConnectionPool,
    *,
    signal_ids: list[UUID],
    kind: str = "weight_change",
    status: str = "pending",
    target: dict[str, Any] | None = None,
    suggested: dict[str, Any] | None = None,
    decided: dict[str, Any] | None = None,
    against_release_no: int = 2,
    created_at: datetime | None = None,
) -> UUID:
    """A proposal row written directly (superuser), for tests of reads, decisions and the
    reconcile. Fills the shape CHECKs' required columns for the chosen status."""
    target = (
        target
        if target is not None
        else {"question_key": "platforms", "option": "Instagram", "current": 3}
    )
    suggested = (
        suggested if suggested is not None else ({"delta": 1} if kind == "weight_change" else {})
    )
    named = status in ("approved", "rejected", "applied")
    weighty = kind in ("weight_change", "weight_suggestion")
    async with pool.connection() as conn:
        cur = await conn.execute(
            """INSERT INTO intel_proposals (kind, status, target, suggested, decided, rationale,
                   against_scoring_version, against_release_no, model_id, prompt_version,
                   decided_by, decided_at, decision_reason, created_at, applied_ref,
                   supersede_reason)
               VALUES (%s, %s, %s, %s, %s, 'seeded', %s, %s, 'claude-opus-5-5', 'propose-v1',
                       %s, %s, %s, coalesce(%s, now()), %s, %s)
               RETURNING proposal_id""",
            (
                kind,
                status,
                Jsonb(target),
                Jsonb(suggested),
                Jsonb(decided) if decided is not None else None,
                f"s{against_release_no}" if weighty else None,
                against_release_no if weighty else None,
                "seed-op" if named else None,
                datetime.now(UTC) if named else None,
                "seeded" if named else None,
                created_at,
                "s3" if status == "applied" else None,
                "newer_proposal" if status == "superseded" else None,
            ),
        )
        row = await cur.fetchone()
        assert row is not None
        for signal_id in signal_ids:
            await conn.execute(
                "INSERT INTO intel_proposal_signals (proposal_id, signal_id) VALUES (%s, %s)",
                (row[0], signal_id),
            )
    proposal_id: UUID = row[0]
    return proposal_id
