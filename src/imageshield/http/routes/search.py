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

from imageshield.http.auth import require_service_token
from imageshield.http.deps import get_search_store
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
from imageshield.search.store import SearchStore
from imageshield.types import ProviderId, UserRef, parse_provider_id

log = structlog.get_logger("imageshield.search")

router = APIRouter(prefix="/v1", dependencies=[Depends(require_service_token)])


@router.post("/seeds", status_code=201)
async def create_seed(
    body: SeedCreateRequest,
    store: SearchStore = Depends(get_search_store),
) -> SeedCreateResponse:
    seed_id = await store.create_seed(body.user_ref, body.seed_kind, body.source_object_uri)
    log.info("search.seed_created", seed_id=str(seed_id), seed_kind=body.seed_kind)
    return SeedCreateResponse(seed_id=seed_id)


@router.post("/search", status_code=202)
async def create_search(
    body: SearchCreateRequest,
    store: SearchStore = Depends(get_search_store),
) -> SearchCreateResponse:
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

    run_id = await store.create_run(body.user_ref, body.seed_id, selected)
    log.info(
        "search.run_enqueued",
        run_id=str(run_id),
        seed_id=str(body.seed_id),
        providers=[str(p) for p in selected],
    )
    return SearchCreateResponse(run_id=run_id)


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
                image_url=row.image_url,
                keyed_on=row.keyed_on,
                first_seen_at=row.first_seen_at,
                last_seen_at=row.last_seen_at,
                seen_count=row.seen_count,
                band=row.band,
                status=row.status,
                band_reason=row.band_reason,
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
