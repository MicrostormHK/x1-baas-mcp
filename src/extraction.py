"""
Structured extraction engine for the BaaS platform.

Pure functions operating on raw HTML strings — no FastAPI, no browser, no I/O.
This keeps the extraction logic fully unit-testable and reusable by both the
HTTP endpoint (`routes_extract.py`) and future crawl/webhook pipelines.

Supported extraction methods:
  1. Schema-based  — JSON schema + heuristics (JSON-LD, meta tags, semantic selectors)
  2. CSS selector  — explicit CSS selectors (flat or nested list format)
  3. XPath         — explicit XPath 1.0 queries (text, attribute, element results)

Each method returns `(data, confidence)` where `confidence` is a 0.0–1.0 score.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from bs4 import BeautifulSoup
from lxml import html as lxml_html

__all__ = [
    "extract_from_html",
    "extract_by_schema",
    "extract_by_selectors",
    "extract_by_xpaths",
    "coerce_value",
    "coerce_field",
]


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def _normalize(text: Any) -> str:
    """Normalize a field name into lowercase, space-separated tokens.

    Handles snake_case, kebab-case, and camelCase boundaries.
    """
    s = str(text)
    # Split camelCase boundaries ("productTitle" -> "product Title")
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)
    s = re.sub(r"[^a-zA-Z0-9]+", " ", s)
    return s.strip().lower()


# ---------------------------------------------------------------------------
# Field alias tables (keyed by normalized field name)
# ---------------------------------------------------------------------------

# JSON-LD / attribute / generic matching aliases.
FIELD_ALIASES: dict[str, list[str]] = {
    "title": ["title", "name", "headline"],
    "name": ["name", "title", "headline"],
    "headline": ["headline", "title", "name"],
    "price": ["price", "cost", "amount", "lowprice", "highprice", "priceamount"],
    "cost": ["cost", "price", "amount"],
    "amount": ["amount", "price", "cost"],
    "description": ["description", "summary", "abstract", "details", "body"],
    "summary": ["summary", "description", "abstract"],
    "image": ["image", "images", "thumbnail", "thumbnailurl", "imageurl", "photo", "photos"],
    "images": ["images", "image", "photos"],
    "url": ["url", "link", "href", "canonical"],
    "link": ["link", "url", "href"],
    "author": ["author", "creator", "writer", "byline"],
    "creator": ["creator", "author"],
    "date": ["date", "datepublished", "datecreated", "publisheddate", "pubdate", "dateposted"],
    "published": ["datepublished", "publisheddate", "pubdate", "date"],
    "sku": ["sku", "productid", "product_id", "itemid", "itemnumber"],
    "brand": ["brand", "manufacturer", "make"],
    "rating": ["rating", "ratingvalue", "score", "reviewrating", "ratingvalue"],
    "category": ["category", "genre", "type"],
    "availability": ["availability", "stock", "instock", "in_stock", "availabilitystatus"],
    "stock": ["stock", "availability", "instock", "in_stock"],
    "keywords": ["keywords", "tags", "keyw"],
    "tags": ["tags", "keywords"],
    "language": ["language", "lang", "inlanguage"],
    "duration": ["duration", "time"],
    "size": ["size", "dimensions"],
    "color": ["color", "colour"],
    "weight": ["weight"],
}

# Meta tag key aliases (lowercase, colon-preserved — matched against
# meta[name]/meta[property]/meta[itemprop] keys).
META_ALIASES: dict[str, list[str]] = {
    "title": ["og:title", "twitter:title", "title", "headline", "dc.title"],
    "description": ["og:description", "twitter:description", "description", "abstract", "dc.description"],
    "image": ["og:image", "twitter:image", "twitter:image:src", "image"],
    "images": ["og:image", "twitter:image", "twitter:image:src", "image"],
    "url": ["og:url", "twitter:url", "canonical", "url"],
    "author": ["author", "og:article:author", "twitter:creator", "dc.creator"],
    "date": ["article:published_time", "article:modified_time", "dc.date", "date"],
    "published": ["article:published_time", "date", "dc.date"],
    "price": ["price", "og:price:amount", "product:price:amount", "twitter:data1"],
    "keywords": ["keywords", "news_keywords"],
    "language": ["og:locale", "language", "dc.language"],
    "rating": ["rating", "ratingvalue"],
}

# Semantic CSS selectors to try (most-specific first).
SEMANTIC_SELECTORS: dict[str, list[str]] = {
    "title": [
        "h1",
        "[itemprop='name']",
        "[itemprop='headline']",
        ".product-title",
        ".entry-title",
        ".post-title",
        ".page-title",
        "#title",
        ".title",
    ],
    "name": ["h1", "[itemprop='name']", ".product-name", ".name", "#name"],
    "price": [
        "[itemprop='price']",
        "meta[itemprop='price']",
        "[data-price]",
        ".price-value",
        ".product-price",
        ".price",
        "#price",
        "[class*='price']",
    ],
    "description": [
        "[itemprop='description']",
        ".product-description",
        ".description",
        "#description",
        "meta[name='description']",
        "[class*='description']",
    ],
    "image": ["[itemprop='image']", "img.featured", ".product-image img", "img"],
    "images": ["[itemprop='image']", ".product-image img", "img"],
    "author": ["[itemprop='author']", ".author", "[rel='author']", "a.author", ".byline"],
    "date": ["[itemprop='datePublished']", "time[datetime]", ".date", ".published-date", "[datetime]"],
    "url": ["[itemprop='url']", "link[rel='canonical']", "a.permalink"],
    "rating": ["[itemprop='ratingValue']", ".rating", ".rating-value", "[class*='rating']"],
    "sku": ["[itemprop='sku']", ".sku", "[data-sku]", "[class*='sku']"],
    "brand": ["[itemprop='brand']", ".brand", "[class*='brand']"],
    "category": ["[itemprop='category']", ".category", "a.category", "[class*='category']"],
    "availability": ["[itemprop='availability']", ".availability", ".stock", "[class*='availability']"],
    "keywords": ["meta[name='keywords']", "[itemprop='keywords']", ".keywords"],
    "language": ["[itemprop='inLanguage']", "html[lang]", "[lang]"],
}


def _field_keys(field_norm: str) -> list[str]:
    """Return candidate lookup keys for a normalized field name.

    Includes the full normalized name plus any individual word that has a
    known alias table entry (so "product_title" / "productTitle" still map to
    "title" without matching generic words like "product").
    """
    keys = [field_norm]
    for word in field_norm.split():
        if word in FIELD_ALIASES and word not in keys:
            keys.append(word)
    return keys


# ---------------------------------------------------------------------------
# Type coercion
# ---------------------------------------------------------------------------

_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def coerce_value(value: Any, target_type: str = "string") -> Any:
    """Coerce a raw extracted value to a schema primitive type."""
    if value is None:
        return None

    if target_type == "string":
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (dict, list)):
            return value
        return str(value)

    if target_type == "number" or target_type == "float":
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip().replace(",", "")
        for sym in ("$", "€", "£", "¥", "₹", "usd", "eur", "gbp"):
            text = text.replace(sym, "")
        match = _NUMBER_RE.search(text)
        return float(match.group()) if match else None

    if target_type == "integer" or target_type == "int":
        num = coerce_value(value, "number")
        return int(num) if num is not None else None

    if target_type == "boolean" or target_type == "bool":
        if isinstance(value, bool):
            return value
        t = str(value).strip().lower()
        if t in ("true", "yes", "y", "1", "on", "available", "in stock", "instock"):
            return True
        if t in ("false", "no", "n", "0", "off", "unavailable", "out of stock", "outofstock"):
            return False
        return None

    if target_type == "null":
        return None

    if target_type == "array":
        if isinstance(value, list):
            return value
        if isinstance(value, tuple):
            return list(value)
        if value == "":
            return []
        return [value]

    if target_type == "object":
        if isinstance(value, dict):
            return value
        return {"value": value}

    # Unknown/fallback — return as-is (strings stripped).
    if isinstance(value, str):
        return value.strip()
    return value


def coerce_field(value: Any, prop_schema: Optional[dict]) -> Any:
    """Coerce a value using a full JSON-schema property definition."""
    if not isinstance(prop_schema, dict):
        return value
    target_type = prop_schema.get("type", "string")
    if target_type == "array":
        items = prop_schema.get("items", {})
        items_type = items.get("type", "string") if isinstance(items, dict) else "string"
        if isinstance(value, list):
            return [coerce_value(v, items_type) for v in value]
        if isinstance(value, tuple):
            return [coerce_value(v, items_type) for v in value]
        if value in (None, ""):
            return []
        return [coerce_value(value, items_type)]
    return coerce_value(value, target_type)


# ---------------------------------------------------------------------------
# Schema-based extraction
# ---------------------------------------------------------------------------

def _iter_jsonld_objects(data: Any):
    """Yield every dict found within (possibly nested) JSON-LD data."""
    if isinstance(data, dict):
        yield data
        for v in data.values():
            yield from _iter_jsonld_objects(v)
    elif isinstance(data, list):
        for item in data:
            yield from _iter_jsonld_objects(item)


def _jsonld_index(soup: BeautifulSoup) -> dict[str, list]:
    """Index all JSON-LD values by normalized key (first-seen wins on lookup)."""
    index: dict[str, list] = {}
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text() or ""
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        for obj in _iter_jsonld_objects(data):
            if not isinstance(obj, dict):
                continue
            for key, val in obj.items():
                k = _normalize(key)
                index.setdefault(k, []).append(val)
    return index


def _extract_metas(soup: BeautifulSoup) -> dict[str, str]:
    """Index meta tags by lowercase name/property/itemprop key."""
    metas: dict[str, str] = {}
    for m in soup.find_all("meta"):
        key = m.get("name") or m.get("property") or m.get("itemprop")
        content = m.get("content")
        if key and content:
            metas.setdefault(key.lower(), content.strip())
    return metas


def _lookup_ld(index: dict[str, list], field_norm: str) -> Any:
    for key in _field_keys(field_norm):
        for alias in FIELD_ALIASES.get(key, [key]):
            vals = index.get(alias)
            if vals:
                return vals[0]
    return None


def _lookup_meta(metas: dict[str, str], field_norm: str) -> Any:
    for key in _field_keys(field_norm):
        for alias in META_ALIASES.get(key, [key]):
            if alias in metas:
                return metas[alias]
    return None


def _semantic_selectors(field_norm: str) -> list[str]:
    return SEMANTIC_SELECTORS.get(
        field_norm,
        [
            f"[itemprop='{field_norm}']",
            f"#{field_norm}",
            f".{field_norm}",
            f"[name='{field_norm}']",
            f"[data-{field_norm.replace(' ', '-')}]",
            f"[class*='{field_norm}']",
        ],
    )


def _element_value(el, target_type: str) -> Any:
    """Extract the most useful scalar value from an element for a given type."""
    if el.name == "meta":
        return el.get("content")

    if target_type in ("image", "images"):
        for attr in ("src", "content", "href", "data-src"):
            v = el.get(attr)
            if v:
                return v

    if target_type == "url":
        for attr in ("href", "content", "src"):
            v = el.get(attr)
            if v:
                return v

    if el.name == "img":
        return el.get("src") or el.get("alt")

    return el.get_text(strip=True)


def _generic_match(soup: BeautifulSoup, field_norm: str, target_type: str) -> Any:
    """Fallback: find an element whose attributes reference the field name."""
    token = field_norm.replace(" ", "-")
    for el in soup.find_all(True):
        el_id = (el.get("id") or "").lower()
        itemprop = (el.get("itemprop") or "").lower()
        name = (el.get("name") or "").lower()
        classes = [c.lower() for c in (el.get("class") or []) if isinstance(c, str)]
        data_keys = [k.lower() for k in el.attrs if k.startswith("data-")]

        hit = (
            field_norm in el_id
            or field_norm in itemprop
            or field_norm in name
            or token in el_id
            or any(field_norm in c or token in c for c in classes)
            or any(field_norm in k for k in data_keys)
        )
        if not hit:
            continue

        v = _element_value(el, target_type)
        if v in (None, ""):
            continue
        # Avoid returning huge container text for scalar fields.
        if target_type not in ("array", "object") and isinstance(v, str) and len(v) > 500:
            continue
        return v
    return None


def _extract_field(
    soup: BeautifulSoup,
    field: str,
    prop_schema: dict,
    ld_index: dict[str, list],
    metas: dict[str, str],
) -> tuple[Any, float]:
    """Extract a single schema field. Returns (coerced_value, confidence)."""
    target_type = prop_schema.get("type", "string") if isinstance(prop_schema, dict) else "string"
    field_norm = _normalize(field)

    # 1. JSON-LD — strongest structured signal.
    val = _lookup_ld(ld_index, field_norm)
    if val is not None:
        return coerce_field(val, prop_schema), 0.95

    # 2. Meta tags.
    val = _lookup_meta(metas, field_norm)
    if val is not None:
        return coerce_field(val, prop_schema), 0.85

    # 3. Semantic selectors.
    for sel in _semantic_selectors(field_norm):
        if target_type == "array":
            els = soup.select(sel)
            if els:
                values = [v for v in (_element_value(e, target_type) for e in els) if v not in (None, "")]
                if values:
                    return coerce_field(values, prop_schema), 0.7
        else:
            el = soup.select_one(sel)
            if el:
                v = _element_value(el, target_type)
                if v not in (None, ""):
                    return coerce_field(v, prop_schema), 0.7

    # 4. Generic attribute/class match (best-effort fallback).
    v = _generic_match(soup, field_norm, target_type)
    if v not in (None, ""):
        return coerce_field(v, prop_schema), 0.5

    return None, 0.0


def extract_by_schema(html_text: str, schema: dict) -> tuple[dict, float]:
    """Extract fields described by a JSON schema (object with properties)."""
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not properties or not isinstance(properties, dict):
        return {}, 0.0

    soup = BeautifulSoup(html_text or "", "lxml")
    ld_index = _jsonld_index(soup)
    metas = _extract_metas(soup)

    result: dict[str, Any] = {}
    confidences: list[float] = []
    for field, prop in properties.items():
        prop_schema = prop if isinstance(prop, dict) else {}
        value, conf = _extract_field(soup, field, prop_schema, ld_index, metas)
        result[field] = value
        confidences.append(conf)

    confidence = round(sum(confidences) / len(confidences), 4) if confidences else 0.0
    return result, confidence


# ---------------------------------------------------------------------------
# CSS selector extraction
# ---------------------------------------------------------------------------

def _el_text(el) -> str:
    return el.get_text(strip=True) if el is not None else ""


def _extract_css_sub_field(parent, spec) -> Any:
    """Extract a nested field relative to a parent element."""
    if isinstance(spec, str):
        el = parent.select_one(spec)
        return _el_text(el) if el is not None else None
    if isinstance(spec, dict):
        sel = spec.get("selector")
        if not sel:
            return None
        el = parent.select_one(sel)
        if el is None:
            return None
        attr = spec.get("attribute")
        if attr:
            return el.get(attr)
        return _el_text(el)
    return None


def _extract_css_field(soup: BeautifulSoup, spec) -> tuple[Any, float]:
    """Extract one CSS-selector field. Returns (value, confidence)."""
    if isinstance(spec, str):
        selector, attribute, is_list, fields = spec, None, False, None
    elif isinstance(spec, dict):
        selector = spec.get("selector")
        attribute = spec.get("attribute")
        is_list = spec.get("type") == "list" or bool(spec.get("multiple") or spec.get("all"))
        fields = spec.get("fields")
        if not selector:
            return None, 0.0
    else:
        return None, 0.0

    elements = soup.select(selector)
    if not elements:
        return None, 0.0

    if is_list:
        if fields and isinstance(fields, dict):
            items = []
            for el in elements:
                item = {}
                for fname, fspec in fields.items():
                    item[fname] = _extract_css_sub_field(el, fspec)
                items.append(item)
            return items, 0.9
        if attribute:
            return [el.get(attribute) for el in elements], 0.9
        return [_el_text(el) for el in elements], 0.9

    el = elements[0]
    if attribute:
        return el.get(attribute), 0.9
    return _el_text(el), 0.9


def extract_by_selectors(html_text: str, selectors: dict) -> tuple[dict, float]:
    """Extract fields using explicit CSS selectors."""
    if not isinstance(selectors, dict) or not selectors:
        return {}, 0.0

    soup = BeautifulSoup(html_text or "", "lxml")
    result: dict[str, Any] = {}
    confidences: list[float] = []
    for field, spec in selectors.items():
        value, conf = _extract_css_field(soup, spec)
        result[field] = value
        confidences.append(conf)

    confidence = round(sum(confidences) / len(confidences), 4) if confidences else 0.0
    return result, confidence


# ---------------------------------------------------------------------------
# XPath extraction
# ---------------------------------------------------------------------------

def _extract_xpath_field(tree, xpath: str) -> tuple[Any, float]:
    try:
        nodes = tree.xpath(xpath)
    except Exception:
        return None, 0.0

    if not nodes:
        return None, 0.0

    results: list[Any] = []
    for n in nodes:
        if isinstance(n, str):
            results.append(n.strip())
        elif hasattr(n, "text_content"):
            results.append(n.text_content().strip())
        elif hasattr(n, "text"):
            results.append((n.text or "").strip())
        else:
            results.append(str(n))

    results = [r for r in results if r != ""]
    if not results:
        return None, 0.0

    if len(results) == 1:
        return results[0], 0.9
    return results, 0.9


def extract_by_xpaths(html_text: str, xpaths: dict) -> tuple[dict, float]:
    """Extract fields using explicit XPath 1.0 queries."""
    if not isinstance(xpaths, dict) or not xpaths:
        return {}, 0.0

    try:
        tree = lxml_html.fromstring(html_text or "")
    except Exception:
        return {}, 0.0

    result: dict[str, Any] = {}
    confidences: list[float] = []
    for field, xpath in xpaths.items():
        if not isinstance(xpath, str):
            result[field], conf = None, 0.0
        else:
            value, conf = _extract_xpath_field(tree, xpath)
            result[field] = value
        confidences.append(conf)

    confidence = round(sum(confidences) / len(confidences), 4) if confidences else 0.0
    return result, confidence


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def extract_from_html(
    html_text: str,
    schema: Optional[dict] = None,
    selectors: Optional[dict] = None,
    xpaths: Optional[dict] = None,
) -> tuple[dict, float, Optional[str]]:
    """Run extraction using the highest-priority method provided.

    Priority: schema > CSS selectors > XPath.

    Returns (data, confidence, method) where `method` is one of
    "schema", "css_selector", "xpath", or None if no method was applicable.
    """
    if schema:
        data, confidence = extract_by_schema(html_text, schema)
        return data, confidence, "schema"
    if selectors:
        data, confidence = extract_by_selectors(html_text, selectors)
        return data, confidence, "css_selector"
    if xpaths:
        data, confidence = extract_by_xpaths(html_text, xpaths)
        return data, confidence, "xpath"
    return {}, 0.0, None
