"""Bearer-token authentication for Gateway consumers.

The token authenticates a consumer to this Gateway only. It is not a Google
credential and is intentionally loaded directly from the runtime environment.
"""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass

from app.auth.principals import Principal, PrincipalResolver
from app.auth.panky_bridge import PankyBridgeError, bridge_mode, verify_access_token

GATEWAY_TOKEN_ENVIRONMENT_VARIABLE = "BRUNOVA_GATEWAY_TOKEN"


class GatewayAuthenticationConfigurationError(RuntimeError):
    """Raised when consumer authentication is not configured at runtime."""


@dataclass(frozen=True, repr=False)
class GatewayTokenAuthenticator:
    """Validate one opaque Bearer token without exposing it in representations."""

    _expected_token: str

    @classmethod
    def from_environment(cls) -> "GatewayTokenAuthenticator":
        token = os.getenv(GATEWAY_TOKEN_ENVIRONMENT_VARIABLE, "").strip()
        if not token:
            raise GatewayAuthenticationConfigurationError(
                "Gateway consumer authentication is not configured."
            )
        return cls(token)

    def authenticate(self, authorization: str | None) -> str | None:
        """Return an error code when authentication fails, otherwise ``None``."""

        if authorization is None:
            return "missing_authentication"
        scheme, separator, provided_token = authorization.partition(" ")
        if not separator or scheme.casefold() != "bearer" or not provided_token:
            return "invalid_authentication"
        if not hmac.compare_digest(provided_token, self._expected_token):
            return "invalid_authentication"
        return None


@dataclass(frozen=True, repr=False)
class GatewayPrincipalAuthenticator:
    """Resolve either the legacy management identity or one scoped developer."""

    _resolver: PrincipalResolver

    @classmethod
    def from_environment(cls) -> "GatewayPrincipalAuthenticator":
        try:
            return cls(PrincipalResolver.from_environment())
        except Exception as error:
            raise GatewayAuthenticationConfigurationError(
                "Gateway consumer authentication is not configured."
            ) from error

    def authenticate(
        self, authorization: str | None
    ) -> tuple[Principal | None, str | None]:
        if bridge_mode() != "off" and authorization:
            scheme, separator, token = authorization.partition(" ")
            if separator and scheme.casefold() == "bearer" and token.count(".") == 2:
                try:
                    identity = verify_access_token(token)
                except PankyBridgeError:
                    return None, "invalid_authentication"
                return Principal.panky(subject=identity.subject, email=identity.email), None
        return self._resolver.resolve(authorization)
