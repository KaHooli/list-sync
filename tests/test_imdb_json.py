"""Exercise the browser-free IMDb extraction against realistic page shapes."""
import json
import sys
import types

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
def stub(name, attrs=()):
    m = types.ModuleType(name)
    for a in attrs: setattr(m, a, type(a, (), {}))
    sys.modules[name] = m
    return m
for n in ("seleniumbase", "bs4", "halo"):
    try: __import__(n)
    except ImportError: stub(n, ("SB", "BeautifulSoup", "Halo"))
c = stub("cryptography"); f = stub("cryptography.fernet", ("Fernet", "InvalidToken")); c.fernet = f
d = stub("dotenv"); d.load_dotenv = lambda *a, **k: None; d.set_key = lambda *a, **k: None

import logging
logging.disable(logging.CRITICAL)

from list_sync.providers.imdb import (
    _extract_titles_from_html, _resolve_imdb_url, fetch_imdb_list_via_http,
)
import list_sync.providers.imdb as imdb

fail = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fail.append(label)


def page(payload, script_id='__NEXT_DATA__'):
    return (b'<html><body>only a few rendered</body><script id="' + script_id.encode() +
            b'" type="application/json">' + json.dumps(payload).encode() +
            b'</script></html>')


# --- modern GraphQL-ish shape, as IMDb currently nests things --------------
modern = {"props": {"pageProps": {"mainColumnData": {"predefinedList": {"titleListItemSearch": {
    "edges": [
        {"listItem": {
            "id": "tt0145487",
            "titleText": {"text": "Spider-Man"},
            "releaseYear": {"year": 2002},
            "titleType": {"id": "movie", "text": "Movie", "isSeries": False},
        }},
        {"listItem": {
            "id": "tt4574334",
            "titleText": {"text": "Stranger Things"},
            "releaseYear": {"year": 2016},
            "titleType": {"id": "tvSeries", "text": "TV Series", "isSeries": True},
        }},
        {"listItem": {
            "id": "tt10872600",
            "titleText": {"text": "Spider-Man: No Way Home"},
            "releaseYear": {"year": 2021},
            "titleType": {"id": "movie", "text": "Movie", "isSeries": False},
        }},
    ]}}}}}}

items = _extract_titles_from_html(page(modern))
by_id = {i["imdb_id"]: i for i in items}

check("modern: count", len(items), 3)
check("modern: title", by_id["tt0145487"]["title"], "Spider-Man")
check("modern: year", by_id["tt0145487"]["year"], 2002)
check("modern: movie", by_id["tt0145487"]["media_type"], "movie")
check("modern: tv detected", by_id["tt4574334"]["media_type"], "tv")
check("modern: tv year", by_id["tt4574334"]["year"], 2016)

# --- flat CSV-ish / legacy shape -------------------------------------------
legacy = {"list": {"items": [
    {"const": "tt0111161", "primaryTitle": "The Shawshank Redemption", "year": 1994,
     "titleType": "movie"},
    {"const": "tt0903747", "primaryTitle": "Breaking Bad", "startYear": "2008",
     "titleType": "tvSeries"},
]}}
items = _extract_titles_from_html(page(legacy))
by_id = {i["imdb_id"]: i for i in items}
check("legacy: count", len(items), 2)
check("legacy: title", by_id["tt0111161"]["title"], "The Shawshank Redemption")
check("legacy: year int", by_id["tt0111161"]["year"], 1994)
check("legacy: year str", by_id["tt0903747"]["year"], 2008)
check("legacy: tv from string type", by_id["tt0903747"]["media_type"], "tv")

# --- the whole list is present even though few are rendered ----------------
big = {"props": {"pageProps": {"items": [
    {"id": f"tt{i:07d}", "titleText": {"text": f"Film {i}"}, "releaseYear": {"year": 2000}}
    for i in range(1, 251)
]}}}
items = _extract_titles_from_html(page(big))
check("large list fully recovered", len(items), 250)

