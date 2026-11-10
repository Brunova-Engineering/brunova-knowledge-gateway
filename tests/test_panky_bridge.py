"""Legacy tokens and new short access tokens coexist without fallback."""

import json
import time
import asyncio
from types import SimpleNamespace

import pytest

from app.auth.gateway_auth import GatewayPrincipalAuthenticator
from app.auth.panky_bridge import (
    PankyBridgeError, PankyIdentity, issue_access_token, policy_decision,
    verify_access_token,
)
from app.auth.principals import PrincipalResolver
from app.auth.principals import Principal, bind_principal, reset_principal
from app.mcp_server import mcp_server
from mcp.types import CallToolResult, TextContent
from mcp.types import Tool as MCPTool
from app.source_registry import SourceDefinition, SourceRegistry, SourceRegistryDocument


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


def test_list_sources_uses_source_read_policy_and_filters_result(monkeypatch):
    def source(source_id, *, read=True):
        return SourceDefinition.model_validate({
            "id": source_id, "name": source_id, "system": "google_workspace",
            "location_type": "folder", "location_id": f"folder_{source_id}",
            "classification": "management_only", "owner": ["Management"],
            "status": "active", "capabilities": {"read": read},
        })

    registry = SourceRegistry(SourceRegistryDocument(version=1, sources=(
        source("allowed"), source("denied"), source("not_readable", read=False),
    )))
    monkeypatch.setattr("app.mcp_server.get_runtime_gateway",
                        lambda: SimpleNamespace(registry=registry))
    monkeypatch.setattr("app.mcp_server.emit_audit_record", lambda **kwargs: None)
    monkeypatch.setattr("app.mcp_server._mcp_request_id", lambda context: "synthetic-request")
    seen = []

    def preview(requests):
        seen.extend(requests)
        return [{"allowed": request["resource_ref"] == "allowed"} for request in requests]

    monkeypatch.setattr("app.mcp_server.preview_policy_decisions", preview)
    monkeypatch.setattr("app.mcp_server.policy_decision",
                        lambda *args, **kwargs: pytest.fail("global policy decision called"))
    binding = bind_principal(Principal.panky(subject="fixture-sub", email="dev@example.com"))
    try:
        result = asyncio.run(mcp_server.call_tool("list_sources", {}))
    finally:
        reset_principal(binding)

    assert not result.is_error
    assert [source["id"] for source in result.structured_content["sources"]] == ["allowed"]
    assert {request["resource_ref"] for request in seen} == {"allowed", "denied"}
    assert all(request["resource_kind"] == "source" and request["operation"] == "read"
               for request in seen)


def test_list_sources_denies_when_no_source_is_readable(monkeypatch):
    registry = SourceRegistry(SourceRegistryDocument(version=1, sources=(
        SourceDefinition.model_validate({
            "id": "denied", "name": "Denied", "system": "google_workspace",
            "location_type": "folder", "location_id": "folder_denied",
            "classification": "management_only", "owner": ["Management"],
            "status": "active", "capabilities": {"read": True},
        }),
    )))
    monkeypatch.setattr("app.mcp_server.get_runtime_gateway",
                        lambda: SimpleNamespace(registry=registry))
    monkeypatch.setattr("app.mcp_server.preview_policy_decisions",
                        lambda requests: [{"allowed": False} for _ in requests])
    binding = bind_principal(Principal.panky(subject="fixture-sub", email="dev@example.com"))
    try:
        result = asyncio.run(mcp_server.call_tool("list_sources", {}))
    finally:
        reset_principal(binding)
    assert result.is_error
    assert "policy_denied" in result.content[0].text


