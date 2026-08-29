"""
x402 v2 payment verification (PAYMENT-SIGNATURE acceptance).

Hand-rolled, minimal v2 models + EIP-712 (EIP-3009 "exact" scheme) signature
verification + facilitator ``/verify`` integration.

This module intentionally does NOT use the legacy Python ``x402`` v0.3.0 package
for v2 parsing — that package models the v1 wire shape (``x402_version`` /
``scheme`` / ``network`` / ``payload.signature``+``payload.authorization`` at the
top level). Standard v2 clients send a different shape:

    PAYMENT-SIGNATURE: base64(JSON.stringify({
        x402Version: 2,
        resource?:        ResourceInfo,
        accepted:         PaymentRequirements,   # the single offer the client chose
        payload: { ... },                        # scheme-specific (EIP-3009 auth + sig)
        extensions?:      Record<string, unknown>,
    }))

Reference (x402 Foundation, typescript):
  - packages/core/src/types/payments.ts     -> PaymentPayload / PaymentRequirements
  - packages/core/src/http/index.ts         -> header names + base64 encoding
  - packages/mechanisms/evm/src/exact/client/eip3009.ts -> EIP-712 domain + types
  - packages/mechanisms/evm/src/constants.ts -> authorizationTypes (EIP-3009)

The EIP-712 signing domain is derived by the client as::

    { name, version, chainId(from network), verifyingContract: asset }

where ``name``/``version`` come from the requirement's ``extra`` and ``chainId``
is the EVM chain id for the network (8453 for Base). We reconstruct the exact
same domain and recover the signer from ``payload.signature`` over the EIP-3009
``TransferWithAuthorization`` typed data.
"""

from __future__ import annotations

import base64
import json as _json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger("baas-engine.x402")

# ---------------------------------------------------------------------------
# Network -> EVM chain id (CAIP-2 "eip155:<id>" or legacy alias like "base")
# ---------------------------------------------------------------------------
# v2 clients send ``network`` as a CAIP-2 chain id (e.g. "eip155:8453"); our
# v2 envelope (D-009) advertises the legacy alias "base" for backwards
# compatibility with v1 clients. Accept both when resolving the chain id.
_CHAIN_ID_ALIASES = {
    "base": 8453,
    "8453": 8453,
    "eip155:8453": 8453,
    "base-sepolia": 84532,
    "eip155:84532": 84532,
    "ethereum": 1,
    "eip155:1": 1,
    "sepolia": 11155111,
    "eip155:11155111": 11155111,
}


def resolve_chain_id(network: str) -> int:
    """Resolve a network identifier to an EVM chain id.

    Accepts CAIP-2 (``eip155:8453``), a bare decimal string, or a known legacy
    alias (``base``). Raises ``ValueError`` on unknown networks.
    """
    if not isinstance(network, str) or not network.strip():
        raise ValueError("network is empty")
    key = network.strip().lower()
    if key in _CHAIN_ID_ALIASES:
        return _CHAIN_ID_ALIASES[key]
    if key.startswith("eip155:"):
        raw = key.split(":", 1)[1]
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError(f"invalid CAIP-2 chain id: {network}") from exc
    # Bare decimal chain id
    try:
        return int(key)
    except ValueError as exc:
        raise ValueError(f"unsupported network: {network}") from exc


# ---------------------------------------------------------------------------
# v2 pydantic models
# ---------------------------------------------------------------------------

class ResourceInfo(BaseModel):
    url: str
    description: Optional[str] = None
    mimeType: Optional[str] = None
    serviceName: Optional[str] = None
    tags: Optional[list[str]] = None
    iconUrl: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class PaymentRequirementsModel(BaseModel):
    """The ``accepts`` entry we advertise / the ``accepted`` entry a client echoes."""

    scheme: str
    network: str
    asset: str
    amount: str
    payTo: str
    maxTimeoutSeconds: int
    extra: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="allow")


