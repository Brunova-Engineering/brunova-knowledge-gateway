"""Private policy decision API called by the existing Knowledge Gateway."""

from __future__ import annotations

import os
from functools import lru_cache

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token

from .models import ActorIdentity, DecisionBatchRequest, DecisionRequest, DecisionResponse, GrantUseCompletion
from .store import PolicyStore
from .publication import activate_reviewed_commit, publisher_for_identity
from pydantic import BaseModel, Field

app = FastAPI(title="Panky policy backend", docs_url=None, redoc_url=None)


class ReleaseRequest(BaseModel):
    actor: ActorIdentity
    core_commit: str = Field(pattern=r"^[0-9a-f]{40}$")


@lru_cache
def store() -> PolicyStore:
    dsn = os.environ.get("PANKY_POLICY_DSN", "")
    if not dsn:
        raise RuntimeError("Policy database is not configured")
    return PolicyStore(dsn)


def require_gateway(authorization: str | None = Header(default=None)) -> None:
    audience = os.environ.get("PANKY_POLICY_AUDIENCE", "")
    gateway_email = os.environ.get("PANKY_GATEWAY_SERVICE_ACCOUNT", "")
    if not audience or not gateway_email:
        raise HTTPException(503, "Policy service identity is not configured")
    scheme, separator, token = (authorization or "").partition(" ")
    if not separator or scheme.casefold() != "bearer" or not token:
        raise HTTPException(401, "Invalid Gateway identity")
    try:
        claims = id_token.verify_oauth2_token(token, GoogleRequest(), audience)
    except (ValueError, TypeError):
        raise HTTPException(401, "Invalid Gateway identity") from None
    if (claims.get("email") != gateway_email
            or claims.get("email_verified") is not True
            or claims.get("iss") not in
            {"https://accounts.google.com", "accounts.google.com"}):
        raise HTTPException(403, "Gateway identity is not authorized")


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


@app.post("/v1/releases/activate")
def activate_release(
    request: ReleaseRequest,
    _: None = Depends(require_gateway),
    database: PolicyStore = Depends(store),
) -> dict[str, str]:
    try:
        publisher = publisher_for_identity(
            database.dsn, request.actor.email, request.actor.subject
        )
        release = activate_reviewed_commit(request.core_commit, publisher)
        return {"release_id": release, "core_commit": request.core_commit}
    except ValueError:
        raise HTTPException(403, "Policy publication is not authorized") from None
    except Exception:
        raise HTTPException(503, "Policy publication is unavailable") from None
