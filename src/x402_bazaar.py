"""
x402 Bazaar discovery extension (CDP x402 Bazaar listing).

Builds the ``extensions.bazaar`` block that a resource server advertises in its
402 ``PaymentRequired`` envelope so the CDP Facilitator can index the route in
the x402 Bazaar (discoverable via CDP APIs, the Bazaar MCP server, Amazon
Bedrock AgentCore, and agentic.market).

Python discovery is OPT-IN (the Python CDP SDK has no ``createX402Server``
auto-declaration; you call ``declare_discovery_extension`` per route). This
module reproduces the x402-foundation ``declare_discovery_extension`` wire shape
(``python/x402/extensions/bazaar/resource_service.py`` + ``types.py``) without
pulling the ``x402`` package in — the rest of the codebase hand-rolls v2 models
(see ``x402_verify.py``), so we keep that convention.

The wire shape of a body-method (POST) declaration is::

    {
      "bazaar": {
        "info": {
          "input": {
            "type": "http",
            "method": "POST",
            "bodyType": "json",
            "body": { ... example request body ... }
          },
          "output": {
            "type": "json",
            "example": { ... example response body ... }
          }
        },
        "schema": {
          "$schema": "https://json-schema.org/draft/2020-12/schema",
          "type": "object",
          "properties": {
            "input": {
              "type": "object",
              "properties": {
                "type":        {"type": "string", "const": "http"},
                "method":      {"type": "string", "enum": ["POST", "PUT", "PATCH"]},
                "bodyType":    {"type": "string", "enum": ["json", "form-data", "text"]},
                "body":        { ... JSON Schema for the request body ... }
              },
              "required": ["type", "method", "bodyType", "body"],
              "additionalProperties": false
            },
            "output": {
              "type": "object",
              "properties": {
                "type": {"type": "string"},
                "example": {...}
              },
              "required": ["type"]
            }
          },
          "required": ["input"]
        }
      }
    }

The natural-language ``description`` that tells an agent *when* to call the route
is NOT part of the bazaar extension block — it lives on the ``resource`` object
of the PaymentRequired envelope (``resource.description``). The CDP Facilitator
REJECTS verify/settle whose description exceeds 500 characters, so keep it
short (see the per-route descriptions below).
"""

from __future__ import annotations

from typing import Any

# Extension key under which the declaration lives in the 402 envelope's
# ``extensions`` object. The facilitator keys its ``EXTENSION-RESPONSES`` by
# this name and reports ``bazaar.status`` (success|processing|rejected).
BAZAAR_EXTENSION_KEY = "bazaar"

# HTTP methods whose discovery declaration uses a request *body* (rather than
# query parameters).
_BODY_METHODS = ("POST", "PUT", "PATCH")


def _build_body_discovery_extension(
    *,
    method: str,
    body_example: dict[str, Any],
    body_schema: dict[str, Any],
    output_example: dict[str, Any],
) -> dict[str, Any]:
    """Build the ``info`` + ``schema`` for a body-method (POST/PUT/PATCH) route."""
    method = method.upper()
    if method not in _BODY_METHODS:
        raise ValueError(f"method {method!r} is not a body method")

    info = {
        "input": {
            "type": "http",
            "method": method,
            "bodyType": "json",
            "body": body_example,
        },
        "output": {
            "type": "json",
            "example": output_example,
        },
    }

    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "input": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "const": "http"},
                    "method": {"type": "string", "enum": list(_BODY_METHODS)},
                    "bodyType": {"type": "string", "enum": ["json", "form-data", "text"]},
                    "body": body_schema,
                },
                "required": ["type", "method", "bodyType", "body"],
                "additionalProperties": False,
            },
            "output": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "example": {"type": "object"},
                },
                "required": ["type"],
            },
        },
        "required": ["input"],
    }

    return {"info": info, "schema": schema}


def declare_discovery_extension(
    *,
    method: str,
    body_schema: dict[str, Any],
    body_example: dict[str, Any],
    output_example: dict[str, Any],
) -> dict[str, Any]:
    """Declare a Bazaar discovery extension for a POST route.

    Args:
        method: HTTP method (POST for both scrape and extract).
        body_schema: JSON Schema for the request body (must match the actual
            Pydantic request model so the CDP Facilitator's strict JSON Schema
            validation of ``schema.properties.input`` passes).
        body_example: A realistic example request body.
        output_example: A realistic example response body.

    Returns:
        A dict ``{"bazaar": {info, schema}}`` to place in the 402 envelope's
        ``extensions`` field. Keyed by ``BAZAAR_EXTENSION_KEY``.
    """
    declaration = _build_body_discovery_extension(
        method=method,
        body_example=body_example,
        body_schema=body_schema,
        output_example=output_example,
    )
    return {BAZAAR_EXTENSION_KEY: declaration}


