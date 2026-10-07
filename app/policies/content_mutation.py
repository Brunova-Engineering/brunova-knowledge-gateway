"""Fail-closed authorization and input validation for governed mutations."""

import re
from enum import Enum

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.auth.principals import active_principal, authorize_workspace_operation
from app.policies.source_access import AllowedSource, SourceAccessPolicy
from app.source_registry import SourceRegistry, SourceType


class MutationOperation(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    MOVE = "move"
    DELETE = "delete"
    SHARE = "share"
    CONVERT = "convert"


class ContentMutationPolicy:
    APPROVAL_REFERENCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,127}$")
    RESOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
    AUDIENCE_PATTERN = re.compile(
        r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@"
        r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,187}[A-Za-z0-9])?$"
    )
    MAX_NAME_LENGTH = 100
    MAX_CHANGE_LENGTH = 4000

    def __init__(
        self,
        registry: SourceRegistry,
        source_access_policy: SourceAccessPolicy,
    ) -> None:
        self._registry = registry
        self._source_access_policy = source_access_policy

    def authorize(
        self,
        *,
        source_id: str,
        operation: MutationOperation,
        approval_reference: str = "",
    ) -> AllowedSource:
        principal = active_principal()
        try:
            source = self._registry.get(source_id)
        except KeyError as error:
            raise WorkspaceAdapterError(
                "source_not_found",
                "The requested knowledge source is not registered.",
                404,
            ) from error
        if principal.type == "developer":
            authorize_workspace_operation(
                principal,
                capability=operation.value,
                source=source,
            )
        allowed_source = self._source_access_policy.authorize_source(
            source,
            require_read=False,
        )
        if not getattr(source.capabilities, operation.value):
            raise WorkspaceAdapterError(
                "source_capability_denied",
                "The selected source does not allow the requested mutation.",
                403,
            )
        return allowed_source

    def authorize_archive_destination(self, source_id: str) -> AllowedSource:
        try:
            source = self._registry.get(source_id)
        except KeyError as error:
            raise WorkspaceAdapterError(
                "archive_destination_not_found",
                "The requested archive destination is not registered.",
                404,
            ) from error
        if source.source_type != SourceType.ARCHIVE_DESTINATION:
            raise WorkspaceAdapterError(
                "archive_destination_invalid",
                "The requested destination is not an approved archive destination.",
                403,
            )
        allowed_source = self._source_access_policy.authorize_source(
            source,
            require_read=False,
        )
        if not source.capabilities.move:
            raise WorkspaceAdapterError(
                "source_capability_denied",
                "The archive destination does not allow governed moves.",
                403,
            )
        return allowed_source

    @classmethod
    def normalized_approval_reference(cls, value: str) -> str | None:
        """Normalize optional legacy audit metadata; it grants no authority."""
        candidate = value.strip()
        if cls.APPROVAL_REFERENCE_PATTERN.fullmatch(candidate):
            return candidate
        return None

    @classmethod
    def validate_name(cls, name: str) -> str:
        candidate = name.strip()
        if not candidate or len(candidate) > cls.MAX_NAME_LENGTH:
            raise WorkspaceAdapterError(
                "mutation_input_invalid",
                "Artifact name must contain between 1 and 100 characters.",
                422,
            )
        return candidate

    @classmethod
    def validate_change(cls, change: str) -> str:
        if not change.strip() or len(change) > cls.MAX_CHANGE_LENGTH:
            raise WorkspaceAdapterError(
                "mutation_input_invalid",
                "Document change must contain between 1 and 4000 characters.",
                422,
            )
        return change

    @classmethod
    def validate_resource_id(cls, resource_id: str) -> str:
        if not cls.RESOURCE_ID_PATTERN.fullmatch(resource_id):
            raise WorkspaceAdapterError(
                "mutation_input_invalid",
                "Workspace resource ID has an invalid format.",
                422,
            )
        return resource_id

    @classmethod
    def normalized_audience(cls, audience: str) -> str | None:
        candidate = audience.strip().casefold()
        if len(candidate) <= 254 and cls.AUDIENCE_PATTERN.fullmatch(candidate):
            return candidate
        return None

    @classmethod
    def validate_audience(cls, audience: str) -> str:
        candidate = cls.normalized_audience(audience)
        if candidate is None:
            raise WorkspaceAdapterError(
                "mutation_audience_invalid",
                "Share audience must be one explicit email address.",
                422,
            )
        return candidate
