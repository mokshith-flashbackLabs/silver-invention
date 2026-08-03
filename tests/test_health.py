from __future__ import annotations

from fastapi.testclient import TestClient

from imageshield.config import APP_VERSION, Config
from imageshield.http.app import create_app


def _client_with_db_check(config: Config, *, db_ok: bool) -> TestClient:
    app = create_app(config=config)

    async def check() -> None:
        if not db_ok:
            raise RuntimeError("db unreachable")

    app.state.db_check = check
    return TestClient(app)


def test_health_ok(config: Config) -> None:
    response = _client_with_db_check(config, db_ok=True).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": APP_VERSION, "db": "ok"}


def test_health_degraded_is_still_200_and_terse(config: Config) -> None:
    response = _client_with_db_check(config, db_ok=False).get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body == {"status": "degraded", "version": APP_VERSION, "db": "degraded"}
    # No exception detail, class names, or dependency map may leak.
    assert "unreachable" not in response.text


def test_health_carries_request_id_header(config: Config) -> None:
    response = _client_with_db_check(config, db_ok=True).get("/health")
    assert response.headers.get("x-request-id")
