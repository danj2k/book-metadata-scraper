"""Light Novel Releases calendar scoped source plugin.

Discovers and parses book metadata from the LNRelease calendar at
https://lnrelease.github.io/.

Data source
-----------
A single JSON file at ``https://lnrelease.github.io/data.json`` contains
the entire calendar (currently ~7700 entries across ~1000 series).  The
file is cached locally and only re-fetched once per day.

Entry format (list of 8 elements)::

    [series_id, url, publisher_idx, title, volume_str, format_type, isbn, date]

Format types: 1=Paperback, 2=Ebook, 3=Hardcover, 4=Audiobook.

Multiple entries can share the same title+volume (different formats).
``discover_book_urls`` groups these and yields one URL per unique
title+volume.  ``parse_book`` combines all format ISBNs into
identifiers and takes the earliest release date.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import AsyncIterator

from book_metadata_scraper.fetcher import SESSION_HTTP, SESSION_STEALTHY
from book_metadata_scraper.models import BookData
from book_metadata_scraper.sources.base import BaseScopedSource
from book_metadata_scraper.sources.registry import scoped_source

logger = logging.getLogger(__name__)

DATA_URL = "https://lnrelease.github.io/data.json"
DEFAULT_CACHE_TTL = 86400  # 24 hours

FORMAT_NAMES = {1: "Paperback", 2: "Ebook", 3: "Hardcover", 4: "Audiobook"}


def _cache_dir(config: dict) -> Path:
    """Return (and create) the cache directory for this source."""
    base = config.get("cache_dir", os.path.expanduser("~/.book-metadata-scraper/cache/lnrelease"))
    path = Path(base)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_path(config: dict) -> Path:
    return _cache_dir(config) / "data.json"


def _cache_age(config: dict) -> float | None:
    """Return seconds since the cache was last modified, or None if no cache."""
    p = _cache_path(config)
    if not p.exists():
        return None
    return time.time() - p.stat().st_mtime


def _load_raw_data(config: dict) -> dict:
    """Load the raw JSON data, using cache if fresh enough."""
    age = _cache_age(config)
    ttl = config.get("cache_ttl", DEFAULT_CACHE_TTL)

    if age is not None and age < ttl:
        logger.debug("Using cached LNRelease data (age %.0fs)", age)
        with open(_cache_path(config)) as f:
            return json.load(f)

    return {}


def _save_raw_data(config: dict, data: dict) -> None:
    """Persist raw JSON data to the local cache."""
    with open(_cache_path(config), "w") as f:
        json.dump(data, f)
    logger.debug("Cached LNRelease data to %s", _cache_path(config))


def _group_entries(raw: dict) -> dict[tuple[str, str], list[list]]:
    """Group data entries by (title, volume) → list of entries.

    Returns a dict keyed by ``(title, volume_str)`` where each value is
    the list of raw entries sharing that key.
    """
    groups: dict[tuple[str, str], list[list]] = {}
    for entry in raw.get("data", []):
        title = entry[3]
        volume = entry[4]
        key = (title, volume)
        groups.setdefault(key, []).append(entry)
    return groups


@scoped_source
class LNReleaseSource(BaseScopedSource):
    """Light Novel Releases calendar source."""

    name = "lnrelease"
    session_type = SESSION_HTTP

    def __init__(self, session, config: dict):
        super().__init__(session, config)
        self._raw: dict = {}
        self._groups: dict[tuple[str, str], list[list]] = {}
        self._url_to_key: dict[str, tuple[str, str]] = {}

    async def _ensure_loaded(self) -> None:
        """Load and group the JSON data if not already in memory."""
        if self._groups:
            return

        # Try cache first
        self._raw = _load_raw_data(self.config)

        # Fetch if cache missing or stale
        if not self._raw:
            logger.info("Fetching LNRelease data from %s", DATA_URL)
            response = await self.session.fetch_http(DATA_URL)
            # Scrapling returns the JSON as text in html_content or text
            text = response.text or response.html_content or ""
            self._raw = json.loads(text)
            _save_raw_data(self.config, self._raw)

        self._groups = _group_entries(self._raw)

        # Build URL → group key lookup
        for key, entries in self._groups.items():
            for entry in entries:
                self._url_to_key[entry[1]] = key

        logger.info(
            "Loaded %d LNRelease entries → %d unique title+volume groups",
            len(self._raw.get("data", [])),
            len(self._groups),
        )

    async def discover_book_urls(self) -> AsyncIterator[str]:
        """Yield the canonical URL for each unique title+volume group.

        The first entry in each group is used as the canonical URL.
        The orchestrator's dedup check (via source_url) ensures we
        don't re-process groups we've already scraped.
        """
        await self._ensure_loaded()

        for key, entries in self._groups.items():
            # Use the first entry's URL as the canonical source URL
            yield entries[0][1]

    async def parse_book(self, response) -> BookData | None:
        """Build a BookData from the cached JSON group for this URL.

        The ``response`` parameter is ignored — all data comes from
        the JSON cache loaded during discovery.
        """
        url = response.url if hasattr(response, "url") else None
        if url is None:
            logger.warning("parse_book called with no URL on response")
            return None

        key = self._url_to_key.get(url)
        if key is None:
            logger.warning("URL %s not found in LNRelease cache", url)
            return None

        entries = self._groups[key]
        title, volume_str = key

        # Determine series name from the series lookup table
        series_id = entries[0][0]
        series_name = None
        raw_series = self._raw.get("series", [])
        if series_id < len(raw_series):
            series_name = raw_series[series_id][1]

        # Determine publisher from the publisher lookup table
        publisher_idx = entries[0][2]
        publisher_name = None
        raw_publishers = self._raw.get("publishers", [])
        if publisher_idx < len(raw_publishers):
            publisher_name = raw_publishers[publisher_idx]

        # Parse series position from volume string
        series_position = None
        if volume_str:
            try:
                series_position = float(volume_str)
            except (ValueError, TypeError):
                pass

        # Collect ISBNs from all format entries
        identifiers: dict[str, str] = {}
        dates: list[str] = []
        for entry in entries:
            isbn = entry[6]
            fmt = entry[5]
            date = entry[7]

            if isbn:
                # Normalise: strip hyphens for the canonical key, keep
                # original for display
                digits = isbn.replace("-", "")
                fmt_name = FORMAT_NAMES.get(fmt, f"format_{fmt}")
                identifiers[f"isbn_{fmt_name.lower()}"] = isbn
                # Also add a plain isbn13 if it's exactly 13 digits
                if len(digits) == 13:
                    identifiers["isbn13"] = digits

            if date:
                dates.append(date)

        # Use the earliest release date
        published_date = min(dates) if dates else None

        return BookData(
            title=title,
            authors=[],  # No author data in LNRelease
            publisher=publisher_name,
            published_date=published_date,
            series=series_name,
            series_position=series_position,
            identifiers=identifiers,
            source_url=url,
        )
