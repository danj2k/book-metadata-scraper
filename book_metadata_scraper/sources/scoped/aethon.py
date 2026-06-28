"""Aethon Books scoped source plugin.

Discovers and parses book metadata from https://aethonbooks.com/.

Discovery strategy
------------------
The site's ``/series/`` page lists every series (currently ~500) with no
pagination.  Each series page embeds a ``CreativeWorkSeries`` JSON-LD block
whose ``hasPart`` array contains every book in the series with its URL.
``discover_book_urls`` walks this hierarchy: ``/series/`` → series pages →
book URLs.

Individual book pages carry a ``Book`` JSON-LD block with full metadata
(ISBN, description, page count, genres, formats).  ``parse_book`` extracts
this directly — no CSS scraping required.

Rate limiting
-------------
The standard HTTP session is used (no anti-bot protection observed).
``SessionManager`` enforces the configured ``http_rate_limit`` globally, so
Aethon fetches are automatically throttled.
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

BASE_URL = "https://aethonbooks.com"


def _extract_json_ld(response) -> dict | None:
    """Return the first ``<script type="application/ld+json">`` block parsed
    as a dict, or ``None`` if none found or unparseable."""
    for script in response.css('script[type="application/ld+json"]'):
        text = script.text
        if not text:
            continue
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def _extract_all_json_ld(response) -> list[dict]:
    """Return *all* JSON-LD blocks on the page."""
    results = []
    for script in response.css('script[type="application/ld+json"]'):
        text = script.text
        if not text:
            continue
        try:
            results.append(json.loads(text))
        except (json.JSONDecodeError, TypeError):
            continue
    return results


def _normalise_book_url(url: str) -> str:
    """Ensure consistent URL format for DB lookups.

    Strips trailing slashes and ensures the full absolute URL.
    Both discovery and parse_book must use this so that
    ``find_book_by_source_url`` matches on subsequent runs.
    """
    url = url.rstrip("/")
    if url.startswith("/"):
        url = BASE_URL + url
    return url


def _parse_book_jsonld(data: dict, source_url: str) -> BookData | None:
    """Convert a ``@type: Book`` JSON-LD block into a BookData."""
    if data.get("@type") != "Book":
        return None

    title = data.get("name") or data.get("headline")
    if not title:
        logger.warning("Book JSON-LD missing name/headline at %s", source_url)
        return None

    # Authors
    authors = []
    for author_raw in data.get("author", []):
        if isinstance(author_raw, dict):
            name = author_raw.get("name")
            if name:
                authors.append(AuthorData(name=name))

    # Series info
    series_name = None
    series_position = None
    is_part_of = data.get("isPartOf")
    if isinstance(is_part_of, dict) and is_part_of.get("@type") == "CreativeWorkSeries":
        series_name = is_part_of.get("name")
        # position may be on the isPartOf or we infer from workExample context
        pos = is_part_of.get("position")
        if pos is not None:
            try:
                series_position = float(pos)
            except (ValueError, TypeError):
                pass

    # Genres
    genres = []
    raw_genres = data.get("genre", [])
    if isinstance(raw_genres, str):
        raw_genres = [raw_genres]
    for g in raw_genres:
        if isinstance(g, str) and g.strip():
            genres.append(g.strip())

    # Identifiers (ISBN, ASIN)
    identifiers = {}
    isbn = data.get("isbn")
    if isbn:
        isbn_clean = str(isbn).replace("-", "").strip()
        if len(isbn_clean) == 13:
            identifiers["isbn13"] = isbn_clean
        elif len(isbn_clean) == 10:
            identifiers["isbn10"] = isbn_clean

    # Check workExample for additional ISBNs / ASINs
    for example in data.get("workExample", []):
        if not isinstance(example, dict):
            continue
        ex_isbn = example.get("isbn")
        if ex_isbn:
            ex_isbn_clean = str(ex_isbn).replace("-", "").strip()
            if len(ex_isbn_clean) == 13 and "isbn13" not in identifiers:
                identifiers["isbn13"] = ex_isbn_clean
            elif len(ex_isbn_clean) == 10 and "isbn10" not in identifiers:
                identifiers["isbn10"] = ex_isbn_clean

        # ASIN from @id or url (Amazon/Audible URLs)
        ex_url = example.get("@id", "") or example.get("url", "")
        fmt = example.get("bookFormat", "")
        # Amazon uses /dp/, Audible uses /pd/
        asin_match = re.search(r"/[dp]{2}/([A-Z0-9]{10})", ex_url)
        if asin_match:
            asin = asin_match.group(1)
            if "audible" in ex_url:
                identifiers["asin_audiobook"] = asin
            elif "EBook" in fmt or "ebook" in fmt.lower():
                identifiers["asin_ebook"] = asin
            elif "Paperback" in fmt:
                identifiers["asin_paperback"] = asin
            elif "asin" not in identifiers:
                identifiers["asin"] = asin

    # Page count
    page_count = None
    raw_pages = data.get("numberOfPages")
    if raw_pages is not None:
        try:
            page_count = int(raw_pages)
        except (ValueError, TypeError):
            pass

    # Publisher
    publisher = None
    pub_raw = data.get("publisher")
    if isinstance(pub_raw, dict):
        publisher = pub_raw.get("name")
    elif isinstance(pub_raw, str):
        publisher = pub_raw

    # Description (strip HTML tags if present)
    description = data.get("description")
    if description:
        description = re.sub(r"<[^>]+>", "", description).strip()
        if not description:
            description = None

    # Cover image
    cover_image = data.get("image")
    if isinstance(cover_image, list):
        cover_image = cover_image[0] if cover_image else None

    # Language
    language = data.get("inLanguage")
    if isinstance(language, list):
        language = language[0] if language else None

    # Publication date
    published_date = data.get("datePublished")

    return BookData(
        title=title,
        authors=authors,
        description=description,
        publisher=publisher,
        published_date=published_date,
        page_count=page_count,
        language=language,
        series=series_name,
        series_position=series_position,
        cover_image_url=cover_image,
        genres=genres,
        identifiers=identifiers,
        source_url=source_url,
    )


@scoped_source
class AethonBooks(BaseScopedSource):
    """Aethon Books — https://aethonbooks.com/"""

    name = "aethon_books"
    session_type = SESSION_HTTP

    async def discover_book_urls(self) -> AsyncIterator[tuple[str, float | None]]:
        """Yield (book_url, series_position) tuples by walking:
        /series/ → series pages → hasPart.
        """
        seen_urls: set[str] = set()

        # Step 1: Fetch the master series index
        logger.info("Fetching series index from %s/series/", BASE_URL)
        try:
            index_response = await self.fetch(f"{BASE_URL}/series/")
        except Exception:
            logger.exception("Failed to fetch /series/ index page")
            return

        # Extract all series page URLs
        series_urls: list[str] = []
        for link in index_response.css('a[href*="/book-series/"]'):
            href = link.attrib.get("href", "")
            if not href:
                continue
            normalised = _normalise_book_url(href)
            if normalised not in series_urls:
                series_urls.append(normalised)

        logger.info("Found %d series on index page", len(series_urls))

        # Step 2: For each series, fetch the page and extract book URLs from JSON-LD
        for i, series_url in enumerate(series_urls, 1):
            logger.debug("Fetching series %d/%d: %s", i, len(series_urls), series_url)
            try:
                series_response = await self.fetch(series_url)
            except Exception:
                logger.exception("Failed to fetch series page %s", series_url)
                continue

            # Parse the CreativeWorkSeries JSON-LD
            json_ld = _extract_json_ld(series_response)
            if not json_ld or json_ld.get("@type") != "CreativeWorkSeries":
                # Try all JSON-LD blocks in case the first one isn't the series
                for block in _extract_all_json_ld(series_response):
                    if block.get("@type") == "CreativeWorkSeries":
                        json_ld = block
                        break

            if not json_ld or json_ld.get("@type") != "CreativeWorkSeries":
                logger.warning("No CreativeWorkSeries JSON-LD found at %s", series_url)
                continue

            # Extract book URLs and positions from hasPart
            has_part = json_ld.get("hasPart", [])
            for book_entry in has_part:
                if not isinstance(book_entry, dict):
                    continue
                book_url = book_entry.get("url")
                if not book_url:
                    continue
                book_url = _normalise_book_url(book_url)

                # Extract position from hasPart entry
                position = None
                raw_pos = book_entry.get("position")
                if raw_pos is not None:
                    try:
                        position = float(raw_pos)
                    except (ValueError, TypeError):
                        pass

                if book_url not in seen_urls:
                    seen_urls.add(book_url)
                    yield (book_url, position)

        logger.info(
            "Discovery complete: %d unique book URLs from %d series",
            len(seen_urls),
            len(series_urls),
        )

    async def parse_book(self, response) -> BookData | None:
        """Extract book metadata from a book page's JSON-LD."""
        source_url = _normalise_book_url(response.url or "")

        json_ld = _extract_json_ld(response)
        if not json_ld or json_ld.get("@type") != "Book":
            # Try all blocks
            for block in _extract_all_json_ld(response):
                if block.get("@type") == "Book":
                    json_ld = block
                    break

        if not json_ld or json_ld.get("@type") != "Book":
            logger.warning("No Book JSON-LD found at %s", source_url)
            return None

        return _parse_book_jsonld(json_ld, source_url)
