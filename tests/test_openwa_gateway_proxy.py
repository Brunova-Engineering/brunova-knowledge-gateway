import asyncio
import json

from mcp import Client

import app.mcp_server as mcp_module
from app.adapters.openwa.models import OpenWAToolDescriptor
from app.openwa_approval import reference_for_confirmed_operation


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


def test_management_projects_catalog_and_governs_write_without_auditing_body(monkeypatch):
    fake = FakeOpenWAClient()
    audits = []
    monkeypatch.setattr(mcp_module, "get_openwa_client", lambda: fake)
    monkeypatch.setattr(mcp_module, "emit_audit_record", lambda **kwargs: audits.append(kwargs))

    reference = reference_for_confirmed_operation(
        "test-thread", "confirmed-turn", "MessageSendText",
        {"sessionId": "safe-session", "text": "private-message-body"},
    )

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            catalog = await client.list_tools()
            read = await client.call_tool(
                "openwa_MessageHistory", {"sessionId": "safe-session"}
            )
            denied = await client.call_tool(
                "openwa_MessageSendText",
                {"sessionId": "safe-session", "text": "private-message-body"},
            )
            written = await client.call_tool(
                "openwa_MessageSendText",
                {"sessionId": "safe-session", "text": "private-message-body"},
                meta={"approval_reference": reference},
            )
            return catalog, read, denied, written

    catalog, read, denied, written = asyncio.run(scenario())
    projected = {tool.name: tool for tool in catalog.tools if tool.name.startswith("openwa_")}

    assert projected["openwa_MessageHistory"].input_schema["required"] == ["sessionId"]
    assert projected["openwa_MessageSendText"].meta["approval_reference_required"] is True
    assert "approval_evidence" in projected["openwa_MessageSendText"].input_schema["required"]
    assert "approval_reference" not in projected["openwa_MessageSendText"].input_schema["properties"]
    assert "openwa_prepare_approval_reference" in {tool.name for tool in catalog.tools}
    assert read.is_error is False
    assert denied.is_error is True
    assert "openwa_approval_required" in denied.content[0].text
    assert written.is_error is False
    assert [call[0] for call in fake.calls] == ["MessageHistory", "MessageSendText"]
    assert all("private-message-body" not in repr(event) for event in audits)
    assert audits[-1]["provider"] == "openwa"
    assert audits[-1]["approval_reference"] == reference


def test_voice_compatible_approval_tool_binds_exact_send_without_downstream_effect(monkeypatch):
    fake = FakeOpenWAClient()
    monkeypatch.setattr(mcp_module, "get_openwa_client", lambda: fake)
    payload = {"sessionId": "safe-session", "chatId": "safe-chat", "text": "private-body"}

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            prepared = await client.call_tool("openwa_prepare_approval_reference", {
                "thread_id": "actual-conversation-reference",
                "confirmation_id": "actual-human-request-reference",
                "tool": "MessageSendText",
                "arguments": payload,
            })
            assert not prepared.is_error
            reference = json.loads(prepared.content[0].text)["approval_reference"]
            assert reference == reference_for_confirmed_operation(
                "actual-conversation-reference", "actual-human-request-reference",
                "MessageSendText", payload,
            )
            assert fake.calls == []
            written = await client.call_tool(
                "openwa_MessageSendText", {**payload, "approval_reference": reference}
            )
            assert not written.is_error
            return reference

    reference = asyncio.run(scenario())
    assert reference.startswith("owa1:")
    assert fake.calls == [("MessageSendText", payload)]


def test_inline_human_evidence_is_bound_server_side_and_not_forwarded(monkeypatch):
    fake = FakeOpenWAClient()
    audits = []
    monkeypatch.setattr(mcp_module, "get_openwa_client", lambda: fake)
    monkeypatch.setattr(mcp_module, "emit_audit_record", lambda **kw: audits.append(kw))
    payload = {"sessionId": "safe-session", "chatId": "safe-chat", "text": "private-body"}
    evidence = {"conversation_ref": "visible conversation 42",
                "human_request_ref": "human requested this exact message at 12:28"}

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            invalid = await client.call_tool("openwa_MessageSendText", {
                **payload, "approval_evidence": {"conversation_ref": "made-up"}})
            assert invalid.is_error and "openwa_approval_required" in invalid.content[0].text
            conflict = await client.call_tool("openwa_MessageSendText", {
                **payload, "approval_evidence": evidence,
                "approval_reference": "owa1:invalid"})
            assert conflict.is_error and "openwa_approval_conflict" in conflict.content[0].text
            written = await client.call_tool("openwa_MessageSendText", {
                **payload, "approval_evidence": evidence})
            assert not written.is_error

    asyncio.run(scenario())
    assert fake.calls == [("MessageSendText", payload)]
    assert audits[-1]["approval_reference"] == reference_for_confirmed_operation(
        evidence["conversation_ref"], evidence["human_request_ref"],
        "MessageSendText", payload)
    assert all("private-body" not in repr(event) and "human requested" not in repr(event)
               for event in audits)


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


