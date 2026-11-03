"""Activate a reviewed Core policy commit as one PostgreSQL transaction."""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import psycopg
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


NAMESPACE = UUID("8de4d0f8-35db-4ae0-bff9-b311e941b5ae")
CORE_REMOTES = frozenset({
    "git@github.com:Pancracio-s/pancracio-core.git",
    "https://github.com/Pancracio-s/pancracio-core.git",
})
POLICY_PATH = "policy/brunova-gateway-policy.json"


class PolicyActor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor_id: str
    departments: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)


class PolicyDepartment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    name: str
    parent: str | None = None


class PolicyRole(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    name: str


class PolicyGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject_kind: str
    subject_id: str
    provider: str
    tool: str
    resource_kind: str
    resource_ref: str
    operation: str
    effect: str
    inherit_to_children: bool = False
    expires_at: datetime | None = None
    max_uses: int | None = Field(default=None, ge=1)
    reason: str | None = None
    approved_by: str | None = None

    @model_validator(mode="after")
    def valid_scope(self):
        if self.subject_kind not in {"actor", "role", "department"}:
            raise ValueError("Grant subject is invalid")
        if self.effect not in {"allow", "deny"}:
            raise ValueError("Grant effect is invalid")
        if self.max_uses is not None and (
            self.subject_kind != "actor" or self.effect != "allow"
        ):
            raise ValueError("Usage limits require a direct allow exception")
        if self.inherit_to_children and self.subject_kind != "department":
            raise ValueError("Only department grants can inherit")
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            raise ValueError("Grant expiry must include a timezone")
        return self


class PolicyDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = Field(ge=1, le=1)
    tenant: str
    actors: list[PolicyActor]
    departments: list[PolicyDepartment] = Field(default_factory=list)
    roles: list[PolicyRole] = Field(default_factory=list)
    grants: list[PolicyGrant]

    @model_validator(mode="after")
    def valid_references(self):
        actor_ids = {actor.actor_id for actor in self.actors}
        department_codes = {department.code for department in self.departments}
        role_codes = {role.code for role in self.roles}
        if len(actor_ids) != len(self.actors):
            raise ValueError("Duplicate actors")
        if len(department_codes) != len(self.departments):
            raise ValueError("Duplicate departments")
        if len(role_codes) != len(self.roles):
            raise ValueError("Duplicate roles")
        for actor in self.actors:
            if not set(actor.departments) <= department_codes:
                raise ValueError("Unknown department assignment")
            if not set(actor.roles) <= role_codes:
                raise ValueError("Unknown role assignment")
        for department in self.departments:
            if department.parent and department.parent not in department_codes:
                raise ValueError("Unknown parent department")
        parents = {department.code: department.parent for department in self.departments}
        for code in parents:
            seen = set()
            current = code
            while current is not None:
                if current in seen:
                    raise ValueError("Department hierarchy contains a cycle")
                seen.add(current)
                current = parents[current]
        for grant in self.grants:
            ids = {"actor": actor_ids, "department": department_codes,
                   "role": role_codes}[grant.subject_kind]
            if grant.subject_id not in ids:
                raise ValueError("Unknown grant subject")
            if grant.approved_by and grant.approved_by not in actor_ids:
                raise ValueError("Unknown grant approver")
        return self


def _git(core_repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(core_repo), *args],
        check=True, capture_output=True, text=True, timeout=30,
    )
    return result.stdout.strip()


def load_reviewed_policy(core_repo: Path, commit: str) -> tuple[PolicyDocument, dict]:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Full Core commit required")
    if _git(core_repo, "remote", "get-url", "origin") not in CORE_REMOTES:
        raise ValueError("Core origin is not approved")
    _git(core_repo, "fetch", "--quiet", "origin", "main")
    _git(core_repo, "merge-base", "--is-ancestor", commit, "origin/main")
    policy = PolicyDocument.model_validate_json(
        _git(core_repo, "show", f"{commit}:{POLICY_PATH}")
    )
    if policy.tenant != "brunova":
        raise ValueError("Unexpected tenant")
    registry = yaml.safe_load(
        _git(core_repo, "show", f"{commit}:registry/actors.yaml")
    )["actors"]
    for actor in policy.actors:
        record = registry.get(actor.actor_id)
        if not record or record.get("status") != "active":
            raise ValueError("Policy actor is not active in Core")
    return policy, registry


