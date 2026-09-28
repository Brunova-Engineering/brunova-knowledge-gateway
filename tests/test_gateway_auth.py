import secrets
from unittest.mock import Mock

from fastapi.testclient import TestClient

from app.auth.gateway_auth import GatewayTokenAuthenticator
from app.main import app, get_source_registry
from app.source_registry import SourceRegistry, SourceRegistryDocument


def empty_registry() -> SourceRegistry:
    return SourceRegistry(SourceRegistryDocument(version=1, sources=()))


def test_authenticator_distinguishes_missing_invalid_and_valid_tokens():
    token = secrets.token_urlsafe(32)
    authenticator = GatewayTokenAuthenticator(token)

    assert authenticator.authenticate(None) == "missing_authentication"
    assert authenticator.authenticate("Basic ignored") == "invalid_authentication"
    assert authenticator.authenticate("Bearer incorrect") == "invalid_authentication"
    assert authenticator.authenticate(f"Bearer {token}") is None
    assert "_expected_token" not in repr(authenticator)


def test_gateway_rejects_missing_and_invalid_authentication(ephemeral_gateway_token):
    client = TestClient(app)
    missing = client.get("/sources")
    invalid = client.get(
        "/sources",
        headers={"Authorization": "Bearer invalid-test-value"},
    )

    assert missing.status_code == 401
    assert missing.json() == {"error": "missing_authentication"}
    assert missing.headers["WWW-Authenticate"] == "Bearer"
    assert invalid.status_code == 401
    assert invalid.json() == {"error": "invalid_authentication"}


def test_gateway_accepts_valid_authentication(ephemeral_gateway_token):
    app.dependency_overrides[get_source_registry] = empty_registry
    try:
        response = TestClient(app).get(
            "/sources",
            headers={"Authorization": f"Bearer {ephemeral_gateway_token}"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == []


def test_gateway_fails_closed_when_authentication_is_not_configured(monkeypatch):
    monkeypatch.delenv("BRUNOVA_GATEWAY_TOKEN", raising=False)

    response = TestClient(app).get(
        "/sources",
        headers={"Authorization": "Bearer non-production-test-value"},
    )

    assert response.status_code == 503
    assert response.json() == {"error": "authentication_unavailable"}


def test_mcp_transport_is_protected(ephemeral_gateway_token):
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0"},
        },
    }
    mcp_headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    with TestClient(app, base_url="http://localhost:8080") as client:
        rejected = client.post("/mcp/", json=initialize, headers=mcp_headers)
        accepted = client.post(
            "/mcp/",
            json=initialize,
            headers={
                **mcp_headers,
                "Authorization": f"Bearer {ephemeral_gateway_token}",
            },
        )

    assert rejected.status_code == 401
    assert rejected.json() == {"error": "missing_authentication"}
    assert accepted.status_code == 200
    assert accepted.json()["result"]["serverInfo"]["version"] == "0.30.1"


def test_authentication_audit_never_receives_authorization_value(
    monkeypatch,
    ephemeral_gateway_token,
):
    audit = Mock()
    monkeypatch.setattr("app.middleware.authentication.emit_audit_record", audit)
    app.dependency_overrides[get_source_registry] = empty_registry
    try:
        response = TestClient(app).get(
            "/sources",
            headers={"Authorization": f"Bearer {ephemeral_gateway_token}"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert audit.call_args.kwargs["action"] == "authentication"
    assert audit.call_args.kwargs["result"] == "success"
    assert audit.call_args.kwargs["consumer"] == "api_client"
    assert "authorization" not in audit.call_args.kwargs