def test_workspace_authorization_preview_is_read_only_and_checks_effective_access(monkeypatch):
    source = SourceDefinition.model_validate({
        "id": "hq_client", "name": "HQ", "system": "google_workspace",
        "location_type": "folder", "location_id": "hq_folder_12345",
        "classification": "management_only", "owner": ["Management"],
        "status": "active", "capabilities": {"read": True, "delete": True, "share": False},
    })
    registry = SourceRegistry(SourceRegistryDocument(version=1, sources=(source,)))
    monkeypatch.setattr("app.mcp_server.get_runtime_gateway",
                        lambda: SimpleNamespace(registry=registry))
    monkeypatch.setattr("app.mcp_server.emit_audit_record", lambda **kwargs: None)
    monkeypatch.setattr("app.mcp_server._mcp_request_id", lambda context: "preview-test")
    monkeypatch.setattr("app.mcp_server.policy_decision",
                        lambda *args, **kwargs: pytest.fail("mutating policy decision called"))
    seen = []

    def preview(requests):
        seen.extend(requests)
        return [{"allowed": request["operation"] == "read"} for request in requests]

    monkeypatch.setattr("app.mcp_server.preview_policy_decisions", preview)
    binding = bind_principal(Principal.panky(subject="fixture-sub", email="dev@example.com"))
    try:
        read = asyncio.run(mcp_server.call_tool(
            "check_workspace_authorization", {"source_id": "hq_client", "tool_name": "list_source_documents"}))
        delete = asyncio.run(mcp_server.call_tool(
            "check_workspace_authorization", {"source_id": "hq_client", "tool_name": "delete_source_artifact"}))
        share = asyncio.run(mcp_server.call_tool(
            "check_workspace_authorization", {"source_id": "hq_client", "tool_name": "share_source_artifact"}))
        outside = asyncio.run(mcp_server.call_tool(
            "check_workspace_authorization", {"source_id": "outside", "tool_name": "list_source_documents"}))
        catalog = asyncio.run(mcp_server._panky_catalog(
            [MCPTool(name="check_workspace_authorization", description="Preview", input_schema={"type": "object"})],
            Principal.panky(subject="fixture-sub", email="dev@example.com")))
    finally:
        reset_principal(binding)

    assert read.structured_content["allowed"] is True
    assert read.structured_content["reason"] == "allowed"
    assert delete.structured_content["allowed"] is False
    assert delete.structured_content["reason"] == "policy_denied"
    assert share.structured_content["allowed"] is False
    assert share.structured_content["reason"] == "source_capability_denied"
    assert outside.structured_content["allowed"] is False
    assert outside.structured_content["reason"] == "source_unavailable"
    assert [item["operation"] for item in seen] == ["read", "delete"]
    assert [item["tool"] for item in seen] == ["list_source_documents", "delete_source_artifact"]
    assert all(item["preview"] is True and item["resource_kind"] == "source"
               for item in seen)
    assert [tool.name for tool in catalog] == ["check_workspace_authorization"]


def test_workspace_authorization_preview_is_hidden_from_legacy_principals():
    from app.mcp_server import _tool_authorization_error

    assert _tool_authorization_error(Principal.management(), "check_workspace_authorization") == "tool_denied"


def test_policy_allowed_panky_can_read_acquisition_without_widening_developer(monkeypatch):
    import httpx
    from app import acquisition

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"cycleId": "cycle-1", "status": "ACTIVE"})

    monkeypatch.setenv("ACQUISITION_PORTAL_ORIGIN", "https://portal.example.test")
    monkeypatch.setenv("ACQUISITION_PORTAL_SERVICE_TOKEN", "synthetic_service_token_not_a_real_secret_123456789")
    real_client = httpx.AsyncClient
    monkeypatch.setattr(acquisition.httpx, "AsyncClient",
                        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr("app.mcp_server.policy_decision",
                        lambda *args, **kwargs: {"allowed": True, "reason": "super_admin"})

    binding = bind_principal(Principal.panky(subject="fixture-sub", email="jorge@example.com"))
    try:
        result = asyncio.run(mcp_server.call_tool("acquisition_get_cycle", {"cycle_id": "cycle-1"}))
    finally:
        reset_principal(binding)
    assert not result.is_error
    assert json.loads(result.content[0].text)["data"]["cycleId"] == "cycle-1"
    assert len(calls) == 1
    assert calls[0].url.path == "/api/acquisition/v1/cycles/cycle-1"

    monkeypatch.setattr("app.mcp_server.policy_decision",
                        lambda *args, **kwargs: {"allowed": False, "reason": "default_deny"})
    binding = bind_principal(Principal.panky(subject="fixture-sub", email="jorge@example.com"))
    try:
        policy_denied = asyncio.run(mcp_server.call_tool("acquisition_get_cycle", {"cycle_id": "cycle-1"}))
    finally:
        reset_principal(binding)
    assert policy_denied.is_error
    assert len(calls) == 1

    from dataclasses import replace
    binding = bind_principal(replace(Principal.management(), type="developer"))
    try:
        denied = asyncio.run(mcp_server.call_tool("acquisition_get_cycle", {"cycle_id": "cycle-1"}))
    finally:
        reset_principal(binding)
    assert denied.is_error
    assert len(calls) == 1


def test_v2_shadow_disagreement_never_changes_v1_decision(monkeypatch, caplog):
    from app.auth import panky_bridge

    monkeypatch.setenv("PANKY_POLICY_BACKEND_URL", "https://policy.example.invalid")
    monkeypatch.setenv("PANKY_POLICY_V2_MODE", "shadow")
    monkeypatch.setattr(panky_bridge.id_token, "fetch_id_token",
                        lambda request, audience: "synthetic-service-proof")
    paths = []

    def reply(url, **kwargs):
        paths.append(url)
        payload = ({"allowed": True} if url.endswith("/v1/decide")
                   else [{"allowed": False}])
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)

    monkeypatch.setattr(panky_bridge.httpx, "post", reply)
    result = policy_decision(
        PankyIdentity("https://accounts.google.com", "synthetic-sub", "dev@example.invalid"),
        provider="workspace", tool="retrieve_document", resource_kind="source",
        resource_ref="hq_client", operation="read", request_id="shadow-test",
    )
    assert result["allowed"] is True
    assert paths == ["https://policy.example.invalid/v1/decide",
                     "https://policy.example.invalid/v2/decisions/preview"]
    assert "decision mismatch" in caplog.text


