"""
Shared authentication dependencies — avoids circular imports.
"""
from __future__ import annotations

import base64
import json
import os
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

security = HTTPBearer(auto_error=False)


class AuthResult:
    """Authentication result — carries auth method + optional x402 context."""
    __slots__ = ('method', 'x402_context')

    def __init__(self, method: str, x402_context: Optional[dict] = None):
        self.method = method
        self.x402_context = x402_context

    @property
    def is_x402(self) -> bool:
        return self.method == "x402"


# ---------------------------------------------------------------------------
# x402 payment configuration
#
# Replicated here (rather than imported from server.py) to avoid a circular
# import — server.py imports this module. Keep in sync with the x402 config
# block at the top of server.py.
# ---------------------------------------------------------------------------
X402_ENABLED = os.getenv("X402_ENABLED", "false").lower() == "true"
X402_RECIPIENT_WALLET = os.getenv("X402_RECIPIENT_WALLET", "")
X402_PRICE_USD = os.getenv("X402_PRICE_USD", "0.005")
X402_NETWORK = os.getenv("X402_NETWORK", "base")  # Base Mainnet (override to base-sepolia for testnet dry-run)
X402_USDC_BASE = os.getenv(
    "X402_ASSET", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
)  # Base Mainnet USDC default; override to Base Sepolia USDC for testnet
X402_USDC_BASE_SEPOLIA = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
X402_RESOURCE_URL = os.getenv(
    "X402_RESOURCE_URL", "https://api.tazpal.com/v1/scrape"
)
X402_DESCRIPTION = os.getenv("X402_DESCRIPTION", "X1-BaaS scraping API")
X402_VERSION = 2
X402_SERVICE_NAME = os.getenv("X402_SERVICE_NAME", "X1-BaaS Scraping API")
# EIP-712 domain parameters for the exact/EV M payment scheme. Standard x402
# EVM clients (e.g. @x402/evm) THROW unless extra.name and extra.version are
# present: they build the signing domain as {name, version, chainId,
# verifyingContract: asset}. KEEP THESE STABLE — changing them changes the
# signing domain for every client.
X402_EIP712_NAME = os.getenv("X402_EIP712_NAME", "USD Coin")
X402_EIP712_VERSION = os.getenv("X402_EIP712_VERSION", "2")
X402_MAX_TIMEOUT_SECONDS = 60


def _x402_max_amount_required() -> str:
    """Convert the USD price into USDC atomic units (6 decimals)."""
    try:
        return str(int(float(X402_PRICE_USD) * 1_000_000))
    except (TypeError, ValueError):
        return "0"


def build_x402_payment_requirements(resource: Optional[str] = None) -> dict:
    """Build the x402 v2 PaymentRequired envelope for a 402 response.

    Matches the x402 Foundation v2 spec (typescript ``@x402/core``):
    - ``x402Version: 2`` — current protocol version
    - ``resource`` at the top level (moved out of each accept entry in v2)
    - ``accepts[].amount`` in atomic units (v2 renamed v1's maxAmountRequired)
    - ``accepts[].extra`` carries the EIP-712 domain parameters (name, version)
      that standard EVM clients require to sign a payment — without them a
      standard x402 client cannot pay this route.
    - ``extensions.bazaar`` (CDP x402 Bazaar discovery) is advertised so the CDP
      Facilitator indexes the route after a settled payment. Discovery is
      OPT-IN: only routes that declare Bazaar metadata are indexed (see
      ``build_x402_payment_requirements_for_route``).

    This default builder advertises the primary scrape route WITHOUT a bazaar
    extension (back-compat with the D-009/D-010 envelope used by x402-list.com).
    Call ``build_x402_payment_requirements_for_route`` for a Bazaar-listed route.
    """
    requirements = {
        "x402Version": X402_VERSION,
        "resource": {
            "url": resource or X402_RESOURCE_URL,
            "description": X402_DESCRIPTION,
            "mimeType": "application/json",
            "serviceName": X402_SERVICE_NAME,
        },
        "accepts": [
            {
                "scheme": "exact",
                "network": X402_NETWORK,
                "asset": X402_USDC_BASE,
                "amount": _x402_max_amount_required(),
                "payTo": X402_RECIPIENT_WALLET,
                "maxTimeoutSeconds": X402_MAX_TIMEOUT_SECONDS,
                "extra": {
                    "name": X402_EIP712_NAME,
                    "version": X402_EIP712_VERSION,
                },
            }
        ],
        "error": "Payment required — no valid API key or X-PAYMENT header",
    }
    return requirements


