"""
IMDb list provider for ListSync.
"""

import gzip
import io
import json
import logging
import re
import time
import urllib.error
import urllib.request
import zlib
from typing import List, Dict, Any, Optional

from seleniumbase import SB

from . import register_provider, check_and_raise_if_cancelled, SyncCancelledException

# IMDb renders lists client-side but ships the whole list as JSON in the initial
# HTML response. Reading that is one HTTP request with no browser, no CSS
# selectors and no pagination, so it's tried before falling back to Selenium.
_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_IMDB_ID_RE = re.compile(r"^tt\d{7,9}$")
_IMDB_ID_ANYWHERE_RE = re.compile(rb"tt\d{7,9}")

# IMDb sits behind AWS WAF, which answers unrecognised clients with a
# JavaScript challenge rather than the page. Naming it in the log saves the
# next person working out why a perfectly good HTTP request returns 2KB.
_AWS_WAF_RE = re.compile(rb"awswaf|AwsWafIntegration|gokuProps", re.IGNORECASE)

# Keys IMDb has used for a title's ID, its display title, and its year. Several
# are checked because the exact shape changes between page rewrites; the walker
# below doesn't depend on where in the tree these appear.
_ID_KEYS = ("id", "const", "titleId", "tconst")
_TITLE_KEYS = ("titleText", "originalTitleText", "primaryTitle", "title", "text", "name")
_YEAR_KEYS = ("releaseYear", "year", "startYear")

# IMDb title types that are episodic
_TV_TITLE_TYPES = {
    "tvseries", "tvminiseries", "tvepisode", "tvspecial", "tvshort",
}


# IMDb answers plain HTTP clients with a small bot-check interstitial rather
# than the list. Once that's been seen, back off for a while: retrying wastes a
# request per list and puts avoidable bot-detection pressure on the same IP the
# browser fetch is about to use. Time-boxed rather than permanent so a
# long-lived process picks it up again if IMDb's behaviour changes, without
# needing a restart.
_DIRECT_FETCH_BLOCKED_UNTIL = 0.0
_DIRECT_FETCH_BACKOFF_SECONDS = 30 * 60

# An interstitial is short and carries no titles; a real list page is far
# larger. Used only to label the log line, never to decide correctness.
_INTERSTITIAL_MAX_BYTES = 20_000


def _http_get(url: str, timeout: int = 30) -> Optional[bytes]:
    """
    Fetch a URL with a browser-like User-Agent, returning None on any failure.

    Any problem here is non-fatal: the caller falls back to the browser path.
    """
    request = urllib.request.Request(url, headers=_BROWSER_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            encoding = (response.headers.get("Content-Encoding") or "").lower()
            if encoding == "gzip":
                raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            elif encoding == "deflate":
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)

            # A 2xx that is too small to be a list page is the bot check.
            if response.status != 200 or len(raw) < _INTERSTITIAL_MAX_BYTES:
                if not _IMDB_ID_ANYWHERE_RE.search(raw or b""):
                    if _AWS_WAF_RE.search(raw or b""):
                        logging.info(
                            f"IMDb served an AWS WAF JavaScript challenge (HTTP "
                            f"{response.status}, {len(raw)} bytes) instead of the list. "
                            f"Solving it requires executing challenge.js to obtain an "
                            f"aws-waf-token, so a real browser is needed - falling back "
                            f"to Selenium. Swapping the TLS fingerprint does not help."
                        )
                    else:
                        logging.info(
                            f"IMDb answered the direct fetch with HTTP {response.status} and "
                            f"{len(raw)} bytes containing no titles - this is its bot check, "
                            f"not the list. Using the browser instead."
                        )
                    return None

            return raw
    except urllib.error.HTTPError as e:
        logging.info(f"IMDb direct fetch returned HTTP {e.code} for {url}")
        return None
    except Exception as e:
        logging.info(f"IMDb direct fetch failed for {url}: {type(e).__name__}: {e}")
        return None


def _first_string(node: Any, keys) -> Optional[str]:
    """
    Read the first usable string from a node, following IMDb's habit of
    wrapping display text one level down (e.g. {"titleText": {"text": "..."}}).
    """
    if not isinstance(node, dict):
        return None
    for key in keys:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for inner in ("text", "value", "title"):
                nested = value.get(inner)
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
    return None


def _first_year(node: Any) -> Optional[int]:
    """Read a release year from a node, unwrapping IMDb's nested shapes."""
    if not isinstance(node, dict):
        return None
    for key in _YEAR_KEYS:
        value = node.get(key)
        if isinstance(value, int) and 1800 < value < 2200:
            return value
        if isinstance(value, str) and value[:4].isdigit():
            return int(value[:4])
        if isinstance(value, dict):
            for inner in ("year", "text", "value"):
                nested = value.get(inner)
                if isinstance(nested, int) and 1800 < nested < 2200:
                    return nested
                if isinstance(nested, str) and nested[:4].isdigit():
                    return int(nested[:4])
    return None


