"""OIDC source discovery must execute with the same source policy as its catalog."""

import asyncio
import importlib
import json
from types import SimpleNamespace

import pytest
from mcp.types import CallToolResult, TextContent, Tool

from app.auth.panky_bridge import PankyBridgeError
from app.auth.principals import Principal, active_principal, bind_principal, reset_principal
from app.source_registry import SourceDefinition

module = importlib.import_module("app.mcp_server")


@pytest.fixture
def sources(monkeypatch):
    def source(name, *, status="active", read=True):
        return SourceDefinition.model_validate({
            "id": name, "name": name, "system": "google_workspace",
            "location_type": "folder", "location_id": "fixture_folder_123",
            "classification": "internal_delivery", "owner": ["fixture"],
            "status": status, "capabilities": {"read": read},
        })
    registry = SimpleNamespace(sources=[source("hq_client"), source("other_source"),
        source("disabled_source", status="disabled"), source("no_read_source", read=False)])
    monkeypatch.setattr(module, "get_runtime_gateway", lambda: SimpleNamespace(registry=registry))
    monkeypatch.setattr(module, "emit_audit_record", lambda **_: None)
    return registry


def test_advertised_source_catalog_executes_and_filters_denied_sources(monkeypatch, sources):
    requests = []
    def decision(*_args, **kwargs):
        requests.append(kwargs)
        return {"allowed": kwargs["resource_kind"] == "source"
                and kwargs["resource_ref"] == "hq_client"}
    monkeypatch.setattr(module, "policy_decision", decision)
    monkeypatch.setattr(module, "preview_policy_decisions", lambda rows: [
        {"allowed": row["resource_kind"] == "source" and row["resource_ref"] == "hq_client"}
        for row in rows])
    principal = Principal.panky(subject="dev-oidc", email="dev@example.invalid")
    advertised = asyncio.run(module.mcp_server._panky_catalog([
        Tool(name="list_sources", description="Sources", inputSchema={"type": "object"})
    ], principal))
    assert [tool.name for tool in advertised] == ["list_sources"]

    async def execute(name, arguments, context=None):
        assert name == "list_sources"
        await asyncio.sleep(0)
        # Exercise the real metadata filtering and audit wrapper.
        data = module.list_sources(SimpleNamespace(request_id="source-catalog-test"))
        return CallToolResult(content=[TextContent(type="text", text=data.model_dump_json())])
    monkeypatch.setattr(module.mcp_server, "_call_tool_impl", execute)
    binding = bind_principal(principal)
    try:
        async def invoke():
            result = await module.mcp_server.call_tool("list_sources", {})
            assert active_principal() is principal
            return result
        result = asyncio.run(invoke())
    finally:
        reset_principal(binding)
    assert not result.is_error
    assert [source["id"] for source in json.loads(result.content[0].text)["sources"]] == ["hq_client"]
    assert {row["resource_ref"] for row in requests} == {"hq_client", "other_source"}
    assert all(row["operation"] == "read" and row["tool"] == "list_sources" for row in requests)


def test_catalog_denial_never_executes_the_listing(monkeypatch, sources):
    monkeypatch.setattr(module, "policy_decision", lambda *_args, **_kwargs: {"allowed": False})
    async def forbidden(*_args, **_kwargs):
        pytest.fail("ungranted listing executed")
    monkeypatch.setattr(module.mcp_server, "_call_tool_impl", forbidden)
    binding = bind_principal(Principal.panky(subject="dev-oidc", email="dev@example.invalid"))
    try:
        result = asyncio.run(module.mcp_server.call_tool("list_sources", {}))
    finally:
        reset_principal(binding)
    assert result.is_error and "policy_denied" in result.content[0].text


@pytest.mark.parametrize("unavailable", [False, True])
def test_catalog_finishes_reserved_read_and_restores_principal(monkeypatch, sources, unavailable):
    completed = []
    def decide(*_args, **kwargs):
        if kwargs["resource_ref"] == "hq_client":
            return {"allowed": True, "reservation_id": "read-use"}
        if unavailable:
            raise PankyBridgeError("unavailable")
        return {"allowed": False}
    monkeypatch.setattr(module, "policy_decision", decide)
    monkeypatch.setattr(module, "complete_policy_use", lambda *args: completed.append(args))
    async def listing(*_args, **_kwargs):
        assert not unavailable
        assert active_principal().sources == frozenset({"hq_client"})
        return CallToolResult(content=[TextContent(type="text", text="metadata")])
    monkeypatch.setattr(module.mcp_server, "_call_tool_impl", listing)
    principal = Principal.panky(subject="dev-oidc", email="dev@example.invalid")
    binding = bind_principal(principal)
    try:
        async def invoke():
            result = await module.mcp_server.call_tool("list_sources", {})
            assert active_principal() is principal
            return result
        result = asyncio.run(invoke())
    finally:
        reset_principal(binding)
    assert result.is_error is unavailable
    assert len(completed) == 1
    assert completed[0][0] == "read-use"
    assert completed[0][2] == ("uncertain" if unavailable else "committed")


def test_concurrent_oidc_catalogs_do_not_share_source_scope(monkeypatch, sources):
    def decide(identity, **kwargs):
        target = "hq_client" if identity.subject == "dev-one" else "other_source"
        return {"allowed": kwargs["resource_ref"] == target}
    monkeypatch.setattr(module, "policy_decision", decide)
    async def listing(*_args, **_kwargs):
        await asyncio.sleep(0)
        data = module.list_sources(SimpleNamespace(request_id="concurrent-catalog"))
        return CallToolResult(content=[TextContent(type="text", text=data.model_dump_json())])
    monkeypatch.setattr(module.mcp_server, "_call_tool_impl", listing)
    async def invoke(subject):
        principal = Principal.panky(subject=subject, email="dev@example.invalid")
        binding = bind_principal(principal)
        try:
            result = await module.mcp_server.call_tool("list_sources", {})
            assert active_principal() is principal
            return [s["id"] for s in json.loads(result.content[0].text)["sources"]]
        finally:
            reset_principal(binding)
    async def concurrent():
        return await asyncio.gather(invoke("dev-one"), invoke("dev-two"))
    assert asyncio.run(concurrent()) == [["hq_client"], ["other_source"]]
