"""Mountaindale Press scoped source.

Data source:
- Shopify collections API: ``/collections/all-books/products.json``
  Returns structured JSON with all products in the "All Books" collection.
  Limited to 250 products per request; paginated with ``page`` parameter.

Discovery strategy:
- Fetch all products from the Shopify collections API.
- Filter out bundles and box sets by title/variant count.
- Yield the product handle URL for each individual book.

Book parsing:
- Extract metadata from the Shopify JSON response.
- Title is cleaned of "Kindle Edition" suffix and series info in parentheses.
- Author extracted from the ``vendor`` field (collection endpoint provides
  correct author names for most products).
- Series name extracted from tags (e.g., ``"the-completionist-chronicles-books"``
  becomes ``"The Completionist Chronicles"``).
- Series position extracted from the title (e.g., ``"(The Metier Apocalypse Book 2)"``
  becomes ``2.0``).
- Cover image URL from the product images.
- Description from the ``body_html`` field (HTML stripped).
- Price from the first variant.
- No ISBNs available in the Shopify data.

Identifiers:
- ``mountaindale_id``: The Shopify product handle (e.g.,
  ``"attuned-dungeon-an-apocalyptic-litrpg-adventure-the-metier-apocalypse-book-2-kindle-edition-b0bq4wr7t6"``).

Session type: HTTP (Shopify API is plain JSON, no anti-bot protection).
"""

import logging
import re
from html import unescape
from typing import AsyncIterator

from book_metadata_scraper.models import AuthorData, BookData
from book_metadata_scraper.sources.base import BaseScopedSource
from book_metadata_scraper.sources.registry import scoped_source
from book_metadata_scraper.fetcher import SESSION_HTTP

logger = logging.getLogger(__name__)

# Tags that indicate a bundle or box set (to be filtered out)
BUNDLE_TAGS = {"bundle", "box set", "big-bundles-of-books", "sets-and-bundles"}


