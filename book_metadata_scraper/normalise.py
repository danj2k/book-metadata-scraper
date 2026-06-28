import re


def normalise_author_name(name: str) -> str:
    """Normalise an author name into a deduplication key.

    Lowercase, strip leading/trailing whitespace, then replace all runs of
    dots and/or spaces with a single hyphen.

    Examples:
        "J.R.R. Tolkien"  -> "j-r-r-tolkien"
        "Le Carré, John"  -> "le-carré,-john"   (punctuation other than dots preserved)
        "  Ann Leckie  "  -> "ann-leckie"

    Characters other than dots and spaces (commas, accented characters,
    apostrophes) are preserved intentionally. The goal is a stable key
    for matching, not a slug for use in URLs.
    """
    name = name.strip().lower()
    name = re.sub(r'[.\s]+', '-', name)
    name = re.sub(r'-{2,}', '-', name)  # collapse accidental double hyphens
    name = name.strip('-')  # remove any leading/trailing hyphens
    return name
