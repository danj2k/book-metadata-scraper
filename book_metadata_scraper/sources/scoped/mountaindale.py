"""Mountaindale Press scoped source plugin.

Discovers and parses book metadata from https://www.mountaindalepress.store/.

Discovery strategy
------------------
Mountaindale Press runs on Shopify.  Their ``/collections/all-books/products.json``
endpoint returns all book products (currently ~144) in a single paginated JSON
response (limit=250 covers everything).  This avoids JavaScript-rendered catalog
pages entirely.

``discover_book_urls`` fetches the collection JSON once, filters out non-book
product types (merch, clothing, etc.) and bundle/box-set products, then yields
the canonical URL for each individual book.

Parsing
-------
Each product in the Shopify JSON carries:
- ``title`` -- often includes series info, e.g.
  ``"Uncapped | Completionist Chronicles Book 14"``
- ``body_html`` -- description (HTML)
- ``vendor`` -- typically the author name (e.g. "Dakota Krout")
- ``tags`` -- series abbreviations, genre labels, format tags
- ``product_type`` -- "Books", "E-books", "Hardcover", "Audiobook"
- ``variants[].sku`` -- sometimes an ASIN
- ``images`` -- cover image URLs
- ``published_at`` -- publication date

Series name and position are parsed from the title using common Mountaindale
title patterns.  Author comes from the ``vendor`` field.  ISBNs are not
available in the Shopify API; ASINs are extracted from SKU when present.

Rate limiting
-------------
The standard HTTP session is used.  ``SessionManager`` enforces the configured
``http_rate_limit`` globally.  Discovery is a single fetch; individual book
pages are not fetched (all data comes from the collection JSON).
"""

import json
import logging
import re
from typing import AsyncIterator

from book_metadata_scraper.fetcher import SESSION_HTTP
from book_metadata_scraper.models import AuthorData, BookData
from book_metadata_scraper.sources.base import BaseScopedSource
from book_metadata_scraper.sources.registry import scoped_source

logger = logging.getLogger(__name__)

BASE_URL = "https://www.mountaindalepress.store"
COLLECTION_URL = f"{BASE_URL}/collections/all-books/products.json?limit=250"

# Product types considered to be individual books (not merch)
_BOOK_PRODUCT_TYPES = {"books", "e-books", "hardcover", "audiobook", "audiobooks", ""}

# Tags that indicate bundles/box sets (excluded from discovery)
_BUNDLE_TAGS = {"box set", "bundle", "big bundles"}

# ASIN regex: 10-char alphanumeric starting with B0
_Asin_RE = re.compile(r"^[A-Z0-9]0[A-Z0-9]{8}$")

# Common Mountaindale title patterns for series extraction.
# Each pattern has named groups: ?P<series> and ?P<pos>.
_SERIES_PATTERNS = [
    # "Title | Series Name Book 14"
    re.compile(
        r"\|\s*(?P<series>.+?)\s+Book\s+(?P<pos>\d+(?:\.\d+)?)\s*[!!]?\s*$",
        re.IGNORECASE,
    ),
    # "Title | Book 1 in the Series Name"
    re.compile(
        r"\|\s*Book\s+(?P<pos>\d+(?:\.\d+)?)\s+in\s+(?:the\s+)?(?P<series>.+?)\s*[!!]?\s*$",
        re.IGNORECASE,
    ),
    # "Title | Book 1 of 5 in The Divine Dungeon"
    re.compile(
        r"\|\s*Book\s+(?P<pos>\d+(?:\.\d+)?)\s+of\s+\d+\s+in\s+(?:the\s+)?(?P<series>.+?)\s*[!!]?\s*$",
        re.IGNORECASE,
    ),
    # "Title | Series Name, Book 14"
    re.compile(
        r"\|\s*(?P<series>.+?),\s*Book\s+(?P<pos>\d+(?:\.\d+)?)\s*[!!]?\s*$",
        re.IGNORECASE,
    ),
    # "Title | Book 14 of the Series Name"
    re.compile(
        r"\|\s*Book\s+(?P<pos>\d+(?:\.\d+)?)\s+of\s+(?:the\s+)?(?P<series>.+?)\s*[!!]?\s*$",
        re.IGNORECASE,
    ),
]


def _normalise_url(url: str) -> str:
    """Ensure a consistent absolute URL with no trailing slash."""
    url = url.rstrip("/")
    if url.startswith("/"):
        url = BASE_URL + url
    return url