class PaymentPayloadV2(BaseModel):
    """The v2 ``PAYMENT-SIGNATURE`` payload body (after base64 + JSON decode)."""

    x402Version: int = Field(alias="x402Version")
    resource: Optional[ResourceInfo] = None
    accepted: PaymentRequirementsModel
    payload: dict[str, Any]
    extensions: Optional[dict[str, Any]] = None

    model_config = ConfigDict(populate_by_name=True, extra="allow")


# EIP-712 / EIP-3009 typed-data constants (per x402 mechanisms/evm constants.ts)
_TRANSFER_WITH_AUTHORIZATION_TYPES = {
    "TransferWithAuthorization": [
        {"name": "from", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"},
        {"name": "nonce", "type": "bytes32"},
    ],
}


# ---------------------------------------------------------------------------
# Parsing / decoding
# ---------------------------------------------------------------------------

class X402VerifyError(Exception):
    """Raised when a payment cannot be verified (clearly distinguishable from a
    runtime bug so callers can map it to a client-facing 401/402, never a 500)."""


def decode_payment_signature_header(header_value: str) -> PaymentPayloadV2:
    """Decode a base64 ``PAYMENT-SIGNATURE`` header into a v2 PaymentPayload.

    Raises ``X402VerifyError`` on malformed base64 / JSON / model mismatch.
    """
    if not header_value:
        raise X402VerifyError("empty PAYMENT-SIGNATURE header")
    try:
        raw = base64.b64decode(header_value, validate=False)
    except Exception as exc:
        raise X402VerifyError("PAYMENT-SIGNATURE is not valid base64") from exc
    try:
        data = _json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, _json.JSONDecodeError) as exc:
        raise X402VerifyError("PAYMENT-SIGNATURE is not valid JSON") from exc
    if not isinstance(data, dict):
        raise X402VerifyError("PAYMENT-SIGNATURE must decode to a JSON object")
    try:
        return PaymentPayloadV2.model_validate(data)
    except ValidationError as exc:
        # Don't leak full pydantic internals; keep a concise hint.
        raise X402VerifyError("malformed v2 payment payload") from exc


# ---------------------------------------------------------------------------
# accepted-vs-advertised matching
# ---------------------------------------------------------------------------

def _norm_addr(value: str) -> str:
    return value.strip().lower()


def requirements_match(accepted: PaymentRequirementsModel,
                       advertised: dict[str, Any]) -> bool:
    """Check that a client's ``accepted`` entry exactly matches the offer we made.

    Every field that matters for a valid payment must match. We compare against
    the ``accepts`` entry we advertise (the object from
    ``build_x402_payment_requirements()``). Asset + payTo are compared
    case-insensitively (checksummed addresses); network is resolved to a chain
    id so "base" == "eip155:8453".
    """
    try:
        if accepted.scheme != advertised.get("scheme"):
            return False
        if resolve_chain_id(accepted.network) != resolve_chain_id(str(advertised.get("network", ""))):
            return False
        if _norm_addr(accepted.asset) != _norm_addr(str(advertised.get("asset", ""))):
            return False
        if accepted.amount != str(advertised.get("amount", "")):
            return False
        if _norm_addr(accepted.payTo) != _norm_addr(str(advertised.get("payTo", ""))):
            return False
        if accepted.maxTimeoutSeconds != int(advertised.get("maxTimeoutSeconds", 0)):
            return False
        # EIP-712 domain parameters must match so the signature domain is equal.
        adv_extra = advertised.get("extra") or {}
        acc_extra = accepted.extra or {}
        if str(acc_extra.get("name", "")) != str(adv_extra.get("name", "")):
            return False
        if str(acc_extra.get("version", "")) != str(adv_extra.get("version", "")):
            return False
        return True
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# EIP-3009 / EIP-712 signature verification
# ---------------------------------------------------------------------------

