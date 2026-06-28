# Project: Book Metadata Scraper

## Purpose

A CLI tool that scrapes book metadata from multiple web sources and stores it in a local SQLite database. Designed to run unattended as a daily cron job on Ubuntu.

The primary use case is maintaining a comprehensive, deduplicated catalogue of book metadata by combining high-quality data from publisher/scoped sources with enrichment from universal catalogues.

## Goals

- Aggregate book metadata from multiple configurable sources into a single local database.
- Run unattended on a schedule (daily cron) without human intervention.
- Deduplicate books across sources using identifiers and title+author matching.
- Preserve scoped source data as authoritative — never overwrite it with enrichment data.
- Enrich books with supplementary data from universal sources where fields are NULL.
- Be extensible: new sources can be added by creating a single Python module without touching core code.

## Non-Goals

- Not a user-facing web application or API.
- Not a recommendation engine — this is purely a data collection tool.
- Not a replacement for Goodreads, OpenLibrary, or other book platforms — it aggregates from them.
- Not designed for real-time scraping or high-throughput data pipelines.
- Does not handle user accounts, authentication, or multi-user access.

## Constraints

- **Python 3.12+** — uses modern type hints (`X | None`, `list[str]`).
- **No more than 5 concurrent fetch operations** at any time, enforced by an asyncio Semaphore.
- **Single shared Scrapling session** for all fetching — no per-request session creation/teardown.
- **SQLite** as the only database — no external database server required.
- **Scoped source data is sacred**: values from publisher/catalogue sources are written on insert and never overwritten by enrichment.
- **Universal sources only fill NULL fields**: they add identifiers, genres, and fill missing metadata, but never replace existing values.
- **Primary key for books is an autoincrement integer** — never an ISBN or other external identifier, because a book can have multiple identifiers.
- Must run on a VPS (Ubuntu) with no display server — all browser-based fetching uses headless mode.
