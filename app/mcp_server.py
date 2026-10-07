"""Governed MCP interface backed exclusively by Knowledge Gateway operations."""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from typing import Any, Literal, TypeVar

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent
from mcp.types import Tool as MCPTool
from pydantic import BaseModel

from app.acquisition import TOOLS as ACQUISITION_TOOLS
from app.acquisition import register_acquisition_tools
from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.adapters.google_workspace.models import (
    ArtifactConversionTarget,
    ArtifactMetadata,
    DriveFile,
    GoogleDocContent,
    SourceArtifactMutationResult,
    SourceMetadata,
)
from app.adapters.hubspot.mcp_client import account_metadata_from_result
from app.adapters.hubspot.models import HubSpotToolDescriptor, HubSpotToolResult
from app.adapters.hubspot.runtime import get_hubspot_runtime
from app.adapters.n8n.models import (
    N8NStatusResult,
    N8NToolListResult,
    N8NToolResult,
)
from app.adapters.n8n.instances import N8NInstanceRegistry
from app.adapters.n8n.runtime import get_n8n_client
from app.adapters.openwa.models import OpenWAStatusResult, OpenWAToolListResult
from app.adapters.openwa.runtime import get_openwa_client
from app.agent_signals import (
    AgentSignalRecord,
    AgentSignalStatusResult,
    SignalPriority,
    SignalStatus,
)
from app.artifact_refs import ArtifactReferenceCodec
from app.audit import correlation_id, emit_audit_record
from app.auth.principals import (
    CapabilityScope,
    active_principal,
    authorize_workspace_operation,
)
from app.document_production import (
    ArtifactReference,
    ArtifactReferenceConversionResult,
    ArtifactReferenceMutationResult,
    DocumentEditOperation,
    DocumentEditResult,
    DocumentQualityRequirements,
    DocumentQualityResult,
    DocumentStructure,
    DocumentTabInspectionResult,
    DocumentTabMutationResult,
)
from app.docx_production import (
    DocxEditOperation,
    DocxEditResult,
    DocxRequirements,
    DocxStructure,
    DocxValidationResult,
)
from app.knowledge import (
    convert_authorized_source_artifact,
    copy_authorized_source_artifact,
    create_authorized_document_tab,
    create_authorized_source_artifact,
    delete_authorized_document_tab,
    delete_authorized_source_artifact,
    discover_candidate_sources,
    edit_authorized_document_images,
    edit_authorized_source_document,
    edit_authorized_source_docx,
    edit_authorized_source_spreadsheet,
    get_candidate_details,
    inspect_authorized_document_structure,
    inspect_authorized_document_tab,
    inspect_authorized_docx_structure,
    inspect_authorized_sheet_validation,
    inspect_authorized_source_artifacts,
    inspect_authorized_spreadsheet_structure,
    inspect_authorized_visual_asset,
    list_authorized_source_files,
    list_registered_source_proposals,
    list_registered_sources,
    move_authorized_source_artifact,
    register_source_proposal,
    registered_source,
    registered_source_proposal,
    rename_authorized_document_tab,
    rename_authorized_source_artifact,
    resolve_authorized_source_artifact,
    retrieve_authorized_document,
    retrieve_authorized_sheet_range,
    share_authorized_source_artifact,
    update_authorized_source_artifact,
    validate_authorized_document_structure,
    validate_authorized_docx_structure,
    validate_authorized_spreadsheet_structure,
)
from app.operation_history import (
    DEFAULT_OPERATION_HISTORY_LIMIT,
    AgentSignalOperation,
    AgentSignalOperationHistoryEntry,
    GovernedOperation,
    OperationHistoryEntry,
    list_authorized_operation_history,
)
from app.policies.content_mutation import ContentMutationPolicy
from app.runtime import KnowledgeRuntime, get_runtime_gateway
from app.source_discovery.interface import CandidateDetailsResponse, DiscoveryResponse
from app.source_governance import (
    SourceProposalDetails,
    SourceProposalReceipt,
    SourceProposalSummary,
)
from app.source_registry import Classification, SourceRegistryMetadata
from app.spreadsheet_production import (
    SpreadsheetEditOperation,
    SpreadsheetEditResult,
    SpreadsheetQualityRequirements,
    SpreadsheetQualityResult,
    SpreadsheetStructure,
)
from app.visual_assets import (
    GoogleDocImageEditResult,
    GoogleDocImageOperation,
    VisualAssetInspection,
)

T = TypeVar("T")
MCP_DOCUMENT_RESULT_LIMIT = 20


class SourceListToolResult(BaseModel):
    sources: list[SourceRegistryMetadata]
    request_id: str


class DocumentListToolResult(BaseModel):
    documents: list[DriveFile]
    request_id: str


class ArtifactInspectionToolResult(BaseModel):
    artifacts: list[ArtifactMetadata]
    request_id: str


class SourceDiscoveryToolResult(DiscoveryResponse):
    request_id: str


class CandidateDetailsToolResult(CandidateDetailsResponse):
    request_id: str


class SourceProposalToolResult(SourceProposalReceipt):
    request_id: str


class SourceProposalListToolResult(BaseModel):
    proposals: list[SourceProposalSummary]
    request_id: str


class SourceProposalDetailsToolResult(BaseModel):
    proposal: SourceProposalDetails
    request_id: str


class SourceMutationToolResult(SourceArtifactMutationResult):
    request_id: str


class ArtifactConversionToolResult(ArtifactReferenceConversionResult):
    request_id: str


class ArtifactReferenceToolResult(ArtifactReference):
    request_id: str


class ArtifactReferenceMutationToolResult(ArtifactReferenceMutationResult):
    request_id: str


class DocumentStructureToolResult(DocumentStructure):
    request_id: str


class DocumentEditToolResult(DocumentEditResult):
    request_id: str


class DocumentQualityToolResult(DocumentQualityResult):
    request_id: str


class VisualAssetToolResult(VisualAssetInspection):
    request_id: str


class DocumentImageEditToolResult(GoogleDocImageEditResult):
    request_id: str


class DocxStructureToolResult(DocxStructure):
    request_id: str


class DocxEditToolResult(DocxEditResult):
    request_id: str


class DocxValidationToolResult(DocxValidationResult):
    request_id: str


class SpreadsheetStructureToolResult(SpreadsheetStructure):
    request_id: str


class SpreadsheetEditToolResult(SpreadsheetEditResult):
    request_id: str


class SpreadsheetQualityToolResult(SpreadsheetQualityResult):
    request_id: str


class SpreadsheetRangeToolResult(BaseModel):
    artifact_ref: str
    sheet_ref: str
    range: str
    values: list[list[object]]
    request_id: str
    source: SourceMetadata | None = None


class SheetValidationToolResult(BaseModel):
    artifact_ref: str
    sheet_ref: str
    sheet_title: str
    range: str
    cells: list[dict[str, Any]]
    validation_sources: list[dict[str, Any]]
    request_id: str
    source: SourceMetadata


class DocumentTabInspectionToolResult(DocumentTabInspectionResult):
    request_id: str


class DocumentTabMutationToolResult(DocumentTabMutationResult):
    request_id: str


class OperationHistoryToolResult(BaseModel):
    operations: list[OperationHistoryEntry]
    request_id: str


class AgentSignalListToolResult(BaseModel):
    signals: list[AgentSignalRecord]
    request_id: str


class AgentSignalToolResult(BaseModel):
    signal: AgentSignalRecord
    request_id: str


class AgentSignalOperationHistoryToolResult(BaseModel):
    operations: list[AgentSignalOperationHistoryEntry]
    request_id: str


class HubSpotToolListResult(BaseModel):
    tools: list[HubSpotToolDescriptor]
    request_id: str


