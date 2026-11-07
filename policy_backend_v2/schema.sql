-- Panky 2.0 policy database. Apply only to a NEW dedicated database.
-- One database belongs to exactly one organization. Core owns policy;
-- PostgreSQL stores the validated active projection and audit evidence.
BEGIN;

CREATE TABLE panky_actors (
  actor_id text PRIMARY KEY,
  email text NOT NULL UNIQUE,
  oidc_issuer text NOT NULL DEFAULT 'https://accounts.google.com',
  oidc_subject text,
  status text NOT NULL CHECK (status IN ('active','disabled')),
  super_admin boolean NOT NULL DEFAULT false,
  UNIQUE (oidc_issuer, oidc_subject)
);

CREATE TABLE panky_context_nodes (
  node_id text PRIMARY KEY,
  parent_node_id text REFERENCES panky_context_nodes(node_id),
  kind text NOT NULL CHECK (kind IN ('department','seat','subcontext')),
  visibility text NOT NULL CHECK (visibility IN ('inherit','restricted','owner')),
  lifecycle text NOT NULL CHECK (lifecycle IN ('active','archived')),
  repository text NOT NULL UNIQUE,
  store_id text NOT NULL UNIQUE,
  generation integer NOT NULL CHECK (generation > 0),
  local_path text NOT NULL UNIQUE,
  binding_mode text NOT NULL DEFAULT 'authoritative' CHECK (binding_mode IN ('authoritative','generated')),
  delivery_repository text UNIQUE,
  delivery_store_id text UNIQUE,
  delivery_generation integer CHECK (delivery_generation > 0),
  delivery_local_path text UNIQUE,
  CHECK ((delivery_repository IS NULL AND delivery_store_id IS NULL
          AND delivery_generation IS NULL AND delivery_local_path IS NULL)
      OR (delivery_repository IS NOT NULL AND delivery_store_id IS NOT NULL
          AND delivery_generation IS NOT NULL AND delivery_local_path IS NOT NULL)),
  CHECK (node_id <> parent_node_id)
);

CREATE TABLE panky_departments (
  department_id uuid PRIMARY KEY,
  code text NOT NULL UNIQUE,
  name text NOT NULL,
  parent_department_id uuid REFERENCES panky_departments(department_id),
  context_node_id text NOT NULL UNIQUE REFERENCES panky_context_nodes(node_id),
  CHECK (department_id <> parent_department_id)
);

CREATE TABLE panky_roles (
  role_id uuid PRIMARY KEY,
  code text NOT NULL UNIQUE,
  name text NOT NULL,
  status text NOT NULL CHECK (status IN ('active','disabled'))
);

CREATE TABLE panky_seats (
  seat_id uuid PRIMARY KEY,
  code text NOT NULL UNIQUE,
  department_id uuid NOT NULL REFERENCES panky_departments(department_id),
  role_id uuid NOT NULL REFERENCES panky_roles(role_id),
  context_node_id text NOT NULL UNIQUE REFERENCES panky_context_nodes(node_id),
  reports_to_seat_id uuid REFERENCES panky_seats(seat_id),
  occupancy text NOT NULL CHECK (occupancy IN ('single','multiple')),
  status text NOT NULL CHECK (status IN ('active','disabled')),
  CHECK (seat_id <> reports_to_seat_id)
);

CREATE TABLE panky_actor_seats (
  assignment_id uuid PRIMARY KEY,
  actor_id text NOT NULL REFERENCES panky_actors(actor_id),
  seat_id uuid NOT NULL REFERENCES panky_seats(seat_id),
  active_from timestamptz NOT NULL,
  active_until timestamptz,
  CHECK (active_until IS NULL OR active_until > active_from)
);
CREATE UNIQUE INDEX panky_one_open_actor_seat ON panky_actor_seats(actor_id, seat_id)
  WHERE active_until IS NULL;

CREATE TABLE panky_tools (
  tool_id uuid PRIMARY KEY,
  code text NOT NULL UNIQUE,
  provider text NOT NULL,
  operations_json jsonb NOT NULL CHECK (jsonb_typeof(operations_json) = 'array'),
  status text NOT NULL CHECK (status IN ('active','disabled'))
);

CREATE TABLE panky_resources (
  resource_id uuid PRIMARY KEY,
  tool_id uuid NOT NULL REFERENCES panky_tools(tool_id),
  resource_kind text NOT NULL,
  external_ref text NOT NULL,
  status text NOT NULL CHECK (status IN ('active','disabled'))
);
CREATE UNIQUE INDEX panky_one_active_resource_ref
  ON panky_resources(tool_id, resource_kind, external_ref)
  WHERE status = 'active';

CREATE TABLE panky_department_resources (
  department_id uuid NOT NULL REFERENCES panky_departments(department_id),
  resource_id uuid NOT NULL REFERENCES panky_resources(resource_id),
  relation text NOT NULL CHECK (relation IN ('owned','shared')),
  PRIMARY KEY (department_id, resource_id)
);
CREATE UNIQUE INDEX panky_one_resource_owner ON panky_department_resources(resource_id)
  WHERE relation = 'owned';

