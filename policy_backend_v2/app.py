"""Dedicated Panky 2.0 service; legacy v1 endpoints remain untouched."""

from __future__ import annotations

import os
from functools import lru_cache
from uuid import UUID

import psycopg
import yaml
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from policy_backend.service_auth import (require_core_workflow_v2,
                                         require_core_governance_workflow, require_gateway)
from .decision import Request
from .models import Policy
from .publication import activate, stable
from .store import PolicyStoreV2
from .governance import GovernanceStore


app = FastAPI(title="Panky 2.0 policy backend", docs_url=None, redoc_url=None)


class ActorProof(BaseModel):
    issuer: str
    subject: str = Field(min_length=1)
    email: str = Field(min_length=3)


class ExternalRequest(BaseModel):
    actor: ActorProof
    provider: str
    tool: str
    resource_kind: str
    resource_ref: str
    parent_source: str | None = None
    parent_resource_kind: str = "source"
    source_alias: str | None = None
    operation: str
    request_id: str = Field(min_length=1)
    preview: bool = False


class ContextRequest(BaseModel):
    actor: ActorProof
    node_id: str
    surface: str
    operation: str
    request_id: str = Field(min_length=1)
    preview: bool = False


class OrgActionRequest(BaseModel):
    actor: ActorProof
    action: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    preview: bool = False


class Completion(BaseModel):
    reservation_id: UUID
    request_id: str = Field(min_length=1)
    status: str


class Publication(BaseModel):
    core_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    policy: Policy
    registry_yaml: str = Field(min_length=1, max_length=500_000)
    topology_yaml: str = Field(min_length=1, max_length=500_000)
    seats_yaml: str = Field(min_length=1, max_length=500_000)


class ShareRequest(BaseModel):
    actor: ActorProof
    resource_id: str
    target_department_id: str
    via_seat_id: str
    reason: str = Field(min_length=3, max_length=1000)


class ShareDecision(BaseModel):
    actor: ActorProof
    request_id: UUID
    via_seat_id: str
    accept: bool


class FounderTransferRequest(BaseModel):
    actor: ActorProof
    target_actor_id: str = Field(min_length=1)
    deadline_hours: int = Field(default=72, ge=1, le=168)


class FounderTransferDecision(BaseModel):
    actor: ActorProof
    transfer_id: UUID
    accept: bool


class SuperAdminChange(BaseModel):
    actor: ActorProof
    target_actor_id: str = Field(min_length=1)
    action: str
    reason: str = Field(min_length=3, max_length=1000)


class ResourceGrantRequest(BaseModel):
    actor: ActorProof
    resource_id: str
    recipient_seat_id: str
    via_seat_id: str
    operation: str = Field(min_length=1)
    reason: str = Field(min_length=3, max_length=1000)


class ToolRegistrationRequest(BaseModel):
    actor: ActorProof
    provider: str = Field(min_length=1, max_length=64)
    operations: list[str] = Field(min_length=1, max_length=16)
    via_seat_id: str
    reason: str = Field(min_length=3, max_length=1000)


class ToolRetirementRequest(BaseModel):
    actor: ActorProof
    provider: str = Field(min_length=1, max_length=64)
    via_seat_id: str
    reason: str = Field(min_length=3, max_length=1000)


class ResourceRetirementRequest(BaseModel):
    actor: ActorProof
    resource_id: str
    via_seat_id: str
    reason: str = Field(min_length=3, max_length=1000)


class EntityRetirementRequest(BaseModel):
    actor: ActorProof
    entity_kind: str
    entity_code: str
    via_seat_id: str
    reason: str = Field(min_length=3, max_length=1000)


class ResourceSharingQuery(BaseModel):
    actor: ActorProof
    resource_id: str
    via_seat_id: str


@lru_cache
def store() -> PolicyStoreV2:
    dsn = os.getenv("PANKY_POLICY_V2_DSN", "")
    if not dsn:
        raise RuntimeError("Panky 2.0 policy database is not configured")
    return PolicyStoreV2(dsn)


