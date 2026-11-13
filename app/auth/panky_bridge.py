"""Temporary OIDC-to-Gateway-token bridge; legacy tokens remain independent."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from secrets import token_urlsafe

import httpx
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token


class PankyBridgeError(ValueError):
    pass


_LOG = logging.getLogger(__name__)


def bridge_mode() -> str:
    mode = os.getenv("PANKY_POLICY_MODE", "off")
    if mode not in {"off", "shadow", "enforce"}:
        raise PankyBridgeError("Invalid policy bridge mode")
    return mode


def policy_v2_mode() -> str:
    mode = os.getenv("PANKY_POLICY_V2_MODE", "off")
    if mode not in {"off", "shadow", "enforce"}:
        raise PankyBridgeError("Invalid Panky 2.0 policy mode")
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
        issued = claims.get("iat")
        expires = claims.get("exp")
        if (metadata != {"alg": "HS256", "typ": "JWT"}
                or claims.get("iss") != "brunova-knowledge-gateway"
                or claims.get("aud") != "brunova-knowledge-gateway/mcp"
                or not isinstance(issued, int) or isinstance(issued, bool)
                or not isinstance(expires, int) or isinstance(expires, bool)
                or not now - 300 <= issued <= now + 5
                or not now < expires <= issued + 300
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
    mode = policy_v2_mode()
    try:
        workload_token = id_token.fetch_id_token(GoogleRequest(), base)
        payload = {
            "actor": {"issuer": identity.issuer,
                      "subject": identity.subject, "email": identity.email},
            "provider": provider, "tool": tool,
            "resource_kind": resource_kind, "resource_ref": resource_ref,
            "parent_source": parent_source,
            "operation": operation, "request_id": request_id,
        }
        response = httpx.post(
            f"{base.rstrip('/')}/{'v2' if mode == 'enforce' else 'v1'}/decide",
            headers={"Authorization": f"Bearer {workload_token}"},
            json=payload,
            timeout=5,
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result.get("allowed"), bool):
            raise ValueError("invalid policy response")
        # Shadow only: compare the new model without changing the existing
        # authorization result or reserving a one-use grant.
        if mode == "shadow":
            try:
                shadow = httpx.post(
                    f"{base.rstrip('/')}/v2/decisions/preview",
                    headers={"Authorization": f"Bearer {workload_token}"},
                    json=[{**payload, "preview": True}],
                    timeout=5,
                )
                shadow.raise_for_status()
                outcomes = shadow.json()
                if len(outcomes) != 1 or not isinstance(outcomes[0].get("allowed"), bool):
                    raise ValueError("invalid v2 preview")
                if outcomes[0]["allowed"] != result["allowed"]:
                    _LOG.warning("Panky policy v1/v2 decision mismatch: provider=%s operation=%s",
                                 provider, operation)
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                _LOG.warning("Panky policy v2 shadow comparison unavailable")
        return result
    except (httpx.HTTPError, ValueError):
        raise PankyBridgeError("Policy decision is unavailable") from None


def context_catalog(identity: PankyIdentity) -> dict:
    """Fetch only this verified Actor's active organizational context map."""
    base = os.getenv("PANKY_POLICY_BACKEND_URL", "")
    if not base.startswith("https://") or policy_v2_mode() != "enforce":
        raise PankyBridgeError("Panky 2.0 context catalog is unavailable")
    try:
        workload_token = id_token.fetch_id_token(GoogleRequest(), base)
        response = httpx.post(
            f"{base.rstrip('/')}/v2/context/catalog",
            headers={"Authorization": f"Bearer {workload_token}"},
            json={"issuer": identity.issuer, "subject": identity.subject,
                  "email": identity.email}, timeout=5,
        )
        response.raise_for_status()
        catalog = response.json()
        if catalog == {"status": "denied", "reason": "actor_disabled"}:
            return catalog
        if (catalog.get("status") != "ready"
                or not isinstance(catalog.get("nodes"), list)
                or not isinstance(catalog.get("release"), str)):
            raise ValueError("invalid context catalog")
        return catalog
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        raise PankyBridgeError("Panky 2.0 context catalog is unavailable") from None


