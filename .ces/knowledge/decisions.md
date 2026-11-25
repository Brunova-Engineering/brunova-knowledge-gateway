# Decisions

- **Explicit; accepted gap:** ADR 0001 excludes native Google Sheets image insert and replace operations and rejects `IMAGE()` with a transient signed URL for v0.27.1.
- **Explicit; deployed per dated audit:** OpenWA writes accept a bounded approval reference through MCP metadata or a compatibility argument; conflicting values are rejected. The 2026-09-07 audit records production deployment, while real delivery verification remained pending.
- **Inference from code; implemented:** Source proposals use Cloud Storage generation preconditions and retry on concurrent updates.
- **Explicit; deployed 2026-11-24:** Brunova POC uses `jorgeaveraf` as universal GitHub transport. A new organizational Context Graph backing must be private, match its protected Core marker and grant effective transport access before Policy activates its release. Panky Actor access remains governed by OIDC and the Context Graph; Nat and Ilian need no individual GitHub login in POC. Provisioning still needs its server-side GitHub credential and a live creation check before it is operational.

## Evidence

- `docs/adr/0001-google-sheets-native-images.md`
- `docs/openwa-approval-propagation.md`
- `app/source_proposal_store.py`
- `policy_backend_v2/repository_publication.py`