def _reference(kind: str, value: str) -> UUID:
    if not value or len(value) > 128:
        raise ValueError("invalid_reference")
    try:
        return UUID(value)
    except ValueError:
        return stable(kind, value)


@app.get("/v2/health")
def health(database: PolicyStoreV2 = Depends(store)) -> dict:
    try:
        if not database.healthy():
            raise HTTPException(503, "Policy has no active release")
        return {"status": "ready", "model": "2.0"}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "Policy database is unavailable") from None


@app.post("/v2/resources/visible")
def visible_resources(item: ActorProof, _: None = Depends(require_gateway),
                      database: PolicyStoreV2 = Depends(store)) -> dict:
    """Registered resource references scoped by the active policy decision."""
    try:
        return database.visible_resources(issuer=item.issuer, subject=item.subject,
                                          email=item.email)
    except Exception:
        raise HTTPException(503, "Resource inventory is unavailable") from None


def _decide_external(item: ExternalRequest, database: PolicyStoreV2) -> dict:
    with psycopg.connect(database.dsn, connect_timeout=3) as conn:
        tool = conn.execute(
            "SELECT tool_id FROM panky_tools WHERE provider=%s AND code=%s AND status='active'",
            (item.provider, item.provider),
        ).fetchone()
        if tool is None:
            return {"allowed": False, "reason": "tool_unknown"}
        row = conn.execute(
            "SELECT r.resource_id FROM panky_resources r "
            "WHERE r.tool_id=%s AND r.status='active' AND ((r.resource_kind=%s AND r.external_ref=%s) "
            "OR (r.resource_kind=%s AND r.external_ref=%s) "
            "OR (r.resource_kind='source' AND r.external_ref=%s) "
            "OR (r.resource_kind='source' AND r.external_ref=%s)) "
            "ORDER BY CASE WHEN r.resource_kind=%s AND r.external_ref=%s THEN 0 "
            "WHEN r.resource_kind=%s AND r.external_ref=%s THEN 1 ELSE 2 END "
            "LIMIT 1",
            # Tool rows model connected systems; item.tool is a concrete
            # Gateway command already checked by the Gateway catalog.
            (tool[0], item.resource_kind, item.resource_ref,
             item.parent_resource_kind, item.parent_source,
             item.parent_source, item.source_alias,
             item.resource_kind, item.resource_ref,
             item.parent_resource_kind, item.parent_source),
        ).fetchone()
        if conn.execute(
            "SELECT 1 FROM panky_resources d WHERE d.tool_id=%s AND d.status='disabled' "
            "AND ((d.resource_kind=%s AND d.external_ref=%s) "
            "OR (d.resource_kind='source' AND d.external_ref=%s) "
            "OR (d.resource_kind='source' AND d.external_ref=%s)) "
            "AND NOT EXISTS (SELECT 1 FROM panky_resources a WHERE a.tool_id=d.tool_id "
            "AND a.resource_kind=d.resource_kind AND a.external_ref=d.external_ref "
            "AND a.status='active')",
            (tool[0], item.resource_kind, item.resource_ref,
             item.parent_source, item.source_alias),
        ).fetchone():
            return {"allowed": False, "reason": "resource_disabled"}
    actor_id = database.actor_for_oidc(item.actor.issuer, item.actor.email)
    if actor_id is None:
        return {"allowed": False, "reason": "actor_unknown"}
    return database.decide(
        issuer=item.actor.issuer, subject=item.actor.subject, email=item.actor.email,
        request=Request(actor_id, "external_resource",
                        str(row[0]) if row else f"unregistered:{item.provider}:{item.resource_kind}",
                        item.operation),
        request_id=item.request_id, preview=item.preview,
    )


@app.post("/v2/decide")
def decide_external(item: ExternalRequest, _: None = Depends(require_gateway),
                    database: PolicyStoreV2 = Depends(store)) -> dict:
    try:
        return _decide_external(item.model_copy(update={"preview": False}), database)
    except Exception:
        raise HTTPException(503, "Policy decision is unavailable") from None


