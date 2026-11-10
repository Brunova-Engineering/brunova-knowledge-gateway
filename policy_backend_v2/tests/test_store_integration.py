"""Local PostgreSQL integration; set PANKY_V2_TEST_DSN for a disposable DB."""

import os
from datetime import datetime, timezone

import psycopg
import pytest

from policy_backend_v2.decision import Request
from policy_backend_v2.models import Policy, ContextNode, Grant
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


def test_same_active_core_commit_can_retry_publication_safely(db):
    policy, registry, topology = example()
    first = activate(db, policy, registry, topology, "f" * 40)
    assert activate(db, policy, registry, topology, "f" * 40) == first
    with psycopg.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM panky_policy_releases").fetchone()[0] == 1


def test_catalog_follows_release_and_oidc_scope(db):
    policy, registry, topology = example()
    policy = policy.model_copy(deep=True)
    policy.context_nodes.extend([
        ContextNode(node_id="ctx_hq_delivery", parent_node_id="ctx_root",
                    kind="subcontext", visibility="restricted"),
        ContextNode(node_id="ctx_dev_delivery", parent_node_id="ctx_hq",
                    kind="subcontext", visibility="restricted"),
    ])
    for node_id in ("ctx_hq_delivery", "ctx_dev_delivery"):
        policy.grants.append(Grant(subject_kind="seat", subject="dev_01",
            target_kind="context_node", target=node_id, surface="knowledge",
            operation="read", effect="allow", valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
            reason="approved delivery context", approved_by="act_jorge"))
    topology["nodes"]["ctx_hq_delivery"] = {"parent_id": "ctx_root", "owner_class": "organization", "backing_bindings": [{
        "store_id": "hq_delivery", "repository": "Pancracio-s/repo-hq-delivery",
        "generation": 1, "mode": "generated", "local_path": "domains/publications/hq"}]}
    topology["nodes"]["ctx_dev_delivery"] = {"parent_id": "ctx_hq", "owner_class": "organization", "backing_bindings": [{
        "store_id": "dev_delivery", "repository": "Pancracio-s/repo-dev-delivery",
        "generation": 1, "mode": "generated", "local_path": "domains/publications/dev"}]}
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
    assert visible["ctx_hq_delivery"]["repository"] == "Pancracio-s/repo-hq-delivery"
    assert visible["ctx_hq_delivery"]["binding_mode"] == "generated"
    assert visible["ctx_dev_delivery"]["binding_mode"] == "generated"
    assert visible["ctx_hq"]["repository"] == "Pancracio-s/repo-ctx_hq"
    assert visible["ctx_hq"]["binding_mode"] == "authoritative"
    assert visible["ctx_hq"]["read_surfaces"] == ("knowledge", "state", "work")
    nat = store.context_catalog(issuer="https://accounts.google.com",
                                subject="oidc-nat", email="nat@example.invalid")
    nat_nodes = {row["node_id"]: row for row in nat["nodes"]}
    assert nat_nodes["ctx_hq"]["repository"] == "Pancracio-s/repo-ctx_hq"
    assert nat_nodes["ctx_hq"]["binding_mode"] == "authoritative"
    assert store.context_catalog(issuer="https://accounts.google.com",
                                 subject="another-subject", email="dev@example.invalid")["status"] == "denied"

    # A running bridge release can still expose its old delivery column while
    # the Core publication and client package are being switched together.
    with psycopg.connect(db) as conn:
        conn.execute("UPDATE panky_context_nodes SET lifecycle='archived' "
                     "WHERE node_id IN ('ctx_hq_delivery','ctx_dev_delivery')")
        conn.execute("UPDATE panky_context_nodes SET delivery_repository=%s,"
                     "delivery_store_id=%s,delivery_generation=1,delivery_local_path=%s "
                     "WHERE node_id='ctx_hq'",
                     ("Pancracio-s/repo-hq-delivery", "hq_delivery", "domains/publications/hq"))
    legacy = store.context_catalog(issuer="https://accounts.google.com",
                                   subject="oidc-dev", email="dev@example.invalid")
    legacy_nodes = {row["node_id"]: row for row in legacy["nodes"]}
    assert legacy_nodes["ctx_hq"]["repository"] == "Pancracio-s/repo-hq-delivery"
    assert legacy_nodes["ctx_hq"]["binding_mode"] == "generated"
    with psycopg.connect(db) as conn:
        conn.execute("UPDATE panky_actors SET status='disabled' WHERE actor_id='act_dev_001'")
    assert store.context_catalog(issuer="https://accounts.google.com",
                                 subject="oidc-dev", email="dev@example.invalid") == {
        "status": "denied", "reason": "actor_disabled"}


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
        with psycopg.connect(db) as conn:
            conn.execute("UPDATE panky_actors SET status='disabled' WHERE actor_id='act_dev_001'")
        revoked = TestClient(gateway_app).get(
            "/panky/context/catalog", headers={"Authorization": f"Bearer {bearer}"})
        assert revoked.status_code == 200
        assert revoked.json() == {"status": "denied", "reason": "actor_disabled"}
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


