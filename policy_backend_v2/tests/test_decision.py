from datetime import datetime, timezone

from policy_backend_v2.decision import Request, decide


NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)


def fixture():
    return {
        "active_release_id": "release-2",
        "actors": [
            {"actor_id": "jorge", "status": "active", "super_admin": True},
            {"actor_id": "nat", "status": "active", "super_admin": False},
            {"actor_id": "dev", "status": "active", "super_admin": False},
        ],
        "departments": [
            {"department_id": "management", "parent_department_id": None, "context_node_id": "ctx_root"},
            {"department_id": "hq", "parent_department_id": "management", "context_node_id": "ctx_hq"},
            {"department_id": "finance", "parent_department_id": "management", "context_node_id": "ctx_finance"},
        ],
        "context_nodes": [
            {"node_id": "ctx_root", "parent_node_id": None, "kind": "department", "visibility": "inherit", "lifecycle": "active"},
            {"node_id": "ctx_hq", "parent_node_id": "ctx_root", "kind": "department", "visibility": "inherit", "lifecycle": "active"},
            {"node_id": "ctx_finance", "parent_node_id": "ctx_root", "kind": "department", "visibility": "inherit", "lifecycle": "active"},
            {"node_id": "ctx_dev", "parent_node_id": "ctx_hq", "kind": "seat", "visibility": "owner", "lifecycle": "active"},
            {"node_id": "ctx_finance_seat", "parent_node_id": "ctx_finance", "kind": "seat", "visibility": "owner", "lifecycle": "active"},
            {"node_id": "ctx_budget", "parent_node_id": "ctx_finance", "kind": "subcontext", "visibility": "restricted", "lifecycle": "active"},
            {"node_id": "ctx_runbook", "parent_node_id": "ctx_hq", "kind": "subcontext", "visibility": "inherit", "lifecycle": "active"},
        ],
        "seats": [
            {"seat_id": "s_nat", "code": "management_01", "department_id": "management", "role_id": "manager", "context_node_id": "ctx_root", "status": "active"},
            {"seat_id": "s_dev", "code": "dev_01", "department_id": "hq", "role_id": "developer", "context_node_id": "ctx_dev", "reports_to_seat_id": "s_nat", "status": "active"},
            {"seat_id": "s_finance", "code": "finance_contributor", "department_id": "finance", "role_id": "contributor", "context_node_id": "ctx_finance_seat", "status": "active"},
        ],
        "actor_seats": [
            {"actor_id": "nat", "seat_id": "s_nat", "active_from": NOW, "active_until": None},
            {"actor_id": "dev", "seat_id": "s_dev", "active_from": NOW, "active_until": None},
            {"actor_id": "dev", "seat_id": "s_finance", "active_from": NOW, "active_until": None},
        ],
        "tools": [
            {"tool_id": "workspace", "code": "workspace", "status": "active"},
            {"tool_id": "n8n", "code": "n8n", "status": "active"},
        ],
        "resources": [
            {"resource_id": "hq_drive", "tool_id": "workspace", "status": "active"},
            {"resource_id": "finance_drive", "tool_id": "workspace", "status": "active"},
            {"resource_id": "hq_n8n", "tool_id": "n8n", "status": "active"},
        ],
        "department_resources": [
            {"department_id": "hq", "resource_id": "hq_drive", "relation": "owned"},
            {"department_id": "hq", "resource_id": "hq_n8n", "relation": "owned"},
            {"department_id": "finance", "resource_id": "finance_drive", "relation": "owned"},
            {"department_id": "finance", "resource_id": "hq_drive", "relation": "shared"},
        ],
        "role_operations": [
            {"role_id": "manager", "tool_id": "workspace", "operation": "read"},
            {"role_id": "manager", "tool_id": "workspace", "operation": "move"},
            {"role_id": "developer", "tool_id": "workspace", "operation": "read"},
            {"role_id": "developer", "tool_id": "workspace", "operation": "move"},
            {"role_id": "developer", "tool_id": "n8n", "operation": "call"},
            {"role_id": "contributor", "tool_id": "workspace", "operation": "read"},
        ],
        "context_rules": [
            {"subject_kind": "any_active_seat", "role_id": None, "target_class": "department_context", "scope": "department_subtree", "surface": "shared", "operation": "read", "status": "active"},
            {"subject_kind": "any_active_seat", "role_id": None, "target_class": "department_context", "scope": "department_subtree", "surface": "knowledge", "operation": "read", "status": "active"},
            {"subject_kind": "role_in_seat", "role_id": "manager", "target_class": "department_context", "scope": "department_subtree", "surface": "work", "operation": "assign", "status": "active"},
            {"subject_kind": "role_in_seat", "role_id": "manager", "target_class": "department_context", "scope": "department_subtree", "surface": "knowledge", "operation": "update", "status": "active"},
            {"subject_kind": "role_in_seat", "role_id": "manager", "target_class": "seat_context", "scope": "reporting_subtree", "surface": "knowledge", "operation": "read", "status": "active"},
            {"subject_kind": "seat_occupant", "role_id": None, "target_class": "seat_context", "scope": "own_seat", "surface": "knowledge", "operation": "update", "status": "active"},
        ],
        "grants": [],
    }