def test_confirmed_send_is_bounded_across_both_metadata_channels(monkeypatch):
    fake = FakeOpenWAClient()
    audits = []
    monkeypatch.setattr(mcp_module, "get_openwa_client", lambda: fake)
    monkeypatch.setattr(mcp_module, "emit_audit_record", lambda **kw: audits.append(kw))
    payload = {"sessionId": "safe-session", "chatId": "safe-chat", "text": "private-body"}
    reference = reference_for_confirmed_operation("thread", "human-turn", "MessageSendText", payload)

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            # An explicit conversational yes does not magically populate MCP metadata.
            missing = await client.call_tool("openwa_MessageSendText", payload)
            assert missing.is_error and "openwa_approval_required" in missing.content[0].text
            for changed in ({**payload, "chatId": "different-chat"},
                            {**payload, "text": "different-content"},
                            {**payload, "sessionId": "different-session"},
                            {**payload, "quotedMessageId": "different-message"}):
                denied = await client.call_tool("openwa_MessageSendText", changed,
                                               meta={"approval_reference": reference})
                assert denied.is_error and "openwa_approval_scope_mismatch" in denied.content[0].text
            # Argument-only hosts and metadata-capable clients share the same gate.
            for metadata in (True, False):
                args = payload if metadata else {**payload, "approval_reference": reference}
                written = await client.call_tool("openwa_MessageSendText", args,
                    meta={"approval_reference": reference} if metadata else None)
                assert not written.is_error
            conflict = await client.call_tool("openwa_MessageSendText",
                {**payload, "approval_reference": "different"}, meta={"approval_reference": reference})
            assert conflict.is_error and "openwa_approval_conflict" in conflict.content[0].text
            legacy = await client.call_tool("openwa_MessageSendText", payload,
                                           meta={"approval_reference": "unbounded-legacy-ref"})
            assert legacy.is_error
    asyncio.run(scenario())
    assert fake.calls == [("MessageSendText", payload), ("MessageSendText", payload)]
    assert all("private-body" not in repr(event) and "safe-chat" not in repr(event) for event in audits)


def test_reference_binds_tool_and_provenance_and_is_order_independent():
    from app.openwa_approval import validate_reference
    from app.adapters.google_workspace.errors import WorkspaceAdapterError
    import pytest
    payload = {"text": "private", "chatId": "test"}
    ref = reference_for_confirmed_operation("thread", "turn", "MessageReply", payload)
    assert validate_reference(ref, "MessageReply", dict(reversed(list(payload.items())))) == ref
    with pytest.raises(WorkspaceAdapterError):
        validate_reference(ref, "MessageSendText", payload)
    assert ref != reference_for_confirmed_operation("other-thread", "turn", "MessageReply", payload)
    assert ref != reference_for_confirmed_operation("thread", "other-turn", "MessageReply", payload)
    with pytest.raises(ValueError):
        reference_for_confirmed_operation("", "turn", "MessageReply", payload)


def test_native_mcp_metadata_send_and_reply_after_confirmed_proposal(monkeypatch):
    from app.openwa_approval import call_confirmed_operation

    class ReplyClient(FakeOpenWAClient):
        async def list_tools(self, **kwargs):
            return [*await super().list_tools(**kwargs), OpenWAToolDescriptor(
                name="MessageReply", input_schema={"type": "object"}, tier="write")]

    fake = ReplyClient()
    monkeypatch.setattr(mcp_module, "get_openwa_client", lambda: fake)
    monkeypatch.setattr(mcp_module, "emit_audit_record", lambda **kw: None)

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            for tool in ("MessageSendText", "MessageReply"):
                payload = {"sessionId": "test-session", "chatId": "test-chat", "text": "test"}
                if tool == "MessageReply":
                    payload["messageId"] = "test-message"
                # Fixture: agent proposes this exact operation, human confirms it.
                # Consent interpretation belongs to the agent, not this deterministic helper.
                result = await call_confirmed_operation(client, "fixture-thread", "confirmed-turn",
                                                       tool, payload)
                assert not result.is_error
    asyncio.run(scenario())
    assert [tool for tool, args in fake.calls] == ["MessageSendText", "MessageReply"]
    assert all("approval_reference" not in args for tool, args in fake.calls)
