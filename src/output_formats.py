"""
Output formatters — Phase 2 Work Stream 5.

Additional output formats for the scrape endpoint, layered on top of a live
browser page:

  - screenshot  : base64-encoded PNG/JPEG (viewport or full-page)
  - pdf         : base64-encoded PDF (native page.pdf() when available, with a
                  screenshot→PDF fallback for Firefox-based engines like Camoufox)
  - csv         : tabular data extracted from <table> elements
  - clean html  : sanitized HTML with scripts/styles/ads removed

Each public method accepts a Playwright-compatible ``page`` and an ``options``
dict and returns a plain dict that is serialized directly into the API response.

This module is intentionally dependency-light and does not import from
``server`` (which imports this module) — no circular imports.
"""

from __future__ import annotations

import base64
import csv as csv_mod
import io
import logging
from typing import Optional

from bs4 import BeautifulSoup, Comment

logger = logging.getLogger("baas-output")

# Tags removed from "clean" HTML output.
STRIP_TAGS = {
    "script", "style", "nav", "footer", "aside", "svg", "iframe", "noscript",
    "header", "form", "button", "input", "select", "textarea", "dialog",
    "menu", "menuitem", "template", "canvas", "object", "embed", "picture",
    "source", "track",
}

# Attributes stripped from every element (event handlers + tracking).
STRIP_ATTRS = {
    "onclick", "onload", "onerror", "onmouseover", "onfocus", "onblur",
    "onchange", "onsubmit", "onkeydown", "onkeyup", "onkeypress",
    "data-track", "data-analytics", "data-ga", "data-gtm", "style",
}

# CSS selectors for ad / cookie-banner / social cruft removed from clean HTML.
AD_SELECTORS = [
    ".ad", ".ads", ".advert", ".advertisement", ".banner", ".popup",
    ".cookie-banner", ".cookie-consent", ".social-share", ".share-buttons",
    "#ad", "#ads", "[id*='advert']", "[class*='advert']",
    "[class*='ad-']", "[class*='ad_']", "[class*='sponsor']",
]


