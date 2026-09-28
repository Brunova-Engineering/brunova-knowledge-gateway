# Conventions

Configuration comes from environment variables and validates required Workspace identity and content limits. Public adapter responses use Pydantic models and include request IDs; source metadata includes classification. Source access fails closed outside registered locations or for blocked IDs. Tests inject an ephemeral gateway token.

## Evidence

- `app/config/settings.py`
- `app/adapters/google_workspace/models.py`
- `app/policies/source_access.py`
- `tests/conftest.py`
