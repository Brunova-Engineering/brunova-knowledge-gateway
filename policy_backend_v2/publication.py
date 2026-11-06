"""Compile a reviewed Core policy into the active Panky 2.0 projection."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4, uuid5

import psycopg
from psycopg.types.json import Jsonb

from .models import Policy


NAMESPACE = UUID("8de4d0f8-35db-4ae0-bff9-b311e941b5ae")


def stable(kind: str, code: str):
    return uuid5(NAMESPACE, f"v2:{kind}:{code}")


def verify_core(policy: Policy, registry: dict, topology: dict) -> None:
    if policy.tenant != "brunova":
        raise ValueError("Unexpected tenant")
    registered = registry.get("actors", registry)
    graph = topology.get("nodes", {})
    for actor in policy.actors:
        record = registered.get(actor.actor_id)
        if not record or record.get("status") != "active":
            raise ValueError("Policy Actor is not active in Core")
        if not record.get("identities", {}).get("google_oidc", {}).get("email"):
            raise ValueError("Policy Actor lacks Google OIDC email")
    if {n.node_id for n in policy.context_nodes} != set(graph):
        raise ValueError("Policy context nodes do not match protected Core")
    for node in policy.context_nodes:
        core_node = graph[node.node_id]
        if core_node.get("owner_class") != "organization":
            raise ValueError("Actor-private nodes cannot enter organizational policy")
        if core_node.get("parent_id") != node.parent_node_id:
            raise ValueError("Policy context parent differs from protected Core")
        if node.kind == "seat" and core_node.get("seat_id") is None:
            raise ValueError("Policy seat node lacks protected Seat binding")
        if node.kind != "seat" and core_node.get("seat_id") is not None:
            raise ValueError("Policy department node conflicts with Seat binding")
    core_seats = topology.get("_seats", {})
    active_seats = {code for code, record in core_seats.items()
                    if record.get("status") == "active"}
    if {seat.code for seat in policy.seats} != active_seats:
        raise ValueError("Policy Seats do not match active protected Core Seats")
    for seat in policy.seats:
        record = core_seats[seat.code]
        if graph[seat.context_node_id].get("seat_id") != seat.code:
            raise ValueError("Policy Seat node points to another protected Seat")
        if set(record.get("occupied_by", [])) != set(seat.occupants):
            raise ValueError("Seat occupants differ from protected Core")


def activate(dsn: str, policy: Policy, registry: dict, topology: dict,
             core_commit: str, publisher: str = "github_actions") -> str:
    if len(core_commit) != 40 or any(c not in "0123456789abcdef" for c in core_commit):
        raise ValueError("Full lowercase Core commit required")
    verify_core(policy, registry, topology)
    release_id = uuid4()
    at = datetime.now(timezone.utc)
    actor_ids = [a.actor_id for a in policy.actors]
    node_ids = [n.node_id for n in policy.context_nodes]
    department_ids = [stable("department", d.code) for d in policy.departments]
    role_ids = [stable("role", r.code) for r in policy.roles]
    seat_ids = [stable("seat", s.code) for s in policy.seats]
    tool_ids = [stable("tool", t.code) for t in policy.tools]
    resource_ids = [stable("resource", r.code) for r in policy.resources]
    with psycopg.connect(dsn) as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(8642092)")
        if conn.execute("SELECT 1 FROM panky_policy_releases WHERE core_commit=%s",
                        (core_commit,)).fetchone():
            raise ValueError("Core commit already activated")
        for actor in policy.actors:
            record = registry.get("actors", registry)[actor.actor_id]
            email = record["identities"]["google_oidc"]["email"].casefold()
            admin = "super_admin" in record.get("roles", [])
            conn.execute(
                "INSERT INTO panky_actors(actor_id,email,status,super_admin) "
                "VALUES (%s,%s,'active',%s) ON CONFLICT(actor_id) DO UPDATE SET "
                "oidc_subject=CASE WHEN panky_actors.email=EXCLUDED.email THEN "
                "panky_actors.oidc_subject ELSE NULL END, "
                "email=EXCLUDED.email,status='active',super_admin=EXCLUDED.super_admin",
                (actor.actor_id, email, admin),
            )
        conn.execute("UPDATE panky_actors SET status='disabled' WHERE NOT(actor_id=ANY(%s))",
                     (actor_ids,))

        # Two-pass upserts avoid FK ordering dependencies in protected trees.
        for node in policy.context_nodes:
            conn.execute(
                "INSERT INTO panky_context_nodes(node_id,parent_node_id,kind,visibility,lifecycle) "
                "VALUES (%s,NULL,%s,%s,%s) ON CONFLICT(node_id) DO UPDATE SET "
                "kind=EXCLUDED.kind,visibility=EXCLUDED.visibility,lifecycle=EXCLUDED.lifecycle",
                (node.node_id, node.kind, node.visibility, node.lifecycle),
            )
        conn.execute("UPDATE panky_context_nodes SET lifecycle='archived' WHERE NOT(node_id=ANY(%s))",
                     (node_ids,))
        for node in policy.context_nodes:
            conn.execute("UPDATE panky_context_nodes SET parent_node_id=%s WHERE node_id=%s",
                         (node.parent_node_id, node.node_id))
        for d in policy.departments:
            conn.execute(
                "INSERT INTO panky_departments(department_id,code,name,context_node_id) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT(department_id) DO UPDATE SET "
                "name=EXCLUDED.name,context_node_id=EXCLUDED.context_node_id",
                (stable("department", d.code), d.code, d.name, d.context_node_id),
            )
        for d in policy.departments:
            conn.execute("UPDATE panky_departments SET parent_department_id=%s WHERE department_id=%s",
                         (stable("department", d.parent) if d.parent else None,
                          stable("department", d.code)))
        for r in policy.roles:
            conn.execute(
                "INSERT INTO panky_roles(role_id,code,name,status) VALUES (%s,%s,%s,'active') "
                "ON CONFLICT(role_id) DO UPDATE SET name=EXCLUDED.name,status='active'",
                (stable("role", r.code), r.code, r.name),
            )
        conn.execute("UPDATE panky_roles SET status='disabled' WHERE NOT(role_id=ANY(%s))",
                     (role_ids,))
        for s in policy.seats:
            conn.execute(
                "INSERT INTO panky_seats(seat_id,code,department_id,role_id,context_node_id,"
                "occupancy,status) VALUES (%s,%s,%s,%s,%s,%s,'active') "
                "ON CONFLICT(seat_id) DO UPDATE SET department_id=EXCLUDED.department_id,"
                "role_id=EXCLUDED.role_id,context_node_id=EXCLUDED.context_node_id,"
                "occupancy=EXCLUDED.occupancy,status='active'",
                (stable("seat", s.code), s.code, stable("department", s.department),
                 stable("role", s.role), s.context_node_id, s.occupancy),
            )
        conn.execute("UPDATE panky_seats SET status='disabled' WHERE NOT(seat_id=ANY(%s))",
                     (seat_ids,))
        for s in policy.seats:
            conn.execute("UPDATE panky_seats SET reports_to_seat_id=%s WHERE seat_id=%s",
                         (stable("seat", s.reports_to) if s.reports_to else None,
                          stable("seat", s.code)))
        conn.execute("UPDATE panky_actor_seats SET active_until=%s WHERE active_until IS NULL",
                     (at,))
        for s in policy.seats:
            for actor_id in s.occupants:
                conn.execute(
                    "INSERT INTO panky_actor_seats(assignment_id,actor_id,seat_id,active_from) "
                    "VALUES (%s,%s,%s,%s)",
                    (uuid4(), actor_id, stable("seat", s.code), at),
                )

        for t in policy.tools:
            conn.execute(
                "INSERT INTO panky_tools(tool_id,code,provider,operations_json,status) "
                "VALUES (%s,%s,%s,%s,'active') ON CONFLICT(tool_id) DO UPDATE SET "
                "provider=EXCLUDED.provider,operations_json=EXCLUDED.operations_json,status='active'",
                (stable("tool", t.code), t.code, t.provider, Jsonb(t.operations)),
            )
        conn.execute("UPDATE panky_tools SET status='disabled' WHERE NOT(tool_id=ANY(%s))",
                     (tool_ids,))
        for r in policy.resources:
            conn.execute(
                "INSERT INTO panky_resources(resource_id,tool_id,resource_kind,external_ref,status) "
                "VALUES (%s,%s,%s,%s,'active') ON CONFLICT(resource_id) DO UPDATE SET "
                "tool_id=EXCLUDED.tool_id,resource_kind=EXCLUDED.resource_kind,"
                "external_ref=EXCLUDED.external_ref,status='active'",
                (stable("resource", r.code), stable("tool", r.tool), r.resource_kind,
                 r.external_ref),
            )
        conn.execute("UPDATE panky_resources SET status='disabled' WHERE NOT(resource_id=ANY(%s))",
                     (resource_ids,))
        conn.execute("DELETE FROM panky_department_resources")
        for r in policy.resources:
            for department, relation in [(r.owner_department, "owned"),
                                          *[(d, "shared") for d in r.shared_departments]]:
                conn.execute(
                    "INSERT INTO panky_department_resources VALUES (%s,%s,%s)",
                    (stable("department", department), stable("resource", r.code), relation),
                )
        conn.execute("DELETE FROM panky_role_operations")
        for role in policy.roles:
            for op in role.operations:
                conn.execute("INSERT INTO panky_role_operations VALUES (%s,%s,%s)",
                             (stable("role", role.code), stable("tool", op.tool), op.operation))
        conn.execute("DELETE FROM panky_context_rules")
        for rule in policy.context_rules:
            conn.execute(
                "INSERT INTO panky_context_rules VALUES (%s,%s,%s,%s,%s,%s,%s,'active')",
                (uuid4(), rule.subject_kind,
                 stable("role", rule.role) if rule.role else None,
                 rule.target_class, rule.scope, rule.surface, rule.operation),
            )

        conn.execute("UPDATE panky_policy_releases SET status='superseded' WHERE status='active'")
        conn.execute(
            "INSERT INTO panky_policy_releases VALUES (%s,%s,'active',%s,%s)",
            (release_id, core_commit, at, publisher),
        )
        for g in policy.grants:
            subject = {
                "actor": (g.subject, None, None, None),
                "seat": (None, stable("seat", g.subject), None, None),
                "role": (None, None, stable("role", g.subject), None),
                "department": (None, None, None, stable("department", g.subject)),
            }[g.subject_kind]
            conn.execute(
                "INSERT INTO panky_grants(release_id,grant_id,subject_kind,actor_id,seat_id,"
                "role_id,department_id,target_kind,resource_id,context_node_id,surface,operation,"
                "effect,include_descendants,valid_from,expires_at,max_uses,reason,approved_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (release_id, uuid4(), g.subject_kind, *subject, g.target_kind,
                 stable("resource", g.target) if g.target_kind == "external_resource" else None,
                 g.target if g.target_kind == "context_node" else None,
                 g.surface, g.operation, g.effect, g.include_descendants,
                 g.valid_from, g.expires_at, g.max_uses, g.reason, g.approved_by),
            )
        conn.execute(
            "INSERT INTO panky_audit_events(release_id,target_kind,target_ref,operation,decision) "
            "VALUES (%s,'policy_release',%s,'publish','allow')",
            (release_id, core_commit),
        )
    return str(release_id)