def activate_policy(
    dsn: str, policy: PolicyDocument, registry: dict,
    commit: str, publisher_id: str,
) -> str:
    if publisher_id != "github_actions":
        if publisher_id not in {actor.actor_id for actor in policy.actors}:
            raise ValueError("Publisher is not in this policy")
        if "super_admin" not in registry[publisher_id]["roles"]:
            raise ValueError("Only a Core super admin can publish policy")
    release_id = str(uuid4())
    with psycopg.connect(dsn) as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(8642091)")
            if conn.execute(
                "SELECT 1 FROM panky_policy_releases WHERE core_commit = %s",
                (commit,),
            ).fetchone():
                raise ValueError("Core commit is already published")
            for actor in policy.actors:
                record = registry[actor.actor_id]
                email = record["identities"]["google_oidc"]["email"].casefold()
                super_admin = "super_admin" in record["roles"]
                conn.execute(
                    "INSERT INTO panky_actors "
                    "(actor_id, oidc_issuer, email, status, super_admin) "
                    "VALUES (%s, 'https://accounts.google.com', %s, 'active', %s) "
                    "ON CONFLICT (actor_id) DO UPDATE SET email=EXCLUDED.email,"
                    " status='active', super_admin=EXCLUDED.super_admin",
                    (actor.actor_id, email, super_admin),
                )
            # Former Actors cannot retain access through stale assignments.
            conn.execute(
                "UPDATE panky_actors SET status='disabled' "
                "WHERE actor_id <> ALL(%s)",
                ([actor.actor_id for actor in policy.actors],),
            )
            conn.execute("UPDATE panky_policy_releases SET status='superseded' WHERE status='active'")
            conn.execute(
                "INSERT INTO panky_policy_releases "
                "(release_id, core_commit, status, published_by) "
                "VALUES (%s, %s, 'active', %s)",
                (release_id, commit, publisher_id),
            )
            for department in policy.departments:
                conn.execute(
                    "INSERT INTO panky_departments (department_id, code, name) "
                    "VALUES (%s,%s,%s) ON CONFLICT (code) DO UPDATE SET name=EXCLUDED.name",
                    (uuid5(NAMESPACE, f"dept:{department.code}"),
                     department.code, department.name),
                )
            for department in policy.departments:
                if department.parent:
                    conn.execute(
                        "UPDATE panky_departments SET parent_department_id = %s WHERE code = %s",
                        (uuid5(NAMESPACE, f"dept:{department.parent}"), department.code),
                    )
            for role in policy.roles:
                conn.execute(
                    "INSERT INTO panky_roles (role_id, code, name) VALUES (%s,%s,%s) "
                    "ON CONFLICT (code) DO UPDATE SET name=EXCLUDED.name",
                    (uuid5(NAMESPACE, f"role:{role.code}"), role.code, role.name),
                )
            conn.execute("DELETE FROM panky_actor_departments")
            conn.execute("DELETE FROM panky_actor_roles")
            for actor in policy.actors:
                for code in actor.departments:
                    conn.execute(
                        "INSERT INTO panky_actor_departments VALUES (%s,%s,%s)",
                        (uuid4(), actor.actor_id, uuid5(NAMESPACE, f"dept:{code}")),
                    )
                for code in actor.roles:
                    conn.execute(
                        "INSERT INTO panky_actor_roles VALUES (%s,%s,%s)",
                        (uuid4(), actor.actor_id, uuid5(NAMESPACE, f"role:{code}")),
                    )
            for grant in policy.grants:
                tool_id = uuid5(NAMESPACE, f"tool:{grant.provider}:{grant.tool}")
                resource_id = uuid5(
                    NAMESPACE, f"resource:{grant.provider}:"
                    f"{grant.resource_kind}:{grant.resource_ref}"
                )
                conn.execute(
                    "INSERT INTO panky_tools VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                    (tool_id, grant.provider, grant.tool),
                )
                conn.execute(
                    "INSERT INTO panky_resources VALUES (%s,%s,%s,%s) "
                    "ON CONFLICT DO NOTHING",
                    (resource_id, grant.provider, grant.resource_kind,
                     grant.resource_ref),
                )
                subject_columns = {
                    "actor": (grant.subject_id, None, None),
                    "department": (None, uuid5(NAMESPACE, f"dept:{grant.subject_id}"), None),
                    "role": (None, None, uuid5(NAMESPACE, f"role:{grant.subject_id}")),
                }[grant.subject_kind]
                conn.execute(
                    "INSERT INTO panky_grants "
                    "(grant_id,release_id,actor_id,department_id,role_id,tool_id,"
                    "resource_id,operation,effect,inherit_to_children,expires_at,"
                    "max_uses,reason,approved_by) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (uuid4(), release_id, *subject_columns, tool_id, resource_id,
                     grant.operation, grant.effect, grant.inherit_to_children,
                     grant.expires_at, grant.max_uses, grant.reason,
                     grant.approved_by),
                )
            conn.execute(
                "INSERT INTO panky_audit_events "
                "(release_id, actor_id, event_type, decision, details) "
                "VALUES (%s,%s,'activate_policy','allow',%s)",
                (release_id, None if publisher_id == "github_actions" else publisher_id,
                 psycopg.types.json.Jsonb({"core_commit": commit})),
            )
    return release_id


def activate_reviewed_commit(commit: str, publisher_id: str) -> str:
    core_repo = Path(os.environ["PANKY_CORE_CHECKOUT"])
    policy, registry = load_reviewed_policy(core_repo, commit)
    return activate_policy(os.environ["PANKY_POLICY_DSN"], policy, registry,
                           commit, publisher_id)


def publisher_for_identity(dsn: str, email: str, subject: str) -> str:
    """A publication needs a previously enrolled, live super admin."""
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT actor_id FROM panky_actors WHERE "
            "oidc_issuer='https://accounts.google.com' AND email=%s "
            "AND oidc_subject=%s AND status='active' AND super_admin=true",
            (email.casefold(), subject),
        ).fetchone()
    if row is None:
        raise ValueError("Publisher is not an enrolled super admin")
    return row[0]
