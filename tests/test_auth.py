from __future__ import annotations

from fastapi.testclient import TestClient

from imageshield.http.app import create_app
from tests.conftest import ADMIN_SERVICE_TOKEN, SERVICE_TOKEN, make_config


def test_no_token_gets_401(client: TestClient) -> None:
    assert client.get("/v1/ping").status_code == 401


def test_wrong_token_gets_401(client: TestClient) -> None:
    response = client.get("/v1/ping", headers={"X-Service-Token": "wrong-token-000000000"})
    assert response.status_code == 401


def test_right_token_gets_200(client: TestClient) -> None:
    response = client.get("/v1/ping", headers={"X-Service-Token": SERVICE_TOKEN})
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_admin_route_needs_both_tokens(client: TestClient) -> None:
    assert client.get("/admin/ping").status_code == 401
    assert (
        client.get("/admin/ping", headers={"X-Service-Token": SERVICE_TOKEN}).status_code == 401
    )
    # Admin token alone is not enough either — admin is *in addition to* service.
    assert (
        client.get(
            "/admin/ping", headers={"X-Admin-Service-Token": ADMIN_SERVICE_TOKEN}
        ).status_code
        == 401
    )
    response = client.get(
        "/admin/ping",
        headers={
            "X-Service-Token": SERVICE_TOKEN,
            "X-Admin-Service-Token": ADMIN_SERVICE_TOKEN,
        },
    )
    assert response.status_code == 200


def test_service_token_is_not_valid_as_admin_token(client: TestClient) -> None:
    response = client.get(
        "/admin/ping",
        headers={
            "X-Service-Token": SERVICE_TOKEN,
            "X-Admin-Service-Token": SERVICE_TOKEN,
        },
    )
    assert response.status_code == 401


def test_health_needs_no_token(client: TestClient) -> None:
    assert client.get("/health").status_code == 200


def test_bypass_works_only_in_development() -> None:
    dev_app = create_app(
        config=make_config(environment="development", service_token_auth_disabled=True)
    )
    assert TestClient(dev_app).get("/v1/ping").status_code == 200

    prod_app = create_app(
        config=make_config(environment="production", service_token_auth_disabled=True)
    )
    assert TestClient(prod_app).get("/v1/ping").status_code == 401


def test_docs_are_disabled(client: TestClient) -> None:
    # No public ingress and no human callers — the OpenAPI surface must be off.
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
