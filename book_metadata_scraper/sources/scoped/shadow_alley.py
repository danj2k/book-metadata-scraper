"""Shadow Alley Press scoped source plugin.

Discovers and parses book metadata from https://shadowalleypress.com/.

Discovery strategy
------------------
The ``/library/`` page lists all series as links to ``/book-series/{slug}/``
pages (currently ~58 series).  Each series page contains article elements for
every book in that series, with article order matching the in-series position.

Individual book page URLs have the form ``/book/{slug}/``.  The slug is
extracted and stored as a ``shadow_alley_id`` identifier, which allows the
orchestrator to skip already-scraped books.

Book pages are server-rendered WordPress/Genesis with ``CreativeWork``
microdata.  ``parse_book`` extracts fields from:

- CSS classes on the ``<article>`` element (authors, series, genres/tags)
- ``itemprop="headline"`` for the title
- Metadata ``<li>`` items (pages, duration, published date, formats, narrator)
- ``<footer>`` for series link and genre tags
- ``<p>`` tags in ``itemprop="text"`` for the description
- ``<img>`` for the cover image

Rate limiting
-------------
The standard HTTP session is used.  ``SessionManager`` enforces the configured
``http_rate_limit`` globally, so Shadow Alley fetches are automatically
throttled.
"""

import logging
import re
from typing import AsyncIterator

from book_metadata_scraper.fetcher import SESSION_HTTP
from book_metadata_scraper.models import AuthorData, BookData
from book_metadata_scraper.sources.base import BaseScopedSource
from book_metadata_scraper.sources.registry import scoped_source

logger = logging.getLogger(__name__)

BASE_URL = "https://shadowalleypress.com"
LIBRARY_URL = f"{BASE_URL}/library/"

# Slug from /book/{slug}/ URL — used as unique identifier
_BOOK_SLUG_RE = re.compile(r"/book/([a-z0-9-]+)/?$")

# Series slug from /book-series/{slug}/ URL
_SERIES_SLUG_RE = re.compile(r"/book-series/([a-z0-9-]+)/?$")

# Human-readable month map
_MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def _extract_slug(url: str) -> str | None:
    """Extract the book slug from a /book/{slug}/ URL."""
    m = _BOOK_SLUG_RE.search(url)
    return m.group(1) if m else None


def _parse_human_date(raw: str) -> str | None:
    """Convert 'July 24, 2018' to '2018-07-24'.  Returns None on failure."""
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


def _slug_to_name(slug: str) -> str:
    """Convert a URL slug to a human-readable name.

    ``eden-hudson`` → ``Eden Hudson``
    ``james-a-hunter`` → ``James A Hunter``
    """
    return " ".join(word.capitalize() for word in slug.split("-"))


