from policy_backend_v2 import app as api


def test_resource_sharing_echoes_core_code_after_store_lookup(monkeypatch):
    seen = {}

    class Store:
        def __init__(self, dsn):
            assert dsn == "test-dsn"

        def resource_sharing(self, **kwargs):
            seen.update(kwargs)
            return {"resource_id": str(kwargs["resource_id"]),
                    "active_grants": [], "shared_departments": []}

    monkeypatch.setattr(api, "GovernanceStore", Store)
    request = api.ResourceSharingQuery(
        actor=api.ActorProof(issuer="https://accounts.google.com",
                             subject="subject", email="actor@example.com"),
        resource_id="panky_onboarding_audit_test_20261110",
        via_seat_id="onboarding_audit_01",
    )
    result = api.resource_sharing(request, None, type("Database", (), {"dsn": "test-dsn"})())

    assert result == {"resource_id": request.resource_id,
                      "active_grants": [], "shared_departments": []}
    assert seen["resource_id"] == api._reference("resource", request.resource_id)
    assert seen["via_seat_id"] == api._reference("seat", request.via_seat_id)
