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

## 2026-06-28: Podium Entertainment scoped source

Explored the Podium website structure:
- `/titles` page has a JavaScript-driven "Load More" button (inaccessible to plain HTTP)
- `/sitemap.xml` contains all ~13,500 individual title URLs in one request — ideal for discovery
- Book pages are server-rendered HTML (Next.js App Router) with no JSON-LD or Open Graph metadata
- URL pattern: `/titles/{numeric_id}/{slug}` — the numeric ID is a stable unique identifier

Key discovery: Scrapling's `FetcherSession` returns `html_content` (parsed HTML) and `css()` selectors, but `text` is empty. Must use CSS selectors and `get_all_text()` for content extraction.

Implemented `podium.py`:
- Discovery: single fetch of `sitemap.xml`, regex-extract all `/titles/{id}/{slug}` URLs
- Parsing: CSS selectors for title (h1), series, author, genre; `get_all_text()` for metadata; regex on raw HTML for description
- Identifiers: `podium_id` from URL path, ISBN-13 from Bookshop/B&N/Audiobooks.com links, ASINs from Amazon/Audible links
- Cover image: decoded from `_next/image?url=` wrapper to direct `assets.podiumentertainment.com` URL
- Description: extracted from HTML `<p>` tags between metadata and "This book is part of" section

Verified: parsing works for series books (Columbus Day), standalone books (Enigma), and books without series position. Discovery yields 13,560 unique URLs.

## 2026-06-28: Amazon UK universal source

Explored the Amazon UK website structure:
- `/s?k={query}&i=stripbooks` — search endpoint returns results with `data-asin` attributes
- `/dp/{ASIN}` — product pages with different layouts for Kindle vs audiobook vs print
- Kindle pages have complete metadata (publisher, date, pages, ISBNs, description)
- Audiobook pages use table layout (`<tr>/<th>/<td>`) instead of list layout (`<li>`)
- Format section (`#formats`) contains links to all editions with ASINs
- `tmm-grid-swatch-KINDLE` div contains the Kindle ASIN for the book

Key discoveries:
1. Amazon's search returns the most popular edition (often audiobook), not necessarily the one we want
2. Non-Kindle pages can be redirected to Kindle pages by extracting the Kindle ASIN from format links
3. The stealthy fetcher works reliably for Amazon UK (no CAPTCHA issues observed)
4. Product details section has two formats: list (`<li>`) for print/Kindle, table (`<tr>/<th>/<td>`) for audiobooks
5. The "Product details" heading may have whitespace before `</h2>` — match with just the text

Implemented `amazon_uk.py`:
- Uses stealthy fetcher for all requests (Amazon's WAF blocks plain HTTP)
- Search strategy: ASIN > ISBN > title+author
- `_parse_book_page()` detects non-Kindle pages and returns the Kindle ASIN for redirect
- `_enrich_from_asin()` follows up to 2 redirects to reach the Kindle page
- `_parse_product_details()` handles both list and table formats
- `_parse_search_result()` extracts ASINs from search results and matches by title similarity
- `_find_best_match()` uses Jaccard similarity on title words (threshold: 0.3)
- Enriches: ASIN, ISBNs, publisher, publication date, page count, language, description, cover image
- Title and authors set to sentinels (universal sources don't update these)

Verified end-to-end:
- ISBN search (`9780593135204`) → search → audiobook page → redirect to Kindle page → full metadata
- Audiobook ASIN (`B08G9SKSHR`) → redirect to Kindle page → full metadata
- Kindle ASIN (`B08FFJS3YW`) → direct page → full metadata
- All tests return correct data: publisher (Cornerstone Digital), date (4 May 2021), pages (481), language (en)
