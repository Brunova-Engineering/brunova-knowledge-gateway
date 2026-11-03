import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from mcp import Client
from fastapi.testclient import TestClient

from app.auth.principals import (
    CapabilityScope,
    CrmWorkerPrincipalRecord,
    DeveloperPrincipalRecord,
    Principal,
    PrincipalResolver,
    ProviderScope,
    SignalWorkerPrincipalRecord,
    WhatsAppWorkerPrincipalRecord,
    authorize_workspace_operation,
    bind_principal,
    reset_principal,
)
from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.source_registry import SourceDefinition


def record(token: str, **overrides) -> DeveloperPrincipalRecord:
    values = {
        "id": "dev_example",
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "providers": {"workspace": True},
        "sources": ["hq_client"],
        "capabilities": {
            "read": True,
            "create": True,
            "update": True,
            "move": True,
        },
    }
    values.update(overrides)
    return DeveloperPrincipalRecord.model_validate(values)


def source(source_id="hq_client", *, create=True) -> SourceDefinition:
    return SourceDefinition.model_validate(
        {
            "id": source_id,
            "name": source_id,
            "system": "google_workspace",
            "location_type": "shared_drive",
            "location_id": f"location_{source_id}",
            "classification": "client_shareable",
            "owner": ["Management"],
            "status": "active",
            "capabilities": {
                "read": True,
                "create": create,
                "update": True,
                "move": True,
            },
        }
    )


def test_resolver_preserves_management_and_resolves_developer_without_plaintext():
    developer_token = "developer-token-with-at-least-256-bits-of-randomness"
    resolver = PrincipalResolver("management-token", (record(developer_token),))

    management, management_error = resolver.resolve("Bearer management-token")
    developer, developer_error = resolver.resolve(f"Bearer {developer_token}")

    assert management_error is None
    assert management.type == "management"
    assert developer_error is None
    assert developer.id == "dev_example"
    assert developer.sources == frozenset({"hq_client"})
    assert developer_token not in repr(resolver)


def test_resolver_supports_restricted_acquisition_signal_worker():
    worker_token = "worker-token-with-at-least-256-bits-of-randomness"
    worker_record = SignalWorkerPrincipalRecord.model_validate(
        {
            "id": "acquisition_worker",
            "type": "signal_worker",
            "token_sha256": hashlib.sha256(worker_token.encode()).hexdigest(),
            "signal_types": ["acquisition_work_available"],
            "operations": ["list", "get", "claim", "release", "complete", "dismiss"],
        }
    )
    principal, error = PrincipalResolver("management-token", (worker_record,)).resolve(
        f"Bearer {worker_token}"
    )
    assert error is None
    assert principal.type == "signal_worker"
    assert principal.signal_types == frozenset({"acquisition_work_available"})
    assert principal.providers == ProviderScope()
    assert worker_token not in repr(principal)


def test_resolver_supports_one_session_whatsapp_worker():
    token = "whatsapp-worker-token-with-at-least-256-bits-of-randomness"
    record = WhatsAppWorkerPrincipalRecord.model_validate({
        "id": "acquisition_whatsapp_worker", "type": "whatsapp_worker",
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "session_id": "brunova", "operations": ["history", "send"],
    })
    principal, error = PrincipalResolver("management-token", (record,)).resolve(
        f"Bearer {token}")
    assert error is None
    assert principal.type == "whatsapp_worker"
    assert principal.whatsapp_session_id == "brunova"
    assert principal.whatsapp_operations == frozenset({"history", "send"})
    assert principal.providers == ProviderScope(openwa=True)
    assert token not in repr(principal)


def test_resolver_supports_bounded_crm_worker():
    token = "crm-worker-token-with-at-least-256-bits-of-randomness"
    record = CrmWorkerPrincipalRecord.model_validate({
        "id": "acquisition_crm_worker", "type": "crm_worker",
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
    })
    principal, error = PrincipalResolver("management-token", (record,)).resolve(f"Bearer {token}")
    assert error is None
    assert principal.type == "crm_worker"
    assert principal.providers == ProviderScope(hubspot=True)
    assert token not in repr(principal)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"status": "revoked"}, "principal_revoked"),
        (
            {"expires_at": datetime.now(timezone.utc) - timedelta(minutes=1)},
            "principal_expired",
        ),
    ],
)
def test_resolver_rejects_revoked_and_expired_principals(overrides, expected):
    token = "developer-token"
    principal, error = PrincipalResolver(
        "management-token", (record(token, **overrides),)
    ).resolve(f"Bearer {token}")

    assert principal.id == "dev_example"
    assert error == expected


