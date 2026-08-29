"""
X1-BaaS-Engine — Browser-as-a-Service for Autonomous AI Agents

FastAPI server with stealth Camoufox browser engine, DOM cleaning,
HTML-to-Markdown pipeline, and x402 payment middleware.

Phase 2: x402 micro-settlements on Base Mainnet (USDC).
"""

from __future__ import annotations

import asyncio
import base64
import json as json_mod
import logging
import os
import re
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any, Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Comment
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from markdownify import markdownify as md
from pydantic import BaseModel, Field, HttpUrl

# Database
from database import get_db, init_db, close_db, async_session_factory
from models import User, ApiKey, UsageEvent, Subscription, DailyUsageSummary
from api_keys import hash_api_key
from sqlalchemy import select
from datetime import datetime, timezone
from proxy_manager import should_use_proxy, get_proxy_dict, get_proxy_stats
from redis_rate_limiter import redis_rate_limiter, RedisRateLimiter
from output_formats import OutputFormatter
from usage_logger import record_usage_event, extract_domain
from auth_deps import X402PaymentRequired, x402_http_exception_handler
from auth_deps import build_x402_payment_requirements as _build_x402_requirements

# ---------------------------------------------------------------------------
# Structured Logging
# ---------------------------------------------------------------------------
class StructuredFormatter(logging.Formatter):
    """JSON structured log formatter for production."""
    def format(self, record):
        log_data = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if hasattr(record, 'request_id'):
            log_data["request_id"] = record.request_id
        if hasattr(record, 'url'):
            log_data["url"] = record.url
        if hasattr(record, 'duration_ms'):
            log_data["duration_ms"] = record.duration_ms
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        return json_mod.dumps(log_data)

# Configure logging
LOG_LEVEL = os.getenv("BAAAS_LOG_LEVEL", "info").upper()
LOG_FORMAT = os.getenv("BAAAS_LOG_FORMAT", "text")  # "text" or "json"

if LOG_FORMAT == "json":
    handler = logging.StreamHandler()
    handler.setFormatter(StructuredFormatter())
    logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), handlers=[handler])
else:
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

logger = logging.getLogger("baas-engine")

# ---------------------------------------------------------------------------
# Configuration (env vars)
# ---------------------------------------------------------------------------
API_KEY = os.getenv("BAAAS_API_KEY", "")
RATE_LIMIT_RPM = int(os.getenv("BAAAS_RATE_LIMIT", "30"))
HOST = os.getenv("BAAAS_HOST", "0.0.0.0")
PORT = int(os.getenv("BAAAS_PORT", "8000"))

# x402 Payment Configuration
X402_ENABLED = os.getenv("X402_ENABLED", "false").lower() == "true"
X402_RECIPIENT_WALLET = os.getenv("X402_RECIPIENT_WALLET", "")
X402_PRICE_USD = os.getenv("X402_PRICE_USD", "0.005")
X402_NETWORK = os.getenv("X402_NETWORK", "base")  # Base Mainnet
X402_FACILITATOR_URL = os.getenv("X402_FACILITATOR_URL", "https://x402.org/facilitator")
X402_USDC_BASE = os.getenv(
    "X402_ASSET", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
)  # Base Mainnet USDC default; override to Base Sepolia USDC for testnet dry-run

# Rate limit config
RATE_LIMIT_RPM = int(os.getenv("BAAAS_RATE_LIMIT", "30"))

# ---------------------------------------------------------------------------
# Nonce Cache (double-spend prevention)
# ---------------------------------------------------------------------------
class NonceCache:
    """In-memory nonce cache to prevent replay attacks. Replace with Redis for multi-instance."""

    def __init__(self, ttl_seconds: int = 300) -> None:
        self._used: dict[str, float] = {}
        self._ttl = ttl_seconds

    def is_used(self, nonce: str) -> bool:
        self._prune()
        return nonce in self._used

    def mark_used(self, nonce: str) -> None:
        self._used[nonce] = time.time()

    def _prune(self) -> None:
        cutoff = time.time() - self._ttl
        expired = [k for k, v in self._used.items() if v < cutoff]
        for k in expired:
            del self._used[k]

nonce_cache = NonceCache()

# ---------------------------------------------------------------------------
# Robust Scraping Configuration
# ---------------------------------------------------------------------------
MAX_RETRIES = int(os.getenv("BAAAS_MAX_RETRIES", "3"))
RETRY_DELAY_MS = int(os.getenv("BAAAS_RETRY_DELAY_MS", "1000"))
DOMAIN_COOLDOWN_MS = int(os.getenv("BAAAS_DOMAIN_COOLDOWN_MS", "2000"))
CACHE_TTL_SECONDS = int(os.getenv("BAAAS_CACHE_TTL", "300"))  # 5 min default
CACHE_MAX_SIZE = int(os.getenv("BAAAS_CACHE_MAX_SIZE", "1000"))

# Wait strategy presets by site type
WAIT_STRATEGIES = {
    "default": {"wait_until": "domcontentloaded", "post_wait": 0.5},
    "spa": {"wait_until": "networkidle", "post_wait": 1.0},
    "heavy": {"wait_until": "domcontentloaded", "post_wait": 0.3},
    "cloudflare": {"wait_until": "networkidle", "post_wait": 3.0},
}

# Known heavy/protected domains
DOMAIN_PROFILES = {
    "businessinsider.com": "heavy",
    "linkedin.com": "cloudflare",
    "twitter.com": "spa",
    "x.com": "spa",
    "facebook.com": "cloudflare",
    "instagram.com": "spa",
    "medium.com": "spa",
    "nytimes.com": "heavy",
    "wsj.com": "heavy",
    "bloomberg.com": "heavy",
    "reuters.com": "heavy",
    "techcrunch.com": "spa",
    "verge.com": "heavy",
    "washingtonpost.com": "heavy",
}

# Domain cooldown tracker (avoid hammering same domain)
_domain_last_hit: dict[str, float] = {}

# Response cache (avoid re-scraping identical URLs)
class ScrapeCache:
    def __init__(self, ttl: int = 300, max_size: int = 1000):
        self._cache: dict[str, tuple[float, tuple]] = {}
        self._ttl = ttl
        self._max_size = max_size

    def get(self, url: str) -> tuple | None:
        self._prune()
        if url in self._cache:
            ts, result = self._cache[url]
            if time.time() - ts < self._ttl:
                logger.debug("Cache hit: %s", url)
                return result
            del self._cache[url]
        return None

    def set(self, url: str, result: tuple) -> None:
        if len(self._cache) >= self._max_size:
            # Evict oldest
            oldest = min(self._cache, key=lambda k: self._cache[k][0])
            del self._cache[oldest]
        self._cache[url] = (time.time(), result)

    def _prune(self) -> None:
        cutoff = time.time() - self._ttl
        expired = [k for k, (ts, _) in self._cache.items() if ts < cutoff]
        for k in expired:
            del self._cache[k]

