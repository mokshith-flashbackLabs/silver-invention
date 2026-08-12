"""Every error response carries the envelope — including 401 and 422.

Those two used to keep FastAPI's default shape, on the reasoning that they
predate the envelope and the proxy treats them generically. The proxy team found
the hole by reading our source: a consumer parsing ``error.code``
unconditionally reads an empty string on exactly the two responses it is most
likely to hit while wiring up a new integration.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from imageshield.http.errors import ServiceError, install_error_handlers
from tests.conftest import SERVICE_TOKEN

ENVELOPE_KEYS = {"code", "message", "retryable", "request_id"}


def _error(body: object) -> dict[str, object]:
    assert isinstance(body, dict)
    error = body["error"]
    assert isinstance(error, dict)
    assert set(error) >= ENVELOPE_KEYS
    return error


def test_a_missing_token_is_enveloped(client: TestClient) -> None:
    response = client.get("/v1/ping")

    assert response.status_code == 401
    error = _error(response.json())
    assert error["code"] == "unauthorised"
    assert error["retryable"] is False


def test_a_validation_failure_is_enveloped(client: TestClient) -> None:
    response = client.post(
        "/v1/ping",
        headers={"X-Service-Token": SERVICE_TOKEN},
        json={"message": "hello", "not_a_field": 1},
    )

    assert response.status_code == 422
    error = _error(response.json())
    assert error["code"] == "validation_error"
    assert error["retryable"] is False
    details = error["details"]
    assert isinstance(details, list)
    assert details and {"loc", "msg"} == set(details[0])


def test_a_validation_failure_does_not_echo_the_offending_value(
    client: TestClient,
) -> None:
    """FastAPI's default 422 echoes the rejected ``input`` back. For this service
    that means a value the proxy sent us — potentially the phone number
    ``extra='forbid'`` exists to reject (§3.2) — reappearing in the proxy's error
    logs. ``loc`` and ``msg`` are enough to fix a malformed request."""
    response = client.post(
        "/v1/ping",
        headers={"X-Service-Token": SERVICE_TOKEN},
        json={"message": "hello", "phone_number": "+15550001111"},
    )

    assert response.status_code == 422
    assert "+15550001111" not in response.text
    # The field NAME is fine and is what makes the error actionable; the value
    # is what must not come back.
    assert "phone_number" in response.text


def test_an_unrouted_path_is_enveloped(client: TestClient) -> None:
    """Starlette's own 404 comes through the same exception. A proxy that has to
    special-case it has the same problem in a less obvious place."""
    response = client.get("/v1/no-such-thing", headers={"X-Service-Token": SERVICE_TOKEN})

    assert response.status_code == 404
    assert _error(response.json())["code"] == "not_found"


def test_a_service_error_still_carries_its_own_code() -> None:
    """The refactor must not have flattened ServiceError's codes into the
    framework table. A minimal app rather than a real route: what is under test
    is the handler, and every real route needs half the app.state wiring."""
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/boom")
    async def _boom() -> None:
        raise ServiceError(409, "subject_unknown", "no such subject", retryable=False)

    response = TestClient(app, raise_server_exceptions=False).get("/boom")

    assert response.status_code == 409
    error = _error(response.json())
    assert error["code"] == "subject_unknown"
    assert error["message"] == "no such subject"
    assert error["retryable"] is False
