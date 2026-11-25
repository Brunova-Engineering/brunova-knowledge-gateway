"""Provision POC Git backings before a protected Core release becomes active.

This does not authorize any Panky Actor. The universal GitHub login transports
bytes; the active Gateway policy and Context Graph remain the access authority.
"""

from __future__ import annotations

import os

import httpx
import psycopg

from .github_repository_lifecycle import ensure_private_repository, verify_effective_access
from .models import Policy
from .repository_lifecycle import plan_repository_lifecycle


class RepositoryPublicationUnavailable(RuntimeError):
    """Leave the prior Policy release active and retry after the failure."""


def _previous_topology(dsn: str) -> dict | None:
    with psycopg.connect(dsn, connect_timeout=3) as conn:
        active = conn.execute(
            "SELECT 1 FROM panky_policy_releases WHERE status='active'"
        ).fetchone()
        if active is None:
            # Initial tenant installation has its repositories provisioned by
            # technical setup; no unreviewed bootstrap creation is attempted.
            return None
        rows = conn.execute(
            "SELECT node_id,lifecycle,repository,store_id,generation,local_path,"
            "binding_mode FROM panky_context_nodes"
        ).fetchall()
    return {"nodes": {
        str(node_id): {"lifecycle": lifecycle.upper(), "backing_bindings": [{
            "repository": repository, "store_id": store_id,
            "generation": generation, "local_path": local_path,
            "mode": binding_mode,
        }]}
        for node_id, lifecycle, repository, store_id, generation, local_path,
            binding_mode in rows
    }}


def _marker(node_id: str, node: dict) -> tuple[str, dict]:
    binding = node["backing_bindings"][0]
    if node_id == "ctx_brunova":
        raise RepositoryPublicationUnavailable("canonical Brunova root requires technical setup")
    if binding["mode"] == "generated":
        return "publication.yaml", {
            "schema_version": 1, "target_node_id": node_id,
            "generation": binding["generation"],
        }
    return "node.yaml", {
        "schema_version": 1, "node_id": node_id,
        "owner_class": "organization", "owner_id": "brunova",
        "backing_kind": "durable", "generation": binding["generation"],
    }


def provision_new_repositories(dsn: str, policy: Policy, topology: dict) -> dict:
    """Fail closed until each newly bound private repository is readable."""
    previous = _previous_topology(dsn)
    if previous is None:
        return {"created_or_verified": [], "status": "initial_technical_setup"}
    # The registry retains archived policy nodes whose topology backing may
    # still say ACTIVE for historical materialization. Only effective active
    # policy nodes may cause a GitHub write.
    active_ids = {node.node_id for node in policy.context_nodes
                  if node.lifecycle == "active"}
    eligible = {"nodes": {node_id: node for node_id, node in topology["nodes"].items()
                          if node_id in active_ids}}
    if set(eligible["nodes"]) != active_ids:
        raise RepositoryPublicationUnavailable("active policy node has no Core backing")
    plan = plan_repository_lifecycle(previous, eligible)
    if not plan.grants:
        return {"created_or_verified": [], "status": "no_change"}
    token = os.getenv("PANKY_REPOSITORY_GITHUB_TOKEN", "").strip()
    if not token:
        raise RepositoryPublicationUnavailable("repository publisher credential is unavailable")
    by_repository = {}
    for node_id, node in eligible["nodes"].items():
        if str(node.get("lifecycle", "ACTIVE")).upper() != "ACTIVE":
            continue
        repository = node["backing_bindings"][0]["repository"]
        by_repository[repository] = (node_id, node)
    verified = []
    try:
        with httpx.Client(
            timeout=15, follow_redirects=False,
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
        ) as client:
            for grant in plan.grants:
                node_id, node = by_repository[grant.repository]
                marker_path, marker_data = _marker(node_id, node)
                ensure_private_repository(
                    client, repository=grant.repository, owner="Pancracio-s",
                    marker_path=marker_path, marker_data=marker_data,
                )
                verify_effective_access(
                    client, repository=grant.repository, login=grant.login,
                    permission=grant.permission,
                )
                verified.append(grant.repository)
    except Exception as exc:
        # No token, HTTP response body or GitHub private metadata is returned
        # to the caller. The protected Core workflow may safely retry.
        raise RepositoryPublicationUnavailable(
            "repository provisioning is incomplete; prior Policy release remains active"
        ) from exc
    return {"created_or_verified": verified, "status": "verified"}
