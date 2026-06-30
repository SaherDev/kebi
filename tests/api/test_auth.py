"""Service-to-service auth tests (gateway identity header).

The conftest autouse fixture overrides `require_gateway_identity` for
the global `app`, so these tests build their own FastAPI app and
exercise the real dependency.
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI, status
from fastapi.testclient import TestClient

from kebi.api.deps import GatewayIdentity, require_gateway_identity


@pytest.fixture
def app_with_real_auth(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """A throwaway app that exercises the real `require_gateway_identity`."""
    monkeypatch.setenv("GATEWAY_SHARED_SECRET", "test-gateway-secret-dummy")

    from kebi.core import config as cfg

    cfg._env = None  # type: ignore[attr-defined]

    app = FastAPI()

    @app.get("/v1/protected")
    def protected_v1(
        identity: GatewayIdentity = Depends(require_gateway_identity),  # noqa: B008
    ) -> dict[str, str]:
        return {"user_id": identity.user_id}

    return app


def test_missing_token_returns_validation_error(app_with_real_auth: FastAPI) -> None:
    """Missing required header → FastAPI emits 422 from the Header(...)
    validation layer before the dep body runs."""
    client = TestClient(app_with_real_auth)
    response = client.get("/v1/protected")
    assert response.status_code == 422


def test_wrong_token_returns_401(app_with_real_auth: FastAPI) -> None:
    client = TestClient(app_with_real_auth)
    response = client.get(
        "/v1/protected",
        headers={
            "X-Gateway-Token": "wrong-secret",
            "X-Gateway-User-Id": "user_validClerkId12345678901",
        },
    )
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_malformed_user_id_returns_400(app_with_real_auth: FastAPI) -> None:
    client = TestClient(app_with_real_auth)
    response = client.get(
        "/v1/protected",
        headers={
            "X-Gateway-Token": "test-gateway-secret-dummy",
            "X-Gateway-User-Id": "not-a-clerk-id",
        },
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST


def test_valid_headers_return_identity(app_with_real_auth: FastAPI) -> None:
    client = TestClient(app_with_real_auth)
    response = client.get(
        "/v1/protected",
        headers={
            "X-Gateway-Token": "test-gateway-secret-dummy",
            "X-Gateway-User-Id": "user_validClerkId12345678901",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"user_id": "user_validClerkId12345678901"}


@pytest.fixture
def app_with_entitlements(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Throwaway app exposing the full resolved entitlement set."""
    monkeypatch.setenv("GATEWAY_SHARED_SECRET", "test-gateway-secret-dummy")

    from kebi.core import config as cfg

    cfg._env = None  # type: ignore[attr-defined]

    app = FastAPI()

    @app.get("/v1/entitlements")
    def entitlements(
        identity: GatewayIdentity = Depends(require_gateway_identity),  # noqa: B008
    ) -> dict[str, object]:
        return {
            "taste_enabled": identity.taste_enabled,
            "discovery_enabled": identity.discovery_enabled,
            "save_limit": identity.save_limit,
            "consults_per_day": identity.consults_per_day,
            "advanced_models_enabled": identity.advanced_models_enabled,
        }

    return app


_AUTH = {
    "X-Gateway-Token": "test-gateway-secret-dummy",
    "X-Gateway-User-Id": "user_validClerkId12345678901",
}


def test_entitlements_default_to_restrictive_when_absent(
    app_with_entitlements: FastAPI,
) -> None:
    """Fail closed: missing headers → booleans False, limits None (unlimited)."""
    client = TestClient(app_with_entitlements)
    response = client.get("/v1/entitlements", headers=_AUTH)
    assert response.status_code == 200
    assert response.json() == {
        "taste_enabled": False,
        "discovery_enabled": False,
        "save_limit": None,
        "consults_per_day": None,
        "advanced_models_enabled": False,
    }


def test_entitlements_parse_and_coerce_headers(
    app_with_entitlements: FastAPI,
) -> None:
    client = TestClient(app_with_entitlements)
    response = client.get(
        "/v1/entitlements",
        headers={
            **_AUTH,
            "X-Gateway-Taste-Enabled": "true",
            "X-Gateway-Discovery-Enabled": "false",
            "X-Gateway-Save-Limit": "10",
            "X-Gateway-Consults-Per-Day": "3",
            "X-Gateway-Advanced-Models-Enabled": "1",
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "taste_enabled": True,
        "discovery_enabled": False,
        "save_limit": 10,
        "consults_per_day": 3,
        "advanced_models_enabled": True,
    }
