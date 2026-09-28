import asyncio
import importlib
import json
from dataclasses import replace
from unittest.mock import Mock

from mcp import Client

from app.adapters.google_workspace.models import (
    ArtifactMetadata,
    DriveFile,
    GoogleDocContent,
    SheetRangeContent,
    SourceMetadata,
    WorkspaceResource,
)
from app.artifact_refs import ArtifactReferenceCodec
from app.auth.principals import (
    CapabilityScope,
    Principal,
    ProviderScope,
    bind_principal,
    reset_principal,
)
from app.config.settings import Settings
from app.document_production import (
    DocumentEditResult,
    DocumentStructure,
    ParagraphSummary,
    SegmentSummary,
    TableCellSummary,
    TableSummary,
    TabStructure,
)
from app.operation_history import GovernedOperation, OperationHistoryEntry
from app.policies.content_mutation import ContentMutationPolicy
from app.policies.source_access import SourceAccessPolicy
from app.runtime import KnowledgeRuntime
from app.source_discovery.interface import (
    CandidateSource,
    DiscoveryResult,
    SourceProposalSuggestion,
    candidate_identifier,
)
from app.source_proposal_store import ProposalObjectConflict, YamlSourceProposalStore
from app.source_registry import SourceDefinition, SourceRegistry, SourceRegistryDocument

mcp_module = importlib.import_module("app.mcp_server")


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


def registry(*, mutation_enabled=True, include_archive=False):
    source = SourceDefinition.model_validate(
        {
            "id": "career_ops",
            "name": "Career Ops",
            "system": "google_workspace",
            "location_type": "folder",
            "location_id": "allowed_folder_123",
            "classification": "management_only",
            "owner": ["Jorge", "Nat"],
            "status": "active",
            "capabilities": {
                "read": True,
                "create": mutation_enabled,
                "update": mutation_enabled,
                "move": mutation_enabled,
                "delete": mutation_enabled,
                "share": mutation_enabled,
                "convert": mutation_enabled,
            },
        }
    )
    sources = [source]
    if include_archive:
        sources.append(
            SourceDefinition.model_validate(
                {
                    "id": "legacy_archive",
                    "name": "98 Legacy",
                    "system": "google_workspace",
                    "location_type": "folder",
                    "location_id": "archive_folder_123",
                    "classification": "management_only",
                    "owner": ["Management"],
                    "status": "active",
                    "source_type": "archive_destination",
                    "capabilities": {"read": False, "move": True},
                }
            )
        )
    return SourceRegistry(SourceRegistryDocument(version=1, sources=tuple(sources)))


class FakeWorkspaceAdapter:
    def __init__(self, *, in_source=True):
        self.in_source = in_source
        self.created = []
        self.moved = []
        self.deleted = []
        self.shared = []
        self.converted = []
        self.copied = []
        self.renamed = []

    def list_source_files(self, *, source, limit, source_policy):
        assert source.definition.id == "career_ops"
        assert limit == 100
        return [
            DriveFile(
                id="document_12345",
                name="Career Roadmap",
                type="document",
                source=SourceMetadata(
                    id="career_ops",
                    name="Career Ops",
                    classification="management_only",
                ),
            ),
            DriveFile(
                id="sheet_123456789",
                name="Metrics",
                type="spreadsheet",
                source=SourceMetadata(
                    id="career_ops",
                    name="Career Ops",
                    classification="management_only",
                ),
            ),
        ]

    def inspect_source_artifacts(self, *, source, limit):
        assert source.definition.id == "career_ops"
        assert limit == 100
        return [
            ArtifactMetadata(
                name="Forecast.xlsm",
                type="office_artifact",
                mime_type="application/vnd.ms-excel.sheet.macroenabled.12",
                extension="xlsm",
                size=4096,
                modified_time="2026-08-23T11:00:00Z",
                source_id=source.definition.id,
            )
        ]

    def find_resources(self, *, name, mime_type=None, source=None):
        assert source is not None
        resource = self.get_resource("document_12345")
        return [replace(resource, name=name)] if mime_type in (None, resource.mime_type) else []

    def logical_path(self, resource):
        return f"Career Ops/{resource.name}"

    def get_resource(self, resource_id):
        is_destination = resource_id in (
            "destination_folder_123",
            "archive_folder_123",
            "allowed_folder_123",
        )
        office_mime_type = None
        if resource_id.startswith("xlsx"):
            office_mime_type = (
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            )
        elif resource_id.startswith("xlsm"):
            office_mime_type = "application/vnd.ms-excel.sheet.macroEnabled.12"
        return WorkspaceResource(
            id=resource_id,
            name=(
                "Controlled workbook.xlsx"
                if resource_id.startswith("xlsx")
                else (
                    "Controlled workbook.xlsm"
                    if resource_id.startswith("xlsm")
                    else "Controlled resource"
                )
            ),
            mime_type=(
                "application/vnd.google-apps.folder"
                if is_destination
                else (
                    office_mime_type
                    or (
                        "application/vnd.google-apps.spreadsheet"
                        if resource_id.startswith("spreadsheet")
                        else "application/vnd.google-apps.document"
                    )
                )
            ),
            modified_time="2026-08-22T00:00:00Z",
            drive_id=None,
            ancestor_ids=(
                ("allowed_folder_123",)
                if self.in_source
                else ("another_folder_123",)
            ),
            parent_ids=("allowed_folder_123",),
        )

    def create_document(self, *, source, name):
        self.created.append((source.definition.id, name))
        return WorkspaceResource(
            id="created_document_123",
            name=name,
            mime_type="application/vnd.google-apps.document",
            modified_time="2026-08-22T00:00:00Z",
            drive_id=None,
            ancestor_ids=("allowed_folder_123",),
            parent_ids=("allowed_folder_123",),
        )

    def create_spreadsheet(self, *, source, name):
        self.created.append((source.definition.id, name))
        return WorkspaceResource(
            id="created_spreadsheet_123",
            name=name,
            mime_type="application/vnd.google-apps.spreadsheet",
            modified_time="2026-08-22T00:00:00Z",
            drive_id=None,
            ancestor_ids=("allowed_folder_123",),
            parent_ids=("allowed_folder_123",),
        )

    def move_resource(self, *, resource, destination):
        self.moved.append((resource.id, destination.id))
        return WorkspaceResource(
            id=resource.id,
            name=resource.name,
            mime_type=resource.mime_type,
            modified_time=resource.modified_time,
            drive_id=resource.drive_id,
            ancestor_ids=(destination.id, "allowed_folder_123"),
            parent_ids=(destination.id,),
        )

    def copy_resource(self, *, resource, name, destination):
        self.copied.append((resource.id, name, destination.id))
        return replace(
            resource,
            id="copied_document_123",
            name=name,
            ancestor_ids=(destination.id,),
            parent_ids=(destination.id,),
        )

    def rename_resource(self, *, resource, name):
        self.renamed.append((resource.id, name))
        return replace(resource, name=name)

    def delete_resource(self, *, resource):
        self.deleted.append(resource.id)
        return resource

    def share_resource(self, *, resource, audience):
        self.shared.append((resource.id, audience))
        return resource

    def convert_resource(self, *, resource, target_mime_type, target_name):
        self.converted.append((resource.id, target_mime_type, target_name))
        return WorkspaceResource(
            id=f"converted_{resource.id}",
            name=target_name,
            mime_type=target_mime_type,
            modified_time="2026-08-23T12:00:00Z",
            drive_id=None,
            ancestor_ids=resource.ancestor_ids,
            parent_ids=resource.parent_ids,
        )


class FakeDocsAdapter:
    max_chars = 100

    def __init__(self):
        self.appended = []
        self.structured_edits = []
        self.tab_mutations = []
        self.revision = 1
        self.tabs = [{"id": "tab-1", "title": "Tab 1", "index": 0}]

    def get_document(self, resource, *, max_chars):
        return GoogleDocContent(
            id=resource.id,
            name=resource.name,
            mime_type=resource.mime_type,
            modified_time=resource.modified_time,
            text="authorized content",
            truncated=False,
            limit=max_chars,
        )

    def append_text(self, resource, *, text):
        self.appended.append((resource.id, text))

    def inspect_structure(self, resource, *, artifact_ref, source_id, reference_codec):
        return DocumentStructure(
            artifact_ref=artifact_ref,
            name=resource.name,
            source_id=source_id,
            revision_id=f"revision-{self.revision}",
            tabs=[
                TabStructure(
                    tab_ref=reference_codec.encode_tab(
                        source_id=source_id, artifact_id=resource.id, tab_id=item["id"]
                    ),
                    title=item["title"],
                    index=item["index"],
                    nesting_level=0,
                    paragraphs=[],
                    tables=[],
                )
                for item in self.tabs
            ],
            headers=[],
            footers=[],
            image_count=0,
            document_style={},
            placeholders=[],
            total_characters=0,
        )

    def edit_structure(
        self, resource, *, required_revision_id, operations, tab_id_resolver
    ):
        self.structured_edits.append((resource.id, required_revision_id, operations))
        self.revision += 1
        return f"revision-{self.revision}"

    def create_tab(
        self,
        resource,
        *,
        title,
        required_revision_id,
        index=None,
        parent_tab_id=None,
    ):
        self.revision += 1
        position = len(self.tabs) if index is None else index
        for item in self.tabs:
            if item["index"] >= position:
                item["index"] += 1
        self.tabs.append(
            {"id": f"tab-{len(self.tabs) + 1}", "title": title, "index": position}
        )
        self.tabs.sort(key=lambda item: item["index"])
        self.tab_mutations.append(("create", title, parent_tab_id))
        return f"revision-{self.revision}"

    def rename_tab(self, resource, *, tab_id, title, required_revision_id):
        next(item for item in self.tabs if item["id"] == tab_id)["title"] = title
        self.revision += 1
        self.tab_mutations.append(("rename", tab_id, title))
        return f"revision-{self.revision}"

    def delete_tab(self, resource, *, tab_id, required_revision_id):
        self.tabs = [item for item in self.tabs if item["id"] != tab_id]
        for index, item in enumerate(self.tabs):
            item["index"] = index
        self.revision += 1
        self.tab_mutations.append(("delete", tab_id))
        return f"revision-{self.revision}"


