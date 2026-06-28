from dataclasses import dataclass, field


@dataclass
class AuthorData:
    """An author (or contributor) associated with a book."""

    name: str
    role: str = "author"


@dataclass
class BookData:
    """Book metadata extracted by a source plugin.

    This is the lingua franca between source plugins and the database layer.
    It is not an ORM model — just a plain data container.
    """

    title: str
    authors: list[AuthorData] = field(default_factory=list)
    subtitle: str | None = None
    description: str | None = None
    publisher: str | None = None
    published_date: str | None = None  # ISO 8601: "YYYY-MM-DD", "YYYY-MM", or "YYYY"
    page_count: int | None = None
    language: str | None = None  # BCP 47 tag, e.g. "en", "fr"
    series: str | None = None
    series_position: float | None = None
    cover_image_url: str | None = None
    genres: list[str] = field(default_factory=list)
    identifiers: dict[str, str] = field(default_factory=dict)
    source_url: str | None = None  # The canonical URL this book was scraped from
