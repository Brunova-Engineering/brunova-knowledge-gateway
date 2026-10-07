"""Authenticated principals and fail-closed scoped authorization."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.source_registry import SourceDefinition

PRINCIPALS_FILE_ENVIRONMENT_VARIABLE = "BRUNOVA_PRINCIPALS_FILE"
PRINCIPALS_JSON_ENVIRONMENT_VARIABLE = "BRUNOVA_PRINCIPALS_JSON"


class ProviderScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace: bool = False
    hubspot: bool = False
    n8n: bool = False
    openwa: bool = False


class CapabilityScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    read: bool = False
    create: bool = False
    update: bool = False
    move: bool = False
    delete: bool = False
    share: bool = False
    convert: bool = False


class DeveloperPrincipalRecord(BaseModel):
    """A token lookup record. Only a one-way hash is persisted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    type: Literal["developer"] = "developer"
    status: Literal["active", "revoked"] = "active"
    token_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    providers: ProviderScope
    sources: frozenset[str] = Field(min_length=1)
    capabilities: CapabilityScope
    # Exact downstream tool names per instance. Provider access alone grants none.
    n8n_tools: dict[str, frozenset[str]] = Field(default_factory=dict)
    expires_at: datetime | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("expires_at")
    @classmethod
    def expiration_must_include_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("expires_at must include a timezone")
        return value

    @field_validator("n8n_tools")
    @classmethod
    def n8n_grants_must_be_explicit(
        cls, value: dict[str, frozenset[str]]
    ) -> dict[str, frozenset[str]]:
        import re

        if any(
            not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", instance_id)
            or not names
            or any(not name.strip() or name != name.strip() for name in names)
            for instance_id, names in value.items()
        ):
            raise ValueError("n8n grants require valid instances and exact tool names")
        return value


SignalWorkerOperation = Literal[
    "list", "get", "claim", "release", "complete", "dismiss"
]


