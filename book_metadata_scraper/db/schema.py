"""Database schema creation and migration helpers.

All CREATE TABLE statements live here.  Tables are created on first run.
SQLite is opened with WAL journal mode and foreign keys enabled.
"""

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_TABLES = """
CREATE TABLE IF NOT EXISTS sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    source_type TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS books (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT    NOT NULL,
    subtitle         TEXT,
    description      TEXT,
    publisher        TEXT,
    published_date   TEXT,
    page_count       INTEGER,
    language         TEXT    DEFAULT 'en',
    series           TEXT,
    series_position  REAL,
    cover_image_url  TEXT,
    source_url       TEXT,
    first_seen_source_id INTEGER REFERENCES sources(id),
    created_at       TEXT    DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at       TEXT    DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS authors (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT    NOT NULL,
    normalised_name  TEXT    NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS book_authors (
    book_id    INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    author_id  INTEGER NOT NULL REFERENCES authors(id),
    role       TEXT    NOT NULL DEFAULT 'author',
    PRIMARY KEY (book_id, author_id, role)
);

CREATE TABLE IF NOT EXISTS book_identifiers (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id           INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    identifier_type   TEXT    NOT NULL,
    identifier_value  TEXT    NOT NULL,
    UNIQUE (identifier_type, identifier_value)
);

CREATE INDEX IF NOT EXISTS idx_book_identifiers_lookup
    ON book_identifiers (identifier_type, identifier_value);

CREATE TABLE IF NOT EXISTS genres (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name  TEXT    NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS book_genres (
    book_id   INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    genre_id  INTEGER NOT NULL REFERENCES genres(id),
    PRIMARY KEY (book_id, genre_id)
);

CREATE TABLE IF NOT EXISTS book_enrichment_log (
    book_id    INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    source_name TEXT   NOT NULL,
    enriched_at TEXT   NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (book_id, source_name)
);

CREATE VIRTUAL TABLE IF NOT EXISTS books_fts USING fts5(
    title,
    subtitle,
    description,
    content='books',
    content_rowid='id'
);
"""

_TRIGGERS = [
    """CREATE TRIGGER IF NOT EXISTS books_updated_at
    AFTER UPDATE ON books
    FOR EACH ROW
    BEGIN
        UPDATE books SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
        WHERE id = OLD.id;
    END""",
]

_PRAGMAS = [
    "PRAGMA journal_mode = WAL",
    "PRAGMA foreign_keys = ON",
]

# Pre-split DDL into individual statements (split on top-level semicolons only)
_DDL_STATEMENTS: list[str] = [
    s.strip() for s in _TABLES.split(";") if s.strip()
]


async def create_tables(conn) -> None:
    """Execute all CREATE TABLE / CREATE TRIGGER statements and set PRAGMAs."""
    for pragma in _PRAGMAS:
        await conn.execute(pragma)

    for stmt in _DDL_STATEMENTS:
        await conn.execute(stmt)

    for stmt in _TRIGGERS:
        await conn.execute(stmt)

    await conn.commit()
    logger.info("Database tables created / verified")


async def rebuild_fts_index(conn) -> None:
    """Rebuild the FTS5 index from the books table.

    Call this once at the end of a scrape run, after all inserts and
    updates are committed.  The rebuild reads every row from the books
    table and repopulates books_fts in a single pass — typically
    completes in well under a second for datasets of this size.
    """
    await conn.execute(
        "INSERT INTO books_fts(books_fts) VALUES('rebuild')"
    )
    await conn.commit()
    logger.info("FTS5 index rebuilt")
