"""
Open Library provider for ListSync.
"""

import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from ..utils.url_safety import assert_safe_url
from . import check_and_raise_if_cancelled, register_provider

OPENLIBRARY_HOSTS = ("openlibrary.org",)
BASE_URL = "https://openlibrary.org"

# The reading log shelves Open Library serves publicly, mapped from the names
# people are used to elsewhere.
READING_LOG_SHELVES = {
    "want-to-read": "want-to-read",
    "to-read": "want-to-read",
    "currently-reading": "currently-reading",
    "reading": "currently-reading",
    "already-read": "already-read",
    "read": "already-read",
}

PAGE_SIZE = 100
MAX_PAGES = 50

WORK_ID = re.compile(r"OL\d+W", re.IGNORECASE)
EDITION_ID = re.compile(r"OL\d+M", re.IGNORECASE)
LIST_ID = re.compile(r"OL\d+L", re.IGNORECASE)
AUTHOR_ID = re.compile(r"OL\d+A", re.IGNORECASE)

HEADERS = {"User-Agent": "ListSync/1.0 (+https://github.com/Soluify/list-sync)"}


def parse_openlibrary_list_id(list_id: str) -> Tuple[str, str, str]:
    """
    Work out which Open Library collection a configured list refers to.

    Accepts:
      jane/OL123L                                        - a user's list
      jane:want-to-read                                  - a reading log shelf
      https://openlibrary.org/people/jane/lists/OL123L
      https://openlibrary.org/people/jane/books/want-to-read

    Args:
        list_id (str): Configured list identifier

    Returns:
        Tuple[str, str, str]: (kind, Open Library username, list ID or shelf)
            where kind is "list" or "shelf"

    Raises:
        ValueError: If the value names neither a list nor a reading log shelf
    """
    raw = str(list_id or "").strip().strip("/")
    if not raw:
        raise ValueError("No Open Library list configured")

    if raw.startswith(("http://", "https://")):
        assert_safe_url(raw, allowed_hosts=OPENLIBRARY_HOSTS, what="Open Library URL")
        path = urlparse(raw).path.strip("/")
        parts = path.split("/")
        if len(parts) >= 4 and parts[0] == "people":
            user, section, value = parts[1], parts[2], parts[3]
            if section == "lists" and LIST_ID.fullmatch(value):
                return "list", user, value.upper()
            if section == "books":
                shelf = READING_LOG_SHELVES.get(value.lower())
                if shelf:
                    return "shelf", user, shelf
        raise ValueError(
            f"'{raw}' is not an Open Library list or reading log. Use a link like "
            f"{BASE_URL}/people/jane/lists/OL123L or {BASE_URL}/people/jane/books/want-to-read."
        )

    separator = "/" if "/" in raw else ":"
    user, _, value = raw.partition(separator)
    user, value = user.strip(), value.strip()
    if not user or not value:
        raise ValueError(
            f"'{raw}' is missing either the Open Library username or the list. Use "
            f"'username/OL123L' for a list, or 'username:want-to-read' for a reading log."
        )

    if LIST_ID.fullmatch(value):
        return "list", user, value.upper()

    shelf = READING_LOG_SHELVES.get(value.lower())
    if shelf:
        return "shelf", user, shelf

    raise ValueError(
        f"'{value}' is neither an Open Library list ID (OL123L) nor a reading log shelf "
        f"({', '.join(sorted(set(READING_LOG_SHELVES.values())))})."
    )


def _get_json(url: str, params: Optional[Dict[str, Any]] = None) -> Any:
    """Fetch one Open Library JSON document."""
    response = requests.get(url, params=params, headers=HEADERS, timeout=30)

    if response.status_code == 404:
        raise ValueError(f"Open Library has nothing at {url} - check the username and list.")

    response.raise_for_status()
    try:
        return response.json()
    except ValueError as e:
        raise ValueError(f"Open Library returned a non-JSON answer for {url} ({e})") from e


def _work_id_for_edition(edition_id: str) -> Optional[str]:
    """
    Resolve an edition to the work Seerr requests against.

    Seerr identifies a book by its work, so an entry that names only an edition
    (which a hand-built list often does) has to be looked up once.
    """
    try:
        data = _get_json(f"{BASE_URL}/books/{edition_id}.json")
    except (ValueError, requests.exceptions.RequestException) as e:
        logging.warning(f"Could not resolve Open Library edition {edition_id}: {e}")
        return None

    for work in (data.get("works") or []):
        match = WORK_ID.search(str(work.get("key", "")))
        if match:
            return match.group(0).upper()
    return None


