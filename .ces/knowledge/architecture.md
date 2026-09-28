# Architecture

FastAPI serves HTTP routes and mounts an MCP app at `/mcp`. Google Workspace adapters handle Drive, Docs, and Sheets; source registry and access policies govern which resources can be read. Separate integrations cover HubSpot OAuth and remote MCP, n8n MCP configuration, and an Agent Signal inbox. Cloud Storage backs source proposals and HubSpot OAuth state.

## Evidence

- `app/main.py`
- `app/policies/source_access.py`
- `app/source_proposal_store.py`
- `app/adapters/hubspot/token_store.py`
- `app/adapters/n8n/config.py`
