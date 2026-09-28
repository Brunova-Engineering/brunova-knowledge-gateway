"""Scope binding for agent-attested human approvals; never infers consent."""

import hashlib
import hmac
import json
import re
from typing import Any

from app.adapters.google_workspace.errors import WorkspaceAdapterError

_PATTERN = re.compile(r"owa1:([a-f0-9]{16}):([a-f0-9]{16}):([a-f0-9]{64})")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def reference_for_confirmed_operation(thread_id: str, confirmation_id: str,
                                      tool: str, arguments: dict[str, Any]) -> str:
    """Call ONLY after the agent verifies explicit consent for this exact operation.

    Inputs identify the actual conversation and confirming human turn/request.
    Prefer host IDs; a precise visible conversation/request reference is valid
    when opaque IDs are unavailable. This helper encodes agent-attested provenance
    and scope, not independent proof of consent or an authorization grant.
    """
    if not thread_id.strip() or not confirmation_id.strip():
        raise ValueError("Approval requires conversation and confirmation provenance")
    thread, confirmation = _digest(thread_id)[:16], _digest(confirmation_id)[:16]
    scope = _digest([thread, confirmation, tool, arguments])
    return f"owa1:{thread}:{confirmation}:{scope}"


def validate_reference(reference: Any, tool: str, arguments: dict[str, Any]) -> str:
    match = _PATTERN.fullmatch(reference) if isinstance(reference, str) else None
    if match is None:
        raise WorkspaceAdapterError("openwa_approval_required",
                                    "A bounded human approval reference is required for OpenWA writes.", 403)
    thread, confirmation, scope = match.groups()
    if not hmac.compare_digest(scope, _digest([thread, confirmation, tool, arguments])):
        raise WorkspaceAdapterError("openwa_approval_scope_mismatch",
                                    "The approval does not cover this OpenWA operation.", 403)
    return reference


async def call_confirmed_operation(client: Any, thread_id: str, confirmation_id: str,
                                   tool: str, arguments: dict[str, Any]) -> Any:
    """MCP SDK transport for an operation ALREADY explicitly confirmed by a human.

    The caller verifies identity, intent, sensitivity, exact scope and retry safety.
    Never call this merely because a tool exists or a reference can be generated.
    """
    reference = reference_for_confirmed_operation(thread_id, confirmation_id, tool, arguments)
    return await client.call_tool(f"openwa_{tool}", arguments,
                                  meta={"approval_reference": reference})