def org_action_catalog(identity: PankyIdentity) -> dict:
    """Return only actions the verified Actor can currently use."""
    base = os.getenv("PANKY_POLICY_BACKEND_URL", "")
    if not base.startswith("https://") or policy_v2_mode() != "enforce":
        raise PankyBridgeError("Action catalog is unavailable")
    try:
        token = id_token.fetch_id_token(GoogleRequest(), base)
        response = httpx.post(
            f"{base.rstrip('/')}/v2/org-action/catalog",
            headers={"Authorization": f"Bearer {token}"},
            json={"issuer": identity.issuer, "subject": identity.subject,
                  "email": identity.email}, timeout=5,
        )
        response.raise_for_status()
        result = response.json()
        if (result.get("status") != "ready" or not isinstance(result.get("release"), str)
                or not isinstance(result.get("actions"), list)
                or not all(isinstance(action, str) for action in result["actions"])
                or not isinstance(result.get("scopes"), dict)
                or set(result["scopes"]) != set(result["actions"])):
            raise ValueError("Invalid scoped action catalog")
        return result
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        raise PankyBridgeError("Action catalog is unavailable") from None


def org_action_decision(identity: PankyIdentity, action: str) -> bool:
    """Check a single closed action without exposing the whole catalog."""
    base = os.getenv("PANKY_POLICY_BACKEND_URL", "")
    if not base.startswith("https://") or policy_v2_mode() != "enforce":
        raise PankyBridgeError("Action decision is unavailable")
    try:
        token = id_token.fetch_id_token(GoogleRequest(), base)
        response = httpx.post(
            f"{base.rstrip('/')}/v2/org-action/decide",
            headers={"Authorization": f"Bearer {token}"},
            json={"actor": {"issuer": identity.issuer, "subject": identity.subject,
                            "email": identity.email}, "action": action,
                  "request_id": token_urlsafe(16), "preview": True}, timeout=5,
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result.get("allowed"), bool):
            raise ValueError("Invalid action decision")
        return result["allowed"]
    except (httpx.HTTPError, ValueError, TypeError):
        raise PankyBridgeError("Action decision is unavailable") from None


