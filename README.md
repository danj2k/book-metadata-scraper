# book-metadata-scraper

A Python tool for scraping book metadata from publisher websites and other
sources, storing everything in a local SQLite database. Designed for readers
who want to build and maintain a personal catalogue of book metadata with rich,
authoritative data from publisher catalogues.

## Features

- **Multi-source scraping** — collect metadata from multiple sources via
  pluggable source adapters. Each source is independent and can be enabled or
  disabled in the configuration file.
- **Scoped and universal sources** — *scoped* sources discover books from a
  specific publisher or catalogue (e.g. Aethon Books), while *universal*
  sources enrich already-discovered books with additional data (e.g. Google
  Books).
- **Intelligent identity resolution** — books are matched across sources using
  ISBN, ASIN, Goodreads ID, or title+author fuzzy matching. Null values from
  enrichment sources never overwrite scoped data.
- **Polite rate limiting** — configurable per-host request intervals to avoid
  overwhelming publisher sites.
- **Async architecture** — concurrent fetching with configurable parallelism
  via `asyncio` and `aiosqlite`.
- **Idempotent** — re-running the scraper only fetches pages for books not
  already in the database. Previously scraped book pages are never re-fetched.

## Requirements

- Python 3.12 or later
- `uv` or `pip` for installation

## Installation

```bash
# Clone the repository
git clone <repo-url> book-metadata-scraper
cd book-metadata-scraper

# Create a virtual environment and install
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Configuration

Copy the example configuration file and edit it:

```bash
cp scraper.toml.example scraper.toml
```

### Configuration options

| Key | Type | Default | Description |
|---|---|---|---|
| `db_path` | string | `book_metadata.db` | Path to the SQLite database file |
| `concurrency_limit` | integer | `5` | Maximum number of concurrent HTTP requests |
| `min_interval` | float | `0.0` | Minimum seconds between HTTP requests (per host). Set to `1.0` for polite crawling of publisher sites |
| `log_file` | string | `book-metadata-scraper.log` | Path to the log file |
| `log_level` | string | `INFO` | Logging verbosity: `DEBUG`, `INFO`, `WARNING`, or `ERROR` |
| `enabled_scoped_sources` | list | `[]` | Names of scoped sources to activate |
| `enabled_universal_sources` | list | `[]` | Names of universal sources to activate |
| `source_config.<name>` | table | — | Per-source configuration (e.g. API keys) |

### Example configuration

```toml
db_path = "book_metadata.db"
concurrency_limit = 5
min_interval = 1.0
log_file = "book-metadata-scraper.log"
log_level = "INFO"

enabled_scoped_sources = ["aethon_books"]
# enabled_universal_sources = ["google_books"]

# [source_config.google_books]
# api_key = "YOUR_KEY_HERE"
```

## Usage

Run the scraper with default settings (reads `scraper.toml` from the current
directory):

```bash
book-metadata-scraper
```

### Command-line options

```
book-metadata-scraper [--config PATH] [--db PATH] [--log-level LEVEL]
```

| Option | Description |
|---|---|
| `--config PATH` | Path to configuration file (default: `./scraper.toml`) |
| `--db PATH` | Override the database path from config |
| `--log-level LEVEL` | Override the log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

### Examples

```bash
# Run with a custom config
book-metadata-scraper --config /etc/my-scraper.toml

# Override the database location
book-metadata-scraper --db /data/books.db

# Run with verbose logging
book-metadata-scraper --log-level DEBUG
```

## Data model

Each scraped book is stored with the following metadata:

- **Title and subtitle**
- **Authors** (with role — author, editor, narrator, etc.)
- **Description** (publisher-supplied synopsis)
- **Publisher**
- **Publication date**
- **Page count and language**
- **Series name and position**
- **Cover image URL**
- **Genres / tags**
- **Identifiers** — ISBN-13, ASIN (per format: ebook, paperback, audiobook),
  Goodreads ID, and more

## Source plugins

Sources are Python modules placed in either `book_metadata_scraper/sources/scoped/`
or `book_metadata_scraper/sources/universal/`. They register themselves
automatically via the `@scoped_source` or `@universal_source` decorators.

### Available sources

| Source | Type | Description |
|---|---|---|
| `aethon_books` | Scoped | Discovers and scrapes all books from the Aethon Books catalogue |

### Writing a new source

Create a new `.py` file in the appropriate `sources/` subdirectory. Your source
class should inherit from `BaseScopedSource` or `BaseUniversalSource` and
implement the required interface methods. Register it with the corresponding
decorator:

```python
from book_metadata_scraper.sources.base import BaseScopedSource
from book_metadata_scraper.sources.registry import scoped_source

@scoped_source("my_source")
class MySource(BaseScopedSource):
    async def discover_book_urls(self):
        # Yield (url, position) tuples for books to scrape
        ...

    async def parse_book(self, url, session):
        # Fetch the page and return a BookData instance
        ...
```

## Project structure

```
book-metadata-scraper/
├── pyproject.toml
├── scraper.toml.example
├── book_metadata_scraper/
│   ├── cli.py              # Entry point
│   ├── config.py           # Configuration loading
│   ├── fetcher.py          # HTTP session management with rate limiting
│   ├── matching.py         # Cross-source identity resolution
│   ├── models.py           # BookData and AuthorData dataclasses
│   ├── normalise.py        # Author name normalisation
│   ├── orchestrator.py     # Discovery → fetch → parse → store pipeline
│   ├── db/
│   │   ├── schema.py       # SQLite schema (8 tables)
│   │   └── repository.py   # Async database access layer
│   └── sources/
│       ├── base.py         # Base source interfaces
│       ├── registry.py     # Source auto-registration
│       ├── scoped/         # Publisher-specific sources
│       │   └── aethon.py   # Aethon Books
│       └── universal/      # Cross-source enrichment plugins
```

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for
details.
