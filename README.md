# Book Metadata Scraper

A Python CLI tool that scrapes book metadata from multiple sources (publishers, retailers, aggregators) and stores the results in a local SQLite database for further processing.

## Features

- **Scoped sources** that discover and scrape specific catalogs (e.g. a publisher's full catalogue)
- **Universal sources** that enrich partial book data using external APIs
- **Duplicate prevention** — books are identified by ISBN, ASIN, Goodreads ID, or title+author, so the same book is never scraped twice
- **Incremental enrichment** — previously stored scoped fields are never overwritten by universal sources
- **Enrichment tracking** — the database records which sources contributed to each book, and the last enrichment date, to avoid redundant re-scraping
- **Configurable rate limiting** per source to respect target site limits
- **Plugin architecture** — new sources are added by dropping a Python file into a package directory; no core changes needed

## Requirements

- Python 3.12 or later
- uv (recommended) or pip

## Quick Start

```bash
# Clone and enter the repo
git clone <repo-url> && cd book-metadata-scraper

# Install with uv
uv sync

# Or install with pip
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# Copy and edit the example config
cp scraper.toml.example scraper.toml
# Edit scraper.toml with your preferred settings

# Run
uv run scraper                          # uses scraper.toml + books.db
uv run scraper --config custom.toml     # custom config
uv run scraper --db output.sqlite       # custom database path
uv run scraper --log-level DEBUG        # verbose logging
```

## Configuration

Edit `scraper.toml` (see `scraper.toml.example` for all options):

```toml
rate_limits = { "aethon" = 1.0 }   # seconds between requests per source
request_timeout = 30.0              # HTTP timeout in seconds
http_max_concurrent = 10            # max simultaneous HTTP requests
stealthy_max_concurrent = 3         # max simultaneous stealthy requests
http_request_delay = 0.0            # global delay between HTTP requests
stealthy_request_delay = 0.0        # global delay between stealthy requests
user_agent = "BookMetadataScraper/1.0"  # custom User-Agent
```

Set `--config NONE` to disable the config file entirely and use defaults.

## How It Works

1. **Discovery** — scoped sources crawl their target sites and collect book URLs (skipping books already in the database).
2. **Parsing** — each discovered book page is fetched and parsed into structured metadata.
3. **Identity resolution** — the tool checks whether the book already exists in the database (by ISBN, ASIN, title+author, etc.).
4. **Storage** — new books are inserted; existing books are updated only if the new source provides data in fields the previous source left empty.
5. **Enrichment** — universal sources can then fill in gaps (e.g. Goodreads rating, Amazon ASINs) without overwriting scoped data.

## Project Structure

```
book-metadata-scraper/
├── scraper.toml.example
├── pyproject.toml
└── book_metadata_scraper/
    ├── cli.py                  # CLI entry point
    ├── config.py               # Config loading
    ├── fetcher.py              # HTTP session management & rate limiting
    ├── matching.py             # Identity resolution & duplicate detection
    ├── models.py               # BookData & AuthorData dataclasses
    ├── normalise.py            # Author name deduplication
    ├── orchestrator.py         # Discovery → enrichment pipeline
    ├── db/
    │   ├── schema.py           # SQLite schema & migrations
    │   └── repository.py       # Async database operations
    └── sources/
        ├── base.py             # Base classes for source plugins
        ├── registry.py         # Source registration decorators
        ├── scoped/             # Scoped source plugins (one per site)
        └── universal/          # Universal source plugins (APIs)
```

## Adding a New Source

1. Create a new Python file in `sources/scoped/` or `sources/universal/`.
2. Define a class inheriting from `BaseScopedSource` or `BaseUniversalSource`.
3. Decorate with `@scoped_source` or `@universal_source`.
4. The source is automatically registered on import — no changes to core code needed.

## License

MIT
