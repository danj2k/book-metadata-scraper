# Development Log

## 2026-06-28: Initial project structure

Created the full project skeleton from the design document (book-metadata-scraper-design.md). Implemented all core modules:

- `cli.py` — entry point with argparse, TOML config loading, logging setup
- `config.py` — `ScraperConfig` dataclass + `load_config()`
- `fetcher.py` — `SessionManager` with Scrapling `FetcherSession` and `AsyncStealthySession`, semaphore-capped concurrency
- `models.py` — `BookData` and `AuthorData` dataclasses
- `normalise.py` — author name normalisation for deduplication
- `matching.py` — identity resolution (isbn13 > isbn > asin > goodreads > title+author)
- `orchestrator.py` — two-phase pipeline (scoped discovery → universal enrichment)
- `db/schema.py` — 8 tables + trigger, WAL mode, foreign keys
- `db/repository.py` — full async SQL layer with all CRUD operations
- `sources/base.py` — `BaseSource`, `BaseScopedSource`, `BaseUniversalSource`
- `sources/registry.py` — `@scoped_source` / `@universal_source` decorators
- `sources/__init__.py` — auto-import via `pkgutil.iter_modules`

Verified: all imports clean, database layer end-to-end test passed (insert, fetch, identifier lookup, title+author lookup, null-update merge, enrichment tracking).

## 2026-06-28: Aethon Books scoped source

Explored the Aethon Books website structure:
- `/series/` index page lists all ~500 series with no pagination
- Each series page has JSON-LD `CreativeWorkSeries` with `hasPart` listing all books
- Individual book pages have JSON-LD `Book` with full metadata

Implemented `aethon.py`:
- Discovery walks `/series/` → series pages → `hasPart` URLs
- Yields `(url, position)` tuples (position from `hasPart` entry)
- Parsing extracts JSON-LD directly — no CSS scraping
- Format-specific ASINs: `asin_ebook`, `asin_paperback`, `asin_audiobook`

Infrastructure additions:
- `min_interval` rate limiting in `SessionManager` (configurable `http_rate_limit`)
- `discover_book_urls` can yield `(url, position)` tuples; orchestrator applies position to `book_data`

## 2026-06-28: Google Books universal source

Implemented `google_books.py`:
- Query priority: ISBN > direct volume fetch > title+author search
- Enriches: description, publisher, published_date, page_count, language, cover_image, genres, identifiers
- Only returns identifiers not already in `existing_identifiers`
- Uses `projection=full` for complete volumeInfo
- Uses `google_search=False` to avoid Scrapling's default Google referer (which triggers 429s)

Live testing with API key:
- ISBN lookup (`9780593135204`) returned correct data for "Project Hail Mary"
- Title+author search returned correct data for "The Martian"

Discovered: Google Books API returns 429 without an API key (no IP-based quota). The `key` parameter is supported via `source_config.google_books.api_key`.

## 2026-06-28: README and LICENSE

Added:
- `README.md` — end-user documentation covering features, installation, configuration, pipeline description, project structure, and how to add sources
- `LICENSE` — MIT license

## 2026-06-28: AGENTS.md and design document

Added `AGENTS.md` (project documentation standards) and loaded the original design document into `docs/book-metadata-scraper-design.md` for version control.
