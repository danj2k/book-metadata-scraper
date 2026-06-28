# Known Issues

## Limitations

### Title matching is exact
The fallback identity resolution (when no identifier match is found) uses exact case-sensitive title comparison. Books with slightly different titles across sources (e.g. one includes a subtitle, another doesn't) will be treated as separate books. This is a deliberate trade-off to avoid false positive merges.

### No author disambiguation
The normalised name deduplication merges distinct authors who happen to have the same name (e.g. two different "John Smith" authors). There's no mechanism for external author identifiers (VIAF, ISNI, etc.) to disambiguate.

### Genre name deduplication
Genre names are stored as received from the source with no normalisation. "Science Fiction", "Sci-Fi", and "SF" are stored as three separate genres. A future pass could add a genre alias table.

### Enrichment is one-shot per source
Once a book is marked as enriched by a source (via `book_enrichment_log`), it's never re-enriched. If a universal source adds new data types or improves its data later, already-enriched books won't benefit. A "re-enrich" command could clear the log and re-run enrichment.

### No retry logic for failed fetches
Network errors during discovery or parsing are logged and skipped. There's no automatic retry with backoff. The daily cron schedule means failed books will be retried on the next run (since they won't be in the DB), but transient failures during a single run are permanently skipped.

### Stealthy session memory usage
The `AsyncStealthySession` starts a headless Chromium instance which uses significant memory (~200-300MB). On a VPS with limited RAM, this could be an issue if many sources require stealth fetching. Currently only sources that explicitly set `session_type = SESSION_STEALTHY` use it.

## Technical Debt

### Manual context manager management in SessionManager
The `SessionManager` calls `__aenter__` / `__aexit__` directly on Scrapling context managers rather than using `async with`. This works but is fragile — if Scrapling changes its context manager protocol, this code would break silently.

### Tuple yield form for discovery
The `(url, position)` tuple extension to `discover_book_urls` is a pragmatic hack. A cleaner `BookDiscovery` dataclass would be more extensible but would require changing all existing source plugins.

### No schema migration
Tables are created with `CREATE TABLE IF NOT EXISTS` — there's no migration system. If the schema changes in a future version (e.g. adding a column), existing databases won't be altered. This is acceptable for a v1 tool but will need addressing before schema changes are made.

### Commit per book in enrichment
Each enrichment call commits individually. For large databases, batch commits (e.g. every 100 books) would be significantly faster. The current approach trades performance for simplicity and crash safety.

## Future Improvements

- **Per-source crawl delay** — configurable `crawl_delay_seconds` on `BaseSource` class attribute, respected by the orchestrator between requests to the same source.
- **Proxy support** — `SessionManager.fetch()` already passes `**kwargs` through, so `proxy=` can be forwarded transparently. A `ProxyRotator` could be composed in.
- **Webhook / notification on completion** — the orchestrator's summary step is a natural hook for posting results to Slack or similar.
- **Genre normalisation / taxonomy** — a `genres_aliases` config table mapping variant names to canonical values.
- **Selective enrichment** — per-source `enrich_with` lists or minimum completeness thresholds before triggering enrichment.
- **Re-enrich command** — CLI flag to clear enrichment log and re-run all universal sources.
- **Fuzzy title matching** — with a confidence threshold and manual review queue for ambiguous matches.
- **Source health monitoring** — track success/failure rates per source and alert on degradation.
- **Incremental discovery** — for large catalogues, track last-scraped position and resume from there on subsequent runs.
