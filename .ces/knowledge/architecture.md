# Architecture

FastAPI serves HTTP routes and mounts an MCP app at `/mcp`. Google Workspace adapters handle Drive, Docs, and Sheets; source registry and access policies govern which resources can be read. Separate integrations cover HubSpot OAuth and remote MCP, n8n MCP configuration, and an Agent Signal inbox. Cloud Storage backs source proposals and HubSpot OAuth state.

A development branch exposes two fixed Management tools for the Acquisition exploration map and directed commands through Portal Backend. The command tool forwards bounded zoom, link-coverage, cross-market linkage and market-presence operations while Engine remains authoritative for validation and persistence. Gateway does not own the map or gain database access; Engine public web search remains a separate bounded adapter.

The Acquisition map read passes through Engine-owned company profiles, field provenance, link-specific contexts and scoped dispositions. Management-only company tools forward bounded field/tag updates and exact-ID identity link, unlink or alias split commands through the fixed Portal Backend route; they do not reconcile by name or expose the Human Portal's disposition action. Engine retains authorization, version, evidence and audit checks.

Management-only browser-dispatch tools read one Engine-owned contact checkpoint, record a tightly bounded human-operated observation, and ingest a human-observed Facebook or Instagram reply. Gateway also exposes an exact company/reply read for source verification. It validates the Brunova profile, supported social channels, HTTPS URLs, message hash, reply evidence ID and timestamp, then forwards the exact data through fixed Portal Backend routes. Engine verifies the immutable send and reply context. Gateway does not automate browser activity or perform provider effects.

A dedicated `whatsapp_worker` principal may project only OpenWA text send and message history for one configured session. It is separate from Management and the Agent Signal worker; the Engine must check its exact contact grant before using the delegated Gateway token.

A separate `crm_worker` principal exposes only one bounded HubSpot company transition tool. It cannot use generic CRM reads or edit contacts, deals or arbitrary properties. The Engine owns the one-use checkpoint and readback; Gateway owns the HubSpot credential and executes the narrow mutation.

A separate read-only Management tool lists bounded Facebook and Instagram reply-review targets derived from confirmed Engine sends. The Portal Backend delegates that projection to Engine-owned SQL, returning the latest confirmed intent per destination. A target identifies a thread to inspect; it is never evidence of a reply or permission to send.

## Evidence

- `app/main.py`
- `app/acquisition.py`
- `tests/test_acquisition.py`
- `app/policies/source_access.py`
- `app/source_proposal_store.py`
- `app/adapters/hubspot/token_store.py`
- `app/adapters/n8n/config.py`
