# Book Metadata Scraper

Scrapes book metadata from multiple online sources into a local SQLite database.

## Features

- Multi-source scraping from publisher catalogues and online retailers
- Two source types: **scoped** (publisher catalogues) and **universal** (search APIs for enrichment)
- Identity resolution across sources (ISBN, ASIN, title+author matching)
- Null-safe merging (enrichment never overwrites existing data)
- Rate limiting and concurrent fetching via Scrapling
- Async SQLite storage via aiosqlite

## Requirements

- Python 3.10+
- `uv` (recommended) or `pip`

## Quick Start

```bash
# Clone and enter the project
git clone <repo-url> book-metadata-scraper
cd book-metadata-scraper

# Install with uv (recommended)
uv sync

# Or install with pip
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# Copy and edit the example config
cp scraper.toml.example scraper.toml
# Edit scraper.toml with your preferred settings

# Run with uv
uv run scraper                          # uses scraper.toml + books.db
uv run scraper --config custom.toml     # custom config
uv run scraper --db output.sqlite       # custom database path
uv run scraper --log-level DEBUG        # verbose logging
uv run scraper --list-sources           # show available sources

# Or run with pip (after pip install -e .)
book-metadata-scraper                          # uses scraper.toml + books.db
book-metadata-scraper --config custom.toml     # custom config
book-metadata-scraper --db output.sqlite       # custom database path
book-metadata-scraper --log-level DEBUG        # verbose logging
book-metadata-scraper --list-sources           # show available sources
```

## Configuration

Edit `scraper.toml` (see `scraper.toml.example` for all options):

```toml
# Database path
db_path = "book_metadata.db"

# Concurrency and rate limiting
concurrency_limit = 5        # max simultaneous requests per session type
min_interval = 0.0           # minimum seconds between requests (0 = no limit)

# Logging
log_file = "book-metadata-scraper.log"
log_level = "INFO"

# Enable sources by name (run --list-sources to see all available)
enabled_scoped_sources = ["aethon_books", "podium"]
enabled_universal_sources = ["google_books"]

# Per-source configuration
[source_config.google_books]
api_key = "your-api-key-here"
```

Set `--config NONE` to disable the config file entirely and use defaults.

## How It Works

1. **Discovery** — scoped sources crawl their target sites and collect book URLs (skipping books already in the database).
2. **Parsing** — each discovered book page is fetched and parsed into structured metadata.
3. **Identity Resolution** — books are matched across sources using ISBN, ASIN, or title+author to avoid duplicates.
4. **Storage** — books are inserted into SQLite. Existing scoped values are never overwritten by universal enrichment.
5. **Enrichment** — universal sources (Google Books, Amazon UK) look up existing books and fill in missing fields.

## Available Sources

### Scoped Sources (publisher catalogues)

| Source | Description |
|--------|-------------|
| `aethon_books` | Aethon Books — genre pages and series catalog |
| `podium` | Podium Entertainment — titles catalog and sitemap |
| `lnrelease` | LNRelease — light novel release calendar (cached JSON) |
| `mountaindale_press` | Mountaindale Press — Shopify catalogue |
| `shadow_alley` | Shadow Alley Press — library and series pages |

### Universal Sources (enrichment APIs)

| Source | Description |
|--------|-------------|
| `google_books` | Google Books API — title/author search and enrichment |
| `amazon_uk` | Amazon UK — product pages via stealthy fetcher |

Run `book-metadata-scraper --list-sources` to see which sources are available and enabled.

## Project Structure

```
book_metadata_scraper/
├── cli.py                 # Entry point (--config, --db, --log-level, --list-sources)
├── config.py              # ScraperConfig dataclass + TOML loader
├── fetcher.py             # SessionManager (HTTP + stealthy sessions)
├── matching.py            # Identity resolution across books
├── models.py              # BookData, AuthorData dataclasses
├── normalise.py           # Author name normalisation for deduplication
├── orchestrator.py        # Main pipeline (discovery → parse → store → enrich)
├── db/
│   ├── schema.py          # SQLite DDL (8 tables + trigger)
│   └── repository.py      # Async SQL via aiosqlite
├── sources/
│   ├── __init__.py        # Auto-imports all source plugins
│   ├── base.py            # BaseSource, BaseScopedSource, BaseUniversalSource
│   ├── registry.py        # Source registration and lookup
│   ├── scoped/            # Scoped source plugins
│   └── universal/         # Universal source plugins
```

## Adding a New Source

1. Create a new Python file in `sources/scoped/` or `sources/universal/`
2. Subclass `BaseScopedSource` or `BaseUniversalSource`
3. Implement `discover_book_urls()` and `parse_book()` (scoped) or `enrich()` (universal)
4. Apply the `@scoped_source` or `@universal_source` decorator
5. Add the source name to `enabled_scoped_sources` or `enabled_universal_sources` in `scraper.toml`

The source will be auto-discovered on next run — no other files need editing.

## License

MIT
