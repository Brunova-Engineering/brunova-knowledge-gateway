"""Bounded Portal consumer. No database, business interpretation or local truth."""

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

Identifier = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")]
PageLimit = Annotated[int, Field(ge=1, le=100)]
Cursor = Annotated[str, Field(min_length=1, max_length=256)]
TOOLS = frozenset({
    "acquisition_list_cycles", "acquisition_get_cycle", "acquisition_list_accounts",
    "acquisition_get_account", "acquisition_list_priorities", "acquisition_list_attention",
    "acquisition_get_attention", "acquisition_get_attention_counts", "acquisition_list_work",
    "acquisition_get_health", "acquisition_request_research", "acquisition_get_buyer_dry_run", "acquisition_request_buyer_dry_run",
    "acquisition_record_attention_disposition", "acquisition_authorize_controlled_effect",
    "acquisition_request_effect_reconciliation", "acquisition_acknowledge_effect_attention", "acquisition_edit_message_draft",
    "acquisition_get_effect", "acquisition_get_effect_bindings", "acquisition_list_effect_attention",
    "acquisition_list_crm", "acquisition_request_pre_outbound_sync", "acquisition_request_crm_reconciliation",
    "acquisition_review_commercial_handoff", "acquisition_accept_commercial_handoff",
    "acquisition_list_opportunity_pool", "acquisition_get_cycle_review", "acquisition_compose_wave",
    "acquisition_review_wave", "acquisition_request_reconsideration", "acquisition_stop_discovery",
    "acquisition_get_cycle_policy", "acquisition_resolve_accounts", "acquisition_retain_opportunity", "acquisition_pause_cycle",
    "acquisition_get_policy_settings", "acquisition_propose_discovery_limit", "acquisition_confirm_policy_proposal",
    "acquisition_get_operating_model",
    "acquisition_get_activation_preflight",
    "acquisition_get_discovery",
    "acquisition_get_exploration_map", "acquisition_direct_exploration", "acquisition_direct_candidate_enrichment",
    "acquisition_review_candidate_early_screening",
    "acquisition_review_candidate_enrichment_route",
    "acquisition_review_candidate_fit",
    "acquisition_revise_candidate_fit_copy",
    "acquisition_record_candidate_link_observation",
    "acquisition_manage_company_profile",
    "acquisition_reconcile_company_identity",
    "acquisition_prepare_conversation", "acquisition_bind_candidate_message",
    "acquisition_request_discovery_planning",
    "acquisition_request_authenticated_research",
    "acquisition_record_commercial_calibration",
    "acquisition_record_work_plan",
    "acquisition_approve_conversation_worthiness",
    "acquisition_approve_work_plan",
    "acquisition_record_allocation_outcomes",
    "acquisition_record_exploratory_wave",
    "acquisition_record_exploratory_wave_review",
    "acquisition_record_target_resolution_direction",
    "acquisition_record_target_resolution_result",
    "acquisition_record_copy_review",
    "acquisition_record_7eb2_decisions_and_draft",
    "acquisition_authorize_7eb2_scania_attempt_1",
    "acquisition_record_discovery_investigation",
    "acquisition_request_candidate_investigation",
    "acquisition_begin_discovery_batch", "acquisition_finish_discovery_batch",
    "acquisition_archive_candidate", "acquisition_restore_candidate",
})


async def portal_request(method: str, path: str, *, params: dict | None = None,
                         body: dict | None = None) -> CallToolResult:
    """Called only by fixed tools; never accepts caller URLs, headers or identity."""
    principal = active_principal()
    cid = correlation_id(body.get("commandId") if body else None)
    status = 503
    code = "ACQUISITION_UNAVAILABLE"
    error_reason: str | None = None
    payload: Any = {"code": code}
    if principal.type != "management":
        status, code = 403, "ACQUISITION_CAPABILITY_DENIED"
        payload = {"code": code}
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
                async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
                    async with client.stream(method, base.rstrip("/") + "/api/acquisition/v1" + path,
                                             params={k: v for k, v in (params or {}).items() if v is not None},
                                             json=body, headers={"Authorization": "Bearer " + secret,
                                                                 "x-correlation-id": cid}) as response:
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw) > 2 * 1024 * 1024:
                                raise ValueError("bounded_response_exceeded")
                        data = json.loads(raw)
                        if not isinstance(data, dict):
                            raise ValueError("invalid_contract")
                        status = response.status_code
                        if 200 <= status < 300:
                            payload, code = data, None
                        else:
                            candidate = data.get("code") or data.get("problemCode") or (data.get("problem") or {}).get("code")
                            code = candidate if isinstance(candidate, str) and re.fullmatch(r"[A-Z_]{1,80}", candidate) else "ACQUISITION_REQUEST_FAILED"
                            # Error bodies are not trusted content. Preserve status/code,
                            # never expose upstream text, credentials or stack traces.
                            payload = {"code": code, "correlationId": cid}
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                status, code = 503, "ACQUISITION_UNAVAILABLE"
                payload = {"code": code, "correlationId": cid}
                if isinstance(exc, httpx.TimeoutException):
                    error_reason = "upstream_timeout"
                elif isinstance(exc, httpx.HTTPError):
                    error_reason = "upstream_transport"
                elif isinstance(exc, json.JSONDecodeError):
                    error_reason = "invalid_json"
                elif isinstance(exc, ValueError) and str(exc) == "bounded_response_exceeded":
                    error_reason = "response_too_large"
                else:
                    error_reason = "invalid_contract"
    emit_audit_record(request_id=cid, action="acquisition_control", resource_id=None,
                      resource_type="engine_control", result="success" if status < 300 else "rejected",
                      http_status=status, error_code=code, error_reason=error_reason,
                      provider="acquisition")
    return CallToolResult(content=[TextContent(type="text", text=json.dumps({"httpStatus": status, "data": payload}))],
                          is_error=not 200 <= status < 300)


