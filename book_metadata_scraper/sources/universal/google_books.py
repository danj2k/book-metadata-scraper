"""Google Books API universal source plugin.

Enriches books already in the database with metadata from the Google Books
Volumes API (``GET /books/v1/volumes``).  No user authentication is needed —
only public Volume data is accessed.

Query strategy
--------------
1. If we have an ``isbn13`` or ``isbn`` identifier, search with ``isbn:``.
2. If we have a ``google_books`` volume ID, fetch it directly via
   ``/volumes/{id}`` — no search needed.
3. Fall back to ``intitle:`` + ``inauthor:`` using the first author.
4. Return the book unchanged if nothing is found or there is not enough
   information to search.

Merge rules
-----------
The orchestrator calls ``update_book_nulls`` with the returned BookData,
so only NULL fields in the database are overwritten.  New identifier types
discovered via Google Books are always merged in.  Authors from universal
sources are ignored by the orchestrator (see ``base.py`` docs).

Rate limiting
-------------
Google Books API has strict rate limits:
- 1 request per 1.5 seconds (enforced via ``rate_limit`` class attribute)
- 1,000 requests per 24-hour period (tracked via ``_daily_call_count``)

When a 429 is received or the daily limit is exceeded, a
``RateLimitExhausted`` exception is raised.  The orchestrator catches
this and stops enrichment for this source, preserving whatever data
was gathered so far.  The remaining books are picked up on the next run.
"""

import html
import logging
import re
import time
import urllib.parse

from book_metadata_scraper.fetcher import SESSION_HTTP
from book_metadata_scraper.models import BookData
from book_metadata_scraper.sources.base import (
    BaseUniversalSource,
    RateLimitExhausted,
)
from book_metadata_scraper.sources.registry import universal_source

logger = logging.getLogger(__name__)

BASE_URL = "https://www.googleapis.com/books/v1/volumes"

# Daily rate limit constants
DAILY_CALL_LIMIT = 1000
DAILY_LIMIT_RESET_SECONDS = 24 * 60 * 60  # 24 hours


def _strip_html(text: str | None) -> str | None:
    """Remove HTML tags and unescape entities."""
    if not text:
        return None
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).strip()
    return text or None