def _item_from_seed(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Turn one entry of a list's seeds into a book item."""
    key = str(entry.get("url") or entry.get("key") or "")
    title = (entry.get("title") or "").strip()

    work_match = WORK_ID.search(key)
    edition_id = None
    if work_match:
        work_id = work_match.group(0).upper()
    else:
        edition_match = EDITION_ID.search(key)
        if not edition_match:
            # Subjects and authors can be seeded into a list too; neither names
            # a single book, so there is nothing to request.
            logging.debug(f"Skipping Open Library seed that is not a book: {key or entry}")
            return None
        edition_id = edition_match.group(0).upper()
        work_id = _work_id_for_edition(edition_id)
        if not work_id:
            return None

    if not title:
        return None

    return {
        "title": title,
        "media_type": "book",
        "year": None,
        "author": None,
        "openlibrary_id": work_id,
        "openlibrary_edition_id": edition_id,
    }


def _item_from_reading_log(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Turn one reading log entry into a book item."""
    work = entry.get("work") if isinstance(entry.get("work"), dict) else entry
    match = WORK_ID.search(str(work.get("key", "")))
    title = (work.get("title") or "").strip()
    if not match or not title:
        return None

    authors = work.get("author_names") or work.get("authors") or []
    author = None
    if authors:
        first = authors[0]
        author = first.get("name") if isinstance(first, dict) else str(first)

    author_keys = work.get("author_keys") or []
    author_id = None
    if author_keys:
        author_match = AUTHOR_ID.search(str(author_keys[0]))
        author_id = author_match.group(0).upper() if author_match else None

    edition_match = EDITION_ID.search(str(entry.get("logged_edition") or ""))

    return {
        "title": title,
        "media_type": "book",
        "year": work.get("first_publish_year"),
        "author": author,
        "openlibrary_id": match.group(0).upper(),
        "openlibrary_edition_id": edition_match.group(0).upper() if edition_match else None,
        "openlibrary_author_id": author_id,
    }


@register_provider("openlibrary")
def fetch_openlibrary_list(list_id: str) -> List[Dict[str, Any]]:
    """
    Fetch the books on a public Open Library list or reading log shelf.

    Open Library IDs are what Seerr identifies books by, so these lists need no
    title matching at all - each entry names exactly the book to request.

    Args:
        list_id (str): "user/OL123L", "user:want-to-read", or a list/shelf URL

    Returns:
        List[Dict[str, Any]]: List of book items

    Raises:
        ValueError: If the list ID is unusable or the list is not public
    """
    kind, user, value = parse_openlibrary_list_id(list_id)
    logging.info(f"Fetching Open Library {kind} '{value}' for user {user}")

    media_items: List[Dict[str, Any]] = []
    seen_work_ids = set()

    for page in range(MAX_PAGES):
        check_and_raise_if_cancelled()

        if kind == "list":
            payload = _get_json(
                f"{BASE_URL}/people/{user}/lists/{value}/seeds.json",
                {"limit": PAGE_SIZE, "offset": page * PAGE_SIZE},
            )
            entries = payload.get("entries") if isinstance(payload, dict) else None
            build = _item_from_seed
        else:
            payload = _get_json(
                f"{BASE_URL}/people/{user}/books/{value}.json",
                {"page": page + 1},
            )
            entries = payload.get("reading_log_entries") if isinstance(payload, dict) else None
            build = _item_from_reading_log

        if not entries:
            break

        new_on_page = 0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            item = build(entry)
            if not item or item["openlibrary_id"] in seen_work_ids:
                continue
            seen_work_ids.add(item["openlibrary_id"])
            media_items.append(item)
            new_on_page += 1

        logging.info(f"Open Library {kind} '{value}': page {page + 1} added {new_on_page} book(s)")

        # Open Library answers past the end by repeating the last page, so a
        # page holding nothing new means the list has been read to the end.
        if new_on_page == 0 or len(entries) < PAGE_SIZE:
            break

    if not media_items:
        logging.warning(f"Open Library {kind} '{value}' for user {user} is empty")

    logging.info(f"Open Library {kind} '{value}' fetched successfully. Found {len(media_items)} books.")
    return media_items