def test_v3_owner_admin_explicit_grant_and_role_ceiling(db):
    old, registry, topology = example()
    payload = old.model_dump(mode="python")
    payload["schema_version"] = 3
    payload["founder_actor_id"] = "act_jorge"
    for seat in payload["seats"]:
        seat["seat_type"] = "admin" if seat["code"] == "management_01" else "basic"
    for resource in payload["resources"]:
        resource["owner_seat"] = ("dev_01" if resource["code"] == "hq_drive"
                                  else "finance_contributor")
    policy = Policy.model_validate(payload)
    activate(db, policy, registry, topology, "1" * 40)
    store = PolicyStoreV2(db)
    from policy_backend_v2.publication import stable
    hq = str(stable("resource", "hq_drive"))
    finance = str(stable("resource", "finance_drive"))
    with psycopg.connect(db) as conn:
        conn.execute("DELETE FROM panky_actor_seats WHERE actor_id='act_dev_001' AND seat_id=%s",
                     (stable("seat", "dev_01"),))

    def access(target, operation, actor="act_dev_001", email="dev@example.invalid"):
        return store.decide(issuer="https://accounts.google.com", subject="oidc-" + ("nat" if actor == "act_nat" else "dev"),
                            email=email, request=Request(actor, "external_resource", target, operation),
                            request_id=f"v3-{target}-{operation}-{actor}")

    assert access(finance, "read")["allowed"]  # Own Finance Seat.
    assert not access(hq, "read")["allowed"]  # Shared department is not blanket access.
    assert access(hq, "read", "act_nat", "nat@example.invalid")["allowed"]  # Ancestor admin.

    from policy_backend_v2.governance import GovernanceStore
    grant = {"subject_kind": "seat", "subject": "finance_contributor",
             "target_kind": "external_resource", "target": "hq_drive",
             "operation": "read", "effect": "allow",
             "valid_from": datetime(2020, 1, 1, tzinfo=timezone.utc),
             "reason": "explicit resource share", "approved_by": "act_jorge"}
    payload["grants"].append(grant)
    with pytest.raises(ValueError, match="lacks owner or admin approval"):
        activate(db, Policy.model_validate(payload), registry, topology, "2" * 40)
    governance = GovernanceStore(db)
    with pytest.raises(ValueError, match="recipient_role_denies_operation"):
        governance.request_resource_grant(
            issuer="https://accounts.google.com", subject="jorge-sub",
            email="jorge@example.invalid", resource_id=stable("resource", "hq_drive"),
            recipient_seat_id=stable("seat", "finance_contributor"),
            via_seat_id=stable("seat", "management_01"),
            operation="delete", reason="invalid escalation")
    approved = governance.request_resource_grant(
        issuer="https://accounts.google.com", subject="jorge-sub",
        email="jorge@example.invalid", resource_id=stable("resource", "hq_drive"),
        recipient_seat_id=stable("seat", "finance_contributor"),
        via_seat_id=stable("seat", "management_01"),
        operation="read", reason="explicit resource share")
    assert approved["effective"] is False
    assert governance.approved_for_core()["resource_grants"][0]["resource"] == "hq_drive"
    activate(db, Policy.model_validate(payload), registry, topology, "2" * 40)
    assert governance.approved_for_core()["resource_grants"] == []
    with psycopg.connect(db) as conn:
        conn.execute("DELETE FROM panky_actor_seats WHERE actor_id='act_dev_001' AND seat_id=%s",
                     (stable("seat", "dev_01"),))
    assert access(hq, "read")["allowed"]
    inventory = governance.resource_sharing(
        issuer="https://accounts.google.com", subject="jorge-sub",
        email="jorge@example.invalid", resource_id=stable("resource", "hq_drive"),
        via_seat_id=stable("seat", "management_01"))
    assert inventory["active_grants"] == [{
        "recipient_seat_id": str(stable("seat", "finance_contributor")),
        "recipient_seat": "finance_contributor", "operation": "read"}]
    with pytest.raises(ValueError, match="admin_seat_required"):
        governance.resource_sharing(
            issuer="https://accounts.google.com", subject="oidc-dev",
            email="dev@example.invalid", resource_id=stable("resource", "hq_drive"),
            via_seat_id=stable("seat", "finance_contributor"))
    assert not access(hq, "delete")["allowed"]  # Grant cannot add CRUD to contributor role.
    payload["grants"].pop()
    without_share = Policy.model_validate(payload)
    with pytest.raises(ValueError, match="removal lacks approval"):
        activate(db, without_share, registry, topology, "3" * 40)
    revoked = governance.revoke_resource_grant(
        issuer="https://accounts.google.com", subject="jorge-sub",
        email="jorge@example.invalid", resource_id=stable("resource", "hq_drive"),
        recipient_seat_id=stable("seat", "finance_contributor"),
        via_seat_id=stable("seat", "management_01"), operation="read",
        reason="no longer share this resource")
    assert revoked["status"] == "approved" and revoked["effective"] is False
    assert governance.approved_for_core()["resource_grants"][0]["action"] == "revoke"
    assert access(hq, "read")["allowed"]  # Pending publication changes nothing.
    activate(db, without_share, registry, topology, "3" * 40)
    with psycopg.connect(db) as conn:
        conn.execute("DELETE FROM panky_actor_seats WHERE actor_id='act_dev_001' AND seat_id=%s",
                     (stable("seat", "dev_01"),))
        old_grant = conn.execute(
            "SELECT count(*) FROM panky_grants WHERE resource_id=%s AND effect='allow'",
            (stable("resource", "hq_drive"),)).fetchone()[0]
        active_grant = conn.execute(
            "SELECT count(*) FROM panky_grants g JOIN panky_policy_releases p "
            "ON p.release_id=g.release_id AND p.status='active' "
            "WHERE g.resource_id=%s AND g.effect IN ('allow','deny') "
            "AND g.subject_kind='seat'",
            (stable("resource", "hq_drive"),)).fetchone()[0]
    assert old_grant == 1 and active_grant == 0
    assert governance.resource_sharing(
        issuer="https://accounts.google.com", subject="jorge-sub",
        email="jorge@example.invalid", resource_id=stable("resource", "hq_drive"),
        via_seat_id=stable("seat", "management_01"))["active_grants"] == []
    assert not access(hq, "read")["allowed"]


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


