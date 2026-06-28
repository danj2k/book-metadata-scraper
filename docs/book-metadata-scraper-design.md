# Book Metadata Scraper: Design Document

**Version:** 1.0  
**Language:** Python 3.12+  
**Key dependencies:** Scrapling (fetchers), aiosqlite, asyncio  
**Scrapling docs:** https://scrapling.readthedocs.io/en/latest/index.html

---

## 1. Goals and Constraints

- Scrape book metadata from multiple configurable sources and store it in a local SQLite database.
- Designed to run unattended as a daily cron job on Ubuntu 24.04.
- No more than **5 concurrent fetch operations** at any time, enforced by an asyncio `Semaphore`.
- All fetching runs within a **single shared Scrapling session** (no per-request session creation/teardown).
- New data sources must be addable without touching core orchestration code.
- Primary key for books is an autoincrement integer, never an ISBN or other external identifier.
- **Scoped sources** (publisher sites, curated catalogs) are first-class data: their field values are written on insert and never overwritten by enrichment.
- **Universal sources** (Google Books, Amazon, WorldCat, etc.) only fill in fields that are `NULL` in the database, and add identifiers and genres.

---

## 2. Project Layout

```
book_metadata_scraper/
├── __init__.py
├── cli.py                    # Entry point; parses args, wires everything together
├── config.py                 # Runtime configuration dataclass + loader
├── fetcher.py                # Shared session wrapper with semaphore
├── orchestrator.py           # Top-level run logic
├── matching.py               # Book identity resolution (deduplication)
├── normalise.py              # Author name normalisation and other text utilities
├── models.py                 # Shared dataclasses (BookData, AuthorData, etc.)
├── db/
│   ├── __init__.py
│   ├── schema.py             # CREATE TABLE statements and migration helper
│   └── repository.py        # All database I/O — one class, all methods async
└── sources/
    ├── __init__.py
    ├── base.py               # Abstract base classes for both source types
    ├── registry.py           # Source registration and discovery
    ├── scoped/
    │   ├── __init__.py
    │   └── example_publisher.py  # Illustrative implementation (see §7)
    └── universal/
        ├── __init__.py
        └── google_books.py   # Illustrative implementation (see §7)
```

Configuration file (TOML, loaded by `config.py`):

```
scraper.toml  (default location; overridable via --config CLI flag)
```

---

## 3. Configuration (`config.py`)

```python
@dataclass
class ScraperConfig:
    db_path: str = "book_metadata.db"
    concurrency_limit: int = 5
    log_file: str = "book-metadata-scraper.log"
    log_level: str = "INFO"           # DEBUG | INFO | WARNING | ERROR
    enabled_scoped_sources: list[str] = field(default_factory=list)
    enabled_universal_sources: list[str] = field(default_factory=list)
    # Per-source config blobs passed through to source constructors
    source_config: dict[str, dict] = field(default_factory=dict)
```

`config.py` exposes a `load_config(path: str | None) -> ScraperConfig` function that reads from a TOML file if present and falls back to defaults for any missing key. `db_path` defaults to `"book_metadata.db"` (relative to cwd) if not specified.

Example `scraper.toml`:

```toml
db_path = "book_metadata.db"
concurrency_limit = 5
log_file = "book-metadata-scraper.log"
log_level = "INFO"

enabled_scoped_sources = ["example_publisher"]
enabled_universal_sources = ["google_books"]

[source_config.google_books]
api_key = "YOUR_KEY_HERE"

[source_config.example_publisher]
base_url = "https://www.examplepublisher.com/new-releases"
```

---

## 4. Database Schema (`db/schema.py`)

All tables are created on first run. SQLite is opened with:
- `PRAGMA journal_mode = WAL` (safe for concurrent reads during a run)
- `PRAGMA foreign_keys = ON`

### 4.1 `sources`

Registry of known source modules. Populated automatically on startup from the loaded source classes.

```sql
CREATE TABLE IF NOT EXISTS sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,  -- matches BaseSource.name
    source_type TEXT    NOT NULL          -- 'scoped' or 'universal'
);
```

### 4.2 `books`

