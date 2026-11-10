# Panky 2.0 policy projection

This package projects the approved 19-table model to the existing policy service.
The live `/v1` routes and their database remain available during the bridge.
The `/v2` routes use a **separate PostgreSQL database** selected only by
`PANKY_POLICY_V2_DSN`. Never run `schema.sql` against the v1 database.

## Activation order

1. Create a dedicated PostgreSQL database and least-privilege role on the
   existing IONOS host. Back up and confirm the existing v1 policy database.
2. Apply `schema.sql` only to the new database. Set `PANKY_POLICY_V2_DSN` in
   the root-owned service environment and deploy the code with the existing
   `/v1` routes unchanged. `/v2/health` is expected to return 503 until a
   reviewed Core release is active.
   When applying `migrations/0005_seat_authority_resource_owner.sql` to an
   existing v2 database, grant the runtime database role `SELECT, INSERT,
   UPDATE, DELETE` on its four new request tables before restarting the
   service. Verify the role can read `panky_resource_share_requests`; a
   successful migration as database owner does not grant that access.
3. Publish `policy/brunova-gateway-policy-v2.json` from the exact protected
   Core `main` commit, with its actor, Seat and topology registries. The
   backend rejects mismatched actors, parents, Seat bindings and occupants.
4. Confirm `/health` and `/v2/health`, then run positive and negative OIDC
   synthetic decisions for Jorge, Nat and Dev 01. Verify no live provider
   operation occurred during these previews.
5. Enable `PANKY_POLICY_V2_MODE=shadow` on the Gateway. It logs differences
   but returns the current v1 decision; existing tokens and POST effects are
   unchanged. Collect comparisons, including denial, revocation and an
   interrupted or unavailable v2 service.
6. Only after the live comparisons agree, set `PANKY_POLICY_V2_MODE=enforce`
   for Panky OIDC requests. That mode uses `/v2/decide`, the v2 catalog preview
   and v2 grant completion. Existing manual Gateway tokens keep their current
   route throughout the bridge. An unavailable v2 decision fails closed.

Panky 2.2 uses Google OIDC only. The old static Management token remains
available to unrelated Gateway clients, but cannot authenticate to the
Panky context and governance routes. Earlier Panky installations keep their
governed recovery path until they reach READY on 2.2.

## Security boundaries

- Only the protected Core workflow may publish a complete release. The
  Gateway's verified service identity may query decisions; it cannot publish.
- A desktop access token proves a Google subject and audience. It contains no
  privileges. The backend binds the subject to the Core Actor's verified email
  on first real use and fails closed on a later mismatch.
- Each external action is evaluated against one complete active Seat path:
  reachable department resource and role operation for the same Seat. Direct
  Actor denials apply across all Seats. Limited-use grants reserve atomically.
- Operational rows are a projection. Natural-language role descriptions and
  Knowledge files do not grant permissions.
