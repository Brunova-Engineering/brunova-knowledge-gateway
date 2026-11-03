import asyncio
import hashlib

from mcp import Client

import app.mcp_server as mcp_module
from app.adapters.openwa.models import OpenWAToolDescriptor
from app.auth.principals import (
    Principal, WhatsAppWorkerPrincipalRecord, bind_principal, reset_principal,
)


class FakeOpenWAClient:
    def __init__(self):
        self.calls = []

    async def list_tools(self, **_kwargs):
        return [
            OpenWAToolDescriptor(
                name="ContactCheckNumber",
                description="Resolve a phone number to its canonical chat identity",
                input_schema={"type": "object", "required": ["sessionId", "number"]},
                annotations={"readOnlyHint": True},
                tier="read",
            ),
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


def test_whatsapp_worker_resolves_canonical_chat_without_expanding_send_scope(monkeypatch):
    fake = FakeOpenWAClient()
    monkeypatch.setattr(mcp_module, "get_openwa_client", lambda: fake)
    principal = Principal.from_record(WhatsAppWorkerPrincipalRecord.model_validate({
        "id": "acquisition_whatsapp_worker", "type": "whatsapp_worker",
        "token_sha256": hashlib.sha256(b"worker-token").hexdigest(),
        "session_id": "brunova", "operations": ["history", "send"],
    }))

    async def scenario():
        binding = bind_principal(principal)
        try:
            catalog = {tool.name for tool in await mcp_module.mcp_server.list_tools()}
            denied = await mcp_module.mcp_server.call_tool("openwa_MessageSendText", {
                "sessionId": "another-session", "chatId": "5215512345678@c.us",
                "text": "This is a sufficiently long exact outbound message.",
            })
            history = await mcp_module.mcp_server.call_tool("openwa_MessageHistory", {
                "sessionId": "brunova", "chatId": "5215512345678@c.us", "limit": 50,
            })
            canonical = await mcp_module.mcp_server.call_tool("openwa_MessageHistory", {
                "sessionId": "brunova", "chatId": "15965182017662@lid", "limit": 50,
            })
            lookup = await mcp_module.mcp_server.call_tool("openwa_ContactCheckNumber", {
                "sessionId": "brunova", "number": "5215512345678",
            })
            wrong_lookup = await mcp_module.mcp_server.call_tool("openwa_ContactCheckNumber", {
                "sessionId": "another-session", "number": "5215512345678",
            })
            broad_lookup = await mcp_module.mcp_server.call_tool("openwa_ContactCheckNumber", {
                "sessionId": "brunova", "number": "5215512345678", "extra": "unsafe",
            })
            sent = await mcp_module.mcp_server.call_tool("openwa_MessageSendText", {
                "sessionId": "brunova", "chatId": "5215512345678@c.us",
                "text": "This is a sufficiently long exact outbound message.",
            })
            media = await mcp_module.mcp_server.call_tool("openwa_MessageSendText", {
                "sessionId": "brunova", "chatId": "5215512345678@c.us",
                "text": "This is a sufficiently long exact outbound message.",
                "media": "unapproved",
            })
            blocked = await mcp_module.mcp_server.call_tool("openwa_ContactList", {})
            return catalog, denied, history, canonical, lookup, wrong_lookup, broad_lookup, sent, media, blocked
        finally:
            reset_principal(binding)

    catalog, denied, history, canonical, lookup, wrong_lookup, broad_lookup, sent, media, blocked = asyncio.run(scenario())
    assert catalog == {"openwa_ContactCheckNumber", "openwa_MessageHistory", "openwa_MessageSendText"}
    assert denied.is_error and wrong_lookup.is_error and broad_lookup.is_error and media.is_error and blocked.is_error
    assert not history.is_error and not canonical.is_error and not lookup.is_error and not sent.is_error
    assert fake.calls == [("MessageHistory", {
        "sessionId": "brunova", "chatId": "5215512345678@c.us", "limit": 50,
    }), ("MessageHistory", {
        "sessionId": "brunova", "chatId": "15965182017662@lid", "limit": 50,
    }), ("ContactCheckNumber", {
        "sessionId": "brunova", "number": "5215512345678",
    }), ("MessageSendText", {
        "sessionId": "brunova", "chatId": "5215512345678@c.us",
        "text": "This is a sufficiently long exact outbound message.",
    })]


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
