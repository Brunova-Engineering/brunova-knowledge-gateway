import base64
import yaml

import pytest

from policy_backend_v2 import github_repository_lifecycle as lifecycle


class Response:
    def __init__(self, status, payload=None):
        self.status_code = status
        self.payload = payload

    def json(self):
        if self.payload is None:
            raise ValueError("no JSON")
        return self.payload


class FakeHTTP:
    def __init__(self, routes):
        self.routes = list(routes)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        assert self.routes, f"Unexpected request: {method} {url}"
        expected_method, expected_path, response = self.routes.pop(0)
        assert method == expected_method
        assert url.endswith(expected_path)
        return response


def org():
    return Response(200, {"login": "Pancracio-s", "id": 987,
                          "default_repository_permission": "none"})


def test_rejects_org_wide_repository_access():
    http = FakeHTTP([("GET", "/orgs/Pancracio-s", Response(200, {
        "login": "Pancracio-s", "id": 987,
        "default_repository_permission": "read",
    }))])
    with pytest.raises(lifecycle.GitHubLifecycleError, match="too broad"):
        lifecycle.verify_organization(http)


def repo(name="brunova-gateway-test", private=True):
    return {"full_name": f"Pancracio-s/{name}", "private": private}


def marker_content(marker):
    return {"content": base64.b64encode(yaml.safe_dump(marker).encode()).decode()}


def effective(login="jorgeaveraf", permission="admin", role_name="admin"):
    return Response(200, {"user": {"login": login}, "permission": permission,
                          "role_name": role_name})


def test_ensure_private_repository_creates_marker_main_and_verifies_owner_access():
    marker = {"node_id": "ctx_test", "repository": "Pancracio-s/brunova-gateway-test"}
    http = FakeHTTP([
        ("GET", "/orgs/Pancracio-s", org()),
        ("GET", "/repos/Pancracio-s/brunova-gateway-test", Response(404, {})),
        ("POST", "/orgs/Pancracio-s/repos", Response(201, repo())),
        ("GET", "/contents/node.yaml", Response(404, {})),
        ("PUT", "/contents/node.yaml", Response(201, {"content": {}})),
        ("GET", "/branches/main", Response(200, {"name": "main"})),
        ("GET", "/collaborators/jorgeaveraf/permission", effective()),
        ("GET", "/collaborators/jorgeaveraf/permission", effective()),
    ])
    result = lifecycle.ensure_private_repository(
        http, repository="Pancracio-s/brunova-gateway-test", owner="Pancracio-s",
        marker_path="node.yaml", marker_data=marker,
    )
    assert result["private"] is True
    assert http.calls[2][2]["json"]["private"] is True
    written = yaml.safe_load(base64.b64decode(http.calls[4][2]["json"]["content"]))
    assert written == marker
    assert not any("/protection" in call[1] for call in http.calls)
    assert http.routes == []


def test_existing_private_repository_requires_matching_marker_and_main():
    marker = {"node_id": "ctx_test", "repository": "Pancracio-s/brunova-gateway-test"}
    http = FakeHTTP([
        ("GET", "/orgs/Pancracio-s", org()),
        ("GET", "/repos/Pancracio-s/brunova-gateway-test", Response(200, repo())),
        ("GET", "/contents/node.yaml", Response(200, marker_content(marker))),
        ("GET", "/branches/main", Response(200, {"name": "main"})),
        ("GET", "/collaborators/jorgeaveraf/permission", effective()),
        ("GET", "/collaborators/jorgeaveraf/permission", effective()),
    ])
    lifecycle.ensure_private_repository(http, repository="brunova-gateway-test",
                                        marker_data=marker)
    assert len(http.calls) == 6

    unrelated = FakeHTTP([
        ("GET", "/orgs/Pancracio-s", org()),
        ("GET", "/repos/Pancracio-s/brunova-gateway-test", Response(200, repo())),
        ("GET", "/contents/node.yaml", Response(200, marker_content({"other": True}))),
    ])
    with pytest.raises(lifecycle.GitHubLifecycleError, match="different lifecycle"):
        lifecycle.ensure_private_repository(unrelated, repository="brunova-gateway-test",
                                            marker_data=marker)

    unmarked = FakeHTTP([
        ("GET", "/orgs/Pancracio-s", org()),
        ("GET", "/repos/Pancracio-s/brunova-gateway-test", Response(200, repo())),
        ("GET", "/contents/node.yaml", Response(404, {})),
    ])
    with pytest.raises(lifecycle.GitHubLifecycleError, match="no expected marker"):
        lifecycle.ensure_private_repository(unmarked, repository="brunova-gateway-test",
                                            marker_data=marker)


def test_existing_public_repository_is_rejected():
    http = FakeHTTP([
        ("GET", "/orgs/Pancracio-s", org()),
        ("GET", "/repos/Pancracio-s/existing", Response(200, repo("existing", False))),
    ])
    with pytest.raises(lifecycle.GitHubLifecycleError, match="non-private"):
        lifecycle.ensure_private_repository(http, repository="existing")


def test_repository_owner_and_marker_path_are_validated():
    with pytest.raises(ValueError, match="owner"):
        lifecycle.ensure_private_repository(FakeHTTP([]), repository="repo", owner="someone")
    with pytest.raises(ValueError, match="relative"):
        lifecycle.ensure_private_repository(FakeHTTP([]), repository="repo", marker_path="../node.yaml")


def test_universal_login_is_not_invited_when_effective_write_exists():
    http = FakeHTTP([
        ("GET", "/collaborators/jorgeaveraf/permission", effective(permission="write", role_name="write")),
    ])
    result = lifecycle.ensure_universal_collaborator(
        http, repository="Pancracio-s/repo", login="jorgeaveraf", permission="write")
    assert result["already_has_access"] is True
    assert len(http.calls) == 1


def test_missing_access_invites_but_pending_invite_does_not_verify_as_access():
    http = FakeHTTP([
        ("GET", "/collaborators/jorgeaveraf/permission", Response(404, {})),
        ("PUT", "/collaborators/jorgeaveraf", Response(201, {"id": 4})),
        ("GET", "/collaborators/jorgeaveraf/permission",
         effective(permission="write", role_name="pending")),
    ])
    lifecycle.ensure_universal_collaborator(http, repository="repo", login="jorgeaveraf")
    with pytest.raises(lifecycle.GitHubLifecycleError, match="pending invitation"):
        lifecycle.verify_effective_access(http, repository="repo", login="jorgeaveraf",
                                          permission="write")


def test_effective_access_checks_login_and_requested_permission():
    http = FakeHTTP([
        ("GET", "/collaborators/jorgeaveraf/permission", effective(login="someone-else")),
    ])
    with pytest.raises(lifecycle.GitHubLifecycleError, match="different login"):
        lifecycle.verify_effective_access(http, repository="repo", login="jorgeaveraf")


def test_effective_access_accepts_full_repository_name():
    http = FakeHTTP([
        ("GET", "/repos/Pancracio-s/repo/collaborators/jorgeaveraf/permission",
         effective(permission="write", role_name="write")),
    ])
    result = lifecycle.verify_effective_access(
        http, repository="Pancracio-s/repo", login="jorgeaveraf", permission="write")
    assert result["permission"] == "write"

