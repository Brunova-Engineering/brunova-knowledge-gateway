import pytest

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.policies.hubspot import HubSpotToolPolicy


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
