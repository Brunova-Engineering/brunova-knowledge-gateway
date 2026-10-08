"""Structured operational audit events without Workspace content."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import Request

SERVICE_NAME = "brunova-knowledge-gateway"
CORRELATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

audit_logger = logging.getLogger("brunova.audit")
if not audit_logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    audit_logger.addHandler(handler)
audit_logger.setLevel(logging.INFO)
audit_logger.propagate = False


def correlation_id(incoming: str | None) -> str:
    if incoming and CORRELATION_ID_PATTERN.fullmatch(incoming):
        return incoming
    return str(uuid4())


def request_audit_context(request: Request) -> tuple[str | None, str | None, str | None]:
    path = request.url.path
    # Agent Signal receipt emits a purpose-built record inside the endpoint so
    # invalid payloads can be acknowledged without being mislabeled successful.
    if path == "/events/agent-signals":
        return None, None, None
    if path == "/auth/hubspot/connect":
        return "hubspot_oauth_connect", None, "hubspot_connection"
    if path == "/auth/hubspot/callback":
        return "hubspot_oauth_callback", None, "hubspot_connection"
    if path == "/auth/hubspot/status":
        return "hubspot_connection_status", None, "hubspot_connection"
    if path == "/workspace/drive/list":
        return "list_files", None, "google_drive"
    if path == "/sources/discover":
        return "discover_source_candidates", None, "source_discovery"
    if path.startswith("/sources/") and path.endswith("/files"):
        return "list_files", None, "google_drive"
    if path.startswith("/sources/") and "/docs/" in path:
        return "read_document", request.path_params.get("document_id"), "google_doc"
    if path.startswith("/sources/") and "/sheets/" in path:
        return (
            "read_sheet_range",
            request.path_params.get("spreadsheet_id"),
            "google_sheet",
        )
    if path.startswith("/workspace/docs/"):
        return "read_document", request.path_params.get("document_id"), "google_doc"
    if path.startswith("/workspace/sheets/"):
        return (
            "read_sheet_range",
            request.path_params.get("spreadsheet_id"),
            "google_sheet",
        )
    return None, None, None


def emit_audit_event(
    request: Request,
    *,
    action: str,
    resource_id: str | None,
    resource_type: str | None,
    result: str,
    http_status: int,
    error_code: str | None = None,
    error_reason: str | None = None,
) -> None:
    emit_audit_record(
        request_id=request.state.request_id,
        action=action,
        resource_id=resource_id,
        resource_type=resource_type,
        result=result,
        http_status=http_status,
        error_code=error_code,
        error_reason=error_reason,
        source_id=getattr(request.state, "source_id", None),
        source_classification=getattr(request.state, "classification", None),
        candidate_count=getattr(request.state, "candidate_count", None),
        provider=(
            "hubspot"
            if resource_type and resource_type.startswith("hubspot")
            else "workspace"
            if resource_type
            and (
                resource_type.startswith("google_")
                or resource_type.startswith("source_")
            )
            else "gateway"
        ),
        principal_id=getattr(getattr(request.state, "principal", None), "id", None),
        principal_type=getattr(
            getattr(request.state, "principal", None), "type", None
        ),
    )


def emit_audit_record(
    *,
    request_id: str,
    action: str,
    resource_id: str | None,
    resource_type: str | None,
    result: str,
    http_status: int,
    error_code: str | None = None,
    error_reason: str | None = None,
    source_id: str | list[str] | None = None,
    source_classification: str | list[str] | None = None,
    consumer: str | None = None,
    candidate_count: int | None = None,
    proposal_id: str | None = None,
    approval_reference: str | None = None,
    audience: str | None = None,
    created_resource_id: str | None = None,
    destination_source_id: str | None = None,
    provider: str | None = None,
    operation_classification: str | None = None,
    tool: str | None = None,
    capability: str | None = None,
    authorization_mode: str | None = None,
    duration_ms: int | None = None,
    signal_id: str | None = None,
    signal_type: str | None = None,
    status_transition: str | None = None,
    principal_id: str | None = None,
    principal_type: str | None = None,
    include_active_principal: bool = True,
    revision_id: str | None = None,
    artifact_version: str | None = None,
    asset_refs: list[str] | None = None,
    asset_transformations: list[dict[str, Any]] | None = None,
) -> None:
    if os.getenv("WORKSPACE_AUDIT_ENABLED", "true").strip().lower() not in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return
    if include_active_principal and (principal_id is None or principal_type is None):
        # Lazy import avoids coupling audit initialization to auth configuration.
        from app.auth.principals import active_principal

        principal = active_principal()
        principal_id = principal_id or principal.id
        principal_type = principal_type or principal.type
    service_account = os.getenv("WORKSPACE_SERVICE_ACCOUNT_EMAIL", "")
    event: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "service": SERVICE_NAME,
        "actor": service_account.split("@", 1)[0],
        "delegated_user": os.getenv("WORKSPACE_DELEGATED_USER", ""),
        "action": action,
        "resource_id": resource_id,
        "resource_type": resource_type,
        "result": result,
        "http_status": http_status,
        "request_id": request_id,
        "correlation_id": request_id,
        "source_id": source_id,
        "classification": source_classification,
        "source_classification": source_classification,
    }
    if consumer:
        event["consumer"] = consumer
    if candidate_count is not None:
        event["candidate_count"] = candidate_count
    if proposal_id:
        event["proposal_id"] = proposal_id
    if approval_reference:
        event["approval_reference"] = approval_reference
    if revision_id:
        event["revision_id"] = revision_id[:256]
    if artifact_version:
        event["artifact_version"] = artifact_version[:128]
    if asset_refs:
        event["asset_ref_hashes"] = [
            hashlib.sha256(value.encode()).hexdigest()[:16]
            for value in asset_refs[:10]
        ]
    if asset_transformations:
        event["asset_transformations"] = [
            {
                "asset_ref_hash": hashlib.sha256(str(item["asset_ref"]).encode()).hexdigest()[:16],
                "original_mime_type": str(item["original_mime_type"])[:64],
                "original_dimensions": [
                    item.get("original_width_pixels"),
                    item.get("original_height_pixels"),
                ],
                "derived_mime_type": str(item["derived_mime_type"])[:64],
                "derived_dimensions": [
                    int(item["derived_width_pixels"]),
                    int(item["derived_height_pixels"]),
                ],
                "transformation": str(item["transformation"])[:32],
            }
            for item in asset_transformations[:10]
        ]
    if audience:
        event["audience"] = audience
    if created_resource_id:
        event["created_resource_id"] = created_resource_id
    if destination_source_id:
        event["destination_source_id"] = destination_source_id
    if provider:
        event["provider"] = provider
    if operation_classification:
        event["operation_classification"] = operation_classification
    if tool:
        event["tool"] = tool
    if capability:
        event["capability"] = capability
    if authorization_mode:
        event["authorization_mode"] = authorization_mode
    if duration_ms is not None:
        event["duration_ms"] = duration_ms
    if signal_id:
        event["signal_id"] = signal_id
    if signal_type:
        event["signal_type"] = signal_type
    if status_transition:
        event["status_transition"] = status_transition
    if principal_id:
        event["principal_id"] = principal_id
    if principal_type:
        event["principal_type"] = principal_type
    if error_code:
        event["error_code"] = error_code
    if error_reason:
        event["error_reason"] = error_reason
    audit_logger.info(json.dumps(event, separators=(",", ":"), sort_keys=True))