```sql
CREATE TABLE IF NOT EXISTS books (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT    NOT NULL,
    subtitle         TEXT,
    description      TEXT,
    publisher        TEXT,
    published_date   TEXT,               -- ISO 8601 date string, e.g. "2024-03-01"
    page_count       INTEGER,
    language         TEXT    DEFAULT 'en',
    series           TEXT,               -- Series name, if applicable
    series_position  REAL,               -- Allows "2.5" for novellas etc.
    cover_image_url  TEXT,
    first_seen_source_id INTEGER REFERENCES sources(id),
    created_at       TEXT    DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at       TEXT    DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
```

`first_seen_source_id` records which scoped source originally surfaced the book. It is set on insert and never updated.

### 4.3 `authors`

```sql
CREATE TABLE IF NOT EXISTS authors (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT    NOT NULL,
    normalised_name  TEXT    NOT NULL UNIQUE
);
```

`normalised_name` is computed in Python (see §6.1) and stored alongside the display name. It is used for author identity matching. The `UNIQUE` constraint on `normalised_name` prevents duplicates even if display name casing or punctuation differs slightly across sources.

### 4.4 `book_authors`

```sql
CREATE TABLE IF NOT EXISTS book_authors (
    book_id    INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    author_id  INTEGER NOT NULL REFERENCES authors(id),
    role       TEXT    NOT NULL DEFAULT 'author',
    PRIMARY KEY (book_id, author_id, role)
);
```

`role` is a free-form lowercase string. Expected values include: `author`, `editor`, `illustrator`, `translator`, `foreword`, `narrator`. Source plugins are responsible for populating this where the source provides it; `author` is the default.

### 4.5 `book_identifiers`

```sql
CREATE TABLE IF NOT EXISTS book_identifiers (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id           INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    identifier_type   TEXT    NOT NULL,
    identifier_value  TEXT    NOT NULL,
    UNIQUE (identifier_type, identifier_value)
);

CREATE INDEX IF NOT EXISTS idx_book_identifiers_lookup
    ON book_identifiers (identifier_type, identifier_value);
```

`identifier_type` is a lowercase string. Standard values:

| Type | Example value |
|------|---------------|
| `isbn` | `0593099320` |
| `isbn13` | `9780593099322` |
| `asin` | `B0CXYZ1234` |
| `goodreads` | `12345678` |
| `worldcat` | `ocn123456789` |
| `google_books` | `abc123XYZ` |
| `publisher_sku` | `PUB-2024-001` |

New identifier types can be added freely by source plugins without any schema changes.

### 4.6 `genres`

```sql
CREATE TABLE IF NOT EXISTS genres (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name  TEXT    NOT NULL UNIQUE  -- stored as received; no normalisation applied
);

CREATE TABLE IF NOT EXISTS book_genres (
    book_id   INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    genre_id  INTEGER NOT NULL REFERENCES genres(id),
    PRIMARY KEY (book_id, genre_id)
);
```

Genre names are stored as received from the source (title-cased or otherwise). No normalisation is applied at this stage, but a future deduplication pass could be added.

### 4.7 Trigger: auto-update `updated_at`

```sql
CREATE TRIGGER IF NOT EXISTS books_updated_at
AFTER UPDATE ON books
FOR EACH ROW
BEGIN
    UPDATE books SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
    WHERE id = OLD.id;
END;
```

---

## 5. Shared Data Models (`models.py`)

These dataclasses are the lingua franca between source plugins and the database layer. They are **not** ORM models; they are plain data containers.

```python
from dataclasses import dataclass, field

@dataclass
class AuthorData:
    name: str
    role: str = "author"

@dataclass
class BookData:
    title: str
    authors: list[AuthorData] = field(default_factory=list)
    subtitle: str | None = None
    description: str | None = None
    publisher: str | None = None
    published_date: str | None = None  # ISO 8601: "YYYY-MM-DD", "YYYY-MM", or "YYYY"
    page_count: int | None = None
    language: str | None = None        # BCP 47 tag, e.g. "en", "fr"
    series: str | None = None
    series_position: float | None = None
    cover_image_url: str | None = None
    genres: list[str] = field(default_factory=list)
    identifiers: dict[str, str] = field(default_factory=dict)
    source_url: str | None = None      # The canonical URL this book was scraped from
```

