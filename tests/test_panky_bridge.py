"""Legacy tokens and new short access tokens coexist without fallback."""

import json
import time
import asyncio
from types import SimpleNamespace

import pytest

from app.auth.gateway_auth import GatewayPrincipalAuthenticator
from app.auth.panky_bridge import PankyBridgeError, issue_access_token, verify_access_token
from app.auth.principals import PrincipalResolver
from app.auth.principals import Principal, bind_principal, reset_principal
from app.mcp_server import mcp_server
from mcp.types import CallToolResult, TextContent
from mcp.types import Tool as MCPTool


def test_legacy_management_token_is_unchanged_when_bridge_is_on(monkeypatch):
    monkeypatch.setenv("PANKY_POLICY_MODE", "shadow")
    authenticator = GatewayPrincipalAuthenticator(PrincipalResolver("legacy-secret", ()))
    principal, error = authenticator.authenticate("Bearer legacy-secret")
    assert error is None and principal.type == "management"


def test_new_short_token_binds_verified_google_subject(monkeypatch):
    monkeypatch.setenv("PANKY_POLICY_MODE", "shadow")
    monkeypatch.setenv("PANKY_GATEWAY_SIGNING_KEY", "x" * 40)
    monkeypatch.setenv("PANKY_DESKTOP_OAUTH_CLIENT_ID", "fixture-client")
    monkeypatch.setattr("app.auth.panky_bridge.id_token.verify_oauth2_token",
                        lambda proof, request, audience: {
                            "iss": "https://accounts.google.com",
                            "aud": audience, "sub": "google-subject",
                            "email": "Dev@Example.com", "email_verified": True,
                        })
    token, lifetime = issue_access_token("verified-fixture-proof")
    assert lifetime == 300
    identity = verify_access_token(token)
    assert identity.subject == "google-subject"
    assert identity.email == "dev@example.com"
    authenticator = GatewayPrincipalAuthenticator(PrincipalResolver("legacy-secret", ()))
    principal, error = authenticator.authenticate(f"Bearer {token}")
    assert error is None and principal.type == "panky"
    assert principal.oidc_subject == "google-subject"


def test_tampered_new_token_never_falls_back_to_legacy(monkeypatch):
    monkeypatch.setenv("PANKY_POLICY_MODE", "shadow")
    monkeypatch.setenv("PANKY_GATEWAY_SIGNING_KEY", "x" * 40)
    authenticator = GatewayPrincipalAuthenticator(PrincipalResolver("legacy-secret", ()))
    principal, error = authenticator.authenticate("Bearer a.b.c")
    assert principal is None and error == "invalid_authentication"


def test_expired_token_is_denied(monkeypatch):
    monkeypatch.setenv("PANKY_GATEWAY_SIGNING_KEY", "x" * 40)
    from app.auth import panky_bridge
    header = panky_bridge._b64(b'{"alg":"HS256","typ":"JWT"}')
    body = panky_bridge._b64(json.dumps({
        "iss": "brunova-knowledge-gateway",
        "aud": "brunova-knowledge-gateway/mcp",
        "sub": "google-subject", "email": "dev@example.com",
        "oidc_issuer": "https://accounts.google.com",
        "iat": int(time.time()) - 600,
        "exp": int(time.time()) - 300,
    }).encode())
    with pytest.raises(PankyBridgeError):
        verify_access_token(f"{header}.{body}.{panky_bridge._sign(header, body)}")


def test_short_token_remains_valid_after_first_five_seconds(monkeypatch):
    from app.auth import panky_bridge

    monkeypatch.setenv("PANKY_POLICY_MODE", "shadow")
    monkeypatch.setenv("PANKY_GATEWAY_SIGNING_KEY", "x" * 40)
    monkeypatch.setenv("PANKY_DESKTOP_OAUTH_CLIENT_ID", "fixture-client")
    monkeypatch.setattr("app.auth.panky_bridge.id_token.verify_oauth2_token",
                        lambda proof, request, audience: {
                            "iss": "https://accounts.google.com",
                            "aud": audience, "sub": "google-subject",
                            "email": "dev@example.com", "email_verified": True,
                        })
    monkeypatch.setattr(panky_bridge.time, "time", lambda: 1000)
    token, _ = issue_access_token("verified-fixture-proof")
    monkeypatch.setattr(panky_bridge.time, "time", lambda: 1010)
    assert verify_access_token(token).subject == "google-subject"
    monkeypatch.setattr(panky_bridge.time, "time", lambda: 1300)
    with pytest.raises(PankyBridgeError):
        verify_access_token(token)


def test_policy_denial_stops_tool_before_external_effect(monkeypatch):
    calls = []

    async def effect(*args):
        calls.append(args)
        return CallToolResult(content=[TextContent(type="text", text="done")])

    monkeypatch.setattr(mcp_server, "_call_tool_impl", effect)
    monkeypatch.setattr("app.mcp_server.policy_decision",
                        lambda *args, **kwargs: {"allowed": False, "reason": "default_deny"})
    binding = bind_principal(Principal.panky(subject="fixture-sub", email="dev@example.com"))
    try:
        result = asyncio.run(mcp_server.call_tool("retrieve_document", {"source_id": "other"}))
    finally:
        reset_principal(binding)
    assert result.is_error and not calls


def test_policy_allow_runs_exactly_one_tool(monkeypatch):
    calls = []

    async def effect(*args):
        calls.append(args)
        return CallToolResult(content=[TextContent(type="text", text="done")])

    monkeypatch.setattr(mcp_server, "_call_tool_impl", effect)
    monkeypatch.setattr("app.mcp_server.policy_decision",
                        lambda *args, **kwargs: {"allowed": True, "reason": "grant",
                                                "grant_tool": "*"})
    binding = bind_principal(Principal.panky(subject="fixture-sub", email="dev@example.com"))
    try:
        result = asyncio.run(mcp_server.call_tool("retrieve_document", {"source_id": "hq_client"}))
    finally:
        reset_principal(binding)
    assert not result.is_error and len(calls) == 1


def test_catalog_hides_ungranted_tools(monkeypatch):
    registry = SimpleNamespace(sources=[
        SimpleNamespace(id="hq_client", status=SimpleNamespace(value="active")),
        SimpleNamespace(id="other", status=SimpleNamespace(value="active")),
    ])
    monkeypatch.setattr("app.mcp_server.get_runtime_gateway",
                        lambda: SimpleNamespace(registry=registry))
    monkeypatch.setattr("app.mcp_server.preview_policy_decisions",
                        lambda requests: [{
                            "allowed": request["resource_ref"] == "hq_client"
                            and request["operation"] == "read",
                            "reason": "grant", "grant_tool": "*",
                        } for request in requests])
    tools = [MCPTool(name="retrieve_document", description="Read", input_schema={"type": "object"}),
             MCPTool(name="delete_source_artifact", description="Delete", input_schema={"type": "object"})]
    principal = Principal.panky(subject="fixture-sub", email="dev@example.com")
    visible = asyncio.run(mcp_server._panky_catalog(tools, principal))
    assert [tool.name for tool in visible] == ["retrieve_document"]
