"""Guarded GitHub repository lifecycle helpers for the Brunova POC.

All network access is through the supplied HTTP client, which keeps the
functions straightforward to exercise with a simulated transport.
"""

from __future__ import annotations

import base64
from typing import Any

import yaml


API = "https://api.github.com"
ORG = "Pancracio-s"
UNIVERSAL_LOGIN = "jorgeaveraf"


class GitHubLifecycleError(RuntimeError):
    """A GitHub response or repository state violated lifecycle expectations."""


def _repository_name(repository: str, owner: str = ORG) -> str:
    if "/" in repository:
        actual_owner, name = repository.split("/", 1)
        if actual_owner.casefold() != owner.casefold() or "/" in name:
            raise ValueError(f"Repository must belong to {owner}")
    else:
        name = repository
    if not name or name.strip() != name:
        raise ValueError("Repository name must be a simple, non-empty GitHub name")
    return name


def _request(client: Any, method: str, path: str, **kwargs: Any) -> Any:
    """Use an already authenticated HTTP client; callers own token handling."""
    headers = kwargs.pop("headers", {})
    headers.setdefault("Accept", "application/vnd.github+json")
    headers.setdefault("X-GitHub-Api-Version", "2022-11-28")
    return client.request(method, f"{API}{path}", headers=headers, **kwargs)


