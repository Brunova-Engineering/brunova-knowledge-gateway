-- Expand the deployed Panky 2.0 database before publishing a release with
-- verified Core repository bindings. Existing permissions remain active.
BEGIN;
ALTER TABLE panky_context_nodes ADD COLUMN repository text;
ALTER TABLE panky_context_nodes ADD COLUMN store_id text;
ALTER TABLE panky_context_nodes ADD COLUMN generation integer;
ALTER TABLE panky_context_nodes ADD COLUMN local_path text;
ALTER TABLE panky_context_nodes ADD COLUMN delivery_repository text;
ALTER TABLE panky_context_nodes ADD COLUMN delivery_store_id text;
ALTER TABLE panky_context_nodes ADD COLUMN delivery_generation integer;
ALTER TABLE panky_context_nodes ADD COLUMN delivery_local_path text;
ALTER TABLE panky_context_nodes ADD CONSTRAINT panky_context_generation_positive
  CHECK (generation IS NULL OR generation > 0);
ALTER TABLE panky_context_nodes ADD CONSTRAINT panky_delivery_generation_positive
  CHECK (delivery_generation IS NULL OR delivery_generation > 0);
ALTER TABLE panky_context_nodes ADD CONSTRAINT panky_delivery_binding_complete
  CHECK ((delivery_repository IS NULL AND delivery_store_id IS NULL
          AND delivery_generation IS NULL AND delivery_local_path IS NULL)
      OR (delivery_repository IS NOT NULL AND delivery_store_id IS NOT NULL
          AND delivery_generation IS NOT NULL AND delivery_local_path IS NOT NULL));
CREATE UNIQUE INDEX panky_context_repository_unique ON panky_context_nodes(repository);
CREATE UNIQUE INDEX panky_context_store_unique ON panky_context_nodes(store_id);
CREATE UNIQUE INDEX panky_context_path_unique ON panky_context_nodes(local_path);
CREATE UNIQUE INDEX panky_context_delivery_repository_unique ON panky_context_nodes(delivery_repository);
CREATE UNIQUE INDEX panky_context_delivery_store_unique ON panky_context_nodes(delivery_store_id);
CREATE UNIQUE INDEX panky_context_delivery_path_unique ON panky_context_nodes(delivery_local_path);
COMMIT;
