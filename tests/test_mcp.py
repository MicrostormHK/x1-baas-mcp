"""
X1-BaaS-Engine — MCP Server Test Suite

Tests the MCP server tools via HTTP transport.
Requires: BaaS server on :8000 + MCP server on :8001

Run:
  pytest test_mcp.py -v -s
"""

from __future__ import annotations

import json
import os
import subprocess
import time

import httpx
import pytest

BAAAS_URL = "http://localhost:8000"
MCP_URL = "http://localhost:8001/mcp"
REQUEST_TIMEOUT = 60.0

# Valid API key for authenticated tests. The engine accepts its legacy env-var
# key (``BAAAS_API_KEY``) as a valid bearer token, so that value (or an explicit
# ``TEST_BAAAS_API_KEY``) can be used to exercise the authenticated path.
TEST_API_KEY = os.environ.get("TEST_BAAAS_API_KEY") or os.environ.get("BAAAS_API_KEY", "")

# Deliberately invalid key in the engine's ``baas_live_*`` shape.
INVALID_API_KEY = "baas_live_invalidtestkey0000000000000000000000000000000000000000"


@pytest.fixture(scope="module")
def client() -> httpx.Client:
    with httpx.Client(timeout=REQUEST_TIMEOUT) as c:
        yield c


@pytest.fixture(scope="module", autouse=True)
def check_servers(client: httpx.Client):
    """Verify both BaaS and MCP servers are running."""
    try:
        r = client.get(f"{BAAAS_URL}/health", headers={"Authorization": "Bearer ***"})
        assert r.status_code == 200, f"BaaS health failed: {r.status_code}"
    except httpx.ConnectError:
        pytest.fail("BaaS server not running on :8000. Start with: docker compose up -d")

    # MCP server check — send a JSON-RPC initialize request
    try:
        r = client.post(MCP_URL, json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "0.1.0"},
            },
        })
        assert r.status_code in (200, 405), f"MCP endpoint returned {r.status_code}"
    except httpx.ConnectError:
        pytest.fail("MCP server not running on :8001. Start with: python mcp_server.py --transport http")


class TestMCPServer:
    """Test MCP server tools via JSON-RPC over HTTP."""

    def _parse_sse(self, resp: httpx.Response) -> dict:
        """Parse SSE response into JSON-RPC result."""
        text = resp.text
        for line in text.split("\n"):
            if line.startswith("data: "):
                import json
                return json.loads(line[6:])
        raise ValueError(f"No SSE data found in response: {text[:200]}")

    def _call_tool(self, client: httpx.Client, tool_name: str, arguments: dict, auth_header: str | None = None) -> dict:
        """Call an MCP tool via JSON-RPC."""
        # First initialize
        init_resp = client.post(MCP_URL, json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "0.1.0"},
            },
        })

        # Extract session ID if provided
        headers = {"Content-Type": "application/json"}
        if "mcp-session-id" in init_resp.headers:
            headers["mcp-session-id"] = init_resp.headers["mcp-session-id"]

        # Send initialized notification
        client.post(MCP_URL, json={
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        }, headers=headers)

        # Call the tool
        if auth_header:
            headers["Authorization"] = auth_header
        resp = client.post(MCP_URL, json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        }, headers=headers)

        assert resp.status_code == 200, f"JSON-RPC call failed: {resp.status_code} — {resp.text}"
        return self._parse_sse(resp)

    def test_list_tools(self, client: httpx.Client):
        """Verify MCP server exposes expected tools."""
        print("\n--- MCP: List Tools ---")

        # Initialize
        init_resp = client.post(MCP_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "0.1.0"},
            },
        })
        headers = {"Content-Type": "application/json"}
        if "mcp-session-id" in init_resp.headers:
            headers["mcp-session-id"] = init_resp.headers["mcp-session-id"]

        client.post(MCP_URL, json={
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }, headers=headers)

        # List tools
        resp = client.post(MCP_URL, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        }, headers=headers)

        assert resp.status_code == 200
        data = self._parse_sse(resp)
        tools = data.get("result", {}).get("tools", [])
        tool_names = [t["name"] for t in tools]

        print(f"  Found {len(tools)} tools: {tool_names}")
        assert "scrape" in tool_names, "Missing 'scrape' tool"
        assert "get_pricing" in tool_names, "Missing 'get_pricing' tool"
        assert "server_status" in tool_names, "Missing 'server_status' tool"
        print("  ✅ PASSED")

    def test_server_status_tool(self, client: httpx.Client):
        """Call server_status tool via MCP."""
        print("\n--- MCP: server_status tool ---")
        result = self._call_tool(client, "server_status", {})
        content = result.get("result", {}).get("content", [])
        text = content[0].get("text", "") if content else ""

        print(f"  Response: {text[:200]}")
        assert "SERVER STATUS" in text or "status" in text.lower()
        assert "active" in text.lower() or "ok" in text.lower()
        print("  ✅ PASSED")

    def test_get_pricing_tool(self, client: httpx.Client):
        """Call get_pricing tool via MCP."""
        print("\n--- MCP: get_pricing tool ---")
        result = self._call_tool(client, "get_pricing", {})
        content = result.get("result", {}).get("content", [])
        text = content[0].get("text", "") if content else ""

        print(f"  Response: {text[:200]}")
        assert "pricing" in text.lower() or "free" in text.lower()
        print("  ✅ PASSED")

    @pytest.mark.skipif(not TEST_API_KEY, reason="TEST_BAAAS_API_KEY / BAAAS_API_KEY not set")
    def test_scrape_tool(self, client: httpx.Client):
        """Call scrape tool via MCP to fetch a real page."""
        print("\n--- MCP: scrape tool ---")
        result = self._call_tool(client, "scrape", {
            "url": "https://example.com",
            "timeout_ms": 15000,
        }, auth_header=f"Bearer {TEST_API_KEY}")
        content = result.get("result", {}).get("content", [])
        text = content[0].get("text", "") if content else ""

        print(f"  Response ({len(text)} chars): {text[:200]}...")
        assert "Example Domain" in text or "example" in text.lower()
        assert len(text) > 50
        print("  ✅ PASSED")

    @pytest.mark.skipif(not TEST_API_KEY, reason="TEST_BAAAS_API_KEY / BAAAS_API_KEY not set")
    def test_scrape_invalid_url(self, client: httpx.Client):
        """Call scrape tool with invalid URL — should return error, not crash."""
        print("\n--- MCP: scrape tool (invalid URL) ---")
        result = self._call_tool(client, "scrape", {
            "url": "https://this-does-not-exist-99999.org",
            "timeout_ms": 10000,
        }, auth_header=f"Bearer {TEST_API_KEY}")
        content = result.get("result", {}).get("content", [])
        text = content[0].get("text", "") if content else ""

        print(f"  Response: {text[:200]}")
        assert "ERROR" in text or "error" in text.lower() or "CONNECTION" in text
        print("  ✅ PASSED")


