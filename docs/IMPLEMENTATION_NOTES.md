# Implementation Notes

## Schema DDL splitting

The schema DDL is stored as a single multi-line string and split on semicolons. SQLite trigger bodies contain semicolons inside `BEGIN...END` blocks, so triggers are stored in a separate `_TRIGGERS` list and executed after the table DDL. The table DDL splitting is safe because no table definition contains semicolons outside the statement terminator.

**Invariant:** Every entry in `_DDL_STATEMENTS` must be a complete, valid SQL statement. Every entry in `_TRIGGERS` must be a complete `CREATE TRIGGER` statement.

## Scrapling session lifecycle

`SessionManager` manages Scrapling context managers manually (calling `__aenter__` / `__aexit__` directly) rather than using `async with`. This is because the session must persist across many fetch calls within a single orchestrator run, not be created and destroyed per request.

The `FetcherSession` is accessed via `.get()` while `AsyncStealthySession` uses `.fetch()` — these are different Scrapling APIs for different session types. Source plugins don't need to know this; they call `session.fetch_http()` or `session.fetch_stealthy()` and the manager routes correctly.

## Stealthy session recycling (OOM prevention)

The stealthy session (patchright/Chromium) accumulates memory over time — V8 heap, DOM caches, internal Chromium state — even after individual pages are closed.  On a low-memory VPS (4GB RAM), this leads to OOM crashes after a few hundred stealthy page loads.

The fix is to destroy and recreate the entire `AsyncStealthySession` periodically, killing the Chromium process and starting a fresh one.  This is controlled by `stealthy_page_limit` (default: 20 fetches).  The restart takes ~2-3s but reclaims all accumulated Chromium memory.

**Why full session restart, not just context recycling:** Scrapling's `AsyncStealthySession` uses `launch_persistent_context`, which creates a single Chromium process.  Even destroying the context doesn't release the process's internal heap.  Only stopping the entire session (calling `playwright.stop()`) fully kills the process and its memory.

