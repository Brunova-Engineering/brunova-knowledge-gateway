"""Shared service identities for the v1 bridge and Panky 2.0 policy routes."""

from __future__ import annotations

import os
from functools import lru_cache

import jwt
from fastapi import Header, HTTPException
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token


@lru_cache
def github_jwks() -> jwt.PyJWKClient:
    return jwt.PyJWKClient(
        "https://token.actions.githubusercontent.com/.well-known/jwks",
        timeout=5,
    )


def require_core_workflow(authorization: str | None = Header(default=None)) -> dict:
    scheme, separator, token = (authorization or "").partition(" ")
    if not separator or scheme.casefold() != "bearer" or not token:
        raise HTTPException(401, "Core workflow identity is required")
    try:
        signing_key = github_jwks().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token, signing_key.key, algorithms=["RS256"],
            audience="https://brunova.mx/panky-policy",
            issuer="https://token.actions.githubusercontent.com",
            options={"require": ["exp", "iat", "nbf", "iss", "aud", "sha"]},
        )
    except (jwt.PyJWTError, jwt.PyJWKClientError, ValueError):
        raise HTTPException(401, "Core workflow identity is invalid") from None
    if (str(claims.get("repository_id")) != "1391259854"
            or claims.get("repository") != "Pancracio-s/pancracio-core"
            or claims.get("ref") != "refs/heads/main"
            or claims.get("event_name") not in {"push", "workflow_dispatch"}
            or claims.get("workflow_ref") !=
            "Pancracio-s/pancracio-core/.github/workflows/publish-gateway-policy.yml@refs/heads/main"):
        raise HTTPException(403, "Core workflow is not authorized")
    return claims


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
