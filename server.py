"""
X1-BaaS-Engine — MCP Server Wrapper (Phase 3)

Exposes the BaaS scrape engine as an MCP (Model Context Protocol) server,
allowing AI agents to discover and call the scrape tool natively.

Transports:
  - stdio: for local agent integration (Claude Desktop, etc.)
  - streamable-http: for remote agent access (port 8001)
  - SSE: legacy transport (port 8001/sse)

Usage:
  python mcp_server.py                    # stdio (default)
  python mcp_server.py --transport http   # HTTP on port 8001
  python mcp_server.py --transport sse    # SSE on port 8001
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BAAAS_API_URL = os.getenv("BAAAS_API_URL", "http://localhost:8000")
BAAAS_API_KEY = os.getenv("BAAAS_API_KEY", "")
MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("MCP_PORT", "8001"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("baas-mcp")

# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------
from mcp.server.mcpserver import MCPServer

mcp = MCPServer(
    name="X1-BaaS-Engine",
    title="Browser-as-a-Service for AI Agents",
    description=(
        "Stealth web scraping engine optimized for autonomous AI agents. "
        "Returns clean Markdown content from any URL, with anti-bot bypass "
        "(Cloudflare Turnstile, Datadome). Pay-per-call via x402 micro-settlements."
    ),
    version="0.2.0",
    website_url="https://github.com/x1-baas-engine",
    instructions=(
        "Use the `scrape` tool to fetch and extract web content as clean Markdown. "
        "The engine handles JavaScript rendering, anti-bot bypass, and DOM cleaning automatically. "
        "Use `get_pricing` to check current pricing before scraping. "
        "Use `server_status` to check engine health and availability."
    ),
)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def scrape(
    url: str,
    wait_for_selector: Optional[str] = None,
    timeout_ms: int = 20000,
    block_media: bool = True,
    wait_strategy: Optional[str] = None,
    retry: bool = True,
    bypass_cache: bool = False,
    javascript: Optional[str] = None,
) -> str:
    """
    Scrape a URL and return clean Markdown content optimized for LLM context windows.

    This tool handles:
    - JavaScript rendering (SPA, dynamic content)
    - Anti-bot bypass (Cloudflare Turnstile, Datadome)
    - DOM cleaning (strips scripts, nav, footer, ads)
    - HTML-to-Markdown conversion (Mozilla Readability engine)
    - Automatic retry with escalating wait strategies
    - Domain cooldown to avoid rate-limiting
    - Response caching (5 min TTL)

    Args:
        url: The URL to scrape (must start with http:// or https://)
        wait_for_selector: Optional CSS selector to wait for before extraction (e.g., ".article-content")
        timeout_ms: Navigation timeout in milliseconds (default: 20000, max: 120000)
        block_media: Block images/fonts/video for faster loading (default: true)
        wait_strategy: Wait strategy: "default", "spa", "heavy", "cloudflare" (auto-detected if omitted)
        retry: Enable automatic retry on failure (default: true)
        bypass_cache: Skip cache, force fresh scrape (default: false)
        javascript: Custom JavaScript to execute after page load (e.g., "window.scrollTo(0, 1000)")

    Returns:
        Clean Markdown content extracted from the page, or an error message.
    """
    import httpx

    headers = {"Content-Type": "application/json"}
    if BAAAS_API_KEY:
        headers["Authorization"] = f"Bearer {BAAAS_API_KEY}"

    payload = {
        "url": url,
        "timeout_ms": min(max(timeout_ms, 1000), 120000),
        "block_media": block_media,
        "retry": retry,
        "bypass_cache": bypass_cache,
    }
    if wait_for_selector:
        payload["wait_for_selector"] = wait_for_selector
    if wait_strategy:
        payload["wait_strategy"] = wait_strategy
    if javascript:
        payload["javascript"] = javascript

    try:
        async with httpx.AsyncClient(timeout=130) as client:
            resp = await client.post(f"{BAAAS_API_URL}/v1/scrape", json=payload, headers=headers)

            if resp.status_code == 402:
                detail = resp.json().get("detail", {})
                return (
                    f"PAYMENT REQUIRED: This scrape costs ${detail.get('accepts', [{}])[0].get('price', '?')} USDC. "
                    f"Include X-PAYMENT header with signed EIP-3009 permit."
                )

            if resp.status_code == 401:
                return "AUTHENTICATION FAILED: Invalid or missing API key."

            if resp.status_code == 429:
                return "RATE LIMITED: Too many requests. Please wait and retry."

            if resp.status_code >= 400:
                detail = resp.json().get("detail", {})
                return f"SCRAPE ERROR ({resp.status_code}): {detail.get('message', resp.text)}"

            data = resp.json()
            scrape_data = data.get("data", {})
            title = scrape_data.get("title", "Untitled")
            markdown = scrape_data.get("markdown", "")
            char_count = scrape_data.get("character_count", 0)
            exec_ms = data.get("execution_time_ms", 0)

            return (
                f"# {title}\n\n"
                f"{markdown}\n\n"
                f"---\n"
                f"*Scraped in {exec_ms}ms | {char_count} characters | Source: {url}*"
            )

    except httpx.TimeoutException:
        return f"TIMEOUT: Scrape of {url} timed out after {timeout_ms}ms. Try increasing timeout_ms."
    except httpx.ConnectError:
        return f"CONNECTION ERROR: Cannot reach BaaS engine at {BAAAS_API_URL}. Is the server running?"
    except Exception as exc:
        logger.exception("MCP scrape error")
        return f"ERROR: {exc}"


@mcp.tool()
async def get_pricing() -> str:
    """
    Check current pricing and payment requirements for the BaaS scrape engine.

    Returns pricing info, supported networks, and payment instructions.
    No authentication required.

    Returns:
        Current pricing details and payment instructions.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{BAAAS_API_URL}/v1/pricing")
            data = resp.json()

            if not data.get("x402_enabled"):
                return "PRICING: Free tier — no payment required for scraping."

            return (
                f"PRICING:\n"
                f"- Price: ${data['price_usd']} USDC per scrape\n"
                f"- Network: {data['network']}\n"
                f"- Token: {data['token']} ({data['token_address']})\n"
                f"- Pay to: {data['pay_to']}\n"
                f"- Facilitator: {data['facilitator']}\n"
                f"- Payment header: {data['payment_header']}\n"
                f"- Instructions: {data['description']}"
            )
    except Exception as exc:
        return f"ERROR fetching pricing: {exc}"


@mcp.tool()
async def server_status() -> str:
    """
    Check the BaaS engine health and operational status.

    Returns browser status, uptime, x402 payment mode, and context count.

    Returns:
        Server health and diagnostic information.
    """
    import httpx

    headers = {}
    if BAAAS_API_KEY:
        headers["Authorization"] = f"Bearer {BAAAS_API_KEY}"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{BAAAS_API_URL}/health", headers=headers)

            if resp.status_code == 401:
                return "AUTH FAILED: Cannot check status — invalid API key."

            data = resp.json()
            return (
                f"SERVER STATUS:\n"
                f"- Status: {data['status']}\n"
                f"- Browser: {'active' if data['browser_active'] else 'inactive'}\n"
                f"- Scrapes performed: {data['browser_contexts']}\n"
                f"- Uptime: {data['uptime_seconds']}s\n"
                f"- x402 payments: {'enabled' if data.get('x402_enabled') else 'disabled (free tier)'}"
            )
    except httpx.ConnectError:
        return "OFFLINE: Cannot reach BaaS engine. Server may be down."
    except Exception as exc:
        return f"ERROR checking status: {exc}"


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

@mcp.resource("baas://pricing")
async def pricing_resource() -> str:
    """Current BaaS pricing and payment configuration."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{BAAAS_API_URL}/v1/pricing")
            return resp.text
    except Exception:
        return '{"error": "Cannot fetch pricing"}'


@mcp.resource("baas://status")
async def status_resource() -> str:
    """Current BaaS engine status and diagnostics."""
    import httpx
    headers = {}
    if BAAAS_API_KEY:
        headers["Authorization"] = f"Bearer {BAAAS_API_KEY}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{BAAAS_API_URL}/health", headers=headers)
            return resp.text
    except Exception:
        return '{"error": "Cannot fetch status"}'


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------
# Health Check Server (for Docker)
# ---------------------------------------------------------------------------
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"status":"ok","service":"baas-mcp"}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress health check logs

def start_health_server(port=8002):
    """Start a simple health check server on a separate port."""
    server = HTTPServer(('0.0.0.0', port), HealthHandler)
    server.serve_forever()

# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="X1-BaaS-Engine MCP Server")
    parser.add_argument(
        "--transport", "-t",
        choices=["stdio", "http", "sse"],
        default="stdio",
        help="Transport type (default: stdio)",
    )
    parser.add_argument("--host", default=MCP_HOST, help=f"Host for HTTP/SSE (default: {MCP_HOST})")
    parser.add_argument("--port", type=int, default=MCP_PORT, help=f"Port for HTTP/SSE (default: {MCP_PORT})")
    args = parser.parse_args()

    # Start health check server in background (for Docker)
    if args.transport in ["http", "sse"]:
        health_thread = threading.Thread(target=start_health_server, daemon=True)
        health_thread.start()
        logger.info("Health check server started on port 8002")

    if args.transport == "stdio":
        logger.info("Starting MCP server on stdio")
        mcp.run(transport="stdio")
    elif args.transport == "http":
        logger.info("Starting MCP server on http://%s:%d/mcp", args.host, args.port)
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
    elif args.transport == "sse":
        logger.info("Starting MCP server on sse://%s:%d", args.host, args.port)
        mcp.run(transport="sse", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
