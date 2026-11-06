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
            {"subject_kind": "any_active_seat", "target_class": "department_context", "scope": "department_subtree", "surface": "knowledge", "operation": "read"},
            {"subject_kind": "any_active_seat", "target_class": "department_context", "scope": "department_subtree", "surface": "state", "operation": "read"},
            {"subject_kind": "any_active_seat", "target_class": "department_context", "scope": "department_subtree", "surface": "work", "operation": "read"},
            {"subject_kind": "role_in_seat", "role": "manager", "target_class": "department_context", "scope": "department_subtree", "surface": "knowledge", "operation": "update"},
            {"subject_kind": "role_in_seat", "role": "manager", "target_class": "department_context", "scope": "department_subtree", "surface": "work", "operation": "assign"},
            {"subject_kind": "seat_occupant", "target_class": "seat_context", "scope": "own_seat", "surface": "knowledge", "operation": "read"},
            {"subject_kind": "seat_occupant", "target_class": "seat_context", "scope": "own_seat", "surface": "state", "operation": "read"},
            {"subject_kind": "seat_occupant", "target_class": "seat_context", "scope": "own_seat", "surface": "work", "operation": "read"},
            {"subject_kind": "seat_occupant", "target_class": "seat_context", "scope": "own_seat", "surface": "knowledge", "operation": "update"},
            {"subject_kind": "seat_occupant", "target_class": "seat_context", "scope": "own_seat", "surface": "contributions", "operation": "create"},
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
    nodes = {n.node_id: {"parent_id": n.parent_node_id, "owner_class": "organization",
                         "backing_bindings": [{"store_id": f"store_{n.node_id}",
                                               "repository": f"Pancracio-s/repo-{n.node_id}",
                                               "generation": 1,
                                               "mode": "authoritative",
                                               "local_path": f"domains/nodes/{n.node_id}"}],
                         **({"seat_id": seat_by_node[n.node_id]} if n.kind == "seat" else {})}
             for n in policy.context_nodes}
    topology = {"nodes": nodes, "_seats": {
        s.code: {"status": "active", "occupied_by": s.occupants} for s in policy.seats}}
    return policy, registry, topology


def test_catalog_follows_release_and_oidc_scope(db):
    policy, registry, topology = example()
    topology["nodes"]["ctx_hq"]["backing_bindings"].append({
        "store_id": "hq_delivery", "repository": "Pancracio-s/repo-hq-delivery",
        "generation": 1, "mode": "generated", "local_path": "domains/publications/hq"})
    topology["nodes"]["ctx_dev"]["backing_bindings"].append({
        "store_id": "dev_delivery", "repository": "Pancracio-s/repo-dev-delivery",
        "generation": 1, "mode": "generated", "local_path": "domains/publications/dev"})
    activate(db, policy, registry, topology, "f" * 40)
    store = PolicyStoreV2(db)
    catalog = store.context_catalog(issuer="https://accounts.google.com",
                                    subject="oidc-dev", email="dev@example.invalid")
    assert catalog["status"] == "ready"
    assert catalog["release"] == "f" * 40
    visible = {row["node_id"]: row for row in catalog["nodes"]}
    assert "ctx_dev" in visible
    assert "ctx_nat" not in visible
    assert visible["ctx_dev"]["repository"] == "Pancracio-s/repo-ctx_dev"
    assert visible["ctx_dev"]["binding_mode"] == "authoritative"
    assert "delivery" not in visible["ctx_dev"]
    assert visible["ctx_hq"]["repository"] == "Pancracio-s/repo-hq-delivery"
    assert visible["ctx_hq"]["binding_mode"] == "generated"
    nat = store.context_catalog(issuer="https://accounts.google.com",
                                subject="oidc-nat", email="nat@example.invalid")
    nat_nodes = {row["node_id"]: row for row in nat["nodes"]}
    assert nat_nodes["ctx_hq"]["repository"] == "Pancracio-s/repo-ctx_hq"
    assert nat_nodes["ctx_hq"]["binding_mode"] == "authoritative"
    assert store.context_catalog(issuer="https://accounts.google.com",
                                 subject="another-subject", email="dev@example.invalid")["status"] == "denied"


