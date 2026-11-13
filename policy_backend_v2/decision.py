"""Panky 2.0 authorization over one complete Seat path at a time.

The caller provides a snapshot read in one PostgreSQL transaction. Core owns
the reviewed policy; these rows are its active operational projection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .org_actions import CATALOG


@dataclass(frozen=True)
class Request:
    actor_id: str
    target_kind: str  # external_resource | context_node | org_action
    target_id: str
    operation: str
    surface: str | None = None


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    seat_id: str | None = None
    grant_id: str | None = None


def _lookup(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    return {str(row[key]): row for row in rows}


def _is_current(row: dict[str, Any], now: datetime) -> bool:
    start, end = row.get("active_from"), row.get("active_until")
    return (start is None or start <= now) and (end is None or now < end)


def _descendant_of(child: str, parent: str, rows: dict[str, dict], parent_key: str) -> bool:
    seen: set[str] = set()
    current: str | None = child
    while current is not None:
        if current == parent:
            return True
        if current in seen:
            raise ValueError("Policy hierarchy contains a cycle")
        seen.add(current)
        record = rows.get(current)
        current = str(record[parent_key]) if record and record.get(parent_key) is not None else None
    return False


def _owner_department(node_id: str, nodes: dict[str, dict], departments: list[dict]) -> str | None:
    by_node = {str(row["context_node_id"]): str(row["department_id"]) for row in departments}
    seen: set[str] = set()
    current: str | None = node_id
    while current is not None:
        if current in by_node:
            return by_node[current]
        if current in seen:
            raise ValueError("Context hierarchy contains a cycle")
        seen.add(current)
        row = nodes.get(current)
        current = str(row["parent_node_id"]) if row and row.get("parent_node_id") is not None else None
    return None


def _grant_matches(grant: dict, request: Request, seat: dict, *,
                   departments: dict[str, dict], nodes: dict[str, dict], now: datetime) -> bool:
    if grant["target_kind"] != request.target_kind or grant["operation"] != request.operation:
        return False
    if grant.get("valid_from") and grant["valid_from"] > now:
        return False
    if grant.get("expires_at") and now >= grant["expires_at"]:
        return False
    if request.target_kind == "org_action":
        if grant["subject_kind"] != "seat" or grant["effect"] != "allow":
            return False
        if str(grant.get("org_action_id")) != request.target_id:
            return False
    elif request.target_kind == "external_resource":
        if str(grant.get("resource_id")) != request.target_id:
            return False
    else:
        if grant.get("surface") != request.surface:
            return False
        target = str(grant.get("context_node_id"))
        if target != request.target_id and not (
            grant.get("include_descendants") and
            _descendant_of(request.target_id, target, nodes, "parent_node_id")
        ):
            return False
    kind = grant["subject_kind"]
    if kind == "actor":
        return str(grant["actor_id"]) == request.actor_id
    if kind == "seat":
        return str(grant["seat_id"]) == str(seat["seat_id"])
    if kind == "role":
        return str(grant["role_id"]) == str(seat["role_id"])
    if kind == "department":
        subject = str(grant["department_id"])
        actual = str(seat["department_id"])
        return subject == actual or bool(grant.get("include_descendants") and
            _descendant_of(actual, subject, departments, "parent_department_id"))
    return False


def _role_allows_external(request: Request, seat: dict, data: dict) -> bool:
    resource = _lookup(data["resources"], "resource_id").get(request.target_id)
    if not resource or resource["status"] != "active":
        return False
    tool = _lookup(data["tools"], "tool_id").get(str(resource["tool_id"]))
    if not tool or tool["status"] != "active":
        return False
    return any(
        str(row["role_id"]) == str(seat["role_id"])
        and str(row["tool_id"]) == str(resource["tool_id"])
        and row["operation"] == request.operation
        for row in data["role_operations"]
    )


def _base_external(request: Request, seat: dict, data: dict, departments: dict) -> bool:
    resource = _lookup(data["resources"], "resource_id").get(request.target_id)
    if not resource:
        return False
    seat_dept = str(seat["department_id"])
    bindings = [row for row in data["department_resources"]
                if str(row["resource_id"]) == request.target_id]
    # Transitional v2 rows have no Seat owner. The v3 publisher must populate
    # it; until then retain the old decision so deployment can be staged.
    if resource.get("owner_seat_id") is None:
        return any(_descendant_of(str(row["department_id"]), seat_dept,
                                  departments, "parent_department_id") for row in bindings)
    if str(resource["owner_seat_id"]) == str(seat["seat_id"]):
        return True
    if seat.get("seat_type") != "admin":
        return False
    return any(_descendant_of(str(row["department_id"]), seat_dept,
                              departments, "parent_department_id") for row in bindings)


def _base_context(request: Request, seat: dict, data: dict,
                  departments: dict, nodes: dict) -> bool:
    node = nodes.get(request.target_id)
    if not node or node["lifecycle"] != "active":
        return False
    seat_node = str(seat["context_node_id"])
    seat_dept = str(seat["department_id"])
    seats = _lookup(data["seats"], "seat_id")
    target_seat = next((row for row in seats.values()
                        if str(row["context_node_id"]) == request.target_id), None)
    for rule in data["context_rules"]:
        if rule["status"] != "active" or rule["surface"] != request.surface or rule["operation"] != request.operation:
            continue
        kind = rule["subject_kind"]
        if kind == "role_in_seat" and str(rule["role_id"]) != str(seat["role_id"]):
            continue
        if kind == "seat_occupant":
            if rule["target_class"] == "seat_context" and request.target_id == seat_node:
                return True
            continue
        if (kind == "role_in_seat" and rule["target_class"] == "seat_context"
                and rule["scope"] == "reporting_subtree" and target_seat is not None
                and target_seat["status"] == "active"
                and str(target_seat["seat_id"]) != str(seat["seat_id"])
                and _descendant_of(str(target_seat["seat_id"]), str(seat["seat_id"]),
                                   seats, "reports_to_seat_id")):
            return True
        if kind not in {"role_in_seat", "any_active_seat"}:
            continue
        if rule["target_class"] != "department_context" or rule["scope"] != "department_subtree":
            continue
        if node["kind"] == "seat" or node["visibility"] == "restricted":
            continue
        owner = _owner_department(request.target_id, nodes, data["departments"])
        if owner is not None and _descendant_of(owner, seat_dept, departments, "parent_department_id"):
            return True
    return False


def decide(data: dict[str, list[dict]], request: Request, now: datetime | None = None) -> Decision:
    now = now or datetime.now(timezone.utc)
    if request.target_kind not in {"external_resource", "context_node", "org_action"}:
        return Decision(False, "invalid_target")
    actors = _lookup(data["actors"], "actor_id")
    actor = actors.get(request.actor_id)
    if not actor or actor["status"] != "active":
        return Decision(False, "actor_unknown")
    if request.target_kind == "org_action":
        target = _lookup(data.get("org_actions", []), "action_id").get(request.target_id)
        if (not target or target.get("code") not in CATALOG
                or target.get("code") not in {"tool.register", "tool.retire"}
                or not target.get("delegable") or target.get("governed_only")
                or request.operation != "invoke" or request.surface is not None):
            return Decision(False, "target_unavailable")
    elif request.target_kind == "context_node":
        target = _lookup(data["context_nodes"], "node_id").get(request.target_id)
        if not target or target["lifecycle"] != "active" or request.surface is None:
            return Decision(False, "target_unavailable")
    else:
        target = _lookup(data["resources"], "resource_id").get(request.target_id)
        if request.surface is not None:
            return Decision(False, "target_unavailable")
        # The Gateway has already verified that this provider and command exist.
        # Super admins retain the current broad Gateway catalog, including
        # tools without a resource row. Ordinary Seats still require one.
        if not target:
            return Decision(True, "super_admin") if actor["super_admin"] else Decision(False, "target_unavailable")
        if target["status"] != "active":
            return Decision(False, "target_unavailable")
        tool = _lookup(data["tools"], "tool_id").get(str(target["tool_id"]))
        if not tool or tool["status"] != "active":
            return Decision(False, "target_unavailable")
    if actor["super_admin"]:
        return Decision(True, "super_admin")
    departments = _lookup(data["departments"], "department_id")
    nodes = _lookup(data["context_nodes"], "node_id")
    seats = _lookup(data["seats"], "seat_id")
    roles = _lookup(data.get("roles", []), "role_id")
    active = sorted(
        [seats[str(a["seat_id"])] for a in data["actor_seats"]
         if str(a["actor_id"]) == request.actor_id and _is_current(a, now)
         and str(a["seat_id"]) in seats and seats[str(a["seat_id"])]["status"] == "active"
         and (not roles or roles.get(str(seats[str(a["seat_id"])]["role_id"]), {}).get("status") == "active")],
        key=lambda row: (row["code"], str(row["seat_id"])),
    )
    if not active:
        return Decision(False, "no_active_seat")
    grants = [g for g in data["grants"] if str(g["release_id"]) == str(data["active_release_id"])]
    # An Actor-level denial closes every Seat route.
    for seat in active:
        if any(g["subject_kind"] == "actor" and g["effect"] == "deny"
               and _grant_matches(g, request, seat, departments=departments, nodes=nodes, now=now)
               for g in grants):
            return Decision(False, "actor_deny")
    for seat in active:
        if request.target_kind == "org_action" and seat.get("seat_type") not in {"basic", "admin"}:
            continue
        if request.target_kind == "external_resource":
            if target.get("owner_seat_id") is not None and seat.get("seat_type") not in {"basic", "admin"}:
                continue
            if not _role_allows_external(request, seat, data):
                continue
        applicable = [g for g in grants if _grant_matches(
            g, request, seat, departments=departments, nodes=nodes, now=now)]
        if any(g["effect"] == "deny" for g in applicable):
            continue
        allows = sorted((g for g in applicable if g["effect"] == "allow"),
                        key=lambda g: str(g["grant_id"]))
        base = (_base_external(request, seat, data, departments) if
                request.target_kind == "external_resource" else
                _base_context(request, seat, data, departments, nodes) if
                request.target_kind == "context_node" else False)
        if base or allows:
            selected = allows[0] if allows and not base else None
            return Decision(True, "grant" if selected else "base", str(seat["seat_id"]),
                            str(selected["grant_id"]) if selected else None)
    return Decision(False, "no_authorized_seat")


def decide_via_seat(data: dict[str, list[dict]], request: Request,
                    seat_id: str, now: datetime | None = None) -> Decision:
    """Evaluate one complete Seat route; never borrow authority from another."""
    scoped = dict(data)
    scoped["actor_seats"] = [row for row in data["actor_seats"]
                             if str(row["actor_id"]) == request.actor_id
                             and str(row["seat_id"]) == seat_id]
    return decide(scoped, request, now)
