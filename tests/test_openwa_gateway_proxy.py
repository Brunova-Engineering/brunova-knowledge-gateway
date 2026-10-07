import asyncio

from mcp import Client

import app.mcp_server as mcp_module
from app.adapters.openwa.models import OpenWAToolDescriptor


class FakeOpenWAClient:
    def __init__(self):
        self.calls = []

    async def list_tools(self, **_kwargs):
        return [
            OpenWAToolDescriptor(
                name="MessageHistory",
                description="Read recent messages",
                input_schema={"type": "object", "required": ["sessionId"]},
                annotations={"readOnlyHint": True},
                tier="read",
            ),
            OpenWAToolDescriptor(
                name="MessageSendText",
                description="Send text",
                input_schema={"type": "object", "required": ["sessionId", "text"]},
                annotations={"readOnlyHint": False},
                tier="write",
            ),
        ]

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"content": [{"type": "text", "text": "ok"}], "isError": False}


class FakeN8NClient:
    async def list_tools(self, **_kwargs):
        from app.adapters.n8n.models import N8NToolDescriptor

        return [N8NToolDescriptor(name="healthy", input_schema={"type": "object"})]


def test_management_projects_and_calls_openwa_write_without_approval(monkeypatch):
    fake = FakeOpenWAClient()
    audits = []
    monkeypatch.setattr(mcp_module, "get_openwa_client", lambda: fake)
    monkeypatch.setattr(mcp_module, "emit_audit_record", lambda **kwargs: audits.append(kwargs))
    payload = {"sessionId": "safe-session", "text": "private-message-body"}

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            catalog = await client.list_tools()
            read = await client.call_tool("openwa_MessageHistory", {"sessionId": "safe-session"})
            written = await client.call_tool("openwa_MessageSendText", payload)
            legacy = await client.call_tool("openwa_MessageSendText", {
                **payload,
                "approval_reference": "legacy-metadata",
                "approval_evidence": {"conversation_ref": "legacy"},
            })
            return catalog, read, written, legacy

    catalog, read, written, legacy = asyncio.run(scenario())
    projected = {tool.name: tool for tool in catalog.tools if tool.name.startswith("openwa_")}
    assert projected["openwa_MessageHistory"].input_schema["required"] == ["sessionId"]
    assert projected["openwa_MessageSendText"].input_schema["required"] == ["sessionId", "text"]
    assert "approval_evidence" not in projected["openwa_MessageSendText"].input_schema.get("properties", {})
    assert "approval_reference_required" not in projected["openwa_MessageSendText"].meta
    assert "openwa_prepare_approval_reference" not in {tool.name for tool in catalog.tools}
    assert not read.is_error and not written.is_error and not legacy.is_error
    assert fake.calls == [("MessageHistory", {"sessionId": "safe-session"}),
                          ("MessageSendText", payload), ("MessageSendText", payload)]
    assert all("private-message-body" not in repr(event) for event in audits)
    assert audits[-1]["provider"] == "openwa"


def test_provider_discovery_failures_are_isolated(monkeypatch):
    fake_openwa = FakeOpenWAClient()

    class Failed:
        async def list_tools(self, **_kwargs):
            raise RuntimeError("unavailable")

    async def names():
        async with Client(mcp_module.mcp_server) as client:
            return {tool.name for tool in (await client.list_tools()).tools}

    monkeypatch.setattr(mcp_module, "get_n8n_client", lambda: Failed())
    monkeypatch.setattr(mcp_module, "get_openwa_client", lambda: fake_openwa)
    with_openwa = asyncio.run(names())
    assert "openwa_MessageHistory" in with_openwa
    assert "list_sources" in with_openwa

    monkeypatch.setattr(mcp_module, "get_n8n_client", lambda: FakeN8NClient())
    monkeypatch.setattr(mcp_module, "get_openwa_client", lambda: Failed())
    with_n8n = asyncio.run(names())
    assert "n8n_healthy" in with_n8n
    assert "list_sources" in with_n8n
