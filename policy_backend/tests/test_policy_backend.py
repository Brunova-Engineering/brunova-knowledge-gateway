"""Exercise decisions against an actual temporary PostgreSQL database."""

from __future__ import annotations

import shutil
import socket
import subprocess
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
import yaml
from fastapi.testclient import TestClient

from policy_backend.models import ActorIdentity, DecisionRequest, GrantUseCompletion
from policy_backend.store import PolicyStore
from policy_backend.publication import PolicyDocument, activate_policy
from policy_backend.app import app as policy_app, store as store_dependency


MIGRATION = Path(__file__).resolve().parents[1] / "migrations/001_panky_policy.sql"


@pytest.fixture
def database(tmp_path):
    if not all(shutil.which(binary) for binary in ("initdb", "pg_ctl")):
        pytest.skip("PostgreSQL server binaries unavailable")
    cluster = tmp_path / "cluster"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    subprocess.run(["initdb", "-D", str(cluster), "-A", "trust", "-U", "postgres"],
                   check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["pg_ctl", "-D", str(cluster), "-o", f"-p {port} -h 127.0.0.1",
                    "-w", "start"], check=True, stdout=subprocess.DEVNULL)
    dsn = f"host=127.0.0.1 port={port} user=postgres dbname=postgres"
    try:
        with psycopg.connect(dsn) as conn:
            conn.execute(MIGRATION.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO panky_actors "
                "(actor_id, oidc_issuer, email, status, super_admin) "
                "VALUES ('admin','https://accounts.google.com','admin@example.com','active',true),"
                "('dev','https://accounts.google.com','dev@example.com','active',false)"
            )
            conn.execute(
                "INSERT INTO panky_policy_releases "
                "(release_id, core_commit, status, published_by) "
                "VALUES (%s, %s, 'active', 'admin')",
                (uuid4(), "a" * 40),
            )
        yield dsn
    finally:
        subprocess.run(["pg_ctl", "-D", str(cluster), "-m", "immediate", "-w", "stop"],
                       check=True, stdout=subprocess.DEVNULL)


def request(email="dev@example.com", subject="dev-google-sub", *,
            operation="read", request_id="one"):
    return DecisionRequest(
        actor=ActorIdentity(issuer="https://accounts.google.com",
                            subject=subject, email=email),
        provider="workspace", tool="drive_file", resource_kind="source",
        resource_ref="hq_client", operation=operation, request_id=request_id,
    )


def grant(conn, *, subject="dev", effect="allow", operation="read", max_uses=None):
    tool_id, resource_id, grant_id = uuid4(), uuid4(), uuid4()
    conn.execute("INSERT INTO panky_tools VALUES (%s,'workspace','drive_file')", (tool_id,))
    conn.execute("INSERT INTO panky_resources VALUES (%s,'workspace','source','hq_client')",
                 (resource_id,))
    conn.execute(
        "INSERT INTO panky_grants "
        "(grant_id,release_id,actor_id,tool_id,resource_id,operation,effect,max_uses,valid_from) "
        "VALUES (%s,(SELECT release_id FROM panky_policy_releases WHERE status='active'),"
        "%s,%s,%s,%s,%s,%s,%s)",
        (grant_id, subject, tool_id, resource_id, operation, effect, max_uses,
         datetime.now(timezone.utc)),
    )
    return grant_id


def test_admin_and_unknown_actor(database):
    policy = PolicyStore(database)
    assert policy.decide(request("admin@example.com", "admin-sub")).reason == "super_admin"
    assert policy.decide(request("missing@example.com", "other")).reason == "actor_unknown"
    assert policy.decide(request("admin@example.com", "different")).reason == "subject_mismatch"


def test_specific_grant_and_denial(database):
    with psycopg.connect(database) as conn:
        grant(conn)
    policy = PolicyStore(database)
    assert policy.decide(request()).allowed
    assert not policy.decide(request(operation="delete")).allowed
    assert not policy.decide(request(subject="other-sub")).allowed


def test_one_time_grant_is_atomic_and_consumed(database):
    with psycopg.connect(database) as conn:
        grant(conn, max_uses=1)
    policy = PolicyStore(database)
    first = policy.decide(request())
    assert first.allowed and first.reservation_id
    assert not policy.decide(request(request_id="two")).allowed
    assert not policy.decide(request()).allowed
    assert policy.complete_use(GrantUseCompletion(
        reservation_id=first.reservation_id, request_id="one", status="released"
    ))
    assert policy.decide(request(request_id="three")).allowed


def test_reviewed_release_replaces_previous_grants_atomically(database):
    registry = {
        "admin": {"roles": ["super_admin"], "identities": {
            "google_oidc": {"email": "admin@example.com"}}},
        "dev": {"roles": ["developer"], "identities": {
            "google_oidc": {"email": "dev@example.com"}}},
    }
    document = PolicyDocument.model_validate({
        "schema_version": 1, "tenant": "brunova",
        "actors": [{"actor_id": "admin"},
                   {"actor_id": "dev", "roles": ["developer"]}],
        "roles": [{"code": "developer", "name": "Developer"}],
        "grants": [{
            "subject_kind": "role", "subject_id": "developer",
            "provider": "workspace", "tool": "*",
            "resource_kind": "source", "resource_ref": "hq_client",
            "operation": "read", "effect": "allow",
        }],
    })
    assert activate_policy(database, document, registry, "b" * 40, "admin")
    policy = PolicyStore(database)
    assert policy.decide(request()).allowed
    with psycopg.connect(database) as conn:
        statuses = list(conn.execute(
            "SELECT status FROM panky_policy_releases ORDER BY core_commit"
        ))
    assert statuses == [("superseded",), ("active",)]
    assert policy.decide(request(operation="delete")).reason == "default_deny"