Rules that apply everywhere:
- `title` is required; all other fields are optional.
- `published_date` is always a string. Partial dates ("2024-03") are permitted.
- `identifiers` keys are lowercase strings from the table in §4.5.
- Source plugins must not raise on missing optional fields; they set them to `None`.

---

## 6. Core Utilities

### 6.1 Author Name Normalisation (`normalise.py`)

The normalised name is used as a deduplication key in the `authors` table and in fallback book matching (§8).

```python
import re

def normalise_author_name(name: str) -> str:
    """
    Lowercase, strip leading/trailing whitespace, then replace all runs of
    dots and/or spaces with a single hyphen.

    Examples:
        "J.R.R. Tolkien"  -> "j-r-r-tolkien"
        "Le Carré, John"  -> "le-carré,-john"   (punctuation other than dots preserved)
        "  Ann Leckie  "  -> "ann-leckie"
    """
    name = name.strip().lower()
    name = re.sub(r'[.\s]+', '-', name)
    name = re.sub(r'-{2,}', '-', name)  # collapse accidental double hyphens
    name = name.strip('-')              # remove any leading/trailing hyphens
    return name
```

Note: characters other than dots and spaces (commas, accented characters, apostrophes) are preserved intentionally. The goal is a stable key for matching, not a slug for use in URLs.

### 6.2 Shared Fetch Session (`fetcher.py`)

All HTTP activity in the application is funnelled through one `SessionManager` instance. It owns two long-lived Scrapling sessions — a `FetcherSession` for plain HTTP (fast, no browser overhead, suitable for APIs and straightforward sites) and an `AsyncStealthySession` for sites with anti-bot protection — plus a single `asyncio.Semaphore` capped at `concurrency_limit` that governs both. Source plugins declare which session they need via a `session_type` class attribute; the manager routes accordingly.

```python
import asyncio
from scrapling.fetchers import FetcherSession, AsyncStealthySession

SESSION_HTTP = "http"
SESSION_STEALTHY = "stealthy"

class SessionManager:
    """
    Manages two long-lived Scrapling sessions (HTTP and stealthy) behind a
    shared semaphore that caps total concurrent fetches at `concurrency_limit`.

    Source plugins call fetch_http() or fetch_stealthy() depending on their
    needs. Both methods honour the same semaphore, so the cap applies globally
    regardless of which session type is used.
    """

    def __init__(self, concurrency_limit: int = 5):
        self._sem = asyncio.Semaphore(concurrency_limit)
        self._http: FetcherSession | None = None
        self._stealthy: AsyncStealthySession | None = None

    async def start(self) -> None:
        """Open both sessions. Call once before any fetch operations."""
        self._http = FetcherSession()
        await self._http.start()
        self._stealthy = AsyncStealthySession(headless=True, network_idle=True)
        await self._stealthy.start()

    async def stop(self) -> None:
        """Close both sessions. Call once after all fetch operations complete."""
        if self._http:
            await self._http.stop()
            self._http = None
        if self._stealthy:
            await self._stealthy.stop()
            self._stealthy = None

    async def fetch_http(self, url: str, **kwargs):
        """
        Fetch `url` via the plain HTTP session (FetcherSession).
        Use for APIs and sites that do not require stealth.
        `**kwargs` are forwarded to the session's fetch method.
        Returns a Scrapling Response object.
        """
        if not self._http:
            raise RuntimeError("SessionManager.start() has not been called")
        async with self._sem:
            return await self._http.fetch(url, **kwargs)

    async def fetch_stealthy(self, url: str, **kwargs):
        """
        Fetch `url` via the stealthy browser session (AsyncStealthySession).
        Use for sites with anti-bot protection or JavaScript-rendered content.
        `**kwargs` are forwarded to the session's fetch method.
        Returns a Scrapling Response object.
        """
        if not self._stealthy:
            raise RuntimeError("SessionManager.start() has not been called")
        async with self._sem:
            return await self._stealthy.fetch(url, **kwargs)

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *_):
        await self.stop()
```

