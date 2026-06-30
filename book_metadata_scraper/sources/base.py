"""Base classes for book metadata scraper sources.

All source plugins inherit from one of these base classes.
Scoped sources provide first-pass data; universal sources enrich it.
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, AsyncIterator

if TYPE_CHECKING:
    from book_metadata_scraper.fetcher import SessionManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Session types
# ---------------------------------------------------------------------------

SESSION_HTTP = "http"
SESSION_STEALTHY = "stealthy"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RateLimitExhausted(Exception):
    """Raised by a universal source when its daily rate limit is hit.

    The orchestrator catches this and stops enrichment for that source,
    preserving whatever data was gathered so far. The next run picks up
    where it left off.
    """


# ---------------------------------------------------------------------------
# Response type
# ---------------------------------------------------------------------------


@dataclass
class FetchedPage:
    """Wrapper for a fetched page response."""
    url: str
    text: str = ""
    html_content: str = ""
    css: list = None

    def __post_init__(self):
        if self.css is None:
            self.css = []


# ---------------------------------------------------------------------------
# Abstract base classes
# ---------------------------------------------------------------------------


class BaseSource(abc.ABC):
    """Abstract base for all source plugins.

    Each source plugin must subclass either ``BaseScopedSource`` or
    ``BaseUniversalSource``, both of which inherit from this class.
    """

    # Subclasses MUST set these
    name: str = ""
    session_type: str = SESSION_HTTP  # SESSION_HTTP or SESSION_STEALTHY

    # Optional per-source rate limit (seconds between requests).
    # None = use global http_rate_limit from SessionManager.
    rate_limit: float | None = None

    # Optional per-source concurrency limit (semaphore count).
    # None = use global concurrency_limit from SessionManager.
    concurrency_limit: int | None = None

    def __init__(self, session: SessionManager, config: dict | None = None):
        self.session = session
        self.config = config or {}

    async def fetch(self, url: str, **kwargs) -> FetchedPage:
        """Convenience method that routes to the right session type
        and passes the per-source rate limit as min_interval."""
        if self.session_type == SESSION_STEALTHY:
            return await self.session.fetch_stealthy(
                url, min_interval=self.rate_limit, **kwargs
            )
        else:
            return await self.session.fetch_http(
                url, min_interval=self.rate_limit, **kwargs
            )


class BaseScopedSource(BaseSource):
    """Base class for scoped sources (publisher sites, curated catalogs).

    Scoped sources provide first-pass data — their field values are
    authoritative and never overwritten by enrichment sources.
    """

    @abc.abstractmethod
    async def discover_book_urls(self) -> AsyncIterator[str | tuple[str, int | None]]:
        """Yield book page URLs (and optionally series positions).

        Yields either:
          - A plain URL string: ``"https://..."``
          - A ``(url, position)`` tuple where ``position`` is the
            1-based series position (or ``None`` for standalone books).

        The orchestrator feeds these to ``parse_book()``.
        """

    @abc.abstractmethod
    async def parse_book(self, response: FetchedPage) -> "BookData | None":
        """Parse a fetched book page into structured metadata.

        Returns ``BookData`` or ``None`` if the page couldn't be parsed.
        """


class BaseUniversalSource(BaseSource):
    """Base class for universal sources (Google Books, Amazon, WorldCat).

    Universal sources enrich existing books by filling in NULL fields,
    adding identifiers, and adding genres.  They never overwrite data
    already set by scoped sources.
    """

    @abc.abstractmethod
    async def enrich(
        self, book: "BookData", existing_identifiers: dict[str, str]
    ) -> "BookData":
        """Enrich a book with data from this source.

        Must return a BookData instance. If no new data was found,
        return the original ``book`` object unchanged.

        Args:
            book: Current book metadata (some fields may be None).
            existing_identifiers: Dict of identifier_type → value already
                known for this book (e.g. ``{"isbn13": "978..."}``).

        Returns:
            BookData with new fields populated (original is not mutated).

        Raises:
            RateLimitExhausted: When the source's daily rate limit is
                exhausted. The orchestrator stops enrichment for this source.
        """
