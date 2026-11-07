BEGIN;
ALTER TABLE panky_context_rules
  DROP CONSTRAINT IF EXISTS panky_context_rules_scope_check;
ALTER TABLE panky_context_rules
  ADD CONSTRAINT panky_context_rules_scope_check
  CHECK (scope IN ('department_subtree','own_seat','reporting_subtree'));
COMMIT;
