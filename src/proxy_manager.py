"""Webshare proxy integration for BaaS engine."""

import os
import random
import logging
import urllib.request
import urllib.error
import ssl
import json
from typing import Optional

logger = logging.getLogger("baas-engine")

# Configuration
WEBSHARE_API_KEY = os.getenv("WEBSHARE_API_KEY", "")
PROXY_ENABLED = os.getenv("PROXY_ENABLED", "false").lower() == "true"
PROXY_STRATEGY = os.getenv("PROXY_STRATEGY", "direct_first")  # direct_first, always, never

# Proxy pool
_proxy_pool = []
_proxy_last_fetched = 0
_PROXY_CACHE_SECONDS = 300  # Refresh every 5 minutes


def fetch_proxies() -> list[dict]:
    """Fetch proxy list from Webshare API."""
    global _proxy_pool, _proxy_last_fetched
    
    if not WEBSHARE_API_KEY:
        return []
    
    # Check cache
    import time
    if _proxy_pool and (time.time() - _proxy_last_fetched) < _PROXY_CACHE_SECONDS:
        return _proxy_pool
    
    try:
        url = "https://proxy.webshare.io/api/v2/proxy/list/?mode=direct"
        req = urllib.request.Request(url, headers={"Authorization": f"Token {WEBSHARE_API_KEY}"})
        ctx = ssl.create_default_context()
        
        with urllib.request.urlopen(req, context=ctx, timeout=10) as response:
            data = json.loads(response.read().decode())
            _proxy_pool = data.get("results", [])
            _proxy_last_fetched = time.time()
            logger.info(f"Webshare: fetched {len(_proxy_pool)} proxies")
            return _proxy_pool
    except Exception as e:
        logger.error(f"Webshare: failed to fetch proxies: {e}")
        return _proxy_pool  # Return cached if available


def get_proxy_url(proxy: dict) -> str:
    """Convert proxy dict to URL string."""
    return f"http://{proxy['username']}:{proxy['password']}@{proxy['proxy_address']}:{proxy['port']}"


def select_proxy(strategy: str = "random") -> Optional[dict]:
    """Select a proxy from the pool."""
    proxies = fetch_proxies()
    
    if not proxies:
        return None
    
    if strategy == "random":
        return random.choice(proxies)
    elif strategy == "round_robin":
        # Simple round-robin based on current time
        import time
        idx = int(time.time()) % len(proxies)
        return proxies[idx]
    else:
        return proxies[0]


def get_proxy_dict(strategy: str = "random") -> Optional[dict]:
    """Get proxy as Camoufox-compatible dict."""
    proxy = select_proxy(strategy)
    if not proxy:
        return None
    
    return {
        "server": f"http://{proxy['proxy_address']}:{proxy['port']}",
        "username": proxy['username'],
        "password": proxy['password']
    }


def should_use_proxy(url: str, direct_failed: bool = False) -> bool:
    """Determine if proxy should be used based on strategy."""
    if not PROXY_ENABLED:
        return False
    
    if PROXY_STRATEGY == "never":
        return False
    
    if PROXY_STRATEGY == "always":
        return True
    
    # direct_first: use proxy only if direct failed
    if PROXY_STRATEGY == "direct_first":
        return direct_failed
    
    return False


def get_proxy_stats() -> dict:
    """Get proxy pool statistics."""
    proxies = fetch_proxies()
    
    countries = {}
    for p in proxies:
        cc = p.get("country_code", "unknown")
        countries[cc] = countries.get(cc, 0) + 1
    
    return {
        "enabled": PROXY_ENABLED,
        "strategy": PROXY_STRATEGY,
        "total_proxies": len(proxies),
        "countries": countries,
        "has_api_key": bool(WEBSHARE_API_KEY)
    }
