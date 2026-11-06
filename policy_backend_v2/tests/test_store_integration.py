"""Local PostgreSQL integration; set PANKY_V2_TEST_DSN for a disposable DB."""

import os
from datetime import datetime, timezone

import psycopg
import pytest

from policy_backend_v2.decision import Request
from policy_backend_v2.models import Policy
from policy_backend_v2.publication import activate
from policy_backend_v2.store import PolicyStoreV2


@pytest.fixture
def db():
    dsn = os.getenv("PANKY_V2_TEST_DSN")
    if not dsn:
        pytest.skip("Disposable PostgreSQL DSN not configured")
    with psycopg.connect(dsn) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
    with psycopg.connect(dsn) as conn:
        conn.execute(open(os.path.join(os.path.dirname(__file__), "..", "schema.sql"),
                          encoding="utf-8").read())
    return dsn


def example():
    at = datetime(2026, 11, 1, tzinfo=timezone.utc)
    policy = Policy.model_validate({
        "schema_version": 2, "tenant": "brunova",
        "actors": [{"actor_id": "act_jorge"}, {"actor_id": "act_nat"},
                   {"actor_id": "act_dev_001"}],
        "context_nodes": [
            {"node_id": "ctx_root", "parent_node_id": None, "kind": "department", "visibility": "inherit"},
            {"node_id": "ctx_hq", "parent_node_id": "ctx_root", "kind": "department", "visibility": "inherit"},
            {"node_id": "ctx_finance", "parent_node_id": "ctx_root", "kind": "department", "visibility": "inherit"},
            {"node_id": "ctx_nat", "parent_node_id": "ctx_root", "kind": "seat", "visibility": "owner"},
            {"node_id": "ctx_dev", "parent_node_id": "ctx_hq", "kind": "seat", "visibility": "owner"},
            {"node_id": "ctx_fin", "parent_node_id": "ctx_finance", "kind": "seat", "visibility": "owner"},
            {"node_id": "ctx_budget", "parent_node_id": "ctx_finance", "kind": "subcontext", "visibility": "restricted"},
        ],
        "departments": [
            {"code": "management", "name": "Management", "context_node_id": "ctx_root"},
            {"code": "hq", "name": "HQ", "parent": "management", "context_node_id": "ctx_hq"},
            {"code": "finance", "name": "Finance", "parent": "management", "context_node_id": "ctx_finance"},
        ],
        "roles": [
            {"code": "manager", "name": "Manager", "operations": [{"tool": "workspace", "operation": "read"}, {"tool": "workspace", "operation": "move"}]},
            {"code": "developer", "name": "Developer", "operations": [{"tool": "workspace", "operation": "read"}, {"tool": "workspace", "operation": "move"}]},
            {"code": "contributor", "name": "Contributor", "operations": [{"tool": "workspace", "operation": "read"}]},
        ],
        "seats": [
            {"code": "management_01", "department": "management", "role": "manager", "context_node_id": "ctx_nat", "occupants": ["act_nat"]},
            {"code": "dev_01", "department": "hq", "role": "developer", "context_node_id": "ctx_dev", "occupants": ["act_dev_001"]},
            {"code": "finance_contributor", "department": "finance", "role": "contributor", "context_node_id": "ctx_fin", "occupants": ["act_dev_001"]},
        ],
        "tools": [{"code": "workspace", "provider": "workspace", "operations": ["read", "move", "delete"]}],
        "resources": [
            {"code": "hq_drive", "tool": "workspace", "resource_kind": "source", "external_ref": "hq_client", "owner_department": "hq", "shared_departments": ["finance"]},
            {"code": "finance_drive", "tool": "workspace", "resource_kind": "source", "external_ref": "finance_drive", "owner_department": "finance"},
        ],
        "context_rules": [
            {"subject_kind": "any_active_seat", "target_class": "department_context", "scope": "department_subtree", "surface": "shared", "operation": "read"},
            {"subject_kind": "role_in_seat", "role": "manager", "target_class": "department_context", "scope": "department_subtree", "surface": "work", "operation": "assign"},
            {"subject_kind": "seat_occupant", "target_class": "seat_context", "scope": "own_seat", "surface": "knowledge", "operation": "update"},
        ],
        "grants": [
            {"subject_kind": "seat", "subject": "finance_contributor", "target_kind": "context_node", "target": "ctx_budget", "surface": "knowledge", "operation": "update", "effect": "allow", "valid_from": at, "max_uses": 1, "reason": "one budget update", "approved_by": "act_jorge"},
            {"subject_kind": "actor", "subject": "act_dev_001", "target_kind": "external_resource", "target": "hq_drive", "operation": "move", "effect": "deny", "valid_from": at, "reason": "no HQ drive moves", "approved_by": "act_jorge"},
        ],
    })
    registry = {"actors": {
        "act_jorge": {"status": "active", "roles": ["super_admin"], "identities": {"google_oidc": {"email": "jorge@example.invalid"}}},
        "act_nat": {"status": "active", "roles": ["management"], "identities": {"google_oidc": {"email": "nat@example.invalid"}}},
        "act_dev_001": {"status": "active", "roles": ["developer"], "identities": {"google_oidc": {"email": "dev@example.invalid"}}},
    }}
    seat_by_node = {seat.context_node_id: seat.code for seat in policy.seats}
    nodes = {n.node_id: {"parent_id": n.parent_node_id,
                         **({"seat_id": seat_by_node[n.node_id]} if n.kind == "seat" else {})}
             for n in policy.context_nodes}
    topology = {"nodes": nodes, "_seats": {
        s.code: {"status": "active", "occupied_by": s.occupants} for s in policy.seats}}
    return policy, registry, topology


