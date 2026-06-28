# Architecture

## System Overview

The scraper follows a two-phase pipeline:

1. **Scoped phase** — discover books from publisher/catalogue sites and insert them into the database.
2. **Enrichment phase** — look up each book in universal sources (Google Books, etc.) and fill in any NULL fields.

Both phases share a single `SessionManager` that provides rate-limited, semaphore-capped HTTP access through Scrapling.

```
CLI (cli.py)
  │
  ├─ load config (config.py)
  ├─ configure logging
  ├─ create Repository (db/repository.py)
  │    └─ create tables if needed (db/schema.py)
  │
  └─ run Orchestrator (orchestrator.py)
       │
       ├─ Phase 1: Scoped sources
       │    For each enabled scoped source:
       │    ├─ discover_book_urls() → yields (url, position) tuples
       │    ├─ check source_url in DB → skip if found
       │    ├─ fetch page → parse_book() → BookData
       │    ├─ find_existing_book() → identity resolution
       │    └─ insert or update_book_nulls
       │
       └─ Phase 2: Universal sources
            For each enabled universal source:
            ├─ get books not yet enriched by this source
            ├─ enrich(book, existing_identifiers) → enriched BookData
            └─ update_book_nulls + mark_enriched
```

## Components

### CLI (`cli.py`)
Entry point. Parses `--config`, `--db`, `--log-level` arguments. Loads TOML config, applies CLI overrides, configures logging, creates Repository and Orchestrator, runs the async main loop.

### Config (`config.py`)
`ScraperConfig` dataclass with sensible defaults. Loaded from a TOML file. Per-source config is a nested dict (`source_config.<name>`).

### Fetcher (`fetcher.py`)
`SessionManager` owns two Scrapling sessions: `FetcherSession` for plain HTTP (APIs, simple sites) and `AsyncStealthySession` for sites with anti-bot protection. A shared `asyncio.Semaphore` caps total concurrency. An optional `http_rate_limit` enforces a minimum interval between HTTP requests using a monotonic clock and an asyncio lock.  Individual sources can declare a per-source `rate_limit` (class attribute on `BaseSource`) and call `self.fetch()` instead of `self.session.fetch_http()` — the convenience method passes the rate limit through as `min_interval`, overriding the global for that source.

### Orchestrator (`orchestrator.py`)
Top-level controller. Runs scoped sources first (discovery + parsing), then universal sources (enrichment). Handles the `(url, position)` tuple form from discovery. Tracks run statistics (discovered, inserted, updated, skipped, errors). Each source is wrapped in a try/except so one failing source doesn't abort the run.

### Source Plugins (`sources/`)

**Base classes** (`sources/base.py`):
- `BaseSource` — common base with `name`, `source_type`, `session_type`, and constructor taking `session` + `config`.
- `BaseScopedSource` — defines `discover_book_urls()` and `parse_book()`.
- `BaseUniversalSource` — defines `enrich()`.

**Registry** (`sources/registry.py`):
`@scoped_source` and `@universal_source` decorators register classes into module-level dicts. The `sources/__init__.py` auto-imports all modules under `sources/scoped/` and `sources/universal/` using `pkgutil.iter_modules`, so adding a new plugin file is the only step needed.

**Scoped sources** (`sources/scoped/`):
- `aethon.py` — Aethon Books. Discovery via `/series/` index → JSON-LD `hasPart`. Parsing via JSON-LD `Book` blocks.
- `podium.py` — Podium Entertainment. Discovery via `sitemap.xml` (~13,500 URLs in one request). Parsing via CSS selectors and text extraction (no JSON-LD on this site). Extracts `podium_id` from URL path for deduplication.
- `lnrelease.py` — Light Novel Releases. Discovery via `data.json` (7,700+ entries, cached 24h). Groups entries by (title, volume) across format editions. Format-specific ISBNs.
- `shadow_alley.py` — Shadow Alley Press. Discovery via `/library/` → `/book-series/{slug}/` → `/book/{slug}/`. Handles two WordPress templates (Genesis + block editor). Extracts `shadow_alley_id` slug from URL for deduplication.

**Universal sources** (`sources/universal/`):
- `google_books.py` — Google Books API. Enriches with description, publisher, dates, page count, language, cover image, genres, and identifiers.
- `amazon_uk.py` — Amazon UK. Uses stealthy fetcher to bypass WAF. Enriches with ASIN, ISBNs, publisher, publication date, page count, language, description, and cover image. Handles non-Kindle pages by redirecting to Kindle edition for complete metadata.

### Models (`models.py`)
Plain dataclasses: `AuthorData(name, role)` and `BookData(title, authors, ...)`. Not ORM models — just the lingua franca between plugins and the database layer.

### Matching (`matching.py`)
Identity resolution. Priority order: isbn13 > isbn > asin > goodreads (identifier lookup), then exact title + normalised author name (fallback). Returns `book_id` or `None`.

### Normalise (`normalise.py`)
Author name normalisation for deduplication. Lowercase, replace runs of dots/spaces with hyphens, strip leading/trailing hyphens. Preserves accented characters and punctuation other than dots.

### Database Layer (`db/`)

**Schema** (`db/schema.py`):
8 tables + 1 trigger. Created on first run with `CREATE TABLE IF NOT EXISTS`. PRAGMAs: WAL journal mode, foreign keys on.

**Repository** (`db/repository.py`):
Single `Repository` class with all async methods. All SQL lives here — no SQL anywhere else in the codebase. Key operations: book lookups (by identifier, title+author, source URL), book insert (with author/genre/identifier upserts in a transaction), null-safe update (COALESCE), enrichment tracking.

## Data Flow

```
Source plugin discovery
    → (url, position) tuples
    → orchestrator dedup check (source_url)
    → fetch page
    → parse_book() → BookData
    → find_existing_book() → book_id or None
    → if new: insert_book() with source_id
    → if existing: update_book_nulls()
    → source marks identifiers in book_identifiers

Universal source enrichment
    → repo.get_all_books_for_enrichment() → (book_id, BookData, identifiers)
    → enrich() → enriched BookData
    → update_book_nulls() — only fills NULL fields
    → mark_enriched() in book_enrichment_log
```

## Dependencies

| Package | Purpose |
|---------|---------|
| `scrapling[fetchers]` | Web fetching — both HTTP and headless browser |
| `aiosqlite` | Async SQLite access |
| `tomllib` | TOML config parsing (stdlib in 3.11+) |
| `rich` | Terminal output formatting (used in CLI for progress display) |
| `lxml` | HTML/XML parsing (Scrapling dependency) |
| `curl_cffi` | TLS fingerprint impersonation (Scrapling dependency) |

External tools:
- **beebjit** (for BBC Micro projects, not this one)
- **Playwright Chromium** — installed once via `scrapling install`, used by the stealthy session