def build_x402_payment_requirements_for_route(
    route: str,
    *,
    resource_url: str,
    description: str,
    bazaar: Optional[dict] = None,
) -> dict:
    """Build a Bazaar-discoverable x402 v2 envelope for a specific route.

    Args:
        route: Route tag for logging/reporting (e.g. "/v1/scrape").
        resource_url: Public URL of the route (the ``resource.url``).
        description: Natural-language description of WHEN to call the route.
            MUST be <= 500 chars — the CDP Facilitator rejects verify/settle
            when the description exceeds that limit.
        bazaar: The ``{"bazaar": {...}}`` extension (from
            ``x402_bazaar.declare_discovery_extension``). When None, the route
            is NOT advertised for Bazaar indexing.
    """
    if len(description) > 500:
        raise ValueError(
            f"bazaar description for {route} exceeds 500 chars ({len(description)})"
        )

    requirements = build_x402_payment_requirements(resource=resource_url)
    requirements["resource"]["description"] = description
    if bazaar:
        requirements["extensions"] = bazaar
    return requirements


# Bazaar-listed routes (OPT-IN discovery). Keyed by the FastAPI path.
#
# /v1/extract is deliberately NOT listed: its auth path is API-key-only
# (``get_current_user_from_api_key`` — no x402 verify/settle), so advertising
# it in the Bazaar would let an agent pay and then hit a 401. Only routes that
# can actually accept x402 payments are advertised. (See D-011 follow-up: add
# x402 support to /v1/extract, then re-list.)
_BAZAAR_ROUTES: set[str] = {"/v1/scrape"}


def build_x402_payment_requirements_for_path(path: str) -> dict:
    """Build the envelope for a request path, advertising the Bazaar extension
    when the path is a registered Bazaar route (else a plain envelope).

    Route URLs are derived from the ``X402_RESOURCE_URL`` host (scheme+host
    preserved, path replaced). Bazaar metadata is resolved lazily from
    ``x402_bazaar``.
    """
    path = path or ""
    if path not in _BAZAAR_ROUTES:
        return build_x402_payment_requirements(resource=X402_RESOURCE_URL)

    from x402_bazaar import (
        ROUTE_DESCRIPTIONS,
        declare_discovery_extension_for_route,
    )

    description = ROUTE_DESCRIPTIONS[path]
    base = os.getenv("X402_RESOURCE_URL", "https://api.tazpal.com/v1/scrape")
    resource_url = base
    try:
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(base)
        resource_url = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    except Exception:
        resource_url = base

    return build_x402_payment_requirements_for_route(
        path,
        resource_url=resource_url,
        description=description,
        bazaar=declare_discovery_extension_for_route(path),
    )


class X402PaymentRequired(Exception):
    """Raised when a resource requires x402 payment (HTTP 402)."""

    def __init__(self, requirements: Optional[dict] = None, path: Optional[str] = None):
        if requirements is None:
            requirements = build_x402_payment_requirements_for_path(path or "")
        self.requirements = requirements
        super().__init__("x402 payment required")


def encode_payment_required_header(requirements: dict) -> str:
    """Base64-encode a PaymentRequired envelope for the PAYMENT-REQUIRED header.

    x402 v2 spec: the 402 response carries the envelope as a base64 object in
    the ``PAYMENT-REQUIRED`` response header (standard base64 with padding).
    """
    return base64.b64encode(
        json.dumps(requirements, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


def x402_http_exception_handler(request: Request, exc: X402PaymentRequired):
    """FastAPI exception handler — 402 with the x402 v2 envelope.

    Delivers the PaymentRequired envelope BOTH in the body (JSON) and in the
    ``PAYMENT-REQUIRED`` response header (base64, per the x402 v2 spec) so that
    standard x402 clients and discovery services (e.g. x402-list.com) can parse
    the payment requirements from either channel.
    """
    payload = exc.requirements
    return JSONResponse(
        status_code=402,
        content=payload,
        headers={
            "Content-Type": "application/json",
            "PAYMENT-REQUIRED": encode_payment_required_header(payload),
            "Cache-Control": "no-store",
        },
    )


async def require_auth(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> AuthResult:
    """
    Require authentication — API key or x402 payment.
    This is a lightweight version for route-level dependencies.
    Full authentication logic remains in server.py.
    """
    if not credentials:
        # x402 discovery services (e.g. x402-list.com) probe endpoints for a
        # 402 (Payment Required) response so they can discover the payment
        # requirements. Return 402 when x402 is enabled.
        if X402_ENABLED:
            raise X402PaymentRequired(path=request.url.path)
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "message": "Invalid API key or missing X-PAYMENT header"}
        )

    # For API key auth, we just check that a credential exists
    # Full validation happens in the endpoint
    return AuthResult(method="api_key")
