"""HTTP error handlers for FastAPI (ADR-023)."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class LLMUnavailableError(Exception):
    """Raised when an LLM call fails or times out (maps to HTTP 503)."""


def register_error_handlers(app: FastAPI) -> None:
    """Register exception handlers on FastAPI app.

    Maps domain exceptions to HTTP status codes and error response bodies.
    """

    @app.exception_handler(LLMUnavailableError)
    def llm_unavailable_handler(
        request: Request, exc: LLMUnavailableError
    ) -> JSONResponse:
        """Handle LLM failures → 503 Service Unavailable."""
        return JSONResponse(
            status_code=503,
            content={"error_type": "llm_unavailable", "detail": str(exc)},
        )

    @app.exception_handler(ValueError)
    def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
        """Handle validation errors → 400 Bad Request."""
        return JSONResponse(
            status_code=400,
            content={"error_type": "bad_request", "detail": str(exc)},
        )

    @app.exception_handler(Exception)
    def general_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Handle unhandled exceptions → 500 Internal Server Error."""
        return JSONResponse(
            status_code=500,
            content={
                "error_type": "extraction_error",
                "detail": "Internal server error",
            },
        )
