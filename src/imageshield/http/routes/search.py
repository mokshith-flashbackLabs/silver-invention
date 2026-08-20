"""Seeds + search runs (CLAUDE.md §8 step 5).

``POST /v1/search`` is **202 and enqueue-only**: the run row and its outbox
row land in one transaction (``SearchStore.create_run``) and the
``search:runs`` worker does the dispatch. Discovery is slow and
rate-limited; it never happens on a request thread.

A seed belonging to another ``user_ref`` answers exactly like a missing one
(404 ``seed_not_found``) — the proxy is the auth boundary, but this surface
still must not confirm another user's seed exists.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Query

from imageshield.config import Config
from imageshield.http.auth import require_service_token
from imageshield.http.deps import (
    get_config,
    get_score_store,
    get_search_store,
    get_subject_store,
)
from imageshield.http.errors import ServiceError
from imageshield.http.models import (
    AttestationItem,
    InfringementItem,
    InfringementsResponse,
    SearchCreateRequest,
    SearchCreateResponse,
    SearchRunStatusResponse,
    SeedCreateRequest,
    SeedCreateResponse,
)
from imageshield.score.store import ScoreStore
from imageshield.search.store import SearchStore, UnknownSubject
from imageshield.subjects.store import SubjectStore
from imageshield.types import ProviderId, UserRef, parse_provider_id

log = structlog.get_logger("imageshield.search")

router = APIRouter(prefix="/v1", dependencies=[Depends(require_service_token)])


@router.post("/seeds", status_code=201)
async def create_seed(
    body: SeedCreateRequest,
    store: SearchStore = Depends(get_search_store),
    score_store: ScoreStore = Depends(get_score_store),
) -> SeedCreateResponse:
    # No eligibility check here, deliberately — only an existence one, enforced
    # by migration 0008's FK and surfaced as UnknownSubject. A minor may hold
    # seeds: they enrol, and consent/guardianship/household seats all work. What
    # a minor may not have is a *search*, and that refusal lives on
    # POST /v1/search where the searching happens. Refusing the seed as well
    # would be a second gate on the wrong thing, and would make the enrolment
    # flow fail for a user we deliberately support.
    try:
        seed_id = await store.create_seed(
            body.user_ref, body.seed_kind, body.source_object_ref
        )
    except UnknownSubject as exc:
        raise ServiceError(
            409,
            "subject_unknown",
            "No subject record for this user_ref. Enrolment creates it; a seed"
            " cannot be parented to a subject that does not exist.",
            retryable=False,
        ) from exc
    log.info("search.seed_created", seed_id=str(seed_id), seed_kind=body.seed_kind)
    try:
        await score_store.recompute(body.user_ref, cause_kind="seed_registered")
    except Exception:  # deliberate: the trigger already committed; tick will heal
        log.warning(
            "score.recompute_failed", user_ref=str(body.user_ref), cause="seed_registered"
        )
    return SeedCreateResponse(seed_id=seed_id)


@router.post("/search", status_code=202)
async def create_search(
    body: SearchCreateRequest,
    cfg: Config = Depends(get_config),
    store: SearchStore = Depends(get_search_store),
    subjects: SubjectStore = Depends(get_subject_store),
) -> SearchCreateResponse:
    # Body shape first: cheaper and more absolute than any guard, reads nothing,
    # and refuses before the audit trail begins. There is deliberately NO
    # fallback to the seed — the seed holds a durable ref that no provider can
    # fetch, so substituting it would dispatch a search that cannot work and
    # then report the failure as a provider outage. That is the whole bug.
    if body.seed_url is None:
        raise ServiceError(
            400,
            "seed_url_required",
            "seed_url is required: a freshly-minted presigned GET for this run,"
            " ≥15-minute TTL. The seed holds a durable reference, not a fetchable"
            " URL, and this service holds no S3 credentials to mint one.",
            retryable=False,
        )

    # ── Guard chain step 1: ELIGIBILITY ──────────────────────────────────
    # First of the guards, and before any row exists. An eligibility
    # refusal must consume no budget and touch no breaker, which it cannot do
    # if it happens after them.
    await _require_discovery_eligible(body.user_ref, cfg, subjects)

    seed = await store.get_seed(body.seed_id)
    if seed is None or seed.user_ref != body.user_ref:
        raise ServiceError(
            404,
            "seed_not_found",
            "No such seed for this user_ref.",
            retryable=False,
        )

    enabled = await store.enabled_provider_ids()
    selected = _select_providers(body.providers, enabled)
    if not selected:
        raise ServiceError(
            503,
            "no_providers_enabled",
            "No search providers are currently enabled.",
            retryable=True,
        )

    run_id = await store.create_run(
        body.user_ref, body.seed_id, selected, seed_url=body.seed_url
    )
    log.info(
        "search.run_enqueued",
        run_id=str(run_id),
        seed_id=str(body.seed_id),
        providers=[str(p) for p in selected],
    )
    return SearchCreateResponse(run_id=run_id)


async def _require_discovery_eligible(
    user_ref: UserRef, cfg: Config, subjects: SubjectStore
) -> None:
    """Refuse the whole request when discovery must not run for this subject.

    Writes exactly one ``audit_log`` row and **nothing else**: no
    ``search_runs`` row, no ``provider_calls`` row, no provider client touched.
    A ``search_runs`` row with zero results reads as "we looked and found
    nothing", which for a subject nobody searched is a false reassurance — and
    for a minor it would be a false reassurance about the exact thing the
    refusal exists to prevent.

    Two distinct outcomes, deliberately different status codes:

    - **409 subject_unknown** — no subject row. The proxy has a user we have
      never enrolled, which is a state it can fix (enrol them) and therefore a
      conflict, not a permission problem.
    - **403 discovery_not_available** — the subject is a minor. Nothing the
      proxy can do changes this until v2, so it is a refusal.
    """
    subject = await subjects.get_subject(user_ref)
    ages = {
        "min_enrolment_age": cfg.min_enrolment_age,
        "min_discovery_age": cfg.min_discovery_age,
    }
    if subject is None:
        await subjects.record_discovery_refusal(
            user_ref, outcome="subject_unknown", metadata=ages
        )
        log.warning("search.refused", user_ref=str(user_ref), outcome="subject_unknown")
        raise ServiceError(
            409,
            "subject_unknown",
            "No subject record for this user_ref. Discovery eligibility is"
            " asserted once at enrolment and cannot be inferred per request.",
            retryable=False,
        )
    if not subject.discovery_eligible:
        await subjects.record_discovery_refusal(
            user_ref,
            outcome="discovery_not_available",
            metadata={"eligibility_reason": subject.eligibility_reason, **ages},
        )
        log.warning(
            "search.refused",
            user_ref=str(user_ref),
            outcome="discovery_not_available",
            eligibility_reason=subject.eligibility_reason,
        )
        raise ServiceError(
            403,
            "discovery_not_available",
            "Discovery is not available for this subject"
            f" ({subject.eligibility_reason}). No search was run.",
            retryable=False,
        )


def _select_providers(
    requested: list[str] | None, enabled: tuple[ProviderId, ...]
) -> tuple[ProviderId, ...]:
    if requested is None:
        return enabled
    selected: list[ProviderId] = []
    for raw in requested:
        try:
            provider_id = parse_provider_id(raw)
        except ValueError:
            provider_id = None
        if provider_id is None or provider_id not in enabled:
            raise ServiceError(
                422,
                "unknown_provider",
                "One of the requested providers is unknown or not enabled.",
                retryable=False,
            )
        if provider_id not in selected:
            selected.append(provider_id)
    return tuple(selected)


@router.get("/search/runs/{run_id}")
async def get_search_run(
    run_id: UUID,
    store: SearchStore = Depends(get_search_store),
) -> SearchRunStatusResponse:
    run = await store.get_run(run_id)
    if run is None:
        raise ServiceError(404, "run_not_found", "No such search run.", retryable=False)
    return SearchRunStatusResponse(
        status=run.status,
        providers_attempted=list(run.providers_attempted),
        providers_succeeded=list(run.providers_succeeded),
        matches_found=run.matches_found,
        scan_tier=run.scan_tier,
        next_scan_after=run.next_scan_after,
    )


@router.get("/search/infringements")
async def list_infringements(
    user_ref: UUID,
    since: datetime | None = Query(default=None),
    store: SearchStore = Depends(get_search_store),
) -> InfringementsResponse:
    """One entry per page found, with every provider that attested to it.

    Absence here means "no matches in monitored sources", never "you're
    safe" — the corpus is a partner-supplied set of known sites, and the
    caller is responsible for saying so (CLAUDE.md §4 #26).
    """
    rows = await store.list_infringements(UserRef(user_ref), since)
    return InfringementsResponse(
        infringements=[
            InfringementItem(
                infringement_id=row.infringement_id,
                page_url=row.page_url,
                # No image_url. A user-facing list read must not carry a direct
                # link to the infringing image — the column stays on the row as
                # evidence, it just does not travel here.
                keyed_on=row.keyed_on,
                first_seen_at=row.first_seen_at,
                last_seen_at=row.last_seen_at,
                seen_count=row.seen_count,
                band=row.band,
                status=row.status,
                band_reason=row.band_reason,
                # Both, so the proxy can say "this came down" honestly: a false
                # url_alive with a stale last_checked_at is not the same claim
                # as one we verified this week.
                url_alive=row.url_alive,
                last_checked_at=row.last_checked_at,
                provider_count=len(row.attestations),
                attestations=[
                    AttestationItem(
                        provider_id=att.provider_id,
                        score_kind=att.score_kind,
                        provider_score=(
                            float(att.provider_score)
                            if att.provider_score is not None
                            else None
                        ),
                        provider_category=att.provider_category,
                        query_quality=att.query_quality,
                        score_version=att.score_version,
                        first_confirmed_at=att.first_confirmed_at,
                        last_confirmed_at=att.last_confirmed_at,
                        confirm_count=att.confirm_count,
                        band=att.band,
                        calibration_version=att.calibration_version,
                    )
                    for att in row.attestations
                ],
            )
            for row in rows
        ]
    )
