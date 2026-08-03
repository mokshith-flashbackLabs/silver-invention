"""Structured logging setup.

Adapted from the Flashback agent service (AgentMeeMaw src/flashback/http/logging.py)
with two additions this repo requires:

- a per-request ``request_id`` bound to the structlog context (honouring a
  well-formed ``X-Request-Id`` from the proxy so one request can be traced
  across both repos, echoed back on the response);
- the phone-shape redaction processor, which runs immediately before the JSON
  renderer so nothing escapes it (build spec Phase 1 §6).
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from fastapi import FastAPI, Request, Response

from imageshield.redaction import redact_phone_shapes

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def configure_logging() -> None:
    """One-time process-wide structlog configuration. JSON renderer so log
    aggregation is uniform across this service and the proxy."""
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            timestamper,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            redact_phone_shapes,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )


def install_request_logging_middleware(app: FastAPI) -> None:
    """Bind ``request_id``/method/path to the log context; one line per request.

    Only ``request.url.path`` is logged — never the query string or body,
    which could carry identifiers we must not persist in logs.
    """

    log = structlog.get_logger("imageshield.http")

    @app.middleware("http")
    async def _bind_and_log(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        incoming = request.headers.get("x-request-id")
        if incoming is not None and _REQUEST_ID_RE.match(incoming):
            request_id = incoming
        else:
            request_id = str(uuid.uuid4())
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception(
                "request.failed",
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
            raise
        response.headers["X-Request-Id"] = request_id
        log.info(
            "request.completed",
            status=response.status_code,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        return response
