"""Temporary OIDC-to-Gateway-token bridge; legacy tokens remain independent."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from secrets import token_urlsafe

import httpx
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token


class PankyBridgeError(ValueError):
    pass


def bridge_mode() -> str:
    mode = os.getenv("PANKY_POLICY_MODE", "off")
    if mode not in {"off", "shadow", "enforce"}:
        raise PankyBridgeError("Invalid policy bridge mode")
    return mode


def _key() -> bytes:
    raw = os.getenv("PANKY_GATEWAY_SIGNING_KEY", "").encode()
    if len(raw) < 32:
        raise PankyBridgeError("Gateway signing key is unavailable")
    return raw


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(header: str, payload: str) -> str:
    return _b64(hmac.new(_key(), f"{header}.{payload}".encode(), hashlib.sha256).digest())


def issue_access_token(google_id_token: str) -> tuple[str, int]:
    """Exchange a verified Google proof for a short Gateway-only bearer."""
    if bridge_mode() == "off":
        raise PankyBridgeError("Panky token exchange is disabled")
    audience = os.getenv("PANKY_DESKTOP_OAUTH_CLIENT_ID", "")
    if not audience:
        raise PankyBridgeError("Desktop OAuth audience is unavailable")
    try:
        claims = id_token.verify_oauth2_token(
            google_id_token, GoogleRequest(), audience
        )
    except (ValueError, TypeError):
        raise PankyBridgeError("Google identity proof is invalid") from None
    if (claims.get("iss") not in
            {"https://accounts.google.com", "accounts.google.com"}
            or claims.get("email_verified") is not True
            or not isinstance(claims.get("sub"), str)
            or not isinstance(claims.get("email"), str)):
        raise PankyBridgeError("Google identity proof is incomplete")
    lifetime = 300
    now = int(time.time())
    header = _b64(b'{"alg":"HS256","typ":"JWT"}')
    body = _b64(json.dumps({
        "iss": "brunova-knowledge-gateway",
        "aud": "brunova-knowledge-gateway/mcp",
        "sub": claims["sub"],
        "oidc_issuer": "https://accounts.google.com",
        "email": claims["email"].casefold(),
        "iat": now,
        "exp": now + lifetime,
        "jti": token_urlsafe(18),
    }, separators=(",", ":"), sort_keys=True).encode())
    return f"{header}.{body}.{_sign(header, body)}", lifetime


@dataclass(frozen=True)
class PankyIdentity:
    issuer: str
    subject: str
    email: str


def verify_access_token(token: str) -> PankyIdentity:
    try:
        header, payload, signature = token.split(".")
        if not hmac.compare_digest(signature, _sign(header, payload)):
            raise ValueError("signature")
        metadata = json.loads(_unb64(header))
        claims = json.loads(_unb64(payload))
        now = int(time.time())
        if (metadata != {"alg": "HS256", "typ": "JWT"}
                or claims.get("iss") != "brunova-knowledge-gateway"
                or claims.get("aud") != "brunova-knowledge-gateway/mcp"
                or not now - 5 <= claims.get("iat", 0) <= now
                or not now < claims.get("exp", 0) <= now + 305
                or claims.get("oidc_issuer") != "https://accounts.google.com"
                or not isinstance(claims.get("sub"), str)
                or not isinstance(claims.get("email"), str)
                or not claims["sub"] or not claims["email"]):
            raise ValueError("claims")
        return PankyIdentity(
            issuer=claims["oidc_issuer"], subject=claims["sub"],
            email=claims["email"],
        )
    except (ValueError, TypeError, KeyError, UnicodeDecodeError, json.JSONDecodeError):
        raise PankyBridgeError("Gateway access token is invalid") from None


def policy_decision(
    identity: PankyIdentity, *, provider: str, tool: str,
    resource_kind: str, resource_ref: str, operation: str, request_id: str,
    parent_source: str | None = None,
) -> dict:
    base = os.getenv("PANKY_POLICY_BACKEND_URL", "")
    if not base.startswith("https://"):
        raise PankyBridgeError("Policy backend URL is unavailable")
    try:
        workload_token = id_token.fetch_id_token(GoogleRequest(), base)
        response = httpx.post(
            f"{base.rstrip('/')}/v1/decide",
            headers={"Authorization": f"Bearer {workload_token}"},
            json={
                "actor": {"issuer": identity.issuer,
                          "subject": identity.subject, "email": identity.email},
                "provider": provider, "tool": tool,
                "resource_kind": resource_kind, "resource_ref": resource_ref,
                "parent_source": parent_source,
                "operation": operation, "request_id": request_id,
            },
            timeout=5,
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result.get("allowed"), bool):
            raise ValueError("invalid policy response")
        return result
    except (httpx.HTTPError, ValueError):
        raise PankyBridgeError("Policy decision is unavailable") from None


def complete_policy_use(reservation_id: str, request_id: str, status: str) -> None:
    base = os.getenv("PANKY_POLICY_BACKEND_URL", "")
    if not base.startswith("https://"):
        raise PankyBridgeError("Policy backend URL is unavailable")
    try:
        workload_token = id_token.fetch_id_token(GoogleRequest(), base)
        response = httpx.post(
            f"{base.rstrip('/')}/v1/uses/complete",
            headers={"Authorization": f"Bearer {workload_token}"},
            json={"reservation_id": reservation_id,
                  "request_id": request_id, "status": status},
            timeout=5,
        )
        response.raise_for_status()
    except httpx.HTTPError:
        raise PankyBridgeError("Policy use completion is unavailable") from None


def preview_policy_decisions(requests: list[dict]) -> list[dict]:
    base = os.getenv("PANKY_POLICY_BACKEND_URL", "")
    if not base.startswith("https://"):
        raise PankyBridgeError("Policy backend URL is unavailable")
    try:
        workload_token = id_token.fetch_id_token(GoogleRequest(), base)
        response = httpx.post(
            f"{base.rstrip('/')}/v1/decisions/preview",
            headers={"Authorization": f"Bearer {workload_token}"},
            json={"requests": requests}, timeout=20,
        )
        response.raise_for_status()
        result = response.json()
        if (not isinstance(result, list) or len(result) != len(requests)
                or any(not isinstance(item.get("allowed"), bool) for item in result)):
            raise ValueError("invalid policy preview")
        return result
    except (httpx.HTTPError, ValueError, AttributeError):
        raise PankyBridgeError("Policy preview is unavailable") from None
