"""Book identity resolution (deduplication).

Called after parse_book() returns a BookData.  Returns book_id or None.
"""

import logging

from book_metadata_scraper.db.repository import Repository
from book_metadata_scraper.models import BookData
from book_metadata_scraper.normalise import normalise_author_name

logger = logging.getLogger(__name__)

# Identifier priority order for lookup
_IDENTIFIER_PRIORITY = ["isbn13", "isbn", "asin", "goodreads"]


async def find_existing_book(book_data: BookData, repo: Repository) -> int | None:
    """Determine whether ``book_data`` already exists in the database.

    1. Try identifier lookup in priority order (isbn13, isbn, asin, goodreads).
    2. Fall back to exact title + author normalised name match.
    3. Return None if the book is new.

    Returns the book_id if found, or None.
    """
    # Step 1: identifier lookup
    for id_type in _IDENTIFIER_PRIORITY:
        id_value = book_data.identifiers.get(id_type)
        if id_value:
            existing_id = await repo.find_book_by_identifier(id_type, id_value)
            if existing_id is not None:
                logger.debug(
                    "Book matched by %s=%s -> book_id=%d",
                    id_type,
                    id_value,
                    existing_id,
                )
                return existing_id

    # Step 2: title + author fallback
    normalised_names = [
        normalise_author_name(a.name) for a in book_data.authors
    ]
    if book_data.title and normalised_names:
        existing_id = await repo.find_book_by_title_and_author(
            book_data.title, normalised_names
        )
        if existing_id is not None:
            logger.debug(
                "Book matched by title+author '%s' -> book_id=%d",
                book_data.title,
                existing_id,
            )
            return existing_id

    # Step 3: new book
    return None