CREATE TABLE panky_role_operations (
  role_id uuid NOT NULL REFERENCES panky_roles(role_id),
  tool_id uuid NOT NULL REFERENCES panky_tools(tool_id),
  operation text NOT NULL,
  PRIMARY KEY (role_id, tool_id, operation)
);

CREATE TABLE panky_context_rules (
  rule_id uuid PRIMARY KEY,
  subject_kind text NOT NULL CHECK (subject_kind IN
    ('any_active_seat','role_in_seat','seat_occupant')),
  role_id uuid REFERENCES panky_roles(role_id),
  target_class text NOT NULL CHECK (target_class IN
    ('department_context','seat_context')),
  scope text NOT NULL CHECK (scope IN ('department_subtree','own_seat','reporting_subtree')),
  surface text NOT NULL CHECK (surface IN ('shared','knowledge','state','work','contributions')),
  operation text NOT NULL,
  status text NOT NULL CHECK (status IN ('active','disabled')),
  CHECK ((subject_kind = 'role_in_seat') = (role_id IS NOT NULL))
);

CREATE TABLE panky_policy_releases (
  release_id uuid PRIMARY KEY,
  core_commit char(40) NOT NULL UNIQUE,
  status text NOT NULL CHECK (status IN ('active','superseded')),
  activated_at timestamptz NOT NULL,
  published_by text NOT NULL
);
CREATE UNIQUE INDEX panky_one_active_v2_release ON panky_policy_releases(status)
  WHERE status = 'active';

CREATE TABLE panky_grants (
  grant_id uuid PRIMARY KEY,
  release_id uuid NOT NULL REFERENCES panky_policy_releases(release_id),
  subject_kind text NOT NULL CHECK (subject_kind IN ('actor','seat','role','department')),
  actor_id text REFERENCES panky_actors(actor_id),
  seat_id uuid REFERENCES panky_seats(seat_id),
  role_id uuid REFERENCES panky_roles(role_id),
  department_id uuid REFERENCES panky_departments(department_id),
  target_kind text NOT NULL CHECK (target_kind IN ('external_resource','context_node')),
  resource_id uuid REFERENCES panky_resources(resource_id),
  context_node_id text REFERENCES panky_context_nodes(node_id),
  surface text CHECK (surface IN ('shared','knowledge','state','work','contributions')),
  operation text NOT NULL,
  effect text NOT NULL CHECK (effect IN ('allow','deny')),
  include_descendants boolean NOT NULL DEFAULT false,
  valid_from timestamptz NOT NULL,
  expires_at timestamptz,
  max_uses integer CHECK (max_uses > 0),
  reason text NOT NULL,
  approved_by text NOT NULL REFERENCES panky_actors(actor_id),
  CHECK (num_nonnulls(actor_id, seat_id, role_id, department_id) = 1),
  CHECK ((subject_kind = 'actor' AND actor_id IS NOT NULL) OR
         (subject_kind = 'seat' AND seat_id IS NOT NULL) OR
         (subject_kind = 'role' AND role_id IS NOT NULL) OR
         (subject_kind = 'department' AND department_id IS NOT NULL)),
  CHECK (num_nonnulls(resource_id, context_node_id) = 1),
  CHECK ((target_kind = 'external_resource' AND resource_id IS NOT NULL AND surface IS NULL) OR
         (target_kind = 'context_node' AND context_node_id IS NOT NULL AND surface IS NOT NULL)),
  CHECK (expires_at IS NULL OR expires_at > valid_from),
  CHECK (max_uses IS NULL OR effect = 'allow')
);
CREATE INDEX panky_active_grants ON panky_grants(release_id, subject_kind, effect);
CREATE INDEX panky_grants_resource ON panky_grants(resource_id) WHERE resource_id IS NOT NULL;
CREATE INDEX panky_grants_context ON panky_grants(context_node_id) WHERE context_node_id IS NOT NULL;

CREATE TABLE panky_grant_uses (
  use_id uuid PRIMARY KEY,
  grant_id uuid NOT NULL REFERENCES panky_grants(grant_id),
  request_id text NOT NULL,
  status text NOT NULL CHECK (status IN ('reserved','committed','released','uncertain')),
  reserved_at timestamptz NOT NULL,
  finalized_at timestamptz,
  UNIQUE (grant_id, request_id)
);

CREATE TABLE panky_audit_events (
  event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  release_id uuid REFERENCES panky_policy_releases(release_id),
  actor_id text REFERENCES panky_actors(actor_id),
  seat_id uuid REFERENCES panky_seats(seat_id),
  target_kind text,
  target_ref text,
  surface text,
  operation text,
  decision text NOT NULL,
  occurred_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX panky_audit_recent ON panky_audit_events(occurred_at DESC);

COMMIT;
