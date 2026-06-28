# Development Log

## 2026-06-28: Initial project structure

Created the full project skeleton from the design document (book-metadata-scraper-design.md). Implemented all core modules:

- `cli.py` — entry point with argparse, TOML config loading, logging setup
- `config.py` — `ScraperConfig` dataclass + `load_config()`
- `fetcher.py` — `SessionManager` with Scrapling `FetcherSession` and `AsyncStealthySession`, semaphore-capped concurrency
- `models.py` — `BookData` and `AuthorData` dataclasses
- `normalise.py` — author name dedup keys (strip diacritics, lowercase, collapse whitespace)
- `matching.py` — identity resolution (ISBN > ASIN > title+author)
- `orchestrator.py` — main pipeline: scoped discovery → identity resolution → storage → universal enrichment
- `db/schema.py` — 8 tables + trigger for null-safe scoped updates
- `db/repository.py` — all async SQL via aiosqlite
- `sources/base.py` — `BaseSource`, `BaseScopedSource`, `BaseUniversalSource`
- `sources/registry.py` — decorator-based source registration

## 2026-06-28: Aethon Books scoped source

Implemented `aethon.py` as the first scoped source:

- Discovery via `/series/` index page (all ~500 series in one request)
- Series page parsing via JSON-LD `CreativeWorkSeries.hasPart`
- Book parsing via JSON-LD `@type: Book`
- Added `min_interval` rate limiting to `SessionManager`
- Added `discover_book_urls()` support for `(url, position)` tuples
- Verified: Discovery yields correct positions, parsing extracts all metadata and identifiers

## 2026-06-28: Google Books universal source

Implemented `google_books.py` as the first universal source:

- ISBN lookup via `isbn:` endpoint (most reliable)
- Title+author search via `intitle:` + `inauthor:`
- Enrichment: description, publisher, date, pages, language, cover image, genres/categories
- Uses `projection=full` for complete `volumeInfo` (identifiers, imageLinks, categories)
- Uses `google_search=False` to avoid Scrapling's default Google referer header

## 2026-06-29: Podium Entertainment scoped source

Implemented `podium.py`:

- Discovery via `sitemap.xml` (~13,500 URLs in one request) — avoids JavaScript lazy-loading
- CSS selector parsing (no JSON-LD on this site)
- HTML content quirk: Scrapling's `FetcherSession` returns `html_content` but `text` is empty
- Extracts `podium_id` from URL path (`/titles/{id}/{slug}`) for deduplication
- ISBN extraction from retailer links (Bookshop.org, B&N, Audiobooks.com, Walmart)
- ASIN extraction from Amazon/Audible links

## 2026-06-29: README and LICENSE

Added `README.md` and `LICENSE` (MIT) to the repository root.

## 2026-06-30: Documentation suite

Created comprehensive documentation in `docs/`:

- `PROJECT.md` — purpose, goals, non-goals, constraints
- `ARCHITECTURE.md` — system overview, components, data flow, dependencies
- `DESIGN_DECISIONS.md` — 10 key decisions with alternatives and consequences
- `IMPLEMENTATION_NOTES.md` — non-obvious details, quirks, edge cases
- `DEVELOPMENT_LOG.md` — this file (chronological record)
- `KNOWN_ISSUES.md` — limitations, technical debt, future improvements

## 2026-06-30: Amazon UK universal source

Implemented `amazon_uk.py`:

