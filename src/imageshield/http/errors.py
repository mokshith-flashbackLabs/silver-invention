"""The proxy-facing error envelope (PROXY_INTEGRATION.md §3).

Every feature-endpoint error is a :class:`ServiceError` rendered as::

    {"error": {"code", "message", "retryable", "request_id"}}

``code`` is the stable machine-readable string the proxy maps to client copy;
``message`` is for proxy-side operators and is never forwarded verbatim to a
client. Standard HTTP status codes only — no ad-hoc 600/700 signals.

Auth (401) and pydantic validation (422) keep FastAPI's default shape; they
predate this envelope and the proxy treats them generically.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class ServiceError(Exception):
    def __init__(self, status_code: int, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ServiceError)
    async def _service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
        # Bound by the request-logging middleware; present for every request
        # that reaches a route.
        request_id = structlog.contextvars.get_contextvars().get("request_id")
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "retryable": exc.retryable,
                    "request_id": request_id,
                }
            },
        )