Source plugins declare their preferred session type via a `session_type` class attribute set to either `SESSION_HTTP` or `SESSION_STEALTHY` (constants exported from `fetcher.py`). They then call the corresponding method — `self.session.fetch_http(url)` or `self.session.fetch_stealthy(url)` — directly in their implementation. The `BaseSource` docstring should document this expectation. Both calls share the same semaphore, so the concurrency cap is always respected regardless of which session type is in use.

---

## 7. Source Plugin Interface (`sources/base.py`)

### 7.1 `BaseSource`

```python
from abc import ABC, abstractmethod
from book_metadata_scraper.fetcher import SessionManager, SESSION_HTTP, SESSION_STEALTHY
from book_metadata_scraper.models import BookData

class BaseSource(ABC):
    """
    Common base for all source plugins.

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
```

### 7.2 `BaseScopedSource`

Scoped sources are responsible for two distinct operations:

1. **Discovery** — fetch the source's index/listing/catalog pages and yield the URLs of individual book pages.
2. **Parsing** — given a Response from a book page, extract and return a `BookData`.

```python
from typing import AsyncIterator
from scrapling.spiders import Response  # or whatever Scrapling's response type is

class BaseScopedSource(BaseSource, ABC):
    source_type = "scoped"

    @abstractmethod
    async def discover_book_urls(self) -> AsyncIterator[str]:
        """
        Yield the URL of every book page found on this source's listing pages.
        This should cover the full catalog, not just recent additions — the
        orchestrator is responsible for filtering out books already in the DB
        before calling parse_book().

        This is an async generator; use `yield url` inside it.
        """
        ...

    @abstractmethod
    async def parse_book(self, response) -> BookData | None:
        """
        Given a Scrapling Response from a single book page, return a populated
        BookData, or None if the page cannot be parsed (e.g. 404, unexpected
        structure). Log a warning before returning None.
        """
        ...
```

### 7.3 `BaseUniversalSource`

```python
class BaseUniversalSource(BaseSource, ABC):
    source_type = "universal"

    @abstractmethod
    async def enrich(self, book: BookData, existing_identifiers: dict[str, str]) -> BookData:
        """
        Look up the book in this source and return a BookData populated with
        any additional information found.

        The orchestrator passes `existing_identifiers` (a dict of all identifier
        types already known for this book, drawn from the database). The plugin
        should use the best available identifier (see §8.2) to locate the book.

        The returned BookData is *merged* into the database record by the
        orchestrator according to the enrichment policy in §8.3 — the plugin
        does not need to worry about what is already in the DB.

        Return the original `book` unchanged (or a shallow copy with no new
        fields set) if the book cannot be found in this source.
        """
        ...
```

### 7.4 Source Registration (`sources/registry.py`)

The registry maps source `name` strings to their classes. Source plugins self-register using a decorator.

```python
_SCOPED_REGISTRY: dict[str, type[BaseScopedSource]] = {}
_UNIVERSAL_REGISTRY: dict[str, type[BaseUniversalSource]] = {}

def scoped_source(cls: type[BaseScopedSource]) -> type[BaseScopedSource]:
    """Class decorator. Apply to every BaseScopedSource subclass."""
    _SCOPED_REGISTRY[cls.name] = cls
    return cls

def universal_source(cls: type[BaseUniversalSource]) -> type[BaseUniversalSource]:
    """Class decorator. Apply to every BaseUniversalSource subclass."""
    _UNIVERSAL_REGISTRY[cls.name] = cls
    return cls

def get_scoped_source(name: str) -> type[BaseScopedSource]:
    if name not in _SCOPED_REGISTRY:
        raise KeyError(f"No scoped source named '{name}' is registered")
    return _SCOPED_REGISTRY[name]

def get_universal_source(name: str) -> type[BaseUniversalSource]:
    if name not in _UNIVERSAL_REGISTRY:
        raise KeyError(f"No universal source named '{name}' is registered")
    return _UNIVERSAL_REGISTRY[name]
```

The `sources/__init__.py` imports every module under `sources/scoped/` and `sources/universal/` so that all `@scoped_source` / `@universal_source` decorators run at import time. This means adding a new plugin file is the only step needed to make it available — no other file needs editing.

