-- Add a closed organizational-action target to existing Panky policy databases.
BEGIN;
CREATE TABLE IF NOT EXISTS panky_org_actions (
  action_id uuid PRIMARY KEY,
  code text NOT NULL UNIQUE,
  default_authority text NOT NULL CHECK (default_authority IN ('admin','super_admin','founder')),
  scope_kind text NOT NULL CHECK (scope_kind IN ('department_subtree','receiving_branch','organization','provider')),
  delegable boolean NOT NULL,
  governed_only boolean NOT NULL
);
INSERT INTO panky_org_actions(action_id,code,default_authority,scope_kind,delegable,governed_only) VALUES
  ('309711cc-0c68-5548-b610-f96e2588b2d1','department.manage','admin','department_subtree',false,false),
  ('32bef749-b520-5a43-afb9-0b17d975ec87','actor_seat.manage','admin','department_subtree',false,false),
  ('89fc11d8-64f5-59af-aadd-f76d47a35252','resource_access.manage','admin','department_subtree',false,false),
  ('4036ffde-3d57-5f06-9a34-e3e1403f8d27','share_cross_department.decide','admin','receiving_branch',false,true),
  ('6f049322-44c6-5079-83be-cc159316be55','role.manage','super_admin','organization',false,true),
  ('d3bbf4b6-000d-5483-8b4c-13dab1c33030','tool.register','super_admin','organization',true,false),
  ('07dd3dd0-1409-59fd-9c99-6e58dfeb90f3','tool.retire','super_admin','organization',true,false),
  ('9f9e0bd2-3510-59a4-b186-ff8634b8e083','tool.connector_configure','super_admin','provider',false,true),
  ('01a93ae0-e29f-5167-ac9a-96c0f0b318ce','super_admin.appoint','super_admin','organization',false,true),
  ('15b25657-b56b-5c6a-ba1f-74ab94cdf474','super_admin.revoke','founder','organization',false,true),
  ('2a2954fc-07b0-539e-8a5a-639a768ffec9','founder.transfer','founder','organization',false,true)
ON CONFLICT (action_id) DO UPDATE SET code=EXCLUDED.code,
  default_authority=EXCLUDED.default_authority,scope_kind=EXCLUDED.scope_kind,
  delegable=EXCLUDED.delegable,governed_only=EXCLUDED.governed_only;
ALTER TABLE panky_grants ADD COLUMN IF NOT EXISTS org_action_id uuid REFERENCES panky_org_actions(action_id);
DO $$
DECLARE c record;
BEGIN
  FOR c IN SELECT conname, pg_get_constraintdef(oid) AS definition
           FROM pg_constraint WHERE conrelid='panky_grants'::regclass AND contype='c'
  LOOP
    IF c.definition LIKE '%target_kind%' OR c.definition LIKE '%num_nonnulls(resource_id%'
    THEN EXECUTE format('ALTER TABLE panky_grants DROP CONSTRAINT %I', c.conname);
    END IF;
  END LOOP;
END $$;
ALTER TABLE panky_grants ADD CONSTRAINT panky_grants_org_target_kind_check
  CHECK (target_kind IN ('external_resource','context_node','org_action'));
ALTER TABLE panky_grants ADD CONSTRAINT panky_grants_org_target_count_check
  CHECK (num_nonnulls(resource_id,context_node_id,org_action_id)=1);
ALTER TABLE panky_grants ADD CONSTRAINT panky_grants_org_target_shape_check
  CHECK ((target_kind='external_resource' AND resource_id IS NOT NULL AND surface IS NULL)
      OR (target_kind='context_node' AND context_node_id IS NOT NULL AND surface IS NOT NULL)
      OR (target_kind='org_action' AND org_action_id IS NOT NULL AND surface IS NULL
          AND operation='invoke' AND subject_kind='seat' AND effect='allow'
          AND include_descendants=false));
CREATE INDEX IF NOT EXISTS panky_grants_org_action ON panky_grants(org_action_id) WHERE org_action_id IS NOT NULL;
COMMIT;
