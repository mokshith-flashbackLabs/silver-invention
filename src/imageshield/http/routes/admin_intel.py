"""Likeness intel — the admin surface (step 1, step 2's proposals, step 4's
protection credits, and steps 5 and 6's source proposals, source validations and
weight suggestions) (spec §4.7).

Both tokens at router level, like every admin router — a route added later to
this file is guarded structurally rather than by memory. Every operator write
names the operator and is audited inside the store's own transaction
(``intel/store.py``, ``intel/evidence_store.py``); ``PUT /vocabulary`` and
``POST /proposals/applied`` are the
two exceptions, system writes with no operator at all.

Two refusals need the STORED row rather than the request body alone, and so
live here rather than in a pydantic validator: a saved query naming a person
(checked on create from the body; on patch also against the source's own
``kind``, since a PATCH body never carries ``kind``), and a tag a write would
ADD that is unknown or retired in the loaded vocabulary.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Query, Request

from imageshield.http.auth import require_admin_service_token, require_service_token
from imageshield.http.deps import (
    get_decision_store,
    get_evidence_store,
    get_intel_store,
    get_proposal_store,
    get_protection_store,
    get_question_store,
)
from imageshield.http.errors import ServiceError
from imageshield.http.models import (
    IntelAppliedRequest,
    IntelDecisionRequest,
    IntelDocumentRequest,
    IntelOperatorRequest,
    IntelProposalKind,
    IntelProposalStatus,
    IntelProtectionStatus,
    IntelRetractRequest,
    IntelSourceCreateRequest,
    IntelSourcePatchRequest,
    IntelSourceProposalRequest,
    IntelSourceValidationRequest,
    IntelVocabularyRequest,
    IntelWeightSuggestionRequest,
)
from imageshield.intel.bounds import DEFAULT_SOURCE_CHECK_EVERY_HOURS, PROPOSAL_ORIGIN_DAYS
from imageshield.intel.decisions import DecisionStore
from imageshield.intel.evidence_store import EvidenceStore
from imageshield.intel.models import Run
from imageshield.intel.pii import contains_pii
from imageshield.intel.proposal_models import DecisionRefused
from imageshield.intel.proposal_store import ProposalStore
from imageshield.intel.protection_store import ProtectionStore
from imageshield.intel.question_store import QuestionStore
from imageshield.intel.source_choice import (
    NewSource,
    existing_source_ids,
    identity,
    merge_by_identity,
    option_tags,
    render_source_proposal,
    render_validation,
    validation_problems,
)
from imageshield.intel.store import IntelStore
from imageshield.intel.suggestion import render_suggestion
from imageshield.intel.tags import TagRegistry, membership_problems
from imageshield.intel.text import normalise
from imageshield.intel.vocabulary import parse_vocabulary
from imageshield.search.urlhash import canonicalise, url_hash

log = structlog.get_logger("imageshield.intel")

router = APIRouter(
    prefix="/v1/admin/intel",
    dependencies=[Depends(require_service_token), Depends(require_admin_service_token)],
)

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def _refuse(code: str, message: str, **extra: Any) -> ServiceError:
    return ServiceError(422, code, message, retryable=False, extra=extra or None)


def _source_not_found() -> ServiceError:
    return ServiceError(
        404, "intel_source_not_found", "No intel source with this id.", retryable=False
    )


def _signal_not_found() -> ServiceError:
    return ServiceError(404, "signal_not_found", "No signal with this id.", retryable=False)


def _encode_cursor(created_at: datetime, row_id: UUID) -> str:
    # The `ISO|UUID` literal: the phone-shaped build gate scans only literal
    # strings, and this f-string's constant parts hold no digits, so it is
    # safe (same shape as admin_hits.py's cursor).
    return base64.urlsafe_b64encode(f"{created_at.isoformat()}|{row_id}".encode()).decode()


def _decode_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    if cursor is None:
        return None
    try:
        stamp, row_id = base64.urlsafe_b64decode(cursor.encode()).decode().split("|", 1)
        return datetime.fromisoformat(stamp), UUID(row_id)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ServiceError(422, "invalid_cursor", "cursor is malformed", retryable=False) from exc


async def _check_tags(store: IntelStore, tags: tuple[str, ...]) -> None:
    """Refuse a tag a write would ADD if it is not in the loaded vocabulary, or
    is retired there. ``tags`` must already be the DIFF against whatever the
    target row already carries — a retired tag already on a source's own
    target is not "added" and must never reach this check (spec §3.1)."""
    if not tags:
        return
    vocab = await store.load_vocabulary()
    registry = vocab.registry() if vocab is not None else TagRegistry(frozenset(), frozenset())
    unknown, retired = membership_problems(tags, registry)
    if unknown:
        raise _refuse("unknown_tag", "a tag is not registered", slugs=unknown)
    if retired:
        raise _refuse("tag_retired", "a retired tag cannot be added", slugs=retired)


async def _check_url(store: IntelStore, url: str | None) -> None:
    if url is not None and await store.is_known_hit(url_hash(url)):
        raise _refuse("known_hit_location", "this URL is a known hit location and is never read")


async def _all_tags_unmapped(store: IntelStore, tags: tuple[str, ...]) -> bool:
    """spec §4.10: a source whose non-empty tags are all unmapped cannot run -- the tick would
    pause it again within one poll -- so enabling one is refused rather than silently undone.
    With no readable vocabulary nothing counts as unmapped, exactly as the tick sees it."""
    if not tags:
        return False
    row = await store.load_vocabulary()
    vocabulary = parse_vocabulary(row) if row is not None else None
    return vocabulary is not None and not set(tags) & vocabulary.mapped_tags


@router.post("/sources", status_code=201)
async def create_source(
    body: IntelSourceCreateRequest, store: IntelStore = Depends(get_intel_store)
) -> Any:
    if body.query_text is not None and contains_pii(body.query_text):
        raise _refuse(
            "query_names_a_person", "a saved query must not contain a phone number or email"
        )
    await _check_url(store, body.source_url)
    await _check_tags(store, body.tags)
    source = await store.create_source(
        kind=body.kind,
        source_url=body.source_url,
        query_text=body.query_text,
        tags=body.tags,
        check_every_hours=body.check_every_hours,
        terms_note=body.terms_note,
        operator=body.operator,
    )
    log.info("intel.source_created_via_admin", kind=body.kind, operator=body.operator)
    return source


@router.patch("/sources/{source_id}")
async def patch_source(
    source_id: UUID, body: IntelSourcePatchRequest, store: IntelStore = Depends(get_intel_store)
) -> Any:
    existing = await store.get_source(source_id)
    if existing is None:
        raise _source_not_found()
    if body.query_text is not None:
        # The STORED kind, never the body's — a PATCH carries no `kind` field
        # at all, so this half of the check cannot live in the pydantic model
        # (controller ruling 1).
        if existing.kind != "search_query":
            raise _refuse(
                "query_text_wrong_kind",
                "query_text may only be set on a search_query source",
            )
        if contains_pii(body.query_text):
            raise _refuse(
                "query_names_a_person", "a saved query must not contain a phone number or email"
            )
    if body.tags is not None:
        added = tuple(tag for tag in body.tags if tag not in existing.tags)
        await _check_tags(store, added)
    if body.enabled is True:
        tags = body.tags if body.tags is not None else existing.tags
        if await _all_tags_unmapped(store, tags):
            raise ServiceError(
                409,
                "source_tags_unmapped",
                "No option of the live quiz maps to any of this source's tags; map one, or clear"
                " its tags, before enabling it.",
                retryable=False,
                extra={"slugs": list(tags)},
            )
    source = await store.patch_source(
        source_id,
        operator=body.operator,
        enabled=body.enabled,
        check_every_hours=body.check_every_hours,
        tags=body.tags,
        terms_note=body.terms_note,
        query_text=body.query_text,
    )
    if source is None:
        raise _source_not_found()
    return source


@router.get("/sources")
async def list_sources(
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    store: IntelStore = Depends(get_intel_store),
) -> dict[str, Any]:
    sources = await store.list_sources(cursor=_decode_cursor(cursor), limit=limit)
    next_cursor = (
        _encode_cursor(sources[-1].created_at, sources[-1].source_id)
        if len(sources) == limit
        else None
    )
    return {"sources": sources, "next_cursor": next_cursor}


@router.post("/sources/{source_id}/check")
async def check_source(
    source_id: UUID, body: IntelOperatorRequest, store: IntelStore = Depends(get_intel_store)
) -> dict[str, UUID]:
    run_id = await store.queue_source_check(source_id, operator=body.operator)
    if run_id is None:
        raise _source_not_found()
    return {"run_id": run_id}


@router.post("/documents", status_code=202)
async def paste_document(
    body: IntelDocumentRequest, store: IntelStore = Depends(get_intel_store)
) -> dict[str, UUID]:
    await _check_url(store, body.url)
    run_id = await store.queue_adhoc(body.url, operator=body.operator)
    log.info("intel.document_queued_via_admin", operator=body.operator)
    return {"run_id": run_id}


@router.get("/runs")
async def list_runs(
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    store: IntelStore = Depends(get_intel_store),
) -> dict[str, Any]:
    runs = await store.list_runs(cursor=_decode_cursor(cursor), limit=limit)
    spend = await store.spend_today(datetime.now(UTC))
    headroom = (
        (spend.daily_budget_usd - spend.spent_today_usd)
        if spend.daily_budget_usd is not None
        else None
    )
    next_cursor = (
        _encode_cursor(runs[-1].created_at, runs[-1].run_id) if len(runs) == limit else None
    )
    return {
        "runs": runs,
        "next_cursor": next_cursor,
        "spend": {
            "spend_date": spend.spend_date.isoformat(),
            "call_count": spend.call_count,
            "spent_today_usd": str(spend.spent_today_usd),
            "daily_budget_usd": (
                str(spend.daily_budget_usd) if spend.daily_budget_usd is not None else None
            ),
            "budget_headroom_usd": str(headroom) if headroom is not None else None,
        },
    }


@router.get("/signals")
async def list_signals(
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    evidence: EvidenceStore = Depends(get_evidence_store),
) -> dict[str, Any]:
    signals = await evidence.list_signals(cursor=_decode_cursor(cursor), limit=limit)
    next_cursor = (
        _encode_cursor(signals[-1]["created_at"], signals[-1]["signal_id"])
        if len(signals) == limit
        else None
    )
    return {"signals": signals, "next_cursor": next_cursor}


@router.get("/signals/{signal_id}")
async def get_signal(signal_id: UUID, evidence: EvidenceStore = Depends(get_evidence_store)) -> Any:
    signal = await evidence.get_signal(signal_id)
    if signal is None:
        raise _signal_not_found()
    return signal


@router.post("/signals/{signal_id}/retract")
async def retract_signal(
    signal_id: UUID,
    body: IntelRetractRequest,
    evidence: EvidenceStore = Depends(get_evidence_store),
) -> dict[str, str]:
    result = await evidence.retract_signal(signal_id, operator=body.operator, reason=body.reason)
    if result == "not_found":
        raise _signal_not_found()
    if result == "not_active":
        raise ServiceError(
            409, "signal_not_active", "This signal is already retracted.", retryable=False
        )
    log.info("intel.signal_retracted_via_admin", signal_id=str(signal_id), operator=body.operator)
    return {"status": "retracted"}


@router.put("/vocabulary")
async def put_vocabulary(
    body: IntelVocabularyRequest, store: IntelStore = Depends(get_intel_store)
) -> dict[str, bool]:
    applied = await store.put_vocabulary(
        release_no=body.release_no,
        map_version=body.map_version,
        scoring_version=body.scoring_version,
        quiz_version=body.quiz_version,
        document=body.document.model_dump(),
    )
    log.info(
        "intel.vocabulary_pushed",
        release_no=body.release_no,
        map_version=body.map_version,
        applied=applied,
    )
    return {"applied": applied}


# -- protection credits (step 4) ----------------------------------------------


@router.get("/protection-events")
async def list_protection_events(
    statuses: list[IntelProtectionStatus] | None = Query(default=None, alias="status"),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    protections: ProtectionStore = Depends(get_protection_store),
) -> dict[str, Any]:
    rows = await protections.list_events(
        statuses=statuses, cursor=_decode_cursor(cursor), limit=limit
    )
    next_cursor = (
        _encode_cursor(rows[-1]["created_at"], rows[-1]["event_id"]) if len(rows) == limit else None
    )
    return {"events": rows, "next_cursor": next_cursor}


@router.post("/protection-events/{event_id}/retract")
async def retract_protection_event(
    event_id: UUID,
    body: IntelRetractRequest,
    protections: ProtectionStore = Depends(get_protection_store),
) -> dict[str, Any]:
    retraction = await protections.retract(event_id, operator=body.operator, reason=body.reason)
    if retraction is None:
        raise ServiceError(
            404,
            "protection_event_not_found",
            "No protection event with this id.",
            retryable=False,
        )
    log.info(
        "intel.protection_retracted_via_admin",
        event_id=str(event_id),
        operator=body.operator,
        already_retracted=retraction.already_retracted,
        also_retracted=len(retraction.also_retracted),
    )
    return {
        "event_id": retraction.event_id,
        "status": "retracted",
        "also_retracted": list(retraction.also_retracted),
        "renewal_proposals_rejected": list(retraction.renewal_proposals_rejected),
    }


# -- proposals (step 2) -------------------------------------------------------

# Every decision refusal by name (spec 4.7). The backend maps each code, so none may
# collapse into a generic 409/422.
_REFUSAL_STATUS: dict[str, int] = {
    "proposal_not_found": 404,
    "proposal_not_pending": 409,
    "proposal_not_decidable": 409,
    "proposal_evidence_retracted": 409,
    "proposal_uncorroborated": 409,
    "proposal_tags_unmapped": 409,
    "proposal_cell_awaiting_publish": 409,
    "values_out_of_bounds": 422,
    "unknown_tag": 422,
    "tag_retired": 422,
}


@router.get("/proposals")
async def list_proposals(
    statuses: list[IntelProposalStatus] | None = Query(default=None, alias="status"),
    kinds: list[IntelProposalKind] | None = Query(default=None, alias="kind"),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    proposals: ProposalStore = Depends(get_proposal_store),
) -> dict[str, Any]:
    rows = await proposals.list_proposals(
        statuses=statuses, kinds=kinds, cursor=_decode_cursor(cursor), limit=limit
    )
    next_cursor = (
        _encode_cursor(rows[-1]["created_at"], rows[-1]["proposal_id"])
        if len(rows) == limit
        else None
    )
    return {"proposals": rows, "next_cursor": next_cursor}


@router.get("/proposals/{proposal_id}")
async def get_proposal(
    proposal_id: UUID, request: Request, proposals: ProposalStore = Depends(get_proposal_store)
) -> Any:
    row = await proposals.get_proposal(proposal_id)
    if row is None:
        raise ServiceError(
            404, "proposal_not_found", "No proposal with this id.", retryable=False
        )
    if row["kind"] == "weight_suggestion":
        # spec §4.6: the per-option read. The question store is looked up only here, so every
        # other kind's detail reads exactly as step 2 built it.
        questions = get_question_store(request)
        row = {**row, "options": await questions.options_of_suggestion(proposal_id)}
    return row


@router.post("/proposals/applied")
async def proposals_applied(
    body: IntelAppliedRequest, decisions: DecisionStore = Depends(get_decision_store)
) -> dict[str, list[UUID]]:
    result = await decisions.mark_applied(
        scoring_version=body.scoring_version, proposal_ids=body.proposal_ids
    )
    if result.not_applied:
        # ids only: a withdrawal that raced a publish, or an id this build never wrote.
        log.warning(
            "intel.applied_ack_unmatched",
            scoring_version=body.scoring_version,
            proposal_ids=[str(i) for i in result.not_applied],
        )
    return {
        "applied": list(result.applied),
        "already_applied": list(result.already_applied),
        "not_applied": list(result.not_applied),
    }


@router.post("/proposals/{proposal_id}/decision")
async def decide_proposal(
    proposal_id: UUID,
    body: IntelDecisionRequest,
    decisions: DecisionStore = Depends(get_decision_store),
) -> dict[str, Any]:
    try:
        result = await decisions.decide(
            proposal_id,
            decision=body.decision,
            values=body.values,
            reason=body.reason,
            operator=body.operator,
            applies_regardless_of_location=body.applies_regardless_of_location,
        )
    except DecisionRefused as refused:
        raise ServiceError(
            _REFUSAL_STATUS[refused.code],
            refused.code,
            refused.message,
            retryable=False,
            extra={"slugs": list(refused.slugs)} if refused.slugs else None,
        ) from refused
    log.info(
        "intel.proposal_decided_via_admin",
        proposal_id=str(proposal_id),
        decision=body.decision,
        operator=body.operator,
    )
    return {
        "proposal_id": result.proposal_id,
        "kind": result.kind,
        "status": result.status,
        "applied_ref": result.applied_ref,
        "decided": result.decided,
    }


# -- sources per question and weight suggestions (step 5, spec 4.6 and 4.10) --------------


def _run_not_found() -> ServiceError:
    return ServiceError(
        404, "intel_run_not_found", "No run of this kind with this id.", retryable=False
    )


async def _question_run(questions: QuestionStore, run_id: UUID, kind: str) -> Run:
    """A poll answers only for a run of its own kind: another kind's id is as unknown as no id."""
    run = await questions.get_run(run_id)
    if run is None or run.kind != kind:
        raise _run_not_found()
    return run


