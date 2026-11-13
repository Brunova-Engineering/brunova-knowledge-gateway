BEGIN;
ALTER TABLE panky_departments ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'active';
ALTER TABLE panky_departments DROP CONSTRAINT IF EXISTS panky_departments_status_check;
ALTER TABLE panky_departments ADD CONSTRAINT panky_departments_status_check
  CHECK (status IN ('active','disabled'));
COMMIT;