def register_acquisition_tools(server: Any) -> None:
    @server.tool()
    async def acquisition_get_exploration_map(cycle_id: Identifier,
            market: Literal["MX", "US"] | None = None) -> CallToolResult:
        """Read the current industry-to-company exploration map, company profiles, pending work and provenance. An UNVERIFIED lead is retained for follow-up and is not a verified supply-chain relationship. Management read capability only."""
        return await portal_request("GET", "/exploration/map", params={"cycleId": cycle_id, "market": market})

    @server.tool()
    async def acquisition_direct_candidate_enrichment(
            command_id: Identifier, cycle_id: Identifier, company_id: Identifier, link_id: Identifier,
            field_key: Literal["IDENTITY", "OPERATION", "FUNCTION_IN_LINK", "OPERATING_MARKET", "SCALE_SIGNAL", "ACCESS_ROUTE"],
            dimension: Literal["COMPANY_FIRST", "WEB_FIRST", "JOB_FIRST", "SOCIAL_FIRST"],
            question: Annotated[str, Field(min_length=8, max_length=500)],
            expected_stage_version: Annotated[int, Field(ge=1)],
            reason: Annotated[str, Field(min_length=8, max_length=1000)],
            source_uri: Annotated[str, Field(min_length=9, max_length=1000)] | None = None,
            screening_review_attempt_id: Identifier | None = None,
            screening_review_reason: Annotated[str, Field(min_length=20, max_length=480)] | None = None) -> CallToolResult:
        """Queue one candidate-bound, non-contact enrichment question. Web First uses Brunova Chrome AI Mode to find sources, then reads original pages. Other dimensions require an existing attributed HTTPS source. After a no-signal attempt, further spending requires the latest attempt ID and an explicit screening review explaining why this question is worthwhile. This review does not exhaust sources or close the candidate. Read the current company stage/version first; this tool does not confirm an AI answer as a company fact."""
        if dimension == "WEB_FIRST" and source_uri is not None:
            raise ValueError("Web First takes no source URI")
        if dimension != "WEB_FIRST":
            parsed = urlsplit(source_uri or "")
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("an attributed HTTPS source URI is required")
        request = {"cycleId": cycle_id, "companyId": company_id, "linkId": link_id,
                   "fieldKey": field_key, "dimension": dimension, "question": question,
                   "expectedStageVersion": expected_stage_version, "reason": reason}
        if source_uri is not None:
            request["sourceUri"] = source_uri
        if screening_review_attempt_id is not None:
            request["screeningReviewAttemptId"] = screening_review_attempt_id
        if screening_review_reason is not None:
            request["screeningReviewReason"] = screening_review_reason
        return await portal_request("POST", "/exploration/enrichment-directions", body={
            "commandId": command_id, "request": request})

    @server.tool()
    async def acquisition_review_candidate_early_screening(
            command_id: Identifier, cycle_id: Identifier, company_id: Identifier, link_id: Identifier,
            based_on_attempt_id: Identifier, expected_stage_version: Annotated[int, Field(ge=1)],
            source_receipt_hashes: Annotated[list[str], Field(min_length=1, max_length=12)],
            reason: Annotated[str, Field(min_length=30, max_length=1000)]) -> CallToolResult:
        """Review every original source from one complete Web First read as unattributed to this candidate. Requires the exact source receipt hashes, no sourced minimum, one active link and a written reason. This records Panky's review; the Engine worker separately applies an early CLOSED decision with its own actor and audit trail. A blocked or unread source cannot qualify."""
        return await portal_request("POST", "/exploration/early-screening-reviews", body={
            "commandId": command_id, "request": {"cycleId": cycle_id, "companyId": company_id,
                "linkId": link_id, "basedOnAttemptId": based_on_attempt_id,
                "expectedStageVersion": expected_stage_version,
                "sourceReceiptHashes": source_receipt_hashes, "reason": reason}})

    @server.tool()
    async def acquisition_review_candidate_fit(
            command_id: Identifier, cycle_id: Identifier, company_id: Identifier, link_id: Identifier,
            expected_stage_version: Annotated[int, Field(ge=1)],
            decision: Literal["ADVANCE", "MORE_RESEARCH", "NO_FIT"],
            process: Annotated[str, Field(min_length=12, max_length=600)],
            brunova_capability: Annotated[str, Field(min_length=12, max_length=600)],
            proposed_offer: Annotated[str, Field(min_length=12, max_length=1000)],
            fit_hypothesis: Annotated[str, Field(min_length=20, max_length=1500)],
            commercial_question: Annotated[str, Field(min_length=12, max_length=600)],
            uncertainty: Annotated[str, Field(min_length=12, max_length=1000)],
            counterevidence: Annotated[str, Field(min_length=8, max_length=1000)],
            company_source_url: Annotated[str, Field(min_length=9, max_length=1000)],
            brunova_source_url: Annotated[str, Field(min_length=18, max_length=1000)],
            reason: Annotated[str, Field(min_length=20, max_length=1000)]) -> CallToolResult:
        """Record a versioned, link-specific commercial fit review for a candidate already in BRUNOVA_FIT. ADVANCE is applied later by the Engine worker; this tool never creates an Account, proposal, or contact. The company source must already support the dossier, and the Brunova source must be official. Distinguish facts, hypothesis, uncertainty, and contrary evidence."""
        company_url = urlsplit(company_source_url)
        brunova_url = urlsplit(brunova_source_url)
        if (company_url.scheme != "https" or not company_url.hostname or company_url.username or company_url.password
                or brunova_url.scheme != "https" or brunova_url.hostname != "brunova.mx"
                or brunova_url.username or brunova_url.password):
            raise ValueError("attributed HTTPS company and official Brunova sources required")
        return await portal_request("POST", "/exploration/fit-reviews", body={
            "commandId": command_id, "request": {"cycleId": cycle_id, "companyId": company_id,
                "linkId": link_id, "expectedStageVersion": expected_stage_version,
                "decision": decision, "process": process, "brunovaCapability": brunova_capability,
                "proposedOffer": proposed_offer, "fitHypothesis": fit_hypothesis,
                "commercialQuestion": commercial_question, "uncertainty": uncertainty,
                "counterevidence": counterevidence, "companySourceUrl": company_source_url,
                "brunovaSourceUrl": brunova_source_url, "reason": reason}})

    @server.tool()
    async def acquisition_revise_candidate_fit_copy(
            command_id: Identifier, company_id: Identifier, link_id: Identifier,
            review_id: Identifier, expected_stage_version: Annotated[int, Field(ge=1)],
            proposed_offer: Annotated[str, Field(min_length=40, max_length=1000)],
            fit_hypothesis: Annotated[str, Field(min_length=40, max_length=1500)],
            reason: Annotated[str, Field(min_length=30, max_length=1000)]) -> CallToolResult:
        """Correct the wording of an existing ADVANCE fit review after the Engine moved the exact candidate and link to FILTERING. This records an auditable copy revision without changing stage, evidence, original review, Account, proposal or contact. Explain the company-specific signal, Brunova's relevant contribution and the uncertainty; do not assert an unobserved problem as fact."""
        return await portal_request("POST", "/exploration/fit-copy-revisions", body={
            "commandId": command_id, "request": {"companyId": company_id, "linkId": link_id,
                "reviewId": review_id, "expectedStageVersion": expected_stage_version,
                "proposedOffer": proposed_offer, "fitHypothesis": fit_hypothesis,
                "reason": reason}})

    @server.tool()
    async def acquisition_review_candidate_enrichment_route(
            command_id: Identifier, cycle_id: Identifier, company_id: Identifier, link_id: Identifier,
            field_key: Literal["IDENTITY", "OPERATION", "FUNCTION_IN_LINK", "OPERATING_MARKET", "SCALE_SIGNAL", "ACCESS_ROUTE"],
            dimension: Literal["COMPANY_FIRST", "WEB_FIRST", "JOB_FIRST", "SOCIAL_FIRST"],
            based_on_attempt_id: Identifier, expected_stage_version: Annotated[int, Field(ge=1)],
            reason: Annotated[str, Field(min_length=20, max_length=480)],
            review_mode: Literal["SOURCE_READ", "NO_ATTRIBUTED_SOURCE"] = "SOURCE_READ") -> CallToolResult:
        """Record Panky's evidence-bound review of one field and source dimension. SOURCE_READ requires an exact original-page attempt. NO_ATTRIBUTED_SOURCE requires two distinct successful AI source-discovery attempts with every cited original page read, including searches with zero citations. Panky must explain why no citation belongs to this candidate. An unread or blocked page and a technical failure cannot count as exhaustion. This does not close a candidate by itself."""
        return await portal_request("POST", "/exploration/enrichment-route-reviews", body={
            "commandId": command_id, "request": {"cycleId": cycle_id, "companyId": company_id,
                "linkId": link_id, "fieldKey": field_key, "dimension": dimension,
                "basedOnAttemptId": based_on_attempt_id, "expectedStageVersion": expected_stage_version,
                "reason": reason, "reviewMode": review_mode}})

    @server.tool()
    async def acquisition_record_candidate_link_observation(
            command_id: Identifier, cycle_id: Identifier, company_id: Identifier,
            member_company_id: Identifier, link_id: Identifier, based_on_attempt_id: Identifier,
            source_url: Annotated[str, Field(min_length=9, max_length=1000)],
            excerpt: Annotated[str, Field(min_length=20, max_length=600)],
            statement: Annotated[str, Field(min_length=20, max_length=600)],
            reason: Annotated[str, Field(min_length=20, max_length=1000)],
            expected_stage_version: Annotated[int, Field(ge=1)],
            expected_map_version: Annotated[int, Field(ge=1)]) -> CallToolResult:
        """Record one reviewed company-to-link observation from an exact original-page receipt. The canonical candidate and member lead must first be explicitly reconciled; the member must already appear in this link. The quote must occur in a successful read, and the Engine retains the former inferred claim. This never contacts the company or decides fit."""
        parsed = urlsplit(source_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("an original HTTPS source is required")
        return await portal_request("POST", "/exploration/candidate-link-observations", body={
            "commandId": command_id, "request": {"cycleId": cycle_id, "companyId": company_id,
                "memberCompanyId": member_company_id, "linkId": link_id,
                "basedOnAttemptId": based_on_attempt_id, "sourceUrl": source_url,
                "excerpt": excerpt, "statement": statement, "reason": reason,
                "expectedStageVersion": expected_stage_version,
                "expectedMapVersion": expected_map_version}})

    @server.tool()
    async def acquisition_direct_exploration(command_id: Identifier, cycle_id: Identifier,
            market: Literal["MX", "US"], operation: Literal[
                "EXPLORE", "SELECT_BRANCH", "PAUSE_BRANCH", "MARK_TRAVERSED",
                "RECORD_LINK_COVERAGE", "LINK_MARKETS", "RECORD_PRESENCE"],
            expected_version: Annotated[int, Field(ge=1)], policy_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")],
            target_kind: Literal["MACRO_INDUSTRY", "SPECIALTY", "ACTIVITY", "SUPPLY_CHAIN", "LINK", "COMPANY_LEAD"] | None = None,
            direction: Annotated[str, Field(min_length=8, max_length=1000)] | None = None,
            node_id: Identifier | None = None, chain_id: Identifier | None = None,
            coverage_market: Literal["MX", "US"] | None = None,
            status: Literal["ATTEMPTED", "EXHAUSTED", "OBSERVED", "INFERRED", "UNVERIFIED"] | None = None,
            directive_ids: Annotated[list[Identifier], Field(min_length=1, max_length=20)] | None = None,
            open_gaps: list[str] | None = None,
            reason: str | None = None,
            counterpart_id: Identifier | None = None,
            other_expected_version: Annotated[int, Field(ge=1)] | None = None,
            presence_market: Literal["MX", "US"] | None = None,
            source_url: Annotated[str, Field(min_length=1, max_length=2000)] | None = None) -> CallToolResult:
        """Direct one bounded, non-contact exploration step or record governed link coverage, cross-market linkage, or market presence. Read the map first. The Engine validates the active cycle, mandate, map versions and operation-specific evidence. Failed source reads keep leads UNVERIFIED. This tool cannot admit Accounts or contact prospects."""
        request = {"cycleId": cycle_id, "market": market, "operation": operation,
                   "expectedVersion": expected_version, "policyHash": policy_hash}
        if operation in {"EXPLORE", "SELECT_BRANCH", "PAUSE_BRANCH", "MARK_TRAVERSED"}:
            if target_kind is None or direction is None:
                raise ValueError("target_kind and direction are required for this exploration operation")
            request.update({"targetKind": target_kind, "direction": direction, "nodeId": node_id})
        elif operation == "RECORD_LINK_COVERAGE":
            if chain_id is None or coverage_market is None or status not in {"ATTEMPTED", "EXHAUSTED"}:
                raise ValueError("chain_id, coverage_market and coverage status are required")
            if directive_ids is None or open_gaps is None or reason is None:
                raise ValueError("directive_ids, open_gaps and reason are required")
            if not 20 <= len(reason) <= 1000:
                raise ValueError("reason must contain 20 to 1000 characters")
            request.update({"chainId": chain_id, "coverageMarket": coverage_market, "status": status,
                            "directiveIds": directive_ids, "openGaps": open_gaps, "reason": reason})
        elif operation == "LINK_MARKETS":
            if chain_id is None or counterpart_id is None or other_expected_version is None or reason is None:
                raise ValueError("chain_id, counterpart_id, other_expected_version and reason are required")
            request.update({"chainId": chain_id, "counterpartId": counterpart_id,
                            "otherExpectedVersion": other_expected_version, "reason": reason})
        elif operation == "RECORD_PRESENCE":
            if (node_id is None or presence_market is None
                    or status not in {"OBSERVED", "INFERRED", "UNVERIFIED"} or reason is None):
                raise ValueError("node_id, presence_market, presence status and reason are required")
            if status == "OBSERVED":
                parsed_source = urlsplit(source_url or "")
                if (parsed_source.scheme != "https" or not parsed_source.hostname
                        or parsed_source.username or parsed_source.password):
                    raise ValueError("an HTTPS source_url is required for observed presence")
            elif source_url is not None:
                raise ValueError("source_url is only accepted for observed presence")
            request.update({"nodeId": node_id, "presenceMarket": presence_market,
                            "status": status, "reason": reason})
            if source_url is not None:
                request["sourceUrl"] = source_url
        return await portal_request("POST", "/exploration/commands", body={
            "commandId": command_id, "request": request})

    @server.tool()
    async def acquisition_manage_company_profile(command_id: Annotated[str, Field(
            min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")],
            cycle_id: Identifier, market: Literal["MX", "US"],
            operation: Literal["SET_FIELD", "SET_TAGS"], company_id: Identifier,
            expected_version: Annotated[int, Field(ge=1)],
            field_key: Literal[
                "canonicalName", "alternateNames", "websiteOrRegistry", "disambiguation",
                "activity", "function", "markets", "locations", "scaleSignal",
                "fitProcess", "fitHypothesis", "channel", "interlocutorRole",
                "priorContact", "restrictions",
            ] | None = None,
            field_dimension: Literal["identity", "activity", "presence", "fit", "outreach"] | None = None,
            field_status: Literal["KNOWN", "HYPOTHESIS", "PENDING"] | None = None,
            field_value: Annotated[str, Field(max_length=1000)] | None = None,
            field_source_url: Annotated[str, Field(max_length=1000)] | None = None,
            field_observed_at: Annotated[str, Field(max_length=100)] | None = None,
            field_next_question: Annotated[str, Field(max_length=500)] | None = None,
            tags: Annotated[list[Annotated[str, Field(min_length=1, max_length=80)]],
                            Field(max_length=30)] | None = None) -> CallToolResult:
        """Set one bounded company-profile field or replace its bounded tags. Read the exploration map first and pass its current map version. Engine validates active scope, company root, version and durable receipt. Management capability only; this records profile data and has no external effect."""
        request: dict[str, Any] = {
            "cycleId": cycle_id, "market": market, "operation": operation,
            "companyId": company_id, "expectedVersion": expected_version,
        }
        field_args = (field_key, field_dimension, field_status, field_value,
                      field_source_url, field_observed_at, field_next_question)
        if operation == "SET_FIELD":
            if field_key is None or field_dimension is None or field_status is None:
                raise ValueError("field_key, field_dimension and field_status are required for SET_FIELD")
            dimension_by_key = {
                "canonicalName": "identity", "alternateNames": "identity",
                "websiteOrRegistry": "identity", "disambiguation": "identity",
                "activity": "activity", "function": "activity",
                "markets": "presence", "locations": "presence", "scaleSignal": "presence",
                "fitProcess": "fit", "fitHypothesis": "fit",
                "channel": "outreach", "interlocutorRole": "outreach",
                "priorContact": "outreach", "restrictions": "outreach",
            }
            if dimension_by_key[field_key] != field_dimension:
                raise ValueError("field_dimension does not match field_key")
            if field_status != "PENDING" and (field_value is None or not field_value.strip()):
                raise ValueError("field_value is required for KNOWN and HYPOTHESIS fields")
            if field_status == "PENDING" and field_value is not None:
                raise ValueError("field_value is not accepted for PENDING fields")
            if field_source_url is not None:
                parsed_source = urlsplit(field_source_url)
                if (parsed_source.scheme != "https" or not parsed_source.hostname
                        or parsed_source.username or parsed_source.password):
                    raise ValueError("field_source_url must be an HTTPS URL")
            if tags is not None:
                raise ValueError("tags are only accepted for SET_TAGS")
            request["field"] = {
                "key": field_key, "dimension": field_dimension, "value": field_value,
                "status": field_status, "sourceUrl": field_source_url,
                "observedAt": field_observed_at, "nextQuestion": field_next_question,
            }
        else:
            if tags is None:
                raise ValueError("tags are required for SET_TAGS")
            if any(value is not None for value in field_args):
                raise ValueError("field arguments are only accepted for SET_FIELD")
            request["tags"] = tags
        return await portal_request("POST", "/exploration/company/commands", body={
            "commandId": command_id, "request": request})

    @server.tool()
    async def acquisition_reconcile_company_identity(
            command_id: Annotated[str, Field(min_length=1, max_length=200,
                                             pattern=r"^[A-Za-z0-9._:-]+$")],
            cycle_id: Identifier,
            operation: Literal["LINK_LEADS", "UNLINK_LEAD", "SPLIT_ALIAS"],
            reason: Annotated[str, Field(min_length=8, max_length=500)],
            expected_version_mx: Annotated[int, Field(ge=1, le=999_999_999)] | None = None,
            expected_version_us: Annotated[int, Field(ge=1, le=999_999_999)] | None = None,
            canonical_company_id: Identifier | None = None,
            member_company_id: Identifier | None = None,
            company_id: Identifier | None = None,
            source_url: Annotated[str, Field(max_length=1000)] | None = None) -> CallToolResult:
        """Link or unlink exact company lead roots, or split an existing alias. Supply the current map version for every touched market from the read map. Link/split require an HTTPS evidence URL; names alone never reconcile identity. Engine persists the versioned command receipt. Management capability only."""
        if not reason.strip():
            raise ValueError("reason must not be blank")
        versions = {market: version for market, version in (
            ("MX", expected_version_mx), ("US", expected_version_us)
        ) if version is not None}
        if not versions:
            raise ValueError("at least one expected market version is required")

        request: dict[str, Any] = {
            "cycleId": cycle_id, "operation": operation, "reason": reason,
            "expectedVersions": versions,
        }
        if operation == "SPLIT_ALIAS":
            if company_id is None or canonical_company_id is not None or member_company_id is not None:
                raise ValueError("SPLIT_ALIAS requires only company_id")
            if len(versions) != 1:
                raise ValueError("SPLIT_ALIAS requires exactly one expected market version")
            request["companyId"] = company_id
        else:
            if canonical_company_id is None or member_company_id is None or company_id is not None:
                raise ValueError("link operations require canonical_company_id and member_company_id")
            if canonical_company_id == member_company_id:
                raise ValueError("a company lead cannot be linked to itself")
            request.update({"canonicalCompanyId": canonical_company_id,
                            "memberCompanyId": member_company_id})

        if operation in {"LINK_LEADS", "SPLIT_ALIAS"}:
            parsed_source = urlsplit(source_url or "")
            if (parsed_source.scheme != "https" or not parsed_source.hostname
                    or parsed_source.username or parsed_source.password):
                raise ValueError("an HTTPS source_url is required for this identity operation")
            request["sourceUrl"] = source_url
        elif source_url is not None:
            raise ValueError("source_url is only accepted for LINK_LEADS and SPLIT_ALIAS")

        return await portal_request("POST", "/exploration/company/commands", body={
            "commandId": command_id, "request": request})

    @server.tool()
    async def acquisition_get_discovery() -> CallToolResult:
        """Inspect authoritative Discovery planning, source health/yield and durable candidates BEFORE Account admission. Explain what was observed, unresolved identity, missing evidence and pending work; identity is not qualification. Counts and bounded displayed samples differ. Source failure/empty results do not prove absence of market opportunity. No company list is needed to begin an authorized active Cycle; no activation or provider action occurs here."""
        return await portal_request("GET", "/discovery")

    @server.tool()
    async def acquisition_prepare_conversation(command_id: Identifier, objective_reference: Identifier,
            cycle_id: Identifier, candidate_id: Identifier, expected_version: Annotated[int, Field(ge=0)],
            evidence_observation_ids: Annotated[list[str], Field(min_length=1, max_length=6)],
            proposal: dict[str, Any]) -> CallToolResult:
        """Record Pancracio's evidence-bound, versioned Candidate conversation preparation. Read Discovery and its observation IDs first. The proposal must contain bounded signalKind, hypothesis, unknowns, question, subject, message, alternative economic positions, chosenPosition, claims and feedbackConsideration; Engine validates each field and exact provenance. An admitted DISCOVERY_CONTROL objective must bind the exact request. Human feedback is optional, and Pancracio decides any later governed step. This creates no Account, CRM, outbound intent, provider action or effect authorization."""
        return await management("DISCOVERY_CONTROL", command_id, objective_reference, {
            "operation": "PREPARE_CONVERSATION", "cycleId": cycle_id, "candidateId": candidate_id,
            "expectedVersion": expected_version, "evidenceObservationIds": evidence_observation_ids,
            "proposal": proposal})

    @server.tool()
    async def acquisition_bind_candidate_message(command_id: Identifier, objective_reference: Identifier,
            proposal_id: Identifier, expected_buyer_result_id: Identifier,
            expected_message_version: Annotated[int, Field(ge=1)]) -> CallToolResult:
        """Bind Pancracio's current evidence-bound Candidate proposal to the exact current Buyer/Message state under an admitted DISCOVERY_CONTROL objective. The Engine validates safe copy and persists an immutable Message version and proposal binding. Current implementation is restricted to the disposable synthetic CRM laboratory; it creates no effect, provider call or wake."""
        return await management("DISCOVERY_CONTROL", command_id, objective_reference, {
            "operation": "BIND_CANDIDATE_MESSAGE", "proposalId": proposal_id,
            "expectedBuyerResultId": expected_buyer_result_id,
            "expectedMessageVersion": expected_message_version})

    @server.tool()
    async def acquisition_archive_candidate(command_id: Identifier, objective_reference: Identifier,
            candidate_id: Identifier, reason: Annotated[str, Field(min_length=10, max_length=1000)]) -> CallToolResult:
        """Remove one exact Candidate from the active Management workspace without deleting, rejecting, merging, or rewriting its evidence. Requires an admitted DISCOVERY_CONTROL objective bound to the exact request. The immutable archive event preserves actor, reason and history; no work, outreach, CRM or provider effect is created."""
        return await management("DISCOVERY_CONTROL", command_id, objective_reference, {
            "operation": "ARCHIVE_CANDIDATE", "candidateId": candidate_id, "reason": reason})

    @server.tool()
    async def acquisition_restore_candidate(command_id: Identifier, objective_reference: Identifier,
            candidate_id: Identifier, reason: Annotated[str, Field(min_length=10, max_length=1000)]) -> CallToolResult:
        """Restore one exact archived Candidate to the active Management workspace while preserving its archive history. Requires an admitted DISCOVERY_CONTROL objective bound to the exact request. Restoration grants no qualification, admission, work or outreach authority."""
        return await management("DISCOVERY_CONTROL", command_id, objective_reference, {
            "operation": "RESTORE_CANDIDATE", "candidateId": candidate_id, "reason": reason})

    async def management(operation, command_id, objective_reference, request, wave_id=None):
        path = f"/management-waves/{wave_id}/actions/{operation}" if wave_id else "/management-actions/" + operation
        return await portal_request("POST", path, body={
            "commandId": command_id, "objectiveReference": objective_reference, "request": request})

    @server.tool()
    async def acquisition_begin_discovery_batch(command_id: Identifier, objective_reference: Identifier,
            cycle_id: Identifier, batch_id: Identifier,
            policy_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")],
            baseline_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")],
            maximum_new: Annotated[int, Field(ge=1, le=15)], direction: Annotated[str, Field(min_length=10, max_length=500)]) -> CallToolResult:
        """Begin an explicitly approved review batch in the SAME active, paused Discovery Cycle. Read discovery batchBaselines first, preserve existing candidates, bind exact baseline/policy and maximum 1–15 NEW candidates. Requires an admitted exact DISCOVERY_CONTROL objective. This removes only the Discovery review pause; it cannot activate a Cycle, change safety gates, force Account admission or send anything. Request separately bound planning afterward. At the ceiling/expiry broad work stops for Management review; never self-expand to a third batch."""
        return await management("DISCOVERY_CONTROL", command_id, objective_reference, {
            "operation": "BEGIN_REVIEW_BATCH", "cycleId": cycle_id, "batchId": batch_id,
            "policyHash": policy_hash, "baselineHash": baseline_hash, "maximumNew": maximum_new, "direction": direction})

    @server.tool()
    async def acquisition_finish_discovery_batch(command_id: Identifier, objective_reference: Identifier,
            cycle_id: Identifier, batch_id: Identifier,
            policy_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")],
            reason: Annotated[str, Field(min_length=10, max_length=1000)]) -> CallToolResult:
        """Close the exact Discovery review batch early and pause broad Discovery. Existing observations, candidates and Accounts remain durable. Requires an admitted exact DISCOVERY_CONTROL objective and no in-flight source work. Read back reviewBatches and batchCandidates; explain candidate vs Account, uncertainties and source limitations. No outreach, CRM mutation or next batch permission."""
        return await management("DISCOVERY_CONTROL", command_id, objective_reference, {
            "operation": "FINISH_REVIEW_BATCH", "cycleId": cycle_id, "batchId": batch_id, "policyHash": policy_hash, "reason": reason})

    @server.tool()
    async def acquisition_request_candidate_investigation(command_id: Identifier, objective_reference: Identifier,
            cycle_id: Identifier, candidate_id: Identifier, policy_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")],
            expected_last_seen: str, direction: Annotated[str, Field(min_length=10, max_length=2000)], urls: list[str]) -> CallToolResult:
        """Request real Engine investigation work for ONE existing candidate while broad Discovery remains paused. Read current candidate first and copy last_seen exactly. Provide independent Management direction and 1–6 public HTTPS evidence routes; URLs are hypotheses, never identity verification. Requires an exact admitted DISCOVERY_CONTROL objective. Engine commits WorkItem, existing Signal/Mac executes bounded reads, evidence/reconciliation/screen history is durable. Does not resume Discovery, add candidates, waive identity or contact prospects. Read back journey after execution."""
        return await management("DISCOVERY_CONTROL", command_id, objective_reference, {
            "operation": "REQUEST_CANDIDATE_INVESTIGATION", "cycleId": cycle_id, "candidateId": candidate_id,
            "policyHash": policy_hash, "expectedLastSeen": expected_last_seen, "direction": direction, "urls": urls})

    @server.tool()
    async def acquisition_record_discovery_investigation(command_id: Identifier, objective_reference: Identifier,
            cycle_id: Identifier, candidate_id: Identifier, policy_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")],
            summary: Annotated[str, Field(min_length=10, max_length=4000)], next_action: Annotated[str, Field(min_length=10, max_length=1000)],
            attempts: list[dict[str, str]], limitations: list[str]) -> CallToolResult:
        """Record a bounded public-evidence investigation of an existing candidate while Discovery is paused. Requires exact DISCOVERY_CONTROL objective. Preserve actual executor/search/read provenance; never imply Pancracio independently browsed when Codex supplied the investigation. Findings are Management-recorded observations/inferences, NOT verified identity, qualification, admission or permission to resume. Read journeys afterward. No provider or DB bypass."""
        return await management("DISCOVERY_CONTROL", command_id, objective_reference, {
            "operation": "RECORD_INVESTIGATION", "cycleId": cycle_id, "candidateId": candidate_id,
            "policyHash": policy_hash, "summary": summary, "nextAction": next_action,
            "attempts": attempts, "limitations": limitations})

    @server.tool()
    async def acquisition_request_discovery_planning(command_id: Identifier, objective_reference: Identifier,
            cycle_id: Identifier, policy_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")],
            direction: Annotated[str, Field(min_length=10, max_length=500)]) -> CallToolResult:
        """Request bounded Discovery planning/reorientation, not a company list or policy change. Read the exact current policy first; explain direction, then use an admitted DISCOVERY_CONTROL objective binding this request. Requires a separately authorized ACTIVE Cycle with standing Discovery scope. Does not activate it, bypass cadence, resolve identities by fiat, qualify companies, or call sources directly. WAITING means committed direction, not completed search. Existing pending work must finish/reconcile first."""
        return await management("DISCOVERY_CONTROL", command_id, objective_reference, {
            "operation": "REQUEST_PLANNING", "cycleId": cycle_id, "policyHash": policy_hash, "direction": direction})

    @server.tool()
    async def acquisition_request_authenticated_research(command_id: Identifier, objective_reference: Identifier,
            cycle_id: Identifier, policy_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")],
            uri: Annotated[str, Field(min_length=10, max_length=500)],
            surface: Literal["LINKEDIN", "FACEBOOK", "AUTHENTICATED_RESEARCH_SERVICE"],
            evidence_expected: Annotated[str, Field(min_length=10, max_length=500)]) -> CallToolResult:
        """Request one read-only authenticated capability proof in the exact Jorge browser profile. This is calibration evidence, never prospect evidence, and cannot like, follow, message, connect, submit, publish or admit an Account. Requires an exact admitted DISCOVERY_CONTROL objective; the Engine creates a leased WorkItem, records a minimized receipt and cleans up its owned tab/window."""
        return await management("DISCOVERY_CONTROL", command_id, objective_reference, {
            "operation": "REQUEST_AUTHENTICATED_RESEARCH", "cycleId": cycle_id, "policyHash": policy_hash,
            "profile": "Jorge", "purpose": "CAPABILITY_CALIBRATION_ONLY", "uri": uri,
            "surface": surface, "evidenceExpected": evidence_expected})

    @server.tool()
    async def acquisition_record_commercial_calibration(command_id: Identifier, objective_reference: Identifier,
            cycle_id: Identifier, policy_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")],
            model_version: Annotated[str, Field(min_length=1, max_length=100)], reviews: list[dict[str, Any]]) -> CallToolResult:
        """Record Pancracio's independent dual-lens review for the complete current candidate set. Strict Engine state and conversation-worthiness remain separate; every review must state internalNeed UNKNOWN, a material falsifier and an outreach learning goal. This is interpretation only: no admission, outreach, score override or source mutation."""
        return await management("DISCOVERY_CONTROL", command_id, objective_reference, {
            "operation": "RECORD_COMMERCIAL_CALIBRATION", "cycleId": cycle_id, "policyHash": policy_hash,
            "modelVersion": model_version, "reviews": reviews})

    @server.tool()
    async def acquisition_record_work_plan(command_id: Identifier, objective_reference: Identifier,
            cycle_id: Identifier, policy_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")],
            expires_at: str, rationale: Annotated[str, Field(min_length=20, max_length=2000)],
            inventory: dict[str, Any], items: list[dict[str, Any]]) -> CallToolResult:
        """Propose the smallest expiring Today/Next acquisition work allocation over authoritative inventory. Company, Job and Social are evidence dimensions, not additive scores. Broad Discovery remains denied while paused; each item needs a target, expected information gain, budget and stop condition. The durable result awaits Management and grants no autonomous execution."""
        return await management("DISCOVERY_CONTROL", command_id, objective_reference, {
            "operation": "RECORD_WORK_PLAN", "cycleId": cycle_id, "policyHash": policy_hash,
            "expiresAt": expires_at, "rationale": rationale, "inventory": inventory, "items": items})

    @server.tool()
    async def acquisition_approve_conversation_worthiness(command_id: Identifier, objective_reference: Identifier,
            cycle_id: Identifier, policy_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")],
            semantic_version: Literal["1"], definition: dict[str, Any]) -> CallToolResult:
        """Integrate the Human-approved conversation-worthiness semantic policy beside immutable Cycle v3. This records no score, Account admission, qualification, contact or outreach authority. Internal need remains UNKNOWN by default and the exact strict path is unchanged."""
        return await management("DISCOVERY_CONTROL", command_id, objective_reference, {
            "operation": "APPROVE_CONVERSATION_WORTHINESS", "cycleId": cycle_id,
            "policyHash": policy_hash, "semanticVersion": semantic_version, "definition": definition})

    @server.tool()
    async def acquisition_approve_work_plan(command_id: Identifier, objective_reference: Identifier,
            cycle_id: Identifier, policy_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")],
            plan_id: Identifier, semantic_version: Literal["1"], choices: list[dict[str, Any]]) -> CallToolResult:
        """Approve one current expiring work plan after comparing Company, Job and Social for every item. Each choice binds an existing candidate, selected dimension, exact future investigation command, URLs, budget and stop condition. It does not itself create WorkItems or resume broad Discovery."""
        return await management("DISCOVERY_CONTROL", command_id, objective_reference, {
            "operation": "APPROVE_WORK_PLAN", "cycleId": cycle_id, "policyHash": policy_hash,
            "planId": plan_id, "semanticVersion": semantic_version, "choices": choices})

    @server.tool()
    async def acquisition_record_allocation_outcomes(command_id: Identifier, objective_reference: Identifier,
            cycle_id: Identifier, policy_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")],
            plan_id: Identifier, outcomes: list[dict[str, Any]]) -> CallToolResult:
        """Record Pancracio's bounded post-execution BEFORE/WORK/AFTER and allocator review for every approved item. The Engine verifies exact terminal WorkItems and actual request budgets. This may reconsider conversation-worthiness but never changes strict policy, resumes Discovery or authorizes outreach."""
        return await management("DISCOVERY_CONTROL", command_id, objective_reference, {
            "operation": "RECORD_ALLOCATION_OUTCOMES", "cycleId": cycle_id,
            "policyHash": policy_hash, "planId": plan_id, "outcomes": outcomes})

    @server.tool()
    async def acquisition_record_exploratory_wave(command_id: Identifier, objective_reference: Identifier,
            cycle_id: Identifier, wave_id: Identifier, version: Annotated[int, Field(ge=1)],
            evidence_snapshot: dict[str, Any], all_candidates: list[dict[str, Any]], targets: list[dict[str, Any]]) -> CallToolResult:
        """Persist Pancracio's complete, non-executable 7E-B.1 exploratory learning-wave proposal. It must consider all 16 current candidates and select at most four CandidateOrganizations, each with an UNKNOWN internal need, falsifiable learning goal, supported Person/ContactPoints, channel-specific drafts, one shared cross-channel attempt budget and no automatic escalation. This never creates an Account, authorization, EffectIntent, message, provider mutation or 7E-B.2 start."""
        return await management("DISCOVERY_CONTROL", command_id, objective_reference, {
            "operation": "RECORD_EXPLORATORY_WAVE", "cycleId": cycle_id, "waveId": wave_id,
            "version": version, "targetClass": "EXPLORATORY_CANDIDATE_LEARNING", "semanticVersion": "1",
            "evidenceSnapshot": evidence_snapshot, "allCandidates": all_candidates, "targets": targets,
            "executable": False, "outreachAuthority": "NONE"})

    @server.tool()
    async def acquisition_record_exploratory_wave_review(command_id: Identifier, objective_reference: Identifier,
            wave_id: Identifier, review: dict[str, Any]) -> CallToolResult:
        """Persist Pancracio's independent review of a complete proposed learning wave: selection, learning value, Person identity, channel justification, epistemic honesty, creepiness/generic-copy risk, stop conditions and what silence cannot establish. effectsAuthorized must be false. Review cannot authorize or execute outreach."""
        return await management("DISCOVERY_CONTROL", command_id, objective_reference, {
            "operation": "RECORD_EXPLORATORY_REVIEW", "waveId": wave_id, "review": review})

    @server.tool()
    async def acquisition_record_target_resolution_direction(command_id: Identifier, objective_reference: Identifier,
            cycle_id: Identifier, source_wave_id: Identifier, rationale: Annotated[str, Field(min_length=20, max_length=2000)],
            items: list[dict[str, Any]]) -> CallToolResult:
        """Persist Pancracio's bounded 7E-B.1a direction for exactly Scania, Watsco and Lumexa in that priority order. Creates three non-effect TARGET_CONTACT_RESOLUTION WorkItems only; broad Discovery stays paused, the fourth slot stays empty and outreach authority remains NONE."""
        return await management("DISCOVERY_CONTROL", command_id, objective_reference, {
            "operation": "RECORD_TARGET_RESOLUTION_DIRECTION", "cycleId": cycle_id,
            "sourceWaveId": source_wave_id, "rationale": rationale, "items": items,
            "fourthSlotEmpty": True, "broadDiscoveryPaused": True, "outreachAuthority": "NONE"})

    @server.tool()
    async def acquisition_record_target_resolution_result(command_id: Identifier, objective_reference: Identifier,
            work_item_id: Identifier, person_status: Literal["PROBLEM_OWNER", "ROUTING_PERSON", "UNRESOLVED"],
            person: dict[str, Any], contact_points: list[dict[str, Any]],
            channel_status: Literal["SUPPORTED", "CONTACT_UNRESOLVED", "AUTH_REQUIRED", "CHANNEL_POLICY_BLOCKED"],
            readiness: Literal["READY_FOR_HUMAN_EFFECT_REVIEW", "HOLD_OWNER", "HOLD_CONTACTPOINT", "HOLD_CHANNEL", "HOLD_MESSAGE", "REMOVE", "RETAIN"],
            findings: Annotated[str, Field(min_length=20, max_length=4000)], sources: list[dict[str, Any]],
            jorge_profile_usage: dict[str, Any], actual_requests: Annotated[int, Field(ge=0, le=5)],
            actual_minutes: Annotated[int, Field(ge=0, le=60)], stop_reason: Annotated[str, Field(min_length=10, max_length=2000)],
            pancracio_readback: Annotated[str, Field(min_length=20, max_length=3000)]) -> CallToolResult:
        """Reconcile one claimed target-resolution WorkItem after bounded public/authenticated read research. Preserves Person/ContactPoint provenance and exact READY/HOLD result. It cannot create outreach authority, an EffectIntent or an Account."""
        return await management("DISCOVERY_CONTROL", command_id, objective_reference, {
            "operation": "RECORD_TARGET_RESOLUTION_RESULT", "workItemId": work_item_id,
            "personStatus": person_status, "person": person, "contactPoints": contact_points,
            "channelStatus": channel_status, "readiness": readiness, "findings": findings, "sources": sources,
            "jorgeProfileUsage": jorge_profile_usage, "actualRequests": actual_requests, "actualMinutes": actual_minutes,
            "stopReason": stop_reason, "pancracioReadback": pancracio_readback, "effectCreated": False})

    @server.tool()
    async def acquisition_record_copy_review(command_id: Identifier, objective_reference: Identifier,
            wave_id: Identifier, review: dict[str, Any]) -> CallToolResult:
        """Persist Pancracio's independent 7E-B.1a final-copy review after all three resolution results and the immutable v5 package exist. effectsAuthorized must be false; this cannot start 7E-B.2 or execute outreach."""
        return await management("DISCOVERY_CONTROL", command_id, objective_reference, {
            "operation": "RECORD_COPY_REVIEW", "waveId": wave_id, "review": review})

    @server.tool()
    async def acquisition_record_7eb2_decisions_and_draft(command_id: Identifier, objective_reference: Identifier,
            cycle_id: Identifier, wave_id: Identifier, subject: Annotated[str, Field(min_length=1, max_length=160)],
            text: Annotated[str, Field(min_length=1, max_length=4000)], claims: list[dict[str, Any]]) -> CallToolResult:
        """Record the exact Human 7E-B.2 decisions (Scania Attempt 1 Email approved; Watsco HOLD; Lumexa HOLD_CONTACTPOINT) and immutable Scania message version 3. No effect is created by this step."""
        return await management("DISCOVERY_CONTROL", command_id, objective_reference, {
            "operation": "RECORD_COPY_REVIEW", "b2Operation": "RECORD_7EB2_DECISIONS_AND_DRAFT", "cycleId": cycle_id, "waveId": wave_id,
            "humanActor": "jorgeaveraf", "scaniaDecision": "APPROVE_ATTEMPT_1", "watscoDecision": "HOLD",
            "lumexaDecision": "HOLD_CONTACTPOINT", "subject": subject, "text": text, "claims": claims,
            "unsupportedClaims": 0, "claimValidationErrors": [], "humanQualityErrors": []})

    @server.tool()
    async def acquisition_authorize_7eb2_scania_attempt_1(command_id: Identifier, objective_reference: Identifier,
            expected_binding_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")], authorization_expires_at: str,
            correlation_id_value: Identifier) -> CallToolResult:
        """Create the one exact Human EffectAuthorization and durable EffectIntent for Scania México Attempt 1 by Email after fresh preflight. It cannot authorize Attempt 2 or another target/channel; execution still revalidates and consumes a one-use gate before Gmail."""
        return await management("DISCOVERY_CONTROL", command_id, objective_reference, {
            "operation": "RECORD_COPY_REVIEW", "b2Operation": "AUTHORIZE_7EB2_SCANIA_ATTEMPT_1", "humanActor": "jorgeaveraf",
            "candidateId": "candidate_03c9659012cff866a06b557c0f81c4bfebcedbbb467852dde72a15ab00c97129",
            "channel": "EMAIL", "attempt": 1, "recipient": "alejandro.mondragon@scania.com",
            "sender": "brunova@brunova.mx", "displayFrom": "Brunova", "expectedBindingHash": expected_binding_hash,
            "authorizationExpiresAt": authorization_expires_at, "correlationId": correlation_id_value})

    @server.tool()
    async def acquisition_resolve_accounts(cycle_id: Identifier,
            name: Annotated[str, Field(min_length=2, max_length=120)]) -> CallToolResult:
        """Resolve a business name/domain using a bounded authoritative lookup (maximum 20 candidates). If CLARIFICATION_REQUIRED or truncated, ask which company using domain/country, NEVER choose the first match or ask the Human for an internal ID. Read the chosen Account and current version before any separately authorized command. This lookup never mutates."""
        return await portal_request("GET", f"/cycles/{cycle_id}/resolve-accounts", params={"name": name})

    @server.tool()
    async def acquisition_retain_opportunity(command_id: Identifier, objective_reference: Identifier,
            cycle_id: Identifier, account_id: Identifier, expected_version: Annotated[int, Field(ge=1)],
            reason: Annotated[str, Field(min_length=1, max_length=1000)],
            remove_from_wave: bool = False) -> CallToolResult:
        """Retain an exact resolved Account for later without changing its qualification/evidence. Requires an admitted exact CYCLE_CONTROL objective. Removal cancels the ENTIRE unexecuted wave and its authority (no silent replacement); its logical wave number/history remains consumed. Explain that consequence before requesting removal. Executed waves require Management review instead."""
        return await management("CYCLE_CONTROL", command_id, objective_reference, {
            "cycleId": cycle_id, "accountId": account_id, "expectedVersion": expected_version,
            "operation": "REMOVE_FROM_WAVE" if remove_from_wave else "RETAIN_ACCOUNT", "reason": reason})

    @server.tool()
    async def acquisition_pause_cycle(command_id: Identifier, objective_reference: Identifier,
            cycle_id: Identifier, expected_version: Annotated[int, Field(ge=1)],
            reason: Annotated[str, Field(min_length=1, max_length=1000)]) -> CallToolResult:
        """Apply an exact admitted technical halt to prevent progression; preserve opportunities/evidence. Explain this is a safety/operational pause, not a claim of business non-fit. No automatic resume or activation."""
        return await management("CYCLE_CONTROL", command_id, objective_reference, {
            "cycleId": cycle_id, "expectedVersion": expected_version, "operation": "TECHNICAL_HALT", "reason": reason})

    @server.tool()
    async def acquisition_get_cycle_policy() -> CallToolResult:
        """Inspect the versioned Cycle 1 policy definition and implementation readiness. This definition is not a live Cycle, objective or activation permission. Read current Cycle/health to determine real-data state; Discovery activation does not authorize production effects."""
        return await portal_request("GET", "/cycle-policy")

    @server.tool()
    async def acquisition_get_policy_settings() -> CallToolResult:
        """Read Engine-authoritative candidate settings and mutability. Explain in the user's language: configured is not active; retained opportunities are not rejected; changing an approved limit needs governance. Never infer runtime health from policy readiness."""
        return await portal_request("GET", "/policy-candidate")

    @server.tool()
    async def acquisition_get_operating_model() -> CallToolResult:
        """Explain in ordinary user-language how Acquisition operates without an open Portal or chat. Engine/PostgreSQL retain truth; Mac executes admitted bounded work; scheduler recovers, Signals wake immediately; Pancracio interprets and directs through Management authority; Portal supports visual review. Sleeping/offline Mac delays execution, not durability. Report actual observed schedule/activity with freshness; never promise 24/7 execution, invent health or imply current calibration runtime is already a real active Cycle. Waves, policy and activation remain Management boundaries."""
        return await portal_request("GET", "/operating-model")

    @server.tool()
    async def acquisition_get_activation_preflight() -> CallToolResult:
        """Read authoritative activation preflight, checks, freshness and reasons. This is observability, never permission or an activation action. Stale/missing probes mean not ready. Explain issues in the user's language; distinguish Email readiness from disabled production execution and future 7E-A research from first-wave approval/outreach."""
        return await portal_request("GET", "/activation-preflight")

    @server.tool()
    async def acquisition_propose_discovery_limit(command_id: Identifier, objective_reference: Identifier,
            expected_version: Annotated[int, Field(ge=3)], discovery_maximum: Annotated[int, Field(ge=1, le=75)],
            reason: Annotated[str, Field(min_length=1, max_length=1000)]) -> CallToolResult:
        """Propose a pre-activation candidate change under an admitted exact POLICY_CANDIDATE objective. Explain current/proposed value and impact before confirmation. This bounded editor only reduces/restores within the approved ceiling of 75; increases require material policy revision. Does not change the candidate yet, activate a Cycle or alter snapshots."""
        return await management("POLICY_CANDIDATE", command_id, objective_reference,
            {"operation": "PROPOSE", "expectedVersion": expected_version, "discoveryMaximum": discovery_maximum, "reason": reason})

    @server.tool()
    async def acquisition_confirm_policy_proposal(command_id: Identifier, objective_reference: Identifier,
            expected_version: Annotated[int, Field(ge=3)], proposal_id: Identifier,
            proposal_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]) -> CallToolResult:
        """Confirm the exact previously previewed policy proposal using a separately admitted exact objective. Resolve identifiers from Engine, not from the Human. Creates immutable candidate history with truthful Pancracio provenance; no activation or provider effect. Stale/expired proposals fail closed. Read back settings afterward."""
        return await management("POLICY_CANDIDATE", command_id, objective_reference,
            {"operation": "CONFIRM", "expectedVersion": expected_version, "proposalId": proposal_id, "proposalHash": proposal_hash})

    @server.tool()
    async def acquisition_list_opportunity_pool(cycle_id: Identifier,
            state: Literal["READY_NOW", "RETAINED", "HOLD", "REJECTED"] | None = None,
            limit: Annotated[int, Field(ge=1, le=50)] = 25, after: Identifier | None = None) -> CallToolResult:
        """Read the durable opportunity pool, bounded by stable Account ID pagination. Not selected is not rejected. Readiness reasons and evidence meaning come unchanged from Engine; no local scoring."""
        return await portal_request("GET", f"/cycles/{cycle_id}/pool", params={"state": state, "limit": limit, "after": after})

    @server.tool()
    async def acquisition_get_cycle_review(cycle_id: Identifier) -> CallToolResult:
        """Read Cycle discovery by market, pool, exact waves, attempt budgets, responses and review evidence. Missing market evidence is a discovery limitation, not a country-quality verdict. No automatic ICP changes."""
        return await portal_request("GET", f"/cycles/{cycle_id}/review")

    @server.tool()
    async def acquisition_compose_wave(command_id: Identifier, objective_reference: Identifier,
            cycle_id: Identifier, expected_version: Annotated[int, Field(ge=1)]) -> CallToolResult:
        """Compose at most four currently executable candidates under an exact admitted Management objective. Composition does not approve or activate execution. A next wave requires prior Management review and its own exact approval."""
        return await management("CYCLE_CONTROL", command_id, objective_reference,
                                {"cycleId": cycle_id, "operation": "COMPOSE_WAVE", "expectedVersion": expected_version})

    @server.tool()
    async def acquisition_review_wave(command_id: Identifier, objective_reference: Identifier,
            cycle_id: Identifier, wave_id: Identifier, expected_version: Annotated[int, Field(ge=1)],
            decision: Literal["CONTINUE", "ADJUST", "STOP"], reason: Annotated[str, Field(min_length=1, max_length=1000)]) -> CallToolResult:
        """Record an exact admitted Management review. CONTINUE/ADJUST never approves the next composition. STOP preserves opportunities and blocks progression. Cannot change policy, limits or channels."""
        return await management("CYCLE_CONTROL", command_id, objective_reference,
                                {"cycleId": cycle_id, "operation": "REVIEW_WAVE", "waveId": wave_id,
                                 "expectedVersion": expected_version, "decision": decision, "reason": reason})

    @server.tool()
    async def acquisition_request_reconsideration(command_id: Identifier, objective_reference: Identifier,
            cycle_id: Identifier, account_id: Identifier, expected_version: Annotated[int, Field(ge=1)],
            operation: Literal["DEEPER_RESEARCH", "RESEARCH_AGAIN", "RECONSIDER_HYPOTHESIS", "RECONSIDER_BUYER"],
            reason: Annotated[str, Field(min_length=1, max_length=1000)], wave_id: Identifier | None = None) -> CallToolResult:
        """Request new bounded research under an exact Management objective, preserving prior decisions/evidence. Does not override qualification or declare a buyer. Existing WorkItem/Signal path only."""
        return await management("REQUEST_RECONSIDERATION", command_id, objective_reference,
                                {"cycleId": cycle_id, "accountId": account_id, "expectedVersion": expected_version,
                                 "operation": operation, "reason": reason}, wave_id)

    @server.tool()
    async def acquisition_stop_discovery(command_id: Identifier, objective_reference: Identifier,
            cycle_id: Identifier, expected_version: Annotated[int, Field(ge=1)],
            reason: Annotated[str, Field(min_length=1, max_length=1000)]) -> CallToolResult:
        """Stop further admissions with a durable reason under an exact admitted objective. 75 is a maximum, never a quota. Existing Accounts and research remain intact."""
        return await management("CYCLE_CONTROL", command_id, objective_reference,
                                {"cycleId": cycle_id, "operation": "STOP_DISCOVERY", "expectedVersion": expected_version, "reason": reason})

    @server.tool()
    async def acquisition_list_crm(cycle_id: Identifier, account_id: Identifier | None = None,
            status: Literal["READY", "IN_PROGRESS", "SYNCED", "RECONCILIATION_REQUIRED", "BLOCKED"] | None = None,
            limit: Annotated[int, Field(ge=1, le=50)] = 20, after: Identifier | None = None) -> CallToolResult:
        """Read authoritative bounded CRM mappings, association observations and handoff/authority transfer. Engine state is not a mirror of HubSpot commercial activity. Resolve Account IDs through bounded Account lookup; never query HubSpot directly."""
        return await portal_request("GET", f"/cycles/{cycle_id}/crm", params={"accountId": account_id, "status": status, "limit": limit, "after": after})

    @server.tool()
    async def acquisition_request_pre_outbound_sync(command_id: Identifier, objective_reference: Identifier,
            cycle_id: Identifier, account_id: Identifier, wave_id: Identifier | None = None) -> CallToolResult:
        """Request exact CRM boundary admission under an admitted Management objective. Engine derives all readiness from PostgreSQL. May enqueue controlled synthetic Company/Contact work; no Deal, outreach or commercial activation. No caller-supplied eligibility or provider IDs."""
        return await management("REQUEST_PRE_OUTBOUND_SYNC", command_id, objective_reference, {"cycleId": cycle_id, "accountId": account_id}, wave_id)

    @server.tool()
    async def acquisition_request_crm_reconciliation(command_id: Identifier, objective_reference: Identifier,
            intent_id: Identifier, expected_version: Annotated[int, Field(ge=1)], wave_id: Identifier | None = None) -> CallToolResult:
        """Request CRM reconciliation under exact admitted objective/version. Lookup first; uncertain creates cannot be blindly repeated. No mapping reassignment or generic property patch."""
        return await management("REQUEST_CRM_RECONCILIATION", command_id, objective_reference, {"intentId": intent_id, "expectedVersion": expected_version}, wave_id)

    @server.tool()
    async def acquisition_review_commercial_handoff(command_id: Identifier, objective_reference: Identifier,
            handoff_id: Identifier, decision: Literal["RECOMMEND", "DECLINE"],
            reason: Annotated[str, Field(min_length=1, max_length=1000)]) -> CallToolResult:
        """Management review of QUESTION/UNKNOWN under admitted objective. Recommendation is not acceptance, opportunity or Deal creation. Preserve actual Agent provenance."""
        return await management("REVIEW_COMMERCIAL_HANDOFF", command_id, objective_reference, {"handoffId": handoff_id, "decision": decision, "reason": reason})

    @server.tool()
    async def acquisition_accept_commercial_handoff(command_id: Identifier, objective_reference: Identifier,
            handoff_id: Identifier, reason: Annotated[str, Field(min_length=1, max_length=1000)]) -> CallToolResult:
        """Accept a supported handoff under an admitted objective. Transfers Human commercial follow-up authority to HubSpot and blocks autonomous pre-Human progression. Never creates a Deal or impersonates Human."""
        return await management("ACCEPT_COMMERCIAL_HANDOFF", command_id, objective_reference, {"handoffId": handoff_id, "reason": reason})

    @server.tool()
    async def acquisition_record_attention_disposition(command_id: Identifier, objective_reference: Identifier,
            attention_id: Identifier, expected_version: Annotated[int, Field(ge=1)],
            disposition: Literal["CONTINUE", "HOLD", "REJECT"], reason: Annotated[str, Field(min_length=1,max_length=1000)],
            notes: Annotated[str, Field(max_length=2000)] = "", wave_id: Identifier | None = None) -> CallToolResult:
        """Management decision under an admitted Human objective, not token-only autonomy. Preserve PANCRACIO_GATEWAY provenance, exact version and retry identity. No gate activation."""
        return await management("RECORD_ATTENTION_DISPOSITION", command_id, objective_reference, {
            "attentionId": attention_id, "expectedVersion": expected_version, "disposition": disposition, "reason": reason, "notes": notes}, wave_id)

    @server.tool()
    async def acquisition_authorize_controlled_effect(command_id: Identifier,
            message_id: Identifier, target_id: Identifier, sender_id: Identifier,
            expected_binding_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")],
            expires_at: Annotated[str, Field(min_length=20,max_length=40)],
            objective_reference: Identifier | None = None, wave_id: Identifier | None = None) -> CallToolResult:
        """Submit one exact Email effect to Engine validation. Under the active Human production mandate, Pancracio may derive a short-lived exact objective; there is never blanket send authority or a provider bypass."""
        exact_objective = objective_reference or f"continuous-effect-{command_id}"
        return await management("AUTHORIZE_EFFECT", command_id, exact_objective, {
            "messageId": message_id, "targetId": target_id, "senderId": sender_id,
            "expectedBindingHash": expected_binding_hash, "expiresAt": expires_at}, wave_id)

    @server.tool()
    async def acquisition_request_effect_reconciliation(command_id: Identifier, objective_reference: Identifier, intent_id: Identifier, wave_id: Identifier | None = None) -> CallToolResult:
        """Request bounded lookup-only reconciliation under an admitted objective. Never resend UNKNOWN work or reset total attempts."""
        return await management("REQUEST_EFFECT_RECONCILIATION", command_id, objective_reference, {"intentId": intent_id}, wave_id)

    @server.tool()
    async def acquisition_acknowledge_effect_attention(command_id: Identifier, objective_reference: Identifier,
            attention_id: Identifier, expected_version: Annotated[int, Field(ge=1)],
            reason: Annotated[str, Field(min_length=1,max_length=1000)], wave_id: Identifier | None = None) -> CallToolResult:
        """Acknowledge bounded Management Attention under an admitted objective. Does not send, approve commercial outreach or hand off CRM ownership."""
        return await management("ACKNOWLEDGE_EFFECT_ATTENTION", command_id, objective_reference, {
            "attentionId": attention_id, "expectedVersion": expected_version, "reason": reason}, wave_id)

    @server.tool()
    async def acquisition_edit_message_draft(command_id: Identifier, objective_reference: Identifier,
            cycle_id: Identifier, account_id: Identifier, expected_attention_version: Annotated[int, Field(ge=1)],
            source_key: Literal["clean", "unresolved", "claim-trap", "suppressed", "ambiguous", "stale", "guessed"],
            edit_text: Annotated[str, Field(min_length=1,max_length=4000)], wave_id: Identifier | None = None) -> CallToolResult:
        """Create a new synthetic draft version under an admitted objective. Claims are revalidated; an edit is never approval or send authority."""
        return await management("EDIT_MESSAGE_DRAFT", command_id, objective_reference, {
            "cycleId": cycle_id, "accountId": account_id, "expectedAttentionVersion": expected_attention_version,
            "sourceKey": source_key, "editText": edit_text}, wave_id)

    @server.tool()
    async def acquisition_get_effect(intent_id: Identifier) -> CallToolResult:
        """Read bounded authoritative effect status, exact authorization and observations; do not infer authorization from readiness."""
        return await portal_request("GET", f"/effects/{intent_id}")

    @server.tool()
    async def acquisition_get_effect_bindings(message_id: Identifier) -> CallToolResult:
        """Read exact current controlled bindings (maximum 25). Eligible is not permission to execute."""
        return await portal_request("GET", f"/messages/{message_id}/controlled-test-bindings")

    @server.tool()
    async def acquisition_list_effect_attention(cycle_id: Identifier) -> CallToolResult:
        """Read shared-capacity effect Attention (maximum 25), including items waiting for capacity."""
        return await portal_request("GET", f"/cycles/{cycle_id}/effect-attention")

    @server.tool()
    async def acquisition_get_buyer_dry_run(cycle_id: Identifier, account_id: Identifier) -> CallToolResult:
        """Read Engine buyer/contact evidence, messageability, claims and last ten immutable drafts. PREVIEW_ONLY is never send authority; stale packages are not READY."""
        return await portal_request("GET", f"/cycles/{cycle_id}/accounts/{account_id}/buyer-dry-run")

    @server.tool()
    async def acquisition_request_buyer_dry_run(command_id: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")],
                                               cycle_id: Identifier, account_id: Identifier,
                                               expected_attention_version: Annotated[int, Field(ge=1)],
                                               source_key: Literal["clean", "unresolved", "claim-trap", "suppressed", "ambiguous", "stale", "guessed"]) -> CallToolResult:
        """Request synthetic manual-source Buyer/Message dry run only after authorized CONTINUE/current priority. No arbitrary person/email or send. Edits use the separate objective-governed Management tool. Retry exact command ID; acceptance is not completion."""
        return await portal_request("POST", "/commands/request-buyer-dry-run", body={
            "commandId": command_id, "cycleId": cycle_id, "accountId": account_id,
            "expectedAttentionVersion": expected_attention_version, "sourceKey": source_key,
        })

    @server.tool()
    async def acquisition_list_cycles(status: Literal["DESIGN", "CALIBRATION", "ACTIVE", "REVIEW", "CLOSED"] | None = None,
                                      limit: PageLimit = 25, cursor: Cursor | None = None) -> CallToolResult:
        """Resolve Cycle IDs/state/counts from bounded authoritative pages."""
        return await portal_request("GET", "/cycles", params=locals())

    @server.tool()
    async def acquisition_get_cycle(cycle_id: Identifier) -> CallToolResult:
        """Read the exact Engine Cycle summary."""
        return await portal_request("GET", f"/cycles/{cycle_id}")

    @server.tool()
    async def acquisition_list_accounts(cycle_id: Identifier, limit: PageLimit = 25, cursor: Cursor | None = None,
                                        stage: Literal["DISCOVERED", "RESEARCHING", "RESEARCH_DECIDED"] | None = None,
                                        outcome: Literal["QUALIFIED", "HOLD", "REJECTED"] | None = None) -> CallToolResult:
        """Resolve Account IDs by bounded summaries. Follow cursors; no unbounded dump."""
        return await portal_request("GET", f"/cycles/{cycle_id}/accounts",
                                    params={"limit": limit, "cursor": cursor, "stage": stage, "outcome": outcome})

    @server.tool()
    async def acquisition_get_account(cycle_id: Identifier, account_id: Identifier) -> CallToolResult:
        """Preserve OBSERVED_FACT, SUPPORTED_INFERENCE, WORKING_HYPOTHESIS, UNKNOWN and CONFLICT_OR_STALE separately; do not flatten epistemic truth."""
        return await portal_request("GET", f"/cycles/{cycle_id}/accounts/{account_id}")

    @server.tool()
    async def acquisition_list_priorities(cycle_id: Identifier) -> CallToolResult:
        """Engine ordered top 50; preserve tier, why-now, factors, limits and unknowns verbatim."""
        return await portal_request("GET", f"/cycles/{cycle_id}/priorities")

    @server.tool()
    async def acquisition_list_attention(cycle_id: Identifier, active_only: bool = True) -> CallToolResult:
        """Engine ordered bounded Attention (max 50); active_only selects active capacity."""
        return await portal_request("GET", f"/cycles/{cycle_id}/attention", params={"active": str(active_only).lower()})

    @server.tool()
    async def acquisition_get_attention(attention_id: Identifier) -> CallToolResult:
        """Read Attention rationale/version and Human disposition without changing it."""
        return await portal_request("GET", f"/attention/{attention_id}")

    @server.tool()
    async def acquisition_get_attention_counts(cycle_id: Identifier) -> CallToolResult:
        """Read authoritative active/overflow semantics and configured capacity."""
        return await portal_request("GET", f"/cycles/{cycle_id}/attention-counts")

    @server.tool()
    async def acquisition_list_work(cycle_id: Identifier | None = None, limit: PageLimit = 25, cursor: Cursor | None = None,
                                    state: Literal["QUEUED", "WORKING", "COMPLETED", "BLOCKED", "FAILED", "CANCELLED"] | None = None) -> CallToolResult:
        """Bounded authoritative work status, not permission to complete or mutate work."""
        return await portal_request("GET", "/work", params={"cycleId": cycle_id, "limit": limit, "cursor": cursor, "state": state})

    @server.tool()
    async def acquisition_get_health() -> CallToolResult:
        """Read Engine/PostgreSQL health, pending work and safety gates; no activation."""
        return await portal_request("GET", "/engine-health")

    @server.tool()
    async def acquisition_request_research(command_id: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")],
                                            cycle_id: Identifier, account_id: Identifier,
                                            expected_lifecycle_version: Annotated[int, Field(ge=1)],
                                            reason: Annotated[str, Field(min_length=1, max_length=1000)]) -> CallToolResult:
        """Request existing Engine research only when authorized. Reuse the exact command ID/payload for uncertain retries; conflict requires reading current truth. Acceptance is not completion. Management dispositions use their separate governed tool."""
        return await portal_request("POST", "/commands/request-account-research", body={
            "schemaVersion": "1", "commandId": command_id, "cycleId": cycle_id, "accountId": account_id,
            "expectedLifecycleVersion": expected_lifecycle_version, "reason": reason,
        })
