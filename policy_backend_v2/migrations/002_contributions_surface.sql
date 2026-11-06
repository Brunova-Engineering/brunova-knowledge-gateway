-- Panky 2.0: contribution proposals are an explicit organizational surface.
-- Apply only to the dedicated v2 policy database before publishing Core rules.
ALTER TABLE panky_context_rules
  DROP CONSTRAINT panky_context_rules_surface_check,
  ADD CONSTRAINT panky_context_rules_surface_check
    CHECK (surface IN ('shared','knowledge','state','work','contributions'));

ALTER TABLE panky_grants
  DROP CONSTRAINT panky_grants_surface_check,
  ADD CONSTRAINT panky_grants_surface_check
    CHECK (surface IN ('shared','knowledge','state','work','contributions'));