def _media_type_from_node(node: Any) -> str:
    """Classify a title node as tv or movie, defaulting to movie."""
    if isinstance(node, dict):
        title_type = node.get("titleType")
        raw = None
        if isinstance(title_type, str):
            raw = title_type
        elif isinstance(title_type, dict):
            raw = title_type.get("id") or title_type.get("text")
            if title_type.get("isSeries") is True:
                return "tv"
        if isinstance(raw, str) and raw.replace(" ", "").replace("_", "").lower() in _TV_TITLE_TYPES:
            return "tv"
    return "movie"


def _walk_for_titles(node: Any, found: Dict[str, Dict[str, Any]], depth: int = 0) -> None:
    """
    Walk arbitrary JSON collecting every title that carries an IMDb ID.

    Deliberately schema-agnostic: IMDb reshuffles where the list lives between
    page rewrites, and pinning the path is what makes scrapers brittle. Anything
    shaped like a title is collected wherever it sits in the tree.
    """
    if depth > 30:
        return

    if isinstance(node, list):
        for child in node:
            _walk_for_titles(child, found, depth + 1)
        return

    if not isinstance(node, dict):
        return

    # Does this node identify a title?
    imdb_id = None
    for key in _ID_KEYS:
        value = node.get(key)
        if isinstance(value, str) and _IMDB_ID_RE.match(value):
            imdb_id = value
            break

    if imdb_id:
        title = _first_string(node, _TITLE_KEYS)
        year = _first_year(node)
        media_type = _media_type_from_node(node)

        existing = found.get(imdb_id)
        # Keep the richest record seen for a given ID - the same title can
        # appear both as a bare reference and as a full node.
        if existing is None or (title and not existing.get("title")):
            found[imdb_id] = {
                "title": title,
                "imdb_id": imdb_id,
                "media_type": media_type,
                "year": year,
            }
        elif existing and year and not existing.get("year"):
            existing["year"] = year

    for child in node.values():
        _walk_for_titles(child, found, depth + 1)


