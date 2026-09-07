"""
Shared vocabulary for book lists.

Books are the one media type Seerr cannot always request: support for them
arrived with SeerrNG, which hands them to a Readarr-compatible "Bookshelf"
service (Chaptarr). This module holds the pieces the rest of the app needs to
talk about book lists - the request formats and which list types hold books -
without dragging in the API client or the provider modules.
"""

from typing import Optional

# Request formats. SeerrNG tracks the ebook and the audiobook copy of a book as
# separate requests, and "both" asks for one of each at once.
BOOK_FORMAT_EBOOK = "ebook"
BOOK_FORMAT_AUDIOBOOK = "audiobook"
BOOK_FORMAT_BOTH = "both"
BOOK_FORMATS = (BOOK_FORMAT_EBOOK, BOOK_FORMAT_AUDIOBOOK, BOOK_FORMAT_BOTH)

# What a book list asks for when nothing says otherwise. This matches the
# format Seerr itself assumes for a request that names none.
DEFAULT_BOOK_FORMAT = BOOK_FORMAT_EBOOK

# Which stored formats a request for a given format collides with, mirroring
# the overlap rules in SeerrNG's MediaRequest.request(). A "both" request
# covers - and is blocked by - either single format.
BOOK_FORMAT_OVERLAP = {
    BOOK_FORMAT_EBOOK: {BOOK_FORMAT_EBOOK, BOOK_FORMAT_BOTH},
    BOOK_FORMAT_AUDIOBOOK: {BOOK_FORMAT_AUDIOBOOK, BOOK_FORMAT_BOTH},
    BOOK_FORMAT_BOTH: {BOOK_FORMAT_EBOOK, BOOK_FORMAT_AUDIOBOOK, BOOK_FORMAT_BOTH},
}

# List types whose items are books rather than movies or TV. These are only
# offered when the connected Seerr can actually request books.
BOOK_PROVIDERS = ("goodreads", "openlibrary")

# The labels the UI shows, accepted anywhere a format is configured by hand.
_FORMAT_ALIASES = {
    "ebooks": BOOK_FORMAT_EBOOK,
    "e-book": BOOK_FORMAT_EBOOK,
    "e-books": BOOK_FORMAT_EBOOK,
    "audiobooks": BOOK_FORMAT_AUDIOBOOK,
    "audio": BOOK_FORMAT_AUDIOBOOK,
    "all": BOOK_FORMAT_BOTH,
}


def normalize_book_format(value: Optional[str], default: str = DEFAULT_BOOK_FORMAT) -> str:
    """
    Coerce a configured book format to one Seerr accepts.

    Args:
        value (Optional[str]): Format from a list, setting or API payload
        default (str): Format to fall back to when the value is missing or junk

    Returns:
        str: One of "ebook", "audiobook" or "both"
    """
    candidate = str(value or "").strip().lower()
    if candidate in BOOK_FORMATS:
        return candidate
    if candidate in _FORMAT_ALIASES:
        return _FORMAT_ALIASES[candidate]
    return default if default in BOOK_FORMATS else DEFAULT_BOOK_FORMAT


def is_book_provider(provider_type: Optional[str]) -> bool:
    """
    Whether a list type holds books.

    Args:
        provider_type (Optional[str]): Type of list (e.g. 'imdb', 'goodreads')

    Returns:
        bool: True for book list types
    """
    return str(provider_type or "").lower() in BOOK_PROVIDERS


def format_label(book_format: Optional[str]) -> str:
    """
    Describe a book format the way the UI does, for log lines and messages.

    Args:
        book_format (Optional[str]): Format to describe

    Returns:
        str: "eBooks", "Audiobooks" or "Audiobooks + eBooks"
    """
    normalized = normalize_book_format(book_format)
    return {
        BOOK_FORMAT_EBOOK: "eBooks",
        BOOK_FORMAT_AUDIOBOOK: "Audiobooks",
        BOOK_FORMAT_BOTH: "Audiobooks + eBooks",
    }[normalized]
