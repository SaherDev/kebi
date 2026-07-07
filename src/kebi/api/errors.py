"""HTTP error handlers for FastAPI (ADR-023).

Error responses are intentionally opaque: the raw exception message is
logged server-side but never returned to the client. That avoids
information-disclosure paths where asyncpg / SQLAlchemy / extraction
internals (table names, schema fragments, place ids found by probe
attacks) leak through exception text. Each response carries a
`request_id` so support / oncall can correlate the user-visible failure
with the server log line.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)


class LLMUnavailableError(Exception):
    """Raised when an LLM call fails or times out (maps to HTTP 503)."""


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Stamp every request with a uuid4 on `request.state.request_id`.

    The id is returned in error bodies and the `X-Request-Id` response
    header so users can quote it back to support.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request.state.request_id = uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        return response


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


def register_error_handlers(app: FastAPI) -> None:
    """Register exception handlers on FastAPI app.

    Maps domain exceptions to HTTP status codes and OPAQUE error
    response bodies. The raw exception is logged server-side; clients
    only ever see a stable `detail` token plus a `request_id` for
    correlation.
    """

    app.add_middleware(RequestIdMiddleware)

    def _error_response(
        status: int, error_type: str, detail: str, request: Request
    ) -> JSONResponse:
        rid = _request_id(request)
        return JSONResponse(
            status_code=status,
            content={"error_type": error_type, "detail": detail, "request_id": rid},
            # Set the header here too. BaseHTTPMiddleware doesn't always
            # re-run its tail when an exception_handler short-circuits
            # the response, so we stamp the header at the source.
            headers={"X-Request-Id": rid},
        )

    @app.exception_handler(LLMUnavailableError)
    def llm_unavailable_handler(
        request: Request, exc: LLMUnavailableError
    ) -> JSONResponse:
        logger.warning("llm_unavailable request_id=%s: %s", _request_id(request), exc)
        return _error_response(503, "llm_unavailable", "service_unavailable", request)

    @app.exception_handler(ValueError)
    def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
        logger.warning("bad_request request_id=%s: %s", _request_id(request), exc)
        return _error_response(400, "bad_request", "invalid_request", request)

    @app.exception_handler(Exception)
    def general_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("internal_error request_id=%s", _request_id(request))
        return _error_response(500, "internal_error", "internal_error", request)
