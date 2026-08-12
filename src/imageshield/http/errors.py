"""The proxy-facing error envelope (PROXY_INTEGRATION.md §3).

Every feature-endpoint error is a :class:`ServiceError` rendered as::

    {"error": {"code", "message", "retryable", "request_id"}}

``code`` is the stable machine-readable string the proxy maps to client copy;
``message`` is for proxy-side operators and is never forwarded verbatim to a
client. Standard HTTP status codes only — no ad-hoc 600/700 signals.

**401 and 422 are now in the envelope too.** They used to keep FastAPI's default
shape, on the reasoning that they predate the envelope and the proxy treats them
generically. The proxy team found the hole by reading our source: a consumer
parsing ``error.code`` unconditionally reads an empty string on exactly the two
responses it is most likely to hit while wiring up a new integration. Bringing
them in is a handler and an exception hook, which is cheaper than documenting a
permanent exception nobody remembers.

The ``HTTPException`` hook is deliberately not narrowed to 401. Starlette's own
404 (no route) and 405 (wrong method) come through the same exception, and a
proxy that must special-case *those* has the same problem in a less obvious
place.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ServiceError(Exception):
    def __init__(self, status_code: int, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable


# Stable codes for the errors the framework raises rather than our routes. Kept
# as a table so a status added later gets a code by decision, not by whatever
# ``_STATUS_CODES.get`` happened to fall back to.
_FRAMEWORK_CODES: dict[int, str] = {
    401: "unauthorised",
    404: "not_found",
    405: "method_not_allowed",
}


def _envelope(
    status_code: int, code: str, message: str, *, retryable: bool, **extra: Any
) -> JSONResponse:
    # request_id is bound by the request-logging middleware, so it is present
    # for anything that got past the ASGI entry point.
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "retryable": retryable,
        "request_id": request_id,
    }
    error.update(extra)
    return JSONResponse(status_code=status_code, content={"error": error})


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ServiceError)
    async def _service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
        return _envelope(exc.status_code, exc.code, exc.message, retryable=exc.retryable)

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code = _FRAMEWORK_CODES.get(exc.status_code, f"http_{exc.status_code}")
        # 5xx from this path is retryable; a 4xx here is a caller mistake and
        # retrying it unchanged cannot succeed.
        return _envelope(
            exc.status_code,
            code,
            str(exc.detail),
            retryable=exc.status_code >= 500,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # `loc` and `msg` ONLY. FastAPI's default 422 echoes the offending
        # `input` back, which for this service means a value the proxy sent us —
        # potentially the phone number that ``extra='forbid'`` exists to reject —
        # reappearing in the proxy's error logs. `details` is enough to fix a
        # malformed request without repeating its contents.
        details = [
            {"loc": ".".join(str(part) for part in error["loc"]), "msg": error["msg"]}
            for error in exc.errors()
        ]
        return _envelope(
            422,
            "validation_error",
            "Request body or parameters failed validation.",
            retryable=False,
            details=details,
        )
