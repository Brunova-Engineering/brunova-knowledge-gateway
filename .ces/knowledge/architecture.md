# Architecture

FastAPI serves HTTP routes and mounts an MCP app at `/mcp`. Google Workspace adapters handle Drive, Docs, and Sheets; source registry and access policies govern which resources can be read. Separate integrations cover HubSpot OAuth and remote MCP, n8n MCP configuration, and an Agent Signal inbox. Cloud Storage backs source proposals and HubSpot OAuth state.

A development branch exposes two fixed Management tools for the Acquisition exploration map and directed commands through Portal Backend. The command tool forwards bounded zoom, link-coverage, cross-market linkage and market-presence operations while Engine remains authoritative for validation and persistence. Gateway does not own the map or gain database access; Engine public web search remains a separate bounded adapter.

The Acquisition map read passes through Engine-owned company profiles, field provenance, link-specific contexts and scoped dispositions. Management-only company tools forward bounded field/tag updates and exact-ID identity link, unlink or alias split commands through the fixed Portal Backend route; they do not reconcile by name or expose the Human Portal's disposition action. Engine retains authorization, version, evidence and audit checks.

## Evidence

- `app/main.py`
- `app/policies/source_access.py`
- `app/source_proposal_store.py`
- `app/adapters/hubspot/token_store.py`
- `app/adapters/n8n/config.py`
