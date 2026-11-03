"""PostgreSQL decisions. Every request reads the active release transactionally."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from .decision import Candidate, select_grant
from .models import DecisionRequest, DecisionResponse, GrantUseCompletion


class PolicyStore:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def healthy(self) -> bool:
        with psycopg.connect(self.dsn, connect_timeout=3) as conn:
            return conn.execute("SELECT 1").fetchone()[0] == 1

    def decide(self, request: DecisionRequest) -> DecisionResponse:
        with psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=3) as conn:
            with conn.transaction():
                release = conn.execute(
                    "SELECT core_commit FROM panky_policy_releases "
                    "WHERE status = 'active'"
                ).fetchone()
                if release is None:
                    return DecisionResponse(allowed=False, reason="policy_unavailable")
                release_id = release["core_commit"]
                issuer = "https://accounts.google.com"
                actor = conn.execute(
                    "SELECT actor_id, oidc_subject, status, super_admin "
                    "FROM panky_actors WHERE oidc_issuer = %s AND email = %s FOR UPDATE",
                    (issuer, request.actor.email.casefold()),
                ).fetchone()
                if actor is None or actor["status"] != "active":
                    return DecisionResponse(
                        allowed=False, reason="actor_unknown", release=release_id
                    )
                if actor["oidc_subject"] is None:
                    conn.execute(
                        "UPDATE panky_actors SET oidc_subject = %s WHERE actor_id = %s",
                        (request.actor.subject, actor["actor_id"]),
                    )
                elif actor["oidc_subject"] != request.actor.subject:
                    return DecisionResponse(
                        allowed=False, reason="subject_mismatch", release=release_id
                    )
                actor_id = actor["actor_id"]
                if actor["super_admin"]:
                    if not request.preview:
                        self._audit(conn, actor_id, release_id, request, "allow", "super_admin")
                    return DecisionResponse(
                        allowed=True, reason="super_admin", actor_id=actor_id,
                        release=release_id,
                    )

                role_ids = [row["role_id"] for row in conn.execute(
                    "SELECT role_id FROM panky_actor_roles WHERE actor_id = %s",
                    (actor_id,),
                )]
                departments = list(conn.execute(
                    "WITH RECURSIVE ancestry AS ("
                    " SELECT d.department_id, d.parent_department_id, false AS inherited,"
                    " ARRAY[d.department_id] AS path"
                    " FROM panky_actor_departments ad"
                    " JOIN panky_departments d ON d.department_id = ad.department_id"
                    " WHERE ad.actor_id = %s"
                    " UNION ALL"
                    " SELECT p.department_id, p.parent_department_id, true,"
                    " a.path || p.department_id"
                    " FROM ancestry a JOIN panky_departments p"
                    " ON p.department_id = a.parent_department_id"
                    " WHERE NOT p.department_id = ANY(a.path)"
                    ") SELECT DISTINCT department_id, inherited FROM ancestry",
                    (actor_id,),
                ))
                dept_ids = [row["department_id"] for row in departments]
                direct_depts = {
                    row["department_id"] for row in departments if not row["inherited"]
                }
                rows = conn.execute(
                    "SELECT g.grant_id, g.actor_id, g.department_id, g.role_id,"
                    " g.inherit_to_children, g.operation, g.effect, g.valid_from,"
                    " g.expires_at, g.max_uses, t.provider, t.tool_name,"
                    " r.resource_kind, r.external_ref"
                    " FROM panky_grants g"
                    " JOIN panky_tools t ON t.tool_id = g.tool_id"
                    " JOIN panky_resources r ON r.resource_id = g.resource_id"
                    " WHERE g.release_id = (SELECT release_id FROM panky_policy_releases "
                    " WHERE status = 'active') AND t.provider = %s AND r.provider = %s AND ("
                    " g.actor_id = %s OR g.role_id = ANY(%s)"
                    " OR g.department_id = ANY(%s))",
                    (request.provider, request.provider, actor_id, role_ids, dept_ids),
                ).fetchall()
                candidates = []
                for row in rows:
                    if row["department_id"] is not None:
                        if (row["department_id"] not in direct_depts
                                and not row["inherit_to_children"]):
                            continue
                        subject = "department"
                    elif row["role_id"] is not None:
                        subject = "role"
                    else:
                        subject = "actor"
                    candidates.append(Candidate(
                        grant_id=str(row["grant_id"]), subject=subject,
                        effect=row["effect"], tool_name=row["tool_name"],
                        resource_kind=row["resource_kind"],
                        resource_ref=row["external_ref"],
                        operation=row["operation"], valid_from=row["valid_from"],
                        expires_at=row["expires_at"], max_uses=row["max_uses"],
                    ))
                selected = select_grant(
                    candidates, tool=request.tool,
                    resource_kind=request.resource_kind,
                    resource_ref=request.resource_ref,
                    parent_source=request.parent_source,
                    operation=request.operation, now=datetime.now(timezone.utc),
                )
                if selected is None or selected.effect == "deny":
                    reason = "default_deny" if selected is None else "explicit_deny"
                    if not request.preview:
                        self._audit(conn, actor_id, release_id, request, "deny", reason)
                    return DecisionResponse(
                        allowed=False, reason=reason, actor_id=actor_id,
                        release=release_id,
                    )
                reservation_id = None
                if selected.max_uses is not None:
                    conn.execute(
                        "SELECT grant_id FROM panky_grants WHERE grant_id = %s FOR UPDATE",
                        (selected.grant_id,),
                    )
                    duplicate = conn.execute(
                        "SELECT 1 FROM panky_grant_uses "
                        "WHERE grant_id = %s AND request_id = %s",
                        (selected.grant_id, request.request_id),
                    ).fetchone()
                    used = conn.execute(
                        "SELECT count(*) AS n FROM panky_grant_uses "
                        "WHERE grant_id = %s AND status IN "
                        "('reserved','committed','uncertain')",
                        (selected.grant_id,),
                    ).fetchone()["n"]
                    if duplicate or used >= selected.max_uses:
                        if not request.preview:
                            self._audit(conn, actor_id, release_id, request, "deny", "grant_exhausted")
                        return DecisionResponse(
                            allowed=False, reason="grant_exhausted",
                            actor_id=actor_id, release=release_id,
                        )
                    if not request.preview:
                        reservation_id = str(uuid4())
                        conn.execute(
                            "INSERT INTO panky_grant_uses "
                            "(use_id, grant_id, request_id, status) "
                            "VALUES (%s, %s, %s, 'reserved')",
                            (reservation_id, selected.grant_id, request.request_id),
                        )
                if not request.preview:
                    self._audit(conn, actor_id, release_id, request, "allow", "grant")
                return DecisionResponse(
                    allowed=True, reason="grant", actor_id=actor_id,
                    release=release_id, reservation_id=reservation_id,
                    grant_tool=selected.tool_name,
                )

    def complete_use(self, completion: GrantUseCompletion) -> bool:
        with psycopg.connect(self.dsn, connect_timeout=3) as conn:
            cursor = conn.execute(
                "UPDATE panky_grant_uses SET status = %s, finalized_at = now() "
                "WHERE use_id = %s AND request_id = %s AND status = 'reserved'",
                (completion.status, completion.reservation_id,
                 completion.request_id),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _audit(conn, actor_id, release, request, decision, reason):
        conn.execute(
            "INSERT INTO panky_audit_events "
            "(release_id, actor_id, event_type, decision, request_id, details) "
            "SELECT release_id, %s, 'authorize', %s, %s, %s "
            "FROM panky_policy_releases WHERE core_commit = %s",
            (actor_id, decision, request.request_id,
             psycopg.types.json.Jsonb({
                 "reason": reason, "provider": request.provider,
                 "tool": request.tool, "resource_kind": request.resource_kind,
                 "resource_ref": request.resource_ref,
                 "parent_source": request.parent_source,
                 "operation": request.operation,
             }), release),
        )