def _strip_html(html: str) -> str:
    """Remove HTML tags and collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_series_from_title(title: str) -> tuple[str | None, float | None]:
    """Extract series name and position from a Mountaindale product title.

    Returns (series_name, series_position) — either or both may be None.
    """
    # Skip titles that don't contain a pipe separator
    if "|" not in title:
        return None, None

    for pattern in _SERIES_PATTERNS:
        m = pattern.search(title)
        if m:
            series_name = m.group("series").strip()
            # Remove leading "the " if present
            series_name = re.sub(r"^the\s+", "", series_name, flags=re.IGNORECASE)

            try:
                position = float(m.group("pos"))
            except (ValueError, TypeError):
                position = None

            return series_name, position

    return None, None


def _is_book_product(product: dict) -> bool:
    """Return True if the product is an individual Mountaindale book (not merch/bundle/3P)."""
    product_type = (product.get("product_type") or "").lower()
    if product_type not in _BOOK_PRODUCT_TYPES:
        return False

    # Exclude Amazon-vendor products (third-party Kindle listings)
    vendor = (product.get("vendor") or "").strip()
    if vendor.lower() == "amazon":
        return False

    # Check tags for bundle/box-set indicators
    tags = product.get("tags", "")
    if isinstance(tags, str):
        tag_list = [t.strip().lower() for t in tags.split(",")]
    else:
        tag_list = [t.lower() for t in tags]

    for tag in tag_list:
        if tag in _BUNDLE_TAGS:
            return False

    return True


def _extract_asin_from_sku(sku: str | None) -> str | None:
    """Extract an ASIN from a variant SKU if it matches the ASIN pattern."""
    if not sku:
        return None
    sku = sku.strip()
    if _Asin_RE.match(sku):
        return sku
    return None


@scoped_source
class MountaindalePress(BaseScopedSource):
    """Mountaindale Press — https://www.mountaindalepress.store/"""

    name = "mountaindale"
    session_type = SESSION_HTTP

    def __init__(self, session, config: dict):
        super().__init__(session, config)
        self._products: list[dict] = []
        self._url_to_product: dict[str, dict] = {}

    async def _ensure_loaded(self) -> None:
        """Fetch and cache the collection JSON if not already in memory."""
        if self._products:
            return

        logger.info("Fetching Mountaindale Press books from %s", COLLECTION_URL)
        try:
            response = await self.session.fetch_http(COLLECTION_URL)
        except Exception:
            logger.exception("Failed to fetch Mountaindale Press collection")
            return

        # Parse JSON from the response
        try:
            data = response.json()
        except Exception:
            # Fallback: try parsing text content
            text = response.text or response.html_content or ""
            try:
                data = json.loads(str(text))
            except (json.JSONDecodeError, TypeError):
                logger.error("Failed to parse Mountaindale Press collection JSON")
                return

        all_products = data.get("products", [])

        # Filter to individual books only
        self._products = [p for p in all_products if _is_book_product(p)]

        # Build URL → product lookup
        for product in self._products:
            handle = product.get("handle", "")
            url = _normalise_url(f"/products/{handle}")
            self._url_to_product[url] = product

        logger.info(
            "Loaded %d Mountaindale Press products (%d after filtering)",
            len(all_products),
            len(self._products),
        )

    async def discover_book_urls(self) -> AsyncIterator[str]:
        """Yield the canonical URL for each individual book."""
        await self._ensure_loaded()

        for product in self._products:
            handle = product.get("handle", "")
            url = _normalise_url(f"/products/{handle}")
            yield url

    async def parse_book(self, response) -> BookData | None:
        """Build a BookData from the cached Shopify product data.

        The ``response`` parameter is used only for its ``url`` attribute.
        All data comes from the collection JSON loaded during discovery.
        """
        url = response.url if hasattr(response, "url") else None
        if url is None:
            logger.warning("parse_book called with no URL on response")
            return None

        # Normalise URL for lookup
        url = _normalise_url(url)
        product = self._url_to_product.get(url)
        if product is None:
            logger.warning("URL %s not found in Mountaindale Press cache", url)
            return None

        # --- Title ---
        title = product.get("title", "").strip()
        if not title:
            logger.warning("Empty title for product at %s", url)
            return None

        # --- Series (parsed from title) ---
        series_name, series_position = _parse_series_from_title(title)

        # --- Author (from vendor field) ---
        authors: list[AuthorData] = []
        vendor = (product.get("vendor") or "").strip()
        if vendor and vendor.lower() != "mountaindale press":
            authors.append(AuthorData(name=vendor))

        # --- Description ---
        body_html = product.get("body_html", "") or ""
        description = _strip_html(body_html) if body_html else None
        if description and not description.strip():
            description = None

        # --- Publisher ---
        publisher = "Mountaindale Press"

        # --- Published date ---
        published_date = product.get("published_at")
        if published_date:
            # Shopify returns ISO 8601: "2025-12-09T20:47:10-06:00"
            # Extract just the date portion
            published_date = published_date[:10] if len(published_date) >= 10 else published_date

        # --- Cover image ---
        cover_image = None
        images = product.get("images", [])
        if images:
            cover_image = images[0].get("src")

        # --- Genres (from tags) ---
        genres: list[str] = []
        tags_raw = product.get("tags", "")
        if isinstance(tags_raw, str):
            tag_list = [t.strip() for t in tags_raw.split(",")]
        else:
            tag_list = list(tags_raw)

        # Genre-relevant tags
        _GENRE_TAGS = {"litRPG", "GameLit"}
        for tag in tag_list:
            if tag.lower() in {g.lower() for g in _GENRE_TAGS}:
                genres.append(tag)

        # --- Identifiers ---
        identifiers: dict[str, str] = {}
        for variant in product.get("variants", []):
            asin = _extract_asin_from_sku(variant.get("sku"))
            if asin:
                identifiers.setdefault("asin", asin)

        # --- Language (assume English for Mountaindale) ---
        language = "en"

        return BookData(
            title=title,
            authors=authors,
            description=description,
            publisher=publisher,
            published_date=published_date,
            language=language,
            series=series_name,
            series_position=series_position,
            cover_image_url=cover_image,
            genres=genres,
            identifiers=identifiers,
            source_url=url,
        )
