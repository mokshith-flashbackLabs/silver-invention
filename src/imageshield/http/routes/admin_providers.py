"""Provider kill switches and the observability rollup (step 8).

During an incident — a provider returning garbage, a billing surprise, a vendor
breach — you need to stop calling it in **seconds**, from a console, without
shipping code. That is what these routes are. Everything they change is a column
on ``providers``, re-read by every process within
``PROVIDER_CONFIG_CACHE_SECONDS`` (capped at 30s), so no deploy and no restart
is involved.

Every route here carries ``require_admin_service_token`` *in addition to*
``require_service_token`` at router level, so a route added to this file is
guarded structurally rather than by remembering to decorate it.

Path note: mounted under ``/v1/admin/providers``, per the step-8 brief and
matching ``POST /v1/admin/backfill``, which PROXY_INTEGRATION.md has specified
since before this step. The whole admin surface moved onto that prefix at the
same time — ``/admin/ping`` became ``/v1/admin/ping`` — because two admin
prefixes is the state where step 9's route-auth CI gate has to know about both
and quietly covers one.

Every write lands an ``audit_log`` row in the same transaction as the change
(``providers/store.py``). A kill switch flipped with no audit row is an incident
timeline nobody can reconstruct.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends

from imageshield.config import Config
from imageshield.http.auth import require_admin_service_token, require_service_token
from imageshield.http.deps import (
    get_config,
    get_provider_control_store,
    get_provider_observability,
)
from imageshield.http.errors import ServiceError
from imageshield.http.models import (
    ProviderAdminResponse,
    ProviderDisableRequest,
    ProviderHealthItem,
    ProviderHealthResponse,
    ProviderReasonRequest,
)
from imageshield.providers.observability import ProviderObservability, alarms
from imageshield.providers.store import ProviderControlStore
from imageshield.types import ProviderId, parse_provider_id

log = structlog.get_logger("imageshield.providers")

router = APIRouter(
    prefix="/v1/admin/providers",
    dependencies=[Depends(require_service_token), Depends(require_admin_service_token)],
)

# The audit trail records who asked. There is no per-user auth on this service
# (CLAUDE.md §3.1), so the honest answer is "whoever holds the admin token" —
# recorded as a constant rather than as a name we would be inventing.
_ACTOR = "admin_service_token"


def _provider_id(raw: str) -> ProviderId:
    try:
        return parse_provider_id(raw)
    except ValueError as exc:
        raise ServiceError(
            422, "invalid_provider_id", str(exc), retryable=False
        ) from None


def _not_found(provider_id: ProviderId) -> ServiceError:
    return ServiceError(
        404, "provider_not_found", f"No provider row for {provider_id!r}.", retryable=False
    )


@router.post("/{provider_id}/disable")
async def disable_provider(
    provider_id: str,
    body: ProviderDisableRequest,
    store: ProviderControlStore = Depends(get_provider_control_store),
) -> ProviderAdminResponse:
    """The kill switch. No dispatch, no API call, no cost, no deploy."""
    pid = _provider_id(provider_id)
    if not await store.set_enabled(pid, False, actor=_ACTOR, reason=body.reason):
        raise _not_found(pid)
    return ProviderAdminResponse(provider_id=pid, enabled=False)


@router.post("/{provider_id}/enable")
async def enable_provider(
    provider_id: str,
    body: ProviderReasonRequest,
    store: ProviderControlStore = Depends(get_provider_control_store),
) -> ProviderAdminResponse:
    pid = _provider_id(provider_id)
    if not await store.set_enabled(pid, True, actor=_ACTOR, reason=body.reason):
        raise _not_found(pid)
    return ProviderAdminResponse(provider_id=pid, enabled=True)


@router.post("/{provider_id}/breaker/reset")
async def reset_breaker(
    provider_id: str,
    body: ProviderReasonRequest,
    store: ProviderControlStore = Depends(get_provider_control_store),
) -> ProviderAdminResponse:
    """Force the breaker closed and clear the failure counter.

    Separate from ``enable``: "this provider is fixed, let it back in now
    rather than after the cooldown" and "this provider should be receiving
    traffic at all" are different decisions, and conflating them means an
    operator who wanted the first silently makes the second.
    """
    pid = _provider_id(provider_id)
    if not await store.reset_breaker(pid, actor=_ACTOR, reason=body.reason):
        raise _not_found(pid)
    return ProviderAdminResponse(provider_id=pid, breaker_state="closed")


@router.get("/health")
async def provider_health(
    cfg: Config = Depends(get_config),
    store: ProviderControlStore = Depends(get_provider_control_store),
    observability: ProviderObservability = Depends(get_provider_observability),
) -> ProviderHealthResponse:
    """Per-provider, per-day: calls, cost, success rate, p50/p99 latency,
    breaker state, budget headroom — plus every alarm currently firing.

    The alarm that matters most is ``no_successful_calls_24h``. A provider
    silently returning nothing looks exactly like a quiet week for
    infringements, and in a safety product an undetected outage means users are
    told they are clear when nothing actually looked.
    """
    now = datetime.now(UTC)
    runtimes = await store.runtimes()
    items: list[ProviderHealthItem] = []
    firing: list[str] = []
    for runtime in runtimes.values():
        stats = await observability.daily_stats(
            runtime, now=now, window_hours=cfg.provider_alarm_window_hours
        )
        provider_alarms = alarms(
            stats,
            spend_alarm_fraction=cfg.provider_spend_alarm_fraction,
            success_rate_alarm=cfg.provider_success_rate_alarm,
        )
        firing.extend(f"{a.provider_id}:{a.kind}" for a in provider_alarms)
        items.append(
            ProviderHealthItem(
                provider_id=stats.provider_id,
                enabled=stats.enabled,
                breaker_state=stats.breaker_state,
                breaker_reason=stats.breaker_reason,
                call_count=stats.call_count,
                cost_usd=str(stats.cost_usd),
                daily_budget_usd=(
                    str(stats.daily_budget_usd)
                    if stats.daily_budget_usd is not None
                    else None
                ),
                monthly_budget_usd=(
                    str(stats.monthly_budget_usd)
                    if stats.monthly_budget_usd is not None
                    else None
                ),
                month_to_date_cost_usd=str(stats.month_to_date_cost_usd),
                budget_headroom_usd=(
                    str(stats.budget_headroom_usd)
                    if stats.budget_headroom_usd is not None
                    else None
                ),
                success_rate=stats.success_rate,
                window_call_count=stats.window_call_count,
                successful_calls_24h=stats.successful_calls_24h,
                latency_p50_ms=stats.latency_p50_ms,
                latency_p99_ms=stats.latency_p99_ms,
                alarms=[
                    {"kind": a.kind, "detail": a.detail} for a in provider_alarms
                ],
            )
        )
    if firing:
        log.warning("provider.alarms_firing", alarms=firing)
    return ProviderHealthResponse(
        as_of=now,
        window_hours=cfg.provider_alarm_window_hours,
        providers=items,
    )
