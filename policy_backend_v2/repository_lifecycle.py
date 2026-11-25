"""Pure planner for the Brunova POC's universal GitHub repository access.

This module describes transport access only. A GitHub login or repository grant
must never be used as evidence of a Panky Actor's permissions.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping


UNIVERSAL_GITHUB_LOGIN = "jorgeaveraf"
EXPECTED_OWNER = "Pancracio-s"
_REPOSITORY_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True, order=True)
class RepositoryGrant:
    """A planned GitHub transport grant, with no Actor permission semantics."""

    repository: str
    login: str = UNIVERSAL_GITHUB_LOGIN
    permission: str = "write"


@dataclass(frozen=True)
class RepositoryLifecyclePlan:
    """Desired active repositories and grants missing from the before state."""

    active_repositories: tuple[str, ...]
    grants: tuple[RepositoryGrant, ...]


def _active_repositories(topology: Mapping) -> set[str]:
    nodes = topology.get("nodes")
    if not isinstance(nodes, Mapping):
        raise ValueError("Protected Core topology nodes are unavailable")

    repositories: dict[str, str] = {}
    for node_id, node in nodes.items():
        if not isinstance(node, Mapping):
            raise ValueError(f"Protected Core node {node_id!r} is invalid")
        lifecycle = node.get("lifecycle", "ACTIVE")
        if not isinstance(lifecycle, str) or lifecycle.upper() not in {"ACTIVE", "ARCHIVED"}:
            raise ValueError(f"Protected Core node {node_id!r} lifecycle is invalid")
        if lifecycle.upper() == "ARCHIVED":
            continue
        bindings = node.get("backing_bindings")
        if not isinstance(bindings, list) or len(bindings) != 1:
            raise ValueError(f"Protected Core node {node_id!r} must have one backing")
        binding = bindings[0]
        if not isinstance(binding, Mapping):
            raise ValueError(f"Protected Core node {node_id!r} backing is invalid")
        if binding.get("mode") not in {"authoritative", "generated"}:
            raise ValueError(f"Protected Core node {node_id!r} backing mode is invalid")
        repository = binding.get("repository")
        parts = repository.split("/") if isinstance(repository, str) else []
        if (len(parts) != 2 or parts[0] != EXPECTED_OWNER
                or not _REPOSITORY_NAME.fullmatch(parts[1])
                or parts[1] in {".", ".."}):
            raise ValueError(f"Protected Core node {node_id!r} repository owner is invalid")
        if (not isinstance(binding.get("store_id"), str) or not binding["store_id"]
                or not isinstance(binding.get("generation"), int)
                or isinstance(binding.get("generation"), bool)
                or binding["generation"] < 1
                or not isinstance(binding.get("local_path"), str)
                or not binding["local_path"]):
            raise ValueError(f"Protected Core node {node_id!r} backing marker is invalid")
        if repository in repositories:
            raise ValueError(
                f"Protected Core repository {repository!r} is bound to multiple active nodes"
            )
        repositories[repository] = node_id
    return set(repositories)


def plan_repository_lifecycle(before_topology: Mapping,
                              after_topology: Mapping) -> RepositoryLifecyclePlan:
    """Plan universal login grants for newly introduced active repositories.

    Both inputs are protected Core topology snapshots. Every active node must
    have exactly one valid backing binding. The output is idempotent when the
    active repository set has not changed and does not inspect GitHub state.
    """

    before = _active_repositories(before_topology)
    after = _active_repositories(after_topology)
    added = sorted(after - before)
    return RepositoryLifecyclePlan(
        active_repositories=tuple(sorted(after)),
        grants=tuple(RepositoryGrant(repository=repo) for repo in added),
    )
