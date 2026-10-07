"""Non-secret registry of named n8n instances and their secret bindings."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass


_INSTANCE_ID = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
_SECRET_ENV = re.compile(r"^N8N_[A-Z0-9_]+_MCP_JSON$|^N8N_MCP_JSON$")


@dataclass(frozen=True)
class N8NInstanceRegistry:
    secret_variables: dict[str, str]

    @classmethod
    def from_environment(cls) -> "N8NInstanceRegistry":
        raw = os.getenv("N8N_INSTANCE_REGISTRY_JSON", "").strip()
        if not raw:
            return cls({"brunova": "N8N_MCP_JSON"})
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError("n8n instance registry is invalid JSON") from error
        if not isinstance(entries, dict) or not entries:
            raise ValueError("n8n instance registry must be a nonempty object")
        if not all(
            isinstance(key, str) and _INSTANCE_ID.fullmatch(key)
            and isinstance(value, str) and _SECRET_ENV.fullmatch(value)
            for key, value in entries.items()
        ):
            raise ValueError("n8n instance registry contains an invalid binding")
        if len(set(entries.values())) != len(entries):
            raise ValueError("n8n instances must use distinct secrets")
        return cls(dict(entries))

    def secret_variable(self, instance_id: str) -> str:
        try:
            return self.secret_variables[instance_id]
        except KeyError as error:
            raise ValueError("unknown n8n instance") from error

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.secret_variables))