- Stealthy fetcher required (Amazon's WAF blocks plain HTTP)
- Search strategy: ISBN → title+author → ASIN
- Follows non-Kindle pages (audiobook, hardcover) to Kindle edition for complete metadata
- Handles two product detail formats: list (`<li>`) for print/Kindle, table (`<tr>/<th>/<td>`) for audiobooks
- Title/author similarity matching via Jaccard coefficient (threshold: 0.3)
- Enrichment: ASIN identifiers, ISBN-10/13, publisher, date, pages, language, description, cover image

## 2026-07-01: LNRelease scoped source

Implemented `lnrelease.py`:

- Data source: single JSON file at `lnrelease.github.io/data.json` (~10,000 entries)
- JSON caching with 24-hour TTL in `~/.book-metadata-scraper/cache/lnrelease/`
- Groups entries by (title, volume) — different format editions merge into one record
- Format-specific ISBN identifiers (ebook, paperback, hardcover, audiobook)
- No authors (light novels); series name and position from lookup tables
- Session type: HTTP (GitHub Pages)

## 2026-07-02: Mountaindale Press scoped source

Implemented `mountaindale.py`:

- Data source: Shopify collections API (`/collections/all-books/products.json`)
- Single HTTP request with pagination (250 products per page)
- Filters out bundles/box sets by title patterns and variant count
- Author from `vendor` field (skips "Amazon" entries which aren't real authors)
- Series name extracted from title patterns and tags
- Genres from tags (LitrPG, GameLit, etc.)
- Identifiers: `mountaindale_id` from Shopify product handle

## 2026-07-03: Shadow Alley Press scoped source

Implemented `shadow_alley.py`:

- Discovery: library page → series pages (article order = position) → book URLs; also standalone books from New Releases
- Dual-template support (Genesis + block editor)
- Title: `entry-title` → `wp-block-post-title` → `og:title` fallback
- Authors: from article CSS classes (`book-authors-{slug}` → human-readable name)
- Series: from `book-series-book` element (works for both `<span>` and `<p>`)
- Genres: from article CSS classes (`book-tags-{slug}`) and footer "Tagged with:" links
- Description: Genesis uses `itemprop="text"` + `<h3>` tagline; blocks use `<p class="wp-block-paragraph">` after title heading
- Metadata: Genesis uses `<li>` items (Pages, Published, Duration); blocks extract from different locations
- Cover image: from `author-pro-featured-image` div or first site-hosted image
- Identifiers: `shadow_alley_id` from URL slug

Verified:
- Genesis template: Rogue Dungeon — full metadata (title, 2 authors, series #1, genres, date, pages, description)
- Block template: Wayspring Wildshaper — title, author, series #3, genres, pages, description (905 chars)
- Block template: Apocalypse Redux 2 — title, author, genres, pages, description (box set)
- Discovery: 58 series processed, positions correctly assigned from article order

## 2026-07-03: CLI --list-sources flag

Added `--list-sources` command-line flag to list all available sources and their enabled status.

- Prints formatted table: source name, type (scoped/universal), session type (http/stealthy), enabled/disabled
- Reads `enabled_scoped_sources` and `enabled_universal_sources` from config
- Exits after printing (no database required)
- Example output:
  ```
  Available sources:
    SOURCE              TYPE       SESSION   STATUS
    aethon_books        scoped     http        disabled
    google_books        universal  http        disabled
    amazon_uk           universal  stealthy    disabled
    ```

    ## 2026-07-03: Per-source rate limiting

    Added per-source rate limiting to support sources that need different request cadences:

    - `BaseSource.rate_limit` class attribute (default `None`) — each source can declare its own minimum seconds between requests
    - `BaseSource.fetch()` convenience method — routes to `fetch_http`/`fetch_stealthy` based on `session_type` and passes `self.rate_limit` as `min_interval`
    - `SessionManager.fetch_http()` gained a `min_interval` keyword argument that overrides the global `http_rate_limit` for a single call
    - `MountaindalePressSource` set to `rate_limit = 1.0` (1 req/sec) — Shopify's WAF returns 429 on faster requests
    - Sources that don't set `rate_limit` are unaffected — `self.fetch()` falls through to the global rate limit (or no limit if unset)

    Motivation: Mountaindale Press uses Shopify's storefront API which rate-limits aggressively. Running without rate limiting produced 429 errors during catalog fetches. The global rate limit was left unset so faster sources (Aethon, Podium, etc.) are not slowed down.

## 2026-07-03: Migrate all sources to self.fetch()

Updated all source plugins to use `self.fetch()` instead of calling `self.session.fetch_http()` / `self.session.fetch_stealthy()` directly:

- `aethon.py` — 2 calls migrated
- `shadow_alley.py` — 2 calls migrated
- `podium.py` — 1 call migrated
- `lnrelease.py` — 1 call migrated
- `google_books.py` — 2 calls migrated (passes `google_search=False` through kwargs)
- `amazon_uk.py` — 2 calls migrated (passes `timeout=30000` through kwargs)

All sources now route through `BaseSource.fetch()`, which handles session type selection, per-source rate limit injection, and any future fetch-level concerns uniformly.