def test_gateway_to_backend_catalog_end_to_end(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app as gateway_app
    from app.auth.panky_bridge import issue_access_token
    from app.auth import panky_bridge
    from policy_backend.app import require_gateway
    from policy_backend_v2.app import app as backend_app, store as backend_store

    policy, registry, topology = example()
    activate(db, policy, registry, topology, "9" * 40)
    backend_app.dependency_overrides[require_gateway] = lambda: None
    backend_app.dependency_overrides[backend_store] = lambda: PolicyStoreV2(db)
    monkeypatch.setenv("BRUNOVA_GATEWAY_TOKEN", "legacy-fixture-token-long-enough-123456")
    monkeypatch.setenv("PANKY_POLICY_MODE", "shadow")
    monkeypatch.setenv("PANKY_POLICY_V2_MODE", "enforce")
    monkeypatch.setenv("PANKY_GATEWAY_SIGNING_KEY", "x" * 40)
    monkeypatch.setenv("PANKY_DESKTOP_OAUTH_CLIENT_ID", "fixture-client")
    monkeypatch.setenv("PANKY_POLICY_BACKEND_URL", "https://policy.example.invalid")
    monkeypatch.setattr(panky_bridge.id_token, "verify_oauth2_token",
                        lambda proof, request, audience: {
                            "iss": "https://accounts.google.com", "aud": audience,
                            "sub": "dev-sub", "email": "dev@example.invalid",
                            "email_verified": True,
                        })
    monkeypatch.setattr(panky_bridge.id_token, "fetch_id_token",
                        lambda request, audience: "synthetic-service-proof")
    backend_client = TestClient(backend_app)
    monkeypatch.setattr(panky_bridge.httpx, "post",
                        lambda url, **kwargs: backend_client.post(
                            url.removeprefix("https://policy.example.invalid"),
                            json=kwargs["json"], headers=kwargs.get("headers")))
    try:
        bearer, _ = issue_access_token("synthetic-google-proof")
        response = TestClient(gateway_app).get(
            "/panky/context/catalog", headers={"Authorization": f"Bearer {bearer}"})
        assert response.status_code == 200
        assert response.json()["release"] == "9" * 40
        node_ids = {row["node_id"] for row in response.json()["nodes"]}
        assert "ctx_dev" in node_ids
        assert "ctx_nat" not in node_ids
        allowed = TestClient(gateway_app).post(
            "/panky/context/decide",
            headers={"Authorization": f"Bearer {bearer}"},
            json={"node_id": "ctx_dev", "surface": "knowledge", "operation": "update",
                  "request_id": "own-seat-write", "preview": False})
        denied = TestClient(gateway_app).post(
            "/panky/context/decide",
            headers={"Authorization": f"Bearer {bearer}"},
            json={"node_id": "ctx_hq", "surface": "knowledge", "operation": "update",
                  "request_id": "hq-write", "preview": False})
        assert allowed.status_code == 200 and allowed.json()["allowed"] is True
        assert denied.status_code == 200 and denied.json()["allowed"] is False
    finally:
        backend_app.dependency_overrides.clear()


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
    assert call("dev@example.invalid", "oidc-dev", "act_dev_001", "context_node", "ctx_dev", "create", "contributions")["allowed"]
    assert not call("dev@example.invalid", "oidc-dev", "act_dev_001", "context_node", "ctx_hq", "create", "contributions")["allowed"]
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
        unknown = {**body, "resource_ref": "other_source", "request_id": "unknown"}
        dev_unknown = client.post("/v2/decide", json=unknown)
        assert dev_unknown.json()["allowed"] is False
        admin_unknown = client.post("/v2/decide", json={
            **unknown, "actor": {"issuer": "https://accounts.google.com",
                                  "subject": "oidc-admin", "email": "jorge@example.invalid"},
            "request_id": "admin-unknown",
        })
        assert admin_unknown.json()["reason"] == "super_admin"
        with psycopg.connect(db) as conn:
            conn.execute(
                "UPDATE panky_resources SET status='disabled' WHERE external_ref='hq_client'"
            )
        disabled_child = client.post("/v2/decide", json={
            **body,
            "actor": {"issuer": "https://accounts.google.com",
                      "subject": "oidc-admin", "email": "jorge@example.invalid"},
            "resource_kind": "document", "resource_ref": "document-123",
            "parent_source": "hq_client", "request_id": "admin-disabled-child",
        })
        assert disabled_child.json()["reason"] == "resource_disabled"
        wrong_provider = client.post("/v2/decide", json={
            **unknown, "provider": "unregistered", "request_id": "wrong-provider",
        })
        assert wrong_provider.json()["reason"] == "tool_unknown"
        with psycopg.connect(db) as conn:
            assert conn.execute("SELECT count(*) FROM panky_audit_events WHERE actor_id='act_dev_001'").fetchone()[0] == 2
        assert client.get("/v2/health").json() == {"status": "ready", "model": "2.0"}
    finally:
        app.dependency_overrides.clear()


def test_resource_code_rename_keeps_historical_grants_without_duplicate_ref(db):
    from policy_backend_v2.publication import stable

    policy, registry, topology = example()
    activate(db, policy, registry, topology, "d" * 40)
    renamed = policy.model_copy(deep=True)
    old = next(r for r in renamed.resources if r.code == "finance_drive")
    old.code = "finance_drive_v2"
    activate(db, renamed, registry, topology, "e" * 40)
    with psycopg.connect(db) as conn:
        rows = conn.execute(
            "SELECT resource_id,status FROM panky_resources WHERE external_ref='finance_drive'"
        ).fetchall()
    assert set(rows) == {
        (stable("resource", "finance_drive"), "disabled"),
        (stable("resource", "finance_drive_v2"), "active"),
    }