def _extract_description(html: str) -> str | None:
    """Extract the book description from the page.

    Handles two templates:
    1. Genesis: description inside ``itemprop="text"`` between ``<h3>`` tagline
       and the review/quote paragraphs.
    2. WordPress blocks: description in ``<p>`` tags within the post content
       area, after the title heading.
    """
    # --- Template 1: Genesis with itemprop="text" ---
    text_match = re.search(
        r'itemprop="text"[^>]*>(.*?)(?:<footer|<section[^>]*class="[^"]*footer)',
        html,
        re.DOTALL,
    )
    if text_match:
        section = text_match.group(1)
        h3_match = re.search(r"<h3[^>]*>.*?</h3>", section, re.DOTALL)
        if h3_match:
            after_h3 = section[h3_match.end():]
            paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", after_h3, re.DOTALL)
            if paragraphs:
                cleaned = []
                for p in paragraphs:
                    text = re.sub(r"<[^>]+>", "", p).strip()
                    if not text:
                        continue
                    if text.startswith('"') or text.startswith("\u201c"):
                        break
                    if text.startswith("—") or text.startswith("--"):
                        break
                    cleaned.append(text)
                if cleaned:
                    return "\n\n".join(cleaned)

    # --- Template 2: WordPress blocks ---
    # Description is in <p> tags after the wp-block-post-title heading,
    # before "The Complete Series" or "You Might Also Like" sections.
    # Strip <style> and <script> blocks to avoid matching CSS content.
    stripped = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL)
    stripped = re.sub(r"<script[^>]*>.*?</script>", "", stripped, flags=re.DOTALL)

    # Find the position of the wp-block-post-title heading
    title_pos = 0
    title_heading = re.search(
        r'<h[1-3][^>]*class="[^"]*wp-block-post-title[^"]*"', stripped
    )
    if title_heading:
        title_pos = title_heading.end()

    # Only look at content after the title heading
    after_title = stripped[title_pos:]

    # Extract <p> tags — filter to description paragraphs only (wp-block-paragraph)
    all_paras = re.findall(r'<p[^>]*class="[^"]*wp-block-paragraph[^"]*"[^>]*>(.*?)</p>', after_title, re.DOTALL)
    # If no wp-block-paragraph found, fall back to all <p> tags
    if not all_paras:
        all_paras = re.findall(r"<p[^>]*>(.*?)</p>", after_title, re.DOTALL)

    description_parts: list[str] = []
    for p in all_paras:
        text = re.sub(r"<[^>]+>", "", p).strip()
        if not text or len(text) < 20:
            continue
        # Stop at section markers
        if any(marker in text for marker in (
            "The Complete Series", "You Might Also Like",
            "Book Details", "Copyright", "©",
            "Great Genre Fiction", "Available At",
        )):
            break
        description_parts.append(text)

    return "\n\n".join(description_parts) if description_parts else None


def _extract_cover_image(html: str) -> str | None:
    """Extract the cover image URL from the book page.

    The cover is the first ``<img>`` inside the ``.author-pro-featured-image``
    div, or the first large image on the page.
    """
    # Look for the featured image container
    img_match = re.search(
        r'class="author-pro-featured-image"[^>]*>\s*<img[^>]*src="([^"]+)"',
        html,
    )
    if img_match:
        return img_match.group(1)

    # Fallback: first img with a reasonable src
    for img_match in re.finditer(r'<img[^>]*src="([^"]+)"', html):
        src = img_match.group(1)
        if "shadowalley" in src or "smushcdn" in src:
            return src

    return None


def _extract_metadata_from_text(html: str) -> dict[str, str | None]:
    """Extract metadata from the list items on the book page.

    Returns a dict with keys: pages, duration, published, narrator, formats.
    """
    result: dict[str, str | None] = {
        "pages": None,
        "duration": None,
        "published": None,
        "narrator": None,
        "formats": None,
    }

    # Find all <li> items within the book details section
    items = re.findall(r"<li[^>]*>(.*?)</li>", html, re.DOTALL)
    for item in items:
        text = re.sub(r"<[^>]+>", "", item).strip()
        if text.startswith("Pages:"):
            result["pages"] = text.replace("Pages:", "").strip()
        elif text.startswith("Duration:"):
            result["duration"] = text.replace("Duration:", "").strip()
        elif text.startswith("Published:"):
            result["published"] = text.replace("Published:", "").strip()
        elif text.startswith("Narrator:"):
            result["narrator"] = text.replace("Narrator:", "").strip()
        elif text.startswith("Available in:"):
            result["formats"] = text.replace("Available in:", "").strip()

    return result