def test_share_request_requires_source_and_destination_admin_and_stays_ineffective(db):
    from policy_backend_v2.governance import GovernanceStore
    from policy_backend_v2.publication import stable
    policy, registry, topology = example()
    data = policy.model_dump(mode="json")
    data["schema_version"] = 3
    data["founder_actor_id"] = "act_jorge"
    for seat in data["seats"]:
        seat["seat_type"] = "admin" if seat["code"] in {"management_01", "finance_contributor"} else "basic"
    for resource in data["resources"]:
        resource["owner_seat"] = "dev_01" if resource["code"] == "hq_drive" else "finance_contributor"
    activate(db, Policy.model_validate(data), registry, topology, "3" * 40)
    governance = GovernanceStore(db)
    proof_dev = {"issuer": "https://accounts.google.com", "subject": "dev-sub",
                 "email": "dev@example.invalid"}
    proof_nat = {"issuer": "https://accounts.google.com", "subject": "nat-sub",
                 "email": "nat@example.invalid"}
    resource_id = stable("resource", "finance_drive")
    target_department_id = stable("department", "hq")
    changed = Policy.model_validate({
        **data, "resources": [
            {**r, "shared_departments": ["hq"]} if r["code"] == "finance_drive" else r
            for r in data["resources"]
        ],
    })
    with pytest.raises(ValueError, match="lacks approval"):
        activate(db, changed, registry, topology, "5" * 40)
    with pytest.raises(ValueError, match="admin_seat_required"):
        governance.request_share(**proof_dev, resource_id=resource_id,
                                 target_department_id=target_department_id,
                                 via_seat_id=stable("seat", "dev_01"), reason="share with HQ")
    pending = governance.request_share(
        **proof_dev, resource_id=resource_id, target_department_id=target_department_id,
        via_seat_id=stable("seat", "finance_contributor"), reason="share with HQ")
    assert pending["status"] == "pending" and pending["effective"] is False
    assert governance.inbox(**proof_dev)["share_requests"] == []
    recipient_inbox = governance.inbox(**proof_nat)["share_requests"]
    assert len(recipient_inbox) == 1
    assert recipient_inbox[0]["target_department"] == "hq"
    assert recipient_inbox[0]["external_ref"] == "finance_drive"
    with pytest.raises(ValueError, match="seat_out_of_scope"):
        governance.decide_share(**proof_dev, request_id=pending["request_id"],
                                via_seat_id=stable("seat", "finance_contributor"), accept=True)
    approved = governance.decide_share(
        **proof_nat, request_id=pending["request_id"],
        via_seat_id=stable("seat", "management_01"), accept=True)
    assert approved["status"] == "approved" and approved["effective"] is False
    assert governance.inbox(**proof_nat)["share_requests"] == []
    assert governance.approved_for_core()["shares"][0]["target_department"] == "hq"
    with psycopg.connect(db) as conn:
        assert conn.execute(
            "SELECT 1 FROM panky_department_resources WHERE resource_id=%s "
            "AND department_id=%s", (resource_id, target_department_id)).fetchone() is None
    activate(db, changed, registry, topology, "5" * 40)
    assert governance.approved_for_core()["shares"] == []
    with psycopg.connect(db) as conn:
        assert conn.execute(
            "SELECT relation FROM panky_department_resources WHERE resource_id=%s "
            "AND department_id=%s", (resource_id, target_department_id)).fetchone()[0] == "shared"
        assert conn.execute(
            "SELECT activated_release_id FROM panky_resource_share_requests "
            "WHERE request_id=%s", (pending["request_id"],)).fetchone()[0] is not None


