BEGIN;
ALTER TABLE panky_actors ADD COLUMN founder boolean NOT NULL DEFAULT false;
ALTER TABLE panky_actors ADD CONSTRAINT panky_founder_super_admin
  CHECK (NOT founder OR (super_admin AND status = 'active'));
CREATE UNIQUE INDEX panky_one_founder ON panky_actors(founder) WHERE founder;
ALTER TABLE panky_seats ADD COLUMN seat_type text CHECK (seat_type IN ('basic','admin'));
ALTER TABLE panky_resources ADD COLUMN owner_seat_id uuid REFERENCES panky_seats(seat_id);
ALTER TABLE panky_resources ADD COLUMN code text;
CREATE UNIQUE INDEX panky_one_active_resource_code
  ON panky_resources(code) WHERE status='active' AND code IS NOT NULL;
CREATE TABLE panky_resource_share_requests (
  request_id uuid PRIMARY KEY,
  resource_id uuid NOT NULL REFERENCES panky_resources(resource_id),
  source_department_id uuid NOT NULL REFERENCES panky_departments(department_id),
  target_department_id uuid NOT NULL REFERENCES panky_departments(department_id),
  requested_by_actor_id text NOT NULL REFERENCES panky_actors(actor_id),
  requested_via_seat_id uuid NOT NULL REFERENCES panky_seats(seat_id),
  decided_by_actor_id text REFERENCES panky_actors(actor_id),
  decided_via_seat_id uuid REFERENCES panky_seats(seat_id),
  status text NOT NULL CHECK (status IN ('pending','approved','rejected','cancelled')),
  reason text NOT NULL,
  requested_at timestamptz NOT NULL,
  decided_at timestamptz,
  activated_release_id uuid REFERENCES panky_policy_releases(release_id),
  CHECK (source_department_id <> target_department_id),
  CHECK ((decided_by_actor_id IS NULL) = (decided_at IS NULL))
);
CREATE UNIQUE INDEX panky_one_pending_resource_share
  ON panky_resource_share_requests(resource_id,target_department_id)
  WHERE status IN ('pending','approved') AND activated_release_id IS NULL;
CREATE TABLE panky_founder_transfers (
  transfer_id uuid PRIMARY KEY,
  from_actor_id text NOT NULL REFERENCES panky_actors(actor_id),
  to_actor_id text NOT NULL REFERENCES panky_actors(actor_id),
  status text NOT NULL CHECK (status IN ('pending','accepted','rejected','expired')),
  requested_at timestamptz NOT NULL,
  expires_at timestamptz NOT NULL,
  decided_at timestamptz,
  activated_release_id uuid REFERENCES panky_policy_releases(release_id),
  CHECK (from_actor_id <> to_actor_id),
  CHECK (expires_at > requested_at)
);
CREATE UNIQUE INDEX panky_one_pending_founder_transfer
  ON panky_founder_transfers((true))
  WHERE status IN ('pending','accepted') AND activated_release_id IS NULL;
CREATE TABLE panky_super_admin_changes (
  change_id uuid PRIMARY KEY,
  target_actor_id text NOT NULL REFERENCES panky_actors(actor_id),
  requested_by_actor_id text NOT NULL REFERENCES panky_actors(actor_id),
  action text NOT NULL CHECK (action IN ('promote','revoke')),
  reason text NOT NULL,
  requested_at timestamptz NOT NULL DEFAULT now(),
  activated_release_id uuid REFERENCES panky_policy_releases(release_id)
);
CREATE UNIQUE INDEX panky_one_open_super_admin_change
  ON panky_super_admin_changes(target_actor_id,action)
  WHERE activated_release_id IS NULL;
CREATE TABLE panky_resource_grant_requests (
  request_id uuid PRIMARY KEY,
  resource_id uuid NOT NULL REFERENCES panky_resources(resource_id),
  recipient_seat_id uuid NOT NULL REFERENCES panky_seats(seat_id),
  operation text NOT NULL,
  action text NOT NULL DEFAULT 'allow' CHECK (action IN ('allow','revoke')),
  requested_by_actor_id text NOT NULL REFERENCES panky_actors(actor_id),
  requested_via_seat_id uuid NOT NULL REFERENCES panky_seats(seat_id),
  reason text NOT NULL,
  requested_at timestamptz NOT NULL DEFAULT now(),
  activated_release_id uuid REFERENCES panky_policy_releases(release_id)
);
CREATE UNIQUE INDEX panky_one_open_resource_grant_request
  ON panky_resource_grant_requests(resource_id,recipient_seat_id,operation)
  WHERE activated_release_id IS NULL;
COMMIT;