class FakeSheetsAdapter:
    max_cells = 100

    def __init__(self):
        self.operations = []
        self.ranges = []

    def get_structure(self, resource):
        return {
            "title": resource.name,
            "locale": "en_US",
            "time_zone": "America/Mexico_City",
            "sheets": [
                {
                    "sheet_id": "7",
                    "title": "Summary",
                    "index": 0,
                    "row_count": 100,
                    "column_count": 20,
                    "frozen_row_count": 1,
                    "frozen_column_count": 0,
                },
                {
                    "sheet_id": "8",
                    "title": "Data",
                    "index": 1,
                    "row_count": 200,
                    "column_count": 10,
                    "frozen_row_count": 0,
                    "frozen_column_count": 0,
                },
                {
                    "sheet_id": "9",
                    "title": "Team's Tasks",
                    "index": 2,
                    "row_count": 50,
                    "column_count": 8,
                    "frozen_row_count": 0,
                    "frozen_column_count": 0,
                },
            ],
        }

    def get_range(self, resource, *, range_name, value_render_option="FORMATTED_VALUE"):
        self.ranges.append((resource.id, range_name, value_render_option))
        if value_render_option == "FORMULA":
            values = [["=SUM(A2:A10)"]]
        elif range_name == "Summary!A1:B1":
            values = [["Name", "Total"]]
        else:
            values = [["Header"], ["Value"]]
        return SheetRangeContent(
            spreadsheet_id=resource.id,
            range=range_name,
            values=values,
        )

    def apply_operations(
        self, resource, *, operations, sheet_id_resolver, sheet_title_resolver
    ):
        for operation in operations:
            sheet_ref = getattr(operation, "sheet_ref", None)
            if sheet_ref:
                assert sheet_id_resolver(sheet_ref) in (7, 8, 9)
                assert sheet_title_resolver(sheet_ref) in (
                    "Summary", "Data", "Team's Tasks"
                )
        self.operations.extend(operations)


class FakeDiscovery:
    def __init__(self, source_registry):
        self.source_registry = source_registry

    def discover(self, *, limit=25):
        assert limit in (10, 100)
        before = self.source_registry.sources
        candidate = CandidateSource(
            system="google_workspace",
            location_type="shared_drive",
            location_id="finance_drive_123",
            name="Finance",
            classification_suggestion="management_only",
            reasons=("new shared drive detected",),
        )
        result = DiscoveryResult(
            candidates=(candidate,),
            proposals=(
                SourceProposalSuggestion(
                    candidate=candidate,
                    proposed_id="finance",
                    suggested_classification="management_only",
                    confidence="medium",
                    reasons=candidate.reasons,
                ),
            ),
        )
        assert self.source_registry.sources == before
        return result


class MemoryObjectBackend:
    def __init__(self):
        self.content = None
        self.generation = 0

    def read(self):
        return self.content, self.generation

    def write(self, content, *, generation):
        if generation != self.generation:
            raise ProposalObjectConflict
        self.content = content
        self.generation += 1


def runtime(*, in_source=True, mutation_enabled=True, include_archive=False):
    source_registry = registry(
        mutation_enabled=mutation_enabled,
        include_archive=include_archive,
    )
    runtime_settings = settings()
    source_policy = SourceAccessPolicy(runtime_settings, source_registry)
    return KnowledgeRuntime(
        settings=runtime_settings,
        registry=source_registry,
        source_policy=source_policy,
        workspace_adapter=FakeWorkspaceAdapter(in_source=in_source),
        docs_adapter=FakeDocsAdapter(),
        sheets_adapter=FakeSheetsAdapter(),
        source_discovery=FakeDiscovery(source_registry),
        proposal_store=YamlSourceProposalStore(MemoryObjectBackend()),
        mutation_policy=ContentMutationPolicy(source_registry, source_policy),
        artifact_reference_codec=ArtifactReferenceCodec.for_testing(),
    )


def run(coro):
    return asyncio.run(coro)


def test_mcp_exposes_only_governed_tools(monkeypatch):
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: runtime())

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            return await client.list_tools()

    result = run(scenario())

    assert {tool.name for tool in result.tools} == {
        "acquisition_get_cycle_policy", "acquisition_list_opportunity_pool", "acquisition_get_cycle_review",
        "acquisition_get_policy_settings", "acquisition_propose_discovery_limit", "acquisition_confirm_policy_proposal",
        "acquisition_get_operating_model",
        "acquisition_get_activation_preflight",
        "acquisition_get_discovery", "acquisition_request_discovery_planning",
        "acquisition_archive_candidate", "acquisition_restore_candidate",
        "acquisition_request_authenticated_research", "acquisition_record_commercial_calibration",
        "acquisition_record_work_plan", "acquisition_approve_conversation_worthiness",
        "acquisition_approve_work_plan", "acquisition_record_allocation_outcomes",
        "acquisition_record_exploratory_wave", "acquisition_record_exploratory_wave_review",
        "acquisition_record_target_resolution_direction", "acquisition_record_target_resolution_result",
            "acquisition_record_copy_review",
            "acquisition_record_7eb2_decisions_and_draft", "acquisition_authorize_7eb2_scania_attempt_1",
        "acquisition_record_discovery_investigation",
        "acquisition_request_candidate_investigation",
        "acquisition_begin_discovery_batch", "acquisition_finish_discovery_batch",
        "acquisition_resolve_accounts", "acquisition_retain_opportunity", "acquisition_pause_cycle",
        "acquisition_compose_wave", "acquisition_review_wave", "acquisition_request_reconsideration", "acquisition_stop_discovery",
        "acquisition_list_crm", "acquisition_request_pre_outbound_sync", "acquisition_request_crm_reconciliation",
        "acquisition_review_commercial_handoff", "acquisition_accept_commercial_handoff",
        "acquisition_list_cycles", "acquisition_get_cycle", "acquisition_list_accounts",
        "acquisition_get_account", "acquisition_list_priorities", "acquisition_list_attention",
        "acquisition_get_attention", "acquisition_get_attention_counts", "acquisition_list_work",
        "acquisition_get_health", "acquisition_request_research", "acquisition_get_buyer_dry_run", "acquisition_request_buyer_dry_run",
        "acquisition_record_attention_disposition", "acquisition_authorize_controlled_effect",
        "acquisition_request_effect_reconciliation", "acquisition_acknowledge_effect_attention", "acquisition_edit_message_draft",
        "acquisition_get_effect", "acquisition_get_effect_bindings", "acquisition_list_effect_attention",
        "list_sources",
        "list_source_documents",
        "retrieve_document",
        "retrieve_sheet_range",
        "inspect_sheet_validation",
        "discover_source_candidates",
        "get_source_candidate_details",
        "create_source_proposal",
        "list_source_proposals",
        "get_source_proposal",
        "create_source_artifact",
        "update_source_artifact",
        "move_source_artifact",
        "get_operation_history",
        "delete_source_artifact",
        "share_source_artifact",
        "inspect_source_artifacts",
        "convert_source_artifact",
        "resolve_source_artifact",
        "copy_source_artifact",
        "rename_source_artifact",
        "inspect_document_structure",
        "edit_source_document",
        "validate_document_structure",
        "inspect_spreadsheet_structure",
        "edit_source_spreadsheet",
        "validate_spreadsheet_structure",
        "inspect_document_tab",
        "create_document_tab",
        "rename_document_tab",
        "delete_document_tab",
        "inspect_visual_asset",
        "edit_source_document_images",
        "inspect_docx_structure",
        "edit_source_docx",
        "validate_docx_structure",
        "hubspot_list_tools",
            "hubspot_get_user_details",
            "hubspot_search_crm_objects",
            "hubspot_get_crm_objects",
            "hubspot_search_properties",
            "hubspot_get_properties",
            "hubspot_search_owners",
            "hubspot_call_read_tool",
            "hubspot_manage_crm_objects",
            "n8n_status",
            "n8n_list_tools",
            "openwa_status",
            "openwa_list_tools",
            "openwa_prepare_approval_reference",
            "list_agent_signals",
            "get_agent_signal",
            "claim_agent_signal",
            "complete_agent_signal",
            "dismiss_agent_signal",
            "release_agent_signal",
            "agent_signal_status",
            "get_agent_signal_operation_history",
    }
    tab_tool_schemas = json.dumps(
        [
            tool.model_dump()
            for tool in result.tools
            if tool.name
            in {
                "inspect_document_structure",
                "inspect_document_tab",
                "create_document_tab",
                "rename_document_tab",
                "delete_document_tab",
                "edit_source_document",
            }
        ]
    )
    assert '"tab_id"' not in tab_tool_schemas
    assert '"tab_ids"' not in tab_tool_schemas
    assert "tab_ref" in tab_tool_schemas
    governed_binary_schemas = json.dumps(
        [
            tool.model_dump()
            for tool in result.tools
            if tool.name
            in {
                "inspect_visual_asset",
                "edit_source_document_images",
                "inspect_docx_structure",
                "edit_source_docx",
                "validate_docx_structure",
            }
        ]
    )
    assert '"file_id"' not in governed_binary_schemas
    assert '"drive_id"' not in governed_binary_schemas
    assert "artifact_ref" in governed_binary_schemas


def test_structured_document_production_flow_uses_opaque_refs_and_audits(monkeypatch):
    gateway_runtime = runtime()
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: gateway_runtime)
    audit = Mock()
    monkeypatch.setattr(mcp_module, "emit_audit_record", audit)

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            resolved = await client.call_tool(
                "resolve_source_artifact",
                {
                    "source_id": "career_ops",
                    "name": "Approved Reference",
                    "artifact_type": "document",
                },
            )
            artifact_ref = resolved.structured_content["artifact_ref"]
            copied = await client.call_tool(
                "copy_source_artifact",
                {
                    "source_id": "career_ops",
                    "artifact_ref": artifact_ref,
                    "name": "Controlled Production Copy",
                    "approval_reference": "BR-019-test-copy",
                },
            )
            copied_ref = copied.structured_content["artifact"]["artifact_ref"]
            renamed = await client.call_tool(
                "rename_source_artifact",
                {
                    "source_id": "career_ops",
                    "artifact_ref": copied_ref,
                    "name": "Controlled Production Final",
                    "approval_reference": "BR-019-test-rename",
                },
            )
            inspected = await client.call_tool(
                "inspect_document_structure",
                {"source_id": "career_ops", "artifact_ref": copied_ref},
            )
            edited = await client.call_tool(
                "edit_source_document",
                {
                    "source_id": "career_ops",
                    "artifact_ref": copied_ref,
                    "required_revision_id": "revision-1",
                    "operations": [
                        {"operation": "insert_text_at_index", "index": 1, "text": "BR-019"},
                        {
                            "operation": "apply_paragraph_style",
                            "start_index": 1,
                            "end_index": 7,
                            "named_style_type": "TITLE",
                        },
                    ],
                    "approval_reference": "BR-019-test-edit",
                },
            )
            validated = await client.call_tool(
                "validate_document_structure",
                {
                    "source_id": "career_ops",
                    "artifact_ref": copied_ref,
                    "requirements": {"minimum_characters": 0},
                    "expected_revision_id": "revision-2",
                },
            )
            return resolved, copied, renamed, inspected, edited, validated

    resolved, copied, renamed, inspected, edited, validated = run(scenario())

    assert all(not result.is_error for result in (resolved, copied, renamed, inspected, edited, validated))
    assert resolved.structured_content["artifact_ref"].startswith("artifact_")
    assert "id" not in resolved.structured_content
    assert copied.structured_content["artifact"]["artifact_ref"].startswith("artifact_")
    assert "id" not in copied.structured_content["artifact"]
    assert inspected.structured_content["revision_id"] == "revision-1"
    assert edited.structured_content["revision_id"] == "revision-2"
    assert validated.structured_content["passed"] is True
    assert gateway_runtime.workspace_adapter.copied
    assert gateway_runtime.workspace_adapter.renamed
    assert len(gateway_runtime.docs_adapter.structured_edits[0][2]) == 2
    assert [call.kwargs["action"] for call in audit.call_args_list[-6:]] == [
        "resolve_source_artifact",
        "copy_source_artifact",
        "rename_source_artifact",
        "inspect_document_structure",
        "edit_source_document",
        "validate_document_structure",
    ]
    assert all("content" not in call.kwargs and "operations" not in call.kwargs for call in audit.call_args_list[-6:])