class TestMCPAuth:
    """Caller-authentication pass-through on MCP ``tools/call``.

    The MCP server must NOT authenticate on the caller's behalf. ``tools/call``
    (especially ``scrape``) is forwarded verbatim to the engine, along with any
    caller-supplied auth headers. With no credentials, the engine returns 402
    with x402 payment requirements, which the MCP server relays unchanged (it
    does NOT block the request itself).
    """

    def _parse_sse(self, resp: httpx.Response) -> dict:
        text = resp.text
        for line in text.split("\n"):
            if line.startswith("data: "):
                import json
                return json.loads(line[6:])
        # Non-SSE JSON response (stateless HTTP)
        try:
            return resp.json()
        except Exception:
            raise ValueError(f"No SSE data or JSON found in response: {text[:200]}")

    def _handshake(self, client: httpx.Client, headers: dict | None = None) -> dict:
        """Perform initialize + notifications/initialized, return request headers."""
        h = {"Content-Type": "application/json"}
        if headers:
            h.update(headers)

        init_resp = client.post(MCP_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "auth-test-client", "version": "0.1.0"},
            },
        }, headers=h)
        assert init_resp.status_code in (200, 405), f"initialize failed: {init_resp.status_code}"

        if "mcp-session-id" in init_resp.headers:
            h["mcp-session-id"] = init_resp.headers["mcp-session-id"]

        client.post(MCP_URL, json={
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }, headers=h)
        return h

    def _call_scrape(self, client: httpx.Client, auth_header: str | None = None) -> tuple[dict, int]:
        """Call the ``scrape`` tool, optionally with an Authorization header."""
        headers = self._handshake(client)
        if auth_header:
            headers["Authorization"] = auth_header

        resp = client.post(MCP_URL, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "scrape", "arguments": {
                "url": "https://example.com", "timeout_ms": 15000,
            }},
        }, headers=headers)
        return self._parse_sse(resp), resp.status_code

    @staticmethod
    def _tool_text(result: dict) -> str:
        content = result.get("result", {}).get("content", [])
        return content[0].get("text", "") if content else ""

    def test_unauthenticated_scrape_relays_402(self, client: httpx.Client):
        """tools/call without credentials must relay the engine's 402 x402 payment requirements (not block)."""
        print("\n--- MCP AUTH: unauthenticated scrape (402 pass-through) ---")
        result, status = self._call_scrape(client, auth_header=None)
        text = self._tool_text(result)

        print(f"  Response ({status}): {text[:200]}")
        # The MCP server must NOT block unauthenticated requests. It forwards
        # to the engine, which responds 402 with the x402 payment envelope;
        # the MCP server relays that envelope verbatim.
        try:
            envelope = json.loads(text)
        except json.JSONDecodeError:
            pytest.fail(f"Expected JSON x402 envelope, got: {text[:200]!r}")
        assert envelope.get("x402Version") == 2
        accepts = envelope.get("accepts", [])
        assert any("base" in (a.get("network") or "") for a in accepts), "expected Base USDC rail"
        assert any("solana" in (a.get("network") or "") for a in accepts), "expected Solana USDC rail"
        print("  ✅ PASSED")

    def test_invalid_key_fails(self, client: httpx.Client):
        """An invalid API key must be rejected by the engine."""
        print("\n--- MCP AUTH: invalid API key ---")
        result, status = self._call_scrape(client, auth_header=f"Bearer {INVALID_API_KEY}")
        text = self._tool_text(result)

        print(f"  Response ({status}): {text[:200]}")
        assert "AUTHENTICATION FAILED" in text or "AUTH" in text.upper()
        print("  ✅ PASSED")

    @pytest.mark.skipif(not TEST_API_KEY, reason="TEST_BAAAS_API_KEY / BAAAS_API_KEY not set")
    def test_valid_key_succeeds(self, client: httpx.Client):
        """A valid API key must be forwarded and accepted by the engine."""
        print("\n--- MCP AUTH: valid API key ---")
        result, status = self._call_scrape(client, auth_header=f"Bearer {TEST_API_KEY}")
        text = self._tool_text(result)

        print(f"  Response ({status}, {len(text)} chars): {text[:200]}")
        assert "Example Domain" in text or "example" in text.lower()
        assert len(text) > 50
        print("  ✅ PASSED")
