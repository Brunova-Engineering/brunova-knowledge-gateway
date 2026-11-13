"""Compile a reviewed Core policy into the active Panky 2.0 projection."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4, uuid5

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .models import Policy
from .org_actions import CATALOG
from .decision import Request, decide_via_seat
from .store import PolicyStoreV2


NAMESPACE = UUID("8de4d0f8-35db-4ae0-bff9-b311e941b5ae")


def stable(kind: str, code: str):
    return uuid5(NAMESPACE, f"v2:{kind}:{code}")


def _current_request_route(conn, actor_id: str, seat_id: UUID,
                           department_id: UUID, *, admin: bool) -> bool:
    """Recheck an approval against current identity, Seat, and ancestry."""
    actor = conn.execute(
        "SELECT status,super_admin FROM panky_actors WHERE actor_id=%s",
        (actor_id,),
    ).fetchone()
    if actor is None or (actor["status"] if isinstance(actor, dict) else actor[0]) != "active":
        return False
    is_super_admin = actor["super_admin"] if isinstance(actor, dict) else actor[1]
    if is_super_admin:
        return True
    seat = conn.execute(
        "SELECT s.department_id,s.seat_type FROM panky_seats s "
        "JOIN panky_actor_seats a ON a.seat_id=s.seat_id "
        "WHERE s.seat_id=%s AND a.actor_id=%s AND s.status='active' "
        "AND a.active_from<=now() AND (a.active_until IS NULL OR a.active_until>now())",
        (seat_id, actor_id),
    ).fetchone()
    if seat is None:
        return False
    seat_type = seat["seat_type"] if isinstance(seat, dict) else seat[1]
    seat_department = seat["department_id"] if isinstance(seat, dict) else seat[0]
    if admin and seat_type != "admin":
        return False
    return bool(conn.execute(
        "WITH RECURSIVE lineage AS ("
        "SELECT department_id,parent_department_id FROM panky_departments WHERE department_id=%s "
        "UNION ALL SELECT d.department_id,d.parent_department_id FROM panky_departments d "
        "JOIN lineage l ON d.department_id=l.parent_department_id) "
        "SELECT 1 FROM lineage WHERE department_id=%s",
        (department_id, seat_department),
    ).fetchone())


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
        _node_repository(core_node)
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
        if policy.schema_version == 3 and seat.seat_type is None:
            if any("super_admin" not in registered[actor_id].get("roles", [])
                   for actor_id in seat.occupants):
                raise ValueError("A non-super-admin cannot occupy an untyped Seat")
    if policy.schema_version == 3 and "super_admin" not in registered[policy.founder_actor_id].get("roles", []):
        raise ValueError("Founder must be a Core super admin")
    for grant in policy.grants:
        if grant.target_kind == "org_action" and "super_admin" not in registered[grant.approved_by].get("roles", []):
            raise ValueError("Organizational action delegation requires a Core super admin approver")


def _node_repository(core_node: dict) -> dict:
    """Each organizational context has exactly one Git backing."""
    bindings = core_node.get("backing_bindings")
    if not isinstance(bindings, list) or len(bindings) != 1:
        raise ValueError("Protected context backings are unavailable")
    if bindings[0].get("mode") not in {"authoritative", "generated"}:
        raise ValueError("Context backing mode is invalid")
    for binding in bindings:
        if (not isinstance(binding.get("repository"), str)
                or not binding["repository"].startswith("Pancracio-s/")
                or not isinstance(binding.get("store_id"), str)
                or not isinstance(binding.get("generation"), int)
                or binding["generation"] < 1
                or not isinstance(binding.get("local_path"), str)):
            raise ValueError("Protected context repository binding is invalid")
    return bindings[0]


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
    with psycopg.connect(dsn) as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(8642092)")
        existing_release = conn.execute(
            "SELECT release_id,status FROM panky_policy_releases WHERE core_commit=%s",
            (core_commit,),
        ).fetchone()
        if existing_release is not None:
            if existing_release[1] == "active":
                return str(existing_release[0])
            raise ValueError("Core commit was already superseded")
        active = conn.execute(
            "SELECT release_id FROM panky_policy_releases WHERE status='active'"
        ).fetchone()
        accepted_shares = []
        accepted_transfer = None
        approved_admin_changes = []
        approved_resource_grants = []
        approved_tool_registrations = []
        approved_tool_retirements = []
        approved_resource_retirements = []
        approved_entity_retirements = []
        if active is not None:
            with conn.cursor(row_factory=dict_row) as cursor:
                previous = PolicyStoreV2("")._snapshot(cursor, str(active[0]))
            new_tools = {tool.code: tool for tool in policy.tools}
            for row in conn.execute(
                "SELECT request_id,provider,operations_json,requested_by_actor_id,"
                "requested_via_seat_id FROM panky_tool_registration_requests "
                "WHERE activated_release_id IS NULL FOR UPDATE"
            ):
                target = new_tools.get(row[1])
                result = decide_via_seat(previous, Request(
                    row[3], "org_action",
                    str(stable("org_action", "tool.register")), "invoke"
                ), str(row[4]))
                actor = next((item for item in previous["actors"]
                              if item["actor_id"] == row[3]), None)
                route_ok = bool(result.allowed and (
                    result.seat_id == str(row[4])
                    or (actor and actor["super_admin"] and conn.execute(
                        "SELECT 1 FROM panky_actor_seats a JOIN panky_seats s "
                        "ON s.seat_id=a.seat_id WHERE a.actor_id=%s AND a.seat_id=%s "
                        "AND s.status='active' AND a.active_from<=now() "
                        "AND (a.active_until IS NULL OR a.active_until>now())",
                        (row[3], row[4]),
                    ).fetchone())
                ))
                if (not route_ok or target is None or target.provider != row[1]
                        or target.operations != row[2]):
                    raise ValueError("Tool registration approval is stale or differs from Core")
                approved_tool_registrations.append(row[0])
            previous_tools = {row["code"]: row for row in previous["tools"]}
            new_resources = {resource.code for resource in policy.resources}
            for row in conn.execute(
                "SELECT request_id,provider,requested_by_actor_id,requested_via_seat_id "
                "FROM panky_tool_retirement_requests WHERE activated_release_id IS NULL FOR UPDATE"
            ):
                old_tool = previous_tools.get(row[1])
                result = decide_via_seat(previous, Request(
                    row[2], "org_action", str(stable("org_action", "tool.retire")),
                    "invoke"), str(row[3]))
                actor = next((item for item in previous["actors"]
                              if item["actor_id"] == row[2]), None)
                route_ok = bool(result.allowed and (
                    result.seat_id == str(row[3]) or
                    (actor and actor["super_admin"] and conn.execute(
                        "SELECT 1 FROM panky_actor_seats a JOIN panky_seats s "
                        "ON s.seat_id=a.seat_id WHERE a.actor_id=%s AND a.seat_id=%s "
                        "AND s.status='active' AND a.active_from<=now() "
                        "AND (a.active_until IS NULL OR a.active_until>now())",
                        (row[2], row[3]),
                    ).fetchone())
                ))
                if (not route_ok or old_tool is None or row[1] in new_tools
                        or any(resource["tool_id"] == old_tool["tool_id"]
                               and resource["code"] in new_resources
                               for resource in previous["resources"])
                        or any(operation["tool_id"] == old_tool["tool_id"]
                               for operation in previous["role_operations"])):
                    raise ValueError("Tool retirement approval is stale or has dependents")
                approved_tool_retirements.append(row[0])
            for row in conn.execute(
                "SELECT q.request_id,q.resource_id,q.requested_by_actor_id,"
                "q.requested_via_seat_id,r.code,r.owner_seat_id,d.department_id "
                "FROM panky_resource_retirement_requests q "
                "JOIN panky_resources r ON r.resource_id=q.resource_id "
                "JOIN panky_department_resources d ON d.resource_id=r.resource_id "
                "AND d.relation='owned' "
                "WHERE q.activated_release_id IS NULL FOR UPDATE OF q"
            ):
                seat_route = conn.execute(
                    "SELECT 1 FROM panky_actor_seats a JOIN panky_seats s "
                    "ON s.seat_id=a.seat_id WHERE a.actor_id=%s AND a.seat_id=%s "
                    "AND s.status='active' AND a.active_from<=now() "
                    "AND (a.active_until IS NULL OR a.active_until>now())",
                    (row[2], row[3]),
                ).fetchone()
                owner_route = row[3] == row[5]
                if (not seat_route or row[4] in new_resources
                        or not _current_request_route(
                            conn, row[2], row[3], row[6], admin=not owner_route)
                        or conn.execute(
                            "SELECT 1 FROM panky_resource_share_requests WHERE resource_id=%s "
                            "AND status IN ('pending','approved') AND activated_release_id IS NULL",
                            (row[1],),
                        ).fetchone()
                        or conn.execute(
                            "SELECT 1 FROM panky_resource_grant_requests WHERE resource_id=%s "
                            "AND activated_release_id IS NULL", (row[1],),
                        ).fetchone()
                        or any(grant.target_kind == "external_resource"
                               and grant.target == row[4] for grant in policy.grants)):
                    raise ValueError("Resource retirement approval is stale")
                approved_resource_retirements.append(row[0])
            current_actors = {item.actor_id for item in policy.actors}
            current_seats = {item.code for item in policy.seats}
            current_departments = {item.code for item in policy.departments}
            current_roles = {item.code for item in policy.roles}
            current_nodes = {item.node_id: item for item in policy.context_nodes}
            for row in conn.execute(
                "SELECT request_id,entity_kind,entity_code,requested_by_actor_id,"
                "requested_via_seat_id FROM panky_entity_retirement_requests "
                "WHERE activated_release_id IS NULL FOR UPDATE"
            ):
                kind, code = row[1], row[2]
                seat_route = conn.execute(
                    "SELECT 1 FROM panky_actor_seats a JOIN panky_seats s "
                    "ON s.seat_id=a.seat_id WHERE a.actor_id=%s AND a.seat_id=%s "
                    "AND s.status='active' AND a.active_from<=now() "
                    "AND (a.active_until IS NULL OR a.active_until>now())",
                    (row[3], row[4]),
                ).fetchone()
                if not seat_route:
                    raise ValueError("Entity retirement approval lost its Seat")
                if kind == "actor":
                    old = conn.execute(
                        "SELECT status,super_admin,founder FROM panky_actors WHERE actor_id=%s",
                        (code,),
                    ).fetchone()
                    departments_for_target = [item[0] for item in conn.execute(
                        "SELECT DISTINCT s.department_id FROM panky_actor_seats a "
                        "JOIN panky_seats s ON s.seat_id=a.seat_id "
                        "WHERE a.actor_id=%s AND s.status='active' "
                        "AND a.active_from<=now() "
                        "AND (a.active_until IS NULL OR a.active_until>now())",
                        (code,),
                    )]
                    removed = code not in current_actors
                    valid = bool(old and old[0] == "active" and not old[1]
                                 and not old[2] and code != row[3] and removed)
                elif kind == "seat":
                    old = conn.execute(
                        "SELECT department_id FROM panky_seats WHERE code=%s AND status='active'",
                        (code,),
                    ).fetchone()
                    departments_for_target = [old[0]] if old else []
                    valid = bool(old and code not in current_seats)
                elif kind == "department":
                    old = conn.execute(
                        "SELECT department_id FROM panky_departments WHERE code=%s AND status='active'",
                        (code,),
                    ).fetchone()
                    departments_for_target = [old[0]] if old else []
                    valid = bool(old and code not in current_departments)
                elif kind == "role":
                    old = conn.execute(
                        "SELECT 1 FROM panky_roles WHERE code=%s AND status='active'",
                        (code,),
                    ).fetchone()
                    departments_for_target = []
                    super_admin = conn.execute(
                        "SELECT 1 FROM panky_actors WHERE actor_id=%s "
                        "AND status='active' AND super_admin", (row[3],),
                    ).fetchone()
                    valid = bool(old and code not in current_roles and super_admin)
                elif kind == "subcontext":
                    old = conn.execute(
                        "SELECT 1 FROM panky_context_nodes WHERE node_id=%s "
                        "AND kind='subcontext' AND lifecycle='active'", (code,),
                    ).fetchone()
                    departments_for_target = [item[0] for item in conn.execute(
                        "WITH RECURSIVE lineage AS ("
                        "SELECT node_id,parent_node_id,0 AS depth FROM panky_context_nodes WHERE node_id=%s "
                        "UNION ALL SELECT n.node_id,n.parent_node_id,l.depth+1 "
                        "FROM panky_context_nodes n JOIN lineage l "
                        "ON n.node_id=l.parent_node_id) "
                        "SELECT d.department_id FROM lineage l JOIN panky_departments d "
                        "ON d.context_node_id=l.node_id AND d.status='active' "
                        "ORDER BY l.depth LIMIT 1", (code,),
                    )]
                    valid = bool(old and code in current_nodes
                                 and current_nodes[code].lifecycle == "archived")
                else:
                    valid = False
                    departments_for_target = []
                if (not valid or (kind != "role" and not departments_for_target
                                  and not conn.execute(
                                      "SELECT 1 FROM panky_actors WHERE actor_id=%s "
                                      "AND status='active' AND super_admin",
                                      (row[3],),
                                  ).fetchone())
                        or any(not _current_request_route(
                            conn, row[3], row[4], department_id, admin=True)
                               for department_id in departments_for_target)):
                    raise ValueError("Entity retirement approval is stale")
                approved_entity_retirements.append(row[0])
        if policy.schema_version == 3 and active is not None:
            old_admins = {row[0]: row[1] for row in conn.execute(
                "SELECT actor_id,super_admin FROM panky_actors WHERE status='active'"
            )}
            registered = registry.get("actors", registry)
            for actor_id in set(old_admins) | set(actor_ids):
                old = old_admins.get(actor_id, False)
                new = (actor_id in actor_ids and
                       "super_admin" in registered[actor_id].get("roles", []))
                if old == new:
                    continue
                action = "promote" if new else "revoke"
                change = conn.execute(
                    "SELECT change_id,requested_by_actor_id FROM panky_super_admin_changes "
                    "WHERE target_actor_id=%s AND action=%s AND activated_release_id IS NULL "
                    "FOR UPDATE",
                    (actor_id, action),
                ).fetchone()
                if change is None:
                    raise ValueError("Super admin change lacks governed approval")
                requester = conn.execute(
                    "SELECT super_admin,founder,status FROM panky_actors WHERE actor_id=%s",
                    (change[1],),
                ).fetchone()
                if (requester is None or requester[2] != "active"
                        or not requester[0] or (action == "revoke" and not requester[1])):
                    raise ValueError("Super admin approval is no longer authoritative")
                approved_admin_changes.append(change[0])
            previous_founder = conn.execute(
                "SELECT actor_id FROM panky_actors WHERE founder=true"
            ).fetchone()
            if previous_founder is not None and previous_founder[0] != policy.founder_actor_id:
                accepted_transfer = conn.execute(
                    "SELECT transfer_id FROM panky_founder_transfers "
                    "WHERE from_actor_id=%s AND to_actor_id=%s AND status='accepted' "
                    "AND activated_release_id IS NULL FOR UPDATE",
                    (previous_founder[0], policy.founder_actor_id),
                ).fetchone()
                if accepted_transfer is None:
                    raise ValueError("Founder change lacks accepted transfer")
            for resource in policy.resources:
                for target in resource.shared_departments:
                    resource_id = stable("resource", resource.code)
                    target_id = stable("department", target)
                    if conn.execute(
                        "SELECT 1 FROM panky_department_resources WHERE resource_id=%s "
                        "AND department_id=%s AND relation='shared'",
                        (resource_id, target_id),
                    ).fetchone():
                        continue
                    approved = conn.execute(
                        "SELECT request_id,requested_by_actor_id,requested_via_seat_id,"
                        "decided_by_actor_id,decided_via_seat_id "
                        "FROM panky_resource_share_requests "
                        "WHERE resource_id=%s AND source_department_id=%s "
                        "AND target_department_id=%s AND status='approved' "
                        "AND activated_release_id IS NULL FOR UPDATE",
                        (resource_id, stable("department", resource.owner_department),
                         target_id),
                    ).fetchone()
                    if approved is None:
                        raise ValueError("New department share lacks approval")
                    if (not _current_request_route(
                            conn, approved[1], approved[2],
                            stable("department", resource.owner_department), admin=True)
                            or not _current_request_route(
                                conn, approved[3], approved[4], target_id, admin=True)):
                        raise ValueError("Department share approval is stale")
                    accepted_shares.append(approved[0])
            for grant in policy.grants:
                if grant.target_kind != "external_resource" or grant.effect != "allow":
                    continue
                resource_id = stable("resource", grant.target)
                subject_id = (stable("seat", grant.subject) if grant.subject_kind == "seat"
                              else None)
                if subject_id is not None and conn.execute(
                    "SELECT 1 FROM panky_grants WHERE release_id=%s AND effect='allow' "
                    "AND target_kind='external_resource' AND subject_kind='seat' "
                    "AND seat_id=%s AND resource_id=%s AND operation=%s",
                    (active[0], subject_id, resource_id, grant.operation),
                ).fetchone():
                    continue
                if subject_id is None:
                    raise ValueError("New resource allow must target a Seat")
                approval = conn.execute(
                    "SELECT request_id,requested_by_actor_id,requested_via_seat_id "
                    "FROM panky_resource_grant_requests "
                    "WHERE resource_id=%s AND recipient_seat_id=%s AND operation=%s "
                    "AND action='allow' AND requested_by_actor_id=%s "
                    "AND activated_release_id IS NULL "
                    "FOR UPDATE",
                    (resource_id, subject_id, grant.operation, grant.approved_by),
                ).fetchone()
                if approval is None:
                    raise ValueError("New resource grant lacks owner or admin approval")
                owner_seat = stable("seat", next(
                    r.owner_seat for r in policy.resources if r.code == grant.target))
                recipient_seat = next(s for s in policy.seats if s.code == grant.subject)
                owner_department = stable("department", next(
                    r.owner_department for r in policy.resources if r.code == grant.target))
                scope_department = (stable("department", recipient_seat.department)
                                    if recipient_seat.department != next(
                                        r.owner_department for r in policy.resources
                                        if r.code == grant.target) else owner_department)
                is_owner = approval[2] == owner_seat and scope_department == owner_department
                if not _current_request_route(
                        conn, approval[1], approval[2], scope_department,
                        admin=not is_owner):
                    raise ValueError("Resource grant approval is stale")
                approved_resource_grants.append(approval[0])
            current_allows = {
                (row[0], row[1], row[2])
                for row in conn.execute(
                    "SELECT resource_id,seat_id,operation FROM panky_grants "
                    "WHERE release_id=%s AND target_kind='external_resource' "
                    "AND subject_kind='seat' AND effect='allow'",
                    (active[0],),
                )
            }
            next_allows = {
                (stable("resource", grant.target), stable("seat", grant.subject),
                 grant.operation)
                for grant in policy.grants
                if grant.target_kind == "external_resource" and grant.subject_kind == "seat"
                and grant.effect == "allow"
            }
            for resource_id, seat_id, operation in current_allows - next_allows:
                approval = conn.execute(
                    "SELECT request_id,requested_by_actor_id,requested_via_seat_id "
                    "FROM panky_resource_grant_requests WHERE resource_id=%s "
                    "AND recipient_seat_id=%s AND operation=%s AND action='revoke' "
                    "AND activated_release_id IS NULL FOR UPDATE",
                    (resource_id, seat_id, operation),
                ).fetchone()
                if approval is None:
                    raise ValueError("Resource grant removal lacks approval")
                owner = conn.execute(
                    "SELECT r.owner_seat_id,d.department_id FROM panky_resources r "
                    "JOIN panky_department_resources d ON d.resource_id=r.resource_id "
                    "AND d.relation='owned' WHERE r.resource_id=%s",
                    (resource_id,),
                ).fetchone()
                if owner is None or not _current_request_route(
                        conn, approval[1], approval[2], owner[1],
                        admin=approval[2] != owner[0]):
                    raise ValueError("Resource grant revocation approval is stale")
                approved_resource_grants.append(approval[0])
        conn.execute("UPDATE panky_actors SET founder=false")
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
        if policy.schema_version == 3:
            conn.execute("UPDATE panky_actors SET founder=true WHERE actor_id=%s",
                         (policy.founder_actor_id,))

        # Two-pass upserts avoid FK ordering dependencies in protected trees.
        for node in policy.context_nodes:
            source = _node_repository(topology["nodes"][node.node_id])
            conn.execute(
                "INSERT INTO panky_context_nodes(node_id,parent_node_id,kind,visibility,lifecycle,"
                "repository,store_id,generation,local_path,binding_mode,delivery_repository,delivery_store_id,"
                "delivery_generation,delivery_local_path) "
                "VALUES (%s,NULL,%s,%s,%s,%s,%s,%s,%s,%s,NULL,NULL,NULL,NULL) "
                "ON CONFLICT(node_id) DO UPDATE SET "
                "kind=EXCLUDED.kind,visibility=EXCLUDED.visibility,lifecycle=EXCLUDED.lifecycle,"
                "repository=EXCLUDED.repository,store_id=EXCLUDED.store_id,"
                "generation=EXCLUDED.generation,local_path=EXCLUDED.local_path,"
                "binding_mode=EXCLUDED.binding_mode,delivery_repository=NULL,"
                "delivery_store_id=NULL,delivery_generation=NULL,delivery_local_path=NULL",
                (node.node_id, node.kind, node.visibility, node.lifecycle,
                 source["repository"], source["store_id"], source["generation"],
                 source["local_path"], source["mode"]),
            )
        conn.execute("UPDATE panky_context_nodes SET lifecycle='archived' WHERE NOT(node_id=ANY(%s))",
                     (node_ids,))
        for node in policy.context_nodes:
            conn.execute("UPDATE panky_context_nodes SET parent_node_id=%s WHERE node_id=%s",
                         (node.parent_node_id, node.node_id))
        for d in policy.departments:
            conn.execute(
                "INSERT INTO panky_departments(department_id,code,name,context_node_id,status) "
                "VALUES (%s,%s,%s,%s,'active') ON CONFLICT(department_id) DO UPDATE SET "
                "name=EXCLUDED.name,context_node_id=EXCLUDED.context_node_id,status='active'",
                (stable("department", d.code), d.code, d.name, d.context_node_id),
            )
        conn.execute("UPDATE panky_departments SET status='disabled' "
                     "WHERE NOT(department_id=ANY(%s))", (department_ids,))
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
                "seat_type,occupancy,status) VALUES (%s,%s,%s,%s,%s,%s,%s,'active') "
                "ON CONFLICT(seat_id) DO UPDATE SET department_id=EXCLUDED.department_id,"
                "role_id=EXCLUDED.role_id,context_node_id=EXCLUDED.context_node_id,"
                "seat_type=EXCLUDED.seat_type,occupancy=EXCLUDED.occupancy,status='active'",
                (stable("seat", s.code), s.code, stable("department", s.department),
                 stable("role", s.role), s.context_node_id, s.seat_type, s.occupancy),
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
        # Old resource IDs stay for historical grants. Retire their active
        # references before a renamed code can claim the same external ref.
        conn.execute("UPDATE panky_resources SET status='disabled'")
        for r in policy.resources:
            conn.execute(
                "INSERT INTO panky_resources(resource_id,code,tool_id,owner_seat_id,resource_kind,external_ref,status) "
                "VALUES (%s,%s,%s,%s,%s,%s,'active') ON CONFLICT(resource_id) DO UPDATE SET "
                "code=EXCLUDED.code,tool_id=EXCLUDED.tool_id,resource_kind=EXCLUDED.resource_kind,"
                "owner_seat_id=EXCLUDED.owner_seat_id,external_ref=EXCLUDED.external_ref,status='active'",
                (stable("resource", r.code), r.code, stable("tool", r.tool),
                 stable("seat", r.owner_seat) if r.owner_seat else None, r.resource_kind,
                 r.external_ref),
            )
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
        if accepted_shares:
            conn.execute(
                "UPDATE panky_resource_share_requests SET activated_release_id=%s "
                "WHERE request_id=ANY(%s)",
                (release_id, accepted_shares),
            )
        if accepted_transfer is not None:
            conn.execute(
                "UPDATE panky_founder_transfers SET activated_release_id=%s "
                "WHERE transfer_id=%s",
                (release_id, accepted_transfer[0]),
            )
        if approved_admin_changes:
            conn.execute(
                "UPDATE panky_super_admin_changes SET activated_release_id=%s "
                "WHERE change_id=ANY(%s)",
                (release_id, approved_admin_changes),
            )
        if approved_resource_grants:
            conn.execute(
                "UPDATE panky_resource_grant_requests SET activated_release_id=%s "
                "WHERE request_id=ANY(%s)",
                (release_id, approved_resource_grants),
            )
        if approved_tool_registrations:
            conn.execute(
                "UPDATE panky_tool_registration_requests SET activated_release_id=%s "
                "WHERE request_id=ANY(%s)",
                (release_id, approved_tool_registrations),
            )
        if approved_tool_retirements:
            conn.execute(
                "UPDATE panky_tool_retirement_requests SET activated_release_id=%s "
                "WHERE request_id=ANY(%s)",
                (release_id, approved_tool_retirements),
            )
        if approved_resource_retirements:
            conn.execute(
                "UPDATE panky_resource_retirement_requests SET activated_release_id=%s "
                "WHERE request_id=ANY(%s)",
                (release_id, approved_resource_retirements),
            )
        if approved_entity_retirements:
            conn.execute(
                "UPDATE panky_entity_retirement_requests SET activated_release_id=%s "
                "WHERE request_id=ANY(%s)",
                (release_id, approved_entity_retirements),
            )
        for action in CATALOG.values():
            conn.execute(
                "INSERT INTO panky_org_actions(action_id,code,default_authority,scope_kind,delegable,governed_only) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(action_id) DO UPDATE SET "
                "code=EXCLUDED.code,default_authority=EXCLUDED.default_authority,"
                "scope_kind=EXCLUDED.scope_kind,delegable=EXCLUDED.delegable,"
                "governed_only=EXCLUDED.governed_only",
                (stable("org_action", action.code), action.code, action.default_authority,
                 action.scope_kind, action.delegable, action.governed_only),
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
                "role_id,department_id,target_kind,resource_id,context_node_id,org_action_id,surface,operation,"
                "effect,include_descendants,valid_from,expires_at,max_uses,reason,approved_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (release_id, uuid4(), g.subject_kind, *subject, g.target_kind,
                 stable("resource", g.target) if g.target_kind == "external_resource" else None,
                 g.target if g.target_kind == "context_node" else None,
                 stable("org_action", g.target) if g.target_kind == "org_action" else None,
                 g.surface, g.operation, g.effect, g.include_descendants,
                 g.valid_from, g.expires_at, g.max_uses, g.reason, g.approved_by),
            )
        conn.execute(
            "INSERT INTO panky_audit_events(release_id,target_kind,target_ref,operation,decision) "
            "VALUES (%s,'policy_release',%s,'publish','allow')",
            (release_id, core_commit),
        )
    return str(release_id)