def test_governed_tab_flow_uses_opaque_refs_preserves_non_target_and_audits(monkeypatch):
    gateway_runtime = runtime()
    artifact_ref = gateway_runtime.artifact_reference_codec.encode(
        source_id="career_ops", artifact_id="document_12345"
    )
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: gateway_runtime)
    audit = Mock()
    monkeypatch.setattr(mcp_module, "emit_audit_record", audit)

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            initial = await client.call_tool(
                "inspect_document_structure",
                {"source_id": "career_ops", "artifact_ref": artifact_ref},
            )
            first_ref = initial.structured_content["tabs"][0]["tab_ref"]
            renamed = await client.call_tool(
                "rename_document_tab",
                {
                    "source_id": "career_ops",
                    "artifact_ref": artifact_ref,
                    "tab_ref": first_ref,
                    "title": "EN",
                    "required_revision_id": "revision-1",
                    "approval_reference": "tab-test-rename-en",
                },
            )
            created = await client.call_tool(
                "create_document_tab",
                {
                    "source_id": "career_ops",
                    "artifact_ref": artifact_ref,
                    "title": "ES",
                    "index": 1,
                    "required_revision_id": "revision-2",
                    "approval_reference": "tab-test-create-es",
                },
            )
            es_ref = created.structured_content["tab"]["tab_ref"]
            inspected_es = await client.call_tool(
                "inspect_document_tab",
                {
                    "source_id": "career_ops",
                    "artifact_ref": artifact_ref,
                    "tab_ref": es_ref,
                },
            )
            edited = await client.call_tool(
                "edit_source_document",
                {
                    "source_id": "career_ops",
                    "artifact_ref": artifact_ref,
                    "tab_ref": es_ref,
                    "required_revision_id": "revision-3",
                    "operations": [
                        {
                            "operation": "insert_text_at_index",
                            "index": 1,
                            "text": "Contenido ES",
                        }
                    ],
                    "approval_reference": "tab-test-edit-es",
                },
            )
            final = await client.call_tool(
                "inspect_document_structure",
                {"source_id": "career_ops", "artifact_ref": artifact_ref},
            )
            return initial, renamed, created, inspected_es, edited, final

    initial, renamed, created, inspected_es, edited, final = run(scenario())

    assert all(
        not result.is_error
        for result in (initial, renamed, created, inspected_es, edited, final)
    )
    assert initial.structured_content["tabs"][0]["tab_ref"].startswith("tab_")
    assert "tab_id" not in str(final.structured_content)
    assert renamed.structured_content["tab"]["title"] == "EN"
    assert created.structured_content["tab"]["title"] == "ES"
    assert inspected_es.structured_content["tab"]["title"] == "ES"
    assert edited.structured_content["revision_id"] == "revision-4"
    assert [tab["title"] for tab in final.structured_content["tabs"]] == ["EN", "ES"]
    scoped_operation = gateway_runtime.docs_adapter.structured_edits[-1][2][0]
    assert scoped_operation.tab_ref == created.structured_content["tab"]["tab_ref"]
    mutation_calls = [
        call
        for call in audit.call_args_list
        if call.kwargs["action"]
        in {"rename_document_tab", "create_document_tab", "edit_source_document"}
    ]
    assert [call.kwargs["action"] for call in mutation_calls] == [
        "rename_document_tab",
        "create_document_tab",
        "edit_source_document",
    ]
    assert [call.kwargs["approval_reference"] for call in mutation_calls] == [
        "tab-test-rename-en",
        "tab-test-create-es",
        "tab-test-edit-es",
    ]


def test_delete_document_tab_preserves_last_tab_and_deletes_only_leaf(monkeypatch):
    gateway_runtime = runtime()
    artifact_ref = gateway_runtime.artifact_reference_codec.encode(
        source_id="career_ops", artifact_id="document_12345"
    )
    first_ref = gateway_runtime.artifact_reference_codec.encode_tab(
        source_id="career_ops", artifact_id="document_12345", tab_id="tab-1"
    )
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: gateway_runtime)

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            only_tab = await client.call_tool(
                "delete_document_tab",
                {
                    "source_id": "career_ops",
                    "artifact_ref": artifact_ref,
                    "tab_ref": first_ref,
                    "required_revision_id": "revision-1",
                    "approval_reference": "tab-test-delete-only",
                },
            )
            created = await client.call_tool(
                "create_document_tab",
                {
                    "source_id": "career_ops",
                    "artifact_ref": artifact_ref,
                    "title": "Temporary",
                    "required_revision_id": "revision-1",
                    "approval_reference": "tab-test-create-temporary",
                },
            )
            deleted = await client.call_tool(
                "delete_document_tab",
                {
                    "source_id": "career_ops",
                    "artifact_ref": artifact_ref,
                    "tab_ref": created.structured_content["tab"]["tab_ref"],
                    "required_revision_id": "revision-2",
                    "approval_reference": "tab-test-delete-temporary",
                },
            )
            return only_tab, created, deleted

    only_tab, created, deleted = run(scenario())

    assert only_tab.is_error is True
    assert "document_tab_delete_invalid" in only_tab.content[0].text
    assert created.is_error is False
    assert deleted.is_error is False
    assert deleted.structured_content["result"] == "deleted"
    assert [item["title"] for item in gateway_runtime.docs_adapter.tabs] == ["Tab 1"]


def test_bilingual_quality_gate_validates_each_tab_and_structural_parity(monkeypatch):
    gateway_runtime = runtime()
    codec = gateway_runtime.artifact_reference_codec
    artifact_ref = codec.encode(source_id="career_ops", artifact_id="document_12345")
    en_ref = codec.encode_tab(
        source_id="career_ops", artifact_id="document_12345", tab_id="tab-en"
    )
    es_ref = codec.encode_tab(
        source_id="career_ops", artifact_id="document_12345", tab_id="tab-es"
    )

    def tab(tab_ref, title, heading):
        return TabStructure(
            tab_ref=tab_ref,
            title=title,
            index=0 if title == "EN" else 1,
            nesting_level=0,
            paragraphs=[
                ParagraphSummary(
                    start_index=1,
                    end_index=10,
                    text=f"{heading}\n",
                    named_style_type="HEADING_1",
                ),
                ParagraphSummary(
                    start_index=10,
                    end_index=20,
                    text="Item\n",
                    named_style_type="NORMAL_TEXT",
                    bullet=True,
                ),
            ],
            tables=[
                TableSummary(
                    start_index=20,
                    end_index=30,
                    rows=1,
                    columns=2,
                    tab_ref=tab_ref,
                    cells=[
                        TableCellSummary(
                            row=0, column=0, start_index=21, end_index=24, text="Artifact"
                        ),
                        TableCellSummary(
                            row=0, column=1, start_index=24, end_index=29, text="Status"
                        ),
                    ],
                )
            ],
        )

    gateway_runtime.docs_adapter.inspect_structure = Mock(
        return_value=DocumentStructure(
            artifact_ref=artifact_ref,
            name="Bilingual Artifact",
            source_id="career_ops",
            revision_id="revision-bilingual",
            tabs=[tab(en_ref, "EN", "Overview"), tab(es_ref, "ES", "Resumen")],
            headers=[
                SegmentSummary(segment_id="header-en", tab_ref=en_ref, paragraphs=[]),
                SegmentSummary(segment_id="header-es", tab_ref=es_ref, paragraphs=[]),
            ],
            footers=[
                SegmentSummary(segment_id="footer-en", tab_ref=en_ref, paragraphs=[]),
                SegmentSummary(segment_id="footer-es", tab_ref=es_ref, paragraphs=[]),
            ],
            image_count=0,
            document_style={},
            placeholders=[],
            total_characters=32,
        )
    )
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: gateway_runtime)

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            return await client.call_tool(
                "validate_document_structure",
                {
                    "source_id": "career_ops",
                    "artifact_ref": artifact_ref,
                    "requirements": {
                        "minimum_characters": 0,
                        "tab_requirements": [
                            {
                                "title": "EN",
                                "expected_headings": ["Overview"],
                                "minimum_table_count": 1,
                                "require_document_control": True,
                                "document_control_labels": ["Artifact", "Status"],
                                "require_header": True,
                                "require_footer": True,
                            },
                            {
                                "title": "ES",
                                "expected_headings": ["Resumen"],
                                "minimum_table_count": 1,
                                "require_document_control": True,
                                "document_control_labels": ["Artifact", "Status"],
                                "require_header": True,
                                "require_footer": True,
                            },
                        ],
                        "structural_parity_pairs": [
                            {"left_title": "EN", "right_title": "ES"}
                        ],
                    },
                    "expected_revision_id": "revision-bilingual",
                },
            )

    result = run(scenario())

    assert result.is_error is False
    assert result.structured_content["passed"] is True
    assert result.structured_content["checks"]["tab:EN:exists"] is True
    assert result.structured_content["checks"]["tab:ES:document_control"] is True
    assert result.structured_content["checks"]["parity:EN:ES"] is True


