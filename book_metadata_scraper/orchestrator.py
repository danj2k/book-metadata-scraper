"""Top-level run logic.

The Orchestrator class is constructed by cli.py and called once per run.
"""

import asyncio
import logging
from typing import Sequence

from book_metadata_scraper.config import ScraperConfig
from book_metadata_scraper.db.repository import Repository
from book_metadata_scraper.fetcher import SessionManager
from book_metadata_scraper.matching import find_existing_book
from book_metadata_scraper.models import BookData
from book_metadata_scraper.sources.base import RateLimitExhausted
from book_metadata_scraper.sources.registry import (
    get_scoped_source,
    get_universal_source,
)

logger = logging.getLogger(__name__)


class Orchestrator:
    """Top-level controller for a single scraper run."""

    def __init__(self, config: ScraperConfig, repo: Repository):
        self.config = config
        self.repo = repo
        self._stats = {
            "discovered": 0,
            "inserted": 0,
            "updated": 0,
            "skipped": 0,
            "errors": 0,
        }

    async def run(self) -> None:
        """Execute a full scrape-and-enrich run."""
        logger.info("Run started")
        self._stats = {
            "discovered": 0,
            "inserted": 0,
            "updated": 0,
            "skipped": 0,
            "errors": 0,
        }

        async with SessionManager(
            self.config.concurrency_limit,
            http_rate_limit=self.config.http_rate_limit,
            stealthy_page_limit=self.config.stealthy_page_limit,
        ) as session:
            # Phase 1: scoped sources (discovery + parsing)
            for source_name in self.config.enabled_scoped_sources:
                await self._run_scoped_source(source_name, session)

            # Phase 2: universal sources (enrichment)
            for source_name in self.config.enabled_universal_sources:
                await self._run_universal_source(source_name, session)

        self._log_summary()

        if self._stats["inserted"] + self._stats["updated"] > 0:
            await self.repo.rebuild_fts_index()

    # ------------------------------------------------------------------
    # Scoped source processing
    # ------------------------------------------------------------------

    async def _run_scoped_source(self, source_name: str, session: SessionManager) -> None:
        """Discover and parse books from a single scoped source."""
        logger.info("Starting scoped source: %s", source_name)

        try:
            source_cls = get_scoped_source(source_name)
            source_config = self.config.source_config.get(source_name, {})
            source = source_cls(session=session, config=source_config)
        except KeyError:
            logger.error("Scoped source '%s' is not registered — skipping", source_name)
            self._stats["errors"] += 1
            return

        try:
            async for item in source.discover_book_urls():
                # Support both plain URLs and (url, position) tuples
                if isinstance(item, tuple):
                    url, discovery_position = item
                else:
                    url = item
                    discovery_position = None

                self._stats["discovered"] += 1
                logger.debug("Discovered URL: %s", url)

                # Skip if already in DB
                existing_id = await self.repo.find_book_by_source_url(url)
                if existing_id is not None:
                    logger.debug("Book already in DB (source_url), skipping: %s", url)
                    self._stats["skipped"] += 1
                    continue

                # Fetch and parse
                try:
                    if source.session_type == "http":
                        response = await session.fetch_http(url)
                    else:
                        response = await session.fetch_stealthy(url)
                except Exception:
                    logger.exception("Network error fetching %s", url)
                    self._stats["errors"] += 1
                    continue

                try:
                    book_data = await source.parse_book(response)
                except Exception:
                    logger.exception("parse_book raised for %s", url)
                    self._stats["errors"] += 1
                    continue

                if book_data is None:
                    logger.warning("parse_book returned None for %s", url)
                    self._stats["skipped"] += 1
                    continue

                # Apply series position from discovery if parse didn't set one
                if discovery_position is not None and book_data.series_position is None:
                    book_data.series_position = discovery_position

                # Identity resolution
                existing_id = await find_existing_book(book_data, self.repo)
                source_id = await self.repo.upsert_source(source_name, "scoped")

                if existing_id is None:
                    # New book
                    await self.repo.insert_book(book_data, source_id)
                    self._stats["inserted"] += 1
                    logger.info("Inserted book '%s'", book_data.title)
                else:
                    # Existing book — update NULL fields only
                    await self.repo.update_book_nulls(existing_id, book_data)
                    self._stats["updated"] += 1
                    logger.debug("Updated book id=%d", existing_id)

        except Exception:
            logger.exception("Source '%s' raised unhandled exception", source_name)
            self._stats["errors"] += 1

    # ------------------------------------------------------------------
    # Universal source processing
    # ------------------------------------------------------------------

    async def _run_universal_source(self, source_name: str, session: SessionManager) -> None:
        """Enrich books from a single universal source."""
        logger.info("Starting enrichment with source: %s", source_name)

        try:
            source_cls = get_universal_source(source_name)
            source_config = self.config.source_config.get(source_name, {})
            source = source_cls(session=session, config=source_config)
        except KeyError:
            logger.error("Universal source '%s' is not registered — skipping", source_name)
            self._stats["errors"] += 1
            return

        try:
            books = await self.repo.get_all_books_for_enrichment(source_name)
            logger.info(
                "Found %d books to enrich with '%s'", len(books), source_name
            )

            for book_id, book_data, existing_identifiers in books:
                try:
                    enriched = await source.enrich(book_data, existing_identifiers)
                except RateLimitExhausted as e:
                    logger.warning(
                        "Rate limit reached for %s — stopping enrichment: %s. "
                        "Remaining books will be picked up on the next run.",
                        source_name,
                        e,
                    )
                    break  # stop processing this source
                except Exception:
                    logger.exception(
                        "Error enriching book id=%d with %s", book_id, source_name
                    )
                    self._stats["errors"] += 1
                    continue

                if enriched is not book_data:
                    # Only update if enrich() returned something different
                    # (i.e. it found data)
                    has_new_data = any([
                        enriched.description,
                        enriched.publisher,
                        enriched.published_date,
                        enriched.page_count,
                        enriched.language,
                        enriched.series,
                        enriched.series_position,
                        enriched.cover_image_url,
                        enriched.identifiers,
                        enriched.genres,
                    ])
                    if has_new_data:
                        await self.repo.update_book_nulls(book_id, enriched)
                        await self.repo.mark_enriched(book_id, source_name)
                        logger.debug(
                            "Enriched book '%s' (id=%d) with %s",
                            book_data.title,
                            book_id,
                            source_name,
                        )
                    else:
                        await self.repo.mark_enriched(book_id, source_name)
                        logger.debug(
                            "Enrichment found nothing for book '%s' (id=%d)",
                            book_data.title,
                            book_id,
                        )
                else:
                    # enrich() returned book unchanged — book not found
                    await self.repo.mark_enriched(book_id, source_name)
                    logger.debug(
                        "Enrichment found nothing for book '%s' (id=%d)",
                        book_data.title,
                        book_id,
                    )

        except Exception:
            logger.exception("Universal source '%s' raised unhandled exception", source_name)
            self._stats["errors"] += 1

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def _log_summary(self) -> None:
        """Log a summary of the run."""
        s = self._stats
        logger.info(
            "Run finished: discovered=%d inserted=%d updated=%d skipped=%d errors=%d",
            s["discovered"],
            s["inserted"],
            s["updated"],
            s["skipped"],
            s["errors"],
        )
