"""
Structured extraction routes — Phase 2 Work Stream 1.

Endpoint:
  POST /v1/extract — Extract structured data from a web page using a JSON
                     schema, CSS selectors, or XPath queries.

Auth: API key (subscription) via the shared `get_current_user_from_api_key`
      dependency from api_keys.py.
"""

from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from extraction import extract_from_html
from proxy_manager import should_use_proxy, get_proxy_dict
from api_keys import get_current_user_from_api_key
from models import ApiKey, User
from usage_logger import record_usage_event, extract_domain

router = APIRouter(prefix="/v1", tags=["extract"])


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------

class ExtractOptions(BaseModel):
    format: str = Field("json", description="Output format (currently: json)")
    wait_for: Optional[str] = Field(None, description="CSS selector to wait for before extraction")
    timeout: int = Field(30_000, ge=1000, le=120_000, description="Navigation timeout (ms)")
    block_media: bool = Field(True, description="Block image/font/video requests")
    wait_strategy: Optional[str] = Field(None, description="Wait strategy: default, spa, heavy, cloudflare")
    javascript: Optional[str] = Field(None, description="Custom JS to execute after page load")
    proxy_url: Optional[str] = Field(None, description="Proxy URL (socks5:// or http://)")
    retry: bool = Field(True, description="Enable retry on failure")
    bypass_cache: bool = Field(False, description="Skip cache, force fresh fetch")


class ExtractRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    url: HttpUrl = Field(..., description="Target URL to extract data from")
    # Field is named `json_schema` to avoid shadowing BaseModel.schema, but is
    # serialized/deserialized as `schema` to match the public API contract.
    json_schema: Optional[dict] = Field(None, alias="schema", description="JSON schema defining fields to extract")
    selectors: Optional[dict] = Field(None, description="CSS selectors to extract")
    xpaths: Optional[dict] = Field(None, description="XPath queries to extract")
    options: Optional[ExtractOptions] = Field(None, description="Extraction options")


class ExtractMetadata(BaseModel):
    extraction_method: str
    confidence: float
    processing_time_ms: int


class ExtractResponse(BaseModel):
    status: int
    url: str
    data: dict
    metadata: ExtractMetadata
    payment: Optional[dict] = None


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/extract", response_model=ExtractResponse)
async def extract_endpoint(
    req: ExtractRequest,
    auth: tuple[User, ApiKey] = Depends(get_current_user_from_api_key),
):
    """
    Extract structured data from a web page.

    Provide exactly one (or more) of `schema`, `selectors`, or `xpaths`.
    Priority: schema > selectors > xpaths.
    """
    # Lazy import to avoid circular dependency
    from server import engine
    
    url = str(req.url)
    opts = req.options or ExtractOptions()
    start = time.monotonic()
    user, api_key = auth

    def _log_usage(status_code: int, success: bool, error_message: Optional[str] = None) -> None:
        """Record a usage event for this extract request (fire-and-forget)."""
        record_usage_event(
            key_id=api_key.id,
            user_id=user.id,
            endpoint="/v1/extract",
            url=url,
            domain=extract_domain(url),
            status_code=status_code,
            success=success,
            response_time_ms=int((time.monotonic() - start) * 1000),
            error_message=error_message,
            auth_method="api_key",
        )

    # At least one extraction method must be provided.
    if not req.json_schema and not req.selectors and not req.xpaths:
        raise HTTPException(status_code=400, detail={
            "error": "invalid_request",
            "message": "Provide at least one of: schema, selectors, or xpaths",
        })

    # Determine proxy (mirrors scrape endpoint behavior).
    proxy_config = None
    if should_use_proxy(url, direct_failed=False):
        proxy_config = get_proxy_dict(strategy="random")

    try:
        raw_html = await engine.fetch_html(
            url=url,
            wait_for_selector=opts.wait_for,
            timeout_ms=opts.timeout,
            block_media=opts.block_media,
            proxy_url=opts.proxy_url,
            proxy_config=proxy_config,
            wait_strategy=opts.wait_strategy,
            retry=opts.retry,
            bypass_cache=opts.bypass_cache,
            javascript=opts.javascript,
        )
    except TimeoutError as exc:
        _log_usage(504, False, str(exc))
        raise HTTPException(status_code=504, detail={
            "error": "timeout", "message": str(exc), "url": url, "payment_charged": False,
        })
    except ConnectionError as exc:
        _log_usage(400, False, str(exc))
        raise HTTPException(status_code=400, detail={
            "error": "connection_failed", "message": str(exc), "url": url, "payment_charged": False,
        })
    except Exception as exc:
        error_msg = str(exc).lower()
        if any(kw in error_msg for kw in ["ns_error_unknown_host", "err_name", "net::err", "name_not_resolved"]):
            _log_usage(400, False, str(exc))
            raise HTTPException(status_code=400, detail={
                "error": "connection_failed", "message": str(exc), "url": url, "payment_charged": False,
            })
        _log_usage(500, False, str(exc))
        raise HTTPException(status_code=500, detail={
            "error": "extract_failed", "message": str(exc), "url": url, "payment_charged": False,
        })

    # Extract structured data from the raw HTML.
    data, confidence, method = extract_from_html(raw_html, req.json_schema, req.selectors, req.xpaths)

    # Payment result is always None for the API-key path (x402 settlement for
    # extract is not yet implemented).
    payment_result = None

    elapsed_ms = int((time.monotonic() - start) * 1000)
    _log_usage(200, True)
    return ExtractResponse(
        status=200,
        url=url,
        data=data,
        metadata=ExtractMetadata(
            extraction_method=method or "none",
            confidence=confidence,
            processing_time_ms=elapsed_ms,
        ),
        payment=payment_result,
    )