def test_artifact_resolution_rejects_missing_ambiguous_and_out_of_scope(monkeypatch):
    gateway_runtime = runtime()
    in_scope = gateway_runtime.workspace_adapter.get_resource("document_12345")
    outside = replace(in_scope, id="outside_document_123", ancestor_ids=("other_folder_123",))
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: gateway_runtime)

    async def call_with(matches):
        gateway_runtime.workspace_adapter.find_resources = Mock(return_value=matches)
        async with Client(mcp_module.mcp_server) as client:
            return await client.call_tool(
                "resolve_source_artifact",
                {"source_id": "career_ops", "name": "Exact Name"},
            )

    missing = run(call_with([]))
    ambiguous = run(call_with([in_scope, replace(in_scope, id="document_67890")]))
    out_of_scope = run(call_with([outside]))

    assert "artifact_not_found" in missing.content[0].text
    assert "artifact_selector_ambiguous" in ambiguous.content[0].text
    assert "artifact_not_found" in out_of_scope.content[0].text


def test_artifact_resolution_immediately_finds_new_nested_manual_artifact(monkeypatch):
    gateway_runtime = runtime()
    nested = replace(
        gateway_runtime.workspace_adapter.get_resource("document_12345"),
        id="manual_document_123",
        name="New Manual Manifest",
        ancestor_ids=("nested_folder_123", "allowed_folder_123"),
        parent_ids=("nested_folder_123",),
    )
    live_matches = []
    gateway_runtime.workspace_adapter.find_resources = Mock(
        side_effect=lambda **_: list(live_matches)
    )
    gateway_runtime.workspace_adapter.logical_path = Mock(
        return_value="Career Ops/Delivery/Nested/New Manual Manifest"
    )
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: gateway_runtime)

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            missing = await client.call_tool(
                "resolve_source_artifact",
                {
                    "source_id": "career_ops",
                    "logical_path": "Delivery/Nested/New Manual Manifest",
                    "artifact_type": "document",
                },
            )
            live_matches.append(nested)
            found = await client.call_tool(
                "resolve_source_artifact",
                {
                    "source_id": "career_ops",
                    "logical_path": "Delivery/Nested/New Manual Manifest",
                    "artifact_type": "document",
                },
            )
            return missing, found

    missing, found = run(scenario())

    assert "artifact_not_found" in missing.content[0].text
    assert found.is_error is False
    assert found.structured_content["artifact_ref"]
    assert gateway_runtime.workspace_adapter.find_resources.call_args.kwargs[
        "source"
    ].definition.id == "career_ops"


def test_developer_create_update_and_move_need_no_approval(monkeypatch):
    gateway_runtime = runtime()
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: gateway_runtime)
    audit = Mock()
    monkeypatch.setattr(mcp_module, "emit_audit_record", audit)
    principal = Principal(
        id="developer_test",
        type="developer",
        status="active",
        providers=ProviderScope(workspace=True),
        sources=frozenset({"career_ops"}),
        capabilities=CapabilityScope(read=True, create=True, update=True, move=True),
    )

    async def scenario():
        token = bind_principal(principal)
        try:
            async with Client(mcp_module.mcp_server) as client:
                created = await client.call_tool(
                    "create_source_artifact",
                    {"source_id": "career_ops", "name": "Scoped", "type": "document"},
                )
                updated = await client.call_tool(
                    "update_source_artifact",
                    {
                        "source_id": "career_ops",
                        "document_id": "created_document_123",
                        "change": "Scoped update.",
                    },
                )
                moved = await client.call_tool(
                    "move_source_artifact",
                    {
                        "source_id": "career_ops",
                        "artifact_id": "created_document_123",
                        "destination_folder_id": "destination_folder_123",
                    },
                )
                return created, updated, moved
        finally:
            reset_principal(token)

    created, updated, moved = run(scenario())

    assert all(not result.is_error for result in (created, updated, moved))
    assert gateway_runtime.workspace_adapter.created == [("career_ops", "Scoped")]
    assert gateway_runtime.workspace_adapter.moved == [
        ("created_document_123", "destination_folder_123")
    ]
    mutation_audits = [
        call.kwargs
        for call in audit.call_args_list
        if call.kwargs["action"] in {
            "create_source_artifact",
            "update_source_artifact",
            "move_source_artifact",
        }
    ]
    assert {item["authorization_mode"] for item in mutation_audits} == {
        "principal_scope"
    }
    assert {item["capability"] for item in mutation_audits} == {
        "create",
        "update",
        "move",
    }


def test_developer_spreadsheet_lifecycle_and_tool_filtering(monkeypatch):
    gateway_runtime = runtime()
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: gateway_runtime)
    audit = Mock()
    monkeypatch.setattr(mcp_module, "emit_audit_record", audit)
    principal = Principal(
        id="dev_hq_delivery",
        type="developer",
        status="active",
        providers=ProviderScope(workspace=True),
        sources=frozenset({"career_ops"}),
        capabilities=CapabilityScope(
            read=True, create=True, update=True, move=True, convert=True
        ),
    )
    artifact_ref = gateway_runtime.artifact_reference_codec.encode(
        source_id="career_ops", artifact_id="spreadsheet_12345"
    )
    xlsx_ref = gateway_runtime.artifact_reference_codec.encode(
        source_id="career_ops", artifact_id="xlsx_artifact_123"
    )

    async def scenario():
        token = bind_principal(principal)
        try:
            async with Client(mcp_module.mcp_server) as client:
                tools = await client.list_tools()
                created = await client.call_tool(
                    "create_source_artifact",
                    {"source_id": "career_ops", "name": "Scoped Sheet", "type": "spreadsheet"},
                )
                inspected = await client.call_tool(
                    "inspect_spreadsheet_structure",
                    {"source_id": "career_ops", "artifact_ref": artifact_ref},
                )
                copied = await client.call_tool(
                    "copy_source_artifact",
                    {
                        "source_id": "career_ops",
                        "artifact_ref": artifact_ref,
                        "name": "Scoped Sheet Copy",
                    },
                )
                sheet_ref = inspected.structured_content["sheets"][0]["sheet_ref"]
                edited = await client.call_tool(
                    "edit_source_spreadsheet",
                    {
                        "source_id": "career_ops",
                        "artifact_ref": artifact_ref,
                        "operations": [
                            {
                                "operation": "set_values",
                                "sheet_ref": sheet_ref,
                                "range": "A1:B1",
                                "values": [["Name", "=SUM(A2:A10)"]],
                                "value_input_option": "USER_ENTERED",
                            },
                            {
                                "operation": "insert_rows",
                                "sheet_ref": sheet_ref,
                                "start_index": 2,
                                "count": 1,
                            },
                        ],
                    },
                )
                validated = await client.call_tool(
                    "validate_spreadsheet_structure",
                    {
                        "source_id": "career_ops",
                        "artifact_ref": artifact_ref,
                        "requirements": {
                            "expected_sheets": ["Summary", "Data"],
                            "expected_headers": [
                                {"range": "Summary!A1:B1", "values": ["Name", "Total"]}
                            ],
                            "required_ranges": [
                                {"range": "Summary!B1:B1", "require_formula": True}
                            ],
                        },
                    },
                )
                converted = await client.call_tool(
                    "convert_source_artifact",
                    {
                        "source_id": "career_ops",
                        "artifact_ref": xlsx_ref,
                        "target_type": "google_sheet",
                    },
                )
                denied_delete = await client.call_tool(
                    "delete_source_artifact",
                    {"source_id": "career_ops", "artifact_ref": artifact_ref},
                )
                denied_share = await client.call_tool(
                    "share_source_artifact",
                    {
                        "source_id": "career_ops",
                        "artifact_ref": artifact_ref,
                        "audience": "reviewer@example.com",
                    },
                )
                return tools, created, inspected, copied, edited, validated, converted, denied_delete, denied_share
        finally:
            reset_principal(token)

    tools, created, inspected, copied, edited, validated, converted, denied_delete, denied_share = run(scenario())

    names = {tool.name for tool in tools.tools}
    assert {"inspect_spreadsheet_structure", "edit_source_spreadsheet", "validate_spreadsheet_structure", "convert_source_artifact"} <= names
    assert {"delete_source_artifact", "share_source_artifact", "hubspot_list_tools", "n8n_status", "openwa_status", "openwa_prepare_approval_reference", "list_agent_signals"}.isdisjoint(names)
    assert created.is_error is False
    assert created.structured_content["artifact"]["type"] == "spreadsheet"
    assert inspected.is_error is False
    assert copied.is_error is False
    assert copied.structured_content["artifact"]["type"] == "spreadsheet"
    assert edited.is_error is False
    assert edited.structured_content["applied_operations"] == 2
    assert validated.structured_content["passed"] is True
    assert converted.is_error is False
    assert converted.structured_content["created_artifact_type"] == "google_sheet"
    assert denied_delete.is_error is True
    assert denied_share.is_error is True
    edit_audit = next(
        call.kwargs for call in audit.call_args_list if call.kwargs["action"] == "edit_source_spreadsheet"
    )
    assert edit_audit["authorization_mode"] == "principal_scope"
    assert "operations" not in edit_audit


def test_management_spreadsheet_edit_requires_approval_and_enforces_mime_and_scope(monkeypatch):
    gateway_runtime = runtime()
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: gateway_runtime)
    sheet_ref = gateway_runtime.artifact_reference_codec.encode(
        source_id="career_ops", artifact_id="spreadsheet_12345"
    )
    doc_ref = gateway_runtime.artifact_reference_codec.encode(
        source_id="career_ops", artifact_id="document_12345"
    )
    cross_source_ref = gateway_runtime.artifact_reference_codec.encode(
        source_id="another_source", artifact_id="spreadsheet_12345"
    )
    operation = {
        "operation": "set_values",
        "sheet_ref": gateway_runtime.artifact_reference_codec.encode_sheet(
            source_id="career_ops", artifact_id="spreadsheet_12345", sheet_id="7"
        ),
        "range": "A1:A1",
        "values": [["Safe"]],
    }

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            approved = await client.call_tool(
                "edit_source_spreadsheet",
                {"source_id": "career_ops", "artifact_ref": sheet_ref, "operations": [operation], "approval_reference": "decision-sheet-test"},
            )
            missing_approval = await client.call_tool(
                "edit_source_spreadsheet",
                {"source_id": "career_ops", "artifact_ref": sheet_ref, "operations": [operation]},
            )
            wrong_mime = await client.call_tool(
                "edit_source_spreadsheet",
                {"source_id": "career_ops", "artifact_ref": doc_ref, "operations": [operation], "approval_reference": "decision-sheet-test"},
            )
            cross_source = await client.call_tool(
                "edit_source_spreadsheet",
                {"source_id": "career_ops", "artifact_ref": cross_source_ref, "operations": [operation], "approval_reference": "decision-sheet-test"},
            )
        outside = runtime(in_source=False)
        monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: outside)
        outside_ref = outside.artifact_reference_codec.encode(
            source_id="career_ops", artifact_id="spreadsheet_12345"
        )
        async with Client(mcp_module.mcp_server) as client:
            outside_source = await client.call_tool(
                "edit_source_spreadsheet",
                {"source_id": "career_ops", "artifact_ref": outside_ref, "operations": [operation], "approval_reference": "decision-sheet-test"},
            )
        return approved, missing_approval, wrong_mime, cross_source, outside_source

    approved, missing_approval, wrong_mime, cross_source, outside_source = run(scenario())

    assert approved.is_error is False
    assert "mutation_approval_required" in missing_approval.content[0].text
    assert "resource_type_invalid" in wrong_mime.content[0].text
    assert "artifact_reference_invalid" in cross_source.content[0].text
    assert "resource_not_in_source" in outside_source.content[0].text