def test_founder_transfer_requires_founder_and_recipient_acceptance(db):
    from policy_backend_v2.governance import GovernanceStore
    policy, registry, topology = example()
    data = policy.model_dump(mode="json")
    data["schema_version"] = 3
    data["founder_actor_id"] = "act_jorge"
    for seat in data["seats"]:
        seat["seat_type"] = "admin" if seat["code"] == "management_01" else "basic"
    for resource in data["resources"]:
        resource["owner_seat"] = "dev_01" if resource["code"] == "hq_drive" else "finance_contributor"
    activate(db, Policy.model_validate(data), registry, topology, "4" * 40)
    with psycopg.connect(db) as conn:
        conn.execute("UPDATE panky_actors SET super_admin=true WHERE actor_id='act_nat'")
    governance = GovernanceStore(db)
    founder = {"issuer": "https://accounts.google.com", "subject": "founder-sub",
               "email": "jorge@example.invalid"}
    recipient = {"issuer": "https://accounts.google.com", "subject": "nat-sub",
                 "email": "nat@example.invalid"}
    with pytest.raises(ValueError, match="founder_required"):
        governance.request_founder_transfer(**recipient, target_actor_id="act_jorge")
    pending = governance.request_founder_transfer(**founder, target_actor_id="act_nat")
    assert pending["status"] == "pending" and pending["effective"] is False
    registry["actors"]["act_nat"]["roles"] = ["super_admin"]
    changed = Policy.model_validate({**data, "founder_actor_id": "act_nat"})
    with pytest.raises(ValueError, match="lacks accepted transfer"):
        activate(db, changed, registry, topology, "6" * 40)
    with pytest.raises(ValueError, match="recipient_super_admin_required"):
        governance.decide_founder_transfer(**founder, transfer_id=pending["transfer_id"],
                                           accept=True)
    accepted = governance.decide_founder_transfer(**recipient,
                                                   transfer_id=pending["transfer_id"], accept=True)
    assert accepted["status"] == "accepted" and accepted["effective"] is False
    assert governance.approved_for_core()["founder_transfers"][0]["to_actor"] == "act_nat"
    with psycopg.connect(db) as conn:
        assert conn.execute("SELECT actor_id FROM panky_actors WHERE founder=true").fetchone()[0] == "act_jorge"
    activate(db, changed, registry, topology, "6" * 40)
    assert governance.approved_for_core()["founder_transfers"] == []
    with psycopg.connect(db) as conn:
        assert conn.execute("SELECT actor_id FROM panky_actors WHERE founder=true").fetchone()[0] == "act_nat"


