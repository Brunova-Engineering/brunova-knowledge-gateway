"""The CI application token must expose only the read-only signal list."""

from __future__ import annotations

import httpx
import pytest

from scripts import verify_mcp_catalog


class _Client:
    def __init__(self, names: list[str]):
        self.names = names

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def post(self, _url, *, json, **_kwargs):
        if json["method"] == "initialize":
            return httpx.Response(200, json={"result": {"serverInfo": {"name": "test"}}})
        if json["method"] == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(200, json={"result": {"tools": [{"name": name} for name in self.names]}})


@pytest.mark.parametrize("names,allowed", [
    (["list_agent_signals"], True),
    (["list_agent_signals", "claim_agent_signal"], False),
    (["acquisition_get_cycle"], False),
    ([], False),
])
def test_ci_catalog_requires_exact_read_only_capability(monkeypatch, names, allowed):
    monkeypatch.setenv("MCP_URL", "https://example.test/mcp/")
    monkeypatch.setenv("MCP_VERIFY_TOKEN", "test-gateway-token")
    monkeypatch.setenv("CLOUD_RUN_ID_TOKEN", "test-cloud-run-token")
    monkeypatch.setattr(verify_mcp_catalog.httpx, "Client", lambda **_kwargs: _Client(names))
    if allowed:
        assert verify_mcp_catalog.main() == 0
    else:
        with pytest.raises(RuntimeError, match="unexpected capability catalog"):
            verify_mcp_catalog.main()
