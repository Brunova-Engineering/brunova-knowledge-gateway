"""Bounded Presence Engine MCP tools; the Portal is the sole HTTP boundary."""

from __future__ import annotations

import json
import os
import re
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import httpx
from mcp.types import CallToolResult, TextContent
from pydantic import Field

from app.audit import correlation_id, emit_audit_record
from app.auth.principals import active_principal

Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")]
TOOLS = frozenset({
    "presence_get_dashboard", "presence_list_ideas", "presence_list_drafts",
    "presence_list_interactions", "presence_list_ad_proposals", "presence_list_runs",
    "presence_add_idea", "presence_create_draft", "presence_record_interaction",
    "presence_propose_ad", "presence_start_run", "presence_finish_run",
})


async def _request(method: str, path: str, body: dict | None = None) -> CallToolResult:
    principal = active_principal()
    cid = correlation_id(body.get("commandId") if body else None)
    status, code, payload = 503, "PRESENCE_UNAVAILABLE", {"code": "PRESENCE_UNAVAILABLE"}
    reason: str | None = None
    if principal.type not in {"management", "panky"}:
        status, code, payload = 403, "PRESENCE_DENIED", {"code": "PRESENCE_DENIED"}
    else:
        base = os.getenv("ACQUISITION_PORTAL_ORIGIN", "").strip()
        secret = os.getenv("ACQUISITION_PORTAL_SERVICE_TOKEN", "").strip()
        origin = urlsplit(base)
        configured = (origin.scheme == "https" and bool(origin.hostname)
                      and not origin.username and not origin.password
                      and origin.path in ("", "/") and not origin.query and not origin.fragment
                      and bool(re.fullmatch(r"[A-Za-z0-9_-]{43,128}", secret)))
        if configured:
            try:
                async with httpx.AsyncClient(timeout=12, follow_redirects=False, trust_env=False) as client:
                    response = await client.request(method, base.rstrip("/") + "/api/presence/v1" + path,
                                                    json=body, headers={"Authorization": "Bearer " + secret,
                                                                        "x-correlation-id": cid})
                if len(response.content) > 128_000:
                    raise ValueError("response_too_large")
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError("invalid_response")
                status = response.status_code
                if 200 <= status < 300:
                    payload, code = data, None
                else:
                    candidate = data.get("code")
                    code = candidate if isinstance(candidate, str) and re.fullmatch(r"[A-Z_]{1,80}", candidate) else "PRESENCE_REQUEST_FAILED"
                    payload = {"code": code, "correlationId": cid}
            except (httpx.HTTPError, ValueError, TypeError):
                status, code, payload, reason = 503, "PRESENCE_UNAVAILABLE", {"code": "PRESENCE_UNAVAILABLE", "correlationId": cid}, "upstream_unavailable"
    emit_audit_record(request_id=cid, action="presence_control", resource_id=None,
                      resource_type="presence", result="success" if status < 300 else "rejected",
                      http_status=status, error_code=code, error_reason=reason, provider="presence")
    return CallToolResult(content=[TextContent(type="text", text=json.dumps({"httpStatus": status, "data": payload}))],
                          is_error=not 200 <= status < 300)


def register_presence_tools(server: Any) -> None:
    @server.tool()
    async def presence_get_dashboard() -> CallToolResult:
        """Read Presence Engine progress, review queue, published receipts and recent runs."""
        return await _request("GET", "/dashboard")

    @server.tool()
    async def presence_list_ideas() -> CallToolResult:
        """List sourced editorial ideas; a source is evidence, not permission to reuse its content."""
        return await _request("GET", "/ideas")

    @server.tool()
    async def presence_list_drafts() -> CallToolResult:
        """List channel-specific drafts and human-review states."""
        return await _request("GET", "/drafts")

    @server.tool()
    async def presence_list_interactions() -> CallToolResult:
        """List recorded social interactions; only explicit interest can become an Acquisition handoff."""
        return await _request("GET", "/interactions")

    @server.tool()
    async def presence_list_ad_proposals() -> CallToolResult:
        """List proposed amplification experiments; these are not live ads or authorized spend."""
        return await _request("GET", "/ad_proposals")

    @server.tool()
    async def presence_list_runs() -> CallToolResult:
        """Read daily Presence Engine run receipts."""
        return await _request("GET", "/runs")

    @server.tool()
    async def presence_add_idea(command_id: Identifier, title: Annotated[str, Field(min_length=5, max_length=160)],
                                angle: Annotated[str, Field(min_length=10, max_length=1000)],
                                source_url: Annotated[str, Field(min_length=9, max_length=1200)],
                                source_note: Annotated[str, Field(min_length=10, max_length=1000)]) -> CallToolResult:
        """Add one sourced editorial idea after reading the original page. No social effect."""
        return await _request("POST", "/ideas", {"commandId": command_id, "title": title, "angle": angle,
                                                  "sourceUrl": source_url, "sourceNote": source_note})

    @server.tool()
    async def presence_create_draft(command_id: Identifier, idea_id: Identifier,
                                    platform: Literal["LINKEDIN", "INSTAGRAM", "TIKTOK", "FACEBOOK"],
                                    risk: Literal["LOW", "MEDIUM", "HIGH"],
                                    body: Annotated[str, Field(min_length=20, max_length=6000)],
                                    visual_brief: Annotated[str, Field(max_length=1000)] | None = None) -> CallToolResult:
        """Prepare one platform-specific draft for human review. Does not publish, schedule or approve it."""
        return await _request("POST", "/drafts", {"commandId": command_id, "ideaId": idea_id,
                    "platform": platform, "risk": risk, "body": body, "visualBrief": visual_brief})

    @server.tool()
    async def presence_record_interaction(command_id: Identifier,
            platform: Literal["LINKEDIN", "INSTAGRAM", "TIKTOK", "FACEBOOK"],
            external_url: Annotated[str, Field(min_length=9, max_length=1200)],
            summary: Annotated[str, Field(min_length=10, max_length=2000)],
            explicit_interest: bool = False) -> CallToolResult:
        """Record a verified public interaction with provenance. Never send a reply or create CRM records."""
        return await _request("POST", "/interactions", {"commandId": command_id, "platform": platform,
                    "externalUrl": external_url, "summary": summary, "explicitInterest": explicit_interest})

    @server.tool()
    async def presence_propose_ad(command_id: Identifier, draft_id: Identifier,
            hypothesis: Annotated[str, Field(min_length=20, max_length=1000)],
            audience: Annotated[str, Field(min_length=10, max_length=500)],
            budget_cents: Annotated[int, Field(ge=1, le=1_000_000)]) -> CallToolResult:
        """Propose amplification of a publication with a human-recorded receipt. Never create or modify an ad or spend."""
        return await _request("POST", "/ad_proposals", {"commandId": command_id, "draftId": draft_id,
                    "hypothesis": hypothesis, "audience": audience, "budgetCents": budget_cents})

    @server.tool()
    async def presence_start_run(command_id: Identifier) -> CallToolResult:
        """Start one bounded daily editorial window; a second active run is rejected."""
        return await _request("POST", "/runs", {"commandId": command_id})

    @server.tool()
    async def presence_finish_run(command_id: Identifier, run_id: Identifier,
            status: Literal["COMPLETED", "FAILED", "CANCELLED"],
            summary: Annotated[str, Field(min_length=10, max_length=2000)]) -> CallToolResult:
        """Close the run with an honest result and no claim of unverified publication."""
        return await _request("POST", f"/runs/{run_id}/finish", {"commandId": command_id,
                                                          "status": status, "summary": summary})
