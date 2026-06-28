"""Abstract base classes for source plugins.

Two kinds of source exist:
- **Scoped sources** (publisher sites, curated catalogs) are first-class data:
  their field values are written on insert and never overwritten by enrichment.
- **Universal sources** (Google Books, Amazon, WorldCat, etc.) only fill in
  fields that are NULL in the database, and add identifiers and genres.
"""

from abc import ABC, abstractmethod
from typing import AsyncIterator

from book_metadata_scraper.fetcher import SessionManager, SESSION_HTTP, SESSION_STEALTHY
from book_metadata_scraper.models import BookData


class BaseSource(ABC):
    """Common base for all source plugins.

    Class attributes (set on the subclass, not instances):
        name         -- Unique machine-readable identifier, e.g. "example_publisher".
                        Must be a valid Python identifier. Used as the key in
                        scraper.toml's [source_config] table.
        source_type  -- Either "scoped" or "universal". Must be set by the intermediate
                        base class (BaseScopedSource or BaseUniversalSource), not by
                        leaf implementations.
        session_type -- Either SESSION_HTTP or SESSION_STEALTHY (from fetcher.py).
                        Determines which SessionManager method to call:
                          SESSION_HTTP     -> self.session.fetch_http(url)
                          SESSION_STEALTHY -> self.session.fetch_stealthy(url)
                        Defaults to SESSION_STEALTHY. Override to SESSION_HTTP for
                        API-based sources or plain sites that need no stealth fetching.
    """

    name: str
    source_type: str
    session_type: str = SESSION_STEALTHY

    def __init__(self, session: SessionManager, config: dict):
        """
        Args:
            session -- The shared SessionManager. Call self.session.fetch_http() or
                       self.session.fetch_stealthy() as appropriate for this source.
            config  -- The [source_config.<name>] block from scraper.toml, or {} if
                       not present. Source plugins should use .get() with defaults
                       for all keys so they remain functional without explicit config.
        """
        self.session = session
        self.config = config


class BaseScopedSource(BaseSource, ABC):
    """Base for scoped sources — publisher sites, curated catalogs.

    Scoped sources are responsible for:
    1. Discovery — fetch index/listing pages and yield individual book page URLs.
    2. Parsing — given a Response from a book page, extract and return a BookData.
    """

    source_type = "scoped"

    @abstractmethod
    async def discover_book_urls(self) -> AsyncIterator[str]:
        """Yield the URL of every book page found on this source's listing pages.

        This should cover the full catalog, not just recent additions — the
        orchestrator is responsible for filtering out books already in the DB
        before calling parse_book().
        """
        ...

    @abstractmethod
    async def parse_book(self, response) -> BookData | None:
        """Given a Scrapling Response from a single book page, return a populated
        BookData, or None if the page cannot be parsed (e.g. 404, unexpected
        structure).  Log a warning before returning None.
        """
        ...


class BaseUniversalSource(BaseSource, ABC):
    """Base for universal sources — Google Books, Amazon, WorldCat, etc.

    Universal sources only fill in fields that are NULL in the database,
    and add identifiers and genres.
    """

    source_type = "universal"

    @abstractmethod
    async def enrich(self, book: BookData, existing_identifiers: dict[str, str]) -> BookData:
        """Look up the book in this source and return a BookData populated with
        any additional information found.

        The orchestrator passes ``existing_identifiers`` (a dict of all identifier
        types already known for this book, drawn from the database).  The plugin
        should use the best available identifier to locate the book.

        The returned BookData is *merged* into the database record by the
        orchestrator — the plugin does not need to worry about what is already
        in the DB.

        Return the original ``book`` unchanged if the book cannot be found.
        """
        ...
