"""Deterministic grant precedence, separate from database and HTTP concerns."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Candidate:
    grant_id: str
    subject: str  # actor, role, department
    effect: str  # allow, deny
    tool_name: str
    resource_kind: str
    resource_ref: str
    operation: str
    valid_from: datetime
    expires_at: datetime | None
    max_uses: int | None


def select_grant(
    candidates: list[Candidate],
    *,
    tool: str,
    resource_kind: str,
    resource_ref: str,
    parent_source: str | None = None,
    operation: str,
    now: datetime,
) -> Candidate | None:
    applicable = [
        grant for grant in candidates
        if grant.valid_from <= now
        and (grant.expires_at is None or now < grant.expires_at)
        and grant.tool_name in {tool, "*"}
        and (
            (grant.resource_kind in {resource_kind, "*"}
             and grant.resource_ref in {resource_ref, "*"})
            or (parent_source is not None and grant.resource_kind == "source"
                and grant.resource_ref in {parent_source, "*"})
        )
        and grant.operation in {operation, "*"}
    ]
    if not applicable:
        return None
    # A direct denial wins; a direct exception can widen inherited policy.
    # For inherited policy, an explicit denial wins. Exact targets are chosen
    # before wildcards within a precedence tier for deterministic audit.
    tier = {
        ("actor", "deny"): 0,
        ("actor", "allow"): 1,
        ("role", "deny"): 2,
        ("department", "deny"): 2,
        ("role", "allow"): 3,
        ("department", "allow"): 3,
    }
    return min(
        applicable,
        key=lambda grant: (
            tier[(grant.subject, grant.effect)],
            0 if grant.resource_kind == resource_kind else 1,
            sum(value == "*" for value in (
                grant.tool_name, grant.resource_kind,
                grant.resource_ref, grant.operation,
            )),
            grant.grant_id,
        ),
    )