scrape_cache = ScrapeCache(CACHE_TTL_SECONDS, CACHE_MAX_SIZE)

# ---------------------------------------------------------------------------
# Rate Limiter (in-memory, per-IP, sliding window)
# ---------------------------------------------------------------------------
class RateLimiter:
    """
    Hybrid rate limiter: Redis-backed when available, falls back to in-memory.
    Uses sliding window algorithm.
    """

    def __init__(self, rpm: int = 30) -> None:
        self.rpm = rpm
        self._requests: dict[str, list[float]] = defaultdict(list)

    async def check(self, ip: str) -> bool:
        """Check if request is allowed. Uses Redis if available, falls back to in-memory."""
        # Try Redis first if connected
        if redis_rate_limiter.is_connected:
            key = f"rate:{ip}"
            allowed = await redis_rate_limiter.check_rate_limit(key, self.rpm, window=60)
            if not allowed:
                logger.warning("Rate limit exceeded for %s (Redis)", ip)
            return allowed

        # Fallback to in-memory
        now = time.time()
        window_start = now - 60.0
        self._requests[ip] = [t for t in self._requests[ip] if t > window_start]
        if len(self._requests[ip]) >= self.rpm:
            logger.warning("Rate limit exceeded for %s (in-memory)", ip)
            return False
        self._requests[ip].append(now)
        return True

rate_limiter = RateLimiter(RATE_LIMIT_RPM)
security = HTTPBearer(auto_error=False)

# ---------------------------------------------------------------------------
# x402 Facilitator Client (lazy-initialized)
# ---------------------------------------------------------------------------
_x402_facilitator = None
_x402_requirements = None


def get_x402():
    """Lazy-init x402 facilitator client and payment requirements."""
    global _x402_facilitator, _x402_requirements
    if _x402_facilitator is not None:
        return _x402_facilitator, _x402_requirements

    from x402.facilitator import FacilitatorClient, FacilitatorConfig
    from x402.types import PaymentRequirements

    config = FacilitatorConfig(url=X402_FACILITATOR_URL)
    facilitator = FacilitatorClient(config=config)

    requirements = PaymentRequirements(
        scheme="exact",
        network=X402_NETWORK,
        max_amount_required=str(int(float(X402_PRICE_USD) * 1_000_000)),
        resource="/v1/scrape",
        description="BaaS stealth scrape — clean Markdown for LLM agents",
        mime_type="application/json",
        pay_to=X402_RECIPIENT_WALLET,
        max_timeout_seconds=60,
        asset=X402_USDC_BASE,
    )

    _x402_facilitator = facilitator
    _x402_requirements = requirements
    logger.info("x402 initialized — pay_to=%s, price=$%s, network=%s",
                X402_RECIPIENT_WALLET, X402_PRICE_USD, X402_NETWORK)
    return facilitator, requirements


# ---------------------------------------------------------------------------
# Pydantic Schemas (v2)
# ---------------------------------------------------------------------------

class ScrapeRequest(BaseModel):
    url: HttpUrl = Field(..., description="Target URL to scrape")
    output: Optional[str] = Field(None, description="Output format: markdown (default), screenshot, pdf, csv, html")
    options: Optional[dict] = Field(None, description="Output-specific options")
    wait_for_selector: Optional[str] = Field(None, description="CSS selector to wait for")
    timeout_ms: int = Field(20_000, ge=1000, le=120_000, description="Navigation timeout (ms)")
    block_media: bool = Field(True, description="Block image/font/video requests")
    proxy_url: Optional[str] = Field(None, description="Proxy URL (socks5:// or http://)")
    wait_strategy: Optional[str] = Field(None, description="Wait strategy: default, spa, heavy, cloudflare (auto-detected if omitted)")
    retry: bool = Field(True, description="Enable retry on failure")
    bypass_cache: bool = Field(False, description="Skip cache, force fresh scrape")
    javascript: Optional[str] = Field(None, description="Custom JS to execute after page load")


class ScrapeData(BaseModel):
    title: str
    markdown: str
    character_count: int


# Valid output formats for /v1/scrape.
VALID_OUTPUTS = {"markdown", "screenshot", "pdf", "csv", "html"}


class ScrapeResponse(BaseModel):
    status: int
    url: str
    output: str = "markdown"
    data: Any = Field(..., description="ScrapeData for markdown, format-specific dict otherwise")
    execution_time_ms: int
    payment: Optional[dict] = None


class HealthResponse(BaseModel):
    status: str
    browser_active: bool
    browser_contexts: int
    uptime_seconds: float
    x402_enabled: bool


# ---------------------------------------------------------------------------
# DOM Cleaning & Markdown Pipeline
# ---------------------------------------------------------------------------

STRIP_TAGS = [
    "script", "style", "nav", "footer", "aside", "svg", "iframe",
    "noscript", "header", "form", "button", "input", "select",
    "textarea", "dialog", "menu", "menuitem",
]
STRIP_ATTRS = [
    "onclick", "onload", "onerror", "onmouseover", "onfocus", "onblur",
    "data-track", "data-analytics", "data-ga", "data-gtm",
]


def clean_html(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html, "lxml")
    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()
    for tag_name in STRIP_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()
    for tag in soup.find_all(True):
        for attr in STRIP_ATTRS:
            if attr in tag.attrs:
                del tag.attrs[attr]
    return str(soup)


