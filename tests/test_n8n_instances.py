"""Multi-instance n8n grants must bind the caller before opening a client."""

import asyncio

from mcp import Client

import app.mcp_server as mcp_module
from app.adapters.n8n.instances import N8NInstanceRegistry
from app.adapters.n8n.models import N8NToolDescriptor
from app.auth.principals import Principal, bind_principal, reset_principal
from tests.test_principals import record


class FakeClient:
    def __init__(self):
        self.calls = []

    async def list_tools(self, **_kwargs):
        return [N8NToolDescriptor(name="get_workflow", description="Read workflow", input_schema={"type": "object"})]

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"isError": False, "content": []}


def test_registry_defaults_and_rejects_shared_secret(monkeypatch):
    monkeypatch.delenv("N8N_INSTANCE_REGISTRY_JSON", raising=False)
    assert N8NInstanceRegistry.from_environment().ids() == ("brunova",)
    monkeypatch.setenv("N8N_INSTANCE_REGISTRY_JSON", '{"brunova":"N8N_MCP_JSON","headquarters":"N8N_MCP_JSON"}')
    try:
        N8NInstanceRegistry.from_environment()
    except ValueError as error:
        assert "distinct" in str(error)
    else:
        raise AssertionError("Shared downstream credential accepted")


def test_developer_only_sees_and_calls_headquarters(monkeypatch):
    monkeypatch.setenv("N8N_INSTANCE_REGISTRY_JSON", '{"brunova":"N8N_MCP_JSON","headquarters":"N8N_HEADQUARTERS_MCP_JSON"}')
    clients = {"brunova": FakeClient(), "headquarters": FakeClient()}
    monkeypatch.setattr(mcp_module, "get_n8n_client", lambda instance_id="brunova": clients[instance_id])
    principal = Principal.from_record(record(
        "developer-token", id="dev_01", providers={"workspace": True, "n8n": True},
        n8n_tools={"headquarters": ["get_workflow"]},
    ))

    async def scenario():
        token = bind_principal(principal)
        try:
            async with Client(mcp_module.mcp_server) as client:
                catalog = await client.list_tools()
                instances = await client.call_tool("n8n_instances", {})
                hq = await client.call_tool("n8n_instance_list_tools", {"instance_id": "headquarters"})
                denied = await client.call_tool("n8n_instance_list_tools", {"instance_id": "brunova"})
                called = await client.call_tool("n8n_instance_call_tool", {
                    "instance_id": "headquarters", "tool_name": "get_workflow", "arguments": {},
                })
                denied_tool = await client.call_tool("n8n_instance_call_tool", {
                    "instance_id": "headquarters", "tool_name": "delete_workflow", "arguments": {},
                })
                return catalog, instances, hq, denied, called, denied_tool
        finally:
            reset_principal(token)

    catalog, instances, hq, denied, called, denied_tool = asyncio.run(scenario())
    names = {tool.name for tool in catalog.tools}
    assert "n8n_instance_call_tool" in names
    assert "n8n_get_workflow" not in names
    assert instances.structured_content["instances"] == ["headquarters"]
    assert [tool["name"] for tool in hq.structured_content["tools"]] == ["get_workflow"]
    assert denied.is_error and denied_tool.is_error
    assert not called.is_error
    assert clients["headquarters"].calls == [("get_workflow", {})]
    assert clients["brunova"].calls == []
