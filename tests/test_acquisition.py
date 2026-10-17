import asyncio
import json

import httpx

from app import acquisition
from app.auth.principals import Principal, bind_principal, reset_principal
from app.mcp_server import mcp_server, _tool_authorization_error


def unpack(result):
    return json.loads(result.content[0].text)


def configure(monkeypatch, handler):
    monkeypatch.setenv("ACQUISITION_PORTAL_ORIGIN", "https://portal.example.test")
    monkeypatch.setenv("ACQUISITION_PORTAL_SERVICE_TOKEN", "synthetic_service_token_not_a_real_secret_123456789")
    client = httpx.AsyncClient
    monkeypatch.setattr(acquisition.httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))


def test_exact_truth_auth_and_no_interpretation(monkeypatch):
    truth = {"schemaVersion": "1", "items": [{"epistemicState": tag} for tag in
             ["OBSERVED_FACT", "SUPPORTED_INFERENCE", "WORKING_HYPOTHESIS", "UNKNOWN", "CONFLICT_OR_STALE"]]}
    def handler(request):
        assert request.headers["authorization"].startswith("Bearer synthetic_")
        assert "cookie" not in request.headers
        assert request.url.path == "/api/acquisition/v1/cycles/c/accounts/a"
        return httpx.Response(200, json=truth)
    configure(monkeypatch, handler)
    result = asyncio.run(mcp_server.call_tool("acquisition_get_account", {"cycle_id": "c", "account_id": "a"}))
    assert unpack(result)["data"] == truth


def test_business_lookup_preserves_ambiguity_without_mutation(monkeypatch):
    calls = []
    truth = {"resolution": "CLARIFICATION_REQUIRED", "truncated": False, "candidates": [
        {"display_name": "Acme Logistics", "canonical_domain": "mx.synthetic.local", "country": "MX"},
        {"display_name": "Acme Logistics", "canonical_domain": "us.synthetic.local", "country": "US"}]}
    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.url.params["name"] == "Acme"
        return httpx.Response(200, json=truth)
    configure(monkeypatch, handler)
    result = asyncio.run(mcp_server.call_tool("acquisition_resolve_accounts", {"cycle_id": "synthetic-7c", "name": "Acme"}))
    assert unpack(result)["data"] == truth
    assert len(calls) == 1
    assert asyncio.run(mcp_server.call_tool("acquisition_resolve_accounts", {"cycle_id": "synthetic-7c", "name": "x"})).is_error
    assert len(calls) == 1