def test_developer_spreadsheet_edit_denies_capability_and_source_escalation(monkeypatch):
    gateway_runtime = runtime()
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: gateway_runtime)
    audit = Mock()
    monkeypatch.setattr(mcp_module, "emit_audit_record", audit)
    artifact_ref = gateway_runtime.artifact_reference_codec.encode(
        source_id="career_ops", artifact_id="spreadsheet_12345"
    )
    operation = {
        "operation": "clear_range",
        "sheet_ref": gateway_runtime.artifact_reference_codec.encode_sheet(
            source_id="career_ops", artifact_id="spreadsheet_12345", sheet_id="7"
        ),
        "range": "A1:A1",
    }

    async def call_as(principal):
        token = bind_principal(principal)
        try:
            async with Client(mcp_module.mcp_server) as client:
                return await client.call_tool(
                    "edit_source_spreadsheet",
                    {
                        "source_id": "career_ops",
                        "artifact_ref": artifact_ref,
                        "operations": [operation],
                        "approval_reference": "cannot-elevate-scope",
                    },
                )
        finally:
            reset_principal(token)

    capability_denied = run(
        call_as(
            Principal(
                id="developer_read_only",
                type="developer",
                status="active",
                providers=ProviderScope(workspace=True),
                sources=frozenset({"career_ops"}),
                capabilities=CapabilityScope(read=True),
            )
        )
    )
    source_denied = run(
        call_as(
            Principal(
                id="developer_other_source",
                type="developer",
                status="active",
                providers=ProviderScope(workspace=True),
                sources=frozenset({"another_source"}),
                capabilities=CapabilityScope(read=True, update=True),
            )
        )
    )
    provider_denied = run(
        call_as(
            Principal(
                id="developer_no_workspace",
                type="developer",
                status="active",
                providers=ProviderScope(),
                sources=frozenset({"career_ops"}),
                capabilities=CapabilityScope(read=True, update=True),
            )
        )
    )

    assert "tool_denied" in capability_denied.content[0].text
    assert "source_denied" in source_denied.content[0].text
    assert "tool_denied" in provider_denied.content[0].text
    assert {call.kwargs.get("error_code") for call in audit.call_args_list} >= {
        "capability_denied",
        "source_denied",
        "provider_denied",
    }


def test_structured_mutations_require_approval_and_enforce_source_scope(monkeypatch):
    permitted_runtime = runtime()
    artifact_ref = permitted_runtime.artifact_reference_codec.encode(
        source_id="career_ops", artifact_id="document_12345"
    )
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: permitted_runtime)

    async def missing_approval_scenario():
        async with Client(mcp_module.mcp_server) as client:
            return await client.call_tool(
                "copy_source_artifact",
                {
                    "source_id": "career_ops",
                    "artifact_ref": artifact_ref,
                    "name": "Must Not Be Copied",
                },
            )

    missing_approval = run(missing_approval_scenario())
    assert missing_approval.is_error is True
    assert "mutation_approval_required" in missing_approval.content[0].text
    assert permitted_runtime.workspace_adapter.copied == []

    outside_runtime = runtime(in_source=False)
    outside_ref = outside_runtime.artifact_reference_codec.encode(
        source_id="career_ops", artifact_id="document_12345"
    )
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: outside_runtime)

    async def outside_scope_scenario():
        async with Client(mcp_module.mcp_server) as client:
            return await client.call_tool(
                "edit_source_document",
                {
                    "source_id": "career_ops",
                    "artifact_ref": outside_ref,
                    "required_revision_id": "revision-1",
                    "operations": [
                        {"operation": "insert_text_at_index", "index": 1, "text": "blocked"}
                    ],
                    "approval_reference": "BR-019-test-blocked",
                },
            )

    outside_scope = run(outside_scope_scenario())
    assert outside_scope.is_error is True
    assert "resource_not_in_source" in outside_scope.content[0].text
    assert outside_runtime.docs_adapter.structured_edits == []


def test_document_quality_gate_reports_specific_failures_without_mutation(monkeypatch):
    gateway_runtime = runtime()
    artifact_ref = gateway_runtime.artifact_reference_codec.encode(
        source_id="career_ops", artifact_id="document_12345"
    )
    gateway_runtime.docs_adapter.inspect_structure = Mock(
        return_value=DocumentStructure(
            artifact_ref=artifact_ref,
            name="Controlled Doc",
            source_id="career_ops",
            revision_id="revision-3",
            tabs=[
                TabStructure(
                    tab_ref=gateway_runtime.artifact_reference_codec.encode_tab(
                        source_id="career_ops",
                        artifact_id="document_12345",
                        tab_id="tab-1",
                    ),
                    title="Main",
                    index=0,
                    nesting_level=0,
                    paragraphs=[
                        ParagraphSummary(
                            start_index=1,
                            end_index=20,
                            text="# Draft {{owner}}\n",
                            named_style_type="NORMAL_TEXT",
                        )
                    ],
                    tables=[],
                )
            ],
            headers=[],
            footers=[],
            image_count=0,
            document_style={},
            placeholders=["{{owner}}"],
            total_characters=18,
        )
    )
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: gateway_runtime)

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            return await client.call_tool(
                "validate_document_structure",
                {
                    "source_id": "career_ops",
                    "artifact_ref": artifact_ref,
                    "requirements": {
                        "expected_headings": ["Executive Summary"],
                        "minimum_table_count": 1,
                        "require_header": True,
                        "require_footer": True,
                        "minimum_characters": 100,
                    },
                    "expected_revision_id": "revision-4",
                },
            )

    result = run(scenario())

    assert result.is_error is False
    assert result.structured_content["passed"] is False
    for check in (
        "expected_headings",
        "minimum_tables",
        "header",
        "footer",
        "minimum_content",
        "no_placeholders",
        "no_markdown",
        "revision",
    ):
        assert result.structured_content["checks"][check] is False
    assert gateway_runtime.docs_adapter.structured_edits == []


def test_mcp_inspects_source_scoped_safe_artifact_metadata_and_audits(monkeypatch):
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: runtime())
    audit = Mock()
    monkeypatch.setattr(mcp_module, "emit_audit_record", audit)

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            return await client.call_tool(
                "inspect_source_artifacts",
                {"source_id": "career_ops"},
            )

    result = run(scenario())

    assert result.is_error is False
    artifact = result.structured_content["artifacts"][0]
    assert artifact == {
        "name": "Forecast.xlsm",
        "type": "office_artifact",
        "mime_type": "application/vnd.ms-excel.sheet.macroenabled.12",
        "extension": "xlsm",
        "size": 4096,
        "modified_time": "2026-08-23T11:00:00Z",
        "source_id": "career_ops",
    }
    assert not ({"id", "owners", "permissions", "content"} & set(artifact))
    assert audit.call_args.kwargs["action"] == "inspect_source_artifacts"
    assert audit.call_args.kwargs["source_id"] == "career_ops"
    assert audit.call_args.kwargs["result"] == "success"


def test_mcp_artifact_inspection_rejects_unknown_source(monkeypatch):
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: runtime())
    audit = Mock()
    monkeypatch.setattr(mcp_module, "emit_audit_record", audit)

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            return await client.call_tool(
                "inspect_source_artifacts",
                {"source_id": "unknown_source"},
            )

    result = run(scenario())

    assert result.is_error is True
    assert "source_not_found" in result.content[0].text
    assert audit.call_args.kwargs["error_code"] == "source_not_found"


def test_mcp_discovers_safe_source_proposals_and_audits_count(monkeypatch):
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: runtime())
    audit = Mock()
    monkeypatch.setattr(mcp_module, "emit_audit_record", audit)

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            return await client.call_tool(
                "discover_source_candidates",
                {"limit": 10},
            )

    result = run(scenario())

    assert result.is_error is False
    candidate_id = candidate_identifier(
        CandidateSource(
            system="google_workspace",
            location_type="shared_drive",
            location_id="finance_drive_123",
            name="Finance",
            classification_suggestion="management_only",
            reasons=("new shared drive detected",),
        )
    )
    assert result.structured_content["candidates"][0] == {
        "candidate_id": candidate_id,
        "name": "Finance",
        "location_type": "shared_drive",
        "classification_suggestion": "management_only",
        "reason": ["new shared drive detected"],
        "exists": True,
    }
    assert result.structured_content["proposals"][0]["confidence"] == "medium"
    assert "location_id" not in repr(result.structured_content)
    assert "proposed_id" not in repr(result.structured_content)
    assert audit.call_args.kwargs["action"] == "discover_source_candidates"
    assert audit.call_args.kwargs["candidate_count"] == 1