@app.post("/v2/decisions/preview")
def preview_external(items: list[ExternalRequest], _: None = Depends(require_gateway),
                     database: PolicyStoreV2 = Depends(store)) -> list[dict]:
    if len(items) > 100:
        raise HTTPException(422, "Too many preview decisions")
    try:
        return [_decide_external(item.model_copy(update={"preview": True}), database)
                for item in items]
    except Exception:
        raise HTTPException(503, "Policy preview is unavailable") from None


@app.post("/v2/context/decide")
def decide_context(item: ContextRequest, _: None = Depends(require_gateway),
                   database: PolicyStoreV2 = Depends(store)) -> dict:
    try:
        actor_id = database.actor_for_oidc(item.actor.issuer, item.actor.email)
        if actor_id is None:
            return {"allowed": False, "reason": "actor_unknown"}
        return database.decide(
            issuer=item.actor.issuer, subject=item.actor.subject, email=item.actor.email,
            request=Request(actor_id, "context_node", item.node_id,
                            item.operation, item.surface),
            request_id=item.request_id, preview=item.preview,
        )
    except Exception:
        raise HTTPException(503, "Policy decision is unavailable") from None


@app.post("/v2/org-action/decide")
def decide_org_action(item: OrgActionRequest, _: None = Depends(require_gateway),
                      database: PolicyStoreV2 = Depends(store)) -> dict:
    # The catalog is closed. Other organizational changes have their own
    # governance routes and cannot be opened by a generic decision request.
    if item.action not in {"tool.register", "tool.retire"}:
        return {"allowed": False, "reason": "not_available"}
    try:
        actor_id = database.actor_for_oidc(item.actor.issuer, item.actor.email)
        if actor_id is None:
            return {"allowed": False, "reason": "actor_unknown"}
        result = database.decide(
            issuer=item.actor.issuer, subject=item.actor.subject, email=item.actor.email,
            request=Request(actor_id, "org_action", str(stable("org_action", item.action)), "invoke"),
            request_id=item.request_id, preview=item.preview,
        )
        return result if result["allowed"] else {"allowed": False, "reason": "not_available"}
    except Exception:
        raise HTTPException(503, "Policy decision is unavailable") from None


@app.post("/v2/org-action/catalog")
def org_action_catalog(item: ActorProof, _: None = Depends(require_gateway),
                       database: PolicyStoreV2 = Depends(store)) -> dict:
    try:
        return database.available_org_actions(issuer=item.issuer, subject=item.subject,
                                              email=item.email)
    except Exception:
        raise HTTPException(503, "Action catalog is unavailable") from None


@app.post("/v2/context/catalog")
def context_catalog(item: ActorProof, _: None = Depends(require_gateway),
                    database: PolicyStoreV2 = Depends(store)) -> dict:
    try:
        return database.context_catalog(issuer=item.issuer, subject=item.subject,
                                        email=item.email)
    except Exception:
        raise HTTPException(503, "Context catalog is unavailable") from None


@app.post("/v2/governance/inbox")
def governance_inbox(item: ActorProof, _: None = Depends(require_gateway),
                     database: PolicyStoreV2 = Depends(store)) -> dict:
    try:
        return GovernanceStore(database.dsn).inbox(
            issuer=item.issuer, subject=item.subject, email=item.email)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Governance inbox is unavailable") from None


@app.get("/v2/governance/approved-for-core")
def approved_for_core(_: dict = Depends(require_core_governance_workflow),
                      database: PolicyStoreV2 = Depends(store)) -> dict:
    try:
        return GovernanceStore(database.dsn).approved_for_core()
    except Exception:
        raise HTTPException(503, "Approved governance changes are unavailable") from None


