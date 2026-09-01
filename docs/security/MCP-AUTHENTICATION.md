# MCP Server Authentication

> Status: ✅ Implemented (2026-09-01) — thin pass-through (x402 payment auth)

## Problem

Originally the MCP server (`mcp/server.py`) authenticated on the caller's
behalf using an embedded server-side `BAAAS_API_KEY`. Any client that reached
the public `https://api.tazpal.com/mcp*` ingress could call `tools/call`
(`scrape`) with no credentials and the server would silently pay/authenticate
for them — bypassing the x402 payment system and the engine's rate limits.

## Fix

The MCP server is now a **thin pass-through**. It holds no credentials and
never authenticates for the caller. Every request is forwarded to the BaaS
engine verbatim; the caller's own auth headers (if any) are passed through,
and the engine's response — **including a 402 Payment Required carrying x402
payment requirements** — is relayed back to the caller unchanged. The server
never blocks unauthenticated requests: with no credentials, the engine itself
returns 402 and the MCP server relays that 402 so the caller can pay (Base
USDC and/or Solana) and retry with a payment proof.

```
User/Agent → MCP (localhost:8001) → Engine (localhost:8000)
             ↑ requires caller's    ↑ validates API key
               API key or x402        or x402 payment
```

## Auth flow

| MCP method         | Auth required | Notes                                                        |
|--------------------|---------------|--------------------------------------------------------------|
| `initialize`       | No            | Required by the MCP protocol                                 |
| `tools/list`       | No            | Tool discovery                                               |
| `tools/call`       | Engine-decided | Forwarded; engine returns 402 if no credentials, else 200    |
| `get_pricing`      | No            | Public engine endpoint (`/v1/pricing`)                       |
| `server_status`    | No            | Public engine endpoint (`/health`)                           |

### Credentials the caller may present

1. **API key** — `Authorization: Bearer baas_live_<key>`
   - Create an account: `POST https://api.tazpal.com/v1/auth/register`
   - Create a key: `POST https://api.tazpal.com/v1/keys`
2. **x402 payment proof** — `PAYMENT-SIGNATURE` header (v2), or legacy
   `X-PAYMENT` header (v1)

The MCP server forwards the `Authorization`, `PAYMENT-SIGNATURE`, and
`X-PAYMENT` headers from the incoming MCP request to the engine's
`POST /v1/scrape`. The engine handles validation, rate limiting (30 rpm/IP),
and payment settlement exactly as it does for direct REST callers.

### No credentials on `tools/call`

The MCP server does **not** block the request. It forwards the `scrape` call
to the engine without an auth header; the engine responds `402 Payment
Required` with the full x402 v2 payment envelope (JSON body + a base64
`PAYMENT-REQUIRED` header). The MCP server relays the engine's JSON body
verbatim as the tool result, so an x402 client can parse `accepts[]` (Base
USDC and Solana USDC rails) and pay directly:

```json
{
  "x402Version": 2,
  "resource": { "url": "https://api.tazpal.com/v1/scrape", ... },
  "accepts": [
    { "scheme": "exact", "network": "base", "asset": "0x8335...", "amount": "5000", "payTo": "0x6520..." },
    { "scheme": "exact", "network": "solana:5eykt4Us...", "asset": "EPjFWd...", "amount": "5000", "payTo": "BjSzfv..." }
  ],
  "error": "Payment required — no valid API key or X-PAYMENT header"
}
```

The agent then pays via x402, obtains a payment proof, and retries with the
proof in a `PAYMENT-SIGNATURE` (or `X-PAYMENT`) header — which the MCP server
forwards to the engine for settlement.

## Configuration

- `BAAAS_API_URL` — engine base URL (default `http://localhost:8000`).
- The embedded `BAAAS_API_KEY` env var was **removed** from the MCP container;
  the `baas-mcp` service in `infra/docker-compose.yml` no longer loads the
  shared `.env` file and sets no API key in its `environment`, so no engine
  secrets reach the MCP container.

## Tests

`tests/test_mcp.py` → `TestMCPAuth`:

- `test_unauthenticated_scrape_relays_402` — no credentials → engine 402 x402
  payment envelope relayed verbatim (Base + Solana rails)
- `test_invalid_key_fails` — bad `baas_live_*` key → `AUTHENTICATION FAILED`
- `test_valid_key_succeeds` — valid key → successful scrape

Run with a valid key available (the engine's legacy `BAAAS_API_KEY` is
accepted as a bearer token):

```bash
export TEST_BAAAS_API_KEY="$(grep -E '^BAAAS_API_KEY=' infra/.env | cut -d= -f2-)"
pytest tests/test_mcp.py -v
```
