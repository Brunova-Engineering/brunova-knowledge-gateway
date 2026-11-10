"""Governed, release-gated requests between organizational actors.

These requests never alter effective permissions. Core publication applies an
accepted request; until then the active projection remains unchanged.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row


def _actor(conn, issuer: str, subject: str, email: str):
    if issuer != "https://accounts.google.com" or not subject or not email:
        raise ValueError("invalid_identity")
    actor = conn.execute(
        "SELECT actor_id,oidc_subject,status,super_admin,founder FROM panky_actors "
        "WHERE oidc_issuer=%s AND email=%s FOR UPDATE",
        (issuer, email.casefold()),
    ).fetchone()
    if actor is None or actor["status"] != "active":
        raise ValueError("actor_unknown")
    if actor["oidc_subject"] is not None and actor["oidc_subject"] != subject:
        raise ValueError("subject_mismatch")
    if actor["oidc_subject"] is None:
        conn.execute("UPDATE panky_actors SET oidc_subject=%s WHERE actor_id=%s",
                     (subject, actor["actor_id"]))
    return actor


def _seat(conn, actor_id: str, seat_id: UUID, department_id: UUID,
          *, ancestor: bool = False):
    """A complete current Seat route; authority cannot borrow another Seat."""
    row = conn.execute(
        "SELECT s.seat_id,s.department_id,s.seat_type FROM panky_seats s "
        "JOIN panky_actor_seats a ON a.seat_id=s.seat_id "
        "WHERE s.seat_id=%s AND a.actor_id=%s AND s.status='active' "
        "AND a.active_from<=now() AND (a.active_until IS NULL OR a.active_until>now())",
        (seat_id, actor_id),
    ).fetchone()
    if row is None:
        raise ValueError("seat_unavailable")
    if row["seat_type"] != "admin":
        raise ValueError("admin_seat_required")
    if row["department_id"] == department_id:
        return row
    if ancestor and conn.execute(
        "WITH RECURSIVE lineage AS ("
        "SELECT department_id,parent_department_id FROM panky_departments WHERE department_id=%s "
        "UNION ALL SELECT d.department_id,d.parent_department_id FROM panky_departments d "
        "JOIN lineage l ON d.department_id=l.parent_department_id) "
        "SELECT 1 FROM lineage WHERE department_id=%s",
        (department_id, row["department_id"]),
    ).fetchone():
        return row
    raise ValueError("seat_out_of_scope")


def _actor_seat(conn, actor_id: str, seat_id: UUID) -> None:
    """Even a super admin must attribute a request to their own active Seat."""
    if conn.execute(
        "SELECT 1 FROM panky_seats s JOIN panky_actor_seats a ON a.seat_id=s.seat_id "
        "WHERE s.seat_id=%s AND a.actor_id=%s AND s.status='active' "
        "AND a.active_from<=now() AND (a.active_until IS NULL OR a.active_until>now())",
        (seat_id, actor_id),
    ).fetchone() is None:
        raise ValueError("seat_unavailable")


class GovernanceStore:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def resource_sharing(self, *, issuer: str, subject: str, email: str,
                         resource_id: UUID, via_seat_id: UUID) -> dict:
        """Expose active explicit shares to the owner or a managing Seat."""
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn, conn.transaction():
            actor = _actor(conn, issuer, subject, email)
            resource = conn.execute(
                "SELECT r.owner_seat_id,d.department_id FROM panky_resources r "
                "JOIN panky_department_resources d ON d.resource_id=r.resource_id "
                "AND d.relation='owned' WHERE r.resource_id=%s AND r.status='active'",
                (resource_id,),
            ).fetchone()
            if resource is None:
                raise ValueError("resource_unavailable")
            if not actor["super_admin"]:
                _actor_seat(conn, actor["actor_id"], via_seat_id)
                if via_seat_id != resource["owner_seat_id"]:
                    _seat(conn, actor["actor_id"], via_seat_id,
                          resource["department_id"], ancestor=True)
            grants = [
                {"recipient_seat_id": str(row["seat_id"]),
                 "recipient_seat": row["seat_code"], "operation": row["operation"]}
                for row in conn.execute(
                    "SELECT g.seat_id,s.code AS seat_code,g.operation FROM panky_grants g "
                    "JOIN panky_seats s ON s.seat_id=g.seat_id "
                    "JOIN panky_policy_releases p ON p.release_id=g.release_id "
                    "AND p.status='active' WHERE g.subject_kind='seat' "
                    "AND g.target_kind='external_resource' AND g.resource_id=%s "
                    "AND g.effect='allow' ORDER BY s.code,g.operation",
                    (resource_id,),
                )
            ]
            departments = [
                {"department_id": str(row["department_id"]), "department": row["code"]}
                for row in conn.execute(
                    "SELECT d.department_id,d.code FROM panky_department_resources x "
                    "JOIN panky_departments d ON d.department_id=x.department_id "
                    "WHERE x.resource_id=%s AND x.relation='shared' ORDER BY d.code",
                    (resource_id,),
                )
            ]
            return {"resource_id": str(resource_id), "active_grants": grants,
                    "shared_departments": departments}

    def inbox(self, *, issuer: str, subject: str, email: str) -> dict:
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn, conn.transaction():
            actor = _actor(conn, issuer, subject, email)
            shares = []
            for request in conn.execute(
                "SELECT q.request_id,q.resource_id,q.source_department_id,"
                "q.target_department_id,q.reason,q.requested_at,"
                "r.external_ref,r.resource_kind,t.code AS tool_code,"
                "source.code AS source_code,target.code AS target_code "
                "FROM panky_resource_share_requests q "
                "JOIN panky_resources r ON r.resource_id=q.resource_id "
                "JOIN panky_tools t ON t.tool_id=r.tool_id "
                "JOIN panky_departments source ON source.department_id=q.source_department_id "
                "JOIN panky_departments target ON target.department_id=q.target_department_id "
                "WHERE q.status='pending' "
                "ORDER BY q.requested_at LIMIT 100"
            ):
                if actor["super_admin"]:
                    visible = True
                else:
                    visible = False
                    for seat in conn.execute(
                        "SELECT s.seat_id FROM panky_seats s "
                        "JOIN panky_actor_seats a ON a.seat_id=s.seat_id "
                        "WHERE a.actor_id=%s AND s.status='active' "
                        "AND s.seat_type='admin' AND a.active_from<=now() "
                        "AND (a.active_until IS NULL OR a.active_until>now())",
                        (actor["actor_id"],),
                    ):
                        try:
                            _seat(conn, actor["actor_id"], seat["seat_id"],
                                  request["target_department_id"], ancestor=True)
                            visible = True
                            break
                        except ValueError:
                            pass
                if visible:
                    shares.append({
                        "request_id": str(request["request_id"]),
                        "resource_id": str(request["resource_id"]),
                        "source_department_id": str(request["source_department_id"]),
                        "target_department_id": str(request["target_department_id"]),
                        "source_department": request["source_code"],
                        "target_department": request["target_code"],
                        "tool": request["tool_code"],
                        "resource_kind": request["resource_kind"],
                        "external_ref": request["external_ref"],
                        "reason": request["reason"],
                    })
            transfers = [
                {"transfer_id": str(row["transfer_id"]), "from_actor_id": row["from_actor_id"],
                 "expires_at": row["expires_at"].isoformat()}
                for row in conn.execute(
                    "SELECT transfer_id,from_actor_id,expires_at "
                    "FROM panky_founder_transfers WHERE to_actor_id=%s AND status='pending' "
                    "AND expires_at>now() ORDER BY requested_at LIMIT 20",
                    (actor["actor_id"],),
                )
            ]
            return {"share_requests": shares, "founder_transfers": transfers}

    def approved_for_core(self) -> dict:
        """Return only approved, unused changes to the verified Core workflow."""
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            shares = [
                {"request_id": str(row["request_id"]), "resource": row["resource_code"],
                 "target_department": row["target_code"]}
                for row in conn.execute(
                    "SELECT q.request_id,r.code AS resource_code,"
                    "d.code AS target_code FROM panky_resource_share_requests q "
                    "JOIN panky_resources r ON r.resource_id=q.resource_id "
                    "JOIN panky_departments d ON d.department_id=q.target_department_id "
                    "WHERE q.status='approved' AND q.activated_release_id IS NULL "
                    "ORDER BY q.requested_at"
                )
            ]
            grants = [
                {"request_id": str(row["request_id"]), "resource": row["resource_code"],
                 "recipient_seat": row["seat_code"], "operation": row["operation"],
                 "action": row["action"],
                 "approved_by": row["requested_by_actor_id"], "reason": row["reason"]}
                for row in conn.execute(
                    "SELECT q.request_id,r.code AS resource_code,s.code AS seat_code,"
                    "q.operation,q.action,q.requested_by_actor_id,q.reason "
                    "FROM panky_resource_grant_requests q "
                    "JOIN panky_resources r ON r.resource_id=q.resource_id "
                    "JOIN panky_seats s ON s.seat_id=q.recipient_seat_id "
                    "WHERE q.activated_release_id IS NULL ORDER BY q.requested_at"
                )
            ]
            admins = [
                {"change_id": str(row["change_id"]), "target_actor": row["target_actor_id"],
                 "action": row["action"]}
                for row in conn.execute(
                    "SELECT change_id,target_actor_id,action FROM panky_super_admin_changes "
                    "WHERE activated_release_id IS NULL ORDER BY requested_at"
                )
            ]
            transfers = [
                {"transfer_id": str(row["transfer_id"]), "from_actor": row["from_actor_id"],
                 "to_actor": row["to_actor_id"]}
                for row in conn.execute(
                    "SELECT transfer_id,from_actor_id,to_actor_id "
                    "FROM panky_founder_transfers WHERE status='accepted' "
                    "AND activated_release_id IS NULL ORDER BY requested_at"
                )
            ]
            if any(item.get("resource") is None for item in [*shares, *grants]):
                raise ValueError("Approved request lacks a Core resource code")
            return {"shares": shares, "resource_grants": grants,
                    "super_admin_changes": admins, "founder_transfers": transfers}

    def request_share(self, *, issuer: str, subject: str, email: str,
                      resource_id: UUID, target_department_id: UUID,
                      via_seat_id: UUID, reason: str) -> dict:
        if len(reason.strip()) < 3:
            raise ValueError("reason_required")
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn, conn.transaction():
            actor = _actor(conn, issuer, subject, email)
            if not actor["super_admin"]:
                _actor_seat(conn, actor["actor_id"], via_seat_id)
            resource = conn.execute(
                "SELECT r.resource_id,dr.department_id FROM panky_resources r "
                "JOIN panky_department_resources dr ON dr.resource_id=r.resource_id "
                "AND dr.relation='owned' WHERE r.resource_id=%s AND r.status='active'",
                (resource_id,),
            ).fetchone()
            if resource is None:
                raise ValueError("resource_unavailable")
            if resource["department_id"] == target_department_id:
                raise ValueError("same_department")
            if not conn.execute(
                "SELECT 1 FROM panky_departments WHERE department_id=%s",
                (target_department_id,),
            ).fetchone():
                raise ValueError("target_unknown")
            if not actor["super_admin"]:
                _seat(conn, actor["actor_id"], via_seat_id, resource["department_id"],
                      ancestor=True)
            if conn.execute(
                "SELECT 1 FROM panky_department_resources WHERE resource_id=%s "
                "AND department_id=%s AND relation='shared'",
                (resource_id, target_department_id),
            ).fetchone():
                raise ValueError("already_shared")
            request_id = uuid4()
            status = "approved" if actor["super_admin"] else "pending"
            conn.execute(
                "INSERT INTO panky_resource_share_requests "
                "(request_id,resource_id,source_department_id,target_department_id,"
                "requested_by_actor_id,requested_via_seat_id,decided_by_actor_id,"
                "decided_via_seat_id,status,reason,requested_at,decided_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),%s)",
                (request_id, resource_id, resource["department_id"], target_department_id,
                 actor["actor_id"], via_seat_id,
                 actor["actor_id"] if actor["super_admin"] else None,
                 via_seat_id if actor["super_admin"] else None,
                 status, reason.strip(),
                 datetime.now(timezone.utc) if actor["super_admin"] else None),
            )
            return {"request_id": str(request_id), "status": status, "effective": False}

    def decide_share(self, *, issuer: str, subject: str, email: str,
                     request_id: UUID, via_seat_id: UUID, accept: bool) -> dict:
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn, conn.transaction():
            actor = _actor(conn, issuer, subject, email)
            if not actor["super_admin"]:
                _actor_seat(conn, actor["actor_id"], via_seat_id)
            request = conn.execute(
                "SELECT * FROM panky_resource_share_requests WHERE request_id=%s FOR UPDATE",
                (request_id,),
            ).fetchone()
            if request is None or request["status"] != "pending":
                raise ValueError("request_not_pending")
            if not actor["super_admin"]:
                _seat(conn, actor["actor_id"], via_seat_id,
                      request["target_department_id"], ancestor=True)
            status = "approved" if accept else "rejected"
            conn.execute(
                "UPDATE panky_resource_share_requests SET status=%s,"
                "decided_by_actor_id=%s,decided_via_seat_id=%s,decided_at=now() "
                "WHERE request_id=%s",
                (status, actor["actor_id"], via_seat_id, request_id),
            )
            return {"request_id": str(request_id), "status": status, "effective": False}

    def request_founder_transfer(self, *, issuer: str, subject: str, email: str,
                                 target_actor_id: str, hours: int = 72) -> dict:
        if not 1 <= hours <= 168:
            raise ValueError("invalid_deadline")
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn, conn.transaction():
            actor = _actor(conn, issuer, subject, email)
            if not actor["founder"]:
                raise ValueError("founder_required")
            target = conn.execute(
                "SELECT actor_id FROM panky_actors WHERE actor_id=%s AND status='active' "
                "AND super_admin=true",
                (target_actor_id,),
            ).fetchone()
            if target is None or target["actor_id"] == actor["actor_id"]:
                raise ValueError("target_must_be_other_super_admin")
            transfer_id = uuid4()
            expires = datetime.now(timezone.utc) + timedelta(hours=hours)
            conn.execute(
                "INSERT INTO panky_founder_transfers "
                "(transfer_id,from_actor_id,to_actor_id,status,requested_at,expires_at) "
                "VALUES (%s,%s,%s,'pending',now(),%s)",
                (transfer_id, actor["actor_id"], target_actor_id, expires),
            )
            return {"transfer_id": str(transfer_id), "status": "pending",
                    "effective": False, "expires_at": expires.isoformat()}

    def decide_founder_transfer(self, *, issuer: str, subject: str, email: str,
                                transfer_id: UUID, accept: bool) -> dict:
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn, conn.transaction():
            actor = _actor(conn, issuer, subject, email)
            transfer = conn.execute(
                "SELECT * FROM panky_founder_transfers WHERE transfer_id=%s FOR UPDATE",
                (transfer_id,),
            ).fetchone()
            if transfer is None or transfer["status"] != "pending":
                raise ValueError("transfer_not_pending")
            if actor["actor_id"] != transfer["to_actor_id"] or not actor["super_admin"]:
                raise ValueError("recipient_super_admin_required")
            if transfer["expires_at"] <= datetime.now(timezone.utc):
                conn.execute("UPDATE panky_founder_transfers SET status='expired',decided_at=now() "
                             "WHERE transfer_id=%s", (transfer_id,))
                return {"transfer_id": str(transfer_id), "status": "expired",
                        "effective": False}
            if not conn.execute(
                "SELECT 1 FROM panky_actors WHERE actor_id=%s AND founder=true",
                (transfer["from_actor_id"],),
            ).fetchone():
                raise ValueError("founder_changed")
            status = "accepted" if accept else "rejected"
            conn.execute(
                "UPDATE panky_founder_transfers SET status=%s,decided_at=now() "
                "WHERE transfer_id=%s", (status, transfer_id),
            )
            return {"transfer_id": str(transfer_id), "status": status, "effective": False}

    def change_super_admin(self, *, issuer: str, subject: str, email: str,
                           target_actor_id: str, action: str, reason: str) -> dict:
        if action not in {"promote", "revoke"} or len(reason.strip()) < 3:
            raise ValueError("invalid_change")
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn, conn.transaction():
            actor = _actor(conn, issuer, subject, email)
            if not actor["super_admin"] or (action == "revoke" and not actor["founder"]):
                raise ValueError("insufficient_authority")
            target = conn.execute(
                "SELECT actor_id,super_admin,founder,status FROM panky_actors "
                "WHERE actor_id=%s FOR UPDATE",
                (target_actor_id,),
            ).fetchone()
            if target is None or target["status"] != "active":
                raise ValueError("target_unavailable")
            if action == "promote" and target["super_admin"]:
                raise ValueError("already_super_admin")
            if action == "revoke" and (not target["super_admin"] or target["founder"]):
                raise ValueError("cannot_revoke_target")
            change_id = uuid4()
            conn.execute(
                "INSERT INTO panky_super_admin_changes "
                "(change_id,target_actor_id,requested_by_actor_id,action,reason) "
                "VALUES (%s,%s,%s,%s,%s)",
                (change_id, target_actor_id, actor["actor_id"], action, reason.strip()),
            )
            return {"change_id": str(change_id), "status": "approved", "effective": False}

    def request_resource_grant(self, *, issuer: str, subject: str, email: str,
                               resource_id: UUID, recipient_seat_id: UUID,
                               via_seat_id: UUID, operation: str, reason: str) -> dict:
        if not operation or len(reason.strip()) < 3:
            raise ValueError("invalid_grant")
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn, conn.transaction():
            actor = _actor(conn, issuer, subject, email)
            if not actor["super_admin"]:
                _actor_seat(conn, actor["actor_id"], via_seat_id)
            resource = conn.execute(
                "SELECT r.resource_id,r.owner_seat_id,r.tool_id,d.department_id "
                "FROM panky_resources r JOIN panky_department_resources d "
                "ON d.resource_id=r.resource_id AND d.relation='owned' "
                "WHERE r.resource_id=%s AND r.status='active'",
                (resource_id,),
            ).fetchone()
            recipient = conn.execute(
                "SELECT seat_id,department_id,role_id,status FROM panky_seats "
                "WHERE seat_id=%s", (recipient_seat_id,)
            ).fetchone()
            if resource is None or recipient is None or recipient["status"] != "active":
                raise ValueError("target_unavailable")
            if resource["owner_seat_id"] is None:
                raise ValueError("resource_owner_unavailable")
            cross_department = recipient["department_id"] != resource["department_id"]
            if cross_department and not conn.execute(
                "SELECT 1 FROM panky_department_resources "
                "WHERE resource_id=%s AND department_id=%s AND relation='shared'",
                (resource_id, recipient["department_id"]),
            ).fetchone():
                raise ValueError("cross_department_requires_acceptance")
            if not conn.execute(
                "SELECT 1 FROM panky_role_operations WHERE role_id=%s "
                "AND tool_id=%s AND operation=%s",
                (recipient["role_id"], resource["tool_id"], operation),
            ).fetchone():
                raise ValueError("recipient_role_denies_operation")
            if not actor["super_admin"]:
                route = conn.execute(
                    "SELECT s.seat_id,s.department_id,s.seat_type FROM panky_seats s "
                    "JOIN panky_actor_seats a ON a.seat_id=s.seat_id "
                    "WHERE s.seat_id=%s AND a.actor_id=%s AND s.status='active' "
                    "AND a.active_from<=now() AND "
                    "(a.active_until IS NULL OR a.active_until>now())",
                    (via_seat_id, actor["actor_id"]),
                ).fetchone()
                if route is None:
                    raise ValueError("seat_unavailable")
                if cross_department:
                    _seat(conn, actor["actor_id"], via_seat_id,
                          recipient["department_id"], ancestor=True)
                elif route["seat_id"] != resource["owner_seat_id"]:
                    _seat(conn, actor["actor_id"], via_seat_id,
                          resource["department_id"], ancestor=True)
            request_id = uuid4()
            conn.execute(
                "INSERT INTO panky_resource_grant_requests "
                "(request_id,resource_id,recipient_seat_id,operation,"
                "requested_by_actor_id,requested_via_seat_id,reason) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (request_id, resource_id, recipient_seat_id, operation,
                 actor["actor_id"], via_seat_id, reason.strip()),
            )
            return {"request_id": str(request_id), "status": "approved",
                    "effective": False}

    def revoke_resource_grant(self, *, issuer: str, subject: str, email: str,
                              resource_id: UUID, recipient_seat_id: UUID,
                              via_seat_id: UUID, operation: str, reason: str) -> dict:
        """Withdraw one explicit share in the next Core release, preserving history."""
        if not operation or len(reason.strip()) < 3:
            raise ValueError("invalid_grant_revoke")
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn, conn.transaction():
            actor = _actor(conn, issuer, subject, email)
            resource = conn.execute(
                "SELECT r.owner_seat_id,d.department_id FROM panky_resources r "
                "JOIN panky_department_resources d ON d.resource_id=r.resource_id "
                "AND d.relation='owned' WHERE r.resource_id=%s AND r.status='active'",
                (resource_id,),
            ).fetchone()
            if resource is None:
                raise ValueError("resource_unavailable")
            current = conn.execute(
                "SELECT 1 FROM panky_grants g JOIN panky_policy_releases p "
                "ON p.release_id=g.release_id AND p.status='active' "
                "WHERE g.subject_kind='seat' AND g.seat_id=%s "
                "AND g.target_kind='external_resource' AND g.resource_id=%s "
                "AND g.operation=%s AND g.effect='allow'",
                (recipient_seat_id, resource_id, operation),
            ).fetchone()
            if current is None:
                raise ValueError("grant_not_active")
            if not actor["super_admin"]:
                _actor_seat(conn, actor["actor_id"], via_seat_id)
                if via_seat_id != resource["owner_seat_id"]:
                    _seat(conn, actor["actor_id"], via_seat_id,
                          resource["department_id"], ancestor=True)
            request_id = uuid4()
            conn.execute(
                "INSERT INTO panky_resource_grant_requests "
                "(request_id,resource_id,recipient_seat_id,operation,action,"
                "requested_by_actor_id,requested_via_seat_id,reason) "
                "VALUES (%s,%s,%s,%s,'revoke',%s,%s,%s)",
                (request_id, resource_id, recipient_seat_id, operation,
                 actor["actor_id"], via_seat_id, reason.strip()),
            )
            return {"request_id": str(request_id), "status": "approved",
                    "effective": False}