**Lazy startup:** The stealthy session is created on first `fetch_stealthy()` call, not at `SessionManager.start()`.  This means HTTP-only runs (scoped sources that don't need stealth) never launch Chromium at all, saving ~200-300MB.

**Chromium memory flags:** The session is created with `extra_flags` that reduce baseline Chromium memory:
- `--disable-dev-shm-usage` — use `/tmp` instead of `/dev/shm` (avoids shared memory issues on small VPS)
- `--disable-extensions` — no browser extensions loaded
- `--disable-background-networking` — no background network requests
- `--disable-default-apps` — don't load default apps
- `--no-first-run` — skip first-run wizard
- `--disable-translate` — no translate popups

These are safe defaults that don't affect anti-bot detection — they strip out features a headless scraper never uses.

**Concurrency during restart:** The `_stealthy_lock` serialises the restart operation.  While a restart is in progress (2-3s), other stealthy fetchers block on the lock.  This is acceptable because: (a) restarts are infrequent (every 20 fetches), (b) the alternative (OOM crash) is far worse, and (c) HTTP sources are unaffected since they use a separate session.

**Tuning:** On a VPS with more RAM, increase `stealthy_page_limit` (e.g. 50 or 100) to reduce restart overhead.  On very constrained systems, decrease it (e.g. 10).  The README documents this as a configuration option.

## HTTP rate limiting implementation

The rate limiter uses `time.monotonic()` (immune to system clock changes) and an asyncio lock to serialise the check-and-sleep. The lock ensures that two concurrent coroutines don't both read the same timestamp and both decide to sleep. The rate limit applies only to HTTP requests (not stealthy browser requests), since the stealthy session is typically used for sites that need JavaScript rendering where rate limiting is less critical.

**Edge case:** If `http_rate_limit` is set and a source uses `fetch_stealthy()`, the stealthy requests are not rate-limited. This is intentional — browser-based fetches have their own natural delays from page rendering.

**Per-source rate limiting:** Sources can set `rate_limit = 1.0` (seconds) as a class attribute and use the `self.fetch()` convenience method instead of `self.session.fetch_http()`. The `fetch()` method routes to the correct session type and passes `self.rate_limit` as `min_interval` to `SessionManager.fetch_http()`, overriding the global `http_rate_limit` for that source. This lets publishers with aggressive WAFs (e.g. Mountaindale's Shopify) get their own rate limit without slowing down other sources.

All sources now use `self.fetch()` exclusively — no source calls `self.session.fetch_http()` or `self.session.fetch_stealthy()` directly. This keeps the codebase uniform and ensures per-source rate limits, session routing, and any future fetch-level concerns are handled in one place.

## COALESCE update strategy

`update_book_nulls` builds a single UPDATE statement with COALESCE for each non-NULL field in the incoming BookData. If all fields in the incoming data are NULL, no UPDATE is executed. The method always commits after updating, even if no rows were actually changed — this is harmless and keeps the code simple.

**Invariant:** `update_book_nulls` must never be called with a BookData that has `title` set to a non-empty string for a book where the title should not change. The orchestrator enforces this by only calling it for enrichment results, where title and authors are set to sentinels.

## Author name normalisation edge cases

The normalisation function replaces runs of dots and whitespace with single hyphens. This means:

- "J.R.R. Tolkien" → "j-r-r-tolkien"
- "Ann Leckie" → "ann-leckie"
- "Le Carré, John" → "le-carré,-john" (comma preserved)

Characters other than dots and spaces (commas, accented characters, apostrophes, hyphens) are preserved. This is intentional — the goal is a stable key for matching, not a URL-safe slug. The downside is that "Smith-Jones" and "Smith Jones" produce different normalised names, but this is rare enough to be acceptable.

**Known limitation:** Authors with genuinely different names that happen to normalise the same way (extremely rare) will be merged. There's no disambiguation mechanism yet.

## JSON-LD extraction from Aethon pages

Aethon book pages embed structured data as `<script type="application/ld+json">` blocks. The parser tries the first block, then falls back to scanning all blocks for one with `@type: Book`. This handles cases where the page has multiple JSON-LD blocks (e.g. a `WebPage` block before the `Book` block).

The parser also handles HTML inside `description` fields by stripping tags and unescaping entities.

## Google Books query construction

The query string is built manually rather than using `urllib.parse.urlencode` for the `q` parameter, because `urlencode` would double-encode the special search operators (`intitle:`, `inauthor:`, `isbn:`). The `q` value is encoded with `urllib.parse.quote(q, safe="")` and the rest of the parameters are appended as literal key=value pairs.

The `google_search=False` kwarg is passed to Scrapling's HTTP fetcher to prevent it from adding a Google referer header, which would cause the Google Books API to return 429 (rate limited).

## Enrichment sentinel values

Universal sources return `BookData` with `title=""` and `authors=[]` as sentinels. The orchestrator detects "no new data" by checking `is not book_data` (identity comparison) and by inspecting whether any enrichment-relevant fields are non-NULL. The empty title/authors ensure the orchestrator's null-update merge doesn't accidentally overwrite the scoped source's title or author data.

## Source URL normalisation

The Aethon source normalises URLs by stripping trailing slashes and ensuring the full `https://aethonbooks.com` prefix. Both the discovery phase and the parse phase use `_normalise_book_url()`, so the `source_url` stored in the database matches what the orchestrator checks on subsequent runs. Without this, `https://aethonbooks.com/book/123/` and `https://aethonbooks.com/book/123` would be treated as different books.

## Database transaction boundaries

`insert_book` runs all writes (book row, author upserts, identifier inserts, genre inserts) in a single implicit transaction (aiosqlite commits on `await conn.commit()`). `update_book_nulls` also commits after all updates. `mark_enriched` commits after each call.

**Trade-off:** Committing per book during enrichment means a crash mid-enrichment leaves partial state (some books enriched, some not). This is acceptable because enrichment is idempotent — re-running will pick up where it left off via the enrichment log. The alternative (batch commits) would improve performance but add complexity for rollback handling.

## The `(url, position)` tuple extension

The base class `discover_book_urls` is typed as `AsyncIterator[str | tuple[str, float | None]]`. The orchestrator checks `isinstance(item, tuple)` to handle both forms. This is a pragmatic extension rather than a clean interface change — it avoids requiring all sources to yield tuples while supporting sources that have position information.

A cleaner design would be a `BookDiscovery` dataclass, but that would be a breaking change to all existing source plugins for a feature only one source currently uses.

## Amazon UK non-Kindle page redirect

Amazon product pages have different levels of metadata depending on the format:
- **Kindle pages** have complete metadata: publisher, publication date, page count, ISBNs, language, description
- **Audiobook pages** have minimal metadata: listening length, narrator, release date, but no publisher/page count
- **Print pages** have complete metadata similar to Kindle

The `_parse_book_page()` method detects when it's on a non-Kindle page by checking the `#formats` section for a `tmm-grid-swatch-KINDLE` div containing the Kindle ASIN. If found and the current page's ASIN differs, it returns the Kindle ASIN (as a string) instead of a `BookData`.

The `_enrich_from_asin()` method handles this by looping up to `max_redirects` times: it fetches the product page, checks if the result is a Kindle ASIN string, and if so, fetches the Kindle page instead. This ensures we always get the most complete metadata available.

**Edge case:** If a book has no Kindle edition (e.g. print-only), the format section won't have a KINDLE swatch, so `_parse_book_page()` returns the print page's metadata directly. This is correct — the print page has all the metadata we need.

## Amazon UK search result matching

Amazon's search endpoint returns results sorted by relevance, but the most popular edition (often audiobook) may appear first. The `_parse_search_result()` method extracts ASINs and titles from search results, then uses `_find_best_match()` to find the best match by comparing title words.

The matching algorithm:
1. Tokenize both titles into words (3+ characters, lowercase)
2. Calculate Jaccard similarity: `|intersection| / |union|`
3. Return the result with highest similarity score (if >= 0.3)

The threshold of 0.3 is deliberately low to handle cases where the Amazon title differs significantly from our title (e.g. "Project Hail Mary: A Novel" vs "Project Hail Mary").

**Known limitation:** If the search returns multiple editions with similar titles, the algorithm may pick the wrong one. The subsequent redirect to Kindle page mitigates this for most cases.
