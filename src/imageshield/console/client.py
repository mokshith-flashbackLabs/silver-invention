"""The console's one upstream client: the services admin API.

Every console write flows through this -- the console holds NO database
access of any kind (module docstring, ``imageshield.console``), and since
spec 2026-08-21 §0.2 it holds no fetcher client either: staff never see hit
imagery, so there is nothing for the console to render pixels with. The class
takes an injected ``httpx.AsyncClient`` rather than building its own, so
tests can swap in a ``httpx.MockTransport`` (or a fake object entirely, via
``app.state.services_client``) without a network.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx


class ConsoleUpstreamError(Exception):
    """Raised when an upstream call returns an unexpected status.

    Deliberately small: this console has one purpose (render a form, submit
    it, show the result), not a general-purpose API client. It carries the
    upstream status and body text so an error page can say what failed
    without ever holding or echoing a token.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"upstream error {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class ServicesClient:
    """Talks to the services admin API. Both tokens are attached to every
    request; the admin routes require both (``http/auth.py``)."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        service_token: str,
        admin_service_token: str,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._headers = {
            "X-Service-Token": service_token,
            "X-Admin-Service-Token": admin_service_token,
        }

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code >= 400:
            raise ConsoleUpstreamError(response.status_code, response.text)

    async def provider_health(self) -> dict[str, Any]:
        response = await self._client.get(
            f"{self._base_url}/v1/admin/providers/health", headers=self._headers
        )
        self._raise_for_status(response)
        data: dict[str, Any] = response.json()
        return data

    async def review_next(self) -> dict[str, Any] | None:
        response = await self._client.get(
            f"{self._base_url}/v1/admin/review/next", headers=self._headers
        )
        if response.status_code == 204:
            return None
        self._raise_for_status(response)
        data: dict[str, Any] = response.json()
        return data

    async def review_queue(self) -> dict[str, Any]:
        response = await self._client.get(
            f"{self._base_url}/v1/admin/review/queue", headers=self._headers
        )
        self._raise_for_status(response)
        data: dict[str, Any] = response.json()
        return data

    async def decide(
        self, task_id: UUID, *, decision: str, operator: str, severity: str | None
    ) -> None:
        body: dict[str, Any] = {"decision": decision, "operator": operator}
        if severity is not None:
            body["severity"] = severity
        response = await self._client.post(
            f"{self._base_url}/v1/admin/review/{task_id}/decision",
            json=body,
            headers=self._headers,
        )
        self._raise_for_status(response)

    async def list_events(self) -> list[dict[str, Any]]:
        response = await self._client.get(
            f"{self._base_url}/v1/admin/threat-events", headers=self._headers
        )
        self._raise_for_status(response)
        data: dict[str, Any] = response.json()
        events: list[dict[str, Any]] = list(data.get("events", []))
        return events

    async def create_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(
            f"{self._base_url}/v1/admin/threat-events",
            json=payload,
            headers=self._headers,
        )
        self._raise_for_status(response)
        data: dict[str, Any] = response.json()
        return data

    async def retract_event(self, event_id: UUID, *, operator: str, reason: str) -> None:
        response = await self._client.post(
            f"{self._base_url}/v1/admin/threat-events/{event_id}/retract",
            json={"operator": operator, "reason": reason},
            headers=self._headers,
        )
        self._raise_for_status(response)

    async def score(self, user_ref: str) -> dict[str, Any] | None:
        response = await self._client.get(
            f"{self._base_url}/v1/admin/scores/{user_ref}", headers=self._headers
        )
        if response.status_code == 404:
            return None
        self._raise_for_status(response)
        data: dict[str, Any] = response.json()
        return data

    async def subject_decisions(self, *, limit: int = 50) -> list[dict[str, Any]]:
        response = await self._client.get(
            f"{self._base_url}/v1/admin/review/subject-decisions",
            params={"limit": limit},
            headers=self._headers,
        )
        self._raise_for_status(response)
        data: dict[str, Any] = response.json()
        return list(data.get("decisions", []))

    async def open_hits(self, *, limit: int = 50) -> list[dict[str, Any]]:
        response = await self._client.get(
            f"{self._base_url}/v1/admin/review/open-hits",
            params={"limit": limit},
            headers=self._headers,
        )
        self._raise_for_status(response)
        data: dict[str, Any] = response.json()
        return list(data.get("hits", []))