def test_approved_department_share_fails_if_requester_loses_admin_seat(db):
    from policy_backend_v2.governance import GovernanceStore
    from policy_backend_v2.publication import stable
    policy, registry, topology = example()
    data = policy.model_dump(mode="json")
    data["schema_version"] = 3
    data["founder_actor_id"] = "act_jorge"
    for seat in data["seats"]:
        seat["seat_type"] = "admin" if seat["code"] in {"management_01", "finance_contributor"} else "basic"
    for resource in data["resources"]:
        resource["owner_seat"] = "dev_01" if resource["code"] == "hq_drive" else "finance_contributor"
    data["resources"][1]["shared_departments"] = []
    activate(db, Policy.model_validate(data), registry, topology, "d" * 40)
    governance = GovernanceStore(db)
    requester = {"issuer": "https://accounts.google.com", "subject": "dev-sub",
                 "email": "dev@example.invalid"}
    recipient = {"issuer": "https://accounts.google.com", "subject": "nat-sub",
                 "email": "nat@example.invalid"}
    pending = governance.request_share(
        **requester, resource_id=stable("resource", "finance_drive"),
        target_department_id=stable("department", "hq"),
        via_seat_id=stable("seat", "finance_contributor"), reason="share Finance resource")
    governance.decide_share(
        **recipient, request_id=pending["request_id"],
        via_seat_id=stable("seat", "management_01"), accept=True)
    changed = Policy.model_validate({**data, "resources": [
        {**resource, "shared_departments": ["hq"]}
        if resource["code"] == "finance_drive" else resource
        for resource in data["resources"]]})
    with psycopg.connect(db) as conn:
        conn.execute("DELETE FROM panky_actor_seats WHERE actor_id='act_dev_001' "
                     "AND seat_id=%s", (stable("seat", "finance_contributor"),))
    with pytest.raises(ValueError, match="approval is stale"):
        activate(db, changed, registry, topology, "e" * 40)
    with psycopg.connect(db) as conn:
        assert conn.execute("SELECT 1 FROM panky_department_resources WHERE resource_id=%s "
                            "AND department_id=%s", (stable("resource", "finance_drive"),
                            stable("department", "hq"))).fetchone() is None


