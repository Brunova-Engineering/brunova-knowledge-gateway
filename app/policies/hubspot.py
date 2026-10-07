"""Default-deny governance for downstream HubSpot MCP tools."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from app.adapters.google_workspace.errors import WorkspaceAdapterError

READ_TOOLS = frozenset(
    {
        "get_user_details",
        "search_crm_objects",
        "get_crm_objects",
        "search_properties",
        "get_properties",
        "search_owners",
        "get_campaign_contacts_by_type",
        "get_campaign_analytics",
        "get_campaign_asset_types",
        "get_campaign_asset_metrics",
        "search_conversations",
        "get_conversation_channel_metadata",
    }
)
MUTATION_TOOLS = frozenset({"manage_crm_objects"})
SENSITIVE_ARGUMENT_NAMES = frozenset(
    {"access_token", "refresh_token", "client_secret", "authorization"}
)


@dataclass(frozen=True)
class HubSpotToolDecision:
    classification: Literal["read", "mutation", "unknown"]
    allowed: bool
    approval_required: bool


class HubSpotToolPolicy:
    def classify(self, tool_name: str) -> HubSpotToolDecision:
        if tool_name in READ_TOOLS:
            return HubSpotToolDecision("read", True, False)
        if tool_name in MUTATION_TOOLS:
            return HubSpotToolDecision("mutation", True, False)
        return HubSpotToolDecision("unknown", False, False)

    def authorize(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        approval_reference: str | None = None,
        explicit_intent: bool = False,
    ) -> HubSpotToolDecision:
        decision = self.classify(tool_name)
        if not decision.allowed:
            raise WorkspaceAdapterError(
                "hubspot_tool_not_allowed", "The HubSpot tool is not classified for use.", 403
            )
        self._validate_arguments(arguments)
        if decision.classification == "mutation":
            if not explicit_intent:
                raise WorkspaceAdapterError(
                    "hubspot_mutation_intent_required",
                    "Explicit mutation intent is required.",
                    403,
                )
        return decision

    @staticmethod
    def _validate_arguments(arguments: dict[str, Any]) -> None:
        def contains_sensitive_key(value: Any) -> bool:
            if isinstance(value, dict):
                return any(
                    str(key).casefold() in SENSITIVE_ARGUMENT_NAMES
                    or contains_sensitive_key(child)
                    for key, child in value.items()
                )
            if isinstance(value, list):
                return any(contains_sensitive_key(child) for child in value)
            return False

        if contains_sensitive_key(arguments):
            raise WorkspaceAdapterError(
                "hubspot_arguments_invalid", "Credential arguments are not allowed.", 422
            )
        try:
            serialized = json.dumps(arguments, separators=(",", ":"))
        except (TypeError, ValueError) as error:
            raise WorkspaceAdapterError(
                "hubspot_arguments_invalid", "HubSpot arguments must be JSON-compatible.", 422
            ) from error
        if len(serialized.encode()) > 100_000:
            raise WorkspaceAdapterError(
                "hubspot_arguments_too_large", "HubSpot arguments are too large.", 422
            )