def _strip_html(html: str | None) -> str | None:
    """Remove HTML tags and decode entities."""
    if not html:
        return None
    # Remove tags
    text = re.sub(r"<[^>]+>", "", html)
    # Decode HTML entities
    text = unescape(text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text if text else None


def _extract_series_info(title: str, tags: list[str]) -> tuple[str | None, float | None]:
    """Extract series name and position from title and tags.

    Returns (series_name, series_position).
    """
    series_name = None
    series_position = None

    # Try to extract series from title pattern: "(Series Name Book N)"
    series_match = re.search(r"\(([^)]+?)\s+Book\s+(\d+(?:\.\d+)?)\)", title)
    if series_match:
        series_name = series_match.group(1).strip()
        series_position = float(series_match.group(2))
    else:
        # Try pattern without "Book": "(Series Name N)"
        series_match = re.search(r"\(([^)]+?)\s+(\d+(?:\.\d+)?)\)", title)
        if series_match:
            series_name = series_match.group(1).strip()
            series_position = float(series_match.group(2))

    # If no series from title, try to extract from tags
    if not series_name:
        for tag in tags:
            # Tags like "the-completionist-chronicles-books" or "metier-apocalypse-books"
            if tag.endswith("-books") and not tag.startswith("all-") and not tag.startswith("signed-"):
                # Convert kebab-case to title case
                series_name = tag.replace("-books", "").replace("-", " ").title()
                break
            # Tags like "the-completionist-chronicles"
            elif tag.startswith("the-") and not tag.startswith("the-undying-"):
                series_name = tag.replace("-", " ").title()
                break

    return series_name, series_position


def _clean_title(title: str) -> str:
    """Clean up the title by removing Kindle Edition suffix and other noise."""
    # Remove "Kindle Edition" suffix
    title = re.sub(r"\s+Kindle Edition$", "", title)
    # Remove series info in parentheses at the end
    title = re.sub(r"\s*\([^)]*\)\s*$", "", title)
    return title.strip()


def _is_bundle(product: dict) -> bool:
    """Check if a product is a bundle or box set."""
    title = product.get("title", "").lower()
    tags = [t.lower() for t in product.get("tags", [])]

    # Check title for bundle indicators
    if any(word in title for word in ["bundle", "collection", "box set", "signed paperback collection"]):
        return True

    # Check tags for bundle indicators
    if any(tag in BUNDLE_TAGS for tag in tags):
        return True

    # Check if product has multiple variants with different SKUs
    # (bundles often have multiple book variants)
    variants = product.get("variants", [])
    if len(variants) > 5:  # Heuristic: more than 5 variants likely means bundle
        return True

    return False


@scoped_source
class MountaindalePressSource(BaseScopedSource):
    """Mountaindale Press scoped source."""

    name = "mountaindale_press"
    session_type = SESSION_HTTP
    rate_limit = 1.0  # 1 request per second — Shopify rate-limits aggressively

    BASE_URL = "https://www.mountaindalepress.store"
    COLLECTION_URL = f"{BASE_URL}/collections/all-books/products.json"

    async def discover_book_urls(self) -> AsyncIterator[tuple[str, float | None]]:
        """Discover book URLs from the Shopify collections API.

        Yields tuples of (url, series_position) for each book.
        """
        page = 1
        total_products = 0

        while True:
            url = f"{self.COLLECTION_URL}?limit=250&page={page}"
            logger.info("Fetching Mountaindale catalog page %d", page)

            response = await self.fetch(url)
            if response.status != 200:
                logger.warning("Failed to fetch catalog page %d: HTTP %d", page, response.status)
                break

            try:
                data = response.json()
            except Exception as e:
                logger.warning("Failed to parse catalog JSON on page %d: %s", page, e)
                break

            products = data.get("products", [])
            if not products:
                break

            for product in products:
                if _is_bundle(product):
                    logger.debug("Skipping bundle: %s", product.get("title"))
                    continue

                handle = product.get("handle", "")
                if not handle:
                    continue

                # Extract series position from title for the yield
                title = product.get("title", "")
                tags = product.get("tags", [])
                _, series_position = _extract_series_info(title, tags)

                book_url = f"{self.BASE_URL}/products/{handle}"
                yield book_url, series_position
                total_products += 1

            # Check if there are more pages
            if len(products) < 250:
                break

            page += 1

        logger.info("Discovered %d books from Mountaindale catalog", total_products)

    async def parse_book(self, response) -> BookData | None:
        """Parse a Mountaindale Press book page.

        This method is called with the response from the product page,
        but we actually need the Shopify JSON data.  We'll fetch it
        from the products.json endpoint instead.
        """
        # Extract the product handle from the URL
        url = response.url
        handle_match = re.search(r"/products/([^?]+)", url)
        if not handle_match:
            logger.warning("Could not extract product handle from URL: %s", url)
            return None

        handle = handle_match.group(1)

        # Fetch the product data from the Shopify API
        product_url = f"{self.BASE_URL}/products/{handle}.json"
        product_response = await self.fetch(product_url)

        if product_response.status != 200:
            logger.warning("Failed to fetch product JSON for %s: HTTP %d", handle, product_response.status)
            return None

        try:
            product_data = product_response.json().get("product", {})
        except Exception as e:
            logger.warning("Failed to parse product JSON for %s: %s", handle, e)
            return None

        if not product_data:
            logger.warning("No product data for %s", handle)
            return None

        # Fetch the collection data to get the correct vendor (author)
        # The individual product endpoint sometimes returns "Amazon" as vendor
        # but the collection endpoint has the correct author name
        author_name = None
        try:
            collection_url = f"{self.COLLECTION_URL}?limit=250"
            collection_response = await self.fetch(collection_url)
            if collection_response.status == 200:
                collection_data = collection_response.json()
                for p in collection_data.get("products", []):
                    if p.get("handle") == handle:
                        author_name = p.get("vendor")
                        break
        except Exception as e:
            logger.debug("Failed to fetch collection data: %s", e)

        return self._parse_product(product_data, author_name)

    def _parse_product(self, product: dict, author_name: str | None = None) -> BookData | None:
        """Parse a Shopify product JSON into BookData."""
        title = product.get("title", "")
        if not title:
            logger.warning("Product has no title")
            return None

        # Clean the title
        clean_title = _clean_title(title)

        # Extract tags
        tags = product.get("tags", [])

        # Use provided author or fall back to vendor
        if not author_name:
            author_name = product.get("vendor")

        # Skip "Amazon" as it's not a real author
        if author_name and author_name.lower() == "amazon":
            logger.debug("Skipping 'Amazon' as author for product: %s", product.get("handle"))
            author_name = None

        authors = [AuthorData(name=author_name)] if author_name else []

        # Extract series info
        series_name, series_position = _extract_series_info(title, tags)

        # Extract description from body_html
        description = _strip_html(product.get("body_html"))

        # Extract cover image
        images = product.get("images", [])
        cover_image_url = None
        if images:
            cover_image_url = images[0].get("src")

        # Extract price from first variant
        variants = product.get("variants", [])
        price = None
        if variants:
            price_str = variants[0].get("price")
            if price_str:
                try:
                    price = float(price_str)
                except (ValueError, TypeError):
                    pass

        # Build identifiers
        identifiers = {}
        handle = product.get("handle")
        if handle:
            identifiers["mountaindale_id"] = handle

        # Extract published date
        published_at = product.get("published_at")
        published_date = None
        if published_at:
            # Parse ISO format: "2025-05-28T12:03:31-05:00"
            date_match = re.match(r"(\d{4}-\d{2}-\d{2})", published_at)
            if date_match:
                published_date = date_match.group(1)

        # Extract genres from tags
        genres = []
        genre_tags = ["litrpg", "gamelit", "fantasy", "science fiction", "romance"]
        for tag in tags:
            tag_lower = tag.lower()
            if tag_lower in genre_tags:
                genres.append(tag.title())

        return BookData(
            title=clean_title,
            authors=authors,
            description=description,
            publisher="Mountaindale Press",
            published_date=published_date,
            series=series_name,
            series_position=series_position,
            cover_image_url=cover_image_url,
            genres=genres,
            identifiers=identifiers,
            source_url=f"{self.BASE_URL}/products/{product.get('handle', '')}",
        )
