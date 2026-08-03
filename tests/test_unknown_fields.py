from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import SERVICE_TOKEN


def test_unknown_field_returns_422(client: TestClient) -> None:
    response = client.post(
        "/v1/ping",
        headers={"X-Service-Token": SERVICE_TOKEN},
        json={"message": "hello", "phone_number": "+1 555 000 1111"},
    )
    assert response.status_code == 422


def test_known_fields_accepted(client: TestClient) -> None:
    response = client.post(
        "/v1/ping",
        headers={"X-Service-Token": SERVICE_TOKEN},
        json={"message": "hello"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "message": "hello"}