def test_mcp_candidate_details_and_proposal_creation_are_governed(monkeypatch):
    gateway_runtime = runtime()
    before = gateway_runtime.registry.sources
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: gateway_runtime)
    audit = Mock()
    monkeypatch.setattr(mcp_module, "emit_audit_record", audit)
    candidate_id = candidate_identifier(
        CandidateSource(
            system="google_workspace",
            location_type="shared_drive",
            location_id="finance_drive_123",
            name="Finance",
            classification_suggestion="management_only",
            reasons=("new shared drive detected",),
        )
    )

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            details = await client.call_tool(
                "get_source_candidate_details",
                {"candidate_id": candidate_id},
            )
            proposal = await client.call_tool(
                "create_source_proposal",
                {
                    "candidate_id": candidate_id,
                    "name": "Finance",
                    "classification": "management_only",
                    "reason": "Reviewed; awaiting explicit human approval.",
                },
            )
            proposals = await client.call_tool("list_source_proposals", {})
            proposal_details = await client.call_tool(
                "get_source_proposal",
                {"proposal_id": proposal.structured_content["proposal_id"]},
            )
            return details, proposal, proposals, proposal_details

    details, proposal, proposals, proposal_details = run(scenario())

    assert details.is_error is False
    assert details.structured_content["candidate"]["candidate_id"] == candidate_id
    assert details.structured_content["candidate"]["confidence"] == "medium"
    assert "location_id" not in repr(details.structured_content)
    assert proposal.is_error is False
    assert proposal.structured_content["status"] == "pending_review"
    assert proposal.structured_content["proposal_id"].startswith("proposal_")
    assert proposals.is_error is False
    assert proposals.structured_content["proposals"] == [
        {
            "proposal_id": proposal.structured_content["proposal_id"],
            "name": "Finance",
            "classification": "management_only",
            "status": "pending_review",
        }
    ]
    assert proposal_details.is_error is False
    assert proposal_details.structured_content["proposal"]["candidate"]["name"] == (
        "Finance"
    )
    assert proposal_details.structured_content["proposal"]["confidence"] == "medium"
    assert "location_id" not in repr(proposal_details.structured_content)
    assert "request_id" not in proposal_details.structured_content["proposal"]
    assert gateway_runtime.registry.sources == before
    actions = [call.kwargs["action"] for call in audit.call_args_list]
    assert actions == [
        "get_source_candidate_details",
        "create_source_proposal",
        "list_source_proposals",
        "get_source_proposal",
    ]
    assert audit.call_args.kwargs["proposal_id"] == (
        proposal.structured_content["proposal_id"]
    )
    assert "reason" not in audit.call_args.kwargs


def test_mcp_candidate_details_reject_unknown_candidate(monkeypatch):
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: runtime())
    audit = Mock()
    monkeypatch.setattr(mcp_module, "emit_audit_record", audit)

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            return await client.call_tool(
                "get_source_candidate_details",
                {"candidate_id": "candidate_00000000000000000000000000000000"},
            )

    result = run(scenario())

    assert result.is_error is True
    assert "source_candidate_not_found" in result.content[0].text
    assert audit.call_args.kwargs["error_code"] == "source_candidate_not_found"


def test_mcp_governed_mutations_require_capabilities_scope_and_approval(monkeypatch):
    gateway_runtime = runtime()
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: gateway_runtime)
    audit = Mock()
    monkeypatch.setattr(mcp_module, "emit_audit_record", audit)

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            created = await client.call_tool(
                "create_source_artifact",
                {
                    "source_id": "career_ops",
                    "name": "Controlled Test",
                    "type": "document",
                    "approval_reference": "decision-v013-test",
                },
            )
            updated = await client.call_tool(
                "update_source_artifact",
                {
                    "source_id": "career_ops",
                    "document_id": "created_document_123",
                    "change": "Approved bounded addition.",
                    "approval_reference": "decision-v013-test",
                },
            )
            moved = await client.call_tool(
                "move_source_artifact",
                {
                    "source_id": "career_ops",
                    "artifact_id": "created_document_123",
                    "destination_folder_id": "destination_folder_123",
                    "approval_reference": "decision-v013-test",
                },
            )
            deleted = await client.call_tool(
                "delete_source_artifact",
                {
                    "source_id": "career_ops",
                    "artifact_id": "created_document_123",
                    "approval_reference": "decision-v015-test",
                },
            )
            shared = await client.call_tool(
                "share_source_artifact",
                {
                    "source_id": "career_ops",
                    "artifact_id": "created_document_123",
                    "audience": "REVIEWER@BRUNOVA.MX",
                    "approval_reference": "decision-v015-test",
                },
            )
            return created, updated, moved, deleted, shared

    created, updated, moved, deleted, shared = run(scenario())

    assert created.is_error is False
    assert created.structured_content["status"] == "created"
    assert updated.is_error is False
    assert updated.structured_content["status"] == "updated"
    assert moved.is_error is False
    assert moved.structured_content["status"] == "moved"
    assert deleted.is_error is False
    assert deleted.structured_content["status"] == "deleted"
    assert shared.is_error is False
    assert shared.structured_content["status"] == "shared"
    assert gateway_runtime.workspace_adapter.created == [
        ("career_ops", "Controlled Test")
    ]
    assert gateway_runtime.docs_adapter.appended == [
        ("created_document_123", "Approved bounded addition.")
    ]
    assert gateway_runtime.workspace_adapter.moved == [
        ("created_document_123", "destination_folder_123")
    ]
    assert gateway_runtime.workspace_adapter.deleted == ["created_document_123"]
    assert gateway_runtime.workspace_adapter.shared == [
        ("created_document_123", "reviewer@brunova.mx")
    ]
    mutation_audits = audit.call_args_list[-5:]
    assert [call.kwargs["action"] for call in mutation_audits] == [
        "create_source_artifact",
        "update_source_artifact",
        "move_source_artifact",
        "delete_source_artifact",
        "share_source_artifact",
    ]
    assert all(
        call.kwargs["approval_reference"]
        in ("decision-v013-test", "decision-v015-test")
        for call in mutation_audits
    )
    assert all("change" not in call.kwargs for call in mutation_audits)
    assert mutation_audits[-1].kwargs["audience"] == "reviewer@brunova.mx"


def test_mcp_converts_xlsx_and_xlsm_then_moves_original_with_full_audit(monkeypatch):
    gateway_runtime = runtime()
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: gateway_runtime)
    audit = Mock()
    monkeypatch.setattr(mcp_module, "emit_audit_record", audit)

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            xlsx = await client.call_tool(
                "convert_source_artifact",
                {
                    "source_id": "career_ops",
                    "artifact_id": "xlsx_artifact_123",
                    "target_type": "google_sheet",
                    "approval_reference": "decision-v017-convert",
                },
            )
            xlsm = await client.call_tool(
                "convert_source_artifact",
                {
                    "source_id": "career_ops",
                    "artifact_id": "xlsm_artifact_123",
                    "target_type": "google_sheet",
                    "approval_reference": "decision-v017-convert",
                },
            )
            moved = await client.call_tool(
                "move_source_artifact",
                {
                    "source_id": "career_ops",
                    "artifact_id": "xlsx_artifact_123",
                    "destination_folder_id": "destination_folder_123",
                    "approval_reference": "decision-v017-archive",
                },
            )
            return xlsx, xlsm, moved

    xlsx, xlsm, moved = run(scenario())

    assert xlsx.is_error is False
    assert xlsx.structured_content["original_artifact"]["type"] == "xlsx"
    assert xlsx.structured_content["created_artifact"]["type"] == "spreadsheet"
    assert xlsx.structured_content["created_artifact_type"] == "google_sheet"
    assert xlsm.is_error is False
    assert xlsm.structured_content["original_artifact"]["type"] == "xlsm"
    assert moved.is_error is False
    assert gateway_runtime.workspace_adapter.converted == [
        (
            "xlsx_artifact_123",
            "application/vnd.google-apps.spreadsheet",
            "Controlled workbook",
        ),
        (
            "xlsm_artifact_123",
            "application/vnd.google-apps.spreadsheet",
            "Controlled workbook",
        ),
    ]
    lifecycle_audits = audit.call_args_list[-3:]
    assert [call.kwargs["action"] for call in lifecycle_audits] == [
        "convert_source_artifact",
        "convert_source_artifact",
        "move_source_artifact",
    ]
    assert lifecycle_audits[0].kwargs["resource_id"] is None
    assert lifecycle_audits[0].kwargs["created_resource_id"].startswith("artifact_")
    assert all("content" not in call.kwargs for call in lifecycle_audits)


def test_mcp_conversion_rejects_missing_approval_unknown_source_and_scope(monkeypatch):
    audit = Mock()
    monkeypatch.setattr(mcp_module, "emit_audit_record", audit)

    async def scenario():
        gateway_runtime = runtime()
        monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: gateway_runtime)
        async with Client(mcp_module.mcp_server) as client:
            missing_approval = await client.call_tool(
                "convert_source_artifact",
                {
                    "source_id": "career_ops",
                    "artifact_id": "xlsx_artifact_123",
                    "target_type": "google_sheet",
                },
            )
            unknown_source = await client.call_tool(
                "convert_source_artifact",
                {
                    "source_id": "unknown_source",
                    "artifact_id": "xlsx_artifact_123",
                    "target_type": "google_sheet",
                    "approval_reference": "decision-v017-convert",
                },
            )
        outside_runtime = runtime(in_source=False)
        monkeypatch.setattr(
            mcp_module,
            "get_runtime_gateway",
            lambda: outside_runtime,
        )
        async with Client(mcp_module.mcp_server) as client:
            outside_scope = await client.call_tool(
                "convert_source_artifact",
                {
                    "source_id": "career_ops",
                    "artifact_id": "xlsx_artifact_123",
                    "target_type": "google_sheet",
                    "approval_reference": "decision-v017-convert",
                },
            )
        return missing_approval, unknown_source, outside_scope

    missing_approval, unknown_source, outside_scope = run(scenario())

    assert "mutation_approval_required" in missing_approval.content[0].text
    assert "source_not_found" in unknown_source.content[0].text
    assert "resource_not_in_source" in outside_scope.content[0].text


def test_mcp_moves_office_original_to_explicit_archive_destination(monkeypatch):
    gateway_runtime = runtime(include_archive=True)
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: gateway_runtime)
    audit = Mock()
    monkeypatch.setattr(mcp_module, "emit_audit_record", audit)

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            return await client.call_tool(
                "move_source_artifact",
                {
                    "source_id": "career_ops",
                    "artifact_id": "xlsm_artifact_123",
                    "destination_source_id": "legacy_archive",
                    "approval_reference": "decision-v017-archive",
                },
            )

    result = run(scenario())

    assert result.is_error is False
    assert gateway_runtime.workspace_adapter.moved == [
        ("xlsm_artifact_123", "archive_folder_123")
    ]
    assert audit.call_args.kwargs["destination_source_id"] == "legacy_archive"


def test_mcp_mutation_blocks_missing_approval_and_capability(monkeypatch):
    audit = Mock()
    monkeypatch.setattr(mcp_module, "emit_audit_record", audit)

    async def scenario():
        disabled_runtime = runtime(mutation_enabled=False)
        monkeypatch.setattr(
            mcp_module,
            "get_runtime_gateway",
            lambda: disabled_runtime,
        )
        async with Client(mcp_module.mcp_server) as client:
            missing_approval = await client.call_tool(
                "create_source_artifact",
                {
                    "source_id": "career_ops",
                    "name": "Blocked Test",
                    "type": "document",
                },
            )
            denied_capability = await client.call_tool(
                "share_source_artifact",
                {
                    "source_id": "career_ops",
                    "artifact_id": "created_document_123",
                    "audience": "reviewer@brunova.mx",
                    "approval_reference": "decision-v015-test",
                },
            )
            return missing_approval, denied_capability

    missing_approval, denied_capability = run(scenario())

    assert missing_approval.is_error is True
    assert "mutation_approval_required" in missing_approval.content[0].text
    assert denied_capability.is_error is True
    assert "source_capability_denied" in denied_capability.content[0].text
    assert [call.kwargs["error_code"] for call in audit.call_args_list] == [
        "mutation_approval_required",
        "source_capability_denied",
    ]


