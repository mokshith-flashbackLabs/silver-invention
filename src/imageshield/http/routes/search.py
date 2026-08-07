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
    SearchCreateRequest,
    SearchCreateResponse,
    SearchMatchesResponse,
    SearchMatchItem,
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


@router.get("/search/matches")
async def list_search_matches(
    user_ref: UUID,
    since: datetime | None = Query(default=None),
    store: SearchStore = Depends(get_search_store),
) -> SearchMatchesResponse:
    rows = await store.list_matches(UserRef(user_ref), since)
    return SearchMatchesResponse(
        matches=[
            SearchMatchItem(
                match_id=row.match_id,
                run_id=row.run_id,
                provider_id=row.provider_id,
                image_url=row.image_url,
                page_url=row.page_url,
                score_kind=row.score_kind,
                provider_score=(
                    float(row.provider_score) if row.provider_score is not None else None
                ),
                provider_category=row.provider_category,
                query_quality=row.query_quality,
                band=row.band,
                created_at=row.created_at,
            )
            for row in rows
        ]
    )