# --- duplicates across the tree collapse, richest record wins --------------
dupes = {"a": [{"id": "tt0145487"}],
         "b": [{"id": "tt0145487", "titleText": {"text": "Spider-Man"},
                "releaseYear": {"year": 2002}}]}
items = _extract_titles_from_html(page(dupes))
check("dupes collapse", len(items), 1)
check("dupes keep the titled record", items[0]["title"], "Spider-Man")

# --- IMDbReactInitialState fallback ----------------------------------------
legacy_html = (b'<html><script>window.IMDbReactInitialState = '
               b'{"items":[{"const":"tt0068646","primaryTitle":"The Godfather","year":1972}]};'
               b'</script></html>')
items = _extract_titles_from_html(legacy_html)
check("IMDbReactInitialState parsed", len(items), 1)
check("IMDbReactInitialState title", items[0]["title"], "The Godfather")

# --- graceful failure -------------------------------------------------------
check("empty html", _extract_titles_from_html(b""), [])
check("no json", _extract_titles_from_html(b"<html>nothing here</html>"), [])
check("malformed json", _extract_titles_from_html(page({}) .replace(b'{}', b'{not json')), [])
check("json without titles", _extract_titles_from_html(page({"a": {"b": [1, 2, 3]}})), [])

# ids that merely look close are rejected
check("rejects short ids", _extract_titles_from_html(page({"id": "tt123"})), [])
check("rejects nm ids", _extract_titles_from_html(page({"id": "nm0000158"})), [])

# --- URL resolution ---------------------------------------------------------
check("watchlist url", _resolve_imdb_url("ur171928620"),
      ("https://www.imdb.com/user/ur171928620/watchlist", False))
check("list url", _resolve_imdb_url("ls123456789"),
      ("https://www.imdb.com/list/ls123456789", False))
check("chart url", _resolve_imdb_url("top"), ("https://www.imdb.com/chart/top", True))
check("passthrough url", _resolve_imdb_url("https://www.imdb.com/list/ls999/"),
      ("https://www.imdb.com/list/ls999", False))
try:
    _resolve_imdb_url("garbage")
    check("rejects garbage", "no raise", "ValueError")
except ValueError:
    check("rejects garbage", "ValueError", "ValueError")

# --- the http wrapper returns None so the caller falls back -----------------
def clear_backoff():
    imdb._DIRECT_FETCH_BLOCKED_UNTIL = 0.0

clear_backoff()
imdb._http_get = lambda url, timeout=30: None
check("http failure -> None", fetch_imdb_list_via_http("https://www.imdb.com/list/ls1"), None)

clear_backoff()
imdb._http_get = lambda url, timeout=30: b"<html>no data</html>"
check("no titles -> None", fetch_imdb_list_via_http("https://www.imdb.com/list/ls1"), None)

clear_backoff()
imdb._http_get = lambda url, timeout=30: page(modern)
got = fetch_imdb_list_via_http("https://www.imdb.com/list/ls1")
check("success -> items", len(got or []), 3)

# --- backoff: one failure suppresses further attempts, then expires ---------
clear_backoff()
attempts = []
def counting_get(url, timeout=30):
    attempts.append(url)
    return None

imdb._http_get = counting_get
for _ in range(4):
    fetch_imdb_list_via_http("https://www.imdb.com/list/ls1")
check("backoff: 4 lists cost 1 request", len(attempts), 1)

# once the window passes it probes again rather than staying off forever
imdb._DIRECT_FETCH_BLOCKED_UNTIL = 0.0
fetch_imdb_list_via_http("https://www.imdb.com/list/ls1")
check("backoff expires -> retries", len(attempts), 2)

# a success must not leave a stale block behind
clear_backoff()
imdb._http_get = lambda url, timeout=30: page(modern)
fetch_imdb_list_via_http("https://www.imdb.com/list/ls1")
check("success leaves no block", imdb._DIRECT_FETCH_BLOCKED_UNTIL, 0.0)

print()
print("FAILED:", fail if fail else "none")
sys.exit(1 if fail else 0)