def html_to_markdown(cleaned_html: str, url: str = "") -> tuple[str, str]:
    title = ""
    markdown_content = ""
    try:
        from readabilipy.simple_json import simple_json_from_html_string
        result = simple_json_from_html_string(cleaned_html, use_readability=True)
        if result:
            title = (result.get("title") or "").strip()
            content_html = result.get("content") or ""
            if content_html and len(content_html.strip()) > 50:
                markdown_content = md(
                    content_html, heading_style="ATX", bullets="-",
                    strip=["img", "video", "audio", "picture"],
                ).strip()
    except Exception as exc:
        logger.warning("Readabilipy failed: %s — falling back", exc)

    if not markdown_content or len(markdown_content) < 100:
        markdown_content = md(
            cleaned_html, heading_style="ATX", bullets="-",
            strip=["img", "video", "audio", "picture"],
        ).strip()

    if not title:
        soup = BeautifulSoup(cleaned_html, "lxml")
        title_tag = soup.find("title")
        if title_tag:
            title = title_tag.get_text(strip=True)
        elif markdown_content:
            first_heading = re.search(r"^#+\s+(.+)$", markdown_content, re.MULTILINE)
            if first_heading:
                title = first_heading.group(1).strip()

    markdown_content = re.sub(r"\n{4,}", "\n\n\n", markdown_content)
    markdown_content = re.sub(r"[ \t]+\n", "\n", markdown_content)
    return title, markdown_content


# ---------------------------------------------------------------------------
# Stealth Browser Engine (Camoufox)
# ---------------------------------------------------------------------------

MEDIA_EXTENSIONS = re.compile(
    r"\.(png|jpe?g|gif|webp|svg|ico|bmp|tiff?|woff2?|ttf|eot|otf|mp4|webm|ogg|mp3|wav|avi|mov|flv|wmv|css)(\?|$)",
    re.IGNORECASE,
)