def test_gateway_governance_relay_uses_verified_oidc_identity(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app as gateway_app
    from app.auth.panky_bridge import issue_access_token
    from app.auth import panky_bridge
    from policy_backend.app import require_gateway
    from policy_backend_v2.app import app as backend_app, store as backend_store
    from policy_backend_v2.publication import stable

    policy, registry, topology = example()
    data = policy.model_dump(mode="json")
    data["schema_version"] = 3
    data["founder_actor_id"] = "act_jorge"
    for seat in data["seats"]:
        seat["seat_type"] = "admin" if seat["code"] in {"management_01", "finance_contributor"} else "basic"
    for resource in data["resources"]:
        resource["owner_seat"] = "dev_01" if resource["code"] == "hq_drive" else "finance_contributor"
    activate(db, Policy.model_validate(data), registry, topology, "7" * 40)
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
        response = TestClient(gateway_app).post(
            "/panky/governance/action",
            headers={"Authorization": f"Bearer {bearer}"},
            json={"action": "share_request", "data": {
                "actor": {"issuer": "https://accounts.google.com",
                          "subject": "forged", "email": "jorge@example.invalid"},
                "resource_id": str(stable("resource", "finance_drive")),
                "target_department_id": str(stable("department", "hq")),
                "via_seat_id": str(stable("seat", "finance_contributor")),
                "reason": "share with HQ"}},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "pending"
        with psycopg.connect(db) as conn:
            assert conn.execute(
                "SELECT requested_by_actor_id FROM panky_resource_share_requests"
            ).fetchone()[0] == "act_dev_001"
    finally:
        gateway_app.dependency_overrides.clear()
        backend_app.dependency_overrides.clear()


def test_only_founder_can_request_super_admin_revocation(db):
    from policy_backend_v2.governance import GovernanceStore
    policy, registry, topology = example()
    data = policy.model_dump(mode="json")
    data["schema_version"] = 3
    data["founder_actor_id"] = "act_jorge"
    for seat in data["seats"]:
        seat["seat_type"] = "admin" if seat["code"] == "management_01" else "basic"
    for resource in data["resources"]:
        resource["owner_seat"] = "dev_01" if resource["code"] == "hq_drive" else "finance_contributor"
    activate(db, Policy.model_validate(data), registry, topology, "8" * 40)
    with psycopg.connect(db) as conn:
        conn.execute("UPDATE panky_actors SET super_admin=true WHERE actor_id='act_nat'")
    governance = GovernanceStore(db)
    founder = {"issuer": "https://accounts.google.com", "subject": "founder-sub",
               "email": "jorge@example.invalid"}
    other_admin = {"issuer": "https://accounts.google.com", "subject": "nat-sub",
                   "email": "nat@example.invalid"}
    with pytest.raises(ValueError, match="insufficient_authority"):
        governance.change_super_admin(**other_admin, target_actor_id="act_jorge",
                                      action="revoke", reason="leadership change")
    with pytest.raises(ValueError, match="cannot_revoke_target"):
        governance.change_super_admin(**founder, target_actor_id="act_jorge",
                                      action="revoke", reason="leadership change")
    approved = governance.change_super_admin(
        **founder, target_actor_id="act_nat", action="revoke",
        reason="leadership change")
    assert approved["effective"] is False
    assert governance.approved_for_core()["super_admin_changes"][0]["action"] == "revoke"
    registry["actors"]["act_nat"]["roles"] = []
    changed = Policy.model_validate(data)
    activate(db, changed, registry, topology, "a" * 40)
    assert governance.approved_for_core()["super_admin_changes"] == []
    with psycopg.connect(db) as conn:
        assert conn.execute("SELECT super_admin FROM panky_actors WHERE actor_id='act_nat'").fetchone()[0] is False


def test_existing_policy_database_migration_adds_governance_projection(db):
    with psycopg.connect(db) as conn:
        for table in (
            "panky_resource_grant_requests", "panky_super_admin_changes",
            "panky_founder_transfers", "panky_resource_share_requests",
        ):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("ALTER TABLE panky_resources DROP COLUMN code")
        conn.execute("ALTER TABLE panky_resources DROP COLUMN owner_seat_id")
        conn.execute("ALTER TABLE panky_seats DROP COLUMN seat_type")
        conn.execute("ALTER TABLE panky_actors DROP COLUMN founder")
    migration = os.path.join(os.path.dirname(__file__), "..", "migrations",
                             "0005_seat_authority_resource_owner.sql")
    with psycopg.connect(db) as conn:
        conn.execute(open(migration, encoding="utf-8").read())
        for table in (
            "panky_resource_grant_requests", "panky_super_admin_changes",
            "panky_founder_transfers", "panky_resource_share_requests",
        ):
            assert conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0] == table
        assert conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='panky_resources' AND column_name='owner_seat_id'"
        ).fetchone()[0] == "owner_seat_id"