def test_publish_oidc_seats_exception_and_revoke(db):
    policy, registry, topology = example()
    activate(db, policy, registry, topology, "a" * 40)
    store = PolicyStoreV2(db)
    assert store.healthy()

    def call(email, subject, actor_id, target_kind, target, operation, surface=None, request_id="req"):
        return store.decide(issuer="https://accounts.google.com", subject=subject,
                            email=email, request=Request(actor_id, target_kind, target, operation, surface),
                            request_id=request_id)

    from policy_backend_v2.publication import stable
    finance = str(stable("resource", "finance_drive"))
    hq = str(stable("resource", "hq_drive"))
    assert call("dev@example.invalid", "oidc-dev", "act_dev_001", "external_resource", finance, "read")["allowed"]
    assert not call("dev@example.invalid", "oidc-dev", "act_dev_001", "external_resource", finance, "move")["allowed"]
    assert call("dev@example.invalid", "oidc-dev", "act_dev_001", "external_resource", hq, "move")["reason"] == "actor_deny"
    assert call("dev@example.invalid", "wrong-sub", "act_dev_001", "external_resource", hq, "read")["reason"] == "subject_mismatch"
    assert call("nat@example.invalid", "oidc-nat", "act_nat", "context_node", "ctx_hq", "assign", "work")["allowed"]
    assert not call("nat@example.invalid", "oidc-nat", "act_nat", "context_node", "ctx_hq", "delete", "knowledge")["allowed"]
    first = call("dev@example.invalid", "oidc-dev", "act_dev_001", "context_node", "ctx_budget", "update", "knowledge", "budget1")
    assert first["allowed"] and first["reservation_id"]
    assert store.complete_use(first["reservation_id"], "budget1", "committed")
    second = call("dev@example.invalid", "oidc-dev", "act_dev_001", "context_node", "ctx_budget", "update", "knowledge", "budget2")
    assert not second["allowed"]
    preview_after_use = store.decide(
        issuer="https://accounts.google.com", subject="oidc-dev",
        email="dev@example.invalid",
        request=Request("act_dev_001", "context_node", "ctx_budget", "update", "knowledge"),
        request_id="catalog-budget", preview=True,
    )
    assert not preview_after_use["allowed"]

    updated = policy.model_copy(deep=True)
    updated.roles[2].operations.clear()
    activate(db, updated, registry, topology, "b" * 40)
    assert not call("dev@example.invalid", "oidc-dev", "act_dev_001", "external_resource", finance, "read")["allowed"]


def test_http_contract_ignores_preview_flag_on_real_action(db):
    from fastapi.testclient import TestClient
    from policy_backend.app import require_gateway
    from policy_backend_v2.app import app, store

    policy, registry, topology = example()
    activate(db, policy, registry, topology, "c" * 40)
    app.dependency_overrides[require_gateway] = lambda: None
    app.dependency_overrides[store] = lambda: PolicyStoreV2(db)
    try:
        client = TestClient(app)
        body = {
            "actor": {"issuer": "https://accounts.google.com",
                      "subject": "oidc-dev", "email": "dev@example.invalid"},
            "provider": "workspace", "tool": "workspace_move_document",
            "resource_kind": "source", "resource_ref": "hq_client",
            "operation": "read", "request_id": "http-real", "preview": True,
        }
        result = client.post("/v2/decide", json=body)
        assert result.status_code == 200
        assert result.json()["allowed"]
        with psycopg.connect(db) as conn:
            assert conn.execute("SELECT count(*) FROM panky_audit_events WHERE actor_id='act_dev_001'").fetchone()[0] == 1
        assert client.get("/v2/health").json() == {"status": "ready", "model": "2.0"}
    finally:
        app.dependency_overrides.clear()