def test_effect_wave_binding_survives_outer_allowlist_without_actor_spoof(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.path.endswith('/management-waves/wave-exact/actions/AUTHORIZE_EFFECT')
        assert json.loads(request.content)['objectiveReference'] == 'exact-wave-objective'
        return httpx.Response(403, json={'code': 'CONTROLLED_TEST_POLICY_REQUIRED'})
    configure(monkeypatch, handler)
    args = dict(command_id='synthetic', objective_reference='exact-wave-objective', wave_id='wave-exact',
                message_id='message-exact', target_id='target-exact', sender_id='sender-exact',
                expected_binding_hash='a'*64, expires_at='2026-09-08T00:00:00Z')
    result = asyncio.run(mcp_server.call_tool('acquisition_authorize_controlled_effect', args))
    assert unpack(result)['httpStatus'] == 403
    assert len(calls) == 1
    assert asyncio.run(mcp_server.call_tool('acquisition_authorize_controlled_effect', args | {'caller_type':'HUMAN_PORTAL'})).is_error
    assert len(calls) == 1


def test_cycle_pool_policy_review_and_wave_scope_are_portal_contracts(monkeypatch):
    calls = []
    truth = {"schemaVersion": "1", "review": {"pool": {"RETAINED": 71, "READY_NOW": 4},
             "zeroResponseMeansFailure": False, "productionExecution": "DISABLED"}}
    def handler(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=truth)
        return httpx.Response(403, json={"code": "ACCOUNT_OUTSIDE_APPROVED_WAVE", "unsafe": "never expose upstream text"})
    configure(monkeypatch, handler)
    for name, args, path in [
        ("acquisition_get_cycle_policy", {}, "/cycle-policy"),
        ("acquisition_get_cycle_review", {"cycle_id": "synthetic-7c"}, "/cycles/synthetic-7c/review"),
        ("acquisition_list_opportunity_pool", {"cycle_id": "synthetic-7c", "limit": 50}, "/cycles/synthetic-7c/pool"),
    ]:
        assert unpack(asyncio.run(mcp_server.call_tool(name, args)))["data"] == truth
        assert calls[-1].url.path.endswith(path)
    count = len(calls)
    assert asyncio.run(mcp_server.call_tool("acquisition_list_opportunity_pool", {"cycle_id": "c", "limit": 51})).is_error
    assert len(calls) == count
    denied = unpack(asyncio.run(mcp_server.call_tool("acquisition_request_pre_outbound_sync", {
        "command_id": "synthetic-command", "objective_reference": "exact-wave-objective", "wave_id": "wave-1",
        "cycle_id": "c", "account_id": "outside"})))
    assert calls[-1].url.path.endswith("/management-waves/wave-1/actions/REQUEST_PRE_OUTBOUND_SYNC")
    assert json.loads(calls[-1].content) == {"commandId": "synthetic-command", "objectiveReference": "exact-wave-objective", "request": {"cycleId": "c", "accountId": "outside"}}
    assert denied["httpStatus"] == 403
    assert denied["data"]["code"] == "ACCOUNT_OUTSIDE_APPROVED_WAVE"
    assert "unsafe" not in denied["data"]


def test_bounds_prohibited_tools_and_scoped_principals(monkeypatch):
    def unexpected(request):
        raise AssertionError("must not call Portal")
    configure(monkeypatch, unexpected)
    assert "acquisition_manage_company_profile" in acquisition.TOOLS
    assert "acquisition_direct_candidate_enrichment" in acquisition.TOOLS
    assert "acquisition_review_candidate_enrichment_route" in acquisition.TOOLS
    assert "acquisition_record_candidate_link_observation" in acquisition.TOOLS
    assert _tool_authorization_error(Principal.management(), "acquisition_direct_candidate_enrichment") is None
    assert _tool_authorization_error(Principal.management(), "acquisition_review_candidate_enrichment_route") is None
    assert _tool_authorization_error(Principal.management(), "acquisition_record_candidate_link_observation") is None
    assert _tool_authorization_error(Principal.management(), "acquisition_manage_company_profile") is None
    assert "acquisition_reconcile_company_identity" in acquisition.TOOLS
    assert _tool_authorization_error(Principal.management(), "acquisition_reconcile_company_identity") is None
    from dataclasses import replace
    developer = replace(Principal.management(), type="developer")
    assert _tool_authorization_error(developer, "acquisition_manage_company_profile") == "tool_denied"
    assert _tool_authorization_error(developer, "acquisition_direct_candidate_enrichment") == "tool_denied"
    assert _tool_authorization_error(developer, "acquisition_review_candidate_enrichment_route") == "tool_denied"
    assert _tool_authorization_error(developer, "acquisition_record_candidate_link_observation") == "tool_denied"
    assert _tool_authorization_error(developer, "acquisition_reconcile_company_identity") == "tool_denied"
    for name in ["acquisition_sql", "acquisition_patch", "acquisition_set_priority", "acquisition_send"]:
        assert _tool_authorization_error(Principal.management(), name) == "tool_denied"
        assert asyncio.run(mcp_server.call_tool(name, {})).is_error
    assert asyncio.run(mcp_server.call_tool("acquisition_list_cycles", {"limit": 101})).is_error
    assert asyncio.run(mcp_server.call_tool("acquisition_get_cycle", {"cycle_id": "../auth/session"})).is_error
    principal = replace(Principal.management(), type="developer")
    bound = bind_principal(principal)
    try:
        assert asyncio.run(mcp_server.call_tool("acquisition_get_health", {})).is_error
        assert unpack(asyncio.run(acquisition.portal_request("GET", "/engine-health")))["httpStatus"] == 403
    finally:
        reset_principal(bound)


def test_candidate_web_first_direction_preserves_context_and_rejects_source_uri(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == "POST"
        assert request.url.path.endswith("/exploration/enrichment-directions")
        return httpx.Response(202, json={"status": "QUEUED"})
    configure(monkeypatch, handler)
    args = dict(command_id="synthetic-c3", cycle_id="cycle", company_id="company",
                link_id="selected-link", field_key="FUNCTION_IN_LINK", dimension="WEB_FIRST",
                question="What original source confirms the selected function?",
                expected_stage_version=1, reason="Resolve the selected candidate's missing function.")
    result = unpack(asyncio.run(mcp_server.call_tool("acquisition_direct_candidate_enrichment", args)))
    assert result["httpStatus"] == 202
    assert json.loads(calls[0].content) == {"commandId": "synthetic-c3", "request": {
        "cycleId": "cycle", "companyId": "company", "linkId": "selected-link",
        "fieldKey": "FUNCTION_IN_LINK", "dimension": "WEB_FIRST",
        "question": args["question"], "expectedStageVersion": 1, "reason": args["reason"]}}
    reviewed = args | {"command_id": "synthetic-c3-after-review",
                       "screening_review_attempt_id": "prior-no-signal",
                       "screening_review_reason": "The prior search found no usable company fact; this question tests a distinct source."}
    assert unpack(asyncio.run(mcp_server.call_tool("acquisition_direct_candidate_enrichment", reviewed)))["httpStatus"] == 202
    assert json.loads(calls[1].content)["request"]["screeningReviewAttemptId"] == "prior-no-signal"
    assert json.loads(calls[1].content)["request"]["screeningReviewReason"] == reviewed["screening_review_reason"]
    rejected = asyncio.run(mcp_server.call_tool("acquisition_direct_candidate_enrichment",
                                            args | {"source_uri": "https://example.com"}))
    assert rejected.is_error
    assert len(calls) == 2


def test_early_screening_review_preserves_candidate_and_source_receipts(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(202, json={"status": "recorded"})
    configure(monkeypatch, handler)
    hashes = ["a" * 64, "b" * 64]
    args = dict(command_id="screen-fixture", cycle_id="cycle", company_id="candidate",
                link_id="selected-link", based_on_attempt_id="complete-web-read",
                expected_stage_version=2, source_receipt_hashes=hashes,
                reason="Both original pages refer to other companies and supply no verified identity or access route.")
    result = unpack(asyncio.run(mcp_server.call_tool("acquisition_review_candidate_early_screening", args)))
    assert result["httpStatus"] == 202
    assert calls[0].url.path.endswith("/exploration/early-screening-reviews")
    assert json.loads(calls[0].content)["request"] == {
        "cycleId": "cycle", "companyId": "candidate", "linkId": "selected-link",
        "basedOnAttemptId": "complete-web-read", "expectedStageVersion": 2,
        "sourceReceiptHashes": hashes, "reason": args["reason"]}


def test_fit_review_keeps_selected_link_and_official_sources(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(202, json={"status": "recorded", "externalEffect": False})
    configure(monkeypatch, handler)
    args = dict(command_id="fit-fixture", cycle_id="cycle", company_id="candidate",
                link_id="selected-link", expected_stage_version=3, decision="ADVANCE",
                process="Verified freezing process", brunova_capability="Operational intelligence systems",
                proposed_offer="Explore monitoring of the freezing process",
                fit_hypothesis="A monitoring layer could improve visibility of the observed process.",
                commercial_question="How do you monitor the process today?",
                uncertainty="The company's internal priorities are unknown.",
                counterevidence="No public contradiction was found in reviewed sources.",
                company_source_url="https://company.example/process",
                brunova_source_url="https://brunova.mx/",
                reason="The cited process and published Brunova capability support an exploratory question.")
    result = unpack(asyncio.run(mcp_server.call_tool("acquisition_review_candidate_fit", args)))
    assert result["httpStatus"] == 202
    assert calls[0].url.path.endswith("/exploration/fit-reviews")
    payload = json.loads(calls[0].content)["request"]
    assert payload["companyId"] == "candidate" and payload["linkId"] == "selected-link"
    assert payload["companySourceUrl"] == args["company_source_url"]
    invalid = asyncio.run(mcp_server.call_tool("acquisition_review_candidate_fit",
                                               args | {"brunova_source_url": "https://elsewhere.example/"}))
    assert invalid.is_error and len(calls) == 1


def test_fit_copy_revision_preserves_review_and_stage_context(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(202, json={"status": "recorded", "externalEffect": False})
    configure(monkeypatch, handler)
    args = dict(command_id="copy-revision", company_id="candidate", link_id="selected-link",
                review_id="original-review", expected_stage_version=4,
                proposed_offer="Si el relevo de congelación requiere estados compartidos, Brunova puede definir el registro y sus responsables.",
                fit_hypothesis="La empresa declara congelación rápida; la necesidad de mejorar el relevo sigue por validar.",
                reason="El texto anterior era una oferta genérica y no explicaba el valor para esta empresa.")
    result = unpack(asyncio.run(mcp_server.call_tool("acquisition_revise_candidate_fit_copy", args)))
    assert result["httpStatus"] == 202
    assert calls[0].url.path.endswith("/exploration/fit-copy-revisions")
    payload = json.loads(calls[0].content)["request"]
    assert payload["reviewId"] == "original-review" and payload["expectedStageVersion"] == 4
    assert payload["proposedOffer"] == args["proposed_offer"]


def test_candidate_attention_contact_and_filtering_keep_exact_governed_context(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(202, json={"status": "recorded", "externalEffect": False})
    configure(monkeypatch, handler)
    method = dict(command_id="contact-one", cycle_id="cycle", company_id="candidate",
                  expected_stage_version=4, channel_type="EMAIL", channel_value="info@example.test",
                  source_url="https://example.test/contact",
                  attribution="The company's own contact page names this address.")
    assert unpack(asyncio.run(mcp_server.call_tool("acquisition_record_candidate_contact_method", method)))["httpStatus"] == 202
    assert calls[0].url.path.endswith("/exploration/contact-methods")
    assert json.loads(calls[0].content)["request"]["companyId"] == "candidate"
    attention = dict(command_id="attention-one", cycle_id="cycle", company_id="candidate",
                     link_id="selected-link", expected_stage_version=4, action="RESOLVE",
                     reason_code="NO_VERIFIED_CONTACT", summary="A verified contact method was found.",
                     evidence_refs=["https://example.test/contact"],
                     resolution="The official contact page attributes this address to the candidate.",
                     resolution_action="REGISTER_CONTACT",
                     contact_method_id="contact-one")
    assert unpack(asyncio.run(mcp_server.call_tool("acquisition_record_candidate_attention", attention)))["httpStatus"] == 202
    assert calls[1].url.path.endswith("/exploration/attention/commands")
    assert json.loads(calls[1].content)["request"]["contactMethodId"] == "contact-one"
    decision = dict(command_id="select-one", cycle_id="cycle", company_id="candidate",
                    link_id="selected-link", expected_stage_version=4, decision="SELECT",
                    reason="The observed process and attributed thesis warrant a conversation.",
                    source_url="https://example.test/process")
    assert unpack(asyncio.run(mcp_server.call_tool("acquisition_record_candidate_filtering_decision", decision)))["httpStatus"] == 202
    assert calls[2].url.path.endswith("/exploration/filtering/decisions")
    assert json.loads(calls[2].content)["request"]["linkId"] == "selected-link"
    research = dict(command_id="research-one", cycle_id="cycle", company_id="candidate",
                    link_id="selected-link", expected_stage_version=4,
                    reason_code="NO_VERIFIED_CONTACT",
                    question="Which official public channel belongs to this company?", max_minutes=15)
    assert unpack(asyncio.run(mcp_server.call_tool("acquisition_request_candidate_attention_research", research)))["httpStatus"] == 202
    assert calls[3].url.path.endswith("/exploration/attention/research-requests")
    bad = asyncio.run(mcp_server.call_tool("acquisition_record_candidate_filtering_decision",
                                           decision | {"source_url": "http://example.test/process"}))
    assert bad.is_error and len(calls) == 4


def test_candidate_route_review_preserves_exact_attempt(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == "POST"
        assert request.url.path.endswith("/exploration/enrichment-route-reviews")
        return httpx.Response(202, json={"status": "RECORDED"})
    configure(monkeypatch, handler)
    args = dict(command_id="synthetic-review", cycle_id="cycle", company_id="company",
                link_id="selected-link", field_key="FUNCTION_IN_LINK", dimension="WEB_FIRST",
                based_on_attempt_id="attempt-one", expected_stage_version=2,
                reason="Original source was read and did not resolve the field.")
    result = unpack(asyncio.run(mcp_server.call_tool("acquisition_review_candidate_enrichment_route", args)))
    assert result["httpStatus"] == 202
    assert json.loads(calls[0].content) == {"commandId": "synthetic-review", "request": {
        "cycleId": "cycle", "companyId": "company", "linkId": "selected-link",
        "fieldKey": "FUNCTION_IN_LINK", "dimension": "WEB_FIRST",
        "basedOnAttemptId": "attempt-one", "expectedStageVersion": 2,
        "reason": args["reason"], "reviewMode": "SOURCE_READ"}}
    no_source = unpack(asyncio.run(mcp_server.call_tool("acquisition_review_candidate_enrichment_route",
                                  args | {"command_id": "synthetic-no-source", "review_mode": "NO_ATTRIBUTED_SOURCE"})))
    assert no_source["httpStatus"] == 202
    assert json.loads(calls[1].content)["request"]["reviewMode"] == "NO_ATTRIBUTED_SOURCE"
    assert asyncio.run(mcp_server.call_tool("acquisition_review_candidate_enrichment_route",
                                     args | {"based_on_attempt_id": ""})).is_error
    assert len(calls) == 2


def test_candidate_link_observation_requires_an_original_quote_and_reconciled_member(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.path.endswith("/exploration/candidate-link-observations")
        return httpx.Response(202, json={"status": "recorded"})
    configure(monkeypatch, handler)
    args = dict(command_id="synthetic-link-review", cycle_id="cycle", company_id="canonical",
                member_company_id="member", link_id="storage-link", based_on_attempt_id="source-attempt",
                source_url="https://example.com/official", excerpt="We operate monitored cold storage.",
                statement="The company declares monitored cold storage operation.",
                reason="The original company page explicitly describes this function.",
                expected_stage_version=2, expected_map_version=9)
    result = unpack(asyncio.run(mcp_server.call_tool("acquisition_record_candidate_link_observation", args)))
    assert result["httpStatus"] == 202
    assert json.loads(calls[0].content) == {"commandId": "synthetic-link-review", "request": {
        "cycleId": "cycle", "companyId": "canonical", "memberCompanyId": "member",
        "linkId": "storage-link", "basedOnAttemptId": "source-attempt",
        "sourceUrl": "https://example.com/official", "excerpt": args["excerpt"],
        "statement": args["statement"], "reason": args["reason"],
        "expectedStageVersion": 2, "expectedMapVersion": 9}}
    assert asyncio.run(mcp_server.call_tool("acquisition_record_candidate_link_observation",
                                           args | {"source_url": "http://example.com/official"})).is_error
    assert len(calls) == 1


def test_retries_preserve_command_envelope_and_conflicts(monkeypatch):
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert "callerId" not in body and "callerType" not in body
        assert request.headers["x-correlation-id"] == body["commandId"]
        if len(calls) == 3:
            return httpx.Response(409, json={"problem": {"code": "STALE_AGGREGATE_VERSION", "detail": "private sensitive text"}})
        return httpx.Response(202, json={"commandStatus": "accepted" if len(calls) == 1 else "already_applied", "workItemId": "same-work"})
    configure(monkeypatch, handler)
    args = {"command_id": "synthetic-command", "cycle_id": "c", "account_id": "a", "expected_lifecycle_version": 1, "reason": "synthetic"}
    first = unpack(asyncio.run(mcp_server.call_tool("acquisition_request_research", args)))
    second = unpack(asyncio.run(mcp_server.call_tool("acquisition_request_research", args)))
    assert first["data"]["workItemId"] == second["data"]["workItemId"]
    assert calls[0] == calls[1]
    conflict = unpack(asyncio.run(mcp_server.call_tool("acquisition_request_research", args)))
    assert conflict["httpStatus"] == 409
    assert conflict["data"]["code"] == "STALE_AGGREGATE_VERSION"
    assert "sensitive" not in str(conflict)


def test_unavailable_invalid_auth_and_redirects_fail_without_fallback(monkeypatch):
    for status in [401, 403, 422, 429, 500, 503, 302]:
        configure(monkeypatch, lambda request: httpx.Response(status, json={"code": "FORBIDDEN", "detail": "secret"}))
        result = asyncio.run(acquisition.portal_request("GET", "/engine-health"))
        assert result.is_error
        assert unpack(result)["httpStatus"] == status
        assert "secret" not in str(result)
        monkeypatch.undo()
    monkeypatch.delenv("ACQUISITION_PORTAL_SERVICE_TOKEN", raising=False)
    assert unpack(asyncio.run(acquisition.portal_request("GET", "/engine-health")))["httpStatus"] == 503


def test_discovery_response_ceiling_accepts_bounded_payload_and_rejects_oversize(monkeypatch):
    audit_records = []
    monkeypatch.setattr(acquisition, "emit_audit_record", lambda **kwargs: audit_records.append(kwargs))
    def bounded(request):
        assert request.url.path == "/api/acquisition/v1/discovery"
        return httpx.Response(200, json={"history": "x" * 1_574_454})
    configure(monkeypatch, bounded)
    accepted = unpack(asyncio.run(mcp_server.call_tool("acquisition_get_discovery", {})))
    assert accepted["httpStatus"] == 200
    assert len(accepted["data"]["history"]) == 1_574_454
    monkeypatch.undo()
    monkeypatch.setattr(acquisition, "emit_audit_record", lambda **kwargs: audit_records.append(kwargs))

    def oversized(request):
        return httpx.Response(200, json={"history": "x" * 2_646_646})
    configure(monkeypatch, oversized)
    rejected = unpack(asyncio.run(mcp_server.call_tool("acquisition_get_discovery", {})))
    assert rejected["httpStatus"] == 503
    assert rejected["data"]["code"] == "ACQUISITION_UNAVAILABLE"
    assert "error_reason" not in rejected["data"]
    assert audit_records[-1]["error_reason"] == "response_too_large"


def test_discovery_invalid_json_is_classified_only_in_audit(monkeypatch):
    audit_records = []
    monkeypatch.setattr(acquisition, "emit_audit_record", lambda **kwargs: audit_records.append(kwargs))
    configure(monkeypatch, lambda request: httpx.Response(200, text="not-json"))
    result = unpack(asyncio.run(mcp_server.call_tool("acquisition_get_discovery", {})))
    assert result["httpStatus"] == 503
    assert result["data"]["code"] == "ACQUISITION_UNAVAILABLE"
    assert "error_reason" not in result["data"]
    assert audit_records[-1]["error_reason"] == "invalid_json"


def test_exploration_map_preserves_output_arrays_and_direct_exploration_forwards_contract(monkeypatch):
    calls = []
    map_result = {
        "schemaVersion": "1",
        "industries": [{"id": "industry-1"}],
        "companies": [{"id": "company-1", "epistemicState": "UNVERIFIED"}],
        "companyProfiles": [{
            "companyId": "company-1", "cycleId": "cycle-1", "displayName": "Example Co",
            "identityStatus": "UNRESOLVED", "aliases": [], "leadNodeIds": ["company-1"],
            "fields": [{"key": "activity", "dimension": "activity", "value": "Food distribution",
                        "status": "HYPOTHESIS", "sourceUrl": "https://example.test/about",
                        "observedAt": "2026-10-13T10:00:00Z", "nextQuestion": "Which regions?"}],
            "tags": ["regional"],
            "links": [{"linkId": "link-1", "market": "MX", "function": "distributor",
                       "evidenceStatus": "UNVERIFIED"}],
            "disposition": [{"scope": "LINK", "branchId": "link-1", "status": "DISCARDED",
                             "reason": "Not supported by available evidence.", "actorId": "human-1",
                             "updatedAt": "2026-10-13T10:00:00Z"}],
        }],
        "pendingWork": [{"id": "work-1"}],
        "provenance": [{"source": "synthetic"}],
        "chainCoverage": [{"id": "coverage-1"}],
        "governance": [{"id": "governance-1"}],
        "marketPresence": [{"id": "presence-1"}],
        "chainEquivalences": [{"id": "equivalence-1"}],
    }

    def handler(request):
        calls.append(request)
        if request.method == "GET":
            assert request.url.path.endswith("/exploration/map")
            assert dict(request.url.params) == {"cycleId": "cycle-1", "market": "MX"}
            return httpx.Response(200, json=map_result)
        assert request.method == "POST"
        assert request.url.path.endswith("/exploration/commands")
        return httpx.Response(202, json={"commandStatus": "accepted"})

    configure(monkeypatch, handler)
    result = unpack(asyncio.run(mcp_server.call_tool("acquisition_get_exploration_map", {
        "cycle_id": "cycle-1", "market": "MX",
    })))
    assert result["data"] == map_result
    assert result["data"]["companyProfiles"] == map_result["companyProfiles"]
    assert all(isinstance(result["data"][key], list) for key in (
        "industries", "companies", "companyProfiles", "pendingWork", "provenance", "chainCoverage",
        "governance", "marketPresence", "chainEquivalences",
    ))

    args = {
        "command_id": "explore-command-1",
        "cycle_id": "cycle-1",
        "market": "US",
        "operation": "EXPLORE",
        "target_kind": "SPECIALTY",
        "direction": "Explore a bounded synthetic specialty branch.",
        "expected_version": 3,
        "policy_hash": "a" * 64,
        "node_id": "specialty-1",
    }
    posted = unpack(asyncio.run(mcp_server.call_tool("acquisition_direct_exploration", args)))
    assert posted["httpStatus"] == 202
    request = calls[-1]
    assert json.loads(request.content) == {
        "commandId": "explore-command-1",
        "request": {
            "cycleId": "cycle-1", "market": "US", "operation": "EXPLORE",
            "targetKind": "SPECIALTY", "direction": args["direction"],
            "expectedVersion": 3, "policyHash": "a" * 64, "nodeId": "specialty-1",
        },
    }
    call_count = len(calls)
    invalid = args | {"operation": "CONTACT_PROSPECT"}
    assert asyncio.run(mcp_server.call_tool("acquisition_direct_exploration", invalid)).is_error
    assert len(calls) == call_count


def test_direct_exploration_forwards_governed_link_operations_and_rejects_missing_fields(monkeypatch):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(202, json={"commandStatus": "accepted"})

    configure(monkeypatch, handler)
    common = {"command_id": "explore-op", "cycle_id": "cycle-1", "market": "MX",
              "expected_version": 4, "policy_hash": "b" * 64}
    cases = [
        ({"operation": "RECORD_LINK_COVERAGE", "chain_id": "chain-1", "coverage_market": "US",
          "status": "EXHAUSTED", "directive_ids": ["directive-1"], "open_gaps": ["No independent source"],
          "reason": "The bounded source search did not establish additional links."},
         {"chainId": "chain-1", "coverageMarket": "US", "status": "EXHAUSTED",
          "directiveIds": ["directive-1"], "openGaps": ["No independent source"],
          "reason": "The bounded source search did not establish additional links."}),
        ({"operation": "LINK_MARKETS", "chain_id": "chain-1", "counterpart_id": "chain-us-1",
          "other_expected_version": 2, "reason": "Independent evidence supports this cross-market equivalence."},
         {"chainId": "chain-1", "counterpartId": "chain-us-1", "otherExpectedVersion": 2,
          "reason": "Independent evidence supports this cross-market equivalence."}),
        ({"operation": "RECORD_PRESENCE", "node_id": "company-1", "presence_market": "US",
          "status": "OBSERVED", "source_url": "https://example.test/presence",
          "reason": "The cited public page explicitly lists this location."},
         {"nodeId": "company-1", "presenceMarket": "US", "status": "OBSERVED",
          "sourceUrl": "https://example.test/presence",
          "reason": "The cited public page explicitly lists this location."}),
    ]
    for index, (specific, expected) in enumerate(cases):
        args = common | specific | {"command_id": f"explore-op-{index}"}
        result = unpack(asyncio.run(mcp_server.call_tool("acquisition_direct_exploration", args)))
        assert result["httpStatus"] == 202
        envelope = calls[-1]
        assert envelope["commandId"] == args["command_id"]
        assert envelope["request"] == {
            "cycleId": "cycle-1", "market": "MX", "operation": specific["operation"],
            "expectedVersion": 4, "policyHash": "b" * 64, **expected,
        }

    count = len(calls)
    missing = common | {"operation": "RECORD_PRESENCE", "node_id": "company-1",
                        "presence_market": "US", "status": "UNKNOWN"}
    assert asyncio.run(mcp_server.call_tool("acquisition_direct_exploration", missing)).is_error
    assert len(calls) == count
    observed_without_source = common | {"operation": "RECORD_PRESENCE", "node_id": "company-1",
                                       "presence_market": "US", "status": "OBSERVED",
                                       "reason": "The cited public page explicitly lists this location."}
    assert asyncio.run(mcp_server.call_tool("acquisition_direct_exploration", observed_without_source)).is_error
    assert len(calls) == count


def test_company_profile_commands_forward_only_bounded_engine_contract(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "POST"
        assert request.url.path.endswith("/exploration/company/commands")
        return httpx.Response(202, json={"status": "accepted", "externalEffect": False})

    configure(monkeypatch, handler)
    common = {
        "command_id": "profile-command-1", "cycle_id": "cycle-1", "market": "MX",
        "company_id": "company-1", "expected_version": 9,
    }
    field = {
        "operation": "SET_FIELD", "field_key": "activity", "field_dimension": "activity",
        "field_status": "HYPOTHESIS", "field_value": "Regional food distribution",
        "field_source_url": "https://example.test/about", "field_observed_at": "2026-10-13T10:00:00Z",
        "field_next_question": "Which regions are served?",
    }
    result = unpack(asyncio.run(mcp_server.call_tool("acquisition_manage_company_profile", common | field)))
    assert result["httpStatus"] == 202
    assert json.loads(calls[-1].content) == {
        "commandId": "profile-command-1",
        "request": {
            "cycleId": "cycle-1", "market": "MX", "operation": "SET_FIELD",
            "companyId": "company-1", "expectedVersion": 9,
            "field": {
                "key": "activity", "dimension": "activity", "value": "Regional food distribution",
                "status": "HYPOTHESIS", "sourceUrl": "https://example.test/about",
                "observedAt": "2026-10-13T10:00:00Z", "nextQuestion": "Which regions are served?",
            },
        },
    }

    tags = common | {"command_id": "profile-command-2", "operation": "SET_TAGS",
                     "tags": ["exporter", "regional"]}
    result = unpack(asyncio.run(mcp_server.call_tool("acquisition_manage_company_profile", tags)))
    assert result["httpStatus"] == 202
    assert json.loads(calls[-1].content) == {
        "commandId": "profile-command-2",
        "request": {
            "cycleId": "cycle-1", "market": "MX", "operation": "SET_TAGS",
            "companyId": "company-1", "expectedVersion": 9,
            "tags": ["exporter", "regional"],
        },
    }

    count = len(calls)
    for invalid in [
        common | {"operation": "DELETE_COMPANY"},
        common | {"operation": "SET_FIELD", "field_key": "madeUp", "field_dimension": "activity",
                  "field_status": "KNOWN", "field_value": "Unknown field key"},
        common | {"operation": "SET_FIELD", "field_key": "activity", "field_dimension": "identity",
                  "field_status": "KNOWN", "field_value": "Wrong dimension"},
        common | {"operation": "SET_TAGS", "tags": ["x" * 81]},
    ]:
        assert asyncio.run(mcp_server.call_tool("acquisition_manage_company_profile", invalid)).is_error
    assert len(calls) == count


def test_company_identity_commands_use_fixed_engine_route_and_version_receipts(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "POST"
        assert request.url.path.endswith("/exploration/company/commands")
        return httpx.Response(202, json={"status": "accepted", "externalEffect": False})

    configure(monkeypatch, handler)
    common = {"cycle_id": "cycle-1", "reason": "Official registry identifies the exact organization."}
    cases = [
        ({"command_id": "identity-link", "operation": "LINK_LEADS",
          "canonical_company_id": "lead-mx", "member_company_id": "lead-us",
          "expected_version_mx": 4, "expected_version_us": 7,
          "source_url": "https://registry.example.test/company/1"},
         {"cycleId": "cycle-1", "operation": "LINK_LEADS",
          "canonicalCompanyId": "lead-mx", "memberCompanyId": "lead-us",
          "reason": "Official registry identifies the exact organization.",
          "sourceUrl": "https://registry.example.test/company/1",
          "expectedVersions": {"MX": 4, "US": 7}}),
        ({"command_id": "identity-unlink", "operation": "UNLINK_LEAD",
          "canonical_company_id": "lead-mx", "member_company_id": "lead-us",
          "expected_version_mx": 5, "expected_version_us": 8},
         {"cycleId": "cycle-1", "operation": "UNLINK_LEAD",
          "canonicalCompanyId": "lead-mx", "memberCompanyId": "lead-us",
          "reason": "Official registry identifies the exact organization.",
          "expectedVersions": {"MX": 5, "US": 8}}),
        ({"command_id": "identity-split", "operation": "SPLIT_ALIAS",
          "company_id": "lead-us", "expected_version_us": 9,
          "source_url": "https://registry.example.test/company/2"},
         {"cycleId": "cycle-1", "operation": "SPLIT_ALIAS", "companyId": "lead-us",
          "reason": "Official registry identifies the exact organization.",
          "sourceUrl": "https://registry.example.test/company/2",
          "expectedVersions": {"US": 9}}),
    ]
    for args, expected_request in cases:
        result = unpack(asyncio.run(mcp_server.call_tool(
            "acquisition_reconcile_company_identity", common | args)))
        assert result["httpStatus"] == 202
        assert json.loads(calls[-1].content) == {
            "commandId": args["command_id"], "request": expected_request,
        }

    count = len(calls)
    invalid_requests = [
        common | {"command_id": "invalid-no-evidence", "operation": "LINK_LEADS",
                  "canonical_company_id": "lead-mx", "member_company_id": "lead-us",
                  "expected_version_mx": 1, "expected_version_us": 1},
        common | {"command_id": "invalid-unlink-source", "operation": "UNLINK_LEAD",
                  "canonical_company_id": "lead-mx", "member_company_id": "lead-us",
                  "expected_version_mx": 1, "source_url": "https://example.test"},
        common | {"command_id": "invalid-split-version", "operation": "SPLIT_ALIAS",
                  "company_id": "lead-us", "expected_version_mx": 1,
                  "expected_version_us": 2, "source_url": "https://example.test"},
        common | {"command_id": "invalid-operation", "operation": "MERGE_BY_NAME",
                  "canonical_company_id": "lead-mx", "member_company_id": "lead-us",
                  "expected_version_mx": 1, "source_url": "https://example.test"},
    ]
    for invalid in invalid_requests:
        assert asyncio.run(mcp_server.call_tool("acquisition_reconcile_company_identity", invalid)).is_error
    assert len(calls) == count


def test_signal_accepts_only_canonical_service_provenance():
    from tests.test_agent_signals import acquisition_dict
    from app.agent_signals import AgentSignalPayload
    import pytest
    body = acquisition_dict()
    body.update(actor_type="PANCRACIO_GATEWAY", actor_id="pancracio:gateway")
    assert AgentSignalPayload.model_validate(body).actor_type == "PANCRACIO_GATEWAY"
    body["actor_id"] = "spoof"
    with pytest.raises(ValueError):
        AgentSignalPayload.model_validate(body)


def test_buyer_dry_run_is_bounded_indirect_and_never_human_edit_or_send(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        if request.method == "POST":
            body = json.loads(request.content)
            assert set(body) == {"commandId", "cycleId", "accountId", "expectedAttentionVersion", "sourceKey"}
            return httpx.Response(409, json={"problemCode": "STALE_ATTENTION_VERSION"})
        return httpx.Response(200, json={"package": {"current": False, "messageability": "HOLD", "preview": {"status": "PREVIEW_ONLY", "executable": False}}})
    configure(monkeypatch, handler)
    args = {"command_id": "buyer-test", "cycle_id": "c", "account_id": "a", "expected_attention_version": 2, "source_key": "clean"}
    result = unpack(asyncio.run(mcp_server.call_tool("acquisition_request_buyer_dry_run", args)))
    assert result["httpStatus"] == 409
    assert result["data"]["code"] == "STALE_ATTENTION_VERSION"
    before = len(calls)
    for extra in [{"email": "guess@example.com"}, {"edit_text": "Human impersonation"}, {"caller_type": "HUMAN_PORTAL"}]:
        assert asyncio.run(mcp_server.call_tool("acquisition_request_buyer_dry_run", args | extra)).is_error
    assert len(calls) == before
    result = unpack(asyncio.run(mcp_server.call_tool("acquisition_get_buyer_dry_run", {"cycle_id": "c", "account_id": "a"})))
    assert result["data"]["package"]["preview"]["executable"] is False


def test_management_capability_objective_provenance_and_worker_boundary(monkeypatch):
    from dataclasses import replace
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert request.url.path.endswith('/management-actions/RECORD_ATTENTION_DISPOSITION')
        assert set(body) == {'commandId', 'objectiveReference', 'request'}
        assert body['objectiveReference'] == 'approved-objective'
        assert body['request']['disposition'] == 'CONTINUE'
        return httpx.Response(403, json={'code': 'APPLICABLE_MANAGEMENT_AUTHORIZATION_REQUIRED'})
    configure(monkeypatch, handler)
    name = 'acquisition_record_attention_disposition'
    args = dict(command_id='management-command', objective_reference='approved-objective', attention_id='item', expected_version=1, disposition='CONTINUE', reason='Approved scope')
    assert _tool_authorization_error(Principal.management(), name) is None
    assert unpack(asyncio.run(mcp_server.call_tool(name, args)))['httpStatus'] == 403
    assert unpack(asyncio.run(mcp_server.call_tool(name, args)))['httpStatus'] == 403
    assert calls[0] == calls[1]
    assert asyncio.run(mcp_server.call_tool(name, args | {'caller_type': 'HUMAN_PORTAL'})).is_error
    worker = replace(Principal.management(), type='signal_worker')
    for tool in acquisition.TOOLS:
        assert _tool_authorization_error(worker, tool) is not None
    token = bind_principal(worker)
    try:
        assert asyncio.run(mcp_server.call_tool(name, args)).is_error
    finally:
        reset_principal(token)
    assert len(calls) == 2


def test_all_management_tools_forward_only_narrow_contract(monkeypatch):
    cases = [
        ('acquisition_prepare_conversation', 'DISCOVERY_CONTROL', dict(cycle_id='c',candidate_id='candidate-1',expected_version=0,evidence_observation_ids=['observation-1'],proposal={'signalKind':'OTHER','hypothesis':'Synthetic evidence-bound hypothesis.'})),
        ('acquisition_bind_candidate_message', 'DISCOVERY_CONTROL', dict(proposal_id='proposal-1',expected_buyer_result_id='buyer-1',expected_message_version=1)),
        ('acquisition_archive_candidate', 'DISCOVERY_CONTROL', dict(candidate_id='candidate-1',reason='Remove from the active workspace while preserving evidence.')),
        ('acquisition_restore_candidate', 'DISCOVERY_CONTROL', dict(candidate_id='candidate-1',reason='Return to the active workspace for bounded review.')),
        ('acquisition_request_authenticated_research', 'DISCOVERY_CONTROL', dict(cycle_id='c',policy_hash='a'*64,uri='https://www.linkedin.com/feed/',surface='LINKEDIN',evidence_expected='Authenticated feed navigation is rendered read-only.')),
        ('acquisition_record_commercial_calibration', 'DISCOVERY_CONTROL', dict(cycle_id='c',policy_hash='a'*64,model_version='pancracio-calibration-v1',reviews=[{'candidateId':'candidate-1','internalNeed':'UNKNOWN'}])),
        ('acquisition_record_work_plan', 'DISCOVERY_CONTROL', dict(cycle_id='c',policy_hash='a'*64,expires_at='2026-09-17T12:00:00Z',rationale='Resolve the highest-value current uncertainty first.',inventory={'candidates':1},items=[{'workClass':'COMPANY_ENRICH'}])),
        ('acquisition_approve_conversation_worthiness', 'DISCOVERY_CONTROL', dict(cycle_id='c',policy_hash='a'*64,semantic_version='1',definition={'internalNeedDefault':'UNKNOWN'})),
        ('acquisition_approve_work_plan', 'DISCOVERY_CONTROL', dict(cycle_id='c',policy_hash='a'*64,plan_id='plan-1',semantic_version='1',choices=[{'position':1}])),
        ('acquisition_record_allocation_outcomes', 'DISCOVERY_CONTROL', dict(cycle_id='c',policy_hash='a'*64,plan_id='plan-1',outcomes=[{'position':1}])),
        ('acquisition_record_exploratory_wave', 'DISCOVERY_CONTROL', dict(cycle_id='c',wave_id='wave-1',version=1,evidence_snapshot={'candidateCount':16},all_candidates=[{'candidateId':str(i)} for i in range(16)],targets=[{'position':1}])),
        ('acquisition_record_exploratory_wave_review', 'DISCOVERY_CONTROL', dict(wave_id='wave-1',review={'effectsAuthorized':False})),
        ('acquisition_begin_discovery_batch', 'DISCOVERY_CONTROL', dict(cycle_id='c',batch_id='batch2',policy_hash='a'*64,baseline_hash='b'*64,maximum_new=15,direction='Explore new companies while retaining prior uncertainty.')),
        ('acquisition_finish_discovery_batch', 'DISCOVERY_CONTROL', dict(cycle_id='c',batch_id='batch2',policy_hash='a'*64,reason='Stop for explicit second-batch Management review.')),
        ('acquisition_request_pre_outbound_sync', 'REQUEST_PRE_OUTBOUND_SYNC', dict(cycle_id='c',account_id='a')),
        ('acquisition_request_crm_reconciliation', 'REQUEST_CRM_RECONCILIATION', dict(intent_id='i',expected_version=2)),
        ('acquisition_review_commercial_handoff', 'REVIEW_COMMERCIAL_HANDOFF', dict(handoff_id='h',decision='RECOMMEND',reason='Synthetic review')),
        ('acquisition_accept_commercial_handoff', 'ACCEPT_COMMERCIAL_HANDOFF', dict(handoff_id='h',reason='Synthetic acceptance')),
        ('acquisition_authorize_controlled_effect', 'AUTHORIZE_EFFECT', dict(message_id='m',target_id='t',sender_id='s',expected_binding_hash='a'*64,expires_at='2026-09-06T18:00:00.000Z')),
        ('acquisition_request_effect_reconciliation', 'REQUEST_EFFECT_RECONCILIATION', dict(intent_id='i')),
        ('acquisition_acknowledge_effect_attention', 'ACKNOWLEDGE_EFFECT_ATTENTION', dict(attention_id='a',expected_version=2,reason='Reviewed')),
        ('acquisition_edit_message_draft', 'EDIT_MESSAGE_DRAFT', dict(cycle_id='c',account_id='a',expected_attention_version=2,source_key='clean',edit_text='Synthetic draft')),
    ]
    for name, operation, args in cases:
        def handler(request):
            assert request.url.path.endswith('/management-actions/' + operation)
            body = json.loads(request.content)
            assert body['objectiveReference'] == 'admitted'
            assert 'actorType' not in body and 'callerId' not in body
            return httpx.Response(409, json={'code': 'STALE_ATTENTION_VERSION', 'detail': 'private'})
        configure(monkeypatch, handler)
        result = unpack(asyncio.run(mcp_server.call_tool(name, args | dict(command_id='cmd',objective_reference='admitted'))))
        assert result['httpStatus'] == 409 and result['data']['code'] == 'STALE_ATTENTION_VERSION'
        assert 'private' not in str(result)
        monkeypatch.undo()
