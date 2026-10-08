import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.adapters.hubspot.mcp_client import (
    HubSpotMCPClient,
    HubSpotTokenManager,
    account_metadata_from_result,
)
from app.adapters.hubspot.models import HubSpotTokenResponse
from app.policies.hubspot import HubSpotToolPolicy


class StaticTokenManager:
    async def get_access_token(self):
        return "ephemeral-access-token"


class FakeDownstreamClient:
    def __init__(self):
        self.calls = []

    async def list_tools(self, **_kwargs):
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name="search_crm_objects",
                    description="Search records",
                    input_schema={"type": "object"},
                ),
                SimpleNamespace(
                    name="manage_crm_objects",
                    description="Mutate records",
                    input_schema={"type": "object"},
                ),
                SimpleNamespace(
                    name="future_tool",
                    description="Ambiguous",
                    input_schema={},
                ),
            ],
            next_cursor=None,
        )

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return SimpleNamespace(
            model_dump=lambda **_kwargs: {"content": [{"type": "text", "text": "ok"}]}
        )


def adapter(client):
    @asynccontextmanager
    async def factory(_access_token):
        yield client

    return HubSpotMCPClient(
        server_url="https://mcp.hubspot.com",
        token_manager=StaticTokenManager(),
        policy=HubSpotToolPolicy(),
        client_factory=factory,
    )


def test_downstream_initialization_lists_and_classifies_live_tools():
    tools = asyncio.run(adapter(FakeDownstreamClient()).list_tools())
    decisions = {tool.name: (tool.classification, tool.allowed) for tool in tools}
    assert decisions == {
        "search_crm_objects": ("read", True),
        "manage_crm_objects": ("mutation", True),
        "future_tool": ("unknown", False),
    }
    assert next(tool for tool in tools if tool.name == "manage_crm_objects").approval_required is False


def test_read_tool_calls_official_downstream_tool():
    client = FakeDownstreamClient()
    result = asyncio.run(
        adapter(client).call_tool(
            "search_crm_objects", {"object_type": "companies"}
        )
    )
    assert result["content"][0]["text"] == "ok"
    assert client.calls == [
        ("search_crm_objects", {"object_type": "companies"})
    ]


def test_unknown_tool_is_blocked_before_downstream_invocation():
    client = FakeDownstreamClient()
    with pytest.raises(WorkspaceAdapterError) as raised:
        asyncio.run(adapter(client).call_tool("future_tool", {}))
    assert raised.value.code == "hubspot_tool_not_allowed"
    assert client.calls == []


def test_mutation_needs_explicit_intent_but_no_external_approval():
    client = FakeDownstreamClient()
    with pytest.raises(WorkspaceAdapterError) as raised:
        asyncio.run(adapter(client).call_tool("manage_crm_objects", {}))
    assert raised.value.code == "hubspot_mutation_intent_required"

    asyncio.run(
        adapter(client).call_tool(
            "manage_crm_objects",
            {"object_type": "companies"},
            explicit_intent=True,
        )
    )
    assert client.calls == [("manage_crm_objects", {"object_type": "companies"})]


class RefreshStore:
    def __init__(self):
        self.completed = None
        self.failed = None

    def acquire_refresh_lease(self):
        return SimpleNamespace(
            refresh_token="refresh-one", scope="scope", lock_id="lock", generation=1
        )

    def complete_refresh(self, lease, refresh_token, *, scope):
        self.completed = (lease, refresh_token, scope)

    def fail_refresh(self, lease, *, invalid):
        self.failed = (lease, invalid)


def test_successful_refresh_persists_rotated_token():
    store = RefreshStore()
    oauth = SimpleNamespace(
        refresh=lambda _token: None,
    )

    async def refresh(_token):
        return HubSpotTokenResponse(
            access_token="access-two",
            refresh_token="refresh-two",
            expires_in=1800,
            scope="scope-two",
        )

    oauth.refresh = refresh
    manager = HubSpotTokenManager(oauth, store)
    assert asyncio.run(manager.get_access_token()) == "access-two"
    assert store.completed[1:] == ("refresh-two", "scope-two")
    assert store.failed is None


def test_account_metadata_is_extracted_from_nested_mcp_text_json():
    result = {
        "content": [
            {
                "type": "text",
                "text": '{"content":[{"type":"text","text":"{\\"portalId\\":51921668,\\"accountName\\":\\"Brunova\\",\\"userId\\":123,\\"email\\":\\"operator@example.com\\"}"}]}',
            }
        ]
    }

    account = account_metadata_from_result(result)

    assert account.portal_id == "51921668"
    assert account.account_name == "Brunova"
    assert account.user_id == "123"
    assert account.user_email == "operator@example.com"
