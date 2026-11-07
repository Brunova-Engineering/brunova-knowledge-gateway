# OIDC source catalog regression — 2026-11-07

The installed Panky 2.0.14 Windows runtime verified Google Actor `act_dev_001`, retained `gateway-auth-mode=oidc`, and had no `.panky/credentials/gateway-token`. No credential contents were printed or restored.

The original reported new-session attempt selected the separate global `brunova_knowledge_gateway` MCP registration. That result does not prove the installed runtime's OIDC path. A fresh ephemeral Codex session was therefore directed to the generated `brunovaKnowledgeGateway` registration with `approval_policy=never`, an explicit startup wait and a 60-second startup timeout. The real `list_sources` call reached that server and returned `policy_denied: The requested operation is not authorized.` The original live integration test remains failed until a deployed correction passes the same read in a fresh session.

## Root cause

`_panky_catalog` advertises workspace tools using authorization previews against each active source. `list_sources` has no source argument, so `call_tool` previously evaluated the actual request against `resource_kind=tool`, `resource_ref=list_sources`. An Actor with an authorized source could discover the tool but could not execute it through that unrelated global target.

Allowing that global target alone would also be insufficient: an OIDC principal starts with a broad discovery envelope, and the existing listing must not return sources outside the current policy decisions.

## Correction

For the OIDC `list_sources` aggregate, evaluate actual read decisions against active readable sources. Run the normal listing and audit wrapper with the current request's principal narrowed to the allowed source IDs, then restore the original principal. Denied and disabled sources are not exposed. Limited-use reservations retain their normal completion semantics; unavailable policy fails closed. No grant, Actor role, source registry, legacy credential or host approval policy is changed.

## Local verification

- Before the correction, the new regression module reproduced three failures and one passing denial check.
- After the correction: 85 focused policy, principal, MCP and authentication tests passed.
- Complete Gateway suite: **382 passed, 14 skipped**, exit 0, in 9.65 seconds.
- Coverage includes advertised/executable source agreement, no disclosure of denied sources, disabled/unreadable filtering, denied execution, policy unavailability, reserved-use completion and concurrent OIDC scope isolation.
- `git diff --check` passed.

Production is deliberately not represented as fixed by these local tests. This repository deploys after a push to `main`; a prepared PR can be reviewed before a merge changes production. Repeat the installed runtime's real OIDC MCP read in a fresh session after deployment, with the legacy runtime token still absent.
