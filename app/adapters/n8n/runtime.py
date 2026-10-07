"""Lazy n8n runtime isolated from other provider runtimes."""

from functools import lru_cache

from app.adapters.n8n.config import N8NMCPConfig
from app.adapters.n8n.instances import N8NInstanceRegistry
from app.adapters.n8n.mcp_client import N8NMCPClient
from app.config.settings import get_settings


@lru_cache
def get_n8n_client(instance_id: str = "brunova") -> N8NMCPClient:
    settings = get_settings()
    variable = N8NInstanceRegistry.from_environment().secret_variable(instance_id)
    return N8NMCPClient(
        N8NMCPConfig.from_environment(variable),
        discovery_ttl_seconds=settings.n8n_discovery_ttl_seconds,
        timeout_seconds=settings.n8n_timeout_seconds,
    )
