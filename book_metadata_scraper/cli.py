"""CLI entry point.

Parses args, loads config, configures logging, wires up the orchestrator, and
runs it.
"""

import argparse
import asyncio
import logging
import sys

from book_metadata_scraper.config import load_config, ScraperConfig
from book_metadata_scraper.db.repository import Repository
from book_metadata_scraper.orchestrator import Orchestrator
from book_metadata_scraper.sources import registry  # noqa: F401 — triggers source auto-discovery


def configure_logging(log_file: str, log_level: str) -> None:
    """Set up the root logger with file output."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="book-metadata-scraper",
        description="Scrape book metadata from multiple sources into a local SQLite database.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to scraper.toml (default: ./scraper.toml)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Override db_path from config (default: book_metadata.db)",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=None,
        help="Override log_level from config",
    )
    return parser.parse_args()


def main() -> None:
    """Main entry point."""
    args = parse_args()

    # Load and apply CLI overrides
    config: ScraperConfig = load_config(args.config)
    if args.db:
        config.db_path = args.db
    if args.log_level:
        config.log_level = args.log_level

    configure_logging(config.log_file, config.log_level)
    logger = logging.getLogger(__name__)
    logger.info("book-metadata-scraper starting up")

    # Collect all source names for registration
    all_scoped = list(registry.list_scoped_sources())
    all_universal = list(registry.list_universal_sources())
    all_sources = [(name, "scoped") for name in all_scoped] + [
        (name, "universal") for name in all_universal
    ]

    # Filter to enabled sources
    enabled_scoped = [
        (n, "scoped") for n, _ in all_sources
        if n in config.enabled_scoped_sources
    ]
    enabled_universal = [
        (n, "universal") for n, _ in all_sources
        if n in config.enabled_universal_sources
    ]

    async def _run() -> None:
        repo = Repository(config.db_path)
        await repo.initialise(sources=enabled_scoped + enabled_universal)
        try:
            orch = Orchestrator(config, repo)
            await orch.run()
        finally:
            await repo.close()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(1)
    except Exception:
        logger.exception("Fatal error")
        sys.exit(1)

    logger.info("book-metadata-scraper finished")


if __name__ == "__main__":
    main()
