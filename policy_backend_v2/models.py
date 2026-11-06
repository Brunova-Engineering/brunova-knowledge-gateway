"""Strict reviewed Core contract for one-company Panky 2.0 policy."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Actor(Strict):
    actor_id: str


class ContextNode(Strict):
    node_id: str
    parent_node_id: str | None
    kind: Literal["department", "seat", "subcontext"]
    visibility: Literal["inherit", "restricted", "owner"]
    lifecycle: Literal["active", "archived"] = "active"


class Department(Strict):
    code: str
    name: str
    parent: str | None = None
    context_node_id: str


class Tool(Strict):
    code: str
    provider: str
    operations: list[str] = Field(min_length=1)


class RoleOperation(Strict):
    tool: str
    operation: str


class Role(Strict):
    code: str
    name: str
    operations: list[RoleOperation] = Field(default_factory=list)


class Seat(Strict):
    code: str
    department: str
    role: str
    context_node_id: str
    reports_to: str | None = None
    occupancy: Literal["single", "multiple"] = "single"
    occupants: list[str] = Field(default_factory=list)


class Resource(Strict):
    code: str
    tool: str
    resource_kind: str
    external_ref: str
    owner_department: str
    shared_departments: list[str] = Field(default_factory=list)


class ContextRule(Strict):
    subject_kind: Literal["any_active_seat", "role_in_seat", "seat_occupant"]
    role: str | None = None
    target_class: Literal["department_context", "seat_context"]
    scope: Literal["department_subtree", "own_seat"]
    surface: Literal["shared", "knowledge", "state", "work", "contributions"]
    operation: str


class Grant(Strict):
    subject_kind: Literal["actor", "seat", "role", "department"]
    subject: str
    target_kind: Literal["external_resource", "context_node"]
    target: str
    surface: Literal["shared", "knowledge", "state", "work", "contributions"] | None = None
    operation: str
    effect: Literal["allow", "deny"]
    include_descendants: bool = False
    valid_from: datetime
    expires_at: datetime | None = None
    max_uses: int | None = Field(default=None, gt=0)
    reason: str = Field(min_length=3)
    approved_by: str

    @model_validator(mode="after")
    def check_grant(self):
        if (self.target_kind == "context_node") != (self.surface is not None):
            raise ValueError("Context grants need a surface; resource grants cannot have one")
        if self.expires_at is not None and self.expires_at <= self.valid_from:
            raise ValueError("Grant expiry must be after its start")
        if self.valid_from.tzinfo is None or (self.expires_at and self.expires_at.tzinfo is None):
            raise ValueError("Grant times must be timezone aware")
        if self.max_uses is not None and self.effect != "allow":
            raise ValueError("Usage limits apply only to allow grants")
        return self


class Policy(Strict):
    schema_version: Literal[2]
    tenant: str
    actors: list[Actor]
    context_nodes: list[ContextNode]
    departments: list[Department]
    roles: list[Role]
    seats: list[Seat]
    tools: list[Tool]
    resources: list[Resource]
    context_rules: list[ContextRule]
    grants: list[Grant] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_references(self):
        def unique(items, field):
            values = [getattr(item, field) for item in items]
            if len(values) != len(set(values)):
                raise ValueError(f"Duplicate {field}")
            return set(values)
        actors = unique(self.actors, "actor_id")
        nodes = unique(self.context_nodes, "node_id")
        departments = unique(self.departments, "code")
        roles = unique(self.roles, "code")
        seats = unique(self.seats, "code")
        tools = unique(self.tools, "code")
        resources = unique(self.resources, "code")
        node_map = {n.node_id: n for n in self.context_nodes}
        dept_map = {d.code: d for d in self.departments}
        seat_map = {s.code: s for s in self.seats}
        tool_map = {t.code: t for t in self.tools}
        for n in self.context_nodes:
            if n.parent_node_id is not None and n.parent_node_id not in nodes:
                raise ValueError("Unknown parent context node")
        for d in self.departments:
            if d.parent is not None and d.parent not in departments:
                raise ValueError("Unknown parent department")
            if d.context_node_id not in nodes or node_map[d.context_node_id].kind != "department":
                raise ValueError("Department needs its own department node")
        if len({d.context_node_id for d in self.departments}) != len(self.departments):
            raise ValueError("Department context nodes cannot be reused")
        for s in self.seats:
            if s.department not in departments or s.role not in roles:
                raise ValueError("Seat department or role is unknown")
            if s.context_node_id not in nodes or node_map[s.context_node_id].kind != "seat":
                raise ValueError("Seat needs its own seat node")
            if s.reports_to is not None and s.reports_to not in seats:
                raise ValueError("Seat manager is unknown")
            if not set(s.occupants) <= actors or len(s.occupants) != len(set(s.occupants)):
                raise ValueError("Unknown or duplicate Seat occupant")
            if s.occupancy == "single" and len(s.occupants) > 1:
                raise ValueError("Single Seat has multiple occupants")
            if node_map[s.context_node_id].parent_node_id != dept_map[s.department].context_node_id:
                raise ValueError("Seat node must be under its department node")
        if len({s.context_node_id for s in self.seats}) != len(self.seats):
            raise ValueError("Seat context nodes cannot be reused")
        for d in self.departments:
            expected_parent = dept_map[d.parent].context_node_id if d.parent else None
            if node_map[d.context_node_id].parent_node_id != expected_parent:
                raise ValueError("Department tree and context tree disagree")
        for name, parents in (
            ("department", {d.code: d.parent for d in self.departments}),
            ("context", {n.node_id: n.parent_node_id for n in self.context_nodes}),
            ("seat", {s.code: s.reports_to for s in self.seats}),
        ):
            for start in parents:
                seen = set()
                current = start
                while current is not None:
                    if current in seen:
                        raise ValueError(f"Cycle in {name} tree")
                    seen.add(current)
                    current = parents[current]
        for role in self.roles:
            for op in role.operations:
                if op.tool not in tools or op.operation not in tool_map[op.tool].operations:
                    raise ValueError("Role operation is not in its Tool catalog")
        for r in self.resources:
            if r.tool not in tools or r.owner_department not in departments:
                raise ValueError("Resource Tool or owner is unknown")
            if not set(r.shared_departments) <= departments:
                raise ValueError("Resource shares with an unknown department")
            if r.owner_department in r.shared_departments:
                raise ValueError("Resource owner cannot also be shared")
        for rule in self.context_rules:
            if (rule.subject_kind == "role_in_seat") != (rule.role is not None):
                raise ValueError("Context rule role mismatch")
            if rule.role and rule.role not in roles:
                raise ValueError("Context rule role is unknown")
        targets = {"external_resource": resources, "context_node": nodes}
        subjects = {"actor": actors, "seat": seats, "role": roles, "department": departments}
        for grant in self.grants:
            if grant.subject not in subjects[grant.subject_kind]:
                raise ValueError("Unknown grant subject")
            if grant.target not in targets[grant.target_kind]:
                raise ValueError("Unknown grant target")
            if grant.approved_by not in actors:
                raise ValueError("Unknown grant approver")
            if grant.target_kind == "external_resource":
                resource = next(r for r in self.resources if r.code == grant.target)
                if grant.operation not in tool_map[resource.tool].operations:
                    raise ValueError("Grant operation is not in its Tool catalog")
        return self
