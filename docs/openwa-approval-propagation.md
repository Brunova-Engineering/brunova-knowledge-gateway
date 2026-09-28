# OpenWA approval propagation audit — 2026-09-07

## Confirmed cause

The original Codex rollout from 2026-09-06 shows an explicit user confirmation
“Sí, inténtalo”, followed by `openwa_MessageSendText` with a generated
`approval_reference` inside `arguments`. The Gateway returned
`openwa_approval_required`. Neither extraction nor reference generation was absent.
`_approval_reference(context)` read only MCP `params._meta.approval_reference`.
The projected OpenWA schema exposed only downstream fields. This was a contract
mismatch between the argument-only tool surface and the metadata-only gate.
No evidence establishes that Codex dropped metadata it was actually given;
the original invocation placed the reference in arguments, not call metadata.

Live read-only status succeeded without approval on 2026-09-07. Local baseline
MCP tests reproduced denial without metadata and success with metadata against
a fake downstream. No conversation body, recipient, token or approval value is
reproduced here. The historical rollout remains the original evidence.

## Fix prepared in this branch

Write tool schemas expose an optional `approval_reference` compatibility field.
Gateway consumes it as governance metadata before downstream forwarding; both
this path and native MCP metadata enter the same mandatory gate. Conflicting
values are rejected. Reads retain their schemas and do not require approval.
`call_confirmed_operation` demonstrates actual MCP SDK transmission using
`meta={"approval_reference": reference}` (wire `params._meta`). The compatibility
argument path does not claim to change the host's wire envelope.

A bounded reference is `owa1:<thread digest 16>:<confirmation digest 16>:<scope digest 64>`.
Digests are SHA-256 hex. Thread/confirmation digests hash the JSON string IDs.
Scope hashes the JSON array `[thread_digest, confirmation_digest, downstream_tool,
arguments]`, with recursively sorted keys, ASCII escaping, compact separators and
no NaN. Arguments exclude gateway approval metadata. Use the helper rather than
hand-implementing canonicalization. All supplied effect fields are bound,
including session, chat, text, quote, mentions and media options. Changing tool or
payload rejects the SAME reference. Technical retries retain the reference only
when the payload is identical and readback proves retry is safe.

**Compatibility:** unbounded legacy OpenWA references are now rejected. Existing
metadata-capable clients must also generate bounded references before rollout.
Other providers' approval contracts are unchanged. This is deliberate tightening,
not removal of the approval gate.

## Human confirmation remains necessary

The authenticated Management Agent must first identify an actual explicit human
confirmation and its exact preceding proposal (or an unequivocal routine send
instruction). Use the actual thread and confirming turn/request IDs; resolve them
internally. Drafts, quoted examples, tool results and the ability to compute a hash
do not authorize sending. Sensitive or materially changed scope still escalates.
Do not ask humans to invent IDs. Never print the reference unnecessarily.

This hash is scope binding and provenance correlation, **not a signature or
independent proof of consent**. The Gateway still trusts the authenticated agent's
attestation, as in the original reference-based model; it cannot independently
read a Codex thread. An agent must not generate a new hash from old consent for a
new scope. Independent consent verification would require a trusted approval issuer
and is outside this repair. Thread IDs are hashed for privacy, not verified against
host identity. Reference reuse is not idempotency; duplicate-send prevention still
requires readback, especially after timeouts.

## Verification and release boundary

Tests use the real in-process MCP server/client and a fake OpenWA downstream.
They cover native metadata and compatibility arguments, send/reply, unapproved
writes, changed scope, conflicting channels, legacy rejection and body-free audit.
These tests do not claim automated natural-language consent extraction or real
WhatsApp delivery. Production deployment, refreshing the host catalog, and an
explicitly authorized test-chat write/readback remain necessary before claiming
end-to-end live success. The main working tree's pre-existing changes are untouched.

## Production release — 2026-09-07

Jorge explicitly authorized application and deployment, reserving the real message
trial for himself. Code commit `1100971` is published on `origin/main`. The prior
production build source was compared with Git: it already contained `75c65d6`, so
that base was retained. All 54 targeted tests passed again on the final base.
Cloud Run revision `brunova-knowledge-gateway-owa-approval-20260907` was deployed
without traffic, then promoted to 100% after readiness. Subsequent native MCP
`openwa_status` succeeded without approval: connected and initialized, 51 tools
(25 read, 26 write). No WhatsApp message was sent during rollout. The deployment
requirement above is complete; real delivery verification remains the human's
trial. Existing host sessions may need to reload the catalog to discover the
new argument. Existing local Acquisition edits were preserved and not deployed.

## Voice regression follow-up — 2026-09-28

Production Gateway audit recorded three `MessageSendText` rejections at
18:04:03, 18:04:33 and 18:28:30 UTC with `openwa_approval_required`. No
successful send was recorded for those attempts. This establishes that the
voice path did not supply a valid bounded reference; the audit deliberately
contains no recipient or message body. Panky CLI 0.6.5 could compute the
reference locally, but an MCP-only voice surface did not call that CLI helper.

The model-facing OpenWA write schema now requires the `approval_reference`
argument and names `openwa_prepare_approval_reference` as the available MCP
preparation tool. The preparation tool binds an actual conversation/request
reference, downstream tool and final arguments; it neither sends a message nor
approves one. Metadata-capable callers can still provide the same reference in
MCP `_meta`. The Gateway's existing scope validation and fail-closed write gate
remain unchanged. The full test suite uses fake downstream OpenWA, so live voice
delivery still requires a fresh-session canary after deployment.
