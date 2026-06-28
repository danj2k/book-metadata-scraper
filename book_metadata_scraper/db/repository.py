"""All database I/O — one class, all methods async.

No SQL appears anywhere else in the codebase.  The repository uses aiosqlite
and is initialised with the path to the database file.
"""

import logging
from typing import Optional

import aiosqlite

from book_metadata_scraper.db.schema import create_tables
from book_metadata_scraper.models import BookData, AuthorData
from book_metadata_scraper.normalise import normalise_author_name

logger = logging.getLogger(__name__)


class Repository:
    """Async wrapper around a SQLite database for book metadata."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def initialise(self, sources: list[tuple[str, str]] | None = None) -> None:
        """Open the database, create tables, and upsert source rows.

        Args:
            sources: list of (name, source_type) tuples to register.
        """
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await create_tables(self._conn)

        if sources:
            for name, source_type in sources:
                await self.upsert_source(name, source_type)
            await self._conn.commit()

        logger.info("Repository initialised (%s)", self._db_path)

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Source management
    # ------------------------------------------------------------------

    async def upsert_source(self, name: str, source_type: str) -> int:
        """Register a source and return its id."""
        cursor = await self._conn.execute(
            "INSERT OR IGNORE INTO sources (name, source_type) VALUES (?, ?)",
            (name, source_type),
        )
        if cursor.rowcount == 0:
            # Already existed — fetch existing id
            row = await self._conn.execute(
                "SELECT id FROM sources WHERE name = ?", (name,)
            )
            row = await row.fetchone()
            return row["id"]
        await self._conn.commit()
        return cursor.lastrowid

    # ------------------------------------------------------------------
    # Book lookups
    # ------------------------------------------------------------------

    async def find_book_by_identifier(self, id_type: str, id_value: str) -> int | None:
        """Look up a book by a single identifier.  Returns book_id or None."""
        cursor = await self._conn.execute(
            "SELECT book_id FROM book_identifiers WHERE identifier_type = ? AND identifier_value = ?",
            (id_type, id_value),
        )
        row = await cursor.fetchone()
        return row["book_id"] if row else None

    async def find_book_by_title_and_author(
        self, title: str, normalised_names: list[str]
    ) -> int | None:
        """Exact title match + at least one author with a matching normalised name.

        Returns book_id if exactly one match, None otherwise.
        """
        if not normalised_names:
            return None

        placeholders = ",".join("?" for _ in normalised_names)
        cursor = await self._conn.execute(
            f"""
            SELECT DISTINCT b.id
            FROM books b
            JOIN book_authors ba ON ba.book_id = b.id
            JOIN authors a ON a.id = ba.author_id
            WHERE b.title = ?
              AND a.normalised_name IN ({placeholders})
            """,
            [title, *normalised_names],
        )
        rows = await cursor.fetchall()

        if len(rows) == 1:
            return rows[0]["id"]
        if len(rows) > 1:
            ids = [r["id"] for r in rows]
            logger.warning(
                "Multiple book matches for '%s' with authors %s — ids: %s",
                title,
                normalised_names,
                ids,
            )
        return None

    async def find_book_by_source_url(self, url: str) -> int | None:
        """Check if a book with this source_url already exists."""
        cursor = await self._conn.execute(
            "SELECT id FROM books WHERE source_url = ?", (url,)
        )
        row = await cursor.fetchone()
        return row["id"] if row else None

    async def get_book_by_id(self, book_id: int) -> BookData | None:
        """Fetch a full BookData for the given book_id."""
        cursor = await self._conn.execute(
            "SELECT * FROM books WHERE id = ?", (book_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None

        # Fetch authors
        author_cursor = await self._conn.execute(
            """
            SELECT a.name, ba.role
            FROM book_authors ba
            JOIN authors a ON a.id = ba.author_id
            WHERE ba.book_id = ?
            """,
            (book_id,),
        )
        authors = [
            AuthorData(name=r["name"], role=r["role"])
            for r in await author_cursor.fetchall()
        ]

        # Fetch identifiers
        id_cursor = await self._conn.execute(
            "SELECT identifier_type, identifier_value FROM book_identifiers WHERE book_id = ?",
            (book_id,),
        )
        identifiers = {
            r["identifier_type"]: r["identifier_value"]
            for r in await id_cursor.fetchall()
        }

        # Fetch genres
        genre_cursor = await self._conn.execute(
            """
            SELECT g.name
            FROM book_genres bg
            JOIN genres g ON g.id = bg.genre_id
            WHERE bg.book_id = ?
            """,
            (book_id,),
        )
        genres = [r["name"] for r in await genre_cursor.fetchall()]

        return BookData(
            title=row["title"],
            authors=authors,
            subtitle=row["subtitle"],
            description=row["description"],
            publisher=row["publisher"],
            published_date=row["published_date"],
            page_count=row["page_count"],
            language=row["language"],
            series=row["series"],
            series_position=row["series_position"],
            cover_image_url=row["cover_image_url"],
            genres=genres,
            identifiers=identifiers,
            source_url=row["source_url"],
        )

    async def get_existing_identifiers(self, book_id: int) -> dict[str, str]:
        """Return all identifiers for a book as a {type: value} dict."""
        cursor = await self._conn.execute(
            "SELECT identifier_type, identifier_value FROM book_identifiers WHERE book_id = ?",
            (book_id,),
        )
        return {r["identifier_type"]: r["identifier_value"] for r in await cursor.fetchall()}

    # ------------------------------------------------------------------
    # Book writes
    # ------------------------------------------------------------------

    async def insert_book(self, book: BookData, source_id: int) -> int:
        """Insert a new book with all related rows.  Returns the new book_id.

        Handles author upsert, book_authors, identifiers, and genres.
        All within a single transaction.
        """
        # Insert the book
        cursor = await self._conn.execute(
            """
            INSERT INTO books
                (title, subtitle, description, publisher, published_date,
                 page_count, language, series, series_position,
                 cover_image_url, source_url, first_seen_source_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                book.title,
                book.subtitle,
                book.description,
                book.publisher,
                book.published_date,
                book.page_count,
                book.language,
                book.series,
                book.series_position,
                book.cover_image_url,
                book.source_url,
                source_id,
            ),
        )
        book_id = cursor.lastrowid

        # Upsert authors and link
        for author in book.authors:
            author_id = await self._upsert_author(author.name)
            await self._conn.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, role) VALUES (?, ?, ?)",
                (book_id, author_id, author.role),
            )

        # Identifiers
        for id_type, id_value in book.identifiers.items():
            await self._conn.execute(
                "INSERT OR IGNORE INTO book_identifiers (book_id, identifier_type, identifier_value) VALUES (?, ?, ?)",
                (book_id, id_type, id_value),
            )

        # Genres
        for genre_name in book.genres:
            genre_id = await self._upsert_genre(genre_name)
            await self._conn.execute(
                "INSERT OR IGNORE INTO book_genres (book_id, genre_id) VALUES (?, ?)",
                (book_id, genre_id),
            )

        await self._conn.commit()
        logger.info("Inserted book '%s' (id=%d)", book.title, book_id)
        return book_id

    async def update_book_nulls(self, book_id: int, book: BookData) -> None:
        """Update only NULL fields of an existing record.

        Issues UPDATE ... SET field = COALESCE(field, ?) for each nullable
        field.  Only touches NULL columns; always safe to call.
        """
        # Build COALESCE updates for nullable book table fields
        nullable_fields = {
            "subtitle": book.subtitle,
            "description": book.description,
            "publisher": book.publisher,
            "published_date": book.published_date,
            "page_count": book.page_count,
            "language": book.language,
            "series": book.series,
            "series_position": book.series_position,
            "cover_image_url": book.cover_image_url,
        }

        set_clauses = []
        values = []
        for field_name, new_value in nullable_fields.items():
            if new_value is not None:
                set_clauses.append(f"{field_name} = COALESCE({field_name}, ?)")
                values.append(new_value)

        if not set_clauses:
            return

        values.append(book_id)
        await self._conn.execute(
            f"UPDATE books SET {', '.join(set_clauses)} WHERE id = ?",
            values,
        )

        # Add any new identifiers (INSERT OR IGNORE)
        for id_type, id_value in book.identifiers.items():
            await self._conn.execute(
                "INSERT OR IGNORE INTO book_identifiers (book_id, identifier_type, identifier_value) VALUES (?, ?, ?)",
                (book_id, id_type, id_value),
            )

        # Add any new genres
        for genre_name in book.genres:
            genre_id = await self._upsert_genre(genre_name)
            await self._conn.execute(
                "INSERT OR IGNORE INTO book_genres (book_id, genre_id) VALUES (?, ?)",
                (book_id, genre_id),
            )

        await self._conn.commit()
        logger.debug("Updated null fields for book id=%d: %s", book_id, set_clauses)

    # ------------------------------------------------------------------
    # Enrichment tracking
    # ------------------------------------------------------------------

    async def get_all_books_for_enrichment(self, source_name: str) -> list[tuple[int, BookData, dict]]:
        """Return (book_id, book_data, existing_identifiers) for all books
        not yet enriched by the named source."""
        # Books not in enrichment log for this source
        cursor = await self._conn.execute(
            """
            SELECT b.id
            FROM books b
            WHERE b.id NOT IN (
                SELECT book_id FROM book_enrichment_log WHERE source_name = ?
            )
            ORDER BY b.id
            """,
            (source_name,),
        )
        book_ids = [r["id"] for r in await cursor.fetchall()]

        results = []
        for bid in book_ids:
            book_data = await self.get_book_by_id(bid)
            if book_data:
                identifiers = await self.get_existing_identifiers(bid)
                results.append((bid, book_data, identifiers))

        return results

    async def mark_enriched(self, book_id: int, source_name: str) -> None:
        """Record that a book has been enriched by the given source."""
        await self._conn.execute(
            "INSERT OR IGNORE INTO book_enrichment_log (book_id, source_name) VALUES (?, ?)",
            (book_id, source_name),
        )
        await self._conn.commit()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _upsert_author(self, name: str) -> int:
        """Upsert an author by normalised name and return the author id."""
        norm = normalise_author_name(name)
        cursor = await self._conn.execute(
            "SELECT id FROM authors WHERE normalised_name = ?", (norm,)
        )
        row = await cursor.fetchone()
        if row:
            return row["id"]

        cursor = await self._conn.execute(
            "INSERT INTO authors (name, normalised_name) VALUES (?, ?)",
            (name, norm),
        )
        return cursor.lastrowid

    async def _upsert_genre(self, name: str) -> int:
        """Upsert a genre by name and return the genre id."""
        cursor = await self._conn.execute(
            "SELECT id FROM genres WHERE name = ?", (name,)
        )
        row = await cursor.fetchone()
        if row:
            return row["id"]

        cursor = await self._conn.execute(
            "INSERT INTO genres (name) VALUES (?)", (name,)
        )
        return cursor.lastrowid