def test_parent_manager_can_assign_and_enrich_child_context():
    data = fixture()
    assert decide(data, Request("nat", "context_node", "ctx_hq", "assign", "work"), NOW).allowed
    assert decide(data, Request("nat", "context_node", "ctx_runbook", "update", "knowledge"), NOW).allowed
    assert not decide(data, Request("nat", "context_node", "ctx_hq", "delete", "knowledge"), NOW).allowed


def test_direct_hq_read_is_not_hq_write_and_manager_sees_reporting_seat():
    data = fixture()
    assert decide(data, Request("dev", "context_node", "ctx_hq", "read", "knowledge"), NOW).allowed
    assert not decide(data, Request("dev", "context_node", "ctx_hq", "update", "knowledge"), NOW).allowed
    assert decide(data, Request("dev", "context_node", "ctx_dev", "update", "knowledge"), NOW).allowed
    assert decide(data, Request("nat", "context_node", "ctx_dev", "read", "knowledge"), NOW).allowed
    assert not decide(data, Request("nat", "context_node", "ctx_dev", "update", "knowledge"), NOW).allowed
    assert not decide(data, Request("nat", "context_node", "ctx_finance_seat", "read", "knowledge"), NOW).allowed


def test_two_seats_do_not_mix_role_and_department():
    data = fixture()
    assert decide(data, Request("dev", "external_resource", "finance_drive", "read"), NOW).seat_id == "s_finance"
    assert not decide(data, Request("dev", "external_resource", "finance_drive", "move"), NOW).allowed
    assert decide(data, Request("dev", "external_resource", "hq_n8n", "call"), NOW).seat_id == "s_dev"
    assert not decide(data, Request("dev", "external_resource", "hq_n8n", "read"), NOW).allowed


def test_sibling_resource_requires_explicit_share_and_actor_deny_covers_all_seats():
    data = fixture()
    assert decide(data, Request("dev", "external_resource", "hq_drive", "read"), NOW).allowed
    data["grants"].append({"grant_id": "deny1", "release_id": "release-2", "subject_kind": "actor", "actor_id": "dev", "target_kind": "external_resource", "resource_id": "hq_drive", "operation": "move", "effect": "deny", "valid_from": NOW})
    assert decide(data, Request("dev", "external_resource", "hq_drive", "move"), NOW).reason == "actor_deny"


def test_restricted_subcontext_needs_exception_and_seat_deny_is_local():
    data = fixture()
    request = Request("dev", "context_node", "ctx_budget", "read", "knowledge")
    assert not decide(data, request, NOW).allowed
    data["grants"].append({"grant_id": "allow1", "release_id": "release-2", "subject_kind": "seat", "seat_id": "s_finance", "target_kind": "context_node", "context_node_id": "ctx_budget", "surface": "knowledge", "operation": "read", "effect": "allow", "valid_from": NOW})
    assert decide(data, request, NOW).seat_id == "s_finance"
    data["grants"].append({"grant_id": "deny2", "release_id": "release-2", "subject_kind": "seat", "seat_id": "s_finance", "target_kind": "context_node", "context_node_id": "ctx_budget", "surface": "knowledge", "operation": "read", "effect": "deny", "valid_from": NOW})
    assert not decide(data, request, NOW).allowed


def test_own_seat_context_mutates_but_others_do_not():
    data = fixture()
    assert decide(data, Request("dev", "context_node", "ctx_dev", "update", "knowledge"), NOW).allowed
    assert not decide(data, Request("nat", "context_node", "ctx_dev", "update", "knowledge"), NOW).allowed


def test_super_admin_is_tenant_internal_bypass():
    data = fixture()
    assert decide(data, Request("jorge", "external_resource", "hq_drive", "delete"), NOW).reason == "super_admin"


def test_owned_resource_is_private_to_owner_seat_and_ancestor_admin_until_granted():
    data = fixture()
    for seat in data["seats"]:
        seat["seat_type"] = "admin" if seat["seat_id"] == "s_nat" else "basic"
    data["resources"][0]["owner_seat_id"] = "s_dev"
    data["actors"].append({"actor_id": "peer", "status": "active", "super_admin": False})
    data["seats"].append({"seat_id": "s_peer", "code": "hq_peer", "department_id": "hq",
                          "role_id": "developer", "context_node_id": "ctx_peer",
                          "seat_type": "basic", "status": "active"})
    data["actor_seats"].append({"actor_id": "peer", "seat_id": "s_peer",
                                "active_from": NOW, "active_until": None})
    read = Request("peer", "external_resource", "hq_drive", "read")
    assert not decide(data, read, NOW).allowed
    assert decide(data, Request("dev", "external_resource", "hq_drive", "read"), NOW).seat_id == "s_dev"
    assert decide(data, Request("nat", "external_resource", "hq_drive", "read"), NOW).seat_id == "s_nat"
    data["grants"].append({"grant_id": "share-peer", "release_id": "release-2",
                           "subject_kind": "seat", "seat_id": "s_peer",
                           "target_kind": "external_resource", "resource_id": "hq_drive",
                           "operation": "read", "effect": "allow", "valid_from": NOW})
    assert decide(data, read, NOW).grant_id == "share-peer"
    data["grants"].append({"grant_id": "bad-delete", "release_id": "release-2",
                           "subject_kind": "seat", "seat_id": "s_peer",
                           "target_kind": "external_resource", "resource_id": "hq_drive",
                           "operation": "delete", "effect": "allow", "valid_from": NOW})
    assert not decide(data, Request("peer", "external_resource", "hq_drive", "delete"), NOW).allowed