def context_decision(identity: PankyIdentity, *, node_id: str, surface: str,
                     operation: str, request_id: str, preview: bool = False) -> dict:
    """Ask the active policy service before an organizational context action."""
    base = os.getenv("PANKY_POLICY_BACKEND_URL", "")
    if not base.startswith("https://") or policy_v2_mode() != "enforce":
        raise PankyBridgeError("Panky 2.0 context policy is unavailable")
    try:
        workload_token = id_token.fetch_id_token(GoogleRequest(), base)
        response = httpx.post(
            f"{base.rstrip('/')}/v2/context/decide",
            headers={"Authorization": f"Bearer {workload_token}"},
            json={"actor": {"issuer": identity.issuer, "subject": identity.subject,
                            "email": identity.email}, "node_id": node_id,
                  "surface": surface, "operation": operation, "request_id": request_id,
                  "preview": preview},
            timeout=5,
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or not isinstance(result.get("allowed"), bool):
            raise ValueError("invalid context decision")
        return result
    except (httpx.HTTPError, ValueError, TypeError):
        raise PankyBridgeError("Panky 2.0 context decision is unavailable") from None


_GOVERNANCE_ACTIONS = {
    "tool_register": "/v2/governance/tools",
    "tool_retire": "/v2/governance/tools/retire",
    "resource_retire": "/v2/governance/resources/retire",
    "entity_retire": "/v2/governance/entities/retire",
    "share_request": "/v2/governance/shares",
    "share_decide": "/v2/governance/shares/decide",
    "founder_transfer_request": "/v2/governance/founder-transfers",
    "founder_transfer_decide": "/v2/governance/founder-transfers/decide",
    "super_admin_change": "/v2/governance/super-admin-changes",
    "resource_grant_request": "/v2/governance/resource-grants",
    "resource_grant_revoke": "/v2/governance/resource-grants/revoke",
    "resource_sharing": "/v2/governance/resource-sharing",
}


def governance_action(identity: PankyIdentity, action: str, body: dict) -> dict:
    """Relay a narrow governance action with the verified Panky identity."""
    base = os.getenv("PANKY_POLICY_BACKEND_URL", "")
    if not base.startswith("https://") or policy_v2_mode() != "enforce":
        raise PankyBridgeError("Panky governance is unavailable")
    if action not in _GOVERNANCE_ACTIONS:
        raise PankyBridgeError("Unknown governance action")
    payload = dict(body)
    payload["actor"] = {"issuer": identity.issuer, "subject": identity.subject,
                        "email": identity.email}
    try:
        workload_token = id_token.fetch_id_token(GoogleRequest(), base)
        response = httpx.post(
            f"{base.rstrip('/')}{_GOVERNANCE_ACTIONS[action]}",
            headers={"Authorization": f"Bearer {workload_token}"},
            json=payload, timeout=5,
        )
        if response.status_code in {403, 409, 422}:
            return {"status": "denied", "reason": response.json().get("detail", "invalid_request")}
        response.raise_for_status()
        result = response.json()
        if action == "resource_sharing":
            if (not isinstance(result, dict)
                    or result.get("resource_id") != body.get("resource_id")
                    or not isinstance(result.get("active_grants"), list)
                    or not isinstance(result.get("shared_departments"), list)):
                raise ValueError("invalid resource sharing response")
            return result
        if (not isinstance(result, dict) or
                result.get("status") not in {"pending", "approved", "rejected", "accepted",
                                             "expired"} or
                result.get("effective") is not False):
            raise ValueError("invalid governance response")
        return result
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        raise PankyBridgeError("Panky governance is unavailable") from None


def governance_inbox(identity: PankyIdentity) -> dict:
    base = os.getenv("PANKY_POLICY_BACKEND_URL", "")
    if not base.startswith("https://") or policy_v2_mode() != "enforce":
        raise PankyBridgeError("Panky governance is unavailable")
    try:
        workload_token = id_token.fetch_id_token(GoogleRequest(), base)
        response = httpx.post(
            f"{base.rstrip('/')}/v2/governance/inbox",
            headers={"Authorization": f"Bearer {workload_token}"},
            json={"issuer": identity.issuer, "subject": identity.subject,
                  "email": identity.email}, timeout=5,
        )
        response.raise_for_status()
        result = response.json()
        if (not isinstance(result, dict)
                or not isinstance(result.get("share_requests"), list)
                or not isinstance(result.get("founder_transfers"), list)):
            raise ValueError("invalid governance inbox")
        return result
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        raise PankyBridgeError("Panky governance inbox is unavailable") from None


def complete_policy_use(reservation_id: str, request_id: str, status: str) -> None:
    base = os.getenv("PANKY_POLICY_BACKEND_URL", "")
    if not base.startswith("https://"):
        raise PankyBridgeError("Policy backend URL is unavailable")
    mode = policy_v2_mode()
    try:
        workload_token = id_token.fetch_id_token(GoogleRequest(), base)
        response = httpx.post(
            f"{base.rstrip('/')}/{'v2' if mode == 'enforce' else 'v1'}/uses/complete",
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
    mode = policy_v2_mode()
    try:
        workload_token = id_token.fetch_id_token(GoogleRequest(), base)
        response = httpx.post(
            f"{base.rstrip('/')}/{'v2' if mode == 'enforce' else 'v1'}/decisions/preview",
            headers={"Authorization": f"Bearer {workload_token}"},
            json=requests if mode == "enforce" else {"requests": requests}, timeout=20,
        )
        response.raise_for_status()
        result = response.json()
        if (not isinstance(result, list) or len(result) != len(requests)
                or any(not isinstance(item.get("allowed"), bool) for item in result)):
            raise ValueError("invalid policy preview")
        return result
    except (httpx.HTTPError, ValueError, AttributeError):
        raise PankyBridgeError("Policy preview is unavailable") from None
