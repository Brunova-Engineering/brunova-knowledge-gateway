BEGIN;
CREATE TABLE IF NOT EXISTS panky_entity_retirement_requests (
  request_id uuid PRIMARY KEY,
  entity_kind text NOT NULL CHECK (entity_kind IN ('actor','seat','department','role','subcontext')),
  entity_code text NOT NULL,
  requested_by_actor_id text NOT NULL REFERENCES panky_actors(actor_id),
  requested_via_seat_id uuid NOT NULL REFERENCES panky_seats(seat_id),
  reason text NOT NULL,
  requested_at timestamptz NOT NULL DEFAULT now(),
  activated_release_id uuid REFERENCES panky_policy_releases(release_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS panky_one_open_entity_retirement
  ON panky_entity_retirement_requests(entity_kind,entity_code)
  WHERE activated_release_id IS NULL;
COMMIT;
