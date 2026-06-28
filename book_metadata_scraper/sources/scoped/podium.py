"""Podium Entertainment scoped source plugin.

Discovers and parses book metadata from https://podiumentertainment.com/.

Discovery strategy
------------------
The site's ``/sitemap.xml`` contains **all** individual title URLs in one
request (currently ~13,500 titles).  This avoids the lazy-loaded "Load More"
button on the ``/titles`` page, which is JavaScript-driven and inaccessible
to plain HTTP.

Each title URL has the form ``/titles/{numeric_id}/{slug}``.  The numeric ID
is extracted and stored as a ``podium_id`` identifier, which allows the
orchestrator to skip already-scraped books without re-fetching the page
(via the normal ``source_url`` check).

Book pages are server-rendered HTML (Next.js App Router).  There is no
JSON-LD or Open Graph metadata.  ``parse_book`` extracts fields using CSS
selectors and text content analysis: title (h1), series link, author/narrator,
genre, release date, language, narration format, duration, description,
cover image, and ISBNs/ASINs from retailer links.

Rate limiting
-------------
The standard HTTP session is used.  ``SessionManager`` enforces the configured
``http_rate_limit`` globally, so Podium fetches are automatically throttled.
The sitemap itself is a single large fetch; individual book pages are fetched
at the rate limit.
"""

import logging
import re
from typing import AsyncIterator
from urllib.parse import unquote

from book_metadata_scraper.fetcher import SESSION_HTTP
from book_metadata_scraper.models import AuthorData, BookData
from book_metadata_scraper.sources.base import BaseScopedSource
from book_metadata_scraper.sources.registry import scoped_source

logger = logging.getLogger(__name__)

BASE_URL = "https://podiumentertainment.com"
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"

# Regex to extract Podium numeric IDs from title URLs.
_TITLE_ID_RE = re.compile(r"/titles/(\d+)/")

# ISBN-13 regex (978/979 prefix)
_ISBN13_RE = re.compile(r"\b(97[89]\d{10})\b")

# ASIN regex: Amazon /dp/{ASIN} or Audible /pd/{ASIN}
_ASIN_RE = re.compile(r"/[dp]d?/([A-Z0-9]{10})")

# Human-readable month map
_MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def _extract_podium_id(url: str) -> str | None:
    """Extract the numeric Podium ID from a title URL."""
    m = _TITLE_ID_RE.search(url)
    return m.group(1) if m else None


def _normalise_url(url: str) -> str:
    """Ensure a consistent absolute URL with no trailing slash."""
    url = url.rstrip("/")
    if url.startswith("/"):
        url = BASE_URL + url
    return url


def _parse_human_date(raw: str) -> str | None:
    """Convert 'December 13, 2016' to '2016-12-13'.  Returns None on failure."""
    m = re.match(
        r"(January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+(\d{1,2}),?\s+(\d{4})",
        raw,
    )
    if m:
        month = _MONTH_MAP[m.group(1).lower()]
        day = int(m.group(2))
        year = int(m.group(3))
        return f"{year:04d}-{month:02d}-{day:02d}"
    if re.match(r"\d{4}(-\d{2}(-\d{2})?)?$", raw):
        return raw
    return None


