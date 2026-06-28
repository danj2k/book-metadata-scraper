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
    parser.add_argument(
        "--list-sources",
        action="store_true",
        help="List all available sources and whether they are enabled, then exit",
    )
    return parser.parse_args()


def list_sources(config: ScraperConfig) -> None:
    """Print a table of all registered sources and their enabled status."""
    all_scoped = registry.list_scoped_sources()
    all_universal = registry.list_universal_sources()

    # Collect (name, type, session_type, enabled)
    rows: list[tuple[str, str, str, bool]] = []
    for name in sorted(all_scoped):
        cls = registry.get_scoped_source(name)
        rows.append((name, "scoped", cls.session_type, name in config.enabled_scoped_sources))
    for name in sorted(all_universal):
        cls = registry.get_universal_source(name)
        rows.append((name, "universal", cls.session_type, name in config.enabled_universal_sources))

    if not rows:
        print("No sources registered.")
        return

    # Column widths
    name_w = max(len(r[0]) for r in rows)
    type_w = max(len(r[1]) for r in rows)
    sess_w = max(len(r[2]) for r in rows)

    header = (
        f"  {'SOURCE':<{name_w}}  {'TYPE':<{type_w}}  {'SESSION':<{sess_w}}  STATUS"
    )
    separator = f"  {'─' * name_w}  {'─' * type_w}  {'─' * sess_w}  {'─' * 7}"

    print("Available sources:")
    print(header)
    print(separator)
    for name, stype, session, enabled in rows:
        status = "✓ enabled" if enabled else "  disabled"
        print(f"  {name:<{name_w}}  {stype:<{type_w}}  {session:<{sess_w}}  {status}")

    print()
    n_enabled = sum(1 for r in rows if r[3])
    print(f"  {len(rows)} source(s) available, {n_enabled} enabled.")


def main() -> None:
    """Main entry point."""
    args = parse_args()

    # Load config (--list-sources needs it but doesn't require all fields)
    config: ScraperConfig = load_config(args.config)

    if args.list_sources:
        list_sources(config)
        return

    # Apply CLI overrides
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
