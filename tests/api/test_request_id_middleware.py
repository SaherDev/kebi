"""Verify that the request-id middleware stamps every response and
that error bodies carry the same id.

Used by support / oncall to correlate a user-visible failure with a
specific server log line without leaking exception text.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kebi.api.errors import LLMUnavailableError, register_error_handlers


def _app_with_handlers() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/ok")
    def _ok() -> dict[str, str]:
        return {"hello": "world"}

    @app.get("/boom")
    def _boom() -> None:
        raise RuntimeError("internal detail that must not leak")

    @app.get("/llm-down")
    def _llm() -> None:
        raise LLMUnavailableError("anthropic 503")

    @app.get("/bad")
    def _bad() -> None:
        raise ValueError("internal validator detail")

    return app


def test_success_response_includes_x_request_id_header() -> None:
    client = TestClient(_app_with_handlers())
    response = client.get("/ok")
    assert response.status_code == 200
    rid = response.headers.get("X-Request-Id")
    assert rid is not None
    assert len(rid) == 32  # uuid4 hex


def test_request_ids_are_unique_per_request() -> None:
    client = TestClient(_app_with_handlers())
    r1 = client.get("/ok").headers["X-Request-Id"]
    r2 = client.get("/ok").headers["X-Request-Id"]
    assert r1 != r2


def test_unhandled_exception_returns_opaque_500() -> None:
    client = TestClient(_app_with_handlers(), raise_server_exceptions=False)
    response = client.get("/boom")
    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "internal_error"
    assert "internal detail that must not leak" not in str(body)
    assert body["request_id"] == response.headers["X-Request-Id"]


def test_value_error_returns_opaque_400() -> None:
    client = TestClient(_app_with_handlers(), raise_server_exceptions=False)
    response = client.get("/bad")
    assert response.status_code == 400
    body = response.json()
    assert body["detail"] == "invalid_request"
    assert "internal validator detail" not in str(body)
    assert body["request_id"] == response.headers["X-Request-Id"]


def test_llm_unavailable_returns_opaque_503() -> None:
    client = TestClient(_app_with_handlers(), raise_server_exceptions=False)
    response = client.get("/llm-down")
    assert response.status_code == 503
    body = response.json()
    assert body["detail"] == "service_unavailable"
    assert "anthropic" not in str(body).lower()
    assert body["request_id"] == response.headers["X-Request-Id"]
