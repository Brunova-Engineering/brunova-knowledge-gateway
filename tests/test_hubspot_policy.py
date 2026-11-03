import pytest

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.policies.hubspot import HubSpotToolPolicy
from app.mcp_server import _bounded_crm_worker_transition, _crm_company_write_available
import asyncio
import hashlib
import app.mcp_server as mcp_module
from app.auth.principals import CrmWorkerPrincipalRecord, Principal, bind_principal, reset_principal


def test_crm_worker_accepts_only_c7_company_transitions():
    create = {"confirmationStatus": "CONFIRMED", "createRequest": {"objects": [{
        "objectType": "companies", "properties": {
            "domain": "example.mx", "name": "Example Logistics",
            "hs_lead_status": "CONNECTED", "lifecyclestage": "salesqualifiedlead",
        },
    }]}}
    update = {"confirmationStatus": "CONFIRMED", "updateRequest": {"objects": [{
        "objectType": "companies", "objectId": 1234, "properties": {
            "hs_lead_status": "BAD_TIMING", "lifecyclestage": "salesqualifiedlead",
        },
    }]}}
    assert _bounded_crm_worker_transition(create, True)
    assert _bounded_crm_worker_transition(update, True)
    assert not _bounded_crm_worker_transition(create, False)
    assert not _bounded_crm_worker_transition({**create, "confirmationStatus": "CONFIRMATION_WAIVED_FOR_SESSION"}, True)
    assert not _bounded_crm_worker_transition({**create, "deleteRequest": {}}, True)
    assert not _bounded_crm_worker_transition({"confirmationStatus": "CONFIRMED", "createRequest": {"objects": [{
        "objectType": "contacts", "properties": create["createRequest"]["objects"][0]["properties"],
    }]}}, True)
    assert not _bounded_crm_worker_transition({"confirmationStatus": "CONFIRMED", "updateRequest": {"objects": [{
        "objectType": "companies", "objectId": 1234, "properties": {
            "hs_lead_status": "CONNECTED", "lifecyclestage": "salesqualifiedlead",
        },
    }]}}, True)


def test_crm_worker_catalog_and_call_fail_closed():
    principal = Principal.from_record(CrmWorkerPrincipalRecord.model_validate({
        "id": "acquisition_crm_worker", "type": "crm_worker",
        "token_sha256": hashlib.sha256(b"worker-token").hexdigest(),
    }))

    async def scenario():
        binding = bind_principal(principal)
        try:
            names = {tool.name for tool in await mcp_module.mcp_server.list_tools()}
            denied = await mcp_module.mcp_server.call_tool("hubspot_get_crm_objects", {"arguments": {}})
            return names, denied
        finally:
            reset_principal(binding)

    names, denied = asyncio.run(scenario())
    assert names == {"hubspot_manage_crm_objects"}
    assert denied.is_error


def test_crm_worker_requires_current_company_write_availability():
    import json

    def details(status):
        return {"content": [{"type": "text", "text": json.dumps({
            "toolInformation": {"crmObjectTypeAvailability": {
                "COMPANY": {"read": "AVAILABLE", "write": status},
            }},
        })}]}

    assert _crm_company_write_available(details("AVAILABLE"))
    assert not _crm_company_write_available(details("REQUIRES_REAUTHORIZATION"))
    assert not _crm_company_write_available({"content": [{"type": "text", "text": "{}"}]})


def test_read_tool_is_allowed_without_approval():
    decision = HubSpotToolPolicy().authorize(
        "search_crm_objects", {"object_type": "companies"}
    )
    assert decision.classification == "read"
    assert decision.approval_required is False


def test_unknown_tool_is_default_denied():
    with pytest.raises(WorkspaceAdapterError) as raised:
        HubSpotToolPolicy().authorize("future_ambiguous_tool", {})
    assert raised.value.code == "hubspot_tool_not_allowed"


def test_mutation_requires_explicit_intent_but_no_external_approval():
    policy = HubSpotToolPolicy()
    with pytest.raises(WorkspaceAdapterError) as missing_intent:
        policy.authorize(
            "manage_crm_objects", {}, approval_reference="approval-123"
        )
    assert missing_intent.value.code == "hubspot_mutation_intent_required"

    decision = policy.authorize(
        "manage_crm_objects",
        {"object_type": "contacts"},
        explicit_intent=True,
    )
    assert decision.classification == "mutation"
    assert decision.approval_required is False


def test_credentials_are_rejected_even_when_nested():
    with pytest.raises(WorkspaceAdapterError) as raised:
        HubSpotToolPolicy().authorize(
            "search_crm_objects", {"filter": {"access_token": "forbidden"}}
        )
    assert raised.value.code == "hubspot_arguments_invalid"