def _coerce_uint256(value: Any) -> int:
    if isinstance(value, bool):
        raise TypeError("bool is not a uint256")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        try:
            result = int(value, 0) if value.lower().startswith(("0x",)) else int(value, 10)
        except ValueError:
            raise TypeError("not an integer") from None
    else:
        raise TypeError("not a uint256")
    if result < 0 or result > 2**256 - 1:
        raise TypeError("uint256 out of range")
    return result


def _coerce_bytes32(value: Any) -> bytes:
    """Normalize a bytes32 nonce to its raw 32 bytes.

    Accepts a 0x-prefixed hex string (up to 64 hex chars) or raw bytes. Does NOT
    accept a bare decimal/ASCII string — EIP-3009 nonces are 32-byte values and
    must be encoded as such to recover the correct signer.
    """
    if isinstance(value, bytes):
        if len(value) > 32:
            raise TypeError("nonce too long")
        return value.rjust(32, b"\x00")
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("0x") or s.startswith("0X"):
            hexpart = s[2:]
        else:
            # Support raw (unprefixed) hex, as the legacy Python SDK signed with
            # bytes.fromhex. Reject odd-length or non-hex input.
            hexpart = s
        if len(hexpart) == 0:
            raise TypeError("empty nonce")
        if len(hexpart) % 2 != 0:
            raise TypeError("nonce hex has odd length")
        try:
            raw = bytes.fromhex(hexpart)
        except ValueError:
            raise TypeError("nonce is not valid hex") from None
        if len(raw) > 32:
            raise TypeError("nonce too long")
        return raw.rjust(32, b"\x00")
    raise TypeError("nonce must be bytes or hex string")


def _build_typed_data(requirements: dict[str, Any],
                      chain_id: int,
                      authorization: dict[str, Any],
                      nonce_bytes: bytes) -> dict[str, Any]:
    extra = requirements.get("extra") or {}
    domain = {
        "name": extra.get("name", ""),
        "version": extra.get("version", ""),
        "chainId": chain_id,
        "verifyingContract": requirements.get("asset", ""),
    }
    message = {
        "from": authorization.get("from", ""),
        "to": authorization.get("to", ""),
        "value": _coerce_uint256(authorization.get("value")),
        "validAfter": _coerce_uint256(authorization.get("validAfter")),
        "validBefore": _coerce_uint256(authorization.get("validBefore")),
        "nonce": nonce_bytes,
    }
    return {
        "types": _TRANSFER_WITH_AUTHORIZATION_TYPES,
        "primaryType": "TransferWithAuthorization",
        "domain": domain,
        "message": message,
    }


def recover_signer(requirements: dict[str, Any], payload: dict[str, Any]) -> str:
    """Recover the EIP-712 signer address for an EIP-3009 "exact" payload.

    Returns the checksummed signer address, or raises ``X402VerifyError`` if the
    payload is malformed / the signature is invalid.
    """
    signature = payload.get("signature")
    authorization = payload.get("authorization")
    if not isinstance(signature, str) or not isinstance(authorization, dict):
        raise X402VerifyError("exact payload missing signature/authorization")

    try:
        nonce_bytes = _coerce_bytes32(authorization.get("nonce"))
    except TypeError as exc:
        raise X402VerifyError(f"invalid authorization nonce: {exc}") from exc

    try:
        chain_id = resolve_chain_id(str(requirements.get("network", "")))
    except ValueError as exc:
        raise X402VerifyError(f"unsupported network: {exc}") from exc

    try:
        typed_data = _build_typed_data(requirements, chain_id, authorization, nonce_bytes)
    except TypeError as exc:
        raise X402VerifyError(f"invalid authorization value: {exc}") from exc

    from eth_account import Account
    from eth_account.messages import encode_typed_data

    try:
        signable = encode_typed_data(full_message=typed_data)
    except Exception as exc:
        raise X402VerifyError(f"could not encode typed data: {exc}") from exc

    try:
        recovered = Account.recover_message(signable, signature=signature)
    except Exception as exc:
        raise X402VerifyError(f"signature verification failed: {exc}") from exc

    return recovered


