# Design Decisions

## 1. Autoincrement integer primary key for books

**Decision:** Books are identified by an autoincrement `id` column, not by ISBN or any external identifier.

**Alternatives considered:**
- ISBN as primary key — simpler lookups, but a book can have multiple ISBNs (hardcover, paperback, ebook, audiobook). Tying the primary key to one ISBN means other ISBNs can't be looked up as efficiently.
- Composite key (title + author) — fragile with edition variations, different transliterations, etc.

**Consequences:** All cross-references (authors, identifiers, genres) use the integer `book_id`. External identifiers are stored in a separate `book_identifiers` table with their own lookup index. This adds a join for identifier lookups but keeps the schema clean and extensible.

## 2. Scoped vs Universal source types

**Decision:** Two distinct source types with different merge semantics. Scoped sources own their data; universal sources only fill NULLs.

**Alternatives considered:**
- Single source type with configurable merge policies per field — more flexible but much more complex to reason about. Every new source would need a merge policy configuration.
- Priority-based merge (source A > source B > source C) — works for simple cases but breaks when two scoped sources disagree on a field.

**Consequences:** The two-tier model is simpler to implement and reason about. Scoped sources are authoritative for their data. Universal sources are supplementary. The downside is that if two scoped sources provide conflicting values for the same field, the first one wins (on insert). This is acceptable because scoped sources are typically publisher catalogues where the data is consistent for their own books.

## 3. COALESCE for null-safe updates

**Decision:** `update_book_nulls` uses `SET field = COALESCE(field, ?)` to only fill NULL columns.

**Alternatives considered:**
- Explicit `WHERE field IS NULL` in the UPDATE — similar effect but requires separate UPDATE statements per field.
- Always overwrite — simpler but loses scoped source data.

**Consequences:** A single UPDATE statement handles all nullable fields. The COALESCE approach means non-NULL values are never touched. The trade-off is that if a scoped source legitimately needs to correct a value (e.g. a typo in the original scrape), there's no built-in mechanism for that — it would require a direct SQL update or a new "force overwrite" mode.

## 4. Enrichment tracking via `book_enrichment_log`

**Decision:** Track which universal sources have already enriched each book using a dedicated log table, rather than relying on identifier presence alone.

**Alternatives considered:**
- Use the presence of a source-specific identifier (e.g. `google_books` identifier) as the "done" marker — simpler but breaks for sources that only add genres or other non-identifier data.
- Re-enrich every book on every run — correct but wasteful as the database grows.

**Consequences:** The log table means every book is enriched by each universal source exactly once. The downside is that if a universal source adds new data types later (e.g. Google Books adds a new field), already-enriched books won't get the new data. A future "re-enrich" command could address this by clearing the log.

## 5. Scrapling as the fetching backend

**Decision:** Use Scrapling for all HTTP activity, with two session types: `FetcherSession` (fast HTTP) and `AsyncStealthySession` (headless browser with anti-bot).

**Alternatives considered:**
- `httpx` + `playwright` directly — more control but more boilerplate, and Scrapling already wraps both with a consistent API.
- `requests` + `selenium` — older stack, worse anti-bot evasion.
- Separate libraries per source — no shared session management, harder to enforce concurrency limits.

**Consequences:** Scrapling provides a unified API for both plain HTTP and browser-based fetching, with built-in TLS fingerprint impersonation and anti-bot evasion. The downside is a heavier dependency tree (Playwright, curl_cffi, browserforge). The stealthy session starts a Chromium instance, which uses significant memory — but this is acceptable for a VPS with adequate RAM.

## 6. Global rate limiting via SessionManager

**Decision:** Rate limiting is implemented in `SessionManager` using a monotonic clock and asyncio lock, applied to all HTTP requests globally.

**Alternatives considered:**
- Per-source rate limiting (crawl delay per source) — more granular but more complex to coordinate with the shared semaphore.
- No rate limiting — faster but risks IP bans or 429 errors.

**Consequences:** The global rate limit is simple and prevents abuse. Per-source rate limiting is noted as a future consideration (see design doc section 16). The current implementation uses `time.monotonic()` which is immune to system clock adjustments.

## 7. Source plugin auto-discovery via pkgutil

**Decision:** `sources/__init__.py` uses `pkgutil.iter_modules` to auto-import all modules under `sources/scoped/` and `sources/universal/`. The `@scoped_source` / `@universal_source` decorators run at import time to register classes.

**Alternatives considered:**
- Explicit import list in `sources/__init__.py` — requires editing every time a new source is added.
- Entry points / plugin system (e.g. `importlib.metadata`) — overkill for a single-project tool.
- Config-driven source loading — more flexible but adds complexity and runtime errors.

**Consequences:** Adding a new source is a single-file operation: create the module, decorate the class, and it's available. The decorator approach means registration happens at import time with no explicit wiring. The trade-off is that typos in the source name or class hierarchy produce import-time errors rather than runtime errors — but this is actually preferable for early failure detection.

## 8. Series position from discovery, not parsing

**Decision:** Series positions are extracted during the discovery phase (from the series page's `hasPart` JSON-LD) and passed to the orchestrator as `(url, position)` tuples, rather than being scraped from individual book pages.

**Alternatives considered:**
- Extract position from the book page's JSON-LD `isPartOf.position` — often missing or inconsistent across sources.
- Infer position from the order books appear in the discovery listing — fragile if the listing order changes.

**Consequences:** The discovery-phase approach gives accurate positions because series pages list books in order with explicit position numbers. Individual book pages may not include position information at all (Aethon's book pages don't). The tuple yield form (`url, position`) is an extension to the base class that the orchestrator handles transparently — plain URL strings still work for sources that don't provide positions.

## 9. TOML configuration with CLI overrides

**Decision:** Configuration is a TOML file with CLI flags for key overrides (`--db`, `--log-level`).

**Alternatives considered:**
- Environment variables — harder to document, harder to see all config at a glance.
- YAML — more expressive but more complex parser, and TOML is stdlib in Python 3.11+.
- JSON — no comments, harder to write by hand.

**Consequences:** TOML is clean, readable, and has a stdlib parser. The CLI overrides mean the most common runtime adjustments (pointing at a different database, increasing log verbosity for debugging) don't require editing the config file. The `scraper.toml` is gitignored because it contains API keys; a `scraper.toml.example` with placeholders is committed instead.

## 10. Identity matching: exact title + normalised author fallback

**Decision:** When no identifier match is found, fall back to exact title comparison plus normalised author name matching (via the `normalised_name` column on authors).

**Alternatives considered:**
- Fuzzy title matching (Levenshtein, trigram) — catches typos but introduces false positives (different editions with similar titles).
- Title-only matching — too many collisions (e.g. "The Way of Kings" could be multiple unrelated books).
- No fallback at all — too many books would be duplicated because scoped sources often don't provide ISBNs.

**Consequences:** Exact title matching is conservative but safe. The normalised author name (lowercase, dots/spaces collapsed to hyphens) handles common variations ("J.R.R. Tolkien" vs "J R R Tolkien"). The system will miss books where the title differs slightly between sources (e.g. subtitle included in one but not the other), but this is preferable to false positive merges. A future improvement could add fuzzy matching with a confidence threshold.
