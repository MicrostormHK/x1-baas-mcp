"""
Crawl engine — multi-page crawling for Phase 2 Work Stream 2.

Four crawl modes:
  * sitemap  — parse sitemap.xml (and sitemap indexes) then crawl each URL
  * link     — follow in-page links to a given depth
  * pattern  — follow links whose URL matches a glob/regex pattern
  * batch    — crawl a fixed list of URLs (optionally in parallel)

The engine is intentionally transport-agnostic: it uses httpx (async) for
fetching pages, sitemaps, and robots.txt. A `httpx.AsyncClient` can be injected
at construction time so the engine can be unit-tested with `httpx.MockTransport`
and no live network access.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from typing import AsyncIterator, Optional
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify as md

logger = logging.getLogger("baas-crawl")

USER_AGENT = "X1-BaaS-Crawler/0.2 (+https://baas.microstorm.biz/bot)"

# Tags removed before HTML -> Markdown conversion (mirrors the scrape pipeline).
_STRIP_TAGS = [
    "script", "style", "nav", "footer", "aside", "svg", "iframe",
    "noscript", "header", "form", "button", "input", "select",
    "textarea", "dialog", "menu", "menuitem",
]

# Per-domain robots.txt cache. Value: (RobotFileParser, crawl_delay_seconds).
_robots_cache: dict[str, tuple[RobotFileParser, float]] = {}
_robots_lock = asyncio.Lock()


def _normalize_pattern(pattern: str) -> re.Pattern:
    """Compile a URL pattern that may be a glob (``/blog/*``) or a regex.

    If the pattern contains regex metacharacters it is used as a regex verbatim;
    otherwise ``*`` is treated as a glob wildcard.
    """
    pattern = (pattern or "").strip()
    if not pattern:
        return re.compile(".*")
    # Heuristic: if it looks like a regex, use it directly.
    if re.search(r"[\[\]()\\+^$|{}]", pattern):
        return re.compile(pattern)
    # Otherwise translate glob '*' to '.*'
    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in pattern)
    return re.compile(regex)


def _matches_pattern(url: str, pattern: re.Pattern) -> bool:
    """Match a pattern against both the full URL and the URL path."""
    path = urlparse(url).path or "/"
    return bool(pattern.search(url) or pattern.search(path))


def extract_links(html: str, base_url: str) -> list[str]:
    """Extract absolute, http(s) links from an HTML document."""
    soup = BeautifulSoup(html, "lxml")
    links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        links.append(absolute)
    return links


def _html_to_markdown(html: str, url: str = "") -> tuple[str, str]:
    """Convert raw HTML to (title, markdown)."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(_STRIP_TAGS):
        tag.decompose()

    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else ""
    markdown_content = md(
        str(soup), heading_style="ATX", bullets="-",
        strip=["img", "video", "audio", "picture"],
    ).strip()
    markdown_content = re.sub(r"\n{4,}", "\n\n\n", markdown_content)
    markdown_content = re.sub(r"[ \t]+\n", "\n", markdown_content)
    return title, markdown_content


class CrawlEngine:
    """Async multi-page crawl engine."""

    def __init__(self, timeout: float = 30.0, client: Optional[httpx.AsyncClient] = None) -> None:
        self.timeout = timeout
        self._client = client
        self._owns_client = client is None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            )
            self._owns_client = True
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # HTTP primitives
    # ------------------------------------------------------------------
    async def fetch(self, url: str) -> httpx.Response:
        """Fetch a URL and return the httpx response."""
        client = await self._get_client()
        return await client.get(url)

    async def fetch_text(self, url: str) -> str:
        """Fetch a URL and return its decoded text (raises on non-2xx)."""
        resp = await self.fetch(url)
        resp.raise_for_status()
        return resp.text

    # ------------------------------------------------------------------
    # robots.txt
    # ------------------------------------------------------------------
    def _parse_robots(self, text: str) -> tuple[RobotFileParser, float]:
        rp = RobotFileParser()
        rp.parse(text.splitlines())
        delay = 0.0
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.lower().startswith("crawl-delay"):
                match = re.match(r"(?i)^crawl-delay\s*:\s*([0-9.]+)", line)
                if match:
                    try:
                        delay = float(match.group(1))
                    except ValueError:
                        pass
        return rp, delay

    async def _robots_for(self, url: str) -> tuple[RobotFileParser, float]:
        """Fetch + parse robots.txt for a URL's domain (cached)."""
        parsed = urlparse(url)
        domain = f"{parsed.scheme}://{parsed.netloc}"
        if domain in _robots_cache:
            return _robots_cache[domain]

        robots_url = urljoin(domain + "/", "robots.txt")
        rp = RobotFileParser()
        rp.parse([])  # empty => allow all
        delay = 0.0
        try:
            resp = await self.fetch(robots_url)
            if resp.status_code == 200:
                rp, delay = self._parse_robots(resp.text)
        except Exception as exc:
            logger.debug("robots.txt fetch failed for %s: %s", domain, exc)

        async with _robots_lock:
            _robots_cache[domain] = (rp, delay)
        return rp, delay

    async def is_allowed(self, url: str, respect_robots: bool = True) -> bool:
        """Return True if the URL may be crawled per robots.txt."""
        if not respect_robots:
            return True
        rp, _ = await self._robots_for(url)
        try:
            return rp.can_fetch(USER_AGENT, url)
        except Exception:
            return True

    async def crawl_delay_seconds(self, url: str) -> float:
        """Return the robots.txt crawl-delay (seconds) for a domain, if any."""
        _, delay = await self._robots_for(url)
        return delay

    # ------------------------------------------------------------------
    # Discovery modes
    # ------------------------------------------------------------------
    async def _resolve_sitemap_url(self, start_url: str) -> str:
        """Resolve a likely sitemap.xml URL from a start URL."""
        parsed = urlparse(start_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path or ""

        # Start URL is already a sitemap.
        if path.rstrip("/").endswith(".xml") or "sitemap" in path.lower():
            return start_url

        candidates = [urljoin(base + "/", "sitemap.xml")]

        # Prefer Sitemap: directives from robots.txt, if present.
        try:
            robots_text = await self.fetch_text(urljoin(base + "/", "robots.txt"))
            for line in robots_text.splitlines():
                if line.lower().startswith("sitemap:"):
                    sitemap_url = line.split(":", 1)[1].strip()
                    if sitemap_url:
                        candidates.insert(0, sitemap_url)
        except Exception:
            pass

        # Probe candidates and return the first that responds 200.
        for candidate in candidates:
            try:
                resp = await self.fetch(candidate)
                if resp.status_code == 200:
                    return candidate
            except Exception:
                continue
        return candidates[0]

    async def _parse_sitemap(self, sitemap_url: str, max_pages: int,
                             out: list[str], visited: set[str]) -> None:
        """Parse a sitemap (or sitemap index) and append <loc> URLs to `out`."""
        if sitemap_url in visited or len(out) >= max_pages:
            return
        visited.add(sitemap_url)

        resp = await self.fetch(sitemap_url)
        if resp.status_code != 200:
            return

        try:
            root = BeautifulSoup(resp.text, "xml")
        except Exception:
            # Fall back to lxml XML parsing via element search.
            root = BeautifulSoup(resp.text, "lxml-xml")

        root_tag = root.find(True)
        if root_tag is None:
            return
        kind = root_tag.name.lower()

        if kind == "sitemapindex":
            for loc in root.find_all("loc"):
                url = (loc.get_text() or "").strip()
                if url:
                    await self._parse_sitemap(url, max_pages, out, visited)
                    if len(out) >= max_pages:
                        return
        else:
            for loc in root.find_all("loc"):
                url = (loc.get_text() or "").strip()
                if url and url not in out and len(out) < max_pages:
                    out.append(url)
                    if len(out) >= max_pages:
                        return

    async def discover_sitemap(self, start_url: str, max_pages: int = 100) -> list[str]:
        """Discover URLs from a sitemap (or sitemap index)."""
        sitemap_url = await self._resolve_sitemap_url(start_url)
        out: list[str] = []
        visited: set[str] = set()
        await self._parse_sitemap(sitemap_url, max_pages, out, visited)
        return out[:max_pages]

    async def discover_links(
        self,
        start_url: str,
        max_pages: int = 100,
        depth: int = 3,
        follow_external: bool = False,
        include_patterns: Optional[list[str]] = None,
        exclude_patterns: Optional[list[str]] = None,
    ) -> list[str]:
        """BFS link discovery up to `depth` hops."""
        base_host = urlparse(start_url).netloc
        includes = [_normalize_pattern(p) for p in (include_patterns or [])]
        excludes = [_normalize_pattern(p) for p in (exclude_patterns or [])]

        visited: set[str] = set()
        queued: set[str] = set()
        queue: deque[tuple[str, int]] = deque([(start_url, 0)])
        queued.add(start_url)
        out: list[str] = []

        while queue and len(out) < max_pages:
            url, current_depth = queue.popleft()
            queued.discard(url)
            if url in visited:
                continue
            visited.add(url)
            out.append(url)

            if current_depth >= depth:
                continue

            try:
                html = await self.fetch_text(url)
            except Exception as exc:
                logger.debug("link discovery failed for %s: %s", url, exc)
                continue

            for link in extract_links(html, url):
                if link in visited or link in queued:
                    continue
                if not follow_external and urlparse(link).netloc != base_host:
                    continue
                if excludes and any(_matches_pattern(link, p) for p in excludes):
                    continue
                if includes and not any(_matches_pattern(link, p) for p in includes):
                    continue
                if len(out) + len(queue) < max_pages:
                    queue.append((link, current_depth + 1))
                    queued.add(link)

        return out[:max_pages]

    async def discover_pattern(
        self,
        start_url: str,
        url_pattern: str,
        max_pages: int = 100,
    ) -> list[str]:
        """BFS discovery following only links that match `url_pattern`."""
        pattern = _normalize_pattern(url_pattern)
        visited: set[str] = set()
        queued: set[str] = set()
        queue: deque[str] = deque([start_url])
        queued.add(start_url)
        out: list[str] = []

        while queue and len(out) < max_pages:
            url = queue.popleft()
            queued.discard(url)
            if url in visited:
                continue
            visited.add(url)

            # The start URL is always included (seed), discovered links must match.
            if url == start_url or _matches_pattern(url, pattern):
                out.append(url)

            try:
                html = await self.fetch_text(url)
            except Exception as exc:
                logger.debug("pattern discovery failed for %s: %s", url, exc)
                continue

            for link in extract_links(html, url):
                if link in visited or link in queued:
                    continue
                if not _matches_pattern(link, pattern):
                    continue
                if len(out) < max_pages:
                    queue.append(link)
                    queued.add(link)

        return out[:max_pages]

    # ------------------------------------------------------------------
    # Content crawling
    # ------------------------------------------------------------------
    async def crawl_url(self, url: str) -> dict:
        """Fetch a single page and convert it to Markdown.

        Returns a dict with keys: status, markdown, metadata, error.
        Never raises — errors are captured in the returned dict.
        """
        try:
            resp = await self.fetch(url)
            status = resp.status_code
            content_type = (resp.headers.get("content-type") or "").lower()

            if status >= 400:
                return {"status": status, "markdown": None,
                        "metadata": {"content_type": content_type},
                        "error": f"HTTP {status}"}

            # Only convert textual pages to Markdown; treat binaries as fetched.
            if "html" in content_type or "text" in content_type or not content_type:
                title, markdown_content = _html_to_markdown(resp.text, url)
                return {
                    "status": status,
                    "markdown": markdown_content,
                    "metadata": {"title": title, "char_count": len(markdown_content),
                                 "content_type": content_type},
                    "error": None,
                }

            return {"status": status, "markdown": None,
                    "metadata": {"content_type": content_type},
                    "error": None}
        except Exception as exc:
            return {"status": None, "markdown": None, "metadata": {},
                    "error": str(exc)}

    async def crawl_batch(self, urls: list[str], max_concurrent: int = 5) -> list[dict]:
        """Crawl a fixed list of URLs with bounded concurrency."""
        sem = asyncio.Semaphore(max(1, max_concurrent))

        async def _one(url: str) -> dict:
            async with sem:
                return await self.crawl_url(url)

        return await asyncio.gather(*(_one(u) for u in urls))

    async def crawl_many(
        self,
        urls: list[str],
        respect_robots: bool = True,
        delay_ms: int = 0,
        should_cancel=None,
    ) -> AsyncIterator[dict]:
        """Sequentially crawl `urls`, respecting robots.txt + delay.

        Yields one result dict per URL. `should_cancel`, if provided, is an
        awaitable/callable returning True when the crawl should stop early.
        """
        for url in urls:
            if should_cancel is not None and await should_cancel():
                return
            if not await self.is_allowed(url, respect_robots):
                logger.info("Skipping %s (disallowed by robots.txt)", url)
                yield {"url": url, "skipped": "robots.txt", "status": None,
                       "markdown": None, "metadata": {}, "error": None}
                continue

            if delay_ms > 0:
                await asyncio.sleep(delay_ms / 1000)

            result = await self.crawl_url(url)
            result["url"] = url
            yield result