def test_mcp_delete_and_share_block_artifacts_outside_selected_source(monkeypatch):
    gateway_runtime = runtime(in_source=False)
    monkeypatch.setattr(
        mcp_module,
        "get_runtime_gateway",
        lambda: gateway_runtime,
    )
    audit = Mock()
    monkeypatch.setattr(mcp_module, "emit_audit_record", audit)

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            deleted = await client.call_tool(
                "delete_source_artifact",
                {
                    "source_id": "career_ops",
                    "artifact_id": "outside_document_123",
                    "approval_reference": "decision-v015-test",
                },
            )
            shared = await client.call_tool(
                "share_source_artifact",
                {
                    "source_id": "career_ops",
                    "artifact_id": "outside_document_123",
                    "audience": "reviewer@brunova.mx",
                    "approval_reference": "decision-v015-test",
                },
            )
            return deleted, shared

    deleted, shared = run(scenario())

    assert deleted.is_error is True
    assert "resource_not_in_source" in deleted.content[0].text
    assert shared.is_error is True
    assert "resource_not_in_source" in shared.content[0].text
    assert gateway_runtime.workspace_adapter.deleted == []
    assert gateway_runtime.workspace_adapter.shared == []


def test_mcp_delete_and_share_require_approval_reference(monkeypatch):
    gateway_runtime = runtime()
    monkeypatch.setattr(
        mcp_module,
        "get_runtime_gateway",
        lambda: gateway_runtime,
    )
    audit = Mock()
    monkeypatch.setattr(mcp_module, "emit_audit_record", audit)

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            deleted = await client.call_tool(
                "delete_source_artifact",
                {
                    "source_id": "career_ops",
                    "artifact_id": "created_document_123",
                },
            )
            shared = await client.call_tool(
                "share_source_artifact",
                {
                    "source_id": "career_ops",
                    "artifact_id": "created_document_123",
                    "audience": "reviewer@brunova.mx",
                },
            )
            return deleted, shared

    deleted, shared = run(scenario())

    assert deleted.is_error is True
    assert "mutation_approval_required" in deleted.content[0].text
    assert shared.is_error is True
    assert "mutation_approval_required" in shared.content[0].text
    assert gateway_runtime.workspace_adapter.deleted == []
    assert gateway_runtime.workspace_adapter.shared == []


def test_mcp_delete_and_share_protect_registered_source_root(monkeypatch):
    gateway_runtime = runtime()
    monkeypatch.setattr(
        mcp_module,
        "get_runtime_gateway",
        lambda: gateway_runtime,
    )
    monkeypatch.setattr(mcp_module, "emit_audit_record", Mock())

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            deleted = await client.call_tool(
                "delete_source_artifact",
                {
                    "source_id": "career_ops",
                    "artifact_id": "allowed_folder_123",
                    "approval_reference": "decision-v015-test",
                },
            )
            shared = await client.call_tool(
                "share_source_artifact",
                {
                    "source_id": "career_ops",
                    "artifact_id": "allowed_folder_123",
                    "audience": "reviewer@brunova.mx",
                    "approval_reference": "decision-v015-test",
                },
            )
            return deleted, shared

    deleted, shared = run(scenario())

    assert deleted.is_error is True
    assert "mutation_source_root_protected" in deleted.content[0].text
    assert shared.is_error is True
    assert "mutation_source_root_protected" in shared.content[0].text


def test_mcp_update_blocks_document_outside_selected_source(monkeypatch):
    monkeypatch.setattr(
        mcp_module,
        "get_runtime_gateway",
        lambda: runtime(in_source=False),
    )
    audit = Mock()
    monkeypatch.setattr(mcp_module, "emit_audit_record", audit)

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            return await client.call_tool(
                "update_source_artifact",
                {
                    "source_id": "career_ops",
                    "document_id": "outside_document_123",
                    "change": "Must not be written.",
                    "approval_reference": "decision-v013-test",
                },
            )

    result = run(scenario())

    assert result.is_error is True
    assert "resource_not_in_source" in result.content[0].text
    assert audit.call_args.kwargs["error_code"] == "resource_not_in_source"


def test_mcp_lists_sources_and_filters_authorized_documents(monkeypatch):
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: runtime())
    monkeypatch.setattr(mcp_module, "emit_audit_record", Mock())

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            sources = await client.call_tool("list_sources", {})
            documents = await client.call_tool(
                "list_source_documents",
                {"source_id": "career_ops", "query": "road"},
            )
            return sources, documents

    sources, documents = run(scenario())

    assert sources.is_error is False
    assert sources.structured_content["sources"][0]["id"] == "career_ops"
    assert sources.structured_content["sources"][0]["capabilities"] == {
        "read": True,
        "create": True,
        "update": True,
        "move": True,
        "delete": True,
        "share": True,
        "convert": True,
    }
    assert documents.is_error is False
    assert [item["name"] for item in documents.structured_content["documents"]] == [
        "Career Roadmap"
    ]


def test_mcp_retrieval_tools_apply_source_and_content_policies(monkeypatch):
    gateway_runtime = runtime()
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: gateway_runtime)
    audit = Mock()
    monkeypatch.setattr(mcp_module, "emit_audit_record", audit)
    artifact_ref = gateway_runtime.artifact_reference_codec.encode(
        source_id="career_ops", artifact_id="spreadsheet_12345"
    )
    sheet_ref = gateway_runtime.artifact_reference_codec.encode_sheet(
        source_id="career_ops", artifact_id="spreadsheet_12345", sheet_id="7"
    )

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            document = await client.call_tool(
                "retrieve_document",
                {"source_id": "career_ops", "document_id": "document_12345"},
            )
            sheet = await client.call_tool(
                "retrieve_sheet_range",
                {
                    "source_id": "career_ops",
                    "artifact_ref": artifact_ref,
                    "sheet_ref": sheet_ref,
                    "range": "A1:A2",
                },
            )
            return document, sheet

    document, sheet = run(scenario())

    assert document.is_error is False
    assert document.structured_content["source"]["id"] == "career_ops"
    assert sheet.is_error is False
    assert sheet.structured_content["values"] == [["Header"], ["Value"]]
    assert sheet.structured_content["artifact_ref"] == artifact_ref
    assert sheet.structured_content["sheet_ref"] == sheet_ref
    assert "spreadsheet_id" not in sheet.structured_content
    assert gateway_runtime.sheets_adapter.ranges[-1][1] == "'Summary'!A1:A2"
    assert any(
        call.kwargs.get("source_classification") == "management_only"
        for call in audit.call_args_list
    )


def test_sheet_range_contract_selects_primary_secondary_and_quoted_title(monkeypatch):
    gateway_runtime = runtime()
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: gateway_runtime)
    artifact_ref = gateway_runtime.artifact_reference_codec.encode(
        source_id="career_ops", artifact_id="spreadsheet_12345"
    )

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            tools = await client.list_tools()
            structure = await client.call_tool(
                "inspect_spreadsheet_structure",
                {"source_id": "career_ops", "artifact_ref": artifact_ref},
            )
            refs = {
                sheet["title"]: sheet["sheet_ref"]
                for sheet in structure.structured_content["sheets"]
            }
            reads = []
            for title in ("Summary", "Data", "Team's Tasks"):
                reads.append(
                    await client.call_tool(
                        "retrieve_sheet_range",
                        {
                            "source_id": "career_ops",
                            "artifact_ref": artifact_ref,
                            "sheet_ref": refs[title],
                            "range": "A1:B2",
                        },
                    )
                )
            return tools, structure, reads

    tools, structure, reads = run(scenario())

    assert structure.is_error is False
    assert all(result.is_error is False for result in reads)
    assert [item[1] for item in gateway_runtime.sheets_adapter.ranges[-3:]] == [
        "'Summary'!A1:B2",
        "'Data'!A1:B2",
        "'Team''s Tasks'!A1:B2",
    ]
    tool = next(item for item in tools.tools if item.name == "retrieve_sheet_range")
    assert set(tool.input_schema["required"]) >= {
        "source_id", "artifact_ref", "sheet_ref", "range"
    }
    assert "spreadsheet_id" not in tool.input_schema["properties"]


def test_sheet_range_rejects_cross_artifact_cross_source_stale_and_invalid_refs(
    monkeypatch,
):
    gateway_runtime = runtime()
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: gateway_runtime)
    codec = gateway_runtime.artifact_reference_codec
    artifact_ref = codec.encode(
        source_id="career_ops", artifact_id="spreadsheet_12345"
    )
    rejected_refs = [
        codec.encode_sheet(
            source_id="career_ops", artifact_id="spreadsheet_other", sheet_id="7"
        ),
        codec.encode_sheet(
            source_id="another_source", artifact_id="spreadsheet_12345", sheet_id="7"
        ),
        codec.encode_sheet(
            source_id="career_ops", artifact_id="spreadsheet_12345", sheet_id="999"
        ),
        "sheet_invalid",
    ]

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            results = []
            edits = []
            for sheet_ref in rejected_refs:
                results.append(
                    await client.call_tool(
                        "retrieve_sheet_range",
                        {
                            "source_id": "career_ops",
                            "artifact_ref": artifact_ref,
                            "sheet_ref": sheet_ref,
                            "range": "A1:B2",
                        },
                    )
                )
                edits.append(
                    await client.call_tool(
                        "edit_source_spreadsheet",
                        {
                            "source_id": "career_ops",
                            "artifact_ref": artifact_ref,
                            "operations": [
                                {
                                    "operation": "set_values",
                                    "sheet_ref": sheet_ref,
                                    "range": "A1:A1",
                                    "values": [["value"]],
                                }
                            ],
                            "approval_reference": "sheet-ref-rejection-test",
                        },
                    )
                )
            qualified_range = await client.call_tool(
                "retrieve_sheet_range",
                {
                    "source_id": "career_ops",
                    "artifact_ref": artifact_ref,
                    "sheet_ref": codec.encode_sheet(
                        source_id="career_ops",
                        artifact_id="spreadsheet_12345",
                        sheet_id="7",
                    ),
                    "range": "Summary!A1:B2",
                },
            )
            return results, edits, qualified_range

    results, edits, qualified_range = run(scenario())

    assert all(result.is_error is True for result in results + edits)
    assert all(
        "spreadsheet_sheet_reference_invalid" in result.content[0].text
        for result in results + edits
    )
    assert qualified_range.is_error is True
    assert "spreadsheet_range_invalid" in qualified_range.content[0].text