```python
# sources/__init__.py
import importlib, pkgutil
import book_metadata_scraper.sources.scoped as _scoped
import book_metadata_scraper.sources.universal as _universal

for _pkg in (_scoped, _universal):
    for _info in pkgutil.iter_modules(_pkg.__path__):
        importlib.import_module(f"{_pkg.__name__}.{_info.name}")
```

---

## 8. Orchestration Logic (`orchestrator.py`)

The `Orchestrator` class is the top-level controller. It is constructed by `cli.py` and called once per run.

### 8.1 Run Sequence

```
Orchestrator.run()
│
├─ 1. Initialise DB (create tables if needed, upsert source rows)
│
├─ 2. Open shared SessionManager
│
├─ 3. For each enabled scoped source:
│     a. Instantiate source plugin
│     b. Call discover_book_urls() to get the full URL list
│     c. For each URL:
│           i.  Check if URL already exists in book_identifiers or books
│               (source_url match) → skip if found
│           ii. Acquire semaphore slot, fetch page, release
│          iii. Call parse_book(response) → BookData | None
│           iv. If None: log warning, continue
│            v. Resolve identity (§8.2) — does this book already exist?
│           vi. If new: INSERT book + authors + identifiers + genres
│          vii. If existing: update only NULL fields of the existing record
│                            (scoped→scoped merge, same policy as enrichment)
│
├─ 4. For each enabled universal source:
│     a. Instantiate source plugin
│     b. Fetch all books from DB that have not yet been processed by this source
│        (tracked via book_identifiers: absence of this source's identifier type
│         is a proxy; alternatively use a book_source_runs table — see §8.4)
│     c. For each such book (in batches to respect memory):
│           i.  Call enrich(book_data, existing_identifiers) → enriched BookData
│           ii. Apply enrichment merge (§8.3)
│
└─ 5. Close SessionManager, log summary
```

Steps 3b→3c and 4b→4c use `asyncio.gather` / task queues to keep up to 5 fetch operations in flight simultaneously. The semaphore in `SessionManager.fetch()` is the single chokepoint; no additional concurrency control is needed in the orchestrator.

### 8.2 Book Identity Resolution (`matching.py`)

Called after `parse_book()` returns a `BookData`. Returns `book_id: int | None`.

```
find_existing_book(book_data, repo) -> int | None

1. For each identifier type in priority order [isbn13, isbn, asin, goodreads]:
       If book_data.identifiers contains that type:
           Look up (identifier_type, identifier_value) in book_identifiers
           If found: return book_id

2. If no identifier match:
       Normalise all author names in book_data.authors
       Query books WHERE title = book_data.title (exact, case-sensitive)
         AND at least one linked author has a matching normalised_name
       If exactly one match: return book_id
       If multiple matches: log a WARNING with the conflicting IDs and return None
         (the book will be inserted as a new record; a human should investigate)

3. Return None (book is new)
```

The fallback title+author match uses exact title comparison intentionally. Fuzzy matching introduces false positives (e.g. different editions with the same title but different content). If a scoped source provides an identifier, that should always be preferred.

### 8.3 Enrichment Merge Policy

When `enrich()` returns a `BookData`, the orchestrator applies the following rules field by field:

- **Book table fields** (`subtitle`, `description`, `publisher`, `published_date`, `page_count`, `language`, `series`, `series_position`, `cover_image_url`): update only if the current DB value is `NULL`. Never overwrite a value set by a scoped source.
- **`book_identifiers`**: INSERT OR IGNORE each identifier returned by the universal source. Existing identifier rows are never updated.
- **`book_genres`**: for each genre string returned, upsert the genre name into `genres`, then INSERT OR IGNORE into `book_genres`. Genres accumulate across sources.
- **`book_authors`**: universal sources should not add authors. If they do return authors, these are ignored by the orchestrator unless the book has zero authors in the DB (edge case: scoped source produced no author data).

### 8.4 Tracking Enrichment State

To avoid re-querying every universal source for every book on every daily run, the orchestrator checks whether the book already has an identifier of the universal source's characteristic type (e.g. `google_books` for the Google Books source, `asin` for an Amazon source). If that identifier is present, the enrichment step for that source is skipped for that book.

