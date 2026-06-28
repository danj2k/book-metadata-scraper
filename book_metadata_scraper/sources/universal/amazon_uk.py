"""Amazon UK universal source plugin.

Enriches books already in the database with metadata from Amazon UK
(amazon.co.uk).  Uses the stealthy browser fetcher to bypass Amazon's
WAF (Web Application Firewall).

Query strategy
--------------
1. If we have an ``asin`` identifier, fetch the book page directly via
   ``/dp/{asin}``.
2. If we have an ``isbn13`` or ``isbn`` identifier, search by ISBN.
3. Fall back to title + author search.
4. Return the book unchanged if nothing is found or there is not enough
   information to search.

What it enriches
----------------
- ASIN identifier (the primary goal — books without ASIN get one)
- ISBN-10 and ISBN-13 identifiers
- Publisher
- Publication date
- Page count (print length)
- Language
- Description
- Cover image URL
- Series name and position (from series info if present)

Merge rules
-----------
The orchestrator calls ``update_book_nulls`` with the returned BookData,
so only NULL fields in the database are overwritten.  New identifier types
discovered via Amazon UK are always merged in.  Authors from universal
sources are ignored by the orchestrator.

Rate limiting
-------------
The stealthy fetcher is used for all requests.  Amazon's WAF is aggressive
and may block requests even with stealth fetching.  The source logs
warnings when requests fail but does not retry to avoid triggering further
rate limiting.
"""

import logging
import re
import html as html_module
from urllib.parse import quote_plus

from book_metadata_scraper.fetcher import SESSION_STEALTHY
from book_metadata_scraper.models import AuthorData, BookData
from book_metadata_scraper.sources.base import BaseUniversalSource
from book_metadata_scraper.sources.registry import universal_source

logger = logging.getLogger(__name__)

BASE_URL = "https://www.amazon.co.uk"


def _strip_html(text: str | None) -> str | None:
    """Remove HTML tags and unescape entities."""
    if not text:
        return None
    text = re.sub(r"<[^>]+>", "", text)
    text = html_module.unescape(text).strip()
    return text or None


def _clean_text(text: str) -> str:
    """Remove control characters and normalize whitespace."""
    # Remove non-printable characters except whitespace
    text = "".join(c for c in text if c.isprintable() or c.isspace())
    # Normalize whitespace
    text = " ".join(text.split())
    return text.strip()