@app.post("/v2/governance/shares")
def request_share(item: ShareRequest, _: None = Depends(require_gateway),
                  database: PolicyStoreV2 = Depends(store)) -> dict:
    try:
        return GovernanceStore(database.dsn).request_share(
            issuer=item.actor.issuer, subject=item.actor.subject, email=item.actor.email,
            resource_id=_reference("resource", item.resource_id),
            target_department_id=_reference("department", item.target_department_id),
            via_seat_id=_reference("seat", item.via_seat_id), reason=item.reason)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Share request is unavailable") from None


@app.post("/v2/governance/shares/decide")
def decide_share(item: ShareDecision, _: None = Depends(require_gateway),
                 database: PolicyStoreV2 = Depends(store)) -> dict:
    try:
        return GovernanceStore(database.dsn).decide_share(
            issuer=item.actor.issuer, subject=item.actor.subject, email=item.actor.email,
            request_id=item.request_id, via_seat_id=_reference("seat", item.via_seat_id),
            accept=item.accept)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Share decision is unavailable") from None


@app.post("/v2/governance/founder-transfers")
def request_founder_transfer(item: FounderTransferRequest,
                             _: None = Depends(require_gateway),
                             database: PolicyStoreV2 = Depends(store)) -> dict:
    try:
        return GovernanceStore(database.dsn).request_founder_transfer(
            issuer=item.actor.issuer, subject=item.actor.subject, email=item.actor.email,
            target_actor_id=item.target_actor_id, hours=item.deadline_hours)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Founder transfer request is unavailable") from None


@app.post("/v2/governance/founder-transfers/decide")
def decide_founder_transfer(item: FounderTransferDecision,
                            _: None = Depends(require_gateway),
                            database: PolicyStoreV2 = Depends(store)) -> dict:
    try:
        return GovernanceStore(database.dsn).decide_founder_transfer(
            issuer=item.actor.issuer, subject=item.actor.subject, email=item.actor.email,
            transfer_id=item.transfer_id, accept=item.accept)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Founder transfer decision is unavailable") from None


@app.post("/v2/governance/super-admin-changes")
def change_super_admin(item: SuperAdminChange, _: None = Depends(require_gateway),
                       database: PolicyStoreV2 = Depends(store)) -> dict:
    try:
        return GovernanceStore(database.dsn).change_super_admin(
            issuer=item.actor.issuer, subject=item.actor.subject, email=item.actor.email,
            target_actor_id=item.target_actor_id, action=item.action, reason=item.reason)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Super admin change is unavailable") from None


@app.post("/v2/governance/resource-grants")
def request_resource_grant(item: ResourceGrantRequest, _: None = Depends(require_gateway),
                           database: PolicyStoreV2 = Depends(store)) -> dict:
    try:
        return GovernanceStore(database.dsn).request_resource_grant(
            issuer=item.actor.issuer, subject=item.actor.subject, email=item.actor.email,
            resource_id=_reference("resource", item.resource_id),
            recipient_seat_id=_reference("seat", item.recipient_seat_id),
            via_seat_id=_reference("seat", item.via_seat_id),
            operation=item.operation, reason=item.reason)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Resource grant request is unavailable") from None


@app.post("/v2/governance/tools")
def request_tool_registration(item: ToolRegistrationRequest,
                              _: None = Depends(require_gateway),
                              database: PolicyStoreV2 = Depends(store)) -> dict:
    try:
        return GovernanceStore(database.dsn).request_tool_registration(
            issuer=item.actor.issuer, subject=item.actor.subject,
            email=item.actor.email, provider=item.provider,
            operations=item.operations,
            via_seat_id=_reference("seat", item.via_seat_id), reason=item.reason)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Tool registration is unavailable") from None


@app.post("/v2/governance/tools/retire")
def request_tool_retirement(item: ToolRetirementRequest,
                            _: None = Depends(require_gateway),
                            database: PolicyStoreV2 = Depends(store)) -> dict:
    try:
        return GovernanceStore(database.dsn).request_tool_retirement(
            issuer=item.actor.issuer, subject=item.actor.subject,
            email=item.actor.email, provider=item.provider,
            via_seat_id=_reference("seat", item.via_seat_id), reason=item.reason)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Tool retirement is unavailable") from None


