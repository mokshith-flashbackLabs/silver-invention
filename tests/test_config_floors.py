"""``GET /v1/config/floors`` — the floors both repos carry.

The one property worth testing is not the values. It is that the response
**follows config**: the whole point of the endpoint is that the proxy asserts
against it at boot and refuses to start on a mismatch, and an endpoint serving
its own constants would pass that assertion while the real floor had moved.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from imageshield.http.app import create_app
from tests.conftest import SERVICE_TOKEN, make_config


def test_floors_are_published(client: TestClient) -> None:
    response = client.get("/v1/config/floors", headers={"X-Service-Token": SERVICE_TOKEN})

    assert response.status_code == 200
    assert response.json() == {
        "min_discovery_age": 18,
        "min_enrolment_age": 13,
        "attribution_max_candidates": 5,
        # Decimal as a STRING, matching GET /v1/admin/providers/health. The
        # proxy compares this for equality and a float round-trip is exactly the
        # drift the NUMERIC columns exist to avoid.
        "attribution_match_threshold": "92.00",
    }


def test_the_response_follows_config_and_not_a_constant(client: TestClient) -> None:
    """Done-when: assert by changing config and seeing the response change.

    A second copy of each number declared in the route would satisfy every other
    assertion in this file and start lying the moment somebody edited one and not
    the other — which is the divergence this endpoint exists to catch.
    """
    moved = create_app(
        config=make_config(
            min_discovery_age=21,
            min_enrolment_age=16,
            attribution_max_candidates=9,
            attribution_match_threshold=88.5,
        )
    )

    body = TestClient(moved).get(
        "/v1/config/floors", headers={"X-Service-Token": SERVICE_TOKEN}
    ).json()

    assert body == {
        "min_discovery_age": 21,
        "min_enrolment_age": 16,
        "attribution_max_candidates": 9,
        "attribution_match_threshold": "88.50",
    }


def test_floors_require_the_service_token(client: TestClient) -> None:
    assert client.get("/v1/config/floors").status_code == 401
