"""A v2 publisher cannot borrow the live v1 workflow identity."""

import pytest
from fastapi import HTTPException

from policy_backend import service_auth


def test_v2_core_publisher_is_separate_and_dispatch_only(monkeypatch):
    claims = {
        "repository_id": "1391259854", "repository": "Pancracio-s/pancracio-core",
        "ref": "refs/heads/main", "event_name": "workflow_dispatch",
        "workflow_ref": "Pancracio-s/pancracio-core/.github/workflows/"
                        "publish-gateway-policy-v2.yml@refs/heads/main",
        "sha": "a" * 40,
    }
    monkeypatch.setattr(service_auth, "github_jwks", lambda: type(
        "Keys", (), {"get_signing_key_from_jwt": lambda self, token: type(
            "Key", (), {"key": "synthetic-key"})()})())
    monkeypatch.setattr(service_auth.jwt, "decode", lambda *args, **kwargs: claims)
    assert service_auth._core_workflow_claims(
        "Bearer synthetic-proof", workflow="publish-gateway-policy-v2.yml",
        events={"workflow_dispatch"},
    )["sha"] == "a" * 40
    with pytest.raises(HTTPException) as denied_v1:
        service_auth._core_workflow_claims(
            "Bearer synthetic-proof", workflow="publish-gateway-policy.yml",
            events={"push", "workflow_dispatch"},
        )
    assert denied_v1.value.status_code == 403
    claims["event_name"] = "push"
    with pytest.raises(HTTPException) as denied_push:
        service_auth._core_workflow_claims(
            "Bearer synthetic-proof", workflow="publish-gateway-policy-v2.yml",
            events={"workflow_dispatch"},
        )
    assert denied_push.value.status_code == 403


def test_core_governance_workflow_identity_is_separate(monkeypatch):
    claims = {
        "repository_id": "1391259854", "repository": "Pancracio-s/pancracio-core",
        "ref": "refs/heads/main", "event_name": "workflow_dispatch",
        "workflow_ref": "Pancracio-s/pancracio-core/.github/workflows/"
                        "governance-policy-sync.yml@refs/heads/main",
        "sha": "b" * 40,
    }
    monkeypatch.setattr(service_auth, "github_jwks", lambda: type(
        "Keys", (), {"get_signing_key_from_jwt": lambda self, token: type(
            "Key", (), {"key": "synthetic-key"})()})())
    monkeypatch.setattr(service_auth.jwt, "decode", lambda *args, **kwargs: claims)
    assert service_auth.require_core_governance_workflow(
        "Bearer synthetic-proof")["sha"] == "b" * 40
    claims["event_name"] = "schedule"
    assert service_auth.require_core_governance_workflow(
        "Bearer synthetic-proof")["sha"] == "b" * 40
    claims["event_name"] = "workflow_dispatch"
    claims["workflow_ref"] = (
        "Pancracio-s/pancracio-core/.github/workflows/"
        "publish-gateway-policy-v2.yml@refs/heads/main"
    )
    with pytest.raises(HTTPException) as denied:
        service_auth.require_core_governance_workflow("Bearer synthetic-proof")
    assert denied.value.status_code == 403