class SignalWorkerPrincipalRecord(BaseModel):
    """Machine principal limited to one allowlisted Signal extension."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    type: Literal["signal_worker"]
    status: Literal["active", "revoked"] = "active"
    token_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    signal_types: frozenset[Literal["acquisition_work_available"]] = Field(
        min_length=1
    )
    operations: frozenset[SignalWorkerOperation] = Field(min_length=1)
    expires_at: datetime | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("expires_at")
    @classmethod
    def expiration_must_include_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("expires_at must include a timezone")
        return value


class PrincipalRegistryDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1]
    principals: tuple[DeveloperPrincipalRecord | SignalWorkerPrincipalRecord, ...]

    @field_validator("principals")
    @classmethod
    def principals_must_be_unique(
        cls, value: tuple[DeveloperPrincipalRecord | SignalWorkerPrincipalRecord, ...]
    ) -> tuple[DeveloperPrincipalRecord | SignalWorkerPrincipalRecord, ...]:
        if len({item.id for item in value}) != len(value):
            raise ValueError("Principal IDs must be unique")
        if len({item.token_sha256 for item in value}) != len(value):
            raise ValueError("Principal token hashes must be unique")
        return value


@dataclass(frozen=True, repr=False)
class Principal:
    id: str
    type: Literal["management", "developer", "signal_worker"]
    status: Literal["active", "revoked"]
    providers: ProviderScope
    sources: frozenset[str] | None
    capabilities: CapabilityScope
    n8n_tools: dict[str, frozenset[str]] | None = None
    signal_types: frozenset[str] = frozenset()
    signal_operations: frozenset[str] = frozenset()
    expires_at: datetime | None = None

    @classmethod
    def management(cls) -> "Principal":
        return cls(
            id="management",
            type="management",
            status="active",
            providers=ProviderScope(
                workspace=True, hubspot=True, n8n=True, openwa=True
            ),
            sources=None,
            capabilities=CapabilityScope(
                read=True,
                create=True,
                update=True,
                move=True,
                delete=True,
                share=True,
                convert=True,
            ),
            n8n_tools=None,
        )

    @classmethod
    def from_record(
        cls, record: DeveloperPrincipalRecord | SignalWorkerPrincipalRecord
    ) -> "Principal":
        if isinstance(record, SignalWorkerPrincipalRecord):
            return cls(
                id=record.id,
                type=record.type,
                status=record.status,
                providers=ProviderScope(),
                sources=frozenset(),
                capabilities=CapabilityScope(),
                signal_types=frozenset(record.signal_types),
                signal_operations=frozenset(record.operations),
                expires_at=record.expires_at,
            )
        return cls(
            id=record.id,
            type=record.type,
            status=record.status,
            providers=record.providers,
            sources=record.sources,
            capabilities=record.capabilities,
            n8n_tools=record.n8n_tools,
            expires_at=record.expires_at,
        )

    def allows_provider(self, provider: str) -> bool:
        return bool(getattr(self.providers, provider, False))

    def allows_capability(self, capability: str) -> bool:
        return bool(getattr(self.capabilities, capability, False))

    def allows_source(self, source_id: str) -> bool:
        return self.sources is None or source_id in self.sources

    def allows_n8n_instance(self, instance_id: str) -> bool:
        return self.allows_provider("n8n") and (
            self.type == "management" or instance_id in (self.n8n_tools or {})
        )

    def allows_n8n_tool(self, instance_id: str, tool_name: str) -> bool:
        return self.allows_n8n_instance(instance_id) and (
            self.type == "management"
            or tool_name in (self.n8n_tools or {}).get(instance_id, frozenset())
            or "*" in (self.n8n_tools or {}).get(instance_id, frozenset())
        )


class PrincipalRegistryConfigurationError(RuntimeError):
    pass


class PrincipalResolver:
    """Resolve management and developer bearer tokens without retaining plaintext."""

    def __init__(
        self,
        management_token: str,
        records: tuple[DeveloperPrincipalRecord | SignalWorkerPrincipalRecord, ...],
    ):
        self._management_token = management_token
        self._records = records

    @classmethod
    def from_environment(cls) -> "PrincipalResolver":
        management_token = os.getenv("BRUNOVA_GATEWAY_TOKEN", "").strip()
        if not management_token:
            raise PrincipalRegistryConfigurationError(
                "Gateway consumer authentication is not configured."
            )
        path = os.getenv(PRINCIPALS_FILE_ENVIRONMENT_VARIABLE, "").strip()
        inline = os.getenv(PRINCIPALS_JSON_ENVIRONMENT_VARIABLE, "").strip()
        try:
            if path:
                raw = Path(path).read_text(encoding="utf-8")
            elif inline:
                raw = inline
            else:
                return cls(management_token, ())
            document = PrincipalRegistryDocument.model_validate(json.loads(raw))
        except (OSError, json.JSONDecodeError, ValidationError) as error:
            raise PrincipalRegistryConfigurationError(
                "Developer principal registry is invalid."
            ) from error
        return cls(management_token, document.principals)

    def resolve(self, authorization: str | None) -> tuple[Principal | None, str | None]:
        if authorization is None:
            return None, "missing_authentication"
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.casefold() != "bearer" or not token:
            return None, "invalid_authentication"
        if hmac.compare_digest(token, self._management_token):
            return Principal.management(), None

        fingerprint = hashlib.sha256(token.encode()).hexdigest()
        record = next(
            (
                item
                for item in self._records
                if hmac.compare_digest(fingerprint, item.token_sha256)
            ),
            None,
        )
        if record is None:
            return None, "invalid_authentication"
        if record.status != "active":
            return Principal.from_record(record), "principal_revoked"
        if record.expires_at and record.expires_at <= datetime.now(timezone.utc):
            return Principal.from_record(record), "principal_expired"
        return Principal.from_record(record), None


_active_principal: ContextVar[Principal] = ContextVar(
    "brunova_active_principal", default=Principal.management()
)


def active_principal() -> Principal:
    return _active_principal.get()


def bind_principal(principal: Principal) -> Token[Principal]:
    return _active_principal.set(principal)


def reset_principal(token: Token[Principal]) -> None:
    _active_principal.reset(token)


def authorize_provider(principal: Principal, provider: str) -> None:
    if not principal.allows_provider(provider):
        raise WorkspaceAdapterError(
            "provider_denied", "The requested operation is not authorized.", 403
        )


def authorize_workspace_operation(
    principal: Principal,
    *,
    capability: str,
    source: SourceDefinition | None = None,
) -> None:
    authorize_provider(principal, "workspace")
    if not principal.allows_capability(capability):
        raise WorkspaceAdapterError(
            "capability_denied", "The requested operation is not authorized.", 403
        )
    if source is None:
        return
    if not principal.allows_source(source.id):
        raise WorkspaceAdapterError(
            "source_denied", "The requested source is not authorized.", 403
        )
    if not bool(getattr(source.capabilities, capability, False)):
        raise WorkspaceAdapterError(
            "source_capability_denied",
            "The selected source does not allow this operation.",
            403,
        )