# ---------------------------------------------------------------------------
# Concrete route metadata (POST /v1/scrape and POST /v1/extract)
#
# The input JSON Schemas mirror the actual Pydantic request models
# (server.ScrapeRequest / routes_extract.ExtractRequest) so the CDP
# Facilitator's strict JSON Schema validation passes. Descriptions are
# natural-language "when to call" guidance, each kept <= 500 chars.
# ---------------------------------------------------------------------------

REQUEST_BODY_SCHEMAS = {
    "/v1/scrape": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "format": "uri", "description": "Target URL to scrape"},
            "output": {"type": "string", "enum": ["markdown", "screenshot", "pdf", "csv", "html"], "description": "Output format (default: markdown)"},
            "options": {"type": "object", "description": "Output-specific options"},
            "wait_for_selector": {"type": "string", "description": "CSS selector to wait for"},
            "timeout_ms": {"type": "integer", "description": "Navigation timeout (ms)"},
            "block_media": {"type": "boolean", "description": "Block image/font/video requests"},
            "proxy_url": {"type": "string", "description": "Proxy URL (socks5:// or http://)"},
            "wait_strategy": {"type": "string", "description": "Wait strategy: default, spa, heavy, cloudflare"},
            "retry": {"type": "boolean"},
            "bypass_cache": {"type": "boolean"},
            "javascript": {"type": "string", "description": "Custom JS after page load"},
        },
        "required": ["url"],
    },
    "/v1/extract": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "format": "uri", "description": "Target URL to extract data from"},
            "schema": {"type": "object", "description": "JSON schema defining fields to extract"},
            "selectors": {"type": "object", "description": "CSS selectors to extract"},
            "xpaths": {"type": "object", "description": "XPath queries to extract"},
            "options": {
                "type": "object",
                "properties": {
                    "format": {"type": "string", "enum": ["json"]},
                    "wait_for": {"type": "string"},
                    "timeout": {"type": "integer"},
                    "block_media": {"type": "boolean"},
                    "wait_strategy": {"type": "string"},
                    "javascript": {"type": "string"},
                    "proxy_url": {"type": "string"},
                    "retry": {"type": "boolean"},
                    "bypass_cache": {"type": "boolean"},
                },
            },
        },
        "required": ["url"],
    },
}

REQUEST_EXAMPLES = {
    "/v1/scrape": {
        "url": "https://news.ycombinator.com",
        "output": "markdown",
    },
    "/v1/extract": {
        "url": "https://example.com/product/123",
        "schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "price": {"type": "string"},
                "availability": {"type": "string"},
            },
            "required": ["title"],
        },
    },
}

OUTPUT_EXAMPLES = {
    "/v1/scrape": {
        "status": 200,
        "url": "https://news.ycombinator.com",
        "output": "markdown",
        "data": {
            "title": "Hacker News",
            "markdown": "# Hacker News\n\n1. Story one\n2. Story two\n",
            "character_count": 63,
        },
        "execution_time_ms": 410,
    },
    "/v1/extract": {
        "status": 200,
        "url": "https://example.com/product/123",
        "data": {
            "title": "Acme Widget",
            "price": "$12.99",
            "availability": "In stock",
        },
        "metadata": {
            "extraction_method": "schema",
            "confidence": 0.97,
            "processing_time_ms": 380,
        },
    },
}

# <= 500 characters (hard limit enforced by the CDP Facilitator).
ROUTE_DESCRIPTIONS = {
    "/v1/scrape": (
        "Scrape a web page and return clean Markdown (or another output format) "
        "suitable for LLM/agent consumption. Call this when you need the readable "
        "text content of a URL, bypassing bot protection with a stealth browser. "
        "Priced per scrape; payment is verified before the page is fetched."
    ),
    "/v1/extract": (
        "Extract structured data from a web page using a JSON schema, CSS "
        "selectors, or XPath queries. Call this when you need specific fields "
        "(title, price, etc.) parsed out of a URL as structured JSON rather than "
        "raw Markdown. Priced per extraction; payment is verified before fetching."
    ),
}


def declare_discovery_extension_for_route(route: str) -> dict[str, Any]:
    """Convenience: build the ``{bazaar: ...}`` extension for a known route.

    Raises ``KeyError`` for an unknown route. Routes are keyed by their public
    path (``/v1/scrape``, ``/v1/extract``).
    """
    return declare_discovery_extension(
        method="POST",
        body_schema=REQUEST_BODY_SCHEMAS[route],
        body_example=REQUEST_EXAMPLES[route],
        output_example=OUTPUT_EXAMPLES[route],
    )
