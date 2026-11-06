"""Dedicated Panky 2.0 service; legacy v1 endpoints remain untouched."""

from __future__ import annotations

import os
from functools import lru_cache
from uuid import UUID

import psycopg
import yaml
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from policy_backend.service_auth import require_core_workflow_v2, require_gateway
from .decision import Request
from .models import Policy
from .publication import activate
from .store import PolicyStoreV2


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


@lru_cache
def store() -> PolicyStoreV2:
    dsn = os.getenv("PANKY_POLICY_V2_DSN", "")
    if not dsn:
        raise RuntimeError("Panky 2.0 policy database is not configured")
    return PolicyStoreV2(dsn)


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
            "OR (r.resource_kind='source' AND r.external_ref=%s)) "
            "ORDER BY CASE WHEN r.resource_kind=%s AND r.external_ref=%s THEN 0 ELSE 1 END "
            "LIMIT 1",
            # Tool rows model connected systems; item.tool is a concrete
            # Gateway command already checked by the Gateway catalog.
            (tool[0], item.resource_kind, item.resource_ref, item.parent_source,
             item.resource_kind, item.resource_ref),
        ).fetchone()
        if conn.execute(
            "SELECT 1 FROM panky_resources d WHERE d.tool_id=%s AND d.status='disabled' "
            "AND ((d.resource_kind=%s AND d.external_ref=%s) "
            "OR (d.resource_kind='source' AND d.external_ref=%s)) "
            "AND NOT EXISTS (SELECT 1 FROM panky_resources a WHERE a.tool_id=d.tool_id "
            "AND a.resource_kind=d.resource_kind AND a.external_ref=d.external_ref "
            "AND a.status='active')",
            (tool[0], item.resource_kind, item.resource_ref, item.parent_source),
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


@app.post("/v2/context/catalog")
def context_catalog(item: ActorProof, _: None = Depends(require_gateway),
                    database: PolicyStoreV2 = Depends(store)) -> dict:
    try:
        return database.context_catalog(issuer=item.issuer, subject=item.subject,
                                        email=item.email)
    except Exception:
        raise HTTPException(503, "Context catalog is unavailable") from None


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