def test_hq_client_developer_reads_and_updates_secondary_sheet_by_ref(monkeypatch):
    base_runtime = runtime()
    hq_source = base_runtime.registry.get("career_ops").model_copy(
        update={"id": "hq_client", "name": "HQ"}
    )
    hq_registry = SourceRegistry(
        SourceRegistryDocument(version=1, sources=(hq_source,))
    )
    hq_policy = SourceAccessPolicy(base_runtime.settings, hq_registry)
    gateway_runtime = replace(
        base_runtime,
        registry=hq_registry,
        source_policy=hq_policy,
        mutation_policy=ContentMutationPolicy(hq_registry, hq_policy),
    )
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: gateway_runtime)
    principal = Principal(
        id="developer_hq_client",
        type="developer",
        status="active",
        providers=ProviderScope(workspace=True),
        sources=frozenset({"hq_client"}),
        capabilities=CapabilityScope(read=True, update=True),
    )
    artifact_ref = gateway_runtime.artifact_reference_codec.encode(
        source_id="hq_client", artifact_id="spreadsheet_12345"
    )

    async def scenario():
        token = bind_principal(principal)
        try:
            async with Client(mcp_module.mcp_server) as client:
                structure = await client.call_tool(
                    "inspect_spreadsheet_structure",
                    {"source_id": "hq_client", "artifact_ref": artifact_ref},
                )
                secondary_ref = next(
                    sheet["sheet_ref"]
                    for sheet in structure.structured_content["sheets"]
                    if sheet["title"] == "Data"
                )
                read = await client.call_tool(
                    "retrieve_sheet_range",
                    {
                        "source_id": "hq_client",
                        "artifact_ref": artifact_ref,
                        "sheet_ref": secondary_ref,
                        "range": "A1:B2",
                    },
                )
                update = await client.call_tool(
                    "edit_source_spreadsheet",
                    {
                        "source_id": "hq_client",
                        "artifact_ref": artifact_ref,
                        "operations": [
                            {
                                "operation": "set_values",
                                "sheet_ref": secondary_ref,
                                "range": "A1:B1",
                                "values": [["Name", "Value"]],
                            },
                            {
                                "operation": "append_rows",
                                "sheet_ref": secondary_ref,
                                "range": "A1:B20",
                                "values": [["Item", 1]],
                            },
                            {
                                "operation": "clear_range",
                                "sheet_ref": secondary_ref,
                                "range": "B2:B3",
                            },
                            {
                                "operation": "format_range",
                                "sheet_ref": secondary_ref,
                                "range": "A1:B2",
                                "bold": True,
                            },
                        ],
                    },
                )
                return structure, read, update
        finally:
            reset_principal(token)

    structure, read, update = run(scenario())

    assert structure.is_error is False
    assert read.is_error is False
    assert read.structured_content["source"]["id"] == "hq_client"
    assert update.is_error is False
    assert update.structured_content["applied_operations"] == 4
    assert gateway_runtime.sheets_adapter.ranges[-1][1] == "'Data'!A1:B2"
    assert all(operation.range.split("!")[-1] == operation.range for operation in gateway_runtime.sheets_adapter.operations[-4:])


def test_mcp_operation_history_filters_limits_and_audits(monkeypatch):
    class FakeOperationHistoryStore:
        def __init__(self):
            self.calls = []

        def list(self, *, source_id, operation, limit):
            self.calls.append((source_id, operation, limit))
            return [
                OperationHistoryEntry(
                    timestamp="2026-08-22T19:53:06Z",
                    operation="create_source_artifact",
                    source_id="career_ops",
                    result="success",
                    approval_reference="decision-v014-test",
                    request_id="mutation-request-123",
                    correlation_id="mutation-request-123",
                )
            ]

    store = FakeOperationHistoryStore()
    gateway_runtime = replace(runtime(), operation_history_store=store)
    monkeypatch.setattr(
        mcp_module,
        "get_runtime_gateway",
        lambda: gateway_runtime,
    )
    audit = Mock()
    monkeypatch.setattr(mcp_module, "emit_audit_record", audit)

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            return await client.call_tool(
                "get_operation_history",
                {
                    "source_id": "career_ops",
                    "operation": "create_source_artifact",
                    "limit": 1,
                },
            )

    result = run(scenario())

    assert result.is_error is False
    assert store.calls == [
        ("career_ops", GovernedOperation.CREATE_SOURCE_ARTIFACT, 5)
    ]
    assert result.structured_content["operations"] == [
        {
            "timestamp": "2026-08-22T19:53:06Z",
            "operation": "create_source_artifact",
            "source_id": "career_ops",
            "result": "success",
            "approval_reference": "decision-v014-test",
            "request_id": "mutation-request-123",
            "correlation_id": "mutation-request-123",
        }
    ]
    assert audit.call_args.kwargs["action"] == "get_operation_history"
    assert audit.call_args.kwargs["source_id"] == "career_ops"


def test_mcp_propagates_resource_not_in_source_error(monkeypatch):
    monkeypatch.setattr(
        mcp_module,
        "get_runtime_gateway",
        lambda: runtime(in_source=False),
    )
    audit = Mock()
    monkeypatch.setattr(mcp_module, "emit_audit_record", audit)

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            return await client.call_tool(
                "retrieve_document",
                {"source_id": "career_ops", "document_id": "document_12345"},
            )

    result = run(scenario())

    assert result.is_error is True
    assert "resource_not_in_source" in result.content[0].text
    assert audit.call_args.kwargs["error_code"] == "resource_not_in_source"


def test_validation_tool_governed_source_reads_and_opaque_provenance(monkeypatch):
    gateway_runtime = runtime()
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: gateway_runtime)
    codec = gateway_runtime.artifact_reference_codec
    args = {
        "source_id": "career_ops",
        "artifact_ref": codec.encode(
            source_id="career_ops", artifact_id="spreadsheet_12345"
        ),
        "sheet_ref": codec.encode_sheet(
            source_id="career_ops", artifact_id="spreadsheet_12345", sheet_id="7"
        ),
        "range": "A23",
    }

    def validation(resource, *, range_name, read_source):
        assert range_name == "'Summary'!A23:A23"
        values = read_source("'Data'!A1:A2").values
        return {
            "spreadsheet_id": resource.id,
            "sheet_id": "7",
            "sheet_title": "Summary",
            "range": range_name,
            "cells": [{"cell": "A23", "has_validation": True}],
            "validation_sources": [
                {"sheet_id": "8", "sheet_title": "Data", "resolved_values": values}
            ],
        }

    gateway_runtime.sheets_adapter.get_validation = Mock(side_effect=validation)

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            result = await client.call_tool("inspect_sheet_validation", args)
            rejected = []
            for changes in [
                {
                    "sheet_ref": codec.encode_sheet(
                        source_id="career_ops",
                        artifact_id="other_artifact",
                        sheet_id="7",
                    )
                },
                {
                    "sheet_ref": codec.encode_sheet(
                        source_id="other_source",
                        artifact_id="spreadsheet_12345",
                        sheet_id="7",
                    )
                },
                {
                    "sheet_ref": codec.encode_sheet(
                        source_id="career_ops",
                        artifact_id="spreadsheet_12345",
                        sheet_id="999",
                    )
                },
                {"range": "Data!A1:A2"},
                {"range": "A:A"},
                {"range": "A1:Z100"},
            ]:
                rejected.append(
                    await client.call_tool(
                        "inspect_sheet_validation", {**args, **changes}
                    )
                )
            return result, rejected

    result, rejected = run(scenario())
    assert not result.is_error
    assert all(r.is_error for r in rejected)
    assert "spreadsheet_id" not in result.structured_content
    origin = result.structured_content["validation_sources"][0]
    assert "sheet_id" not in origin
    assert (
        codec.decode_sheet(
            origin["sheet_ref"], source_id="career_ops", artifact_id="spreadsheet_12345"
        )
        == "8"
    )
    assert gateway_runtime.sheets_adapter.ranges[-1][1] == "'Data'!A1:A2"
    gateway_runtime.sheets_adapter.get_validation.assert_called_once()
    assert gateway_runtime.sheets_adapter.operations == []


def test_validation_tool_denies_resource_outside_source(monkeypatch):
    gateway_runtime = runtime(in_source=False)
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: gateway_runtime)
    gateway_runtime.sheets_adapter.get_validation = Mock()
    codec = gateway_runtime.artifact_reference_codec

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            return await client.call_tool(
                "inspect_sheet_validation",
                {
                    "source_id": "career_ops",
                    "artifact_ref": codec.encode(
                        source_id="career_ops", artifact_id="spreadsheet_12345"
                    ),
                    "sheet_ref": codec.encode_sheet(
                        source_id="career_ops",
                        artifact_id="spreadsheet_12345",
                        sheet_id="7",
                    ),
                    "range": "A23",
                },
            )

    assert run(scenario()).is_error
    gateway_runtime.sheets_adapter.get_validation.assert_not_called()


def test_validation_tool_requires_principal_read_and_source_scope(monkeypatch):
    gateway_runtime = runtime()
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: gateway_runtime)
    gateway_runtime.sheets_adapter.get_validation = Mock()
    codec = gateway_runtime.artifact_reference_codec
    args = {
        "source_id": "career_ops",
        "artifact_ref": codec.encode(
            source_id="career_ops", artifact_id="spreadsheet_12345"
        ),
        "sheet_ref": codec.encode_sheet(
            source_id="career_ops", artifact_id="spreadsheet_12345", sheet_id="7"
        ),
        "range": "A23",
    }

    async def scenario():
        results = []
        for sources, read in [
            (frozenset({"career_ops"}), False),
            (frozenset({"elsewhere"}), True),
        ]:
            principal = Principal(
                id="developer_test",
                type="developer",
                status="active",
                providers=ProviderScope(workspace=True),
                sources=sources,
                capabilities=CapabilityScope(read=read),
            )
            token = bind_principal(principal)
            try:
                async with Client(mcp_module.mcp_server) as client:
                    results.append(
                        await client.call_tool("inspect_sheet_validation", args)
                    )
            finally:
                reset_principal(token)
        return results

    assert all(r.is_error for r in run(scenario()))
    gateway_runtime.sheets_adapter.get_validation.assert_not_called()
