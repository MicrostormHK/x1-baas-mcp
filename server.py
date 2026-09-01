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
MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("MCP_PORT", "8001"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("baas-mcp")

# ---------------------------------------------------------------------------
# Caller authentication pass-through
#
# The MCP server is a thin protocol translation layer (JSON-RPC ↔ REST). It
# holds NO credentials of its own and never authenticates on the caller's
# behalf. Every request is forwarded to the BaaS engine verbatim: the caller's
# own auth headers (if any) are passed through, and the engine's response —
# including a 402 Payment Required carrying x402 payment requirements — is
# relayed back to the caller unchanged.
#
#   Authorization: Bearer <api_key>  — baas_live_* key (subscription)
#   PAYMENT-SIGNATURE / X-PAYMENT    — x402 payment proof (pay-per-scrape)
#
# With no auth header, the engine responds 402 with x402 payment requirements,
# which the MCP server relays so the caller can pay (Base USDC and/or Solana)
# and retry with a payment proof.
# ---------------------------------------------------------------------------

_FORWARD_AUTH_HEADERS = ("authorization", "payment-signature", "x-payment")


def _caller_auth_headers(ctx: Optional["Context"]) -> dict[str, str]:
    """Extract the caller's auth headers from the MCP request context.

    Returns a dict of headers to forward to the engine. Empty when the caller
    supplied no credentials (e.g. over stdio, which has no HTTP headers).
    """
    headers = getattr(ctx, "headers", None) if ctx is not None else None
    if not headers:
        return {}
    return {
        name: value
        for name, value in headers.items()
        if name.lower() in _FORWARD_AUTH_HEADERS
    }


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------
from mcp.server.mcpserver import Context, MCPServer

mcp = MCPServer(
    name="X1-BaaS",
    title="X1-BaaS — Stealth Web Scraping for AI Agents",
    description=(
        "Stealth web scraping engine optimized for autonomous AI agents. "
        "Returns clean Markdown content from any URL, with anti-bot bypass "
        "(Cloudflare Turnstile, Datadome). Pay-per-call via x402 micropayments."
    ),
    version="0.3.2",
    website_url="https://baas.tazpal.com",
    instructions=(
        "Use the `scrape` tool to fetch and extract web content as clean Markdown. "
        "The engine handles JavaScript rendering, anti-bot bypass, and DOM cleaning automatically. "
        "The `scrape` tool is paid. If the request carries no API key "
        "(`Authorization: Bearer baas_live_*`) or x402 payment proof, the engine "
        "returns a 402 with x402 payment requirements (Base USDC and/or Solana), "
        "which are relayed to you so you can pay and retry. "
        "Use `get_pricing` to check current pricing before scraping. "
        "Use `server_status` to check engine health and availability."
    ),
)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool(
    annotations={
        "title": "Scrape Web Page",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def scrape(
    url: str,
    output: Optional[str] = None,
    wait_for_selector: Optional[str] = None,
    timeout_ms: int = 20000,
    block_media: bool = True,
    wait_strategy: Optional[str] = None,
    retry: bool = True,
    bypass_cache: bool = False,
    javascript: Optional[str] = None,
    ctx: Optional[Context] = None,
) -> str:
    """
    Scrape a URL and return content in your preferred format.

    Supported output formats:
    - markdown (default): Clean LLM-ready Markdown text
    - screenshot: PNG/JPEG image of the page
    - pdf: PDF document of the page
    - csv: Table data extracted as CSV
    - html: Sanitized HTML with scripts/ads removed

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
        output: Output format: "markdown" (default), "screenshot", "pdf", "csv", "html"
        wait_for_selector: Optional CSS selector to wait for before extraction (e.g., ".article-content")
        timeout_ms: Navigation timeout in milliseconds (default: 20000, max: 120000)
        block_media: Block images/fonts/video for faster loading (default: true)
        wait_strategy: Wait strategy: "default", "spa", "heavy", "cloudflare" (auto-detected if omitted)
        retry: Enable automatic retry on failure (default: true)
        bypass_cache: Skip cache, force fresh scrape (default: false)
        javascript: Custom JavaScript to execute after page load (e.g., "window.scrollTo(0, 1000)")

    Returns:
        Content in the requested format, or an error message.
    """
    import httpx

    # Thin pass-through: forward the caller's auth headers (if any) verbatim to
    # the engine. Never fall back to an embedded server-side key. When the
    # caller supplies no credentials, the engine responds 402 with x402 payment
    # requirements, which are relayed to the caller unchanged (see 402 branch).
    forwarded = _caller_auth_headers(ctx)

    headers = {"Content-Type": "application/json"}
    headers.update(forwarded)

    payload = {
        "url": url,
        "timeout_ms": min(max(timeout_ms, 1000), 120000),
        "block_media": block_media,
        "retry": retry,
        "bypass_cache": bypass_cache,
    }
    if output:
        payload["output"] = output
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
                # Pass through the engine's x402 payment requirements verbatim
                # so the caller can pay (Base USDC and/or Solana) and retry with
                # a payment proof. The engine also sends the envelope in the
                # `PAYMENT-REQUIRED` response header (base64), but the tool
                # result carries the full JSON body so any x402 client can parse
                # the requirements directly.
                return resp.text

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


@mcp.tool(
    annotations={
        "title": "Get Pricing",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
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


@mcp.tool(
    annotations={
        "title": "Server Status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def server_status() -> str:
    """
    Check the BaaS engine health and operational status.

    Returns browser status, uptime, x402 payment mode, and context count.

    Returns:
        Server health and diagnostic information.
    """
    import httpx

    headers = {}

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
