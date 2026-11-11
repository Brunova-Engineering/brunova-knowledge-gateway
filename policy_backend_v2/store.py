"""Transactional policy decisions against a dedicated Panky 2.0 database."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from .decision import Decision, Request, decide


TABLES = {
    "actors": "panky_actors", "departments": "panky_departments",
    "roles": "panky_roles", "seats": "panky_seats",
    "actor_seats": "panky_actor_seats", "context_nodes": "panky_context_nodes",
    "context_rules": "panky_context_rules", "tools": "panky_tools",
    "resources": "panky_resources", "department_resources": "panky_department_resources",
    "role_operations": "panky_role_operations", "grants": "panky_grants",
}


class PolicyStoreV2:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def healthy(self) -> bool:
        with psycopg.connect(self.dsn, connect_timeout=3) as conn:
            return conn.execute(
                "SELECT 1 FROM panky_policy_releases WHERE status='active'"
            ).fetchone() is not None

    def actor_for_oidc(self, issuer: str, email: str) -> str | None:
        if issuer != "https://accounts.google.com":
            return None
        with psycopg.connect(self.dsn, connect_timeout=3) as conn:
            row = conn.execute(
                "SELECT actor_id FROM panky_actors WHERE oidc_issuer=%s "
                "AND email=%s AND status='active'",
                (issuer, email.casefold()),
            ).fetchone()
            return row[0] if row else None

    def context_catalog(self, *, issuer: str, subject: str, email: str) -> dict:
        """Return one release-consistent, actor-scoped map of context repos."""
        if issuer != "https://accounts.google.com" or not subject or not email:
            return {"status": "denied", "reason": "invalid_identity"}
        with psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=3) as conn:
            with conn.transaction():
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                release = conn.execute(
                    "SELECT release_id,core_commit FROM panky_policy_releases WHERE status='active'"
                ).fetchone()
                if release is None:
                    return {"status": "denied", "reason": "policy_unavailable"}
                actor = conn.execute(
                    "SELECT actor_id,oidc_subject,status FROM panky_actors "
                    "WHERE oidc_issuer=%s AND email=%s FOR UPDATE",
                    (issuer, email.casefold()),
                ).fetchone()
                if actor is None:
                    return {"status": "denied", "reason": "actor_unknown"}
                if actor["status"] != "active":
                    return {"status": "denied", "reason": "actor_disabled"}
                if actor["oidc_subject"] is not None and actor["oidc_subject"] != subject:
                    return {"status": "denied", "reason": "subject_mismatch"}
                if actor["oidc_subject"] is None:
                    conn.execute("UPDATE panky_actors SET oidc_subject=%s WHERE actor_id=%s",
                                 (subject, actor["actor_id"]))
                data = self._snapshot(conn, str(release["release_id"]))
                nodes = []
                for row in sorted(data["context_nodes"], key=lambda item: item["node_id"]):
                    if row["lifecycle"] != "active":
                        continue
                    readable = tuple(surface for surface in ("knowledge", "state", "work")
                                     if decide(data, Request(actor["actor_id"], "context_node",
                                                             row["node_id"], "read", surface)).allowed)
                    if "knowledge" not in readable:
                        continue
                    legacy_delivery = bool(row.get("delivery_repository") and not decide(
                        data, Request(actor["actor_id"], "context_node", row["node_id"],
                                      "update", "knowledge")).allowed)
                    generated = row["binding_mode"] == "generated" or legacy_delivery
                    # An authoritative checkout exposes knowledge, state and
                    # work together. Read-only recipients may mount it only
                    # when all three read decisions are currently allowed;
                    # update permission is checked separately on each write.
                    if not generated and len(readable) != 3:
                        continue
                    prefix = "delivery_" if legacy_delivery else ""
                    if not all(row.get(f"{prefix}{key}") for key in
                               ("repository", "store_id", "generation", "local_path")):
                        raise ValueError("Active context repository projection is incomplete")
                    nodes.append({
                        "node_id": row["node_id"], "parent_node_id": row["parent_node_id"],
                        "kind": row["kind"], "repository": row[f"{prefix}repository"],
                        "store_id": row[f"{prefix}store_id"],
                        "generation": row[f"{prefix}generation"],
                        "local_path": row[f"{prefix}local_path"],
                        "binding_mode": "generated" if generated else "authoritative",
                        "read_surfaces": ("knowledge",) if generated else readable,
                    })
                return {"status": "ready", "actor_id": actor["actor_id"],
                        "release": release["core_commit"], "nodes": nodes}

    def _snapshot(self, conn, release_id: str) -> dict:
        data = {name: list(conn.execute(f"SELECT * FROM {table}"))
                for name, table in TABLES.items()}
        data["active_release_id"] = release_id
        return data

    def visible_resources(self, *, issuer: str, subject: str, email: str) -> dict:
        """Return only registered resources readable by this verified Actor."""
        if issuer != "https://accounts.google.com" or not subject or not email:
            return {"status": "denied", "reason": "invalid_identity"}
        with psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=3) as conn:
            with conn.transaction():
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                release = conn.execute(
                    "SELECT release_id,core_commit FROM panky_policy_releases WHERE status='active'"
                ).fetchone()
                if release is None:
                    return {"status": "denied", "reason": "policy_unavailable"}
                actor = conn.execute(
                    "SELECT actor_id,oidc_subject,status FROM panky_actors "
                    "WHERE oidc_issuer=%s AND email=%s",
                    (issuer, email.casefold()),
                ).fetchone()
                if actor is None or actor["status"] != "active":
                    return {"status": "denied", "reason": "actor_unknown"}
                if actor["oidc_subject"] is not None and actor["oidc_subject"] != subject:
                    return {"status": "denied", "reason": "subject_mismatch"}
                data = self._snapshot(conn, str(release["release_id"]))
                tools = {str(row["tool_id"]): row["code"] for row in data["tools"]}
                resources = []
                for row in data["resources"]:
                    if row["status"] != "active":
                        continue
                    result = decide(data, Request(actor["actor_id"], "external_resource",
                                                  str(row["resource_id"]), "read"))
                    if result.allowed:
                        resources.append({
                            "tool": tools[str(row["tool_id"])],
                            "resource_kind": row["resource_kind"],
                            "external_ref": row["external_ref"],
                        })
                return {"status": "ready", "release": release["core_commit"],
                        "resources": resources}

    def decide(self, *, issuer: str, subject: str, email: str,
               request: Request, request_id: str, preview: bool = False) -> dict:
        if issuer != "https://accounts.google.com" or not subject or not email or not request_id:
            return {"allowed": False, "reason": "invalid_identity"}
        with psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=3) as conn:
            with conn.transaction():
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                release = conn.execute(
                    "SELECT release_id, core_commit FROM panky_policy_releases WHERE status='active'"
                ).fetchone()
                if release is None:
                    return {"allowed": False, "reason": "policy_unavailable"}
                actor = conn.execute(
                    "SELECT actor_id, oidc_subject, status FROM panky_actors "
                    "WHERE oidc_issuer=%s AND email=%s FOR UPDATE",
                    (issuer, email.casefold()),
                ).fetchone()
                if actor is None or actor["status"] != "active":
                    return {"allowed": False, "reason": "actor_unknown",
                            "release": release["core_commit"]}
                if actor["oidc_subject"] is not None and actor["oidc_subject"] != subject:
                    return {"allowed": False, "reason": "subject_mismatch",
                            "release": release["core_commit"]}
                if actor["oidc_subject"] is None and not preview:
                    conn.execute(
                        "UPDATE panky_actors SET oidc_subject=%s WHERE actor_id=%s",
                        (subject, actor["actor_id"]),
                    )
                if request.actor_id != actor["actor_id"]:
                    return {"allowed": False, "reason": "actor_mismatch",
                            "release": release["core_commit"]}
                data = self._snapshot(conn, str(release["release_id"]))
                result = decide(data, request)
                # A limited grant must be reserved atomically. If exhausted,
                # remove it and try the next complete Seat route.
                reservation_id = None
                while result.allowed and result.grant_id:
                    grant = next(g for g in data["grants"] if str(g["grant_id"]) == result.grant_id)
                    if grant["max_uses"] is None:
                        break
                    if not preview:
                        conn.execute("SELECT grant_id FROM panky_grants WHERE grant_id=%s FOR UPDATE",
                                     (result.grant_id,))
                        existing = conn.execute(
                            "SELECT use_id,status FROM panky_grant_uses WHERE grant_id=%s AND request_id=%s",
                            (result.grant_id, request_id),
                        ).fetchone()
                        if existing is not None:
                            result = Decision(False, "duplicate_request")
                            break
                    consumed = conn.execute(
                        "SELECT count(*) AS n FROM panky_grant_uses WHERE grant_id=%s "
                        "AND status IN ('reserved','committed','uncertain')",
                        (result.grant_id,),
                    ).fetchone()["n"]
                    if consumed < grant["max_uses"]:
                        if preview:
                            break
                        reservation_id = str(uuid4())
                        conn.execute(
                            "INSERT INTO panky_grant_uses "
                            "(use_id,grant_id,request_id,status,reserved_at) "
                            "VALUES (%s,%s,%s,'reserved',now())",
                            (reservation_id, result.grant_id, request_id),
                        )
                        break
                    data["grants"] = [g for g in data["grants"] if str(g["grant_id"]) != result.grant_id]
                    result = decide(data, request)
                if not preview:
                    conn.execute(
                        "INSERT INTO panky_audit_events "
                        "(release_id,actor_id,seat_id,target_kind,target_ref,surface,operation,decision) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (release["release_id"], actor["actor_id"], result.seat_id,
                         request.target_kind, request.target_id, request.surface,
                         request.operation, "allow" if result.allowed else "deny"),
                    )
                return {"allowed": result.allowed, "reason": result.reason,
                        "actor_id": actor["actor_id"], "seat_id": result.seat_id,
                        "grant_id": result.grant_id, "reservation_id": reservation_id,
                        "release": release["core_commit"]}

    def complete_use(self, reservation_id: str, request_id: str, status: str) -> bool:
        if status not in {"committed", "released", "uncertain"}:
            raise ValueError("Invalid grant completion status")
        with psycopg.connect(self.dsn, connect_timeout=3) as conn:
            result = conn.execute(
                "UPDATE panky_grant_uses SET status=%s, finalized_at=now() "
                "WHERE use_id=%s AND request_id=%s AND status='reserved'",
                (status, reservation_id, request_id),
            )
            return result.rowcount == 1
