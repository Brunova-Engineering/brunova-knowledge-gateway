BEGIN;
INSERT INTO panky_org_actions(action_id,code,default_authority,scope_kind,delegable,governed_only)
VALUES ('07dd3dd0-1409-59fd-9c99-6e58dfeb90f3','tool.retire','super_admin','organization',true,false)
ON CONFLICT (action_id) DO UPDATE SET code=EXCLUDED.code,
  default_authority=EXCLUDED.default_authority,scope_kind=EXCLUDED.scope_kind,
  delegable=EXCLUDED.delegable,governed_only=EXCLUDED.governed_only;
CREATE TABLE IF NOT EXISTS panky_tool_retirement_requests (
  request_id uuid PRIMARY KEY,
  provider text NOT NULL,
  requested_by_actor_id text NOT NULL REFERENCES panky_actors(actor_id),
  requested_via_seat_id uuid NOT NULL REFERENCES panky_seats(seat_id),
  reason text NOT NULL,
  requested_at timestamptz NOT NULL DEFAULT now(),
  activated_release_id uuid REFERENCES panky_policy_releases(release_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS panky_one_open_tool_retirement
  ON panky_tool_retirement_requests(provider) WHERE activated_release_id IS NULL;
COMMIT;