def test_revocation_applies_to_next_decision(database):
    with psycopg.connect(database) as conn:
        grant(conn)
    policy = PolicyStore(database)
    assert policy.decide(request()).allowed
    registry = {
        "admin": {"roles": ["super_admin"], "identities": {
            "google_oidc": {"email": "admin@example.com"}}},
        "dev": {"roles": ["developer"], "identities": {
            "google_oidc": {"email": "dev@example.com"}}},
    }
    replacement = PolicyDocument.model_validate({
        "schema_version": 1, "tenant": "brunova",
        "actors": [{"actor_id": "admin"}, {"actor_id": "dev"}],
        "grants": [],
    })
    activate_policy(database, replacement, registry, "d" * 40, "admin")
    assert policy.decide(request(request_id="next-call")).reason == "default_deny"


def test_brunova_seed_matches_current_developer_scope(database):
    core_path = os.getenv("PANKY_TEST_CORE_PATH")
    if not core_path:
        pytest.skip("Core policy checkout is not available")
    root = Path(core_path)
    document = PolicyDocument.model_validate_json(
        (root / "policy/brunova-gateway-policy.json").read_text()
    )
    registry = yaml.safe_load((root / "registry/actors.yaml").read_text())["actors"]
    activate_policy(database, document, registry, "c" * 40, "act_jorge")
    policy = PolicyStore(database)
    dev_email = registry["act_dev_001"]["identities"]["google_oidc"]["email"]

    def check(provider, tool, kind, ref, operation):
        return policy.decide(DecisionRequest(
            actor=ActorIdentity(issuer="https://accounts.google.com",
                                subject="synthetic-dev-subject", email=dev_email),
            provider=provider, tool=tool, resource_kind=kind,
            resource_ref=ref, operation=operation,
            request_id=f"test:{provider}:{tool}:{ref}:{operation}",
        )).allowed

    for operation in ("read", "create", "update", "move", "convert"):
        assert check("workspace", "drive_file", "source", "hq_client", operation)
    for operation in ("delete", "share"):
        assert not check("workspace", "drive_file", "source", "hq_client", operation)
    assert not check("workspace", "drive_file", "source", "other", "read")
    assert check("n8n", "n8n_instance_call_tool", "instance", "headquarters", "call")
    assert not check("n8n", "n8n_instance_call_tool", "instance", "other", "call")


def test_private_api_requires_gateway_identity(database, monkeypatch):
    monkeypatch.setenv("PANKY_POLICY_AUDIENCE", "https://brunova.mx/panky-policy")
    monkeypatch.setenv("PANKY_GATEWAY_SERVICE_ACCOUNT", "gateway@example.iam.gserviceaccount.com")
    policy_app.dependency_overrides[store_dependency] = lambda: PolicyStore(database)
    try:
        client = TestClient(policy_app)
        assert client.get("/health").status_code == 200
        assert client.post("/v1/decide", json=request().model_dump()).status_code == 401
        monkeypatch.setattr("policy_backend.app.id_token.verify_oauth2_token",
                            lambda token, request, audience: {
                                "iss": "https://accounts.google.com",
                                "aud": audience,
                                "email": "gateway@example.iam.gserviceaccount.com",
                                "email_verified": True,
                            })
        response = client.post(
            "/v1/decide", json=request().model_dump(),
            headers={"Authorization": "Bearer synthetic-service-proof"},
        )
        assert response.status_code == 200
        assert response.json()["reason"] == "default_deny"
    finally:
        policy_app.dependency_overrides.clear()


def test_core_workflow_publishes_only_its_exact_main_commit(database, monkeypatch):
    from policy_backend.app import github_jwks

    commit = "e" * 40
    registry_yaml = yaml.safe_dump({"actors": {
        "admin": {"status": "active", "roles": ["super_admin"],
                  "identities": {"google_oidc": {"email": "admin@example.com"}}},
        "dev": {"status": "active", "roles": ["developer"],
                "identities": {"google_oidc": {"email": "dev@example.com"}}},
    }})
    document = {"schema_version": 1, "tenant": "brunova",
                "actors": [{"actor_id": "admin"}, {"actor_id": "dev"}],
                "grants": []}
    claims = {
        "repository_id": "1391259854",
        "repository": "Pancracio-s/pancracio-core",
        "ref": "refs/heads/main", "event_name": "push",
        "workflow_ref": "Pancracio-s/pancracio-core/.github/workflows/publish-gateway-policy.yml@refs/heads/main",
        "sha": commit,
    }
    monkeypatch.setattr("policy_backend.app.github_jwks", lambda: type(
        "Keys", (), {"get_signing_key_from_jwt": lambda self, token: type(
            "Key", (), {"key": "synthetic-key"})()})())
    monkeypatch.setattr("policy_backend.app.jwt.decode",
                        lambda *args, **kwargs: claims)
    policy_app.dependency_overrides[store_dependency] = lambda: PolicyStore(database)
    try:
        client = TestClient(policy_app)
        payload = {"core_commit": commit, "policy": document,
                   "registry_yaml": registry_yaml}
        assert client.post("/v1/releases/publish-from-core", json=payload).status_code == 401
        denied = client.post("/v1/releases/publish-from-core",
                             json={**payload, "core_commit": "f" * 40},
                             headers={"Authorization": "Bearer synthetic-oidc"})
        assert denied.status_code == 403
        accepted = client.post("/v1/releases/publish-from-core", json=payload,
                               headers={"Authorization": "Bearer synthetic-oidc"})
        assert accepted.status_code == 200
        assert accepted.json()["core_commit"] == commit
    finally:
        policy_app.dependency_overrides.clear()
