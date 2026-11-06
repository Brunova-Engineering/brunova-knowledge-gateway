BEGIN;
ALTER TABLE panky_context_nodes
  ADD COLUMN IF NOT EXISTS binding_mode text NOT NULL DEFAULT 'authoritative'
  CHECK (binding_mode IN ('authoritative','generated'));
COMMIT;