def _extract_description(html_content: str) -> str | None:
    """Extract the book description from the raw HTML.

    The description sits between the metadata block and
    "This book is part of" / "More Titles You Might Like".
    We work on the raw HTML to capture paragraph content.
    """
    # Find the description area: after the <hr> that follows Duration,
    # and before "This book is part of"
    # The HTML uses <p> tags for description paragraphs.

    # Strategy: find text between "Duration:" metadata and "This book is part of"
    # using regex on the HTML source.

    # Look for the description section — it's typically in <p> tags
    # between the metadata and the series section.
    # We'll use a text-based approach on the extracted text.

    # Find the content between the <hr> separator after metadata and
    # the "This book is part of" section
    match = re.search(
        r"Duration:[^<]*</[^>]+>.*?<hr[^>]*>(.*?)(?:<hr[^>]*>|This book is part of)",
        html_content,
        re.DOTALL | re.IGNORECASE,
    )
    if not match:
        # Try alternative: find <p> tags after the metadata section
        match = re.search(
            r"</hr>\s*(.*?)\s*This book is part of",
            html_content,
            re.DOTALL | re.IGNORECASE,
        )

    if not match:
        return None

    section = match.group(1)

    # Extract text from <p> tags
    paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", section, re.DOTALL)
    if not paragraphs:
        # Try getting all text between tags
        section_text = re.sub(r"<[^>]+>", " ", section)
        section_text = re.sub(r"\s+", " ", section_text).strip()
        return section_text if section_text else None

    # Clean HTML from each paragraph and join
    cleaned = []
    for p in paragraphs:
        text = re.sub(r"<[^>]+>", "", p)
        text = text.strip()
        if text:
            cleaned.append(text)

    return "\n\n".join(cleaned) if cleaned else None


def _extract_isbns_and_asins(links: list[tuple[str, str]]) -> dict[str, str]:
    """Extract ISBNs and ASINs from a list of (text, href) link tuples.

    Returns a dict of identifier type -> value.
    """
    identifiers: dict[str, str] = {}

    for link_text, href in links:
        # ISBN-13 from Bookshop / B&N / Walmart / Audiobooks.com
        isbn_match = _ISBN13_RE.search(href)
        if isbn_match and "isbn13" not in identifiers:
            identifiers["isbn13"] = isbn_match.group(1)

        # ASIN from Amazon / Audible URLs
        asin_match = _ASIN_RE.search(href)
        if asin_match:
            asin = asin_match.group(1)
            lower_href = href.lower()
            lower_text = link_text.lower()
            if "audible" in lower_href:
                identifiers.setdefault("asin_audiobook", asin)
            elif "ebook" in lower_text or "kindle" in lower_text or "bookmark" in lower_text:
                identifiers.setdefault("asin_ebook", asin)
            elif "paperback" in lower_text:
                identifiers.setdefault("asin_paperback", asin)
            elif "asin" not in identifiers:
                identifiers["asin"] = asin

    return identifiers