def test_v2_enforce_routes_oidc_decision_catalog_and_completion(monkeypatch):
    from app.auth import panky_bridge

    monkeypatch.setenv("PANKY_POLICY_BACKEND_URL", "https://policy.example.invalid")
    monkeypatch.setenv("PANKY_POLICY_V2_MODE", "enforce")
    monkeypatch.setattr(panky_bridge.id_token, "fetch_id_token",
                        lambda request, audience: "synthetic-service-proof")
    calls = []

    def reply(url, **kwargs):
        calls.append((url, kwargs["json"]))
        if url.endswith("/v2/decisions/preview"):
            payload = [{"allowed": False}]
        elif url.endswith("/v2/uses/complete"):
            payload = {"completed": True}
        else:
            payload = {"allowed": False, "reason": "no_authorized_seat"}
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)

    monkeypatch.setattr(panky_bridge.httpx, "post", reply)
    identity = PankyIdentity("https://accounts.google.com", "synthetic-sub",
                             "dev@example.invalid")
    assert not policy_decision(
        identity, provider="workspace", tool="retrieve_document",
        resource_kind="source", resource_ref="hq_client",
        operation="read", request_id="enforce-test",
    )["allowed"]
    assert not panky_bridge.preview_policy_decisions([{
        "actor": {"issuer": identity.issuer, "subject": identity.subject,
                  "email": identity.email},
        "provider": "workspace", "tool": "retrieve_document",
        "resource_kind": "source", "resource_ref": "hq_client",
        "operation": "read", "request_id": "catalog-test",
    }])[0]["allowed"]
    panky_bridge.complete_policy_use("synthetic-use", "enforce-test", "released")
    assert [url for url, _ in calls] == [
        "https://policy.example.invalid/v2/decide",
        "https://policy.example.invalid/v2/decisions/preview",
        "https://policy.example.invalid/v2/uses/complete",
    ]
    assert isinstance(calls[1][1], list)


def test_context_catalog_requires_v2_and_forwards_verified_actor(monkeypatch):
    from app.auth import panky_bridge

    identity = PankyIdentity("https://accounts.google.com", "synthetic-sub",
                             "dev@example.invalid")
    monkeypatch.setenv("PANKY_POLICY_BACKEND_URL", "https://policy.example.invalid")
    monkeypatch.setenv("PANKY_POLICY_V2_MODE", "shadow")
    with pytest.raises(PankyBridgeError):
        panky_bridge.context_catalog(identity)
    monkeypatch.setenv("PANKY_POLICY_V2_MODE", "enforce")
    monkeypatch.setattr(panky_bridge.id_token, "fetch_id_token",
                        lambda request, audience: "synthetic-service-proof")
    calls = []

    def reply(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {"status": "ready", "release": "a" * 40,
                                             "nodes": []})

    monkeypatch.setattr(panky_bridge.httpx, "post", reply)
    result = panky_bridge.context_catalog(identity)
    assert result["status"] == "ready"
    assert calls[0][0] == "https://policy.example.invalid/v2/context/catalog"
    assert calls[0][1]["json"] == {"issuer": identity.issuer,
                                    "subject": identity.subject, "email": identity.email}
    monkeypatch.setattr(panky_bridge.httpx, "post", lambda *_args, **_kwargs:
                        SimpleNamespace(raise_for_status=lambda: None,
                                        json=lambda: {"status": "denied", "reason": "actor_disabled"}))
    assert panky_bridge.context_catalog(identity) == {
        "status": "denied", "reason": "actor_disabled"}