def _json(response: Any, expected: tuple[int, ...], operation: str) -> dict[str, Any]:
    if response.status_code not in expected:
        raise GitHubLifecycleError(
            f"{operation} failed with HTTP {response.status_code}"
        )
    if response.status_code == 204:
        return {}
    try:
        payload = response.json()
    except (ValueError, TypeError) as exc:
        raise GitHubLifecycleError(f"{operation} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise GitHubLifecycleError(f"{operation} returned an unexpected response")
    return payload


def verify_organization(client: Any) -> dict[str, Any]:
    """Verify GitHub resolves the expected Pancracio organization identity."""
    org = _json(_request(client, "GET", f"/orgs/{ORG}"), (200,), "Organization lookup")
    if org.get("login", "").casefold() != ORG.casefold() or not org.get("id"):
        raise GitHubLifecycleError("GitHub organization identity did not match Pancracio-s")
    if org.get("default_repository_permission") != "none":
        raise GitHubLifecycleError("Organization base repository access is too broad")
    return org


def ensure_private_repository(client: Any, *, repository: str, owner: str = ORG,
                              marker_path: str = "node.yaml",
                              marker_data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return an existing private repository or create it under Pancracio-s."""
    if owner.casefold() != ORG.casefold():
        raise ValueError(f"Repository owner must be {ORG}")
    name = _repository_name(repository, owner)
    if not marker_path or marker_path.startswith("/") or ".." in marker_path.split("/"):
        raise ValueError("Marker path must be a repository-relative path")
    verify_organization(client)
    path = f"/repos/{ORG}/{name}"
    response = _request(client, "GET", path)
    created = False
    if response.status_code == 200:
        repo = _json(response, (200,), "Repository lookup")
        if repo.get("full_name", "").casefold() != f"{ORG}/{name}".casefold():
            raise GitHubLifecycleError("Repository lookup returned a different repository")
        if repo.get("private") is not True:
            raise GitHubLifecycleError("Refusing to use an existing non-private repository")
    elif response.status_code == 404:
        created = True
        repo = _json(
            _request(client, "POST", f"/orgs/{ORG}/repos",
                     json={"name": name, "private": True, "auto_init": True,
                       "description": "Pancracio governed context backing"}),
            (201,), "Repository creation",
        )
    else:
        _json(response, (404,), "Repository lookup")
    if repo.get("private") is not True or repo.get("full_name", "").casefold() != f"{ORG}/{name}".casefold():
        raise GitHubLifecycleError("Created repository did not match the requested private repository")
    # Existing repositories are adopted only when their canonical marker matches.
    marker = marker_data if marker_data is not None else {
        "organization": ORG, "repository": f"{ORG}/{name}",
    }
    initialize_protected_main(client, repository=name, marker_path=marker_path,
                              marker_data=marker, allow_create=created)
    ensure_universal_collaborator(client, repository=name, login=UNIVERSAL_LOGIN,
                                  permission="write")
    verify_effective_access(client, repository=name, login=UNIVERSAL_LOGIN,
                            permission="write")
    return repo


def initialize_protected_main(client: Any, *, repository: str, marker_path: str,
                              marker_data: dict[str, Any], allow_create: bool = False) -> dict[str, Any]:
    """Initialize and verify the marker on the repository's main branch."""
    name = _repository_name(repository)
    marker = marker_data
    encoded = base64.b64encode(yaml.safe_dump(marker, sort_keys=True).encode()).decode()
    contents_path = f"/repos/{ORG}/{name}/contents/{marker_path}"
    existing = _request(client, "GET", contents_path, params={"ref": "main"})
    payload = {"message": "Initialize Pancracio context marker",
               "content": encoded, "branch": "main"}
    if existing.status_code == 404:
        if not allow_create:
            raise GitHubLifecycleError("Existing repository has no expected marker; refusing adoption")
        _json(_request(client, "PUT", contents_path, json=payload), (201,), "Marker initialization")
    else:
        current = _json(existing, (200,), "Marker lookup")
        try:
            current_marker = yaml.safe_load(base64.b64decode(current["content"]).decode())
        except (KeyError, ValueError, TypeError) as exc:
            raise GitHubLifecycleError("Existing repository marker is invalid") from exc
        if current_marker != marker:
            raise GitHubLifecycleError("Existing repository marker belongs to a different lifecycle")
    branch = _json(_request(client, "GET", f"/repos/{ORG}/{name}/branches/main"),
                   (200,), "Main branch lookup")
    return {"marker": marker, "branch": branch}


def ensure_universal_collaborator(client: Any, *, repository: str, login: str,
                                  permission: str = "write") -> dict[str, Any]:
    """Ensure a login has the requested effective access, inviting only if needed."""
    name = _repository_name(repository)
    current = _request(client, "GET",
                       f"/repos/{ORG}/{name}/collaborators/{login}/permission")
    if current.status_code == 200:
        current_permission = _json(current, (200,), "Effective access lookup").get("permission")
        if _permission_satisfies(current_permission, permission):
            return {"already_has_access": True, "permission": current_permission}
    elif current.status_code != 404:
        _json(current, (404,), "Effective access lookup")
    gh_permission = {"read": "pull", "write": "push", "maintain": "maintain", "admin": "admin"}.get(permission)
    if not gh_permission:
        raise ValueError("Permission must be read, write, maintain, or admin")
    response = _request(client, "PUT",
                        f"/repos/{ORG}/{name}/collaborators/{login}",
                        json={"permission": gh_permission})
    return _json(response, (201, 204), "Universal collaborator setup")


def _permission_satisfies(actual: str | None, requested: str) -> bool:
    ranks = {"none": 0, "read": 1, "triage": 1, "write": 2, "maintain": 3, "admin": 4}
    return ranks.get(actual or "none", 0) >= ranks.get(requested, 99)


def verify_effective_access(client: Any, *, repository: str, login: str,
                            permission: str = "write") -> dict[str, Any]:
    """Confirm the universal login has effective write-capable repository access."""
    name = _repository_name(repository)
    result = _json(
        _request(client, "GET",
                 f"/repos/{ORG}/{name}/collaborators/{login}/permission"),
        (200,), "Effective access lookup",
    )
    if result.get("user", {}).get("login", "").casefold() != login.casefold():
        raise GitHubLifecycleError("Effective access was reported for a different login")
    actual = result.get("permission")
    if permission not in {"read", "write", "maintain", "admin"}:
        raise ValueError("Permission must be read, write, maintain, or admin")
    if not _permission_satisfies(actual, permission):
        raise GitHubLifecycleError(f"{login} does not have effective {permission} access")
    if result.get("role_name") == "pending":
        raise GitHubLifecycleError(f"{login} has a pending invitation, not effective access")
    return result