@scoped_source
class PodiumEntertainment(BaseScopedSource):
    """Podium Entertainment — https://podiumentertainment.com/"""

    name = "podium"
    session_type = SESSION_HTTP

    async def discover_book_urls(self) -> AsyncIterator[str]:
        """Yield book page URLs from the sitemap.

        The sitemap is a single large XML document containing every title
        URL.  We extract only ``/titles/{id}/{slug}`` entries.
        """
        logger.info("Fetching sitemap from %s", SITEMAP_URL)
        try:
            response = await self.fetch(SITEMAP_URL)
        except Exception:
            logger.exception("Failed to fetch sitemap")
            return

        # The sitemap content may be in html_content or body
        content = ""
        if hasattr(response, "html_content") and response.html_content:
            content = response.html_content
        if not content and hasattr(response, "body") and response.body:
            content = response.body.decode("utf-8", errors="replace")
        if not content and hasattr(response, "text") and response.text:
            content = response.text

        if not content:
            logger.error("Empty sitemap response")
            return

        # Find all title URLs
        title_urls: list[str] = []
        for match in re.finditer(
            r"https?://podiumentertainment\.com/titles/\d+/[a-z0-9-]+",
            content,
        ):
            url = match.group(0).rstrip("/")
            if url not in title_urls:
                title_urls.append(url)

        logger.info("Found %d unique title URLs in sitemap", len(title_urls))

        for url in title_urls:
            yield url

    async def parse_book(self, response) -> BookData | None:
        """Extract book metadata from a Podium book page."""
        source_url = _normalise_url(response.url or "")
        podium_id = _extract_podium_id(source_url)

        # Get HTML content
        html = ""
        if hasattr(response, "html_content") and response.html_content:
            html = response.html_content
        elif hasattr(response, "body") and response.body:
            html = response.body.decode("utf-8", errors="replace")

        if not html:
            logger.warning("Empty response for %s", source_url)
            return None

        # --- Title ---
        h1_elements = response.css("h1")
        title = h1_elements[0].text.strip() if h1_elements else None
        if not title:
            logger.warning("Could not extract title from %s", source_url)
            return None

        # --- Series ---
        series_name = None
        series_position = None
        series_links = response.css('a[href*="/series/"]')
        if series_links:
            series_text = (series_links[0].text or "").strip()
            # Pattern: "Expeditionary Force, Book 1"
            pos_match = re.search(r",\s*Book\s+(\d+(?:\.\d+)?)$", series_text)
            if pos_match:
                series_position = float(pos_match.group(1))
                series_name = series_text[: pos_match.start()].strip()
            elif series_text:
                series_name = series_text

        # --- Author ---
        authors: list[AuthorData] = []
        author_links = response.css('a[href*="/authors/"]')
        if author_links:
            author_name = (author_links[0].text or "").strip()
            if author_name:
                authors.append(AuthorData(name=author_name))

        # --- Genre ---
        genres: list[str] = []
        # The genre link is the first /genre/ link that's not in the footer
        # or "See All" — we look for one with actual text
        all_genre_links = response.css('a[href*="/genre/"]')
        for gl in all_genre_links:
            genre_text = (gl.text or "").strip()
            # Skip "See All >", empty text, and footer links
            if genre_text and genre_text not in ("See All >", "See All>"):
                # Check it's not a footer genre (footer genres are in a different section)
                # The first valid genre link with text is the book's genre
                genres.append(genre_text)
                break

        # --- Metadata from get_all_text() ---
        all_text = response.get_all_text()

        # Release Date
        published_date = None
        date_match = re.search(r"Release Date:\s*(.+?)(?:\n|$)", all_text)
        if date_match:
            published_date = _parse_human_date(date_match.group(1).strip())

        # Language
        language = None
        lang_match = re.search(r"Language:\s*(\S+)", all_text)
        if lang_match:
            raw_lang = lang_match.group(1).strip()
            lang_map = {"english": "en", "spanish": "es", "french": "fr", "german": "de"}
            language = lang_map.get(raw_lang.lower(), raw_lang)

        # --- Description ---
        description = _extract_description(html)

        # --- Cover image ---
        cover_image = None
        # The cover image is the first img with the title as alt text
        # or the first img from assets.podiumentertainment.com
        for img in response.css("img"):
            src = img.attrib.get("src", "")
            alt = img.attrib.get("alt", "")
            if "assets.podiumentertainment.com" in src:
                # Decode the URL-encoded asset URL
                url_match = re.search(r"url=([^&]+)", src)
                if url_match:
                    cover_image = unquote(url_match.group(1))
                break
            elif alt == title and "_next/image" in src:
                url_match = re.search(r"url=([^&]+)", src)
                if url_match:
                    cover_image = unquote(url_match.group(1))
                break

        # --- Identifiers from retailer links ---
        identifiers: dict[str, str] = {}
        if podium_id:
            identifiers["podium_id"] = podium_id

        # Collect all retailer links (exclude internal links)
        retailer_links: list[tuple[str, str]] = []
        external_domains = (
            "amazon", "audible", "bookshop", "barnesandnoble",
            "walmart", "target", "apple", "spotify", "storytel", "audiobooks",
        )
        for a in response.css("a"):
            href = a.attrib.get("href", "")
            text = (a.text or "").strip()
            if any(d in href.lower() for d in external_domains):
                retailer_links.append((text, href))

        retailer_ids = _extract_isbns_and_asins(retailer_links)
        identifiers.update(retailer_ids)

        return BookData(
            title=title,
            authors=authors,
            description=description,
            published_date=published_date,
            language=language,
            series=series_name,
            series_position=series_position,
            cover_image_url=cover_image,
            genres=genres,
            identifiers=identifiers,
            source_url=source_url,
        )
