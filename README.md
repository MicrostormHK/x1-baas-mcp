# X1-BaaS — Scraping API for AI Agents

> Stealth web scraping engine with native MCP integration and x402 micropayments. Returns clean, LLM-ready Markdown from any URL.

[![smithery badge](https://smithery.ai/badge/tazpal/x1-baas)](https://smithery.ai/servers/tazpal/x1-baas)
[![MCP Compatible](https://img.shields.io/badge/MCP-compatible-blue)](https://modelcontextprotocol.io)
[![x402](https://img.shields.io/badge/x402-payment%20protocol-green)](https://x402.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## Features

- **MCP-native** — plug into Claude Desktop, Cursor, or any MCP client in seconds
- **Stealth browser** — Camoufox engine bypasses Cloudflare Turnstile, Datadome
- **x402 micropayments** — agents pay per request with crypto, no accounts needed
- **LLM-optimized** — clean Markdown output, not raw HTML dumps
- **Anti-bot bypass** — humanized fingerprints, automatic retry
- **Multiple outputs** — Markdown, screenshot, PDF, CSV, HTML

## Quick Start

### Option 1: MCP Client (Recommended)

Add to your MCP client config:

```json
{
  "mcpServers": {
    "x1-baas": {
      "url": "https://api.tazpal.com/mcp",
      "description": "Stealth web scraping for AI agents"
    }
  }
}
```

Or use stdio transport:

```json
{
  "mcpServers": {
    "x1-baas": {
      "command": "npx",
      "args": ["-y", "x1-baas-mcp"],
      "env": {
        "BAAAS_API_URL": "https://api.tazpal.com"
      }
    }
  }
}
```

> **Note:** The MCP server is a thin pass-through — it holds no credentials.
> Over HTTP, callers present their own API key or x402 payment proof.
> Over stdio (no HTTP headers), scrape calls return x402 payment requirements.

### Option 2: Direct API Call

```bash
# Scrape a page (API key auth)
curl -X POST https://api.tazpal.com/v1/scrape \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{"url": "https://example.com"}'

# Or pay with x402 (no account needed)
# Send request without auth → receive 402 with payment instructions
# Sign payment → resend with PAYMENT-SIGNATURE header
```

### Option 3: Self-Hosted

```bash
git clone https://github.com/YOUR_USERNAME/x1-baas-mcp.git
cd x1-baas-mcp
docker compose up -d
```

## MCP Tools

The MCP server exposes 3 tools:

| Tool | Description |
|------|-------------|
| `scrape` | Scrape a URL and return clean Markdown. Handles JS rendering, anti-bot bypass, DOM cleaning. |
| `get_pricing` | Check current pricing and payment requirements. |
| `server_status` | Check engine health and operational status. |

## MCP Resources

| Resource | Description |
|----------|-------------|
| `baas://pricing` | Current pricing and payment configuration |
| `baas://status` | Engine status and diagnostics |

## Pricing

| Plan | Price | Details |
|------|-------|---------|
| **Pay-per-use** | $0.005/request | x402 crypto payments on Base (USDC) |
| **API Key** | Contact api@tazpal.com | For higher limits and traditional auth |

No account required for x402 payments. Failed scrapes are never charged.

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1/scrape` | POST | Scrape a URL → clean Markdown |
| `/v1/extract` | POST | Structured extraction with JSON schema |
| `/v1/crawl` | POST | Multi-page crawl with depth control |
| `/v1/pricing` | GET | Current pricing info |
| `/health` | GET | Health check |

## Architecture

```
┌─────────────┐     ┌─────────────┐     ┌───────────┐
│  MCP Client │────▶│  MCP Server │────▶│  BaaS     │
│  (Claude,   │     │  :8001      │     │  Engine   │
│   Cursor)   │     │  stdio/http │     │  :8000    │
└─────────────┘     └─────────────┘     └─────┬─────┘
                                              │
                                        ┌─────▼─────┐
                                        │  Camoufox │
                                        │  (Firefox)│
                                        └───────────┘
```

## Self-Hosting

### Prerequisites

- Docker & Docker Compose
- (Optional) API key from api@tazpal.com

### Setup

```bash
# Clone
git clone https://github.com/YOUR_USERNAME/x1-baas-mcp.git
cd x1-baas-mcp

# Configure
cp infra/.env.example infra/.env
# Edit infra/.env with your settings

# Start
docker compose -f infra/docker-compose.yml up -d

# Verify
curl http://localhost:8000/health
curl http://localhost:8002/health
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BAAAS_API_URL` | `http://localhost:8000` | BaaS engine URL |
| `MCP_HOST` | `0.0.0.0` | MCP server bind host |
| `MCP_PORT` | `8001` | MCP server port |
| `X402_ENABLED` | `true` | Enable x402 payments |
| `X402_FACILITATOR_URL` | `https://x402.org/facilitator` | Payment facilitator |

## Documentation

- [API Docs](https://api.tazpal.com/docs) — Full API reference
- [MCP Protocol](https://modelcontextprotocol.io) — MCP specification
- [x402 Protocol](https://x402.org) — Payment protocol spec

## License

MIT — see [LICENSE](LICENSE).

## Links

- [Landing Page](https://baas.tazpal.com)
- [API Status](https://api.tazpal.com/health)
- [x402 Bazaar](https://x402.org) — listed on CDP Bazaar
- [MCP Registry](https://modelcontextprotocol.io) — published as `com.tazpal/x1-baas`