def test_gateway_catalog_rejects_legacy_token_and_scopes_oidc_actor(
    monkeypatch, ephemeral_gateway_token,
):
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setenv("PANKY_POLICY_MODE", "shadow")
    monkeypatch.setenv("PANKY_GATEWAY_SIGNING_KEY", "x" * 40)
    monkeypatch.setenv("PANKY_DESKTOP_OAUTH_CLIENT_ID", "fixture-client")
    monkeypatch.setattr("app.auth.panky_bridge.id_token.verify_oauth2_token",
                        lambda proof, request, audience: {
                            "iss": "https://accounts.google.com", "aud": audience,
                            "sub": "google-subject", "email": "dev@example.invalid",
                            "email_verified": True,
                        })
    calls = []

    def catalog(identity):
        calls.append(identity)
        return {"status": "ready", "release": "a" * 40, "nodes": []}

    monkeypatch.setattr("app.main.context_catalog", catalog)
    bearer, _ = issue_access_token("verified-fixture-proof")
    client = TestClient(app)
    denied = client.get("/panky/context/catalog",
                        headers={"Authorization": f"Bearer {ephemeral_gateway_token}"})
    allowed = client.get("/panky/context/catalog",
                         headers={"Authorization": f"Bearer {bearer}"})
    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "ready"
    assert len(calls) == 1 and calls[0].subject == "google-subject"


def test_gateway_context_decision_rejects_legacy_and_uses_verified_actor(
    monkeypatch, ephemeral_gateway_token,
):
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setenv("PANKY_POLICY_MODE", "shadow")
    monkeypatch.setenv("PANKY_GATEWAY_SIGNING_KEY", "x" * 40)
    monkeypatch.setenv("PANKY_DESKTOP_OAUTH_CLIENT_ID", "fixture-client")
    monkeypatch.setattr("app.auth.panky_bridge.id_token.verify_oauth2_token",
                        lambda proof, request, audience: {
                            "iss": "https://accounts.google.com", "aud": audience,
                            "sub": "google-subject", "email": "dev@example.invalid",
                            "email_verified": True,
                        })
    seen = []

    def decide(identity, **kwargs):
        seen.append((identity, kwargs))
        return {"allowed": False, "reason": "no_authorized_seat"}

    monkeypatch.setattr("app.main.context_decision", decide)
    bearer, _ = issue_access_token("verified-fixture-proof")
    client = TestClient(app)
    body = {"node_id": "ctx_hq", "surface": "knowledge", "operation": "update",
            "request_id": "fixture-request", "preview": False}
    denied = client.post("/panky/context/decide", json=body,
                         headers={"Authorization": f"Bearer {ephemeral_gateway_token}"})
    allowed = client.post("/panky/context/decide", json=body,
                          headers={"Authorization": f"Bearer {bearer}"})
    assert denied.status_code == 403
    assert allowed.status_code == 200 and allowed.json()["allowed"] is False
    assert seen[0][0].subject == "google-subject"
    assert seen[0][1] == body


def test_n8n_execution_is_distinct_from_introspection_in_policy():
    from app.mcp_server import _panky_operation

    assert _panky_operation("n8n_instance_call_tool", "n8n", None) == "execute"
    assert _panky_operation("n8n_custom_downstream_tool", "n8n", None) == "execute"
    assert _panky_operation("n8n_instance_status", "n8n", None) == "call"
    assert _panky_operation("n8n_instance_list_tools", "n8n", None) == "call"
    assert _panky_operation("workspace_create", "workspace", "create") == "create"