class BrunovaMCPServer(MCPServer):
    """MCP server that projects provider-curated live downstream catalogs."""

    async def list_tools(self) -> list[MCPTool]:
        tools = await super().list_tools()
        principal = active_principal()
        effective_capabilities = _effective_developer_capabilities(principal)
        tools = [
            tool
            for tool in tools
            if _principal_can_see_tool(
                principal, tool.name, effective_capabilities=effective_capabilities
            )
        ]
        occupied = {tool.name for tool in tools}
        if principal.type == "management":
            try:
                downstream = await get_n8n_client().list_tools()
            except Exception:
                # Provider isolation: one outage never hides another provider.
                downstream = []
            for descriptor in downstream:
                exposed_name = _downstream_exposed_name("n8n", descriptor.name, occupied)
                occupied.add(exposed_name)
                tools.append(MCPTool(
                    name=exposed_name,
                    description=descriptor.description,
                    input_schema=descriptor.input_schema,
                    _meta={**descriptor.metadata, "provider": "n8n", "downstream_tool": descriptor.name},
                ))
            try:
                openwa_tools = await get_openwa_client().list_tools()
            except Exception:
                openwa_tools = []
            for descriptor in openwa_tools:
                exposed_name = _downstream_exposed_name("openwa", descriptor.name, occupied)
                occupied.add(exposed_name)
                tools.append(MCPTool(
                    name=exposed_name,
                    description=descriptor.description,
                    input_schema=descriptor.input_schema,
                    annotations=descriptor.annotations or None,
                    _meta={
                        **descriptor.metadata,
                        "provider": "openwa",
                        "downstream_tool": descriptor.name,
                        "tier": descriptor.tier,
                    },
                ))
        return tools

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Context | None = None,
    ) -> CallToolResult:
        principal = active_principal()
        authorization_error = _tool_authorization_error(principal, name)
        if authorization_error:
            denied_source_id = arguments.get("source_id")
            if not isinstance(denied_source_id, str):
                denied_source_id = None
            emit_audit_record(
                request_id=correlation_id(str(context.request_id) if context else None),
                action="mcp_tool_call",
                resource_id=None,
                resource_type="mcp_tool",
                result="rejected",
                http_status=403,
                error_code=authorization_error,
                provider=_tool_provider(name),
                source_id=denied_source_id,
                tool=name,
            )
            return CallToolResult(
                content=[TextContent(type="text", text="tool_denied: The requested operation is not authorized.")],
                is_error=True,
            )
        is_n8n = name.startswith("n8n_") and name not in N8N_STATIC_TOOLS
        is_openwa = name.startswith("openwa_") and name not in {
            "openwa_status", "openwa_list_tools"
        }
        if not is_n8n and not is_openwa:
            if name in ACQUISITION_TOOLS:
                try:
                    buyer_fields = {
                        "acquisition_get_buyer_dry_run": {"cycle_id", "account_id"},
                        "acquisition_request_buyer_dry_run": {"command_id", "cycle_id", "account_id", "expected_attention_version", "source_key"},
                        "acquisition_record_attention_disposition": {"command_id","objective_reference","attention_id","expected_version","disposition","reason","notes"},
                        "acquisition_authorize_controlled_effect": {"command_id","objective_reference","message_id","target_id","sender_id","expected_binding_hash","expires_at"},
                        "acquisition_request_effect_reconciliation": {"command_id","objective_reference","intent_id"},
                        "acquisition_acknowledge_effect_attention": {"command_id","objective_reference","attention_id","expected_version","reason"},
                        "acquisition_edit_message_draft": {"command_id","objective_reference","cycle_id","account_id","expected_attention_version","source_key","edit_text"},
                    }
                    # These existing commands support the exact approved-wave
                    # path; the strict outer allowlist must preserve that binding.
                    for wave_tool in ("acquisition_record_attention_disposition", "acquisition_authorize_controlled_effect",
                                      "acquisition_request_effect_reconciliation", "acquisition_acknowledge_effect_attention",
                                      "acquisition_edit_message_draft"):
                        buyer_fields[wave_tool].add("wave_id")
                    if name in buyer_fields and set(arguments or {}) - buyer_fields[name]:
                        raise ToolError("unexpected_buyer_argument")
                    return await super().call_tool(name, arguments, context)
                except ToolError:
                    return CallToolResult(content=[TextContent(type="text", text="ACQUISITION_VALIDATION_FAILED: bounded arguments required.")], is_error=True)
            return await super().call_tool(name, arguments, context)
        if is_openwa:
            return await self._call_openwa_tool(name, arguments, context)
        client = get_n8n_client()
        tools = await client.list_tools()
        occupied = set(N8N_STATIC_TOOLS)
        mapping: dict[str, str] = {}
        for descriptor in tools:
            exposed = _downstream_exposed_name("n8n", descriptor.name, occupied)
            occupied.add(exposed)
            mapping[exposed] = descriptor.name
        downstream_name = mapping.get(name)
        if downstream_name is None:
            return CallToolResult(
                content=[TextContent(type="text", text="n8n_tool_unavailable: The n8n tool is not currently exposed.")],
                is_error=True,
            )
        request_id = correlation_id(str(context.request_id) if context else None)
        approval_reference = _approval_reference(context)
        started = time.monotonic()
        try:
            result = await client.call_tool(downstream_name, arguments)
            downstream_failed = bool(result.get("isError", False)) if isinstance(result, dict) else False
            emit_audit_record(
                request_id=request_id, action="n8n_tool_call", resource_id=None,
                resource_type="n8n_mcp_tool",
                result="error" if downstream_failed else "success",
                http_status=502 if downstream_failed else 200,
                provider="n8n", tool=downstream_name,
                approval_reference=approval_reference,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            return CallToolResult.model_validate(result)
        except WorkspaceAdapterError as error:
            emit_audit_record(
                request_id=request_id, action="n8n_tool_call", resource_id=None,
                resource_type="n8n_mcp_tool",
                result="rejected" if error.status_code < 500 else "error",
                http_status=error.status_code, error_code=error.code,
                provider="n8n", tool=downstream_name,
                approval_reference=approval_reference,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            return CallToolResult(
                content=[TextContent(type="text", text=f"{error.code}: {error.message}")],
                is_error=True,
            )

    async def _call_openwa_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Context | None,
    ) -> CallToolResult:
        request_id = correlation_id(str(context.request_id) if context else None)
        approval_reference = ContentMutationPolicy.normalized_approval_reference(
            _approval_reference(context) or ""
        )
        started = time.monotonic()
        try:
            client = get_openwa_client()
            tools = await client.list_tools()
            occupied = {"openwa_status", "openwa_list_tools"}
            mapping: dict[str, Any] = {}
            for descriptor in tools:
                exposed = _downstream_exposed_name("openwa", descriptor.name, occupied)
                occupied.add(exposed)
                mapping[exposed] = descriptor
            descriptor = mapping.get(name)
            if descriptor is None:
                raise WorkspaceAdapterError(
                    "openwa_tool_unavailable",
                    "The OpenWA tool is not currently exposed.",
                    404,
                )
            arguments = dict(arguments)
            argument_reference = arguments.pop("approval_reference", None)
            if approval_reference is None and isinstance(argument_reference, str):
                approval_reference = ContentMutationPolicy.normalized_approval_reference(
                    argument_reference
                )
            arguments.pop("approval_evidence", None)
            result = await client.call_tool(descriptor.name, arguments)
            downstream_failed = bool(result.get("isError", False)) if isinstance(result, dict) else False
            emit_audit_record(
                request_id=request_id,
                action="openwa_tool_call",
                resource_id=None,
                resource_type="openwa_mcp_tool",
                result="error" if downstream_failed else "success",
                http_status=502 if downstream_failed else 200,
                provider="openwa",
                tool=descriptor.name,
                operation_classification=descriptor.tier,
                approval_reference=approval_reference,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            return CallToolResult.model_validate(result)
        except WorkspaceAdapterError as error:
            emit_audit_record(
                request_id=request_id,
                action="openwa_tool_call",
                resource_id=None,
                resource_type="openwa_mcp_tool",
                result="rejected" if error.status_code < 500 else "error",
                http_status=error.status_code,
                error_code=error.code,
                provider="openwa",
                tool=name.removeprefix("openwa_"),
                approval_reference=approval_reference,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            return CallToolResult(
                content=[TextContent(type="text", text=f"{error.code}: {error.message}")],
                is_error=True,
            )


def _downstream_exposed_name(
    provider: str, downstream_name: str, occupied: set[str]
) -> str:
    preferred = f"{provider}_{downstream_name}"
    return f"{provider}_downstream_{downstream_name}" if preferred in occupied else preferred


MANAGEMENT_ONLY_TOOLS = frozenset(
    {
        "get_operation_history",
        "discover_source_candidates",
        "get_source_candidate_details",
        "create_source_proposal",
        "list_source_proposals",
        "get_source_proposal",
        "list_agent_signals",
        "get_agent_signal",
        "claim_agent_signal",
        "complete_agent_signal",
        "dismiss_agent_signal",
        "release_agent_signal",
        "agent_signal_status",
        "get_agent_signal_operation_history",
        "inspect_visual_asset",
        "edit_source_document_images",
        "inspect_docx_structure",
        "edit_source_docx",
        "validate_docx_structure",
    }
)

SIGNAL_WORKER_TOOLS = {
    "list_agent_signals": "list",
    "get_agent_signal": "get",
    "claim_agent_signal": "claim",
    "release_agent_signal": "release",
    "complete_agent_signal": "complete",
    "dismiss_agent_signal": "dismiss",
}

TOOL_CAPABILITIES: dict[str, str] = {
    "list_sources": "read",
    "resolve_source_artifact": "read",
    "inspect_document_structure": "read",
    "inspect_document_tab": "read",
    "validate_document_structure": "read",
    "inspect_spreadsheet_structure": "read",
    "validate_spreadsheet_structure": "read",
    "list_source_documents": "read",
    "inspect_source_artifacts": "read",
    "retrieve_document": "read",
    "retrieve_sheet_range": "read",
    "inspect_sheet_validation": "read",
    "inspect_visual_asset": "read",
    "inspect_docx_structure": "read",
    "validate_docx_structure": "read",
    "copy_source_artifact": "create",
    "create_source_artifact": "create",
    "rename_source_artifact": "update",
    "create_document_tab": "update",
    "rename_document_tab": "update",
    "delete_document_tab": "update",
    "edit_source_document": "update",
    "edit_source_spreadsheet": "update",
    "edit_source_document_images": "update",
    "edit_source_docx": "update",
    "update_source_artifact": "update",
    "move_source_artifact": "move",
    "delete_source_artifact": "delete",
    "share_source_artifact": "share",
    "convert_source_artifact": "convert",
}

N8N_ROUTED_TOOLS = frozenset({
    "n8n_instances", "n8n_instance_status", "n8n_instance_list_tools",
    "n8n_instance_call_tool",
})
N8N_STATIC_TOOLS = N8N_ROUTED_TOOLS | {"n8n_status", "n8n_list_tools"}


def _tool_provider(name: str) -> str:
    if name.startswith("acquisition_"):
        return "acquisition"
    if "agent_signal" in name:
        return "agent_signals"
    if name.startswith("hubspot_"):
        return "hubspot"
    if name.startswith("n8n_"):
        return "n8n"
    if name.startswith("openwa_"):
        return "openwa"
    return "workspace"


def _effective_developer_capabilities(principal: Any) -> frozenset[str] | None:
    if principal.type != "developer":
        return None
    try:
        sources = get_runtime_gateway().registry.sources
    except Exception:
        return frozenset()
    return frozenset(
        capability
        for capability in CapabilityScope.model_fields
        if principal.allows_capability(capability)
        and any(
            principal.allows_source(source.id)
            and source.status.value == "active"
            and bool(getattr(source.capabilities, capability, False))
            for source in sources
        )
    )


def _principal_can_see_tool(
    principal: Any,
    name: str,
    *,
    effective_capabilities: frozenset[str] | None = None,
) -> bool:
    if _tool_authorization_error(principal, name):
        return False
    if name in N8N_ROUTED_TOOLS:
        return True
    capability = TOOL_CAPABILITIES.get(name)
    return effective_capabilities is None or capability in effective_capabilities


def _tool_authorization_error(principal: Any, name: str) -> str | None:
    if name.startswith("acquisition_"):
        return None if principal.type == "management" and name in ACQUISITION_TOOLS else "tool_denied"
    if principal.type == "management":
        return None
    if principal.type == "signal_worker":
        operation = SIGNAL_WORKER_TOOLS.get(name)
        return None if operation in principal.signal_operations else "tool_denied"
    if name in N8N_ROUTED_TOOLS:
        return None if principal.allows_provider("n8n") and principal.n8n_tools else "tool_denied"
    if name in MANAGEMENT_ONLY_TOOLS:
        return "tool_denied"
    provider = _tool_provider(name)
    if not principal.allows_provider(provider):
        return "provider_denied"
    capability = TOOL_CAPABILITIES.get(name)
    # Developer-visible tools are allowlisted rather than inferred by prefix.
    if capability is None:
        return "tool_denied"
    if not principal.allows_capability(capability):
        return "capability_denied"
    return None


def _approval_reference(context: Context | None) -> str | None:
    if context is None:
        return None
    try:
        meta = context.request_context.meta
    except ValueError:
        return None
    if meta is None:
        return None
    value = meta.get("approval_reference") if hasattr(meta, "get") else getattr(meta, "approval_reference", None)
    return str(value)[:256] if value else None


mcp_server = BrunovaMCPServer(
    name="brunova-knowledge-gateway",
    version="0.30.1",
    instructions=(
        "Use only the capabilities and sources exposed in this authenticated "
        "principal's tool catalog. Mutations remain capability-gated and keep "
        "management approval semantics while developer mutations are authorized "
        "by principal scope."
    ),
)


def _reference_codec(runtime: KnowledgeRuntime) -> ArtifactReferenceCodec:
    if runtime.artifact_reference_codec is None:
        raise WorkspaceAdapterError(
            "artifact_reference_unavailable",
            "Artifact references are not configured.",
            503,
        )
    return runtime.artifact_reference_codec


def _resolved_artifact_id(
    runtime: KnowledgeRuntime,
    *,
    source_id: str,
    artifact_ref: str | None,
    legacy_id: str | None,
) -> str:
    if artifact_ref:
        return _reference_codec(runtime).decode(artifact_ref, source_id=source_id)
    if legacy_id:
        return legacy_id
    raise WorkspaceAdapterError(
        "artifact_reference_required", "An artifact reference is required.", 422
    )


def _mcp_request_id(ctx: Context) -> str:
    return correlation_id(str(ctx.request_id))


def _execute_tool(
    *,
    ctx: Context,
    action: str,
    operation: Callable[[KnowledgeRuntime, str], T],
    source_id: str | None = None,
    resource_id: str | None = None,
    resource_type: str | None = None,
    candidate_count: Callable[[T], int] | None = None,
    proposal_id: Callable[[T], str] | None = None,
    result_resource_id: Callable[[T], str] | None = None,
    created_resource_id: Callable[[T], str] | None = None,
    approval_reference: str | None = None,
    audience: str | None = None,
    destination_source_id: str | None = None,
    revision_id: Callable[[T], str] | None = None,
    artifact_version: Callable[[T], str] | None = None,
    asset_refs: list[str] | None = None,
    asset_transformations: Callable[[T], list[dict[str, Any]]] | None = None,
) -> T:
    request_id = _mcp_request_id(ctx)
    source_classification: str | None = None
    capability = TOOL_CAPABILITIES.get(action)
    principal = active_principal()
    authorization_mode = (
        "principal_scope"
        if principal.type == "developer"
        else "authenticated_principal"
        if capability in {"create", "update", "move", "delete", "share", "convert"}
        else None
    )
    try:
        runtime = get_runtime_gateway()
        if principal.type == "developer" and capability is None:
            raise WorkspaceAdapterError(
                "tool_denied", "The requested operation is not authorized.", 403
            )
        if source_id:
            source = registered_source(runtime.registry, source_id)
            source_classification = source.classification.value
            if principal.type == "developer":
                authorize_workspace_operation(
                    principal, capability=capability or "read", source=source
                )
            if destination_source_id and principal.type == "developer":
                destination_source = registered_source(
                    runtime.registry, destination_source_id
                )
                authorize_workspace_operation(
                    principal,
                    capability=capability or "move",
                    source=destination_source,
                )
        elif capability and principal.type == "developer":
            authorize_workspace_operation(principal, capability=capability)
        result = operation(runtime, request_id)
        emit_audit_record(
            request_id=request_id,
            action=action,
            resource_id=(
                result_resource_id(result) if result_resource_id else resource_id
            ),
            resource_type=resource_type,
            result="success",
            http_status=200,
            source_id=source_id,
            source_classification=source_classification,
            candidate_count=(candidate_count(result) if candidate_count else None),
            proposal_id=(proposal_id(result) if proposal_id else None),
            approval_reference=approval_reference,
            audience=audience,
            created_resource_id=(
                created_resource_id(result) if created_resource_id else None
            ),
            destination_source_id=destination_source_id,
            provider="workspace" if capability else "gateway",
            tool=action,
            capability=capability,
            authorization_mode=authorization_mode,
            revision_id=revision_id(result) if revision_id else None,
            artifact_version=artifact_version(result) if artifact_version else None,
            asset_refs=asset_refs,
            asset_transformations=(
                asset_transformations(result) if asset_transformations else None
            ),
        )
        return result
    except WorkspaceAdapterError as error:
        emit_audit_record(
            request_id=request_id,
            action=action,
            resource_id=resource_id,
            resource_type=resource_type,
            result="rejected" if error.status_code < 500 else "error",
            http_status=error.status_code,
            error_code=error.code,
            source_id=source_id,
            source_classification=source_classification,
            approval_reference=approval_reference,
            audience=audience,
            destination_source_id=destination_source_id,
            provider="workspace" if capability else "gateway",
            tool=action,
            capability=capability,
            authorization_mode=authorization_mode,
            asset_refs=asset_refs,
        )
        raise RuntimeError(f"{error.code}: {error.message}") from error
    except Exception:
        emit_audit_record(
            request_id=request_id,
            action=action,
            resource_id=resource_id,
            resource_type=resource_type,
            result="error",
            http_status=500,
            error_code="unhandled_error",
            source_id=source_id,
            source_classification=source_classification,
            approval_reference=approval_reference,
            audience=audience,
            destination_source_id=destination_source_id,
            provider="workspace" if capability else "gateway",
            tool=action,
            capability=capability,
            authorization_mode=authorization_mode,
            asset_refs=asset_refs,
        )
        raise


@mcp_server.tool()
def list_sources(ctx: Context) -> SourceListToolResult:
    """List non-sensitive metadata for Brunova knowledge sources."""

    return _execute_tool(
        ctx=ctx,
        action="list_sources",
        operation=lambda runtime, request_id: SourceListToolResult(
            sources=[
                source
                for source in list_registered_sources(runtime.registry)
                if active_principal().type == "management"
                or (
                    active_principal().allows_source(source.id)
                    and source.status.value == "active"
                    and source.capabilities.read
                )
            ],
            request_id=request_id,
        ),
        resource_type="source_registry",
    )


@mcp_server.tool()
def get_operation_history(
    ctx: Context,
    source_id: str | None = None,
    operation: GovernedOperation | None = None,
    limit: int = DEFAULT_OPERATION_HISTORY_LIMIT,
) -> OperationHistoryToolResult:
    """List safe governed-mutation audit metadata for authorized sources."""

    def operation_query(
        runtime: KnowledgeRuntime,
        request_id: str,
    ) -> OperationHistoryToolResult:
        if runtime.operation_history_store is None:
            raise WorkspaceAdapterError(
                "operation_history_unavailable",
                "Operation history is not configured.",
                503,
            )
        return OperationHistoryToolResult(
            operations=list_authorized_operation_history(
                store=runtime.operation_history_store,
                registry=runtime.registry,
                source_policy=runtime.source_policy,
                source_id=source_id,
                operation=operation,
                limit=limit,
            ),
            request_id=request_id,
        )

    return _execute_tool(
        ctx=ctx,
        action="get_operation_history",
        operation=operation_query,
        source_id=source_id,
        resource_type="operation_history",
    )


@mcp_server.tool()
def discover_source_candidates(
    ctx: Context,
    limit: int = 25,
) -> SourceDiscoveryToolResult:
    """Propose unregistered Shared Drives and root folders for human review."""

    def operation(
        runtime: KnowledgeRuntime,
        request_id: str,
    ) -> SourceDiscoveryToolResult:
        result = discover_candidate_sources(
            source_discovery=runtime.source_discovery,
            limit=limit,
        )
        response = DiscoveryResponse.from_result(result)
        return SourceDiscoveryToolResult(
            **response.model_dump(),
            request_id=request_id,
        )

    return _execute_tool(
        ctx=ctx,
        action="discover_source_candidates",
        operation=operation,
        resource_type="source_discovery",
        candidate_count=lambda result: len(result.candidates),
    )


@mcp_server.tool()
def get_source_candidate_details(
    candidate_id: str,
    ctx: Context,
) -> CandidateDetailsToolResult:
    """Inspect safe details for one currently discoverable source candidate."""

    def operation(
        runtime: KnowledgeRuntime,
        request_id: str,
    ) -> CandidateDetailsToolResult:
        response = get_candidate_details(
            source_discovery=runtime.source_discovery,
            candidate_id=candidate_id,
        )
        return CandidateDetailsToolResult(
            **response.model_dump(),
            request_id=request_id,
        )

    return _execute_tool(
        ctx=ctx,
        action="get_source_candidate_details",
        operation=operation,
        resource_id=candidate_id,
        resource_type="source_candidate",
    )


@mcp_server.tool()
def create_source_proposal(
    candidate_id: str,
    name: str,
    classification: Classification,
    reason: str,
    ctx: Context,
) -> SourceProposalToolResult:
    """Register a pending intent for human review without applying any change."""

    def operation(
        runtime: KnowledgeRuntime,
        request_id: str,
    ) -> SourceProposalToolResult:
        proposal = register_source_proposal(
            source_discovery=runtime.source_discovery,
            proposal_store=runtime.proposal_store,
            candidate_id=candidate_id,
            name=name,
            classification=classification,
            reason=reason,
            request_id=request_id,
        )
        receipt = SourceProposalReceipt(
            proposal_id=proposal.proposal_id,
            status=proposal.status,
        )
        return SourceProposalToolResult(
            **receipt.model_dump(),
            request_id=request_id,
        )

    return _execute_tool(
        ctx=ctx,
        action="create_source_proposal",
        operation=operation,
        resource_id=candidate_id,
        resource_type="source_proposal",
        proposal_id=lambda result: result.proposal_id,
    )


@mcp_server.tool()
def list_source_proposals(ctx: Context) -> SourceProposalListToolResult:
    """List safe pending proposal summaries from the durable registry."""

    return _execute_tool(
        ctx=ctx,
        action="list_source_proposals",
        operation=lambda runtime, request_id: SourceProposalListToolResult(
            proposals=list_registered_source_proposals(runtime.proposal_store),
            request_id=request_id,
        ),
        resource_type="source_proposal_registry",
    )


@mcp_server.tool()
def get_source_proposal(
    proposal_id: str,
    ctx: Context,
) -> SourceProposalDetailsToolResult:
    """Get safe review details for one durable pending source proposal."""

    return _execute_tool(
        ctx=ctx,
        action="get_source_proposal",
        operation=lambda runtime, request_id: SourceProposalDetailsToolResult(
            proposal=registered_source_proposal(
                runtime.proposal_store,
                proposal_id,
            ),
            request_id=request_id,
        ),
        resource_id=proposal_id,
        resource_type="source_proposal",
        proposal_id=lambda result: result.proposal.proposal_id,
    )


@mcp_server.tool()
def resolve_source_artifact(
    source_id: str,
    ctx: Context,
    name: str | None = None,
    logical_path: str | None = None,
    artifact_type: str | None = None,
) -> ArtifactReferenceToolResult:
    """Resolve an exact source-scoped selector to an opaque actionable handle."""

    return _execute_tool(
        ctx=ctx,
        action="resolve_source_artifact",
        operation=lambda runtime, request_id: ArtifactReferenceToolResult(
            **resolve_authorized_source_artifact(
                registry=runtime.registry,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                reference_codec=_reference_codec(runtime),
                source_id=source_id,
                name=name,
                logical_path=logical_path,
                artifact_type=artifact_type,
            ).model_dump(),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_type="source_artifact_reference",
        result_resource_id=lambda result: result.artifact_ref,
    )


@mcp_server.tool()
def copy_source_artifact(
    source_id: str,
    artifact_ref: str,
    name: str,
    ctx: Context,
    approval_reference: str = "",
    destination_ref: str | None = None,
) -> ArtifactReferenceMutationToolResult:
    """Copy one native Google Doc or Sheet while preserving the source artifact."""

    return _execute_tool(
        ctx=ctx,
        action="copy_source_artifact",
        operation=lambda runtime, request_id: ArtifactReferenceMutationToolResult(
            **copy_authorized_source_artifact(
                mutation_policy=runtime.mutation_policy,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                reference_codec=_reference_codec(runtime),
                source_id=source_id,
                artifact_ref=artifact_ref,
                name=name,
                approval_reference=approval_reference,
                destination_ref=destination_ref,
            ).model_dump(),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_id=artifact_ref,
        resource_type="google_drive_artifact",
        result_resource_id=lambda result: result.artifact.artifact_ref,
        approval_reference=ContentMutationPolicy.normalized_approval_reference(approval_reference),
    )


@mcp_server.tool()
def rename_source_artifact(
    source_id: str,
    artifact_ref: str,
    name: str,
    ctx: Context,
    approval_reference: str = "",
) -> ArtifactReferenceMutationToolResult:
    """Rename one source-scoped artifact through the governed update capability."""

    return _execute_tool(
        ctx=ctx,
        action="rename_source_artifact",
        operation=lambda runtime, request_id: ArtifactReferenceMutationToolResult(
            **rename_authorized_source_artifact(
                mutation_policy=runtime.mutation_policy,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                reference_codec=_reference_codec(runtime),
                source_id=source_id,
                artifact_ref=artifact_ref,
                name=name,
                approval_reference=approval_reference,
            ).model_dump(),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_id=artifact_ref,
        resource_type="google_drive_artifact",
        result_resource_id=lambda result: result.artifact.artifact_ref,
        approval_reference=ContentMutationPolicy.normalized_approval_reference(approval_reference),
    )


@mcp_server.tool()
def inspect_document_structure(
    source_id: str,
    artifact_ref: str,
    ctx: Context,
) -> DocumentStructureToolResult:
    """Inspect bounded Google Docs topology, indexes, styles, and placeholders."""

    return _execute_tool(
        ctx=ctx,
        action="inspect_document_structure",
        operation=lambda runtime, request_id: DocumentStructureToolResult(
            **inspect_authorized_document_structure(
                registry=runtime.registry,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                docs_adapter=runtime.docs_adapter,
                reference_codec=_reference_codec(runtime),
                source_id=source_id,
                artifact_ref=artifact_ref,
            ).model_dump(),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_id=artifact_ref,
        resource_type="google_document_structure",
    )


@mcp_server.tool()
def inspect_document_tab(
    source_id: str,
    artifact_ref: str,
    tab_ref: str,
    ctx: Context,
) -> DocumentTabInspectionToolResult:
    """Inspect one document tab selected by an opaque artifact-bound reference."""

    return _execute_tool(
        ctx=ctx,
        action="inspect_document_tab",
        operation=lambda runtime, request_id: DocumentTabInspectionToolResult(
            **inspect_authorized_document_tab(
                registry=runtime.registry,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                docs_adapter=runtime.docs_adapter,
                reference_codec=_reference_codec(runtime),
                source_id=source_id,
                artifact_ref=artifact_ref,
                tab_ref=tab_ref,
            ).model_dump(),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_id=artifact_ref,
        resource_type="google_document_tab",
    )


@mcp_server.tool()
def create_document_tab(
    source_id: str,
    artifact_ref: str,
    title: str,
    required_revision_id: str,
    ctx: Context,
    index: int | None = None,
    parent_tab_ref: str | None = None,
    approval_reference: str = "",
) -> DocumentTabMutationToolResult:
    """Create one governed document tab at an inspected revision."""

    return _execute_tool(
        ctx=ctx,
        action="create_document_tab",
        operation=lambda runtime, request_id: DocumentTabMutationToolResult(
            **create_authorized_document_tab(
                mutation_policy=runtime.mutation_policy,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                docs_adapter=runtime.docs_adapter,
                reference_codec=_reference_codec(runtime),
                source_id=source_id,
                artifact_ref=artifact_ref,
                title=title,
                required_revision_id=required_revision_id,
                approval_reference=approval_reference,
                index=index,
                parent_tab_ref=parent_tab_ref,
            ).model_dump(),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_id=artifact_ref,
        resource_type="google_document_tab",
        approval_reference=ContentMutationPolicy.normalized_approval_reference(
            approval_reference
        ),
    )


@mcp_server.tool()
def rename_document_tab(
    source_id: str,
    artifact_ref: str,
    tab_ref: str,
    title: str,
    required_revision_id: str,
    ctx: Context,
    approval_reference: str = "",
) -> DocumentTabMutationToolResult:
    """Rename one governed document tab selected by an opaque reference."""

    return _execute_tool(
        ctx=ctx,
        action="rename_document_tab",
        operation=lambda runtime, request_id: DocumentTabMutationToolResult(
            **rename_authorized_document_tab(
                mutation_policy=runtime.mutation_policy,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                docs_adapter=runtime.docs_adapter,
                reference_codec=_reference_codec(runtime),
                source_id=source_id,
                artifact_ref=artifact_ref,
                tab_ref=tab_ref,
                title=title,
                required_revision_id=required_revision_id,
                approval_reference=approval_reference,
            ).model_dump(),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_id=artifact_ref,
        resource_type="google_document_tab",
        approval_reference=ContentMutationPolicy.normalized_approval_reference(
            approval_reference
        ),
    )


@mcp_server.tool()
def delete_document_tab(
    source_id: str,
    artifact_ref: str,
    tab_ref: str,
    required_revision_id: str,
    ctx: Context,
    approval_reference: str = "",
) -> DocumentTabMutationToolResult:
    """Delete one leaf document tab while preserving at least one tab."""

    return _execute_tool(
        ctx=ctx,
        action="delete_document_tab",
        operation=lambda runtime, request_id: DocumentTabMutationToolResult(
            **delete_authorized_document_tab(
                mutation_policy=runtime.mutation_policy,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                docs_adapter=runtime.docs_adapter,
                reference_codec=_reference_codec(runtime),
                source_id=source_id,
                artifact_ref=artifact_ref,
                tab_ref=tab_ref,
                required_revision_id=required_revision_id,
                approval_reference=approval_reference,
            ).model_dump(),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_id=artifact_ref,
        resource_type="google_document_tab",
        approval_reference=ContentMutationPolicy.normalized_approval_reference(
            approval_reference
        ),
    )


@mcp_server.tool()
def edit_source_document(
    source_id: str,
    artifact_ref: str,
    required_revision_id: str,
    operations: list[DocumentEditOperation],
    ctx: Context,
    tab_ref: str | None = None,
    approval_reference: str = "",
) -> DocumentEditToolResult:
    """Apply only allowlisted semantic Docs operations at an inspected revision."""

    return _execute_tool(
        ctx=ctx,
        action="edit_source_document",
        operation=lambda runtime, request_id: DocumentEditToolResult(
            **edit_authorized_source_document(
                mutation_policy=runtime.mutation_policy,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                docs_adapter=runtime.docs_adapter,
                reference_codec=_reference_codec(runtime),
                source_id=source_id,
                artifact_ref=artifact_ref,
                required_revision_id=required_revision_id,
                operations=operations,
                tab_ref=tab_ref,
                approval_reference=approval_reference,
            ).model_dump(),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_id=artifact_ref,
        resource_type="google_document",
        approval_reference=ContentMutationPolicy.normalized_approval_reference(approval_reference),
    )


@mcp_server.tool()
def validate_document_structure(
    source_id: str,
    artifact_ref: str,
    requirements: DocumentQualityRequirements,
    ctx: Context,
    expected_revision_id: str | None = None,
) -> DocumentQualityToolResult:
    """Run a read-only structural quality gate against one inspected document."""

    return _execute_tool(
        ctx=ctx,
        action="validate_document_structure",
        operation=lambda runtime, request_id: DocumentQualityToolResult(
            **validate_authorized_document_structure(
                registry=runtime.registry,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                docs_adapter=runtime.docs_adapter,
                reference_codec=_reference_codec(runtime),
                source_id=source_id,
                artifact_ref=artifact_ref,
                requirements=requirements,
                expected_revision_id=expected_revision_id,
            ).model_dump(),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_id=artifact_ref,
        resource_type="google_document_quality",
    )


@mcp_server.tool()
def inspect_visual_asset(
    source_id: str,
    artifact_ref: str,
    ctx: Context,
) -> VisualAssetToolResult:
    """Inspect a source-scoped PNG, JPEG, or SVG and issue an opaque asset_ref."""

    return _execute_tool(
        ctx=ctx,
        action="inspect_visual_asset",
        operation=lambda runtime, request_id: VisualAssetToolResult(
            **inspect_authorized_visual_asset(
                registry=runtime.registry,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                reference_codec=_reference_codec(runtime),
                source_id=source_id,
                artifact_ref=artifact_ref,
            ).model_dump(),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_id=artifact_ref,
        resource_type="governed_visual_asset",
    )


@mcp_server.tool()
def edit_source_document_images(
    source_id: str,
    artifact_ref: str,
    required_revision_id: str,
    operations: list[GoogleDocImageOperation],
    ctx: Context,
    approval_reference: str = "",
) -> DocumentImageEditToolResult:
    """Insert or replace governed assets in a Google Doc at an inspected revision."""

    return _execute_tool(
        ctx=ctx,
        action="edit_source_document_images",
        operation=lambda runtime, request_id: DocumentImageEditToolResult(
            **edit_authorized_document_images(
                mutation_policy=runtime.mutation_policy,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                docs_adapter=runtime.docs_adapter,
                reference_codec=_reference_codec(runtime),
                source_id=source_id,
                artifact_ref=artifact_ref,
                required_revision_id=required_revision_id,
                operations=operations,
                approval_reference=approval_reference,
            ).model_dump(),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_id=artifact_ref,
        resource_type="google_document_images",
        approval_reference=ContentMutationPolicy.normalized_approval_reference(approval_reference),
        revision_id=lambda result: result.revision_id,
        asset_refs=[operation.asset_ref for operation in operations],
        asset_transformations=lambda result: [
            item.model_dump() for item in result.asset_transformations
        ],
    )


@mcp_server.tool()
def inspect_docx_structure(
    source_id: str,
    artifact_ref: str,
    ctx: Context,
) -> DocxStructureToolResult:
    """Inspect bounded DOCX structure, safe anchors, flags, and preconditions."""

    return _execute_tool(
        ctx=ctx,
        action="inspect_docx_structure",
        operation=lambda runtime, request_id: DocxStructureToolResult(
            **inspect_authorized_docx_structure(
                registry=runtime.registry,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                reference_codec=_reference_codec(runtime),
                source_id=source_id,
                artifact_ref=artifact_ref,
            ).model_dump(),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_id=artifact_ref,
        resource_type="docx_structure",
    )


@mcp_server.tool()
def edit_source_docx(
    source_id: str,
    artifact_ref: str,
    required_version: str,
    required_content_hash: str,
    operations: list[DocxEditOperation],
    ctx: Context,
    approval_reference: str = "",
) -> DocxEditToolResult:
    """Apply bounded native OOXML edits to the same governed Drive artifact."""

    return _execute_tool(
        ctx=ctx,
        action="edit_source_docx",
        operation=lambda runtime, request_id: DocxEditToolResult(
            **edit_authorized_source_docx(
                mutation_policy=runtime.mutation_policy,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                reference_codec=_reference_codec(runtime),
                source_id=source_id,
                artifact_ref=artifact_ref,
                required_version=required_version,
                required_content_hash=required_content_hash,
                operations=operations,
                approval_reference=approval_reference,
            ).model_dump(),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_id=artifact_ref,
        resource_type="docx",
        approval_reference=ContentMutationPolicy.normalized_approval_reference(approval_reference),
        artifact_version=lambda result: result.version,
        asset_refs=[
            operation.asset_ref
            for operation in operations
            if hasattr(operation, "asset_ref")
        ],
    )


@mcp_server.tool()
def validate_docx_structure(
    source_id: str,
    artifact_ref: str,
    requirements: DocxRequirements,
    ctx: Context,
    expected_version: str | None = None,
) -> DocxValidationToolResult:
    """Run bounded structural checks against the active DOCX artifact."""

    return _execute_tool(
        ctx=ctx,
        action="validate_docx_structure",
        operation=lambda runtime, request_id: DocxValidationToolResult(
            **validate_authorized_docx_structure(
                registry=runtime.registry,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                reference_codec=_reference_codec(runtime),
                source_id=source_id,
                artifact_ref=artifact_ref,
                requirements=requirements,
                expected_version=expected_version,
            ).model_dump(),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_id=artifact_ref,
        resource_type="docx_quality",
    )


@mcp_server.tool()
def inspect_spreadsheet_structure(
    source_id: str,
    artifact_ref: str,
    ctx: Context,
) -> SpreadsheetStructureToolResult:
    """Inspect allowlisted Google Sheets metadata without returning cell contents."""

    return _execute_tool(
        ctx=ctx,
        action="inspect_spreadsheet_structure",
        operation=lambda runtime, request_id: SpreadsheetStructureToolResult(
            **inspect_authorized_spreadsheet_structure(
                registry=runtime.registry,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                sheets_adapter=runtime.sheets_adapter,
                reference_codec=_reference_codec(runtime),
                source_id=source_id,
                artifact_ref=artifact_ref,
            ).model_dump(),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_id=artifact_ref,
        resource_type="google_spreadsheet_structure",
    )


@mcp_server.tool()
def edit_source_spreadsheet(
    source_id: str,
    artifact_ref: str,
    operations: list[SpreadsheetEditOperation],
    ctx: Context,
    approval_reference: str = "",
) -> SpreadsheetEditToolResult:
    """Apply bounded, allowlisted semantic operations to a native Google Sheet."""

    return _execute_tool(
        ctx=ctx,
        action="edit_source_spreadsheet",
        operation=lambda runtime, request_id: SpreadsheetEditToolResult(
            **edit_authorized_source_spreadsheet(
                mutation_policy=runtime.mutation_policy,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                sheets_adapter=runtime.sheets_adapter,
                reference_codec=_reference_codec(runtime),
                source_id=source_id,
                artifact_ref=artifact_ref,
                operations=operations,
                approval_reference=approval_reference,
            ).model_dump(),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_id=artifact_ref,
        resource_type="google_spreadsheet",
        approval_reference=ContentMutationPolicy.normalized_approval_reference(
            approval_reference
        ),
    )


@mcp_server.tool()
def validate_spreadsheet_structure(
    source_id: str,
    artifact_ref: str,
    requirements: SpreadsheetQualityRequirements,
    ctx: Context,
) -> SpreadsheetQualityToolResult:
    """Run bounded structural and value-presence checks on a native Google Sheet."""

    return _execute_tool(
        ctx=ctx,
        action="validate_spreadsheet_structure",
        operation=lambda runtime, request_id: SpreadsheetQualityToolResult(
            **validate_authorized_spreadsheet_structure(
                registry=runtime.registry,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                sheets_adapter=runtime.sheets_adapter,
                reference_codec=_reference_codec(runtime),
                source_id=source_id,
                artifact_ref=artifact_ref,
                requirements=requirements,
            ).model_dump(),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_id=artifact_ref,
        resource_type="google_spreadsheet_quality",
    )


@mcp_server.tool()
def create_source_artifact(
    source_id: str,
    name: str,
    type: Literal["document", "spreadsheet"],
    ctx: Context,
    approval_reference: str = "",
) -> SourceMutationToolResult:
    """Create one native Google Doc or Sheet at an approved source root."""

    return _execute_tool(
        ctx=ctx,
        action="create_source_artifact",
        operation=lambda runtime, request_id: SourceMutationToolResult(
            **create_authorized_source_artifact(
                mutation_policy=runtime.mutation_policy,
                workspace_adapter=runtime.workspace_adapter,
                source_id=source_id,
                name=name,
                artifact_type=type,
                approval_reference=approval_reference,
            ).model_dump(exclude={"request_id"}),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_type=f"google_{type}",
        result_resource_id=lambda result: result.artifact.id,
        approval_reference=ContentMutationPolicy.normalized_approval_reference(
            approval_reference
        ),
    )


@mcp_server.tool()
def update_source_artifact(
    source_id: str,
    change: str,
    ctx: Context,
    artifact_ref: str | None = None,
    document_id: str | None = None,
    approval_reference: str = "",
) -> SourceMutationToolResult:
    """Append bounded text to an authorized native Google Doc."""

    return _execute_tool(
        ctx=ctx,
        action="update_source_artifact",
        operation=lambda runtime, request_id: SourceMutationToolResult(
            **update_authorized_source_artifact(
                mutation_policy=runtime.mutation_policy,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                docs_adapter=runtime.docs_adapter,
                source_id=source_id,
                document_id=_resolved_artifact_id(
                    runtime,
                    source_id=source_id,
                    artifact_ref=artifact_ref,
                    legacy_id=document_id,
                ),
                change=change,
                approval_reference=approval_reference,
            ).model_dump(exclude={"request_id"}),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_id=artifact_ref or document_id,
        resource_type="google_document",
        approval_reference=ContentMutationPolicy.normalized_approval_reference(
            approval_reference
        ),
    )


@mcp_server.tool()
def move_source_artifact(
    source_id: str,
    ctx: Context,
    artifact_ref: str | None = None,
    artifact_id: str | None = None,
    destination_folder_id: str | None = None,
    approval_reference: str = "",
    destination_source_id: str | None = None,
) -> SourceMutationToolResult:
    """Move an artifact within its source or to an approved archive destination."""

    return _execute_tool(
        ctx=ctx,
        action="move_source_artifact",
        operation=lambda runtime, request_id: SourceMutationToolResult(
            **move_authorized_source_artifact(
                mutation_policy=runtime.mutation_policy,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                source_id=source_id,
                artifact_id=_resolved_artifact_id(
                    runtime,
                    source_id=source_id,
                    artifact_ref=artifact_ref,
                    legacy_id=artifact_id,
                ),
                destination_folder_id=destination_folder_id,
                destination_source_id=destination_source_id,
                approval_reference=approval_reference,
            ).model_dump(exclude={"request_id"}),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_id=artifact_ref or artifact_id,
        resource_type="google_drive_artifact",
        approval_reference=ContentMutationPolicy.normalized_approval_reference(
            approval_reference
        ),
        destination_source_id=destination_source_id,
    )


@mcp_server.tool()
def convert_source_artifact(
    source_id: str,
    target_type: ArtifactConversionTarget,
    ctx: Context,
    artifact_ref: str | None = None,
    artifact_id: str | None = None,
    approval_reference: str = "",
) -> ArtifactConversionToolResult:
    """Convert Office to Google native; opaque references are the preferred input."""

    def operation(runtime: KnowledgeRuntime, request_id: str) -> ArtifactConversionToolResult:
        codec = _reference_codec(runtime)
        if artifact_ref:
            resolved_id = codec.decode(artifact_ref, source_id=source_id)
        elif artifact_id:
            # Compatibility path for pre-v0.18 consumers. New discovery flows do not
            # expose Drive IDs and should always send artifact_ref.
            resolved_id = artifact_id
        else:
            raise WorkspaceAdapterError(
                "artifact_reference_required",
                "An artifact reference is required.",
                422,
            )
        converted = convert_authorized_source_artifact(
            mutation_policy=runtime.mutation_policy,
            source_policy=runtime.source_policy,
            workspace_adapter=runtime.workspace_adapter,
            source_id=source_id,
            artifact_id=resolved_id,
            target_type=target_type,
            approval_reference=approval_reference,
        )
        original_ref = artifact_ref or codec.encode(
            source_id=source_id, artifact_id=converted.original_artifact.id
        )
        return ArtifactConversionToolResult(
            original_artifact=ArtifactReference(
                artifact_ref=original_ref,
                name=converted.original_artifact.name,
                type=converted.original_artifact.type,
                source_id=source_id,
            ),
            created_artifact=ArtifactReference(
                artifact_ref=codec.encode(
                    source_id=source_id, artifact_id=converted.created_artifact.id
                ),
                name=converted.created_artifact.name,
                type=converted.created_artifact.type,
                source_id=source_id,
            ),
            created_artifact_type=converted.created_artifact_type.value,
            source_id=source_id,
            request_id=request_id,
        )

    return _execute_tool(
        ctx=ctx,
        action="convert_source_artifact",
        operation=operation,
        source_id=source_id,
        resource_id=artifact_ref,
        resource_type="office_artifact",
        created_resource_id=lambda result: result.created_artifact.artifact_ref,
        approval_reference=ContentMutationPolicy.normalized_approval_reference(
            approval_reference
        ),
    )


@mcp_server.tool()
def delete_source_artifact(
    source_id: str,
    ctx: Context,
    artifact_ref: str | None = None,
    artifact_id: str | None = None,
    approval_reference: str = "",
) -> SourceMutationToolResult:
    """Move an authorized source artifact to Drive trash."""

    return _execute_tool(
        ctx=ctx,
        action="delete_source_artifact",
        operation=lambda runtime, request_id: SourceMutationToolResult(
            **delete_authorized_source_artifact(
                mutation_policy=runtime.mutation_policy,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                source_id=source_id,
                artifact_id=_resolved_artifact_id(
                    runtime,
                    source_id=source_id,
                    artifact_ref=artifact_ref,
                    legacy_id=artifact_id,
                ),
                approval_reference=approval_reference,
            ).model_dump(exclude={"request_id"}),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_id=artifact_ref or artifact_id,
        resource_type="google_drive_artifact",
        approval_reference=ContentMutationPolicy.normalized_approval_reference(
            approval_reference
        ),
    )


@mcp_server.tool()
def share_source_artifact(
    source_id: str,
    audience: str,
    ctx: Context,
    artifact_ref: str | None = None,
    artifact_id: str | None = None,
    approval_reference: str = "",
) -> SourceMutationToolResult:
    """Grant reader access to one explicit audience for an authorized artifact."""

    return _execute_tool(
        ctx=ctx,
        action="share_source_artifact",
        operation=lambda runtime, request_id: SourceMutationToolResult(
            **share_authorized_source_artifact(
                mutation_policy=runtime.mutation_policy,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                source_id=source_id,
                artifact_id=_resolved_artifact_id(
                    runtime,
                    source_id=source_id,
                    artifact_ref=artifact_ref,
                    legacy_id=artifact_id,
                ),
                audience=audience,
                approval_reference=approval_reference,
            ).model_dump(exclude={"request_id"}),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_id=artifact_ref or artifact_id,
        resource_type="google_drive_artifact",
        approval_reference=ContentMutationPolicy.normalized_approval_reference(
            approval_reference
        ),
        audience=ContentMutationPolicy.normalized_audience(audience),
    )


@mcp_server.tool()
def list_source_documents(
    source_id: str,
    ctx: Context,
    query: str | None = None,
) -> DocumentListToolResult:
    """List authorized documents in one source, optionally filtering by name."""

    def operation(runtime: KnowledgeRuntime, request_id: str) -> DocumentListToolResult:
        _, files = list_authorized_source_files(
            registry=runtime.registry,
            source_policy=runtime.source_policy,
            workspace_adapter=runtime.workspace_adapter,
            source_id=source_id,
            limit=100,
        )
        documents = [item for item in files if item.type == "document"]
        if query and query.strip():
            normalized_query = query.strip().casefold()
            documents = [
                item for item in documents if normalized_query in item.name.casefold()
            ]
        return DocumentListToolResult(
            documents=documents[:MCP_DOCUMENT_RESULT_LIMIT],
            request_id=request_id,
        )

    return _execute_tool(
        ctx=ctx,
        action="list_source_documents",
        operation=operation,
        source_id=source_id,
        resource_type="google_drive",
    )


@mcp_server.tool()
def inspect_source_artifacts(
    source_id: str,
    ctx: Context,
) -> ArtifactInspectionToolResult:
    """Inspect safe native and Office artifact metadata in one approved source."""

    def operation(
        runtime: KnowledgeRuntime,
        request_id: str,
    ) -> ArtifactInspectionToolResult:
        _, artifacts = inspect_authorized_source_artifacts(
            registry=runtime.registry,
            source_policy=runtime.source_policy,
            workspace_adapter=runtime.workspace_adapter,
            source_id=source_id,
        )
        return ArtifactInspectionToolResult(
            artifacts=artifacts,
            request_id=request_id,
        )

    return _execute_tool(
        ctx=ctx,
        action="inspect_source_artifacts",
        operation=operation,
        source_id=source_id,
        resource_type="source_artifact_metadata",
    )


@mcp_server.tool()
def retrieve_document(
    source_id: str,
    ctx: Context,
    artifact_ref: str | None = None,
    document_id: str | None = None,
) -> GoogleDocContent:
    """Retrieve an authorized document that belongs to the selected source."""

    def operation(runtime: KnowledgeRuntime, request_id: str) -> GoogleDocContent:
        _, document = retrieve_authorized_document(
            registry=runtime.registry,
            source_policy=runtime.source_policy,
            workspace_adapter=runtime.workspace_adapter,
            docs_adapter=runtime.docs_adapter,
            source_id=source_id,
            document_id=_resolved_artifact_id(
                runtime,
                source_id=source_id,
                artifact_ref=artifact_ref,
                legacy_id=document_id,
            ),
        )
        return document.model_copy(update={"request_id": request_id})

    return _execute_tool(
        ctx=ctx,
        action="retrieve_document",
        operation=operation,
        source_id=source_id,
        resource_id=artifact_ref or document_id,
        resource_type="google_doc",
    )


@mcp_server.tool()
def retrieve_sheet_range(
    source_id: str,
    artifact_ref: str,
    sheet_ref: str,
    range: str,
    ctx: Context,
) -> SpreadsheetRangeToolResult:
    """Read a bounded local A1 range from an opaque spreadsheet sheet_ref."""

    def operation(
        runtime: KnowledgeRuntime, request_id: str
    ) -> SpreadsheetRangeToolResult:
        _, sheet = retrieve_authorized_sheet_range(
            registry=runtime.registry,
            source_policy=runtime.source_policy,
            workspace_adapter=runtime.workspace_adapter,
            sheets_adapter=runtime.sheets_adapter,
            reference_codec=_reference_codec(runtime),
            source_id=source_id,
            artifact_ref=artifact_ref,
            sheet_ref=sheet_ref,
            range_name=range,
        )
        return SpreadsheetRangeToolResult(
            artifact_ref=artifact_ref,
            sheet_ref=sheet_ref,
            range=sheet.range,
            values=sheet.values,
            request_id=request_id,
            source=sheet.source,
        )

    return _execute_tool(
        ctx=ctx,
        action="retrieve_sheet_range",
        operation=operation,
        source_id=source_id,
        resource_id=artifact_ref,
        resource_type="google_sheet",
    )


@mcp_server.tool()
def inspect_sheet_validation(
    source_id: str,
    artifact_ref: str,
    sheet_ref: str,
    range: str,
    ctx: Context,
) -> SheetValidationToolResult:
    """Inspect validation for a bounded local A1 cell/range. Resolve list domains through governed reads. Unresolved domains must never be guessed. This read grants no authority to change taxonomy or validation and is not a write concurrency token."""

    def operation(
        runtime: KnowledgeRuntime, request_id: str
    ) -> SheetValidationToolResult:
        _, result = inspect_authorized_sheet_validation(
            registry=runtime.registry,
            source_policy=runtime.source_policy,
            workspace_adapter=runtime.workspace_adapter,
            sheets_adapter=runtime.sheets_adapter,
            reference_codec=_reference_codec(runtime),
            source_id=source_id,
            artifact_ref=artifact_ref,
            sheet_ref=sheet_ref,
            range_name=range,
        )
        return SheetValidationToolResult(**result, request_id=request_id)

    return _execute_tool(
        ctx=ctx,
        action="inspect_sheet_validation",
        operation=operation,
        source_id=source_id,
        resource_id=artifact_ref,
        resource_type="google_sheet",
    )



async def _execute_hubspot_tool(
    *,
    ctx: Context,
    tool_name: str,
    arguments: dict[str, Any],
    approval_reference: str | None = None,
    explicit_intent: bool = False,
) -> HubSpotToolResult:
    request_id = _mcp_request_id(ctx)
    runtime = get_hubspot_runtime()
    decision = runtime.policy.classify(tool_name)
    try:
        result = await runtime.mcp_client.call_tool(
            tool_name,
            arguments,
            approval_reference=approval_reference,
            explicit_intent=explicit_intent,
        )
        if tool_name == "get_user_details":
            runtime.token_store.update_account(account_metadata_from_result(result))
        emit_audit_record(
            request_id=request_id,
            action="hubspot_tool_call",
            resource_id=None,
            resource_type="hubspot_mcp_tool",
            result="success",
            http_status=200,
            provider="hubspot",
            tool=tool_name,
            operation_classification=decision.classification,
            approval_reference=approval_reference,
        )
        return HubSpotToolResult(tool=tool_name, result=result, request_id=request_id)
    except WorkspaceAdapterError as error:
        emit_audit_record(
            request_id=request_id,
            action="hubspot_tool_call",
            resource_id=None,
            resource_type="hubspot_mcp_tool",
            result="rejected" if error.status_code < 500 else "error",
            http_status=error.status_code,
            error_code=error.code,
            provider="hubspot",
            tool=tool_name,
            operation_classification=decision.classification,
            approval_reference=approval_reference,
        )
        raise RuntimeError(f"{error.code}: {error.message}") from error
    except Exception:
        emit_audit_record(
            request_id=request_id,
            action="hubspot_tool_call",
            resource_id=None,
            resource_type="hubspot_mcp_tool",
            result="error",
            http_status=500,
            error_code="unhandled_error",
            provider="hubspot",
            tool=tool_name,
            operation_classification=decision.classification,
            approval_reference=approval_reference,
        )
        raise


def _agent_signal_inbox(runtime: KnowledgeRuntime):
    if runtime.agent_signal_inbox is None:
        raise WorkspaceAdapterError(
            "agent_signal_inbox_unavailable",
            "Agent Signal Inbox is not configured.",
            503,
        )
    return runtime.agent_signal_inbox


def _authorized_signal_type_filter(requested: str | None) -> str | None:
    principal = active_principal()
    if principal.type != "signal_worker":
        return requested
    if requested is not None and requested not in principal.signal_types:
        raise WorkspaceAdapterError(
            "signal_type_denied", "The requested Signal type is not authorized.", 403
        )
    # Signal worker records are deliberately constrained to one allowlisted type.
    return next(iter(principal.signal_types))


def _authorized_signal(runtime: KnowledgeRuntime, signal_id: str) -> AgentSignalRecord:
    signal = _agent_signal_inbox(runtime).get(signal_id)
    principal = active_principal()
    if principal.type == "signal_worker" and signal.signal_type not in principal.signal_types:
        raise WorkspaceAdapterError(
            "signal_type_denied", "The requested Signal type is not authorized.", 403
        )
    return signal


def _claim_authorized_signal(runtime: KnowledgeRuntime, signal_id: str) -> AgentSignalRecord:
    _authorized_signal(runtime, signal_id)
    return _agent_signal_inbox(runtime).claim(
        signal_id, principal_id=active_principal().id
    )


def _release_authorized_signal(runtime: KnowledgeRuntime, signal_id: str) -> AgentSignalRecord:
    _authorized_signal(runtime, signal_id)
    return _agent_signal_inbox(runtime).release(
        signal_id, principal_id=active_principal().id
    )


def _complete_authorized_signal(
    runtime: KnowledgeRuntime,
    signal_id: str,
    *,
    completion_summary: str,
    outcome_metadata: dict[str, Any] | None,
) -> AgentSignalRecord:
    _authorized_signal(runtime, signal_id)
    return _agent_signal_inbox(runtime).complete(
        signal_id,
        completion_summary=completion_summary,
        outcome_metadata=outcome_metadata,
        principal_id=active_principal().id,
    )


def _dismiss_authorized_signal(
    runtime: KnowledgeRuntime, signal_id: str, *, reason: str
) -> AgentSignalRecord:
    _authorized_signal(runtime, signal_id)
    return _agent_signal_inbox(runtime).dismiss(
        signal_id, reason=reason, principal_id=active_principal().id
    )


def _execute_agent_signal_tool(
    *,
    ctx: Context,
    action: str,
    operation: Callable[[KnowledgeRuntime, str], T],
    signal_id: str | None = None,
    status_transition: str | None = None,
) -> T:
    request_id = _mcp_request_id(ctx)
    try:
        result = operation(get_runtime_gateway(), request_id)
        signal = getattr(result, "signal", None)
        emit_audit_record(
            request_id=request_id,
            action=action,
            resource_id=signal_id,
            resource_type="agent_signal",
            result="success",
            http_status=200,
            provider="agent_signals",
            tool=action,
            signal_id=signal_id,
            signal_type=getattr(signal, "signal_type", None),
            status_transition=status_transition,
        )
        return result
    except WorkspaceAdapterError as error:
        emit_audit_record(
            request_id=request_id,
            action=action,
            resource_id=signal_id,
            resource_type="agent_signal",
            result="rejected" if error.status_code < 500 else "error",
            http_status=error.status_code,
            error_code=error.code,
            provider="agent_signals",
            tool=action,
            signal_id=signal_id,
            status_transition=status_transition,
        )
        raise RuntimeError(f"{error.code}: {error.message}") from error


@mcp_server.tool()
def list_agent_signals(
    ctx: Context,
    status: SignalStatus | None = SignalStatus.PENDING,
    priority: SignalPriority | None = None,
    signal_type: str | None = None,
    source: str | None = None,
    limit: int = 25,
) -> AgentSignalListToolResult:
    """List Agent Signal Inbox items, urgent first and oldest first."""

    return _execute_agent_signal_tool(
        ctx=ctx,
        action="list_agent_signals",
        operation=lambda runtime, request_id: AgentSignalListToolResult(
            signals=_agent_signal_inbox(runtime).list(
                status=status,
                priority=priority,
                signal_type=_authorized_signal_type_filter(signal_type),
                source=source,
                limit=limit,
            ),
            request_id=request_id,
        ),
    )


@mcp_server.tool()
def get_agent_signal(signal_id: str, ctx: Context) -> AgentSignalToolResult:
    """Get one Agent Signal by its durable signal_id."""

    return _execute_agent_signal_tool(
        ctx=ctx,
        action="get_agent_signal",
        signal_id=signal_id,
        operation=lambda runtime, request_id: AgentSignalToolResult(
            signal=_authorized_signal(runtime, signal_id),
            request_id=request_id,
        ),
    )


@mcp_server.tool()
def claim_agent_signal(signal_id: str, ctx: Context) -> AgentSignalToolResult:
    """Atomically claim a pending Agent Signal for the current management principal."""

    return _execute_agent_signal_tool(
        ctx=ctx,
        action="agent_signal_claimed",
        signal_id=signal_id,
        status_transition="pending->claimed",
        operation=lambda runtime, request_id: AgentSignalToolResult(
            signal=_claim_authorized_signal(runtime, signal_id),
            request_id=request_id,
        ),
    )


@mcp_server.tool()
def complete_agent_signal(
    signal_id: str,
    completion_summary: str,
    ctx: Context,
    outcome_metadata: dict[str, Any] | None = None,
) -> AgentSignalToolResult:
    """Complete a claimed Agent Signal with a brief safe outcome summary."""

    return _execute_agent_signal_tool(
        ctx=ctx,
        action="agent_signal_completed",
        signal_id=signal_id,
        status_transition="claimed->completed",
        operation=lambda runtime, request_id: AgentSignalToolResult(
            signal=_complete_authorized_signal(
                runtime,
                signal_id,
                completion_summary=completion_summary,
                outcome_metadata=outcome_metadata,
            ),
            request_id=request_id,
        ),
    )


@mcp_server.tool()
def dismiss_agent_signal(
    signal_id: str, reason: str, ctx: Context
) -> AgentSignalToolResult:
    """Dismiss a pending or claimed Agent Signal with a brief reason."""

    return _execute_agent_signal_tool(
        ctx=ctx,
        action="agent_signal_dismissed",
        signal_id=signal_id,
        status_transition="pending|claimed->dismissed",
        operation=lambda runtime, request_id: AgentSignalToolResult(
            signal=_dismiss_authorized_signal(runtime, signal_id, reason=reason),
            request_id=request_id,
        ),
    )


@mcp_server.tool()
def release_agent_signal(signal_id: str, ctx: Context) -> AgentSignalToolResult:
    """Release a claimed Agent Signal back to pending."""

    return _execute_agent_signal_tool(
        ctx=ctx,
        action="agent_signal_released",
        signal_id=signal_id,
        status_transition="claimed->pending",
        operation=lambda runtime, request_id: AgentSignalToolResult(
            signal=_release_authorized_signal(runtime, signal_id),
            request_id=request_id,
        ),
    )


@mcp_server.tool()
def agent_signal_status(ctx: Context) -> AgentSignalStatusResult:
    """Return safe Agent Signal Inbox health and counts without signal content."""

    request_id = _mcp_request_id(ctx)
    runtime: KnowledgeRuntime | None = None
    push_configured = bool(
        os.getenv("AGENT_SIGNAL_PUSH_AUDIENCE", "").strip()
        and os.getenv("AGENT_SIGNAL_PUSH_SERVICE_ACCOUNT", "").strip()
    )
    try:
        runtime = get_runtime_gateway()
        result = _agent_signal_inbox(runtime).status(push_configured=push_configured)
        result.request_id = request_id
        available = True
    except Exception:
        configured = bool(
            getattr(getattr(runtime, "settings", None), "agent_signal_bucket", "")
        )
        result = AgentSignalStatusResult(
            configured=configured,
            pubsub_push_configured=push_configured,
            inbox_available=False,
            pending_count=0,
            urgent_count=0,
            claimed_count=0,
            request_id=request_id,
        )
        available = False
    emit_audit_record(
        request_id=request_id,
        action="agent_signal_status",
        resource_id=None,
        resource_type="agent_signal_status",
        result="success" if available else "error",
        http_status=200 if available else 503,
        error_code=None if available else "agent_signal_inbox_unavailable",
        provider="agent_signals",
        tool="agent_signal_status",
    )
    return result


@mcp_server.tool()
def get_agent_signal_operation_history(
    ctx: Context,
    signal_id: str | None = None,
    operation: AgentSignalOperation | None = None,
    limit: int = 10,
) -> AgentSignalOperationHistoryToolResult:
    """List content-free Agent Signal lifecycle audit metadata."""

    if limit < 1 or limit > 50:
        raise RuntimeError(
            "operation_history_limit_invalid: Operation history limit must be between 1 and 50."
        )
    if signal_id is not None and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", signal_id
    ):
        raise RuntimeError("agent_signal_id_invalid: Agent Signal ID is invalid.")

    def query(
        runtime: KnowledgeRuntime, request_id: str
    ) -> AgentSignalOperationHistoryToolResult:
        store = runtime.operation_history_store
        if store is None or not hasattr(store, "list_agent_signals"):
            raise WorkspaceAdapterError(
                "operation_history_unavailable",
                "Agent Signal operation history is unavailable.",
                503,
            )
        return AgentSignalOperationHistoryToolResult(
            operations=store.list_agent_signals(
                signal_id=signal_id, operation=operation, limit=limit
            ),
            request_id=request_id,
        )

    return _execute_agent_signal_tool(
        ctx=ctx,
        action="get_agent_signal_operation_history",
        signal_id=signal_id,
        operation=query,
    )


@mcp_server.tool()
async def hubspot_list_tools(ctx: Context) -> HubSpotToolListResult:
    """List live HubSpot tools with Brunova's safe governance classification."""

    request_id = _mcp_request_id(ctx)
    try:
        tools = await get_hubspot_runtime().mcp_client.list_tools()
        emit_audit_record(
            request_id=request_id,
            action="hubspot_list_tools",
            resource_id=None,
            resource_type="hubspot_mcp_tool_catalog",
            result="success",
            http_status=200,
            provider="hubspot",
            operation_classification="read",
        )
        return HubSpotToolListResult(tools=tools, request_id=request_id)
    except WorkspaceAdapterError as error:
        emit_audit_record(
            request_id=request_id,
            action="hubspot_list_tools",
            resource_id=None,
            resource_type="hubspot_mcp_tool_catalog",
            result="error",
            http_status=error.status_code,
            error_code=error.code,
            provider="hubspot",
            operation_classification="read",
        )
        raise RuntimeError(f"{error.code}: {error.message}") from error


def _authorized_n8n_instance(instance_id: str, tool_name: str | None = None) -> None:
    """Bind a request to one enrolled instance before touching its secret."""

    principal = active_principal()
    allowed = (
        principal.allows_n8n_tool(instance_id, tool_name)
        if tool_name is not None else principal.allows_n8n_instance(instance_id)
    )
    if not allowed:
        raise ToolError("n8n_instance_denied")
    if instance_id not in N8NInstanceRegistry.from_environment().secret_variables:
        raise ToolError("n8n_instance_not_found")


@mcp_server.tool()
async def n8n_instances(ctx: Context) -> dict[str, Any]:
    """List only the n8n instances authorized for this caller; no endpoints or secrets."""

    principal = active_principal()
    instances = [
        instance_id for instance_id in N8NInstanceRegistry.from_environment().ids()
        if principal.allows_n8n_instance(instance_id)
    ]
    return {"instances": instances, "request_id": _mcp_request_id(ctx)}


@mcp_server.tool()
async def n8n_instance_status(instance_id: str, ctx: Context) -> dict[str, Any]:
    """Return safe live status for one authorized n8n instance."""

    _authorized_n8n_instance(instance_id)
    request_id = _mcp_request_id(ctx)
    try:
        status = await get_n8n_client(instance_id).status()
        result = status.model_dump()
    except ValueError:
        result = {"configured": False, "connected": False, "mcp_initialized": False, "tool_count": 0}
    emit_audit_record(
        request_id=request_id, action="n8n_instance_status", resource_id=instance_id,
        resource_type="n8n_instance", result="success" if result["connected"] else "error",
        http_status=200 if result["connected"] else 503, provider="n8n",
        operation_classification="read",
    )
    return {"instance_id": instance_id, **result, "request_id": request_id}


@mcp_server.tool()
async def n8n_instance_list_tools(instance_id: str, ctx: Context) -> dict[str, Any]:
    """Discover only tools allowed for this caller on one n8n instance."""

    _authorized_n8n_instance(instance_id)
    principal = active_principal()
    tools = await get_n8n_client(instance_id).list_tools(force_refresh=True)
    visible = [tool.model_dump() for tool in tools if principal.allows_n8n_tool(instance_id, tool.name)]
    return {"instance_id": instance_id, "tools": visible, "request_id": _mcp_request_id(ctx)}


@mcp_server.tool()
async def n8n_instance_call_tool(
    instance_id: str, tool_name: str, arguments: dict[str, Any], ctx: Context,
) -> N8NToolResult:
    """Call an exact allowlisted tool on one explicitly selected n8n instance."""

    _authorized_n8n_instance(instance_id, tool_name)
    request_id = _mcp_request_id(ctx)
    started = time.monotonic()
    try:
        result = await get_n8n_client(instance_id).call_tool(tool_name, arguments)
        failed = bool(result.get("isError", False)) if isinstance(result, dict) else False
        emit_audit_record(
            request_id=request_id, action="n8n_instance_tool_call", resource_id=instance_id,
            resource_type="n8n_instance", result="error" if failed else "success",
            http_status=502 if failed else 200, provider="n8n", tool=tool_name,
            approval_reference=_approval_reference(ctx),
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        return N8NToolResult(tool=tool_name, result=result, request_id=request_id)
    except WorkspaceAdapterError as error:
        emit_audit_record(
            request_id=request_id, action="n8n_instance_tool_call", resource_id=instance_id,
            resource_type="n8n_instance", result="error", http_status=error.status_code,
            error_code=error.code, provider="n8n", tool=tool_name,
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        raise ToolError(error.code) from error


@mcp_server.tool()
async def n8n_status(ctx: Context) -> N8NStatusResult:
    """Return safe n8n MCP connectivity and live catalog status."""

    request_id = _mcp_request_id(ctx)
    started = time.monotonic()
    try:
        status = await get_n8n_client().status()
    except ValueError:
        result = N8NStatusResult(
            configured=False, connected=False, mcp_initialized=False,
            tool_count=0, request_id=request_id,
        )
    else:
        result = N8NStatusResult(**status.model_dump(), request_id=request_id)
    emit_audit_record(
        request_id=request_id, action="n8n_status", resource_id=None,
        resource_type="n8n_mcp_status",
        result="success" if result.connected else "error",
        http_status=200 if result.connected else 503, provider="n8n",
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    return result


@mcp_server.tool()
async def n8n_list_tools(ctx: Context) -> N8NToolListResult:
    """List the current unfiltered n8n MCP catalog from live discovery."""

    request_id = _mcp_request_id(ctx)
    started = time.monotonic()
    try:
        tools = await get_n8n_client().list_tools(force_refresh=True)
        emit_audit_record(
            request_id=request_id, action="n8n_list_tools", resource_id=None,
            resource_type="n8n_mcp_tool_catalog", result="success", http_status=200,
            provider="n8n", duration_ms=round((time.monotonic() - started) * 1000),
        )
        return N8NToolListResult(tools=tools, request_id=request_id)
    except (ValueError, WorkspaceAdapterError) as error:
        code = error.code if isinstance(error, WorkspaceAdapterError) else "n8n_not_configured"
        status = error.status_code if isinstance(error, WorkspaceAdapterError) else 503
        emit_audit_record(
            request_id=request_id, action="n8n_list_tools", resource_id=None,
            resource_type="n8n_mcp_tool_catalog", result="error", http_status=status,
            error_code=code, provider="n8n",
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        raise RuntimeError(f"{code}: n8n MCP catalog is unavailable") from error


@mcp_server.tool()
async def openwa_status(ctx: Context) -> OpenWAStatusResult:
    """Return safe OpenWA MCP connectivity, mode, and catalog status."""

    request_id = _mcp_request_id(ctx)
    started = time.monotonic()
    try:
        status = await get_openwa_client().status()
    except ValueError:
        result = OpenWAStatusResult(
            configured=False,
            connected=False,
            mcp_initialized=False,
            tool_count=0,
            request_id=request_id,
        )
    else:
        result = OpenWAStatusResult(**status.model_dump(), request_id=request_id)
    emit_audit_record(
        request_id=request_id,
        action="openwa_status",
        resource_id=None,
        resource_type="openwa_mcp_status",
        result="success" if result.connected else "error",
        http_status=200 if result.connected else 503,
        provider="openwa",
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    return result


@mcp_server.tool()
async def openwa_list_tools(ctx: Context) -> OpenWAToolListResult:
    """List the current OpenWA-curated MCP catalog from live discovery."""

    request_id = _mcp_request_id(ctx)
    started = time.monotonic()
    try:
        tools = await get_openwa_client().list_tools(force_refresh=True)
        emit_audit_record(
            request_id=request_id,
            action="openwa_list_tools",
            resource_id=None,
            resource_type="openwa_mcp_tool_catalog",
            result="success",
            http_status=200,
            provider="openwa",
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        return OpenWAToolListResult(tools=tools, request_id=request_id)
    except (ValueError, WorkspaceAdapterError) as error:
        code = (
            error.code
            if isinstance(error, WorkspaceAdapterError)
            else "openwa_not_configured"
        )
        status = error.status_code if isinstance(error, WorkspaceAdapterError) else 503
        emit_audit_record(
            request_id=request_id,
            action="openwa_list_tools",
            resource_id=None,
            resource_type="openwa_mcp_tool_catalog",
            result="error",
            http_status=status,
            error_code=code,
            provider="openwa",
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        raise RuntimeError(f"{code}: OpenWA MCP catalog is unavailable") from error


@mcp_server.tool()
async def hubspot_get_user_details(ctx: Context) -> HubSpotToolResult:
    """Return HubSpot account, user, permission, and capability details."""

    return await _execute_hubspot_tool(ctx=ctx, tool_name="get_user_details", arguments={})


@mcp_server.tool()
async def hubspot_search_crm_objects(
    arguments: dict[str, Any], ctx: Context
) -> HubSpotToolResult:
    """Search HubSpot CRM objects using the official downstream tool arguments."""

    return await _execute_hubspot_tool(
        ctx=ctx, tool_name="search_crm_objects", arguments=arguments
    )


@mcp_server.tool()
async def hubspot_get_crm_objects(
    arguments: dict[str, Any], ctx: Context
) -> HubSpotToolResult:
    """Fetch HubSpot CRM objects using the official downstream tool arguments."""

    return await _execute_hubspot_tool(
        ctx=ctx, tool_name="get_crm_objects", arguments=arguments
    )


@mcp_server.tool()
async def hubspot_search_properties(
    arguments: dict[str, Any], ctx: Context
) -> HubSpotToolResult:
    """Search HubSpot CRM property definitions."""

    return await _execute_hubspot_tool(
        ctx=ctx, tool_name="search_properties", arguments=arguments
    )


@mcp_server.tool()
async def hubspot_get_properties(
    arguments: dict[str, Any], ctx: Context
) -> HubSpotToolResult:
    """Fetch full HubSpot property definitions."""

    return await _execute_hubspot_tool(
        ctx=ctx, tool_name="get_properties", arguments=arguments
    )


@mcp_server.tool()
async def hubspot_search_owners(
    arguments: dict[str, Any], ctx: Context
) -> HubSpotToolResult:
    """Search HubSpot CRM record owners."""

    return await _execute_hubspot_tool(
        ctx=ctx, tool_name="search_owners", arguments=arguments
    )


@mcp_server.tool()
async def hubspot_call_read_tool(
    tool_name: str,
    arguments: dict[str, Any],
    ctx: Context,
) -> HubSpotToolResult:
    """Call a live HubSpot tool only when it is explicitly classified read-only."""

    return await _execute_hubspot_tool(
        ctx=ctx,
        tool_name=tool_name,
        arguments=arguments,
    )


@mcp_server.tool()
async def hubspot_manage_crm_objects(
    arguments: dict[str, Any],
    explicit_intent: bool,
    ctx: Context,
) -> HubSpotToolResult:
    """Create or update HubSpot CRM data when mutation intent is explicit."""

    return await _execute_hubspot_tool(
        ctx=ctx,
        tool_name="manage_crm_objects",
        arguments=arguments,
        explicit_intent=explicit_intent,
    )


def _csv_environment(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=_csv_environment(
        "MCP_ALLOWED_HOSTS",
        "localhost:*,127.0.0.1:*",
    ),
    allowed_origins=_csv_environment("MCP_ALLOWED_ORIGINS"),
)

register_acquisition_tools(mcp_server)

mcp_http_app = mcp_server.streamable_http_app(
    streamable_http_path="/",
    stateless_http=True,
    json_response=True,
    transport_security=transport_security,
    host="0.0.0.0",
)