This means:
- On first enrichment, the universal source is called and (if successful) writes its identifier.
- On subsequent runs, the presence of that identifier acts as a "done" marker, and the source is skipped.
- If a universal source does not produce a recognisable identifier (e.g. it only adds genres), add a lightweight `book_enrichment_log` table:

```sql
CREATE TABLE IF NOT EXISTS book_enrichment_log (
    book_id    INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    source_name TEXT   NOT NULL,
    enriched_at TEXT   NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (book_id, source_name)
);
```

This table is checked before calling `enrich()` and written to after a successful enrichment call.

---

## 9. Database Access Layer (`db/repository.py`)

All SQL is written in this module. No SQL appears anywhere else in the codebase. The repository uses `aiosqlite` and is initialised with the path to the database file.

Key methods (all `async`):

```
Repository.__init__(db_path: str)
Repository.initialise() -> None
    # Creates tables, sets PRAGMAs, upserts source rows

Repository.get_book_by_id(book_id: int) -> BookData | None
Repository.find_book_by_identifier(type: str, value: str) -> int | None
Repository.find_book_by_title_and_author(title: str, normalised_names: list[str]) -> int | None
Repository.find_book_by_source_url(url: str) -> int | None

Repository.insert_book(book: BookData, source_id: int) -> int
    # Returns the new book_id
    # Also handles author upsert, book_authors, identifiers, genres

Repository.update_book_nulls(book_id: int, book: BookData) -> None
    # Issues UPDATE ... SET field = COALESCE(field, ?) for each nullable field
    # Only touches NULL columns; always safe to call

Repository.upsert_identifier(book_id: int, type: str, value: str) -> None
Repository.upsert_genre(book_id: int, genre_name: str) -> None

Repository.get_all_books_for_enrichment(source_name: str) -> list[tuple[int, BookData, dict]]
    # Returns (book_id, book_data, existing_identifiers) for all books not yet
    # enriched by the named source (checks book_enrichment_log)

Repository.mark_enriched(book_id: int, source_name: str) -> None
Repository.upsert_source(name: str, source_type: str) -> int
    # Returns source_id
```

All write operations that span multiple tables (`insert_book`, `update_book_nulls`) run inside a single `async with conn.execute("BEGIN"):` transaction to ensure atomicity.

---

## 10. Logging (`cli.py` / orchestrator setup)

Standard library `logging` is used throughout. The root logger is configured in `cli.py` before any other code runs:

```python
import logging

def configure_logging(log_file: str, log_level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )
```

Each module obtains its logger via `logger = logging.getLogger(__name__)`. This gives clean hierarchical names like `book_metadata_scraper.orchestrator`, `book_metadata_scraper.sources.scoped.example_publisher`, etc.

Log rotation is handled externally by `logrotate` and is out of scope for this project.

**Minimum logging expectations per component:**

| Event | Level |
|---|---|
| Run started / finished | INFO |
| Source starting discovery | INFO |
| Book discovered (URL) | DEBUG |
| Book already in DB, skipped | DEBUG |
| Book inserted (title, id) | INFO |
| Book updated (which fields changed) | DEBUG |
| Enrichment started for source | INFO |
| Enrichment found data for book | DEBUG |
| Enrichment found nothing | DEBUG |
| Book identity collision (multiple matches) | WARNING |
| parse_book returned None | WARNING |
| Network error (with URL and exception) | ERROR |
| Source raised unhandled exception | ERROR (with traceback) |

---

## 11. CLI Entry Point (`cli.py`)

```
usage: book-metadata-scraper [-h] [--config CONFIG] [--db DB] [--log-level {DEBUG,INFO,WARNING,ERROR}]

Options:
  --config      Path to scraper.toml (default: ./scraper.toml)
  --db          Override db_path from config (default: book_metadata.db)
  --log-level   Override log_level from config
```

The entry point:
1. Parses args
2. Loads config (`load_config()`)
3. Applies CLI overrides to config
4. Calls `configure_logging()`
5. Creates `Repository` and calls `initialise()`
6. Creates `Orchestrator(config, repo)` and calls `asyncio.run(orchestrator.run())`

