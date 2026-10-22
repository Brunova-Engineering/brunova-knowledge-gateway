"""Verify an authenticated MCP tools/list response without logging credentials."""

from __future__ import annotations

import json
import os
import sys

import httpx

PROTOCOL_VERSION = "2025-06-18"


def parse_message(response: httpx.Response) -> dict:
    if response.status_code != 200:
        raise RuntimeError(f"MCP request returned HTTP {response.status_code}.")
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        return response.json()
    if "text/event-stream" in content_type:
        for line in response.text.splitlines():
            if line.startswith("data:"):
                try:
                    return json.loads(line.partition(":")[2].strip())
                except json.JSONDecodeError:
                    continue
    raise RuntimeError("MCP response did not contain a JSON-RPC message.")


def main() -> int:
    url = os.environ.get("MCP_URL", "").strip()
    token = os.environ.get("MCP_VERIFY_TOKEN", "")
    cloud_run_token = os.environ.get("CLOUD_RUN_ID_TOKEN", "")
    if not url or not token or not cloud_run_token:
        raise SystemExit("MCP_URL, MCP_VERIFY_TOKEN, and CLOUD_RUN_ID_TOKEN are required.")

    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "X-Serverless-Authorization": f"Bearer {cloud_run_token}",
    }
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        initialized = client.post(
            url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "gateway-deploy-verifier", "version": "1.0"},
                },
            },
        )
        initialize_message = parse_message(initialized)
        if "error" in initialize_message or not initialize_message.get("result", {}).get("serverInfo"):
            raise RuntimeError("MCP initialization failed.")

        session_id = initialized.headers.get("mcp-session-id")
        session_headers = {**headers, "MCP-Protocol-Version": PROTOCOL_VERSION}
        if session_id:
            session_headers["Mcp-Session-Id"] = session_id
        notification = client.post(
            url,
            headers=session_headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        if notification.status_code not in {200, 202}:
            raise RuntimeError("MCP initialization notification failed.")

        names: list[str] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        request_id = 2
        while True:
            params = {"cursor": cursor} if cursor else {}
            listed = client.post(
                url,
                headers=session_headers,
                json={"jsonrpc": "2.0", "id": request_id, "method": "tools/list", "params": params},
            )
            catalog_message = parse_message(listed)
            if "error" in catalog_message:
                raise RuntimeError("MCP tools/list failed.")
            result = catalog_message.get("result", {})
            tools = result.get("tools")
            if not isinstance(tools, list):
                raise RuntimeError("MCP tools/list returned an invalid catalog page.")
            page_names = [tool.get("name") for tool in tools if isinstance(tool, dict)]
            if len(page_names) != len(tools) or any(not isinstance(name, str) or not name for name in page_names):
                raise RuntimeError("MCP tools/list returned malformed tool entries.")
            names.extend(page_names)
            cursor = result.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                raise RuntimeError("MCP tools/list returned an invalid pagination cursor.")
            seen_cursors.add(cursor)
            request_id += 1

    if not names:
        raise RuntimeError("MCP tools/list returned an empty catalog.")
    if len(names) != len(set(names)):
        raise RuntimeError("MCP tools/list returned duplicate tool names.")

    print(f"Verified authenticated MCP catalog with {len(names)} tools.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (httpx.HTTPError, RuntimeError, ValueError) as error:
        print(f"MCP verification failed: {error}", file=sys.stderr)
        raise SystemExit(1)