def test_authorization_is_principal_source_capability_intersection():
    principal = Principal.from_record(record("developer-token"))

    authorize_workspace_operation(principal, capability="create", source=source())
    with pytest.raises(WorkspaceAdapterError, match="not authorized") as wrong_source:
        authorize_workspace_operation(
            principal, capability="read", source=source("other_client")
        )
    with pytest.raises(WorkspaceAdapterError) as source_denied:
        authorize_workspace_operation(
            principal, capability="create", source=source(create=False)
        )

    assert wrong_source.value.code == "source_denied"
    assert source_denied.value.code == "source_capability_denied"


def test_developer_mcp_catalog_is_workspace_only_and_capability_filtered(monkeypatch):
    import app.mcp_server as mcp_module

    principal = Principal.from_record(record("developer-token"))
    monkeypatch.setattr(
        mcp_module,
        "get_runtime_gateway",
        lambda: SimpleNamespace(
            registry=SimpleNamespace(sources=(source(),))
        ),
    )

    async def scenario():
        context_token = bind_principal(principal)
        try:
            async with Client(mcp_module.mcp_server) as client:
                tools = await client.list_tools()
                denied = await client.call_tool("hubspot_get_user_details", {})
                openwa_denied = await client.call_tool("openwa_MessageHistory", {})
                return {tool.name for tool in tools.tools}, denied, openwa_denied
        finally:
            reset_principal(context_token)

    import asyncio

    names, denied, openwa_denied = asyncio.run(scenario())
    assert "retrieve_document" in names
    assert "create_source_artifact" in names
    assert "delete_source_artifact" not in names
    assert "share_source_artifact" not in names
    assert "convert_source_artifact" not in names
    assert not any(name.startswith("hubspot_") for name in names)
    assert not any(name.startswith("n8n_") for name in names)
    assert not any(name.startswith("openwa_") for name in names)
    assert "discover_source_candidates" not in names
    assert "inspect_visual_asset" not in names
    assert "edit_source_document_images" not in names
    assert "inspect_docx_structure" not in names
    assert "edit_source_docx" not in names
    assert "validate_docx_structure" not in names
    assert denied.is_error is True
    assert "tool_denied" in denied.content[0].text
    assert openwa_denied.is_error is True
    assert "tool_denied" in openwa_denied.content[0].text


def test_principal_registry_can_reload_from_secret_manager_mounted_file(
    monkeypatch, tmp_path
):
    token = "developer-token"
    path = tmp_path / "principals.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "principals": [record(token).model_dump(mode="json")],
            }
        )
    )
    monkeypatch.setenv("BRUNOVA_GATEWAY_TOKEN", "management-token")
    monkeypatch.setenv("BRUNOVA_PRINCIPALS_FILE", str(path))

    principal, error = PrincipalResolver.from_environment().resolve(f"Bearer {token}")

    assert error is None
    assert principal.id == "dev_example"


def test_developer_http_rejects_unassigned_source_and_provider(
    monkeypatch, ephemeral_gateway_token
):
    from app.main import app

    developer_token = "developer-http-token"
    monkeypatch.setenv(
        "BRUNOVA_PRINCIPALS_JSON",
        json.dumps(
            {
                "version": 1,
                "principals": [record(developer_token).model_dump(mode="json")],
            }
        ),
    )
    headers = {"Authorization": f"Bearer {developer_token}"}

    client = TestClient(app)
    source_denied = client.get("/sources/career_ops", headers=headers)
    provider_denied = client.get("/auth/hubspot/status", headers=headers)

    assert source_denied.status_code == 403
    assert source_denied.json() == {"error": "source_denied"}
    assert provider_denied.status_code == 403
    assert provider_denied.json() == {"error": "provider_denied"}