The `pyproject.toml` entry point is defined in §14.

---

## 12. Illustrative Source Implementations

### 12.1 Scoped source skeleton

```python
# book_metadata_scraper/sources/scoped/example_publisher.py
import logging
from typing import AsyncIterator
from book_metadata_scraper.sources.base import BaseScopedSource
from book_metadata_scraper.sources.registry import scoped_source
from book_metadata_scraper.models import BookData, AuthorData

logger = logging.getLogger(__name__)

@scoped_source
class ExamplePublisherSource(BaseScopedSource):
    name = "example_publisher"

    async def discover_book_urls(self) -> AsyncIterator[str]:
        base_url = self.config.get("base_url", "https://www.examplepublisher.com/new-releases")
        page = 1
        while True:
            response = await self.session.fetch_stealthy(f"{base_url}?page={page}")
            links = response.css("a.book-listing::attr(href)").getall()
            if not links:
                break
            for link in links:
                yield response.urljoin(link)
            if not response.css("a.next-page"):
                break
            page += 1

    async def parse_book(self, response) -> BookData | None:
        title = response.css("h1.book-title::text").get()
        if not title:
            logger.warning("Could not find title on %s", response.url)
            return None

        authors = [
            AuthorData(name=a.strip())
            for a in response.css("span.author::text").getall()
        ]
        isbn13 = response.css("span.isbn13::text").get("").strip() or None

        return BookData(
            title=title.strip(),
            authors=authors,
            publisher="Example Publisher",
            published_date=response.css("span.pub-date::text").get(),
            identifiers={"isbn13": isbn13} if isbn13 else {},
            source_url=response.url,
        )
```

### 12.2 Universal source skeleton

```python
# book_metadata_scraper/sources/universal/google_books.py
import logging
from book_metadata_scraper.sources.base import BaseUniversalSource
from book_metadata_scraper.sources.registry import universal_source
from book_metadata_scraper.models import BookData, AuthorData
from book_metadata_scraper.fetcher import SESSION_HTTP

logger = logging.getLogger(__name__)

@universal_source
class GoogleBooksSource(BaseUniversalSource):
    name = "google_books"
    session_type = SESSION_HTTP   # JSON API — no stealth needed
    BASE_URL = "https://www.googleapis.com/books/v1/volumes"

    async def enrich(self, book: BookData, existing_identifiers: dict[str, str]) -> BookData:
        api_key = self.config.get("api_key", "")
        params = self._build_query(book, existing_identifiers, api_key)
        if params is None:
            return book  # not enough info to search

        response = await self.session.fetch_http(f"{self.BASE_URL}?{params}")
        data = response.json()
        items = data.get("items", [])
        if not items:
            logger.debug("Google Books: no results for '%s'", book.title)
            return book

        info = items[0].get("volumeInfo", {})
        ids = {
            id_["type"]: id_["identifier"]
            for id_ in info.get("industryIdentifiers", [])
        }
        ids["google_books"] = items[0].get("id", "")

        return BookData(
            title=book.title,       # never update title from enrichment
            authors=book.authors,   # see §8.3 — authors from universal sources ignored
            description=info.get("description"),
            publisher=info.get("publisher"),
            published_date=info.get("publishedDate"),
            page_count=info.get("pageCount"),
            language=info.get("language"),
            genres=info.get("categories", []),
            identifiers=ids,
            cover_image_url=info.get("imageLinks", {}).get("thumbnail"),
        )

    def _build_query(self, book, existing_identifiers, api_key) -> str | None:
        import urllib.parse
        if "isbn13" in existing_identifiers:
            q = f"isbn:{existing_identifiers['isbn13']}"
        elif "isbn" in existing_identifiers:
            q = f"isbn:{existing_identifiers['isbn']}"
        elif book.title and book.authors:
            first_author = book.authors[0].name
            q = f"intitle:{book.title}+inauthor:{first_author}"
        else:
            return None
        parts = {"q": q, "maxResults": "1"}
        if api_key:
            parts["key"] = api_key
        return urllib.parse.urlencode(parts)
```

