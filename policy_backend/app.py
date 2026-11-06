"""Private policy decision API called by the existing Knowledge Gateway."""

from __future__ import annotations

import os
from functools import lru_cache

import psycopg
import yaml
from fastapi import Depends, FastAPI, HTTPException

from .models import DecisionBatchRequest, DecisionRequest, DecisionResponse, GrantUseCompletion
from .store import PolicyStore
from .publication import PolicyDocument, activate_policy
from .service_auth import require_core_workflow, require_gateway
from pydantic import BaseModel, Field

app = FastAPI(title="Panky policy backend", docs_url=None, redoc_url=None)


class CorePublicationRequest(BaseModel):
    core_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    policy: PolicyDocument
    registry_yaml: str = Field(min_length=1, max_length=500_000)


@lru_cache
def store() -> PolicyStore:
    dsn = os.environ.get("PANKY_POLICY_DSN", "")
    if not dsn:
        raise RuntimeError("Policy database is not configured")
    return PolicyStore(dsn)


@app.get("/health")
def health(database: PolicyStore = Depends(store)) -> dict[str, str]:
    try:
        with psycopg.connect(database.dsn, connect_timeout=3) as conn:
            active = conn.execute(
                "SELECT 1 FROM panky_policy_releases WHERE status='active'"
            ).fetchone()
        if not active:
            raise HTTPException(503, "Policy has no active release")
        return {"status": "ready"}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "Policy database is unavailable") from None


@app.post("/v1/decide", response_model=DecisionResponse)
def decide(
    request: DecisionRequest,
    _: None = Depends(require_gateway),
    database: PolicyStore = Depends(store),
) -> DecisionResponse:
    try:
        return database.decide(request)
    except Exception:
        raise HTTPException(503, "Policy decision is unavailable") from None


@app.post("/v1/decisions/preview", response_model=list[DecisionResponse])
def preview_decisions(
    request: DecisionBatchRequest,
    _: None = Depends(require_gateway),
    database: PolicyStore = Depends(store),
) -> list[DecisionResponse]:
    try:
        return [database.decide(item.model_copy(update={"preview": True}))
                for item in request.requests]
    except Exception:
        raise HTTPException(503, "Policy preview is unavailable") from None


@app.post("/v1/uses/complete")
def complete_use(
    request: GrantUseCompletion,
    _: None = Depends(require_gateway),
    database: PolicyStore = Depends(store),
) -> dict[str, bool]:
    try:
        if not database.complete_use(request):
            raise HTTPException(409, "Reservation was not open")
        return {"completed": True}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "Policy decision is unavailable") from None


@app.post("/v1/releases/publish-from-core")
def publish_from_core(
    request: CorePublicationRequest,
    claims: dict = Depends(require_core_workflow),
    database: PolicyStore = Depends(store),
) -> dict[str, str]:
    if request.core_commit != claims.get("sha"):
        raise HTTPException(403, "Core commit does not match workflow identity")
    try:
        registry = yaml.safe_load(request.registry_yaml)["actors"]
        for actor in request.policy.actors:
            record = registry.get(actor.actor_id)
            if not record or record.get("status") != "active":
                raise ValueError("Policy actor is not active in Core")
        if request.policy.tenant != "brunova":
            raise ValueError("Unexpected tenant")
        release = activate_policy(
            database.dsn, request.policy, registry,
            request.core_commit, "github_actions",
        )
    except (ValueError, KeyError, TypeError, yaml.YAMLError):
        raise HTTPException(422, "Core policy publication is invalid") from None
    except Exception:
        raise HTTPException(503, "Core policy publication is unavailable") from None
    return {"release_id": release, "core_commit": request.core_commit}


# Keep the deployed v1 service and URL unchanged while the v2 policy is
# tested in its own database. The bridge continues to call /v1 routes.
from policy_backend_v2.app import app as v2_app  # noqa: E402
app.include_router(v2_app.router)
