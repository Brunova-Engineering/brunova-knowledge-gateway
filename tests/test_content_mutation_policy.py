import pytest

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.auth.principals import (
    CapabilityScope,
    Principal,
    ProviderScope,
    bind_principal,
    reset_principal,
)
from app.config.settings import Settings
from app.policies.content_mutation import ContentMutationPolicy, MutationOperation
from app.policies.source_access import SourceAccessPolicy
from app.source_registry import SourceDefinition, SourceRegistry, SourceRegistryDocument


def settings():
    return Settings(
        workspace_delegated_user="reader@example.com",
        workspace_service_account_email="gateway@project.iam.gserviceaccount.com",
        workspace_doc_max_chars=100,
        workspace_sheet_max_cells=100,
        workspace_blocked_source_ids=(),
        workspace_source_max_depth=20,
        workspace_audit_enabled=True,
        workspace_source_registry_path="unused.yaml",
    )


def policy(
    *,
    status="active",
    create=True,
    update=False,
    move=False,
    delete=False,
    share=False,
    convert=False,
):
    source = SourceDefinition.model_validate(
        {
            "id": "safe_templates",
            "name": "Safe Templates",
            "system": "google_workspace",
            "location_type": "folder",
            "location_id": "allowed_folder_123",
            "classification": "management_only",
            "owner": ["Management"],
            "status": status,
            "capabilities": {
                "read": True,
                "create": create,
                "update": update,
                "move": move,
                "delete": delete,
                "share": share,
                "convert": convert,
            },
        }
    )
    registry = SourceRegistry(SourceRegistryDocument(version=1, sources=(source,)))
    source_policy = SourceAccessPolicy(settings(), registry)
    return ContentMutationPolicy(registry, source_policy)


def test_approved_active_source_with_capability_is_allowed():
    allowed = policy().authorize(
        source_id="safe_templates",
        operation=MutationOperation.CREATE,
        approval_reference="decision-v013-test",
    )

    assert allowed.definition.id == "safe_templates"


def test_unregistered_source_is_blocked():
    with pytest.raises(WorkspaceAdapterError) as captured:
        policy().authorize(
            source_id="unknown_source",
            operation=MutationOperation.CREATE,
            approval_reference="decision-v013-test",
        )

    assert captured.value.code == "source_not_found"


def test_disabled_source_is_blocked():
    with pytest.raises(WorkspaceAdapterError) as captured:
        policy(status="disabled").authorize(
            source_id="safe_templates",
            operation=MutationOperation.CREATE,
            approval_reference="decision-v013-test",
        )

    assert captured.value.code == "source_disabled"


def test_missing_capability_is_blocked():
    with pytest.raises(WorkspaceAdapterError) as captured:
        policy(create=False).authorize(
            source_id="safe_templates",
            operation=MutationOperation.CREATE,
            approval_reference="decision-v013-test",
        )

    assert captured.value.code == "source_capability_denied"


def test_management_mutation_needs_no_external_approval_reference():
    allowed = policy().authorize(
        source_id="safe_templates",
        operation=MutationOperation.CREATE,
        approval_reference="",
    )

    assert allowed.definition.id == "safe_templates"


def test_developer_scope_authorizes_mutation_without_external_approval():
    principal = Principal(
        id="developer_test",
        type="developer",
        status="active",
        providers=ProviderScope(workspace=True),
        sources=frozenset({"safe_templates"}),
        capabilities=CapabilityScope(create=True, update=True, move=True),
    )
    token = bind_principal(principal)
    try:
        allowed = policy().authorize(
            source_id="safe_templates",
            operation=MutationOperation.CREATE,
            approval_reference="",
        )
    finally:
        reset_principal(token)

    assert allowed.definition.id == "safe_templates"


def test_approval_reference_cannot_elevate_developer_capability():
    principal = Principal(
        id="developer_test",
        type="developer",
        status="active",
        providers=ProviderScope(workspace=True),
        sources=frozenset({"safe_templates"}),
        capabilities=CapabilityScope(create=True),
    )
    token = bind_principal(principal)
    try:
        with pytest.raises(WorkspaceAdapterError) as captured:
            policy(delete=True).authorize(
                source_id="safe_templates",
                operation=MutationOperation.DELETE,
                approval_reference="decision-cannot-elevate",
            )
    finally:
        reset_principal(token)

    assert captured.value.code == "capability_denied"


@pytest.mark.parametrize(
    ("operation", "capability"),
    [
        (MutationOperation.DELETE, "delete"),
        (MutationOperation.SHARE, "share"),
        (MutationOperation.CONVERT, "convert"),
    ],
)
def test_mutations_require_their_exact_capability(operation, capability):
    allowed = policy(**{capability: True}).authorize(
        source_id="safe_templates",
        operation=operation,
        approval_reference="decision-v015-test",
    )

    assert allowed.definition.id == "safe_templates"

    with pytest.raises(WorkspaceAdapterError) as captured:
        policy().authorize(
            source_id="safe_templates",
            operation=operation,
            approval_reference="decision-v015-test",
        )

    assert captured.value.code == "source_capability_denied"


@pytest.mark.parametrize("audience", ["", "not-an-email", "a@b@c.com"])
def test_share_audience_must_be_one_explicit_email(audience):
    with pytest.raises(WorkspaceAdapterError) as captured:
        ContentMutationPolicy.validate_audience(audience)

    assert captured.value.code == "mutation_audience_invalid"


def test_share_audience_is_normalized():
    assert (
        ContentMutationPolicy.validate_audience("  REVIEWER@BRUNOVA.MX ")
        == "reviewer@brunova.mx"
    )