@universal_source
class GoogleBooksSource(BaseUniversalSource):
    """Google Books — https://books.google.com/"""

    name = "google_books"
    session_type = SESSION_HTTP
    rate_limit = 1.5  # 1 request every 1.5 seconds

    # Daily rate limit tracking (class-level, shared across instances)
    _daily_call_count: int = 0
    _daily_call_window_start: float = 0.0

    # ------------------------------------------------------------------
    # Daily rate limit tracking
    # ------------------------------------------------------------------

    @classmethod
    def _check_daily_limit(cls) -> None:
        """Check if we've exceeded the daily call limit.

        Raises RateLimitExhausted if the limit is reached.
        Resets the counter if the 24-hour window has expired.
        """
        now = time.monotonic()

        # Check limit BEFORE resetting — otherwise the reset on first
        # call after expiry would wipe a just-hit limit
        if cls._daily_call_count >= DAILY_CALL_LIMIT:
            raise RateLimitExhausted(
                f"Google Books daily limit reached: {cls._daily_call_count}/{DAILY_CALL_LIMIT} calls"
            )

        # Reset window if more than 24 hours have passed
        if now - cls._daily_call_window_start > DAILY_LIMIT_RESET_SECONDS:
            logger.info(
                "Google Books: daily limit window reset (was %d calls in last 24h)",
                cls._daily_call_count,
            )
            cls._daily_call_count = 0
            cls._daily_call_window_start = now

    @classmethod
    def _record_api_call(cls) -> None:
        """Record that an API call was made.

        Raises RateLimitExhausted if recording this call exceeds the daily limit.
        """
        now = time.monotonic()

        # Reset window if more than 24 hours have passed
        if now - cls._daily_call_window_start > DAILY_LIMIT_RESET_SECONDS:
            cls._daily_call_count = 0
            cls._daily_call_window_start = now

        cls._daily_call_count += 1

        if cls._daily_call_count % 100 == 0:
            logger.info(
                "Google Books: %d/%d daily API calls used",
                cls._daily_call_count,
                DAILY_CALL_LIMIT,
            )

        # Check AFTER incrementing — the call already happened, so we
        # need to stop the next one.  Reset window first so the next
        # run (after 24h) starts fresh.
        if cls._daily_call_count >= DAILY_CALL_LIMIT:
            cls._daily_call_window_start = now  # pin window so next check resets
            raise RateLimitExhausted(
                f"Google Books daily limit reached: {cls._daily_call_count}/{DAILY_CALL_LIMIT} calls"
            )

    # ------------------------------------------------------------------
    # Query construction
    # ------------------------------------------------------------------

    def _build_query(
        self, book: BookData, existing_identifiers: dict[str, str]
    ) -> str | None:
        """Return the query-string portion (without leading ``?``) for the
        Volumes search endpoint, or ``None`` if there is not enough
        information.
        """
        # Priority: ISBN > Google Books ID (handled separately) > title+author
        if "isbn13" in existing_identifiers:
            q = f"isbn:{existing_identifiers['isbn13']}"
        elif "isbn" in existing_identifiers:
            q = f"isbn:{existing_identifiers['isbn']}"
        elif book.title and book.authors:
            first_author = book.authors[0].name
            q = f"intitle:{book.title} inauthor:{first_author}"
        else:
            return None

        # Manually build to avoid double-encoding the query value
        encoded_q = urllib.parse.quote(q, safe="")
        params = f"q={encoded_q}&maxResults=1&projection=full"
        api_key = self.config.get("api_key")
        if api_key:
            params += f"&key={api_key}"
        return params

    # ------------------------------------------------------------------
    # Volume → BookData conversion
    # ------------------------------------------------------------------

    def _volume_to_book(
        self, volume: dict, existing_identifiers: dict[str, str]
    ) -> BookData | None:
        """Convert a Google Books Volume resource into a BookData.

        Only fields that are useful for enrichment are populated.  Title
        and authors are set to empty sentinels so the orchestrator can
        detect "no change" (it compares with ``is not book_data``).
        """
        info = volume.get("volumeInfo", {})
        if not info:
            return None

        # --- Identifiers ------------------------------------------------
        identifiers: dict[str, str] = {}

        # Google Books volume ID
        volume_id = volume.get("id", "")
        if volume_id:
            identifiers["google_books"] = volume_id

        # Industry identifiers (ISBN_10, ISBN_13, OTHER)
        for entry in info.get("industryIdentifiers", []):
            id_type = entry.get("type", "")
            id_value = entry.get("identifier", "")
            if not id_value:
                continue
            id_clean = id_value.replace("-", "").strip()
            if id_type == "ISBN_13" and "isbn13" not in existing_identifiers:
                identifiers["isbn13"] = id_clean
            elif id_type == "ISBN_10" and "isbn" not in existing_identifiers:
                identifiers["isbn"] = id_clean

        # Only return identifiers we don't already have
        new_identifiers = {
            k: v for k, v in identifiers.items() if k not in existing_identifiers
        }

        # --- Cover image ------------------------------------------------
        cover = None
        image_links = info.get("imageLinks", {})
        if image_links:
            for size in ("extraLarge", "large", "medium", "thumbnail"):
                if size in image_links:
                    cover = image_links[size]
                    if cover.startswith("http://"):
                        cover = "https://" + cover[7:]
                    break

        # --- Description ------------------------------------------------
        description = _strip_html(info.get("description"))

        # --- Genres / categories ----------------------------------------
        genres = info.get("categories", [])

        return BookData(
            title="",  # sentinel: never update title from enrichment
            authors=[],  # sentinel: authors from universal sources ignored
            description=description,
            publisher=info.get("publisher"),
            published_date=info.get("publishedDate"),
            page_count=info.get("pageCount"),
            language=info.get("language"),
            cover_image_url=cover,
            genres=genres,
            identifiers=new_identifiers,
        )

    # ------------------------------------------------------------------
    # Main enrichment entry point
    # ------------------------------------------------------------------

    async def enrich(
        self, book: BookData, existing_identifiers: dict[str, str]
    ) -> BookData:
        """Look up *book* in the Google Books API and return enriched data.

        Raises RateLimitExhausted on HTTP 429 or when the daily call
        limit is reached.  The orchestrator catches this and stops
        enrichment for this source.
        """
        # Check daily limit before making any API calls
        self._check_daily_limit()

        volume_id = existing_identifiers.get("google_books")

        # If we already have a Google Books ID, fetch directly
        if volume_id:
            url = f"{BASE_URL}/{urllib.parse.quote(volume_id)}"
            api_key = self.config.get("api_key")
            if api_key:
                url += f"?key={api_key}"
            try:
                response = await self.fetch(
                    url, google_search=False
                )
                self._record_api_call()
                data = response.json()
                if data.get("kind") == "books#volume":
                    result = self._volume_to_book(data, existing_identifiers)
                    if result:
                        return result
                    # Fall through to search if direct fetch yielded nothing
            except RateLimitExhausted:
                raise  # propagate immediately — don't catch our own
            except Exception:
                logger.debug(
                    "Google Books: direct fetch for volume %s failed, trying search",
                    volume_id,
                )

        # Search-based lookup
        params = self._build_query(book, existing_identifiers)
        if params is None:
            logger.debug(
                "Google Books: not enough info to search for '%s'", book.title
            )
            return book

        try:
            response = await self.fetch(
                f"{BASE_URL}?{params}", google_search=False
            )
            self._record_api_call()
            data = response.json()
        except RateLimitExhausted:
            raise  # propagate immediately
        except Exception as e:
            # Check if this is a 429 error (rate limited)
            response_obj = getattr(e, 'response', None)
            if response_obj is not None:
                status_code = getattr(response_obj, 'status_code', None)
                if status_code == 429:
                    logger.warning(
                        "Google Books: 429 rate limited (call %d/%d)",
                        self._daily_call_count,
                        DAILY_CALL_LIMIT,
                    )
                    raise RateLimitExhausted(
                        "Google Books returned 429 Too Many Requests"
                    ) from e
            logger.exception("Google Books: search request failed")
            return book

        items = data.get("items", [])
        if not items:
            logger.debug("Google Books: no results for '%s'", book.title)
            return book

        result = self._volume_to_book(items[0], existing_identifiers)
        if result:
            logger.debug(
                "Google Books: found match for '%s' (volume %s)",
                book.title,
                items[0].get("id"),
            )
            return result

        return book
