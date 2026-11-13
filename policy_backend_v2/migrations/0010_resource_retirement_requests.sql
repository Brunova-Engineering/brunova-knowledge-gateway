BEGIN;
CREATE TABLE IF NOT EXISTS panky_resource_retirement_requests (
  request_id uuid PRIMARY KEY,
  resource_id uuid NOT NULL REFERENCES panky_resources(resource_id),
  requested_by_actor_id text NOT NULL REFERENCES panky_actors(actor_id),
  requested_via_seat_id uuid NOT NULL REFERENCES panky_seats(seat_id),
  reason text NOT NULL,
  requested_at timestamptz NOT NULL DEFAULT now(),
  activated_release_id uuid REFERENCES panky_policy_releases(release_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS panky_one_open_resource_retirement
  ON panky_resource_retirement_requests(resource_id) WHERE activated_release_id IS NULL;
COMMIT;
