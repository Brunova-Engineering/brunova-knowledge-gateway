"""Versioned, closed catalog of organizational actions.

These are policy actions, separate from a Role's operations on an external Tool.
Founder and super-administrator transitions keep their dedicated governance flows.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OrgAction:
    code: str
    default_authority: str
    scope_kind: str
    delegable: bool = False
    governed_only: bool = False


CATALOG = {action.code: action for action in (
    OrgAction("department.manage", "admin", "department_subtree"),
    OrgAction("actor_seat.manage", "admin", "department_subtree"),
    OrgAction("resource_access.manage", "admin", "department_subtree"),
    OrgAction("share_cross_department.decide", "admin", "receiving_branch", governed_only=True),
    OrgAction("role.manage", "super_admin", "organization", governed_only=True),
    OrgAction("tool.register", "super_admin", "organization", delegable=True),
    OrgAction("tool.connector_configure", "super_admin", "provider", governed_only=True),
    OrgAction("super_admin.appoint", "super_admin", "organization", governed_only=True),
    OrgAction("super_admin.revoke", "founder", "organization", governed_only=True),
    OrgAction("founder.transfer", "founder", "organization", governed_only=True),
)}