@router.post("/source-proposals", status_code=202)
async def propose_sources(
    body: IntelSourceProposalRequest, questions: QuestionStore = Depends(get_question_store)
) -> dict[str, UUID]:
    """Stage 1 of spec §4.10: queue a source_proposal run. Nothing is registered."""
    run_id = await questions.queue_source_proposal(body.question_request(), operator=body.operator)
    log.info("intel.source_proposal_queued_via_admin", operator=body.operator)
    return {"run_id": run_id}


@router.get("/source-proposals/{run_id}")
async def source_proposal_poll(
    run_id: UUID, questions: QuestionStore = Depends(get_question_store)
) -> dict[str, Any]:
    run = await _question_run(questions, run_id, "source_proposal")
    sources = await questions.sources_by_ids(existing_source_ids(run))
    return render_source_proposal(run, {s.source_id: s for s in sources})


@router.post("/source-validations", status_code=202)
async def validate_sources(
    body: IntelSourceValidationRequest, questions: QuestionStore = Depends(get_question_store)
) -> dict[str, UUID]:
    """Stage 3 of spec §4.10: queue a source_validation run."""
    run_id = await questions.queue_source_validation(
        body.validation_request(), operator=body.operator
    )
    log.info(
        "intel.source_validation_queued_via_admin",
        operator=body.operator,
        candidates=len(body.candidates),
    )
    return {"run_id": run_id}


