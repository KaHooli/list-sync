"""
Goodreads bookshelf provider for ListSync.
"""

import logging
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import requests

from ..utils.url_safety import assert_safe_url
from . import check_and_raise_if_cancelled, register_provider

GOODREADS_HOSTS = ("goodreads.com",)

# Goodreads' own default shelf, and the one a "want to read" list means.
DEFAULT_SHELF = "to-read"

# The RSS feed caps out at 100 reviews per request; the page cap is a
# stop-loss against a feed that keeps answering with the same page.
PER_PAGE = 100
MAX_PAGES = 100

# Goodreads appends the series to the title ("Dune (Dune, #1)"). Seerr matches
# against Open Library, where the series is not part of the title, so the
# suffix has to come off before the book can be found.
SERIES_SUFFIX = re.compile(r"\s*\((?:[^()]*,\s*)?#[\d.\-]+\)\s*$")


def parse_goodreads_list_id(list_id: str) -> Tuple[str, str]:
    """
    Work out which Goodreads user and shelf a configured list refers to.

    Accepts the forms a user is likely to have to hand:
      19281606                    - numeric user ID, "to-read" shelf
      19281606:read               - user ID and shelf
      https://www.goodreads.com/review/list/19281606?shelf=read
      https://www.goodreads.com/user/show/19281606-jane

    Args:
        list_id (str): Configured list identifier

    Returns:
        Tuple[str, str]: (numeric Goodreads user ID, shelf name)

    Raises:
        ValueError: If no Goodreads user ID can be found in the value
    """
    raw = str(list_id or "").strip()
    if not raw:
        raise ValueError("No Goodreads shelf configured")

    if raw.startswith(("http://", "https://")):
        assert_safe_url(raw, allowed_hosts=GOODREADS_HOSTS, what="Goodreads URL")
        parsed = urlparse(raw)
        shelf = (parse_qs(parsed.query).get("shelf") or [DEFAULT_SHELF])[0].strip() or DEFAULT_SHELF
        # Both /review/list/19281606-jane and /user/show/19281606-jane start
        # the useful part of the path with the numeric ID.
        match = re.search(r"/(?:review/list(?:_rss)?|user/show)/(\d+)", parsed.path)
        if not match:
            raise ValueError(
                f"Could not find a Goodreads user ID in '{raw}'. Use the numeric ID from "
                f"your profile URL, or a link to your shelf."
            )
        return match.group(1), shelf

    user_part, _, shelf_part = raw.partition(":")
    # A profile ID can be typed as "19281606-jane"; only the digits matter.
    user_id = re.match(r"\d+", user_part.strip())
    if not user_id:
        raise ValueError(
            f"'{raw}' is not a Goodreads user ID. Use the numeric ID from your profile URL "
            f"(goodreads.com/user/show/19281606-jane), optionally followed by ':shelf'."
        )

    return user_id.group(0), shelf_part.strip() or DEFAULT_SHELF


def clean_book_title(title: str) -> str:
    """
    Strip the series suffix Goodreads adds to a book's title.

    Args:
        title (str): Title as it appears on the shelf

    Returns:
        str: Title without its trailing "(Series, #n)", when one is present
    """
    cleaned = SERIES_SUFFIX.sub("", str(title or "")).strip()
    # Never hand back an empty title just because the whole thing looked like
    # a series suffix.
    return cleaned or str(title or "").strip()


def _text(item: ET.Element, tag: str) -> Optional[str]:
    """Read one child element's text, if it has any."""
    node = item.find(tag)
    if node is None or node.text is None:
        return None
    value = node.text.strip()
    return value or None


def _parse_year(value: Optional[str]) -> Optional[int]:
    """Turn a Goodreads publication year into an int, ignoring junk."""
    if not value:
        return None
    match = re.search(r"\d{4}", value)
    if not match:
        return None
    year = int(match.group(0))
    return year if 1000 <= year <= 2999 else None


def _fetch_page(user_id: str, shelf: str, page: int) -> List[ET.Element]:
    """
    Fetch one page of a shelf's RSS feed.

    The RSS feed is used rather than the HTML shelf because it is a documented,
    paginated endpoint that carries the ISBN and publication year outright - no
    browser, and no scraping of a layout that changes.
    """
    url = f"https://www.goodreads.com/review/list_rss/{user_id}"
    response = requests.get(
        url,
        params={"shelf": shelf, "per_page": PER_PAGE, "page": page},
        headers={"User-Agent": "ListSync/1.0 (+https://github.com/Soluify/list-sync)"},
        timeout=30,
    )

    if response.status_code == 404:
        raise ValueError(
            f"Goodreads has no public shelf '{shelf}' for user {user_id}. "
            f"Check the shelf name, and that the profile's shelves are public."
        )

    response.raise_for_status()

    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as e:
        raise ValueError(
            f"Goodreads returned something that is not an RSS feed for user {user_id} "
            f"shelf '{shelf}' - the profile is probably private ({e})"
        ) from e

    return root.findall("./channel/item")


@register_provider("goodreads")
def fetch_goodreads_list(list_id: str) -> List[Dict[str, Any]]:
    """
    Fetch the books on a public Goodreads shelf.

    Args:
        list_id (str): Goodreads user ID, "user_id:shelf", or a shelf URL

    Returns:
        List[Dict[str, Any]]: List of book items

    Raises:
        ValueError: If the list ID is unusable or the shelf is not public
    """
    user_id, shelf = parse_goodreads_list_id(list_id)
    logging.info(f"Fetching Goodreads shelf '{shelf}' for user {user_id}")

    media_items: List[Dict[str, Any]] = []
    seen_book_ids = set()

    for page in range(1, MAX_PAGES + 1):
        check_and_raise_if_cancelled()

        items = _fetch_page(user_id, shelf, page)
        if not items:
            break

        new_on_page = 0
        for item in items:
            title = clean_book_title(_text(item, "title") or "")
            if not title:
                continue

            book_id = _text(item, "book_id")
            # Goodreads answers past the last page by repeating it, so a page
            # with nothing new on it means the shelf has been read to the end.
            if book_id:
                if book_id in seen_book_ids:
                    continue
                seen_book_ids.add(book_id)
            new_on_page += 1

            media_items.append({
                "title": title,
                "media_type": "book",
                "year": _parse_year(_text(item, "book_published")),
                "author": _text(item, "author_name"),
                "isbn": _text(item, "isbn"),
                "goodreads_id": book_id,
            })

        logging.info(f"Goodreads shelf '{shelf}': page {page} added {new_on_page} book(s)")

        if new_on_page == 0 or len(items) < PER_PAGE:
            break

    if not media_items:
        logging.warning(f"Goodreads shelf '{shelf}' for user {user_id} is empty")

    logging.info(f"Goodreads shelf '{shelf}' fetched successfully. Found {len(media_items)} books.")
    return media_items