@universal_source
class AmazonUKSource(BaseUniversalSource):
    """Amazon UK — https://www.amazon.co.uk/"""

    name = "amazon_uk"
    session_type = SESSION_STEALTHY

    # ------------------------------------------------------------------
    # Search strategy
    # ------------------------------------------------------------------

    def _build_search_url(
        self, book: BookData, existing_identifiers: dict[str, str]
    ) -> str | None:
        """Build a search URL for finding the book on Amazon UK.

        Returns the URL to fetch, or None if there is not enough information.
        """
        # Priority: ASIN > ISBN > title+author
        asin = existing_identifiers.get("asin")
        if asin:
            return f"{BASE_URL}/dp/{asin}"

        isbn13 = existing_identifiers.get("isbn13")
        isbn = existing_identifiers.get("isbn")
        if isbn13:
            return f"{BASE_URL}/s?k={isbn13}&i=stripbooks"
        if isbn:
            return f"{BASE_URL}/s?k={isbn}&i=stripbooks"

        if book.title and book.authors:
            query = f"{book.title} {book.authors[0].name}"
            return f"{BASE_URL}/s?k={quote_plus(query)}&i=stripbooks"

        return None

    def _is_asin(self, identifier: str) -> bool:
        """Check if a string looks like an Amazon ASIN."""
        return bool(re.match(r"^[A-Z0-9]{10}$", identifier))

    # ------------------------------------------------------------------
    # Search result parsing
    # ------------------------------------------------------------------

    def _parse_search_result(self, response, book: BookData) -> str | None:
        """Extract the book's ASIN from a search results page.

        Returns the ASIN of the best matching result, or None if no match
        found.
        """
        html = response.html_content

        # Find all search result items with valid ASINs
        results = []
        for m in re.finditer(r'data-asin="([A-Z0-9]{10})"', html):
            asin = m.group(1)
            pos = m.end()

            # Check if this is a search result (has data-component-type nearby)
            context_after = html[pos : pos + 500]
            if "s-search-result" not in context_after:
                continue

            # Find the title after this ASIN
            end = min(len(html), pos + 8000)
            title_context = html[pos:end]

            # Extract title from aria-label (most reliable)
            title_match = re.search(
                r'<h2[^>]*aria-label="([^"]*)"[^>]*>', title_context
            )
            if title_match:
                title = title_match.group(1).strip()
                results.append({"asin": asin, "title": title})

        if not results:
            logger.debug("Amazon UK: no search results found")
            return None

        # Find the best match by comparing titles
        best_match = self._find_best_match(results, book.title)
        if best_match:
            logger.debug(
                "Amazon UK: matched '%s' (ASIN %s)", best_match["title"], best_match["asin"]
            )
            return best_match["asin"]

        # If no good match, return the first result's ASIN
        logger.debug(
            "Amazon UK: no title match, using first result ASIN %s",
            results[0]["asin"],
        )
        return results[0]["asin"]

    def _find_best_match(
        self, results: list[dict], target_title: str
    ) -> dict | None:
        """Find the best matching result by comparing titles.

        Uses a simple word-overlap scoring system.
        """
        if not target_title or not results:
            return None

        # Normalize the target title
        target_words = set(
            w.lower() for w in re.split(r"\W+", target_title) if len(w) > 2
        )

        best_score = 0
        best_result = None

        for result in results:
            result_words = set(
                w.lower() for w in re.split(r"\W+", result["title"]) if len(w) > 2
            )
            if not target_words or not result_words:
                continue

            # Calculate Jaccard similarity
            intersection = target_words & result_words
            union = target_words | result_words
            score = len(intersection) / len(union) if union else 0

            if score > best_score:
                best_score = score
                best_result = result

        # Require a minimum similarity score
        if best_score >= 0.3:
            return best_result

        return None

    # ------------------------------------------------------------------
    # Book page parsing
    # ------------------------------------------------------------------

    def _parse_book_page(self, response) -> BookData | str | None:
        """Parse a product page and extract book metadata.

        If this is a non-Kindle page (audiobook, hardcover, etc.),
        try to find the Kindle ASIN from the format links and fetch
        the Kindle page instead, as it has the most complete metadata.

        Returns:
            BookData if parsing succeeded
            str (Kindle ASIN) if we need to fetch the Kindle page instead
            None if parsing failed
        """
        html = response.html_content

        # Check if this is a Kindle page
        format_section = response.css("#formats")
        if format_section:
            # Find the Kindle ASIN from format swatches
            format_html = format_section[0].html_content
            kindle_match = re.search(
                r'id="tmm-grid-swatch-KINDLE"(.*?)(?=id="tmm-grid-swatch-|$)',
                format_html,
                re.DOTALL,
            )
            if kindle_match:
                kindle_html = kindle_match.group(1)
                asin_match = re.search(r"/dp/([A-Z0-9]{10})", kindle_html)
                if asin_match:
                    kindle_asin = asin_match.group(1)
                    # Check if current page is NOT the Kindle page
                    current_asin = None
                    for m in re.finditer(r'data-asin="([A-Z0-9]{10})"', html[:1000]):
                        current_asin = m.group(1)
                        break
                    if current_asin != kindle_asin:
                        # This is a non-Kindle page - we'll need to fetch the Kindle page
                        logger.debug(
                            "Amazon UK: non-Kindle page, Kindle ASIN is %s",
                            kindle_asin,
                        )
                        return kindle_asin

        # --- Title ---
        title_el = response.css("#productTitle")
        title = _clean_text(title_el[0].text) if title_el else None
        if not title:
            logger.debug("Amazon UK: no title found on product page")
            return None

        # --- Author ---
        authors = []
        author_el = response.css("#bylineInfo")
        if author_el:
            author_text = author_el[0].text.strip()
            # Extract author names from links
            author_links = author_el[0].css("a")
            for link in author_links:
                name = link.text.strip()
                if name and name not in ("(Author)", "(Author), "):
                    # Clean up author name
                    name = name.strip().rstrip(",").strip()
                    if name:
                        authors.append(AuthorData(name=name))

        # --- Product details ---
        details = self._parse_product_details(html)

        # --- Description ---
        description = self._parse_description(html)

        # --- Cover image ---
        cover_image = self._parse_cover_image(response)

        # --- ASIN (from page itself) ---
        asin = details.get("asin", "")

        # --- Identifiers ---
        identifiers: dict[str, str] = {}
        if asin:
            identifiers["asin"] = asin
        isbn13 = details.get("isbn13", "").replace("-", "")
        isbn = details.get("isbn10", "").replace("-", "")
        if isbn13:
            identifiers["isbn13"] = isbn13
        if isbn:
            identifiers["isbn"] = isbn

        return BookData(
            title=title,
            authors=authors,
            description=description,
            publisher=details.get("publisher"),
            published_date=details.get("publication_date"),
            page_count=self._parse_page_count(details.get("print_length")),
            language=self._normalize_language(details.get("language")),
            cover_image_url=cover_image,
            identifiers=identifiers,
        )

    def _parse_product_details(self, html: str) -> dict[str, str]:
        """Extract product details from the detail bullets section.

        Handles two formats:
        1. Standard books: <ul> with <li> items (label : value)
        2. Audiobooks: <table> with <tr> rows (<th> label, <td> value)
        """
        details = {}

        # Find the product details section
        detail_start = html.find("Product details")
        if detail_start == -1:
            return details

        detail_section = html[detail_start : detail_start + 10000]

        # Try list format first (standard books)
        items = re.findall(r"<li>(.*?)</li>", detail_section, re.DOTALL)
        if items:
            for item in items:
                text = re.sub(r"<[^>]+>", " ", item)
                text = _clean_text(text)
                self._parse_detail_pair(text, details)
        else:
            # Try table format (audiobooks)
            rows = re.findall(r"<tr[^>]*>(.*?)</tr>", detail_section, re.DOTALL)
            for row in rows:
                # Extract label from <th> and value from <td>
                th_match = re.search(r"<th[^>]*>(.*?)</th>", row, re.DOTALL)
                td_match = re.search(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
                if th_match and td_match:
                    # Remove HTML tags and get text
                    label = re.sub(r"<[^>]+>", " ", th_match.group(1))
                    label = _clean_text(label)
                    value = re.sub(r"<[^>]+>", " ", td_match.group(1))
                    value = _clean_text(value)
                    if label and value:
                        self._parse_detail_pair(f"{label} : {value}", details)

        return details

    def _parse_detail_pair(self, text: str, details: dict[str, str]) -> None:
        """Parse a key-value pair from a detail line and add to details dict."""
        parts = re.split(r"\s*:\s*", text, maxsplit=1)
        if len(parts) != 2:
            return

        key = parts[0].strip().lower()
        value = parts[1].strip()

        # Map known keys
        if "asin" in key:
            details["asin"] = value
        elif "publisher" in key:
            details["publisher"] = value
        elif "publication date" in key:
            details["publication_date"] = value
        elif "isbn-13" in key:
            details["isbn13"] = value
        elif "isbn-10" in key:
            details["isbn10"] = value
        elif "print length" in key:
            details["print_length"] = value
        elif "language" in key:
            details["language"] = value
        elif "edition" in key:
            details["edition"] = value

    def _parse_description(self, html: str) -> str | None:
        """Extract the book description from the page."""
        # Try the book description feature div
        desc_match = re.search(
            r'id="bookDescription_feature_div".*?data-a-expander-name="book_description_expander"[^>]*>(.*?)</div>\s*</div>\s*</div>',
            html,
            re.DOTALL,
        )
        if desc_match:
            desc_html = desc_match.group(1)
            desc_text = _strip_html(desc_html)
            if desc_text:
                return desc_text

        # Fallback: try to find any description
        desc_match = re.search(r'id="bookDescription_feature_div".*?<p>(.*?)</p>', html, re.DOTALL)
        if desc_match:
            desc_html = desc_match.group(1)
            desc_text = _strip_html(desc_html)
            if desc_text:
                return desc_text

        return None

    def _parse_cover_image(self, response) -> str | None:
        """Extract the cover image URL."""
        # Try #imgBlkFront first (hardcover/paperback)
        cover_el = response.css("#imgBlkFront")
        if cover_el:
            src = cover_el[0].attrib.get("src", "")
            if src:
                return src

        # Try #landingImage (kindle)
        cover_el = response.css("#landingImage")
        if cover_el:
            src = cover_el[0].attrib.get("src", "")
            if src:
                return src

        return None

    def _parse_page_count(self, text: str | None) -> int | None:
        """Parse page count from text like '476 pages'."""
        if not text:
            return None
        match = re.search(r"(\d+)", text)
        if match:
            return int(match.group(1))
        return None

    def _normalize_language(self, text: str | None) -> str | None:
        """Normalize language name to BCP 47 tag."""
        if not text:
            return None
        lang_map = {
            "english": "en",
            "french": "fr",
            "german": "de",
            "spanish": "es",
            "italian": "it",
            "portuguese": "pt",
            "dutch": "nl",
            "japanese": "ja",
            "chinese": "zh",
            "russian": "ru",
        }
        return lang_map.get(text.lower(), text.lower())

    # ------------------------------------------------------------------
    # Main enrichment entry point
    # ------------------------------------------------------------------

    async def _enrich_from_asin(
        self,
        asin: str,
        existing_identifiers: dict[str, str],
        book: BookData,
        max_redirects: int = 2,
    ) -> BookData:
        """Fetch a product page by ASIN and parse it.

        Handles the case where the ASIN points to a non-Kindle page
        by following redirects to the Kindle page.

        Args:
            asin: The ASIN to fetch
            existing_identifiers: Already-known identifiers for the book
            book: The original book data (for fallback)
            max_redirects: Maximum number of Kindle redirects to follow

        Returns:
            BookData with enriched fields, or original book if not found
        """
        for _ in range(max_redirects):
            product_url = f"{BASE_URL}/dp/{asin}"
            try:
                product_response = await self.session.fetch_stealthy(
                    product_url, timeout=30000
                )
            except Exception:
                logger.exception(
                    "Amazon UK: product page request failed for %s", product_url
                )
                return book

            result = self._parse_book_page(product_response)

            if result is None:
                # parse_book_page failed
                return book

            if isinstance(result, str):
                # result is a Kindle ASIN - fetch that page instead
                logger.debug(
                    "Amazon UK: redirecting from ASIN %s to Kindle ASIN %s",
                    asin,
                    result,
                )
                asin = result
                continue

            # result is a BookData - process it
            # Only return new identifiers we don't already have
            new_identifiers = {
                k: v
                for k, v in result.identifiers.items()
                if k not in existing_identifiers
            }
            result.identifiers = new_identifiers
            # Set title and authors to sentinels (universal sources don't update these)
            result.title = ""
            result.authors = []
            return result

        logger.warning(
            "Amazon UK: too many redirects for ASIN %s", asin
        )
        return book

    async def enrich(
        self, book: BookData, existing_identifiers: dict[str, str]
    ) -> BookData:
        """Look up *book* on Amazon UK and return enriched data."""
        # Build search URL
        url = self._build_search_url(book, existing_identifiers)
        if url is None:
            logger.debug(
                "Amazon UK: not enough info to search for '%s'", book.title
            )
            return book

        try:
            response = await self.session.fetch_stealthy(url, timeout=30000)
        except Exception:
            logger.exception("Amazon UK: request failed for %s", url)
            return book

        # If this is a direct product page (we had an ASIN), parse it directly
        asin = existing_identifiers.get("asin")
        if asin and "/dp/" in url:
            return await self._enrich_from_asin(asin, existing_identifiers, book)

        # This is a search results page — find the ASIN
        found_asin = self._parse_search_result(response, book)
        if not found_asin:
            logger.debug("Amazon UK: no match found for '%s'", book.title)
            return book

        return await self._enrich_from_asin(found_asin, existing_identifiers, book)