@app.post("/v2/governance/resources/retire")
def request_resource_retirement(item: ResourceRetirementRequest,
                                _: None = Depends(require_gateway),
                                database: PolicyStoreV2 = Depends(store)) -> dict:
    try:
        return GovernanceStore(database.dsn).request_resource_retirement(
            issuer=item.actor.issuer, subject=item.actor.subject,
            email=item.actor.email, resource_id=_reference("resource", item.resource_id),
            via_seat_id=_reference("seat", item.via_seat_id), reason=item.reason)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Resource retirement is unavailable") from None


@app.post("/v2/governance/entities/retire")
def request_entity_retirement(item: EntityRetirementRequest,
                              _: None = Depends(require_gateway),
                              database: PolicyStoreV2 = Depends(store)) -> dict:
    try:
        return GovernanceStore(database.dsn).request_entity_retirement(
            issuer=item.actor.issuer, subject=item.actor.subject,
            email=item.actor.email, entity_kind=item.entity_kind,
            entity_code=item.entity_code,
            via_seat_id=_reference("seat", item.via_seat_id), reason=item.reason)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Entity retirement is unavailable") from None


@app.post("/v2/governance/resource-grants/revoke")
def revoke_resource_grant(item: ResourceGrantRequest, _: None = Depends(require_gateway),
                          database: PolicyStoreV2 = Depends(store)) -> dict:
    try:
        return GovernanceStore(database.dsn).revoke_resource_grant(
            issuer=item.actor.issuer, subject=item.actor.subject, email=item.actor.email,
            resource_id=_reference("resource", item.resource_id),
            recipient_seat_id=_reference("seat", item.recipient_seat_id),
            via_seat_id=_reference("seat", item.via_seat_id),
            operation=item.operation, reason=item.reason)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Resource grant revocation is unavailable") from None


@app.post("/v2/governance/resource-sharing")
def resource_sharing(item: ResourceSharingQuery, _: None = Depends(require_gateway),
                     database: PolicyStoreV2 = Depends(store)) -> dict:
    try:
        inventory = GovernanceStore(database.dsn).resource_sharing(
            issuer=item.actor.issuer, subject=item.actor.subject, email=item.actor.email,
            resource_id=_reference("resource", item.resource_id),
            via_seat_id=_reference("seat", item.via_seat_id))
        # The governed client addresses a resource by its Core code. Keep that
        # code in the response; the store uses a stable UUID only internally.
        return {**inventory, "resource_id": item.resource_id}
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Resource sharing inventory is unavailable") from None


@app.post("/v2/uses/complete")
def complete_use(item: Completion, _: None = Depends(require_gateway),
                 database: PolicyStoreV2 = Depends(store)) -> dict:
    try:
        if not database.complete_use(str(item.reservation_id), item.request_id, item.status):
            raise HTTPException(409, "Reservation was not open")
        return {"completed": True}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "Policy decision is unavailable") from None


@app.post("/v2/releases/publish-from-core")
def publish(item: Publication, claims: dict = Depends(require_core_workflow_v2),
            database: PolicyStoreV2 = Depends(store)) -> dict:
    if item.core_commit != claims.get("sha"):
        raise HTTPException(403, "Core commit does not match workflow identity")
    try:
        registry = yaml.safe_load(item.registry_yaml)
        topology = yaml.safe_load(item.topology_yaml)
        topology["_seats"] = yaml.safe_load(item.seats_yaml)["seats"]
        release = activate(database.dsn, item.policy, registry, topology,
                           item.core_commit)
        return {"release_id": release, "core_commit": item.core_commit}
    except (ValueError, KeyError, TypeError, yaml.YAMLError):
        raise HTTPException(422, "Core policy publication is invalid") from None
    except Exception:
        raise HTTPException(503, "Core policy publication is unavailable") from None