# ---------------------------------------------------------------------------
# Time-window / replay helpers
# ---------------------------------------------------------------------------

def is_expired(requirements: dict[str, Any], authorization: dict[str, Any],
               now: Optional[float] = None) -> bool:
    """Check EIP-3009 validity window (`validAfter` <= now <= `validBefore`).

    Also enforces our advertised ``maxTimeoutSeconds``: a payment whose
    authorization window extends beyond ``now + maxTimeoutSeconds`` is rejected.
    """
    try:
        valid_after = _coerce_uint256(authorization.get("validAfter"))
        valid_before = _coerce_uint256(authorization.get("validBefore"))
    except TypeError:
        return True  # treat unparseable as expired

    now_ts = int(now if now is not None else time.time())
    if now_ts < valid_after:
        return True
    if now_ts > valid_before:
        return True

    # Enforce our advertised maxTimeoutSeconds: a payment whose authorization
    # extends further into the future than we're willing to wait is rejected.
    # (Do NOT also bound `validBefore - validAfter`, because standard x402/evm
    # clients sign validAfter=0 as the convention.)
    max_timeout = int(requirements.get("maxTimeoutSeconds", 0) or 0)
    if max_timeout > 0 and valid_before > now_ts + max_timeout:
        return True
    return False


def extract_nonce(payload: dict[str, Any]) -> str:
    """Best-effort nonce extraction for replay protection.

    Return a stable string key for the double-spend cache (uses the raw bytes32
    nonce when available). Returns ``""`` when no nonce can be found.
    """
    auth = payload.get("authorization")
    if isinstance(auth, dict) and auth.get("nonce") is not None:
        try:
            raw = _coerce_bytes32(auth.get("nonce"))
            return raw.hex()
        except TypeError:
            pass
        n = auth.get("nonce")
        if isinstance(n, str):
            return n
    # fall back to any top-level nonce field
    n = payload.get("nonce")
    if isinstance(n, str):
        return n
    return ""


# ---------------------------------------------------------------------------
# High-level verification entry point
# ---------------------------------------------------------------------------

@dataclass
class VerificationResult:
    """Result of a successful local (pre-facilitator) v2 payment check."""

    payer: str
    nonce: str
    payload: PaymentPayloadV2
    advertised: dict[str, Any]


def verify_v2_payment(header_value: str, advertised: dict[str, Any]) -> VerificationResult:
    """Fully parse + locally validate a v2 ``PAYMENT-SIGNATURE`` header.

    Performs, in order:
      1. base64 -> JSON -> pydantic v2 model
      2. ``accepted`` entry matches the ``accepts`` entry we advertise
      3. EIP-3009 authorization is not expired (validAfter/validBefore window,
         bounded by our advertised ``maxTimeoutSeconds``)
      4. EIP-712 signature recovers a valid signer matching ``payload.authorization.from``

    Does NOT contact the facilitator (caller does that after this passes).
    Raises ``X402VerifyError`` on any failure.
    """
    payload = decode_payment_signature_header(header_value)

    if payload.x402Version != 2:
        raise X402VerifyError(f"unsupported x402Version: {payload.x402Version}")

    if not requirements_match(payload.accepted, advertised):
        raise X402VerifyError("payment does not match advertised requirements")

    authorization = payload.payload.get("authorization")
    if not isinstance(authorization, dict):
        raise X402VerifyError("exact payload missing authorization")

    if is_expired(advertised, authorization):
        raise X402VerifyError("payment authorization is expired or out of window")

    signer = recover_signer(advertised, payload.payload)

    auth_from = authorization.get("from", "")
    if _norm_addr(str(signer)) != _norm_addr(str(auth_from)):
        raise X402VerifyError(
            "signature signer does not match authorization.from"
        )

    # F-1 (CRITICAL): the EIP-3009 authorization.value is the *actual* amount
    # transferred on-chain. It must exactly equal the advertised amount, else a
    # client could sign a 1-atomic-unit transfer while `accepted.amount` claims
    # the advertised price (underpayment). Compare as uint256 so "5000" and
    # "0x1388" are treated equal.
    try:
        auth_value = _coerce_uint256(authorization.get("value"))
        adv_amount = _coerce_uint256(advertised.get("amount"))
    except TypeError as exc:
        raise X402VerifyError(f"invalid amount: {exc}") from exc
    if auth_value != adv_amount:
        raise X402VerifyError(
            "authorization value does not match advertised amount"
        )

    # F-2 (CRITICAL): the EIP-3009 authorization.to is the actual transfer
    # recipient. It must equal the advertised payTo, else a client could sign a
    # transfer to its own (attacker) address while `accepted.payTo` claims the
    # server wallet (misdirected payment).
    if _norm_addr(str(authorization.get("to", ""))) != _norm_addr(str(advertised.get("payTo", ""))):
        raise X402VerifyError(
            "authorization recipient does not match advertised payTo"
        )

    nonce = extract_nonce(payload.payload)
    return VerificationResult(
        payer=signer,
        nonce=nonce,
        payload=payload,
        advertised=advertised,
    )