@scoped_source
class ShadowAlleyPress(BaseScopedSource):
    """Shadow Alley Press — https://shadowalleypress.com/"""

    name = "shadow_alley"
    session_type = SESSION_HTTP

    async def discover_book_urls(self) -> AsyncIterator[tuple[str, float | None]]:
        """Yield (url, position) tuples from series pages.

        First discovers all series from the library page, then for each
        series fetches the series page and yields individual book URLs
        with their position in the series (derived from article order).
        """
        logger.info("Fetching library page from %s", LIBRARY_URL)
        try:
            response = await self.fetch(LIBRARY_URL)
        except Exception:
            logger.exception("Failed to fetch library page")
            return

        html = ""
        if hasattr(response, "html_content") and response.html_content:
            html = response.html_content
        if not html and hasattr(response, "body") and response.body:
            html = response.body.decode("utf-8", errors="replace")

        if not html:
            logger.error("Empty library page response")
            return

        # Extract series URLs
        series_urls = list(dict.fromkeys(
            m.group(0) for m in re.finditer(
                r"https://shadowalleypress\.com/book-series/[a-z0-9-]+/",
                html,
            )
        ))
        logger.info("Found %d series on library page", len(series_urls))

        # Also extract standalone book URLs (New Releases section)
        standalone_urls = list(dict.fromkeys(
            m.group(0) for m in re.finditer(
                r"https://shadowalleypress\.com/book/[a-z0-9-]+/",
                html,
            )
        ))
        # Filter out box sets and omnibuses
        standalone_urls = [
            url for url in standalone_urls
            if not any(kw in url for kw in ("box", "omnibus", "expansion-pack"))
        ]
        logger.info("Found %d standalone book URLs on library page", len(standalone_urls))

        # Track all book URLs we yield to avoid duplicates
        seen_slugs: set[str] = set()

        # Process each series page
        for series_url in series_urls:
            series_slug_m = _SERIES_SLUG_RE.search(series_url)
            series_name = _slug_to_name(series_slug_m.group(1)) if series_slug_m else None

            try:
                series_response = await self.fetch(series_url)
            except Exception:
                logger.warning("Failed to fetch series page %s", series_url)
                continue

            series_html = ""
            if hasattr(series_response, "html_content") and series_response.html_content:
                series_html = series_response.html_content
            if not series_html and hasattr(series_response, "body") and series_response.body:
                series_html = series_response.body.decode("utf-8", errors="replace")

            if not series_html:
                logger.warning("Empty series page response for %s", series_url)
                continue

            # Extract book URLs from article elements (in order = position)
            # Articles are inside <article> tags with CreativeWork microdata
            book_entries = re.findall(
                r'<article[^>]*CreativeWork[^>]*>.*?'
                r'href="(https://shadowalleypress\.com/book/[a-z0-9-]+/)".*?'
                r'</article>',
                series_html,
                re.DOTALL,
            )

            for position, book_url in enumerate(book_entries, start=1):
                slug = _extract_slug(book_url)
                if slug and slug not in seen_slugs:
                    seen_slugs.add(slug)
                    yield (book_url, float(position))

        # Process standalone books not already discovered via series pages
        for url in standalone_urls:
            slug = _extract_slug(url)
            if slug and slug not in seen_slugs:
                seen_slugs.add(slug)
                yield (url, None)

    async def parse_book(self, response) -> BookData | None:
        """Extract book metadata from a Shadow Alley Press book page."""
        source_url = response.url or ""
        if source_url:
            source_url = source_url.rstrip("/")

        # Get HTML content
        html = ""
        if hasattr(response, "html_content") and response.html_content:
            html = response.html_content
        elif hasattr(response, "body") and response.body:
            html = response.body.decode("utf-8", errors="replace")

        if not html:
            logger.warning("Empty response for %s", source_url)
            return None

        # Check for 404
        if "Not found, error 404" in html:
            logger.warning("404 page: %s", source_url)
            return None

        # --- Slug as identifier ---
        slug = _extract_slug(source_url)

        # --- Title (two templates: Genesis with entry-title, blocks with wp-block-post-title) ---
        title = None
        # Genesis template: <h1 class="entry-title" itemprop="headline">
        title_match = re.search(
            r'class="entry-title"[^>]*>(.*?)</h1>', html, re.DOTALL
        )
        if not title_match:
            # WordPress block template: <h1/h2/h3 class="...wp-block-post-title...">
            # Must match actual HTML elements, not CSS selectors in <style> tags
            title_match = re.search(
                r'<h[1-3][^>]*class="[^"]*wp-block-post-title[^"]*"[^>]*>(.*?)</h[1-3]>',
                html, re.DOTALL,
            )
        if title_match:
            title = re.sub(r"<[^>]+>", "", title_match.group(1)).strip()
        if not title:
            # Fallback: og:title (strip " | Shadow Alley Press" suffix)
            og_match = re.search(r'og:title["\s]*content="([^"]+)"', html)
            if og_match:
                title = re.sub(r"\s*\|\s*Shadow Alley Press$", "", og_match.group(1)).strip()
        if not title:
            logger.warning("Could not extract title from %s", source_url)
            return None

        # --- Authors from article CSS classes ---
        # The article element has classes like: book-authors-eden-hudson book-authors-james-a-hunter
        article_match = re.search(r'<article[^>]*class="([^"]+)"', html)
        author_slugs: list[str] = []
        if article_match:
            author_slugs = re.findall(r"book-authors-([a-z0-9-]+)", article_match.group(1))

        authors: list[AuthorData] = []
        if author_slugs:
            for author_slug in author_slugs:
                authors.append(AuthorData(name=_slug_to_name(author_slug)))
        else:
            # Fallback: try author links in the HTML
            author_links = re.findall(
                r'class="book-author-link"[^>]*>([^<]+)', html
            )
            for name in author_links:
                name = name.strip()
                if name:
                    authors.append(AuthorData(name=name))

        # --- Series info ---
        series_name = None
        series_position = None

        # Try the book-series-book element (may be <span> or <p> depending on template):
        # <span class="book-series-book"><strong>Rogue Dungeon</strong> Book <strong>1</strong></span>
        # <p class="book-series-book"><strong>The Desert Druid</strong> Book <strong> 3</strong></p>
        series_span = re.search(
            r'book-series-book[^>]*>.*?<strong>([^<]+)</strong>\s*(?:Book|book)\s*<strong>\s*(\d+(?:\.\d+)?)</strong>',
            html,
            re.DOTALL,
        )
        if series_span:
            series_name = series_span.group(1).strip()
            series_position = float(series_span.group(2))
        else:
            # Try the footer series link
            footer_series = re.search(
                r'<footer[^>]*>.*?Series:\s*<a[^>]*>([^<]+)</a>',
                html,
                re.DOTALL,
            )
            if footer_series:
                series_name = footer_series.group(1).strip()

        # --- Genres from article CSS classes and footer tags ---
        genres: list[str] = []
        if article_match:
            # Tags from CSS classes: book-tags-high-fantasy
            tag_slugs = re.findall(r"book-tags-([a-z0-9-]+)", article_match.group(1))
            genres.extend(_slug_to_name(ts) for ts in tag_slugs)

        # Also check footer "Tagged with:" links
        tag_links = re.findall(
            r'Tagged with:.*?</p>', html, re.DOTALL
        )
        if tag_links:
            footer_genres = re.findall(r"<a[^>]*>([^<]+)</a>", tag_links[0])
            for g in footer_genres:
                g = g.strip()
                if g and g not in genres:
                    genres.append(g)

        # --- Metadata from list items ---
        meta = _extract_metadata_from_text(html)

        page_count = None
        if meta["pages"]:
            try:
                page_count = int(meta["pages"])
            except ValueError:
                pass

        published_date = None
        if meta["published"]:
            published_date = _parse_human_date(meta["published"])

        # --- Description ---
        description = _extract_description(html)

        # --- Cover image ---
        cover_image = _extract_cover_image(html)

        # --- Identifiers ---
        identifiers: dict[str, str] = {}
        if slug:
            identifiers["shadow_alley_id"] = slug

        return BookData(
            title=title,
            authors=authors,
            description=description,
            published_date=published_date,
            page_count=page_count,
            series=series_name,
            series_position=series_position,
            cover_image_url=cover_image,
            genres=genres,
            identifiers=identifiers,
            source_url=source_url,
        )
