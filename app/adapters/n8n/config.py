"""Secret-backed n8n MCP transport configuration."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True, repr=False)
class N8NMCPConfig:
    transport: Literal["streamable_http", "sse", "stdio"]
    url: str = field(default="", repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    command: str = field(default="", repr=False)
    args: tuple[str, ...] = field(default_factory=tuple, repr=False)
    environment: dict[str, str] = field(default_factory=dict, repr=False)

    @classmethod
    def from_environment(cls, variable: str = "N8N_MCP_JSON") -> "N8NMCPConfig":
        raw = os.getenv(variable, "")
        if not raw.strip():
            raise ValueError("n8n MCP configuration must be injected from Secret Manager")
        return cls.from_json(raw)

    @classmethod
    def from_json(cls, raw: str) -> "N8NMCPConfig":
        try:
            document = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("N8N_MCP_JSON is invalid JSON") from error
        if not isinstance(document, dict):
            raise ValueError("N8N_MCP_JSON must contain an object")
        servers = document.get("mcpServers", document)
        if not isinstance(servers, dict) or not servers:
            raise ValueError("N8N_MCP_JSON does not define an MCP server")
        entry = servers.get("n8n-mcp")
        if entry is None and len(servers) == 1:
            entry = next(iter(servers.values()))
        if not isinstance(entry, dict):
            raise ValueError("N8N_MCP_JSON does not define n8n-mcp")

        raw_type = str(entry.get("type", "")).strip().lower().replace("-", "").replace("_", "")
        if entry.get("command"):
            transport = "stdio"
        elif raw_type in {"sse"}:
            transport = "sse"
        elif raw_type in {"http", "streamablehttp"}:
            transport = "streamable_http"
        else:
            raise ValueError("N8N_MCP_JSON uses an unsupported transport")

        url = entry.get("url", "")
        command = entry.get("command", "")
        if transport == "stdio" and not isinstance(command, str):
            raise ValueError("n8n stdio command must be a string")
        if transport != "stdio" and (not isinstance(url, str) or not url.startswith(("https://", "http://"))):
            raise ValueError("n8n MCP URL is invalid")
        headers = entry.get("headers", {})
        environment = entry.get("env", {})
        args = entry.get("args", [])
        if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
            raise ValueError("n8n MCP headers must be strings")
        if not isinstance(environment, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in environment.items()):
            raise ValueError("n8n MCP environment must contain strings")
        if not isinstance(args, list) or not all(isinstance(value, str) for value in args):
            raise ValueError("n8n MCP args must be strings")
        return cls(
            transport=transport, url=url, headers=dict(headers), command=command,
            args=tuple(args), environment=dict(environment),
        )

    def safe_summary(self) -> dict[str, Any]:
        return {
            "configured": True,
            "transport": self.transport,
            "has_endpoint": bool(self.url),
            "authenticated": bool(self.headers or self.environment),
        }