---

## 13. Error Handling Conventions

- Source plugins **must not** allow unhandled exceptions to propagate. Each `discover_book_urls()` and `parse_book()` implementation should wrap its logic in `try/except Exception` and log + return `None` / yield nothing on failure. The orchestrator also wraps each source in a `try/except` for defence in depth, but a failing source should not abort the run.
- Network errors (timeouts, HTTP 4xx/5xx) caught by Scrapling should be logged at ERROR with the URL and re-raised. The orchestrator catches these at the book level and continues to the next book.
- The orchestrator logs a final summary at INFO level: total books discovered, inserted, updated, skipped, and errors per source.

---

## 14. Installation and Dependencies

```toml
# pyproject.toml (dependencies section)
[project]
name = "book-metadata-scraper"
requires-python = ">=3.12"
dependencies = [
    "scrapling[fetchers]",
    "aiosqlite",
    "tomllib",      # stdlib in 3.11+, no extra needed
]

[project.scripts]
book-metadata-scraper = "book_metadata_scraper.cli:main"
```

After `pip install -e .`, the browser dependencies must be installed once:

```bash
scrapling install
```

This is a one-time step on the VPS (Scrapling downloads Playwright's Chromium and its system dependencies). It does not need to be repeated on each cron run.

Cron entry example (runs daily at 03:00):

```
0 3 * * * cd /opt/book-metadata-scraper && /opt/book-metadata-scraper/.venv/bin/book-metadata-scraper --config scraper.toml >> /var/log/book-metadata-scraper/cron.log 2>&1
```

`logrotate` should be configured to rotate `/var/log/book-metadata-scraper/book-metadata-scraper.log` and `/var/log/book-metadata-scraper/cron.log` independently.

---

## 15. Version Control

The project uses **Git** for version control.

### Repository initialisation

```bash
git init
git add .
git commit -m "Initial project structure"
```

### `.gitignore`

The following should be excluded from version control:

```gitignore
# Python
__pycache__/
*.py[cod]
*.egg-info/
dist/
build/
.venv/

# Runtime data — these are instance-specific, not project files
book_metadata.db
book_metadata.db-wal
book_metadata.db-shm
*.log

# Configuration containing secrets (API keys etc.)
# Commit a scraper.toml.example with placeholder values instead
scraper.toml
```

`scraper.toml` is excluded because it will typically contain API keys. A `scraper.toml.example` with all keys present but values set to placeholders (e.g. `api_key = "YOUR_KEY_HERE"`) should be committed in its place so new contributors know the expected structure.

### Branching and commit conventions

No specific branching model is mandated, but the following are recommended:

- `main` should always represent a runnable state.
- Feature branches should be named `feature/<short-description>`, e.g. `feature/amazon-source`.
- Each new source plugin is a natural unit of work for a single branch and PR.
- Commit messages should follow the conventional commits format where practical: `feat:`, `fix:`, `refactor:`, `docs:`, `chore:`.

---

## 16. Future Considerations (Out of Scope for v1)

These are noted here so the initial design does not inadvertently make them harder to add later:

- **Per-source rate limiting / crawl delay**: the `BaseSource` could gain a `crawl_delay_seconds: float = 0.0` class attribute that the orchestrator respects between requests to the same source, without affecting the global semaphore.
- **Proxy support**: `SessionManager.fetch()` already passes `**kwargs` through to Scrapling, so a `proxy=` argument can be forwarded transparently. A future `ProxyRotator` could be composed in.
- **Webhook / notification on completion**: the orchestrator's summary step is a natural hook for posting a result payload to a Slack webhook or similar.
- **Genre normalisation / taxonomy**: a future `genres_aliases` config table could map variant names ("Sci-Fi", "Science Fiction", "SF") to a canonical value.
- **Selective enrichment**: once real-world data shapes are known, the config could gain an `enrich_with` list per scoped source, or a minimum completeness threshold before triggering enrichment.
- **Author disambiguation**: the normalised-name deduplication works for most cases but will merge distinct authors with the same name. A future `author_identifiers` table (VIAF, ISNI, Wikipedia) would allow proper disambiguation.