# ---------------------------------------------------------------------------
# Facilitator /verify integration
# ---------------------------------------------------------------------------

# CDP x402 Facilitator base URL + route (per the CDP SDK's ``cdp.x402`` module).
CDP_FACILITATOR_BASE_URL = "https://api.cdp.coinbase.com"
CDP_FACILITATOR_V2_ROUTE = "/platform/v2/x402"
CDP_FACILITATOR_URL = CDP_FACILITATOR_BASE_URL + CDP_FACILITATOR_V2_ROUTE
CDP_X402_VERSION = "2.0.0"


def is_cdp_facilitator(facilitator_url: str) -> bool:
    """True when ``facilitator_url`` points at the CDP Facilitator.

    Matches either the bare base (``https://api.cdp.coinbase.com``) or the full
    x402 route (``.../platform/v2/x402``), with optional trailing slash.
    """
    url = facilitator_url.rstrip("/").lower()
    return (
        url == CDP_FACILITATOR_BASE_URL.lower()
        or url == CDP_FACILITATOR_URL.lower()
        or url.startswith(CDP_FACILITATOR_BASE_URL.lower() + "/")
    )


def _cdp_jwt(api_key_id: str, api_key_secret: str, method: str, path: str) -> str:
    """Generate a CDP-authenticated JWT (Bearer token) for a facilitator request.

    Replicates the CDP SDK's ``cdp.auth.utils.jwt.generate_jwt`` scheme WITHOUT
    depending on the ``cdp-sdk`` package:

    - header: ``{alg, kid: api_key_id, typ: "JWT", nonce: <16 random digits>}``
    - claims: ``{sub: api_key_id, iss: "cdp", aud: None, nbf: now, exp: now+120,
      uris: ["<METHOD> <host><path>"]}``
    - signed ES256 (PEM EC key) or EdDSA (base64 Ed25519 key), auto-detected.

    Raises ``X402VerifyError`` if the CDP key secret cannot be parsed/signed
    (fail closed — the caller treats it as an auth/verify error, never valid).
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519

    try:
        import jwt as pyjwt
    except ImportError as exc:
        raise X402VerifyError("PyJWT is required for CDP facilitator auth") from exc

    # Normalize literal \n that may leak through an unquoted env var.
    key_data = api_key_secret
    if "\\n" in key_data:
        key_data = key_data.replace("\\n", "\n")

    private_key = None
    algorithm = None
    # PEM EC key
    try:
        key = serialization.load_pem_private_key(key_data.encode(), password=None)
        if isinstance(key, ec.EllipticCurvePrivateKey):
            private_key = key
            algorithm = "ES256"
    except Exception:
        pass
    # base64 Ed25519 key (64 bytes: 32 seed + 32 public)
    if private_key is None:
        try:
            import base64 as _b64
            decoded = _b64.b64decode(key_data)
            if len(decoded) == 64:
                private_key = ed25519.Ed25519PrivateKey.from_private_bytes(decoded[:32])
                algorithm = "EdDSA"
        except Exception:
            pass
    if private_key is None:
        raise X402VerifyError(
            "CDP_API_KEY_SECRET must be a PEM EC key or base64 Ed25519 key"
        )

    import secrets
    now = int(time.time())
    header = {
        "alg": algorithm,
        "kid": api_key_id,
        "typ": "JWT",
        # CSPRNG nonce — ``random`` is predictable and must not be used here
        # (Security review L-1).
        "nonce": "".join(secrets.choice("0123456789") for _ in range(16)),
    }
    # The CDP SDK builds ``uri = f"{method} {parsed_url.netloc}{parsed_url.path}"``
    # from ``request_host`` + ``request_path``; request_host has no scheme, so
    # netloc == host.
    host = CDP_FACILITATOR_BASE_URL.replace("https://", "")
    claims = {
        "sub": api_key_id,
        "iss": "cdp",
        "aud": None,
        "nbf": now,
        "exp": now + 120,
        "uris": [f"{method} {host}{path}"],
    }
    try:
        return pyjwt.encode(claims, private_key, algorithm=algorithm, headers=header)
    except Exception as exc:
        raise X402VerifyError(f"failed to generate CDP JWT: {exc}") from exc


def build_facilitator_headers(
    facilitator_url: str,
    operation: str = "verify",
) -> dict[str, str]:
    """Build request headers for a facilitator call.

    - x402.org (default): no auth — ``{Content-Type: application/json}`` only.
    - CDP Facilitator: add ``Authorization: Bearer <JWT>`` signed with
      ``CDP_API_KEY_ID`` / ``CDP_API_KEY_SECRET`` (env-driven), plus a
      ``Correlation-Context`` header.

    The CDP key is read from the environment here (never hardcoded). When the
    URL points at CDP but the key is unset, calls raise ``X402VerifyError`` so
    the caller fails closed (an unpaid/unverified request is never served).
    """
    import os

    headers = {"Content-Type": "application/json"}
    if not is_cdp_facilitator(facilitator_url):
        return headers

    api_key_id = os.getenv("CDP_API_KEY_ID", "")
    api_key_secret = os.getenv("CDP_API_KEY_SECRET", "")
    if not api_key_id or not api_key_secret:
        raise X402VerifyError(
            "CDP facilitator requires CDP_API_KEY_ID and CDP_API_KEY_SECRET"
        )

    path = f"{CDP_FACILITATOR_V2_ROUTE}/{operation}"
    token = _cdp_jwt(api_key_id, api_key_secret, "POST", path)
    headers["Authorization"] = f"Bearer {token}"
    headers["Correlation-Context"] = (
        "sdk_language=python,source=x402,source_version=" + CDP_X402_VERSION
    )
    return headers

@dataclass
class FacilitatorVerifyResponse:
    """Normalized facilitator /verify response."""

    is_valid: bool
    payer: Optional[str] = None
    invalid_reason: Optional[str] = None
    invalid_message: Optional[str] = None


def _caip2_normalize(body: dict) -> dict:
    """Ensure network fields use CAIP-2 format (e.g. 'eip155:8453') not aliases.

    The CDP Facilitator validates network as CAIP-2; legacy aliases like 'base'
    fail schema validation.
    """
    for key in ("paymentPayload", "paymentRequirements"):
        section = body.get(key)
        if not isinstance(section, dict):
            continue
        # paymentPayload.accepted is nested; paymentRequirements is flat
        target = section.get("accepted", section) if key == "paymentPayload" else section
        raw = str(target.get("network", ""))
        cid = resolve_chain_id(raw)
        if cid and ":" not in raw:
            target["network"] = f"eip155:{cid}"
    return body


async def facilitator_verify(
    facilitator_url: str,
    payload: PaymentPayloadV2,
    timeout_seconds: float = 30.0,
) -> FacilitatorVerifyResponse:
    """POST the v2 payment to the facilitator ``/verify`` endpoint.

    Mirrors the canonical x402 facilitator contract
    (typescript ``httpFacilitatorClient.verify``): body is
    ``{x402Version, paymentPayload, paymentRequirements}`` and the response is
    ``{isValid, invalidReason?, invalidMessage?, payer?, ...}``.

    Contract it to the exact Requirements object the client signed against (the
    ``accepted`` entry) — not our raw advertised envelope — so the facilitator
    validates signature value/nonce against what was actually signed.

    Raises ``X402VerifyError`` on network / non-200 / non-JSON errors so callers
    can fail closed (never treat an unreachable facilitator as "valid").
    """
    import httpx

    body = _caip2_normalize({
        "x402Version": payload.x402Version,
        "paymentPayload": payload.model_dump(by_alias=True, exclude_none=True),
        "paymentRequirements": payload.accepted.model_dump(by_alias=True, exclude_none=True),
    })

    url = facilitator_url.rstrip("/") + "/verify"
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.post(
                url, json=body, headers=build_facilitator_headers(facilitator_url, "verify")
            )
    except httpx.HTTPError as exc:
        raise X402VerifyError(f"facilitator unavailable: {exc}") from exc

    if resp.status_code != 200:
        raise X402VerifyError(
            f"facilitator returned HTTP {resp.status_code}"
        )

    try:
        data = resp.json()
    except _json.JSONDecodeError as exc:
        raise X402VerifyError("facilitator returned non-JSON response") from exc

    if not isinstance(data, dict):
        raise X402VerifyError("facilitator returned unexpected response")

    return FacilitatorVerifyResponse(
        is_valid=bool(data.get("isValid", False)),
        payer=data.get("payer"),
        invalid_reason=data.get("invalidReason"),
        invalid_message=data.get("invalidMessage"),
    )


async def facilitator_settle(
    facilitator_url: str,
    payload: PaymentPayloadV2,
    timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    """POST a verified v2 payment to the facilitator ``/settle`` endpoint.

    Returns a normalized dict with ``settled`` bool plus the facilitator's
    transaction/network/payer/error fields when present. Fails closed: on
    transport error it returns ``{"settled": False, "error": ...}`` (the scrape
    has already succeeded by this point, so we log and move on rather than
    crash the response).
    """
    import httpx

    body = _caip2_normalize({
        "x402Version": payload.x402Version,
        "paymentPayload": payload.model_dump(by_alias=True, exclude_none=True),
        "paymentRequirements": payload.accepted.model_dump(by_alias=True, exclude_none=True),
    })

    url = facilitator_url.rstrip("/") + "/settle"
    try:
        headers = build_facilitator_headers(facilitator_url, "settle")
    except X402VerifyError as exc:
        logger.error("x402 facilitator settle auth failed: %s", exc)
        return {"settled": False, "error": str(exc)}
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.post(
                url, json=body, headers=headers
            )
    except httpx.HTTPError as exc:
        logger.error("x402 facilitator settle request failed: %s", exc)
        return {"settled": False, "error": str(exc)}

    if resp.status_code != 200:
        logger.error("x402 facilitator settle returned HTTP %s", resp.status_code)
        return {"settled": False, "error": f"facilitator HTTP {resp.status_code}"}

    try:
        data = resp.json()
    except _json.JSONDecodeError:
        return {"settled": False, "error": "facilitator non-JSON response"}
    if not isinstance(data, dict):
        return {"settled": False, "error": "facilitator unexpected response"}

    success = bool(data.get("success", False))
    return {
        "settled": success,
        "amount_usd": None,
        "transaction": data.get("transaction"),
        "network": data.get("network"),
        "payer": data.get("payer"),
        "error": data.get("errorReason") or data.get("errorMessage"),
    }
