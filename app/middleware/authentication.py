"""Default-deny authentication middleware for Gateway capabilities."""

from __future__ import annotations

from collections.abc import Callable

from fastapi.responses import JSONResponse
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from app.audit import correlation_id, emit_audit_record
from app.auth.gateway_auth import (
    GatewayAuthenticationConfigurationError,
    GatewayPrincipalAuthenticator,
)
from app.auth.principals import Principal, bind_principal, reset_principal

AuthenticatorFactory = Callable[[], GatewayPrincipalAuthenticator]

PUBLIC_PATHS = frozenset(
    {
        "/health",
        "/docs",
        "/docs/oauth2-redirect",
        "/openapi.json",
        "/redoc",
        "/auth/hubspot/callback",
        # This route verifies a Google OIDC proof before issuing a short bearer.
        "/auth/panky/exchange",
        # This route performs dedicated Google OIDC authentication in-app.
        "/events/agent-signals",
    }
)


class GatewayAuthenticationMiddleware:
    """Protect every HTTP capability except explicitly public operational paths."""

    def __init__(
        self,
        app: ASGIApp,
        authenticator_factory: AuthenticatorFactory | None = None,
    ) -> None:
        self.app = app
        self.authenticator_factory = (
            authenticator_factory or GatewayPrincipalAuthenticator.from_environment
        )

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http" or scope.get("path") in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        request_id = getattr(
            request.state,
            "request_id",
            correlation_id(request.headers.get("X-Correlation-ID")),
        )
        consumer = (
            "mcp_client" if request.url.path.startswith("/mcp") else "api_client"
        )

        try:
            authenticator = self.authenticator_factory()
        except GatewayAuthenticationConfigurationError:
            await self._reject(
                scope=scope,
                receive=receive,
                send=send,
                request_id=request_id,
                consumer=consumer,
                error_code="authentication_unavailable",
                status_code=503,
            )
            return

        principal, error_code = authenticator.authenticate(
            request.headers.get("Authorization")
        )
        if error_code:
            await self._reject(
                scope=scope,
                receive=receive,
                send=send,
                request_id=request_id,
                consumer=consumer,
                error_code=error_code,
                status_code=401,
                principal_id=principal.id if principal else None,
                principal_type=principal.type if principal else None,
            )
            return

        assert principal is not None
        if principal.type == "developer":
            authorization_error = self._developer_http_authorization(
                request.url.path, principal
            )
            if authorization_error:
                denied_source_id = self._developer_http_source_id(request.url.path)
                denied_provider = (
                    "hubspot"
                    if request.url.path.startswith("/auth/hubspot")
                    else "workspace"
                    if request.url.path.startswith("/sources/")
                    else "gateway"
                )
                await self._reject(
                    scope=scope,
                    receive=receive,
                    send=send,
                    request_id=request_id,
                    consumer=consumer,
                    error_code=authorization_error,
                    status_code=403,
                    principal_id=principal.id,
                    principal_type=principal.type,
                    provider=denied_provider,
                    source_id=denied_source_id,
                )
                return
        elif principal.type == "panky" and not (
            request.url.path.startswith("/mcp")
            or request.url.path in {"/panky/context/catalog", "/panky/context/decide",
                                    "/panky/org-action/catalog",
                                    "/panky/context/uses/complete",
                                    "/panky/governance/action",
                                    "/panky/governance/inbox"}
        ):
            await self._reject(
                scope=scope, receive=receive, send=send,
                request_id=request_id, consumer=consumer,
                error_code="tool_denied", status_code=403,
                principal_id=principal.id, principal_type=principal.type,
            )
            return
        elif principal.type == "signal_worker" and not request.url.path.startswith("/mcp"):
            await self._reject(
                scope=scope,
                receive=receive,
                send=send,
                request_id=request_id,
                consumer=consumer,
                error_code="tool_denied",
                status_code=403,
                principal_id=principal.id,
                principal_type=principal.type,
                provider="agent_signals",
            )
            return
        request.state.principal = principal
        emit_audit_record(
            request_id=request_id,
            action="authentication",
            resource_id=None,
            resource_type="gateway",
            result="success",
            http_status=200,
            consumer=consumer,
            principal_id=principal.id,
            principal_type=principal.type,
        )
        principal_token = bind_principal(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_principal(principal_token)

    @staticmethod
    async def _reject(
        *,
        scope: Scope,
        receive: Receive,
        send: Send,
        request_id: str,
        consumer: str,
        error_code: str,
        status_code: int,
        principal_id: str | None = None,
        principal_type: str | None = None,
        provider: str | None = None,
        source_id: str | None = None,
    ) -> None:
        emit_audit_record(
            request_id=request_id,
            action="authentication",
            resource_id=None,
            resource_type="gateway",
            result="rejected" if status_code < 500 else "error",
            http_status=status_code,
            error_code=error_code,
            consumer=consumer,
            principal_id=principal_id,
            principal_type=principal_type,
            include_active_principal=False,
            provider=provider,
            source_id=source_id,
        )
        public_error = (
            "invalid_authentication"
            if error_code in {"principal_revoked", "principal_expired"}
            else error_code
        )
        response = JSONResponse(
            status_code=status_code,
            content={"error": public_error},
            headers={
                "WWW-Authenticate": "Bearer",
                "X-Correlation-ID": request_id,
            },
        )
        await response(scope, receive, send)

    @staticmethod
    def _developer_http_authorization(
        path: str, principal: Principal
    ) -> str | None:
        """Keep developer REST access source-scoped; MCP is filtered separately."""

        if path.startswith("/mcp"):
            return None
        if not principal.allows_provider("workspace"):
            return "provider_denied"
        if not principal.allows_capability("read"):
            return "capability_denied"
        if path.startswith("/auth/hubspot"):
            return "provider_denied"
        segments = [part for part in path.split("/") if part]
        if len(segments) >= 2 and segments[0] == "sources":
            source_id = segments[1]
            if source_id == "discover":
                return "tool_denied"
            return None if principal.allows_source(source_id) else "source_denied"
        return "tool_denied"

    @staticmethod
    def _developer_http_source_id(path: str) -> str | None:
        segments = [part for part in path.split("/") if part]
        if len(segments) >= 2 and segments[0] == "sources":
            return None if segments[1] == "discover" else segments[1]
        return None
