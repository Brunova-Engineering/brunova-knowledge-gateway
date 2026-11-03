-- One database per organization. Core is the reviewed source; these tables
-- hold only the currently activated policy and its operational audit trail.
-- Run in a transaction in the dedicated policy database.

CREATE TABLE panky_actors (
    actor_id text PRIMARY KEY,
    oidc_issuer text NOT NULL,
    oidc_subject text,
    email text NOT NULL UNIQUE,
    status text NOT NULL CHECK (status IN ('active', 'disabled')),
    super_admin boolean NOT NULL DEFAULT false,
    UNIQUE (oidc_issuer, oidc_subject)
);

CREATE TABLE panky_departments (
    department_id uuid PRIMARY KEY,
    code text NOT NULL UNIQUE,
    name text NOT NULL,
    parent_department_id uuid REFERENCES panky_departments(department_id)
);

CREATE TABLE panky_actor_departments (
    assignment_id uuid PRIMARY KEY,
    actor_id text NOT NULL REFERENCES panky_actors(actor_id),
    department_id uuid NOT NULL REFERENCES panky_departments(department_id),
    UNIQUE (actor_id, department_id)
);

CREATE TABLE panky_roles (
    role_id uuid PRIMARY KEY,
    code text NOT NULL UNIQUE,
    name text NOT NULL
);

CREATE TABLE panky_actor_roles (
    assignment_id uuid PRIMARY KEY,
    actor_id text NOT NULL REFERENCES panky_actors(actor_id),
    role_id uuid NOT NULL REFERENCES panky_roles(role_id),
    UNIQUE (actor_id, role_id)
);

CREATE TABLE panky_tools (
    tool_id uuid PRIMARY KEY,
    provider text NOT NULL,
    tool_name text NOT NULL,
    UNIQUE (provider, tool_name)
);

CREATE TABLE panky_resources (
    resource_id uuid PRIMARY KEY,
    provider text NOT NULL,
    resource_kind text NOT NULL,
    external_ref text NOT NULL,
    UNIQUE (provider, resource_kind, external_ref)
);

CREATE TABLE panky_policy_releases (
    release_id uuid PRIMARY KEY,
    core_commit char(40) NOT NULL UNIQUE,
    status text NOT NULL CHECK (status IN ('active', 'superseded')),
    activated_at timestamptz NOT NULL DEFAULT now(),
    published_by text NOT NULL REFERENCES panky_actors(actor_id)
);
CREATE UNIQUE INDEX panky_one_active_policy_idx ON panky_policy_releases(status)
    WHERE status = 'active';

CREATE TABLE panky_grants (
    grant_id uuid PRIMARY KEY,
    release_id uuid NOT NULL REFERENCES panky_policy_releases(release_id),
    actor_id text REFERENCES panky_actors(actor_id),
    department_id uuid REFERENCES panky_departments(department_id),
    role_id uuid REFERENCES panky_roles(role_id),
    tool_id uuid NOT NULL REFERENCES panky_tools(tool_id),
    resource_id uuid NOT NULL REFERENCES panky_resources(resource_id),
    operation text NOT NULL,
    effect text NOT NULL CHECK (effect IN ('allow', 'deny')),
    inherit_to_children boolean NOT NULL DEFAULT false,
    valid_from timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz,
    max_uses integer CHECK (max_uses > 0),
    reason text,
    approved_by text REFERENCES panky_actors(actor_id),
    CHECK (num_nonnulls(actor_id, department_id, role_id) = 1),
    CHECK (expires_at IS NULL OR expires_at > valid_from),
    CHECK (max_uses IS NULL OR (actor_id IS NOT NULL AND effect = 'allow'))
);

CREATE INDEX panky_grants_actor_idx ON panky_grants(actor_id)
    WHERE actor_id IS NOT NULL;
CREATE INDEX panky_grants_department_idx ON panky_grants(department_id)
    WHERE department_id IS NOT NULL;
CREATE INDEX panky_grants_role_idx ON panky_grants(role_id)
    WHERE role_id IS NOT NULL;
CREATE INDEX panky_grants_target_idx ON panky_grants(tool_id, resource_id, operation);
CREATE INDEX panky_grants_release_idx ON panky_grants(release_id);

CREATE TABLE panky_grant_uses (
    use_id uuid PRIMARY KEY,
    grant_id uuid NOT NULL REFERENCES panky_grants(grant_id),
    request_id text NOT NULL,
    status text NOT NULL CHECK (status IN ('reserved', 'committed', 'released', 'uncertain')),
    reserved_at timestamptz NOT NULL DEFAULT now(),
    finalized_at timestamptz,
    UNIQUE (grant_id, request_id)
);
CREATE INDEX panky_grant_uses_active_idx ON panky_grant_uses(grant_id)
    WHERE status IN ('reserved', 'committed', 'uncertain');

CREATE TABLE panky_audit_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    release_id uuid REFERENCES panky_policy_releases(release_id),
    actor_id text REFERENCES panky_actors(actor_id),
    event_type text NOT NULL,
    decision text,
    request_id text,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX panky_audit_occurred_idx ON panky_audit_events(occurred_at DESC);