class BrowserEngine:
    def __init__(self) -> None:
        self.camoufox = None
        self._start_time = time.time()
        self._context_count = 0

    async def start(self) -> None:
        try:
            from camoufox.async_api import AsyncCamoufox
            self.camoufox = AsyncCamoufox
            logger.info("Camoufox loaded")
        except ImportError:
            logger.error("Camoufox not installed")
            raise

    async def stop(self) -> None:
        logger.info("Browser engine stopped")

    def _detect_domain_profile(self, url: str) -> str:
        """Auto-detect wait strategy based on domain."""
        try:
            hostname = urlparse(url).hostname or ""
            for domain, profile in DOMAIN_PROFILES.items():
                if domain in hostname:
                    return profile
        except Exception:
            pass
        return "default"

    async def _domain_cooldown(self, url: str) -> None:
        """Enforce cooldown between requests to same domain."""
        try:
            hostname = urlparse(url).hostname or ""
            if hostname in _domain_last_hit:
                elapsed = (time.time() - _domain_last_hit[hostname]) * 1000
                if elapsed < DOMAIN_COOLDOWN_MS:
                    wait_ms = DOMAIN_COOLDOWN_MS - elapsed
                    logger.debug("Domain cooldown: %s — waiting %dms", hostname, wait_ms)
                    await asyncio.sleep(wait_ms / 1000)
            _domain_last_hit[hostname] = time.time()
        except Exception:
            pass

    async def _stealth_setup(self, page) -> None:
        """Apply stealth techniques to avoid bot detection."""
        try:
            # Override navigator.webdriver (Camoufox handles most, but belt-and-suspenders)
            await page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                // Override permissions query
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                        Promise.resolve({state: Notification.permission}) :
                        originalQuery(parameters)
                );
                // Override plugins length
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5]
                });
                // Override languages
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['en-US', 'en']
                });
            """)
        except Exception:
            pass  # Non-critical

    async def _smart_wait(self, page, strategy: str, timeout_ms: int) -> None:
        """Apply smart wait strategy based on site type."""
        config = WAIT_STRATEGIES.get(strategy, WAIT_STRATEGIES["default"])
        wait_until = config["wait_until"]
        post_wait = config["post_wait"]

        # Wait for primary load state
        try:
            await page.wait_for_load_state(wait_until, timeout=min(timeout_ms, 15_000))
        except Exception:
            logger.debug("Wait strategy '%s' timed out on %s — proceeding", strategy, wait_until)

        # Post-wait for JS rendering
        if post_wait > 0:
            await asyncio.sleep(post_wait)

    async def _try_extract_content(self, page, url: str) -> tuple[str, str, int]:
        """Extract content from page, with fallback strategies."""
        raw_html = await page.content()

        # Check if we got a real page (not empty/blocked)
        if len(raw_html) < 500:
            logger.warning("Suspiciously short HTML (%d chars) on %s", len(raw_html), url)

        cleaned = clean_html(raw_html)
        title, markdown_content = html_to_markdown(cleaned, url)
        char_count = len(markdown_content)

        # If content is too short, try scrolling to trigger lazy loading
        if char_count < 200:
            logger.debug("Content too short (%d chars) — trying scroll", char_count)
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(1.0)
                await page.evaluate("window.scrollTo(0, 0)")
                await asyncio.sleep(0.5)
                raw_html = await page.content()
                cleaned = clean_html(raw_html)
                title, markdown_content = html_to_markdown(cleaned, url)
                char_count = len(markdown_content)
            except Exception:
                pass

        return title, markdown_content, char_count

    async def scrape(self, url, wait_for_selector=None, timeout_ms=20_000,
                     block_media=True, proxy_url=None, proxy_config=None,
                     wait_strategy=None, retry=True, bypass_cache=False,
                     javascript=None) -> tuple[str, str, int]:
        """Robust scrape with retry, smart waits, and anti-bot bypass."""

        # Check cache first
        if not bypass_cache:
            cached = scrape_cache.get(url)
            if cached:
                return cached

        # Domain cooldown
        await self._domain_cooldown(url)

        # Auto-detect strategy if not specified
        if not wait_strategy:
            wait_strategy = self._detect_domain_profile(url)

        max_attempts = MAX_RETRIES if retry else 1
        last_error = None

        for attempt in range(1, max_attempts + 1):
            self._context_count += 1
            start = time.monotonic()

            try:
                result = await self._scrape_once(
                    url, wait_for_selector, timeout_ms, block_media,
                    proxy_url, proxy_config, wait_strategy, javascript,
                )
                # Cache successful result
                scrape_cache.set(url, result)
                return result

            except (TimeoutError, ConnectionError) as exc:
                last_error = exc
                elapsed_ms = int((time.monotonic() - start) * 1000)
                logger.warning("Attempt %d/%d failed for %s (%dms): %s",
                              attempt, max_attempts, url, elapsed_ms, exc)

                if attempt < max_attempts:
                    # Exponential backoff
                    delay = (RETRY_DELAY_MS / 1000) * (2 ** (attempt - 1))
                    logger.info("Retrying in %.1fs...", delay)
                    await asyncio.sleep(delay)

                    # On retry, try a different strategy
                    if attempt == 2 and wait_strategy == "default":
                        wait_strategy = "spa"
                        logger.info("Switching to SPA wait strategy")
                    elif attempt == 3 and wait_strategy == "spa":
                        wait_strategy = "heavy"
                        logger.info("Switching to heavy wait strategy")

            except Exception as exc:
                last_error = exc
                logger.error("Unexpected error scraping %s: %s", url, exc)
                break  # Don't retry unexpected errors

        # All retries exhausted
        raise last_error or RuntimeError(f"Failed to scrape {url} after {max_attempts} attempts")

    def _launch_options(self, proxy_url, proxy_config) -> dict:
        """Build Camoufox launch options (shared by scrape and fetch_html)."""
        launch_options = {
            "humanize": True,
            "disable_coop": True,
            "headless": True,
            "geoip": True,
            "i_know_what_im_doing": True,  # Suppress COOP warning
        }
        if proxy_config:
            launch_options["proxy"] = proxy_config
        elif proxy_url:
            launch_options["proxy"] = {"server": proxy_url}
        return launch_options

    async def _setup_page(self, page, block_media: bool) -> None:
        """Apply stealth + optional media blocking before navigation."""
        await self._stealth_setup(page)
        if block_media:
            async def block_media_routes(route):
                if MEDIA_EXTENSIONS.search(route.request.url):
                    await route.abort()
                else:
                    await route.continue_()
            await page.route("**/*", block_media_routes)

    async def _navigate(self, page, url, wait_for_selector, timeout_ms,
                        wait_strategy, javascript) -> None:
        """Navigate to a URL, handle Cloudflare, apply waits and custom JS."""
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            # Check for Cloudflare challenge pages
            if response and response.status == 403:
                page_text = await page.text_content("body") or ""
                if "challenge" in page_text.lower() or "cloudflare" in page_text.lower():
                    logger.info("Cloudflare challenge detected on %s — waiting", url)
                    await asyncio.sleep(5.0)  # Wait for challenge to resolve
                    try:
                        await page.wait_for_load_state("networkidle", timeout=15_000)
                    except Exception:
                        pass
        except Exception as exc:
            await page.close()
            error_msg = str(exc)
            if "Timeout" in error_msg or "timeout" in error_msg:
                raise TimeoutError(f"Navigation timed out after {timeout_ms}ms: {url}")
            if "net::" in error_msg or "ERR_NAME" in error_msg:
                raise ConnectionError(f"Failed to connect to {url}: {error_msg}")
            raise

        # Wait for selector if specified
        if wait_for_selector:
            try:
                await page.wait_for_selector(wait_for_selector, timeout=min(timeout_ms, 10_000))
            except Exception:
                logger.warning("Selector '%s' not found on %s", wait_for_selector, url)

        # Smart wait
        await self._smart_wait(page, wait_strategy, timeout_ms)

        # Execute custom JS if provided
        if javascript:
            try:
                await page.evaluate(javascript)
                await asyncio.sleep(0.5)
            except Exception as exc:
                logger.warning("Custom JS failed on %s: %s", url, exc)

    async def _scrape_once(self, url, wait_for_selector, timeout_ms, block_media,
                           proxy_url, proxy_config, wait_strategy, javascript) -> tuple[str, str, int]:
        """Single scrape attempt with full browser lifecycle."""
        from camoufox.async_api import AsyncCamoufox
        start = time.monotonic()

        async with AsyncCamoufox(**self._launch_options(proxy_url, proxy_config)) as browser:
            page = await browser.new_page()
            await self._setup_page(page, block_media)
            await self._navigate(page, url, wait_for_selector, timeout_ms, wait_strategy, javascript)

            # Extract content
            title, markdown_content, char_count = await self._try_extract_content(page, url)
            await page.close()

        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.info("Scraped %s — %d chars, %dms (strategy: %s)",
                    url, char_count, elapsed_ms, wait_strategy)
        return title, markdown_content, char_count

    async def _fetch_html_once(self, url, wait_for_selector, timeout_ms, block_media,
                               proxy_url, proxy_config, wait_strategy, javascript) -> str:
        """Single fetch attempt returning the raw rendered HTML."""
        from camoufox.async_api import AsyncCamoufox
        start = time.monotonic()

        async with AsyncCamoufox(**self._launch_options(proxy_url, proxy_config)) as browser:
            page = await browser.new_page()
            await self._setup_page(page, block_media)
            await self._navigate(page, url, wait_for_selector, timeout_ms, wait_strategy, javascript)

            raw_html = await page.content()
            await page.close()

        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.info("Fetched HTML for %s — %d chars, %dms (strategy: %s)",
                    url, len(raw_html), elapsed_ms, wait_strategy)
        return raw_html

    async def fetch_html(self, url, wait_for_selector=None, timeout_ms=20_000,
                         block_media=True, proxy_url=None, proxy_config=None,
                         wait_strategy=None, retry=True, bypass_cache=False,
                         javascript=None) -> str:
        """Fetch raw rendered HTML with retry, smart waits, and anti-bot bypass.

        Returns the full post-JS DOM HTML (not cleaned Markdown), suitable for
        schema/CSS/XPath extraction. The scrape cache is intentionally not used
        here — raw HTML is large and extraction requests are typically unique.
        """
        await self._domain_cooldown(url)

        if not wait_strategy:
            wait_strategy = self._detect_domain_profile(url)

        max_attempts = MAX_RETRIES if retry else 1
        last_error = None

        for attempt in range(1, max_attempts + 1):
            self._context_count += 1
            start = time.monotonic()

            try:
                return await self._fetch_html_once(
                    url, wait_for_selector, timeout_ms, block_media,
                    proxy_url, proxy_config, wait_strategy, javascript,
                )

            except (TimeoutError, ConnectionError) as exc:
                last_error = exc
                elapsed_ms = int((time.monotonic() - start) * 1000)
                logger.warning("HTML fetch attempt %d/%d failed for %s (%dms): %s",
                               attempt, max_attempts, url, elapsed_ms, exc)

                if attempt < max_attempts:
                    delay = (RETRY_DELAY_MS / 1000) * (2 ** (attempt - 1))
                    logger.info("Retrying in %.1fs...", delay)
                    await asyncio.sleep(delay)

                    if attempt == 2 and wait_strategy == "default":
                        wait_strategy = "spa"
                    elif attempt == 3 and wait_strategy == "spa":
                        wait_strategy = "heavy"

            except Exception as exc:
                last_error = exc
                logger.error("Unexpected error fetching HTML for %s: %s", url, exc)
                break

        raise last_error or RuntimeError(f"Failed to fetch {url} after {max_attempts} attempts")

    async def _render_once(self, url, renderer, wait_for_selector, timeout_ms, block_media,
                           proxy_url, proxy_config, wait_strategy, javascript):
        """Single render attempt: open page, hand it to `renderer`, close page."""
        from camoufox.async_api import AsyncCamoufox
        start = time.monotonic()

        async with AsyncCamoufox(**self._launch_options(proxy_url, proxy_config)) as browser:
            page = await browser.new_page()
            await self._setup_page(page, block_media)
            await self._navigate(page, url, wait_for_selector, timeout_ms, wait_strategy, javascript)
            result = await renderer(page)
            await page.close()

        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.info("Rendered %s — %dms (strategy: %s)", url, elapsed_ms, wait_strategy)
        return result

    async def render(self, url, renderer, wait_for_selector=None, timeout_ms=20_000,
                     block_media=True, proxy_url=None, proxy_config=None,
                     wait_strategy=None, retry=True, javascript=None):
        """Render a URL and pass the live page to an async `renderer(page)`.

        ``renderer`` receives the navigated Playwright page and returns arbitrary
        data (e.g. a screenshot / pdf / csv / clean-html payload). Retry, stealth
        setup, and navigation mirror ``fetch_html``; the markdown scrape cache is
        intentionally not used here.
        """
        await self._domain_cooldown(url)

        if not wait_strategy:
            wait_strategy = self._detect_domain_profile(url)

        max_attempts = MAX_RETRIES if retry else 1
        last_error = None

        for attempt in range(1, max_attempts + 1):
            self._context_count += 1
            start = time.monotonic()

            try:
                return await self._render_once(
                    url, renderer, wait_for_selector, timeout_ms, block_media,
                    proxy_url, proxy_config, wait_strategy, javascript,
                )

            except (TimeoutError, ConnectionError) as exc:
                last_error = exc
                elapsed_ms = int((time.monotonic() - start) * 1000)
                logger.warning("Render attempt %d/%d failed for %s (%dms): %s",
                              attempt, max_attempts, url, elapsed_ms, exc)

                if attempt < max_attempts:
                    delay = (RETRY_DELAY_MS / 1000) * (2 ** (attempt - 1))
                    logger.info("Retrying in %.1fs...", delay)
                    await asyncio.sleep(delay)

                    if attempt == 2 and wait_strategy == "default":
                        wait_strategy = "spa"
                    elif attempt == 3 and wait_strategy == "spa":
                        wait_strategy = "heavy"

            except Exception as exc:
                last_error = exc
                logger.error("Unexpected error rendering %s: %s", url, exc)
                break

        raise last_error or RuntimeError(f"Failed to render {url} after {max_attempts} attempts")

    @property
    def uptime(self) -> float:
        return time.time() - self._start_time

    @property
    def context_count(self) -> int:
        return self._context_count


# ---------------------------------------------------------------------------
# FastAPI Application
# ---------------------------------------------------------------------------

engine = BrowserEngine()
output_formatter = OutputFormatter()

# Map output format -> OutputFormatter method name (for the /v1/scrape endpoint).
OUTPUT_RENDERERS = {
    "screenshot": "screenshot",
    "pdf": "pdf",
    "csv": "csv",
    "html": "clean_html",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting X1-BaaS-Engine (x402=%s)...", X402_ENABLED)
    await engine.start()
    if X402_ENABLED:
        get_x402()

    # Initialize Redis rate limiter
    redis_connected = await redis_rate_limiter.connect()
    if redis_connected:
        logger.info("Redis rate limiter initialized")
    else:
        logger.warning("Redis unavailable — using in-memory rate limiting")

    # Initialize database
    await init_db()
    logger.info("Database initialized")

    logger.info("Ready")
    yield

    # Shutdown
    await close_db()
    await redis_rate_limiter.disconnect()
    await engine.stop()


# Disable docs in production
BAAAS_ENV = os.getenv("BAAAS_ENV", "development")
IS_PRODUCTION = BAAAS_ENV == "production"

app = FastAPI(
    title="X1-BaaS-Engine",
    description="Browser-as-a-Service for AI Agents — x402 payment-gated stealth scraping",
    version="0.2.0",
    lifespan=lifespan,
    docs_url=None if IS_PRODUCTION else "/docs",
    redoc_url=None if IS_PRODUCTION else "/redoc",
    openapi_url=None if IS_PRODUCTION else "/openapi.json",
)

# Return 402 (Payment Required) with x402 requirements when x402 is enabled and
# no valid authentication is provided — x402 discovery services probe for this.
app.add_exception_handler(X402PaymentRequired, x402_http_exception_handler)

# Request ID middleware
@app.middleware("http")
async def add_request_id(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response

# Request logging middleware
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.monotonic()
    response = await call_next(request)
    duration_ms = int((time.monotonic() - start) * 1000)
    logger.info(
        "%s %s -> %d (%dms)",
        request.method, request.url.path, response.status_code, duration_ms,
        extra={"request_id": getattr(request.state, 'request_id', 'unknown'), "duration_ms": duration_ms}
    )
    return response

# CORS - restrict in production
ALLOWED_ORIGINS = [
    "https://baas.tazpal.com",
    "https://api.tazpal.com",
]
if not IS_PRODUCTION:
    ALLOWED_ORIGINS.append("*")

app.add_middleware(CORSMiddleware, 
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

# Include auth routes
from routes_auth import router as auth_router
app.include_router(auth_router)

# Include billing routes
from routes_billing import router as billing_router
app.include_router(billing_router)

# Include API key routes
from routes_keys import router as keys_router
app.include_router(keys_router)

# Include dashboard routes
from routes_dashboard import router as dashboard_router
app.include_router(dashboard_router)

# Include admin routes
from routes_admin import router as admin_router
app.include_router(admin_router)


# ---------------------------------------------------------------------------
# Auth & Rate Limiting
# ---------------------------------------------------------------------------

class AuthResult:
    """Authentication result — carries auth method + optional x402 context.

    `key_id` and `user_id` are populated for the database API-key auth path so
    endpoints can attribute usage events to the caller. They are None for the
    legacy env-var key and x402 (pay-per-scrape) paths.
    """
    __slots__ = ('method', 'x402_context', 'key_id', 'user_id')

    def __init__(self, method: str, x402_context: Optional[dict] = None,
                 key_id: Optional[int] = None, user_id: Optional[int] = None):
        self.method = method          # "api_key" or "x402"
        self.x402_context = x402_context  # payment context for settlement (x402 only)
        self.key_id = key_id          # api_keys.id (API-key auth only)
        self.user_id = user_id        # users.id (API-key auth only)

    @property
    def is_x402(self) -> bool:
        return self.method == "x402"


async def authenticate(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> AuthResult:
    """
    Dual-path authentication: API key OR x402 payment.

    Flow:
      1. Rate limit check (per-IP)
      2. API key valid? → allow (subscription user)
      3. x402 payment valid? → allow (pay-per-scrape user)
      4. Neither → 401
    """
    client_ip = request.client.host if request.client else "unknown"

    # 1. Rate limit
    if not await rate_limiter.check(client_ip):
        raise HTTPException(status_code=429, detail={
            "error": "rate_limited",
            "message": f"Rate limit: {RATE_LIMIT_RPM} requests per minute",
        })

    # 2. API key check (database-stored keys)
    if credentials:
        full_key = credentials.credentials
        key_hash = hash_api_key(full_key)
        
        result = await db.execute(
            select(ApiKey).where(ApiKey.key_hash == key_hash)
        )
        api_key = result.scalar_one_or_none()
        
        if api_key and api_key.is_valid:
            # Update last used timestamp
            api_key.last_used_at = datetime.now(timezone.utc)
            await db.flush()
            return AuthResult(method="api_key", key_id=api_key.id, user_id=api_key.user_id)
    
    # 3. Legacy API key check (env var)
    if API_KEY and credentials and credentials.credentials == API_KEY:
        return AuthResult(method="api_key")

    # 4. x402 payment check (only if x402 enabled)
    if X402_ENABLED:
        # v2: PAYMENT-SIGNATURE header (standard base64 JSON of a v2 PaymentPayload)
        payment_signature = (
            request.headers.get("PAYMENT-SIGNATURE")
            or request.headers.get("payment-signature")
        )
        if payment_signature:
            x402_ctx = await _verify_x402_payment_v2(payment_signature)
            if x402_ctx:
                return AuthResult(method="x402", x402_context=x402_ctx)

        # v1: X-PAYMENT header (legacy x402 v0.3.0 payload)
        payment_header = request.headers.get("X-PAYMENT") or request.headers.get("x-payment")
        if payment_header:
            x402_ctx = await _verify_x402_payment(payment_header)
            if x402_ctx:
                return AuthResult(method="x402", x402_context=x402_ctx)

    # 5. Neither path succeeded
    if X402_ENABLED and not credentials:
        # x402 discovery services probe without credentials for a 402
        # (Payment Required) response to discover payment requirements.
        # Advertise the Bazaar discovery extension for Bazaar-listed routes.
        raise X402PaymentRequired(path=request.url.path)
    if API_KEY:
        raise HTTPException(status_code=401, detail={
            "error": "unauthorized",
            "message": "Invalid API key or missing X-PAYMENT header",
        })
    else:
        raise HTTPException(status_code=401, detail={
            "error": "unauthorized",
            "message": "Missing X-PAYMENT header (x402 payment required)",
        })


async def _verify_x402_payment(payment_header: str) -> Optional[dict]:
    """
    Verify x402 payment header. Returns payment context if valid, None if invalid.
    Does NOT raise — caller decides fallback behavior.
    """
    try:
        payment_data = json_mod.loads(base64.b64decode(payment_header))
    except Exception:
        return None

    # Nonce double-spend check
    nonce = ""
    payload = payment_data.get("payload", {})
    if isinstance(payload, dict):
        nonce = payload.get("nonce", "")
    if not nonce:
        nonce = payment_data.get("nonce", "")
    if nonce and nonce_cache.is_used(nonce):
        return None

    # Verify via facilitator
    from x402.types import PaymentPayload
    facilitator, requirements = get_x402()

    try:
        payment_payload = PaymentPayload(**payment_data)
        verify_result = await facilitator.verify(payment_payload, requirements)
        if not verify_result.is_valid:
            return None
    except Exception as exc:
        logger.error("x402 verify error: %s", exc)
        return None

    if nonce:
        nonce_cache.mark_used(nonce)

    logger.info("x402 verified — payer=%s, nonce=%s", verify_result.payer, nonce[:16] if nonce else "?")
    return {
        "payment_payload": payment_payload,
        "requirements": requirements,
        "payer": verify_result.payer,
        "nonce": nonce,
    }


async def _verify_x402_payment_v2(payment_signature: str) -> Optional[dict]:
    """Verify a v2 ``PAYMENT-SIGNATURE`` header (x402 v2 protocol).

    Two-phase verification:
      1. LOCAL: parse + validate the v2 PaymentPayload against the requirements
         we advertise (amount, asset, payTo, network, maxTimeoutSeconds,
         EIP-712 extra), check the EIP-3009 validity window (expiry), and
         recover + match the EIP-712 signer.
      2. FACILITATOR: POST to ``X402_FACILITATOR_URL/verify`` for on-chain
         authorization-state / nonce / balance validation.

    Fails closed: any parse/validation/network error returns None (caller falls
    through to 402), never raising to a 500.
    """
    from x402_verify import (
        VerificationResult,
        X402VerifyError,
        extract_nonce,
        facilitator_verify,
        verify_v2_payment,
    )

    # Advertised requirements (single exact/evm accept entry).
    advertised_env = _build_x402_requirements()
    advertised_accepts = advertised_env.get("accepts") or []
    advertised = advertised_accepts[0] if advertised_accepts else {}

    try:
        result: VerificationResult = verify_v2_payment(payment_signature, advertised)
    except X402VerifyError as exc:
        logger.info("x402 v2 local verification failed: %s", exc)
        return None

    # Double-spend / replay check (nonce from the EIP-3009 authorization).
    nonce = result.nonce
    if nonce and nonce_cache.is_used(nonce):
        logger.warning("x402 v2 replay attempt — nonce already used: %s", nonce[:16])
        return None

    # F-3 (TOCTOU): mark the nonce used BEFORE the facilitator await, so two
    # concurrent requests with the same nonce can't both pass `is_used()` while
    # the first is still awaiting the facilitator. This is fail-closed: if the
    # facilitator then rejects (or the request later fails), the nonce stays
    # marked and that client cannot retry the same nonce — but a failed
    # verification returns 402 (no payment consumed), so burning the nonce is a
    # safe, preferable tradeoff over the race (the client re-pays with a fresh
    # nonce). See D-010.
    if nonce:
        nonce_cache.mark_used(nonce)

    # Facilitator /verify (authoritative on-chain authorization state).
    try:
        fac = await facilitator_verify(X402_FACILITATOR_URL, result.payload)
    except X402VerifyError as exc:
        logger.error("x402 v2 facilitator verify error: %s", exc)
        return None

    if not fac.is_valid:
        logger.info(
            "x402 v2 facilitator rejected payment: %s",
            fac.invalid_reason or fac.invalid_message or "unknown",
        )
        return None

    payer = fac.payer or result.payer
    logger.info("x402 v2 verified — payer=%s, nonce=%s", payer, nonce[:16] if nonce else "?")
    return {
        "x402_version": 2,
        "payment_payload": result.payload,
        "requirements": advertised,
        "payer": payer,
        "nonce": nonce,
    }


async def settle_x402_payment(ctx: dict) -> dict:
    """Settle payment AFTER successful scrape. Returns settlement result."""
    # v2 path: facilitator /settle with the v2 PaymentPayload.
    if ctx.get("x402_version") == 2:
        from x402_verify import facilitator_settle
        result = await facilitator_settle(
            X402_FACILITATOR_URL, ctx["payment_payload"]
        )
        if result.get("amount_usd") is None:
            result["amount_usd"] = X402_PRICE_USD
        return result

    # v1 path: legacy x402 v0.3.0 facilitator client.
    facilitator, _ = get_x402()
    try:
        result = await facilitator.settle(ctx["payment_payload"], ctx["requirements"])
        logger.info("x402 settled — tx=%s, success=%s", result.transaction, result.success)
        return {
            "settled": result.success,
            "amount_usd": X402_PRICE_USD,
            "transaction": result.transaction,
            "network": result.network,
            "payer": result.payer,
            "error": result.error_reason,
        }
    except Exception as exc:
        logger.error("x402 settlement failed: %s", exc)
        return {"settled": False, "error": str(exc), "amount_usd": X402_PRICE_USD}





# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/v1/scrape", response_model=ScrapeResponse, dependencies=[Depends(authenticate)])
async def scrape_endpoint(
    req: ScrapeRequest,
    auth: AuthResult = Depends(authenticate),
):
    """
    Scrape a URL with stealth browser automation.

    Dual auth: API key (subscription) OR x402 payment (pay-per-scrape).
    x402: payment verified BEFORE scrape, settled ONLY on success.
    Failed scrapes = no charge.

    Output formats (via ``output``): markdown (default), screenshot, pdf,
    csv, html.
    """
    url = str(req.url)
    start = time.monotonic()
    output = (req.output or "markdown").strip().lower()
    options = req.options or {}

    def _log_usage(status_code: int, success: bool, error_message: Optional[str] = None) -> None:
        """Record a usage event for this scrape (database API-key auth only).

        Fire-and-forget; never blocks the response. Skipped for the legacy
        env-var key and x402 paths, which have no key_id/user_id to attribute.
        """
        if not auth.key_id:
            return
        record_usage_event(
            key_id=auth.key_id,
            user_id=auth.user_id,
            endpoint="/v1/scrape",
            url=url,
            domain=extract_domain(url),
            status_code=status_code,
            success=success,
            response_time_ms=int((time.monotonic() - start) * 1000),
            error_message=error_message,
            auth_method="api_key",
        )

    if output not in VALID_OUTPUTS:
        raise HTTPException(status_code=400, detail={
            "error": "invalid_output",
            "message": f"output must be one of: {', '.join(sorted(VALID_OUTPUTS))}",
        })

    # Visual outputs need images/fonts/CSS to render correctly.
    block_media = False if output in ("screenshot", "pdf") else req.block_media

    async def execute(proxy_config, retry=req.retry):
        """Run one scrape/render pass against the given proxy (or None)."""
        if output == "markdown":
            title, markdown_content, char_count = await engine.scrape(
                url=url, wait_for_selector=req.wait_for_selector,
                timeout_ms=req.timeout_ms, block_media=block_media,
                proxy_url=req.proxy_url, proxy_config=proxy_config,
                wait_strategy=req.wait_strategy,
                retry=retry, bypass_cache=req.bypass_cache,
                javascript=req.javascript,
            )
            return {"title": title, "markdown": markdown_content, "character_count": char_count}

        renderer = getattr(output_formatter, OUTPUT_RENDERERS[output])
        return await engine.render(
            url=url, renderer=lambda page: renderer(page, options),
            wait_for_selector=req.wait_for_selector, timeout_ms=req.timeout_ms,
            block_media=block_media, proxy_url=req.proxy_url, proxy_config=proxy_config,
            wait_strategy=req.wait_strategy, retry=retry, javascript=req.javascript,
        )

    # Determine proxy
    proxy_config = None
    if should_use_proxy(url, direct_failed=False):
        proxy_config = get_proxy_dict(strategy="random")
        if proxy_config:
            logger.info(f"Using Webshare proxy for {url}")

    try:
        result = await execute(proxy_config)
    except TimeoutError as exc:
        # On timeout, try with proxy if not already using one
        if not proxy_config and should_use_proxy(url, direct_failed=True):
            proxy_config = get_proxy_dict(strategy="random")
            if proxy_config:
                logger.info(f"Retrying with Webshare proxy for {url}")
                try:
                    result = await execute(proxy_config, retry=False)
                except Exception as exc2:
                    if auth.is_x402:
                        logger.warning("Scrape timeout with proxy — NOT settling for %s", url)
                    _log_usage(504, False, str(exc2))
                    raise HTTPException(status_code=504, detail={
                        "error": "timeout", "message": str(exc2), "url": url, "payment_charged": False,
                    })
        if auth.is_x402:
            logger.warning("Scrape timeout — NOT settling for %s", url)
        _log_usage(504, False, str(exc))
        raise HTTPException(status_code=504, detail={
            "error": "timeout", "message": str(exc), "url": url, "payment_charged": False,
        })
    except ConnectionError as exc:
        # On connection error, try with proxy if not already using one
        if not proxy_config and should_use_proxy(url, direct_failed=True):
            proxy_config = get_proxy_dict(strategy="random")
            if proxy_config:
                logger.info(f"Retrying with Webshare proxy for {url}")
                try:
                    result = await execute(proxy_config, retry=False)
                except Exception as exc2:
                    if auth.is_x402:
                        logger.warning("Scrape connection failed with proxy — NOT settling for %s", url)
                    _log_usage(400, False, str(exc2))
                    raise HTTPException(status_code=400, detail={
                        "error": "connection_failed", "message": str(exc2), "url": url, "payment_charged": False,
                    })
        if auth.is_x402:
            logger.warning("Scrape connection failed — NOT settling for %s", url)
        _log_usage(400, False, str(exc))
        raise HTTPException(status_code=400, detail={
            "error": "connection_failed", "message": str(exc), "url": url, "payment_charged": False,
        })
    except Exception as exc:
        if auth.is_x402:
            logger.warning("Scrape failed — NOT settling for %s", url)
        error_msg = str(exc).lower()
        if any(kw in error_msg for kw in ["ns_error_unknown_host", "err_name", "net::err", "name_not_resolved"]):
            _log_usage(400, False, str(exc))
            raise HTTPException(status_code=400, detail={
                "error": "connection_failed", "message": str(exc), "url": url, "payment_charged": False,
            })
        _log_usage(500, False, str(exc))
        raise HTTPException(status_code=500, detail={
            "error": "scrape_failed", "message": str(exc), "url": url, "payment_charged": False,
        })

    # Scrape succeeded — settle x402 payment (only for x402 auth)
    payment_result = None
    if auth.is_x402 and auth.x402_context:
        payment_result = await settle_x402_payment(auth.x402_context)

    if output == "markdown":
        data = ScrapeData(
            title=result["title"], markdown=result["markdown"],
            character_count=result["character_count"],
        )
    else:
        data = result

    elapsed_ms = int((time.monotonic() - start) * 1000)
    _log_usage(200, True)
    return ScrapeResponse(
        status=200, url=url, output=output, data=data,
        execution_time_ms=elapsed_ms, payment=payment_result,
    )


@app.get("/v1/scrape", dependencies=[Depends(authenticate)])
async def scrape_endpoint_get(request: Request):
    """
    GET /v1/scrape — x402 discovery probe support (CDP Bazaar validator).

    The CDP x402 Bazaar validator crawls each resource URL with a plain GET to
    confirm it returns HTTP 402 with a valid ``extensions.bazaar`` block
    (``returns_402`` / ``has_bazaar_extension`` preflight checks). POST is the
    only method that actually fulfills a scrape (it carries the request body),
    so an authenticated GET receives 405 rather than scraping.

    Auth flows through the same ``authenticate`` dependency as POST:
    unauthenticated probes → 402 + payment requirements envelope (with the
    Bazaar extension served by the 402 exception handler); valid API key or
    x402 payment → 405 (method not allowed).
    """
    raise HTTPException(status_code=405, detail={
        "error": "method_not_allowed",
        "message": "Use POST /v1/scrape (GET exists for x402 payment discovery).",
    })


@app.get("/health", response_model=HealthResponse)
async def health_endpoint():
    return HealthResponse(
        status="ok", browser_active=engine.camoufox is not None,
        browser_contexts=engine.context_count, uptime_seconds=round(engine.uptime, 1),
        x402_enabled=X402_ENABLED,
    )


@app.get("/v1/proxy/status")
async def proxy_status_endpoint():
    """Get proxy pool status."""
    return get_proxy_stats()


@app.get("/v1/pricing")
async def pricing_endpoint():
    """Public endpoint — returns current pricing and payment requirements."""
    if not X402_ENABLED:
        return {"x402_enabled": False, "message": "Payment not required (free tier)"}
    return {
        "x402_enabled": True, "price_usd": X402_PRICE_USD,
        "token": "***", "token_address": X402_USDC_BASE,
        "network": X402_NETWORK, "pay_to": X402_RECIPIENT_WALLET,
        "facilitator": X402_FACILITATOR_URL,
        "payment_header": "X-PAYMENT",
        "description": "Send base64-encoded EIP-3009 signed permit in X-PAYMENT header",
    }


# ---------------------------------------------------------------------------
# Structured extraction routes (Phase 2 WS1)
#
# Imported here (after `authenticate`, `engine`, `AuthResult`, and the
# settlement helpers are defined) so routes_extract.py can import them without
# a circular-import problem.
# ---------------------------------------------------------------------------
from routes_extract import router as extract_router
app.include_router(extract_router)


# ---------------------------------------------------------------------------
# Multi-page crawl routes (Phase 2 WS2)
# ---------------------------------------------------------------------------
from routes_crawl import router as crawl_router
app.include_router(crawl_router)


# ---------------------------------------------------------------------------
# Webhook callback routes (Phase 2 WS3)
# ---------------------------------------------------------------------------
from routes_webhooks import router as webhooks_router
app.include_router(webhooks_router)


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    logger.info("Starting on %s:%d (auth=%s, x402=%s, rate_limit=%d rpm)",
                HOST, PORT, "enabled" if API_KEY else "disabled",
                "enabled" if X402_ENABLED else "disabled", RATE_LIMIT_RPM)

    # Use Gunicorn in production (multi-worker)
    use_gunicorn = os.getenv("USE_GUNICORN", "false").lower() == "true"
    if use_gunicorn:
        try:
            import gunicorn.app.base

            class StandaloneApplication(gunicorn.app.base.BaseApplication):
                def __init__(self, app, options=None):
                    self.options = options or {}
                    self.application = app
                    super().__init__()

                def load_config(self):
                    for key, value in self.options.items():
                        if key in self.cfg.settings and value is not None:
                            self.cfg.set(key.lower(), value)

                def load(self):
                    return self.application

            options = {
                "bind": f"{HOST}:{PORT}",
                "workers": int(os.getenv("GUNICORN_WORKERS", "4")),
                "worker_class": "uvicorn.workers.UvicornWorker",
                "timeout": 120,
                "graceful_timeout": 30,
                "accesslog": "-",
                "errorlog": "-",
                "loglevel": LOG_LEVEL.lower(),
            }
            logger.info("Starting with Gunicorn (workers=%d)", options["workers"])
            StandaloneApplication(app, options).run()
        except ImportError:
            logger.warning("Gunicorn not installed, falling back to Uvicorn")
            uvicorn.run("server:app", host=HOST, port=PORT, log_level=LOG_LEVEL.lower(), reload=False)
    else:
        uvicorn.run("server:app", host=HOST, port=PORT, log_level=LOG_LEVEL.lower(), reload=False)
