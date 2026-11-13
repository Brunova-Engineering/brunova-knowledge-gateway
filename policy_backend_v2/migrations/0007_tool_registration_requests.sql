BEGIN;
CREATE TABLE IF NOT EXISTS panky_tool_registration_requests (
  request_id uuid PRIMARY KEY,
  provider text NOT NULL,
  operations_json jsonb NOT NULL CHECK (jsonb_typeof(operations_json)='array'),
  requested_by_actor_id text NOT NULL REFERENCES panky_actors(actor_id),
  requested_via_seat_id uuid NOT NULL REFERENCES panky_seats(seat_id),
  reason text NOT NULL,
  requested_at timestamptz NOT NULL DEFAULT now(),
  activated_release_id uuid REFERENCES panky_policy_releases(release_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS panky_one_open_tool_registration
  ON panky_tool_registration_requests(provider) WHERE activated_release_id IS NULL;
COMMIT;