@router.get("/source-validations/{run_id}")
async def source_validation_poll(
    run_id: UUID, questions: QuestionStore = Depends(get_question_store)
) -> dict[str, Any]:
    return render_validation(await _question_run(questions, run_id, "source_validation"))


@router.post("/weight-suggestions", status_code=202)
async def suggest_weights(
    body: IntelWeightSuggestionRequest,
    store: IntelStore = Depends(get_intel_store),
    questions: QuestionStore = Depends(get_question_store),
) -> dict[str, UUID]:
    """Stage 4 of spec §4.10 (and §4.6). Every chosen source must be ready in its validation run
    within 24 hours. The known-hit and PII checks run again, because registration is one of the
    places §6.1 names. A tag a chosen source would carry must be registered (§3.1), and a retired
    one is left off the new source. Then ONE transaction registers or reuses the sources and
    queues the run. Nothing is registered when any check refuses."""
    now = datetime.now(UTC)
    validations = await questions.validations(sorted({s.validation_run_id for s in body.sources}))
    problems = validation_problems(body.sources, validations, now=now)
    if problems:
        raise _refuse(
            "source_not_validated",
            "a chosen source is not ready in its validation run",
            entries=problems,
        )
    for source in body.sources:
        if source.query_text is not None and contains_pii(source.query_text):
            raise _refuse(
                "query_names_a_person", "a saved query must not contain a phone number or email"
            )
        await _check_url(store, source.source_url)
    row = await store.load_vocabulary()
    registry = row.registry() if row is not None else TagRegistry(frozenset(), frozenset())
    vocabulary = parse_vocabulary(row) if row is not None else None
    tags_by_option = {
        s.option: option_tags(
            s.option,
            question_key=body.question_key,
            request_tags=body.tags,
            vocabulary=vocabulary,
        )
        for s in body.sources
    }
    unknown = sorted(
        {
            tag
            for tags in tags_by_option.values()
            for tag in tags
            if tag not in registry.active and tag not in registry.retired
        }
    )
    if unknown:
        raise _refuse("unknown_tag", "a tag is not registered", slugs=unknown)
    proposed: frozenset[str] = frozenset()
    if body.sources:
        proposed = await questions.proposed_identities(
            body.question_key, since=now - timedelta(days=PROPOSAL_ORIGIN_DAYS)
        )
    chosen: list[NewSource] = []
    for s in body.sources:
        was_proposed = identity(s.kind, s.source_url, s.query_text) in proposed
        chosen.append(
            NewSource(
                kind=s.kind,
                source_url=canonicalise(s.source_url) if s.source_url is not None else None,
                query_text=normalise(s.query_text) if s.query_text is not None else None,
                tags=tuple(t for t in tags_by_option[s.option] if t not in registry.retired),
                check_every_hours=s.check_every_hours or DEFAULT_SOURCE_CHECK_EVERY_HOURS,
                terms_note=s.terms_note,
                origin="suggested" if was_proposed else "operator",
                question_key=body.question_key,
                option=s.option,
            )
        )
    queued = await questions.register_and_queue_suggestion(
        body.question_request(), merge_by_identity(chosen), operator=body.operator
    )
    log.info(
        "intel.weight_suggestion_queued_via_admin",
        operator=body.operator,
        registered=len(queued.registered),
        reused=len(queued.reused),
    )
    return {"run_id": queued.run_id}


@router.get("/weight-suggestions/{run_id}")
async def weight_suggestion_poll(
    run_id: UUID, questions: QuestionStore = Depends(get_question_store)
) -> dict[str, Any]:
    run = await _question_run(questions, run_id, "weight_suggestion")
    return render_suggestion(run, await questions.suggestion_of_run(run_id))