def _extract_titles_from_html(html: bytes) -> List[Dict[str, Any]]:
    """
    Pull every title out of the JSON IMDb embeds in a list page.

    Returns [] when nothing usable is found, which tells the caller to fall
    back to the browser.
    """
    if not html:
        return []

    found: Dict[str, Dict[str, Any]] = {}

    blocks = []
    match = re.search(rb'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if match:
        blocks.append(match.group(1))
    blocks.extend(re.findall(rb'<script[^>]+type="application/json"[^>]*>(.*?)</script>', html, re.DOTALL))
    match = re.search(rb'IMDbReactInitialState[^=]*=\s*(\{.*?\});\s*</script>', html, re.DOTALL)
    if match:
        blocks.append(match.group(1))

    for block in blocks:
        try:
            data = json.loads(block.decode("utf-8", "replace"))
        except (ValueError, AttributeError):
            continue
        _walk_for_titles(data, found)

    if found:
        return list(found.values())

    # Nothing parsed. If the raw HTML nonetheless carries many title IDs the
    # data is there in a shape this doesn't understand - say so loudly rather
    # than silently falling back, because that's a cheap fix worth making.
    raw_ids = {m.decode() for m in _IMDB_ID_ANYWHERE_RE.findall(html)}
    if len(raw_ids) > 5:
        logging.warning(
            f"IMDb page contains {len(raw_ids)} title IDs but none were in a recognised "
            f"JSON block - the embedded format has changed. Falling back to the browser."
        )

    return []


def fetch_imdb_list_via_http(url: str) -> Optional[List[Dict[str, Any]]]:
    """
    Try to read an IMDb list with a single HTTP request and no browser.

    Args:
        url (str): Full IMDb list, watchlist or chart URL

    Returns:
        Optional[List[Dict[str, Any]]]: Media items, or None if this route
            didn't work and the caller should fall back to Selenium.
    """
    global _DIRECT_FETCH_BLOCKED_UNTIL

    now = time.monotonic()
    if now < _DIRECT_FETCH_BLOCKED_UNTIL:
        logging.debug(
            f"Skipping IMDb direct fetch - backing off for another "
            f"{int(_DIRECT_FETCH_BLOCKED_UNTIL - now)}s"
        )
        return None

    logging.info(f"Trying IMDb direct fetch (no browser): {url}")
    html = _http_get(url)
    if not html:
        _DIRECT_FETCH_BLOCKED_UNTIL = now + _DIRECT_FETCH_BACKOFF_SECONDS
        return None

    items = _extract_titles_from_html(html)
    if not items:
        _DIRECT_FETCH_BLOCKED_UNTIL = now + _DIRECT_FETCH_BACKOFF_SECONDS
        return None

    with_titles = sum(1 for i in items if i.get("title"))
    logging.info(
        f"IMDb direct fetch recovered {len(items)} title(s) "
        f"({with_titles} with a display title) - no browser needed"
    )
    return items


def _resolve_imdb_url(list_id: str):
    """
    Turn a list ID, chart name or URL into a full IMDb URL.

    Args:
        list_id (str): IMDb list ID, chart name, or URL

    Returns:
        tuple: (url, is_chart)

    Raises:
        ValueError: If the list ID or URL format is not recognised
    """
    if list_id.startswith(('http://', 'https://')):
        url = list_id.rstrip('/')
        if '/chart/' in url:
            return url, True
        if '/list/' in url or '/user/' in url:
            return url, False
        raise ValueError("Invalid IMDb URL format")

    if list_id in ['top', 'boxoffice', 'moviemeter', 'tvmeter']:
        return f"https://www.imdb.com/chart/{list_id}", True
    if list_id.startswith("ls"):
        return f"https://www.imdb.com/list/{list_id}", False
    if list_id.startswith("ur"):
        return f"https://www.imdb.com/user/{list_id}/watchlist", False

    raise ValueError("Invalid IMDb list ID format")


@register_provider("imdb")
def fetch_imdb_list(list_id: str) -> List[Dict[str, Any]]:
    """
    Fetch IMDb list using Selenium with pagination
    
    Args:
        list_id (str): IMDb list ID, chart name, or URL
        
    Returns:
        List[Dict[str, Any]]: List of media items
        
    Raises:
        ValueError: If list ID format is invalid
    """
    media_items = []
    logging.info(f"Fetching IMDb list: {list_id}")

    url, is_chart = _resolve_imdb_url(list_id)

    # Prefer the browser-free route. It reads the JSON IMDb embeds in the page,
    # which avoids Chrome, the CSS selectors and the pagination entirely.
    # Anything less than a clean result falls through to Selenium below.
    try:
        direct_items = fetch_imdb_list_via_http(url)
    except Exception as e:
        logging.warning(f"IMDb direct fetch raised, falling back to browser: {e}")
        direct_items = None

    if direct_items:
        return direct_items

    logging.info("Falling back to browser-based IMDb scrape")

    try:
        with SB(uc=True, headless=True) as sb:
            logging.info(f"Attempting to load URL: {url}")
            sb.open(url)
            
            # Common wait logic for all IMDb pages (both lists and charts)
            logging.info(f"Attempting to load IMDb page: {url}")
            sb.open(url)
            
            # Initial wait for page load
            sb.sleep(5)  # Longer initial wait to ensure page starts loading
            
            # Add some human-like scrolling behavior to avoid bot detection
            try:
                sb.execute_script("window.scrollTo(0, 300);")
                sb.sleep(1)
                sb.execute_script("window.scrollTo(0, 600);")
                sb.sleep(1)
            except Exception as e:
                logging.warning(f"Could not perform scrolling: {str(e)}")
            
            # Wait for any potential captcha/anti-bot verification to load
            sb.sleep(3)
            
            # Check for cancellation before processing
            check_and_raise_if_cancelled()
            
            if is_chart:
                # Process chart page (IMDb top charts)
                media_items.extend(_process_imdb_chart(sb))
            else:
                # Process regular list page
                media_items.extend(_process_imdb_list(sb, url))
            
            logging.info(f"Found {len(media_items)} items from IMDb list {list_id}")
            return media_items
    
    except SyncCancelledException:
        logging.warning(f"⚠️ IMDb list fetch cancelled by user - returning {len(media_items)} items fetched so far")
        raise
        
    except Exception as e:
        logging.error(f"Error fetching IMDb list {list_id}: {str(e)}")
        raise


def _process_imdb_chart(sb) -> List[Dict[str, Any]]:
    """
    Process an IMDb chart page.
    
    Args:
        sb: SeleniumBase instance
        
    Returns:
        List[Dict[str, Any]]: List of media items
    """
    media_items = []
    chart_found = False
    
    # Try different approaches to find chart content
    # First, try direct data-testid selectors
    data_testid_selectors = [
        '[data-testid="chart-layout-parent"]',
        '[data-testid="chart-layout-main-column"]',
        '[data-testid="chart-layout-total-items"]'
    ]
    
    for selector in data_testid_selectors:
        try:
            logging.info(f"Trying to find chart with data-testid selector: {selector}")
            # Use a longer timeout for charts
            sb.wait_for_element_present(selector, timeout=10)
            chart_found = True
            logging.info(f"Chart parent found with selector: {selector}")
            # Add extra wait after finding the element to ensure it's fully loaded
            sb.sleep(2)
            break
        except Exception as e:
            logging.warning(f"Could not find chart with data-testid selector {selector}: {str(e)}")
    
    # If not found, try different class-based selectors for the list itself
    if not chart_found:
        class_selectors = [
            'ul.ipc-metadata-list.compact-list-view',
            'ul.ipc-metadata-list.detailed-list-view',
            '.ipc-metadata-list.ipc-metadata-list--dividers-between',
            'ul.ipc-metadata-list'  # Most generic one
        ]
        
        for selector in class_selectors:
            try:
                logging.info(f"Trying to find chart with class selector: {selector}")
                # Use a longer timeout for charts
                sb.wait_for_element_present(selector, timeout=10)
                chart_found = True
                logging.info(f"Chart found with selector: {selector}")
                # Add extra wait after finding the element
                sb.sleep(2)
                break
            except Exception as e:
                logging.warning(f"Could not find chart with class selector {selector}: {str(e)}")
    
    if not chart_found:
        # Try a more aggressive approach with longer waits and more scrolling
        logging.warning("Could not find chart with standard selectors, trying more aggressive approach")
        sb.sleep(8)  # Wait longer for full page load
        
        # Add more extensive human-like behavior
        sb.execute_script("window.scrollTo(0, 300);")
        sb.sleep(2)
        sb.execute_script("window.scrollTo(0, 600);")
        sb.sleep(2)
        sb.execute_script("window.scrollTo(0, 900);")
        sb.sleep(2)
        sb.execute_script("window.scrollTo(0, 1200);")
        sb.sleep(2)
        # Scroll back up a bit to simulate natural browsing
        sb.execute_script("window.scrollTo(0, 800);")
        sb.sleep(3)
        
        # Try a very generic selector that should match any list with a much longer timeout
        try:
            sb.wait_for_element_present('ul', timeout=15)
            # If we find any ul, let's look for list items inside it
            uls = sb.find_elements('ul')
            for ul in uls:
                try:
                    items = ul.find_elements("css selector", "li")
                    if len(items) > 5:  # If we find a list with several items, it's likely our chart
                        logging.debug(f"Found a ul with {len(items)} items, likely our chart")
                        chart_found = True
                        break
                except Exception:
                    pass
        except Exception as e:
            logging.error(f"Could not find any ul elements after scrolling: {str(e)}")
    
    if not chart_found:
        raise ValueError("Could not find chart content on IMDb page after multiple attempts")
    
    # Get total number of items if possible
    try:
        total_elements = sb.find_elements('[data-testid="chart-layout-total-items"]')
        if total_elements:
            total_text = total_elements[0].text
            total_match = re.search(r'(\d+)\s+Titles?', total_text)
            if total_match:
                total_items = int(total_match.group(1))
                logging.info(f"Total items in chart: {total_items}")
        else:
            total_items = None
    except Exception as e:
        logging.warning(f"Could not determine total items: {str(e)}")
        total_items = None
    
    # Process items in the chart - try multiple selectors for items
    items = []
    
    # Try different selectors for the chart items, starting with the most specific
    item_selectors = [
        "li.ipc-metadata-list-summary-item",  # Most specific for compact view
        ".ipc-metadata-list-summary-item",    # Alternative for compact view
        ".cli-parent",                        # From the example
        ".ipc-metadata-list-item"             # For other views
    ]
    
    for selector in item_selectors:
        try:
            logging.debug(f"Trying to find list items with selector: {selector}")
            items = sb.find_elements(selector)
            if items and len(items) > 0:
                logging.info(f"Found {len(items)} items using selector: {selector}")
                break
        except Exception as e:
            logging.warning(f"Could not find items with selector {selector}: {str(e)}")
    
    if not items or len(items) == 0:
        # Last resort: try to find any list items on the page
        try:
            items = sb.find_elements("li")
            logging.warning(f"Using generic li selector as fallback, found {len(items)} items")
        except Exception as e:
            logging.error(f"Could not find any list items on the page: {str(e)}")
            raise ValueError("Could not find any list items in the chart")
    
    logging.info(f"Found {len(items)} items in chart")
    
    # Check for cancellation before processing items
    check_and_raise_if_cancelled()
    
    for idx, item in enumerate(items):
        # Check for cancellation every 10 items
        if idx > 0 and idx % 10 == 0:
            check_and_raise_if_cancelled()
        try:
            # Get title element - try multiple selectors
            title_element = None
            full_title = ""
            
            title_selectors = [
                ".ipc-title__text",
                "h3.ipc-title__text",
                ".cli-title h3",
                "a.ipc-title-link-wrapper"
            ]
            
            for selector in title_selectors:
                try:
                    title_elements = item.find_elements("css selector", selector)
                    if title_elements:
                        for element in title_elements:
                            element_text = element.text
                            if element_text and len(element_text) > 0:
                                full_title = element_text
                                logging.info(f"Found title using selector: {selector}")
                                break
                        if full_title:
                            break
                except Exception:
                    pass
            
            if not full_title:
                # Last resort: try to get any text from the item
                try:
                    item_text = item.text
                    text_lines = item_text.split("\n")
                    for line in text_lines:
                        if line and len(line) > 2 and not line.isdigit() and not re.match(r'^\d+\.$', line):
                            full_title = line
                            logging.warning(f"Using fallback method for title: {full_title}")
                            break
                except Exception:
                    pass
            
            if not full_title:
                logging.warning("Could not find title for item, skipping")
                continue
            
            # Remove ranking number if present (e.g., "1. The Shawshank Redemption" -> "The Shawshank Redemption")
            title = re.sub(r'^\d+\.\s*', '', full_title)
            
            # Get year from metadata with various selectors
            year = None
            metadata_text = ""
            
            metadata_selectors = [
                ".cli-title-metadata",
                ".cli-title-metadata-item",
                ".sc-44e0e03-6",
                ".sc-44e0e03-7"
            ]
            
            for selector in metadata_selectors:
                try:
                    metadata_elements = item.find_elements("css selector", selector)
                    if metadata_elements:
                        for element in metadata_elements:
                            element_text = element.text
                            metadata_text += " " + element_text
                            # Try to extract year directly from this element
                            year_match = re.search(r'(\d{4})', element_text)
                            if year_match:
                                year = int(year_match.group(1))
                                break
                        if year:
                            break
                except Exception:
                    pass
            
            if not year and metadata_text:
                # Try to extract year from concatenated metadata
                year_match = re.search(r'(\d{4})', metadata_text)
                if year_match:
                    year = int(year_match.group(1))
            
            # If still no year, try to extract from the item's entire text
            if not year:
                try:
                    item_text = item.text
                    year_match = re.search(r'(\d{4})', item_text)
                    if year_match:
                        year = int(year_match.group(1))
                except Exception:
                    pass
            
            # Get IMDB ID from the title link - try different approaches
            imdb_id = None
            link_selectors = [
                "a.ipc-title-link-wrapper",
                "a[href*='/title/']",
                "a"  # Most generic selector
            ]
            
            for selector in link_selectors:
                try:
                    links = item.find_elements("css selector", selector)
                    for link in links:
                        href = link.get_attribute("href")
                        if href and "/title/" in href:
                            # Extract IMDb ID from URL
                            imdb_match = re.search(r'/title/(tt\d+)', href)
                            if imdb_match:
                                imdb_id = imdb_match.group(1)
                                break
                    if imdb_id:
                        break
                except Exception:
                    pass
            
            if not imdb_id:
                logging.warning(f"Could not find IMDb ID for {title}, skipping")
                continue
            
            # For charts, all items are movies unless explicitly marked as TV
            media_type = "movie"
            if metadata_text and ("TV" in metadata_text or "Series" in metadata_text):
                media_type = "tv"
            
            media_items.append({
                "title": title.strip(),
                "imdb_id": imdb_id,
                "media_type": media_type,
                "year": year
            })
            logging.info(f"Added {media_type}: {title} ({year}) (IMDB ID: {imdb_id})")
            
        except Exception as e:
            logging.warning(f"Failed to parse IMDb chart item: {str(e)}")
            continue
    
    return media_items


def _process_imdb_list(sb, url) -> List[Dict[str, Any]]:
    """
    Process a regular IMDb list page.
    
    Args:
        sb: SeleniumBase instance
        url: URL of the list
        
    Returns:
        List[Dict[str, Any]]: List of media items
    """
    media_items = []
    
    try:
        # Wait for list content to load with a more resilient approach
        logging.info("Waiting for list content to load...")
        
        # Try multiple selector approaches in sequence with longer timeouts
        content_found = False
        
        # First try the data-testid attribute
        try:
            logging.info("Looking for content by data-testid attribute...")
            sb.wait_for_element_present('[data-testid="list-page-mc-list-content"]', timeout=10)
            content_found = True
            logging.info("Found content by data-testid attribute")
            # Add extra wait after finding the element
            sb.sleep(2)
        except Exception as e:
            logging.warning(f"Could not find content by data-testid: {str(e)}")
        
        # If that fails, try looking for the list element directly
        if not content_found:
            try:
                logging.info("Looking for list element directly...")
                sb.wait_for_element_present("ul.ipc-metadata-list", timeout=10)
                content_found = True
                logging.info("Found list element directly")
                # Add extra wait after finding the element
                sb.sleep(2)
            except Exception as e:
                logging.warning(f"Could not find list element: {str(e)}")
        
        # If that also fails, try looking for list items
        if not content_found:
            try:
                logging.info("Looking for list items...")
                sb.wait_for_element_present("li.ipc-metadata-list-summary-item", timeout=10)
                content_found = True
                logging.debug("Found list items")
                # Add extra wait after finding the element
                sb.sleep(2)
            except Exception as e:
                logging.warning(f"Could not find list items: {str(e)}")
        
        # If everything fails, try a more aggressive approach with longer waits and scrolling
        if not content_found:
            logging.warning("Could not find list content, attempting more aggressive approach...")
            # Scroll more and wait longer
            sb.sleep(8)
            
            # Add more extensive human-like behavior
            sb.execute_script("window.scrollTo(0, 300);")
            sb.sleep(2)
            sb.execute_script("window.scrollTo(0, 600);")
            sb.sleep(2)
            sb.execute_script("window.scrollTo(0, 900);")
            sb.sleep(2)
            sb.execute_script("window.scrollTo(0, 1200);")
            sb.sleep(2)
            # Scroll back up a bit to simulate natural browsing
            sb.execute_script("window.scrollTo(0, 800);")
            sb.sleep(3)
            
            # Reload the page to handle potential temporary glitches
            sb.open(url)
            sb.sleep(10)  # Wait longer after reload
            
            # Try once more with very generic selectors and longer timeouts
            try:
                # Try to find any ul element with items
                sb.wait_for_element_present("ul", timeout=15)
                uls = sb.find_elements("ul")
                for ul in uls:
                    try:
                        items = ul.find_elements("css selector", "li")
                        if len(items) > 5:  # If we find a list with several items, it's likely our list
                            logging.info(f"Found a ul with {len(items)} items, likely our list content")
                            content_found = True
                            break
                    except Exception:
                        pass
                
                if not content_found:
                    raise ValueError("Could not find list content on IMDb page after multiple attempts")
            except Exception as e:
                logging.error(f"Could not find any list content after multiple attempts: {str(e)}")
                raise ValueError("Could not find list content on IMDb page after multiple attempts")
        
        # Additional wait to ensure everything is loaded
        sb.sleep(3)
    
    except Exception as e:
        logging.error(f"Failed to load IMDb list page: {str(e)}")
        raise
    
    # Get total number of items
    try:
        # Try to find the container with the total items count using exact classes from HTML
        titles_container = sb.find_element("css selector", ".ipc-inline-list__item.sc-d6269c7a-1")
        if titles_container:
            total_text = titles_container.text
            # Extract the number from text like "500 titles"
            titles_match = re.search(r'(\d+)\s*titles?', total_text, re.IGNORECASE)
            if titles_match:
                total_items = int(titles_match.group(1))
                logging.info(f"Total items in list: {total_items}")
                expected_pages = (total_items + 249) // 250  # Round up division by 250
                logging.info(f"Expected number of pages: {expected_pages}")
            else:
                # Try another approach - find the text showing range like "1 - 250"
                range_container = sb.find_element("css selector", ".ipc-inline-list__item")
                range_text = range_container.text
                logging.info(f"Found range text: {range_text}")
                if " - " in range_text:
                    # This is like "1 - 250"
                    try:
                        _, end = range_text.split(" - ")
                        per_page = int(end)
                        logging.info(f"Items per page: {per_page}")
                        # Find total in the next element
                        next_item = sb.find_element("css selector", ".ipc-inline-list__item.sc-d6269c7a-1")
                        if next_item:
                            titles_text = next_item.text
                            titles_match = re.search(r'(\d+)', titles_text)
                            if titles_match:
                                total_items = int(titles_match.group(1))
                                expected_pages = (total_items + per_page - 1) // per_page
                                logging.info(f"Total items: {total_items}, expected pages: {expected_pages}")
                    except Exception as e:
                        logging.warning(f"Could not parse range: {str(e)}")
                        total_items = None
                        expected_pages = None
                else:
                    total_items = None
                    expected_pages = None
        else:
            logging.warning("Total items container not found")
            total_items = None
            expected_pages = None
    except Exception as e:
        logging.warning(f"Could not determine total items using new selector: {str(e)}")
        # Fallback to original selector
        try:
            total_element = sb.find_element('[data-testid="list-page-mc-total-items"]')
            total_text = total_element.text
            total_items = int(re.search(r'(\d+)\s+titles?', total_text).group(1))
            logging.info(f"Total items in list (fallback): {total_items}")
            expected_pages = (total_items + 249) // 250  # Round up division by 250
        except Exception as e2:
            logging.warning(f"Could not determine total items with fallback: {str(e2)}")
            total_items = None
            expected_pages = None
    
    current_page = 1

    # Track what we've already collected so a page that doesn't actually
    # advance can be detected. IMDb watchlists paginate by infinite scroll and
    # ignore ?page=N, so the fallback navigation below re-serves the same page
    # forever; without this the loop never terminates and re-adds the same
    # titles on every pass.
    seen_imdb_ids = set()

    # Absolute ceiling, in case a list paginates in some way that defeats both
    # the expected-page count and the no-progress check. At 250 items per page
    # this still allows lists far larger than IMDb permits.
    MAX_PAGES = 200

    # Process items on the page
    while True:
        # Check for cancellation at the start of each page
        check_and_raise_if_cancelled()
        # Try multiple approaches to find list items
        items = []
        
        # First try using the most specific selector
        try:
            items = sb.find_elements("css selector", "li.ipc-metadata-list-summary-item")
            if items:
                logging.info(f"Found {len(items)} items using specific selector")
        except Exception as e:
            logging.warning(f"Could not find items using specific selector: {str(e)}")
        
        # If that fails, try a more generic selector
        if not items:
            try:
                items = sb.find_elements("css selector", ".ipc-metadata-list-summary-item")
                if items:
                    logging.info(f"Found {len(items)} items using generic class selector")
            except Exception as e:
                logging.warning(f"Could not find items using generic selector: {str(e)}")
        
        # If that also fails, try an even more generic approach
        if not items:
            try:
                # Try to find the list first
                list_element = sb.find_element("css selector", "ul.ipc-metadata-list")
                # Then get its children
                items = list_element.find_elements("css selector", "li")
                if items:
                    logging.info(f"Found {len(items)} items via parent list element")
            except Exception as e:
                logging.warning(f"Could not find items via parent: {str(e)}")
        
        logging.info(f"Processing page {current_page}: Found {len(items)} items")

        # Baseline for the no-progress check after this page is parsed
        items_before_page = len(media_items)

        if not items:
            logging.warning("No items found on this page, attempting to continue to next page")
            # We might need to try the next page
            if current_page < min(expected_pages or 2, MAX_PAGES):  # Try at least page 2 if we don't know expected pages
                # Try to navigate to next page directly
                next_page = current_page + 1
                next_url = f"{url}/?page={next_page}"
                logging.info(f"Attempting to navigate directly to page {next_page}: {next_url}")
                sb.open(next_url)
                sb.sleep(5)  # Wait longer for page load
                current_page += 1
                continue
            else:
                logging.info("No more pages expected, breaking loop")
                break
        
        for idx, item in enumerate(items):
            # Check for cancellation every 10 items
            if idx > 0 and idx % 10 == 0:
                check_and_raise_if_cancelled()
            
            try:
                # Get title element with multiple fallbacks
                title_element = None
                full_title = ""
                
                # Try different approaches to find the title
                selectors_to_try = [
                    ".ipc-title__text", 
                    "h3.ipc-title__text",
                    "a.ipc-title-link-wrapper h3",
                    ".dli-title h3"
                ]
                
                for selector in selectors_to_try:
                    try:
                        title_element = item.find_element("css selector", selector)
                        if title_element:
                            full_title = title_element.text
                            logging.info(f"Found title using selector: {selector}")
                            break
                    except Exception:
                        pass
                
                if not full_title:
                    # Last resort: try to get any text from the item
                    full_title = item.text.split("\n")[0]
                    logging.warning(f"Using fallback method for title: {full_title}")
                
                title = re.sub(r'^\d+\.\s*', '', full_title)  # Remove the numbering (e.g., "1. ")
                
                # Get year from metadata with multiple fallbacks
                year = None
                metadata_text = ""
                
                # Try different approaches to find the metadata
                metadata_selectors = [
                    ".sc-44e0e03-6.liNdun",
                    ".dli-title-metadata",
                    ".sc-44e0e03-6",
                    "[class*='title-metadata']"
                ]
                
                for selector in metadata_selectors:
                    try:
                        metadata = item.find_element("css selector", selector)
                        if metadata:
                            metadata_text = metadata.text
                            logging.info(f"Found metadata using selector: {selector}")
                            break
                    except Exception:
                        pass
                
                # Extract year if we found metadata text
                if metadata_text:
                    # Extract year from formats like "2008–2013" or "2024"
                    year_match = re.search(r'(\d{4})', metadata_text)
                    if year_match:
                        year = int(year_match.group(1))
                    logging.debug(f"Extracted year for {title}: {year}")
                
                # More robust media type detection
                media_type = "movie"  # default
                if metadata_text:
                    # Look for TV Series indicator in metadata text
                    if "TV Series" in metadata_text or "TV Mini Series" in metadata_text:
                        media_type = "tv"
                    # Or try to find episodes indicator
                    elif "eps" in metadata_text.lower() or "episodes" in metadata_text.lower():
                        media_type = "tv"
                
                # Get IMDB ID from the title link with multiple fallbacks
                imdb_id = None
                link_selectors = [
                    "a.ipc-title-link-wrapper",
                    "a[href*='/title/']",
                    ".dli-title a"
                ]
                
                for selector in link_selectors:
                    try:
                        title_link = item.find_element("css selector", selector)
                        if title_link:
                            href = title_link.get_attribute("href")
                            if href and "/title/" in href:
                                imdb_id = href.split("/")[4]
                                logging.info(f"Found IMDb ID using selector: {selector}")
                                break
                    except Exception:
                        pass
                
                if not imdb_id:
                    # Try to extract it from any href in the item
                    try:
                        links = item.find_elements("css selector", "a")
                        for link in links:
                            href = link.get_attribute("href")
                            if href and "/title/" in href:
                                imdb_id = href.split("/")[4]
                                logging.info("Found IMDb ID from generic link")
                                break
                    except Exception:
                        pass
                
                if not imdb_id:
                    logging.warning(f"Could not find IMDb ID for {title}, skipping")
                    continue
                
                if imdb_id in seen_imdb_ids:
                    # Already collected on an earlier page - the list re-served
                    # content we've seen rather than advancing.
                    continue

                seen_imdb_ids.add(imdb_id)
                media_items.append({
                    "title": title.strip(),
                    "imdb_id": imdb_id,
                    "media_type": media_type,
                    "year": year
                })
                logging.info(f"Added {media_type}: {title} ({year}) (IMDB ID: {imdb_id})")

            except Exception as e:
                logging.warning(f"Failed to parse IMDb item: {str(e)}")
                continue

        # If a page contributed nothing new, pagination isn't advancing. This
        # is the only stop condition that works when the total-item count
        # couldn't be parsed, which is the normal case for watchlists.
        new_this_page = len(media_items) - items_before_page
        if new_this_page == 0:
            logging.info(
                f"Page {current_page} added no new titles ({len(items)} item(s) all seen before) - "
                f"pagination is not advancing, stopping with {len(media_items)} item(s)"
            )
            break

        logging.info(f"Page {current_page} added {new_this_page} new title(s), {len(media_items)} total")

        # Check if we've processed all expected pages
        if expected_pages and current_page >= expected_pages:
            logging.info(f"Reached final page {current_page} of {expected_pages}")
            break

        if current_page >= MAX_PAGES:
            logging.warning(
                f"Stopping at the {MAX_PAGES}-page safety limit with {len(media_items)} item(s). "
                f"If this list really is larger, raise MAX_PAGES in the IMDb provider."
            )
            break

        # Try to navigate to next page
        try:
            # First try clicking the button using a more specific selector
            try:
                # Log the HTML structure to help debugging
                logging.info("Looking for next button...")
                pagination_element = sb.find_element("css selector", "div[data-testid='index-pagination']")
                if pagination_element:
                    logging.info("Pagination element found")
                
                next_button = sb.find_element(
                    "css selector", 
                    "button[data-testid='index-pagination-nxt']"
                )
                
                # Check if the button is disabled
                if next_button:
                    is_disabled = next_button.get_attribute("disabled")
                    logging.info(f"Next button found, disabled attribute: {is_disabled}")
                    if is_disabled:
                        logging.info("Next button is disabled, no more pages")
                        break
                    
                    # Button is enabled, so click it
                    sb.execute_script("arguments[0].scrollIntoView(true);", next_button)
                    sb.sleep(1)  # Give time for scrolling
                    next_button.click()
                    
                    # Wait for loading spinner to disappear and content to load
                    sb.wait_for_element_present('[data-testid="list-page-mc-list-content"]', timeout=10)
                    sb.sleep(3)  # Additional wait for content to fully render
                    
                    # Verify we have items on the page
                    new_items = sb.find_elements("css selector", "li.ipc-metadata-list-summary-item")
                    if not new_items:
                        logging.warning(f"No items found after navigation to page {current_page + 1}, retrying...")
                        # Fall back to direct URL navigation
                        next_page = current_page + 1
                        next_url = f"{url}/?page={next_page}"
                        sb.open(next_url)
                        sb.wait_for_element_present('[data-testid="list-page-mc-list-content"]', timeout=10)
                        sb.sleep(3)
            except Exception as e:
                logging.info(f"Could not click next button: {str(e)}")
                # Fall back to direct URL navigation
                next_page = current_page + 1
                next_url = f"{url}/?page={next_page}"
                logging.info(f"Attempting to navigate directly to page {next_page}: {next_url}")
                sb.open(next_url)
                sb.wait_for_element_present('[data-testid="list-page-mc-list-content"]', timeout=10)
                sb.sleep(3)
            
            current_page += 1
            sb.sleep(2)
        except Exception as e:
            logging.info(f"No more pages available: {str(e)}")
            break
    
    # Validate total items found
    if total_items and len(media_items) < total_items:
        logging.warning(f"Only found {len(media_items)} items out of {total_items} total")
    
    return media_items
