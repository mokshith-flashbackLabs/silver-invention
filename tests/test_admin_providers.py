"""Admin kill switches and the observability rollup — route behaviour over
in-memory fakes (repo convention: TestClient never runs the lifespan).

The auth assertion is the load-bearing one. These routes can stop a provider
mid-incident and can expose spend; both tokens are required, and the router
carries the dependencies at router level so a route added to the file is guarded
structurally rather than by remembering to decorate it.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi.testclient import TestClient

from imageshield.http.app import create_app
from imageshield.providers.models import ProviderDailyStats, ProviderRuntime
from imageshield.types import ProviderId
from tests.conftest import ADMIN_SERVICE_TOKEN, SERVICE_TOKEN, make_config
from tests.providers_fakes import GOOGLE, HIVE, FakeControlStore, runtime

AUTH = {"X-Service-Token": SERVICE_TOKEN}
ADMIN = {**AUTH, "X-Admin-Service-Token": ADMIN_SERVICE_TOKEN}


class FakeObservability:
    def __init__(self, stats: dict[ProviderId, ProviderDailyStats]) -> None:
        self.stats = stats

    async def daily_stats(
        self, runtime_row: ProviderRuntime, *, now: datetime, window_hours: int
    ) -> ProviderDailyStats:
        return self.stats[runtime_row.provider_id]


def _stats(
    provider_id: ProviderId,
    *,
    enabled: bool = True,
    breaker_state: str = "closed",
    cost: str = "1.00",
    daily_budget: str | None = "10.00",
    success_rate: float | None = 1.0,
    window_calls: int = 10,
    successful_24h: int = 10,
) -> ProviderDailyStats:
    budget = Decimal(daily_budget) if daily_budget is not None else None
    return ProviderDailyStats(
        provider_id=provider_id,
        enabled=enabled,
        breaker_state=breaker_state,  # type: ignore[arg-type]
        breaker_reason="timeout" if breaker_state != "closed" else None,
        call_count=window_calls,
        cost_usd=Decimal(cost),
        daily_budget_usd=budget,
        monthly_budget_usd=None,
        month_to_date_cost_usd=Decimal(cost),
        budget_headroom_usd=(budget - Decimal(cost)) if budget is not None else None,
        success_rate=success_rate,
        window_call_count=window_calls,
        successful_calls_24h=successful_24h,
        latency_p50_ms=120,
        latency_p99_ms=980,
    )


def make_client(
    stats: dict[ProviderId, ProviderDailyStats] | None = None,
) -> tuple[TestClient, FakeControlStore]:
    app = create_app(config=make_config())
    control = FakeControlStore({HIVE: runtime(HIVE), GOOGLE: runtime(GOOGLE)})
    app.state.provider_control_store = control
    app.state.provider_observability = FakeObservability(
        stats if stats is not None else {HIVE: _stats(HIVE), GOOGLE: _stats(GOOGLE)}
    )
    return TestClient(app), control


def test_every_admin_route_needs_both_tokens() -> None:
    client, control = make_client()
    body = {"reason": "incident"}
    writes = [
        "/v1/admin/providers/hive/disable",
        "/v1/admin/providers/hive/enable",
        "/v1/admin/providers/hive/breaker/reset",
    ]
    for path in writes:
        # No tokens at all.
        assert client.post(path, json=body).status_code == 401, path
        # Service token only — the admin token is required IN ADDITION.
        assert client.post(path, json=body, headers=AUTH).status_code == 401, path

    assert client.get("/v1/admin/providers/health").status_code == 401
    assert client.get("/v1/admin/providers/health", headers=AUTH).status_code == 401

    # And nothing was written by any of the rejected calls.
    assert control.enabled_writes == []
    assert control.breaker_resets == []


def test_disable_flips_the_switch_and_carries_the_reason() -> None:
    client, control = make_client()

    response = client.post(
        "/v1/admin/providers/hive/disable",
        json={"reason": "billing surprise on the Hive invoice"},
        headers=ADMIN,
    )

    assert response.status_code == 200
    assert response.json() == {
        "provider_id": "hive",
        "enabled": False,
        "breaker_state": None,
    }
    assert control.enabled_writes == [
        (HIVE, False, "billing surprise on the Hive invoice")
    ]


def test_a_reason_is_mandatory_and_must_be_more_than_a_keystroke() -> None:
    """`enabled = false` with no recorded reason is the state where nobody
    remembers whether the provider is off for a billing surprise, a vendor
    breach, or a test someone forgot to undo."""
    client, control = make_client()

    assert (
        client.post("/v1/admin/providers/hive/disable", json={}, headers=ADMIN).status_code
        == 422
    )
    assert (
        client.post(
            "/v1/admin/providers/hive/disable", json={"reason": "x"}, headers=ADMIN
        ).status_code
        == 422
    )
    assert control.enabled_writes == []


def test_enable_and_breaker_reset_are_separate_endpoints() -> None:
    client, control = make_client()

    client.post(
        "/v1/admin/providers/hive/enable", json={"reason": "vendor confirmed fixed"},
        headers=ADMIN,
    )
    reset = client.post(
        "/v1/admin/providers/hive/breaker/reset",
        json={"reason": "verified by hand, skip the cooldown"},
        headers=ADMIN,
    )

    assert control.enabled_writes == [(HIVE, True, "vendor confirmed fixed")]
    assert control.breaker_resets == [HIVE]
    assert reset.json()["breaker_state"] == "closed"


def test_an_unknown_provider_is_404_and_an_invalid_id_is_422() -> None:
    client, _control = make_client()
    body = {"reason": "incident response"}

    unknown = client.post("/v1/admin/providers/pimeyes/disable", json=body, headers=ADMIN)
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "provider_not_found"

    invalid = client.post("/v1/admin/providers/HIVE!/disable", json=body, headers=ADMIN)
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_provider_id"


def test_health_reports_money_as_strings_and_no_alarms_when_healthy() -> None:
    client, _ = make_client()

    body = client.get("/v1/admin/providers/health", headers=ADMIN).json()

    assert body["window_hours"] == 1
    by_id = {item["provider_id"]: item for item in body["providers"]}
    assert set(by_id) == {"hive", "google"}
    hive = by_id["hive"]
    # Decimal strings, never JSON floats: a float round-trip is exactly the
    # drift the NUMERIC columns exist to avoid.
    assert hive["cost_usd"] == "1.00"
    assert hive["budget_headroom_usd"] == "9.00"
    assert hive["latency_p50_ms"] == 120
    assert hive["latency_p99_ms"] == 980
    assert hive["alarms"] == []


def test_health_surfaces_every_alarm_kind() -> None:
    client, _ = make_client(
        {
            HIVE: _stats(
                HIVE,
                breaker_state="open",
                cost="8.50",  # 85% of a 10.00 budget, past the 80% threshold
                success_rate=0.5,
                successful_24h=0,
            ),
            GOOGLE: _stats(GOOGLE),
        }
    )

    body = client.get("/v1/admin/providers/health", headers=ADMIN).json()

    by_id = {item["provider_id"]: item for item in body["providers"]}
    kinds = {alarm["kind"] for alarm in by_id["hive"]["alarms"]}
    assert kinds == {
        "breaker_open",
        "daily_spend_near_budget",
        "low_success_rate",
        "no_successful_calls_24h",
    }
    assert by_id["google"]["alarms"] == []


def test_a_deliberately_disabled_provider_raises_no_alarms() -> None:
    """Alarming on a deliberate action trains people to ignore alarms. Its
    absence is still visible in `enabled` on the same payload."""
    client, _ = make_client(
        {
            HIVE: _stats(HIVE, enabled=False, breaker_state="open", successful_24h=0),
            GOOGLE: _stats(GOOGLE),
        }
    )

    body = client.get("/v1/admin/providers/health", headers=ADMIN).json()

    hive = next(i for i in body["providers"] if i["provider_id"] == "hive")
    assert hive["enabled"] is False
    assert hive["alarms"] == []


def test_operator_in_the_body_becomes_the_audit_actor() -> None:
    """The console names a person; the audit row should say who, not which
    token was held."""
    client, control = make_client()

    response = client.post(
        "/v1/admin/providers/hive/disable",
        json={"reason": "vendor breach notice", "operator": "alice"},
        headers=ADMIN,
    )

    assert response.status_code == 200
    assert control.actors == ["alice"]
    assert control.enabled_writes == [(HIVE, False, "vendor breach notice")]


def test_an_omitted_operator_still_records_the_token_holder() -> None:
    """curl callers are unchanged: no operator, the fallback actor."""
    client, control = make_client()

    client.post(
        "/v1/admin/providers/hive/breaker/reset",
        json={"reason": "verified by hand"},
        headers=ADMIN,
    )

    assert control.actors == ["admin_service_token"]