class OutputFormatter:
    """Turn a rendered browser page into one of several output formats."""

    # -- Screenshot ---------------------------------------------------------

    async def screenshot(self, page, options: Optional[dict] = None) -> dict:
        """Capture a PNG/JPEG screenshot of the page (viewport or full-page)."""
        options = options or {}
        fmt = str(options.get("format", "png")).lower()
        if fmt not in ("png", "jpeg"):
            fmt = "png"
        full_page = bool(options.get("full_page", True))

        # Optional explicit viewport dimensions.
        width = options.get("width")
        height = options.get("height")
        if width or height:
            try:
                await page.set_viewport_size({
                    "width": int(width or 1280),
                    "height": int(height or 720),
                })
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("set_viewport_size failed: %s", exc)

        screenshot_kwargs: dict = {"full_page": full_page, "type": fmt}
        if fmt == "jpeg":
            # JPEG does not support full-page capture.
            screenshot_kwargs["full_page"] = False
            screenshot_kwargs["quality"] = int(options.get("quality", 90))

        screenshot_bytes = await page.screenshot(**screenshot_kwargs)
        return {
            "data": base64.b64encode(screenshot_bytes).decode("ascii"),
            "format": fmt,
            "size_bytes": len(screenshot_bytes),
        }

    # -- PDF ------------------------------------------------------------------

    async def pdf(self, page, options: Optional[dict] = None) -> dict:
        """Generate a PDF from the page.

        Uses native ``page.pdf()`` when the underlying browser supports it
        (Chromium). Firefox-based engines (Camoufox) do not implement it, so we
        fall back to embedding a rendered screenshot as a single-page PDF.
        """
        options = options or {}
        try:
            pdf_bytes = await page.pdf(**self._pdf_options(options))
        except Exception as exc:  # noqa: BLE001 - any pdf() failure triggers fallback
            logger.warning(
                "page.pdf() unavailable (%s) — falling back to screenshot PDF", exc,
            )
            pdf_bytes = await self._pdf_from_screenshot(page, options)

        return {
            "data": base64.b64encode(pdf_bytes).decode("ascii"),
            "format": "pdf",
            "size_bytes": len(pdf_bytes),
        }

    def _pdf_options(self, options: dict) -> dict:
        """Whitelist and forward supported Playwright ``page.pdf`` options."""
        allowed = {
            "format", "landscape", "print_background", "scale",
            "prefer_css_page_size", "page_ranges", "header_template",
            "footer_template",
        }
        pdf_options = {k: options[k] for k in allowed if k in options}
        margin = options.get("margin")
        if isinstance(margin, dict):
            pdf_options["margin"] = margin
        return pdf_options

    async def _pdf_from_screenshot(self, page, options: dict) -> bytes:
        """Render a single-page PDF from a JPEG screenshot (pure stdlib)."""
        jpeg_bytes = await page.screenshot(
            type="jpeg",
            full_page=False,
            quality=int(options.get("quality", 90)),
        )
        return self._jpeg_to_pdf(jpeg_bytes)

    @staticmethod
    def _jpeg_dimensions(data: bytes) -> tuple[int, int]:
        """Parse a JPEG's SOF marker to recover pixel width/height."""
        if len(data) < 4 or data[:2] != b"\xff\xd8":
            return 0, 0
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in {
                0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
            }:
                height = int.from_bytes(data[i + 5:i + 7], "big")
                width = int.from_bytes(data[i + 7:i + 9], "big")
                return width, height
            segment_length = int.from_bytes(data[i + 2:i + 4], "big")
            i += 2 + segment_length
        return 0, 0

    @classmethod
    def _jpeg_to_pdf(cls, jpeg_bytes: bytes) -> bytes:
        """Embed a JPEG image as a minimal, valid single-page PDF (DCTDecode)."""
        width, height = cls._jpeg_dimensions(jpeg_bytes)
        if width <= 0 or height <= 0:
            raise ValueError("Unable to determine JPEG dimensions for PDF fallback")

        # Scale pixels → points (assume 96 dpi → 0.75 pt/px).
        scale = 72.0 / 96.0
        page_w = max(1, int(round(width * scale)))
        page_h = max(1, int(round(height * scale)))

        def obj(body: bytes) -> bytes:
            return body

        catalog = obj(b"<< /Type /Catalog /Pages 2 0 R >>")
        pages = obj(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
        page = obj(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] "
            b"/Resources << /XObject << /Im1 4 0 R >> /ProcSet [/PDF /ImageC] >> "
            b"/Contents 5 0 R >>" % (page_w, page_h)
        )
        image = obj(
            b"<< /Type /XObject /Subtype /Image /Width %d /Height %d "
            b"/ColorSpace /DeviceRGB /BitsPerComponent 8 "
            b"/Filter /DCTDecode /Length %d >>\nstream\n" % (width, height, len(jpeg_bytes))
            + jpeg_bytes
            + b"\nendstream"
        )
        content = (
            "q\n%d 0 0 %d 0 0 cm\n/Im1 Do\nQ\n" % (page_w, page_h)
        ).encode("ascii")
        content_stream = obj(
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"
        )

        objects = [catalog, pages, page, image, content_stream]
        out = io.BytesIO()
        out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets: list[int] = []
        for idx, body in enumerate(objects, start=1):
            offsets.append(out.tell())
            out.write(b"%d 0 obj\n" % idx)
            out.write(body)
            out.write(b"\nendobj\n")

        xref_pos = out.tell()
        count = len(objects) + 1
        out.write(b"xref\n0 %d\n" % count)
        out.write(b"0000000000 65535 f \n")
        for off in offsets:
            out.write(b"%010d 00000 n \n" % off)
        out.write(
            b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (count, xref_pos)
        )
        return out.getvalue()

    # -- CSV -------------------------------------------------------------------

    async def csv(self, page, options: Optional[dict] = None) -> dict:
        """Extract ``<table>`` elements to CSV strings."""
        options = options or {}
        selector = options.get("table_selector", "table")
        raw_html = await page.content()
        soup = BeautifulSoup(raw_html, "lxml")

        tables: list[str] = []
        for table in soup.select(selector):
            csv_text = self._table_to_csv(table)
            if csv_text:
                tables.append(csv_text)

        return {
            "format": "csv",
            "tables": tables,
            "table_count": len(tables),
            "data": "\n\n".join(tables) if tables else "",
        }

    def _table_to_csv(self, table) -> str:
        """Convert one HTML table to a CSV string, honoring rowspan/colspan."""
        grid = self._table_to_grid(table)
        if not grid:
            return ""
        width = max(len(row) for row in grid)
        buf = io.StringIO()
        writer = csv_mod.writer(buf)
        for row in grid:
            writer.writerow(row + [""] * (width - len(row)))
        return buf.getvalue().rstrip("\r\n")

    def _table_to_grid(self, table) -> list[list[str]]:
        """Flatten an HTML table into a rectangular string grid."""
        trs = self._table_rows(table)
        if not trs:
            return []

        grid: list[list[Optional[str]]] = [[] for _ in trs]
        for row_index, tr in enumerate(trs):
            cells = tr.find_all(["th", "td"], recursive=False)
            col = 0
            for cell in cells:
                # Skip columns already occupied by a rowspan from above.
                row = grid[row_index]
                while col < len(row) and row[col] is not None:
                    col += 1

                text = " ".join(cell.get_text(" ", strip=True).split())
                try:
                    rowspan = max(1, int(cell.get("rowspan") or 1))
                    colspan = max(1, int(cell.get("colspan") or 1))
                except (TypeError, ValueError):
                    rowspan = colspan = 1

                for r_off in range(rowspan):
                    target_row = grid[row_index + r_off]
                    while len(target_row) < col + colspan:
                        target_row.append(None)
                    for c_off in range(colspan):
                        target_row[col + c_off] = text if c_off == 0 else ""

                col += colspan

        # None should never remain, but guard defensively.
        return [["" if c is None else c for c in row] for row in grid]

    @staticmethod
    def _table_rows(table) -> list:
        """Collect direct ``<tr>`` elements, respecting thead/tbody/tfoot."""
        trs: list = []
        for section in table.find_all(["thead", "tbody", "tfoot"], recursive=False):
            trs.extend(section.find_all("tr", recursive=False))
        trs.extend(table.find_all("tr", recursive=False))
        if not trs:
            # Malformed tables with no section wrapper — accept any descendant rows.
            trs = table.find_all("tr")
        return trs

    # -- Clean HTML -------------------------------------------------------------

    async def clean_html(self, page, options: Optional[dict] = None) -> dict:
        """Return sanitized HTML with scripts, styles, ads, and trackers removed."""
        options = options or {}
        raw_html = await page.content()
        html = self._sanitize(raw_html, options)
        return {
            "html": html,
            "format": "html",
            "size_bytes": len(html.encode("utf-8")),
        }

    def _sanitize(self, raw_html: str, options: dict) -> str:
        soup = BeautifulSoup(raw_html, "lxml")

        # 1. Drop comments.
        for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
            comment.extract()

        # 2. Drop noisy/unsafe tags.
        for tag_name in STRIP_TAGS:
            for tag in soup.find_all(tag_name):
                tag.decompose()

        # 3. Drop ad / social / cookie-banner cruft.
        for selector in AD_SELECTORS:
            for tag in soup.select(selector):
                tag.decompose()

        # 4. Strip unsafe attributes + javascript:/data: URLs.
        for tag in soup.find_all(True):
            for attr in list(tag.attrs):
                value = tag.attrs[attr]
                if attr in STRIP_ATTRS or attr.startswith("on"):
                    del tag.attrs[attr]
                elif attr == "href" and str(value).strip().lower().startswith("javascript:"):
                    del tag.attrs[attr]
                elif attr == "src" and str(value).strip().lower().startswith("javascript:"):
                    del tag.attrs[attr]

        return str(soup)
